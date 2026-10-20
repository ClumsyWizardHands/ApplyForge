"""LinkedIn guest (logged-out) discovery: CLAUDE.md hard rule 11's exception of
2026-10-12, discovery only. Offline: LinkedIn and the ATS are httpx
MockTransports serving the shapes recorded live on 2026-10-12
(tests/fixtures/linkedin_guest/) and the recorded Greenhouse board."""

from __future__ import annotations

import asyncio
import copy
import datetime
import json
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from jobkit.config import Filters, load_filters, parse_linkedin_guest
from jobkit.discovery import ingest
from jobkit.discovery import linkedin_guest as lg
from tests.conftest import FIXTURES, load_fixture

FIX = FIXTURES / "linkedin_guest"
DAY = datetime.date(2026, 10, 12)
CONTACT = "jobkit-tests@example.invalid"
GH_API = "boards-api.greenhouse.io"
GH_JOB_ID = "4461450008"  # "Account Executive, Startups" in greenhouse_anthropic.json


def card_li(li_id: str, title: str, company: str, location: str = "New York, NY") -> str:
    """One search card in the recorded shape (search_cards.html)."""
    return (
        '<li><div class="base-card relative w-full base-card--link base-search-card '
        f'base-search-card--link job-search-card" data-entity-urn="urn:li:jobPosting:{li_id}">'
        '<a class="base-card__full-link" '
        f'href="https://www.linkedin.com/jobs/view/x-{li_id}"><span class="sr-only">{title}'
        '</span></a><div class="base-search-card__info">'
        f'<h3 class="base-search-card__title">{title}</h3>'
        f'<h4 class="base-search-card__subtitle"><a class="hidden-nested-link" '
        f'href="https://www.linkedin.com/company/x">{company}</a></h4>'
        '<div class="base-search-card__metadata">'
        f'<span class="job-search-card__location">{location}</span>'
        '<time class="job-search-card__listdate" datetime="2026-10-10">2 days ago</time>'
        "</div></div></div></li>"
    )


ANTHROPIC_AE = ("4400000001", "Account Executive, Startups", "Anthropic")
ACME_MANAGER = ("4400000002", "Sales Manager", "Acme Widgets Co")
ENGINEER = ("4400000003", "Solutions Engineer", "Zzqx Robotics")
PAUSED_SDR = ("4400000004", "Sales Development Representative", "Zzqx Robotics")
ALL_CARDS = "".join(card_li(*c) for c in (ANTHROPIC_AE, ACME_MANAGER, ENGINEER, PAUSED_SDR))


def recorded_detail() -> str:
    return (FIX / "detail_offsite.html").read_text()


LEGACY_APPLY = (
    '<code id="applyUrl" style="display: none"><!--"https://www.linkedin.com/jobs/view/'
    "externalApply/4400000002?url=https%3A%2F%2Fjob-boards%2Egreenhouse%2Eio%2Fanthropic%2Fjobs"
    f'%2F{GH_JOB_ID}%3Fgh_src%3Dlinkedin&urlHash=AbCd"--></code>'
)


# Hand-built (not recorded) in the shape of Cloudflare's managed-challenge page,
# which Cloudflare normally serves as HTTP 503 (or 403) with a cf-mitigated header.
CLOUDFLARE_CHALLENGE = (
    '<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title></head>'
    "<body><noscript>Enable JavaScript and cookies to continue</noscript>"
    "<script>(function(){window._cf_chl_opt={cType:'managed'};var a=document."
    "createElement('script');a.src='/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/"
    "v1?ray=0';document.head.appendChild(a);}());</script></body></html>"
)


def legacy_detail() -> str:
    """The pre-2026 guest page shape: the recorded page plus <code id="applyUrl">
    (an externalApply link whose url= is the employer's apply URL)."""
    return recorded_detail() + LEGACY_APPLY


