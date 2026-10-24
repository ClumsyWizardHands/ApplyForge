"""Repost sites are kept out (discovery/reposters.py, owner decision D5).

A repost site publishes other companies' jobs under its own name ("Our
partner is looking for..."). The built-in list (registries/reposters.yml)
and a wording heuristic keep such boards out of discovery, board resolution,
polling and add-url, and the digest says so. Ordinary employer boards are
untouched.

Offline: a MockTransport serves two hand-built, fictional Lever boards from
tests/fixtures/reposters/: Hirewave (a repost site, not on the built-in list:
four of five postings hide the employer) and Northwind (an ordinary employer
whose copy mentions "our partners" and "our clients")."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any

import httpx
import pytest
import yaml

from jobkit import db as q
from jobkit.config import Filters
from jobkit.discovery import board_resolver as br
from jobkit.discovery import ingest, linkedin_guest, registry, reposters
from jobkit.normalize import normalize_company
from jobkit.pipeline.filter import run_filters
from jobkit.pipeline.poll import poll_selected
from tests.conftest import FIXTURES, REPO_ROOT
from tests.test_board_resolver import Boards, feed_row, run

FIX = FIXTURES / "reposters"
HIREWAVE = json.loads((FIX / "lever_hirewave.json").read_text())
NORTHWIND = json.loads((FIX / "lever_northwind.json").read_text())


class LeverBoards:
    """The public Lever list API for a few fictional boards; 404 otherwise."""

    def __init__(self, boards: dict[str, Any]) -> None:
        self.boards = boards
        self.tokens: list[str] = []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.lever.co", request.url
        token = request.url.path.split("/")[3].lower()
        self.tokens.append(token)
        if token not in self.boards:
            return httpx.Response(404, json={"ok": False, "error": "Document not found"})
        return httpx.Response(200, json=self.boards[token])


def board_row(conn: sqlite3.Connection, company: str, ats: str, token: str) -> dict[str, Any]:
    crow = conn.execute(
        q.UPSERT_COMPANY_IF_NEW,
        {"name": company, "normalized_name": normalize_company(company), "domain": None, "tier": 3},
    ).fetchone()
    brow = conn.execute(
        q.UPSERT_BOARD, {"company_id": crow["id"], "ats": ats, "token": token, "verified_at": None}
    ).fetchone()
    conn.commit()
    return {
        "board_id": int(brow["id"]),
        "company_id": int(crow["id"]),
        "company_name": company,
        "ats": ats,
        "token": token,
    }


def board_status(conn: sqlite3.Connection, token: str) -> str:
    row = conn.execute("SELECT status FROM boards WHERE token = ?", (token,)).fetchone()
    return str(row["status"])


def jobs_on(conn: sqlite3.Connection, board_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM jobs WHERE board_id = ? ORDER BY id", (board_id,)).fetchall()


def reposter_events(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT detail FROM events WHERE kind = 'board_reposter' ORDER BY id")
    return [json.loads(r["detail"]) for r in rows]


def insert_job(conn: sqlite3.Connection, board: dict[str, Any], ext: str, status: str) -> int:
    cur = conn.execute(
        """INSERT INTO jobs (board_id, company_id, external_id, title, normalized_title,
           company_name, normalized_company, location_raw, location_bucket, cluster_key,
           jd_html, jd_hash, apply_url, status)
           VALUES (?, ?, ?, 'Director of Sales', 'director of sales', ?, ?, 'Remote - US',
           'remote-us', ?, '<p>x</p>', ?, 'https://jobs.lever.co/x', ?)""",
        (
            board["board_id"],
            board["company_id"],
            ext,
            board["company_name"],
            normalize_company(board["company_name"]),
            f"k{ext}",
            f"h{ext}",
            status,
        ),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


# ---------------------------------------------------------------------------
# the built-in list
# ---------------------------------------------------------------------------


def test_the_built_in_list_names_jobgether_by_name_and_board() -> None:
    assert reposters.named("Jobgether") == "Jobgether"
    assert reposters.named("jobgether") == "Jobgether"
    assert reposters.listed_board("lever", "JobGether") == "Jobgether"
    assert reposters.named("Jobright") == "Jobright.ai"  # an alias
    assert reposters.named("Northwind") is None
    assert reposters.named("") is None
    assert reposters.listed_board("lever", "northwind") is None


def test_every_list_entry_says_how_it_reposts() -> None:
    data = yaml.safe_load((REPO_ROOT / "registries" / "reposters.yml").read_text())
    assert data["heuristic"]["min_postings"] >= 1
    assert 0 < data["heuristic"]["share"] < 1
    for entry in data["reposters"]:
        assert str(entry.get("name") or "").strip()
        assert len(str(entry.get("note") or "")) > 20, entry


# ---------------------------------------------------------------------------
# the wording heuristic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "<p>This position is listed on behalf of a partner company. Our partner is looking "
        "for a Director of Sales.</p>",
        "Our client is seeking an Account Executive.",
        "Our partner company is hiring a Deal Desk Lead.",
        "Our clients are recruiting sales leaders.",
        "We are hiring on behalf of one of our clients, a software company.",
        "Posted by Hirewave on behalf of a fast-growing software company.",
        "&lt;p&gt;Our partner is looking for a Sales Manager.&lt;/p&gt;",  # escaped HTML
    ],
)
def test_repost_wording_is_recognised(text: str) -> None:
    assert reposters.says_partner(text)


@pytest.mark.parametrize(
    "text",
    [
        "You will work with our partners to close enterprise deals.",
        "Serve our clients across the East Coast.",
        "Our client success team is looking forward to working with you.",
        "Our partners are the heart of our channel.",
        "Approve non-standard terms on behalf of the sales team.",
        "You will present to our clients' finance teams.",
        "",
    ],
)
def test_ordinary_employer_copy_is_not_repost_wording(text: str) -> None:
    assert not reposters.says_partner(text)


def test_the_heuristic_flags_a_board_where_most_postings_hide_the_employer() -> None:
    why = reposters.heuristic(p["description"] for p in HIREWAVE)
    assert why is not None and "4 of 5" in why
    assert reposters.heuristic(p["description"] for p in NORTHWIND) is None


def test_the_heuristic_needs_most_postings_and_enough_of_them() -> None:
    partner = "<p>Our partner is looking for a Sales Director.</p>"
    plain = "<p>Join our sales team.</p>"
    assert reposters.heuristic([partner, partner, plain, plain]) is None  # half is not most
    assert reposters.heuristic([partner, partner]) is None  # too few to judge
    assert reposters.heuristic([partner, partner, "", None, ""]) is None  # blanks not counted
    assert reposters.heuristic([partner, partner, plain]) is not None


def test_owner_listed_boards_are_judged_only_by_the_built_in_list() -> None:
    descriptions = [p["description"] for p in HIREWAVE]
    assert reposters.verdict("lever", "hirewave", "Hirewave", descriptions) is not None
    assert (
        reposters.verdict("lever", "hirewave", "Hirewave", descriptions, owner_listed=True) is None
    )
    assert reposters.verdict("lever", "jobgether", "Jobgether", (), owner_listed=True)


# ---------------------------------------------------------------------------
# polling
# ---------------------------------------------------------------------------


def test_poll_keeps_a_repost_board_out_and_leaves_an_employer_board_alone(
    conn: sqlite3.Connection,
) -> None:
    lever = LeverBoards({"hirewave": HIREWAVE, "northwind": NORTHWIND})
    hirewave = board_row(conn, "Hirewave", "lever", "hirewave")
    northwind = board_row(conn, "Northwind", "lever", "northwind")
    undecided = insert_job(conn, hirewave, "old-1", "filtered")
    approved = insert_job(conn, hirewave, "old-2", "approved")

    stats = asyncio.run(poll_selected(conn, [hirewave, northwind], transport=lever.transport))

    assert stats["reposters"] == 1 and stats["ok"] == 1
    assert board_status(conn, "hirewave") == "stale"
    assert board_status(conn, "northwind") == "active"
    # nothing from the repost board entered jobkit
    assert [r["external_id"] for r in jobs_on(conn, hirewave["board_id"])] == ["old-1", "old-2"]
    assert len(jobs_on(conn, northwind["board_id"])) == len(NORTHWIND)
    # the undecided job is rejected and logged; a human's approval is left alone
    assert conn.execute(q.JOB_STATUS, {"job_id": undecided}).fetchone()["status"] == "rejected"
    reason = conn.execute(
        "SELECT layer, reason FROM rejections WHERE job_id = ?", (undecided,)
    ).fetchone()
    assert (reason["layer"], reason["reason"]) == ("filter", "reposter:Hirewave")
    assert conn.execute(q.JOB_STATUS, {"job_id": approved}).fetchone()["status"] == "approved"
    (event,) = [e for e in reposter_events(conn) if "rejected" not in e]
    assert (event["ats"], event["token"], event["where"]) == ("lever", "hirewave", "poll")
    lines = br.digest_lines(conn)
    assert "## Repost sites kept out" in lines
    assert any("Hirewave (lever/hirewave)" in ln and "1 of its open job(s)" in ln for ln in lines)


def test_a_listed_repost_board_is_never_fetched(conn: sqlite3.Connection) -> None:
    lever = LeverBoards({"jobgether": HIREWAVE, "northwind": NORTHWIND})
    jobgether = board_row(conn, "Jobgether", "lever", "jobgether")
    northwind = board_row(conn, "Northwind", "lever", "northwind")

    stats = asyncio.run(poll_selected(conn, [jobgether, northwind], transport=lever.transport))

    assert lever.tokens == ["northwind"]
    assert stats["reposters"] == 1 and stats["boards"] == 2
    assert board_status(conn, "jobgether") == "stale"
    assert jobs_on(conn, jobgether["board_id"]) == []


def test_a_board_listed_in_targets_is_exempt_from_the_wording_check(
    conn: sqlite3.Connection,
) -> None:
    # tests/fixtures/config/targets.yaml lists lever/hooli
    lever = LeverBoards({"hooli": HIREWAVE})
    hooli = board_row(conn, "Hooli", "lever", "hooli")

    stats = asyncio.run(poll_selected(conn, [hooli], transport=lever.transport))

    assert "reposters" not in stats
    assert board_status(conn, "hooli") == "active"
    assert len(jobs_on(conn, hooli["board_id"])) == len(HIREWAVE)
    assert reposter_events(conn) == []


def test_an_employer_poll_reports_the_same_stats_as_before(conn: sqlite3.Connection) -> None:
    lever = LeverBoards({"northwind": NORTHWIND})
    northwind = board_row(conn, "Northwind", "lever", "northwind")
    stats = asyncio.run(poll_selected(conn, [northwind], transport=lever.transport))
    assert stats == {
        "boards": 1,
        "ok": 1,
        "not_modified": 0,
        "failed": 0,
        "jobs_seen": len(NORTHWIND),
        "jobs_new": len(NORTHWIND),
    }


# ---------------------------------------------------------------------------
# add-url and discovery's board poll (discovery/ingest.py)
# ---------------------------------------------------------------------------


def test_ingest_never_adds_a_listed_repost_board(conn: sqlite3.Connection) -> None:
    lever = LeverBoards({"jobgether": HIREWAVE})
    polled = asyncio.run(ingest.poll_board(conn, "lever", "jobgether", "", lever.transport))
    assert polled.status == "reposter" and not polled.ok
    assert "Jobgether is a repost site" in polled.describe()
    assert conn.execute("SELECT COUNT(*) AS n FROM boards").fetchone()["n"] == 0
    assert lever.tokens == []

    url = f"https://jobs.lever.co/jobgether/{HIREWAVE[0]['id']}"
    res = asyncio.run(ingest.ingest_url(conn, url, transport=lever.transport))
    assert res.status == "reposter" and res.job is None


def test_add_url_on_a_listed_repost_board_says_so_and_adds_nothing(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import functools

    from typer.testing import CliRunner

    from jobkit import cli

    lever = LeverBoards({"jobgether": HIREWAVE})
    monkeypatch.setattr(cli, "_db", lambda: conn)
    monkeypatch.setattr(
        cli, "ingest_url", functools.partial(cli.ingest_url, transport=lever.transport)
    )
    url = f"https://jobs.lever.co/jobgether/{HIREWAVE[0]['id']}"
    res = CliRunner().invoke(cli.app, ["add-url", url])
    assert res.exit_code == 0, res.output
    assert "not added:" in res.output and "Jobgether is a repost site" in res.output
    assert "wasn't in the public feed" not in res.output
    assert conn.execute("SELECT COUNT(*) AS n FROM boards").fetchone()["n"] == 0
    assert lever.tokens == []


def test_ingest_sets_a_board_that_reads_like_reposts_stale(conn: sqlite3.Connection) -> None:
    lever = LeverBoards({"hirewave": HIREWAVE})
    polled = asyncio.run(ingest.poll_board(conn, "lever", "hirewave", "Hirewave", lever.transport))
    assert polled.status == "reposter" and not polled.ok
    assert board_status(conn, "hirewave") == "stale"
    assert jobs_on(conn, polled.board_id) == []


# ---------------------------------------------------------------------------
# discovery (registry.resolve_discovery_queue)
# ---------------------------------------------------------------------------


def _queue(conn: sqlite3.Connection, *names: str) -> None:
    for name in names:
        conn.execute(
            "INSERT INTO discovery_queue (company_name, normalized_name, source) "
            "VALUES (?, ?, 'adzuna')",
            (name, normalize_company(name)),
        )
    conn.commit()


def test_discovery_skips_repost_sites_and_blocklisted_companies(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    fictional = {
        "jobgether": ("lever", "jobgether"),
        "initech": ("greenhouse", "initech"),
        "northwind": ("lever", "northwind"),
    }
    monkeypatch.setattr(registry, "_index", lambda: fictional)
    _queue(conn, "Jobgether", "Initech", "Northwind")
    filters = Filters(raw={"filters": {"blocklist_companies": ["Initech"]}})

    stats = registry.resolve_discovery_queue(conn, filters=filters)

    assert stats == {"checked": 3, "found": 1, "not_found": 0, "skipped": 2}
    boards = {r["token"] for r in conn.execute("SELECT token FROM boards")}
    assert boards == {"northwind"}
    status = {
        r["company_name"]: r["status"]
        for r in conn.execute("SELECT company_name, status FROM discovery_queue")
    }
    assert status == {"Jobgether": "skipped", "Initech": "skipped", "Northwind": "found"}
    # the repost site is named in the digest; a blocklisted company is the person's own choice
    assert [e["company"] for e in reposter_events(conn)] == ["Jobgether"]
    assert any("Jobgether" in ln for ln in reposters.digest_lines(conn))


def test_the_digest_names_every_boardless_repost_site_kept_out(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two listed repost sites skipped by name on one day, neither with a
    board in the registry: both are named (review 2026-10-23: both were
    grouped under one empty board key and only the first was named)."""
    monkeypatch.setattr(registry, "_index", lambda: {})
    _queue(conn, "Jobgether", "Lensa")
    registry.resolve_discovery_queue(conn, filters=Filters(raw={"filters": {}}))
    lines = reposters.digest_lines(conn)
    assert any("Jobgether" in ln for ln in lines), lines
    assert any("Lensa" in ln for ln in lines), lines


