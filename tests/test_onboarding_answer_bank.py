"""Onboarding 2.7 — the answer bank is built from the human's answers only:
never inherited from the example file (its identity is a fictional person),
EEO only from an explicit choice (hard rule 4), salary history never
answered (hard rule 5), legal acknowledgements only for approved labels."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from jobkit.autofill.answer_bank import BankMatcher, approved_acknowledgement
from jobkit.onboarding import mcp_tools
from jobkit.onboarding.answer_bank_builder import (
    EEO_KEYS,
    build_answer_bank,
    questions_text,
)
from jobkit.onboarding.files import GENERATED_MARK, SetupError

# A new user's answers: a second fictional person (Morgan Ellis), never the
# template's Jordan Rivera, so anything inherited from the template shows up.
REQUIRED: dict[str, Any] = {
    "identity.first_name": "Morgan",
    "identity.last_name": "Ellis",
    "identity.email": "morgan.ellis@example.org",
    "identity.phone": "555 010 0142",
    "identity.city": "Denver",
    "identity.state": "CO",
    "identity.country": "United States",
    "screening.work_auth": {"value": "yes", "said": "Yes, I'm a US citizen"},
    "screening.sponsorship": "no",
    "screening.notice_period": "Two weeks",
    "screening.start_date": "Two weeks after an offer",
    "screening.relocation": "No, staying in Denver",
    "screening.onsite_ok": "Yes, in Denver, up to 2 days a week",
    "screening.non_compete": "no",
    "screening.background_check": "yes",
    "screening.references": "Available on request",
    "comp_expectation": {"policy": "number", "amount": 105000, "floor": 95000},
}


def _bank(home: Path) -> dict[str, Any]:
    data = yaml.safe_load((home / "data" / "answer_bank.yaml").read_text())
    assert isinstance(data, dict)
    return data


def _screening(bank: dict[str, Any], eid: str) -> dict[str, Any]:
    return next(s for s in bank["screening"] if s["id"] == eid)


def test_nothing_is_inherited_from_the_example(jobkit_home: Path) -> None:
    out = build_answer_bank({"identity.first_name": "Morgan"})
    text = (jobkit_home / "data" / "answer_bank.yaml").read_text()
    assert text.startswith(GENERATED_MARK)
    for example_value in ("Jordan", "Rivera", "Bristol", "jordan.rivera", "555 0100"):
        assert example_value not in text
    bank = _bank(jobkit_home)
    assert bank["identity"]["first_name"] == "Morgan"
    assert bank["identity"]["city"] == ""  # unanswered: blank, never the example's
    for s in bank["screening"]:
        if s["id"] != "salary_history":
            assert s.get("answer") is None, s
    assert "still required" in out and "screening.work_auth" in out


def test_answers_build_a_bank_the_matcher_reads(jobkit_home: Path) -> None:
    out = build_answer_bank(REQUIRED)
    assert "the bank is complete" in out
    bank = _bank(jobkit_home)
    assert _screening(bank, "work_auth")["answer"] == "Yes"
    assert _screening(bank, "sponsorship")["answer"] == "No"
    comp = _screening(bank, "comp_expectation")
    assert (comp["answer"], comp["floor"]) == ("105000", 95000)
    m = BankMatcher(bank=bank, use_embeddings=False)
    assert (
        m.match("Will you now or in the future require sponsorship?")["entry_id"] == "sponsorship"
    )
    assert m.match("First Name")["entry_id"] == "first_name"
    # what the human said is kept alongside
    record = yaml.safe_load(
        (jobkit_home / "data" / "setup" / "answer_bank.answers.yaml").read_text()
    )
    assert record["answers"]["screening.work_auth"]["said"] == "Yes, I'm a US citizen"


def test_salary_history_is_never_answered(jobkit_home: Path) -> None:
    build_answer_bank(REQUIRED)
    entry = _screening(_bank(jobkit_home), "salary_history")
    assert entry["answer"] == "" and entry["action"] == "leave_blank_and_flag"
    with pytest.raises(SetupError, match="unknown answer key"):
        build_answer_bank({"screening.salary_history": "120000"})


def test_eeo_only_from_an_explicit_choice(jobkit_home: Path) -> None:
    build_answer_bank(
        {
            **REQUIRED,
            "eeo.gender": "I don't wish to answer",
            "eeo.veteran_status": "I am not a protected veteran",
        }
    )
    bank = _bank(jobkit_home)
    assert set(bank["eeo"]) == set(EEO_KEYS)
    assert bank["eeo"]["gender"] == "I don't wish to answer"
    assert bank["eeo"]["race_ethnicity"] is None  # never answered: blank and flagged
    m = BankMatcher(bank=bank, use_embeddings=False)
    assert m.match("Gender", "eeo") == {
        "kind": "eeo",
        "entry_id": "gender",
        "answer": "I don't wish to answer",
        "source": "answer_bank",
    }
    assert m.match("Race", "eeo") == {"kind": "blank_flag", "reason": "eeo-no-stored-choice"}
    assert "race_ethnicity" in questions_text()


def test_legal_acknowledgements_are_off_until_opted_in(jobkit_home: Path) -> None:
    label = "Please read the arbitration agreement below"
    build_answer_bank(REQUIRED)
    bank = _bank(jobkit_home)
    assert not any(s["id"] == "legal_acknowledgement" for s in bank["screening"])
    build_answer_bank({"legal_acknowledgement": {"opt_in": True}})
    entry = _screening(_bank(jobkit_home), "legal_acknowledgement")
    assert entry["approved_labels"] == []  # empty by default: nothing ticked yet
    assert not approved_acknowledgement(_bank(jobkit_home), label)
    m = BankMatcher(bank=_bank(jobkit_home), use_embeddings=False)
    assert m.match(label)["kind"] == "blank_flag"
    build_answer_bank({"legal_acknowledgement": {"opt_in": True, "approved_labels": [label]}})
    assert approved_acknowledgement(_bank(jobkit_home), label)


@pytest.mark.parametrize(
    "answers, message",
    [
        ({"screening.work_auth": "maybe"}, "answer yes or no"),
        ({"identity.email": "not an email"}, "not an email address"),
        ({"comp_expectation": {"policy": "guess"}}, "policy must be one of"),
        ({"comp_expectation": {"policy": "number"}}, "amount must be a whole number"),
        ({"legal_acknowledgement": {"approved_labels": ["x"]}}, "opt_in"),
        ({"eeo.favorite_color": "blue"}, "unknown answer key"),
    ],
)
def test_bad_answers_save_nothing(jobkit_home: Path, answers: dict[str, Any], message: str) -> None:
    with pytest.raises(SetupError, match=message):
        build_answer_bank({"identity.first_name": "Morgan", **answers})
    assert not (jobkit_home / "data" / "answer_bank.yaml").exists()
    assert not (jobkit_home / "data" / "setup" / "answer_bank.answers.yaml").exists()


def test_answers_accumulate_over_sittings(jobkit_home: Path) -> None:
    build_answer_bank({"identity.first_name": "Morgan"})
    build_answer_bank({"screening.sponsorship": "no"})
    bank = _bank(jobkit_home)
    assert bank["identity"]["first_name"] == "Morgan"
    assert _screening(bank, "sponsorship")["answer"] == "No"


def test_comp_policies(jobkit_home: Path) -> None:
    build_answer_bank(
        {"comp_expectation": {"policy": "text", "text": "Open to discussing", "floor": 90000}}
    )
    comp = _screening(_bank(jobkit_home), "comp_expectation")
    assert (comp["answer"], comp["floor"]) == ("Open to discussing", 90000)
    build_answer_bank({"comp_expectation": {"policy": "leave_blank"}})
    comp = _screening(_bank(jobkit_home), "comp_expectation")
    assert comp["answer"] is None and comp["floor"] is None


def test_hand_written_bank_is_protected(jobkit_home: Path) -> None:
    bank = jobkit_home / "data" / "answer_bank.yaml"
    bank.write_text("# mine\nidentity: {first_name: Sam}\nscreening: []\neeo: {}\n")
    with pytest.raises(SetupError, match="not written by the setup interview"):
        build_answer_bank({"identity.first_name": "Morgan"})
    assert bank.read_text().startswith("# mine")
    build_answer_bank({"identity.first_name": "Morgan"}, replace_existing=True)
    kept = list((jobkit_home / "data" / "setup" / "backup").glob("answer_bank.yaml.*"))
    assert len(kept) == 1 and kept[0].read_text().startswith("# mine")


def test_residence_entries_when_answered(jobkit_home: Path) -> None:
    build_answer_bank(
        {
            "screening.country_of_residence": "United States",
            "screening.state_of_residence": "Colorado",
        }
    )
    bank = _bank(jobkit_home)
    assert _screening(bank, "country_of_residence")["answer"] == "United States"
    m = BankMatcher(bank=bank, use_embeddings=False)
    assert m.match("What is your state of residence?")["entry_id"] == "state_of_residence"


def test_mcp_wrappers(jobkit_home: Path) -> None:
    assert "identity.first_name" in mcp_tools.answer_bank_questions()
    out = mcp_tools.build_answer_bank('{"identity.first_name": "Morgan"}')
    assert "wrote data/answer_bank.yaml" in out
