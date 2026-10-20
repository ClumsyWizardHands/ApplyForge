"""M1b acceptance: a job on both a direct ATS feed and an aggregator produces
exactly one row — ATS canonical, both recorded in sources. Offline fixtures only."""

from __future__ import annotations

import sqlite3

import pytest

from jobkit.adapters import ADAPTERS
from jobkit.config import Board, Company, Filters, Targets
from jobkit.models import NormalizedJob
from jobkit.pipeline.feeds import ingest_feed_jobs
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
