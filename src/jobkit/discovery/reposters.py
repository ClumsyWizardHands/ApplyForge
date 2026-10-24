"""Repost sites: boards and "companies" that publish other employers' jobs
(owner decision D5, 2026-10-23).

Jobgether reposted 4,737 jobs into jobkit as if it were an employer ("Our
partner is looking for...", no pay range), through its own Lever board, which
the board registry listed like any employer's. A repost is never the
employer's own record (hard rule 10) and its apply flow is not the employer's
form, so jobkit keeps repost sites out:

* a built-in list, registries/reposters.yml (public; its header documents the
  criterion): by company name (and aliases) and by (ats, token) board;
* a content heuristic for boards not on the list: a board with at least
  `heuristic.min_postings` described postings, MORE than `heuristic.share` of
  which use repost wording (PARTNER_WORDING: "our partner is looking for",
  "our client is seeking", "on behalf of a partner company", "posted ... on
  behalf of"). An ordinary employer that mentions "our partners" or "our
  clients" does not match: the wording must say the partner or client is the
  one hiring. A board the person listed in config/targets.yaml is never judged
  by the heuristic (the built-in list still applies).

Where it applies: discovery/registry.py (a queued company is not looked up),
discovery/board_resolver.py (a reposter is never looked up, its board never
picked), pipeline/poll.py and discovery/ingest.py (a reposter board is not
polled or added; one already in jobkit is set stale and its undecided open
jobs (new, filtered or queued; never approved) rejected as
`reposter:<name>`), pipeline/filter.py (a job filed under a reposter's name
is rejected), rules.is_employer_posting (a job on a stale or listed board is
never approved automatically). keep_out() records one `board_reposter` event
per board and day; digest_lines() turns them into the digest section."""

from __future__ import annotations

import datetime
import html
import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from jobkit import db as q
from jobkit.log import log
from jobkit.normalize import normalize_company

REPOSTERS_PATH = Path("registries") / "reposters.yml"
DEFAULT_MIN_POSTINGS = 3
DEFAULT_SHARE = 0.5

# Wording that says someone other than the board's owner is hiring. Each
# alternative names the partner/client AS the hirer, or says the posting is
# listed for someone else: "work with our partners" and "serve our clients"
# (ordinary employer copy) do not match.
PARTNER_WORDING = re.compile(
    r"\bour (?:partner|client)s?(?: company| organi[sz]ation| firm)? (?:is|are) "
    r"(?:looking|seeking|hiring|searching|recruiting)\b"
    r"|\bon behalf of (?:our|a|an|one of our) (?:partner|client|customer)s?\b"
    r"|\b(?:listed|posted|published|advertised) (?:here )?(?:by [\w .&'-]{1,40} )?"
    r"on behalf of\b",
    re.I,
)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Reposters:
    names: dict[str, str]  # normalized company name or alias -> display name
    boards: dict[tuple[str, str], str]  # (ats, lowercased token) -> display name
    min_postings: int = DEFAULT_MIN_POSTINGS
    share: float = DEFAULT_SHARE


def parse(data: Any) -> Reposters:
    """registries/reposters.yml's content -> Reposters. Entries with no name
    are ignored; a missing heuristic block uses the defaults."""
    data = data if isinstance(data, dict) else {}
    names: dict[str, str] = {}
    boards: dict[tuple[str, str], str] = {}
    for entry in data.get("reposters") or []:
        if not isinstance(entry, dict) or not str(entry.get("name") or "").strip():
            continue
        name = str(entry["name"]).strip()
        for alias in [name, *(entry.get("aliases") or [])]:
            norm = normalize_company(str(alias))
            if norm:
                names[norm] = name
        for b in entry.get("boards") or []:
            if isinstance(b, dict) and b.get("ats") and b.get("token"):
                boards[(str(b["ats"]).lower(), str(b["token"]).lower())] = name
    h = data.get("heuristic") or {}
    return Reposters(
        names=names,
        boards=boards,
        min_postings=int(h.get("min_postings", DEFAULT_MIN_POSTINGS)),
        share=float(h.get("share", DEFAULT_SHARE)),
    )


@lru_cache(maxsize=1)
def load() -> Reposters:
    """The built-in list. A missing or unreadable file is an empty list (the
    heuristic still runs on its defaults), logged."""
    try:
        return parse(yaml.safe_load(REPOSTERS_PATH.read_text()))
    except (OSError, yaml.YAMLError) as e:
        log("reposters.unavailable", path=str(REPOSTERS_PATH), error=str(e)[:200])
        return parse({})


def named(company: str | None) -> str | None:
    """The listed repost site this company name is, or None."""
    norm = normalize_company(company or "")
    return load().names.get(norm) if norm else None


def listed_board(ats: str, token: str) -> str | None:
    """The listed repost site that owns this board, or None."""
    return load().boards.get((ats.lower(), token.lower()))


def built_in(ats: str, token: str, company: str | None) -> str | None:
    """Why a board is a listed repost site ("Jobgether is a repost site"), or None."""
    who = listed_board(ats, token) or named(company)
    return f"{who} is a repost site (registries/reposters.yml)" if who else None


