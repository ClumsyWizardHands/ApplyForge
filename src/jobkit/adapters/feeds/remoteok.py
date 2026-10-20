"""RemoteOK feed — free, needs a real User-Agent; index 0 is a legal notice."""

from __future__ import annotations

import httpx

from jobkit.adapters.feeds.base import SearchSpec, feed_get
from jobkit.models import NormalizedJob


class RemoteOKFeed:
    source_name = "remoteok"
    requires_key = False

    async def search(self, client: httpx.AsyncClient, query: SearchSpec) -> list[NormalizedJob]:
        resp = await feed_get(client, "https://remoteok.com/api")
        payload = resp.json()
        jobs: list[NormalizedJob] = []
        for j in payload[1:]:  # index 0 is the legal notice, not a job
            if not isinstance(j, dict) or not j.get("position"):
                continue
            comp_min = int(j["salary_min"]) if j.get("salary_min") else None
            comp_max = int(j["salary_max"]) if j.get("salary_max") else None
            jobs.append(
                NormalizedJob(
                    ats=self.source_name,
                    board_token="",
                    external_id=str(j.get("id", j.get("slug", ""))),
                    title=j.get("position", ""),
                    company_name=j.get("company", ""),
                    location_raw=j.get("location", "") or "Remote",
                    remote=True,
                    jd_html=j.get("description", "") or "",
                    apply_url=j.get("apply_url") or j.get("url", ""),
                    posted_at=(j.get("date") or "")[:10] or None,
                    comp_min=comp_min,
                    comp_max=comp_max,
                    comp_currency="USD" if (comp_min or comp_max) else None,
                    extra={"tags": j.get("tags", [])},
                )
            )
        return jobs
