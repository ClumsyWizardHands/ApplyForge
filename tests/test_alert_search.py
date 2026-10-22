"""Web search for the jobs in job-alert emails that board lookup missed
(track/alert_search.py, MCP alert_jobs_to_search / record_search_result).

The alert jobs come from tests/fixtures/email/job_alerts.json (fictional
companies). Board lookup's outcome for each card is either the real
resolver's (on the recorded fictional Greenhouse boards) or written the way
the resolver writes it. Claude's web search is not run here: the tests pass
what it would have found. No LLM is called, and nothing touches a network:
board reads go to a MockTransport and every background launch is mocked."""

from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from jobkit import background, llm
from jobkit import db as q
from jobkit.config import Filters, load_filters
from jobkit.discovery import registry
from jobkit.pipeline import digest
from jobkit.track import alert_search, job_alerts, responses
from tests import test_board_resolver as tbr
from tests.test_job_alert_emails import ALERT, INDEED, _dump, _email_boards

NOW = datetime.datetime(2026, 10, 21, 12, 0, tzinfo=datetime.UTC)
LARKSPUR_NY = "https://job-boards.greenhouse.io/larkspuranalytics/jobs/7301000101"
WORKDAY = (
    "https://harborview.wd5.myworkdayjobs.com/en-US/External/job/Austin-TX/"
    "Account-Executive_R1042?source=LinkedIn&utm_medium=email&q=sales"
)
WORKDAY_CLEAN = (
    "https://harborview.wd5.myworkdayjobs.com/en-US/External/job/Austin-TX/"
    "Account-Executive_R1042?q=sales"
)


@pytest.fixture(autouse=True)
def _fixed_clock_no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(responses, "_now", lambda: NOW)

    def refuse(*a: Any, **k: Any) -> Any:
        raise AssertionError("alert search must not call an LLM")

    monkeypatch.setattr(llm, "call", refuse)
    monkeypatch.setattr(llm, "call_json", refuse)


@pytest.fixture()
def launches(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """background.spawn, recorded: nothing starts."""
    out: list[tuple[str, ...]] = []

    def spawn(c: sqlite3.Connection, *args: str, job_id: int | None = None) -> background.Launch:
        out.append(args)
        return background.Launch(50 + len(out), 4242, tuple(args), Path("logs/spawn/x.log"))

    monkeypatch.setattr(background, "spawn", spawn)
    return out


def _search_on(home: Path, per_check: int | None = None) -> None:
    path = home / "config" / "filters.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["search"] = {"alert_web_search": True}
    if per_check is not None:
        raw["search"]["max_searches_per_check"] = per_check
    path.write_text(yaml.safe_dump(raw, sort_keys=False))


@pytest.fixture()
def searching(jobkit_home: Path) -> None:
    """The person turned web search for alert jobs on."""
    _search_on(jobkit_home)


def _refs(conn: sqlite3.Connection) -> dict[str, str]:
    """company -> alert reference of each kept alert job."""
    out: dict[str, str] = {}
    for r in conn.execute("SELECT detail FROM events WHERE kind = 'job_alert.item' ORDER BY id"):
        d = json.loads(r["detail"])
        if d["card"]:
            out[d["company"]] = d["id"]
    return out


def _lookup(conn: sqlite3.Connection, ref: str, outcome: str) -> None:
    """A board lookup outcome, in the shape board_resolver writes."""
    conn.execute(
        q.RECORD_EVENT,
        {
            "job_id": None,
            "kind": q.BOARD_RESOLVE_CARD_EVENT,
            "detail": json.dumps({"li_id": ref, "outcome": outcome}),
        },
    )
    conn.commit()


def _missed(conn: sqlite3.Connection) -> dict[str, str]:
    """The LinkedIn alert recorded; board lookup found no board for
    Larkspur, no matching posting for Quillfeather, and flagged Cobaltline."""
    job_alerts.record_job_alerts(
        conn, ALERT["extracted"], ALERT["message_id"], ALERT["received_at"], "linkedin"
    )
    refs = _refs(conn)
    _lookup(conn, refs["Larkspur Analytics"], "no_board")
    _lookup(conn, refs["Quillfeather Labs"], "no_title_match")
    _lookup(conn, refs["Cobaltline Health"], "ambiguous")
    return refs


# ---- the switch ------------------------------------------------------------------


def test_search_is_off_when_the_key_is_absent(conn: sqlite3.Connection) -> None:
    """The test profile, like the owner's config, has no search block."""
    f = load_filters()
    assert f.alert_web_search is False and f.max_searches_per_check is None
    _missed(conn)
    out = alert_search.to_search(conn)
    assert not out.on and out.render().startswith("Web search for alert jobs: off")
    assert "alert-" not in out.render() and "record_search_result" not in out.render()
    assert "2 job(s) from recent job-alert emails were not found" in out.render()


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ({"alert_web_search": "yes"}, "search.alert_web_search must be true or false"),
        ({"alert_web_search": True, "max_searches_per_check": 0}, "1 or more"),
        ({"alert_web_search": True, "max_searches_per_check": True}, "1 or more"),
        ({"alert_web_search": True, "max_searches_per_check": "5"}, "1 or more"),
    ],
)
def test_bad_search_values_raise(block: dict[str, Any], message: str) -> None:
    f = Filters(raw={"search": block})
    with pytest.raises(ValueError, match=message):
        _ = f.alert_web_search, f.max_searches_per_check
    assert Filters(raw={"search": None}).alert_web_search is False
    with pytest.raises(ValueError, match="search must be a mapping"):
        _ = Filters(raw={"search": [1]}).alert_web_search


