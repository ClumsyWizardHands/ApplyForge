"""A job marked submitted on an aggregator row, then merged into the
employer's ATS row: a LEFT JOIN applications on the feed row's id showed no
application.

The aggregator row's apply URL was pointed at the employer's posting before
staging; mark_submitted recorded an application row at 'submitted'; the
next nightly poll merged the feed row into its ATS row (merged_into events),
and the application and the decision moved to the ATS row ('applied').
Nothing was lost, but lookups by the feed id (and MCP record_outcome) found
nothing. Fixed alongside: mark_submitted now leaves exactly ONE application
row at 'submitted' (several rows are folded into one), re-tailoring never
sets a submitted row
back, an auto-approve never adds a second row, record_outcome follows merges,
and `jobkit repair-applications [--dry-run]` / MCP repair_applications repair
rows left inconsistent before. Offline; never a live DB."""

from __future__ import annotations

import datetime
import json
import sqlite3
from collections.abc import Callable
from typing import Any

import pytest
from typer.testing import CliRunner

from jobkit.pipeline import tailor
from jobkit.pipeline.dedup import merge_feed_row
from jobkit.track import applications
from tests.test_targeting import GOOD_SEL, _sel, env  # noqa: F401 — env is a fixture

TODAY = datetime.date.today()
TYRELL_URL = "https://job-boards.greenhouse.io/tyrell/jobs/5102000309"


def _days_ago(n: int) -> str:
    return (TODAY - datetime.timedelta(days=n)).isoformat()


