"""A drafted denial is a claim (claim-judge review 2026-10-10).

"No." to "Have you ever been terminated or asked to resign?", "Have you ever
been convicted of a felony?" or "Have you ever been placed on a PIP?" states a
fact about the candidate's past that the corpus never records. The
rule-based check cannot see it (it reads numbers, names and employers), and
version 1.0 of the judge prompt told the judge never to report "a plain
denial". Such a draft passed both gates, was stored in
form_specs.drafted_answers and filled at staging as 'ai_drafted'.

Two gates now stand in front of it, and both fail closed:
- never_draft (autofill/answer_bank.py) keeps record and status questions
  (employment record, criminal record, restrictive covenants, work status)
  away from the drafter at capture and blanks them again at staging; the
  bank's own screening entries still answer the wordings they match;
- the claim judge treats any denial as a claim, and a judge "fail" keeps the
  draft out of the form like any fact-check failure.

Offline: every LLM call is a fake (tests/test_claims.FakeLLM)."""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from jobkit import llm
from jobkit.autofill import browser, form_spec
from jobkit.autofill.answer_bank import BankMatcher, never_draft
from jobkit.autofill.browser import staging_match
from jobkit.validate.claims import drafting_profile
from tests.test_claims import (
    CORPUS,
    FABRIKAM_JD,
    JUDGE_PASS,
    KNOWN,
    FakeLLM,
    _check,
    _kinds,
    flagging_judge,
    judged_text,
)

# the three questions of the review's offline repro
DENIAL_QUESTIONS = [
    "Have you ever been terminated or asked to resign?",
    "Have you ever been convicted of a felony?",
    "Have you ever been placed on a PIP?",
]
DENIALS = ["No.", "No, I have not.", "N/A"]

FABRIKAM_JOB = {
    "id": 1,
    "title": "Director of Revenue Operations",
    "company_name": "Fabrikam Payments",
    "jd_html": f"<p>{FABRIKAM_JD}</p>",
}


# ---- the rules alone cannot see a denial: the judge and never_draft must ----


@pytest.mark.parametrize("question", DENIAL_QUESTIONS)
@pytest.mark.parametrize("answer", DENIALS)
def test_the_rules_alone_pass_a_denial_the_corpus_never_records(question: str, answer: str) -> None:
    """The blind spot this file closes (like tests/test_claims.BLIND_SPOT): if
    the rule-based check learns to fail these, keep the judge tests anyway."""
    assert _check(answer, [], question=question) == []


# ---- the judge fails a denial, and the draft is never returned ----


@pytest.mark.parametrize("question", DENIAL_QUESTIONS)
@pytest.mark.parametrize("answer", DENIALS)
def test_draft_novel_answer_never_returns_a_denial_the_judge_fails(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, question: str, answer: str
) -> None:
    fake = FakeLLM(
        {
            "novel": [{"answer": answer, "bullets_used": []}],
            "claim_judge": [flagging_judge(answer)],
        }
    )
    monkeypatch.setattr(llm, "call_json", fake.call_json)
    monkeypatch.setattr(llm, "forget", fake.forget)
    drafted, reason, violations = form_spec.draft_novel_answer(
        conn,
        prompt=llm.PromptFile("novel_question"),
        corpus=CORPUS,
        profile=drafting_profile(CORPUS),
        job=FABRIKAM_JOB,
        label=question,
        question=question,
        known_companies=KNOWN,
    )
    assert drafted is None and reason == "unsourced-claim"
    assert _kinds(violations) == {"claim_judge_unsupported"}
    assert violations[0]["sentence"] == answer
    # the rules passed both drafts, so the judge read both; one regeneration
    assert len(fake.prompts["novel"]) == 2 and len(fake.prompts["claim_judge"]) == 2
    assert "claim_judge_unsupported" in fake.prompts["novel"][1]
    assert fake.forgotten.count("novel") == 1
    # the judge read the denial with its question, under the prompt that
    # calls a denial a claim and no longer exempts it
    judge_prompt = fake.prompts["claim_judge"][0]
    asked = judge_prompt.split("\nFORM QUESTION (", 1)[1].split("\nCITED SOURCES", 1)[0]
    assert question in asked
    assert judged_text(judge_prompt).strip() == answer
    flat = " ".join(judge_prompt.split())
    assert "A denial is a claim about the candidate's past or status." in flat
    assert "plain denial" not in flat.lower() and "cannot overstate" not in flat.lower()