def test_email_targets_says_whether_search_is_on(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, jobkit_home: Path
) -> None:
    from jobkit import mcp_server

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    assert (
        mcp_server.email_targets()
        .rstrip()
        .endswith("Jobs board lookup cannot find are listed in the digest.")
    )
    _search_on(jobkit_home)
    assert "Web search for alert jobs: on, no limit per check" in mcp_server.email_targets()
    _search_on(jobkit_home, per_check=5)
    assert "on, at most 5 searches per email check" in mcp_server.email_targets()


# ---- what to search for --------------------------------------------------------------


def test_unmatched_recent_jobs_are_listed_and_older_ones_are_not(
    conn: sqlite3.Connection, jobkit_home: Path
) -> None:
    _search_on(jobkit_home)
    refs = _missed(conn)
    # an Indeed alert from five weeks ago: outside the 14-day freshness window
    job_alerts.record_job_alerts(conn, INDEED["extracted"], "m-old", "2026-09-15", "indeed")
    old = {
        json.loads(r["detail"])["id"]
        for r in conn.execute("SELECT detail FROM events WHERE kind = 'job_alert.item'")
        if json.loads(r["detail"])["message_id"] == "m-old" and json.loads(r["detail"])["card"]
    }
    assert old
    for ref in old:
        _lookup(conn, ref, "no_board")
    out = alert_search.to_search(conn)
    assert [it.company for it in out.items] == ["Larkspur Analytics", "Quillfeather Labs"]
    assert out.items[0].alert_ref == refs["Larkspur Analytics"]
    text = out.render()
    assert (
        f"- {refs['Larkspur Analytics']}: Larkspur Analytics — Account Executive, Mid-Market "
        "(New York, NY), LinkedIn alert 2026-10-20; board lookup: no Greenhouse, Lever or "
        'Ashby board found; search: "Account Executive, Mid-Market" "Larkspur Analytics" '
        "New York, NY job -site:linkedin.com"
    ) in text
    assert "Never open a linkedin.com or lnkd.in page" in text
    assert "skip those results without opening them" in text and "stop at any CAPTCHA" in text
    assert "Never sign in" in text and "robots.txt" in text
    assert refs["Cobaltline Health"] not in text  # flagged for the person, not searched


def test_a_job_still_waiting_for_board_lookup_is_not_offered(
    conn: sqlite3.Connection, jobkit_home: Path
) -> None:
    _search_on(jobkit_home)
    job_alerts.record_job_alerts(
        conn, ALERT["extracted"], ALERT["message_id"], ALERT["received_at"], "linkedin"
    )
    out = alert_search.to_search(conn, lookup_running="`jobkit same-day --resolve-boards`")
    assert out.items == [] and out.waiting_for_lookup == 3
    text = out.render()
    assert "Board lookup is still running" in text and "3 job(s) still wait" in text


