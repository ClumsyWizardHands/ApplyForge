"""Retarget 2026-10-08 (audit §2 item 5, §4): role tracks + per-track comp
floors, geo fixes, Greenhouse/Lever pay parsing, feed config, `refilter`.
Offline: recorded fixtures, in-memory DBs, the LLM boundary faked."""

from __future__ import annotations

import asyncio
import copy
import html
import json
import re
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from jobkit import db as q
from jobkit import llm
from jobkit import rules as rules_mod
from jobkit.adapters import ADAPTERS
from jobkit.adapters.ats.greenhouse import GreenhouseAdapter, comp_basis, pay_range
from jobkit.adapters.feeds.adzuna import AdzunaFeed
from jobkit.config import Filters, load_filters
from jobkit.normalize import location_bucket
from jobkit.pipeline import triage
from jobkit.pipeline.feeds import _spec_for
from jobkit.pipeline.filter import (
    _comp_reason,
    _reject_reason,
    defers_to_remote_check,
    refilter,
    run_filters,
)
from jobkit.pipeline.triage import geo_restriction_acceptable, run_remote_check
from jobkit.rules import _title_class, run_standing_rules
from tests.conftest import load_fixture
from tests.test_filter import _insert
from tests.test_rules import _queued


@pytest.fixture()
def cfg() -> Filters:
    """The real config/filters.yaml (the sandbox's private copy)."""
    return load_filters()


@pytest.fixture()
def cfg_sdr_active(cfg: Filters) -> Filters:
    """The test profile with the sdr_bdr track re-enabled (the profile pauses
    it, tests/test_track_pause.py). The SDR floor and refilter tests below
    cover the track as it runs whenever a user sets active: true."""
    raw = copy.deepcopy(cfg.raw)
    for t in raw["filters"]["tracks"]:
        if t["id"] == "sdr_bdr":
            t["active"] = True
    out = Filters(raw=raw)
    assert out.paused_tracks == [t for t in cfg.paused_tracks if t != "sdr_bdr"]
    return out


@pytest.fixture()
def mem() -> sqlite3.Connection:
    c = q.connect(Path(":memory:"))
    q.init_db(c)
    return c


def _verdict(title: str, cfg: Filters, **row: Any) -> str | None:
    base: dict[str, Any] = {
        "title": title,
        "company_name": "Acme",
        "location_raw": "Remote, US",
        "location_bucket": "remote-us",
        "remote": 1,
        "comp_max": None,
        "comp_currency": None,
        "posted_at": None,
    }
    base.update(row)
    return _reject_reason(base, cfg)


# ---- role tracks ----


@pytest.mark.parametrize(
    ("title", "track"),
    [
        ("Sales Development Representative", "sdr_bdr"),
        ("Business Development Representative, Majors", "sdr_bdr"),
        ("Account Development Representative", "sdr_bdr"),
        ("Senior BDR", "sdr_bdr"),
        ("Manager, Sales Development", "sdr_bdr_manager"),
        ("SDR Team Lead", "sdr_bdr_manager"),
        ("Director of Sales Development", "sdr_bdr_manager"),
        ("Head of Business Development", "sdr_bdr_manager"),
        ("VP, Sales Development", "sales_exec"),  # exec, not an IC SDR
        ("Regional Director, Strategic Sales", "sales_exec"),
        ("Head of GTM Strategy & Operations", "sales_exec"),
        ("Chief Revenue Officer", "sales_exec"),
        ("Enterprise Sales Director", "sales_exec"),
        ("Enterprise Account Executive", "account_executive"),
        ("Mid-Market AE", "account_executive"),
        ("Senior Sales Executive", "account_executive"),
        ("Deal Desk Manager", None),
        ("CRO, Conversion Rate Optimization", None),  # bare CRO is not a track
    ],
)
def test_track_for(cfg: Filters, title: str, track: str | None) -> None:
    assert cfg.track_for(title) == track


def test_tracks_defined_once_and_ordered(cfg: Filters) -> None:
    ids = [t.id for t in cfg.tracks]
    assert ids.index("sdr_bdr_manager") < ids.index("sdr_bdr"), "manager before IC"
    assert ids.index("sales_exec") < ids.index("sdr_bdr"), "exec before IC"
    raw_includes = cfg.raw["filters"]["include_titles"]
    for t in cfg.tracks:
        assert t.title_regex not in raw_includes, f"{t.id} regex copied into include_titles"
        assert t.title_regex in cfg.include_titles, f"{t.id} regex must be an include"
    assert set(cfg.raw["filters"]["comp"]["track_floors"]) <= set(ids)


@pytest.mark.parametrize(
    "title",
    [
        # IC SDR/BDR is paused in the test profile: rejected as track_paused,
        # see tests/test_track_pause.py; SDR/BDR managers stay included
        "SDR Manager",
        "Manager, Sales Development",
        "Account Executive, API",
        "Deal Desk Manager",
        "Revenue Operations Manager (Process)",
        "Sales Ops Analyst",
        "Channel Sales Manager, Resellers",
        "Partner Programs Lead",
        "Sales Compensation Analyst",
        "Senior Director, Revenue Enablement",
        "Sales Enablement Manager",
        "Sales Manager",
    ],
)
def test_included_titles(cfg: Filters, title: str) -> None:
    assert _verdict(title, cfg) is None


@pytest.mark.parametrize(
    "title",
    [
        "Solutions Engineer",
        "Forward Deployed Engineer",
        "Sales Engineer",
        "Engineering Manager, Sales Systems",
        "Director of Forward Deployed Engineering",
    ],
)
def test_engineer_titles_excluded(cfg: Filters, title: str) -> None:
    reason = _verdict(title, cfg)
    assert reason is not None and reason.startswith("exclude_title:(?i)\\bengineer")


