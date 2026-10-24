"""Deterministic filter: every rejection logged with its reason."""

from __future__ import annotations

import sqlite3

from jobkit.config import Filters
from jobkit.pipeline.filter import run_filters

FILTERS = Filters(
    raw={
        "filters": {
            "include_titles": [r"(?i)\bdirector\b", r"(?i)\bdeal desk\b"],
            "exclude_titles": [r"(?i)\bintern\b"],
            "blocklist_companies": ["EvilCorp"],
            "comp": {"floor": 165000},
            "freshness": {"max_age_days": 30},
            "locations": {"allowed": ["Philadelphia"], "accept_remote": True},
        },
        # relocation cities pass the location filter like allowed places
        "remote_policy": {"relocation_cities": ["Chicago", "Raleigh"]},
    }
)


def _insert(conn: sqlite3.Connection, **kw: object) -> int:
    defaults = {
        "board_id": None,
        "company_id": 1,
        "external_id": "x",
        "title": "Director of Sales",
        "normalized_title": "director of sales",
        "company_name": "Acme",
        "normalized_company": "acme",
        "location_raw": "Remote, US",
        "location_bucket": "remote-us",
        "cluster_key": "a|b|c",
        "remote": 1,
        "comp_min": None,
        "comp_max": None,
        "comp_currency": None,
        "jd_html": "x" * 300,
        "jd_hash": "h",
        "apply_url": "https://x.com",
        "posted_at": None,
    }
    defaults.update(kw)
    conn.execute(
        "INSERT INTO companies (id, name, normalized_name) VALUES (1, 'Acme', 'acme') "
        "ON CONFLICT DO NOTHING"
    )
    cur = conn.execute(
        """INSERT INTO jobs (board_id, company_id, external_id, title, normalized_title,
           company_name, normalized_company, location_raw, location_bucket, cluster_key,
           remote, comp_min, comp_max, comp_currency, jd_html, jd_hash, apply_url, posted_at)
           VALUES (:board_id, :company_id, :external_id, :title, :normalized_title,
           :company_name, :normalized_company, :location_raw, :location_bucket,
           :cluster_key, :remote, :comp_min, :comp_max, :comp_currency, :jd_html,
           :jd_hash, :apply_url, :posted_at)""",
        defaults,
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def clear_for_auto_approve(conn: sqlite3.Connection, jid: int) -> None:
    """Make job `jid` the employer's own posting (a board of its own) with a
    clear location (remote check verdicts 'remote'): a job without both is
    never approved automatically, whatever the rule (owner decisions D4/D6),
    so a test of another rule condition sets both. Keeps a sanity row's
    flags; adds a row with no flags when there is none."""
    company = conn.execute("SELECT company_id FROM jobs WHERE id = ?", (jid,)).fetchone()[0]
    board = conn.execute(
        "INSERT INTO boards (company_id, ats, token) VALUES (?, 'greenhouse', ?)",
        (company, f"test-board-{jid}"),
    ).lastrowid
    conn.execute("UPDATE jobs SET board_id = ? WHERE id = ?", (board, jid))
    conn.execute(
        """INSERT INTO sanity (job_id, flags, remote_stated, remote_actual)
           VALUES (?, '[]', 'remote', 'remote')
           ON CONFLICT(job_id) DO UPDATE SET remote_stated = 'remote', remote_actual = 'remote'""",
        (jid,),
    )


def test_filters_log_every_rejection(conn: sqlite3.Connection) -> None:
    ids = {
        "pass": _insert(conn, external_id="1"),
        "excl": _insert(conn, external_id="2", title="Director Intern"),
        "no_incl": _insert(conn, external_id="3", title="Account Executive"),
        "block": _insert(conn, external_id="4", company_name="EvilCorp Inc"),
        "loc": _insert(
            conn, external_id="5", remote=0, location_raw="Tulsa, OK", location_bucket="other-tulsa"
        ),
        "comp": _insert(conn, external_id="6", comp_min=90000, comp_max=120000),
        "stale": _insert(conn, external_id="7", posted_at="2026-01-01"),
    }
    conn.commit()
    stats = run_filters(conn, FILTERS)
    assert stats == {"examined": 7, "passed": 1, "rejected": 6}
    status = {
        k: conn.execute("SELECT status FROM jobs WHERE id=?", (v,)).fetchone()["status"]
        for k, v in ids.items()
    }
    assert status["pass"] == "filtered"
    assert all(v == "rejected" for k, v in status.items() if k != "pass")
    reasons = {
        r["job_id"]: r["reason"]
        for r in conn.execute(
            "SELECT job_id, reason FROM rejections WHERE layer='filter'"
        ).fetchall()
    }
    assert len(reasons) == 6, "every rejection must be logged"
    assert reasons[ids["excl"]].startswith("exclude_title")
    assert reasons[ids["no_incl"]] == "no_include_title_match"
    assert reasons[ids["block"]].startswith("blocklist_company")
    assert reasons[ids["loc"]].startswith("location:")
    assert reasons[ids["comp"]].startswith("comp_below_floor")
    assert reasons[ids["stale"]].startswith("stale:")


def test_relocation_cities_pass(conn: sqlite3.Connection) -> None:
    a = _insert(conn, external_id="8", remote=0, location_bucket="chicago-metro")
    b = _insert(conn, external_id="9", remote=0, location_bucket="other-raleigh")
    conn.commit()
    run_filters(conn, FILTERS)
    for jid in (a, b):
        assert (
            conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()["status"]
            == "filtered"
        )
