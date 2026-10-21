"""The no-reply rule (track/applications.close_ghosted): an application still
at stage 'submitted', with no response on record, submitted
tracking.ghost_after_days or more days ago becomes 'ghosted'. Plain
bookkeeping, no LLM. Offline, temp DB only."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from jobkit import db as q
from jobkit import llm
from jobkit.pipeline.digest import write_digest
from jobkit.track import responses
from jobkit.track.applications import GHOSTED_EVENT, close_ghosted
from tests.helpers.applications import days_ago, sql_days_ago, submitted


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*a: Any, **k: Any) -> Any:
        raise AssertionError("the no-reply rule must not call an LLM")

    monkeypatch.setattr(llm, "call", refuse)
    monkeypatch.setattr(llm, "call_json", refuse)


def _stage(conn: sqlite3.Connection, aid: int) -> str:
    return str(conn.execute("SELECT stage FROM applications WHERE id=?", (aid,)).fetchone()[0])


def _filters(home: Path, **blocks: Any) -> None:
    """Rewrite the sandbox filters.yaml with these top-level blocks (None
    removes one)."""
    path = home / "config" / "filters.yaml"
    raw = yaml.safe_load(path.read_text())
    for key, value in blocks.items():
        if value is None:
            raw.pop(key, None)
        else:
            raw[key] = value
    path.write_text(yaml.safe_dump(raw, sort_keys=False))


@contextlib.contextmanager
def _tz(name: str) -> Iterator[None]:
    """Set the process time zone (C library included) and restore it. Done by
    hand: undoing the shared monkeypatch would also undo the autouse sandbox."""
    before = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()


@pytest.fixture(params=["UTC", "America/Los_Angeles", "Pacific/Kiritimati"])
def zone(request: pytest.FixtureRequest) -> Iterator[str]:
    """A time zone west of, at and east of UTC: the edges must not move with
    the local clock."""
    with _tz(request.param):
        yield request.param


def test_only_submitted_rows_at_least_n_days_old_are_ghosted(
    conn: sqlite3.Connection, zone: str
) -> None:
    _, old = submitted(conn, "old", days=0, on=sql_days_ago(conn, 31))
    _, edge = submitted(conn, "edge", days=0, on=sql_days_ago(conn, 30))
    _, young = submitted(conn, "young", days=0, on=sql_days_ago(conn, 29))
    stages = {
        s: submitted(conn, f"s-{s}", days=60, stage=s)[1]
        for s in ("replied", "interview", "offer", "rejected", "tailored", "ghosted")
    }
    report = close_ghosted(conn, 30)
    assert sorted(r["id"] for r in report.ghosted) == sorted([old, edge])
    assert _stage(conn, old) == _stage(conn, edge) == "ghosted"
    assert _stage(conn, young) == "submitted"
    assert {s: _stage(conn, a) for s, a in stages.items()} == {s: s for s in stages}
    events = conn.execute("SELECT job_id, detail FROM events WHERE kind=?", (GHOSTED_EVENT,))
    details = [json.loads(r["detail"]) for r in events]
    assert sorted(d["application_id"] for d in details) == sorted([old, edge])
    assert {d["after_days"] for d in details} == {30}
    # a second run finds nothing new
    assert close_ghosted(conn, 30).ghosted == []
    assert (
        conn.execute("SELECT COUNT(*) FROM events WHERE kind=?", (GHOSTED_EVENT,)).fetchone()[0]
        == 2
    )


def test_a_timestamp_counts_from_its_local_day(conn: sqlite3.Connection) -> None:
    """mark_submitted with no day stamps UTC "now". An evening submission in
    New York is already the next day in UTC; it counts from the local day,
    as the old follow-up schedule did. A bare date is never shifted."""
    with _tz("America/New_York"):
        utc_day = sql_days_ago(conn, 29)  # 01:30 UTC that day is the evening before in NY
        _, evening = submitted(conn, "evening", days=0, on=f"{utc_day} 01:30:00")
        _, noon = submitted(conn, "noon", days=0, on=f"{utc_day} 16:00:00")
        _, bare = submitted(conn, "bare", days=0, on=utc_day)
        ghosted = {r["id"] for r in close_ghosted(conn, 30).ghosted}
    assert ghosted == {evening}
    assert _stage(conn, noon) == _stage(conn, bare) == "submitted"


def test_dry_run_changes_nothing(conn: sqlite3.Connection) -> None:
    _, aid = submitted(conn, "dry", days=45)
    before = [tuple(r) for r in conn.execute("SELECT * FROM applications")]
    report = close_ghosted(conn, 30, dry_run=True)
    assert [r["id"] for r in report.ghosted] == [aid]
    assert report.render().startswith("DRY RUN — nothing changed. would mark 1 application(s)")
    assert [tuple(r) for r in conn.execute("SELECT * FROM applications")] == before
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_a_recorded_response_is_never_ghosted(conn: sqlite3.Connection) -> None:
    # a stage left at 'submitted' with an outcome on record (an older event)
    jid, outcome = submitted(conn, "outcome", days=40)
    conn.execute(
        "INSERT INTO events (job_id, kind, detail) VALUES (?, 'outcome', ?)",
        (jid, json.dumps({"outcome": "reply", "application_id": outcome})),
    )
    # an email reply whose date kept the stage, and an acknowledgement only
    _, replied = submitted(conn, "email", days=40, company=(2, "Globex"))
    _, acked = submitted(conn, "ack", days=40, company=(3, "Initech"))
    for aid, kind, mid in ((replied, "recruiter_question", "m-1"), (acked, "acknowledged", "m-2")):
        responses.record_response(conn, aid, kind, days_ago(39), mid)
    conn.execute("UPDATE applications SET stage='submitted' WHERE id=?", (replied,))
    conn.commit()
    ghosted = {r["id"] for r in close_ghosted(conn, 30).ghosted}
    assert ghosted == {acked}  # an automatic receipt is not a reply


def test_a_reply_after_ghosting_moves_the_stage_on(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import mcp_server

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    jid, aid = submitted(conn, "late", days=50)
    close_ghosted(conn, 30)
    assert _stage(conn, aid) == "ghosted"
    out = mcp_server.record_outcome(jid, "reply", "wrote back after seven weeks")
    assert "stage ghosted -> replied" in out
    assert close_ghosted(conn, 30).ghosted == []
    assert _stage(conn, aid) == "replied"


def test_days_come_from_tracking_then_the_legacy_block(
    conn: sqlite3.Connection, jobkit_home: Path
) -> None:
    _, aid = submitted(conn, "cfg", days=12)
    # the sales test profile carries only the legacy followups block: 30
    assert close_ghosted(conn).days == 30
    assert _stage(conn, aid) == "submitted"
    _filters(jobkit_home, followups=[{"days_after_submit": 10, "kind": "close_as_ghosted"}])
    assert close_ghosted(conn, dry_run=True).days == 10
    _filters(jobkit_home, tracking={"ghost_after_days": 14})
    assert close_ghosted(conn).days == 14 and _stage(conn, aid) == "submitted"
    _filters(jobkit_home, tracking={"ghost_after_days": 11})
    assert [r["id"] for r in close_ghosted(conn).ghosted] == [aid]


def test_nightly_and_run_all_run_ghost_close_before_the_digest(
    conn: sqlite3.Connection,
) -> None:
    from jobkit import cli
    from jobkit.pipeline import nightly

    stages = dict(cli._run_all_stages())
    names = list(stages)
    assert names[-2:] == ["ghost-close", "digest"]
    night = [n for n, _ in nightly.nightly_stages(cli._run_all_stages())]
    assert night.index("ghost-close") < night.index("stage") < night.index("digest")
    _, aid = submitted(conn, "stage", days=31)
    summary = str(stages["ghost-close"](conn))
    assert summary.startswith("marked 1 application(s) ghosted")
    assert _stage(conn, aid) == "ghosted"


def test_cli_ghost_close(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    from jobkit import cli

    monkeypatch.setattr(cli, "_db", lambda: conn)
    jid, aid = submitted(conn, "cli", days=33)
    res = CliRunner().invoke(cli.app, ["ghost-close", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "DRY RUN" in res.output and f"job {jid} Acme — Account Executive" in res.output
    assert _stage(conn, aid) == "submitted"
    res = CliRunner().invoke(cli.app, ["ghost-close"])
    assert res.exit_code == 0 and "marked 1 application(s) ghosted" in res.output
    assert _stage(conn, aid) == "ghosted"
    assert CliRunner().invoke(cli.app, ["followups"]).exit_code != 0  # the command is gone


def test_digest_and_stats_count_no_reply(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import mcp_server

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    submitted(conn, "a", days=40)
    submitted(conn, "b", days=5)
    close_ghosted(conn, 30)
    digest = write_digest(conn).read_text()
    funnel = digest[digest.index("## Funnel") :]
    assert "- no reply after 30 days: 1\n" in funnel
    assert "Follow-ups" not in digest and "follow-up" not in digest.lower()
    assert "no reply after 30 days: 1" in mcp_server.get_stats()


def test_no_followups_table_in_a_new_database(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "followups" not in tables and "contacts" in tables
    assert q.migrate_followups(conn) == 0


def test_mcp_ghost_close_lists_first(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import mcp_server

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    jid, aid = submitted(conn, "mcp", days=35)
    out = mcp_server.ghost_close()
    assert out.startswith("DRY RUN") and f"job {jid}" in out
    assert _stage(conn, aid) == "submitted"
    assert mcp_server.ghost_close(confirm=True).startswith("marked 1 application(s) ghosted")
    assert _stage(conn, aid) == "ghosted"