def test_the_real_resolver_s_misses_are_offered(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, jobkit_home: Path
) -> None:
    """End to end on the recorded boards: an alert job at a company with no
    Greenhouse, Lever or Ashby board is offered for search once board
    lookup gave up on it; the job found on its board is not."""
    monkeypatch.setattr(registry, "lookup", lambda name: None)
    _search_on(jobkit_home)
    jobs = [
        {
            "company": "Larkspur Analytics",
            "title": "Account Executive, Mid-Market",
            "location": "New York, NY",
        },
        {"company": "Wrenfield Paper", "title": "Account Executive", "location": "Denver, CO"},
    ]
    job_alerts.record_job_alerts(conn, jobs, "m-e2e", "2026-10-20", "linkedin")
    rep = tbr.run(conn, _email_boards())
    assert rep.ingested == 1 and rep.no_board == 1, rep.render()
    (item,) = alert_search.to_search(conn).items
    assert (item.company, item.lookup) == ("Wrenfield Paper", "no_board")


# ---- recording a result --------------------------------------------------------------


def test_a_greenhouse_link_ingests_one_job_and_starts_same_day_scoring(
    conn: sqlite3.Connection,
    jobkit_home: Path,
    launches: list[tuple[str, ...]],
    searching: None,
) -> None:
    refs = _missed(conn)
    boards = _email_boards()
    rec = alert_search.record_search_result(
        conn,
        refs["Larkspur Analytics"],
        LARKSPUR_NY + "?gh_src=abc",
        "found",
        ats_transport=boards.transport,
    )
    row = tbr.ats_row(conn, "greenhouse", "larkspuranalytics", "7301000101")
    assert row is not None and row["status"] == "new"
    assert rec.outcome == "ingested" and rec.job_id == row["id"]
    # one read confirms the board is Larkspur's, one polls it; only the board API
    assert [r.url.host for r in boards.requests] == ["boards-api.greenhouse.io"] * 2
    # scoring happens in the background, never in the request
    assert launches == [("same-day", str(row["id"]))]
    text = rec.render()
    assert text.startswith(
        "recorded: Larkspur Analytics — Account Executive, Mid-Market (New York, NY): found by "
        f"web search on the employer's own board (greenhouse/larkspuranalytics): job {row['id']}"
    )
    assert f"same-day scoring: started `jobkit same-day {row['id']}`" in text
    # one job row for the posting; the whole board came in as new rows
    n = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE title = 'Account Executive, Mid-Market' "
        "AND location_raw = 'New York, NY'"
    ).fetchone()[0]
    assert n == 1
    # recorded once: a repeat changes nothing and starts nothing
    jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    again = alert_search.record_search_result(
        conn, refs["Larkspur Analytics"], LARKSPUR_NY, "found", ats_transport=boards.transport
    )
    assert again.already and again.render().startswith("already recorded:")
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == jobs
    assert len(launches) == 1
    assert len(conn.execute(q.ALERT_SEARCHES_SINCE, {"since": "2000-01-01"}).fetchall()) == 1
    # no longer offered; the summary and the digest say where it was found
    assert refs["Larkspur Analytics"] not in alert_search.to_search(conn).render()
    lines = job_alerts.items_after(conn, 0)
    assert any(
        ln.startswith("- Larkspur Analytics") and "found by web search" in ln for ln in lines
    )


def test_a_board_link_is_matched_by_title_and_location(
    conn: sqlite3.Connection,
    jobkit_home: Path,
    launches: list[tuple[str, ...]],
    searching: None,
) -> None:
    refs = _missed(conn)
    rec = alert_search.record_search_result(
        conn,
        refs["Quillfeather Labs"],
        "https://job-boards.greenhouse.io/quillfeather",
        "found",
        ats_transport=_email_boards().transport,
    )
    row = tbr.ats_row(conn, "greenhouse", "quillfeather", "7303000101")
    assert row is not None and rec.job_id == row["id"]
    assert launches == [("same-day", str(row["id"]))]


