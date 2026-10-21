"""M1b acceptance: a job on both a direct ATS feed and an aggregator produces
exactly one row — ATS canonical, both recorded in sources. Offline fixtures only."""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from jobkit.adapters import ADAPTERS
from jobkit.adapters.feeds import FEEDS
from jobkit.config import Board, Company, Filters, Targets
from jobkit.models import NormalizedJob
from jobkit.pipeline.feeds import _spec_for, ingest_feed_jobs
from jobkit.pipeline.poll import seed_targets, upsert_jobs
from tests.conftest import load_fixture

PERMISSIVE = Filters(raw={"filters": {"include_titles": [], "exclude_titles": []}})


def _ingest_greenhouse(conn: sqlite3.Connection) -> tuple[dict, list[NormalizedJob]]:
    targets = Targets(
        companies=[
            Company(
                name="Anthropic",
                domain="anthropic.com",
                tier=1,
                board=Board(ats="greenhouse", token="anthropic"),
            )
        ]
    )
    board = seed_targets(conn, targets)[0]
    payload = load_fixture("greenhouse_anthropic.json")
    upsert_jobs(conn, board, payload)
    conn.commit()
    return board, ADAPTERS["greenhouse"].parse(payload)


def test_cross_source_dedup_ats_canonical(conn: sqlite3.Connection) -> None:
    _, gh_jobs = _ingest_greenhouse(conn)
    target = gh_jobs[0]
    # the same posting arrives from an aggregator (different id/url, same identity)
    agg = NormalizedJob(
        ats="remotive",
        board_token="",
        external_id="agg-123",
        title=target.title,
        company_name="Anthropic",
        location_raw=target.location_raw,
        remote=target.remote,
        jd_html=target.jd_html,
        apply_url="https://remotive.com/j/agg-123",
        posted_at=None,
    )
    stats = ingest_feed_jobs(conn, "remotive", [agg], PERMISSIVE)
    assert stats["merged"] == 1 and stats["new"] == 0
    rows = conn.execute(
        "SELECT id FROM jobs WHERE normalized_company='anthropic' AND title=?", (target.title,)
    ).fetchall()
    assert len(rows) == 1, "exactly one row for the same posting"
    sources = conn.execute(
        "SELECT source, is_canonical FROM sources WHERE job_id=? ORDER BY source", (rows[0]["id"],)
    ).fetchall()
    by = {r["source"]: r["is_canonical"] for r in sources}
    assert by == {"greenhouse": 1, "remotive": 0}, "ATS canonical, aggregator not"


def test_new_company_discovered_and_queued(conn: sqlite3.Connection) -> None:
    agg = NormalizedJob(
        ats="arbeitnow",
        board_token="",
        external_id="xyz-1",
        title="Director of Sales",
        company_name="Undiscovered Rail Co",
        location_raw="Remote, US",
        remote=True,
        jd_html="x" * 300,
        apply_url="https://example.com/j/1",
        posted_at=None,
    )
    stats = ingest_feed_jobs(conn, "arbeitnow", [agg], PERMISSIVE)
    assert stats["new"] == 1
    dq = conn.execute("SELECT * FROM discovery_queue").fetchall()
    assert len(dq) == 1 and dq[0]["status"] == "pending"
    src = conn.execute("SELECT is_canonical FROM sources WHERE source='arbeitnow'").fetchone()
    assert src["is_canonical"] == 0, "aggregator rows are never canonical"


def test_feed_title_filter_logs_rejections(conn: sqlite3.Connection) -> None:
    filters = Filters(
        raw={
            "filters": {
                "include_titles": [r"(?i)\bdirector\b"],
                "exclude_titles": [r"(?i)\bintern\b"],
            }
        }
    )
    jobs = [
        NormalizedJob(
            ats="remotive",
            board_token="",
            external_id=f"r-{i}",
            title=t,
            company_name="Acme",
            location_raw="Remote",
            remote=True,
            jd_html="x" * 300,
            apply_url=f"https://x.com/{i}",
            posted_at=None,
        )
        for i, t in enumerate(["Sales Intern", "Director of Sales", "Barista"])
    ]
    stats = ingest_feed_jobs(conn, "remotive", jobs, filters)
    assert stats == {"seen": 3, "rejected": 2, "merged": 0, "new": 1}
    reasons = [
        r["reason"] for r in conn.execute("SELECT reason FROM rejections ORDER BY id").fetchall()
    ]
    assert any("exclude_title" in r for r in reasons)
    assert any(r == "no_include_title_match" for r in reasons)


