"""Gap questions under the prompt_set switch.

Golden: with `prompt_set` absent (the sales test profile), the questions
built from a fixed gap list and fixed self-review keys are byte-identical to
snapshots taken before prompt_set existed
(tests/fixtures/golden/sales_gap_questions.json), and questions.TOPICS, which
the claims validator imports, is unchanged (questions_topics.json).
Regenerate only on a deliberate change: JOBKIT_REGEN_GOLDEN=1 pytest
tests/test_questions_general.py.

General: prompt_set general words experience questions for any field ("Have
you worked in ...?"), never "sold", and turns off the sales tool and method
packs for gap text.

Offline: no LLM call (questions.py makes none)."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from jobkit.pipeline import questions
from jobkit.validate import claims
from tests.conftest import FIXTURES
from tests.test_questions import _job, _self_review_question, corpus_file  # noqa: F401

GOLDEN = FIXTURES / "golden"
REGEN = os.environ.get("JOBKIT_REGEN_GOLDEN") == "1"

# One gap per experience topic, the sales tools and methods, and the lines
# that are never questions (overqualified, tenure).
GAPS = (
    "Clari proficiency (required tool not documented)",
    "Missing ZoomInfo/Cognism and 6sense platform experience",
    "Gong call reviews",
    "Outreach sequences not documented",
    "Clay enrichment workflows",
    "Tableau dashboards for the board",
    "MEDDPICC rather than MEDDIC expertise",
    "Challenger Sale training",
    "No SDR team management experience",
    "Healthcare sector sales experience not documented",
    "No fintech or banking background",
    "No law firm experience",
    "Public sector buyers not documented",
    "No documented experience with cybersecurity products",
    "Developer tools experience missing",
    "Technical buyers (CTOs) not documented",
    "Channel partners experience not shown",
    "Territory planning not documented",
    "Board-level reporting not shown",
    "Product-led motion not documented",
    "Cold calling not documented",
    "Hiring reps not documented",
    "Usage-based pricing not documented",
    "EMEA expansion not documented",
    "Marketing automation not documented",
    "overqualified: 2.3 yrs managing AEs",
    "5+ years closing AE experience (has 3.7 years)",
    "Epic EHR charting not documented",
    "Articulate Storyline authoring not documented",
)
SELF_REVIEW_KEYS = (
    ("hubspot_experience", "Have you used HubSpot?"),
    ("sandler_experience", "Have you used Sandler?"),
    ("ledgerline_expansion_revenue_split", "How much came from repeat borrowers?"),
)


def _golden(name: str, value: Any) -> None:
    path = GOLDEN / name
    text = json.dumps(value, indent=1, sort_keys=True) + "\n"
    if REGEN:
        GOLDEN.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return
    assert path.is_file(), f"no golden file {path}; run with JOBKIT_REGEN_GOLDEN=1"
    assert text == path.read_text(), f"{name} differs from its golden snapshot"


def _topics_snapshot() -> list[dict[str, Any]]:
    return [
        {
            "key": t.key,
            "kind": t.kind,
            "label": t.label,
            "pattern": t.pattern.pattern,
            "flags": int(t.pattern.flags),
            "loose": t.loose.pattern,
            "loose_flags": int(t.loose.flags),
            "ask": t.ask,
        }
        for t in questions.TOPICS
    ]


def _questions_snapshot(ranked: list[questions.Question]) -> list[dict[str, Any]]:
    return [
        {
            "key": qn.key,
            "kind": qn.kind,
            "label": qn.label,
            "question": qn.question,
            "gap_examples": qn.gap_examples,
            "jobs": sorted(qn.jobs),
        }
        for qn in ranked
    ]


@pytest.fixture()
def gapped(conn: sqlite3.Connection, corpus_file: Path) -> sqlite3.Connection:  # noqa: F811
    """Two open jobs carrying every gap in GAPS, plus self-review questions."""
    j1 = _job(conn, "g1", fit=80, gaps=GAPS[::2])
    j2 = _job(conn, "g2", fit=70, gaps=GAPS[1::2])
    for key, question in SELF_REVIEW_KEYS:
        _self_review_question(conn, j1, key, question)
    _self_review_question(conn, j2, SELF_REVIEW_KEYS[0][0], SELF_REVIEW_KEYS[0][1])
    return conn


def test_topics_are_unchanged() -> None:
    """claims.py imports TOPICS (unsourced_tool): it must not move."""
    _golden("questions_topics.json", _topics_snapshot())


def test_sales_gap_questions_are_byte_identical(gapped: sqlite3.Connection) -> None:
    ranked = questions.aggregate(gapped)
    _golden("sales_gap_questions.json", _questions_snapshot(ranked))


# ---------------------------------------------------------------------------
# prompt_set general
# ---------------------------------------------------------------------------

PERSONAS = FIXTURES / "personas"
SALES_WORDS = re.compile(r"(?i)\b(?:sold|sell\w*|deals?|quota)\b")
CYBER_GAP = "No documented experience with cybersecurity products"


def _use_persona(home: Path, name: str) -> None:
    """The persona as the sandbox's config/filters.yaml."""
    (home / "config" / "filters.yaml").write_text((PERSONAS / f"{name}.yaml").read_text())


def test_topics_is_the_object_the_claims_validator_reads() -> None:
    assert claims.TOPICS is questions.TOPICS


