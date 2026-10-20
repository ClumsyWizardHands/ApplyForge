"""Stage 1b — aggregator feeds. Discover jobs and companies beyond the target
list. Cross-source dedup on the canonical cluster key; the direct ATS record is
always canonical (CLAUDE.md rule 10). New companies auto-queue for board discovery.

Feeds are a firehose, so the deterministic title filter runs at ingest here —
every rejection logged to `rejections` with its reason (layer=filter)."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

import httpx

from jobkit import db as q
from jobkit.adapters.feeds import FEEDS
from jobkit.adapters.feeds.base import SearchSpec
from jobkit.config import Filters, load_filters
from jobkit.log import log
from jobkit.models import NormalizedJob
from jobkit.normalize import (
    cluster_key,
    jd_hash,
    location_bucket,
    normalize_company,
    normalize_title,
)

INSERT_REJECTION = """
INSERT INTO rejections (external_ref, layer, reason)
VALUES (:external_ref, 'filter', :reason)
"""

QUEUE_DISCOVERY = """
INSERT INTO discovery_queue (company_name, normalized_name, source, first_seen_url)
VALUES (:company_name, :normalized_name, :source, :url)
ON CONFLICT(normalized_name) DO NOTHING
"""


def _title_verdict(title: str, filters: Filters) -> str | None:
    """None = passes; otherwise the rejection reason.

    A title on a paused track (filters.yaml `active: false`) passes here on
    purpose: it is stored, and the filter pass rejects it as
    `track_paused:<id>` against the job row, so re-enabling the track and
    running refilter restores it. Rejected here it would leave no job row."""
    for pat in filters.exclude_titles:
        if re.search(pat, title):
            return f"exclude_title:{pat[:40]}"
    if filters.include_titles and not any(re.search(p, title) for p in filters.include_titles):
        return "no_include_title_match"
    return None


def _spec_for(source: str, filters: Filters) -> SearchSpec | None:
    cfg = filters.raw.get("feeds", {}).get(source, {})
    if not cfg.get("enabled", False):
        return None
    extra: dict[str, str] = {}
    series = cfg.get("series")
    if series:  # USAJOBS occupational series codes, one string or a list
        codes = series if isinstance(series, list) else [series]
        extra["series"] = ";".join(str(s) for s in codes)
    if cfg.get("pay_grade_low"):
        extra["pay_grade_low"] = str(cfg["pay_grade_low"])
    return SearchSpec(
        keywords=tuple(cfg.get("keywords", []) or cfg.get("what", [])),
        categories=tuple(cfg.get("categories", [])),
        remote_only=bool(cfg.get("remote_only", False)),
        max_days_old=int(cfg.get("max_days_old", 21)),
        salary_min=filters.lowest_comp_floor or None,
        extra=extra,
    )


def ingest_feed_jobs(
    conn: sqlite3.Connection, source: str, jobs: list[NormalizedJob], filters: Filters
) -> dict[str, int]:
    stats = {"seen": len(jobs), "rejected": 0, "merged": 0, "new": 0}
    for j in jobs:
        if not j.company_name or not j.title or not j.apply_url:
            stats["rejected"] += 1
            continue
        reason = _title_verdict(j.title, filters)
        ref = f"{source}/{j.external_id}"
        if reason:
            conn.execute(INSERT_REJECTION, {"external_ref": ref, "reason": reason})
            stats["rejected"] += 1
            continue
        ck = cluster_key(j.company_name, j.title, j.location_raw, j.remote)
        existing = conn.execute(
            "SELECT id FROM jobs WHERE cluster_key = ? "
            "ORDER BY (board_id IS NOT NULL) DESC LIMIT 1",
            (ck,),
        ).fetchone()
        if existing:
            # same posting already known — record provenance only. ATS stays canonical.
            conn.execute(
                q.UPSERT_SOURCE,
                {
                    "job_id": existing["id"],
                    "source": source,
                    "source_url": j.apply_url,
                    "is_canonical": 0,
                },
            )
            stats["merged"] += 1
            continue
        crow = conn.execute(
            q.UPSERT_COMPANY_IF_NEW,
            {
                "name": j.company_name,
                "normalized_name": normalize_company(j.company_name),
                "domain": None,
                "tier": 3,
            },
        ).fetchone()
        params: dict[str, Any] = {
            "board_id": None,
            "company_id": crow["id"],
            "external_id": j.external_id,
            "title": j.title,
            "normalized_title": normalize_title(j.title),
            "company_name": j.company_name,
            "normalized_company": normalize_company(j.company_name),
            "location_raw": j.location_raw,
            "location_bucket": location_bucket(j.location_raw, j.remote),
            "cluster_key": ck,
            "remote": None if j.remote is None else int(j.remote),
            "comp_min": j.comp_min,
            "comp_max": j.comp_max,
            "comp_currency": j.comp_currency,
            "jd_html": j.jd_html,
            "jd_hash": jd_hash(j.jd_html),
            "apply_url": j.apply_url,
            "posted_at": j.posted_at,
        }
        dupe = conn.execute(
            "SELECT id FROM jobs WHERE board_id IS NULL AND external_id = ? "
            "AND normalized_company = ?",
            (j.external_id, params["normalized_company"]),
        ).fetchone()
        if dupe:
            job_id = dupe["id"]
        else:
            cur = conn.execute(
                """INSERT INTO jobs (board_id, company_id, external_id, title,
                     normalized_title, company_name, normalized_company, location_raw,
                     location_bucket, cluster_key, remote, comp_min, comp_max,
                     comp_currency, jd_html, jd_hash, apply_url, posted_at)
                   VALUES (:board_id, :company_id, :external_id, :title,
                     :normalized_title, :company_name, :normalized_company,
                     :location_raw, :location_bucket, :cluster_key, :remote,
                     :comp_min, :comp_max, :comp_currency, :jd_html, :jd_hash,
                     :apply_url, :posted_at)""",
                params,
            )
            assert cur.lastrowid is not None
            job_id = cur.lastrowid
            stats["new"] += 1
            conn.execute(
                QUEUE_DISCOVERY,
                {
                    "company_name": j.company_name,
                    "normalized_name": params["normalized_company"],
                    "source": source,
                    "url": j.apply_url,
                },
            )
        conn.execute(
            q.UPSERT_SOURCE,
            {
                "job_id": job_id,
                "source": source,
                "source_url": j.apply_url,
                "is_canonical": 0,
            },
        )
    conn.commit()
    return stats


async def run_feeds(
    conn: sqlite3.Connection, filters: Filters | None = None
) -> dict[str, dict[str, int]]:
    filters = filters or load_filters()
    results: dict[str, dict[str, int]] = {}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for source, feed in FEEDS.items():
            spec = _spec_for(source, filters)
            if spec is None:
                continue
            try:
                jobs = await feed.search(client, spec)
                if not jobs and getattr(feed, "requires_key", False):
                    log("feeds.skipped_no_key", source=source)
                    results[source] = {
                        "seen": 0,
                        "rejected": 0,
                        "merged": 0,
                        "new": 0,
                        "skipped_no_key": 1,
                    }
                    continue
            except httpx.HTTPError as e:
                log("feeds.failed", source=source, error=str(e))
                results[source] = {"seen": 0, "rejected": 0, "merged": 0, "new": 0, "failed": 1}
                continue
            stats = ingest_feed_jobs(conn, source, jobs, filters)
            log("feeds.ok", source=source, **stats)
            results[source] = stats
    return results