def test_feed_adapters_parse_fixtures(conn: sqlite3.Connection) -> None:
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from jobkit.adapters.feeds import FEEDS
    from jobkit.adapters.feeds.base import SearchSpec

    fixture_by_source = {
        "arbeitnow": "feed_arbeitnow.json",
        "remotive": "feed_remotive.json",
        "remoteok": "feed_remoteok.json",
        "himalayas": "feed_himalayas.json",
    }
    for source, fixture in fixture_by_source.items():
        payload = load_fixture(fixture)
        client = MagicMock()
        resp = MagicMock()
        resp.json.return_value = payload
        resp.raise_for_status.return_value = None
        client.get = AsyncMock(return_value=resp)
        jobs = asyncio.run(FEEDS[source].search(client, SearchSpec()))
        assert jobs, f"{source} fixture produced no jobs"
        for j in jobs:
            assert j.title and j.company_name is not None and j.apply_url


def test_usajobs_searches_only_what_the_config_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """No occupational series is built in: title keywords alone unless
    filters.yaml names series codes (and, optionally, a minimum grade)."""
    import asyncio

    import httpx

    from jobkit.adapters.feeds.usajobs import USAJobsFeed
    from jobkit.pipeline.feeds import _spec_for

    monkeypatch.setenv("USAJOBS_API_KEY", "test-key")
    monkeypatch.setenv("USAJOBS_USER_AGENT_EMAIL", "someone@example.com")
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"SearchResult": {"SearchResultItems": []}})

    async def run(cfg: dict[str, object]) -> None:
        spec = _spec_for("usajobs", Filters(raw={"feeds": {"usajobs": cfg}}))
        assert spec is not None
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await USAJobsFeed().search(client, spec)

    asyncio.run(run({"enabled": True, "keywords": ["Program Analyst"]}))
    assert seen == [{"PositionTitle": "Program Analyst", "ResultsPerPage": "500"}]
    seen.clear()
    cfg = {"enabled": True, "series": ["1101", "0343"], "pay_grade_low": 12, "remote_only": True}
    asyncio.run(run(cfg))
    assert seen == [
        {
            "JobCategoryCode": "1101;0343",
            "SortField": "opendate",
            "SortDirection": "desc",
            "ResultsPerPage": "500",
            "RemoteIndicator": "True",
            "PayGradeLow": "12",
        }
    ]


# ---------------------------------------------------------------------------
# Location-aware Adzuna and USAJOBS, missing-key detection (package P4).
# Every request goes to an httpx.MockTransport: nothing reaches the network.
# tests/fixtures/feeds/ holds hand-built payloads in the APIs' documented
# shapes and the request golden; neither is a live recording.
# ---------------------------------------------------------------------------

FEED_FIX = Path(__file__).parent / "fixtures" / "feeds"
GOLDEN = json.loads((FEED_FIX / "golden_requests.json").read_text())
FEED_KEYS = {
    "ADZUNA_APP_ID": "test-id",
    "ADZUNA_APP_KEY": "test-key",
    "USAJOBS_API_KEY": "test-key",
    "USAJOBS_USER_AGENT_EMAIL": "someone@example.com",
}
EMPTY_REPLY = {
    "adzuna": {"results": []},
    "usajobs": {"SearchResult": {"SearchResultItems": []}},
}


@pytest.fixture()
def feed_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in FEED_KEYS.items():
        monkeypatch.setenv(name, value)


def _requests_for(
    source: str, filters: Filters, reply: object | None = None
) -> tuple[list[httpx.Request], list[NormalizedJob]]:
    """Run one feed adapter against a MockTransport; return what it sent."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=reply if reply is not None else EMPTY_REPLY[source])

    spec = _spec_for(source, filters)
    assert spec is not None

    async def go() -> list[NormalizedJob]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await FEEDS[source].search(client, spec)

    return sent, asyncio.run(go())


def _shape(request: httpx.Request) -> dict[str, object]:
    """A request as the golden stores it: the URL and the headers that carry
    meaning (httpx's own User-Agent varies with its version)."""
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() in ("host", "user-agent", "authorization-key")
        and not v.startswith("python-httpx")
    }
    return {"url": str(request.url), "headers": headers}


