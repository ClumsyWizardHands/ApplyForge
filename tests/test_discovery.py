"""Discovery expansion: URL resolver, registry lookup, new-feed parsing. Offline."""

from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock

from jobkit.adapters.feeds.base import SearchSpec
from jobkit.adapters.feeds.cosnetwork import CosNetworkFeed
from jobkit.adapters.feeds.getro import GetroFeed
from jobkit.discovery.registry import lookup, resolve_discovery_queue
from jobkit.discovery.url_resolver import resolve
from tests.conftest import FIXTURES


def test_url_resolver_supported_ats() -> None:
    r = resolve("https://boards.greenhouse.io/anthropic/jobs/12345")
    assert r and (r.ats, r.token, r.external_id, r.supported) == (
        "greenhouse",
        "anthropic",
        "12345",
        True,
    )
    r = resolve("https://job-boards.greenhouse.io/stripe/jobs/999?utm=x")
    assert r and r.token == "stripe" and r.external_id == "999"
    r = resolve("https://jobs.lever.co/palantir/0f5f0b2a-1234-4c3d-9e8f-aaaaaaaaaaaa/apply")
    assert r and r.ats == "lever" and r.external_id.startswith("0f5f0b2a")
    r = resolve("https://jobs.ashbyhq.com/ramp")
    assert r and r.ats == "ashby" and r.token == "ramp" and r.external_id == ""


def test_url_resolver_manual_path_ats() -> None:
    r = resolve("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/x/123")
    assert r and r.ats == "workday" and not r.supported and "manual" in r.note
    r = resolve("https://careers-acme.icims.com/jobs/123/login")
    assert r and r.ats == "icims" and not r.supported
    assert resolve("https://example.com/some/random/page") is None


def test_registry_lookup_real_data() -> None:
    # freehire registries ship in-repo; 1-800 Contacts is greenhouse's first entry
    assert lookup("1-800 Contacts") == ("greenhouse", "1800contacts")
    assert lookup("company-that-does-not-exist-xyz") is None


def test_resolve_discovery_queue(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO discovery_queue (company_name, normalized_name, source) "
        "VALUES ('1-800 Contacts', '1800 contacts', 'remotive')"
    )
    conn.execute(
        "INSERT INTO discovery_queue (company_name, normalized_name, source) "
        "VALUES ('Nonexistent Co XYZ', 'nonexistent xyz', 'remotive')"
    )
    conn.commit()
    stats = resolve_discovery_queue(conn)
    assert stats["found"] == 1 and stats["not_found"] == 1
    board = conn.execute("SELECT ats, token FROM boards WHERE token='1800contacts'").fetchone()
    assert board and board["ats"] == "greenhouse"


def _client_json(payload: object, text: str = "") -> MagicMock:
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    resp.text = text
    resp.raise_for_status.return_value = None
    client.get = AsyncMock(return_value=resp)
    client.post = AsyncMock(return_value=resp)
    return client


def test_getro_fixture_parses() -> None:
    import json

    payload = json.loads((FIXTURES / "feed_getro.json").read_text())
    jobs = asyncio.run(
        GetroFeed().search(_client_json(payload), SearchSpec(keywords=("revenue operations",)))
    )
    assert jobs, "getro fixture produced no jobs"
    j = jobs[0]
    assert j.title and j.company_name and j.apply_url.startswith("http")


def test_cosnetwork_fixture_parses() -> None:
    xml = (FIXTURES / "feed_cosnetwork.xml").read_text()
    jobs = asyncio.run(CosNetworkFeed().search(_client_json(None, text=xml), SearchSpec()))
    assert len(jobs) == 5
    assert all(j.title and j.apply_url for j in jobs)
