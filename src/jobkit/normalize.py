"""Normalization for the canonical dedup key: (normalized_company, normalized_title,
location_bucket), with jd_hash as tiebreak (SPEC §2.2). One place, one owner."""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass

from jobkit.autofill.options import state_code

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


# ---- the place a job title names (owner decision D4, 2026-10-23) ----
#
# "Account Executive - Denver, CO" on a board whose feed location says
# "Boston, MA", "Chief of Staff | Colombia" with "Cambridge, MA": the title
# names where the job is, and the filter uses it (pipeline/filter.py). This
# parser is conservative: a title segment counts only when ALL of it is a
# place ("Account Executive - Strategic" and "- German Speaking" name none),
# the head of the title (the role, before the first separator or comma) is
# never read as a place, and a lone two-letter code is never a state ("ME",
# "IN", "OR" are words). A title that names places both inside and outside
# the US ("US/Canada"), or several US places ("NYC or SF"), gives None.

# work modes and filler around a place: "(Remote - Canada)", "DACH Territory",
# "US-Based", "Greater Boston Area"
_TITLE_FILLER = re.compile(
    r"(?<![a-z])(remote|hybrid|on-?site|in-office|office|offices|based|only|territory"
    r"|territories|region|area|greater|metro|hq|headquarters|first|friendly|preferred)(?![a-z])",
    re.I,
)
_TITLE_REMOTE = re.compile(r"(?<![a-z])remote(?![a-z])", re.I)
# directions that may precede a non-US region ("Central EMEA", "Northern Europe")
_DIRECTIONS = re.compile(
    r"(?<![a-z])(central|east|west|north|south|eastern|western|northern|southern)(?![a-z])", re.I
)
_BRACKETS = re.compile(r"[\(\[]([^\)\]]*)[\)\]]")
# segment separators: a pipe, a colon, or a dash with a space on at least one
# side ("Mid-Market" and "Full-Cycle" are words, "Engineer- Strategic" is not)
_TITLE_SEP = re.compile(r"\s*\|\s*|\s*:\s+|\s+[-–—]+\s*|\s*[-–—]+\s+")
_CONNECTORS = re.compile(r"\s*(?:&|/|\+|(?<![a-z])(?:and|or)(?![a-z]))\s*", re.I)
_CITYISH = re.compile(r"[A-Z][\w.'’-]*(?:\s+[A-Za-z][\w.'’-]*){0,3}")
# capitalised words a "<word>, <code>" pair must not read as a city
_NOT_CITY = frozenset(
    {
        "federal",
        "enterprise",
        "commercial",
        "strategic",
        "public sector",
        "sled",
        "smb",
        "mid-market",
        "state",
        "local",
        "government",
        "healthcare",
        "mid market",
        "majors",
        "select",
        "central",
        "east",
        "west",
        "north",
        "south",
    }
)
# US cities and metros a title may name on their own ("- Denver", "[NYC]"),
# with their state. Names shared with a place abroad ("Cambridge", "Richmond")
# or with a state ("Washington") are left out: they count only with their state.
US_CITY_STATES: dict[str, str] = {
    "new york": "NY",
    "new york city": "NY",
    "nyc": "NY",
    "manhattan": "NY",
    "brooklyn": "NY",
    "jersey city": "NJ",
    "hoboken": "NJ",
    "san francisco": "CA",
    "sf": "CA",
    "sf bay area": "CA",
    "bay area": "CA",
    "silicon valley": "CA",
    "oakland": "CA",
    "palo alto": "CA",
    "mountain view": "CA",
    "san jose": "CA",
    "san mateo": "CA",
    "menlo park": "CA",
    "redwood city": "CA",
    "sunnyvale": "CA",
    "santa clara": "CA",
    "seattle": "WA",
    "bellevue": "WA",
    "boston": "MA",
    "los angeles": "CA",
    "santa monica": "CA",
    "irvine": "CA",
    "san diego": "CA",
    "sacramento": "CA",
    "chicago": "IL",
    "austin": "TX",
    "dallas": "TX",
    "houston": "TX",
    "san antonio": "TX",
    "plano": "TX",
    "denver": "CO",
    "boulder": "CO",
    "miami": "FL",
    "orlando": "FL",
    "tampa": "FL",
    "atlanta": "GA",
    "washington dc": "DC",
    "washington d.c.": "DC",
    "dc": "DC",
    "d.c.": "DC",
    "philadelphia": "PA",
    "pittsburgh": "PA",
    "phoenix": "AZ",
    "scottsdale": "AZ",
    "salt lake city": "UT",
    "lehi": "UT",
    "minneapolis": "MN",
    "detroit": "MI",
    "nashville": "TN",
    "charlotte": "NC",
    "raleigh": "NC",
    "durham": "NC",
    "indianapolis": "IN",
    "st. louis": "MO",
    "st louis": "MO",
    "kansas city": "MO",
    "las vegas": "NV",
    "baltimore": "MD",
    "cleveland": "OH",
    "cincinnati": "OH",
    "columbus": "OH",
    "milwaukee": "WI",
    "stamford": "CT",
    "reston": "VA",
    "mclean": "VA",
}
_US_COUNTRY_WORDS = frozenset(
    {"us", "u.s.", "u.s", "usa", "u.s.a.", "united states", "united states of america"}
    | {"america", "north america", "americas", "amer"}
    # North America as a sales region: "Technical Recruiter (Remote EU, LATAM,
    # NAMER)" is open to the US (review 2026-10-23)
    | {"namer", "noram", "nam", "n. america", "n america", "na"}
)
# regions outside the US that _NON_US does not list
_NON_US_EXTRA = re.compile(r"\b(anz|uki|uk&i|uk and ireland|dach region|cee|sea)\b")
# Regions, as opposed to countries and cities: in a sales title they often
# name the territory covered, not where the job sits ("Channel Account
# Executive, LATAM" based in Miami), so a region in the title overrides only
# a feed location that names no place (filter.title_location_override).
_REGIONS = re.compile(
    r"(?<![a-z])(emea|apac|apj|europe|eu|mena|latam|latin america|south america"
    r"|central america|nordics|dach|dach region|benelux|anz|uki|uk&i|uk and ireland|cee|sea)"
    r"(?![a-z])"
)
# Cities abroad whose name a US city shares ("Melbourne", Florida; "Paris",
# Texas; "Athens", Georgia): in a title they count only with their country
# ("Melbourne, Australia"), as "Cambridge" and "Richmond" count only with
# their state in US_CITY_STATES.
_AMBIGUOUS_ABROAD = frozenset(
    {"melbourne", "paris", "athens", "rome", "dublin", "lima", "berlin", "delhi", "cairo"}
)


