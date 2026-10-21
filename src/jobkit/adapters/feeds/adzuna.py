"""Adzuna feed — official API, auth via app_id/app_key query params.
Free-tier budget is tight (official ToS: 250/day; plan for ~1,000/month), so:
one call per configured title phrase per day (at most MAX_PHRASES),
salary_include_unknown always on. salary_min is never hardcoded: it is the
lowest floor the filter accepts (SearchSpec.salary_min, from filters.yaml comp),
so Adzuna never drops a posting the filter would keep.
salary_is_predicted numbers are Adzuna's model — recorded in extra, never
treated as a posted range. redirect_url is a tracking hop, resolved lazily,
never canonical (aggregator rows never are).

Location (optional): with `feeds.adzuna.locations` set, each phrase is
searched once per place with Adzuna's documented `where` and `distance` (km)
parameters, at most MAX_PLACES places, so a run makes at most
MAX_PHRASES x MAX_PLACES calls. Without locations the request is the same
location-free one as before. With locations and
`feeds.adzuna.search_nationwide: true` (someone near a place who also takes
remote work), each phrase is also searched once with no place, within the
same MAX_PLACES budget, so remote postings from anywhere come back too.
`feeds.adzuna.country` picks the country path segment (default "us").

With locations set, a US posting's location_raw also names the state Adzuna
gives in location.area ("Dublin, Alameda County, California"), so
filters.locations.match_state can tell Dublin, CA from Dublin, OH. Without
locations it is Adzuna's display_name alone, as before."""

from __future__ import annotations

import os

import httpx

from jobkit.adapters.feeds.base import SearchPlace, SearchSpec
from jobkit.log import log
from jobkit.models import NormalizedJob

# API-budget guard (free tier ~250 calls/day): one call per phrase per run.
MAX_PHRASES = 10
# with locations: one call per phrase per place, for at most this many places
MAX_PLACES = 3
# searched when the config names no phrase (kept from the sales-first default)
DEFAULT_PHRASE = "account executive"

# Adzuna reports salaries in the country's own currency
CURRENCY_BY_COUNTRY = {
    "us": "USD",
    "gb": "GBP",
    "ca": "CAD",
    "au": "AUD",
    "nz": "NZD",
    "in": "INR",
    "sg": "SGD",
    "za": "ZAR",
    "br": "BRL",
    "mx": "MXN",
    "pl": "PLN",
    "ch": "CHF",
    "at": "EUR",
    "be": "EUR",
    "de": "EUR",
    "es": "EUR",
    "fr": "EUR",
    "it": "EUR",
    "nl": "EUR",
}


def _plan(query: SearchSpec) -> list[tuple[str, SearchPlace | None]]:
    """(phrase, place) for every call this run makes, after the budget cuts.
    Logs what the cuts dropped."""
    phrases = list(query.keywords[:MAX_PHRASES])
    if not phrases:
        phrases = [DEFAULT_PHRASE]
        log("feeds.default_keywords_used", source="adzuna", keywords=phrases)
    elif len(query.keywords) > MAX_PHRASES:
        log("feeds.adzuna_capped", dropped_phrases=list(query.keywords[MAX_PHRASES:]))
    if not query.locations:
        return [(p, None) for p in phrases]
    nationwide = query.extra.get("nationwide") == "1"
    # the location-free search takes one of the MAX_PLACES slots
    cap = MAX_PLACES - 1 if nationwide else MAX_PLACES
    places: list[SearchPlace | None] = list(query.locations[:cap])
    if len(query.locations) > cap:
        log("feeds.adzuna_capped", dropped_places=[p.name for p in query.locations[cap:]])
    if nationwide:
        places.append(None)
    return [(phrase, place) for phrase in phrases for place in places]


def _location_raw(location: object, *, with_state: bool) -> str:
    """Adzuna's display_name; with_state adds the state from area
    (["US", "California", "Alameda County", "Dublin"]) when the display name
    does not already end with it."""
    if not isinstance(location, dict):
        return ""
    shown = str(location.get("display_name") or "")
    area = location.get("area")
    if not with_state or not isinstance(area, list) or len(area) < 2 or area[0] != "US":
        return shown
    state = str(area[1] or "").strip()
    if not state or not shown or shown.lower().endswith(state.lower()):
        return shown
    return f"{shown}, {state}"


class AdzunaFeed:
    source_name = "adzuna"
    requires_key = True
    key_env = ("ADZUNA_APP_ID", "ADZUNA_APP_KEY")

    async def search(self, client: httpx.AsyncClient, query: SearchSpec) -> list[NormalizedJob]:
        app_id = os.environ.get("ADZUNA_APP_ID")
        app_key = os.environ.get("ADZUNA_APP_KEY")
        if not app_id or not app_key:
            return []  # key-gated; runner logs the skip
        country = (query.country or "us").lower()
        currency = CURRENCY_BY_COUNTRY.get(country)
        jobs: list[NormalizedJob] = []
        seen: set[str] = set()
        for phrase, place in _plan(query):
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
            if place is not None:
                params["where"] = place.name
                params["distance"] = str(place.radius)
            r = await client.get(
                f"https://api.adzuna.com/v1/api/jobs/{country}/search/1",
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
                loc = _location_raw(j.get("location"), with_state=bool(query.locations))
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
                        else (currency if j.get("salary_min") else None),
                        extra={
                            "adref": j.get("adref"),
                            "salary_predicted": predicted,
                            "category": (j.get("category") or {}).get("label"),
                            "attribution": "Jobs by Adzuna",
                        },
                    )
                )
        return jobs
