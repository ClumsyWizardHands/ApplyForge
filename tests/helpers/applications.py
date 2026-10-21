"""Helpers that build jobs and application rows on a test database."""

from __future__ import annotations

import datetime
import sqlite3

from tests.test_filter import _insert


def days_ago(n: int) -> str:
    """The local date n days ago, read when called (a run may cross midnight)."""
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


def sql_days_ago(conn: sqlite3.Connection, n: int) -> str:
    """n days before SQLite's date('now'), the UTC clock that rules such as
    GHOST_CANDIDATES compare against, so a test's edges hold in any time zone."""
    return str(conn.execute("SELECT date('now', ?)", (f"-{int(n)} days",)).fetchone()[0])


def applied(conn: sqlite3.Connection, ext: str = "s1", stage: str = "tailored") -> int:
    """An approved Acme 'Account Executive' job with one application row at
    `stage` (not yet submitted). Returns the job id."""
    jid = _insert(conn, external_id=ext, title="Account Executive")
    conn.execute("UPDATE jobs SET status='approved' WHERE id=?", (jid,))
    conn.execute("INSERT INTO applications (job_id, stage) VALUES (?, ?)", (jid, stage))
    conn.commit()
    return jid


def submitted(
    conn: sqlite3.Connection,
    ext: str,
    *,
    days: int,
    stage: str = "submitted",
    title: str = "Account Executive",
    company: tuple[int, str] = (1, "Acme"),
    apply_url: str = "https://x.com",
    board: tuple[int, str, str] | None = None,
    on: str | None = None,
) -> tuple[int, int]:
    """A job marked applied with one application row submitted `days` ago at
    `stage` (or with submitted_at `on` exactly, when given). `company` is
    (id, name); `board` is (id, ats, token). Returns (job id, application id)."""
    cid, name = company
    conn.execute(
        "INSERT INTO companies (id, name, normalized_name) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
        (cid, name, name.lower()),
    )
    board_id = None
    if board is not None:
        board_id, ats, token = board
        conn.execute(
            "INSERT OR IGNORE INTO boards (id, company_id, ats, token) VALUES (?, ?, ?, ?)",
            (board_id, cid, ats, token),
        )
    jid = _insert(
        conn,
        external_id=ext,
        title=title,
        company_id=cid,
        company_name=name,
        normalized_company=name.lower(),
        apply_url=apply_url,
        board_id=board_id,
    )
    conn.execute("UPDATE jobs SET status='applied' WHERE id=?", (jid,))
    aid = conn.execute(
        "INSERT INTO applications (job_id, stage, submitted_at) VALUES (?, ?, ?)",
        (jid, stage, on if on is not None else days_ago(days)),
    ).lastrowid
    conn.commit()
    assert aid is not None
    return jid, int(aid)
