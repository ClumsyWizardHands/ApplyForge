"""Normalization for the canonical dedup key: (normalized_company, normalized_title,
location_bucket), with jd_hash as tiebreak (SPEC §2.2). One place, one owner."""

from __future__ import annotations

import hashlib
import html
import re

_COMPANY_NOISE = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|the)\b\.?", re.I)
_WS = re.compile(r"\s+")
_TITLE_NOISE = re.compile(
    r"[\(\[][^\)\]]*[\)\]]"  # parentheticals: (Remote), [NYC], (Series B)
    r"|\b(remote|hybrid|onsite|us|usa|east coast|west coast)\b"
    r"|[-–—|,/].*?$",  # trailing qualifiers after a separator
    re.I,
)

# metro buckets: substring match on the raw location, first hit wins
_METROS: list[tuple[str, str]] = [
    ("new york", "nyc-metro"),
    ("nyc", "nyc-metro"),
    ("manhattan", "nyc-metro"),
    ("brooklyn", "nyc-metro"),
    ("jersey city", "nyc-metro"),
    ("newark", "nyc-metro"),
    ("hoboken", "nyc-metro"),
    ("san francisco", "sf-bay"),
    ("sf", "sf-bay"),
    ("bay area", "sf-bay"),
    ("oakland", "sf-bay"),
    ("palo alto", "sf-bay"),
    ("mountain view", "sf-bay"),
    ("seattle", "seattle-metro"),
    ("bellevue", "seattle-metro"),
    ("boston", "boston-metro"),
    ("cambridge", "boston-metro"),
    ("los angeles", "la-metro"),
    ("santa monica", "la-metro"),
    ("chicago", "chicago-metro"),
    ("austin", "austin-metro"),
    ("denver", "denver-metro"),
    ("washington", "dc-metro"),
    ("arlington", "dc-metro"),
    ("miami", "miami-metro"),
    ("atlanta", "atlanta-metro"),
    ("dallas", "dallas-metro"),
    ("london", "london-metro"),
    ("toronto", "toronto-metro"),
]

# The US states each named metro bucket covers. A place outside these states
# that lands in a metro bucket only by its spelling ("Mansfield, OH" holds
# "sf"; "Newark, OH" is not Newark, NJ) is mis-filed: with
# filters.locations.match_state the filter then also checks the posting's
# state (config.Filters.bucket_states). Non-US metros cover no US state.
METRO_STATES: dict[str, frozenset[str]] = {
    "nyc-metro": frozenset({"NY", "NJ"}),
    "sf-bay": frozenset({"CA"}),
    "seattle-metro": frozenset({"WA"}),
    "boston-metro": frozenset({"MA"}),
    "la-metro": frozenset({"CA"}),
    "chicago-metro": frozenset({"IL"}),
    "austin-metro": frozenset({"TX"}),
    "denver-metro": frozenset({"CO"}),
    "dc-metro": frozenset({"DC", "VA", "MD"}),
    "miami-metro": frozenset({"FL"}),
    "atlanta-metro": frozenset({"GA"}),
    "dallas-metro": frozenset({"TX"}),
    "london-metro": frozenset(),
    "toronto-metro": frozenset(),
}

# EEO/benefits boilerplate stripped before hashing so JDs don't look 40% identical
_BOILERPLATE = re.compile(
    r"(equal (employment )?opportunity[^.]*\.|we (are|’re|'re) an equal[^.]*\."
    r"|without regard to race[^.]*\.|all qualified applicants[^.]*\."
    r"|reasonable accommodations?[^.]*\.|e-?verify[^.]*\."
    r"|affirmative action[^.]*\.)",
    re.I,
)
_TAGS = re.compile(r"<[^>]+>")


def normalize_company(name: str) -> str:
    s = _COMPANY_NOISE.sub("", name.lower())
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return _WS.sub(" ", s).strip()


def normalize_title(title: str) -> str:
    s = _TITLE_NOISE.sub(" ", title.lower())
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return _WS.sub(" ", s).strip()


# Whole-word anchors only (audit 2026-10-08 §2 item 5): the old substring test
# for "us" put "Remote - Austria" (a-US-tria) in remote-us. Countries, major
# non-US cities and regions. "georgia" is left out on purpose (US state too).
_NON_US = re.compile(
    r"\b(uk|u\.k|united kingdom|great britain|england|scotland|wales|london|edinburgh"
    r"|ireland|dublin|france|paris|germany|berlin|munich|hamburg|frankfurt|dach"
    r"|austria|vienna|switzerland|zurich|geneva|belgium|brussels|benelux|luxembourg"
    r"|netherlands|amsterdam|sweden|stockholm|norway|oslo|denmark|copenhagen"
    r"|finland|helsinki|iceland|estonia|tallinn|latvia|riga|lithuania|vilnius"
    r"|italy|milan|rome|spain|madrid|barcelona|portugal|lisbon|greece|athens"
    r"|poland|warsaw|krakow|kraków|czechia|czech republic|prague|slovakia|hungary"
    r"|budapest|romania|bucharest|bulgaria|sofia|serbia|belgrade|croatia|zagreb"
    r"|slovenia|ukraine|kyiv|turkey|türkiye|istanbul|israel|tel aviv|cyprus|malta"
    r"|canada|toronto|vancouver|montreal|montréal|ottawa|calgary|mexico|mexico city"
    r"|brazil|são paulo|sao paulo|argentina|buenos aires|chile|santiago|colombia"
    r"|bogota|bogotá|peru|lima|costa rica|uruguay|latin america|south america"
    r"|central america|india|bangalore|bengaluru|hyderabad|pune|mumbai|delhi"
    r"|gurgaon|gurugram|noida|chennai|pakistan|bangladesh|sri lanka|nepal"
    r"|uae|dubai|abu dhabi|saudi arabia|riyadh|qatar|doha|egypt|cairo|morocco"
    r"|nigeria|lagos|kenya|nairobi|ghana|south africa|cape town|johannesburg"
    r"|singapore|malaysia|kuala lumpur|indonesia|jakarta|philippines|manila"
    r"|vietnam|thailand|bangkok|china|beijing|shanghai|shenzhen|hong kong|taiwan"
    r"|taipei|korea|seoul|japan|tokyo|osaka|australia|sydney|melbourne|brisbane"
    r"|new zealand|auckland|nordics|emea|apac|apj|europe|eu|mena|latam)\b"
)