def _logged(home: Path, event: str) -> list[dict[str, object]]:
    path = home / "logs" / "jobkit.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("event") == event]


@pytest.mark.parametrize("case", sorted(GOLDEN))
def test_requests_without_location_keys_are_byte_identical(case: str, feed_keys: None) -> None:
    """The golden was captured from the code before location support, on the
    sales sandbox profile, the template and edge cases (no phrase, the `what`
    alias, 12 phrases against the cap of 10, USAJOBS series). None of those
    configs has a locations key, so every request must match byte for byte."""
    want = GOLDEN[case]
    sent, _ = _requests_for(want["source"], Filters(raw=want["filters"]))
    assert [_shape(r) for r in sent] == want["requests"]


def test_the_sales_sandbox_profile_still_matches_the_golden(feed_keys: None) -> None:
    """The live sandbox config (tests/fixtures/config/filters.yaml) has no
    location keys and `country: us`: its Adzuna requests are the golden's."""
    raw = yaml.safe_load((Path(__file__).parent / "fixtures/config/filters.yaml").read_text())
    want = GOLDEN["adzuna_sales_fixture"]
    if raw.get("feeds", {}).get("adzuna") != want["filters"]["feeds"]["adzuna"]:
        pytest.skip("the sandbox profile's adzuna block changed since the golden was taken")
    sent, _ = _requests_for("adzuna", Filters(raw=raw))
    assert [_shape(r) for r in sent] == want["requests"]


def _adzuna(**cfg: object) -> Filters:
    return Filters(raw={"feeds": {"adzuna": {"enabled": True, **cfg}}})


def test_adzuna_locations_add_where_and_distance(feed_keys: None) -> None:
    filters = _adzuna(
        keywords=["registered nurse", "charge nurse"],
        locations=[{"where": "Columbus, OH", "distance_km": 30}, {"where": "Dayton, OH"}],
    )
    sent, _ = _requests_for("adzuna", filters)
    got = [
        (r.url.params["what_phrase"], r.url.params["where"], r.url.params["distance"]) for r in sent
    ]
    assert got == [
        ("registered nurse", "Columbus, OH", "30"),
        ("registered nurse", "Dayton, OH", "40"),  # distance_km defaults to 40
        ("charge nurse", "Columbus, OH", "30"),
        ("charge nurse", "Dayton, OH", "40"),
    ]
    assert all(r.url.path == "/v1/api/jobs/us/search/1" for r in sent)
    # the documented parameters only, appended after the old ones
    keys = list(sent[0].url.params.keys())
    assert keys[-2:] == ["where", "distance"]
    assert "location0" not in keys


def test_adzuna_calls_are_capped_at_three_places(feed_keys: None, jobkit_home: Path) -> None:
    from jobkit.adapters.feeds.adzuna import MAX_PHRASES, MAX_PLACES

    places = [{"where": f"Town {i}, OH"} for i in range(5)]
    phrases = [f"phrase {i}" for i in range(MAX_PHRASES + 2)]
    sent, _ = _requests_for("adzuna", _adzuna(keywords=phrases, locations=places))
    assert len(sent) == MAX_PHRASES * MAX_PLACES == 30
    assert {r.url.params["where"] for r in sent} == {"Town 0, OH", "Town 1, OH", "Town 2, OH"}
    capped = _logged(jobkit_home, "feeds.adzuna_capped")
    assert {"dropped_places": ["Town 3, OH", "Town 4, OH"]} in [
        {k: v for k, v in c.items() if k.startswith("dropped")} for c in capped
    ]
    assert any(c.get("dropped_phrases") == ["phrase 10", "phrase 11"] for c in capped)


def test_adzuna_country_sets_the_path_and_currency(feed_keys: None) -> None:
    reply = json.loads((FEED_FIX / "adzuna_search.json").read_text())
    sent, jobs = _requests_for("adzuna", _adzuna(keywords=["nurse"], country="gb"), reply)
    assert sent[0].url.path == "/v1/api/jobs/gb/search/1"
    assert jobs[0].comp_currency == "GBP"


def test_a_stale_country_value_falls_back_to_us(feed_keys: None, jobkit_home: Path) -> None:
    """Older templates wrote `country:` and nothing read it, so a value that
    is not a two-letter code must not break the run."""
    sent, _ = _requests_for("adzuna", _adzuna(keywords=["nurse"], country="usa"))
    assert sent[0].url.path == "/v1/api/jobs/us/search/1"
    assert _logged(jobkit_home, "feeds.adzuna_country_ignored")


