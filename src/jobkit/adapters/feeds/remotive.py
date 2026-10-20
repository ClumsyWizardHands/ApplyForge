"""Remotive feed — free, no key, remote tech jobs with category filters."""

from __future__ import annotations

import httpx

from jobkit.adapters.feeds.base import SearchSpec, feed_get
from jobkit.models import NormalizedJob


class RemotiveFeed:
    source_name = "remotive"
    requires_key = False

    async def search(self, client: httpx.AsyncClient, query: SearchSpec) -> list[NormalizedJob]:
        jobs: list[NormalizedJob] = []
        categories = query.categories or ("",)
        for cat in categories:
            url = "https://remotive.com/api/remote-jobs"
            if cat:
                url += f"?category={cat}"
            resp = await feed_get(client, url)
            for j in resp.json().get("jobs", []):
                jobs.append(
                    NormalizedJob(
                        ats=self.source_name,
                        board_token="",
                        external_id=str(j.get("id", "")),
                        title=j.get("title", ""),
                        company_name=j.get("company_name", ""),
                        location_raw=j.get("candidate_required_location", "") or "Remote",
                        remote=True,
                        jd_html=j.get("description", "") or "",
                        apply_url=j.get("url", ""),
                        posted_at=(j.get("publication_date") or "")[:10] or None,
                        extra={"salary": j.get("salary"), "category": j.get("category")},
                    )
                )
        return jobs