# A US anchor wins over a non-US one: "Remote, Canada; Remote, US" is open to a
# US resident, and "New Mexico" / "New England" contain non-US words.
_US = re.compile(
    r"(?<![a-z])(us|u\.s\.?|usa|u\.s\.a\.?|united states|north america|amer|americas"
    r"|new york|nyc|manhattan|brooklyn|new jersey|jersey city|newark|hoboken"
    r"|san francisco|bay area|silicon valley|seattle|boston|los angeles|chicago"
    r"|austin|denver|atlanta|miami|dallas|houston|philadelphia|phoenix|san diego"
    r"|new england|new mexico|alabama|alaska|arizona|arkansas|california|colorado"
    r"|connecticut|delaware|florida|hawaii|idaho|illinois|indiana|iowa|kansas"
    r"|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota"
    r"|mississippi|missouri|montana|nebraska|nevada|new hampshire|north carolina"
    r"|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina"
    r"|south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia"
    r"|wisconsin|wyoming)(?![a-z])"
)


def mentions_us(location_raw: str) -> bool:
    """True when the location names the US, a US region or a US place."""
    return _US.search((location_raw or "").lower()) is not None


def mentions_non_us(location_raw: str) -> bool:
    """True when the location names a country, city or region outside the US."""
    return _NON_US.search((location_raw or "").lower()) is not None


# The US named as a whole country or continent, never a state or city.
_US_COUNTRY = re.compile(
    r"(?<![a-z])(united states of america|united states|u\.s\.a\.?|u\.s\.?|usa|us"
    r"|north america|americas|america|amer)(?![a-z])"
)
# Words in a location that place nothing: directions, regions, work mode.
_NO_PLACE_WORDS = re.compile(
    r"(?<![a-z])(distributed|nationwide|anywhere|any|in|the|of|only|and|or|based"
    r"|east|west|north|south|central|coast|eastern|western|midwest|northeast"
    r"|northwest|southeast|southwest|region|regions|hybrid|onsite|on-site|office)"
    r"(?![a-z])"
)
_LETTER = re.compile(r"[^\W\d_]")


def mentions_us_country(location_raw: str) -> bool:
    """True when the location names the US as a country ("United States",
    "USA", "US / Canada", "North America"). A state or city does not count."""
    return _US_COUNTRY.search((location_raw or "").lower()) is not None


def names_no_specific_place(location_raw: str) -> bool:
    """True when the location names no city, state or site: blank,
    "Distributed", or only countries and regions ("United States",
    "USA - West", "US / Canada"). "Tulsa, Oklahoma" and "San Mateo, CA United
    States" name a place."""
    s = (location_raw or "").lower()
    for pat in (_US_COUNTRY, _NON_US, _NO_PLACE_WORDS):
        s = pat.sub(" ", s)
    return _LETTER.search(s) is None


def location_bucket(location_raw: str, remote: bool | None) -> str:
    loc = location_raw.lower()
    if remote or "remote" in loc:
        if mentions_us(loc):
            return "remote-us"
        # a country/region anchor outside the US (and none inside) makes
        # "remote" an intl hire
        if mentions_non_us(loc):
            return "remote-intl"
        if any(k in loc for k in ("global", "worldwide", "anywhere")):
            return "remote-global"
        # bare "Remote": the LLM remote check reads the JD for a geo restriction
        return "remote-us"
    for needle, bucket in _METROS:
        if needle in loc:
            return bucket
    return "other-" + (re.sub(r"[^a-z]", "", loc.split(",")[0])[:20] or "unknown")


def jd_hash(jd_html: str) -> str:
    text = _TAGS.sub(" ", jd_html)
    text = _BOILERPLATE.sub("", text)
    text = _WS.sub(" ", text).lower().strip()
    return hashlib.sha256(text.encode()).hexdigest()[:32]


# Below this many characters of description text a job is not triaged or
# tailored (audit 2026-10-08 §2 item 2). Feed rows with no JD (Getro's search
# payload has no description; a one-line RSS stub) all hashed alike and shared
# one cached LLM answer — one company's cover letter went out under another's
# name. Refused jobs are logged as rejections layer=sanity reason=jd_missing.
MIN_JD_CHARS = 500
JD_MISSING_REASON = "jd_missing"


def jd_text_length(jd_html: str) -> int:
    """Characters of description text: entities decoded, tags dropped, runs of
    whitespace collapsed. Escaped HTML (Greenhouse) is decoded first, so markup
    never counts as description."""
    text = _TAGS.sub(" ", html.unescape(jd_html or ""))
    return len(_WS.sub(" ", text).strip())


def jd_is_missing(jd_html: str | None) -> bool:
    return jd_text_length(jd_html or "") < MIN_JD_CHARS


def cluster_key(company: str, title: str, location_raw: str, remote: bool | None) -> str:
    """The canonical dedup key, joined for storage/indexing."""
    return "|".join(
        (normalize_company(company), normalize_title(title), location_bucket(location_raw, remote))
    )
