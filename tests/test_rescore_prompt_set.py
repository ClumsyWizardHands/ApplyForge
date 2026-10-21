"""Rescore and extract_and_score under the prompt sets, through their real
entry points (not score_rows alone):

* under prompt_set general, rescore.rescore() and triage.extract_and_score()
  score with score_fit_general and the career-change note;
* a re-score honours the track's own min_score, and the global gate for a
  track without one;
* on the sales test profile (an existing config: no prompt_set, no per-track
  min_score), a database in the shape the code before prompt sets left it
  selects nothing to re-score and nothing to re-gate.

Offline: every LLM call is faked at llm.call_json; temp DB only."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from jobkit import llm
from jobkit.pipeline import rescore, triage
from tests.test_claims import CORPUS
from tests.test_filter import _insert
from tests.test_prompt_set import JD, TEACHER_CORPUS, FakeCalls, use_persona

GENERAL_KEY = "score_fit_general:1.0"
PIVOT_NOTE = "CAREER-CHANGE NOTE: this job is on"


def _job(conn: sqlite3.Connection, title: str, status: str, *, requirements: bool = True) -> int:
    jid = _insert(
        conn,
        external_id=title,
        title=title,
        jd_html=JD,
        jd_hash=f"h-{title}",
        location_raw="Remote, US",
        remote=1,
    )
    conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, jid))
    if requirements:
        conn.execute(
            "INSERT INTO job_requirements (job_id, must_have) VALUES (?, ?)",
            (jid, json.dumps(["write assessments"])),
        )
    conn.commit()
    return jid


def _scored(conn: sqlite3.Connection, jid: int, score: int, detail: dict[str, Any]) -> None:
    conn.execute("INSERT INTO scores (job_id, fit_score) VALUES (?, ?)", (jid, score))
    conn.execute(
        "INSERT INTO events (job_id, kind, detail) VALUES (?, 'score_profile', ?)",
        (jid, json.dumps(detail)),
    )
    conn.commit()


def _gate_rejected(conn: sqlite3.Connection, jid: int, reason: str) -> None:
    conn.execute(
        "INSERT INTO rejections (job_id, layer, reason) VALUES (?, 'filter', ?)", (jid, reason)
    )
    conn.commit()


@pytest.fixture()
def teacher(jobkit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (jobkit_home / "data" / "career_corpus.yaml").write_text(yaml.safe_dump(TEACHER_CORPUS))
    use_persona(jobkit_home, "teacher_to_id")
    monkeypatch.setattr(rescore, "run_standing_rules", lambda c: {"evaluated": 0})


STALE = {"profile_hash": "an older corpus", "prompt_version": "1.5"}


def test_rescore_scores_with_the_general_prompt(
    conn: sqlite3.Connection, teacher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeCalls({"score": {"score": 72, "rationale": "a. b.", "gaps": []}})
    monkeypatch.setattr(llm, "call_json", fake)
    jid = _job(conn, "Curriculum Designer", "queued")
    _scored(conn, jid, 70, STALE)
    report = rescore.rescore(conn)
    assert [r.job_id for r in report.rescored] == [jid]
    assert [c["stage"] for c in fake.calls] == ["score"]
    assert GENERAL_KEY in fake.calls[0]["cache_key"]
    assert PIVOT_NOTE in fake.calls[0]["prompt"]
    ev = conn.execute(
        "SELECT detail FROM events WHERE kind='score_profile' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert json.loads(ev["detail"])["prompt"] == "score_fit_general"


def test_extract_and_score_scores_with_the_general_prompt(
    conn: sqlite3.Connection, teacher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeCalls(
        {
            "extract": {"must_have": ["write assessments"], "nice_to_have": []},
            "score": {"score": 61, "rationale": "a. b.", "gaps": []},
        }
    )
    monkeypatch.setattr(llm, "call_json", fake)
    jid = _job(conn, "Instructional Designer", "queued", requirements=False)
    assert triage.extract_and_score(conn, [jid]) == {jid: 61}
    score = [c for c in fake.calls if c["stage"] == "score"]
    assert len(score) == 1
    assert GENERAL_KEY in score[0]["cache_key"] and PIVOT_NOTE in score[0]["prompt"]
    # requirements are extracted with the mixed-field prompt, under its own key
    extract = [c for c in fake.calls if c["stage"] == "extract"]
    assert len(extract) == 1
    assert extract[0]["cache_key"].startswith("extract-extract_requirements_general:")
    assert "The job can be in any field." in extract[0]["prompt"]


def test_the_sales_profile_extracts_with_the_original_prompt_and_key(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (jobkit_home / "data" / "career_corpus.yaml").write_text(yaml.safe_dump(CORPUS))
    fake = FakeCalls(
        {
            "extract": {"must_have": ["quota"], "nice_to_have": []},
            "score": {"score": 70, "rationale": "a. b.", "gaps": []},
        }
    )
    monkeypatch.setattr(llm, "call_json", fake)
    jid = _job(conn, "Account Executive", "queued", requirements=False)
    triage.extract_and_score(conn, [jid])
    extract = [c for c in fake.calls if c["stage"] == "extract"]
    version = llm.PromptFile("extract_requirements").version
    assert extract[0]["cache_key"] == f"extract:h-Account Executive:{version}"
    assert "own the number" in extract[0]["prompt"]


def test_a_rescore_gates_with_the_tracks_min_score(
    conn: sqlite3.Connection, teacher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """instructional_designer sets min_score 50; classroom_teacher has none,
    so the global 65 applies to it."""
    monkeypatch.setattr(
        llm, "call_json", FakeCalls({"score": {"score": 55, "rationale": "a. b.", "gaps": []}})
    )
    pivot = _job(conn, "Instructional Designer", "queued")
    teacher_job = _job(conn, "Science Teacher", "queued")
    for jid in (pivot, teacher_job):
        _scored(conn, jid, 70, STALE)
    rescore.rescore(conn)
    status = dict(conn.execute("SELECT id, status FROM jobs").fetchall())
    assert status == {pivot: "queued", teacher_job: "rejected"}
    reasons = {
        r["job_id"]: r["reason"] for r in conn.execute("SELECT job_id, reason FROM rejections")
    }
    assert reasons == {teacher_job: "score_below_surface:55<65"}


def test_the_sales_profile_selects_nothing_new(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing config: no prompt_set and no track min_score. Jobs
    rejected under an older global gate stay rejected, and scores recorded by
    the code before prompt sets (no prompt name), or by the sales prompt now,
    are current: no re-score, no re-gate, no filter rejection."""
    (jobkit_home / "data" / "career_corpus.yaml").write_text(yaml.safe_dump(CORPUS))
    monkeypatch.setattr(llm, "call_json", FakeCalls({}))  # any call raises KeyError
    current = triage.current_profile_hash()
    legacy = {"profile_hash": current, "prompt_version": "1.5"}
    old_gate = _job(conn, "Account Executive", "rejected")
    _scored(conn, old_gate, 67, legacy)
    _gate_rejected(conn, old_gate, "score_below_surface:67<70")
    below = _job(conn, "Enterprise Account Executive", "rejected")
    _scored(conn, below, 60, legacy)
    _gate_rejected(conn, below, "score_below_surface:60<65")
    queued = _job(conn, "Director of Sales", "queued")
    _scored(conn, queued, 80, legacy)
    filtered = _job(conn, "Sales Development Representative", "filtered")
    _scored(conn, filtered, 70, legacy)
    # a score the sales prompt wrote after the change: the same shape
    newer = _job(conn, "Senior Account Executive", "queued")
    conn.execute(
        "INSERT INTO events (job_id, kind, detail) VALUES (?, 'score_profile', ?)",
        (newer, json.dumps({"profile_hash": current, "prompt_version": "9.9"})),
    )
    conn.execute("INSERT INTO scores (job_id, fit_score) VALUES (?, 75)", (newer,))
    conn.commit()
    dry = rescore.rescore(conn, dry_run=True)
    assert dry.candidates == [] and dry.regated == [] and dry.filter_rejected == []


