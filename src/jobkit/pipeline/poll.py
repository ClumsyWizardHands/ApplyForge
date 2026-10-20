"""Stage 1 — poll. Concurrent board fetch with ETags, raw JSON cache, idempotent
upsert, vanished postings marked closed. Concurrency 4, backoff on 429/5xx (SPEC §5)."""

from __future__ import annotations

import asyncio
import datetime
import json
import sqlite3
from typing import Any

import httpx

from jobkit import db as q
from jobkit import paths
from jobkit.adapters import ADAPTERS
from jobkit.config import Targets, load_targets
from jobkit.log import log
from jobkit.normalize import (
    cluster_key,
    jd_hash,
    location_bucket,
    normalize_company,
    normalize_title,
)
from jobkit.pipeline.dedup import feed_rows_for_board, merge_feed_row

RAW_DIR = paths.HOME / "data" / "raw"
CONCURRENCY = 4
RETRIES = 3


def seed_targets(conn: sqlite3.Connection, targets: Targets) -> list[dict[str, Any]]:
    """Ensure companies/boards rows exist. Returns pollable board dicts."""
    boards: list[dict[str, Any]] = []
    for c in targets.companies:
        row = conn.execute(
            q.UPSERT_COMPANY,
            {
                "name": c.name,
                "normalized_name": normalize_company(c.name),
                "domain": c.domain,
                "tier": c.tier,
            },
        ).fetchone()
        company_id = row["id"]
        if c.board is None:
            continue
        if c.board.ats not in ADAPTERS:
            log("poll.skip_unsupported_ats", company=c.name, ats=c.board.ats)
            continue
        brow = conn.execute(
            q.UPSERT_BOARD,
            {
                "company_id": company_id,
                "ats": c.board.ats,
                "token": c.board.token,
                "verified_at": datetime.date.today().isoformat(),
            },
        ).fetchone()
        boards.append(
            {
                "board_id": brow["id"],
                "company_id": company_id,
                "company_name": c.name,
                "ats": c.board.ats,
                "token": c.board.token,
            }
        )
    conn.commit()
    return boards


async def _fetch_board(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    board: dict[str, Any],
    etag: str | None,
) -> tuple[dict[str, Any], Any]:
    adapter = ADAPTERS[board["ats"]]
    async with sem:
        for attempt in range(RETRIES):
            try:
                result = await adapter.fetch_with(client, board["token"], etag)
                return board, result
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if code in (429, 500, 502, 503, 504) and attempt < RETRIES - 1:
                    await asyncio.sleep(2 ** (attempt + 1))
                    continue
                return board, e
            except httpx.HTTPError as e:
                if attempt < RETRIES - 1:
                    await asyncio.sleep(2 ** (attempt + 1))
                    continue
                return board, e
    return board, RuntimeError("unreachable")


def _cache_raw(ats: str, token: str, payload: Any) -> None:
    d = RAW_DIR / ats / token
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{datetime.date.today().isoformat()}.json").write_text(json.dumps(payload, default=str))


def upsert_jobs(conn: sqlite3.Connection, board: dict[str, Any], payload: Any) -> tuple[int, int]:
    """Parse + upsert one board's payload. Returns (jobs_seen, new_rows).

    An open aggregator row for the same posting (its apply URL names this
    board's job id) is merged into the ATS row: the ATS record is canonical
    (hard rule 10), whichever arrived first (pipeline/dedup.py)."""
    adapter = ADAPTERS[board["ats"]]
    jobs = adapter.parse(payload)
    seen_ids: list[str] = []
    new = 0
    feed_twins = feed_rows_for_board(conn, board["ats"], board["token"])
    for j in jobs:
        company = j.company_name or board["company_name"]
        params = {
            "board_id": board["board_id"],
            "company_id": board["company_id"],
            "external_id": j.external_id,
            "title": j.title,
            "normalized_title": normalize_title(j.title),
            "company_name": company,
            "normalized_company": normalize_company(company),
            "location_raw": j.location_raw,
            "location_bucket": location_bucket(j.location_raw, j.remote),
            "cluster_key": cluster_key(company, j.title, j.location_raw, j.remote),
            "remote": None if j.remote is None else int(j.remote),
            "comp_min": j.comp_min,
            "comp_max": j.comp_max,
            "comp_currency": j.comp_currency,
            "jd_html": j.jd_html,
            "jd_hash": jd_hash(j.jd_html),
            "apply_url": j.apply_url,
            "posted_at": j.posted_at,
        }
        existed = conn.execute(
            "SELECT 1 FROM jobs WHERE board_id=? AND external_id=?",
            (board["board_id"], j.external_id),
        ).fetchone()
        row = conn.execute(q.UPSERT_JOB, params).fetchone()
        conn.execute(
            q.UPSERT_SOURCE,
            {
                "job_id": row["id"],
                "source": board["ats"],
                "source_url": j.apply_url,
                "is_canonical": 1,
            },
        )
        for feed_id in feed_twins.get(j.external_id.lower(), []):
            merge_feed_row(conn, feed_id, row["id"])
        seen_ids.append(j.external_id)
        if not existed:
            new += 1
    conn.execute(q.MARK_VANISHED, {"board_id": board["board_id"], "seen_ids": json.dumps(seen_ids)})
    return len(jobs), new


