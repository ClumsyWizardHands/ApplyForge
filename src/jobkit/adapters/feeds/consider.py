"""Consider-classic VC portfolio boards (verified live 2026-09-02): Sequoia,
Lightspeed, Bessemer, Kleiner Perkins. Two-step flow per board: GET /jobs/
(cookie jar; extract csrfToken from HTML) → POST /api-boards/search-jobs with
x-csrf-token. Salary data + direct employer ATS apply URLs."""

from __future__ import annotations

import re

import httpx

from jobkit.adapters.feeds.base import SearchSpec
from jobkit.contact import user_agent
from jobkit.models import NormalizedJob

BOARDS = {
    "jobs.sequoiacap.com": "sequoia-capital",
    "jobs.lsvp.com": "lightspeed",
    "jobs.bvp.com": "bessemer-ventures",
    "jobs.kleinerperkins.com": "kleiner-perkins",
}
_CSRF = re.compile(r'"csrfToken"\s*:\s*"([^"]+)"')


class ConsiderFeed:
    source_name = "consider"
    requires_key = False

    async def search(self, client: httpx.AsyncClient, query: SearchSpec) -> list[NormalizedJob]:
        jobs: list[NormalizedJob] = []
        seen: set[str] = set()
        for host, board_id in BOARDS.items():
            try:
                page = await client.get(
                    f"https://{host}/jobs/", headers={"User-Agent": user_agent()}, timeout=30.0
                )
                m = _CSRF.search(page.text)
                if not m:
                    continue
                r = await client.post(
                    f"https://{host}/api-boards/search-jobs",
                    headers={
                        "User-Agent": user_agent(),
                        "Content-Type": "application/json",
                        "x-csrf-token": m.group(1),
                    },
                    json={
                        "board": {"id": board_id, "isParent": True},
                        "query": {"jobTypes": ["sales"]} if query.categories else {},
                        "meta": {"size": 50},
                    },
                    timeout=30.0,
                )
                r.raise_for_status()
            except httpx.HTTPError:
                continue  # one board down must not kill the sweep
            for j in r.json().get("jobs", []):
                jid = f"{board_id}-{j.get('id', '')}"
                if jid in seen:
                    continue
                seen.add(jid)
                comp = j.get("compensation") or {}
                locs = j.get("locations") or []
                loc = ", ".join(str(x) for x in locs[:3])
                remote = bool(j.get("remote")) or "remote" in loc.lower()
                jobs.append(
                    NormalizedJob(
                        ats=self.source_name,
                        board_token=board_id,
                        external_id=jid,
                        title=str(j.get("title", "")),
                        company_name=str(j.get("companyName", "")),
                        location_raw=loc or ("Remote" if remote else ""),
                        remote=remote or None,
                        jd_html=str(j.get("description", "") or ""),
                        apply_url=str(j.get("applyUrl") or j.get("url", "")),
                        posted_at=str(j.get("timeStamp", ""))[:10] or None,
                        comp_min=comp.get("salaryMin"),
                        comp_max=comp.get("salaryMax"),
                        comp_currency=comp.get("salaryCurrency"),
                        extra={"board": board_id, "jobFunctions": j.get("jobFunctions")},
                    )
                )
        return jobs
