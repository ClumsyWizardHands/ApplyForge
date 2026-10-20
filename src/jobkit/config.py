"""Config loading: targets.yaml + filters.yaml. One knob, one owner.

The files under config/ hold one person's choices (role tracks, comp floors,
locations, standing rules, writing style, commute times), so they are local
and gitignored. The repo ships a commented template for each,
config/<name>.example.yaml; the setup interview (say "set up jobkit" in
Claude) writes the real files. Every reader goes through read_config_text, so
a missing file fails with one message that names both ways to create it."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from jobkit.normalize import location_bucket

CONFIG_DIR = Path("config")

# The personal config files: local, gitignored, written by the setup interview
# or copied from config/<stem>.example.yaml. `jobkit doctor` checks each one.
PERSONAL_CONFIG_FILES = (
    "filters.yaml",
    "targets.yaml",
    "autoapprove.yaml",
    "banned_phrases.yaml",
    "commute_table.yaml",
)

COMP_BASES = ("base", "ote")

# Location buckets (normalize.location_bucket) for remote postings the filter
# accepts when filters.locations.accept_remote is true (the default).
REMOTE_BUCKETS = frozenset({"remote-us", "remote-global"})
# US postal codes that are also English words: never an acceptable geo
# restriction by themselves ("US or Canada" is not Oregon).
_WORDLIKE_STATE_CODES = frozenset(
    {"AL", "CO", "DE", "HI", "ID", "IN", "LA", "MA", "ME", "OH", "OK", "OR", "PA"}
)


class ConfigMissing(FileNotFoundError):
    """A personal config file is not there. The message names the setup
    interview and the template to copy. A FileNotFoundError, so callers that
    already handle a missing file keep doing so."""


def config_missing_message(path: str | Path) -> str:
    name = Path(path).name
    stem = name.removesuffix(".yaml")
    return (
        f"config/{name} not found — run the jobkit setup interview "
        f'(say "set up jobkit" in Claude) or copy config/{stem}.example.yaml'
    )


def read_config_text(path: str | Path) -> str:
    """The text of a config file. A missing file raises ConfigMissing."""
    try:
        return Path(path).read_text()
    except FileNotFoundError as e:
        raise ConfigMissing(config_missing_message(path)) from e


def read_config_yaml(path: str | Path) -> Any:
    """yaml.safe_load of a config file. A missing file raises ConfigMissing."""
    return yaml.safe_load(read_config_text(path))


class Board(BaseModel):
    ats: str
    token: str


class Company(BaseModel):
    name: str
    domain: str | None = None
    tier: int = 3
    board: Board | None = None


class Targets(BaseModel):
    companies: list[Company]


@dataclass(frozen=True)
class Track:
    """One role track from `filters.tracks`: an id plus the title regex that
    places a posting in it. Tracks are defined once, in filters.yaml; comp
    floors and the standing-rules title class refer to them by id.

    `active` (filters.yaml `active:`, default true) is whether the track is
    pursued. A paused track still classifies — track_for returns it — so its
    floor, standing-rule class, scoring and tailoring emphasis keep working;
    only the filter treats its titles differently (Filters.paused_track_for)."""

    id: str
    title_regex: str
    active: bool = True

    def matches(self, title: str) -> bool:
        return re.search(self.title_regex, title) is not None


@dataclass(frozen=True)
class CompFloor:
    """The comp floor that applies to one title.

    basis "ote" means the floor is an on-target-earnings number. Many postings
    publish base only, so an OTE-basis posting passes when comp_max reaches the
    floor OR reaches floor × ote_base_share (a base that implies that OTE).
    With ote_base_share <= 1 that is simply comp_max >= floor × ote_base_share.

    That allowance is for a posted base only. When the posted figure is itself
    OTE (posted_basis "ote", see adapters.ats.greenhouse.comp_basis) it is
    compared with the full floor: an SDR posting with $72K OTE is under an $80K
    OTE floor. A base-basis floor is the same whatever the posting shows: an OTE
    figure under it means the base is under it too."""

    track: str | None  # None: the title matched no track, the default floor applies
    floor: int
    basis: str  # "base" | "ote"
    ote_base_share: float

    def effective_floor_for(self, posted_basis: str = "unknown") -> int:
        """The lowest comp_max that passes, given what the posted figure is
        ("base", "ote", or "unknown", which is treated as base)."""
        if self.basis == "ote" and posted_basis != "ote":
            return round(self.floor * self.ote_base_share)
        return self.floor

    @property
    def effective_floor(self) -> int:
        """The lowest comp_max that passes for a base (or unlabelled) figure."""
        return self.effective_floor_for()

    def passes(self, comp_max: int, posted_basis: str = "unknown") -> bool:
        return comp_max >= self.effective_floor_for(posted_basis)

    def describe(self) -> str:
        label = self.track or "default"
        return f"{label}(ote)" if self.basis == "ote" else label


# ---- LinkedIn guest discovery (CLAUDE.md hard rule 11, exception of 2026-10-12) ----
# The exception allows "a few queries a day". These are ceilings, not knobs:
# feeds.linkedin_guest in filters.yaml may set lower values, never higher ones
# (a higher value fails at load). Raising one means changing hard rule 11.
LINKEDIN_MAX_KEYWORDS = 4
LINKEDIN_SEARCH_CEILING = 6  # search requests per local day
LINKEDIN_DETAIL_CEILING = 40  # posting-detail requests per local day
LINKEDIN_MIN_DELAY_FLOOR = 3.0  # seconds between requests (randomized up to 2x)
# LinkedIn's workplace filter (f_WT): 1 on-site, 2 remote, 3 hybrid; "any" omits it
LINKEDIN_WORKPLACES = {"remote": "2", "hybrid": "3", "onsite": "1", "any": ""}
_LINKEDIN_DEFAULT_KEYWORDS = (
    "account executive",
    "sales manager",
    "sales director",
    "head of sales",
)
_LINKEDIN_DEFAULT_LOCATIONS = ({"location": "United States", "workplace": "remote"},)


@dataclass(frozen=True)
class LinkedInLocation:
    """One LinkedIn search location: the place name LinkedIn resolves
    (`location=`) and the workplace filter (`f_WT`)."""

    location: str
    workplace: str  # remote | hybrid | onsite | any

    @property
    def f_wt(self) -> str:
        return LINKEDIN_WORKPLACES[self.workplace]


@dataclass(frozen=True)
class LinkedInGuest:
    """`feeds.linkedin_guest` in filters.yaml: LinkedIn's logged-out job
    search, discovery only (discovery/linkedin_guest.py). The one owner of its
    switch, queries, caps and pacing."""

    enabled: bool
    keywords: tuple[str, ...]
    locations: tuple[LinkedInLocation, ...]
    posted_within_days: int
    max_search_requests_per_day: int
    max_detail_requests_per_day: int
    min_delay_seconds: float


def _whole(value: Any, name: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError(
            f"feeds.linkedin_guest.{name} must be a whole number {lo}-{hi}, got {value!r}"
        )
    return value


def parse_linkedin_guest(cfg: Any) -> LinkedInGuest:
    """Validate `feeds.linkedin_guest`. Absent: disabled, with the defaults."""
    if cfg is None:
        cfg = {}
    if not isinstance(cfg, dict):
        raise ValueError("feeds.linkedin_guest must be a mapping")
    enabled = cfg.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(f"feeds.linkedin_guest.enabled must be true or false, got {enabled!r}")
    raw_kw = cfg.get("keywords", list(_LINKEDIN_DEFAULT_KEYWORDS))
    if not isinstance(raw_kw, list) or not raw_kw:
        raise ValueError("feeds.linkedin_guest.keywords must be a non-empty list")
    keywords = tuple(str(k).strip() for k in raw_kw)
    if any(not k for k in keywords) or len(set(keywords)) != len(keywords):
        raise ValueError(f"feeds.linkedin_guest.keywords: empty or repeated keyword in {raw_kw}")
    if len(keywords) > LINKEDIN_MAX_KEYWORDS:
        raise ValueError(
            f"feeds.linkedin_guest.keywords: at most {LINKEDIN_MAX_KEYWORDS}, got {len(keywords)}"
        )
    raw_loc = cfg.get("locations", list(_LINKEDIN_DEFAULT_LOCATIONS))
    if not isinstance(raw_loc, list) or not raw_loc:
        raise ValueError("feeds.linkedin_guest.locations must be a non-empty list")
    locations: list[LinkedInLocation] = []
    for i, loc in enumerate(raw_loc):
        if not isinstance(loc, dict) or not str(loc.get("location", "")).strip():
            raise ValueError(f"feeds.linkedin_guest.locations[{i}] needs a location")
        workplace = str(loc.get("workplace", "any"))
        if workplace not in LINKEDIN_WORKPLACES:
            raise ValueError(
                f"feeds.linkedin_guest.locations[{i}].workplace must be one of "
                f"{sorted(LINKEDIN_WORKPLACES)}, got {workplace!r}"
            )
        locations.append(LinkedInLocation(str(loc["location"]).strip(), workplace))
    delay = cfg.get("min_delay_seconds", LINKEDIN_MIN_DELAY_FLOOR)
    if isinstance(delay, bool) or not isinstance(delay, int | float):
        raise ValueError(f"feeds.linkedin_guest.min_delay_seconds must be a number, got {delay!r}")
    if delay < LINKEDIN_MIN_DELAY_FLOOR:
        raise ValueError(
            f"feeds.linkedin_guest.min_delay_seconds must be at least "
            f"{LINKEDIN_MIN_DELAY_FLOOR:g}, got {delay!r}"
        )
    return LinkedInGuest(
        enabled=enabled,
        keywords=keywords,
        locations=tuple(locations),
        posted_within_days=_whole(cfg.get("posted_within_days", 7), "posted_within_days", 1, 30),
        max_search_requests_per_day=_whole(
            cfg.get("max_search_requests_per_day", LINKEDIN_SEARCH_CEILING),
            "max_search_requests_per_day",
            0,
            LINKEDIN_SEARCH_CEILING,
        ),
        max_detail_requests_per_day=_whole(
            cfg.get("max_detail_requests_per_day", 0),
            "max_detail_requests_per_day",
            0,
            LINKEDIN_DETAIL_CEILING,
        ),
        min_delay_seconds=float(delay),
    )


# ---- board resolution: aggregator / LinkedIn jobs -> the employer's own board ----
# discovery/board_resolver.py. The delay floor and the request ceiling are
# limits, not knobs: board_resolution in filters.yaml may be slower or ask
# less, never faster or more (a value past them fails at load).
BOARD_PROBE_MIN_DELAY = 1.0  # seconds between two requests to the ATS APIs
BOARD_PROBE_REQUEST_CEILING = 1000  # board-listing requests per run


@dataclass(frozen=True)
class BoardResolution:
    """`board_resolution` in filters.yaml: the one owner of the switch, the
    probe cache and the pacing of discovery/board_resolver.py."""

    enabled: bool
    probe_cache_days: int  # a company is slug-probed at most once per this many days
    delay_seconds: float  # between two requests to the Greenhouse/Lever/Ashby APIs
    max_requests_per_run: int  # board-listing requests one run may send


def parse_board_resolution(cfg: Any) -> BoardResolution:
    """Validate `board_resolution`. Absent: on, with the defaults."""
    if cfg is None:
        cfg = {}
    if not isinstance(cfg, dict):
        raise ValueError("board_resolution must be a mapping")
    enabled = cfg.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"board_resolution.enabled must be true or false, got {enabled!r}")
    days = cfg.get("probe_cache_days", 14)
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 90:
        raise ValueError(
            f"board_resolution.probe_cache_days must be a whole number 1-90, got {days!r}"
        )
    delay = cfg.get("delay_seconds", BOARD_PROBE_MIN_DELAY)
    if isinstance(delay, bool) or not isinstance(delay, int | float):
        raise ValueError(f"board_resolution.delay_seconds must be a number, got {delay!r}")
    if delay < BOARD_PROBE_MIN_DELAY:
        raise ValueError(
            f"board_resolution.delay_seconds must be at least {BOARD_PROBE_MIN_DELAY:g}, "
            f"got {delay!r}"
        )
    cap = cfg.get("max_requests_per_run", 300)
    if (
        isinstance(cap, bool)
        or not isinstance(cap, int)
        or not 0 <= cap <= BOARD_PROBE_REQUEST_CEILING
    ):
        raise ValueError(
            f"board_resolution.max_requests_per_run must be a whole number "
            f"0-{BOARD_PROBE_REQUEST_CEILING}, got {cap!r}"
        )
    return BoardResolution(
        enabled=enabled,
        probe_cache_days=days,
        delay_seconds=float(delay),
        max_requests_per_run=cap,
    )


class Filters(BaseModel):
    """Typed view of the parts M1 needs; the full file rides along in raw."""

    raw: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_tracks_and_floors(self) -> Filters:
        """Fail at load, not mid-run: every track has an id and a compiling
        regex, ids are unique, and every per-track floor names a defined track."""
        ids = [t.id for t in self.tracks]
        if len(ids) != len(set(ids)):
            raise ValueError(f"filters.tracks: duplicate track id in {ids}")
        for t in self.tracks:
            try:
                re.compile(t.title_regex)
            except re.error as e:
                raise ValueError(f"filters.tracks[{t.id}]: bad title_regex: {e}") from e
        floors = self._comp().get("track_floors") or {}
        if not isinstance(floors, dict):
            raise ValueError("filters.comp.track_floors must be a mapping of track id -> floor")
        for track_id, spec in floors.items():
            if track_id not in ids:
                raise ValueError(
                    f"filters.comp.track_floors: '{track_id}' is not a track id "
                    f"(defined: {ids}) — tracks are defined once, in filters.tracks"
                )
            if not isinstance(spec, dict) or "floor" not in spec:
                raise ValueError(f"filters.comp.track_floors.{track_id}: needs a floor")
            int(spec["floor"])
            if str(spec.get("basis", "base")) not in COMP_BASES:
                raise ValueError(
                    f"filters.comp.track_floors.{track_id}.basis must be one of {COMP_BASES}"
                )
        share = self.ote_base_share
        if not 0 < share <= 1:
            raise ValueError(f"filters.comp.ote_base_share must be in (0, 1], got {share}")
        return self

    def _filters(self) -> dict[str, Any]:
        f = self.raw.get("filters", {}) or {}
        return f if isinstance(f, dict) else {}

    def _comp(self) -> dict[str, Any]:
        c = self._filters().get("comp", {}) or {}
        return c if isinstance(c, dict) else {}

    # ---- role tracks ----

    @property
    def tracks(self) -> list[Track]:
        """Ordered role tracks. First match wins."""
        out: list[Track] = []
        for i, t in enumerate(self._filters().get("tracks") or []):
            if not isinstance(t, dict) or not t.get("id") or not t.get("title_regex"):
                raise ValueError(f"filters.tracks[{i}] needs an id and a title_regex")
            active = t.get("active", True)
            if not isinstance(active, bool):
                raise ValueError(
                    f"filters.tracks[{t['id']}].active must be true or false, got {active!r}"
                )
            if "hold_regex" in t:
                # the release exception it guarded was declined (owner,
                # 2026-10-10): a knob that no longer does anything must not
                # look as if it did
                raise ValueError(
                    f"filters.tracks[{t['id']}].hold_regex is no longer supported: a paused "
                    "track holds every title whose first matching track it is"
                )
            out.append(Track(id=str(t["id"]), title_regex=str(t["title_regex"]), active=active))
        return out

    def track_for(self, title: str) -> str | None:
        """The id of the first track whose title_regex matches, or None.
        Active or paused alike: pausing a track never moves its titles into a
        later track (an "SDR (AE track)" title stays sdr_bdr, never an AE)."""
        for t in self.tracks:
            if t.matches(title):
                return t.id
        return None

    def paused_track_for(self, title: str) -> str | None:
        """The id of the title's track (its FIRST matching one) when that track
        is paused (`active: false`), else None. The filter rejects such a title
        as `track_paused:<id>`. A title whose first match is active is never
        paused, even if a later, paused track also matches it: "SDR Manager"
        is sdr_bdr_manager, whatever sdr_bdr's state.

        This is the rule, with no exception: neither a later active track
        ("Sales Development Representative - AE Track") nor a non-track
        include_titles pattern ("Revenue Operations, Business Development")
        releases a title its first, paused track claims. Some roles a user
        may want are held this way ("Business Development Executive",
        "Revenue Operations, Business Development"); that cost was accepted,
        and the README lists the known cases."""
        for t in self.tracks:
            if t.matches(title):
                return None if t.active else t.id
        return None

    @property
    def paused_tracks(self) -> list[str]:
        """Ids of the tracks set `active: false`, in track order."""
        return [t.id for t in self.tracks if not t.active]

    # ---- titles ----

    @property
    def include_titles(self) -> list[str]:
        """Every track's title_regex, then the non-track include patterns.
        A paused track's regex stays an include: its titles are rejected as
        `track_paused:<id>` (which refilter can reverse), not as
        no_include_title_match."""
        return [t.title_regex for t in self.tracks] + self.explicit_include_titles

    @property
    def explicit_include_titles(self) -> list[str]:
        """The non-track include patterns (filters.yaml `include_titles`): roles
        targeted on their own, outside every track. They never release a title
        a paused track claims first (see paused_track_for)."""
        return [str(p) for p in self._filters().get("include_titles", []) or []]

    @property
    def exclude_titles(self) -> list[str]:
        return [str(p) for p in self._filters().get("exclude_titles", []) or []]

    # ---- comp ----

    @property
    def comp_floor(self) -> int:
        """The default floor: applies to titles that match no track."""
        return int(self._comp().get("floor", 0) or 0)

    @property
    def comp_currency(self) -> str:
        return str(self._comp().get("currency", "USD") or "USD")

    @property
    def ote_base_share(self) -> float:
        return float(self._comp().get("ote_base_share", 1.0))

    def _track_floor(self, track_id: str) -> tuple[int, str] | None:
        spec = (self._comp().get("track_floors") or {}).get(track_id)
        if not spec:
            return None
        return int(spec["floor"]), str(spec.get("basis", "base"))

    def comp_floor_for(self, title: str) -> CompFloor:
        """The floor for this title: its track's floor if the track has one,
        otherwise the default floor (comp.floor)."""
        track = self.track_for(title)
        hit = self._track_floor(track) if track else None
        floor, basis = hit if hit else (self.comp_floor, "base")
        return CompFloor(track=track, floor=floor, basis=basis, ote_base_share=self.ote_base_share)

    @property
    def lowest_comp_floor(self) -> int:
        """The lowest comp_max the filter accepts for any title: the minimum
        effective floor over the default and every track floor. Source-side
        salary filters (Adzuna salary_min) use this, so a feed never drops a
        posting the filter itself would keep. 0 when no floor is configured.
        Paused tracks count too: their postings are still ingested and held
        as track_paused, so re-enabling the track and running refilter brings
        them back (a source-side cut would have dropped them unseen)."""
        specs: list[tuple[int, str]] = [(self.comp_floor, "base")]
        for t in self.tracks:
            hit = self._track_floor(t.id)
            if hit:
                specs.append(hit)
        floors = [
            CompFloor(None, floor, basis, self.ote_base_share).effective_floor
            for floor, basis in specs
            if floor
        ]
        return min(floors) if floors else 0

    @property
    def max_age_days(self) -> int:
        return int(self._filters().get("freshness", {}).get("max_age_days", 30))

    # ---- locations (filters.locations + remote_policy.relocation_cities) ----
    # No place is hardcoded in Python: the filter, the commute exemption and
    # the geo-restriction check all read these lists.

    def _locations(self) -> dict[str, Any]:
        loc = self._filters().get("locations", {}) or {}
        return loc if isinstance(loc, dict) else {}

    def _remote_policy(self) -> dict[str, Any]:
        rp = self.raw.get("remote_policy", {}) or {}
        return rp if isinstance(rp, dict) else {}

    @staticmethod
    def _places(value: object, key: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError(f"{key} must be a list of place names, got {value!r}")
        return [str(p).strip() for p in value if str(p).strip()]

    @property
    def allowed_locations(self) -> list[str]:
        """`filters.locations.allowed`: the places (cities, metros) a posting
        that is not remote may be in."""
        return self._places(self._locations().get("allowed"), "filters.locations.allowed")

    @property
    def relocation_cities(self) -> list[str]:
        """`remote_policy.relocation_cities`: places the candidate would move
        to. A posting there passes the location filter like an allowed place,
        and the commute cap does not apply to it (the triage remote check)."""
        return self._places(
            self._remote_policy().get("relocation_cities"), "remote_policy.relocation_cities"
        )

    @property
    def accept_remote(self) -> bool:
        """`filters.locations.accept_remote` (default true): a remote posting
        passes the location filter wherever its company is."""
        value = self._locations().get("accept_remote", True)
        if not isinstance(value, bool):
            raise ValueError(
                f"filters.locations.accept_remote must be true or false, got {value!r}"
            )
        return value

    @staticmethod
    def _place_buckets(places: list[str]) -> frozenset[str]:
        """Each place's location bucket, from normalize.location_bucket — the
        function that buckets postings — so "Brooklyn" names nyc-metro and a
        city outside the metro table ("Columbus") names other-columbus, which
        is what a "Columbus, OH" posting gets. Remote buckets never come from a
        place name (accept_remote decides those)."""
        buckets = {location_bucket(p, False) for p in places}
        return frozenset(b for b in buckets if not b.startswith("remote-") and b != "other-unknown")

    @property
    def accepted_location_buckets(self) -> frozenset[str]:
        """The location buckets the deterministic filter accepts: every allowed
        place's and relocation city's bucket, plus the remote buckets when
        accept_remote is on."""
        out = set(self._place_buckets(self.allowed_locations + self.relocation_cities))
        if self.accept_remote:
            out |= REMOTE_BUCKETS
        return frozenset(out)

    @property
    def relocation_buckets(self) -> frozenset[str]:
        """The location buckets of the relocation cities (commute cap exempt)."""
        return self._place_buckets(self.relocation_cities)

    @property
    def acceptable_geo_restrictions(self) -> list[str]:
        """What a geo restriction found in a posting ("US only", "must live in
        NJ") may name and still pass the remote check: the entries of
        `remote_policy.acceptable_geo_restrictions`, every allowed place and
        relocation city (where the candidate lives or would move), and, for
        each US state those places name, the state's name and its postal code
        ("New Jersey" also accepts "NJ"). Codes that are English words ("OR",
        "IN", "ME", ...) are left out: they would match "US or Canada"."""
        from jobkit.autofill.options import US_STATES, states_in

        entries = self._places(
            self._remote_policy().get("acceptable_geo_restrictions"),
            "remote_policy.acceptable_geo_restrictions",
        )
        places = self.allowed_locations + self.relocation_cities
        out = entries + places
        for place in places:
            for code in sorted(states_in(place)):
                out.append(US_STATES[code])
                if code not in _WORDLIKE_STATE_CODES:
                    out.append(code)
        return list(dict.fromkeys(out))

    @property
    def stale_after_hours(self) -> float:
        """`schedule.stale_after_hours`: past this many hours since the last
        successful board poll, the digest, get_queue and get_stats warn that
        the data is stale (default 36)."""
        sched = self.raw.get("schedule") or {}
        value = sched.get("stale_after_hours", 36) if isinstance(sched, dict) else 36
        hours = float(value)
        if hours <= 0:
            raise ValueError(f"schedule.stale_after_hours must be positive, got {value}")
        return hours

    @property
    def self_review_max_rounds(self) -> int:
        """`self_review.max_revision_rounds` (default 1): how many self-review
        revise rounds a passing resume selection may get per run, and so how
        many reviews (at least one) each drafted artifact gets
        (pipeline/self_review.py). A cover letter or drafted answer is never
        revised. 0 keeps the review and its report, with no revision. A
        non-integer or negative value fails loudly."""
        sr = self.raw.get("self_review") or {}
        value = sr.get("max_revision_rounds", 1) if isinstance(sr, dict) else 1
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(
                f"self_review.max_revision_rounds must be a whole number >= 0, got {value!r}"
            )
        return value

    @property
    def questions_per_day(self) -> int:
        """`questions.max_per_day` (default 3): how many questions for the
        human (pipeline/questions.py) the digest, `jobkit questions`, MCP
        get_questions and get_queue put in front of them per local day. A
        question answered or declined that day still counts. 0 asks nothing.
        A non-integer or negative value fails loudly."""
        qs = self.raw.get("questions") or {}
        value = qs.get("max_per_day", 3) if isinstance(qs, dict) else 3
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"questions.max_per_day must be a whole number >= 0, got {value!r}")
        return value

    @property
    def min_score_to_surface(self) -> int:
        """`gates.min_score_to_surface` (default 65): the queue gate. A scored
        job at or above it is queued, one below it is rejected
        (score_below_surface). Read by triage.run_queue and pipeline/rescore."""
        gates = self.raw.get("gates") or {}
        value = gates.get("min_score_to_surface", 65) if isinstance(gates, dict) else 65
        return int(value)

    @property
    def linkedin_guest(self) -> LinkedInGuest:
        """`feeds.linkedin_guest` (default: disabled). Read only by
        discovery/linkedin_guest.py. A bad value, or a cap above its ceiling,
        fails loudly."""
        feeds = self.raw.get("feeds") or {}
        return parse_linkedin_guest(
            feeds.get("linkedin_guest") if isinstance(feeds, dict) else None
        )

    @property
    def board_resolution(self) -> BoardResolution:
        """`board_resolution` (default: on). Read only by
        discovery/board_resolver.py. A bad value fails loudly."""
        return parse_board_resolution(self.raw.get("board_resolution"))

    @property
    def nightly_at(self) -> tuple[int, int]:
        """`schedule.nightly_at` ("HH:MM", 24-hour local time, default "02:00"):
        when the launchd agent runs `jobkit nightly`. Read by `jobkit schedule
        install`; a change takes effect on the next install."""
        sched = self.raw.get("schedule") or {}
        value = sched.get("nightly_at", "02:00") if isinstance(sched, dict) else "02:00"
        if not isinstance(value, str):
            # unquoted 2:00 is a YAML 1.1 sexagesimal integer (120), not a time
            raise ValueError(f'schedule.nightly_at must be a quoted "HH:MM" string, got {value!r}')
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ValueError(f'schedule.nightly_at must be "HH:MM" (00:00-23:59), got {value!r}')
        return int(m.group(1)), int(m.group(2))


def load_targets(path: Path | None = None) -> Targets:
    p = path or CONFIG_DIR / "targets.yaml"
    return Targets.model_validate(read_config_yaml(p))


def load_filters(path: Path | None = None) -> Filters:
    p = path or CONFIG_DIR / "filters.yaml"
    return Filters(raw=read_config_yaml(p))
