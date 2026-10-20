"""USAJOBS feed — official federal API. Free key from developer.usajobs.gov;
auth = Authorization-Key header + registered email as User-Agent (their rule).
Federal vocabulary differs from private-sector titles, so besides title
keywords the search can name occupational series codes (filters.yaml
`feeds.usajobs.series`, e.g. ["1101", "0343"]) and a minimum GS grade
(`pay_grade_low`). Nothing is searched by series unless the config names
one; `remote_only` asks the API for remote postings only."""

from __future__ import annotations

import os

import httpx

from jobkit.adapters.feeds.base import SearchSpec
from jobkit.models import NormalizedJob


class USAJobsFeed:
    source_name = "usajobs"
    requires_key = True

    async def search(self, client: httpx.AsyncClient, query: SearchSpec) -> list[NormalizedJob]:
        key = os.environ.get("USAJOBS_API_KEY")
        email = os.environ.get("USAJOBS_USER_AGENT_EMAIL")
        if not key or not email:
            return []  # key-gated; runner logs the skip
        headers = {"Host": "data.usajobs.gov", "User-Agent": email, "Authorization-Key": key}
        jobs: list[NormalizedJob] = []
        seen: set[str] = set()
        remote_only = {"RemoteIndicator": "True"} if query.remote_only else {}
        param_sets: list[dict[str, str]] = []
        series = query.extra.get("series", "")
        if series:
            by_series = {
                "JobCategoryCode": series,
                "SortField": "opendate",
                "SortDirection": "desc",
                "ResultsPerPage": "500",
                **remote_only,
            }
            if query.extra.get("pay_grade_low"):
                by_series["PayGradeLow"] = query.extra["pay_grade_low"]
            param_sets.append(by_series)
        param_sets += [
            {"PositionTitle": kw, "ResultsPerPage": "500", **remote_only}
            for kw in query.keywords[:4]
        ]
        for params in param_sets:
            r = await client.get(
                "https://data.usajobs.gov/api/Search",
                params=params,
                headers=headers,
                timeout=30.0,
            )
            r.raise_for_status()
            items = (r.json().get("SearchResult") or {}).get("SearchResultItems") or []
            for it in items:
                d = it.get("MatchedObjectDescriptor") or {}
                oid = str(it.get("MatchedObjectId", ""))
                if not oid or oid in seen:
                    continue
                seen.add(oid)
                rem = (d.get("PositionRemuneration") or [{}])[0]
                annual = rem.get("RateIntervalCode") == "PA"
                details = (d.get("UserArea") or {}).get("Details") or {}
                remote = bool(details.get("RemoteIndicator"))
                apply_uri = (d.get("ApplyURI") or [d.get("PositionURI", "")])[0]
                jobs.append(
                    NormalizedJob(
                        ats=self.source_name,
                        board_token="",
                        external_id=oid,
                        title=d.get("PositionTitle", ""),
                        company_name=d.get("OrganizationName")
                        or d.get("DepartmentName", "US Federal"),
                        location_raw=(
                            "Remote, US" if remote else d.get("PositionLocationDisplay", "")
                        ),
                        remote=remote or None,
                        jd_html=str(details.get("JobSummary") or ""),
                        apply_url=str(apply_uri),
                        posted_at=(d.get("PublicationStartDate") or "")[:10] or None,
                        comp_min=int(float(rem.get("MinimumRange") or 0)) or None
                        if annual
                        else None,
                        comp_max=int(float(rem.get("MaximumRange") or 0)) or None
                        if annual
                        else None,
                        comp_currency="USD" if annual else None,
                        extra={
                            "close_date": d.get("ApplicationCloseDate"),
                            "telework": details.get("TeleworkEligible"),
                            "who_may_apply": (details.get("WhoMayApply") or {}).get("Name"),
                        },
                    )
                )
        return jobs