def says_partner(text: str | None) -> bool:
    """True when a posting's description uses repost wording."""
    plain = _WS.sub(" ", _TAGS.sub(" ", html.unescape(html.unescape(text or ""))))
    return PARTNER_WORDING.search(plain) is not None


def heuristic(descriptions: Iterable[str | None], cfg: Reposters | None = None) -> str | None:
    """Why a board's postings read like reposts, or None. Postings without a
    description are not counted (a list API without content says nothing)."""
    cfg = cfg or load()
    texts = [d for d in descriptions if d and _TAGS.sub("", d).strip()]
    if len(texts) < max(cfg.min_postings, 1):
        return None
    hits = sum(1 for t in texts if says_partner(t))
    if hits / len(texts) <= cfg.share:
        return None
    return (
        f"{hits} of {len(texts)} postings say another company is hiring "
        "('our partner/our client is looking for', 'on behalf of')"
    )


def verdict(
    ats: str,
    token: str,
    company: str | None,
    descriptions: Iterable[str | None] = (),
    *,
    owner_listed: bool = False,
) -> str | None:
    """Why this board is kept out, or None: the built-in list always; the
    heuristic only for a board the person did not list in targets.yaml."""
    why = built_in(ats, token, company)
    if why or owner_listed:
        return why
    return heuristic(descriptions)


def keep_out(
    conn: sqlite3.Connection,
    *,
    ats: str,
    token: str,
    company: str,
    why: str,
    where: str,
    board_id: int | None = None,
) -> int:
    """Keep a repost board out: set its boards row stale (when jobkit has
    one), reject its open jobs no one has acted on yet (layer filter, reason
    `reposter:<company>`), and record one `board_reposter` event per board
    and day for the digest. Returns the number of jobs rejected. Commits."""
    if board_id is None:
        row = conn.execute(q.BOARD_BY_ATS_TOKEN_NOCASE, {"ats": ats, "token": token}).fetchone()
        board_id = int(row["board_id"]) if row is not None else None
    rejected = 0
    if board_id is not None:
        conn.execute(q.SET_BOARD_STALE, {"board_id": board_id})
        reason = f"reposter:{company or token}"
        for r in conn.execute(q.UNDECIDED_OPEN_JOBS_ON_BOARD, {"board_id": board_id}).fetchall():
            conn.execute(q.REJECT_JOB, {"job_id": r["id"]})
            conn.execute(q.INSERT_FILTER_REJECTION, {"job_id": r["id"], "reason": reason})
            rejected += 1
    detail = {
        "ats": ats,
        "token": token.lower(),
        "company": company,
        "why": why,
        "where": where,
        "day": datetime.date.today().isoformat(),
    }
    conn.execute(
        q.RECORD_EVENT_ONCE,
        {
            "job_id": None,
            "kind": q.BOARD_REPOSTER_EVENT,
            "detail": json.dumps(detail, sort_keys=True),
        },
    )
    if rejected:
        conn.execute(
            q.RECORD_EVENT,
            {
                "job_id": None,
                "kind": q.BOARD_REPOSTER_EVENT,
                "detail": json.dumps({**detail, "rejected": rejected}, sort_keys=True),
            },
        )
    conn.commit()
    log(
        "reposters.kept_out",
        ats=ats,
        token=token,
        company=company,
        why=why,
        where=where,
        board_id=board_id,
        rejected=rejected,
    )
    return rejected


def digest_lines(conn: sqlite3.Connection, day: str | None = None) -> list[str]:
    """The digest section: each repost board kept out today, once, with the
    number of its jobs rejected."""
    day = day or datetime.date.today().isoformat()
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for r in conn.execute(q.BOARD_REPOSTER_EVENTS_ON_DAY, {"day": day}):
        d = json.loads(r["detail"] or "{}")
        ats, token = str(d.get("ats") or ""), str(d.get("token") or "")
        # a reposter kept out by name has no board: group it by its name, so
        # two of them on one day are both named (review 2026-10-23)
        key = (ats, token) if token else ("", normalize_company(str(d.get("company") or "")))
        cur = seen.setdefault(key, {**d, "rejected": 0})
        cur["rejected"] = int(cur["rejected"]) + int(d.get("rejected") or 0)
    if not seen:
        return []
    lines = ["## Repost sites kept out"]
    for d in seen.values():
        ats, token = str(d.get("ats") or ""), str(d.get("token") or "")
        name = d.get("company") or token
        board = f" ({ats}/{token})" if ats and token else ""
        line = f"- {name}{board}: {d.get('why')}. Not added or polled"
        if d.get("rejected"):
            line += f"; {d['rejected']} of its open job(s) rejected"
        lines.append(line + ".")
    lines.append(
        "A repost site lists other companies' jobs: apply on the employer's own site instead. "
        "A board kept out by its wording that is really an employer comes back when you list "
        "it in config/targets.yaml (a site on registries/reposters.yml stays out)."
    )
    lines.append("")
    return lines