def test_conversion_rate_optimization_not_included(cfg: Filters) -> None:
    assert _verdict("CRO Manager (Conversion Rate Optimization)", cfg) == "no_include_title_match"


# ---- per-track comp floors ----


@pytest.mark.parametrize(
    ("title", "comp_max", "passes"),
    [
        # sdr_bdr: 75K OTE basis -> a base of 75K x 0.6 = 45K passes
        ("Sales Development Representative", 45000, True),
        ("Sales Development Representative", 90000, True),
        ("Sales Development Representative", 42000, False),
        # sdr_bdr_manager: 110K, base basis
        ("Manager, Sales Development", 130000, True),
        ("Manager, Sales Development", 105000, False),
        # account_executive: 140K OTE basis -> 84K base passes
        ("Enterprise Account Executive", 84000, True),
        ("Enterprise Account Executive", 82000, False),
        # sales_exec: 190K
        ("Director of Sales", 190000, True),
        ("Director of Sales", 180000, False),
        # no track: the default floor (165K)
        ("Deal Desk Manager", 165000, True),
        ("Deal Desk Manager", 155000, False),
    ],
)
def test_comp_floor_per_track(
    cfg_sdr_active: Filters, title: str, comp_max: int, passes: bool
) -> None:
    reason = _verdict(title, cfg_sdr_active, comp_max=comp_max, comp_currency="USD")
    if passes:
        assert reason is None
    else:
        assert reason is not None and reason.startswith(f"comp_below_floor:{comp_max}<")


def test_comp_floor_reason_names_track(cfg: Filters, cfg_sdr_active: Filters) -> None:
    reason = _verdict("Sales Development Representative", cfg_sdr_active, comp_max=42000)
    assert reason == "comp_below_floor:42000<45000:sdr_bdr(ote)"
    # paused, the track decides before comp does
    reason = _verdict("Sales Development Representative", cfg, comp_max=42000)
    assert reason == "track_paused:sdr_bdr"
    reason = _verdict("Deal Desk Manager", cfg, comp_max=100000)
    assert reason == "comp_below_floor:100000<165000:default"


def test_comp_floor_skips_other_currencies_and_unpublished(cfg: Filters) -> None:
    assert _verdict("Deal Desk Manager", cfg, comp_max=100000, comp_currency="CAD") is None
    assert _verdict("Deal Desk Manager", cfg, comp_max=None) is None


def test_lowest_comp_floor(cfg: Filters) -> None:
    # the SDR OTE floor applied the way the filter applies it: 75,000 x 0.6.
    # sdr_bdr is paused but still counts: its postings are fetched and held as
    # track_paused so a re-enable + refilter can restore them
    assert cfg.paused_tracks == ["sdr_bdr"]
    assert cfg.lowest_comp_floor == 45000
    assert Filters(raw={"filters": {"comp": {"floor": 128000}}}).lowest_comp_floor == 128000
    assert Filters(raw={}).lowest_comp_floor == 0


@pytest.mark.parametrize(
    ("comp", "tracks", "err"),
    [
        (
            {"track_floors": {"nope": {"floor": 1}}},
            [{"id": "a", "title_regex": "a"}],
            "not a track",
        ),
        ({"ote_base_share": 1.5}, [], "ote_base_share"),
        (
            {"track_floors": {"a": {"floor": 1, "basis": "gross"}}},
            [{"id": "a", "title_regex": "a"}],
            "basis",
        ),
        ({}, [{"id": "a", "title_regex": "a"}, {"id": "a", "title_regex": "b"}], "duplicate"),
        ({}, [{"id": "a", "title_regex": "(unclosed"}], "bad title_regex"),
    ],
)
def test_bad_track_config_fails_at_load(
    comp: dict[str, Any], tracks: list[dict[str, str]], err: str
) -> None:
    with pytest.raises(ValidationError, match=err):
        Filters(raw={"filters": {"tracks": tracks, "comp": comp}})


# ---- location ----


@pytest.mark.parametrize(
    ("loc", "bucket"),
    [
        ("Remote - Austria", "remote-intl"),  # was remote-us: "us" inside "austria"
        ("Remote (Netherlands)", "remote-intl"),
        ("Remote, Latin America", "remote-intl"),
        ("Remote - Canada", "remote-intl"),
        ("Remote, Canada; Remote, United States", "remote-us"),  # a US resident qualifies
        ("Remote - New Mexico", "remote-us"),
        ("Remote - Indiana", "remote-us"),
        ("Remote, U.S.", "remote-us"),
        ("Remote", "remote-us"),
        ("Remote - Worldwide", "remote-global"),
    ],
)
def test_remote_location_bucket(loc: str, bucket: str) -> None:
    assert location_bucket(loc, None) == bucket


def test_remote_intl_rejected_even_when_remote_flag_set(cfg: Filters) -> None:
    reason = _verdict(
        "Deal Desk Manager",
        cfg,
        remote=1,
        location_raw="Remote - Austria",
        location_bucket="remote-intl",
    )
    assert reason == "location:remote-intl"


@pytest.mark.parametrize(
    ("loc", "deferred"),
    [
        ("United States", True),
        ("US / Canada", True),
        ("", True),
        ("Distributed", True),
        ("USA - West", True),
        ("United States (Hybrid)", True),
        ("Tulsa, OK", False),
        # a US city or state is a place: still a location rejection (review 2026-10-08)
        ("Tulsa, Oklahoma", False),
        ("Tulsa, OK, United States", False),
        ("San Mateo, CA United States", False),
        ("Lehi, Utah", False),
        ("Sacramento, California", False),
        ("West Palm Beach, Florida", False),
        ("California", False),
        ("Canada", False),
        ("中央区, Tokyo Prefecture, Japan", False),  # buckets other-unknown, names Japan
    ],
)
def test_us_wide_and_blank_locations_go_to_remote_check(
    cfg: Filters, loc: str, deferred: bool
) -> None:
    bucket = location_bucket(loc, None)
    assert defers_to_remote_check(bucket, loc) is deferred
    reason = _verdict(
        "Deal Desk Manager", cfg, remote=None, location_raw=loc, location_bucket=bucket
    )
    assert (reason is None) is deferred
    if not deferred:
        assert reason == f"location:{bucket}"


