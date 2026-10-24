"""The false positives of the claims validator seen in the cover letters
rejected on 2026-10-22/23. The validators are main's (5341b76), unchanged,
and main still flags these sentences, all of them soft:

(A) a word of experience copied from a cited bullet, then a connective no
    bullet has ("…three specialist pods, covering inbound, …"; "Earlier, at
    Corvane Analytics, …");
(B) a scheduling question ("When can we talk this week?");
(C) a closing stance clause ("…, which is the part of the work I'd point to
    first.");
(E) the noun "halves" ("both halves of the team"), read as the verb.

(D), the target's name for the posting's "Our" ("Vandelay & Company teams
partner with …"), fails as fabricated_employer, a hard kind.

A letter is not repaired by deleting such sentences (repair by deletion was
removed on 2026-10-23: it saved few letters and kept opening meaning-shift
holes). The letter is written again with the exact violations fed back
(pipeline/cover_feedback.py, tests/test_cover_attempts.py), and the prompt
(prompts/cover_letter.md) steers clear of these shapes. Fictional versions:
invented candidate, employers, target and posting.

The near-miss fabrications that must still fail are in
tests/test_claims_adversarial_1023.py, which imports this file's corpus.
Offline and LLM-free."""

from __future__ import annotations

from typing import Any

import pytest

from jobkit.validate.claims import HARD_KINDS, validate_claims
from jobkit.validate.provenance import ProvenanceResult

CORPUS: dict[str, Any] = {
    "identity": {"name": "Test Candidate", "email": "candidate@example.com"},
    "roles": [
        {
            "id": "role_larkspur",
            "company": "Larkspur Health",
            "title": "Director of Sales",
            "start": "2021-02",
            "end": "2024-06",
        },
        {
            "id": "role_corvane",
            "company": "Corvane Analytics",
            "title": "Sales Development Manager",
            "start": "2017-01",
            "end": "2021-02",
        },
    ],
    "education": [
        {"school": "Lakeside State University", "degree": "B.A.", "years": "2009 – 2013"}
    ],
    "skill_index": {"Salesforce": "hands-on"},
    "constraints": {
        "never_claim_titles": ["VP of Sales", "Chief Revenue Officer"],
        "never_claim_phrases": [],
    },
    "bullets": [
        {
            "id": "b_101",
            "role_id": "role_larkspur",
            "metric_confidence": "verified",
            "text_long": "Split the inside sales desk into three specialist pods (inbound, "
            "expansion, and win-back); pipeline coverage rose from 61% to 88%.",
            "metric": "pipeline coverage 61% → 88%",
        },
        {
            "id": "b_102",
            "role_id": "role_corvane",
            "metric_confidence": "verified",
            "text_long": "Turned inbound chat leads into qualified demos, booking 15–20 "
            "qualified demos a week across the team.",
            "metric": "15–20 qualified demos a week",
        },
        {
            "id": "b_103",
            "role_id": "role_larkspur",
            "metric_confidence": "verified",
            "text_long": "Promoted five shift leads into team managers and ran a floor of 14 "
            "account managers.",
            "metric": "14 account managers",
        },
        {
            "id": "b_104",
            "role_id": "role_larkspur",
            "metric_confidence": "verified",
            "text_long": "Ran hiring and onboarding for both the inbound and expansion pods, "
            "hiring 22 reps in 14 months.",
            "metric": "22 reps in 14 months",
        },
    ],
}
CITED = ("b_101", "b_102", "b_103", "b_104")

TARGET = "Vandelay & Company"
TITLE = "Director of Sales Development"
SPECIALIST_TITLE = "Head of Specialist Partnerships, APAC"
JD = (
    "Vandelay & Company is hiring a Director of Sales Development. Our teams partner with "
    "organizations across industries. You will lead the SDR team and partner with Account "
    "Executives."
)
KNOWN = [TARGET, "Initech"]
# validate/claims.py cuts a violation's "sentence" field at this many characters
SENTENCE_FIELD_MAX = 160


def _check(text: str) -> ProvenanceResult:
    return validate_claims(
        text,
        list(CITED),
        CORPUS,
        target_company=TARGET,
        job_title=TITLE,
        jd_text=JD,
        known_companies=KNOWN,
        estimate_style="confident",
    )


