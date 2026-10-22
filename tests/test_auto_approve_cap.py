"""The optional daily limit on automatic approval (max_auto_approve_per_day)
and the flag that opens the filled forms after the daily run
(staging.open_after_daily_run). There is no limit unless the person sets
one, and a config without either key behaves exactly as before. Offline."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from jobkit import doctor, rules, schedule
from jobkit.pipeline import digest
from jobkit.rules import run_standing_rules
from tests.test_filter import _insert

# the rule set of tests/test_rules.py: every condition key, approve and reject
RULES: dict[str, Any] = {
    "auto_approve": [
        {
            "when": {"tier": 1, "score_gte": 80, "sanity_flags": "none", "friction_lte": 40},
            "reason": "tier1-quick-apply",
        },
        {"when": {"score_gte": 95, "remote": True, "comp_min_gte": 190000}, "reason": "rhb"},
    ],
    "auto_reject": [
        {
            "when": {"title_class": "account_executive", "comp_max_lt": 130000},
            "reason": "ae-ceiling-too-low",
        },
        {"when": {"tier": 3, "score_lt": 72}, "reason": "tier3-below-bar"},
    ],
}

# the interview's rule: approve what clears the bar, no sanity flag
BAR = {"auto_approve": [{"when": {"score_gte": 75, "sanity_flags": "none"}, "reason": "bar"}]}


def _write_rules(path: Path, monkeypatch: pytest.MonkeyPatch, cfg: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setattr(rules, "RULES_PATH", str(path))


@pytest.fixture()
def rules_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "autoapprove.yaml"
    _write_rules(p, monkeypatch, RULES)
    return p


def _queued(
    conn: sqlite3.Connection,
    ext: str,
    score: int | None,
    *,
    company: int = 1,
    tier: int = 2,
    flags: str = "[]",
    friction: int = 20,
    **kw: object,
) -> int:
    conn.execute(
        "INSERT INTO companies (id, name, normalized_name, tier) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET tier = excluded.tier",
        (company, f"Co{company}", f"co{company}", tier),
    )
    jid = _insert(conn, external_id=ext, company_id=company, **kw)
    conn.execute("UPDATE jobs SET status='queued' WHERE id=?", (jid,))
    if score is not None:
        conn.execute("INSERT INTO scores (job_id, fit_score) VALUES (?, ?)", (jid, score))
    conn.execute("INSERT INTO sanity (job_id, flags) VALUES (?, ?)", (jid, flags))
    conn.execute("INSERT INTO form_specs (job_id, friction_score) VALUES (?, ?)", (jid, friction))
    conn.commit()
    return jid


def _status(conn: sqlite3.Connection, jid: int) -> str:
    return str(conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()["status"])


def _golden_jobs(conn: sqlite3.Connection) -> dict[str, int]:
    """Jobs whose verdicts under RULES are fixed (see GOLDEN)."""
    return {
        "t1-clean": _queued(conn, "g1", 90, company=1, tier=1, friction=30),
        "t1-flagged": _queued(conn, "g2", 90, company=1, tier=1, flags='["remote-conflict: x"]'),
        "remote-high-base": _queued(conn, "g3", 96, company=2, tier=2, comp_min=200000),
        "ae-low-ceiling": _queued(
            conn,
            "g4",
            95,
            company=2,
            tier=2,
            title="Enterprise Account Executive",
            comp_min=110000,
            comp_max=125000,
        ),
        "t3-weak": _queued(conn, "g5", 70, company=3, tier=3),
        "t2-middling": _queued(conn, "g6", 78, company=2, tier=2),
        "unscored": _queued(conn, "g7", None, company=2, tier=2),
    }


GOLDEN = {
    "t1-clean": "approved",
    "t1-flagged": "queued",
    "remote-high-base": "approved",
    "ae-low-ceiling": "rejected",
    "t3-weak": "rejected",
    "t2-middling": "queued",
    "unscored": "queued",
}


# ---- owner invariance: no key, no change -----------------------------------


@pytest.mark.parametrize("cfg_extra", [{}, {rules.MAX_PER_DAY_KEY: None}])
def test_without_a_limit_verdicts_and_counts_are_unchanged(
    conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cfg_extra: dict[str, Any],
) -> None:
    _write_rules(tmp_path / "autoapprove.yaml", monkeypatch, {**RULES, **cfg_extra})
    jobs = _golden_jobs(conn)
    stats = run_standing_rules(conn)
    assert {name: _status(conn, jid) for name, jid in jobs.items()} == GOLDEN
    # the counts keep exactly their old keys
    assert stats == {"evaluated": 7, "auto_approved": 2, "auto_rejected": 2}
    assert conn.execute("SELECT COUNT(*) n FROM events").fetchone()["n"] == 0
    # without a limit nothing is sorted: rows are decided in job id order
    decided = [r["job_id"] for r in conn.execute("SELECT job_id FROM decisions ORDER BY rowid")]
    assert len(decided) == 4 and decided == sorted(decided)
    assert rules.digest_lines(conn) == []


def test_a_limit_above_the_day_changes_no_verdict(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_rules(
        tmp_path / "autoapprove.yaml", monkeypatch, {**RULES, "max_auto_approve_per_day": 50}
    )
    jobs = _golden_jobs(conn)
    stats = run_standing_rules(conn)
    assert {name: _status(conn, jid) for name, jid in jobs.items()} == GOLDEN
    assert stats == {
        "evaluated": 7,
        "auto_approved": 2,
        "auto_rejected": 2,
        "waiting_for_tomorrow": 0,
    }


def test_the_owner_profile_has_no_new_keys(jobkit_home: Path) -> None:
    """The test profile stands in for the owner's own gitignored config: it
    has neither key, so both read as off."""
    cfg = yaml.safe_load((jobkit_home / "config" / "autoapprove.yaml").read_text())
    assert rules.MAX_PER_DAY_KEY not in cfg
    assert rules.max_auto_approve_per_day(cfg) is None
    filters = yaml.safe_load((jobkit_home / "config" / "filters.yaml").read_text())
    assert "staging" not in filters


# ---- the limit, when the person sets one ------------------------------------


def test_limit_approves_the_best_first_and_the_rest_wait(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_rules(tmp_path / "autoapprove.yaml", monkeypatch, {**BAR, "max_auto_approve_per_day": 2})
    a = _queued(conn, "c1", 80)
    b = _queued(conn, "c2", 95)
    c = _queued(conn, "c3", 90)
    d = _queued(conn, "c4", 85)
    low = _queued(conn, "c5", 60)  # under the bar: never counted as waiting
    stats = run_standing_rules(conn)
    assert stats["auto_approved"] == 2 and stats["waiting_for_tomorrow"] == 2
    assert [_status(conn, j) for j in (b, c)] == ["approved", "approved"]
    assert [_status(conn, j) for j in (a, d, low)] == ["queued", "queued", "queued"]
    waiting = conn.execute(
        "SELECT job_id, detail FROM events WHERE kind=? ORDER BY job_id", (rules.WAITING_EVENT,)
    ).fetchall()
    assert [w["job_id"] for w in waiting] == [a, d]
    assert "daily limit of 2" in waiting[0]["detail"]
    # a second run the same day (get_queue runs the rules too): nothing more
    # is approved, and no event is recorded twice
    again = run_standing_rules(conn)
    assert again["auto_approved"] == 0 and again["waiting_for_tomorrow"] == 2
    n = conn.execute("SELECT COUNT(*) n FROM events WHERE kind=?", (rules.WAITING_EVENT,))
    assert n.fetchone()["n"] == 2
    assert rules.waiting_for_tomorrow(conn) == 2 and rules.auto_approved_on(conn) == 2
    assert rules.digest_lines(conn) == [
        "## Automatic approval",
        "Approved automatically today: 2 (your daily limit is 2). 2 more cleared your bar "
        "and wait for tomorrow's run. They are in your queue, and you can approve any of "
        "them now.",
        "",
    ]


def test_approvals_earlier_today_count_and_older_ones_do_not(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_rules(tmp_path / "autoapprove.yaml", monkeypatch, {**BAR, "max_auto_approve_per_day": 2})
    old = _insert(conn, external_id="o1")
    conn.execute(
        "INSERT INTO decisions (job_id, decision, auto, decided_at) "
        "VALUES (?, 'approve', 1, datetime('now', '-3 days'))",
        (old,),
    )
    # a manual approval today does not use up the limit
    conn.execute("INSERT INTO decisions (job_id, decision, auto) VALUES (?, 'approve', 0)", (old,))
    conn.execute("INSERT INTO decisions (job_id, decision, auto) VALUES (?, 'approve', 1)", (old,))
    conn.commit()
    a = _queued(conn, "e1", 90)
    b = _queued(conn, "e2", 88)
    stats = run_standing_rules(conn)
    assert stats["auto_approved"] == 1 and stats["waiting_for_tomorrow"] == 1
    assert (_status(conn, a), _status(conn, b)) == ("approved", "queued")


def test_a_job_that_waited_yesterday_waits_again_today(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The waiting event is once per job per local day: yesterday's does not
    stand in for today's, so the digest's count for today is right."""
    _write_rules(tmp_path / "autoapprove.yaml", monkeypatch, {**BAR, "max_auto_approve_per_day": 1})
    a = _queued(conn, "y1", 95)
    b = _queued(conn, "y2", 90)
    conn.execute(
        "INSERT INTO events (job_id, kind, detail, at) "
        "VALUES (?, ?, 'x', datetime('now', '-1 day'))",
        (b, rules.WAITING_EVENT),
    )
    conn.commit()
    yesterday = conn.execute("SELECT date('now', 'localtime', '-1 day')").fetchone()[0]
    assert rules.waiting_for_tomorrow(conn, yesterday) == 1
    stats = run_standing_rules(conn)
    assert stats["waiting_for_tomorrow"] == 1
    assert (_status(conn, a), _status(conn, b)) == ("approved", "queued")
    n = conn.execute(
        "SELECT COUNT(*) FROM events WHERE job_id = ? AND kind = ?", (b, rules.WAITING_EVENT)
    ).fetchone()[0]
    assert n == 2
    assert rules.waiting_for_tomorrow(conn) == 1
    assert rules.waiting_for_tomorrow(conn, yesterday) == 1
    # another run today records nothing new
    run_standing_rules(conn)
    n2 = conn.execute(
        "SELECT COUNT(*) FROM events WHERE job_id = ? AND kind = ?", (b, rules.WAITING_EVENT)
    ).fetchone()[0]
    assert n2 == 2


