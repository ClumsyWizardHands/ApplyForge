"""Arbeitnow feed — free, no key, ATS-sourced with a remote flag."""

from __future__ import annotations

import httpx

from jobkit.adapters.feeds.base import SearchSpec, feed_get
from jobkit.models import NormalizedJob


class ArbeitnowFeed:
    source_name = "arbeitnow"
    requires_key = False

    async def search(self, client: httpx.AsyncClient, query: SearchSpec) -> list[NormalizedJob]:
        resp = await feed_get(client, "https://www.arbeitnow.com/api/job-board-api")
        jobs: list[NormalizedJob] = []
        for j in resp.json().get("data", []):
            if query.remote_only and not j.get("remote"):
                continue
            jobs.append(
                NormalizedJob(
                    ats=self.source_name,
                    board_token="",
                    external_id=j.get("slug", ""),
                    title=j.get("title", ""),
                    company_name=j.get("company_name", ""),
                    location_raw=j.get("location", "") or "",
                    remote=bool(j.get("remote")),
                    jd_html=j.get("description", "") or "",
                    apply_url=j.get("url", ""),
                    posted_at=None,
                    extra={"tags": j.get("tags", []), "job_types": j.get("job_types", [])},
                )
            )
        return jobs
