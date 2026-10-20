"""One path from an employer's ATS board to jobs rows, polled now.

`jobkit add-url` (MCP add_job_url) and LinkedIn guest discovery
(discovery/linkedin_guest.py) both use it, so a job found anywhere enters
jobkit the same way: as the employer's own ATS record, never as a copy of the
page it was found on (hard rule 10).

* ensure_board: the (ats, token) board row. An existing board is used as it
  is, with its company row, so the tier the user set in targets.yaml is never
  touched. A new board gets a company row added at tier 3 only if none exists
  (db.UPSERT_COMPANY_IF_NEW, the rule feeds and discovery follow since
  2026-10-12). Before this module, add-url went through poll.seed_targets,
  whose UPSERT_COMPANY reset an existing company to tier 3.
* poll_board: fetch and upsert that one board (pipeline/poll.poll_selected:
  ETag, retries, raw cache, vanished postings closed). Only that board: add-url
  used to run the full poll, which also re-polled up to 150 discovered boards.
* ingest_url: url_resolver.resolve, then poll_board for a supported ATS, then
  the posting the URL names (when it names one).

Nothing here changes a job's status. The add-url command puts the job a human
pasted in the queue itself (db.QUEUE_PASTED_JOB); discovered jobs go through
the filter and triage like any other new row."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from jobkit import db as q
from jobkit.adapters import ADAPTERS
from jobkit.discovery.url_resolver import ResolvedUrl, resolve
from jobkit.log import log
from jobkit.normalize import normalize_company
from jobkit.pipeline.poll import poll_selected


@dataclass(frozen=True)
class BoardPoll:
    ats: str
    token: str
    board_id: int
    company_id: int
    board_added: bool  # the board row did not exist before
    status: str  # ok | not_modified | failed
    jobs_seen: int
    jobs_new: int

    @property
    def ok(self) -> bool:
        return self.status != "failed"

    def describe(self) -> str:
        added = " (board added)" if self.board_added else ""
        return (
            f"{self.ats}/{self.token}{added}: {self.status}, "
            f"{self.jobs_seen} open posting(s), {self.jobs_new} new"
        )


def ensure_board(
    conn: sqlite3.Connection, ats: str, token: str, company_name: str
) -> tuple[dict[str, Any], bool]:
    """The board dict poll_selected takes, and whether it was just added."""
    row = conn.execute(q.BOARD_BY_ATS_TOKEN, {"ats": ats, "token": token}).fetchone()
    if row is not None:
        return dict(row), False
    name = company_name.strip() or token
    crow = conn.execute(
        q.UPSERT_COMPANY_IF_NEW,
        {"name": name, "normalized_name": normalize_company(name), "domain": None, "tier": 3},
    ).fetchone()
    brow = conn.execute(
        q.UPSERT_BOARD,
        {"company_id": crow["id"], "ats": ats, "token": token, "verified_at": None},
    ).fetchone()
    conn.commit()
    company = conn.execute("SELECT name FROM companies WHERE id = ?", (crow["id"],)).fetchone()
    log("ingest.board_added", ats=ats, token=token, company=company["name"])
    return (
        {
            "board_id": brow["id"],
            "company_id": crow["id"],
            "company_name": company["name"],
            "ats": ats,
            "token": token,
        },
        True,
    )


async def poll_board(
    conn: sqlite3.Connection,
    ats: str,
    token: str,
    company_name: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
) -> BoardPoll:
    """Ensure the board exists and poll it, now. `ats` must be a supported
    ATS (adapters.ADAPTERS)."""
    if ats not in ADAPTERS:
        raise ValueError(f"no board adapter for {ats!r} (supported: {sorted(ADAPTERS)})")
    board, added = ensure_board(conn, ats, token, company_name)
    stats = await poll_selected(conn, [board], transport=transport)
    if stats["failed"]:
        status = "failed"
    elif stats["not_modified"]:
        status = "not_modified"
    else:
        status = "ok"
        conn.execute(q.SET_BOARD_VERIFIED_IF_UNSET, {"board_id": board["board_id"]})
        conn.commit()
    result = BoardPoll(
        ats=ats,
        token=token,
        board_id=int(board["board_id"]),
        company_id=int(board["company_id"]),
        board_added=added,
        status=status,
        jobs_seen=stats["jobs_seen"],
        jobs_new=stats["jobs_new"],
    )
    log("ingest.board_polled", **asdict(result))
    return result


def job_on_board(conn: sqlite3.Connection, board_id: int, external_id: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        q.JOB_ON_BOARD, {"board_id": board_id, "external_id": external_id}
    ).fetchone()
    return row


@dataclass(frozen=True)
class UrlIngest:
    url: str
    resolved: ResolvedUrl | None  # None: no ATS recognized in the URL
    poll: BoardPoll | None  # None: not a supported ATS, or no board token in the URL
    job: sqlite3.Row | None  # the posting the URL names, when it was in the board feed

    @property
    def status(self) -> str:
        if self.resolved is None:
            return "unrecognized"
        if self.poll is None:
            return "manual_ats"
        if not self.poll.ok:
            return "poll_failed"
        if not self.resolved.external_id:
            return "board_polled"
        return "job_found" if self.job is not None else "job_not_in_feed"


async def ingest_url(
    conn: sqlite3.Connection,
    url: str,
    company_name: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
) -> UrlIngest:
    """Resolve the employer ATS in `url`; for a supported one, poll its board
    now and find the posting the URL names. `company_name` names a new
    company row (default: the board token)."""
    hit = resolve(url)
    if hit is None or not hit.supported or not hit.token or hit.ats not in ADAPTERS:
        log("ingest.url_not_polled", url=url, ats=hit.ats if hit else None)
        return UrlIngest(url=url, resolved=hit, poll=None, job=None)
    polled = await poll_board(conn, hit.ats, hit.token, company_name, transport=transport)
    job = (
        job_on_board(conn, polled.board_id, hit.external_id)
        if polled.ok and hit.external_id
        else None
    )
    return UrlIngest(url=url, resolved=hit, poll=polled, job=job)
