"""The one-time removal of the follow-up notes table (db.migrate_followups,
run by init_db). A database made before follow-up drafting was removed has a
`followups` table: init_db copies the database to
<name>.pre-followups.db (never over an existing copy), keeps every row,
drafts included, as a 'followups_archived' event, then drops the table. It
runs once; every later init_db finds nothing to do.

Also the grep gate: no follow-up drafting is left in src/ or prompts/.
Temp databases only; never data/jobkit.db."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from jobkit import db as q
from jobkit.track import applications
from tests.conftest import REPO_ROOT

# the table as every database before the removal had it
OLD_FOLLOWUPS_TABLE = """
CREATE TABLE IF NOT EXISTS followups (
  id INTEGER PRIMARY KEY,
  application_id INTEGER NOT NULL REFERENCES applications(id),
  kind TEXT NOT NULL, due_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',      -- pending|done|skipped
  draft TEXT
);
"""

DRAFT = json.dumps({"subject": "Checking in", "body": "Still interested.\nAny news?"})


def _old_db(path: Path) -> sqlite3.Connection:
    """A database in the shape the last release left it: the current schema
    plus the followups table with rows, one of them an orphan (its
    application deleted while foreign keys were off)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = q.connect(path, create=True)
    conn.executescript(q.SCHEMA)
    conn.executescript(OLD_FOLLOWUPS_TABLE)
    conn.execute("INSERT INTO companies (id, name, normalized_name) VALUES (1, 'Acme', 'acme')")
    for jid in (1, 2):
        conn.execute(
            """INSERT INTO jobs (id, company_id, external_id, title, normalized_title,
               company_name, normalized_company, location_bucket, cluster_key, jd_html,
               jd_hash, apply_url, status)
               VALUES (?, 1, ?, 'Account Executive', 'ae', 'Acme', 'acme', 'remote-us',
                       ?, '<p>JD</p>', 'h', 'https://x.com', 'applied')""",
            (jid, f"e{jid}", f"k{jid}"),
        )
    # job 1: two application rows (a duplicate), each with follow-ups
    conn.execute(
        "INSERT INTO applications (id, job_id, stage, submitted_at) "
        "VALUES (1, 1, 'submitted', '2026-09-01')"
    )
    conn.execute("INSERT INTO applications (id, job_id, stage) VALUES (2, 1, 'tailored')")
    rows = [
        (1, 1, "status_check", "2026-09-08", "done", DRAFT),
        (2, 1, "close_as_ghosted", "2026-10-01", "pending", None),
        (3, 2, "status_check", "2026-09-08", "pending", None),
    ]
    conn.executemany(
        "INSERT INTO followups (id, application_id, kind, due_at, status, draft) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO followups (id, application_id, kind, due_at, status, draft) "
        "VALUES (4, 99, 'hiring_manager_note', '2026-09-15', 'pending', ?)",
        (DRAFT,),
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _archived(conn: sqlite3.Connection) -> list[tuple[Any, dict[str, Any]]]:
    return [
        (r["job_id"], json.loads(r["detail"]))
        for r in conn.execute(
            "SELECT job_id, detail FROM events WHERE kind='followups_archived' ORDER BY id"
        )
    ]


def test_init_db_backs_up_archives_every_row_and_drops_the_table(
    tmp_path: Path, jobkit_home: Path
) -> None:
    path = tmp_path / "data" / "jobkit.db"
    conn = _old_db(path)
    q.init_db(conn)

    assert "followups" not in _tables(conn)
    archived = _archived(conn)
    assert [d["id"] for _, d in archived] == [1, 2, 3, 4]
    by_id = {d["id"]: (job, d) for job, d in archived}
    assert by_id[1] == (
        1,
        {
            "id": 1,
            "application_id": 1,
            "kind": "status_check",
            "due_at": "2026-09-08",
            "status": "done",
            "draft": DRAFT,
        },
    )
    assert by_id[3][0] == 1  # application 2 belongs to job 1
    assert by_id[4][0] is None and by_id[4][1]["draft"] == DRAFT  # the orphan, kept

    backup = tmp_path / "data" / "jobkit.pre-followups.db"
    assert backup.is_file()
    copy = sqlite3.connect(backup)
    try:
        assert copy.execute("SELECT COUNT(*) FROM followups").fetchone()[0] == 4
        assert copy.execute("SELECT draft FROM followups WHERE id=1").fetchone()[0] == DRAFT
        events = "SELECT COUNT(*) FROM events WHERE kind='followups_archived'"
        assert copy.execute(events).fetchone()[0] == 0  # taken before anything changed
    finally:
        copy.close()

    log = (jobkit_home / "logs" / "jobkit.jsonl").read_text()
    assert '"event": "db.followups_archived"' in log and '"rows": 4' in log


def test_the_backup_of_a_wal_database_is_one_file(tmp_path: Path) -> None:
    """The source runs in WAL mode, and the backup API copies that mode into the
    copy. The copy is set back to a rollback journal before it is closed, so
    only data/jobkit.pre-followups.db is left, and opening it later writes no
    -wal or -shm file beside it."""
    path = tmp_path / "data" / "jobkit.db"
    conn = _old_db(path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    q.init_db(conn)
    backup = path.with_name("jobkit.pre-followups.db")
    assert backup.read_bytes()[18:20] == b"\x01\x01"  # rollback journal, not WAL

    def beside() -> list[str]:
        return sorted(p.name for p in path.parent.iterdir() if "pre-followups" in p.name)

    assert beside() == ["jobkit.pre-followups.db"]
    copy = sqlite3.connect(backup)
    try:
        assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert copy.execute("SELECT COUNT(*) FROM followups").fetchone()[0] == 4
        assert beside() == ["jobkit.pre-followups.db"]  # even while it is open
    finally:
        copy.close()
    assert beside() == ["jobkit.pre-followups.db"]


def test_running_it_again_changes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "data" / "jobkit.db"
    conn = _old_db(path)
    q.init_db(conn)
    before = [tuple(r) for r in conn.execute("SELECT * FROM events ORDER BY id")]
    q.init_db(conn)
    assert q.migrate_followups(conn) == 0
    assert [tuple(r) for r in conn.execute("SELECT * FROM events ORDER BY id")] == before
    # a second connection (another process) finds nothing to do either
    other = q.connect(path)
    q.init_db(other)
    assert [tuple(r) for r in other.execute("SELECT * FROM events ORDER BY id")] == before
    assert sorted(p.name for p in path.parent.iterdir() if "pre-followups" in p.name) == [
        "jobkit.pre-followups.db"
    ]


def test_an_existing_backup_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "data" / "jobkit.db"
    conn = _old_db(path)
    first = path.with_name("jobkit.pre-followups.db")
    first.write_bytes(b"an earlier backup")
    q.init_db(conn)
    assert first.read_bytes() == b"an earlier backup"
    second = path.with_name("jobkit.pre-followups-2.db")
    copy = sqlite3.connect(second)
    try:
        assert copy.execute("SELECT COUNT(*) FROM followups").fetchone()[0] == 4
    finally:
        copy.close()


def test_a_failed_backup_leaves_the_table_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "data" / "jobkit.db"
    conn = _old_db(path)

    def disk_full(db_file: Path, target: Path) -> None:
        raise OSError("disk full")

    # a scoped patch: monkeypatch.undo() would also undo the autouse sandbox
    with monkeypatch.context() as m:
        m.setattr(q, "_backup", disk_full)
        with pytest.raises(OSError, match="disk full"):
            q.init_db(conn)
    assert "followups" in _tables(conn)
    assert conn.execute("SELECT COUNT(*) FROM followups").fetchone()[0] == 4
    assert _archived(conn) == []
    q.init_db(conn)  # the next start completes it
    assert "followups" not in _tables(conn) and len(_archived(conn)) == 4


def _backups(path: Path) -> list[str]:
    return sorted(p.name for p in path.parent.iterdir() if "pre-followups" in p.name)


def test_an_old_session_recreating_the_table(tmp_path: Path) -> None:
    """An MCP server still running the old code runs the old schema on every
    tool call, which re-creates an empty followups table: dropping an empty
    table costs no copy. Its mark_submitted adds rows: those are backed up
    first, under the next free name (D2), then archived."""
    path = tmp_path / "data" / "jobkit.db"
    conn = _old_db(path)
    q.init_db(conn)
    assert _backups(path) == ["jobkit.pre-followups.db"]
    for _ in range(3):
        old = q.connect(path)
        old.executescript(OLD_FOLLOWUPS_TABLE)  # the old server's _conn()
        old.close()
        q.init_db(conn)
        assert "followups" not in _tables(conn)
    assert _backups(path) == ["jobkit.pre-followups.db"]
    assert len(_archived(conn)) == 4  # an empty table adds no events

    old = q.connect(path)
    old.executescript(OLD_FOLLOWUPS_TABLE)
    old.execute(
        "INSERT INTO followups (application_id, kind, due_at) "
        "VALUES (1, 'status_check', '2026-10-28')"
    )
    old.commit()
    old.close()
    assert q.migrate_followups(conn) == 1
    assert "followups" not in _tables(conn)
    assert _backups(path) == ["jobkit.pre-followups-2.db", "jobkit.pre-followups.db"]
    copy = sqlite3.connect(path.with_name("jobkit.pre-followups-2.db"))
    assert copy.execute("SELECT COUNT(*) FROM followups").fetchone()[0] == 1
    copy.close()
    assert len(_archived(conn)) == 5


def test_backups_stop_at_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, jobkit_home: Path
) -> None:
    path = tmp_path / "data" / "jobkit.db"
    conn = _old_db(path)
    monkeypatch.setattr(q, "FOLLOWUPS_BACKUP_MAX", 1)
    q.init_db(conn)
    old = q.connect(path)
    old.executescript(OLD_FOLLOWUPS_TABLE)
    old.execute(
        "INSERT INTO followups (application_id, kind, due_at) "
        "VALUES (1, 'status_check', '2026-10-28')"
    )
    old.commit()
    old.close()
    assert q.migrate_followups(conn) == 1
    assert _backups(path) == ["jobkit.pre-followups.db"]
    assert len(_archived(conn)) == 5
    log = (jobkit_home / "logs" / "jobkit.jsonl").read_text()
    assert '"event": "db.followups_backup_skipped"' in log


def test_init_db_waits_out_a_long_migration_lock(tmp_path: Path) -> None:
    """Another process copying the database for the migration holds the write
    lock longer than sqlite3's default 5 s; init_db waits for it, then puts
    the connection's own timeout back."""
    path = tmp_path / "data" / "jobkit.db"
    conn = _old_db(path)
    q.init_db(conn)
    other = sqlite3.connect(path)
    assert other.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    seen: list[int] = []
    real = q.migrate_followups

    def spy(c: sqlite3.Connection) -> int:
        seen.append(c.execute("PRAGMA busy_timeout").fetchone()[0])
        return real(c)

    q.migrate_followups = spy  # type: ignore[assignment]
    try:
        q.init_db(other)
    finally:
        q.migrate_followups = real  # type: ignore[assignment]
    assert seen == [q.INIT_DB_BUSY_TIMEOUT_MS]
    assert other.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    other.close()


def test_an_empty_table_is_dropped_without_a_backup(tmp_path: Path, jobkit_home: Path) -> None:
    path = tmp_path / "data" / "jobkit.db"
    path.parent.mkdir(parents=True)
    conn = q.connect(path, create=True)
    conn.executescript(q.SCHEMA)
    conn.executescript(OLD_FOLLOWUPS_TABLE)
    conn.commit()
    q.init_db(conn)
    assert "followups" not in _tables(conn)
    assert _backups(path) == [] and _archived(conn) == []
    log = (jobkit_home / "logs" / "jobkit.jsonl").read_text()
    assert '"event": "db.followups_dropped_empty"' in log


def test_a_partial_backup_file_is_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "data" / "jobkit.db"
    conn = _old_db(path)
    real_connect = sqlite3.connect

    class Broken:
        def __init__(self, inner: sqlite3.Connection) -> None:
            self.inner = inner

        def backup(self, dest: sqlite3.Connection) -> None:
            dest.execute("CREATE TABLE half (x)")
            dest.commit()
            raise sqlite3.OperationalError("interrupted")

        def close(self) -> None:
            self.inner.close()

    def connect(database: Any, *a: Any, **k: Any) -> Any:
        c = real_connect(database, *a, **k)
        return Broken(c) if Path(str(database)).resolve() == path.resolve() else c

    monkeypatch.setattr(q.sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        q.migrate_followups(conn)
    assert not path.with_name("jobkit.pre-followups.db").exists()
    assert "followups" in _tables(conn)


def test_an_in_memory_database_migrates_without_a_backup_file() -> None:
    conn = q.connect(Path(":memory:"))
    conn.executescript(q.SCHEMA)
    conn.executescript(OLD_FOLLOWUPS_TABLE)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO followups (application_id, kind, due_at) VALUES (7, 'status_check', 'x')"
    )
    conn.commit()
    assert q.migrate_followups(conn) == 1
    assert "followups" not in _tables(conn)


def test_folding_duplicate_rows_works_after_the_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import mcp_server

    conn = _old_db(tmp_path / "data" / "jobkit.db")
    q.init_db(conn)
    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    conn.executescript(mcp_server.PINS_SCHEMA)
    out = mcp_server.mark_submitted(job_ids="1")
    assert "already marked submitted (2026-09-01)" in out and "1 duplicate" in out
    assert [r[0] for r in conn.execute("SELECT id FROM applications WHERE job_id=1")] == [1]
    rep = applications.repair(conn)
    assert not [f for f in rep.findings if f.fixed], rep.render()


# ---- the grep gate ----------------------------------------------------------------

# identifiers of the removed drafting, MCP tools and CLI command
DRAFTING = re.compile(
    r"process_followups|followup_lines|get_followups|run_followups|mark_followup_sent"
    r"|schedule_followups|DUE_FOLLOWUPS|PromptFile\(\"followup\"\)|jobkit followups"
    r"|track\.followups|track/followups"
)
# files that may still say "follow-up(s)": the migration (db.py), the legacy
# ghost-days fallback (config.py, doctor.py), the setup interview's template
# copy (onboarding/intake.py), the old LaunchAgent label removed on
# install/uninstall (schedule.py), and unrelated words in two validators
MAY_MENTION = {
    "src/jobkit/db.py",
    "src/jobkit/config.py",
    "src/jobkit/doctor.py",
    "src/jobkit/onboarding/intake.py",
    "src/jobkit/schedule.py",
    "src/jobkit/validate/claims.py",
    "src/jobkit/autofill/answer_bank.py",
}
FOLLOW_UP = re.compile(r"follow[-_ ]?ups?\b", re.I)


def _sources() -> list[Path]:
    """src/**/*.py and prompts/*.md, skipping hidden dirs (stray .claude/worktrees
    copies, virtualenvs) and __pycache__."""
    files = [*(REPO_ROOT / "src").rglob("*.py"), *(REPO_ROOT / "prompts").glob("*.md")]
    return sorted(
        p
        for p in files
        if not any(
            part.startswith(".") or part == "__pycache__" for part in p.relative_to(REPO_ROOT).parts
        )
    )


def test_no_follow_up_drafting_is_left() -> None:
    assert not (REPO_ROOT / "src" / "jobkit" / "track" / "followups.py").exists()
    assert not list((REPO_ROOT / "prompts").glob("followup*"))
    hits = [
        f"{p.relative_to(REPO_ROOT)}:{n}"
        for p in _sources()
        for n, line in enumerate(p.read_text().splitlines(), 1)
        if DRAFTING.search(line)
    ]
    assert hits == []


def test_follow_ups_are_named_only_where_expected() -> None:
    named = {
        str(p.relative_to(REPO_ROOT))
        for p in _sources()
        if FOLLOW_UP.search(p.read_text()) and "__init__" not in p.name
    }
    assert named <= MAY_MENTION, sorted(named - MAY_MENTION)


def test_the_mcp_server_has_no_follow_up_tools() -> None:
    from jobkit import mcp_server

    for name in ("get_followups", "run_followups", "mark_followup_sent"):
        assert not hasattr(mcp_server, name)
    for name in ("email_targets", "match_email", "record_email_response", "finish_email_check"):
        assert callable(getattr(mcp_server, name))
