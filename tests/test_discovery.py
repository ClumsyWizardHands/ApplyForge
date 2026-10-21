"""Discovery expansion: URL resolver, registry lookup, new-feed parsing. Offline."""

from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from jobkit.adapters.feeds.base import SearchSpec
from jobkit.adapters.feeds.cosnetwork import CosNetworkFeed
from jobkit.adapters.feeds.getro import GetroFeed
from jobkit.discovery import registry as registry_mod
from jobkit.discovery.registry import lookup, resolve_discovery_queue
from jobkit.discovery.url_resolver import (
    KNOWN_SYSTEMS,
    UNSUPPORTED_ATS,
    resolve,
    resolve_for_board_matching,
)
from jobkit.normalize import normalize_company
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
    assert r and r.ats == "workday" and not r.supported and r.system == "Workday"
    assert r.note.startswith("Workday posting: ")
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


# ---------------------------------------------------------------------------
# Hiring systems jobkit recognises but does not read (package P4). One URL per
# system, in the shape the system publishes. Recognition only: nothing is
# fetched (Hard Rules 9 and 11).
# ---------------------------------------------------------------------------

UNSUPPORTED_URLS: list[tuple[str, str, str, str, str]] = [
    # (url, ats, system, token, external_id)
    (
        "https://wd5.myworkdaysite.com/en-US/recruiting/initech/External/job/Columbus/RN_R1",
        "workday",
        "Workday",
        "initech",
        "",
    ),
    (
        "https://jobs-initech.icims.com/jobs/4521/registered-nurse/job",
        "icims",
        "iCIMS",
        "initech",
        "4521",
    ),
    (
        "https://initech.taleo.net/careersection/2/jobdetail.ftl?job=2600123&lang=en",
        "taleo",
        "Taleo",
        "initech",
        "2600123",
    ),
    (
        "https://eabc.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/5512",
        "oracle_hcm",
        "Oracle Cloud HCM",
        "eabc",
        "5512",
    ),
    (
        "https://career4.successfactors.com/career?company=initechP&career_ns=job_listing",
        "successfactors",
        "SAP SuccessFactors",
        "initechP",
        "",
    ),
    (
        "https://recruiting.ultipro.com/INI1000INIT/JobBoard/0f1e2d3c/OpportunityDetail?opportunityId=1",
        "ukg",
        "UKG (UltiPro)",
        "INI1000INIT",
        "",
    ),
    (
        "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=abc",
        "adp",
        "ADP",
        "",
        "",
    ),
    (
        "https://recruiting.paylocity.com/Recruiting/Jobs/Details/1834567",
        "paylocity",
        "Paylocity",
        "",
        "1834567",
    ),
    ("https://initech.bamboohr.com/careers/112", "bamboohr", "BambooHR", "initech", "112"),
    ("https://jobs.jobvite.com/initech/job/oAbC1fwX", "jobvite", "Jobvite", "initech", "oAbC1fwX"),
    (
        "https://initech.applytojob.com/apply/Xy12AbCd/Registered-Nurse",
        "jazzhr",
        "JazzHR",
        "initech",
        "Xy12AbCd",
    ),
    (
        "https://initech.breezy.hr/p/9a8b7c6d5e4f-charge-nurse",
        "breezy",
        "Breezy HR",
        "initech",
        "9a8b7c6d5e4f",
    ),
    (
        "https://www.governmentjobs.com/careers/franklincounty/jobs/4790123/public-health-nurse",
        "neogov",
        "NEOGOV",
        "franklincounty",
        "4790123",
    ),
    (
        "https://www.schooljobs.com/careers/exampleschools/jobs/3311/teacher",
        "neogov",
        "NEOGOV",
        "exampleschools",
        "3311",
    ),
    (
        "https://www.applitrack.com/exampleschools/onlineapp/default.aspx?AppliTrackJobId=88",
        "applitrack",
        "Frontline AppliTrack",
        "exampleschools",
        "",
    ),
    (
        "https://www.schoolspring.com/jobs?jobid=4410021",
        "schoolspring",
        "SchoolSpring",
        "",
        "4410021",
    ),
    (
        "https://www.usajobs.gov:443/GetJob/ViewDetails/900000101?PostingChannelID=x",
        "usajobs",
        "USAJOBS",
        "",
        "900000101",
    ),
    ("https://www.usajobs.gov/job/900000102", "usajobs", "USAJOBS", "", "900000102"),
]


