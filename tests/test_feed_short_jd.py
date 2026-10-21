"""Feed jobs whose description is too short to score are set aside as
jd_missing (triage). The digest and get_stats say how many, so they do not
vanish unseen; a digest with none and no ghosted applications ends its funnel
as it did before."""

from __future__ import annotations

import sqlite3

import pytest

from jobkit.pipeline import triage
from jobkit.pipeline.digest import write_digest
from tests.test_filter import _insert


def _short(conn: sqlite3.Connection, ext: str, *, board_id: int | None) -> int:
    if board_id is not None:
        conn.execute(
            "INSERT OR IGNORE INTO boards (id, company_id, ats, token) "
            "VALUES (?, 1, 'lever', 'acme')",
            (board_id,),
        )
    jid = _insert(
        conn,
        external_id=ext,
        jd_html="Provide nursing care.",
        board_id=board_id,
        title=f"Registered Nurse {ext}",
        location_raw="Columbus, OH",
        apply_url=f"https://www.adzuna.com/details/{ext}",
    )
    triage.record_jd_missing(conn, jid, "test", "Provide nursing care.", reject_job=True)
    conn.commit()
    return jid


def test_digest_counts_feed_jobs_set_aside(conn: sqlite3.Connection) -> None:
    _short(conn, "feed-1", board_id=None)
    _short(conn, "feed-2", board_id=None)
    _short(conn, "board-1", board_id=7)  # an employer-board job is not a feed job
    digest = write_digest(conn).read_text()
    assert "## Feed jobs not scored\n2 jobs from the job feeds came with too short" in digest
    # each is listed with its link, so the person can act on it
    section = digest[digest.index("## Feed jobs not scored") :].split("\n\n", 1)[0]
    for ext in ("feed-1", "feed-2"):
        line = f"- Registered Nurse {ext}, Acme, Columbus, OH: https://www.adzuna.com/details/{ext}"
        assert line in section
    assert "board-1" not in section


def test_digest_caps_the_list(conn: sqlite3.Connection) -> None:
    for i in range(12):
        _short(conn, f"feed-{i}", board_id=None)
    digest = write_digest(conn).read_text()
    section = digest[digest.index("## Feed jobs not scored") :].split("\n\n", 1)[0]
    assert section.count("https://www.adzuna.com/details/") == 10
    assert "- and 2 more (get_stats lists the newest 25)" in section


def test_digest_without_them_keeps_the_old_funnel(conn: sqlite3.Connection) -> None:
    digest = write_digest(conn).read_text()
    assert "Feed jobs not scored" not in digest
    funnel = digest[digest.index("## Funnel") : digest.index("## Top rejection reasons")]
    assert funnel.rstrip().splitlines()[-1].startswith("- submitted: ")
    assert "no reply after" not in funnel


def test_stats_count_feed_jobs_set_aside(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import mcp_server

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    assert "feed jobs not scored" not in mcp_server.get_stats()
    _short(conn, "feed-1", board_id=None)
    stats = mcp_server.get_stats()
    assert "feed jobs not scored (description too short): 1" in stats
    assert (
        "- Registered Nurse feed-1, Acme, Columbus, OH: https://www.adzuna.com/details/feed-1"
        in stats
    )


def test_an_approved_job_refused_by_tailoring_is_not_listed(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tailoring refuses an approved feed job whose description is too short:
    the same jd_missing rejection row, but the job keeps its approved status.
    It was not kept out of the queue, so the digest and get_stats do not list
    it as a feed job not scored."""
    from jobkit import mcp_server

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    jid = _insert(
        conn,
        external_id="approved-1",
        jd_html="Provide nursing care.",
        board_id=None,
        title="Registered Nurse approved-1",
        location_raw="Columbus, OH",
        apply_url="https://www.adzuna.com/details/approved-1",
    )
    conn.execute("UPDATE jobs SET status = 'approved' WHERE id = ?", (jid,))
    triage.record_jd_missing(conn, jid, "tailor", "Provide nursing care.", reject_job=False)
    conn.commit()
    reasons = conn.execute("SELECT layer, reason FROM rejections WHERE job_id = ?", (jid,))
    assert [tuple(r) for r in reasons] == [("sanity", "jd_missing")]
    assert "Feed jobs not scored" not in write_digest(conn).read_text()
    assert "feed jobs not scored" not in mcp_server.get_stats()
    # a rejected one beside it is still counted, alone
    _short(conn, "feed-1", board_id=None)
    digest = write_digest(conn).read_text()
    assert "## Feed jobs not scored\n1 job from the job feeds" in digest
    assert "approved-1" not in digest
    stats = mcp_server.get_stats()
    assert "feed jobs not scored (description too short): 1" in stats
    assert "approved-1" not in stats


def test_an_approved_job_refused_by_tailoring_then_rejected_is_not_listed(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approved, refused by tailoring for its short description, then rejected
    by the person with the MCP reject tool: the job's status is 'rejected' and
    the jd_missing row is still there, but the person scored and saw it, so
    the digest and get_stats do not list it as a feed job not scored."""
    from jobkit import mcp_server

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    jid = _insert(
        conn,
        external_id="approved-1",
        jd_html="Provide nursing care.",
        board_id=None,
        title="Registered Nurse approved-1",
        location_raw="Columbus, OH",
        apply_url="https://www.adzuna.com/details/approved-1",
    )
    conn.execute("UPDATE jobs SET status = 'approved' WHERE id = ?", (jid,))
    triage.record_jd_missing(conn, jid, "tailor", "Provide nursing care.", reject_job=False)
    conn.commit()
    monkeypatch.setattr(mcp_server, "_pinned", lambda _conn: {1: jid})
    assert mcp_server.reject("1", "too thin") == "rejected [1] (too thin)"
    status = conn.execute("SELECT status FROM jobs WHERE id = ?", (jid,)).fetchone()
    assert status["status"] == "rejected"
    assert "Feed jobs not scored" not in write_digest(conn).read_text()
    assert "feed jobs not scored" not in mcp_server.get_stats()
    # a feed job triage set aside beside it is still counted, alone
    _short(conn, "feed-1", board_id=None)
    digest = write_digest(conn).read_text()
    assert "## Feed jobs not scored\n1 job from the job feeds" in digest
    assert "approved-1" not in digest
    stats = mcp_server.get_stats()
    assert "feed jobs not scored (description too short): 1" in stats
    assert "approved-1" not in stats
