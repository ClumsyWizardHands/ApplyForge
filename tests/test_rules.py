"""Standing rules engine + pinned queue numbering. Offline."""

from __future__ import annotations

import sqlite3

import pytest
import yaml

from jobkit import rules as rules_mod
from jobkit.rules import _title_class, run_standing_rules
from tests.conftest import SpawnRecorder
from tests.test_filter import _insert

# an invented rule set that exercises every condition key
RULES = {
    "auto_approve": [
        {
            "when": {"tier": 1, "score_gte": 80, "sanity_flags": "none", "friction_lte": 40},
            "reason": "tier1-quick-apply",
        },
        {
            "when": {"score_gte": 95, "remote": True, "comp_min_gte": 190000},
            "reason": "remote-high-base",
        },
    ],
    "auto_reject": [
        {
            "when": {"title_class": "account_executive", "comp_max_lt": 130000},
            "reason": "ae-ceiling-too-low",
        },
        {
            "when": {
                "title_class": "sdr_bdr_manager",
                "any_of": [
                    {"score_lt": 70},
                    {"flag_present": "ote-unrealistic"},
                    {"comp_unpublished": True},
                ],
            },
            "reason": "manager-pay-unclear",
        },
        {"when": {"tier": 3, "score_lt": 72}, "reason": "tier3-below-bar"},
    ],
}


@pytest.fixture(autouse=True)
def rules_file(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    p = tmp_path / "autoapprove.yaml"
    p.write_text(yaml.safe_dump(RULES))
    monkeypatch.setattr(rules_mod, "RULES_PATH", str(p))


def _queued(
    conn: sqlite3.Connection,
    *,
    score: int,
    tier: int = 2,
    friction: int | None = 20,
    flags: str = "[]",
    **kw: object,
) -> int:
    jid = _insert(conn, **kw)
    conn.execute("UPDATE jobs SET status='queued' WHERE id=?", (jid,))
    conn.execute("UPDATE companies SET tier=? WHERE id=1", (tier,))
    conn.execute("INSERT OR REPLACE INTO scores (job_id, fit_score) VALUES (?, ?)", (jid, score))
    conn.execute("INSERT OR REPLACE INTO sanity (job_id, flags) VALUES (?, ?)", (jid, flags))
    if friction is not None:
        conn.execute(
            "INSERT OR REPLACE INTO form_specs (job_id, friction_score) VALUES (?, ?)",
            (jid, friction),
        )
    conn.commit()
    return jid


def test_title_class() -> None:
    assert _title_class("Enterprise Account Executive") == "account_executive"
    assert _title_class("Founding Account Executive") is None  # the good-deal carve-out
    assert _title_class("Director of Sales") is None


def test_auto_approve_tier1_clean(conn: sqlite3.Connection) -> None:
    jid = _queued(conn, score=90, tier=1, friction=30, external_id="r1")
    stats = run_standing_rules(conn)
    assert stats["auto_approved"] == 1
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()["status"] == (
        "approved"
    )
    d = conn.execute("SELECT auto, decision FROM decisions WHERE job_id=?", (jid,)).fetchone()
    assert d["auto"] == 1 and d["decision"] == "approve"


def test_no_auto_approve_with_sanity_flag(conn: sqlite3.Connection) -> None:
    _queued(conn, score=90, tier=1, friction=30, flags='["remote-conflict: x"]', external_id="r2")
    stats = run_standing_rules(conn)
    assert stats["auto_approved"] == 0, "sanity_flags: none must block auto-approval"


def test_ae_low_ceiling_auto_rejected(conn: sqlite3.Connection) -> None:
    jid = _queued(
        conn,
        score=95,
        title="Enterprise Account Executive",
        remote=1,
        comp_min=110000,
        comp_max=125000,
        external_id="r3",
    )
    stats = run_standing_rules(conn)
    assert stats["auto_rejected"] == 1
    reason = conn.execute(
        "SELECT reason FROM rejections WHERE job_id=? AND layer='standing_rule'", (jid,)
    ).fetchone()["reason"]
    assert reason == "ae-ceiling-too-low"


def test_manager_role_with_clear_pay_survives_a_modest_score(conn: sqlite3.Connection) -> None:
    # any_of: published, realistic pay and a score above the rule's floor of 70
    # leave none of its conditions true
    jid = _queued(
        conn,
        score=76,
        title="Manager, Sales Development",
        remote=1,
        comp_min=160000,
        comp_max=180000,
        external_id="r4",
    )
    run_standing_rules(conn)
    status = conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()["status"]
    assert status == "queued", "clear, realistic pay must survive a modest score"


def test_manager_role_with_inflated_ote_rejected(conn: sqlite3.Connection) -> None:
    jid = _queued(
        conn,
        score=76,
        title="Manager, Sales Development",
        remote=1,
        comp_min=160000,
        comp_max=180000,
        flags='["ote-unrealistic: uncapped commission doing all the work"]',
        external_id="r4b",
    )
    run_standing_rules(conn)
    status = conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()["status"]
    assert status == "rejected", "inflated OTE must reject"


def test_manager_role_with_unpublished_pay_rejected(conn: sqlite3.Connection) -> None:
    jid = _queued(conn, score=76, title="Manager, Sales Development", remote=1, external_id="r4c")
    run_standing_rules(conn)
    status = conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()["status"]
    assert status == "rejected", "unpublished pay must reject"


def test_manager_role_below_the_score_floor_rejected(conn: sqlite3.Connection) -> None:
    jid = _queued(
        conn,
        score=65,
        title="Manager, Sales Development",
        remote=1,
        comp_min=160000,
        comp_max=180000,
        external_id="r4d",
    )
    run_standing_rules(conn)
    status = conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()["status"]
    assert status == "rejected", "a score under the floor rejects even with clear pay"


def test_tier3_weak_fit_rejected(conn: sqlite3.Connection) -> None:
    jid = _queued(conn, score=70, tier=3, external_id="r5")
    run_standing_rules(conn)
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()["status"] == (
        "rejected"
    )


def test_queue_numbering_pinned(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, spawned: SpawnRecorder
) -> None:
    from jobkit import mcp_server

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    monkeypatch.setattr(mcp_server, "run_standing_rules", lambda c: {"evaluated": 0})
    conn.executescript(mcp_server.PINS_SCHEMA)
    a = _queued(conn, score=90, external_id="p1", title="Director A")
    b = _queued(conn, score=80, external_id="p2", title="Director B")
    out = mcp_server.get_queue()
    assert "1. " in out and "2. " in out
    # queue re-sorts between render and reply: item 2's score jumps above item 1
    conn.execute("UPDATE scores SET fit_score=99 WHERE job_id=?", (b,))
    conn.commit()
    mcp_server.approve("2")  # must still resolve to job b's ORIGINAL number
    status_b = conn.execute("SELECT status FROM jobs WHERE id=?", (b,)).fetchone()["status"]
    status_a = conn.execute("SELECT status FROM jobs WHERE id=?", (a,)).fetchone()["status"]
    assert status_b == "approved" and status_a == "queued", "a re-sort must not misroute approval"
    # approve() kicks off background tailoring. The spawn is recorded, never run:
    # a real `jobkit tailor` here would tailor every approved job in the live DB.
    assert spawned.calls == [mcp_server.background.cli_argv("tailor")]