class LinkedIn:
    """A MockTransport standing in for www.linkedin.com: records every request."""

    def __init__(
        self,
        search: str | Callable[[httpx.Request], httpx.Response] = ALL_CARDS,
        details: dict[str, str | httpx.Response] | None = None,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.search = search
        self.details = details or {}
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        cookie = {"set-cookie": "bcookie=abc; Path=/; Domain=.linkedin.com"}
        if request.url.path == lg.SEARCH_PATH:
            if callable(self.search):
                return self.search(request)
            return httpx.Response(200, text=self.search, headers=cookie)
        li_id = request.url.path.rsplit("/", 1)[-1]
        body = self.details.get(li_id)
        if isinstance(body, httpx.Response):
            return body
        if body is None:
            return httpx.Response(404, text="")
        return httpx.Response(200, text=body, headers=cookie)

    def count(self, kind: str) -> int:
        path = lg.SEARCH_PATH if kind == "search" else "/jobs-guest/jobs/api/jobPosting/"
        return sum(1 for r in self.requests if r.url.path.startswith(path))


class Ats:
    """A MockTransport standing in for the employer ATS APIs."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert "linkedin" not in str(request.url)
        if request.url.host == GH_API and request.url.path == "/v1/boards/anthropic/jobs":
            return httpx.Response(200, json=load_fixture("greenhouse_anthropic.json"))
        return httpx.Response(404, json={})


@pytest.fixture(autouse=True)
def _linkedin_off_baseline(jobkit_home: Path) -> None:
    """Every test starts from LinkedIn discovery switched OFF in its sandbox
    config, whatever a real config says; tests that need it on use
    enabled()."""
    path = jobkit_home / "config" / "filters.yaml"
    cfg = yaml.safe_load(path.read_text())
    cfg["feeds"]["linkedin_guest"]["enabled"] = False
    path.write_text(yaml.safe_dump(cfg))


def enabled(**overrides: Any) -> Filters:
    raw = copy.deepcopy(load_filters().raw)
    raw["feeds"]["linkedin_guest"].update({"enabled": True, **overrides})
    return Filters(raw=raw)


@pytest.fixture()
def contact(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(lg.CONTACT_ENV, CONTACT)
    return CONTACT


def run(
    conn: sqlite3.Connection,
    li: LinkedIn,
    filters: Filters | None = None,
    ats: Ats | None = None,
    day: datetime.date = DAY,
    sleeps: list[float] | None = None,
) -> lg.Report:
    record = sleeps if sleeps is not None else []
    return lg.run(
        conn,
        filters or enabled(),
        transport=li.transport,
        ats_transport=(ats or Ats()).transport,
        sleep=record.append,
        rand=lambda: 0.5,
        today=day,
    )


def events(conn: sqlite3.Connection, kind: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT job_id, detail FROM events WHERE kind = ? ORDER BY id", (kind,))
    return [{"job_id": r["job_id"], **json.loads(r["detail"])} for r in rows]


def assert_no_linkedin_rows(conn: sqlite3.Connection) -> None:
    """Hard rule 10 + the exception's terms: no job row, source or queued URL
    is LinkedIn's."""
    assert not conn.execute("SELECT id FROM jobs WHERE apply_url LIKE '%linkedin%'").fetchall()
    assert not conn.execute(
        "SELECT job_id FROM sources WHERE source LIKE '%linkedin%' OR source_url LIKE '%linkedin%'"
    ).fetchall()
    assert not conn.execute(
        "SELECT id FROM discovery_queue WHERE first_seen_url LIKE '%linkedin%'"
    ).fetchall()


# ---------------------------------------------------------------------------
# parsing the recorded shapes
# ---------------------------------------------------------------------------


def test_parse_cards_recorded_fixture() -> None:
    cards = lg.parse_cards((FIX / "search_cards.html").read_text())
    assert [(c.li_id, c.title, c.company, c.location, c.listed) for c in cards] == [
        (
            "4476943614",
            "Remote Account Executive/ Sr. Account Executive - Salt Lake City",
            "Q1Media",
            "Salt Lake City, UT",
            "2026-10-07",
        ),
        (
            "4457706293",
            "Mid-Market Account Executive - East",
            "Contentsquare",
            "Miami, FL",
            "2026-10-09",
        ),
        (
            "4467408266",
            "Account Executive | SMB | New Logo",
            "Jamf",
            "Minneapolis, MN",
            "2026-10-06",
        ),
    ]


def test_parse_cards_falls_back_to_the_view_link_and_dedupes() -> None:
    html = card_li(*ANTHROPIC_AE).replace('data-entity-urn="urn:li:jobPosting:4400000001"', "")
    cards = lg.parse_cards(html + html + "<li>not a card</li>")
    assert [c.li_id for c in cards] == ["4400000001"]


def test_parse_posting_current_shape_has_no_employer_url() -> None:
    """Live 2026-10-12: the off-site Apply button opens a sign-in modal; the
    employer's URL is not in the logged-out page."""
    p = lg.parse_posting(recorded_detail(), "4457706293")
    assert p.apply_type == "offsite"
    assert p.employer_url is None
    assert (p.title, p.company, p.location) == (
        "Mid-Market Account Executive - East",
        "Contentsquare",
        "Miami, FL",
    )
    assert lg.block_reason(httpx.Response(200, text=recorded_detail())) is None


def test_parse_posting_legacy_apply_url() -> None:
    p = lg.parse_posting(legacy_detail(), "4400000002")
    assert p.apply_type == "offsite"
    assert p.employer_url == (
        f"https://job-boards.greenhouse.io/anthropic/jobs/{GH_JOB_ID}?gh_src=linkedin"
    )


def test_parse_posting_easy_apply_only() -> None:
    """Easy Apply only: no employer URL. (Not seen live: all four postings
    sampled on 2026-10-12 were off-site; the marker is LinkedIn's tracking
    name for its own apply flow. Either way the company is only queued.)"""
    html = recorded_detail().replace("apply-link-offsite", "apply-link-simple")
    p = lg.parse_posting(html, "1")
    assert (p.apply_type, p.employer_url) == ("easy_apply", None)
    bare = recorded_detail().replace("apply-link-offsite", "apply-link-unknown")
    assert lg.parse_posting(bare, "1").apply_type == "unknown"


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (
            '"https://jobs.lever.co/acme/1b2c3d4e-0000-0000-0000-000000000000"',
            "https://jobs.lever.co/acme/1b2c3d4e-0000-0000-0000-000000000000",
        ),
        ("https://www.linkedin.com/jobs/view/1", None),
        (
            "https://www.linkedin.com/jobs/view/externalApply/1?url=https%3A%2F%2Fwww.linkedin.com%2Fx",
            None,
        ),
        ("https://lnkd.in/abc", None),
        ("javascript:alert(1)", None),
        ("", None),
    ],
)
def test_employer_url_never_returns_a_linkedin_url(raw: str, want: str | None) -> None:
    assert lg.employer_url(raw) == want