# ---- never_draft: record and status questions are never sent to the drafter ----

NEVER_DRAFTED = [
    ("Have you ever been terminated or asked to resign?", "employment-record"),
    ("Have you ever been fired from a job?", "employment-record"),
    ("Were you ever let go from a position?", "employment-record"),
    ("Has your employment ever been terminated involuntarily?", "employment-record"),
    ("Have you ever been placed on a PIP?", "employment-record"),
    ("Have you ever been placed on a performance improvement plan?", "employment-record"),
    ("Have you been the subject of disciplinary action at work?", "employment-record"),
    ("Have you ever been convicted of a felony?", "criminal-record"),
    ("Have you been convicted of a crime in the last 7 years?", "criminal-record"),
    ("Do you have a criminal record?", "criminal-record"),
    ("Do you have any pending charges?", "criminal-record"),
    ("Are you bound by a non-compete agreement?", "restrictive-covenant"),
    ("Do you have a noncompete?", "restrictive-covenant"),
    (
        "Are you subject to any non-solicitation or restrictive covenants with a current employer?",
        "restrictive-covenant",
    ),
    (
        "Are you party to any agreement that would restrict your ability to join us?",
        "restrictive-covenant",
    ),
    ("Do you now or in the future require sponsorship?", "work-status"),
    ("Will you now or in the future need an employer to sponsor your visa?", "work-status"),
    ("Do you need visa sponsorship to work in the US?", "work-status"),
    ("Are you legally authorized to work in the US?", "work-status"),
    ("Are you eligible to work in the United States without sponsorship?", "work-status"),
    ("Are you a US citizen?", "work-status"),
    ("What is your work authorization status?", "work-status"),
    ("Are you currently on an H1B visa?", "work-status"),
]


@pytest.mark.parametrize(("label", "reason"), NEVER_DRAFTED)
def test_record_and_status_questions_are_never_drafted(label: str, reason: str) -> None:
    assert never_draft(label) == reason, label


# sales vocabulary and ordinary questions that share a word with the above
STILL_DRAFTED = [
    "Why do you want to work at Visa?",
    "Why Visa?",
    "Tell us about a deal where you engaged an executive sponsor.",
    "Have you engaged executive sponsors to work deals?",
    "Have you sold event sponsorships?",
    "Do your deals require executive sponsorship?",
    "Describe your forecasting discipline.",
    "Tell us about a time you were disciplined about pipeline hygiene.",
    "Describe a time you sold with conviction.",
    "How do you handle a prospect who has been dismissive?",
    "Why are you looking to leave your current role?",
    "Have you ever been promoted?",
    "Have you worked in a startup?",
]


@pytest.mark.parametrize("label", STILL_DRAFTED)
def test_sales_vocabulary_is_not_a_record_or_status_question(label: str) -> None:
    assert never_draft(label) is None, label


# ---- capture: the drafter is never asked; staging: a stored draft is never filled ----