def test_a_job_already_in_jobkit_is_not_added_or_scored_again(
    conn: sqlite3.Connection,
    jobkit_home: Path,
    launches: list[tuple[str, ...]],
    searching: None,
) -> None:
    refs = _missed(conn)
    boards = _email_boards()
    from tests.test_job_alert_emails import _poll

    _poll(conn, boards, "larkspuranalytics", "Larkspur Analytics")
    row = tbr.ats_row(conn, "greenhouse", "larkspuranalytics", "7301000101")
    assert row is not None
    conn.execute("UPDATE jobs SET status = 'queued' WHERE id = ?", (row["id"],))
    conn.commit()
    jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    rec = alert_search.record_search_result(
        conn, refs["Larkspur Analytics"], LARKSPUR_NY, "found", ats_transport=boards.transport
    )
    assert rec.job_id == row["id"] and launches == []
    assert "already in jobkit (status queued)" in rec.render()
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == jobs


def test_a_workday_lead_keeps_the_cleaned_link_and_creates_no_job(
    conn: sqlite3.Connection,
    jobkit_home: Path,
    launches: list[tuple[str, ...]],
    searching: None,
) -> None:
    """A posting on a system jobkit does not read is kept as a lead: the
    system's name and the employer's apply link with its tracking parameters
    removed. No job row is created (CLAUDE.md rules 10 and 11), the lead is
    recorded once, and a linkedin.com link is still refused."""
    refs = _missed(conn)
    ref = refs["Larkspur Analytics"]
    # the email check that took the alert finished before the search
    responses.finish_email_check(conn, "2026-10-21T10:00:00Z")
    jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    with pytest.raises(ValueError, match="LinkedIn results are skipped"):
        alert_search.record_search_result(
            conn, ref, "https://www.linkedin.com/jobs/view/4300000101/", "found"
        )
    rec = alert_search.record_search_result(conn, ref, WORKDAY, "found")
    assert rec.outcome == "elsewhere" and rec.system == "Workday"
    assert rec.url == WORKDAY_CLEAN and rec.job_id is None
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == jobs and launches == []
    said = (
        "found by web search on Workday, which jobkit does not read; "
        f"apply on the employer's site: {WORKDAY_CLEAN}"
    )
    assert said in rec.render()
    # the event holds the cleaned link, never a tracking parameter or LinkedIn
    (row,) = conn.execute(q.ALERT_SEARCHES_SINCE, {"since": "2000-01-01"}).fetchall()
    assert row["job_id"] is None and json.loads(row["detail"])["url"] == WORKDAY_CLEAN
    stored = _dump(conn).lower()
    for needle in ("linkedin.com", "lnkd.in", "utm_", "source=linkedin"):
        assert needle not in stored, needle
    # idempotent by alert_ref: a repeat, even with another link, changes nothing
    again = alert_search.record_search_result(
        conn, ref, "https://acme.icims.com/jobs/1234/job", "found"
    )
    assert again.already and again.url == WORKDAY_CLEAN and said in again.render()
    assert len(conn.execute(q.ALERT_SEARCHES_SINCE, {"since": "2000-01-01"}).fetchall()) == 1
    # the digest lists it with the link
    text = digest.write_digest(conn, "2026-10-21").read_text()
    assert (
        "- Larkspur Analytics — Account Executive, Mid-Market (New York, NY), LinkedIn alert "
        f"2026-10-20: {said}"
    ) in text
    # an alert job an earlier digest listed gets its own line for the result
    lines = alert_search.digest_lines(conn, "2000-01-01 00:00:00", listed=set())
    assert lines[0] == "## Web search results for alert jobs since the last digest (1)"
    assert lines[1] == (
        f"- Larkspur Analytics — Account Executive, Mid-Market (New York, NY): {said}"
    )
    assert alert_search.digest_lines(conn, "2000-01-01 00:00:00", {ref}) == []
    # the next email check's summary names it with the link
    check = responses.finish_email_check(conn, "2026-10-21T11:00:00Z").render()
    assert "Found by web search on sites jobkit does not read (apply there yourself):" in check
    assert (f"- Larkspur Analytics — Account Executive, Mid-Market (New York, NY): {said}") in check
    # and only once: the check after it does not repeat the lead
    assert said not in responses.finish_email_check(conn, "2026-10-21T12:00:00Z").render()


