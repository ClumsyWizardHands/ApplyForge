"""Per-track pause switch. The test profile pauses the IC sdr_bdr track
(`active: false` in config/filters.yaml, the one owner), as a user who no
longer takes individual-contributor SDR roles would; SDR/BDR MANAGER roles
stay active.

A title whose FIRST matching track is paused is a filter-layer rejection,
`track_paused:<id>`. Classification is unchanged (the paused track still claims
its titles), refilter treats the reason as config-reversible, and setting the
track back to `active: true` + refilter restores the held postings.
Offline: the sandbox's private copy of config/, in-memory or tmp DBs."""

from __future__ import annotations

import copy
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from jobkit import db as q
from jobkit.config import Filters, load_filters
from jobkit.models import NormalizedJob
from jobkit.pipeline.feeds import ingest_feed_jobs
from jobkit.pipeline.filter import (
    _reject_reason,
    refilter,
    reject_reason_now,
    run_filters,
)
from jobkit.rules import _title_class
from tests.test_filter import _insert

SDR_IC_TITLES = [
    "Sales Development Representative",
    "Business Development Representative, Majors",
    "Account Development Representative",
    "Market Development Representative",
    "Senior BDR",
    "SDR",
    "BDR - Enterprise",
    "Sales Development Representative - AE Track",
    "BDR (Associate Account Executive path)",
]

SDR_MANAGER_TITLES = [
    "Manager, Sales Development",
    "SDR Manager",
    "BDR Manager",
    "SDR Team Lead",
    "Director of Sales Development",
    "Head of Business Development",
    "Senior Manager, Business Development",
]


def _row(title: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": title,
        "company_name": "Acme",
        "location_raw": "Remote, US",
        "location_bucket": "remote-us",
        "remote": 1,
        "comp_min": None,
        "comp_max": None,
        "comp_currency": None,
        "posted_at": None,
    }
    base.update(kw)
    return base


@pytest.fixture()
def cfg() -> Filters:
    """The real config/filters.yaml (the sandbox's private copy)."""
    return load_filters()


def _set_sdr_active(home: Path, active: bool) -> Filters:
    """Flip sdr_bdr's switch in the sandbox's filters.yaml as a user would
    (edit the one line), then reload it."""
    path = home / "config" / "filters.yaml"
    text = path.read_text()
    old, new = (
        ("- id: sdr_bdr\n      active: false", "- id: sdr_bdr\n      active: true")
        if active
        else ("- id: sdr_bdr\n      active: true", "- id: sdr_bdr\n      active: false")
    )
    assert text.count(old) == 1, "sdr_bdr's active: line moved; update this helper"
    path.write_text(text.replace(old, new))
    return load_filters()


def _latest_reason(conn: sqlite3.Connection, jid: int) -> str:
    row = conn.execute(
        "SELECT reason FROM rejections WHERE job_id=? ORDER BY id DESC LIMIT 1", (jid,)
    ).fetchone()
    return str(row[0])


def _status(conn: sqlite3.Connection, jid: int) -> str:
    return str(conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0])


# ---- the config ----


def test_sdr_bdr_is_the_only_paused_track(cfg: Filters) -> None:
    assert cfg.paused_tracks == ["sdr_bdr"]
    active = {t.id: t.active for t in cfg.tracks}
    assert active == {
        "sdr_bdr_manager": True,
        "sales_exec": True,
        "sdr_bdr": False,
        "account_executive": True,
    }
    ids = [t.id for t in cfg.tracks]
    assert ids.index("sdr_bdr_manager") < ids.index("sdr_bdr"), "managers route first"


def test_active_defaults_true_and_must_be_a_bool() -> None:
    f = Filters(raw={"filters": {"tracks": [{"id": "a", "title_regex": "(?i)alpha"}]}})
    assert f.tracks[0].active is True and f.paused_tracks == []
    with pytest.raises(ValidationError, match="active must be true or false"):
        Filters(raw={"filters": {"tracks": [{"id": "a", "title_regex": "a", "active": "no"}]}})


def test_hold_regex_is_rejected_at_load(cfg: Filters) -> None:
    """The release exception hold_regex guarded was declined: a config that
    still carries one fails loudly at load rather
    than looking like a knob that does something."""
    for hold in ("(?i)\\bsdr\\b", "", "."):
        with pytest.raises(ValidationError, match="hold_regex is no longer supported"):
            Filters(
                raw={"filters": {"tracks": [{"id": "a", "title_regex": "a", "hold_regex": hold}]}}
            )
    assert "hold_regex" not in json.dumps(cfg.raw), "the real config carries no hold_regex"
    with pytest.raises(ValidationError, match="bad title_regex"):
        Filters(raw={"filters": {"tracks": [{"id": "a", "title_regex": "("}]}})