@pytest.mark.parametrize(
    ("resp", "blocked"),
    [
        (httpx.Response(429), True),
        (httpx.Response(999), True),
        (httpx.Response(403), True),
        (
            httpx.Response(302, headers={"location": "https://www.linkedin.com/authwall?trk=x"}),
            True,
        ),
        (httpx.Response(303, headers={"location": "/checkpoint/lg/login"}), True),
        (httpx.Response(200, text='<div id="captcha-internal"></div>'), True),
        (httpx.Response(200, text="<html><title>Security Verification | LinkedIn</title>"), True),
        # a challenge is a block at any status, not only 200
        (httpx.Response(503, text=CLOUDFLARE_CHALLENGE), True),
        (httpx.Response(200, text=CLOUDFLARE_CHALLENGE), True),
        (httpx.Response(400, text='<div id="captcha-internal"></div>'), True),
        (httpx.Response(503, text='<div class="cf-turnstile"></div>'), True),
        (httpx.Response(503, headers={"cf-mitigated": "challenge"}), True),
        (httpx.Response(200, text="<title>Attention Required! | Cloudflare</title>"), True),
        (httpx.Response(401), True),
        (httpx.Response(200, text=ALL_CARDS), False),
        (httpx.Response(200, text=""), False),
        (httpx.Response(404), False),
        (httpx.Response(500), False),
        (httpx.Response(503), False),  # a bare 5xx stops the run, not the day
    ],
)
def test_block_reason(resp: httpx.Response, blocked: bool) -> None:
    assert (lg.block_reason(resp) is not None) is blocked


def test_recorded_pages_are_not_blocks() -> None:
    """The Cloudflare markers must not fire on the pages recorded live."""
    for name in ("search_cards.html", "detail_offsite.html"):
        assert lg.block_reason(httpx.Response(200, text=(FIX / name).read_text())) is None
    assert lg.no_cards_reason((FIX / "search_cards.html").read_text()) is None


@pytest.mark.parametrize(
    ("html", "suspect"),
    [
        ("", False),
        ("  \n<!-- nothing here -->\n ", False),
        (ALL_CARDS, False),
        ('<li><div class="base-search-card"></div></li>', False),  # a changed card shape
        ("<html><body><p>Please wait while we check your browser</p></body></html>", True),
        ('<div class="jobs-search__results-list"></div>', True),
    ],
)
def test_no_cards_reason(html: str, suspect: bool) -> None:
    assert (lg.no_cards_reason(html) is not None) is suspect


# ---------------------------------------------------------------------------
# config: one owner, ceilings from the rule-11 exception
# ---------------------------------------------------------------------------


def test_config_defaults_off_and_ceilings_hold() -> None:
    cfg = parse_linkedin_guest(None)
    assert cfg.enabled is False
    assert len(cfg.keywords) == 4 and cfg.max_search_requests_per_day == 6
    shipped = load_filters().linkedin_guest
    assert shipped.enabled is False  # sandbox baseline (see _linkedin_off_baseline)
    assert shipped.max_detail_requests_per_day == 0
    assert [(loc.location, loc.f_wt) for loc in shipped.locations] == [
        ("United States", "2"),
        ("Greater Philadelphia", "3"),
    ]
    for bad in (
        {"keywords": ["a", "b", "c", "d", "e"]},
        {"max_search_requests_per_day": 7},
        {"max_detail_requests_per_day": 41},
        {"min_delay_seconds": 2},
        {"locations": [{"location": "Boston", "workplace": "office"}]},
        {"enabled": "yes"},
    ):
        with pytest.raises(ValueError):
            parse_linkedin_guest(bad)