def test_a_posting_on_the_employers_own_careers_site_is_a_lead(
    conn: sqlite3.Connection, launches: list[tuple[str, ...]], searching: None
) -> None:
    """A posting on a domain that spells the employer's name is the
    posting's own link: kept as a lead with no job row. A job site's copy,
    or another company's site, is refused with nothing written."""
    refs = _missed(conn)
    ref = refs["Larkspur Analytics"]
    before = _dump(conn)
    for url in (
        "https://www.ziprecruiter.com/c/Larkspur-Analytics/Job/Account-Executive/-in-New-York,NY",
        "https://careers.otherco.com/jobs/ae-mid-market",
    ):
        with pytest.raises(ValueError, match="not the posting's own link"):
            alert_search.record_search_result(conn, ref, url, "found")
    assert _dump(conn) == before
    rec = alert_search.record_search_result(
        conn,
        ref,
        "https://careers.larkspuranalytics.com/jobs/ae-mid-market?utm_source=google&gclid=x1",
        "found",
    )
    assert rec.outcome == "elsewhere" and rec.system == alert_search.EMPLOYER_SITE
    assert rec.url == "https://careers.larkspuranalytics.com/jobs/ae-mid-market"
    assert "apply on the employer's site: https://careers.larkspuranalytics.com/" in rec.render()
    assert rec.job_id is None and launches == []
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_a_lead_recorded_before_links_were_kept_still_reads() -> None:
    """An older lead event (the system's name only) is described as before."""
    assert alert_search.describe({"outcome": "elsewhere", "system": "Taleo"}) == (
        "on the employer's own site (Taleo), which jobkit does not read: nothing added"
    )


def test_not_found_is_recorded_once_and_listed_once(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import mcp_server

    _search_on(jobkit_home)
    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    refs = _missed(conn)
    ref = refs["Quillfeather Labs"]
    out = mcp_server.record_search_result(ref, "", "not_found")
    assert out == (
        "recorded: Quillfeather Labs — Senior Account Executive (Remote - US): not found by "
        "web search"
    )
    assert mcp_server.record_search_result(ref, "", "not_found").startswith("already recorded")
    assert mcp_server.record_search_result(ref, LARKSPUR_NY, "found").startswith("already recorded")
    assert ref not in mcp_server.alert_jobs_to_search()
    # the same job in a second alert email: still one line
    job_alerts.record_job_alerts(conn, [ALERT["extracted"][1]], "m-2", "2026-10-21", "linkedin")
    lines = [ln for ln in job_alerts.items_after(conn, 0) if "Quillfeather" in ln]
    assert len(lines) == 1 and lines[0].endswith("not found by web search")
    assert mcp_server.record_search_result(ref, LARKSPUR_NY, "not_found").startswith(
        "already recorded"
    )


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https://www.linkedin.com/jobs/view/4300000101/", "LinkedIn results are skipped"),
        ("https://linkedin.com/comm/jobs/view/4300000101", "LinkedIn results are skipped"),
        ("https://lnkd.in/e4300000105", "LinkedIn results are skipped"),
        ("https://www.indeed.com/viewjob?jk=a1b2c3", "not the posting's own link"),
        (
            "https://r.example.com/www.linkedin.com/jobs/view/4300000101/"
            "harborview.wd5.myworkdayjobs.com",
            "LinkedIn results are skipped",
        ),
        (
            "https://r.example.com/redirect?to=https%3A%2F%2Fwww.linkedin.com%2Fjobs%2Fview%2F1",
            "LinkedIn results are skipped",
        ),
        (
            "https://www.indeed.com/viewjob/4300000101?from=harborview.wd5.myworkdayjobs.com",
            "not the posting's own link",
        ),
        ("https://harborview.wd5.myworkdayjobs.com@evil.example/apply", "user name or password"),
        (
            "https://evil.example/harborview.wd5.myworkdayjobs.com/apply",
            "not the posting's own link",
        ),
        (
            "https://harborview.wd5.myworkdayjobs.com.evil.example/apply",
            "not the posting's own link",
        ),
        (
            "https://acme.wd1.myworkdayjobs.com/External/linkedin/jobs/view/4012345678",
            "LinkedIn results are skipped",
        ),
        ("ftp://boards.greenhouse.io/x", "http"),
        ("", "url is required"),
        ("https://job-boards.greenhouse.io/larkspuranalytics/jobs/7301000101 x", "one link"),
    ],
)
def test_linkedin_and_other_links_are_refused_with_nothing_recorded(
    conn: sqlite3.Connection,
    url: str,
    message: str,
    launches: list[tuple[str, ...]],
    searching: None,
) -> None:
    refs = _missed(conn)
    before = _dump(conn)
    with pytest.raises(ValueError, match=message):
        alert_search.record_search_result(conn, refs["Larkspur Analytics"], url, "found")
    assert _dump(conn) == before and launches == []