def test_discovery_without_skips_reports_the_same_stats(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(registry, "_index", lambda: {"northwind": ("lever", "northwind")})
    _queue(conn, "Northwind")
    assert registry.resolve_discovery_queue(conn) == {"checked": 1, "found": 1, "not_found": 0}


# ---------------------------------------------------------------------------
# board resolution
# ---------------------------------------------------------------------------


def test_board_resolution_never_looks_up_a_repost_site_or_a_blocklisted_company(
    conn: sqlite3.Connection,
) -> None:
    boards = Boards({("lever", "jobgether"): HIREWAVE})
    gether = feed_row(conn, "Jobgether", "Director of Sales", "Remote - US")
    blocked = feed_row(conn, "Initech", "Director of Sales", "Remote - US")
    filters = Filters(raw={"filters": {"blocklist_companies": ["initech"]}})

    rep = run(conn, boards, filters)

    assert boards.requests == []
    assert rep.skipped == 2
    text = "\n".join(rep.lines)
    assert "Jobgether is a repost site, not the employer: skipped" in text
    assert "on your blocklist (initech" in text
    for job_id in (gether, blocked):
        row = conn.execute("SELECT closed_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert row["closed_at"] is None  # left as it was: nothing merged


def test_board_resolution_drops_a_board_that_reads_like_reposts(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A company whose registry board turns out to be a repost board: the
    board is not picked and no posting from it enters jobkit."""
    monkeypatch.setattr(
        registry,
        "lookup",
        lambda name: ("lever", "hirewave") if normalize_company(name) == "hirewave" else None,
    )
    boards = Boards({("lever", "hirewave"): HIREWAVE})
    job_id = feed_row(conn, "Hirewave", "Director of Sales", "United States")

    rep = run(conn, boards)

    assert ("lever", "hirewave") in boards.tokens()
    assert rep.merged == 0
    assert any("lever/hirewave: kept out" in ln for ln in rep.lines)
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE board_id IS NOT NULL").fetchone()["n"]
        == 0
    )
    assert conn.execute("SELECT closed_at FROM jobs WHERE id = ?", (job_id,)).fetchone()[0] is None
    assert [e["where"] for e in reposter_events(conn)] == ["board resolution"]


# ---------------------------------------------------------------------------
# the filter and LinkedIn cards
# ---------------------------------------------------------------------------


def test_the_filter_rejects_jobs_filed_under_a_repost_site(conn: sqlite3.Connection) -> None:
    gether = feed_row(conn, "Jobgether", "Director of Sales", "Remote - US", status="new")
    dice = feed_row(conn, "Jobs via Dice", "Director of Sales", "Remote - US", status="new")
    employer = feed_row(conn, "Northwind", "Director of Sales", "Remote - US", status="new")
    filters = Filters(raw={"filters": {"locations": {"accept_remote": True}}})

    stats = run_filters(conn, filters)

    assert stats["rejected"] == 2 and stats["passed"] == 1
    reasons = {
        r["job_id"]: r["reason"] for r in conn.execute("SELECT job_id, reason FROM rejections")
    }
    assert reasons[gether] == "reposter:Jobgether"
    assert reasons[dice] == "reposter:Jobs via Dice"
    assert employer not in reasons


def test_a_linkedin_card_from_a_repost_site_is_rejected_before_any_request() -> None:
    card = linkedin_guest.Card(
        li_id="4100000001", title="Director of Sales", company="Lensa", location="", listed=None
    )
    assert linkedin_guest.card_verdict(card, Filters(raw={})) == "reposter:Lensa"
    ok = linkedin_guest.Card(
        li_id="4100000002", title="Director of Sales", company="Northwind", location="", listed=None
    )
    assert linkedin_guest.card_verdict(ok, Filters(raw={})) is None
