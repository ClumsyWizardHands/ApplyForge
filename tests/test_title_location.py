"""A place named in the job title wins over a contradicting feed location
(normalize.title_location, pipeline/filter.py; owner decision D4).

"Account Executive - Denver, CO" filed by the feed under Boston, or "Chief
of Staff | Colombia" filed under Cambridge, MA: the filter reads the title's
place, and logs the override. Titles that name no place, or a place that
agrees with the feed, are filtered exactly as before. Fictional jobs only."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from jobkit.config import Filters
from jobkit.normalize import TitleLocation, title_location
from jobkit.pipeline.filter import (
    refilter,
    reject_reason_now,
    run_filters,
    title_location_override,
)
from tests.test_filter import _insert

FILTERS = Filters(
    raw={
        "filters": {
            "include_titles": [r"(?i)\bdirector\b", r"(?i)\baccount executive\b"],
            "locations": {"allowed": ["Philadelphia, PA", "Boston, MA"], "accept_remote": True},
        },
    }
)


# ---------------------------------------------------------------------------
# the parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "text", "us", "remote"),
    [
        ("Account Executive - Denver, CO", "Denver, CO", True, False),
        ("Account Executive, Denver, CO", "Denver, CO", True, False),
        ("Director, Sales - New York, NY", "New York, NY", True, False),
        ("Strategic AE - Washington, DC", "Washington, DC", True, False),
        ("Sales Director - Austin", "Austin", True, False),
        ("Mid-Market AE [NYC]", "NYC", True, False),
        ("Account Executive (Hybrid - Boston)", "Boston", True, False),
        ("Senior Solutions Engineer- Strategic - Minnesota (Fabrikam)", "Minnesota", True, False),
        ("Sales Engineer - Phoenix, AZ (Hybrid)", "Phoenix, AZ", True, False),
        ("AE - New Mexico", "New Mexico", True, False),
        ("Chief of Staff | Colombia", "Colombia", False, False),
        ("Sales Manager (Remote - Canada)", "Canada", False, True),
        ("Account Executive - LATAM", "LATAM", False, False),
        ("Head of Sales, EMEA & APAC", "EMEA / APAC", False, False),
        ("Partner Sales Manager (Central EMEA)", "Central EMEA", False, False),
        ("Sales Development Representative - DACH Territory", "DACH", False, False),
        ("Business Development Representative | Ontario, Canada", "Ontario, Canada", False, False),
        ("Account Executive - Mexico", "Mexico", False, False),
        ("Director of Sales: Toronto", "Toronto", False, False),
        # a role or business word is no city (review 2026-10-23): the place alone
        ("Solutions Architect, Platforms, UK", "UK", False, False),
        ("Consultant, Professional Services, Toronto", "Toronto", False, False),
        ("Director, Strategic Pursuits, APAC", "APAC", False, False),
        ("Account Executive, Remote Denver, CO", "Denver, CO", True, True),
    ],
)
def test_the_place_a_title_names(title: str, text: str, us: bool, remote: bool) -> None:
    assert title_location(title) == TitleLocation(text=text, us=us, remote=remote)


@pytest.mark.parametrize(
    "title",
    [
        "Director of Sales",
        "Account Executive | Mid-Market",
        "Account Executive, Mid-Market & Enterprise",
        "Manager, Customer Success - Upmarket",
        "Forward Deployed Engineer - German Speaking",
        "Director, IT",
        "Product Manager, ID",  # a lone two-letter code is a word, not a state
        "Account Executive - Remote",
        "Enterprise Account Executive - SLED, Higher Ed (West)",
        "Legal Counsel - US Government",  # the US as a whole is no override
        "Sales Lead - Americas",
        "Account Executive - US/Canada",  # open to the US
        "Enterprise AE - NYC or SF",  # several US places
        "Account Executive (Key Accounts)",
        # open to North America (review 2026-10-23)
        "Technical Recruiter (Remote EU, LATAM, NAMER)",
        "Account Executive - EMEA / NAMER",
        "",
    ],
)
def test_titles_that_name_no_single_place(title: str) -> None:
    assert title_location(title) is None


# ---------------------------------------------------------------------------
# the override
# ---------------------------------------------------------------------------


def _reason(conn: sqlite3.Connection, job_id: int) -> str | None:
    row = conn.execute("SELECT reason FROM rejections WHERE job_id = ?", (job_id,)).fetchone()
    return None if row is None else str(row["reason"])


def _status(conn: sqlite3.Connection, job_id: int) -> str:
    return str(conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()[0])


def test_a_us_city_in_the_title_overrides_a_contradicting_feed_city(
    conn: sqlite3.Connection, jobkit_home: Path
) -> None:
    denver = _insert(
        conn,
        external_id="1",
        title="Account Executive - Denver, CO",
        location_raw="Boston, MA",
        location_bucket="boston-metro",
        remote=0,
    )
    # the other way round: the title's allowed city wins over the feed's
    philly = _insert(
        conn,
        external_id="2",
        title="Director of Sales - Philadelphia, PA",
        location_raw="Denver, CO",
        location_bucket="denver-metro",
        remote=0,
    )
    conn.commit()

    run_filters(conn, FILTERS)

    assert _reason(conn, denver) == "location:denver-metro:title"
    assert _status(conn, philly) == "filtered"
    logged = [
        json.loads(line)
        for line in (jobkit_home / "logs" / "jobkit.jsonl").read_text().splitlines()
        if '"filter.title_location"' in line
    ]
    by_job = {e["job_id"]: e for e in logged}
    assert by_job[denver]["feed_location"] == "Boston, MA"
    assert by_job[denver]["title_location"] == "Denver, CO"
    assert by_job[denver]["verdict"] == "location:denver-metro:title"
    assert by_job[philly]["verdict"] == "passed"


def test_a_country_outside_the_us_in_the_title_is_that_country(conn: sqlite3.Connection) -> None:
    on_site = _insert(
        conn,
        external_id="1",
        title="Director, Chief of Staff | Colombia",
        location_raw="Cambridge, MA",
        location_bucket="boston-metro",
        remote=0,
    )
    remote = _insert(
        conn,
        external_id="2",
        title="Director of Sales (Remote - Canada)",
        location_raw="Remote",
        location_bucket="remote-us",
        remote=1,
    )
    region = _insert(
        conn,
        external_id="3",
        title="Account Executive - LATAM",
        location_raw="Remote",
        location_bucket="remote-us",
        remote=1,
    )
    blank = _insert(
        conn,
        external_id="4",
        title="Director of Partnerships | Colombia",
        location_raw="",
        location_bucket="other-unknown",
        remote=None,
    )
    conn.commit()

    stats = run_filters(conn, FILTERS)

    assert stats["passed"] == 0
    assert _reason(conn, on_site) == "location:other-colombia:title"
    assert _reason(conn, remote) == "location:remote-intl:title"
    assert _reason(conn, region) == "location:remote-intl:title"
    assert _reason(conn, blank) == "location:other-colombia:title"


def test_a_title_place_that_agrees_with_the_feed_changes_nothing(conn: sqlite3.Connection) -> None:
    same_metro = _insert(
        conn,
        external_id="1",
        title="Director of Sales - Boston",
        location_raw="Cambridge, MA",
        location_bucket="boston-metro",
        remote=0,
    )
    same_state = _insert(
        conn,
        external_id="2",
        title="Director of Sales - Denver",
        location_raw="Boulder, CO",
        location_bucket="other-boulder",
        remote=0,
    )
    us_wide = _insert(
        conn,
        external_id="3",
        title="Director of Sales - Denver, CO",
        location_raw="United States",
        location_bucket="other-unitedstates",
        remote=None,
    )
    abroad_both = _insert(
        conn,
        external_id="4",
        title="Account Executive - DACH",
        location_raw="Berlin, Germany",
        location_bucket="other-berlin",
        remote=0,
    )
    conn.commit()
    for job_id in (same_metro, same_state, us_wide, abroad_both):
        row = dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
        assert title_location_override(row) is None

    run_filters(conn, FILTERS)

    assert _status(conn, same_metro) == "filtered"
    assert _reason(conn, same_state) == "location:other-boulder"  # the feed's own verdict
    assert _status(conn, us_wide) == "filtered"  # deferred to the remote check, as before
    assert _reason(conn, abroad_both) == "location:other-berlin"


@pytest.mark.parametrize(
    "feed", ["Remote - US", "Remote - United States", "Remote, USA", "US Remote", "Remote"]
)
def test_a_us_city_in_the_title_agrees_with_a_us_wide_remote_feed(feed: str) -> None:
    """The US as a whole, remote or not, is never contradicted by a US city
    in the title: no override, no filter.title_location log (review
    2026-10-23: "Remote - US" became "Remote - Boston")."""
    row = {"title": "Sales Manager, Boston", "location_raw": feed, "remote": 1}
    assert title_location_override(row) is None


def test_a_title_state_agrees_with_a_feed_listing_several_places() -> None:
    """A feed location listing several places agrees with the title's state
    when any listed place is in it, not only the last one."""
    row = {
        "title": "District Manager - Grocery - South Carolina",
        "location_raw": "Greenville, SC; Spartanburg, SC; Savannah, GA",
        "remote": 0,
    }
    assert title_location_override(row) is None
    row["location_raw"] = "Savannah, GA; Macon, GA"
    over = title_location_override(row)
    assert over is not None and over.location_raw == "South Carolina"


def test_a_place_abroad_still_overrides_a_us_wide_remote_feed() -> None:
    row = {"title": "Account Executive | Colombia", "location_raw": "Remote - US", "remote": 1}
    over = title_location_override(row)
    assert over is not None and over.location_raw == "Remote - Colombia"


def test_a_remote_us_job_keeps_its_remote_verdict_with_a_us_city_in_the_title(
    conn: sqlite3.Connection,
) -> None:
    job_id = _insert(
        conn,
        external_id="1",
        title="Account Executive - Denver, CO",
        location_raw="Remote or Boston, MA",
        location_bucket="remote-us",
        remote=1,
    )
    conn.commit()
    run_filters(conn, FILTERS)
    assert _status(conn, job_id) == "filtered"


def test_reject_reason_now_reads_the_title_place_too(conn: sqlite3.Connection) -> None:
    job_id = _insert(
        conn,
        external_id="1",
        title="Director of Sales | Colombia",
        location_raw="Philadelphia, PA",
        location_bucket="other-philadelphia",
        remote=0,
    )
    conn.commit()
    row = dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
    assert reject_reason_now(row, FILTERS) == "location:other-colombia:title"
    row["title"] = "Director of Sales"
    assert reject_reason_now(row, FILTERS) is None


def test_titles_without_a_place_are_filtered_as_before(conn: sqlite3.Connection) -> None:
    ids = [
        _insert(conn, external_id="1", title="Director of Sales, Mid-Market"),
        _insert(
            conn,
            external_id="2",
            title="Account Executive | Enterprise",
            location_raw="Tulsa, OK",
            location_bucket="other-tulsa",
            remote=0,
        ),
    ]
    conn.commit()
    run_filters(conn, FILTERS)
    assert _status(conn, ids[0]) == "filtered"
    assert _reason(conn, ids[1]) == "location:other-tulsa"


# ---------------------------------------------------------------------------
# a region in the title is often the territory covered (review 2026-10-23)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "location_raw", "remote"),
    [
        ("Channel Account Executive, LATAM", "Miami, FL - Remote", 1),
        ("Channel Account Executive, LATAM", "US FL Miami - Remote", 1),
        ("Account Executive, Mid-Market (LATAM)", "USA, Remote", 1),
        ("Account Executive - LATAM", "Remote, US", 1),
        ("Forward Deployed Strategist (EMEA)", "Boston, MA", 0),
        ("Partner Manager - Central EMEA", "United States", None),
    ],
)
def test_a_region_in_the_title_never_overrides_a_us_feed_location(
    title: str, location_raw: str, remote: int | None
) -> None:
    row = {"title": title, "location_raw": location_raw, "remote": remote}
    assert title_location_override(row) is None


def test_a_us_remote_job_covering_a_region_passes_the_location_check(
    conn: sqlite3.Connection,
) -> None:
    latam = _insert(
        conn,
        external_id="1",
        title="Channel Account Executive, LATAM",
        location_raw="Miami, FL - Remote",
        location_bucket="remote-us",
        remote=1,
    )
    emea = _insert(
        conn,
        external_id="2",
        title="Director of Sales (EMEA)",
        location_raw="Boston, MA",
        location_bucket="boston-metro",
        remote=0,
    )
    # a country still wins, as D4 says
    colombia = _insert(
        conn,
        external_id="3",
        title="Director, Chief of Staff | Colombia",
        location_raw="Cambridge, MA",
        location_bucket="boston-metro",
        remote=0,
    )
    conn.commit()
    run_filters(conn, FILTERS)
    assert _status(conn, latam) == "filtered"
    assert _status(conn, emea) == "filtered"
    assert _reason(conn, colombia) == "location:other-colombia:title"


@pytest.mark.parametrize(
    ("title", "location_raw", "remote", "location"),
    [
        ("Account Executive - LATAM", "Remote", 1, "Remote - LATAM"),
        ("Account Executive - LATAM", "", None, "LATAM"),
        ("Account Executive - EMEA", "Hybrid", None, "EMEA"),
        ("Account Executive - EMEA", "Distributed", None, "EMEA"),
    ],
)
def test_a_region_in_the_title_overrides_a_feed_naming_no_place(
    title: str, location_raw: str, remote: int | None, location: str
) -> None:
    over = title_location_override({"title": title, "location_raw": location_raw, "remote": remote})
    assert over is not None
    assert over.location_raw == location


# ---------------------------------------------------------------------------
# a city abroad whose name a US city shares counts only with its country
# ---------------------------------------------------------------------------


def test_an_ambiguous_city_alone_is_no_place_abroad() -> None:
    assert title_location("Account Executive (Melbourne)") is None
    row = {
        "title": "Account Executive (Melbourne)",
        "location_raw": "Eau Gallie, Brevard County",
        "remote": None,
    }
    assert title_location_override(row) is None


def test_an_ambiguous_city_with_its_country_is_that_place() -> None:
    assert title_location("Account Executive (Melbourne, Australia)") == TitleLocation(
        text="Melbourne, Australia", us=False, remote=False
    )
    row = {
        "title": "Account Executive (Melbourne, Australia)",
        "location_raw": "Eau Gallie, Brevard County",
        "remote": None,
    }
    over = title_location_override(row)
    assert over is not None
    assert over.location_raw == "Melbourne, Australia"


def test_refilter_leaves_a_rejection_that_only_gains_title_alone(
    conn: sqlite3.Connection,
) -> None:
    job_id = _insert(
        conn,
        external_id="1",
        title="Director of Sales (Remote - Canada)",
        location_raw="Remote",
        location_bucket="remote-us",
        remote=1,
    )
    conn.execute("UPDATE jobs SET status='rejected' WHERE id=?", (job_id,))
    conn.execute(
        "INSERT INTO rejections (job_id, layer, reason) VALUES (?, 'filter', ?)",
        (job_id, "location:remote-intl"),
    )
    conn.commit()
    report = refilter(conn, FILTERS, dry_run=True)
    assert report.unchanged == 1 and report.reset == 0