def test_the_sales_prompt_writes_its_event_in_the_original_shape(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (jobkit_home / "data" / "career_corpus.yaml").write_text(yaml.safe_dump(CORPUS))
    monkeypatch.setattr(
        llm, "call_json", FakeCalls({"score": {"score": 70, "rationale": "a. b.", "gaps": []}})
    )
    _job(conn, "Account Executive", "filtered")
    assert triage.run_score(conn) == 1
    ev = conn.execute("SELECT detail FROM events WHERE kind='score_profile'").fetchone()
    assert set(json.loads(ev["detail"])) == {"profile_hash", "prompt_version"}


def _current_detail() -> str:
    from jobkit.config import load_filters

    now = rescore.current_scoring(load_filters())
    return json.dumps(
        {"profile_hash": now.profile_hash, "prompt_version": now.version, "prompt": now.name}
    )


def _below_old_gate(conn: sqlite3.Connection, title: str, **where: Any) -> int:
    """A job the old global gate (65) rejected at 58, with a current score:
    the teacher's instructional_designer gate (50) would re-queue it."""
    jid = _insert(conn, external_id=title, title=title, jd_html=JD, jd_hash=f"g-{title}", **where)
    conn.execute("UPDATE jobs SET status='rejected' WHERE id=?", (jid,))
    conn.execute("INSERT INTO scores (job_id, fit_score) VALUES (?, 58)", (jid,))
    conn.execute("INSERT INTO job_requirements (job_id, must_have) VALUES (?, '[]')", (jid,))
    conn.execute(
        "INSERT INTO rejections (job_id, layer, reason) VALUES (?, 'filter', ?)",
        (jid, triage.score_gate_reason(58, 65)),
    )
    conn.execute(
        "INSERT INTO events (job_id, kind, detail) VALUES (?, 'score_profile', ?)",
        (jid, _current_detail()),
    )
    conn.commit()
    return jid


def test_a_regated_job_goes_through_the_filters_first(
    conn: sqlite3.Connection, teacher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evaluation order: deterministic filters, then sanity, then standing
    rules. An on-site job in a place the teacher does not accept meets the
    lowered track gate, but the filters reject it, so it never re-queues."""
    monkeypatch.setattr(llm, "call_json", FakeCalls({}))  # any call raises KeyError
    jid = _below_old_gate(conn, "Instructional Designer", location_raw="Seattle, WA", remote=0)
    dry = rescore.rescore(conn, dry_run=True)
    assert dry.regated == []
    assert [j for j, _ in dry.filter_rejected] == [jid]
    rescore.rescore(conn)
    status = conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0]
    assert status == "rejected"
    newest = conn.execute(
        "SELECT layer, reason FROM rejections WHERE job_id=? ORDER BY id DESC LIMIT 1", (jid,)
    ).fetchone()
    assert newest["layer"] == "filter" and newest["reason"].startswith("location:")
    assert conn.execute("SELECT 1 FROM events WHERE kind='regated'").fetchone() is None


def test_standing_rules_judge_a_regated_job(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real standing rules run on a job a lowered track gate re-queues:
    the person's own auto_reject rule decides it (no stub here)."""
    (jobkit_home / "data" / "career_corpus.yaml").write_text(yaml.safe_dump(TEACHER_CORPUS))
    use_persona(jobkit_home, "teacher_to_id")
    (jobkit_home / "config" / "autoapprove.yaml").write_text(
        yaml.safe_dump(
            {
                "version": "1.0",
                "auto_approve": [],
                "auto_reject": [
                    {
                        "when": {"title_class": "instructional_designer", "tier": 3},
                        "reason": "id-tier3",
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(llm, "call_json", FakeCalls({}))  # any call raises KeyError
    jid = _below_old_gate(conn, "Instructional Designer", location_raw="Remote, US", remote=1)
    conn.execute("UPDATE companies SET tier=3 WHERE id=1")
    conn.commit()
    report = rescore.rescore(conn)
    assert [c.job_id for c, _ in report.regated] == [jid]
    assert report.rules is not None and report.rules["auto_rejected"] == 1
    status = conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0]
    assert status == "rejected"
    decision = conn.execute("SELECT decision, reason, auto FROM decisions WHERE job_id=?", (jid,))
    assert [tuple(r) for r in decision] == [("reject", "id-tier3", 1)]