# ---------------------------------------------------------------------------
# the letters (fictional versions of the rejected ones)
# ---------------------------------------------------------------------------

OPENING = (
    "I'm applying for the Director of Sales Development role at Vandelay & Company. "
    "Vandelay & Company is hiring a Director of Sales Development. A development team lives "
    "or dies on the first meeting with a buyer. Pipeline starts at that first meeting, and so "
    "does the forecast."
)
FLOOR = (
    "At Larkspur Health I promoted five shift leads into team managers and ran a floor of 14 "
    "account managers. I also ran hiring and onboarding for both the inbound and expansion "
    "pods, hiring 22 reps in 14 months. Those were not easy hires to make. Each new rep needed "
    "a manager close enough to coach the first calls."
)
DEMOS = (
    "At Corvane Analytics I turned inbound chat leads into qualified demos, booking 15–20 "
    "qualified demos a week across the team. For a development team, that means booked "
    "meetings the account executives want to take, and reps who stay long enough to get good. "
    "One result matters most for this role."
)
CLOSE = "Let's put a meeting on the calendar."

# (A) the letter of 2026-10-22, run 2: "covering", and a leading "Earlier"
COVERING = (
    "At Larkspur Health I split the inside sales desk into three specialist pods, covering "
    "inbound, expansion, and win-back, and pipeline coverage went from 61% to 88%."
)
EARLIER = "Earlier, at Corvane Analytics, I turned inbound chat leads into qualified demos."
# (B) and (C) the letter of 2026-10-23
STANCE = (
    "I also promoted five shift leads into team managers, which is the part of the work I'd "
    "point to first."
)
TALK = "When can we talk this week?"
# (E) the letter of 2026-10-22, run 1
PODS = (
    "At Larkspur Health I split the inside sales desk into three specialist pods (inbound, "
    "expansion, and win-back); pipeline coverage rose from 61% to 88%."
)
HALVES = (
    "At Larkspur Health I ran hiring and onboarding for both halves of the team, hiring 22 "
    "reps in 14 months."
)
# (D) hard: the target's name for the posting's "Our"
TEAMS = "Vandelay & Company teams partner with organizations across industries."

# name, letter, the sentences main flags (each soft)
LETTERS: list[tuple[str, str, list[str]]] = [
    (
        "covering",
        "\n\n".join([OPENING, f"{COVERING} {FLOOR}", f"{EARLIER} {DEMOS} {CLOSE}"]),
        [COVERING, EARLIER],
    ),
    (
        "talk_and_stance",
        "\n\n".join([OPENING, f"{FLOOR} {STANCE}", f"{DEMOS} {TALK}"]),
        [STANCE, TALK],
    ),
    ("halves", "\n\n".join([OPENING, f"{PODS} {HALVES}", f"{DEMOS} {CLOSE}"]), [HALVES]),
]
IDS = [name for name, _l, _f in LETTERS]


@pytest.mark.parametrize(("name", "letter", "flagged"), LETTERS, ids=IDS)
def test_main_still_flags_the_false_positive_sentences(
    name: str, letter: str, flagged: list[str]
) -> None:
    out = _check(letter)
    assert not out.ok
    # each violation names its sentence, cut at 160 characters (the covering
    # sentence is longer)
    got = sorted({v["sentence"] for v in out.violations})
    assert got == sorted(s[:SENTENCE_FIELD_MAX] for s in flagged), out.violations
    assert not {v["kind"] for v in out.violations} & HARD_KINDS


def test_the_targets_teams_sentence_is_hard() -> None:
    letter = "\n\n".join([f"{OPENING} {TEAMS}", FLOOR, f"{DEMOS} {CLOSE}"])
    out = _check(letter)
    assert "fabricated_employer" in {v["kind"] for v in out.violations}, out.violations
    assert {v["kind"] for v in out.violations} & HARD_KINDS


def test_the_letters_without_the_flagged_sentences_pass() -> None:
    # what a regeneration that rewrites only the flagged sentences (here: cuts
    # them) is aiming at: the rest of each letter passes as written
    for _name, letter, flagged in LETTERS:
        kept = letter
        for s in flagged:
            kept = kept.replace(f" {s}", "").replace(f"{s} ", "")
        out = _check(kept)
        assert out.ok and out.violations == [], (_name, out.violations)