def test_adzuna_default_phrase_is_logged(feed_keys: None, jobkit_home: Path) -> None:
    sent, _ = _requests_for("adzuna", _adzuna(keywords=[]))
    assert [r.url.params["what_phrase"] for r in sent] == ["account executive"]
    assert _logged(jobkit_home, "feeds.default_keywords_used")


def test_adzuna_fixture_normalizes(feed_keys: None) -> None:
    reply = json.loads((FEED_FIX / "adzuna_search.json").read_text())
    _, jobs = _requests_for("adzuna", _adzuna(keywords=["nurse"]), reply)
    by_id = {j.external_id: j for j in jobs}
    rn = by_id["4900000001"]
    assert (rn.title, rn.company_name, rn.location_raw) == (
        "Registered Nurse - Medical/Surgical",
        "Riverbend Health System",
        "Columbus, Franklin County",
    )
    assert (rn.comp_min, rn.comp_max, rn.comp_currency) == (78000, 96000, "USD")
    assert rn.posted_at == "2026-10-15" and rn.apply_url.startswith("https://www.adzuna.com/")
    assert rn.extra["attribution"] == "Jobs by Adzuna"
    predicted = by_id["4900000002"]  # Adzuna's model guess is never a posted range
    assert (predicted.comp_min, predicted.comp_max, predicted.comp_currency) == (None, None, None)
    assert predicted.extra["salary_predicted"] is True
    remote = by_id["4900000003"]
    assert remote.remote is True and remote.comp_min is None


def test_adzuna_with_places_names_the_state(feed_keys: None) -> None:
    """With places searched, location_raw carries the state from location.area,
    so match_state can tell Dublin, OH from Dublin, CA; without places it is
    the display name alone, as before."""
    from jobkit.pipeline.filter import reject_reason_now

    reply = json.loads((FEED_FIX / "adzuna_search.json").read_text())
    ca = copy.deepcopy(reply["results"][1])
    ca["id"] = "4900000099"
    ca["location"] = {
        "display_name": "Dublin, Alameda County",
        "area": ["US", "California", "Alameda County", "Dublin"],
    }
    reply["results"].append(ca)
    places = [{"where": "Columbus, OH", "distance_km": 40}]
    _, jobs = _requests_for("adzuna", _adzuna(keywords=["nurse"], locations=places), reply)
    by_id = {j.external_id: j for j in jobs}
    assert by_id["4900000002"].location_raw == "Dublin, Franklin County, Ohio"
    assert by_id["4900000099"].location_raw == "Dublin, Alameda County, California"
    assert by_id["4900000003"].location_raw == "Remote"
    filters = Filters(
        raw={"filters": {"locations": {"allowed": ["Dublin, OH"], "match_state": True}}}
    )

    def row(location: str) -> dict[str, Any]:
        return {
            "title": "Registered Nurse",
            "company_name": "Acme",
            "location_raw": location,
            "remote": 0,
            "posted_at": None,
            "comp_min": None,
            "comp_max": None,
            "comp_currency": None,
            "jd_html": "",
        }

    assert reject_reason_now(row(by_id["4900000002"].location_raw), filters) is None
    assert reject_reason_now(row(by_id["4900000099"].location_raw), filters) == (
        "location:other-dublin:other-state"
    )
    _, jobs = _requests_for("adzuna", _adzuna(keywords=["nurse"]), reply)
    by_id = {j.external_id: j for j in jobs}
    assert by_id["4900000099"].location_raw == "Dublin, Alameda County"


def _usajobs(**cfg: object) -> Filters:
    return Filters(raw={"feeds": {"usajobs": {"enabled": True, **cfg}}})


def test_usajobs_locations_add_location_name_and_radius(feed_keys: None) -> None:
    filters = _usajobs(
        keywords=["Clinical Nurse"],
        series=["0610"],
        locations=[{"name": "Columbus, Ohio", "radius_miles": 30}, {"name": "Dayton, Ohio"}],
    )
    sent, _ = _requests_for("usajobs", filters)
    got = [
        (
            r.url.params.get("JobCategoryCode") or r.url.params.get("PositionTitle"),
            r.url.params["LocationName"],
            r.url.params["Radius"],
        )
        for r in sent
    ]
    assert got == [
        ("0610", "Columbus, Ohio", "30"),
        ("0610", "Dayton, Ohio", "25"),  # radius_miles defaults to 25
        ("Clinical Nurse", "Columbus, Ohio", "30"),
        ("Clinical Nurse", "Dayton, Ohio", "25"),
    ]
    assert all(r.headers["Authorization-Key"] == "test-key" for r in sent)