def is_region_only(place_text: str) -> bool:
    """True when every place of a title place's text ("EMEA / APAC",
    "Central EMEA", "LATAM") is a region, not a country or a city."""
    atoms = [a.strip() for a in place_text.lower().split(" / ") if a.strip()]
    return bool(atoms) and all(
        _LETTER.search(_DIRECTIONS.sub(" ", _REGIONS.sub(" ", a))) is None
        and _REGIONS.search(a) is not None
        for a in atoms
    )


@dataclass(frozen=True)
class TitleLocation:
    """The place a job title names. `text` as written ("Denver, CO",
    "Colombia", "EMEA / APAC"); `us`: a US city, metro or state (False: a
    country, city or region outside the US); `remote`: the title says remote."""

    text: str
    us: bool
    remote: bool


def _is_non_us(atom: str) -> bool:
    a = atom.lower().strip(" .,")
    if not a or not (_NON_US.search(a) or _NON_US_EXTRA.search(a)):
        return False  # "West" alone names no region
    rest = _NON_US_EXTRA.sub(" ", _NON_US.sub(" ", a))
    if _LETTER.search(rest) is None:
        return True
    # "Central EMEA", "Northern Europe": a direction before a region
    rest = _DIRECTIONS.sub(" ", rest)
    return _LETTER.search(rest) is None and _DIRECTIONS.search(a) is not None


def _atom_kind(atom: str) -> str | None:
    """'us' (a US place), 'us-wide' (the US as a whole), 'abroad', or None
    (not a place)."""
    a = " ".join(atom.split()).strip(" .,;-–—")
    low = a.lower()
    if not low:
        return None
    if low in _US_COUNTRY_WORDS:
        return "us-wide"
    if len(a) > 2 and state_code(a):
        return "us"  # a state's full name ("Texas", "New Mexico")
    if low in US_CITY_STATES:
        return "us"
    if low in _AMBIGUOUS_ABROAD:
        return None  # "Melbourne" alone may be Florida's
    if _is_non_us(a):
        return "abroad"
    return None