def test_a_link_to_another_companys_board_is_refused_with_nothing_written(
    conn: sqlite3.Connection, launches: list[tuple[str, ...]], searching: None
) -> None:
    """A search result for Northwind Traders that points at Quillfeather's
    Greenhouse board: the board reports another company, so nothing is
    polled, no companies row is named after Northwind, and no job enters."""
    job_alerts.record_job_alerts(
        conn,
        [
            {
                "company": "Northwind Traders",
                "title": "Senior Account Executive",
                "location": "Remote - US",
            }
        ],
        "m-northwind",
        "2026-10-20T10:00:00Z",
        "linkedin",
    )
    ref = _refs(conn)["Northwind Traders"]
    _lookup(conn, ref, "no_board")
    before = _dump(conn)
    boards = _email_boards()
    with pytest.raises(ValueError, match="is Quillfeather Labs's, not Northwind Traders's"):
        alert_search.record_search_result(
            conn,
            ref,
            "https://job-boards.greenhouse.io/quillfeather/jobs/7303000101",
            "found",
            ats_transport=boards.transport,
        )
    assert _dump(conn) == before and launches == []
    assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0] == 0


def test_a_board_jobkit_holds_for_another_company_is_refused(
    conn: sqlite3.Connection, launches: list[tuple[str, ...]], searching: None
) -> None:
    from tests.test_job_alert_emails import _poll

    refs = _missed(conn)
    boards = _email_boards()
    _poll(conn, boards, "quillfeather", "Quillfeather Labs")
    before = _dump(conn)
    with pytest.raises(ValueError, match="is Quillfeather Labs's, not Larkspur Analytics's"):
        alert_search.record_search_result(
            conn,
            refs["Larkspur Analytics"],
            "https://job-boards.greenhouse.io/quillfeather/jobs/7303000101",
            "found",
            ats_transport=boards.transport,
        )
    assert _dump(conn) == before and launches == []


def test_a_result_is_refused_while_search_is_off(
    conn: sqlite3.Connection, launches: list[tuple[str, ...]]
) -> None:
    """The key is absent (the owner's config): no result is taken."""
    refs = _missed(conn)
    before = _dump(conn)
    for url, outcome in ((LARKSPUR_NY, "found"), ("", "not_found")):
        with pytest.raises(ValueError, match="web search for alert jobs is off"):
            alert_search.record_search_result(
                conn,
                refs["Larkspur Analytics"],
                url,
                outcome,
                ats_transport=_email_boards().transport,
            )
    assert _dump(conn) == before and launches == []


def test_bad_references_and_outcomes_are_refused(conn: sqlite3.Connection, searching: None) -> None:
    refs = _missed(conn)
    with pytest.raises(ValueError, match="alert reference"):
        alert_search.record_search_result(conn, "4300000101", "", "not_found")
    with pytest.raises(ValueError, match="no job from a job-alert email"):
        alert_search.record_search_result(conn, "alert-0123456789abcdef", "", "not_found")
    with pytest.raises(ValueError, match="outcome must be"):
        alert_search.record_search_result(conn, refs["Larkspur Analytics"], "", "maybe")
    with pytest.raises(ValueError, match="leave url empty"):
        alert_search.record_search_result(
            conn, refs["Larkspur Analytics"], LARKSPUR_NY, "not_found"
        )
    assert not conn.execute(q.ALERT_SEARCHES_SINCE, {"since": "2000-01-01"}).fetchall()


def test_mcp_refuses_a_linkedin_link_in_plain_words(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, searching: None
) -> None:
    from jobkit import mcp_server

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    refs = _missed(conn)
    out = mcp_server.record_search_result(
        refs["Larkspur Analytics"], "https://www.linkedin.com/jobs/view/4300000101/", "found"
    )
    assert out.startswith("not recorded: LinkedIn results are skipped")


# ---- the person's limit per check -----------------------------------------------------


