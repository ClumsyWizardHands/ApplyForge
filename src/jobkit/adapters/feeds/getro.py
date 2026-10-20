"""Getro-powered VC portfolio boards (verified live 2026-09-02): one adapter
covers General Catalyst (network 222), Insight Partners (246), Khosla (257).
POST api.getro.com/api/v2/collections/{id}/search/jobs — the same
unauthenticated JSON the public board serves; Accept header must be EXACTLY
application/json (anything else → 406). Filters are undocumented server-side,
so we post-filter client-side. Politeness: a few queries per network per day.
Apply URLs point at the employer's own ATS → our direct adapters take over."""

from __future__ import annotations

from typing import Any

import httpx

from jobkit.adapters.feeds.base import SearchSpec
from jobkit.contact import user_agent
from jobkit.models import NormalizedJob

NETWORKS = {"generalcatalyst": 222, "insightpartners": 246, "khosla": 257}


class GetroFeed:
    source_name = "getro"
    requires_key = False

    async def search(self, client: httpx.AsyncClient, query: SearchSpec) -> list[NormalizedJob]:
        jobs: list[NormalizedJob] = []
        seen: set[str] = set()
        for net_name, net_id in NETWORKS.items():
            for phrase in query.keywords[:4] or ("account executive",):
                r = await client.post(
                    f"https://api.getro.com/api/v2/collections/{net_id}/search/jobs",
                    headers={
                        "User-Agent": user_agent(),
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    json={"hitsPerPage": 50, "page": 0, "query": phrase, "filters": {}},
                    timeout=30.0,
                )
                r.raise_for_status()
                for j in r.json().get("results", {}).get("jobs", []) or r.json().get("jobs", []):
                    jid = f"{net_id}-{j.get('id', j.get('slug', ''))}"
                    if jid in seen:
                        continue
                    seen.add(jid)
                    org = j.get("organization") or {}
                    locs = j.get("searchable_locations") or j.get("locations") or []
                    loc = ", ".join(str(x) for x in locs[:3]) if locs else ""
                    work_mode = str(j.get("work_mode") or "").lower()
                    comp_min, comp_max = (
                        _cents(j.get("compensation_amount_min_cents")),
                        _cents(j.get("compensation_amount_max_cents")),
                    )
                    if not j.get("compensation_public", True):
                        comp_min = comp_max = None
                    jobs.append(
                        NormalizedJob(
                            ats=self.source_name,
                            board_token=net_name,
                            external_id=jid,
                            title=str(j.get("title", "")),
                            company_name=str(org.get("name") or j.get("organization_name", "")),
                            location_raw=loc or ("Remote" if work_mode == "remote" else ""),
                            remote=True if work_mode == "remote" else None,
                            jd_html=str(j.get("description", "") or ""),
                            apply_url=str(j.get("url", "")),  # direct employer ATS
                            posted_at=str(j.get("created_at", ""))[:10] or None,
                            comp_min=comp_min,
                            comp_max=comp_max,
                            comp_currency="USD" if comp_min else None,
                            extra={
                                "network": net_name,
                                "seniority": j.get("seniority"),
                                "stage": org.get("stage"),
                            },
                        )
                    )
        return jobs


def _cents(v: Any) -> int | None:
    try:
        return int(v) // 100 if v else None
    except (TypeError, ValueError):
        return None
