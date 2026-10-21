"""Triage plumbing with the LLM boundary mocked — tests never hit live models."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from jobkit import llm
from jobkit.normalize import MIN_JD_CHARS, jd_hash, jd_text_length
from jobkit.pipeline import triage
from jobkit.pipeline.triage import (
    rank_correlation,
    run_comp_check,
    run_extract,
    run_queue,
    run_remote_check,
    run_score,
)
from tests.test_filter import FILTERS, _insert

CANNED: dict[str, dict[str, Any]] = {
    "extract": {
        "must_have": ["8+ years sales leadership"],
        "nice_to_have": [],
        "implied": ["quota ownership"],
        "disqualifier": [],
    },
    "remote": {
        "remote_stated": "remote",
        "remote_actual": "remote",
        "remote_conflict": False,
        "geo_restriction": None,
        "office_days_required": None,
        "evidence": "",
    },
    "comp": {
        "demand_score": 55,
        "demand_factors": ["management"],
        "breadth_roles": 1,
        "management_scope": True,
        "on_call": False,
        "travel_pct": None,
        "unusual_credentials": [],
    },
    "score": {
        "score": 82,
        "rationale": "Strong leadership match. Domain adjacent.",
        "gaps": ["developer-tools domain"],
    },
}


# A JD long enough to triage (normalize.MIN_JD_CHARS). Test jobs default to it.
REAL_JD = (
    "<p>We are hiring a Director of Sales to own the enterprise number. You will "
    "build and coach a team of account executives, run forecast and pipeline "
    "reviews, and partner with marketing on outbound programs.</p>"
    "<ul><li>8+ years of B2B sales, 3+ leading quota-carrying reps</li>"
    "<li>Experience selling to developer-tools or infrastructure buyers</li>"
    "<li>Comfort with MEDDICC, Salesforce and Gong</li></ul>"
    "<p>This role is remote within the United States. Base salary plus variable "
    "compensation, equity and benefits. We review every application.</p>"
    "<p>You will report to the CRO and join a go-to-market team of forty.</p>"
)
assert jd_text_length(REAL_JD) > MIN_JD_CHARS


@pytest.fixture()
def mock_llm(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """The REAL llm.call (cache and all) over a fake `claude -p` backend that
    counts calls, so these tests exercise the effective cache key, not a copy."""
    calls = {"n": 0}

    def fake_backend(stage: str, prompt: str) -> tuple[str, dict[str, Any]]:
        calls["n"] += 1
        return json.dumps(CANNED[stage]), {}

    monkeypatch.setattr(llm, "_call_claude_cli", fake_backend)
    monkeypatch.setattr(triage, "_profile_summary", lambda: "EXPERIENCE: test profile")
    monkeypatch.setattr(triage, "load_filters", lambda: FILTERS)
    return calls


def _seed_filtered_job(conn: sqlite3.Connection, **kw: object) -> int:
    kw.setdefault("jd_html", REAL_JD)
    jid = _insert(conn, **kw)
    conn.execute("UPDATE jobs SET status='filtered' WHERE id=?", (jid,))
    conn.commit()
    return jid


def test_triage_pipeline_end_to_end(conn: sqlite3.Connection, mock_llm: dict[str, int]) -> None:
    jid = _seed_filtered_job(conn, external_id="t1")
    assert run_extract(conn) == 1
    assert run_remote_check(conn)["checked"] == 1
    assert run_comp_check(conn)["checked"] == 1
    assert run_score(conn) == 1
    assert run_queue(conn, FILTERS) == 1
    row = conn.execute(
        """SELECT j.status, sc.fit_score, s.remote_conflict, r.must_have
           FROM jobs j JOIN scores sc ON sc.job_id=j.id
           JOIN sanity s ON s.job_id=j.id JOIN job_requirements r ON r.job_id=j.id
           WHERE j.id=?""",
        (jid,),
    ).fetchone()
    assert row["status"] == "queued" and row["fit_score"] == 82
    assert row["remote_conflict"] == 0
    assert "8+ years sales leadership" in row["must_have"]


def test_llm_cache_prevents_double_spend(
    conn: sqlite3.Connection, mock_llm: dict[str, int]
) -> None:
    """Byte-identical input (same stage, model and rendered prompt) is the one
    case where reusing a cached answer is correct."""
    _seed_filtered_job(conn, external_id="t2", jd_hash="samehash")
    run_extract(conn)
    first = mock_llm["n"]
    conn.execute("DELETE FROM job_requirements")
    conn.commit()
    run_extract(conn)  # same stage + model + prompt -> cache hit, no new call
    assert mock_llm["n"] == first


EMPTY_HASH = jd_hash("")  # e3b0c442…, what every JD-less feed job hashed to


@pytest.mark.parametrize(
    "jd_html",
    [
        "",  # Getro: the search payload has no description key
        "<p>Knot is looking for a Revenue Operations Manager in Chicago, IL.</p>",  # RSS stub
        "&lt;p&gt;" + "&lt;b&gt;stub&lt;/b&gt; " * 60 + "&lt;/p&gt;",  # markup is not text
    ],
    ids=["empty", "rss-stub", "escaped-markup"],
)
def test_short_jd_refused_before_any_llm_call(
    conn: sqlite3.Connection, mock_llm: dict[str, int], jd_html: str
) -> None:
    """Audit 2026-10-08 §2.2: JD-less jobs shared one cached answer (a
    Virtucon score, a Lumon cover letter). Now no stage calls the model for
    them: each is rejected once, layer=sanity, reason=jd_missing."""
    ids = [
        _seed_filtered_job(conn, external_id=f"e{i}", jd_html=jd_html, jd_hash=EMPTY_HASH)
        for i in range(2)
    ]
    assert run_extract(conn) == 0
    assert run_remote_check(conn)["checked"] == 0
    assert run_comp_check(conn)["checked"] == 0
    assert run_score(conn) == 0
    assert run_queue(conn, FILTERS) == 0
    assert mock_llm["n"] == 0
    for jid in ids:
        status = conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0]
        assert status == "rejected"
        rows = conn.execute("SELECT layer, reason FROM rejections WHERE job_id=?", (jid,))
        assert [tuple(r) for r in rows] == [("sanity", "jd_missing")]
    conn.executescript(llm.CACHE_SCHEMA)
    assert conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] == 0


def test_each_stage_refuses_short_jd_on_its_own(
    conn: sqlite3.Connection, mock_llm: dict[str, int]
) -> None:
    """A JD-less job that already has extract output (legacy rows) is still
    refused at the next stage it reaches."""
    jid = _seed_filtered_job(conn, external_id="legacy-r", jd_html="", jd_hash=EMPTY_HASH)
    conn.execute("INSERT INTO job_requirements (job_id, must_have) VALUES (?, '[]')", (jid,))
    conn.commit()
    assert run_score(conn) == 0
    assert mock_llm["n"] == 0
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "rejected"


def test_short_jd_with_legacy_score_is_not_queued(
    conn: sqlite3.Connection, mock_llm: dict[str, int]
) -> None:
    """A score written before the guard existed must not promote a JD-less job."""
    jid = _seed_filtered_job(conn, external_id="legacy", jd_html="", jd_hash=EMPTY_HASH)
    conn.execute("INSERT INTO scores (job_id, fit_score, gaps) VALUES (?, 90, '[]')", (jid,))
    conn.commit()
    assert run_queue(conn, FILTERS) == 0
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "rejected"


def test_same_jd_hash_different_prompt_misses_cache(
    conn: sqlite3.Connection, mock_llm: dict[str, int]
) -> None:
    """jd_hash alone no longer decides a cache hit: two jobs whose rendered
    prompts differ (here, the title) each get their own model call."""
    _seed_filtered_job(conn, external_id="a", jd_hash="shared", title="Director of Sales")
    _seed_filtered_job(conn, external_id="b", jd_hash="shared", title="Deal Desk Manager")
    assert run_extract(conn) == 2
    assert mock_llm["n"] == 2


def test_corpus_change_misses_score_cache(
    conn: sqlite3.Connection, mock_llm: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resume update must reach scoring: a new profile is a new prompt."""
    _seed_filtered_job(conn, external_id="s1", jd_hash="same")
    run_extract(conn)
    run_score(conn)
    before = mock_llm["n"]
    conn.execute("DELETE FROM scores")
    conn.commit()
    run_score(conn)  # unchanged profile -> cache hit
    assert mock_llm["n"] == before
    conn.execute("DELETE FROM scores")
    conn.commit()
    monkeypatch.setattr(triage, "_profile_summary", lambda: "EXPERIENCE: updated resume")
    run_score(conn)
    assert mock_llm["n"] == before + 1


