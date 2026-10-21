"""USAJOBS feed — official federal API. Free key from developer.usajobs.gov;
auth = Authorization-Key header + registered email as User-Agent (their rule).
Federal vocabulary differs from private-sector titles, so besides title
keywords the search can name occupational series codes (filters.yaml
`feeds.usajobs.series`, e.g. ["1101", "0343"]) and a minimum GS grade
(`pay_grade_low`). Nothing is searched by series unless the config names
one; `remote_only` asks the API for remote postings only.

Location (optional): with `feeds.usajobs.locations` set, each search runs
once per place with the documented LocationName and Radius (miles)
parameters, at most MAX_PLACES places. Without locations the requests are
the same location-free ones as before.

Description: the JobSummary, as before. With `feeds.usajobs.full_description:
true` (the setup interview writes it) it is built from the summary plus the
duties, qualifications, requirements and education the search result carries
(`_description`). The summary alone is a sentence or two, too short to score,
so a federal job with only a summary is set aside as jd_missing.

A posting shown as "Multiple Locations" (common for VA nursing) names its
sites in PositionLocation. With locations searched, location_raw is the site
in a searched place (`_location_raw`), so the location filter sees a city."""

from __future__ import annotations

import html
import os
import re
from typing import Any

import httpx

from jobkit.adapters.feeds.base import SearchSpec
from jobkit.autofill.options import state_code
from jobkit.log import log
from jobkit.models import NormalizedJob

# with locations: each search runs once per place, for at most this many places
MAX_PLACES = 5


def _with_places(param_sets: list[dict[str, str]], query: SearchSpec) -> list[dict[str, str]]:
    """Each search once per configured place (LocationName + Radius), or the
    searches unchanged when no place is configured."""
    if not query.locations:
        return param_sets
    places = query.locations[:MAX_PLACES]
    if len(query.locations) > MAX_PLACES:
        log("feeds.usajobs_capped", dropped_places=[p.name for p in query.locations[MAX_PLACES:]])
    return [
        {**params, "LocationName": place.name, "Radius": str(place.radius)}
        for params in param_sets
        for place in places
    ]


def _text_items(value: Any) -> list[str]:
    """A USAJOBS text field (a string or a list of strings) as non-empty strings."""
    items = value if isinstance(value, list) else [value]
    return [str(v).strip() for v in items if isinstance(v, str | int | float) and str(v).strip()]


def _description(d: dict[str, Any], details: dict[str, Any]) -> str:
    """The posting's text as HTML: JobSummary, then MajorDuties,
    QualificationSummary, KeyRequirements, Requirements and Education when
    present. With only a summary it is that summary, unchanged."""
    summary = str(details.get("JobSummary") or "")
    sections = [
        ("Duties", _text_items(details.get("MajorDuties"))),
        ("Qualifications", _text_items(d.get("QualificationSummary"))),
        ("Key requirements", _text_items(details.get("KeyRequirements"))),
        ("Requirements", _text_items(details.get("Requirements"))),
        ("Education", _text_items(details.get("Education"))),
    ]
    sections = [(title, items) for title, items in sections if items]
    if not sections:
        return summary
    parts = [f"<p>{html.escape(summary)}</p>"] if summary else []
    for title, items in sections:
        parts.append(f"<h3>{title}</h3>")
        if len(items) == 1:
            parts.append(f"<p>{html.escape(items[0])}</p>")
        else:
            parts.append("<ul>" + "".join(f"<li>{html.escape(i)}</li>" for i in items) + "</ul>")
    return "".join(parts)


# PositionLocationDisplay values that name no one place
_NO_ONE_PLACE = re.compile(
    r"(?i)^\s*(?:multiple locations|various locations|location negotiable|many vacancies)"
)


def _location_raw(d: dict[str, Any], query: SearchSpec) -> str:
    """PositionLocationDisplay, or, when it names no one place and the search
    named places, the posting's site in a searched place (the same city, else
    the same state), else its first site. Without searched places it is the
    display text, as before."""
    shown = str(d.get("PositionLocationDisplay") or "")
    if not query.locations or not _NO_ONE_PLACE.search(shown):
        return shown
    sites = [
        str(p.get("LocationName") or "").strip()
        for p in d.get("PositionLocation") or []
        if isinstance(p, dict)
    ]
    sites = [x for x in sites if x]
    if not sites:
        return shown

    def parts(name: str) -> tuple[str, str]:
        """(city, state) lowercased; the state as its two-letter code when it
        names a US state, so 'Columbus, OH' and 'Dayton, Ohio' share one."""
        city, _, state = name.partition(",")
        state = state.strip()
        return city.strip().lower(), (state_code(state) or state).lower()

    for place in query.locations:
        city, state = parts(place.name)
        for site in sites:
            if parts(site)[0] == city:
                return site
    for place in query.locations:
        _, state = parts(place.name)
        for site in sites:
            if state and parts(site)[1] == state:
                return site
    return sites[0]


class USAJobsFeed:
    source_name = "usajobs"
    requires_key = True
    key_env = ("USAJOBS_API_KEY", "USAJOBS_USER_AGENT_EMAIL")

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
        for params in _with_places(param_sets, query):
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
                        location_raw=("Remote, US" if remote else _location_raw(d, query)),
                        remote=remote or None,
                        jd_html=(
                            _description(d, details)
                            if query.extra.get("full_description")
                            else str(details.get("JobSummary") or "")
                        ),
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