DISCOVERED_BOARD_CAP = 150  # registry-discovered boards polled per run, newest first


async def poll(conn: sqlite3.Connection, targets: Targets | None = None) -> dict[str, int]:
    """Poll every configured board once, plus registry-discovered boards from
    the DB (cap DISCOVERED_BOARD_CAP). Idempotent: re-runs create no duplicates."""
    boards = seed_targets(conn, targets or load_targets())
    seeded_ids = {b["board_id"] for b in boards}
    discovered = conn.execute(
        """SELECT b.id AS board_id, b.company_id, c.name AS company_name, b.ats, b.token
           FROM boards b JOIN companies c ON c.id = b.company_id
           WHERE b.status = 'active' ORDER BY b.id DESC LIMIT ?""",
        (DISCOVERED_BOARD_CAP + len(seeded_ids),),
    ).fetchall()
    for row in discovered:
        if row["board_id"] not in seeded_ids and row["ats"] in ADAPTERS:
            boards.append(dict(row))
            seeded_ids.add(row["board_id"])
            if len(boards) >= len(load_targets().companies) + DISCOVERED_BOARD_CAP:
                break
    stats = await poll_selected(conn, boards)
    log("poll.done", **stats)
    return stats


async def poll_selected(
    conn: sqlite3.Connection,
    boards: list[dict[str, Any]],
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, int]:
    """Fetch and upsert exactly these boards (dicts with board_id, company_id,
    company_name, ats, token): ETag-conditional, retried on 429/5xx, raw
    payload cached, vanished postings closed. poll() runs it over every board;
    discovery/ingest.py over the one board a pasted or discovered URL names.
    `transport` is for tests (recorded fixtures)."""
    etags = {
        b["board_id"]: r["etag"]
        for b in boards
        if (r := conn.execute("SELECT etag FROM boards WHERE id=?", (b["board_id"],)).fetchone())
    }
    sem = asyncio.Semaphore(CONCURRENCY)
    stats = {
        "boards": len(boards),
        "ok": 0,
        "not_modified": 0,
        "failed": 0,
        "jobs_seen": 0,
        "jobs_new": 0,
    }
    async with httpx.AsyncClient(follow_redirects=True, transport=transport) as client:
        tasks = [_fetch_board(client, sem, b, etags.get(b["board_id"])) for b in boards]
        for coro in asyncio.as_completed(tasks):
            board, result = await coro
            if isinstance(result, Exception):
                stats["failed"] += 1
                conn.execute(q.BUMP_BOARD_FAILURE, {"board_id": board["board_id"]})
                log("poll.board_failed", ats=board["ats"], token=board["token"], error=str(result))
                continue
            if result.not_modified:
                stats["not_modified"] += 1
                conn.execute(
                    q.SET_BOARD_POLLED, {"etag": result.etag, "board_id": board["board_id"]}
                )
                continue
            _cache_raw(board["ats"], board["token"], result.payload)
            seen, new = upsert_jobs(conn, board, result.payload)
            stats["ok"] += 1
            stats["jobs_seen"] += seen
            stats["jobs_new"] += new
            conn.execute(q.SET_BOARD_POLLED, {"etag": result.etag, "board_id": board["board_id"]})
            log("poll.board_ok", ats=board["ats"], token=board["token"], jobs=seen, new=new)
            conn.commit()
    conn.commit()
    return stats