def test_capture_never_drafts_a_record_or_status_question(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (jobkit_home / "data" / "career_corpus.yaml").write_text(yaml.safe_dump(CORPUS))
    conn.execute("INSERT INTO companies (id, name, normalized_name) VALUES (1, 'Hooli', 'w')")
    conn.execute("INSERT INTO boards (id, company_id, ats, token) VALUES (1, 1, 'greenhouse', 'w')")
    jid = conn.execute(
        """INSERT INTO jobs (board_id, company_id, external_id, title, normalized_title,
           company_name, normalized_company, location_bucket, cluster_key, jd_html, jd_hash,
           apply_url, status)
           VALUES (1, 1, 'g1', 'Senior Account Executive', 'sae', 'Hooli', 'hooli',
                   'remote-us', 'k', '<p>JD</p>', 'h', 'https://x', 'queued')"""
    ).lastrowid
    conn.commit()

    class StubMatcher:
        bank: dict[str, Any] = {"scaffolds": []}

        def match(
            self, label: str, compliance_type: str | None = None, **kw: Any
        ) -> dict[str, Any]:
            return {"kind": "novel"}

    labels = [*DENIAL_QUESTIONS, "Are you bound by a non-compete agreement?"]
    fields = [
        {
            "identity": f"q_{i}",
            "label": label,
            "type": "multi_value_single_select",
            "required": True,
            "options": ["Yes", "No"],
            "compliance_type": None,
        }
        for i, label in enumerate(labels)
    ]
    monkeypatch.setattr(form_spec, "BankMatcher", StubMatcher)
    monkeypatch.setitem(form_spec.FETCHERS, "greenhouse", lambda c, t, e: copy.deepcopy(fields))
    asked: list[str] = []

    def call_json(c: sqlite3.Connection, stage: str, prompt: str, **kw: Any) -> dict[str, Any]:
        asked.append(stage)
        return JUDGE_PASS if stage == "claim_judge" else {"answer": "No.", "bullets_used": []}

    monkeypatch.setattr(llm, "call_json", call_json)
    monkeypatch.setattr(llm, "forget", lambda *a: None)

    stats = form_spec.capture_form_specs(conn)
    assert stats["drafted"] == 0 and "novel" not in asked and "claim_judge" not in asked
    row = conn.execute(
        "SELECT fields, drafted_answers FROM form_specs WHERE job_id=?", (jid,)
    ).fetchone()
    assert json.loads(row["drafted_answers"]) == {}
    reasons = [f["match"] for f in json.loads(row["fields"])]
    assert reasons == [
        {"kind": "blank_flag", "reason": "never-draft:employment-record"},
        {"kind": "blank_flag", "reason": "never-draft:criminal-record"},
        {"kind": "blank_flag", "reason": "never-draft:employment-record"},
        {"kind": "blank_flag", "reason": "never-draft:restrictive-covenant"},
    ]


# no personal values: a made-up bank whose screening entries answer two wordings
BANK: dict[str, Any] = {
    "match_threshold": 0.82,
    "identity": {"first_name": "Pat", "last_name": "Doe", "email": "pat@example.com"},
    "screening": [
        {"id": "sponsorship", "aliases": ["Do you require visa sponsorship?"], "answer": "No"},
        {"id": "non_compete", "aliases": ["Are you subject to a non-compete?"], "answer": "No"},
    ],
}


@pytest.mark.parametrize(
    ("label", "reason"),
    [
        ("Have you ever been terminated or asked to resign?", "employment-record"),
        ("Have you ever been convicted of a felony?", "criminal-record"),
        ("Have you ever been placed on a PIP?", "employment-record"),
        ("Are you bound by a non-compete agreement?", "restrictive-covenant"),
        ("Will you now or in the future need an employer to sponsor your visa?", "work-status"),
    ],
)
def test_staging_never_fills_a_stored_draft_to_a_record_or_status_question(
    label: str, reason: str
) -> None:
    """A "No." drafted before this fix is still in form_specs.drafted_answers:
    staging re-matches the field and leaves it blank and flagged."""
    matcher = BankMatcher(bank=BANK, use_embeddings=False)
    field = {"identity": "q", "label": label, "match": {"kind": "novel"}}
    verdict = staging_match(matcher, field, None)
    assert verdict["kind"] == "blank_flag"
    if reason == "work-status":
        # a sponsorship label the sponsorship entry does not answer is
        # blanked by the matcher itself, before never_draft (2026-10-15)
        assert verdict["reason"].startswith("sponsorship-needs-human:"), verdict
    else:
        assert verdict == {"kind": "blank_flag", "reason": f"never-draft:{reason}"}
    assert browser._value_for(verdict, BANK, {"q": "No."}, "q") == (None, "unfilled")
    # the human may still answer it (MCP answer_novel): a human answer is not a draft
    human = staging_match(matcher, field, None, human_answered={"q"})
    assert human == {"kind": "human", "source": "human_typed"}
    assert browser._value_for(human, BANK, {"q": "No."}, "q") == ("No.", "human_typed")


@pytest.mark.parametrize(
    ("label", "entry"),
    [
        ("Do you require visa sponsorship?", "sponsorship"),
        ("Are you subject to a non-compete?", "non_compete"),
    ],
)
def test_the_banks_own_entries_still_answer_their_wordings(label: str, entry: str) -> None:
    """never_draft runs only on fields the bank leaves to drafting."""
    matcher = BankMatcher(bank=BANK, use_embeddings=False)
    verdict = staging_match(matcher, {"identity": "q", "label": label, "match": {}}, None)
    assert (verdict["kind"], verdict.get("entry_id")) == ("screening", entry)
    assert browser._value_for(verdict, BANK, {}, "q") == ("No", "answer_bank")
