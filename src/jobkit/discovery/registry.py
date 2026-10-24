"""Board-token registry lookup (SPEC §2.4 strategy between manual and probe).
Data: registries/*.yml — freehire's MIT tenant lists (see registries/NOTICE.md),
~15K public board tokens across greenhouse/lever/ashby. When an aggregator
surfaces an unknown company, this resolves its direct board so polling switches
to the employer's own public API (aggregator rows are never canonical).

A queued company on the person's filters.blocklist_companies, or a repost
site (discovery/reposters.py: listed by name, or the registry board is a
listed reposter board), is never looked up: its queue row is marked
'skipped' and no board is added; a repost site is named in the digest."""

from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path

import yaml

from jobkit import db as q
from jobkit.config import Filters, load_filters
from jobkit.discovery import reposters
from jobkit.log import log
from jobkit.normalize import normalize_company

REGISTRY_DIR = Path("registries")
_ATS_ORDER = ("greenhouse", "lever", "ashby")  # canonical_source_priority order


@lru_cache(maxsize=1)
def _index() -> dict[str, tuple[str, str]]:
    """normalized company name -> (ats, board token). First ATS in priority wins."""
    idx: dict[str, tuple[str, str]] = {}
    for ats in reversed(_ATS_ORDER):  # earlier ATSs overwrite later ones
        path = REGISTRY_DIR / f"{ats}.yml"
        if not path.exists():
            continue
        for entry in yaml.safe_load(path.read_text()) or []:
            name = normalize_company(str(entry.get("company", "")))
            token = str(entry.get("board", "")).strip()
            if name and token:
                idx[name] = (ats, token)
    return idx


def lookup(company_name: str) -> tuple[str, str] | None:
    return _index().get(normalize_company(company_name))


def _filters_or_none() -> Filters | None:
    try:
        return load_filters()
    except Exception as e:  # noqa: BLE001 — no filters: no blocklist to honour
        log("discovery.filters_unavailable", error=str(e)[:150])
        return None


def resolve_discovery_queue(
    conn: sqlite3.Connection, limit: int = 200, filters: Filters | None = None
) -> dict[str, int]:
    """Resolve pending discovery-queue companies against the registry. Found →
    company+board rows created (tier 3); the next poll pulls their direct feed.
    Blocklisted companies and repost sites are skipped (module docstring)."""
    from jobkit.pipeline.filter import blocklist_match

    filters = filters or _filters_or_none()
    rows = conn.execute(
        "SELECT id, company_name, normalized_name FROM discovery_queue "
        "WHERE status='pending' LIMIT ?",
        (limit,),
    ).fetchall()
    # "skipped" appears only when a company was skipped
    stats = {"checked": len(rows), "found": 0, "not_found": 0}
    for r in rows:
        blocked = blocklist_match(r["company_name"], filters) if filters else None
        hit = _index().get(r["normalized_name"]) or lookup(r["company_name"])
        ats, token = hit if hit is not None else ("", "")
        why = reposters.built_in(ats, token, r["company_name"])
        if blocked or why:
            conn.execute(q.MARK_DISCOVERY_SKIPPED, {"id": r["id"]})
            stats["skipped"] = stats.get("skipped", 0) + 1
            if why:
                reposters.keep_out(
                    conn,
                    ats=ats,
                    token=token,
                    company=r["company_name"],
                    why=why,
                    where="discovery",
                )
            log(
                "discovery.skipped",
                company=r["company_name"],
                reason=f"blocklist_company:{blocked}" if blocked else why,
            )
            continue
        if hit is None:
            conn.execute(
                "UPDATE discovery_queue SET status='not_found', "
                "attempted_at=datetime('now') WHERE id=?",
                (r["id"],),
            )
            stats["not_found"] += 1
            continue
        ats, token = hit
        crow = conn.execute(
            q.UPSERT_COMPANY_IF_NEW,
            {
                "name": r["company_name"],
                "normalized_name": r["normalized_name"],
                "domain": None,
                "tier": 3,
            },
        ).fetchone()
        conn.execute(
            q.UPSERT_BOARD,
            {
                "company_id": crow["id"],
                "ats": ats,
                "token": token,
                "verified_at": None,  # registry-sourced; verified on first successful poll
            },
        )
        conn.execute(
            "UPDATE discovery_queue SET status='found', attempted_at=datetime('now') WHERE id=?",
            (r["id"],),
        )
        stats["found"] += 1
    conn.commit()
    if rows:
        log("discovery.resolved", **stats)
    return stats
