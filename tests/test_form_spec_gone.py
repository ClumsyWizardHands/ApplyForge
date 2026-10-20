"""Review 2026-10-12: a posting the ATS says is gone is closed at
form-spec capture, never tailored or staged. Offline, recorded fixtures.

Fixtures (tests/fixtures/form_spec_gone/), recorded 2026-10-12 with jobkit's
honest User-Agent:
* greenhouse_job_not_found.json: GET boards-api.greenhouse.io/v1/boards/<token>/
  jobs/1?questions=true answered 404 with this body (a wrong board token
  answers the same);
* lever_posting_not_found.json: GET api.lever.co/v0/postings/<site>/<zero
  uuid> answered 404 with this body (an unknown site answers the same);
* ashby_job_posting_null.json: HAND-BUILT, not recorded. Ashby's GraphQL
  endpoint takes a POST, and the live checks were limited to GETs;
* ashby_board_list.json: HAND-BUILT, the shape of the public posting API's
  job list (api.ashbyhq.com/posting-api/job-board/{token}), which must not
  list a posting before a jobPosting: null counts as gone. The GraphQL
  endpoint is shared by every posting, so its HTTP status never counts;
* greenhouse_wonka_7004321.json: HAND-BUILT, not recorded: an invented
  employer's Greenhouse job with its questions, trimmed to the keys the
  fetcher reads. It has a location_questions block, which the fetcher once
  ignored."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from jobkit import db
from jobkit.autofill import form_spec
from jobkit.autofill.form_spec import PostingGone, _ashby_form, _gh_form, _lever_form
from tests.conftest import FIXTURES

GONE = FIXTURES / "form_spec_gone"


def _resp(status: int, payload: Any = None, text: str = "") -> MagicMock:
    r = MagicMock()
    r.status_code = status
    if isinstance(payload, Exception):
        r.json.side_effect = payload
    else:
        r.json.return_value = payload
    r.text = text
    return r


def _fixture(name: str) -> Any:
    return json.loads((GONE / name).read_text())


# ---- the fetchers tell a definite not-found from any other failure --------------


@pytest.mark.parametrize("status", [404, 410])
def test_greenhouse_not_found_is_gone(status: int) -> None:
    client = MagicMock()
    client.get.return_value = _resp(status, _fixture("greenhouse_job_not_found.json"))
    assert _gh_form(client, "wonka", "1") == PostingGone(f"greenhouse-http-{status}")


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_greenhouse_other_failures_keep_the_old_behaviour(status: int) -> None:
    client = MagicMock()
    client.get.return_value = _resp(status, {"error": "x"})
    assert _gh_form(client, "wonka", "1") is None


def test_greenhouse_location_question_is_captured_without_hidden_fields() -> None:
    """An employer's Greenhouse form had a required Location box the spec
    never listed."""
    client = MagicMock()
    client.get.return_value = _resp(200, _fixture("greenhouse_wonka_7004321.json"))
    fields = _gh_form(client, "wonka", "7004321")
    assert isinstance(fields, list)
    by_id = {f["identity"]: f for f in fields}
    assert by_id["location"]["label"] == "Location" and by_id["location"]["required"]
    assert "latitude" not in by_id and "longitude" not in by_id
    assert not any(f["type"] == "input_hidden" for f in fields)
    # the questions block is read as before
    assert by_id["resume"]["type"] == "input_file" and by_id["resume_text"]["type"] == "textarea"
    assert by_id["question_51000002"]["label"].startswith("Will you now or in the future need")


def _lever_client(apply: MagicMock, api: MagicMock | Exception) -> MagicMock:
    client = MagicMock()

    def get(url: str, **kw: Any) -> MagicMock:
        if url.startswith("https://jobs.lever.co/"):
            return apply
        assert url.startswith("https://api.lever.co/v0/postings/"), url
        if isinstance(api, Exception):
            raise api
        return api

    client.get.side_effect = get
    return client


def test_lever_gone_when_the_postings_api_says_not_found() -> None:
    body = _fixture("lever_posting_not_found.json")
    client = _lever_client(_resp(404, text="Not found"), _resp(404, body))
    assert _lever_form(client, "acme", "x") == PostingGone("lever-api-http-404")
    # the not-found body counts even behind another status
    client = _lever_client(_resp(503), _resp(200, body))
    assert _lever_form(client, "acme", "x") == PostingGone("lever-api-not-found")


@pytest.mark.parametrize(
    "api",
    [
        _resp(200, {"id": "x", "text": "Account Executive"}),  # the posting is there
        _resp(500, {"ok": False, "error": "Internal error"}),
        _resp(200, ValueError("not json")),
        httpx.ConnectTimeout("timed out"),
    ],
    ids=["api-has-it", "api-500", "api-not-json", "api-timeout"],
)
def test_lever_apply_page_failure_alone_is_not_gone(api: Any) -> None:
    client = _lever_client(_resp(404, text="Not found"), api)
    assert _lever_form(client, "acme", "x") is None


def test_lever_apply_page_that_loads_is_never_checked_against_the_api() -> None:
    html = (FIXTURES / "lever_apply.html").read_text()
    client = _lever_client(_resp(200, text=html), AssertionError("api must not be called"))
    assert isinstance(_lever_form(client, "acme", "x"), list)


LISTED = "2f1c0a9e-5b7d-4c3e-9a8f-1d2e3f405162"  # on ashby_board_list.json
UNLISTED = "9d8c7b6a-0000-4000-8000-000000000000"


def _ashby_client(graphql: MagicMock, board: MagicMock | Exception | None = None) -> MagicMock:
    """GraphQL answers `graphql` (POST); the public job list answers `board`
    (GET; None: it must not be asked)."""
    client = MagicMock()
    client.post.return_value = graphql

    def get(url: str, **kw: Any) -> MagicMock:
        assert board is not None, f"the job list must not be asked: {url}"
        assert url == "https://api.ashbyhq.com/posting-api/job-board/acme", url
        assert kw["headers"]["User-Agent"].startswith("jobkit/")
        if isinstance(board, Exception):
            raise board
        return board

    client.get.side_effect = get
    return client


def test_ashby_null_job_posting_not_on_the_board_is_gone() -> None:
    """jobPosting: null (hand-built: the endpoint takes a POST), confirmed by
    the public job list (hand-built), which does not list the posting."""
    client = _ashby_client(
        _resp(200, _fixture("ashby_job_posting_null.json")),
        _resp(200, _fixture("ashby_board_list.json")),
    )
    assert _ashby_form(client, "acme", UNLISTED) == PostingGone(
        "ashby-jobPosting-null-and-not-on-board"
    )


@pytest.mark.parametrize(
    "board",
    [
        _resp(404, {}),  # no such board (or the token moved)
        _resp(500, {}),
        _resp(429, {}),
        _resp(200, ValueError("not json")),
        _resp(200, {"apiVersion": "1"}),  # no job list
        httpx.ConnectTimeout("timed out"),
    ],
    ids=["board-404", "board-500", "board-429", "not-json", "no-jobs", "timeout"],
)
def test_ashby_null_job_posting_without_the_lists_confirmation_is_not_gone(board: Any) -> None:
    client = _ashby_client(_resp(200, _fixture("ashby_job_posting_null.json")), board)
    assert _ashby_form(client, "acme", UNLISTED) is None


_BOARD_LISTED = _resp(200, _fixture("ashby_board_list.json"))


def test_a_still_listed_posting_is_never_gone_whatever_graphql_says() -> None:
    client = _ashby_client(_resp(200, _fixture("ashby_job_posting_null.json")), _BOARD_LISTED)
    assert _ashby_form(client, "acme", LISTED) is None
    # the id compares whatever its case
    assert _ashby_form(client, "acme", LISTED.upper()) is None


@pytest.mark.parametrize("status", [404, 410, 403, 500, 503])
def test_an_ashby_graphql_http_error_is_never_gone(status: int) -> None:
    """Review 2026-10-12: one shared GraphQL endpoint serves every posting, so
    a 404/410 there says the endpoint moved, not that this posting is gone.
    The job list is not even asked; the job keeps the dom_scrape fallback."""
    client = _ashby_client(_resp(status, {}))
    assert _ashby_form(client, "acme", UNLISTED) is None
    client.get.assert_not_called()


def test_an_ashby_graphql_answer_that_is_not_json_is_not_gone() -> None:
    client = _ashby_client(_resp(200, ValueError("not json")))
    assert _ashby_form(client, "acme", UNLISTED) is None


def test_ashby_form_still_parses() -> None:
    client = MagicMock()
    client.post.return_value = _resp(200, json.loads((FIXTURES / "ashby_form.json").read_text()))
    fields = _ashby_form(client, "oscorp", "x")
    assert isinstance(fields, list) and fields


# ---- capture closes the job; tailoring and staging never see it -----------------


class StubMatcher:
    bank: dict[str, Any] = {"scaffolds": []}

    def match(self, label: str, compliance_type: str | None = None, **kw: Any) -> dict[str, Any]:
        return {"kind": "identity", "entry_id": "first_name", "source": "answer_bank"}


def _job(conn: sqlite3.Connection, ext: str, status: str) -> int:
    jid = conn.execute(
        """INSERT INTO jobs (board_id, company_id, external_id, title, normalized_title,
           company_name, normalized_company, location_bucket, cluster_key, jd_html, jd_hash,
           apply_url, status)
           VALUES (1, 1, ?, 'Account Executive', 'ae', 'Acme', 'acme', 'remote-us', ?,
                   '<p>JD</p>', 'h', 'https://job-boards.greenhouse.io/acme/jobs/' || ?, ?)""",
        (ext, f"k{ext}", ext, status),
    ).lastrowid
    assert jid is not None
    return int(jid)


@pytest.fixture()
def board(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.execute("INSERT INTO companies (id, name, normalized_name) VALUES (1, 'Acme', 'acme')")
    conn.execute(
        "INSERT INTO boards (id, company_id, ats, token) VALUES (1, 1, 'greenhouse', 'acme')"
    )
    conn.commit()
    return conn


def test_capture_closes_a_gone_posting_and_writes_no_form_spec(
    board: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = board
    gone = _job(conn, "111", "approved")
    conn.execute("INSERT INTO applications (job_id, stage) VALUES (?, 'approved')", (gone,))
    alive = _job(conn, "222", "approved")
    conn.execute("INSERT INTO applications (job_id, stage) VALUES (?, 'approved')", (alive,))
    broken = _job(conn, "333", "queued")
    conn.commit()
    calls: list[str] = []

    def fetch(client: Any, token: str, external_id: str) -> Any:
        calls.append(external_id)
        if external_id == "111":
            return PostingGone("greenhouse-http-404")
        if external_id == "333":
            return None  # a 500 / timeout / unparseable page: not a definite answer
        return [
            {
                "identity": "first_name",
                "label": "First Name",
                "type": "input_text",
                "required": True,
                "options": [],
                "compliance_type": None,
            }
        ]

    monkeypatch.setattr(form_spec, "BankMatcher", StubMatcher)
    monkeypatch.setitem(form_spec.FETCHERS, "greenhouse", fetch)
    stats = form_spec.capture_form_specs(conn, draft=False)
    assert stats["posting_gone"] == 1 and stats["captured"] == 1 and stats["dom_fallback"] == 1

    row = conn.execute("SELECT status, closed_at FROM jobs WHERE id=?", (gone,)).fetchone()
    assert row["status"] == "closed" and row["closed_at"]
    ev = conn.execute(
        "SELECT kind, detail FROM events WHERE job_id=? AND kind='posting_gone'", (gone,)
    ).fetchone()
    assert json.loads(ev["detail"]) == {
        "ats": "greenhouse",
        "token": "acme",
        "external_id": "111",
        "signal": "greenhouse-http-404",
    }
    specs = {r[0]: r[1] for r in conn.execute("SELECT job_id, source FROM form_specs")}
    assert gone not in specs
    assert specs == {alive: "api_questions", broken: "dom_scrape"}
    # any other failure keeps the job open, as before
    assert conn.execute("SELECT closed_at FROM jobs WHERE id=?", (broken,)).fetchone()[0] is None
    logged = [
        json.loads(line)
        for line in (jobkit_home / "logs" / "jobkit.jsonl").read_text().splitlines()
        if '"form_spec.posting_gone"' in line
    ]
    assert logged and logged[0]["job_id"] == gone and logged[0]["signal"] == "greenhouse-http-404"

    # a second run never fetches the closed job again
    calls.clear()
    form_spec.capture_form_specs(conn, draft=False)
    assert "111" not in calls

    # tailoring skips it
    from jobkit.pipeline import tailor

    tailored: list[int] = []
    monkeypatch.setattr(tailor, "tailor_job", lambda c, jid, **kw: tailored.append(jid))
    tailor.tailor_approved(conn)
    assert tailored == [alive]  # the open approved job is tailored; the gone one never

    # and MCP get_job says why it closed
    from jobkit.autofill import browser

    lines = browser.staging_lines(conn, gone)
    assert lines and "posting is gone (greenhouse-http-404)" in lines[0]


def test_a_job_already_closed_is_not_closed_again(board: sqlite3.Connection) -> None:
    conn = board
    jid = _job(conn, "444", "applied")
    row = {"id": jid, "ats": "greenhouse", "token": "acme", "external_id": "444"}
    assert not form_spec.close_gone_posting(conn, row, PostingGone("greenhouse-http-404"))
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "applied"
    assert conn.execute(db.POSTING_GONE_EVENT, {"job_id": jid}).fetchone() is None