def test_usajobs_fixture_normalizes(feed_keys: None) -> None:
    reply = json.loads((FEED_FIX / "usajobs_search.json").read_text())
    _, jobs = _requests_for("usajobs", _usajobs(keywords=["Nurse"]), reply)
    by_id = {j.external_id: j for j in jobs}
    nurse = by_id["900000101"]
    assert (nurse.title, nurse.company_name, nurse.location_raw) == (
        "Clinical Nurse",
        "Federal Example Health Agency",
        "Columbus, Ohio",
    )
    assert (nurse.comp_min, nurse.comp_max, nurse.comp_currency) == (79000, 118500, "USD")
    assert nurse.apply_url.startswith("https://www.usajobs.gov:443/GetJob/ViewDetails/900000101")
    hourly = by_id["900000102"]  # hourly pay is not stored as an annual range
    assert (hourly.comp_min, hourly.comp_max) == (None, None)
    assert hourly.remote is True and hourly.location_raw == "Remote, US"


class _FakeKeyedFeed:
    source_name = "adzuna"
    requires_key = True
    key_env = ("ADZUNA_APP_ID", "ADZUNA_APP_KEY")

    def __init__(self, jobs: list[NormalizedJob]) -> None:
        self.jobs = jobs
        self.calls = 0

    async def search(self, client: httpx.AsyncClient, query: object) -> list[NormalizedJob]:
        self.calls += 1
        return self.jobs


def _run_feeds_with(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, feed: _FakeKeyedFeed
) -> dict[str, dict[str, int]]:
    from jobkit.pipeline import feeds as feeds_mod

    monkeypatch.setattr(feeds_mod, "FEEDS", {"adzuna": feed})
    filters = Filters(raw={"feeds": {"adzuna": {"enabled": True, "keywords": ["nurse"]}}})
    return asyncio.run(feeds_mod.run_feeds(conn, filters))


def test_a_missing_key_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, jobkit_home: Path
) -> None:
    monkeypatch.setenv("ADZUNA_APP_ID", "test-id")
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)
    feed = _FakeKeyedFeed([])
    results = _run_feeds_with(monkeypatch, conn, feed)
    assert results["adzuna"]["skipped_no_key"] == 1
    assert feed.calls == 0, "no request without the key"
    (skip,) = _logged(jobkit_home, "feeds.skipped_no_key")
    assert skip["missing"] == ["ADZUNA_APP_KEY"]


def test_a_keyed_feed_with_no_results_is_not_reported_as_missing_a_key(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, jobkit_home: Path, feed_keys: None
) -> None:
    feed = _FakeKeyedFeed([])
    results = _run_feeds_with(monkeypatch, conn, feed)
    assert feed.calls == 1
    assert "skipped_no_key" not in results["adzuna"] and results["adzuna"]["empty"] == 1
    assert _logged(jobkit_home, "feeds.empty") and not _logged(jobkit_home, "feeds.skipped_no_key")


def test_a_keyed_feed_with_results_is_ingested(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection, feed_keys: None
) -> None:
    job = NormalizedJob(
        ats="adzuna",
        board_token="",
        external_id="az-1",
        title="Registered Nurse",
        company_name="Riverbend Health System",
        location_raw="Columbus, OH",
        remote=None,
        jd_html="x" * 300,
        apply_url="https://www.adzuna.com/land/ad/1",
        posted_at=None,
    )
    results = _run_feeds_with(monkeypatch, conn, _FakeKeyedFeed([job]))
    assert results["adzuna"] == {"seen": 1, "rejected": 0, "merged": 0, "new": 1}


def test_missing_keys_helper() -> None:
    from jobkit.adapters.feeds.base import missing_keys

    assert missing_keys(FEEDS["remotive"]) == []  # keyless feeds need nothing
    assert FEEDS["adzuna"].requires_key and FEEDS["usajobs"].requires_key


