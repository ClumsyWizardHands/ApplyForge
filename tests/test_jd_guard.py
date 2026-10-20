"""Empty-JD poisoning (audit 2026-10-08 §2 item 2): tailoring refuses a job
with no readable JD, and `purge-empty-jd-cache` removes what the old
jd_hash-only cache keys left behind. Runs against the sandbox DB only."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest
from typer.testing import CliRunner

from jobkit import db as q
from jobkit import llm
from jobkit.normalize import jd_hash
from jobkit.pipeline import tailor, triage
from jobkit.pipeline.maintenance import EMPTY_JD_HASH_PREFIX, purge_empty_jd_cache
from tests.test_filter import _insert
from tests.test_triage import REAL_JD

EMPTY_HASH = jd_hash("")


@pytest.fixture()
def no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("no LLM call may be made for a job without a JD")

    monkeypatch.setattr(llm, "call", boom)
    monkeypatch.setattr(llm, "_call_claude_cli", boom)
    monkeypatch.setattr(llm, "_call_sdk", boom)


def _job(conn: sqlite3.Connection, ext: str, status: str, jd_html: str, **kw: object) -> int:
    jid = _insert(conn, external_id=ext, jd_html=jd_html, **kw)
    conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, jid))
    conn.commit()
    return jid


# ---- tailoring ----


def test_tailor_refuses_jd_less_job(conn: sqlite3.Connection, no_llm: None) -> None:
    jid = _job(conn, "lumon-copy", "approved", "", jd_hash=EMPTY_HASH)
    conn.execute("INSERT INTO applications (job_id, stage) VALUES (?, 'approved')", (jid,))
    conn.commit()
    with pytest.raises(tailor.JDMissingError, match="jd_missing"):
        tailor.tailor_job(conn, jid)
    # the batch path skips it too, run after run, with a single rejection row
    assert tailor.tailor_approved(conn) == 0
    assert tailor.tailor_approved(conn) == 0
    rows = conn.execute("SELECT layer, reason FROM rejections WHERE job_id=?", (jid,)).fetchall()
    assert [tuple(r) for r in rows] == [("sanity", "jd_missing")]
    # the human's approval stands; no artifacts were produced
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "approved"
    app = conn.execute("SELECT stage, resume_path FROM applications WHERE job_id=?", (jid,))
    assert tuple(app.fetchone()) == ("approved", None)


# ---- purge-empty-jd-cache ----

LEGACY_EMPTY_KEYS = [
    f"extract:{EMPTY_HASH}:1.0",
    f"score:{EMPTY_HASH}:1.0",
    f"cover:{EMPTY_HASH}:2a4b8dc89b0d72f5:1.0",  # a cached letter
]
KEPT_KEYS = ["extract:3c518e49aaaabbbbccccddddeeeeffff:1.0", "critic:" + "a" * 64]


def _seed(conn: sqlite3.Connection) -> dict[str, int]:
    conn.executescript(llm.CACHE_SCHEMA)
    conn.executemany(
        "INSERT INTO llm_cache (cache_key, stage, model, response) VALUES (?, 'x', 'haiku', '{}')",
        [(k,) for k in LEGACY_EMPTY_KEYS + KEPT_KEYS],
    )
    ids = {
        "short_queued": _job(conn, "a", "queued", "", jd_hash=EMPTY_HASH),
        "short_approved": _job(conn, "b", "approved", "<p>one-line RSS stub</p>"),
        "long_queued": _job(conn, "c", "queued", REAL_JD),
        "short_unscored": _job(conn, "d", "filtered", "", jd_hash=EMPTY_HASH),
    }
    for name in ("short_queued", "short_approved", "long_queued"):
        conn.execute(
            "INSERT INTO scores (job_id, fit_score, gaps) VALUES (?, 67, ?)",
            (ids[name], json.dumps(["Virtucon product or platform experience"])),
        )
    conn.commit()
    return ids


def _status(conn: sqlite3.Connection, jid: int) -> str:
    return str(conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0])


def test_purge_dry_run_changes_nothing(conn: sqlite3.Connection) -> None:
    ids = _seed(conn)
    rep = purge_empty_jd_cache(conn, dry_run=True)
    assert (rep.cache_rows, rep.short_jd_jobs, rep.scores_reset, rep.requeued) == (3, 3, 2, 1)
    assert "DRY RUN" in rep.render()
    assert conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 3
    assert _status(conn, ids["short_queued"]) == "queued"
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_purge_deletes_empty_jd_rows_and_resets_scores(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, no_llm: None
) -> None:
    assert EMPTY_JD_HASH_PREFIX == "e3b0c442"
    ids = _seed(conn)
    rep = purge_empty_jd_cache(conn)
    assert (rep.cache_rows, rep.short_jd_jobs, rep.scores_reset, rep.requeued) == (3, 3, 2, 1)
    left = sorted(r[0] for r in conn.execute("SELECT cache_key FROM llm_cache"))
    assert left == sorted(KEPT_KEYS)
    scored = {r[0] for r in conn.execute("SELECT job_id FROM scores")}
    assert scored == {ids["long_queued"]}
    assert _status(conn, ids["short_queued"]) == "filtered"  # back to triage
    assert _status(conn, ids["short_approved"]) == "approved"  # human decision stands
    assert _status(conn, ids["long_queued"]) == "queued"
    ev = conn.execute("SELECT job_id, kind, detail FROM events").fetchone()
    assert ev["job_id"] is None and ev["kind"] == "purge_empty_jd_cache"
    assert json.loads(ev["detail"])["scores_reset"] == 2

    # idempotent
    again = purge_empty_jd_cache(conn)
    assert (again.cache_rows, again.scores_reset, again.requeued) == (0, 0, 0)

    # the requeued job is now rejected by triage, without a model call
    conn.execute(
        "INSERT INTO job_requirements (job_id, must_have) VALUES (?, '[]')",
        (ids["short_queued"],),
    )
    conn.commit()
    monkeypatch.setattr(triage, "_profile_summary", lambda: "EXPERIENCE: test")
    assert triage.run_score(conn) == 0
    assert _status(conn, ids["short_queued"]) == "rejected"


def test_purge_without_llm_cache_table(conn: sqlite3.Connection) -> None:
    rep = purge_empty_jd_cache(conn)
    assert rep.cache_rows == 0 and rep.short_jd_jobs == 0


def test_purge_cli_command(jobkit_home: object) -> None:
    from jobkit.cli import app

    c = q.connect(create=True)  # the sandboxed DB_PATH
    q.init_db(c)
    _seed(c)
    c.close()
    runner = CliRunner()
    dry = runner.invoke(app, ["purge-empty-jd-cache", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "DRY RUN" in dry.output and ": 3 (would delete)" in dry.output
    real = runner.invoke(app, ["purge-empty-jd-cache"])
    assert real.exit_code == 0, real.output
    assert "DRY RUN" not in real.output
    c = q.connect()
    assert c.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] == len(KEPT_KEYS)


def test_purge_mcp_tool_is_dry_run_until_confirmed(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import mcp_server

    _seed(conn)
    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    out = mcp_server.purge_empty_jd_cache()
    assert "DRY RUN" in out
    assert conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] == 5
    out = mcp_server.purge_empty_jd_cache(confirm=True)
    assert "DRY RUN" not in out
    assert conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] == len(KEPT_KEYS)