# ---- the filter verdict ----


@pytest.mark.parametrize("title", SDR_IC_TITLES)
def test_sdr_bdr_ic_titles_rejected_as_track_paused(cfg: Filters, title: str) -> None:
    assert cfg.track_for(title) == "sdr_bdr"
    assert _reject_reason(_row(title), cfg) == "track_paused:sdr_bdr"


@pytest.mark.parametrize("title", SDR_MANAGER_TITLES)
def test_sdr_bdr_manager_titles_still_pass(cfg: Filters, title: str) -> None:
    assert cfg.track_for(title) == "sdr_bdr_manager", "manager titles route to the manager track"
    assert cfg.paused_track_for(title) is None
    assert _reject_reason(_row(title), cfg) is None


@pytest.mark.parametrize(
    "title",
    ["Enterprise Account Executive", "Mid-Market AE", "VP, Sales Development", "Deal Desk Manager"],
)
def test_other_tracks_unaffected(cfg: Filters, title: str) -> None:
    assert _reject_reason(_row(title), cfg) is None


# ---- no exception: an include never releases a paused title ----

# Each is classed sdr_bdr (the bare "business development" words). Some are
# roles the test profile targets through a non-track include_titles pattern
# (revenue or sales operations, deal desk, sales compensation) or as an AE.
# The rule has no exception: the title's FIRST matching track is paused, so it
# is held. These wrongful holds are accepted and listed in the README ("Known
# wrongful holds").
ACCEPTED_WRONGFUL_HOLDS = [
    "Business Development Executive",
    "Revenue Operations, Business Development",
    "Revenue Operations & Business Development",
    "Full-time Sales Ops & Business Development (Fintech) job",  # a feed-style title
    "Deal Desk Analyst, Business Development",
    "Sales Compensation Analyst, Business Development",
    "Account Executive, Business Development",
]


@pytest.mark.parametrize("title", ACCEPTED_WRONGFUL_HOLDS)
def test_an_include_never_releases_a_paused_title(cfg: Filters, title: str) -> None:
    assert cfg.track_for(title) == "sdr_bdr", "classification is unchanged"
    assert cfg.paused_track_for(title) == "sdr_bdr"
    assert _reject_reason(_row(title), cfg) == "track_paused:sdr_bdr"
    assert reject_reason_now(_row(title), cfg) == "track_paused:sdr_bdr"


def test_the_readme_lists_the_known_wrongful_holds() -> None:
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    section = readme[readme.index("Known wrongful holds") :]
    for title in ("Business Development Executive", "Revenue Operations, Business Development"):
        assert title in section, title


# ---- titles the pause must hold although an include matches them ----

# Track-pause review findings: the founding/first include accepts
# a bare "sales" or "gtm" word and the enablement include a "sales/gtm ...
# enablement" phrase, so under the declined release exception these IC SDR/BDR
# titles met an include and passed the filter while sdr_bdr was paused. Under
# the plain rule each is held: sdr_bdr is its first matching track.
HELD_DESPITE_GENERIC_INCLUDE = [
    "Founding Sales Team - SDR",
    "First Sales Hire - BDR",
    "BDR, Founding Sales Team",
    "SDR - GTM Enablement",
    "Sales Development Representative, Sales Enablement",
    "Founding Sales (SDR to Full-Cycle)",
    "Founding SDR / Sales",
    "SDR (Founding Sales Team)",
    "Founding BDR - Sales",
    "Business Development Representative - First Sales Hire",
    "First BDR at Acme (Sales)",
    "BDR, Sales Enablement",
    "Sales Enablement | SDR",
    "Business Development Associate, Founding Sales Team",
    # an AE/BDR hybrid is held like "Account Executive / BDR" (which the
    # paused track claims before the AE track): a founding AE title that is
    # also a BDR role is not released by the founding-AE include
    "Founding Account Executive / BDR",
]


@pytest.mark.parametrize("title", HELD_DESPITE_GENERIC_INCLUDE)
def test_ic_title_is_held_even_when_an_include_matches(cfg: Filters, title: str) -> None:
    assert cfg.track_for(title) == "sdr_bdr"
    assert any(re.search(p, title) for p in cfg.explicit_include_titles), (
        "the case under test: a non-track include matches the title"
    )
    assert cfg.paused_track_for(title) == "sdr_bdr"
    assert _reject_reason(_row(title), cfg) == "track_paused:sdr_bdr"
    assert reject_reason_now(_row(title), cfg) == "track_paused:sdr_bdr"


