"""Questions for the human, corpus proposals and rescore (package 3, owner
request 2026-10-09): ask only for facts the system cannot know, then adjust
everything they affect.

Offline: no LLM call except the score stage under a fake `claude -p` backend
(the real llm.call, cache and all); the corpus and the proposals file are the
sandbox's (tests/conftest.py), never data/career_corpus.yaml."""

from __future__ import annotations

import datetime
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from jobkit import background, llm
from jobkit import db as q
from jobkit.config import Filters, load_filters
from jobkit.pipeline import questions, rescore, triage
from jobkit.pipeline.digest import write_digest
from jobkit.validate import provenance
from jobkit.validate.provenance import corpus_hash
from tests.conftest import SpawnRecorder
from tests.test_filter import _insert
from tests.test_triage import REAL_JD

TODAY = datetime.date(2026, 10, 9)

# A corpus with comments in three places: a confirmed proposal must keep them.
CORPUS_TEXT = """\
# career corpus fixture — comments must survive a confirmed proposal
corpus_version: "1.0"
identity:
  name: "Test Person"
roles:
  - id: role_mgr
    company: "Haulmark"
    title: "Regional Sales Manager"
    start: "2024-03"
    end: null
  - id: role_ops
    company: "ledgerline"
    title: "Head of Revenue Operations"
    start: "2021-11"
    end: "2024-03"
skill_index:
  # confirmed by the human, 2026-10-08
  Salesforce: hands-on (confirmed 2026-10-08)
  MEDDIC: ran own deals on it
constraints:
  never_claim_titles: ["VP"]
  never_claim_tools: ["Kubernetes"]
bullets:
  # ---- Haulmark · Regional Sales Manager ----
  - id: b_001
    role_id: role_mgr
    text_long: Led a team of 9 AEs selling route planning to regional carriers.
    text_short: Led 9 AEs selling to regional carriers.
    tags: [sales, team-leadership]
    metric: "9 AEs"
    metric_confidence: verified
    evidence: "CRM report"
  - id: b_002
    role_id: role_ops
    text_long: Moved the sales reports from HubSpot into Looker.
    text_short: Moved the sales reports from HubSpot to Looker.
    tags: [reporting]
    metric_confidence: none
    evidence: "migration doc"

# voice samples follow — this comment belongs to the next block
voice_samples:
  - id: v_001
    source: "memo"
    text: "Short sample."
"""

CLARI_GAP = "Clari proficiency (required tool not documented)"
# the companies of the fixture corpus's roles: the only places a skill answer
# may name (questions.corpus_places)
PLACES = ("Haulmark", "ledgerline")
HEALTH_GAP = "Healthcare sector sales experience not documented"


@pytest.fixture()
def corpus_file(jobkit_home: Path) -> Path:
    path = jobkit_home / "data" / "career_corpus.yaml"
    path.write_text(CORPUS_TEXT)
    return path


def _job(
    conn: sqlite3.Connection,
    ext: str,
    *,
    status: str = "queued",
    fit: int | None = 80,
    gaps: tuple[str, ...] = (),
    title: str = "SDR Manager",
    company: str = "Acme",
    closed: bool = False,
    requirements: bool = False,
    posted_at: str | None = None,
) -> int:
    jid = _insert(
        conn,
        external_id=ext,
        title=title,
        company_name=company,
        jd_html=REAL_JD,
        posted_at=posted_at,
    )
    conn.execute(
        "UPDATE jobs SET status = ?, closed_at = ? WHERE id = ?",
        (status, "2026-10-01 00:00:00" if closed else None, jid),
    )
    if fit is not None:
        conn.execute(
            "INSERT INTO scores (job_id, fit_score, rationale, gaps, scored_at, prompt_version) "
            "VALUES (?, ?, 'r', ?, datetime('now'), '1.1')",
            (jid, fit, json.dumps(list(gaps))),
        )
    if requirements:
        conn.execute(
            "INSERT INTO job_requirements (job_id, must_have, nice_to_have, implied, "
            "disqualifier) VALUES (?, '[\"x\"]', '[]', '[]', '[]')",
            (jid,),
        )
    conn.commit()
    return jid


def _self_review_question(
    conn: sqlite3.Connection, job_id: int, key: str, question: str, why: str = ""
) -> None:
    conn.execute(
        q.RECORD_EVENT,
        {
            "job_id": job_id,
            "kind": "question_open",
            "detail": json.dumps(
                {
                    "key": key,
                    "question": question,
                    "why": why,
                    "fact_needed": question,
                    "job_id": job_id,
                    "artifact": "cover",
                }
            ),
        },
    )
    conn.commit()


@pytest.fixture()
def seeded(conn: sqlite3.Connection, corpus_file: Path) -> dict[str, int]:
    """Open jobs with overlapping gaps and self-review questions, plus jobs
    that must not count (rejected, closed)."""
    j1 = _job(
        conn,
        "j1",
        fit=80,
        gaps=(
            CLARI_GAP,
            "Salesforce CPQ administration (uses it, does not configure it)",
            "overqualified: 2.3 yrs managing AEs — the application must explain the move",
            "5+ years closing AE experience (has 3.7 years)",
        ),
    )
    j2 = _job(
        conn,
        "j2",
        status="approved",
        fit=70,
        gaps=("Missing Clari, ZoomInfo/Cognism and 6sense platform experience", HEALTH_GAP),
    )
    j3 = _job(conn, "j3", fit=90, gaps=(HEALTH_GAP, "MEDDIC rather than MEDDPICC expertise"))
    j4 = _job(conn, "j4", status="rejected", fit=85, gaps=(CLARI_GAP, "Gong call reviews"))
    j5 = _job(conn, "j5", fit=88, gaps=(CLARI_GAP,), closed=True)
    _self_review_question(conn, j3, "clari_experience", "Have you used Clari?")
    _self_review_question(
        conn,
        j1,
        "ledgerline_expansion_revenue_split",
        "How much of the $2.4M at ledgerline came from repeat borrowers?",
        why="the JD asks for expansion revenue",
    )
    _self_review_question(
        conn,
        j2,
        "expansion_revenue_split_at_ledgerline",
        "What share of ledgerline revenue came from repeat borrowers?",
    )
    _self_review_question(
        conn, j1, "sdr_team_management_experience", "Have you managed SDRs?", why="SDR team"
    )
    return {"j1": j1, "j2": j2, "j3": j3, "j4": j4, "j5": j5}


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("gap", "keys"),
    [
        (CLARI_GAP, ["tool-clari"]),
        ("Sales Navigator proficiency", ["tool-linkedin-sales-navigator"]),
        (
            "Missing Clari, ZoomInfo/Cognism, LinkedIn Sales Navigator, and 6sense",
            [
                "tool-clari",
                "tool-linkedin-sales-navigator",
                "tool-zoominfo",
                "tool-cognism",
                "tool-6sense",
            ],
        ),
        (
            "MEDDIC rather than MEDDPICC sales methodology expertise",
            ["method-meddpicc", "method-meddic"],
        ),
        (HEALTH_GAP, ["exp-healthcare"]),
        (
            "Public sector automation expertise not demonstrated; only generic logistics work",
            ["exp-public-sector"],
        ),
        # judgments about tenure and degree: the corpus dates already state them
        ("Limited direct experience closing complex enterprise or healthcare deals", []),
        ("3+ years in healthcare sales (has 1 year)", []),
        ("overqualified: Clari admin and 2.3 yrs managing AEs", []),
        # an everyday word is not the tool
        ("No cold outreach volume targets documented", []),
        ("Documented ability to sell to technical stakeholders", ["exp-technical-buyers"]),
        ("No demonstrated passion for the mission", []),
    ],
)
def test_gap_topics(gap: str, keys: list[str]) -> None:
    assert sorted(t.key for t in questions.topics_in_gap(gap)) == sorted(keys)


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("clari_experience", "tool-clari"),
        ("Clari", "tool-clari"),
        ("healthcare_sales_experience", "exp-healthcare"),
        ("sdr_team_management_experience", "exp-sdr-bdr-team-management"),
        # another content word: its own fact, not "have you used Salesforce"
        ("commissions_in_salesforce_at_ledgerline", "commissions_ledgerline_salesforce"),
        ("ledgerline_expansion_revenue_split", "expansion_ledgerline_revenue_split"),
        ("expansion_revenue_split_at_ledgerline", "expansion_ledgerline_revenue_split"),
    ],
)
def test_self_review_keys_normalize(raw: str, key: str) -> None:
    assert questions.normalize_key(raw) == key


# ---------------------------------------------------------------------------
# aggregation, dedupe, ranking
# ---------------------------------------------------------------------------


def test_aggregate_dedupes_and_ranks_by_jobs_helped_then_fit(
    conn: sqlite3.Connection, seeded: dict[str, int]
) -> None:
    ranked = questions.aggregate(conn)
    keys = [qn.key for qn in ranked]
    assert keys == [
        "tool-clari",  # 3 open jobs (two gap reports + one self-review question)
        "exp-healthcare",  # 2 jobs, fit 70 + 90
        "expansion_ledgerline_revenue_split",  # 2 jobs, fit 80 + 70: two keys, one fact
        "method-meddpicc",  # 1 job, fit 90
        "exp-sdr-bdr-team-management",  # 1 job, fit 80
        "tool-6sense",  # 1 job, fit 70 each: then by key
        "tool-cognism",
        "tool-zoominfo",
    ]
    clari = ranked[0]
    assert set(clari.jobs) == {seeded["j1"], seeded["j2"], seeded["j3"]}
    assert clari.default_entry == "skill"
    assert "Clari" in clari.question and "gap for 3 open job(s)" in clari.why_text()
    # the corpus already answers these: never asked
    assert "tool-salesforce" not in keys and "method-meddic" not in keys
    # a rejected or closed job helps nobody: Gong only came from the rejected job
    assert "tool-gong" not in keys
    assert seeded["j4"] not in clari.jobs and seeded["j5"] not in clari.jobs
    split = ranked[2]
    assert split.kind == "fact" and split.default_entry == "bullet"
    assert "expansion revenue" in split.why_text()


def test_corpus_answers_are_read_from_skill_index_bullets_and_constraints(
    conn: sqlite3.Connection, corpus_file: Path
) -> None:
    _job(
        conn,
        "a",
        gaps=("HubSpot proficiency", "Salesforce CPQ", "Looker admin", "Gong reviews"),
    )
    keys = {qn.key for qn in questions.aggregate(conn)}
    # Salesforce is a skill_index key; HubSpot and Looker are only named in a
    # bullet, which never answers a tool question (review 2026-10-09,
    # skill-answers); Gong is not in the corpus at all
    assert keys == {"tool-gong", "tool-hubspot", "tool-looker"}
    # a tool the human said never to claim is answered too: never asked
    corpus_file.write_text(CORPUS_TEXT.replace('["Kubernetes"]', '["Kubernetes", "Gong"]'))
    assert {qn.key for qn in questions.aggregate(conn)} == {"tool-hubspot", "tool-looker"}


# ---------------------------------------------------------------------------
# the daily cap
# ---------------------------------------------------------------------------


def _set_cap(home: Path, n: int) -> None:
    path = home / "config" / "filters.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["questions"] = {"max_per_day": n}
    path.write_text(yaml.safe_dump(raw))


def test_daily_cap_is_one_knob_and_answering_does_not_reveal_more(
    conn: sqlite3.Connection, seeded: dict[str, int], jobkit_home: Path
) -> None:
    assert load_filters().questions_per_day == 3  # the shipped default
    _set_cap(jobkit_home, 2)
    assert load_filters().questions_per_day == 2
    d = questions.todays(conn, today=TODAY)
    assert [qn.key for qn in d.shown] == ["tool-clari", "exp-healthcare"]
    assert d.open_total == 8
    # the same day, again: the same two, nothing new recorded
    assert [qn.key for qn in questions.todays(conn, today=TODAY).shown] == [
        "tool-clari",
        "exp-healthcare",
    ]
    questions.answer(conn, "exp-healthcare", "No, never sold into healthcare")
    later = questions.todays(conn, today=TODAY)
    assert [qn.key for qn in later.shown] == ["tool-clari"]  # no third question today
    assert later.settled_today == 1
    tomorrow = questions.todays(conn, today=TODAY + datetime.timedelta(days=1))
    assert [qn.key for qn in tomorrow.shown] == ["tool-clari", "expansion_ledgerline_revenue_split"]


