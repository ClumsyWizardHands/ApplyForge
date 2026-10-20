"""a16z portfolio board (Consider new stack on Convex, verified 2026-09-02).
GET jobs.a16z.com/jobs?q=<term> — first ~25 jobs ship as initialData JSON
embedded in the RSC payload. EXPERIMENTAL: the embedding format is the most
brittle of the discovery adapters; parse failures degrade to an empty result
and a log line, never an exception.

DISABLED in filters.yaml since 2026-10-08: the parse fails on every run
("initialData.jobs not found")."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from jobkit.adapters.feeds.base import SearchSpec
from jobkit.contact import user_agent
from jobkit.log import log
from jobkit.models import NormalizedJob

_JOBS_ARRAY = re.compile(r'\\?"jobs\\?":(\[.*?\])[,}]', re.S)


class A16zFeed:
    source_name = "a16z"
    requires_key = False

    async def search(self, client: httpx.AsyncClient, query: SearchSpec) -> list[NormalizedJob]:
        jobs: list[NormalizedJob] = []
        seen: set[str] = set()
        for phrase in query.keywords[:4] or ("account executive",):
            try:
                r = await client.get(
                    "https://jobs.a16z.com/jobs",
                    params={"q": phrase},
                    # the honest jobkit UA — never a spoofed browser string
                    # (audit 2026-10-08 §3.8; the feed is disabled in filters.yaml)
                    headers={"User-Agent": user_agent()},
                    timeout=30.0,
                )
                r.raise_for_status()
                parsed = _extract_jobs(r.text)
            except (httpx.HTTPError, ValueError) as e:
                log("a16z.parse_failed", phrase=phrase, error=str(e)[:120])
                continue
            for j in parsed:
                jid = str(j.get("id") or j.get("slug", ""))
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                locs = j.get("locations") or []
                loc = ", ".join(str(x) for x in locs[:3]) if isinstance(locs, list) else str(locs)
                jobs.append(
                    NormalizedJob(
                        ats=self.source_name,
                        board_token="a16z",
                        external_id=jid,
                        title=str(j.get("title", "")),
                        company_name=str(j.get("company_name", "")),
                        location_raw=loc,
                        remote=True
                        if str(j.get("remote", "")).lower() in ("true", "remote")
                        else None,
                        jd_html=str(j.get("description", "") or ""),
                        apply_url=str(j.get("apply_url", "")),  # direct employer ATS
                        posted_at=str(j.get("posted_at", ""))[:10] or None,
                        comp_min=_num(j.get("salary_min")),
                        comp_max=_num(j.get("salary_max")),
                        comp_currency=(
                            str(j["salary_currency"]) if j.get("salary_currency") else None
                        ),
                        extra={"stage": j.get("company_stage"), "markets": j.get("job_markets")},
                    )
                )
        return jobs


def _num(v: Any) -> int | None:
    try:
        return int(float(v)) if v else None
    except (TypeError, ValueError):
        return None


def _extract_jobs(html: str) -> list[dict[str, Any]]:
    """Pull the embedded initialData.jobs array out of the RSC payload.
    The payload escapes quotes; try raw first, then unescaped."""
    for text in (html, html.replace('\\"', '"')):
        m = _JOBS_ARRAY.search(text)
        if not m:
            continue
        try:
            arr = json.loads(m.group(1))
            if isinstance(arr, list) and arr and isinstance(arr[0], dict):
                return arr
        except json.JSONDecodeError:
            continue
    raise ValueError("initialData.jobs not found in page")