@pytest.mark.parametrize(
    "title",
    [
        # the founding-sales include matches these only through "Sales
        # Development": IC SDR roles, held
        "Founding Sales Development Representative",
        "First Sales Development Rep",
        "Founding BDR",
        "First Sales Development hire",
        # a later active TRACK never releases (unchanged): still an SDR role
        "Sales Development Representative - AE Track",
    ],
)
def test_include_met_through_paused_words_stays_held(cfg: Filters, title: str) -> None:
    assert _reject_reason(_row(title), cfg) == "track_paused:sdr_bdr"
    assert reject_reason_now(_row(title), cfg) == "track_paused:sdr_bdr"


def test_first_matching_paused_track_holds_whatever_else_matches() -> None:
    """The rule on a minimal config, independent of the real regexes: the
    first matching track decides; a later active track and a non-track
    include alike leave a paused title held."""
    includes = [r"(?i)\bfounding\b.{0,6}\b(sdr|ae)\b", r"(?i)\bdeal desk\b"]
    f = Filters(
        raw={
            "filters": {
                "tracks": [
                    {"id": "mgr", "title_regex": r"(?i)\bsdr manager\b"},
                    {"id": "ic", "title_regex": r"(?i)\bsdr\b", "active": False},
                    {"id": "ae", "title_regex": r"(?i)\bae\b"},
                ],
                "include_titles": includes,
            }
        }
    )
    assert f.explicit_include_titles == includes
    for title in (
        "SDR",
        "Founding SDR",
        "Founding AE / SDR",
        "Deal Desk Manager (SDR org)",
        "SDR, AE",
    ):
        assert f.paused_track_for(title) == "ic", title
    assert f.paused_track_for("SDR Manager") is None, "its first match is active"
    assert f.paused_track_for("Founding AE") is None
    assert f.paused_track_for("Deal Desk Manager") is None


# Senior business-development titles. "business development" is in the
# sdr_bdr regex; the sales_exec regex has no "business development" and the
# manager regex has no "vp", so these fall through to sdr_bdr and are held.
# The fix is a config change, the user's call (calibration proposes; the human
# disposes). Proposed, NOT applied, two items to accept or decline separately,
# both to the FIRST alternation of sales_exec's title_regex in filters.yaml:
#   1. add "business development" to the function words:
#      ...\b(sales|revenue|gtm|go.to.market|growth|business development)\b
#      -> VP / Vice President / Regional or Senior Vice President / Chief ...
#         Business Development become sales_exec (190K base floor).
#   2. add "s?vp|evp" to the leadership words: \b(director|head|s?vp|evp|...)
#      -> with item 1, the abbreviations "SVP Business Development" and "EVP
#         Business Development"; on its own, "SVP Sales" (today
#         no_include_title_match).
# Tradeoff while declined: these postings are held as track_paused:sdr_bdr,
# so a senior BD role is never surfaced. Before the pause they passed, but as
# sdr_bdr with the $75K-OTE SDR floor. "Director/Head of Business Development"
# is unaffected: the manager track takes it first, with or without the change.
# The xfails are strict: once an item is applied they XPASS and fail, as a
# reminder to drop the marker.
PROPOSED_SALES_EXEC_REGEX = (
    r"(?i)\b(director|head|s?vp|evp|vice president|chief)\b.{0,30}"
    r"\b(sales|revenue|gtm|go.to.market|growth|business development)\b"
    r"|\b(sales|revenue)\b.{0,20}\b(director|leader|vp|officer)\b"
    r"|\bchief (revenue|sales) officer\b"
)
_PENDING_1 = pytest.mark.xfail(
    strict=True,
    reason="user decision pending: proposal 1, 'business development' in the sales_exec regex",
)
_PENDING_2 = pytest.mark.xfail(
    strict=True,
    reason="user decision pending: needs proposals 1 AND 2 ('s?vp|evp') in the sales_exec regex",
)


@pytest.mark.parametrize(
    "title",
    [
        pytest.param("VP Business Development", marks=_PENDING_1),
        pytest.param("Vice President, Business Development", marks=_PENDING_1),
        pytest.param("Regional Vice President, Business Development", marks=_PENDING_1),
        pytest.param("Regional Vice President, Business Development EMEA", marks=_PENDING_1),
        pytest.param("Chief Business Development Officer", marks=_PENDING_1),
        pytest.param("SVP Business Development", marks=_PENDING_2),
        pytest.param("EVP Business Development", marks=_PENDING_2),
    ],
)
def test_senior_business_development_is_not_held(cfg: Filters, title: str) -> None:
    assert cfg.paused_track_for(title) is None
    assert _reject_reason(_row(title), cfg) is None


