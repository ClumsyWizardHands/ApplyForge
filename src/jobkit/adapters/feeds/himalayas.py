"""Himalayas feed — free, no key, remote jobs with structured salary + geo limits."""

from __future__ import annotations

import httpx

from jobkit.adapters.feeds.base import SearchSpec, feed_get
from jobkit.models import NormalizedJob


class HimalayasFeed:
    source_name = "himalayas"
    requires_key = False

    async def search(self, client: httpx.AsyncClient, query: SearchSpec) -> list[NormalizedJob]:
        resp = await feed_get(client, "https://himalayas.app/jobs/api?limit=100")
        jobs: list[NormalizedJob] = []
        for j in resp.json().get("jobs", []):
            restrictions = j.get("locationRestrictions") or []
            jobs.append(
                NormalizedJob(
                    ats=self.source_name,
                    board_token="",
                    external_id=str(j.get("guid", "")),
                    title=j.get("title", ""),
                    company_name=j.get("companyName", ""),
                    location_raw=", ".join(restrictions) or "Remote",
                    remote=True,
                    jd_html=j.get("description", "") or j.get("excerpt", "") or "",
                    apply_url=j.get("applicationLink", ""),
                    posted_at=None,
                    comp_min=int(j["minSalary"]) if j.get("minSalary") else None,
                    comp_max=int(j["maxSalary"]) if j.get("maxSalary") else None,
                    comp_currency=j.get("currency"),
                    extra={"categories": j.get("categories", [])},
                )
            )
        return jobs