@pytest.mark.parametrize(
    "loc",
    [
        "Phoenix, Arizona",
        "Pittsburgh, PA",
        "Portland, Oregon",
        "Columbus, Ohio",
        "Charlotte, North Carolina",
        "Tulsa, Oklahoma",
        "San Mateo, CA United States",
    ],
)
def test_non_metro_us_city_rejected_by_filter(cfg: Filters, loc: str) -> None:
    """remote=0 in a named US city outside the accepted metros: rejected on
    location, not sent to the LLM (which let hybrid ones reach the queue)."""
    bucket = location_bucket(loc, None)
    assert bucket.startswith("other-")
    reason = _verdict(
        "Manager, Sales Development",
        cfg,
        remote=0,
        location_raw=loc,
        location_bucket=bucket,
    )
    assert reason == f"location:{bucket}"


@pytest.mark.parametrize(
    ("geo", "ok"),
    [
        ("US only", True),
        ("U.S only", True),
        ("United States", True),
        ("EST hours", True),
        ("Must be based in Pennsylvania", True),
        ("Austria", False),  # substring "us"
        ("Netherlands", False),  # substring "et"
        ("West Europe", False),  # substring "est"
        ("Egypt", False),  # substring "pt"
        ("Canada", False),
        ("EMEA", False),
    ],
)
def test_geo_restriction_whole_words(cfg: Filters, geo: str, ok: bool) -> None:
    # the configured entries plus the candidate's own places (Pennsylvania is
    # allowed)
    assert geo_restriction_acceptable(geo, cfg.acceptable_geo_restrictions) is ok


def _remote_answer(**kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "remote_stated": "remote",
        "remote_actual": "remote",
        "remote_conflict": False,
        "geo_restriction": None,
        "office_days_required": None,
        "evidence": "",
    }
    out.update(kw)
    return out


@pytest.fixture()
def fake_remote_llm(monkeypatch: pytest.MonkeyPatch, cfg: Filters) -> dict[str, dict[str, Any]]:
    """Answers keyed by the job title appearing in the rendered prompt."""
    answers: dict[str, dict[str, Any]] = {}

    def backend(stage: str, prompt: str) -> tuple[str, dict[str, Any]]:
        for title, answer in answers.items():
            if title in prompt:
                return json.dumps(answer), {}
        raise AssertionError("no canned answer for prompt")

    monkeypatch.setattr(llm, "_call_claude_cli", backend)
    monkeypatch.setattr(triage, "load_filters", lambda: cfg)
    return answers


JD = "<p>" + ("We are hiring for a remote revenue role in the United States. " * 12) + "</p>"


def test_remote_check_geo_and_unknown_office(
    conn: sqlite3.Connection, fake_remote_llm: dict[str, dict[str, Any]]
) -> None:
    fake_remote_llm["Deal Desk Manager Austria"] = _remote_answer(geo_restriction="Austria")
    fake_remote_llm["Deal Desk Manager Eastern"] = _remote_answer(geo_restriction="EST hours")
    fake_remote_llm["Deal Desk Manager Hybrid"] = _remote_answer(
        remote_stated="hybrid", remote_actual="hybrid", office_days_required=2
    )
    ids = {}
    for i, (title, loc, bucket) in enumerate(
        [
            ("Deal Desk Manager Austria", "Remote", "remote-us"),
            ("Deal Desk Manager Eastern", "Remote", "remote-us"),
            ("Deal Desk Manager Hybrid", "United States", "other-unitedstates"),
        ]
    ):
        jid = _insert(
            conn,
            external_id=f"g{i}",
            title=title,
            location_raw=loc,
            location_bucket=bucket,
            remote=None,
            jd_html=JD,
            jd_hash=f"h{i}",
        )
        conn.execute("UPDATE jobs SET status='filtered' WHERE id=?", (jid,))
        ids[title] = jid
    conn.commit()
    run_remote_check(conn)

    def status(t: str) -> str:
        return str(conn.execute("SELECT status FROM jobs WHERE id=?", (ids[t],)).fetchone()[0])

    assert status("Deal Desk Manager Austria") == "rejected"
    reason = conn.execute(
        "SELECT reason FROM rejections WHERE job_id=?", (ids["Deal Desk Manager Austria"],)
    ).fetchone()["reason"]
    assert reason == "sanity:geo:Austria"
    assert status("Deal Desk Manager Eastern") == "filtered"
    assert status("Deal Desk Manager Hybrid") == "filtered"
    flags = json.loads(
        conn.execute(
            "SELECT flags FROM sanity WHERE job_id=?", (ids["Deal Desk Manager Hybrid"],)
        ).fetchone()["flags"]
    )
    assert any(f.startswith("hybrid-office-unknown") for f in flags)