def test_proposed_sales_exec_regex_routes_senior_bd_and_nothing_else(cfg: Filters) -> None:
    """The proposal above, applied to a copy of the real config: senior BD
    becomes sales_exec; BD managers, IC BDRs and existing execs do not move."""
    raw = copy.deepcopy(cfg.raw)
    for t in raw["filters"]["tracks"]:
        if t["id"] == "sales_exec":
            t["title_regex"] = PROPOSED_SALES_EXEC_REGEX
    proposed = Filters(raw=raw)
    for title in (
        "VP Business Development",
        "Vice President, Business Development",
        "Regional Vice President, Business Development",
        "Senior Vice President of Business Development",
        "Chief Business Development Officer",
        "SVP Business Development",
        "EVP Business Development",
        "VP, Sales Development",
    ):
        assert proposed.track_for(title) == "sales_exec", title
        assert _reject_reason(_row(title), proposed) is None, title
    for title in SDR_MANAGER_TITLES:
        assert proposed.track_for(title) == "sdr_bdr_manager", title
    for title in SDR_IC_TITLES:
        assert _reject_reason(_row(title), proposed) == "track_paused:sdr_bdr", title
    for title in ("Enterprise Account Executive", "Mid-Market AE"):
        assert proposed.track_for(title) == "account_executive", title


def test_pause_decides_before_location_and_comp(cfg: Filters) -> None:
    """The pause is a title verdict: it comes after include/exclude and before
    location, comp and freshness, so the reason names the real cause."""
    title = "Sales Development Representative"
    assert (
        _reject_reason(_row(title, comp_max=40000, comp_currency="USD"), cfg)
        == "track_paused:sdr_bdr"
    )
    assert (
        _reject_reason(
            _row(title, remote=0, location_raw="Tulsa, OK", location_bucket="other-tulsa"), cfg
        )
        == "track_paused:sdr_bdr"
    )
    # an exclude still wins: an SDR internship is excluded, not paused
    excluded = _reject_reason(_row("SDR Intern"), cfg)
    assert excluded is not None and excluded.startswith("exclude_title:")


def test_classification_unchanged_for_the_paused_track(cfg: Filters) -> None:
    """Scoring, tailoring emphasis, comp floors and the standing-rules class
    read track_for / comp_floor_for: a paused track keeps all of them, so
    re-enabling needs no other edit, and an SDR title never becomes an AE."""
    title = "Sales Development Representative - AE Track"
    assert cfg.track_for(title) == "sdr_bdr"
    floor = cfg.comp_floor_for(title)
    assert (floor.track, floor.floor, floor.basis) == ("sdr_bdr", 75000, "ote")
    assert _title_class(title, cfg) == "sdr_bdr"


def test_rescore_never_revives_a_paused_title(cfg: Filters) -> None:
    """rescore puts a score-gate rejection back only if the filters pass it now."""
    assert reject_reason_now(_row("Senior BDR"), cfg) == "track_paused:sdr_bdr"
    assert reject_reason_now(_row("SDR Manager"), cfg) is None


# ---- the filter pass, refilter, and the switch ----


def test_run_filters_logs_track_paused(conn: sqlite3.Connection, cfg: Filters) -> None:
    sdr = _insert(conn, external_id="1", title="Sales Development Representative")
    mgr = _insert(conn, external_id="2", title="Manager, Sales Development")
    conn.commit()
    assert run_filters(conn, cfg) == {"examined": 2, "passed": 1, "rejected": 1}
    assert _status(conn, sdr) == "rejected" and _status(conn, mgr) == "filtered"
    rows = conn.execute("SELECT job_id, layer, reason FROM rejections").fetchall()
    assert [tuple(r) for r in rows] == [(sdr, "filter", "track_paused:sdr_bdr")]