def test_rejects_are_never_limited(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = {
        **BAR,
        "auto_reject": [{"when": {"score_lt": 50}, "reason": "weak"}],
        "max_auto_approve_per_day": 1,
    }
    _write_rules(tmp_path / "autoapprove.yaml", monkeypatch, cfg)
    weak = [_queued(conn, f"w{i}", 40) for i in range(3)]
    good = [_queued(conn, f"g{i}", 90 - i) for i in range(2)]
    stats = run_standing_rules(conn)
    assert stats["auto_rejected"] == 3
    assert [_status(conn, j) for j in weak] == ["rejected"] * 3
    assert [_status(conn, j) for j in good] == ["approved", "queued"]


@pytest.mark.parametrize("bad", [True, False, 0, -2, 2.5, "five", [3]])
def test_a_bad_limit_fails_loudly_before_any_change(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: object
) -> None:
    _write_rules(
        tmp_path / "autoapprove.yaml", monkeypatch, {**BAR, "max_auto_approve_per_day": bad}
    )
    jid = _queued(conn, "b1", 99)
    with pytest.raises(ValueError, match="max_auto_approve_per_day"):
        run_standing_rules(conn)
    assert _status(conn, jid) == "queued"
    assert rules.digest_lines(conn) == []  # the rules stage reports it, not the digest


def test_digest_shows_the_limit_only_when_set(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(digest, "DIGEST_DIR", tmp_path / "digest")
    _write_rules(tmp_path / "autoapprove.yaml", monkeypatch, BAR)
    _queued(conn, "d1", 90)
    run_standing_rules(conn)
    text = digest.write_digest(conn).read_text()
    assert "Automatic approval" not in text and "daily limit" not in text
    _write_rules(tmp_path / "autoapprove.yaml", monkeypatch, {**BAR, "max_auto_approve_per_day": 3})
    text = digest.write_digest(conn).read_text()
    assert "## Automatic approval\nApproved automatically today: 1 (your daily limit is 3)." in text
    assert "wait for tomorrow" not in text


def test_a_broken_rules_file_never_stops_the_digest(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A YAML syntax error in autoapprove.yaml: the rules stage reports it;
    the digest is still written, without the automatic-approval section."""
    monkeypatch.setattr(digest, "DIGEST_DIR", tmp_path / "digest")
    path = tmp_path / "autoapprove.yaml"
    path.write_text("auto_approve: [\n  - when: {score_gte: 85\n")
    monkeypatch.setattr(rules, "RULES_PATH", str(path))
    assert rules.digest_lines(conn) == []
    text = digest.write_digest(conn, date="2026-10-22").read_text()
    assert "Automatic approval" not in text


# ---- doctor ----------------------------------------------------------------


def _checks(jobkit_home: Path) -> dict[str, doctor.Check]:
    return {c.name: c for c in doctor.check_automation(doctor.current_env(jobkit_home))}


def test_doctor_reports_automation(jobkit_home: Path) -> None:
    checks = _checks(jobkit_home)
    # the test profile: two approve rules, no limit, forms not opened
    assert checks["automatic approval"].ok
    assert checks["automatic approval"].detail == "2 auto_approve rules"
    assert checks["open forms after the daily run"].detail.startswith("off")
    auto = jobkit_home / "config" / "autoapprove.yaml"
    auto.write_text(auto.read_text() + "\nmax_auto_approve_per_day: 8\n")
    assert "at most 8 a day" in _checks(jobkit_home)["automatic approval"].detail
    auto.write_text(
        auto.read_text().replace("max_auto_approve_per_day: 8", "max_auto_approve_per_day: yes")
    )
    bad = _checks(jobkit_home)["automatic approval"]
    assert not bad.ok and bad.required and "delete the line for no daily limit" in bad.fix
    filters = jobkit_home / "config" / "filters.yaml"
    filters.write_text(filters.read_text() + "\nstaging:\n  open_after_daily_run: true\n")
    assert _checks(jobkit_home)["open forms after the daily run"].detail.startswith("on")
    filters.write_text(
        filters.read_text().replace("open_after_daily_run: true", "open_after_daily_run: 2")
    )
    assert not _checks(jobkit_home)["open forms after the daily run"].ok


def test_doctor_says_off_with_no_rule(jobkit_home: Path) -> None:
    (jobkit_home / "config" / "autoapprove.yaml").write_text(
        'version: "1.0"\nauto_approve: []\nauto_reject: []\n'
    )
    assert _checks(jobkit_home)["automatic approval"].detail.startswith("off")


# ---- schedule status: the line the scheduled run reads ---------------------------


def _stage(conn: sqlite3.Connection, ext: str, status: str = "staged") -> int:
    jid = _insert(conn, external_id=ext)
    conn.execute(
        "INSERT INTO autofill_runs (job_id, status, filled_fields) VALUES (?, ?, '[\"name\"]')",
        (jid, status),
    )
    conn.commit()
    return jid


def test_open_forms_line(
    jobkit_home: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(schedule, "_platform", lambda: "win32")  # no launchctl
    _stage(conn, "s1")
    _stage(conn, "s2")
    # absent (the owner's config): off, and the scheduled run opens nothing
    assert schedule.open_forms_line(conn) == (
        "open forms after the daily run: off (staging.open_after_daily_run)"
    )
    filters = jobkit_home / "config" / "filters.yaml"
    filters.write_text(filters.read_text() + "\nstaging:\n  open_after_daily_run: true\n")
    assert schedule.open_forms_line(conn) == (
        "open forms after the daily run: on; 2 applications ready to open (open_queue)"
    )
    assert schedule.open_forms_line(None).endswith("on; no database, nothing to open")
    text, _ = schedule.status(conn)
    assert text.splitlines()[-1].startswith(schedule.OPEN_FORMS_PREFIX + " on; 2 ")
    filters.write_text(
        filters.read_text().replace("open_after_daily_run: true", "open_after_daily_run: maybe")
    )
    assert schedule.open_forms_line(conn).startswith("open forms after the daily run: off (")


def test_schedule_status_mcp_tool_carries_the_line(
    jobkit_home: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import mcp_server

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    monkeypatch.setattr(schedule, "_platform", lambda: "win32")
    assert "open forms after the daily run: off" in mcp_server.schedule_status()