def test_usajobs_description_carries_duties_and_qualifications(feed_keys: None) -> None:
    """The summary alone is too short to score; the duties, qualifications,
    requirements and education in the search result make the description."""
    from jobkit.normalize import jd_is_missing

    reply = json.loads((FEED_FIX / "usajobs_search.json").read_text())
    _, jobs = _requests_for("usajobs", _usajobs(keywords=["Nurse"], full_description=True), reply)
    by_id = {j.external_id: j for j in jobs}
    nurse = by_id["900000101"]
    assert nurse.jd_html.startswith("<p>Provide nursing care to patients")
    assert "<h3>Duties</h3><ul><li>Assess, plan" in nurse.jd_html
    assert "<h3>Qualifications</h3>" in nurse.jd_html
    assert "<h3>Education</h3>" in nurse.jd_html
    assert not jd_is_missing(nurse.jd_html)
    # a result with only a summary keeps that summary, unchanged, as before
    summary_only = by_id["900000102"]
    detail = reply["SearchResult"]["SearchResultItems"][1]["MatchedObjectDescriptor"]
    assert summary_only.jd_html == detail["UserArea"]["Details"]["JobSummary"]


def test_usajobs_without_full_description_keeps_the_summary(feed_keys: None) -> None:
    """D9: a config without feeds.usajobs.full_description (one written before
    the key existed) stores the JobSummary alone, as before, and sends the
    same location-free requests."""
    reply = json.loads((FEED_FIX / "usajobs_search.json").read_text())
    sent, jobs = _requests_for("usajobs", _usajobs(keywords=["Nurse"]), reply)
    assert [dict(r.url.params) for r in sent] == [
        {"PositionTitle": "Nurse", "ResultsPerPage": "500"}
    ]
    by_id = {j.external_id: j for j in jobs}
    assert by_id["900000101"].jd_html == (
        "Provide nursing care to patients in an outpatient clinic."
    )
    assert by_id["900000101"].location_raw == "Columbus, Ohio"


def _multi_site_reply() -> dict[str, Any]:
    reply = json.loads((FEED_FIX / "usajobs_search.json").read_text())
    item = reply["SearchResult"]["SearchResultItems"][0]["MatchedObjectDescriptor"]
    item["PositionLocationDisplay"] = "Multiple Locations"
    item["PositionLocation"] = [
        {"LocationName": "Chillicothe, Ohio"},
        {"LocationName": "Columbus, Ohio"},
        {"LocationName": "Dayton, Ohio"},
    ]
    assert isinstance(reply, dict)
    return reply


def test_usajobs_multiple_locations_names_the_searched_site(feed_keys: None) -> None:
    """A radius search around Columbus returns a VA posting shown as "Multiple
    Locations"; its Columbus site becomes location_raw, so the location filter
    sees a city instead of rejecting other-multiplelocations."""
    places = [{"name": "Columbus, Ohio", "radius_miles": 25}]
    _, jobs = _requests_for(
        "usajobs", _usajobs(keywords=["Nurse"], locations=places), _multi_site_reply()
    )
    assert {j.external_id: j for j in jobs}["900000101"].location_raw == "Columbus, Ohio"
    # no site in the searched city: one in the same state
    _, jobs = _requests_for(
        "usajobs",
        _usajobs(keywords=["Nurse"], locations=[{"name": "Toledo, Ohio"}]),
        _multi_site_reply(),
    )
    assert {j.external_id: j for j in jobs}["900000101"].location_raw == "Chillicothe, Ohio"
    # without searched places the display text is kept, as before
    _, jobs = _requests_for("usajobs", _usajobs(keywords=["Nurse"]), _multi_site_reply())
    assert {j.external_id: j for j in jobs}["900000101"].location_raw == "Multiple Locations"


def test_usajobs_same_state_reads_a_code_and_a_name_alike(feed_keys: None) -> None:
    """A place searched as 'Columbus, OH' and a site listed as 'Dayton, Ohio'
    are in the same state: the Ohio site is named, not the first site."""
    reply = _multi_site_reply()
    item = reply["SearchResult"]["SearchResultItems"][0]["MatchedObjectDescriptor"]
    item["PositionLocation"] = [
        {"LocationName": "Indianapolis, Indiana"},
        {"LocationName": "Dayton, Ohio"},
    ]
    _, jobs = _requests_for(
        "usajobs", _usajobs(keywords=["Nurse"], locations=[{"name": "Columbus, OH"}]), reply
    )
    assert {j.external_id: j for j in jobs}["900000101"].location_raw == "Dayton, Ohio"