def test_flipping_active_restores_held_postings(
    conn: sqlite3.Connection, jobkit_home: Path
) -> None:
    paused = load_filters()
    assert paused.paused_tracks == ["sdr_bdr"]
    ids = {
        "sdr": _insert(conn, external_id="1", title="Sales Development Representative"),
        "bdr": _insert(conn, external_id="2", title="Senior BDR"),
        "sdr_low": _insert(
            conn,
            external_id="3",
            title="Business Development Representative",
            comp_max=40000,
            comp_currency="USD",
        ),
        "sdr_human": _insert(conn, external_id="4", title="SDR"),
        "mgr": _insert(conn, external_id="5", title="SDR Manager"),
    }
    conn.commit()
    run_filters(conn, paused)
    for k in ("sdr", "bdr", "sdr_low", "sdr_human"):
        assert _latest_reason(conn, ids[k]) == "track_paused:sdr_bdr", k
    assert _status(conn, ids["mgr"]) == "filtered"
    # the human rejected one of them explicitly: a decision is never undone
    conn.execute(
        "INSERT INTO decisions (job_id, decision, reason, auto) VALUES (?, 'reject', 'no', 0)",
        (ids["sdr_human"],),
    )
    conn.commit()
    assert dict(conn.execute(q.PAUSED_TRACK_HOLDS).fetchall()) == {"track_paused:sdr_bdr": 3}

    # still paused: refilter sees the holds and leaves them alone
    held = refilter(conn, paused)
    assert held.candidates == 3 and held.by_reason == {"track_paused": 3}
    assert held.unchanged == 3 and held.reset == 0
    assert "paused tracks (filters.yaml active: false): sdr_bdr" in held.render()

    # the user sets active: true, then runs refilter
    resumed = _set_sdr_active(jobkit_home, True)
    assert resumed.paused_tracks == []
    dry = refilter(conn, resumed, dry_run=True)
    assert dry.reset == 3 and dry.would_pass == 2
    assert "paused tracks" not in dry.render()
    rep = refilter(conn, resumed)
    assert rep.reset == 3
    assert rep.filter_stats == {"examined": 3, "passed": 2, "rejected": 1}
    assert _status(conn, ids["sdr"]) == "filtered"
    assert _status(conn, ids["bdr"]) == "filtered"
    # back to the track's own rules: the SDR comp floor applies again
    assert _latest_reason(conn, ids["sdr_low"]).startswith("comp_below_floor:40000<45000:sdr_bdr")
    assert _status(conn, ids["sdr_human"]) == "rejected", "a human decision stands"
    details = [
        json.loads(r["detail"])
        for r in conn.execute("SELECT detail FROM events WHERE kind='refiltered'").fetchall()
    ]
    assert details == [{"previous_reason": "track_paused:sdr_bdr"}] * 3

    # paused again: a later refilter re-holds only what is still rejected
    repaused = _set_sdr_active(jobkit_home, False)
    again = refilter(conn, repaused)
    assert again.reset == 1  # sdr_low: comp_below_floor -> track_paused
    assert _latest_reason(conn, ids["sdr_low"]) == "track_paused:sdr_bdr"


def test_feed_ingest_stores_paused_titles(conn: sqlite3.Connection, cfg: Filters) -> None:
    """A feed posting on a paused track is stored (not dropped at ingest), so
    the filter pass holds it against a job row that refilter can restore."""
    jobs = [
        NormalizedJob(
            ats="remotive",
            board_token="",
            external_id=f"r-{i}",
            title=t,
            company_name=f"Co{i}",
            location_raw="Remote, US",
            remote=True,
            jd_html="x" * 300,
            apply_url=f"https://x.com/{i}",
            posted_at=None,
        )
        for i, t in enumerate(["Sales Development Representative", "SDR Manager"])
    ]
    stats = ingest_feed_jobs(conn, "remotive", jobs, cfg)
    assert stats == {"seen": 2, "rejected": 0, "merged": 0, "new": 2}
    assert run_filters(conn, cfg) == {"examined": 2, "passed": 1, "rejected": 1}
    held = conn.execute(
        "SELECT j.title, r.reason FROM rejections r JOIN jobs j ON j.id = r.job_id"
    ).fetchall()
    assert [tuple(r) for r in held] == [
        ("Sales Development Representative", "track_paused:sdr_bdr")
    ]


# ---- the surfaces: CLI refilter, MCP refilter + tune_filters ----


def test_paused_tracks_surface_in_cli_and_mcp(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import mcp_server
    from jobkit.cli import app

    _insert(conn, external_id="1", title="Sales Development Representative")
    conn.commit()
    run_filters(conn, load_filters())
    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    tuned = mcp_server.tune_filters()
    assert "- sdr_bdr: 1 open postings held as track_paused:sdr_bdr" in tuned
    assert "set its active: true, then refilter(confirm=true)" in tuned
    out = mcp_server.refilter()  # a dry run
    assert out.startswith("DRY RUN")
    assert "paused tracks (filters.yaml active: false): sdr_bdr" in out

    monkeypatch.setattr(q, "DB_PATH", Path(":memory:"))
    res = CliRunner().invoke(app, ["refilter", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "paused tracks (filters.yaml active: false): sdr_bdr" in res.output