def test_prompt_edit_and_model_change_miss_cache(
    conn: sqlite3.Connection, mock_llm: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    llm.call(conn, "extract", "prompt v1", cache_key="extract:h:1.0")
    llm.call(conn, "extract", "prompt v1", cache_key="extract:h:1.0")
    assert mock_llm["n"] == 1  # identical -> hit
    llm.call(conn, "extract", "prompt v1, edited", cache_key="extract:h:1.0")
    assert mock_llm["n"] == 2  # same caller key, different prompt -> miss
    monkeypatch.setitem(llm.TIER_MODEL_ID, "haiku", "claude-haiku-next")
    llm.call(conn, "extract", "prompt v1", cache_key="extract:h:1.0")
    assert mock_llm["n"] == 3  # same prompt, different model -> miss
    models = {r[0] for r in conn.execute("SELECT model FROM llm_cache")}
    assert models == {"claude-haiku-5-5", "claude-haiku-next"}  # resolved ids, not aliases


def test_onsite_actual_rejected_by_sanity(
    conn: sqlite3.Connection, mock_llm: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    lying = dict(
        CANNED,
        remote={
            "remote_stated": "remote",
            "remote_actual": "onsite",
            "remote_conflict": True,
            "geo_restriction": None,
            "office_days_required": 5,
            "evidence": "5 days in office required",
        },
    )
    monkeypatch.setitem(CANNED, "remote", lying["remote"])
    jid = _seed_filtered_job(conn, external_id="t3", jd_hash="liar")
    stats = run_remote_check(conn)
    assert stats["conflicts"] == 1 and stats["rejected"] == 1
    row = conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["status"] == "rejected"
    reason = conn.execute(
        "SELECT reason FROM rejections WHERE job_id=? AND layer='sanity'", (jid,)
    ).fetchone()["reason"]
    assert reason.startswith("sanity:")


def test_low_score_rejected_at_queue(
    conn: sqlite3.Connection, mock_llm: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(CANNED, "score", {"score": 40, "rationale": "Weak.", "gaps": ["all"]})
    jid = _seed_filtered_job(conn, external_id="t4", jd_hash="weak")
    run_extract(conn)
    run_remote_check(conn)
    run_comp_check(conn)
    run_score(conn)
    run_queue(conn, FILTERS)
    assert (
        conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()["status"]
        == "rejected"
    )
    reason = conn.execute("SELECT reason FROM rejections WHERE job_id=?", (jid,)).fetchone()[
        "reason"
    ]
    assert reason.startswith("score_below_surface")


def test_extract_and_score_a_merged_approved_job(
    conn: sqlite3.Connection, mock_llm: dict[str, int]
) -> None:
    """An ATS row that took the human's approval from an aggregator row
    (discovery/board_resolver.py) is extracted and scored whatever its status;
    its status is left alone, and a JD-less one is skipped, never rejected."""
    jid = _seed_filtered_job(conn, external_id="m1", jd_hash="merged")
    conn.execute("UPDATE jobs SET status='approved' WHERE id=?", (jid,))
    short = _insert(conn, external_id="m2", jd_hash="short", jd_html="too short")
    conn.execute("UPDATE jobs SET status='approved' WHERE id=?", (short,))
    conn.commit()
    assert triage.extract_and_score(conn, [jid, short]) == {jid: 82}
    rows = conn.execute("SELECT id, status FROM jobs ORDER BY id").fetchall()
    assert [(r["id"], r["status"]) for r in rows] == [(jid, "approved"), (short, "approved")]
    assert conn.execute("SELECT job_id FROM job_requirements").fetchall()[0][0] == jid
    assert not conn.execute("SELECT 1 FROM rejections").fetchall()
    calls = mock_llm["n"]
    assert triage.extract_and_score(conn, [jid]) == {}  # done once: no second call
    assert mock_llm["n"] == calls and triage.extract_and_score(conn, []) == {}


def test_rank_correlation() -> None:
    perfect = [(90, 3), (70, 2), (50, 1)]
    assert rank_correlation(perfect) == pytest.approx(1.0)
    inverse = [(90, 1), (70, 2), (50, 3)]
    assert rank_correlation(inverse) == pytest.approx(-1.0)
    assert rank_correlation([(1, 1)]) == 0.0


def test_queue_gate_uses_a_track_min_score(conn: sqlite3.Connection) -> None:
    """A track's own min_score gates its jobs; other titles keep
    gates.min_score_to_surface (65 here)."""
    from jobkit.config import Filters

    filters = Filters(
        raw={
            "filters": {
                "tracks": [
                    {"id": "nurse", "title_regex": r"(?i)\bnurse\b", "min_score": 55},
                    {"id": "sales_lead", "title_regex": r"(?i)\bsales\b"},
                ]
            },
            "gates": {"min_score_to_surface": 65},
        }
    )
    ids = {}
    for i, title in enumerate(("Staff Nurse", "Sales Lead", "Office Manager")):
        jid = _insert(conn, external_id=f"t{i}", title=title, jd_html=REAL_JD, jd_hash=f"t{i}")
        conn.execute("UPDATE jobs SET status='filtered' WHERE id=?", (jid,))
        conn.execute("INSERT INTO scores (job_id, fit_score) VALUES (?, 60)", (jid,))
        ids[title] = jid
    conn.commit()
    assert run_queue(conn, filters) == 1
    status = dict(conn.execute("SELECT title, status FROM jobs").fetchall())
    assert status == {
        "Staff Nurse": "queued",
        "Sales Lead": "rejected",
        "Office Manager": "rejected",
    }
    reason = conn.execute(
        "SELECT reason FROM rejections WHERE job_id = ?", (ids["Sales Lead"],)
    ).fetchone()[0]
    assert reason == "score_below_surface:60<65"