@pytest.mark.parametrize(("url", "ats", "system", "token", "external_id"), UNSUPPORTED_URLS)
def test_url_resolver_names_unsupported_systems(
    url: str, ats: str, system: str, token: str, external_id: str
) -> None:
    r = resolve(url)
    assert r is not None, url
    assert (r.ats, r.system, r.token, r.external_id, r.supported) == (
        ats,
        system,
        token,
        external_id,
        False,
    )
    assert r.note.startswith(f"{system} posting: ")
    assert "Greenhouse, Lever and Ashby" in r.note
    assert ats in UNSUPPORTED_ATS and ats in KNOWN_SYSTEMS


def test_a_system_name_outside_the_host_is_not_a_match() -> None:
    for url in (
        "https://remotive.com/remote-jobs/taleo.net-administrator-123",
        "https://example.com/careers?via=jobs.jobvite.com",
        "https://www.adzuna.com/land/ad/1?src=usajobs.gov",
        "https://www.bamboohr.com/pricing",  # the vendor's own site, not a job board
    ):
        assert resolve(url) is None, url


# The answer `resolve` gave before the newer systems existed, for the URLs the
# board resolver and the other callers see most. Supported shapes keep their
# token and id; the original unsupported ones keep their ats and token.
BEFORE: list[tuple[str, tuple[str, str, str, bool] | None]] = [
    (
        "https://boards.greenhouse.io/tyrell/jobs/5102000309",
        ("greenhouse", "tyrell", "5102000309", True),
    ),
    ("https://boards.greenhouse.io/embed/job_app?for=tyrell", ("greenhouse", "tyrell", "", True)),
    ("https://greenhouse.io/embed/job_board?for=tyrell", ("greenhouse", "tyrell", "", True)),
    (
        "https://jobs.lever.co/soylent_ai/0f5f0b2a-1234-4c3d-9e8f-aaaaaaaaaaaa",
        ("lever", "soylent_ai", "0f5f0b2a-1234-4c3d-9e8f-aaaaaaaaaaaa", True),
    ),
    ("https://jobs.ashbyhq.com/cyberdyne-careers", ("ashby", "cyberdyne-careers", "", True)),
    ("https://acme.wd1.myworkdayjobs.com/External/job/x", ("workday", "acme", "", False)),
    ("https://jobs.smartrecruiters.com/Initech/7430", ("smartrecruiters", "Initech", "", False)),
    ("https://careers-initech.icims.com/jobs/1/x/job", ("icims", "initech", "", False)),
    ("https://uscareers-initech.icims.com/jobs/1/x/job", ("icims", "initech", "", False)),
    ("https://apply.workable.com/initech/j/ABC/", ("workable", "initech", "", False)),
    ("https://initech.recruitee.com/o/nurse", ("recruitee", "initech", "", False)),
    ("https://ats.rippling.com/initech/jobs/1", ("rippling", "initech", "", False)),
    ("https://www.adzuna.com/land/ad/4900000001", None),
    ("https://himalayas.app/companies/tyrell/jobs/x", None),
]


@pytest.mark.parametrize(("url", "want"), BEFORE)
def test_urls_resolved_before_resolve_the_same(
    url: str, want: tuple[str, str, str, bool] | None
) -> None:
    r = resolve(url)
    got = None if r is None else (r.ats, r.token, r.external_id, r.supported)
    assert got == want
    pinned = resolve_for_board_matching(url)
    assert (None if pinned is None else (pinned.ats, pinned.token, pinned.external_id)) == (
        None if want is None else want[:3]
    )


