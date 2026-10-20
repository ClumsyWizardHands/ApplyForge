"""Onboarding 2.1-2.6 — interview answers → config/filters.yaml and its
companions. Each interview-driven setting quotes the answer that produced it;
the generated file loads with the pipeline's own loader; a config the
interview did not write is never replaced silently."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from jobkit.autofill.answer_bank import BankMatcher, comp_expectation_block
from jobkit.config import load_filters, read_config_yaml
from jobkit.onboarding import mcp_tools
from jobkit.onboarding.answer_bank_builder import build_answer_bank
from jobkit.onboarding.files import GENERATED_MARK, SetupError
from jobkit.onboarding.intake import run_intake
from jobkit.pipeline.filter import reject_reason_now as _reject_reason
from jobkit.pipeline.triage import commute_minutes

ANSWERS: dict[str, Any] = {
    "tracks": [
        {
            "name": "Customer Success Manager",
            "titles": ["Customer Success Manager", "Customer Success Lead"],
            "comp_floor": 95000,
            "said": "CSM roles, at least 95k base",
        },
        {
            "name": "Implementation Manager",
            "comp_floor": 110000,
            "comp_basis": "ote",
            "said": "implementation if OTE is 110k+",
        },
    ],
    "exclude_titles": {"items": ["Collections"], "said": "never collections"},
    "seniority_floor": {"level": "experienced", "said": "nothing junior or entry level"},
    "locations": {
        "work_mode": "hybrid",
        "places": ["Denver", "Boulder"],
        "max_office_days_per_week": 2,
        "max_commute_minutes": 40,
        "commute_minutes": {"Denver": 20},
        "relocation_cities": ["Austin"],
        "said": "remote, or hybrid in Denver or Boulder, 2 days max",
    },
    "comp": {"default_floor": 90000, "said": "nothing under 90k"},
    "industries": {"wanted": ["health tech"], "refused": ["gambling"], "said": "no gambling"},
    "blocklist_companies": {"items": ["Harbor Health"], "said": "not going back there"},
}


@pytest.fixture()
def fresh(jobkit_home: Path) -> Path:
    """A clone with no personal config (the sandbox ships the test profile)."""
    for name in ("filters", "targets", "autoapprove", "banned_phrases", "commute_table"):
        (jobkit_home / "config" / f"{name}.yaml").unlink()
    return jobkit_home


def _job(title: str, location: str = "Remote", remote: bool | None = True) -> dict[str, Any]:
    return {
        "title": title,
        "company_name": "Acme",
        "location_raw": location,
        "remote": None if remote is None else int(remote),
        "posted_at": None,
        "comp_min": None,
        "comp_max": None,
        "comp_currency": None,
        "jd_html": "",
    }


def test_generated_filters_load_and_filter(fresh: Path) -> None:
    out = run_intake(ANSWERS)
    path = fresh / "config" / "filters.yaml"
    assert path.read_text().startswith(GENERATED_MARK)
    f = load_filters(path)
    assert [t.id for t in f.tracks] == ["customer_success_manager", "implementation_manager"]
    assert f.track_for("Senior Customer Success Manager, Mid-Market") == "customer_success_manager"
    assert f.track_for("Implementation Manager") == "implementation_manager"
    assert f.comp_floor == 90000
    cf = f.comp_floor_for("Implementation Manager")
    assert (cf.floor, cf.basis) == (110000, "ote")
    assert f.allowed_locations == ["Denver", "Boulder"] and f.relocation_cities == ["Austin"]
    assert f.accept_remote
    assert f.raw["filters"]["blocklist_companies"] == ["Harbor Health"]
    assert f.raw["remote_policy"]["max_office_days_per_week"] == 2
    # the seniority floor and the named exclusion reject; the tracks pass
    assert _reject_reason(_job("Junior Customer Success Manager"), f) is not None
    assert _reject_reason(_job("Associate Customer Success Manager"), f) is not None
    assert _reject_reason(_job("Collections Specialist"), f) is not None
    assert _reject_reason(_job("Customer Success Lead"), f) is None
    assert _reject_reason(_job("Customer Success Manager", "Denver, CO", False), f) is None
    assert _reject_reason(_job("Customer Success Manager", "Chicago, IL", False), f) is not None
    assert "no commute time for Boulder" in out


def test_every_interview_setting_quotes_the_answer(fresh: Path) -> None:
    run_intake(ANSWERS)
    text = (fresh / "config" / "filters.yaml").read_text()
    for said in (
        "CSM roles, at least 95k base",
        "implementation if OTE is 110k+",
        "never collections",
        "nothing junior or entry level",
        "remote, or hybrid in Denver or Boulder, 2 days max",
        "nothing under 90k",
        "not going back there",
        "no gambling",
    ):
        assert f'# you said: "{said}"' in text
    # no industry knob is invented: recorded as a comment only
    assert "industries" not in yaml.safe_load(text)["filters"]
    assert "#   refused: gambling" in text


def test_companion_files(fresh: Path) -> None:
    run_intake(ANSWERS)
    table = read_config_yaml(fresh / "config" / "commute_table.yaml")
    assert commute_minutes(table, "denver-metro") == 20
    assert commute_minutes(table, "other-boulder") is None
    rules = read_config_yaml(fresh / "config" / "autoapprove.yaml")
    assert rules["auto_approve"] == [] and rules["auto_reject"] == []  # none until accepted
    banned = read_config_yaml(fresh / "config" / "banned_phrases.yaml")
    assert banned["cover_letter"]["hard_reject"]
    record = yaml.safe_load((fresh / "data" / "setup" / "preferences.answers.yaml").read_text())
    assert record["answers"]["tracks"][0]["said"] == "CSM roles, at least 95k base"


def test_feeds_search_the_users_titles(fresh: Path) -> None:
    run_intake(ANSWERS)
    feeds = load_filters(fresh / "config" / "filters.yaml").raw["feeds"]
    assert feeds["adzuna"]["keywords"][:2] == ["Customer Success Manager", "Customer Success Lead"]
    assert "account executive" not in feeds["adzuna"]["keywords"]
    # not a sales search: no sales-only categories
    assert feeds["remotive"]["categories"] == [] and feeds["consider"]["categories"] == []
    assert feeds["getro"]["enabled"] is False  # hard rule 11


def test_remote_only(fresh: Path) -> None:
    answers = copy.deepcopy(ANSWERS)
    answers["locations"] = {"work_mode": "remote_only", "said": "remote only"}
    run_intake(answers)
    f = load_filters(fresh / "config" / "filters.yaml")
    assert f.allowed_locations == []
    assert f.raw["remote_policy"]["max_office_days_per_week"] == 0
    assert f.raw["remote_policy"]["reject_onsite"] is True


@pytest.mark.parametrize(
    "change, message",
    [
        ({"locations": {"work_mode": "remote_only", "places": ["Denver"]}}, "takes no places"),
        (
            {"locations": {"work_mode": "hybrid", "places": ["Denver"], "max_commute_minutes": 30}},
            "max_office_days",
        ),
        ({"locations": {"work_mode": "onsite_ok"}}, "needs at least one place"),
        ({"tracks": []}, "tracks"),
        ({"nightly_at": "2am"}, "HH:MM"),
        ({"unknown_section": 1}, "unknown_section"),
        (
            {"tracks": [{"name": "Associate Account Manager"}]},
            "would be rejected by the exclude",
        ),
        ({"tracks": [{"name": "X", "title_regex": "(?i)(unclosed"}]}, "does not compile"),
        # no default floor: refused (asked), never written as `floor: 0`
        ({"comp": {"said": "no floor in mind"}}, "comp.default_floor: ask the floor"),
        ({"comp": {"default_floor": 0}}, "comp.default_floor: ask the floor"),
    ],
)
def test_bad_answers_write_nothing(fresh: Path, change: dict[str, Any], message: str) -> None:
    answers = {**copy.deepcopy(ANSWERS), **change}
    with pytest.raises(SetupError, match=message):
        run_intake(answers)
    assert not (fresh / "config" / "filters.yaml").exists()


def test_existing_config_is_protected(jobkit_home: Path) -> None:
    # the sandbox's test profile stands in for a config written by hand
    path = jobkit_home / "config" / "filters.yaml"
    before = path.read_text()
    with pytest.raises(SetupError, match="not written by the setup interview"):
        run_intake(ANSWERS)
    assert path.read_text() == before
    run_intake(ANSWERS, replace_existing=True)
    kept = list((jobkit_home / "data" / "setup" / "backup").glob("filters.yaml.*"))
    assert len(kept) == 1 and kept[0].read_text() == before
    # hand-written companions are never replaced
    auto = jobkit_home / "config" / "autoapprove.yaml"
    assert not auto.read_text().startswith(GENERATED_MARK)


def test_rerun_rewrites_its_own_files(fresh: Path) -> None:
    run_intake(ANSWERS)
    answers = copy.deepcopy(ANSWERS)
    answers["comp"]["default_floor"] = 100000
    run_intake(answers)  # its own output: no replace_existing needed
    assert load_filters(fresh / "config" / "filters.yaml").comp_floor == 100000


def _bank(home: Path) -> dict[str, Any]:
    data = yaml.safe_load((home / "data" / "answer_bank.yaml").read_text())
    assert isinstance(data, dict)
    return data


def test_intake_and_answer_bank_agree_on_comp_expectation(fresh: Path) -> None:
    """The filters run_intake writes and the bank build_answer_bank writes,
    read by the form-filling gate: the salary-expectation figure goes on a
    job in a track whose floor is at or above the default floor."""
    run_intake(ANSWERS)
    build_answer_bank({"comp_expectation": {"policy": "number", "amount": 120000, "floor": 100000}})
    bank = _bank(fresh)
    f = load_filters(fresh / "config" / "filters.yaml")
    assert f.comp_floor == 90000
    # both tracks' floors (95000 base, 110000 OTE) are at or above the default
    for title in ("Customer Success Manager", "Customer Success Lead", "Implementation Manager"):
        assert comp_expectation_block(title, None, bank, f) is None, title
    matcher = BankMatcher(bank, use_embeddings=False)
    hit = matcher.match(
        "What are your salary expectations?",
        comp_expectation_block=comp_expectation_block("Customer Success Manager", None, bank, f),
    )
    assert hit == {"kind": "screening", "entry_id": "comp_expectation", "source": "answer_bank"}
    # the posting's own ceiling under the bank's floor still blocks
    assert comp_expectation_block("Customer Success Manager", 90000, bank, f) is not None


def test_track_floor_under_the_default_is_named(fresh: Path) -> None:
    answers = copy.deepcopy(ANSWERS)
    answers["comp"]["default_floor"] = 100000
    out = run_intake(answers)
    # Customer Success Manager's 95000 is under the 100000 default: the gate
    # blanks its salary-expectation field, and run_intake says so
    assert "track customer_success_manager: its floor (95,000) is under your default" in out
    assert "track implementation_manager" not in out
    build_answer_bank({"comp_expectation": {"policy": "number", "amount": 120000}})
    f = load_filters(fresh / "config" / "filters.yaml")
    block = comp_expectation_block("Customer Success Manager", None, _bank(fresh), f)
    assert block == (
        "comp-expectation-needs-human:track-customer_success_manager-floor-95000-below-100000"
    )
    assert comp_expectation_block("Implementation Manager", None, _bank(fresh), f) is None


def test_writing_style_answer(fresh: Path) -> None:
    answers = {**ANSWERS, "writing_style": {"estimate_style": "confident", "said": "state it"}}
    run_intake(answers)
    banned = read_config_yaml(fresh / "config" / "banned_phrases.yaml")
    assert banned["drift"]["estimate_style"] == "confident"


def test_mcp_wrapper(fresh: Path) -> None:
    import json

    out = mcp_tools.run_intake(json.dumps(ANSWERS))
    assert "config/filters.yaml" in out