def test_remote_check_hybrid_office_outside_commute_rejected(
    conn: sqlite3.Connection, fake_remote_llm: dict[str, dict[str, Any]]
) -> None:
    """Review 2026-10-08: a hybrid role whose office is a named city with no
    commute-table entry is rejected, not passed with a flag; an unclear policy
    on a location that names no office is flagged."""
    hybrid = _remote_answer(remote_actual="hybrid", remote_conflict=True, office_days_required=2)
    fake_remote_llm["Deal Desk Manager Tulsa"] = hybrid
    fake_remote_llm["Deal Desk Manager Chicago"] = hybrid
    fake_remote_llm["Deal Desk Manager Unclear"] = _remote_answer(
        remote_stated="unclear", remote_actual="unclear"
    )
    ids = {}
    for i, (title, loc, bucket, remote) in enumerate(
        [
            # ATS flagged it remote (so the filter passed it), the JD says hybrid
            ("Deal Desk Manager Tulsa", "Tulsa, Oklahoma", "other-tulsa", 1),
            ("Deal Desk Manager Chicago", "Chicago, Illinois", "chicago-metro", 0),
            ("Deal Desk Manager Unclear", "United States", "other-unitedstates", None),
        ]
    ):
        jid = _insert(
            conn,
            external_id=f"c{i}",
            title=title,
            location_raw=loc,
            location_bucket=bucket,
            remote=remote,
            jd_html=JD,
            jd_hash=f"c{i}",
        )
        conn.execute("UPDATE jobs SET status='filtered' WHERE id=?", (jid,))
        ids[title] = jid
    conn.commit()
    run_remote_check(conn)

    def status(t: str) -> str:
        return str(conn.execute("SELECT status FROM jobs WHERE id=?", (ids[t],)).fetchone()[0])

    def flags(t: str) -> list[str]:
        row = conn.execute("SELECT flags FROM sanity WHERE job_id=?", (ids[t],)).fetchone()
        return list(json.loads(row["flags"]))

    assert status("Deal Desk Manager Tulsa") == "rejected"
    reason = conn.execute(
        "SELECT reason FROM rejections WHERE job_id=?", (ids["Deal Desk Manager Tulsa"],)
    ).fetchone()["reason"]
    assert reason == "sanity:commute:unknown"
    assert status("Deal Desk Manager Chicago") == "filtered"  # relocation city
    assert status("Deal Desk Manager Unclear") == "filtered"
    assert any(f.startswith("remote-policy-unclear") for f in flags("Deal Desk Manager Unclear"))


# ---- standing rules: SDR/BDR is never an AE ----


@pytest.mark.parametrize(
    ("title", "cls"),
    [
        ("Sales Development Representative - AE Track", "sdr_bdr"),
        ("BDR (Associate Account Executive path)", "sdr_bdr"),
        ("SDR Manager", "sdr_bdr_manager"),
        ("Enterprise Account Executive", "account_executive"),
        ("Founding Account Executive", None),
        ("Director of Sales", None),
        # the filters.yaml account_executive track is the one AE definition
        ("Senior Sales Executive", "account_executive"),
        ("Enterprise Sales Executive", "account_executive"),
        ("Sales Executive - Hybrid Philadelphia", "account_executive"),
        ("Founding Sales Executive", None),  # the good-deal carve-out
        ("Regional Sales Director, AE team", None),  # sales_exec track, not an AE
    ],
)
def test_title_class_sdr_never_ae(title: str, cls: str | None) -> None:
    assert _title_class(title) == cls


def test_title_class_agrees_with_ae_track(cfg: Filters) -> None:
    """Review 2026-10-08: rules kept its own AE regex ("account executive|ae"),
    so a title the filter admitted at the AE floor escaped the AE rules. Every
    non-founding account_executive-track title is the AE class, and nothing
    else is."""
    titles = [
        "Senior Sales Executive",
        "Enterprise Account Executive",
        "Mid-Market AE",
        "Founding AE",
        "SDR - AE Track",
        "VP of Sales",
        "Deal Desk Manager",
    ]
    for t in titles:
        is_ae = cfg.track_for(t) == "account_executive" and not re.search(
            r"(?i)\b(founding|first)\b", t
        )
        assert (_title_class(t, cfg) == "account_executive") is is_ae, t


def test_sales_executive_at_a_tier3_company_rejected(conn: sqlite3.Connection) -> None:
    """The test profile's autoapprove.yaml: a "Senior Sales Executive" is an
    AE posting, so its AE rule for tier-3 companies rejects it."""
    jid = _queued(conn, score=95, tier=3, title="Senior Sales Executive", remote=0)
    stats = run_standing_rules(conn)
    assert stats["auto_rejected"] == 1
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "rejected"
    reason = conn.execute("SELECT reason FROM rejections WHERE job_id=?", (jid,)).fetchone()[0]
    assert reason == "ae-at-tier3-company"


def test_sdr_title_escapes_ae_rules(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rules = {
        "auto_approve": [],
        "auto_reject": [
            {
                "when": {"title_class": "account_executive", "remote": False},
                "reason": "ae-not-remote",
            }
        ],
    }
    p = tmp_path / "rules.yaml"
    p.write_text(yaml.safe_dump(rules))
    monkeypatch.setattr(rules_mod, "RULES_PATH", str(p))
    sdr = _insert(conn, external_id="s1", title="SDR → Account Executive Program", remote=0)
    ae = _insert(conn, external_id="s2", title="Enterprise Account Executive", remote=0)
    conn.execute("UPDATE jobs SET status='queued' WHERE id IN (?, ?)", (sdr, ae))
    conn.commit()
    stats = run_standing_rules(conn)
    assert stats["auto_rejected"] == 1
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (sdr,)).fetchone()[0] == "queued"
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (ae,)).fetchone()[0] == "rejected"


# ---- Greenhouse + Lever pay parsing (recorded fixtures) ----

GREENHOUSE_PAY = {
    "1000001": (198400, 251000, "USD"),  # one USD base block
    "6008747004": (129600, 161900, "USD"),  # tier 1 / tier 2: the higher tier
    "7592588": (85680, 126000, "USD"),  # base + OTE blocks: the base block
    "5168141": (71998, 71998, "USD"),  # cents rounded
    "4135236007": (85000, 145000, "USD"),  # titled "Hourly", annual amounts
    "7858932": (None, None, None),  # hourly amounts ($19.37 – $50)
    "7848317": (None, None, None),  # "Base Pay (Monthly)"
    "5391499008": (None, None, None),  # CAD only
    "7981883": (None, None, None),  # EUR only
    "1000002": (None, None, None),  # no pay transparency block
}