def _part_places(part: str) -> list[tuple[str, str]] | None:
    """The places one comma part names, as (text, kind); [] when it is only a
    work mode ("Remote"); None when it is not (all) a place."""
    cleaned = " ".join(_TITLE_FILLER.sub(" ", part).split()).strip(" .,;-–—")
    if not cleaned:
        return []
    atoms = [a for a in _CONNECTORS.split(cleaned) if a and a.strip(" .,;-–—")]
    out: list[tuple[str, str]] = []
    for atom in atoms:
        kind = _atom_kind(atom)
        if kind is None:
            return None
        out.append((atom.strip(" .,;-–—"), kind))
    return out


# words of a role or a line of business, never of a city: "Solutions
# Architect, Platforms, UK" and "Professional Services, Sydney" pair no city
# with the place after them (review 2026-10-23); the place alone still counts
_NOT_CITY_WORDS = frozenset(
    re.findall(
        r"[a-z]+",
        """services service platforms platform pursuits pursuit solutions solution partnerships
    partnership partners partner accounts account sales operations ops products product
    engineering marketing success support alliances alliance channels channel programs program
    projects project strategy strategic enterprise commercial growth revenue business
    development team teams segment segments verticals vertical industries industry markets
    majors global named logos smb public sector healthcare financial fintech technology tech
    cloud data security infrastructure networking digital customer customers client clients
    consulting advisory delivery implementation professional technical field inside outbound
    inbound recruiting talent finance legal""",
    )
)


def _pair_place(city: str, region: str) -> tuple[str, str] | None:
    """'Denver', 'CO' -> ('Denver, CO', 'us'); 'Bogotá', 'Colombia' ->
    ('Bogotá, Colombia', 'abroad'); otherwise None. A work mode or filler
    word is not part of the city ("Remote EU, LATAM" pairs nothing)."""
    city = " ".join(_TITLE_FILLER.sub(" ", city).split()).strip(" .,;-–—")
    region = region.strip()
    if not _CITYISH.fullmatch(city) or city.lower() in _NOT_CITY:
        return None
    if any(w in _NOT_CITY_WORDS for w in re.findall(r"[a-z]+", city.lower())):
        return None
    if _atom_kind(city) in ("us-wide", "abroad"):
        return None
    # "VA, WV" lists states; "New York, NY" and "Washington, DC" are cities
    if state_code(city) and city.lower() not in ("new york", "washington"):
        return None
    if state_code(region):  # "CO" (upper case) or "Colorado"
        return f"{city}, {region}", "us"
    if _is_non_us(region):
        return f"{city}, {region}", "abroad"
    return None


def _segment_places(segment: str, head: bool) -> list[tuple[str, str]]:
    """The places a title segment names. In the head segment the first comma
    part is the role and is skipped."""
    parts = [p.strip() for p in segment.split(",")]
    start = 1 if head else 0
    out: list[tuple[str, str]] = []
    i = start
    while i < len(parts):
        if i + 1 < len(parts):
            pair = _pair_place(parts[i], parts[i + 1])
            if pair is not None:
                out.append(pair)
                i += 2
                continue
        places = _part_places(parts[i])
        if places:
            out.extend(places)
        i += 1
    return out


def title_location(title: str) -> TitleLocation | None:
    """The one place a job title names, or None (see the comment above)."""
    text = html.unescape(title or "").strip()
    if not text:
        return None
    remote = _TITLE_REMOTE.search(text) is not None
    inner = _BRACKETS.findall(text)
    outer = _BRACKETS.sub(" | ", text)
    chunks = [s for s in _TITLE_SEP.split(outer) if s.strip()]
    found: list[tuple[str, str]] = []
    for n, chunk in enumerate(chunks):
        found += _segment_places(chunk, head=n == 0)
    for content in inner:
        for chunk in _TITLE_SEP.split(content):
            if chunk.strip():
                found += _segment_places(chunk, head=False)
    us = [t for t, k in found if k == "us"]
    abroad = [t for t, k in found if k == "abroad"]
    wide = any(k == "us-wide" for _, k in found)
    if abroad:
        if us or wide:
            return None  # "US/Canada", "NYC or London": open to the US
        return TitleLocation(text=" / ".join(dict.fromkeys(abroad)), us=False, remote=remote)
    if len(set(us)) == 1:
        return TitleLocation(text=us[0], us=True, remote=remote)
    return None