def test_plan_rotates_pairs_by_day_and_continues_within_a_day() -> None:
    cfg = enabled().linkedin_guest
    pairs = {(k, loc.location) for loc in cfg.locations for k in cfg.keywords}
    covered = set()
    for n in range(4):
        plan = lg.plan_searches(cfg, DAY + datetime.timedelta(days=n), 0)
        assert len(plan) == 6 and all(qy.start == 0 for qy in plan)
        covered |= {(qy.keywords, qy.location.location) for qy in plan}
    assert covered == pairs
    full = lg.plan_searches(cfg, DAY, 0)
    assert lg.plan_searches(cfg, DAY, 4) == full[4:]
    assert lg.plan_searches(cfg, DAY, 6) == []
    params = full[0].params(cfg)
    assert params["f_TPR"] == "r604800" and params["sortBy"] == "DD"
    one = enabled(keywords=["account executive"], max_search_requests_per_day=3).linkedin_guest
    assert [qy.start for qy in lg.plan_searches(one, DAY, 0)] == [0, 0, 10]


# ---------------------------------------------------------------------------
# a run
# ---------------------------------------------------------------------------


def test_disabled_makes_no_request_and_writes_nothing(
    conn: sqlite3.Connection, contact: str
) -> None:
    li = LinkedIn()
    rep = run(conn, li, filters=load_filters())
    assert rep.status == "disabled" and li.requests == []
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_dry_run_makes_no_request_and_writes_nothing(
    conn: sqlite3.Connection, contact: str
) -> None:
    li = LinkedIn()
    rep = lg.run(conn, enabled(), dry_run=True, transport=li.transport, today=DAY)
    assert rep.status == "dry_run" and li.requests == []
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    text = rep.render()
    assert "would search" in text and lg.CONTACT_ENV in text and CONTACT not in text