def test_cap_validation() -> None:
    with pytest.raises(ValueError, match="max_per_day"):
        _ = Filters(raw={"questions": {"max_per_day": -1}}).questions_per_day
    assert Filters(raw={}).questions_per_day == 3
    assert Filters(raw={"questions": {"max_per_day": 0}}).questions_per_day == 0


def test_digest_and_get_queue_show_todays_questions(
    conn: sqlite3.Connection,
    seeded: dict[str, int],
    jobkit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jobkit import mcp_server

    text = write_digest(conn, TODAY.isoformat()).read_text()
    section = text.split("## Questions for you", 1)[1].split("\n## ", 1)[0]
    assert section.count("[tool-") + section.count("[exp-") + section.count("[expansion_") == 3
    assert "Why:" in section and "Helps 3 open job(s)" in section
    assert f"job {seeded['j3']} Acme" in section
    assert 'jobkit answer tool-clari "<your answer>"' in section

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    monkeypatch.setattr(mcp_server, "run_standing_rules", lambda c: {"evaluated": 0})
    conn.executescript(mcp_server.PINS_SCHEMA)
    out = mcp_server.get_queue()
    assert "Questions for you today" in out and "[tool-clari]" in out


@pytest.mark.parametrize("broken", ["corpus", "proposals", "cap"])
def test_a_file_that_does_not_parse_never_takes_down_the_queue_or_the_digest(
    conn: sqlite3.Connection,
    seeded: dict[str, int],
    corpus_file: Path,
    jobkit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    broken: str,
) -> None:
    """Review 2026-10-09: a typo in data/career_corpus.yaml or
    data/corpus_proposals.yaml made get_queue and write_digest raise, staging
    halts (hard rule 2) included. The questions section is one line now."""
    from jobkit import mcp_server

    halted = _job(conn, "halt", status="approved", fit=77, company="Haltco")
    conn.execute(
        "INSERT INTO autofill_runs (job_id, status, abort_reason, filled_fields) "
        "VALUES (?, 'needs_human', 'captcha-or-turnstile-visible', '[]')",
        (halted,),
    )
    conn.commit()
    if broken == "corpus":
        corpus_file.write_text('corpus_version: "1.0\nroles: [\n')
        expect = "data/career_corpus.yaml does not parse"
    elif broken == "proposals":
        (jobkit_home / "data" / "corpus_proposals.yaml").write_text("proposals:\n  - {id: 1\n")
        expect = "data/corpus_proposals.yaml does not parse"
    else:
        _set_cap(jobkit_home, -1)
        expect = "questions.max_per_day must be a whole number"

    text = write_digest(conn, TODAY.isoformat()).read_text()
    assert f"Questions unavailable: {expect}" in text
    assert "job " + str(halted) + " · Haltco" in text  # the staging halt is still there
    assert "## Review queue" in text and "Acme — SDR Manager" in text
    # nothing was recorded as asked while the questions could not be shown
    assert (
        conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'question_asked'").fetchone()[0] == 0
    )

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    monkeypatch.setattr(mcp_server, "run_standing_rules", lambda c: {"evaluated": 0})
    conn.executescript(mcp_server.PINS_SCHEMA)
    out = mcp_server.get_queue()
    assert f"Questions unavailable: {expect}" in out
    assert "jobs in queue:" in out and "Acme" in out
    assert "Haltco" in out  # the halt line under the banner
    assert mcp_server.get_questions().startswith(f"Questions unavailable: {expect}")
    if broken != "cap":
        res = questions.answer(conn, "tool-clari", "Yes, I used Clari daily at Haulmark")
        assert not res.ok and expect in res.text
        assert not questions.confirm(conn, 1).ok


# ---------------------------------------------------------------------------
# answers: declined never re-asked; proposals; confirmation writes the corpus
# ---------------------------------------------------------------------------


def test_declined_question_is_never_asked_again(
    conn: sqlite3.Connection, seeded: dict[str, int], corpus_file: Path
) -> None:
    before = corpus_file.read_text()
    res = questions.answer(conn, "exp-healthcare", "never did that")
    assert res.ok and res.declined
    assert "exp-healthcare" not in {qn.key for qn in questions.aggregate(conn)}
    # a new job with the same gap does not bring it back; the gap stays a gap
    _job(conn, "new", fit=95, gaps=(HEALTH_GAP,))
    assert "exp-healthcare" not in {qn.key for qn in questions.aggregate(conn)}
    again = questions.answer(conn, "exp-healthcare", "Actually yes, a lab courier")
    assert not again.ok and "declined" in again.text
    assert corpus_file.read_text() == before
    assert questions.load_proposals() == []
    ev = conn.execute("SELECT detail FROM events WHERE kind = 'question_declined'").fetchone()
    assert json.loads(ev[0])["key"] == "exp-healthcare"


@pytest.mark.parametrize(
    ("key", "text", "verdict"),
    [
        ("tool-clari", "No", "decline"),
        ("tool-clari", "no.", "decline"),
        ("tool-clari", "Nope", "decline"),
        ("tool-clari", "n/a", "decline"),
        ("tool-clari", "Never used it", "decline"),
        ("tool-clari", "I haven't", "decline"),
        ("tool-clari", "I've never used Clari", "decline"),
        ("tool-clari", "I\u2019ve never touched Clari.", "decline"),
        ("tool-clari", "No, I have never used it at any job", "decline"),
        ("tool-clari", "never did that", "decline"),
        ("exp-healthcare", "No, never sold into healthcare", "decline"),
        ("exp-sdr-bdr-team-management", "I have never managed SDRs", "decline"),
        # review 2026-10-09: these became skill_index proposals
        ("tool-clari", "I have never used Clari", "decline"),
        ("tool-clari", "Haven't used it", "decline"),
        ("tool-clari", "I've not used it", "decline"),
        ("tool-clari", "Unfortunately not", "decline"),
        ("tool-clari", "Not that I recall", "decline"),
        # review 2026-10-09: short yes-answers that were permanent declines
        ("tool-clari", "Never below 115% of quota at Haulmark", "ambiguous"),
        ("tool-clari", "None of my AEs churned; ran Clari weekly", "ambiguous"),
        # package S: "No-code" is not a no, but a tool answer must be the one
        # accepted sentence of use (review 2026-10-09, skill-answers)
        ("tool-clay", "No-code Clay workflows at ledgerline for 10 months", "ambiguous"),
        ("tool-clay", "Yes, no-code Clay workflows at ledgerline for 10 months", "ambiguous"),
        ("tool-clay", "Yes, I used Clay for 10 months at ledgerline", "fact"),
        ("exp-marketing-automation", "No-code workflows at ledgerline for 10 months", "fact"),
        # a no that turns, or a long one: maybe a no, maybe a fact
        ("tool-clari", "No, but I used HubSpot daily", "ambiguous"),
        ("tool-clari", "Never formally, though I coached reps on it", "ambiguous"),
        (
            "tool-clari",
            "Never missed quota in two years at Haulmark running weekly Clari calls",
            "ambiguous",
        ),
        # the subject denied somewhere, or another tool named
        ("tool-clari", "Used Gong daily; never used Clari", "ambiguous"),
        ("tool-clari", "Limited exposure to Clari", "ambiguous"),
        ("tool-clari", "I have never used Gong", "ambiguous"),
        ("tool-clari", "Yes, but never used it for forecasting", "ambiguous"),
        # facts; a bare "yes" or "daily" no longer opens a tool answer
        # (review 2026-10-09, skill-answers)
        ("tool-clari", "Yes, daily at Haulmark", "ambiguous"),
        ("tool-clari", "Yeah, daily at Haulmark", "ambiguous"),
        ("tool-clari", "Yes, I used Clari daily at Haulmark", "fact"),
        ("tool-clari", "Yeah, I used it daily at Haulmark", "fact"),
        ("exp-healthcare", "Yes \u2014 never below 115% of quota", "fact"),
        ("tool-clari", "Noted: I used Clari at Haulmark", "ambiguous"),  # package S
        ("exp-healthcare", "Noted: I sold to lab couriers at Haulmark", "fact"),
        ("tool-clari", "Nonstop Clari use for two years at Haulmark", "ambiguous"),  # package S
        ("exp-healthcare", "I did 25 demos a week at Haulmark", "fact"),
        (
            "expansion_ledgerline_revenue_split",
            "Built the reseller program from no partners to 17",
            "fact",
        ),
        ("tool-clari", "Ran Clari forecast calls for the NA team", "ambiguous"),  # package S
        ("tool-clari", "I ran Clari forecast calls weekly at Haulmark", "fact"),
        ("method-meddpicc", "Yes, ran every deal on it at Haulmark", "ambiguous"),
        ("method-meddpicc", "Yes, I ran MEDDPICC on every deal at Haulmark", "fact"),
        # review 2026-10-09: plain noes that became skill_index proposals
        ("tool-clari", "Definitely not", "decline"),
        ("tool-clari", "Absolutely not", "decline"),
        ("tool-clari", "Negative", "decline"),
        ("tool-clari", "Clari? no", "decline"),
        ("exp-healthcare", "Healthcare? Never.", "decline"),
        ("tool-clari", "My manager used Clari; I did not", "ambiguous"),
        ("tool-clari", "Our team used Clari; I did not", "ambiguous"),
        ("tool-clari", "Only HubSpot and Salesforce", "ambiguous"),
        # a skill answer fails closed: it states use plainly or it is refused
        ("tool-clari", "Yes \u2014 never below 115% of quota", "ambiguous"),
        ("tool-clari", "Yes, only HubSpot and Salesforce", "ambiguous"),
        ("tool-clari", "Yes, used Gong instead of Clari", "ambiguous"),
        ("tool-clari", "Yes, I know Clari without having used it", "ambiguous"),
        ("tool-clari", "Minimal Clari exposure", "ambiguous"),
        ("tool-clari", "Built the Clari rollout from no experience", "ambiguous"),
        ("tool-clari", "Clari didn't stick, we went with Gong", "ambiguous"),
        ("tool-clari", "Noted: I used it at Haulmark", "ambiguous"),
        ("tool-clari", "Nonstop for two years at Haulmark", "ambiguous"),
        ("tool-clari", "I did 25 demos a week at Haulmark", "ambiguous"),
        ("tool-outreach", "Did cold outreach daily", "ambiguous"),
        ("exp-healthcare", "Absolutely not, but I sold to banks", "ambiguous"),
    ],
)
def test_answer_classification(key: str, text: str, verdict: str) -> None:
    """A decline is the whole answer and nothing else; an answer that opens
    with a negation or denies the subject is ambiguous, and so is an answer
    to a tool or method question that is not the one accepted sentence of
    the human's own use; the rest are facts."""
    assert questions.classify_answer(text, key, places=PLACES) == verdict
    assert questions.is_decline(text, questions.subject_pattern(key)) is (verdict == "decline")


@pytest.mark.parametrize(
    "neg",
    [
        "I have never used Clari",
        "Haven't used it",
        "I've not used it",
        "Unfortunately not",
        "Not that I recall",
    ],
)
def test_a_no_never_becomes_a_proposal(
    conn: sqlite3.Connection, seeded: dict[str, int], corpus_file: Path, neg: str
) -> None:
    """Review 2026-10-09: each of these was a pending proposal to add Clari to
    skill_index, with the key marked answered, and one confirm wrote it."""
    before = corpus_file.read_text()
    res = questions.answer(conn, "tool-clari", neg)
    assert res.ok and res.declined and res.proposal_id is None
    assert questions.settled_keys(conn)["tool-clari"] == "question_declined"
    assert questions.load_proposals() == []
    assert corpus_file.read_text() == before


@pytest.mark.parametrize(
    "said",
    ["Never below 115% of quota at Haulmark", "None of my AEs churned; ran Clari weekly"],
)
def test_a_short_yes_that_opens_with_a_negation_is_refused_not_declined(
    conn: sqlite3.Connection, seeded: dict[str, int], corpus_file: Path, said: str
) -> None:
    """Review 2026-10-09: these were permanent declines. Now nothing is
    recorded, the question stays open, and the same fact after a "yes" is a
    bullet proposal. A skill answer may have no negation anywhere, so after a
    "yes" it is still refused, with the reason. An answer to any other
    question that names a tool is refused for that, with or without a "yes"
    (review 2026-10-09, skill-answers)."""
    names_tool = "Clari" in said
    for key in ("exp-healthcare", "tool-clari"):
        res = questions.answer(conn, key, said)
        assert not res.ok and "--decline" in res.text
        if key == "exp-healthcare" and names_tool:
            assert "it names Clari" in res.text, res.text
        else:
            assert "may be a no or a fact" in res.text, res.text
    assert questions.settled_keys(conn) == {}
    assert questions.load_proposals() == []
    assert {"exp-healthcare", "tool-clari"} <= {qn.key for qn in questions.aggregate(conn)}
    said_yes = f"Yes, {said[0].lower()}{said[1:]}"
    skill = questions.answer(conn, "tool-clari", said_yes)
    assert not skill.ok and "add Clari to your skill_index" in skill.text
    assert re.search(r'it contains "(?:never|none)"', skill.text)
    assert questions.load_proposals() == []
    yes = questions.answer(conn, "exp-healthcare", said_yes)
    if names_tool:
        assert not yes.ok and yes.proposal_id is None and "it names Clari" in yes.text
        assert questions.load_proposals() == []
        return
    assert yes.ok and yes.proposal_id == 1 and not yes.declined
    (p,) = questions.pending_proposals()
    assert p["text"] == said and p["kind"] == "bullet"


# review 2026-10-09: each was a skill_index proposal, and one confirm put
# Clari into skill_index, so a tailored resume could list it
DENIED_CLARI = (
    "Definitely not",
    "Absolutely not",
    "Negative",
    "Clari? no",
    "My manager used Clari; I did not",
    "Our team used Clari; I did not",
    "Only HubSpot and Salesforce",
    "Yes, only HubSpot and Salesforce",
    # package S (review 2026-10-09): each named Clari with no negation, so it
    # was a skill proposal
    "Clari? Only heard of it",
    "Clari was on my wishlist",
    "My manager used Clari",
    "Clari was rolled out after I left",
)


@pytest.mark.parametrize("said", DENIED_CLARI)
def test_a_denied_tool_never_reaches_the_skills_line(
    conn: sqlite3.Connection, seeded: dict[str, int], corpus_file: Path, said: str
) -> None:
    """The reviewer's repro: answer, then confirm, then the skills line. Filed
    as a bullet instead, the answer is refused the same way."""
    before = corpus_file.read_text()
    for kind in ("bullet", ""):
        res = questions.answer(
            conn,
            "tool-clari",
            said,
            evidence="n/a",
            metric_confidence="none",
            role_id="role_mgr",
            kind=kind,
        )
        assert res.proposal_id is None, res.text
    assert questions.load_proposals() == []
    assert not questions.confirm(conn, 1, launch=lambda c, j: "no launch").ok
    assert corpus_file.read_text() == before
    assert provenance.validate_skills_line(["Clari"], yaml.safe_load(before)) != []


def test_the_skill_rule_follows_the_entry_as_well_as_the_topic(
    conn: sqlite3.Connection, seeded: dict[str, int], corpus_file: Path
) -> None:
    assert questions.skill_bound("tool-clari") and questions.skill_bound("method-meddpicc")
    assert not questions.skill_bound("exp-healthcare")
    assert questions.skill_bound("exp-healthcare", kind="skill")
    assert questions.skill_bound("tool-clari", kind="bullet")  # a tool question stays strict
    # a question_kind read back from the proposals file does not lift it either
    assert questions.skill_bound("tool-clari", kind="bullet", question_kind="fact")
    assert not questions.skill_bound("expansion_ledgerline_revenue_split", question_kind="fact")
    # review 2026-10-09 (skill-answers): an experience answer is never a
    # skill item; filed as one it wrote "healthcare" into skill_index. Even an
    # answer in the accepted form of use is refused, and nothing is recorded
    for said in (
        "Sold to 12 lab couriers at Haulmark",
        "Yes, sold to 40 hospitals",
        "I used it daily",
    ):
        refused = questions.answer(conn, "exp-healthcare", said, kind="skill")
        assert not refused.ok and refused.proposal_id is None, refused.text
        assert "needs a tool or method question" in refused.text
        assert questions.skill_doubt(said, "exp-healthcare", places=PLACES)
    assert questions.load_proposals() == [] and questions.settled_keys(conn) == {}
    bullet = questions.answer(conn, "exp-healthcare", "Sold to 12 lab couriers at Haulmark")
    assert bullet.ok and bullet.proposal_id == 1, bullet.text
    assert questions.pending_proposals()[0]["kind"] == "bullet"


def test_a_no_code_answer_is_not_a_no(conn: sqlite3.Connection, corpus_file: Path) -> None:
    """A hyphenated "no-code" is not a no: never a decline. A tool answer must
    still be the one accepted sentence of use, so with or without a "yes" it
    is refused, not declined; "I used Clay ..." is a proposal."""
    _job(conn, "clay", fit=81, gaps=("Clay proficiency (required tool not documented)",))
    for said in (
        "No-code Clay workflows at ledgerline for 10 months",
        "Yes, no-code Clay workflows at ledgerline for 10 months",
    ):
        res = questions.answer(conn, "tool-clay", said)
        assert not res.ok and not res.declined and "not of the form" in res.text, res.text
    assert questions.settled_keys(conn) == {}
    res = questions.answer(conn, "tool-clay", "Yes, I used Clay for 10 months at ledgerline")
    assert res.ok and res.proposal_id == 1, res.text
    assert questions.settled_keys(conn) == {"tool-clay": "question_answered"}


# package S (review 2026-10-09): a skill answer is a fact only when it opens
# with an explicit first-person affirmative of use and has no negation,
# third-party subject, non-use signal or other tool anywhere
SKILL_NOT_STATED = (
    # the reviewer's repros
    "Clari? Only heard of it",
    "Clari was on my wishlist",
    "My manager used Clari",
    "Clari was rolled out after I left",
    "My manager used Clari; I did not",
    # someone else's use, after a "yes" or a first-person opener
    "Yes, my manager used it daily",
    "Yes, the team used Clari",
    "Yes, we used it at Haulmark",
    "Yes, our CRO ran it",
    "Yes, the SDRs used it daily",
    "Yes, Haulmark used it company-wide",
    "Yes, Haulmark had Clari",
    "Yes, Sarah ran it every week",
    "Yes, it was used at Haulmark",
    "Yes, Clari was in use at Haulmark",
    "Yes, used by the AE team",
    "Yes, at a company that used it",
    "I used Clari with my team",
    # knowing, training, evaluating, wanting, planning, timing, hedges
    "Yes, I've heard of it",
    "Yes, familiar with Clari",
    "Yes, I know Clari well",
    "Yes, trained on it at Haulmark",
    "Yes, Clari certified",
    "Yes, saw a demo",
    "Yes, a two-week trial",
    "Yes, we piloted it",
    "Yes, it was on my wish list",
    "Yes, planned for 2025",
    "Yes, I would use it",
    "Yes, I'd like to",
    "Yes, I'll use it at my next job",
    "Yes, it was rolled out at Haulmark",
    "Yes, after I left Haulmark",
    "Yes, maybe once",
    "Yes, once",
    "Yes, kind of",
    "Yes, a little",
    "Yes, read-only access",
    "Yes, just the dashboards",
    "Yes, but rarely",
    # no explicit first-person opener
    "Clari at Haulmark",
    "Clari was our forecasting tool",
    "Used Clari daily",
    "Ran Clari forecast calls weekly",
    "Noted: I used Clari at Haulmark",
    "Haulmark ran on Clari",
    "I ran the Clari budget",
    "I used to sell against Clari",
    "Weekly at Haulmark",
    "Yesterday's pipeline review ran on Clari",
    # another tool, even beside this one
    "Yes, Gong and Clari",
    "I used Clari alongside Salesforce",
    # package S accepted these: a bare "yes" or "daily" opener, a "?", or
    # anything between "I" and the verb (review 2026-10-09, skill-answers)
    "Yes, daily at Haulmark",
    "Yeah, weekly forecast calls at Haulmark",
    "Yep, for two years at Haulmark",
    "Daily at Haulmark",
    "Clari? Yes, daily at Haulmark",
    "Clari? Yes, I used it daily at Haulmark",
    "Yes, once a week at Haulmark",
    "Yes, I personally ran it in every forecast call",
    # a place that is not one of the human's own corpus companies
    "I used Clari daily at Acme",
    "I used Clari daily at Haulmark Labs",
    "I used Clari for 2 days",
)

# review 2026-10-09 (skill-answers): every one of these was a fact for
# tool-clari, and the reviewer confirmed several into skill_index Clari
SKILL_REVIEW_REPROS = (
    # another tool, or a name that only contains the topic's
    "Yes, I used Clarity",
    "Yes, BoostUp",
    "Yes, Clari-like tools",
    "Yes, a Clari clone",
    "Yes, Clari-style forecasting in Excel",
    "Yes, Clarity daily",
    # someone else
    "Yes, my AE",
    "Yes, the SDR next to me",
    "Yes, the rep beside me",
    "Yes, my analyst",
    "Yes, my direct reports",
    "Yes, my mentor",
    "Yes, my spouse",
    "Yes, the customer",
    "Yes, the prospect",
    "Yes, the vendor showed it",
    "Yes, a friend showed me",
    "Yes, Haulmark paid for it",
    "Yes, Haulmark subscribed",
    "Yes, IT installed it",
    "Yes, the dashboards were emailed to me",
    # non-use the word lists did not name
    "Yes, I used it zero times",
    "Yes, I used it 0 times",
    "Yes, I tried it",
    "Yes, I can use it",
    "Yes, I can learn it",
    "Yes, if hired",
    "Yes, starting next month",
    "Yes, kind-of",
    "Yes, I sold Clari",
    "Yes, I invested in Clari",
    "Yes, I worked at Clari",
    "Yes, in a roleplay",
    "Yes, hypothetically",
    "Yes, technically",
    "Yes, on paper",
    "Yes, I listened in",
    # a bare "daily" or "yes" opener
    "Daily emails from Clari",
    "Daily reports from it hit my inbox",
    "Daily standup mentioned it",
    "Daily newsletter",
    "Clari: daily",
    "Yes?",
    "Yes, jk",
    "Yes, scratch that",
    "Yes, I lied",
    "Yes, fake",
    # possessive and hyphenated words
    "Yes, my manager's Clari dashboards",
    "Yes, on my team’s Clari instance",
    "Yes, my boss's Clari account at Haulmark",
    "Yes, the CRO's Clari board",
    "Yes, the company's Clari license",
    "Yes, manager-led Clari calls",
    "Yes, team-wide Clari forecasting",
    "Yes, demo-only",
    "Yes, trial-period at Haulmark",
    "Yes, wish-listed",
)
# the same, for tools named with everyday words
SKILL_REVIEW_REPROS_OTHER = (
    ("tool-outreach", "Yes, cold outreach daily"),
    ("tool-outreach", "Yes, LinkedIn outreach for 2 years"),
    ("tool-outreach", "I used outreach daily"),
    ("tool-clay", "Yes, clay pottery daily"),
    ("tool-clay", "I used clay daily"),
    ("tool-gong", "Yes, rang the sales gong daily"),
)

SKILL_STATED = (
    "Clari: I used it weekly at Haulmark",
    "Clari - I've used it in every forecast call",
    "I used Clari weekly at Haulmark for two years",
    "Yes, I used Clari weekly at Haulmark for two years",
    "I’ve used Clari daily at Haulmark",
    "I have used Clari for forecasting at Haulmark",
    "I use Clari daily",
    "I ran Clari forecast calls weekly at Haulmark",
    "I used it daily at Haulmark",
    "Yes, I used Clari twice a week at Haulmark.",
    "Yep - I used it for 18 months at ledgerline",
    "I used Clari at Haulmark and at ledgerline",
    "I used it on a weekly basis for the past two years",
    "yes i used clari daily",
    "I used it",
)


@pytest.mark.parametrize("said", SKILL_NOT_STATED + SKILL_REVIEW_REPROS)
def test_a_skill_answer_that_does_not_state_own_use_is_ambiguous(said: str) -> None:
    assert questions.classify_answer(said, "tool-clari", places=PLACES) == "ambiguous"
    assert questions.skill_doubt(said, "tool-clari", places=PLACES)
    assert not questions.states_use(said, "tool-clari", places=PLACES)
    # filed as a skill from any question, the same rule holds
    assert questions.classify_answer(said, "exp-healthcare", skill=True, places=PLACES) != "fact"


@pytest.mark.parametrize(("key", "said"), SKILL_REVIEW_REPROS_OTHER)
def test_an_everyday_word_is_not_the_tool(key: str, said: str) -> None:
    """The topic must be named with its own pattern (case-sensitive for
    "Outreach", "Clay") in the use clause itself."""
    assert questions.classify_answer(said, key, places=PLACES) == "ambiguous"
    name = questions.TOPIC_BY_KEY[key].label
    assert questions.classify_answer(f"I used {name} daily", key, places=PLACES) == "fact"


@pytest.mark.parametrize("said", SKILL_STATED)
def test_a_skill_answer_that_states_own_use_is_a_fact(said: str) -> None:
    assert questions.skill_doubt(said, "tool-clari", places=PLACES) == ""
    assert questions.classify_answer(said, "tool-clari", places=PLACES) == "fact"


def test_a_skill_answer_may_name_only_the_humans_own_companies() -> None:
    """The places come from the corpus roles; with none, no "at ..." part is
    accepted at all."""
    assert questions.corpus_places(yaml.safe_load(CORPUS_TEXT)) == list(PLACES)
    assert questions.corpus_places({"roles": [{"id": "r"}, "x", {"company": " "}]}) == []
    said = "I used Clari daily at Haulmark"
    assert questions.classify_answer(said, "tool-clari", places=PLACES) == "fact"
    assert questions.classify_answer(said, "tool-clari") == "ambiguous"
    assert questions.classify_answer(said, "tool-clari", places=("Acme",)) == "ambiguous"
    assert questions.classify_answer("I used Clari daily", "tool-clari") == "fact"


def _pending_skill(key: str, said: str, name: str | None, jobs: list[int]) -> dict[str, Any]:
    """A pending skill proposal as an older, looser rule recorded it."""
    return {
        "id": 1,
        "key": key,
        "question_kind": questions.TOPIC_BY_KEY[key].kind if key in questions.TOPIC_BY_KEY else "",
        "question": "Have you used it?",
        "answer": said,
        "kind": "skill",
        "name": name,
        "text": said,
        "evidence": "me",
        "metric_confidence": "verified",
        "role_id": None,
        "jobs": jobs,
        "status": "pending",
        "answered_at": "2026-10-09 00:00:00",
    }


@pytest.mark.parametrize("said", SKILL_REVIEW_REPROS + SKILL_NOT_STATED[-10:])
def test_the_review_repros_never_reach_skill_index(
    conn: sqlite3.Connection, seeded: dict[str, int], corpus_file: Path, said: str
) -> None:
    """The reviewer's end-to-end check: answer, then confirm. Nothing is
    proposed; recorded under the older rule, the confirm gate refuses it."""
    before = corpus_file.read_text()
    res = questions.answer(conn, "tool-clari", said, evidence="me", metric_confidence="verified")
    assert not res.ok and res.proposal_id is None and not res.declined, res.text
    assert questions.load_proposals() == [] and questions.settled_keys(conn) == {}
    questions.save_proposals([_pending_skill("tool-clari", said, "Clari", [seeded["j1"]])])
    out = questions.confirm(conn, 1, launch=lambda c, j: "no launch")
    assert not out.ok and "reads as a no" in out.text, out.text
    assert "Yes, I used Clari weekly at Haulmark for two years" in out.text
    assert corpus_file.read_text() == before
    assert questions.load_proposals()[0]["status"] == "pending"


@pytest.mark.parametrize(("key", "said"), SKILL_REVIEW_REPROS_OTHER)
def test_an_everyday_word_never_reaches_skill_index(
    conn: sqlite3.Connection, corpus_file: Path, key: str, said: str
) -> None:
    name = questions.TOPIC_BY_KEY[key].label
    _job(conn, "j", fit=81, gaps=(f"{name} proficiency (required tool not documented)",))
    before = corpus_file.read_text()
    res = questions.answer(conn, key, said, evidence="me", metric_confidence="verified")
    assert not res.ok and res.proposal_id is None and not res.declined, res.text
    assert questions.load_proposals() == []
    questions.save_proposals([_pending_skill(key, said, name, [])])
    assert not questions.confirm(conn, 1, launch=lambda c, j: "no launch").ok
    assert corpus_file.read_text() == before


@pytest.mark.parametrize(
    ("key", "said", "name"),
    [
        # an experience answer as a skill item: "healthcare" in skill_index
        ("exp-healthcare", "I used it daily at Haulmark", "healthcare"),
        ("exp-healthcare", "I used hospitals daily", "healthcare"),
        # a skill item named after a tool the question did not ask about
        ("tool-clari", "I used Clari daily at Haulmark", "Gong"),
        ("tool-clari", "I used Clari daily at Haulmark", None),
        # a skill item for a self-review fact question
        ("expansion_ledgerline_revenue_split", "I used it daily", "expansion revenue"),
    ],
)
def test_confirm_writes_a_skill_item_only_for_its_own_tool_or_method(
    conn: sqlite3.Connection,
    seeded: dict[str, int],
    corpus_file: Path,
    key: str,
    said: str,
    name: str | None,
) -> None:
    """The proposals file is plain YAML: a skill proposal is confirmed only
    when its question is a tool or method and its name is that topic's."""
    questions.save_proposals([_pending_skill(key, said, name, [seeded["j1"]])])
    before = corpus_file.read_text()
    out = questions.confirm(conn, 1, launch=lambda c, j: "no launch")
    assert not out.ok and "needs a tool or method question" in out.text, out.text
    assert corpus_file.read_text() == before
    assert questions.load_proposals()[0]["status"] == "pending"


def test_a_tool_bullet_stays_strict_whatever_the_proposals_file_says(
    conn: sqlite3.Connection, seeded: dict[str, int], corpus_file: Path
) -> None:
    """A tool question's answer filed as a bullet is held to the skill rule
    even when the proposals file records the question as a plain fact."""
    p = _pending_skill("tool-clari", "Yes, my AE", None, [seeded["j1"]])
    questions.save_proposals([{**p, "kind": "bullet", "question_kind": "fact"}])
    before = corpus_file.read_text()
    out = questions.confirm(conn, 1, role_id="role_mgr", launch=lambda c, j: "no launch")
    assert not out.ok and "reads as a no" in out.text, out.text
    assert corpus_file.read_text() == before


# review 2026-10-09 (skill-answers): an answer to a question that is not the
# tool's own (a self-review fact, an experience) went through the plain
# decline check, became a bullet naming the tool, closed the tool's own
# question, and sourced "At Haulmark I ran Clari forecast calls every week."
TOOL_IN_OTHER_ANSWER = (
    # the reviewer's repros: (a) a self-review fact key about a tool
    # ("clari_forecast_cadence" normalizes to it), (b) an experience key
    ("cadence_clari_forecast", "Yes, my manager ran Clari forecast calls at Haulmark"),
    (
        "exp-healthcare",
        "Yes, sold to 12 lab couriers at Haulmark; the team used Clari for the forecast",
    ),
    # the human's own use, too: it enters only through the tool's own question
    ("cadence_clari_forecast", "Yes, I ran Clari forecast calls weekly at Haulmark"),
    ("exp-healthcare", "Yes, sold to 12 lab couriers at Haulmark using MEDDPICC"),
    ("exp-sdr-bdr-team-management", "Yes, managed 4 SDRs at Haulmark on Outreach sequences"),
    ("expansion_ledgerline_revenue_split", "About half of it, tracked in HubSpot by the team"),
    # an alias, and a tool the corpus already indexes: refused all the same
    ("exp-healthcare", "Yes, 12 lab couriers at Haulmark, all logged in SFDC"),
)


def _clari_cadence_question(conn: sqlite3.Connection, job_id: int) -> None:
    _self_review_question(
        conn, job_id, "clari_forecast_cadence", "How often did you run Clari forecast calls?"
    )


@pytest.mark.parametrize(("key", "said"), TOOL_IN_OTHER_ANSWER)
def test_an_answer_naming_a_tool_is_refused_whatever_the_question(
    conn: sqlite3.Connection, seeded: dict[str, int], corpus_file: Path, key: str, said: str
) -> None:
    """A tool or method named in any answer but its own question's is
    refused, filed as a bullet or not, and nothing is recorded: the tool's own
    question stays open and is the only way in."""
    _clari_cadence_question(conn, seeded["j1"])
    assert questions.normalize_key("clari_forecast_cadence") == "cadence_clari_forecast"
    assert questions.classify_answer(said, key, places=PLACES) == "ambiguous"
    assert questions.classify_answer(said, key, skill=False, places=PLACES) == "ambiguous"
    before = corpus_file.read_text()
    for kind in ("", "bullet"):
        res = questions.answer(
            conn,
            key,
            said,
            evidence="me",
            metric_confidence="none",
            role_id="role_mgr",
            kind=kind,
        )
        assert not res.ok and res.proposal_id is None and not res.declined, res.text
        assert "it names " in res.text and "--decline" in res.text, res.text
    assert questions.load_proposals() == [] and questions.settled_keys(conn) == {}
    assert questions.confirm(conn, 1, launch=lambda c, j: "no launch").ok is False
    assert corpus_file.read_text() == before
    assert {"tool-clari", key} <= {qn.key for qn in questions.aggregate(conn)}


def test_the_tool_refusal_points_at_the_tools_own_question(
    conn: sqlite3.Connection, seeded: dict[str, int], corpus_file: Path
) -> None:
    _clari_cadence_question(conn, seeded["j1"])
    res = questions.answer(
        conn, "cadence_clari_forecast", "Yes, my manager ran Clari forecast calls at Haulmark"
    )
    assert not res.ok and "it names Clari" in res.text
    assert "from its own question (tool-clari)" in res.text
    assert '`jobkit answer tool-clari "Yes, I used Clari weekly at Haulmark for two years"`' in (
        res.text
    )
    # the same fact without the tool is a bullet proposal; the tool question,
    # answered in the one accepted form, is a skill proposal
    ok = questions.answer(conn, "exp-healthcare", "Yes, sold to 12 lab couriers at Haulmark")
    assert ok.ok and ok.proposal_id == 1, ok.text
    tool = questions.answer(conn, "tool-clari", "Yes, I used Clari weekly at Haulmark")
    assert tool.ok and tool.proposal_id == 2, tool.text
    # an everyday word is not the tool: lowercase "outreach" is not Outreach
    plain = questions.answer(
        conn,
        "exp-sdr-bdr-team-management",
        "Yes, managed 4 SDRs doing cold outreach at Haulmark",
    )
    assert plain.ok and plain.proposal_id == 3, plain.text


@pytest.mark.parametrize(("key", "said"), TOOL_IN_OTHER_ANSWER)
@pytest.mark.parametrize("where", ["answer", "text"])
def test_confirm_refuses_a_bullet_that_names_a_tool(
    conn: sqlite3.Connection,
    seeded: dict[str, int],
    corpus_file: Path,
    key: str,
    said: str,
    where: str,
) -> None:
    """A bullet proposal recorded before this rule (or with only its text
    edited in the plain-YAML proposals file) never reaches the corpus."""
    clean = "Sold to 12 lab couriers at Haulmark"
    recorded = {"answer": said, "text": said} if where == "answer" else {"answer": clean}
    questions.save_proposals(
        [
            {
                "id": 1,
                "key": key,
                "question_kind": questions.TOPIC_BY_KEY[key].kind
                if key in questions.TOPIC_BY_KEY
                else "fact",
                "question": "Tell us more?",
                "answer": recorded["answer"],
                "kind": "bullet",
                "name": None,
                "text": recorded.get("text", said),
                "evidence": "me",
                "metric_confidence": "verified",
                "role_id": "role_mgr",
                "jobs": [seeded["j1"]],
                "status": "pending",
                "answered_at": "2026-10-09 00:00:00",
            }
        ]
    )
    before = corpus_file.read_text()
    out = questions.confirm(conn, 1, launch=lambda c, j: "no launch")
    assert not out.ok and f"your answer to '{key}' names " in out.text, out.text
    assert "from its own question (tool-" in out.text or "(method-" in out.text
    assert corpus_file.read_text() == before
    assert questions.load_proposals()[0]["status"] == "pending"


def test_a_bullet_naming_a_tool_never_closes_the_tools_question(
    conn: sqlite3.Connection, corpus_file: Path
) -> None:
    """Only a skill_index key that IS the tool (or a never-claim list)
    answers a tool or method question: not a bullet, a role, or a
    skill_index value that names it in passing (review 2026-10-09,
    skill-answers: confirming "My manager ran Clari forecast calls at
    Haulmark" dropped tool-clari, so the human was never asked)."""
    corpus_file.write_text(
        CORPUS_TEXT.replace(
            "  MEDDIC: ran own deals on it\n",
            "  MEDDIC: ran own deals on it, with Gong call reviews\n  Salesforce CPQ: quotes\n",
        ).replace(
            '    evidence: "migration doc"\n',
            '    evidence: "migration doc"\n'
            "  - id: b_003\n"
            "    role_id: role_mgr\n"
            "    text_long: My manager ran Clari forecast calls at Haulmark\n"
            "    text_short: My manager ran Clari forecast calls at Haulmark\n"
            "    tags: [cadence-clari-forecast]\n"
            "    metric_confidence: none\n"
            "    evidence: me\n",
        )
    )
    corpus = questions.load_corpus()
    assert "Clari" in questions.corpus_fact_text(corpus)
    _job(conn, "a", gaps=(CLARI_GAP, "Gong reviews", "MEDDIC", "Salesforce CPQ", HEALTH_GAP))
    keys = {qn.key for qn in questions.aggregate(conn)}
    assert keys == {"tool-clari", "tool-gong", "exp-healthcare"}
    clari = questions.TOPIC_BY_KEY["tool-clari"]
    assert not questions.topic_answered(clari, corpus)
    assert not questions.in_skill_index(clari, corpus)
    # "Salesforce CPQ" is not Salesforce; the key "Salesforce" is
    sfdc = questions.TOPIC_BY_KEY["tool-salesforce"]
    assert questions.in_skill_index(sfdc, corpus)
    assert not questions.in_skill_index(sfdc, {"skill_index": {"Salesforce CPQ": "x"}})
    assert questions.in_skill_index(sfdc, {"skill_index": {" SFDC ": "x"}})


def test_the_skill_rule_reads_the_topic_name_as_it() -> None:
    """A topic name that contains a signal word ("Force Management") does not
    trip the rule; the name of another tool does."""
    key = "method-command-of-the-message"
    assert questions.classify_answer("I used Force Management on every deal", key) == "fact"
    assert questions.classify_answer("I used MEDDPICC on every deal", "method-meddpicc") == "fact"
    doubt = questions.skill_doubt("Yes, MEDDPICC and Sandler", "method-meddpicc")
    assert doubt == "it names Sandler; answer about MEDDPICC alone"


@pytest.mark.parametrize(
    ("said", "reason"),
    [
        ("Clari? Only heard of it", 'it contains "only"'),
        ("Clari was on my wishlist", 'it contains "wishlist"'),
        ("My manager used Clari", 'it contains "manager"'),
        ("Clari was rolled out after I left", 'it contains "rolled"'),
        ("Yes, Haulmark used it", 'it contains "haulmark used"'),
        ("Clari at Haulmark", 'it is not of the form "I used Clari ..."'),
        # review 2026-10-09 (skill-answers)
        ("Yes, daily at Haulmark", 'it is not of the form "I used Clari ..."'),
        ("Daily emails from Clari", 'it is not of the form "I used Clari ..."'),
        ("Yes, I used Clarity", 'it is not of the form "I used Clari ..."'),
        ("Yes, my AE", 'it contains "ae"'),
        ("Yes, my manager's Clari dashboards", 'it contains "manager\'s"'),
        ("Yes, team-wide Clari forecasting", 'it contains "team-wide"'),
        ("Yes, I tried it", 'it contains "tried"'),
        ("Clari? Yes, I used it daily", 'it contains "?"'),
        ("I used Clari daily at Acme", 'it is not of the form "I used Clari ..."'),
    ],
)
def test_a_skill_refusal_names_the_reason_and_records_nothing(
    conn: sqlite3.Connection, seeded: dict[str, int], corpus_file: Path, said: str, reason: str
) -> None:
    before = corpus_file.read_text()
    for kind in ("", "skill", "bullet"):
        res = questions.answer(
            conn,
            "tool-clari",
            said,
            evidence="me",
            metric_confidence="none",
            role_id="role_mgr",
            kind=kind,
        )
        assert not res.ok and not res.declined and res.proposal_id is None, res.text
        assert reason in res.text and "add Clari to your skill_index" in res.text
        assert '"Yes, I used Clari weekly at Haulmark for two years"' in res.text
        assert "at Haulmark / at ledgerline" in res.text  # the human's own companies
    assert questions.load_proposals() == []
    assert questions.settled_keys(conn) == {}
    assert "tool-clari" in {qn.key for qn in questions.aggregate(conn)}
    assert corpus_file.read_text() == before


@pytest.mark.parametrize(
    ("key", "said", "kind"),
    [
        ("tool-clari", "I have never used Clari", "skill"),
        ("tool-clari", "Haven't used it", "skill"),
        ("exp-healthcare", "Never below 115% of quota at Haulmark", "bullet"),
        *(("tool-clari", said, "skill") for said in DENIED_CLARI),
        ("tool-clari", "Yes \u2014 never below 115% of quota", "skill"),
        ("tool-clari", "My manager used Clari; I did not", "bullet"),
    ],
)
def test_confirm_refuses_a_proposal_that_reads_as_a_no(
    conn: sqlite3.Connection,
    seeded: dict[str, int],
    corpus_file: Path,
    key: str,
    said: str,
    kind: str,
) -> None:
    """A proposal recorded before the review fix (the file is the human's own
    data) is refused at the gate: one confirm used to write 'Clari' into
    skill_index from "I have never used Clari"."""
    questions.save_proposals(
        [
            {
                "id": 1,
                "key": key,
                "question_kind": "tool" if key.startswith("tool-") else "experience",
                "question": "Have you used it?",
                "answer": said,
                "kind": kind,
                "name": "Clari" if kind == "skill" else None,
                "text": said,
                "evidence": None,
                "metric_confidence": None,
                "role_id": None,
                "jobs": [seeded["j1"]],
                "status": "pending",
                "answered_at": "2026-10-09 00:00:00",
            }
        ]
    )
    before = corpus_file.read_text()
    out = questions.confirm(
        conn,
        1,
        evidence="me",
        metric_confidence="none",
        role_id="role_mgr",
        launch=lambda c, j: "no launch",
    )
    assert not out.ok and "reads as a no" in out.text and "--decline" in out.text
    # the exact text confirming would write, whole (here it is the answer)
    assert f'confirming would write exactly "{said}".' in out.text
    assert corpus_file.read_text() == before
    (p,) = questions.load_proposals()
    assert p["status"] == "pending" and p["evidence"] is None  # nothing saved either
    # declining it closes the proposal
    assert questions.answer(conn, key, "", decline=True).declined
    assert questions.load_proposals()[0]["status"] == "declined"


def test_explicit_decline_and_undecline(
    conn: sqlite3.Connection, seeded: dict[str, int], corpus_file: Path
) -> None:
    # decline=True needs no text, and refuses a text that reads as a fact
    said = "Yes, I used Clari daily at Haulmark"
    refused = questions.answer(conn, "tool-clari", said, decline=True)
    assert not refused.ok and "reads as a fact" in refused.text
    assert questions.settled_keys(conn) == {}
    res = questions.answer(conn, "tool-clari", "", decline=True)
    assert res.ok and res.declined and "jobkit undecline tool-clari" in res.text
    ev = conn.execute("SELECT detail FROM events WHERE kind = 'question_declined'").fetchone()
    assert json.loads(ev[0])["explicit"] is True
    # an explicit decline of an ambiguous answer is the human's call
    assert questions.answer(conn, "exp-healthcare", "Never formally", decline=True).declined
    assert "tool-clari" not in {qn.key for qn in questions.aggregate(conn)}
    again = questions.answer(conn, "tool-clari", said)
    assert not again.ok and "jobkit undecline tool-clari" in again.text

    assert not questions.undecline(conn, "tool-zoominfo").ok  # never declined
    back = questions.undecline(conn, "tool-clari")
    assert back.ok and "open again" in back.text
    assert questions.settled_keys(conn) == {"exp-healthcare": "question_declined"}
    assert "tool-clari" in {qn.key for qn in questions.aggregate(conn)}
    assert not questions.undecline(conn, "tool-clari").ok  # not declined any more
    yes = questions.answer(conn, "tool-clari", said)
    assert yes.ok and yes.proposal_id == 1
    assert questions.settled_keys(conn)["tool-clari"] == "question_answered"
    # a later decline wins again, and an undecline after it reopens
    assert questions.answer(conn, "tool-clari", "no").declined
    assert questions.settled_keys(conn)["tool-clari"] == "question_declined"


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        ("Yes — used Clari daily", "Used Clari daily"),
        ("yep, at Haulmark", "At Haulmark"),
        ("I did 25 demos a week at Haulmark", "I did 25 demos a week at Haulmark"),
        ("Yesterday's pipeline review ran on Clari", "Yesterday's pipeline review ran on Clari"),
    ],
)
def test_answer_text_drops_only_a_leading_yes(raw: str, text: str) -> None:
    assert questions.answer_text(raw) == text