@pytest.mark.parametrize(("url", "ats", "system", "token", "external_id"), UNSUPPORTED_URLS)
def test_board_matching_ignores_the_newly_recognised_systems(
    url: str, ats: str, system: str, token: str, external_id: str
) -> None:
    """The board resolver skips a row whose apply URL is on a system it does
    not poll. Its skip is pinned to the systems it skipped on before, so a
    row on any newly recognised system is still name-matched."""
    assert resolve_for_board_matching(url) is None


@pytest.mark.parametrize(
    "apply_url",
    [
        "https://jobs.jobvite.com/tyrell/job/oAbC1fwX",
        "https://tyrell.taleo.net/careersection/2/jobdetail.ftl?job=1",
        "https://jobs-tyrell.icims.com/jobs/4521/x/job",
        "https://tyrell.bamboohr.com/careers/112",
        "https://career4.successfactors.com/career?company=tyrell",
        "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=1",
        "https://www.usajobs.gov:443/GetJob/ViewDetails/900000101",
    ],
)
def test_board_resolver_still_name_matches_rows_on_newly_recognised_systems(
    apply_url: str, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Companies change ATS: an aggregator row whose apply link is on Taleo,
    Jobvite and the rest is still matched by company name to the employer's
    Greenhouse board, exactly as when the link was unrecognised."""
    from tests import test_board_resolver as tbr

    monkeypatch.setattr(
        registry_mod,
        "lookup",
        lambda name: tbr.FICTIONAL_REGISTRY.get(normalize_company(name)),
    )
    fid = tbr.feed_row(
        conn,
        "Tyrell",
        tbr.TYRELL_TITLE,
        "United States",
        source="himalayas",
        remote=True,
        apply_url=apply_url,
    )
    rep = tbr.run(conn, tbr.Boards())
    target = tbr.ats_row(conn, "greenhouse", "tyrell", tbr.TYRELL_ENABLEMENT)
    assert target is not None and rep.merged_into == {fid: int(target["id"])}
    assert not any("which jobkit does not poll" in line for line in rep.lines)


def test_board_resolver_still_skips_the_systems_it_skipped_before(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests import test_board_resolver as tbr

    monkeypatch.setattr(
        registry_mod,
        "lookup",
        lambda name: tbr.FICTIONAL_REGISTRY.get(normalize_company(name)),
    )
    boards = tbr.Boards()
    tbr.feed_row(
        conn,
        "Tyrell",
        tbr.TYRELL_TITLE,
        "United States",
        source="himalayas",
        remote=True,
        apply_url="https://tyrell.wd1.myworkdayjobs.com/External/job/x",
    )
    rep = tbr.run(conn, boards)
    assert rep.merged_into == {} and boards.requests == []
    assert any("its apply URL is on workday" in line for line in rep.lines)


def test_add_url_on_an_unsupported_system_names_it_and_adds_nothing(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Taleo link is recognised, not polled: add-url exits 0, names the
    system, tells the person to apply on the employer's site, and adds no job.
    The note promises no in-app tracking (no command adds such a job yet)."""
    from typer.testing import CliRunner

    from jobkit import cli

    monkeypatch.setattr(cli, "_db", lambda: conn)
    url = "https://initech.taleo.net/careersection/2/jobdetail.ftl?job=2600123&lang=en"
    res = CliRunner().invoke(cli.app, ["add-url", url])
    assert res.exit_code == 0, res.output
    assert "recognized: taleo" in res.output
    assert "Taleo posting: " in res.output
    assert "Apply on the employer's site." in res.output
    assert "by hand" not in res.output and "pasting details" not in res.output
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_add_url_on_an_unrecognized_site_says_to_apply_there(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hospital's own careers domain names no known system: add-url exits 0
    (nothing failed), says it can't read the site and adds no job."""
    from typer.testing import CliRunner

    from jobkit import cli

    monkeypatch.setattr(cli, "_db", lambda: conn)
    url = "https://jobs.examplehealth.org/job/columbus/registered-nurse/123"
    res = CliRunner().invoke(cli.app, ["add-url", url])
    assert res.exit_code == 0, res.output
    assert "jobkit can't read this site" in res.output
    assert "Apply on the employer's site." in res.output
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