def test_no_contact_no_request(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(lg.CONTACT_ENV, raising=False)
    li = LinkedIn()
    rep = run(conn, li)
    assert rep.status == "no_contact" and li.requests == []


def test_contact_from_the_answer_bank(jobkit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(lg.CONTACT_ENV, raising=False)
    (jobkit_home / "data" / "answer_bank.yaml").write_text(
        yaml.safe_dump({"identity": {"email": "bank@example.invalid"}})
    )
    assert lg.contact() == ("bank@example.invalid", "answer bank identity.email")


def test_honest_user_agent_and_no_cookies(conn: sqlite3.Connection, contact: str) -> None:
    li = LinkedIn(details={})
    run(conn, li, filters=enabled(max_detail_requests_per_day=2))
    assert li.count("search") == 6 and li.count("detail") == 1
    for r in li.requests:
        ua = r.headers["user-agent"]
        assert ua.startswith("jobkit/0.1 (") and f"contact: {contact}" in ua
        assert "mozilla" not in ua.lower() and "chrome" not in ua.lower()
        assert "cookie" not in r.headers, "a cookie LinkedIn set was sent back"
        assert r.url.host == "www.linkedin.com" and r.url.path.startswith("/jobs-guest/")
    client = lg.make_client(contact, li.transport)
    assert client.trust_env is False and client.follow_redirects is False


def test_pacing_waits_between_requests(conn: sqlite3.Connection, contact: str) -> None:
    sleeps: list[float] = []
    li = LinkedIn()
    run(conn, li, filters=enabled(min_delay_seconds=3), sleeps=sleeps)
    assert len(sleeps) == len(li.requests) - 1  # none before the first request
    assert all(3 <= s <= 6 for s in sleeps)


def test_a_name_match_only_queues_the_company(conn: sqlite3.Connection, contact: str) -> None:
    """Rule 11's exception as written: a posting enters jobkit only when its
    apply link resolves to a supported ATS; otherwise only the company name
    goes to the discovery queue. A registry match on the company name is not
    an apply link: the run adds no company or board and polls nothing. The
    discovery stage (next in run-all and the nightly) then adds the board."""
    from jobkit.discovery import registry

    ats = Ats()
    li = LinkedIn()
    rep = run(conn, li, ats=ats)
    assert rep.status == "ok", rep.render()
    assert rep.searches == 6 and rep.cards == 4  # deduplicated across the searches
    assert rep.title_rejected == 2  # the engineer and the paused SDR track
    assert (rep.boards_polled, rep.boards_added, rep.new_via_ats) == (0, 0, 0)
    assert rep.queued_companies == 2
    assert ats.requests == []  # no board polled by this run
    assert conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    # the registry hit and the miss alike: the company name only
    queued = conn.execute(
        "SELECT company_name, source, first_seen_url FROM discovery_queue ORDER BY id"
    ).fetchall()
    assert [tuple(r) for r in queued] == [
        ("Anthropic", "linkedin_guest", None),
        ("Acme Widgets Co", "linkedin_guest", None),
    ]
    # rejections recorded like feed rejections, with a LinkedIn ref and no job row
    rejected = {r["external_ref"]: r["reason"] for r in conn.execute("SELECT * FROM rejections")}
    assert rejected[f"linkedin_guest/{ENGINEER[0]}"].startswith("exclude_title:")
    assert rejected[f"linkedin_guest/{PAUSED_SDR[0]}"] == "track_paused:sdr_bdr"
    seen = {e["id"]: e for e in events(conn, lg.EV_SEEN)}
    assert seen[ANTHROPIC_AE[0]]["outcome"] == "queued_company:registry_board:greenhouse/anthropic"
    assert seen[ANTHROPIC_AE[0]]["job_id"] is None
    assert seen[ACME_MANAGER[0]]["outcome"] == "queued_company:no_registry_board"
    assert li.count("detail") == 0  # posting pages are off by default
    assert "new via ATS 0, queued companies 2, blocked: no" in str(rep)
    # the discovery stage adds the registry board (tier 3); the next poll reads it
    assert registry.resolve_discovery_queue(conn) == {"checked": 2, "found": 1, "not_found": 1}
    board = conn.execute(
        "SELECT b.ats, b.token, c.tier FROM boards b JOIN companies c ON c.id = b.company_id"
    ).fetchall()
    assert [tuple(b) for b in board] == [("greenhouse", "anthropic", 3)]
    assert ats.requests == []
    assert_no_linkedin_rows(conn)


def test_seen_ids_are_skipped_for_14_days(conn: sqlite3.Connection, contact: str) -> None:
    for li_id, age in ((ANTHROPIC_AE[0], "-13 days"), (ACME_MANAGER[0], "-15 days")):
        conn.execute(
            "INSERT INTO events (kind, detail, at) VALUES (?, ?, datetime('now', ?))",
            (lg.EV_SEEN, json.dumps({"id": li_id, "outcome": "x"}), age),
        )
    conn.commit()
    ats = Ats()
    rep = run(conn, LinkedIn(), ats=ats)
    assert rep.seen_before == 1
    assert ats.requests == []  # the Anthropic card was not handed off again
    assert rep.queued_companies == 1  # the 15-day-old one was handled again


def test_known_board_needs_nothing(conn: sqlite3.Connection, contact: str) -> None:
    conn.execute(
        "INSERT INTO companies (id, name, normalized_name, tier) "
        "VALUES (7, 'Anthropic', 'anthropic', 1)"
    )
    conn.execute(
        "INSERT INTO boards (company_id, ats, token) VALUES (7, 'greenhouse', 'anthropic')"
    )
    conn.commit()
    ats = Ats()
    rep = run(conn, LinkedIn(search=card_li(*ANTHROPIC_AE)), ats=ats)
    assert rep.known_board == 1 and ats.requests == []
    assert conn.execute("SELECT tier FROM companies WHERE id = 7").fetchone()[0] == 1


def test_caps_hold_across_runs(conn: sqlite3.Connection, contact: str) -> None:
    li = LinkedIn()
    filters = enabled(max_search_requests_per_day=2)
    assert run(conn, li, filters).searches == 2
    again = run(conn, li, filters)
    assert again.status == "no_budget" and li.count("search") == 2
    # a new local day, a new budget
    assert run(conn, li, filters, day=DAY + datetime.timedelta(days=1)).searches == 2
    assert li.count("search") == 4


def test_detail_cap_defers_the_rest_to_tomorrow(conn: sqlite3.Connection, contact: str) -> None:
    misses = card_li("4400000010", "Sales Manager", "Acme Widgets Co") + card_li(
        "4400000011", "Account Executive", "Zzqx Robotics"
    )
    li = LinkedIn(
        search=misses, details={"4400000010": recorded_detail(), "4400000011": recorded_detail()}
    )
    filters = enabled(max_search_requests_per_day=1, max_detail_requests_per_day=1)
    first = run(conn, li, filters)
    assert (first.details, first.deferred, first.queued_companies) == (1, 1, 1)
    assert run(conn, li, filters).status == "no_budget" and li.count("detail") == 1
    second = run(conn, li, filters, day=DAY + datetime.timedelta(days=1))
    assert (second.seen_before, second.details, second.queued_companies) == (1, 1, 1)
    outcomes = [e["outcome"] for e in events(conn, lg.EV_SEEN)]
    assert outcomes == ["queued_company:offsite", "queued_company:offsite"]
    assert_no_linkedin_rows(conn)


def test_employer_url_on_a_posting_page_goes_through_add_url(
    conn: sqlite3.Connection, contact: str
) -> None:
    """If a logged-out posting page carries the employer URL again (the older
    shape), a supported ATS is ingested by that URL, matched by job id."""
    li = LinkedIn(
        search=card_li("4400000002", "Account Executive (Startups)", "Acme Widgets Co"),
        details={"4400000002": legacy_detail()},
    )
    ats = Ats()
    rep = run(conn, li, filters=enabled(max_detail_requests_per_day=5), ats=ats)
    assert (rep.details, rep.new_via_ats, rep.queued_companies) == (1, 1, 0)
    seen = events(conn, lg.EV_SEEN)
    assert seen[0]["outcome"] == "ats_new:greenhouse/anthropic"
    job = conn.execute("SELECT external_id FROM jobs WHERE id = ?", (seen[0]["job_id"],)).fetchone()
    assert job["external_id"] == GH_JOB_ID
    assert_no_linkedin_rows(conn)


def _authwall(request: httpx.Request) -> httpx.Response:
    return httpx.Response(302, headers={"location": "https://www.linkedin.com/authwall?trk=guest"})


@pytest.mark.parametrize(
    "search",
    [
        lambda r: httpx.Response(429),
        lambda r: httpx.Response(999),
        lambda r: httpx.Response(403),
        _authwall,
        lambda r: httpx.Response(200, text='<form id="captcha-challenge"></form>'),
        lambda r: httpx.Response(401),
        lambda r: httpx.Response(
            503, text=CLOUDFLARE_CHALLENGE, headers={"cf-mitigated": "challenge"}
        ),
        lambda r: httpx.Response(503, text=CLOUDFLARE_CHALLENGE),  # no header: the body
        lambda r: httpx.Response(200, text=CLOUDFLARE_CHALLENGE),
        lambda r: httpx.Response(200, text="<html><body><p>One moment please</p></body></html>"),
    ],
    ids=[
        "429",
        "999",
        "403",
        "authwall-redirect",
        "captcha-page",
        "401",
        "cloudflare-503",
        "cloudflare-503-body-only",
        "cloudflare-200-interstitial",
        "200-page-with-no-cards",
    ],
)
def test_a_block_stops_the_run_and_the_day(
    conn: sqlite3.Connection, contact: str, search: Callable[[httpx.Request], httpx.Response]
) -> None:
    li = LinkedIn(search=search)
    ats = Ats()
    rep = run(conn, li, ats=ats)
    assert rep.status == "blocked" and rep.blocked
    assert len(li.requests) == 1, "nothing after the block"
    assert ats.requests == [] and not events(conn, lg.EV_SEEN)
    (blocked,) = events(conn, lg.EV_BLOCKED)
    assert blocked["day"] == DAY.isoformat() and blocked["type"] == "search"
    # the rest of the local day: not a single request
    quiet = LinkedIn()
    assert run(conn, quiet, ats=ats).status == "blocked_today"
    assert quiet.requests == []
    # the next calendar day: allowed again
    fresh = LinkedIn()
    assert run(conn, fresh, day=DAY + datetime.timedelta(days=1)).status == "ok"
    assert fresh.count("search") == 6


def test_a_block_on_a_posting_page_stops_everything(conn: sqlite3.Connection, contact: str) -> None:
    misses = card_li("4400000010", "Sales Manager", "Acme Widgets Co") + card_li(
        "4400000011", "Account Executive", "Zzqx Robotics"
    )
    li = LinkedIn(search=misses, details={"4400000010": httpx.Response(999)})
    rep = run(
        conn, li, filters=enabled(max_search_requests_per_day=1, max_detail_requests_per_day=9)
    )
    assert rep.status == "blocked" and li.count("detail") == 1
    assert not conn.execute("SELECT * FROM discovery_queue").fetchall()


def test_a_server_error_stops_the_run_but_not_the_day(
    conn: sqlite3.Connection, contact: str
) -> None:
    li = LinkedIn(search=lambda r: httpx.Response(503))
    rep = run(conn, li)
    assert rep.status == "error" and len(li.requests) == 1
    assert not events(conn, lg.EV_BLOCKED) and events(conn, lg.EV_ERROR)
    again = LinkedIn()
    assert run(conn, again).status == "ok" and again.count("search") == 5  # 1 of 6 already used


def test_an_empty_search_page_is_no_results_not_a_block(
    conn: sqlite3.Connection, contact: str
) -> None:
    li = LinkedIn(search="\n")
    rep = run(conn, li)
    assert rep.status == "ok" and not rep.blocked and rep.cards == 0
    assert li.count("search") == 6
    assert not events(conn, lg.EV_BLOCKED)


# ---------------------------------------------------------------------------
# one run at a time: overlapping runs (nightly + linkedin-discover)
# ---------------------------------------------------------------------------


def db_file(conn: sqlite3.Connection) -> str:
    return str(
        conn.execute("SELECT file FROM pragma_database_list WHERE name = 'main'").fetchone()[0]
    )


def test_a_block_another_run_records_stops_this_run_before_its_next_request(
    conn: sqlite3.Connection, contact: str
) -> None:
    """The block is read again before every request, not only at the start:
    a block another process records mid-run stops this run at once."""
    other = sqlite3.connect(db_file(conn))  # another process, same database

    def search(request: httpx.Request) -> httpx.Response:
        if len(li.requests) == 2:  # the other run gets HTTP 999 meanwhile
            other.execute(
                "INSERT INTO events (kind, detail) VALUES (?, ?)",
                (lg.EV_BLOCKED, json.dumps({"day": DAY.isoformat(), "reason": "HTTP 999"})),
            )
            other.commit()
        return httpx.Response(200, text=ALL_CARDS)

    li = LinkedIn(search=search)
    ats = Ats()
    rep = run(conn, li, ats=ats)
    other.close()
    assert len(li.requests) == 2, "no request after the other run's block"
    assert rep.status == "blocked" and "HTTP 999" in rep.blocked
    assert ats.requests == [] and not events(conn, lg.EV_SEEN)
    assert len(events(conn, lg.EV_REQUEST)) == 2
    assert len(events(conn, lg.EV_BLOCKED)) == 1  # the other run's; none added
    assert run(conn, LinkedIn(), ats=ats).status == "blocked_today"


def test_a_second_run_is_busy_while_one_holds_the_lock(
    conn: sqlite3.Connection, contact: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nightly in its linkedin-guest stage holds the run lock; a
    `jobkit linkedin-discover` started meanwhile sends nothing."""
    monkeypatch.setattr(lg, "LOCK_WAIT_SECONDS", 0.0)
    assert lg.running_now(conn) is None
    li = LinkedIn()
    with lg.run_lock(conn) as holder:  # the first run (a separate descriptor)
        assert holder is None
        assert lg.running_now(conn) == f"pid {os.getpid()}"
        rep = run(conn, li)
        assert rep.status == "busy" and "in progress" in rep.note
        assert str(rep).startswith("linkedin guest: busy")
        dry = lg.run(conn, enabled(), dry_run=True, transport=li.transport, today=DAY)
        assert "running now" in dry.render()
    assert li.requests == [] and not events(conn, lg.EV_REQUEST)
    assert [e["status"] for e in events(conn, lg.EV_RUN)] == ["busy"]
    assert lg.running_now(conn) is None  # released with the descriptor
    assert run(conn, li).status == "ok" and li.count("search") == 6
    assert lg.running_now(conn) is None  # the run released it too


def test_the_lock_is_one_file_per_database_whatever_the_path(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    link = tmp_path / "via-symlink"
    link.symlink_to(Path(db_file(conn)).parent, target_is_directory=True)
    same = sqlite3.connect(link / Path(db_file(conn)).name)
    same.row_factory = sqlite3.Row
    try:
        assert lg.lock_path(same) == lg.lock_path(conn)
        assert lg.lock_path(conn).name == "test.db" + lg.LOCK_SUFFIX
    finally:
        same.close()


def test_the_delay_holds_across_runs(conn: sqlite3.Connection, contact: str) -> None:
    """The first request of a run waits out what is left of the delay since
    the last request of any run; a request an hour old needs no wait."""
    conn.execute(
        "INSERT INTO events (kind, detail, at) VALUES (?, ?, datetime('now', '-1 hour'))",
        (lg.EV_REQUEST, json.dumps({"day": "2026-10-11", "type": "search"})),
    )
    conn.commit()
    first: list[float] = []
    li = LinkedIn(search=lambda r: httpx.Response(503))  # one request, then it stops
    run(conn, li, sleeps=first)
    assert first == [] and len(li.requests) == 1
    second: list[float] = []
    again = LinkedIn()
    run(conn, again, sleeps=second)
    assert len(second) == len(again.requests) == 5  # one wait before every request
    # rand 0.5 -> a 4.5 s delay; the last request was 0-1 s ago (whole seconds)
    assert 4.4 <= second[0] <= 5.6
    assert all(3 <= s <= 6 for s in second[1:])


# ---------------------------------------------------------------------------
# the add-url path it shares, the stage, the digest, the MCP tool
# ---------------------------------------------------------------------------


def test_add_url_path_polls_one_board_and_keeps_the_tier(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO companies (id, name, normalized_name, tier) "
        "VALUES (1, 'Anthropic', 'anthropic', 1)"
    )
    conn.execute(
        "INSERT INTO boards (company_id, ats, token) VALUES (1, 'greenhouse', 'anthropic')"
    )
    conn.execute(
        "INSERT INTO companies (id, name, normalized_name, tier) VALUES (2, 'Other', 'other', 2)"
    )
    conn.execute("INSERT INTO boards (company_id, ats, token) VALUES (2, 'lever', 'other')")
    conn.commit()
    ats = Ats()
    res = asyncio.run(
        ingest.ingest_url(
            conn,
            f"https://job-boards.greenhouse.io/anthropic/jobs/{GH_JOB_ID}",
            transport=ats.transport,
        )
    )
    assert res.status == "job_found" and res.job is not None
    assert [r.url.path for r in ats.requests] == ["/v1/boards/anthropic/jobs"]
    assert conn.execute("SELECT tier FROM companies WHERE id = 1").fetchone()[0] == 1
    assert res.poll is not None and not res.poll.board_added
    manual = asyncio.run(ingest.ingest_url(conn, "https://acme.wd5.myworkdayjobs.com/x/job/1"))
    assert manual.status == "manual_ats" and manual.poll is None


def test_stage_runs_after_feeds_before_discovery() -> None:
    from jobkit import cli

    names = [n for n, _ in cli._run_all_stages()]
    assert names.index("linkedin-guest") == names.index("feeds") + 1
    assert names.index("discovery") == names.index("linkedin-guest") + 1


def test_stage_is_a_no_op_while_disabled(conn: sqlite3.Connection) -> None:
    from jobkit import cli

    stage = dict(cli._run_all_stages())["linkedin-guest"]
    assert str(stage(conn)).startswith("linkedin guest: disabled")


def test_digest_line(conn: sqlite3.Connection, contact: str) -> None:
    assert lg.digest_lines(conn, load_filters().linkedin_guest, DAY.isoformat()) == []
    run(conn, LinkedIn())
    lines = lg.digest_lines(conn, enabled().linkedin_guest, DAY.isoformat())
    assert lines[1] == (
        "LinkedIn today: searched 6 (cap 6), new via ATS 0, companies queued for board "
        "discovery 2, blocked: no."
    )
    run(conn, LinkedIn(search=lambda r: httpx.Response(999)), day=DAY + datetime.timedelta(days=1))
    nxt = lg.digest_lines(
        conn, enabled().linkedin_guest, (DAY + datetime.timedelta(days=1)).isoformat()
    )
    assert "blocked: HTTP 999" in nxt[1]


def test_mcp_tool(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch, spawned: Any
) -> None:
    from jobkit import mcp_server

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    plan = mcp_server.linkedin_discover()
    assert "enabled: NO" in plan and "confirm=true" in plan and spawned.calls == []
    assert "not started" in mcp_server.linkedin_discover(confirm=True) and spawned.calls == []
    path = jobkit_home / "config" / "filters.yaml"
    cfg = yaml.safe_load(path.read_text())
    cfg["feeds"]["linkedin_guest"]["enabled"] = True
    path.write_text(yaml.safe_dump(cfg))
    out = mcp_server.linkedin_discover(confirm=True)
    assert "linkedin-discover" in out
    assert spawned.calls and spawned.calls[0][-1] == "linkedin-discover"


def test_mcp_tool_is_busy_during_any_run_that_includes_linkedin(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch, spawned: Any
) -> None:
    """linkedin_discover(confirm=true) starts nothing while a LinkedIn run
    holds the lock (whoever started it), while a nightly is in progress (a
    scheduled one is not a launch from here), or while a launched nightly or
    run-all is still running: each has a linkedin-guest stage."""
    from jobkit import mcp_server

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    path = jobkit_home / "config" / "filters.yaml"
    cfg = yaml.safe_load(path.read_text())
    cfg["feeds"]["linkedin_guest"]["enabled"] = True
    path.write_text(yaml.safe_dump(cfg))
    me = os.getpid()  # a live pid for the records below

    with lg.run_lock(conn):
        out = mcp_server.linkedin_discover(confirm=True)
    assert out.startswith("not started: a LinkedIn guest run is in progress"), out

    start = {"run": "n1", "mode": "nightly", "pid": me, "stages": ["linkedin-guest"]}
    conn.execute(
        "INSERT INTO events (kind, detail) VALUES ('run_all.start', ?)", (json.dumps(start),)
    )
    conn.commit()
    out = mcp_server.linkedin_discover(confirm=True)
    assert out.startswith("not started: the nightly run is in progress (run n1"), out
    done = {"run": "n1", "mode": "nightly", "stages": 1, "failed": []}
    conn.execute(
        "INSERT INTO events (kind, detail) VALUES ('run_all.done', ?)", (json.dumps(done),)
    )

    for args in (["run-all"], ["nightly"]):
        launch = {"args": args, "pid": me, "log": str(jobkit_home / "logs" / "spawn" / "x.log")}
        conn.execute("INSERT INTO events (kind, detail) VALUES ('spawn', ?)", (json.dumps(launch),))
        conn.commit()
        out = mcp_server.linkedin_discover(confirm=True)
        assert out.startswith(f"not started: `jobkit {args[0]}` is still running"), out
    assert spawned.calls == []