def test_ambiguous_answer_records_nothing(
    conn: sqlite3.Connection, seeded: dict[str, int], corpus_file: Path
) -> None:
    res = questions.answer(conn, "tool-clari", "No, but I used HubSpot forecasting daily")
    assert not res.ok and "may be a no or a fact" in res.text
    assert questions.load_proposals() == []
    assert questions.settled_keys(conn) == {}
    assert "tool-clari" in {qn.key for qn in questions.aggregate(conn)}


def test_answer_makes_a_proposal_and_confirm_writes_a_skill_item(
    conn: sqlite3.Connection,
    seeded: dict[str, int],
    corpus_file: Path,
    spawned: SpawnRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(questions, "_today", lambda: TODAY)
    before = corpus_file.read_text()
    res = questions.answer(
        conn,
        "tool-clari",
        "Yes — I used Clari daily for forecast calls at Haulmark",
        evidence="Clari forecast exports",
        metric_confidence="verified",
    )
    assert res.ok and res.proposal_id == 1
    assert "skill_index → Clari" in res.text
    said = "Yes — I used Clari daily for forecast calls at Haulmark"
    text = "I used Clari daily for forecast calls at Haulmark"
    # the exact text the entry writes, and the whole answer beside it
    shown = f'skill_index Clari, writing exactly: "{text}" (your answer, verbatim: "{said}")'
    assert shown in res.text
    # a proposal is not a corpus edit
    assert corpus_file.read_text() == before
    (p,) = questions.load_proposals()
    assert p["status"] == "pending" and p["kind"] == "skill" and p["name"] == "Clari"
    assert p["text"] == "I used Clari daily for forecast calls at Haulmark"
    assert p["answer"].startswith("Yes —")  # verbatim answer kept beside it
    assert p["jobs"] == [seeded["j1"], seeded["j2"], seeded["j3"]]
    kinds = [r[0] for r in conn.execute("SELECT kind FROM events WHERE job_id IS NULL")]
    assert "corpus_proposal" in kinds and "question_answered" in kinds
    assert "tool-clari" not in {qn.key for qn in questions.aggregate(conn)}  # answered

    done = questions.confirm(conn, 1)
    assert done.ok, done.text
    assert f"wrote {shown} to" in done.text  # the name next to the exact text written
    after = corpus_file.read_text()
    for comment in (
        "# career corpus fixture",
        "# confirmed by the human, 2026-10-08",
        "# ---- Haulmark · Regional Sales Manager ----",
        "# voice samples follow",
    ):
        assert comment in after
    new = yaml.safe_load(after)
    old = yaml.safe_load(before)
    value = new["skill_index"].pop("Clari")
    assert new == old  # exactly one entry added, nothing else touched
    assert value.startswith("I used Clari daily for forecast calls at Haulmark")
    assert "evidence: Clari forecast exports" in value
    assert "metric_confidence: verified" in value and "confirmed 2026-10-09" in value
    assert (corpus_file.parent / "career_corpus.yaml.bak").read_text() == before
    (p,) = questions.load_proposals()
    assert p["status"] == "confirmed" and p["written"] == "skill_index Clari"
    ev = conn.execute("SELECT detail FROM events WHERE kind = 'proposal_confirmed'").fetchone()
    assert json.loads(ev[0])["written"] == "skill_index Clari"
    # the confirmation re-scores the jobs the question listed, in the background
    ids = ",".join(str(seeded[k]) for k in ("j1", "j2", "j3"))
    assert spawned.calls == [background.cli_argv("rescore", "--job-ids", ids)]
    # a confirmed proposal cannot be confirmed twice
    assert not questions.confirm(conn, 1).ok


HEALTHCARE_ANSWER = (
    "I sold route planning to 12 lab couriers that carry specimens for testing labs at Haulmark"
)


def test_bullet_proposal_needs_human_evidence_confidence_and_role(
    conn: sqlite3.Connection,
    seeded: dict[str, int],
    corpus_file: Path,
    spawned: SpawnRecorder,
) -> None:
    before = corpus_file.read_text()
    res = questions.answer(conn, "exp-healthcare", HEALTHCARE_ANSWER)
    assert res.ok and "evidence" in res.text and "role_id" in res.text
    (p,) = questions.pending_proposals()
    assert p["kind"] == "bullet"
    assert p["evidence"] is None and p["metric_confidence"] is None  # never defaulted

    refused = questions.confirm(conn, p["id"])
    assert not refused.ok
    for missing in ("evidence", "metric_confidence", "role_id"):
        assert missing in refused.text
    assert corpus_file.read_text() == before

    bad = questions.confirm(conn, p["id"], metric_confidence="probably")
    assert not bad.ok and "metric_confidence must be one of" in bad.text

    partial = questions.confirm(
        conn, p["id"], evidence="carrier contracts", metric_confidence="estimate"
    )
    assert not partial.ok and "role_id" in partial.text
    assert corpus_file.read_text() == before
    (p,) = questions.pending_proposals()
    assert p["evidence"] == "carrier contracts" and p["metric_confidence"] == "estimate"

    done = questions.confirm(conn, p["id"], role_id="role_mgr")
    assert done.ok, done.text
    new = yaml.safe_load(corpus_file.read_text())
    b = new["bullets"][-1]
    assert b == {
        "id": "b_003",
        "role_id": "role_mgr",
        "text_long": HEALTHCARE_ANSWER,
        "text_short": HEALTHCARE_ANSWER,
        "tags": ["healthcare"],
        "metric": "12",
        "metric_confidence": "estimate",
        "evidence": "carrier contracts",
    }
    assert new["bullets"][:-1] == yaml.safe_load(before)["bullets"]
    assert "# ---- Haulmark · Regional Sales Manager ----" in corpus_file.read_text()
    # the new bullet sits inside the bullets block, before the next block's comment
    text = corpus_file.read_text()
    assert text.index("id: b_003") < text.index("# voice samples follow")
    assert len(spawned.calls) == 1


# Review 2026-10-09: confirm checked the proposal's `answer` but wrote its
# `text`, and showed the answer. The proposals file is plain YAML, so a text
# edited apart from its answer was written unchecked. Each (answer, text) below
# has an answer that reads as a fact and a text that does not.
TEXT_READS_AS_NO = [
    pytest.param(
        "tool-clari",
        "skill",
        "Yes, I used Clari weekly at Haulmark",
        "I have never used Clari",
        id="skill-text-is-a-decline",
    ),
    pytest.param(
        "tool-clari",
        "skill",
        "Yes, I used Clari weekly at Haulmark",
        "My manager used Clari weekly at Haulmark",
        id="skill-text-names-another-user",
    ),
    pytest.param(
        "tool-clari",
        "skill",
        "Yes, I used Clari weekly at Haulmark",
        "I used Clari weekly at Haulmark; I did not",
        id="skill-text-retracts",
    ),
    pytest.param(
        "tool-clari", "skill", "Yes, I used Clari weekly at Haulmark", "", id="skill-empty"
    ),
    pytest.param(
        "exp-healthcare",
        "bullet",
        "Yes, sold to 12 lab couriers at Haulmark",
        "Never sold to lab couriers at Haulmark",
        id="bullet-text-opens-negated",
    ),
    pytest.param(
        "exp-healthcare",
        "bullet",
        "Yes, sold to 12 lab couriers at Haulmark",
        "No, never sold into healthcare",
        id="bullet-text-is-a-decline",
    ),
    pytest.param(
        "exp-healthcare",
        "bullet",
        "Yes, sold to 12 lab couriers at Haulmark",
        "Sold to 12 lab couriers at Haulmark; never used it though",
        id="bullet-text-denies",
    ),
    pytest.param("exp-healthcare", "bullet", "Yes, no.", "No.", id="bullet-yes-no"),
    pytest.param(
        "exp-healthcare", "bullet", "Yes, sold to 12 lab couriers at Haulmark", "", id="empty"
    ),
]


def _proposal(key: str, kind: str, said: str, text: str, jobs: list[int]) -> dict[str, Any]:
    return {
        "id": 1,
        "key": key,
        "question_kind": "tool" if key.startswith("tool-") else "experience",
        "question": "Have you?",
        "answer": said,
        "kind": kind,
        "name": "Clari" if kind == "skill" else None,
        "text": text,
        "evidence": "me",
        "metric_confidence": "verified",
        "role_id": "role_mgr" if kind == "bullet" else None,
        "jobs": jobs,
        "status": "pending",
        "answered_at": "2026-10-09 00:00:00",
    }


@pytest.mark.parametrize(("key", "kind", "said", "text"), TEXT_READS_AS_NO)
def test_confirm_checks_and_shows_the_exact_text_it_would_write(
    conn: sqlite3.Connection,
    seeded: dict[str, int],
    corpus_file: Path,
    key: str,
    kind: str,
    said: str,
    text: str,
) -> None:
    p = _proposal(key, kind, said, text, [seeded["j1"]])
    assert questions.classify_answer(said, key, places=PLACES) == "fact"  # the answer passes
    assert questions.reads_as_no(p, places=PLACES)
    assert questions.text_reads_as_no(p, places=PLACES)
    questions.save_proposals([p])
    before = corpus_file.read_text()
    out = questions.confirm(conn, 1, launch=lambda c, j: "no launch")
    assert not out.ok and "reads as a no" in out.text, out.text
    # the message shows the exact text it would have written, and the answer
    assert f'confirming would write exactly "{text}" (your answer, verbatim: "{said}")' in (
        out.text
    )
    assert corpus_file.read_text() == before
    assert questions.load_proposals()[0]["status"] == "pending"


def test_confirm_refuses_a_text_naming_a_tool_and_shows_it(
    conn: sqlite3.Connection, seeded: dict[str, int], corpus_file: Path
) -> None:
    said, text = (
        "Yes, sold to 12 lab couriers at Haulmark",
        "Sold to 12 lab couriers at Haulmark with Clari",
    )
    questions.save_proposals([_proposal("exp-healthcare", "bullet", said, text, [])])
    before = corpus_file.read_text()
    out = questions.confirm(conn, 1, launch=lambda c, j: "no launch")
    assert not out.ok and "names Clari" in out.text
    assert f'confirming would write exactly "{text}" (your answer, verbatim: "{said}")' in (
        out.text
    )
    assert corpus_file.read_text() == before


def test_an_answer_whose_text_is_a_no_is_never_proposed(
    conn: sqlite3.Connection, seeded: dict[str, int], corpus_file: Path
) -> None:
    """ "Yes, no." reads as a fact, but its text, with the "yes" dropped, is
    "No.": nothing is recorded, and the reply shows the text."""
    for said in ("Yes, no.", "Yes, never.", "Yep, not really"):
        assert questions.classify_answer(said, "exp-healthcare", places=PLACES) == "fact"
        res = questions.answer(conn, "exp-healthcare", said)
        assert not res.ok and res.proposal_id is None and not res.declined, res.text
        assert f'written as exactly "{questions.answer_text(said)}"' in res.text
    assert questions.load_proposals() == []
    assert questions.settled_keys(conn) == {}


def test_a_yes_before_a_negation_still_confirms_with_its_exact_text(
    conn: sqlite3.Connection, seeded: dict[str, int], corpus_file: Path
) -> None:
    """The one excused text: the answer less its "yes", a fact that opens with
    a negation and denies nothing. Confirm writes it and shows it exactly."""
    said = "Yes — never below 115% of quota at Haulmark"
    res = questions.answer(
        conn,
        "exp-healthcare",
        said,
        evidence="quota reports",
        metric_confidence="verified",
        role_id="role_mgr",
    )
    assert res.ok and res.proposal_id == 1, res.text
    (p,) = questions.pending_proposals()
    text = "Never below 115% of quota at Haulmark"
    assert p["text"] == text and not questions.text_reads_as_no(p, places=PLACES)
    done = questions.confirm(conn, 1, launch=lambda c, j: "no launch")
    assert done.ok, done.text
    assert f'wrote bullet b_003, writing exactly: "{text}" (your answer, verbatim: "{said}")' in (
        done.text
    )
    assert yaml.safe_load(corpus_file.read_text())["bullets"][-1]["text_long"] == text
    # the same text beside an answer it was not derived from is not excused
    edited = {**p, "answer": "Yes, sold to 12 lab couriers at Haulmark"}
    assert questions.text_reads_as_no(edited, places=PLACES)


# Review 2026-10-09 (third pass, Hard Rule 8): the "yes" excuse let a denial
# into the corpus. "Yes, not in healthcare." became proposal 1, and one
# confirm wrote bullet b_003 "Not in healthcare.", tagged exp-healthcare, and
# started a rescore; "Yes, no healthcare at all." wrote "No healthcare at
# all.". Only a threshold and a figure may open the written text with a
# negation.
YES_THEN_DENIAL = ["Yes, not in healthcare.", "Yes, no healthcare at all."]


@pytest.mark.parametrize("said", YES_THEN_DENIAL)
def test_a_yes_before_a_denial_is_never_proposed(
    conn: sqlite3.Connection, seeded: dict[str, int], corpus_file: Path, said: str
) -> None:
    """The reviewer's repro, end to end: answer, then confirm."""
    before = corpus_file.read_text()
    assert questions.classify_answer(said, "exp-healthcare", places=PLACES) == "fact"
    res = questions.answer(
        conn,
        "exp-healthcare",
        said,
        evidence="x",
        metric_confidence="verified",
        role_id="role_mgr",
    )
    assert not res.ok and res.proposal_id is None and not res.declined, res.text
    assert f'written as exactly "{questions.answer_text(said)}"' in res.text
    assert "only a threshold" in res.text
    assert questions.load_proposals() == []
    assert questions.settled_keys(conn) == {}
    assert "exp-healthcare" in {qn.key for qn in questions.aggregate(conn)}
    out = questions.confirm(conn, 1, launch=lambda c, j: "no launch")
    assert not out.ok, out.text
    assert corpus_file.read_text() == before


@pytest.mark.parametrize("said", YES_THEN_DENIAL)
def test_confirm_refuses_a_yes_before_a_denial_recorded_earlier(
    conn: sqlite3.Connection, seeded: dict[str, int], corpus_file: Path, said: str
) -> None:
    """A proposal recorded under the looser rule (the file is plain YAML) is
    refused at the gate, with the exact text it would have written."""
    text = questions.answer_text(said)
    p = _proposal("exp-healthcare", "bullet", said, text, [seeded["j1"]])
    assert questions.text_reads_as_no(p, places=PLACES)
    assert questions.reads_as_no(p, places=PLACES)
    questions.save_proposals([p])
    before = corpus_file.read_text()
    out = questions.confirm(conn, 1, launch=lambda c, j: "no launch")
    assert not out.ok and "reads as a no" in out.text, out.text
    assert f'confirming would write exactly "{text}" (your answer, verbatim: "{said}")' in (
        out.text
    )
    assert "only a threshold" in out.text
    assert corpus_file.read_text() == before
    assert questions.load_proposals()[0]["status"] == "pending"


@pytest.mark.parametrize(
    ("said", "excused"),
    [
        # a threshold and a figure: the one excused opening
        ("Yes — never below 115% of quota at Haulmark", True),
        ("Yes, not below 100% of quota in any quarter at Haulmark", True),
        ("Yes, never fewer than 12 meetings a week at Haulmark", True),
        ("Yes, never less than $300K a month at Haulmark", True),
        ("Yes, not under 95% attainment at Haulmark", True),
        # anything else that opens with a negation is a denial, or may be one
        ("Yes, not in healthcare.", False),
        ("Yes, no healthcare at all.", False),
        ("Yes, never below quota at Haulmark", False),  # no figure
        ("Yes, not once below quota", False),
        ("Yes, never missed quota at Haulmark", False),
        ("Yes, none of my AEs churned", False),
        ("Yes, never below 115% of quota; not in healthcare", False),
        ("Yes, never below 115% of quota, but I didn't sell to lab couriers", False),
    ],
)
def test_only_a_threshold_may_open_the_written_text_with_a_negation(
    said: str, excused: bool
) -> None:
    text = questions.answer_text(said)
    p = _proposal("exp-healthcare", "bullet", said, text, [])
    assert questions.classify_answer(said, "exp-healthcare", places=PLACES) == "fact"
    assert questions.text_reads_as_no(p, places=PLACES) is (not excused), text


def test_confirm_shows_an_edited_text_that_passes_exactly(
    conn: sqlite3.Connection, seeded: dict[str, int], corpus_file: Path
) -> None:
    """A text edited apart from its answer that passes every rule is written,
    and the reply shows exactly that text beside the answer."""
    said, text = (
        "Yes, sold to 12 lab couriers at Haulmark",
        "Sold to 12 lab couriers at Haulmark in 2023",
    )
    questions.save_proposals([_proposal("exp-healthcare", "bullet", said, text, [])])
    out = questions.confirm(conn, 1, launch=lambda c, j: "no launch")
    assert out.ok, out.text
    assert f'writing exactly: "{text}" (your answer, verbatim: "{said}")' in out.text
    assert yaml.safe_load(corpus_file.read_text())["bullets"][-1]["text_long"] == text


def test_never_claim_constraint_blocks_confirmation(
    conn: sqlite3.Connection, corpus_file: Path
) -> None:
    jid = _job(conn, "k8s", fit=77)
    _self_review_question(conn, jid, "cluster_admin_role", "Did you run the cluster yourself?")
    before = corpus_file.read_text()
    res = questions.answer(
        conn,
        "admin_cluster_role",
        "Yes, I ran our Kubernetes cluster at ledgerline",
        evidence="ops runbook",
        metric_confidence="none",
        role_id="role_ops",
    )
    assert res.ok, res.text
    out = questions.confirm(conn, res.proposal_id or 0, launch=lambda c, j: "no launch")
    assert not out.ok and "never to claim 'Kubernetes'" in out.text
    assert corpus_file.read_text() == before


def test_reanswer_supersedes_and_bad_inputs_record_nothing(
    conn: sqlite3.Connection, seeded: dict[str, int], corpus_file: Path
) -> None:
    first = questions.answer(conn, "tool-clari", "I used Clari at Haulmark for forecasting")
    second = questions.answer(conn, "tool-clari", "Yes, I used it in weekly forecast calls")
    assert first.ok and second.ok
    statuses = {p["id"]: p["status"] for p in questions.load_proposals()}
    assert statuses == {1: "superseded", 2: "pending"}
    assert not questions.answer(conn, "tool-nonexistent", "yes").ok
    assert not questions.answer(conn, "exp-healthcare", "Yes").ok  # a bare yes is no fact
    assert not questions.answer(conn, "exp-healthcare", "Yes, a lab courier", role_id="nope").ok
    assert not questions.answer(
        conn, "expansion_ledgerline_revenue_split", "about a third", kind="skill"
    ).ok
    assert len(questions.load_proposals()) == 2


def test_insert_into_block_appends_or_opens_blocks() -> None:
    text = "a: 1\nskill_index: {}\nbullets:\n- id: b_001\n  text_long: x\n"
    out = questions.insert_into_block(text, "skill_index", "Gong: daily\n", list_block=False)
    assert yaml.safe_load(out)["skill_index"] == {"Gong": "daily"}
    out = questions.insert_into_block(out, "bullets", "- id: b_002\n", list_block=True)
    assert [b["id"] for b in yaml.safe_load(out)["bullets"]] == ["b_001", "b_002"]
    out = questions.insert_into_block("a: 1", "skill_index", "Gong: daily\n", list_block=False)
    assert yaml.safe_load(out) == {"a": 1, "skill_index": {"Gong": "daily"}}
    with pytest.raises(questions.CorpusWriteError):
        questions.insert_into_block(
            "skill_index: {Gong: x}\n", "skill_index", "a: b\n", list_block=False
        )


# ---------------------------------------------------------------------------
# rescore
# ---------------------------------------------------------------------------

NEW_SCORES = {"Alpha": 50, "Charlie": 80, "Delta": 75, "Golf": 40, "Hotel": 66, "Bravo": 70}


@pytest.fixture()
def fake_scorer(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The real llm.call over a fake `claude -p`: the score comes from the
    job title in the rendered prompt. Records each title scored."""
    calls: list[str] = []

    def backend(stage: str, prompt: str) -> tuple[str, dict[str, Any]]:
        assert stage == "score"
        m = re.search(r"JOB TITLE: (\w+)", prompt)
        assert m is not None
        calls.append(m.group(1))
        return json.dumps({"score": NEW_SCORES[m.group(1)], "rationale": "r", "gaps": []}), {}

    monkeypatch.setattr(llm, "_call_claude_cli", backend)
    monkeypatch.setattr(triage, "_profile_summary", lambda: "EXPERIENCE: test profile")
    return calls


@pytest.fixture()
def rules_calls(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    ran: list[bool] = []

    def fake_rules(c: sqlite3.Connection) -> dict[str, int]:
        ran.append(True)
        return {"evaluated": 0, "auto_approved": 0, "auto_rejected": 0}

    monkeypatch.setattr(rescore, "run_standing_rules", fake_rules)
    return ran


def _profile_event(conn: sqlite3.Connection, job_id: int, h: str) -> None:
    conn.execute(
        q.RECORD_EVENT,
        {
            "job_id": job_id,
            "kind": triage.SCORE_PROFILE_EVENT,
            "detail": json.dumps({"profile_hash": h, "prompt_version": "1.1"}),
        },
    )


@pytest.fixture()
def rescore_jobs(conn: sqlite3.Connection, corpus_file: Path) -> dict[str, int]:
    current = corpus_hash(corpus_file.read_text())
    j = {
        # queued, scored against an older corpus: re-scored, falls below the gate
        "a": _job(conn, "a", title="Alpha", fit=70, requirements=True),
        # queued, scored against the current corpus: not stale
        "b": _job(conn, "b", title="Bravo", fit=70, requirements=True),
        "c": _job(conn, "c", title="Charlie", status="filtered", fit=60, requirements=True),
        # rejected only by the score gate, and the current filters pass it:
        # re-scored and re-queued (the fake scorer reads the first word)
        "d": _job(
            conn, "d", title="Delta SDR Manager", status="rejected", fit=55, requirements=True
        ),
        # rejected by the score gate, then rejected by a human too: never revived
        "e": _job(conn, "e", title="Echo", status="rejected", fit=55, requirements=True),
        # rejected by the sanity layer
        "f": _job(conn, "f", title="Foxtrot", status="rejected", fit=90, requirements=True),
        "g": _job(conn, "g", title="Golf", status="approved", fit=72, requirements=True),
        "h": _job(conn, "h", title="Hotel", status="applied", fit=68, requirements=True),
        "i": _job(conn, "i", title="India", fit=70, closed=True, requirements=True),
        "k": _job(conn, "k", title="Kilo", status="snoozed", fit=70, requirements=True),
    }
    _profile_event(conn, j["b"], current)
    _profile_event(conn, j["a"], "an-older-corpus")
    for key in ("d", "e"):
        conn.execute(
            q.RECORD_SCORE_GATE_REJECTION, {"job_id": j[key], "reason": "score_below_surface:55<65"}
        )
    conn.execute(
        "INSERT INTO decisions (job_id, decision, reason, auto) VALUES (?, 'reject', 'human', 0)",
        (j["e"],),
    )
    conn.execute(
        "INSERT INTO rejections (job_id, layer, reason) VALUES (?, 'sanity', 'sanity:onsite')",
        (j["f"],),
    )
    conn.commit()
    return j


def _status(conn: sqlite3.Connection, job_id: int) -> str:
    return str(conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()[0])


def _score(conn: sqlite3.Connection, job_id: int) -> int:
    return int(
        conn.execute("SELECT fit_score FROM scores WHERE job_id = ?", (job_id,)).fetchone()[0]
    )


def test_rescore_selection_rules_and_gates(
    conn: sqlite3.Connection,
    rescore_jobs: dict[str, int],
    corpus_file: Path,
    fake_scorer: list[str],
    rules_calls: list[bool],
) -> None:
    j = rescore_jobs
    dry = rescore.rescore(conn, dry_run=True)
    assert [c.job_id for c in dry.candidates] == [j["a"], j["c"], j["d"], j["g"]]
    assert fake_scorer == [] and dry.render().startswith("DRY RUN")
    assert _status(conn, j["a"]) == "queued"

    report = rescore.rescore(conn)
    assert sorted(fake_scorer) == ["Alpha", "Charlie", "Delta", "Golf"]
    assert {r.job_id: (r.old, r.new) for r in report.rescored} == {
        j["a"]: (70, 50),
        j["c"]: (60, 80),
        j["d"]: (55, 75),
        j["g"]: (72, 40),
    }
    # the queue gate (gates.min_score_to_surface: 65), re-applied
    assert _status(conn, j["a"]) == "rejected"
    reason = conn.execute(
        "SELECT reason FROM rejections WHERE job_id = ? ORDER BY id DESC", (j["a"],)
    ).fetchone()[0]
    assert reason == "score_below_surface:50<65"
    assert _status(conn, j["c"]) == "queued"
    assert _status(conn, j["d"]) == "queued"
    # approved: new score reported, status never touched
    assert _status(conn, j["g"]) == "approved" and _score(conn, j["g"]) == 40
    assert "approved (status never changed here)" in report.render()
    # untouched: current hash, human-rejected, sanity-rejected, applied, closed, snoozed
    for key, status, fit in [
        ("b", "queued", 70),
        ("e", "rejected", 55),
        ("f", "rejected", 90),
        ("h", "applied", 68),
        ("i", "queued", 70),
        ("k", "snoozed", 70),
    ]:
        assert (_status(conn, j[key]), _score(conn, j[key])) == (status, fit), key
    assert rules_calls == [True]  # standing rules re-run over the queue
    current = corpus_hash(corpus_file.read_text())
    for key in ("a", "c", "d", "g"):
        detail = conn.execute(
            "SELECT detail FROM events WHERE job_id = ? AND kind = 'score_profile' "
            "ORDER BY id DESC",
            (j[key],),
        ).fetchone()[0]
        assert json.loads(detail)["profile_hash"] == current
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'rescored'").fetchone()[0] == 4

    # every score is current now: a second default run re-scores nothing
    fake_scorer.clear()
    again = rescore.rescore(conn)
    assert again.candidates == [] and fake_scorer == []


def test_rescore_never_revives_a_job_the_current_filters_reject(
    conn: sqlite3.Connection,
    corpus_file: Path,
    jobkit_home: Path,
    fake_scorer: list[str],
    rules_calls: list[bool],
) -> None:
    """Review 2026-10-09: a score-gate rejection went back to the queue (and on
    to the standing rules) without the deterministic filters, so a stale
    posting or a blocklisted company could be auto-approved. Evaluation order
    is filters first: such a job stays rejected, is not re-scored, and the
    rejection log names the filter layer."""
    path = jobkit_home / "config" / "filters.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["filters"]["blocklist_companies"] = ["Blockco"]
    path.write_text(yaml.safe_dump(raw))
    max_age = load_filters().max_age_days
    old_post = (datetime.date.today() - datetime.timedelta(days=max_age + 60)).isoformat()
    fresh = datetime.date.today().isoformat()
    title = "Delta SDR Manager"  # passes the title filter; the fake scorer gives it 75
    j = {
        "stale": _job(conn, "st", title=title, status="rejected", fit=55, posted_at=old_post),
        "blocked": _job(conn, "bl", title=title, company="Blockco", status="rejected", fit=55),
        "untitled": _job(conn, "un", title="Delta", status="rejected", fit=55),
        "ok": _job(conn, "ok", title=title, status="rejected", fit=55, posted_at=fresh),
    }
    for jid in j.values():
        conn.execute(
            "INSERT INTO job_requirements (job_id, must_have, nice_to_have, implied, "
            "disqualifier) VALUES (?, '[\"x\"]', '[]', '[]', '[]')",
            (jid,),
        )
        conn.execute(
            q.RECORD_SCORE_GATE_REJECTION, {"job_id": jid, "reason": "score_below_surface:55<65"}
        )
    conn.commit()

    def rejections(jid: int) -> list[tuple[str, str]]:
        return [
            (str(r[0]), str(r[1]))
            for r in conn.execute(
                "SELECT layer, reason FROM rejections WHERE job_id = ? ORDER BY id", (jid,)
            )
        ]

    dry = rescore.rescore(conn, dry_run=True)
    assert [c.job_id for c in dry.candidates] == [j["ok"]]
    assert dict(dry.filter_rejected) == {
        j["stale"]: f"stale:{max_age + 60}d",
        j["blocked"]: "blocklist_company:Blockco",
        j["untitled"]: "no_include_title_match",
    }
    assert "stay rejected" in dry.render() and f"stale:{max_age + 60}d" in dry.render()
    assert len(rejections(j["stale"])) == 1  # a dry run writes nothing

    report = rescore.rescore(conn)
    assert fake_scorer == ["Delta"]  # only the job the filters still pass is re-scored
    assert _status(conn, j["ok"]) == "queued"
    for key, reason in [
        ("stale", f"stale:{max_age + 60}d"),
        ("blocked", "blocklist_company:Blockco"),
        ("untitled", "no_include_title_match"),
    ]:
        assert _status(conn, j[key]) == "rejected", key
        assert _score(conn, j[key]) == 55, key
        assert rejections(j[key])[-1] == ("filter", reason), key
    assert "filter rejection recorded" in report.render()
    assert rules_calls == [True]  # only the revived job reached the standing rules

    # the newest rejection is the filter's now: no later rescore picks them up,
    # even by explicit id, and nothing is recorded twice
    fake_scorer.clear()
    again = rescore.rescore(conn, [j["stale"], j["blocked"]])
    assert fake_scorer == [] and again.filter_rejected == []
    assert {jid for jid, _ in again.skipped} == {j["stale"], j["blocked"]}
    assert len(rejections(j["stale"])) == 2


def test_rescore_explicit_job_ids(
    conn: sqlite3.Connection,
    rescore_jobs: dict[str, int],
    fake_scorer: list[str],
    rules_calls: list[bool],
) -> None:
    j = rescore_jobs
    report = rescore.rescore(conn, [j["b"], j["h"], j["e"], j["f"], j["i"], j["k"]])
    # explicit: re-scored even with a current hash; applied too, status kept
    assert sorted(fake_scorer) == ["Bravo", "Hotel"]
    assert _status(conn, j["h"]) == "applied" and _score(conn, j["h"]) == 66
    assert _status(conn, j["b"]) == "queued"
    skipped = dict(report.skipped)
    assert set(skipped) == {j["e"], j["f"], j["i"], j["k"]}
    assert "closed" in skipped[j["i"]] and "snoozed" in skipped[j["k"]]
    assert "not only by the score gate" in skipped[j["f"]]


def test_run_score_records_the_profile_hash(
    conn: sqlite3.Connection, corpus_file: Path, fake_scorer: list[str]
) -> None:
    jid = _job(conn, "z", title="Alpha", status="filtered", fit=None, requirements=True)
    assert triage.run_score(conn) == 1
    detail = conn.execute(
        "SELECT detail FROM events WHERE job_id = ? AND kind = 'score_profile'", (jid,)
    ).fetchone()[0]
    assert json.loads(detail)["profile_hash"] == corpus_hash(corpus_file.read_text())
    assert triage.current_profile_hash() == corpus_hash(corpus_file.read_text())


# ---------------------------------------------------------------------------
# MCP + CLI: every capability on both
# ---------------------------------------------------------------------------


def _mcp(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> Any:
    from jobkit import mcp_server

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    conn.executescript(mcp_server.PINS_SCHEMA)
    return mcp_server


def test_mcp_questions_answer_confirm_and_rescore(
    conn: sqlite3.Connection,
    seeded: dict[str, int],
    corpus_file: Path,
    spawned: SpawnRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _mcp(conn, monkeypatch)
    out = mcp.get_questions()
    assert "[tool-clari]" in out and "Why:" in out and "Helps 3 open job(s)" in out
    full = mcp.get_questions(show_all=True)
    assert "All 8 open question(s)" in full and "[tool-zoominfo]" in full
    said = "Yes, I used Clari in daily forecast calls at Haulmark"
    res = mcp.answer_question("tool-clari", said)
    assert "proposal 1 recorded" in res and "evidence" in res
    text = "I used Clari in daily forecast calls at Haulmark"
    shown = f'skill_index Clari, writing exactly: "{text}" (your answer, verbatim: "{said}")'
    assert f"Check it: {shown}." in res
    pending = mcp.get_questions()
    assert "Awaiting your confirmation" in pending and shown in pending
    refused = mcp.confirm_proposal(1)
    assert refused.startswith("not confirmed") and spawned.calls == []
    assert f"It would write {shown}." in refused
    ok = mcp.confirm_proposal(1, evidence="Clari exports", metric_confidence="estimate")
    assert ok.startswith("confirmed proposal 1")
    assert "Clari" in yaml.safe_load(corpus_file.read_text())["skill_index"]
    assert spawned.calls[-1][-2:] == ["--job-ids", f"{seeded['j1']},{seeded['j2']},{seeded['j3']}"]
    dry = mcp.rescore()
    assert dry.startswith("DRY RUN") and "rescore(confirm=true)" in dry
    started = mcp.rescore(job_ids=str(seeded["j1"]), confirm=True)
    assert "started `jobkit rescore --job-ids" in started
    assert spawned.calls[-1] == background.cli_argv("rescore", "--job-ids", str(seeded["j1"]))
    assert mcp.rescore(job_ids="x", confirm=True).startswith("nothing re-scored")


def test_cli_questions_answer_confirm_and_rescore(
    jobkit_home: Path, corpus_file: Path, spawned: SpawnRecorder
) -> None:
    from jobkit import cli

    c = q.connect(create=True)
    q.init_db(c)
    jid = _job(c, "cli", fit=82, gaps=(CLARI_GAP,), requirements=True)
    c.close()
    runner = CliRunner()
    res = runner.invoke(cli.app, ["questions"])
    assert res.exit_code == 0 and "[tool-clari]" in res.output
    res = runner.invoke(cli.app, ["questions", "--all"])
    assert res.exit_code == 0 and "All 1 open question(s)" in res.output
    res = runner.invoke(
        cli.app,
        [
            "answer",
            "tool-clari",
            "Yes, I used Clari daily at Haulmark",
            "--evidence",
            "Clari exports",
            "--metric-confidence",
            "verified",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "proposal 1 recorded" in res.output
    res = runner.invoke(cli.app, ["answer", "tool-nothing", "yes"])
    assert res.exit_code == 1
    res = runner.invoke(cli.app, ["confirm-proposal", "1"])
    assert res.exit_code == 0, res.output
    assert "Clari" in yaml.safe_load(corpus_file.read_text())["skill_index"]
    assert spawned.calls == [background.cli_argv("rescore", "--job-ids", str(jid))]
    res = runner.invoke(cli.app, ["rescore", "--dry-run"])
    assert res.exit_code == 0 and "DRY RUN" in res.output and f"job {jid}" in res.output
    res = runner.invoke(cli.app, ["rescore", "--job-ids", "x"])
    assert res.exit_code == 1


def test_mcp_and_cli_decline_and_undecline(
    conn: sqlite3.Connection,
    seeded: dict[str, int],
    jobkit_home: Path,
    corpus_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _mcp(conn, monkeypatch)
    out = mcp.answer_question("tool-clari", "Never below 115% of quota at Haulmark")
    assert "may be a no or a fact" in out
    out = mcp.answer_question("tool-clari", decline=True)
    assert out.startswith("recorded: you declined 'tool-clari'")
    assert "[tool-clari]" not in mcp.get_questions(show_all=True)
    assert "open again" in mcp.undecline_question("tool-clari")
    assert "[tool-clari]" in mcp.get_questions(show_all=True)
    assert mcp.undecline_question("tool-clari").startswith("nothing changed")

    from jobkit import cli

    c = q.connect(create=True)
    q.init_db(c)
    _job(c, "cli", fit=82, gaps=(CLARI_GAP,), requirements=True)
    c.close()
    runner = CliRunner()
    res = runner.invoke(cli.app, ["answer", "tool-clari", "Never below 115% of quota"])
    assert res.exit_code == 1 and "may be a no or a fact" in res.output
    res = runner.invoke(cli.app, ["answer", "tool-clari", "--decline"])
    assert res.exit_code == 0 and "declined 'tool-clari'" in res.output, res.output
    res = runner.invoke(cli.app, ["answer", "tool-clari", "I have never used Clari"])
    assert res.exit_code == 1 and "jobkit undecline tool-clari" in res.output
    res = runner.invoke(cli.app, ["undecline", "tool-clari"])
    assert res.exit_code == 0 and "open again" in res.output
    res = runner.invoke(cli.app, ["undecline", "tool-clari"])
    assert res.exit_code == 1
    res = runner.invoke(cli.app, ["answer", "tool-clari", "I have never used Clari"])
    assert res.exit_code == 0 and "declined 'tool-clari'" in res.output