def _mcp(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> Any:
    from jobkit import mcp_server

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    conn.executescript(mcp_server.PINS_SCHEMA)
    return mcp_server


def _company(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO companies (id, name, normalized_name) VALUES (1, 'Tyrell', 'tyrell')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO boards (id, company_id, ats, token) "
        "VALUES (1, 1, 'greenhouse', 'tyrell')"
    )


def _job(
    conn: sqlite3.Connection,
    ext: str,
    *,
    board: int | None = None,
    status: str = "approved",
    apply_url: str = TYRELL_URL,
) -> int:
    _company(conn)
    jid = conn.execute(
        """INSERT INTO jobs (board_id, company_id, external_id, title, normalized_title,
           company_name, normalized_company, location_raw, location_bucket, cluster_key,
           jd_html, jd_hash, apply_url, status)
           VALUES (?, 1, ?, 'Sales Enablement Manager', 'sem', 'Tyrell', 'tyrell',
                   'United States', 'remote-us', ?, '<p>JD</p>', 'h', ?, ?)""",
        (board, ext, f"k-{ext}", apply_url, status),
    ).lastrowid
    assert jid is not None
    conn.commit()
    return int(jid)


def _app(
    conn: sqlite3.Connection, jid: int, stage: str = "tailored", submitted_at: str | None = None
) -> int:
    aid = conn.execute(
        """INSERT INTO applications (job_id, stage, resume_path, submitted_at)
           VALUES (?, ?, ?, ?)""",
        (jid, stage, f"/out/{jid}-{stage}/resume.pdf", submitted_at),
    ).lastrowid
    assert aid is not None
    conn.commit()
    return int(aid)


def _apps(conn: sqlite3.Connection, jid: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM applications WHERE job_id=? ORDER BY id", (jid,)).fetchall()


def _dump(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    out: list[tuple[Any, ...]] = []
    for t in ("applications", "jobs", "events"):
        out += [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY id")]  # noqa: S608
    return out


# ---- submitted, then merged by the nightly poll ------------------------------------


def test_a_submission_merged_into_the_ats_row_stays_one_submitted_row(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = _mcp(conn, monkeypatch)
    feed = _job(conn, "himalayas-tyrell")  # the aggregator row the human approved
    _app(conn, feed)
    conn.execute(
        "INSERT INTO decisions (job_id, decision, reason, auto) VALUES (?, 'approve', 'h', 0)",
        (feed,),
    )
    day = _days_ago(4)
    assert "submitted " + day in mcp.mark_submitted(job_ids=str(feed), submitted_on=day)
    ats = _job(conn, "5102000309", board=1, status="new")
    assert merge_feed_row(conn, feed, ats) == "merged"
    conn.commit()

    # the symptom: nothing on the feed row's id any more ...
    row = conn.execute(
        "SELECT j.status, a.stage FROM jobs j LEFT JOIN applications a ON a.job_id=j.id "
        "WHERE j.id=?",
        (feed,),
    ).fetchone()
    assert tuple(row) == ("closed", None)
    # ... because it all moved to the ATS row: one row, submitted
    (app,) = _apps(conn, ats)
    assert (app["stage"], app["submitted_at"]) == ("submitted", day)
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (ats,)).fetchone()[0] == "applied"
    # nothing to repair
    rep = applications.repair(conn, dry_run=True)
    assert rep.findings == [], rep.render()
    # the feed row's id still reaches it: record_outcome used to say "no submitted application"
    out = mcp.record_outcome(feed, "reply", "recruiter wrote back")
    assert f"recorded reply for job {ats}" in out and f"(was {feed})" in out
    assert "already marked submitted" in mcp.mark_submitted(job_ids=str(feed))
    assert len(_apps(conn, ats)) == 1


# ---- mark_submitted: exactly one row at 'submitted' -------------------------------


def test_mark_submitted_folds_two_application_rows_into_one(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = _mcp(conn, monkeypatch)
    jid = _job(conn, "dupe")
    old = _app(conn, jid, "approved")
    conn.execute(
        "UPDATE applications SET resume_path=NULL, cover_path='/out/c.pdf' WHERE id=?", (old,)
    )
    new = _app(conn, jid, "tailored")
    out = mcp.mark_submitted(job_ids=str(jid), submitted_on=_days_ago(1))
    assert "1 duplicate application row(s) folded into one" in out
    (app,) = _apps(conn, jid)
    assert app["id"] == new and app["stage"] == "submitted"
    assert app["resume_path"] == f"/out/{jid}-tailored/resume.pdf"  # the newest tailoring
    assert app["cover_path"] == "/out/c.pdf"  # taken from the folded row where it had none
    (ev,) = conn.execute("SELECT detail FROM events WHERE kind='applications_consolidated'")
    detail = json.loads(ev[0])
    assert detail["kept"] == new and [r["id"] for r in detail["removed"]] == [old]
    assert set(detail) == {"kept", "removed"}


def test_a_second_row_on_a_submitted_job_is_folded_and_the_date_kept(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = _mcp(conn, monkeypatch)
    jid = _job(conn, "double")
    first = _app(conn, jid, "submitted", _days_ago(9))
    _app(conn, jid, "tailored")  # a later approve added another row
    conn.commit()
    out = mcp.mark_submitted(job_ids=str(jid))
    assert f"already marked submitted ({_days_ago(9)})" in out
    (app,) = _apps(conn, jid)
    assert app["id"] == first and app["submitted_at"] == _days_ago(9)


def test_mark_submitted_creates_the_row_when_there_is_none(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = _mcp(conn, monkeypatch)
    jid = _job(conn, "by-hand", status="queued")
    mcp.mark_submitted(job_ids=str(jid), submitted_on=_days_ago(2))
    (app,) = _apps(conn, jid)
    assert (app["stage"], app["submitted_at"]) == ("submitted", _days_ago(2))


def test_an_already_submitted_row_is_left_as_it_was(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = _mcp(conn, monkeypatch)
    jid = _job(conn, "already", status="applied")
    _app(conn, jid, "submitted", _days_ago(3))
    before = _dump(conn)
    out = mcp.mark_submitted(job_ids=str(jid))
    assert out.endswith(f"already marked submitted ({_days_ago(3)}), unchanged")
    assert _dump(conn) == before


def test_an_auto_approve_never_adds_a_second_row(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import rules

    jid = _job(conn, "auto", status="queued")
    _app(conn, jid, "approved")  # e.g. approved once, then sent back to the queue
    conn.execute("INSERT INTO scores (job_id, fit_score) VALUES (?, 99)", (jid,))
    conn.commit()
    monkeypatch.setattr(
        rules,
        "_load_rules",
        lambda: {"auto_approve": [{"when": {"score_gte": 50}, "reason": "test rule"}]},
    )
    assert rules.run_standing_rules(conn)["auto_approved"] == 1
    assert len(_apps(conn, jid)) == 1


# ---- re-tailoring a submitted job ------------------------------------------------


def test_re_tailoring_never_sets_a_submitted_row_back(
    conn: sqlite3.Connection,
    env: Callable[..., Any],  # noqa: F811
) -> None:
    _, jid, _ = env([_sel(*GOOD_SEL), _sel(*GOOD_SEL)])
    tailor.tailor_job(conn, jid)
    before = dict(_apps(conn, jid)[0])
    conn.execute(
        "UPDATE applications SET stage='submitted', submitted_at=? WHERE id=?",
        (_days_ago(1), before["id"]),
    )
    conn.commit()
    tailor.tailor_job(conn, jid)  # `jobkit tailor --job-id`, MCP retailor
    (app,) = _apps(conn, jid)
    assert (app["stage"], app["submitted_at"]) == ("submitted", _days_ago(1))
    assert app["resume_path"] == before["resume_path"]  # what was sent stays recorded
    kinds = [r[0] for r in conn.execute("SELECT kind FROM events WHERE job_id=?", (jid,))]
    assert "retailored_after_submit" in kinds


# ---- repair-applications -----------------------------------------------------------


def _broken(conn: sqlite3.Connection) -> dict[str, int]:
    """One job per inconsistency."""
    ids: dict[str, int] = {}
    # a submission recorded by event, with no application row saying so
    ids["lost"] = _job(conn, "lost", status="applied")
    conn.execute(
        "INSERT INTO events (job_id, kind, detail) VALUES (?, 'marked_submitted', ?)",
        (ids["lost"], json.dumps({"submitted_on": _days_ago(5), "due": [], "skipped": []})),
    )
    # two rows on one job, one submitted
    ids["dupes"] = _job(conn, "dupes", status="applied")
    _app(conn, ids["dupes"], "approved")
    _app(conn, ids["dupes"], "submitted", _days_ago(6))
    # a submitted row a re-tailor set back
    ids["went_back"] = _job(conn, "went-back", status="applied")
    _app(conn, ids["went_back"], "tailored", _days_ago(7))
    # a submitted row on a job still 'approved'
    ids["not_applied"] = _job(conn, "not-applied", status="approved")
    _app(conn, ids["not_applied"], "submitted", _days_ago(8))
    # an application left on an aggregator row merged into an ATS row
    ids["feed"] = _job(conn, "feed-left", status="closed")
    ids["ats"] = _job(conn, "ats-left", board=1, status="applied")
    conn.execute("UPDATE jobs SET closed_at=datetime('now') WHERE id=?", (ids["feed"],))
    conn.execute(
        "INSERT INTO events (job_id, kind, detail) VALUES (?, 'merged_into', ?)",
        (ids["feed"], json.dumps({"ats_job_id": ids["ats"]})),
    )
    _app(conn, ids["feed"], "submitted", _days_ago(2))
    # 'applied' with no submission on record: listed, never guessed
    ids["unknown"] = _job(conn, "unknown", status="applied")
    conn.commit()
    return ids


def test_repair_dry_run_lists_everything_and_changes_nothing(conn: sqlite3.Connection) -> None:
    ids = _broken(conn)
    before = _dump(conn)
    rep = applications.repair(conn, dry_run=True)
    assert _dump(conn) == before
    found = {(f.job_id, f.kind, f.fixed) for f in rep.findings}
    assert (ids["lost"], "submission-not-recorded", True) in found
    assert (ids["dupes"], "several-applications", True) in found
    assert (ids["went_back"], "stage-went-back", True) in found
    assert (ids["not_applied"], "status-not-applied", True) in found
    assert not [f for f in rep.findings if "follow" in f.kind or "follow" in f.detail]
    assert (ids["ats"], "application-on-merged-row", True) in found
    assert (ids["unknown"], "applied-without-submission", False) in found
    text = rep.render()
    assert text.startswith("DRY RUN — nothing changed.") and "[needs you]" in text


def test_repair_leaves_one_submitted_row_everywhere(
    conn: sqlite3.Connection,
) -> None:
    ids = _broken(conn)
    applications.repair(conn)
    for key in ("lost", "dupes", "went_back", "not_applied", "ats"):
        jid = ids[key]
        (app,) = _apps(conn, jid)
        assert app["stage"] == "submitted" and app["submitted_at"], key
        assert conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0] == (
            "applied"
        ), key
    assert _apps(conn, ids["lost"])[0]["submitted_at"] == _days_ago(5)  # the event's day
    assert _apps(conn, ids["feed"]) == []
    assert _apps(conn, ids["unknown"]) == []  # never invented
    events = conn.execute("SELECT COUNT(*) FROM events WHERE kind='application_repaired'")
    assert events.fetchone()[0] >= 5
    # a second run finds only what needs the human
    again = applications.repair(conn)
    assert [(f.job_id, f.kind) for f in again.findings] == [
        (ids["unknown"], "applied-without-submission")
    ]


def test_repair_never_moves_an_application_onto_a_row_that_has_one(
    conn: sqlite3.Connection,
) -> None:
    feed = _job(conn, "feed-both", status="closed")
    ats = _job(conn, "ats-both", board=1, status="applied")
    conn.execute("UPDATE jobs SET closed_at=datetime('now') WHERE id=?", (feed,))
    conn.execute(
        "INSERT INTO events (job_id, kind, detail) VALUES (?, 'merged_into', ?)",
        (feed, json.dumps({"ats_job_id": ats})),
    )
    _app(conn, feed, "submitted", _days_ago(2))
    _app(conn, ats, "submitted", _days_ago(3))
    rep = applications.repair(conn)
    (f,) = [x for x in rep.findings if x.kind == "application-on-merged-row"]
    assert f.job_id == feed and not f.fixed and "decide which to keep" in f.detail
    assert len(_apps(conn, feed)) == 1 and len(_apps(conn, ats)) == 1


def test_cli_and_mcp(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    from jobkit import cli

    ids = _broken(conn)
    monkeypatch.setattr(cli, "_db", lambda: conn)
    before = _dump(conn)
    res = CliRunner().invoke(cli.app, ["repair-applications", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "DRY RUN" in res.output and f"job {ids['lost']}: submission-not-recorded" in res.output
    assert _dump(conn) == before
    mcp = _mcp(conn, monkeypatch)
    assert "DRY RUN" in mcp.repair_applications()
    assert _dump(conn) == before
    assert "fixed" in mcp.repair_applications(confirm=True)
    res = CliRunner().invoke(cli.app, ["repair-applications"])
    assert res.exit_code == 0 and "fixed 0, 1 left for you" in res.output
