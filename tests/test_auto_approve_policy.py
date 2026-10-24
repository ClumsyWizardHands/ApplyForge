"""Which jobs the setup interview's clears-your-bar rule approves without
asking (owner decisions D3, D4 and D6, 2026-10-22/23).

* D3: only real deal-breakers block automatic approval: a doubt about remote
  work or the location (remote-conflict, remote-policy-unclear,
  hybrid-office-unknown, geo, office-days, onsite) and unrealistic pay
  (ote-unrealistic). A missing pay range, several roles in one posting, an
  unclear base-vs-OTE basis and a wide range do not. Rule condition
  `blocking_flags: none`; the lists are filters.yaml `auto_approve`.
* D4: a job whose remote/location verdict is unclear is never approved
  automatically (`location_known: true`).
* D6: a feed (aggregator) row with no employer posting found is never
  approved automatically (`employer_posting: true`); it stays in the queue
  marked "no employer form found: apply on the employer's site (found via
  <link>)", and the person can still
  approve it by hand.

A rule with `sanity_flags: none` (the owner's older rules) keeps its meaning:
any flag at all holds the job. Fictional companies only; offline."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from jobkit import mcp_server, rules
from jobkit.config import (
    AUTO_APPROVE_BLOCKING_FLAGS,
    AUTO_APPROVE_NON_BLOCKING_FLAGS,
    AutoApproveFlags,
    Filters,
    flag_class,
    load_filters,
    parse_auto_approve_flags,
)
from jobkit.discovery import reposters
from jobkit.onboarding import intake
from jobkit.pipeline.digest import write_digest
from tests.conftest import REPO_ROOT, SpawnRecorder
from tests.test_filter import _insert

FEED_LINK = "https://jobs.example-feed.test/listing/4471"

# one flag of each kind the sanity layer writes (pipeline/triage.py)
DEAL_BREAKERS = [
    "remote-conflict: says remote, the fine print asks for 4 office days",
    "remote-policy-unclear: the location names no office; confirm remote",
    "hybrid-office-unknown: confirm the office is within commute",
    "ote-unrealistic: $400k OTE on a $90k base",
    "geo-restriction: residents of one city only",
    "office-days: 4",
    "onsite: the job is in the office",
]
NOT_DEAL_BREAKERS = [
    "no-range-where-required",
    "role-sprawl: 3 jobs in one posting",
    "ote-basis-unclear: big number, base-vs-OTE not stated",
    "range-dodge: width 90%",
]


@pytest.fixture()
def interview_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """config/autoapprove.yaml as the setup interview writes it on a yes."""
    path = tmp_path / "autoapprove.yaml"
    path.write_text(intake.render_autoapprove(intake.AutoApproveAnswer(enabled=True)))
    monkeypatch.setattr(rules, "RULES_PATH", str(path))
    return path


def _board(conn: sqlite3.Connection) -> int:
    conn.execute(
        "INSERT INTO companies (id, name, normalized_name) VALUES (1, 'Acme', 'acme') "
        "ON CONFLICT DO NOTHING"
    )
    conn.execute(
        "INSERT OR IGNORE INTO boards (id, company_id, ats, token) VALUES (1, 1, 'greenhouse', 'a')"
    )
    return 1


def _job(
    conn: sqlite3.Connection,
    name: str,
    *,
    score: int = 88,
    flags: list[str] | None = None,
    stated: str | None = "remote",
    actual: str | None = "remote",
    employer: bool = True,
    sanity: bool = True,
    demand: int | None = 40,
) -> int:
    board = _board(conn) if employer else None
    jid = _insert(
        conn,
        external_id=name,
        board_id=board,
        apply_url=FEED_LINK if not employer else f"https://boards.example.test/acme/{name}",
    )
    conn.execute("UPDATE jobs SET status = 'queued' WHERE id = ?", (jid,))
    conn.execute("INSERT INTO scores (job_id, fit_score, gaps) VALUES (?, ?, '[]')", (jid, score))
    if sanity:
        conn.execute(
            """INSERT INTO sanity (job_id, flags, remote_stated, remote_actual, demand_score)
               VALUES (?, ?, ?, ?, ?)""",
            (jid, json.dumps(flags or []), stated, actual, demand),
        )
    conn.commit()
    return jid


def _status(conn: sqlite3.Connection, jid: int) -> str:
    return str(conn.execute("SELECT status FROM jobs WHERE id = ?", (jid,)).fetchone()[0])


# ---------------------------------------------------------------------------
# D3: only deal-breakers block
# ---------------------------------------------------------------------------


def test_default_flag_lists_are_the_owners_decision() -> None:
    cfg = AutoApproveFlags()
    for flag in DEAL_BREAKERS:
        assert cfg.blocks(flag), flag
    for flag in NOT_DEAL_BREAKERS:
        assert not cfg.blocks(flag), flag
    assert set(AUTO_APPROVE_BLOCKING_FLAGS) >= {
        "remote-conflict",
        "remote-policy-unclear",
        "hybrid-office-unknown",
        "geo",
        "office-days",
        "onsite",
        "ote-unrealistic",
    }
    assert set(AUTO_APPROVE_NON_BLOCKING_FLAGS) >= {
        "no-range-where-required",
        "role-sprawl",
        "ote-basis-unclear",
        "range-dodge",
    }
    assert flag_class("Remote-Conflict: x: y") == "remote-conflict"


def test_a_flag_nobody_classified_blocks() -> None:
    """A new kind of flag (another check, a typo) is never waved through."""
    cfg = AutoApproveFlags()
    assert cfg.blocks("repost-suspected: 'our partner is looking for'")
    assert cfg.blocks("remote conflict")  # not the class name
    assert cfg.blocking_in(["role-sprawl: 4", "mystery"]) == ["mystery"]


def test_underpriced_blocks_until_the_person_lists_it() -> None:
    """D3 named no rule for a low ceiling, so it is not waved through by default."""
    assert "underpriced" not in AUTO_APPROVE_NON_BLOCKING_FLAGS
    assert AutoApproveFlags().blocks("underpriced: demand 80 at ceiling $120,000")
    custom = parse_auto_approve_flags(
        {"non_blocking_flags": [*AUTO_APPROVE_NON_BLOCKING_FLAGS, "underpriced"]}
    )
    assert not custom.blocks("underpriced: demand 80 at ceiling $120,000")


def test_the_interview_rule_approves_past_harmless_flags_only(
    conn: sqlite3.Connection, interview_rule: Path
) -> None:
    harmless = _job(conn, "harmless", flags=NOT_DEAL_BREAKERS)
    clean = _job(conn, "clean")
    held = {
        f: _job(conn, f"held-{i}", flags=[f, "role-sprawl: 3"]) for i, f in enumerate(DEAL_BREAKERS)
    }
    below_bar = _job(conn, "below", score=60, flags=NOT_DEAL_BREAKERS)
    stats = rules.run_standing_rules(conn)
    assert _status(conn, harmless) == "approved"
    assert _status(conn, clean) == "approved"
    for flag, jid in held.items():
        assert _status(conn, jid) == "queued", flag
    assert _status(conn, below_bar) == "queued"
    assert stats["auto_approved"] == 2
    reason = conn.execute("SELECT reason, auto FROM decisions WHERE job_id = ?", (harmless,))
    assert tuple(reason.fetchone()) == ("clears-your-bar", 1)


def test_the_owners_sanity_flags_none_rule_keeps_its_meaning(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "owner.yaml"
    path.write_text(
        yaml.safe_dump(
            {"auto_approve": [{"when": {"score_gte": 75, "sanity_flags": "none"}, "reason": "r6"}]}
        )
    )
    monkeypatch.setattr(rules, "RULES_PATH", str(path))
    flagged = _job(conn, "no-range", flags=["no-range-where-required"])
    clean = _job(conn, "clean")
    rules.run_standing_rules(conn)
    assert _status(conn, flagged) == "queued"
    assert _status(conn, clean) == "approved"


def test_the_flag_lists_are_configurable_in_filters_yaml(
    conn: sqlite3.Connection, interview_rule: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = dict(load_filters().raw)
    raw["auto_approve"] = {"blocking_flags": ["remote-conflict", "role-sprawl"]}
    monkeypatch.setattr(rules, "load_filters", lambda: Filters(raw=raw))
    sprawl = _job(conn, "sprawl", flags=["role-sprawl: 3 jobs in one posting"])
    # ote-unrealistic left the blocking list; it is not in the non-blocking
    # list either, so it still blocks (a class no list names blocks)
    ote = _job(conn, "ote", flags=["ote-unrealistic: no"])
    no_range = _job(conn, "no-range", flags=["no-range-where-required"])
    rules.run_standing_rules(conn)
    assert _status(conn, sprawl) == "queued"
    assert _status(conn, ote) == "queued"
    assert _status(conn, no_range) == "approved"


def test_flag_list_settings_are_validated() -> None:
    assert parse_auto_approve_flags(None) == AutoApproveFlags()
    assert parse_auto_approve_flags({}) == AutoApproveFlags()
    custom = parse_auto_approve_flags({"non_blocking_flags": ["Remote-Conflict"]})
    # a class in both lists blocks (the blocking default still names it)
    assert custom.blocks("remote-conflict: x")
    with pytest.raises(ValueError, match="both"):
        parse_auto_approve_flags({"blocking_flags": ["geo"], "non_blocking_flags": ["geo"]})
    with pytest.raises(ValueError, match="list"):
        parse_auto_approve_flags({"blocking_flags": "geo"})
    with pytest.raises(ValueError, match="flag name"):
        parse_auto_approve_flags({"blocking_flags": ["remote conflict"]})
    with pytest.raises(ValueError, match="takes blocking_flags"):
        parse_auto_approve_flags({"blocking": ["geo"]})
    with pytest.raises(ValueError, match="mapping"):
        parse_auto_approve_flags(["geo"])
    # fails when filters.yaml loads, not mid-run
    with pytest.raises(ValueError, match="auto_approve"):
        Filters(raw={"auto_approve": {"blocking_flags": [3]}})


@pytest.mark.parametrize("name", ["filters.example.yaml", "filters.sales.example.yaml"])
def test_the_templates_carry_the_default_lists(name: str) -> None:
    cfg = load_filters(REPO_ROOT / "config" / name).auto_approve_flags
    assert set(cfg.blocking) == set(AUTO_APPROVE_BLOCKING_FLAGS)
    assert set(cfg.non_blocking) == set(AUTO_APPROVE_NON_BLOCKING_FLAGS)


# ---------------------------------------------------------------------------
# D4: an unclear location is never approved automatically
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stated", "actual"),
    [
        ("unclear", "unclear"),
        ("remote", "unclear"),
        ("unclear", "hybrid"),
        (None, "remote"),
        ("remote", None),
    ],
)
def test_an_unclear_location_is_held(
    conn: sqlite3.Connection, interview_rule: Path, stated: str | None, actual: str | None
) -> None:
    jid = _job(conn, "unclear", stated=stated, actual=actual)
    rules.run_standing_rules(conn)
    assert _status(conn, jid) == "queued"


def test_no_remote_check_means_no_location_verdict(
    conn: sqlite3.Connection, interview_rule: Path
) -> None:
    unchecked = _job(conn, "unchecked", sanity=False)
    hybrid = _job(conn, "hybrid", stated="hybrid", actual="hybrid")
    rules.run_standing_rules(conn)
    assert _status(conn, unchecked) == "queued"
    assert _status(conn, hybrid) == "approved"


def test_location_known_reads_both_verdicts() -> None:
    base: dict[str, Any] = {
        "sanity_job_id": 1,
        "remote_stated": "onsite",
        "remote_actual": "hybrid",
    }
    assert rules.location_known(base)
    assert not rules.location_known({**base, "sanity_job_id": None})
    assert not rules.location_known({**base, "remote_actual": "unclear"})
    assert not rules.location_known({**base, "remote_stated": "unclear"})
    assert rules._cond_matches({"location_known": False}, {**base, "remote_actual": "unclear"})


# ---------------------------------------------------------------------------
# D6: a feed job with no employer posting is the person's call
# ---------------------------------------------------------------------------


def test_a_feed_job_without_an_employer_posting_is_not_approved_automatically(
    conn: sqlite3.Connection, interview_rule: Path
) -> None:
    feed = _job(conn, "feed", employer=False)
    employer = _job(conn, "employer")
    rules.run_standing_rules(conn)
    assert _status(conn, feed) == "queued"
    assert _status(conn, employer) == "approved"


# rules without location_known/employer_posting, shaped like an owner's older
# ones (review 2026-10-23): D4 and D6 say "never", so they hold whatever a
# rule says
RULES_WITHOUT_THE_CONDITIONS: dict[str, Any] = {
    "auto_approve": [
        {
            "when": {"tier": 1, "score_gte": 85, "sanity_flags": "none", "friction_lte": 60},
            "reason": "tier1-strong-clean-cheap",
        },
        {
            "when": {"score_gte": 92, "remote": True, "comp_min_gte": 180000},
            "reason": "exceptional-remote-comp",
        },
    ],
    "auto_reject": [{"when": {"score_lt": 40}, "reason": "far-below-bar"}],
}


def test_no_rule_approves_an_unclear_location_or_a_feed_row(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "autoapprove.yaml"
    path.write_text(yaml.safe_dump(RULES_WITHOUT_THE_CONDITIONS))
    monkeypatch.setattr(rules, "RULES_PATH", str(path))
    conn.execute(
        "INSERT INTO companies (id, name, normalized_name, tier) VALUES (1, 'Acme', 'acme', 1) "
        "ON CONFLICT(id) DO UPDATE SET tier = 1"
    )
    jobs = {
        "feed-unclear": _job(
            conn, "f1", score=95, stated="unclear", actual="unclear", employer=False
        ),
        "feed-clear": _job(conn, "f2", score=95, employer=False),
        "employer-unclear": _job(conn, "e1", score=95, stated="unclear", actual="unclear"),
        "employer-unchecked": _job(conn, "e2", score=95, sanity=False),
        "employer-clear": _job(conn, "e3", score=95),
        "feed-far-below": _job(conn, "f3", score=30, employer=False, stated="unclear"),
    }
    for jid in jobs.values():
        conn.execute(
            "UPDATE jobs SET remote = 1, comp_min = 200000, comp_max = 230000 WHERE id = ?",
            (jid,),
        )
        conn.execute("INSERT INTO form_specs (job_id, friction_score) VALUES (?, 20)", (jid,))
    conn.commit()
    # the second rule alone would match the feed row (rules._cond_matches)
    feed = {
        "fit_score": 93,
        "remote": 1,
        "comp_min": 200000,
        "comp_basis": "unknown",
        "board_id": None,
        "sanity_job_id": 1,
        "remote_stated": "unclear",
        "remote_actual": "unclear",
        "flags": [],
    }
    assert rules._cond_matches(RULES_WITHOUT_THE_CONDITIONS["auto_approve"][1]["when"], feed)
    stats = rules.run_standing_rules(conn)
    assert {name: _status(conn, jid) for name, jid in jobs.items()} == {
        "feed-unclear": "queued",
        "feed-clear": "queued",
        "employer-unclear": "queued",
        "employer-unchecked": "queued",
        "employer-clear": "approved",
        "feed-far-below": "rejected",  # an auto_reject rule still applies
    }
    assert stats == {"evaluated": 6, "auto_approved": 1, "auto_rejected": 1}


@pytest.mark.parametrize(
    "when",
    [
        {},  # a catch-all rule
        {"score_gte": 0},
        {"any_of": [{"score_gte": 0}, {"tier": 1}]},
        # a rule that asks for the very thing D4/D6 forbid still cannot approve it
        {"location_known": False},
        {"employer_posting": False},
        {"location_known": False, "employer_posting": False},
    ],
)
def test_the_location_and_employer_gate_is_global(
    conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    when: dict[str, Any],
) -> None:
    # location_known and employer_posting hold on every auto_approve rule,
    # whatever its own conditions (review 2026-10-23)
    path = tmp_path / "autoapprove.yaml"
    path.write_text(yaml.safe_dump({"auto_approve": [{"when": when, "reason": "any"}]}))
    monkeypatch.setattr(rules, "RULES_PATH", str(path))
    held = {
        "feed": _job(conn, "g1", score=95, employer=False),
        "unclear-stated": _job(conn, "g2", score=95, stated="unclear"),
        "unclear-actual": _job(conn, "g3", score=95, actual="unclear"),
        "no-verdict": _job(conn, "g4", score=95, stated=None, actual=None),
        "unchecked": _job(conn, "g5", score=95, sanity=False),
        "feed-unclear": _job(conn, "g6", score=95, employer=False, stated="unclear"),
    }
    clear = _job(conn, "g7", score=95)
    stats = rules.run_standing_rules(conn)
    assert {name: _status(conn, jid) for name, jid in held.items()} == dict.fromkeys(held, "queued")
    approved = _status(conn, clear) == "approved"
    # a rule that asks for an unclear location or a feed row matches no clear
    # employer job either, so nothing is approved under it
    assert approved is not any(k in when for k in ("location_known", "employer_posting"))
    assert stats["auto_approved"] == int(approved)


def test_a_queued_job_on_a_board_kept_out_as_a_repost_site_is_not_approved(
    conn: sqlite3.Connection, interview_rule: Path
) -> None:
    # review 2026-10-23: the wording check trips on a later poll, after a job
    # of the board was already scored and queued
    queued = _job(conn, "queued-on-reposter")
    approved = _job(conn, "already-approved")
    conn.execute("UPDATE jobs SET status = 'approved' WHERE id = ?", (approved,))
    conn.commit()
    rejected = reposters.keep_out(
        conn, ats="greenhouse", token="a", company="Acme", why="wording", where="poll", board_id=1
    )
    assert rejected == 1  # the queued job: nobody had decided it
    assert _status(conn, queued) == "rejected"
    assert _status(conn, approved) == "approved"  # a decision stays
    reason = conn.execute("SELECT reason FROM rejections WHERE job_id = ?", (queued,))
    assert reason.fetchone()[0] == "reposter:Acme"
    rules.run_standing_rules(conn)
    assert _status(conn, queued) == "rejected"


def test_a_job_on_a_stale_board_is_not_an_employer_posting(
    conn: sqlite3.Connection, interview_rule: Path
) -> None:
    # fail closed: a board no longer polled (gone, or kept out) approves nothing
    jid = _job(conn, "on-stale-board")
    conn.execute("UPDATE boards SET status = 'stale' WHERE id = 1")
    conn.commit()
    rules.run_standing_rules(conn)
    assert _status(conn, jid) == "queued"


@pytest.mark.parametrize(
    ("company", "ats", "token"),
    [("Jobgether", "greenhouse", "a"), ("Acme", "lever", "jobgether")],
)
def test_a_listed_repost_site_is_not_an_employer_posting(
    conn: sqlite3.Connection, interview_rule: Path, company: str, ats: str, token: str
) -> None:
    # an install whose queued jobs come from a site newly added to the list
    jid = _job(conn, "listed")
    conn.execute("UPDATE jobs SET company_name = ? WHERE id = ?", (company, jid))
    conn.execute("UPDATE boards SET ats = ?, token = ? WHERE id = 1", (ats, token))
    conn.commit()
    rules.run_standing_rules(conn)
    assert _status(conn, jid) == "queued"
    assert not rules.is_employer_posting(
        {"board_id": 1, "company_name": company, "board_ats": ats, "board_token": token}
    )


def test_the_queue_and_digest_say_apply_on_the_employers_site(
    conn: sqlite3.Connection,
    interview_rule: Path,
    monkeypatch: pytest.MonkeyPatch,
    spawned: SpawnRecorder,
) -> None:
    conn.executescript(mcp_server.PINS_SCHEMA)
    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    feed = _job(conn, "feed", employer=False)
    held = _job(conn, "held", flags=["remote-conflict: x"])  # an employer posting
    queue = mcp_server.get_queue()
    assert f"apply on the employer's site (found via {FEED_LINK})" in queue
    assert queue.count("apply on the employer's site") == 1
    digest = write_digest(conn).read_text()
    assert f"apply on the employer's site (found via {FEED_LINK})" in digest
    assert digest.count("apply on the employer's site (found via") == 1
    # the person can still approve it by hand
    pins = dict(conn.execute("SELECT job_id, n FROM queue_pins").fetchall())
    mcp_server.approve(str(pins[feed]))
    assert _status(conn, feed) == "approved"
    assert _status(conn, held) == "queued"
    decision = conn.execute("SELECT reason, auto FROM decisions WHERE job_id = ?", (feed,))
    assert tuple(decision.fetchone()) == ("human", 0)


def test_employer_site_note() -> None:
    assert rules.employer_site_note(4, FEED_LINK) == ""
    # the link is the feed's page, never called the employer's site
    assert rules.employer_site_note(None, FEED_LINK) == (
        f" · no employer form found: apply on the employer's site (found via {FEED_LINK})"
    )
    assert rules.employer_site_note(None, "") == (
        " · no employer form found: apply on the employer's site"
    )


def test_employer_site_note_drops_tracking_parameters() -> None:
    # SPEC §16.6: no utm_* or click id in a link shown to the person
    link = (
        "https://www.example-feed.test/land/ad/4471?se=abc&utm_medium=api"
        "&utm_source=app-1234&gclid=xyz#top"
    )
    assert rules.employer_site_note(None, link) == (
        " · no employer form found: apply on the employer's site "
        "(found via https://www.example-feed.test/land/ad/4471?se=abc)"
    )


# ---------------------------------------------------------------------------
# the new condition keys take only what they document
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "when",
    [
        {"blocking_flags": "some"},
        {"blocking_flags": True},
        {"location_known": "yes"},
        {"employer_posting": 1},
    ],
)
def test_a_bad_condition_value_fails_loudly(when: dict[str, Any]) -> None:
    job = {"flags": [], "sanity_job_id": 1, "remote_stated": "remote", "remote_actual": "remote"}
    with pytest.raises(ValueError, match="standing-rule key"):
        rules._cond_matches(when, {**job, "board_id": 1, "blocking_flags": []})


def test_the_interview_rule_is_what_setup_writes() -> None:
    assert intake.interview_rule(80)["when"] == {
        "score_gte": 80,
        "sanity_checked": True,
        "blocking_flags": "none",
        "location_known": True,
        "employer_posting": True,
    }
    text = intake.render_autoapprove(intake.AutoApproveAnswer(enabled=True, min_score=80))
    assert yaml.safe_load(text)["auto_approve"] == [intake.interview_rule(80)]