def test_a_limit_per_check_holds_the_rest_for_the_next_check(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import mcp_server

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    _search_on(jobkit_home, per_check=1)
    refs = _missed(conn)
    out = alert_search.to_search(conn)
    assert [it.company for it in out.items] == ["Larkspur Analytics"]
    assert "1 more wait for the next check (your limit is 1 searches per check)" in out.render()
    alert_search.record_search_result(conn, refs["Larkspur Analytics"], "", "not_found")
    assert alert_search.to_search(conn).items == []
    assert "the limit of 1 searches is used" in alert_search.to_search(conn).render()
    # a new email check: the next one is offered
    monkeypatch.setattr(job_alerts, "alert_cards_after", lambda c, after_id: 0)
    responses.finish_email_check(conn, "2026-10-21T11:00:00Z")
    assert [it.company for it in alert_search.to_search(conn).items] == ["Quillfeather Labs"]


def test_finish_email_check_points_to_the_search_only_when_on(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import mcp_server

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    assert "alert_jobs_to_search" not in mcp_server.finish_email_check("2026-10-21T09:00:00Z")
    _search_on(jobkit_home)
    out = mcp_server.finish_email_check("2026-10-21T10:00:00Z")
    assert out.endswith("call alert_jobs_to_search.")


def test_cli_to_search_and_search_result(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from jobkit import cli

    monkeypatch.setattr(cli, "_db", lambda: conn)
    _search_on(jobkit_home)
    refs = _missed(conn)
    run = CliRunner().invoke
    res = run(cli.app, ["email", "to-search"])
    assert res.exit_code == 0 and refs["Larkspur Analytics"] in res.output
    ref = ["email", "search-result", "--alert-ref", refs["Larkspur Analytics"]]
    res = run(cli.app, [*ref, "--url", "https://www.linkedin.com/jobs/view/1/"])
    assert res.exit_code == 1 and "LinkedIn results are skipped" in res.output
    res = run(cli.app, [*ref, "--url", WORKDAY])
    assert res.exit_code == 0 and "found by web search on Workday" in res.output
    assert f"apply on the employer's site: {WORKDAY_CLEAN}" in res.output
    res = run(cli.app, [*ref, "--outcome", "not_found"])
    assert res.exit_code == 0 and res.output.startswith("already recorded")


@pytest.mark.parametrize("name", ["filters.example.yaml", "filters.sales.example.yaml"])
def test_the_shipped_templates_leave_web_search_off(name: str) -> None:
    """Copying a template never turns web search for alert jobs on: the
    setup interview asks and writes the person's answer."""
    path = Path(__file__).resolve().parents[1] / "config" / name
    raw = yaml.safe_load(path.read_text())
    assert raw["search"]["alert_web_search"] is False
    assert "max_searches_per_check" not in raw["search"]


def test_every_recognised_system_has_a_host() -> None:
    """A found URL counts only on its system's own host, so every system the
    resolver can name needs its hosts listed."""
    from jobkit.discovery.url_resolver import KNOWN_SYSTEMS

    assert set(KNOWN_SYSTEMS) <= set(alert_search.SYSTEM_HOSTS)


def test_linkedin_tracking_is_dropped_from_a_stored_link() -> None:
    """LinkedIn's tracking keys and 'li-' ids are dropped before a link is read."""
    clean = alert_search._clean_url(
        "https://acme.wd1.myworkdayjobs.com/External/job/X_R1"
        "?refId=li-4012345678&trk=public_jobs&trackingId=abc&lipi=x&li_fat_id=y"
        "&src=li-4012345678&from=lnkd&q=sales"
    )
    assert clean == "https://acme.wd1.myworkdayjobs.com/External/job/X_R1?q=sales"


def test_a_filters_yaml_that_does_not_parse_reads_as_off(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A YAML syntax error in filters.yaml: schedule status, email_targets and
    finish_email_check still answer, and the check is recorded."""
    from jobkit import mcp_server, schedule

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    (jobkit_home / "config" / "filters.yaml").write_text("gates: [unclosed\n  x: : :\n")
    assert not alert_search.search_on()
    assert alert_search.search_status().startswith("Web search for alert jobs: off (")
    assert schedule.open_forms_line(conn).startswith(schedule.OPEN_FORMS_PREFIX + " off (")
    assert "Web search for alert jobs: off (" in mcp_server.email_targets()
    out = mcp_server.finish_email_check("2026-10-21T10:00:00Z")
    assert not out.startswith("not recorded")
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'email_check'").fetchone()[0]
