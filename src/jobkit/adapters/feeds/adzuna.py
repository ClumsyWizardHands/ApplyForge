"""Adzuna feed — official API, auth via app_id/app_key query params.
Free-tier budget is tight (official ToS: 250/day; plan for ~1,000/month), so:
one call per configured title phrase per day (at most MAX_PHRASES),
salary_include_unknown always on. salary_min is never hardcoded: it is the
lowest floor the filter accepts (SearchSpec.salary_min, from filters.yaml comp),
so Adzuna never drops a posting the filter would keep.
salary_is_predicted numbers are Adzuna's model — recorded in extra, never
treated as a posted range. redirect_url is a tracking hop, resolved lazily,
never canonical (aggregator rows never are)."""

from __future__ import annotations

import os

import httpx

from jobkit.adapters.feeds.base import SearchSpec
from jobkit.models import NormalizedJob

# API-budget guard (free tier ~250 calls/day): one call per phrase per run.
MAX_PHRASES = 10


class AdzunaFeed:
    source_name = "adzuna"
    requires_key = True

    async def search(self, client: httpx.AsyncClient, query: SearchSpec) -> list[NormalizedJob]:
        app_id = os.environ.get("ADZUNA_APP_ID")
        app_key = os.environ.get("ADZUNA_APP_KEY")
        if not app_id or not app_key:
            return []  # key-gated; runner logs the skip
        jobs: list[NormalizedJob] = []
        seen: set[str] = set()
        for phrase in query.keywords[:MAX_PHRASES] or ("account executive",):
            params = {
                "app_id": app_id,
                "app_key": app_key,
                "what_phrase": phrase,
                "salary_include_unknown": "1",
                "max_days_old": str(query.max_days_old or 21),
                "sort_by": "date",
                "sort_direction": "down",
                "results_per_page": "50",
            }
            if query.salary_min:
                params["salary_min"] = str(query.salary_min)
            r = await client.get(
                "https://api.adzuna.com/v1/api/jobs/us/search/1",
                params=params,
                timeout=30.0,
            )
            r.raise_for_status()
            for j in r.json().get("results", []):
                jid = str(j.get("id", ""))
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                predicted = str(j.get("salary_is_predicted", "0")) in ("1", "true", "True")
                loc = (j.get("location") or {}).get("display_name", "") or ""
                jobs.append(
                    NormalizedJob(
                        ats=self.source_name,
                        board_token="",
                        external_id=jid,
                        title=j.get("title", ""),
                        company_name=(j.get("company") or {}).get("display_name", ""),
                        location_raw=loc,
                        remote=True if "remote" in loc.lower() else None,
                        jd_html=j.get("description", "") or "",
                        apply_url=j.get("redirect_url", ""),
                        posted_at=(j.get("created") or "")[:10] or None,
                        comp_min=None
                        if predicted
                        else int(j["salary_min"])
                        if j.get("salary_min")
                        else None,
                        comp_max=None
                        if predicted
                        else int(j["salary_max"])
                        if j.get("salary_max")
                        else None,
                        comp_currency=None
                        if predicted
                        else ("USD" if j.get("salary_min") else None),
                        extra={
                            "adref": j.get("adref"),
                            "salary_predicted": predicted,
                            "category": (j.get("category") or {}).get("label"),
                            "attribution": "Jobs by Adzuna",
                        },
                    )
                )
        return jobs