def test_greenhouse_pay_transparency_parsed() -> None:
    jobs = ADAPTERS["greenhouse"].parse(load_fixture("greenhouse_pay.json"))
    got = {j.external_id: (j.comp_min, j.comp_max, j.comp_currency) for j in jobs}
    assert got == GREENHOUSE_PAY


GREENHOUSE_BASIS = {
    "1000001": "base",  # "Remote Base Salary Range"
    "6008747004": "unknown",  # "Tier 1 Pay Range": the title does not say
    "7592588": "base",  # base + OTE blocks: comp is the base block
    "5168141": "ote",  # "The annual OTE range": the only block
    "4135236007": "unknown",
    "7858932": "unknown",  # no comp
}


def _gh_jobs() -> dict[str, Any]:
    return {
        j.external_id: j for j in ADAPTERS["greenhouse"].parse(load_fixture("greenhouse_pay.json"))
    }


def test_greenhouse_comp_basis() -> None:
    jobs = _gh_jobs()
    for ext_id, basis in GREENHOUSE_BASIS.items():
        j = jobs[ext_id]
        assert comp_basis(j.jd_html, j.comp_min, j.comp_max) == basis, ext_id
    # a stored range no block produced is unknown
    j = jobs["7592588"]
    assert comp_basis(j.jd_html, 1, 2) == "unknown"
    assert comp_basis(None, 85680, 126000) == "unknown"


def _pay_html(*blocks: tuple[str, int, int]) -> str:
    """A Greenhouse `content` field (HTML-escaped) with pay-transparency blocks."""
    body = "".join(
        f'<div class="pay-input"><div class="title">{t}</div><div class="pay-range">'
        f'<span>${lo:,}</span><span class="divider">&mdash;</span><span>${hi:,} USD</span>'
        "</div></div>"
        for t, lo, hi in blocks
    )
    return html.escape("<p>About the role.</p>" + body)


def test_pay_range_prefers_base_over_ote() -> None:
    content = _pay_html(
        ("Total on-target earnings (base + commission)", 90000, 110000),
        ("Base salary, Tier 1", 55000, 65000),
        ("Base salary, Tier 2", 50000, 60000),
    )
    r = pay_range(content)
    assert r is not None and (r.lo, r.hi, r.basis) == (55000, 65000, "base")
    # an OTE-floor track (SDR/BDR, AE) stores the OTE block instead
    r = pay_range(content, prefer="ote")
    assert r is not None and (r.lo, r.hi, r.basis) == (90000, 110000, "ote")
    only_ote = _pay_html(("OTE", 50000, 60000))
    for prefer in ("base", "ote"):
        r = pay_range(only_ote, prefer=prefer)
        assert r is not None and (r.lo, r.hi, r.basis) == (50000, 60000, "ote")
    only_base = _pay_html(("Base salary", 50000, 60000), ("Pay range", 40000, 70000))
    r = pay_range(only_base, prefer="ote")
    assert r is not None and (r.lo, r.hi, r.basis) == (50000, 60000, "base")
    with pytest.raises(ValueError):
        pay_range(content, prefer="total")


def _gh_parse(title: str, content: str, filters: Filters | None = None) -> Any:
    """One job through the Greenhouse adapter, as poll.upsert_jobs parses it."""
    payload = {
        "_token": "acme",
        "jobs": [
            {
                "id": 1,
                "title": title,
                "content": content,
                "location": {"name": "Remote - US"},
                "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1",
            }
        ],
    }
    return GreenhouseAdapter().parse(payload, filters=filters)[0]


@pytest.mark.parametrize(
    ("title", "stored", "basis"),
    [
        ("Enterprise Account Executive", (240000, 300000), "ote"),  # AE: OTE floor
        ("Sales Development Representative", (240000, 300000), "ote"),  # SDR: OTE floor
        ("Manager, Sales Development", (120000, 150000), "base"),  # base floor
        ("VP of Sales", (120000, 150000), "base"),  # base floor
        ("Deal Desk Manager", (120000, 150000), "base"),  # no track: default base floor
    ],
)
def test_greenhouse_base_and_ote_stored_by_floor_basis(
    cfg: Filters, title: str, stored: tuple[int, int], basis: str
) -> None:
    """Review 2026-10-08 (second pass): with a base and an OTE block, the
    stored range is the one of the same kind as the title's floor."""
    content = _pay_html(("Base", 120000, 150000), ("On-Target Earnings", 240000, 300000))
    j = _gh_parse(title, content, cfg)
    assert (j.comp_min, j.comp_max, j.comp_currency) == (*stored, "USD")
    assert comp_basis(j.jd_html, j.comp_min, j.comp_max) == basis