def test_every_experience_topic_has_a_general_question() -> None:
    experience = {t.key for t in questions.TOPICS if t.kind == "experience"}
    assert set(questions.GENERAL_ASK) == experience
    for key, ask in questions.GENERAL_ASK.items():
        assert ask.startswith("Have you ") and ask.endswith("?"), key
        assert not SALES_WORDS.search(ask), key


def test_engineer_cybersecurity_gap_is_worded_for_the_field(
    conn: sqlite3.Connection,
    corpus_file: Path,  # noqa: F811
    jobkit_home: Path,
) -> None:
    """The engineer persona's config is read by aggregate itself."""
    _use_persona(jobkit_home, "engineer")
    _job(conn, "e1", title="Senior Backend Engineer", gaps=(CYBER_GAP,))
    (qn,) = questions.aggregate(conn)
    assert qn.key == "exp-cybersecurity"
    assert qn.question.startswith("Have you worked in cybersecurity? ")
    assert "sold" not in qn.question


def test_sales_profile_still_asks_the_sales_question(
    conn: sqlite3.Connection,
    corpus_file: Path,  # noqa: F811
) -> None:
    _job(conn, "s1", gaps=(CYBER_GAP,))
    (qn,) = questions.aggregate(conn)
    assert qn.question.startswith("Have you sold cybersecurity products? ")


def test_general_questions_never_say_sold(gapped: sqlite3.Connection) -> None:
    sales = {qn.key: qn for qn in questions.aggregate(gapped, prompt_set="sales")}
    general = {qn.key: qn for qn in questions.aggregate(gapped, prompt_set="general")}
    for qn in general.values():
        assert not SALES_WORDS.search(qn.question), qn.question
    # the experience topics are the same; only their wording changes
    assert {k for k in general if k.startswith("exp-")} == {
        k for k in sales if k.startswith("exp-")
    }
    # gap text never becomes a tool or method question
    gap_only = {"tool-clari", "tool-cognism", "tool-gong", "tool-outreach", "tool-clay"}
    assert gap_only <= set(sales) and not gap_only & set(general)
    assert not {k for k in general if k.startswith("method-meddpicc")}
    # a self-review question about a tool or method is still asked, plainly worded
    assert general["tool-hubspot"].question == sales["tool-hubspot"].question
    assert general["method-sandler"].question.startswith("Have you used Sandler? If yes:")
    assert sales["method-sandler"].question.startswith("Have you run deals")
    # self-review fact questions are the self-review's own words in both
    fact = "expansion_ledgerline_revenue_split"
    assert general[fact].question == sales[fact].question
    # the job lists and gap examples behind each shared question are the same
    for key in set(general) & set(sales):
        assert general[key].jobs.keys() == sales[key].jobs.keys(), key
        assert general[key].gap_examples == sales[key].gap_examples, key


@pytest.mark.parametrize(
    "gap",
    [
        "Clay County Schools district experience",
        "Family Outreach Coordinator role not documented",
        "Apollo Federation (GraphQL) not documented",
        "Challenger Learning Center programs",
    ],
)
def test_general_gaps_do_not_read_place_names_as_sales_tools(gap: str) -> None:
    assert [t for t in questions.topics_in_gap(gap) if t.kind != "experience"]
    assert questions.topics_in_gap(gap, prompt_set="general") == []


def test_overqualified_and_tenure_gaps_stay_unasked_under_general() -> None:
    for gap in (
        "overqualified: 9 years as a charge nurse",
        "5+ years in healthcare required (has 3)",
    ):
        assert questions.topics_in_gap(gap, prompt_set="general") == []


def test_a_missing_filters_file_reads_as_sales(
    gapped: sqlite3.Connection, jobkit_home: Path
) -> None:
    (jobkit_home / "config" / "filters.yaml").unlink()
    assert questions.active_prompt_set() == "sales"
    got = _questions_snapshot(questions.aggregate(gapped))
    assert got == _questions_snapshot(questions.aggregate(gapped, prompt_set="sales"))


def test_a_filters_file_that_does_not_parse_makes_questions_unavailable(
    gapped: sqlite3.Connection, jobkit_home: Path
) -> None:
    (jobkit_home / "config" / "filters.yaml").write_text("filters: [unclosed\n")
    with pytest.raises(questions.SourceError):
        questions.aggregate(gapped)
    assert questions.render(gapped, all_=True).startswith("Questions unavailable:")


@pytest.mark.parametrize(
    "gap, general, sales",
    [
        ("No documented experience designing REST API endpoint contracts", [], ["cyber"]),
        ("No EDR or SIEM experience", ["cyber"], ["cyber"]),
        ("No endpoint security background", ["cyber"], ["cyber"]),
        ("No cybersecurity experience", ["cyber"], ["cyber"]),
    ],
)
def test_general_cybersecurity_needs_a_security_word(
    gap: str, general: list[str], sales: list[str]
) -> None:
    """Under prompt_set general an API endpoint is not a cybersecurity gap;
    the sales path keeps the topic's own pattern (TOPICS is unchanged)."""
    from jobkit.pipeline.questions import _GENERAL_GAP_PATTERNS, TOPICS, topics_in_gap

    def keys(prompt_set: str) -> list[str]:
        return [
            "cyber"
            for t in topics_in_gap(gap, prompt_set=prompt_set)
            if t.key == "exp-cybersecurity"
        ]

    assert keys("general") == general and keys("sales") == sales
    assert set(_GENERAL_GAP_PATTERNS) <= {t.key for t in TOPICS}