def test_ae_base_and_ote_not_rejected_by_the_ae_pay_rule(conn: sqlite3.Connection) -> None:
    """Review 2026-10-08 (second pass): an AE posting with base $120-150K and
    OTE $240-300K was stored as base and auto-rejected by an AE pay rule
    (comp_max_lt), against the rule's own comment ("base, or an OTE that the
    comp check judged realistic"). Stored as OTE, the test profile's
    autoapprove.yaml leaves it for the human: its AE pay rule (comp_max under
    $160K) does not fire, and its comp_min_gte rule does not auto-approve on
    an OTE minimum. An AE posting with only a base under $160K is still
    rejected."""
    title = "Enterprise Account Executive"
    both = _pay_html(("Base", 120000, 150000), ("On-Target Earnings", 240000, 300000))
    base_only = _pay_html(("Base", 120000, 150000))
    ids = {}
    for i, content in enumerate((both, base_only)):
        j = _gh_parse(title, content)  # filters.yaml loaded by the adapter itself
        ids[i] = _queued(
            conn,
            score=95,
            tier=2,
            external_id=f"ae{i}",
            title=title,
            remote=1,
            comp_min=j.comp_min,
            comp_max=j.comp_max,
            comp_currency=j.comp_currency,
            jd_html=j.jd_html,
        )
    stats = run_standing_rules(conn)
    assert stats == {"evaluated": 2, "auto_approved": 0, "auto_rejected": 1}
    status = "SELECT status FROM jobs WHERE id=?"
    assert conn.execute(status, (ids[0],)).fetchone()[0] == "queued"
    assert conn.execute(status, (ids[1],)).fetchone()[0] == "rejected"
    reasons = conn.execute("SELECT job_id, reason FROM rejections").fetchall()
    assert [tuple(r) for r in reasons] == [(ids[1], "ae-low-ceiling")]


def test_bdr_ote_below_ote_floor_rejected(cfg: Filters, cfg_sdr_active: Filters) -> None:
    """Review 2026-10-08: the Bellevue BDR posts OTE $71,998, under the $75K
    SDR OTE floor. It passed because the base allowance (x0.6 = $45K) was
    applied to a number that was already OTE. Judged as a remote posting, so
    the location filter stays out of the way."""
    bdr = _gh_jobs()["5168141"]
    row = {
        "location_raw": bdr.location_raw,
        "location_bucket": location_bucket(bdr.location_raw, True),
        "remote": 1,
        "comp_min": bdr.comp_min,
        "comp_max": bdr.comp_max,
        "comp_currency": bdr.comp_currency,
        "jd_html": bdr.jd_html,
    }
    active = cfg_sdr_active
    assert (
        _verdict(bdr.title, active, **row) == "comp_below_floor:71998<75000:sdr_bdr(ote):posted-ote"
    )
    # the same figure with no basis on record (a Lever range, a feed) is read as base
    assert _verdict(bdr.title, active, **{**row, "jd_html": ""}) is None
    # while the track is paused the posting is held whatever its pay
    assert _verdict(bdr.title, cfg, **row) == "track_paused:sdr_bdr"


@pytest.mark.parametrize(
    ("title", "blocks", "reason"),
    [
        # an SDR posting with OTE $60K: under the $75K OTE floor
        (
            "Sales Development Representative",
            [("OTE range", 50000, 60000)],
            "comp_below_floor:60000<75000:sdr_bdr(ote):posted-ote",
        ),
        # base and OTE published on an OTE-floor track: judged on the OTE, at
        # the full floor
        (
            "Sales Development Representative",
            [("Base salary", 52000, 60000), ("OTE (base + commission)", 80000, 95000)],
            None,
        ),
        # a $60K base clears the base allowance ($45K), but the posted $73K
        # OTE is under the $75K OTE floor
        (
            "Sales Development Representative",
            [("Base salary", 55000, 60000), ("OTE (base + commission)", 68000, 73000)],
            "comp_below_floor:73000<75000:sdr_bdr(ote):posted-ote",
        ),
        # a base floor with both published: judged on base
        (
            "Manager, Sales Development",
            [("Base salary", 95000, 105000), ("OTE", 140000, 160000)],
            "comp_below_floor:105000<110000:sdr_bdr_manager",
        ),
        (
            "Enterprise Account Executive",
            [("On-target earnings", 100000, 115000)],
            ("comp_below_floor:115000<140000:account_executive(ote):posted-ote"),
        ),
        ("Enterprise Account Executive", [("Base pay", 85000, 100000)], None),
        # a base floor: an OTE under it means the base is under it too
        (
            "Manager, Sales Development",
            [("OTE", 90000, 105000)],
            "comp_below_floor:105000<110000:sdr_bdr_manager:posted-ote",
        ),
        ("Manager, Sales Development", [("OTE", 110000, 130000)], None),
    ],
)
def test_comp_floor_by_posted_basis(
    cfg_sdr_active: Filters, title: str, blocks: list[tuple[str, int, int]], reason: str | None
) -> None:
    cfg = cfg_sdr_active  # the SDR rows assume the track is active
    content = _pay_html(*blocks)
    j = _gh_parse(title, content, cfg)
    assert j.comp_max is not None
    got = _verdict(
        title,
        cfg,
        comp_min=j.comp_min,
        comp_max=j.comp_max,
        comp_currency=j.comp_currency,
        jd_html=j.jd_html,
    )
    assert got == reason


def test_account_manager_judged_on_base(cfg: Filters) -> None:
    """Review 2026-10-08: Account Manager, CPG has base max $126K and OTE max
    $168.5K. Stored as the OTE block it passed a $165K default base floor."""
    am = _gh_jobs()["7592588"]
    row = {
        "title": am.title,
        "comp_min": am.comp_min,
        "comp_max": am.comp_max,
        "comp_currency": am.comp_currency,
        "jd_html": am.jd_html,
    }
    assert (am.comp_min, am.comp_max) == (85680, 126000)
    assert _comp_reason(row, cfg) == "comp_below_floor:126000<165000:default"


def test_auto_approve_never_fires_on_an_ote_minimum(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An auto-approve on comp_min_gte (then tailor and stage) must not read an
    OTE minimum as salary."""
    rules = {
        "auto_approve": [
            {
                "when": {"score_gte": 95, "remote": True, "comp_min_gte": 200000},
                "reason": "remote-high-base",
            }
        ],
        "auto_reject": [],
    }
    p = tmp_path / "rules.yaml"
    p.write_text(yaml.safe_dump(rules))
    monkeypatch.setattr(rules_mod, "RULES_PATH", str(p))
    ids = {}
    for i, label in enumerate(("Base", "OTE")):
        content = _pay_html((label, 210000, 260000))
        jid = _insert(
            conn,
            external_id=f"r{i}",
            title="Deal Desk Manager",
            remote=1,
            comp_min=210000,
            comp_max=260000,
            comp_currency="USD",
            jd_html=content,
        )
        conn.execute("UPDATE jobs SET status='queued' WHERE id=?", (jid,))
        conn.execute("INSERT INTO scores (job_id, fit_score) VALUES (?, 97)", (jid,))
        ids[label] = jid
    conn.commit()
    stats = run_standing_rules(conn)
    assert stats["auto_approved"] == 1
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (ids["Base"],)).fetchone()[0] == (
        "approved"
    )
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (ids["OTE"],)).fetchone()[0] == (
        "queued"
    )


def test_lever_salary_range_parsed() -> None:
    payload = load_fixture("lever_pay.json")
    assert isinstance(payload, list)
    jobs = {j.title: j for j in ADAPTERS["lever"].parse(payload)}
    assert (jobs["Lead Value Consultant"].comp_min, jobs["Lead Value Consultant"].comp_max) == (
        180000,
        210000,
    )
    rev = jobs["Manager, Customer Operations (Revenue Operations)"]
    assert (rev.comp_min, rev.comp_max, rev.comp_currency) == (120000, 155000, "USD")
    nopay = [j for t, j in jobs.items() if t.startswith("Director of Applied Science")]
    assert nopay and nopay[0].comp_max is None
    hourly = dict(
        payload[0], salaryRange={**payload[0]["salaryRange"], "interval": "per-hour-wage"}
    )
    (h,) = ADAPTERS["lever"].parse([hourly])
    assert (h.comp_min, h.comp_max, h.comp_currency) == (None, None, None)


# ---- feeds ----


def test_feed_config(cfg: Filters) -> None:
    assert _spec_for("getro", cfg) is None, "getro: robots.txt disallows the endpoint"
    assert _spec_for("a16z", cfg) is None, "a16z: broken parse + spoofed UA"
    remotive = _spec_for("remotive", cfg)
    assert remotive and remotive.categories == ("sales", "business")
    adzuna = _spec_for("adzuna", cfg)
    assert adzuna and adzuna.salary_min == cfg.lowest_comp_floor
    assert any("revenue operations" in k for k in adzuna.keywords)
    assert not any(re.search(p, k) for k in adzuna.keywords for p in cfg.exclude_titles)


def test_adzuna_salary_min_from_config(cfg: Filters, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADZUNA_APP_ID", "test-id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "test-key")
    resp = MagicMock()
    resp.json.return_value = {"results": []}
    resp.raise_for_status.return_value = None
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    spec = _spec_for("adzuna", cfg)
    assert spec is not None
    asyncio.run(AdzunaFeed().search(client, spec))
    params = [c.kwargs["params"] for c in client.get.call_args_list]
    assert [p["what_phrase"] for p in params] == list(spec.keywords), "every phrase queried"
    assert {p["salary_min"] for p in params} == {str(cfg.lowest_comp_floor)}


# ---- refilter (in-memory DB) ----


def _rejected(conn: sqlite3.Connection, reason: str, layer: str = "filter", **kw: Any) -> int:
    jid = _insert(conn, **kw)
    conn.execute("UPDATE jobs SET status='rejected' WHERE id=?", (jid,))
    conn.execute(
        "INSERT INTO rejections (job_id, layer, reason) VALUES (?, ?, ?)", (jid, layer, reason)
    )
    return jid


@pytest.fixture()
def seeded(mem: sqlite3.Connection) -> dict[str, int]:
    old_sdr = "exclude_title:(?i)\\b(sdr|bdr|sales development|busi"
    ids = {
        "sdr_title": _rejected(
            mem, old_sdr, external_id="1", title="Sales Development Representative"
        ),
        "mgr_include": _rejected(
            mem, "no_include_title_match", external_id="2", title="Manager, Sales Development"
        ),
        "sdr_comp": _rejected(
            mem,
            "comp_below_floor:60000<165000",
            external_id="3",
            title="Business Development Representative",
            comp_max=60000,
            comp_currency="USD",
        ),
        "us_wide": _rejected(
            mem,
            "location:other-unitedstates",
            external_id="4",
            title="Deal Desk Manager",
            remote=None,
            location_raw="United States",
            location_bucket="other-unitedstates",
        ),
        "rebucket": _rejected(
            mem,
            "location:remote-intl",
            external_id="5",
            title="Deal Desk Manager",
            remote=None,
            location_raw="Remote, Canada; Remote, United States",
            location_bucket="remote-intl",
        ),
        "now_engineer": _rejected(
            mem, "no_include_title_match", external_id="6", title="Solutions Engineer"
        ),
        "same_reason": _rejected(
            mem, "no_include_title_match", external_id="7", title="Marketing Coordinator"
        ),
        "stale": _rejected(mem, "stale:40d", external_id="8", title="Sales Development Rep"),
        "blocklist": _rejected(mem, "blocklist_company:Acme", external_id="9", title="SDR"),
        "closed": _rejected(mem, "no_include_title_match", external_id="10", title="SDR"),
        "sanity_latest": _rejected(mem, "no_include_title_match", external_id="11", title="BDR"),
        "queued_by_human": _rejected(mem, "no_include_title_match", external_id="12", title="BDR"),
    }
    mem.execute("UPDATE jobs SET closed_at=datetime('now') WHERE id=?", (ids["closed"],))
    mem.execute(
        "INSERT INTO rejections (job_id, layer, reason) VALUES (?, 'sanity', 'sanity:onsite')",
        (ids["sanity_latest"],),
    )
    mem.execute("UPDATE jobs SET status='queued' WHERE id=?", (ids["queued_by_human"],))
    mem.commit()
    return ids


def _status(conn: sqlite3.Connection, jid: int) -> str:
    return str(conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0])


def test_refilter_dry_run_changes_nothing(
    mem: sqlite3.Connection, seeded: dict[str, int], cfg_sdr_active: Filters
) -> None:
    cfg = cfg_sdr_active  # the seeded SDR rows assume the track is active
    before = mem.execute("SELECT id, status, location_bucket FROM jobs ORDER BY id").fetchall()
    rep = refilter(mem, cfg, dry_run=True)
    assert rep.candidates == 7
    assert rep.by_reason == {
        "exclude_title": 1,
        "no_include_title_match": 3,
        "comp_below_floor": 1,
        "location": 2,
    }
    assert rep.would_pass == 5 and rep.unchanged == 1 and rep.reset == 6
    assert rep.rebucketed == 1
    assert "DRY RUN" in rep.render()
    after = mem.execute("SELECT id, status, location_bucket FROM jobs ORDER BY id").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
    assert mem.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_refilter_resets_and_reevaluates(
    mem: sqlite3.Connection, seeded: dict[str, int], cfg_sdr_active: Filters
) -> None:
    cfg = cfg_sdr_active  # the seeded SDR rows assume the track is active
    rep = refilter(mem, cfg)
    assert rep.reset == 6
    assert rep.filter_stats == {"examined": 6, "passed": 5, "rejected": 1}
    for k in ("sdr_title", "mgr_include", "sdr_comp", "us_wide", "rebucket"):
        assert _status(mem, seeded[k]) == "filtered", k
    assert (
        mem.execute(
            "SELECT location_bucket FROM jobs WHERE id=?", (seeded["rebucket"],)
        ).fetchone()[0]
        == "remote-us"
    )
    # re-rejected, now with the current reason
    assert _status(mem, seeded["now_engineer"]) == "rejected"
    latest = mem.execute(
        "SELECT reason FROM rejections WHERE job_id=? ORDER BY id DESC LIMIT 1",
        (seeded["now_engineer"],),
    ).fetchone()[0]
    assert latest.startswith("exclude_title:(?i)\\bengineer")
    # untouched: same verdict, non-config reasons, closed, later layers, human-queued
    for k, want in [
        ("same_reason", "rejected"),
        ("stale", "rejected"),
        ("blocklist", "rejected"),
        ("closed", "rejected"),
        ("sanity_latest", "rejected"),
        ("queued_by_human", "queued"),
    ]:
        assert _status(mem, seeded[k]) == want, k
    events = mem.execute("SELECT job_id, kind, detail FROM events").fetchall()
    assert len(events) == 6 and {e["kind"] for e in events} == {"refiltered"}
    by_job = {e["job_id"]: json.loads(e["detail"]) for e in events}
    assert by_job[seeded["sdr_comp"]] == {"previous_reason": "comp_below_floor:60000<165000"}
    # a second run finds nothing left to change
    again = refilter(mem, cfg)
    assert again.reset == 0 and again.unchanged == 2  # same_reason + now_engineer


def test_refilter_without_filter_pass_leaves_jobs_new(
    mem: sqlite3.Connection, seeded: dict[str, int], cfg_sdr_active: Filters
) -> None:
    cfg = cfg_sdr_active  # the seeded SDR rows assume the track is active
    rep = refilter(mem, cfg, run_filter_pass=False)
    assert rep.filter_stats is None
    assert _status(mem, seeded["sdr_title"]) == "new"
    assert run_filters(mem, cfg)["passed"] == 5


def test_refilter_never_revives_a_human_reject(
    mem: sqlite3.Connection, cfg_sdr_active: Filters, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 2026-10-08: the MCP reject tool writes only a decisions row, so a
    refiltered job the human then rejected still had the old filter rejection
    as its latest one, and the next refilter put it back in the queue."""
    from jobkit import mcp_server

    cfg = cfg_sdr_active  # the SDR row assumes the track is active
    old_sdr = "exclude_title:(?i)\\b(sdr|bdr|sales development|busi"
    jid = _rejected(mem, old_sdr, external_id="1", title="Sales Development Representative")
    mem.commit()
    assert refilter(mem, cfg).reset == 1
    assert _status(mem, jid) == "filtered"
    # triage queues it; the human rejects it through the MCP tool
    mem.execute("UPDATE jobs SET status='queued' WHERE id=?", (jid,))
    mem.executescript(mcp_server.PINS_SCHEMA)
    mem.execute("INSERT INTO queue_pins (n, job_id) VALUES (1, ?)", (jid,))
    mem.commit()
    monkeypatch.setattr(mcp_server, "_conn", lambda: mem)
    assert mcp_server.reject("1", "not for me") == "rejected [1] (not for me)"
    assert _status(mem, jid) == "rejected"
    assert mem.execute("SELECT COUNT(*) FROM rejections WHERE job_id=?", (jid,)).fetchone()[0] == 1

    again = refilter(mem, cfg)
    assert again.candidates == 0 and again.reset == 0
    assert _status(mem, jid) == "rejected"
    # the reset statement itself refuses a job with a decision
    u = {"job_id": jid, "location_bucket": "remote-us", "cluster_key": "a|b|c"}
    assert mem.execute(q.RESET_REJECTED_TO_NEW, u).rowcount == 0


def test_refilter_cli_and_mcp_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from jobkit import mcp_server
    from jobkit.cli import app

    # both entry points open the default DB: point it at a fresh in-memory one
    monkeypatch.setattr(q, "DB_PATH", Path(":memory:"))
    out = mcp_server.refilter()  # confirm defaults to False: a dry run
    assert out.startswith("DRY RUN") and "reset to new: 0" in out
    res = CliRunner().invoke(app, ["refilter", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "DRY RUN" in res.output
