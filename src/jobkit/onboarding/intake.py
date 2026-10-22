"""Phase 2.1-2.6 — intake interview answers → config (ONBOARDING.md). MCP:
run_intake.

Claude asks the questions (against the corpus: ONBOARDING.md says why); this
module turns the human's answers into config/filters.yaml and
config/commute_table.yaml, and writes config/autoapprove.yaml and
config/banned_phrases.yaml (the template's lists) when those are missing.

Automatic approval is the person's choice (answer `auto_approve`). Yes writes
one standing rule: approve every job with a fit score at or above their bar
(default 75) and no sanity flag, reason "clears-your-bar". A daily limit
(max_auto_approve_per_day) is written only when they give a number; the
default is no limit. No, or no answer, writes no rule. An autoapprove.yaml
holding rules the interview did not write is always kept as it is, whatever
replace_existing says (that flag is for filters.yaml and the commute table):
the notes name the rule to add, or to remove on a no.

The answer `open_forms` sets staging.open_after_daily_run: whether the
scheduled daily run ends by opening the filled forms for review. It is yes
when not given.

The answer `alert_search` writes the `search` block: search.alert_web_search
(whether Claude searches the web for alert jobs board lookup cannot find)
and, only when the person names a number, search.max_searches_per_check.
No answer writes no block, and search stays off.

Every interview-driven setting in filters.yaml carries a comment quoting the
answer that produced it, so when a filter rejects something surprising the
comment says which answer to revisit. Sections the interview does not cover
(gates, tracking, tailoring, friction) are copied from
config/filters.example.yaml with its documentation.

It works for any field. A track's titles become a phrase regex in which a
space also matches a hyphen, a slash or nothing, and "senior" matches "Sr.";
title_words (the word every title has, and the words that narrow it) add a
match on words in any order; should_match / should_not_match titles are
checked at setup and never written. A track can be a change of field (pivot)
with its own score gate (min_score) and a line on what to lead with
(emphasis). Hourly pay is converted to a yearly floor, with the arithmetic
in a comment. Nearby towns become places with their own commute times; a
town that jobkit would file under a different metro is kept with its state,
or left out and named when it has none.
Feeds get the person's titles and places, the remote-only boards are turned
off when remote work is not wanted, and USAJOBS is turned on only when they
want federal jobs. prompt_set is general, or sales when every track is a
software sales title (not retail, insurance, device or other sales) and no
track sets pivot or emphasis (only the general prompts
read those two).

The generated file is loaded with the same loader the pipeline uses before
it is written; a config that would not load is never written. A config the
interview did not write is not replaced without replace_existing (and is then
backed up to data/setup/backup/).

The default comp floor (comp.default_floor) is always asked, never derived:
it covers titles outside every track and is also the line the answer bank's
single salary-expectation figure is measured against. Without it the
form-filling gate blanks that field on every track's jobs, so the answers
are refused until it is given; a track whose own floor is under it is named
in the notes (its jobs get the field blank and flagged).

What there is no knob for is said, not invented: jobkit has no industry
filter (postings carry no industry), so wanted and refused industries are
recorded as comments and used to seed targets.yaml, and named companies to
avoid go to `blocklist_companies`."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from jobkit.autofill.answer_bank import comp_expectation_block
from jobkit.autofill.options import US_STATES, is_us, state_code, states_in
from jobkit.config import TRACK_EMPHASIS_MAX, CompSanity, Filters
from jobkit.normalize import METRO_STATES, location_bucket
from jobkit.onboarding.files import (
    GENERATED_MARK,
    SetupError,
    SetupFiles,
    backup,
    dump_yaml,
    guard_overwrite,
    is_generated,
    setup_files,
    template_path,
    today,
    write_text_atomic,
)
from jobkit.rules import max_auto_approve_per_day

# ---------------------------------------------------------------------------
# the answers
# ---------------------------------------------------------------------------


class _Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    said: str = ""  # the human's own words; quoted in the config comment


class TitleWords(BaseModel):
    """Words a title must hold, in any order. `all`: each entry must appear
    (an entry that is a list is met by any one of its words, e.g. ["engineer",
    "developer"]). `any`: at least one of these must appear. Each word or
    phrase matches like a title phrase (_word_regex)."""

    model_config = ConfigDict(extra="forbid")
    all: list[str | list[str]] = Field(default_factory=list)
    any: list[str] = Field(default_factory=list)


class TrackAnswer(_Answer):
    name: str
    id: str | None = None
    titles: list[str] = Field(default_factory=list)  # title phrases; default [name]
    title_words: TitleWords | None = None
    title_regex: str | None = None  # expert override of the phrases and words
    # titles the person kept or dropped from a proposed list: checked at
    # setup (check_titles), never written to filters.yaml
    should_match: list[str] = Field(default_factory=list)
    should_not_match: list[str] = Field(default_factory=list)
    comp_floor: int | float | None = None  # hourly when comp.unit is hourly
    comp_basis: Literal["base", "ote"] = "base"
    active: bool = True
    pivot: bool = False  # a change of field
    min_score: int | None = None  # this track's queue gate
    emphasis: str | None = None  # what should lead the resume, in their words

    @model_validator(mode="after")
    def _check_track(self) -> TrackAnswer:
        if self.min_score is not None and not 0 <= self.min_score <= 100:
            raise ValueError(f"track {self.name!r}: min_score must be 0-100")
        if self.emphasis is not None:
            text = " ".join(self.emphasis.split())
            if len(text) > TRACK_EMPHASIS_MAX:
                raise ValueError(
                    f"track {self.name!r}: emphasis is one line of at most "
                    f"{TRACK_EMPHASIS_MAX} characters"
                )
            self.emphasis = text or None
        return self


class ListAnswer(_Answer):
    items: list[str] = Field(default_factory=list)


class SeniorityAnswer(_Answer):
    level: Literal["any", "no_internships", "experienced", "senior"] = "any"


class LocationAnswer(_Answer):
    work_mode: Literal["remote_only", "hybrid", "onsite_ok"]
    accept_remote: bool = True
    places: list[str] = Field(default_factory=list)  # where a non-remote posting may be
    # the US state of the places ("OH" or "Ohio"), for the feed searches that
    # want "City, ST"; a place written "Columbus, OH" carries its own
    state: str | None = None
    # towns within the commute that the person kept: town -> minutes. Each
    # becomes a place with its own commute time. A bare town gets `state`; a
    # town across a state line is given as "Covington, KY". A town jobkit
    # files under a different metro is kept only when it has a state, and is
    # named in the notes either way
    nearby: dict[str, int] = Field(default_factory=dict)
    relocation_cities: list[str] = Field(default_factory=list)
    max_office_days_per_week: int | None = None
    max_commute_minutes: int | None = None
    commute_minutes: dict[str, int] = Field(default_factory=dict)  # place -> minutes
    acceptable_geo_restrictions: list[str] | None = None


class CompAnswer(_Answer):
    # titles that match no track. Required (Preferences._check): it is also
    # the line the answer bank's salary-expectation figure is measured
    # against (autofill.answer_bank.comp_expectation_block), which blocks
    # that figure on every track's jobs while there is no default floor.
    default_floor: int | float | None = None
    currency: str = "USD"
    ote_base_share: float | None = None
    # hourly: default_floor and the track floors are hourly rates, written
    # as yearly floors (rate x hours_per_week x 52) with the sum in a comment
    unit: Literal["annual", "hourly"] = "annual"
    hours_per_week: float | None = None


class FederalAnswer(_Answer):
    wanted: bool = False  # turns on the USAJOBS feed


class IndustryAnswer(_Answer):
    wanted: list[str] = Field(default_factory=list)
    refused: list[str] = Field(default_factory=list)


AUTO_APPROVE_MIN_SCORE_DEFAULT = 75


class AutoApproveAnswer(_Answer):
    """Whether jobs that clear the person's bar are approved without asking.
    `per_day` is an optional daily limit: absent (the default) means none."""

    enabled: bool
    # strict: true is not a bar of 1, and "80" is not a number
    min_score: int = Field(default=AUTO_APPROVE_MIN_SCORE_DEFAULT, strict=True)
    per_day: int | None = Field(default=None, strict=True)  # strict: true is not 1

    @model_validator(mode="after")
    def _check_auto(self) -> AutoApproveAnswer:
        if not 0 <= self.min_score <= 100:
            raise ValueError("auto_approve.min_score must be 0-100")
        if self.per_day is not None:
            if not self.enabled:
                raise ValueError(
                    "auto_approve.per_day is a limit on automatic approval; leave it out "
                    "when enabled is false"
                )
            if self.per_day < 1:
                raise ValueError(
                    "auto_approve.per_day must be 1 or more; leave it out for no daily limit"
                )
        return self


class OpenFormsAnswer(_Answer):
    """Whether the scheduled daily run ends by opening the filled forms."""

    after_daily_run: bool = True


class AlertSearchAnswer(_Answer):
    """Whether Claude searches the web for a job from a job-alert email that
    board lookup cannot find, and how much of the person's Claude usage that
    may take: `per_check` is an optional number of searches per email check;
    absent (the default) means no limit."""

    enabled: bool = Field(strict=True)
    per_check: int | None = Field(default=None, strict=True)  # strict: true is not 1

    @model_validator(mode="after")
    def _check_search(self) -> AlertSearchAnswer:
        if self.per_check is not None:
            if not self.enabled:
                raise ValueError(
                    "alert_search.per_check is a limit on web searches; leave it out when "
                    "enabled is false"
                )
            if self.per_check < 1:
                raise ValueError(
                    "alert_search.per_check must be 1 or more; leave it out for no limit"
                )
        return self


class StyleAnswer(_Answer):
    estimate_style: Literal["hedged", "confident"]


# cities that share a state's name: a place written this way is the city
_CITIES_NAMED_LIKE_STATES = frozenset({"new york", "washington"})


def _given_state(segment: str) -> str | None:
    """The US state written after a place's comma, in any case: "KY",
    "ky", "Kentucky", "D.C."; None when it is not a US state."""
    code = state_code(segment)
    if code is None:
        bare = segment.replace(".", "").strip()
        if re.fullmatch(r"[A-Za-z]{2}", bare):
            code = state_code(bare.upper())
    return code


def _with_state_code(place: str, field: str) -> str:
    """A place as 'Town, ST': the part after the comma must be a US state
    (a trailing ", USA" is dropped), written back as its two-letter code. A
    place with no comma is returned as given. Raises ValueError naming the
    place when the part after the comma is not a US state."""
    town, comma, rest = place.partition(",")
    if not comma:
        return place
    parts = [s.strip() for s in rest.split(",")]
    code = _given_state(parts[0]) if parts[0] else None
    if code is None or not all(is_us(s) for s in parts[1:]):
        raise ValueError(
            f"locations.{field}: {place!r}: {rest.strip()!r} is not a US state. Write the "
            "place as 'Town, ST' with a US state ('Covington, KY'), or leave the state off "
            "and give it as locations.state"
        )
    return f"{' '.join(town.split())}, {code}"


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tracks: list[TrackAnswer] = Field(min_length=1)
    include_titles: ListAnswer = Field(default_factory=ListAnswer)
    exclude_titles: ListAnswer = Field(default_factory=ListAnswer)
    seniority_floor: SeniorityAnswer = Field(default_factory=SeniorityAnswer)
    locations: LocationAnswer
    comp: CompAnswer = Field(default_factory=CompAnswer)
    industries: IndustryAnswer = Field(default_factory=IndustryAnswer)
    blocklist_companies: ListAnswer = Field(default_factory=ListAnswer)
    federal: FederalAnswer = Field(default_factory=FederalAnswer)
    max_age_days: int | None = None
    nightly_at: str | None = None
    writing_style: StyleAnswer | None = None
    auto_approve: AutoApproveAnswer | None = None
    open_forms: OpenFormsAnswer = Field(default_factory=OpenFormsAnswer)
    alert_search: AlertSearchAnswer | None = None

    @model_validator(mode="after")
    def _check(self) -> Preferences:
        loc = self.locations
        if loc.work_mode == "remote_only" and loc.places:
            raise ValueError(
                "locations: work_mode remote_only takes no places; use hybrid or onsite_ok "
                "for places a non-remote job may be in"
            )
        if loc.work_mode != "remote_only" and not loc.places:
            raise ValueError(f"locations: work_mode {loc.work_mode} needs at least one place")
        if loc.work_mode == "hybrid" and loc.max_office_days_per_week is None:
            raise ValueError("locations: hybrid needs max_office_days_per_week (ask)")
        if loc.work_mode != "remote_only" and loc.max_commute_minutes is None:
            raise ValueError(f"locations: {loc.work_mode} needs max_commute_minutes (ask)")
        if loc.max_office_days_per_week is not None and not 0 <= loc.max_office_days_per_week <= 5:
            raise ValueError("locations.max_office_days_per_week must be 0-5")
        if loc.work_mode == "remote_only" and not loc.accept_remote:
            raise ValueError("locations: work_mode remote_only needs accept_remote true")
        if loc.work_mode == "remote_only" and loc.nearby:
            raise ValueError("locations: work_mode remote_only takes no nearby towns")
        if loc.state is not None and state_code(loc.state) is None:
            raise ValueError(
                f"locations.state: {loc.state!r} is not a US state (give 'OH' or 'Ohio')"
            )
        for place in [*loc.places, *loc.nearby]:
            town = place.split(",")[0].strip()
            if town.lower() not in _CITIES_NAMED_LIKE_STATES and state_code(town) is not None:
                raise ValueError(
                    f"locations: {place!r} is a state, not a city. Postings name a city, so "
                    "ask which cities in that state they would work in, and give the state "
                    "as locations.state"
                )
        # the state after a comma is checked and written as its two-letter code
        # ("Covington, ky" -> "Covington, KY"), so match_state and the feed
        # searches read it
        loc.places = [_with_state_code(p, "places") for p in loc.places]
        loc.nearby = {_with_state_code(k, "nearby"): v for k, v in loc.nearby.items()}
        loc.commute_minutes = {
            _with_state_code(k, "commute_minutes"): v for k, v in loc.commute_minutes.items()
        }
        comp = self.comp
        if comp.unit == "hourly" and (comp.hours_per_week is None or comp.hours_per_week <= 0):
            raise ValueError(
                "comp.hours_per_week: ask how many hours a week the hourly pay is for (it "
                "turns the hourly rate into the yearly floor the filters compare)"
            )
        if comp.hours_per_week is not None and comp.hours_per_week > 84:
            raise ValueError("comp.hours_per_week must be at most 84")
        if self.nightly_at is not None and not re.fullmatch(
            r"(?:[01]?\d|2[0-3]):[0-5]\d", self.nightly_at.strip()
        ):
            raise ValueError('nightly_at must be "HH:MM" (24-hour)')
        if self.comp.ote_base_share is not None and not 0 < self.comp.ote_base_share <= 1:
            raise ValueError("comp.ote_base_share must be in (0, 1]")
        if self.comp.default_floor is None or self.comp.default_floor <= 0:
            raise ValueError(
                "comp.default_floor: ask the floor for titles outside your tracks (the lowest "
                "pay you would take for a role you did not name). The answer bank's "
                "salary-expectation figure is measured against it: without one, the "
                "salary-expectation field on every job in your tracks is left blank and flagged"
            )
        return self


# ---------------------------------------------------------------------------
# YAML text with comments
# ---------------------------------------------------------------------------


def _q(s: str) -> str:
    """A single-quoted YAML scalar (regexes stay readable: only ' is escaped)."""
    return "'" + " ".join(s.split()).replace("'", "''") + "'"


def _flow(items: list[str]) -> str:
    return "[" + ", ".join(_q(i) for i in items) + "]"


def _said(text: str, indent: str) -> list[str]:
    s = " ".join(text.split()).replace('"', "'")
    if not s:
        return []
    if len(s) > 240:
        s = s[:237] + "..."
    return [f'{indent}# you said: "{s}"']


_SENIOR = re.compile(r"(?i)senior|sr\.?")
_WORD_GAP = r"[\s\-/]*"


# where one written word can be two: a lower-case letter then a capital
# ("eLearning", "SharePoint"), so "E-Learning" and "Share Point" match too
_CAMEL_SPLIT = re.compile(r"(?<=[a-z])(?=[A-Z])")


def _word_regex(phrase: str) -> str:
    """One title phrase as a regex body: its words in order, and between two
    words any run of spaces, hyphens or slashes, or nothing ("back end"
    matches "Back-End" and "Backend"; "eLearning" matches "E-Learning").
    "senior" and "sr." match each other."""
    words = [w for w in re.split(r"[\s\-/]+", phrase.strip()) if w]
    return _WORD_GAP.join(
        r"(?:senior|sr\.?)"
        if _SENIOR.fullmatch(w)
        else _WORD_GAP.join(re.escape(part) for part in _CAMEL_SPLIT.split(w))
        for w in words
    )


_SEARCH_DROP = re.compile(r"(?i)\b(?:senior|sr|junior|jr)\b\.?|\((?:senior|sr\.?)\)")
_SEARCH_LEVEL = re.compile(r"(?i)\s+(?:i{1,3}|iv|[1-4])\s*$")


def _search_phrase(title: str) -> str:
    """An example title as a search phrase: the part before a comma, dash or
    bracket, with seniority words and a trailing level dropped."""
    head = re.split(r",|\s[-–—]\s|\s\(", title, maxsplit=1)[0]
    head = _SEARCH_DROP.sub(" ", head)
    head = _SEARCH_LEVEL.sub("", " ".join(head.split()))
    return head.strip(" .")


def _bounded(phrases: list[str]) -> str:
    alts = [_word_regex(p) for p in phrases if p.strip()]
    return r"(?<!\w)(?:" + "|".join(alts) + r")(?!\w)"


def _phrase_regex(phrases: list[str]) -> str:
    """(?i) whole-phrase alternation (_word_regex)."""
    return "(?i)" + _bounded(phrases)


def _title_words_regex(tw: TitleWords | None) -> str | None:
    """Lookaheads that hold when every `all` entry and one `any` word appear
    anywhere in the title, in any order; None when there are no words."""
    if tw is None:
        return None
    looks: list[str] = []
    for item in tw.all:
        alts = [a for a in ([item] if isinstance(item, str) else item) if a.strip()]
        if alts:
            looks.append(f"(?=.*{_bounded(alts)})")
    anys = [a for a in tw.any if a.strip()]
    if anys:
        looks.append(f"(?=.*{_bounded(anys)})")
    return "^" + "".join(looks) if looks else None


def _track_regex(phrases: list[str], tw: TitleWords | None) -> str:
    """The track's title regex: its phrases, or its title words."""
    words = _title_words_regex(tw)
    if words is None:
        return _phrase_regex(phrases)
    if not phrases:
        return "(?i)" + words
    return "(?i)(?:" + _bounded(phrases) + "|" + words + ")"


# The seat itself, not a job that works with one: "Student Nurse" and
# "Engineering Co-op" are excluded, "Student Health Services", "Student Loans",
# "Graduate Admissions", "Internship Coordinator" and "Co-op Preceptor" are not.
_FOR_THEM = (
    r"(?!\s+(?:preceptor|coordinator|manager|program|director|supervisor|advisor"
    r"|recruiter|liaison))"
)
_INTERN = (
    r"(?i)\b(?:intern|internship|apprentice(?:ship)?|co-?op)\b"
    + _FOR_THEM
    + r"|\bstudent\s+(?:worker|nurse|intern|assistant|employee|technician|tech|aide)\b"
)
# "graduate" is the seat when a title opens with it ("Graduate Software
# Engineer") or names a graduate scheme, not in "Graduate Admissions Counselor"
# or "Graduate Program Coordinator"
_GRAD_OFFICE = (
    r"(?!\s+(?:admissions?|studies|school|student|enrollment|recruit\w*|records|affairs"
    r"|division|college|program\s+(?:coordinator|manager|director|administrator"
    r"|specialist|assistant)))"
)
_JUNIOR = (
    r"(?i)\b(?:junior|jr\.?|entry[- ]level|trainee|(?:new|recent)\s+grad(?:uate)?)\b"
    r"|^\W*graduate\b"
    + _GRAD_OFFICE
    + r"|\bgraduate\s+(?:nurse|engineer|programme|scheme|trainee|analyst|developer"
    r"|consultant|associate)\b"
)
_ASSOCIATE = (
    r"(?i)\bassociate\b(?!\s+(?:director|vice president|vp|general counsel|partner"
    r"|principal|manager|creative director))"
)
# a level I / II title, unless it also says senior, staff, lead or principal
# ("Senior Software Engineer II" is a senior role at many companies)
_LEVEL_I_II = r"(?i)^(?!.*\b(?:senior|sr|staff|lead|principal)\b).*(?<![\w-])(?:i|ii|1|2)\s*$"
SENIORITY_EXCLUDES: dict[str, list[tuple[str, str]]] = {
    "any": [],
    "no_internships": [(_INTERN, "internships and apprenticeships")],
    "experienced": [
        (_INTERN, "internships and apprenticeships"),
        (_JUNIOR, "junior / entry-level"),
        (_ASSOCIATE, "associate-level (not Associate Director/VP/...)"),
    ],
    "senior": [
        (_INTERN, "internships and apprenticeships"),
        (_JUNIOR, "junior / entry-level"),
        (_ASSOCIATE, "associate-level (not Associate Director/VP/...)"),
        (_LEVEL_I_II, "level I / II titles"),
    ],
}
# A sales seat, by whole words in the track's name or titles. Deliberately
# narrow: "Revenue Cycle Specialist" and "Patient Account Manager" are not
# sales titles. Only a search where every track is a sales title gets the
# sales prompt set and the sales feed categories.
_SALES = re.compile(
    r"(?i)\b(?:sales|account executive|business development|sdr|bdr|ae|gtm"
    r"|go[\s-]to[\s-]market|chief revenue officer|revenue operations)\b"
)
# Sales outside B2B software: the sales prompt set's anchors (ACV, quota,
# MEDDIC, SaaS tools) would misjudge these, so a track naming one of these is
# not a sales track for the prompt set ("Retail Sales Associate", "Medical
# Device Sales Representative", "Insurance Sales Agent", "Solar Sales
# Consultant"). Mostly phrases, not bare words: "Associate Account Executive"
# and "Account Executive, Automotive" (software sold to car makers) are
# software sales, so "associate", "agent", "auto", "car", "store", "route",
# "insurance" and the like count only next to a word that makes them retail,
# vehicle, insurance or other non-software selling. "Mortgage" and
# "wholesale" too: "Account Executive, Mortgage Technology" sells software to
# lenders, while "Mortgage Loan Officer" and "Wholesale Account Executive"
# (wholesale mortgage lending) do not.
_NOT_SOFTWARE_SALES = re.compile(
    r"(?i)\b(?:"
    # retail floors and counters
    r"retail\s+(?:sales|associate|store|consultant)|(?:in[\s-])?store\s+(?:sales|associate)"
    r"|sales\s+(?:associate|agent|clerk|floor)|clerk|cashier|counter\s+sales|showroom"
    r"|merchandis\w*|route\s+sales"
    r"|wholesale\s+(?:sales|rep\w*|lending|mortgage|distribution|account\s+executive)"
    # insurance, real estate, lending
    r"|insurance\s+(?:sales|agent|agency|producer|broker)|real\s+estate\s+(?:sales|agent|broker)"
    r"|realtor|loan\s+(?:officer|originator|sales)"
    r"|mortgage\s+(?:sales|loan\w*|lending|lender|banker|broker|consultant|advisor"
    r"|officer|originator|specialist|account\s+executive)"
    # medical, device and pharmaceutical
    r"|medical\s+(?:device|equipment)\s+(?:sales|rep\w*)|medical\s+sales|device\s+sales"
    r"|surgical\s+sales|dental\s+sales|pharma(?:ceutical)?\s+(?:sales|rep\w*)"
    # vehicles
    r"|(?:auto|automotive|car|vehicle|truck|rv|boat)\s+(?:sales|dealer\w*)|dealership"
    # solar and door-to-door
    r"|solar\s+(?:sales|consultant|advisor|energy\s+(?:sales|consultant|advisor))"
    r"|door[\s-]to[\s-]door"
    # advertising and media
    r"|(?:advertising|ad|media|radio|television|tv|broadcast|print|newspaper|magazine"
    r"|outdoor|billboard)\s+sales"
    # staffing
    r"|(?:staffing|recruitment|recruiting)\s+(?:sales|agency|firm)"
    # telecom
    r"|(?:telecom|telecommunications|wireless|cellular|cable)\s+sales"
    r")\b"
)
_SALES_CATEGORIES = {"remotive": ["sales", "business"], "consider": ["sales"]}
_REMOTE_BOARDS = ("arbeitnow", "remotive", "remoteok", "himalayas")
LINKEDIN_KEYWORDS_MAX = 4
ADZUNA_DISTANCE_KM = 40
USAJOBS_RADIUS_MILES = 25
UNDERPRICED_FACTOR = 1.25


# Sales track names that mean one of the tracks the sales resume prompt
# (prompts/select_bullets.md) has an emphasis line for, and that the standing
# rules' founding-AE carve-out (rules.AE_TRACK) reads: written with that id.
_SALES_TRACK_ALIASES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)^\s*(aes?|account executives?)\s*$"), "account_executive"),
    (
        re.compile(
            r"(?i)^\s*((sdr|bdr)s?( ?/ ?(sdr|bdr)s?)?|sales development rep(resentative)?s?"
            r"|business development rep(resentative)?s?)\s*$"
        ),
        "sdr_bdr",
    ),
    (re.compile(r"(?i)^\s*(sdr|bdr)( ?/ ?(sdr|bdr))? managers?\s*$"), "sdr_bdr_manager"),
)
# the track ids prompts/select_bullets.md has an emphasis line for
SALES_EMPHASIS_TRACKS = ("sdr_bdr", "sdr_bdr_manager", "account_executive", "sales_exec")


def _track_id(name: str, used: set[str]) -> str:
    for pattern, alias in _SALES_TRACK_ALIASES:
        if pattern.match(name) and alias not in used:
            used.add(alias)
            return alias
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "track"
    tid, n = base, 2
    while tid in used:
        tid, n = f"{base}_{n}", n + 1
    used.add(tid)
    return tid


def _template_blocks(text: str) -> dict[str, str]:
    """Top-level key → its block in the template, with the comment lines
    directly above it (the file header excluded)."""
    lines = text.splitlines()
    keys = [
        (i, m.group(1)) for i, ln in enumerate(lines) if (m := re.match(r"^([A-Za-z_][\w-]*):", ln))
    ]
    out: dict[str, str] = {}
    for n, (i, key) in enumerate(keys):
        start = i
        while (
            n > 0
            and start > 0
            and (lines[start - 1].startswith("#") or not lines[start - 1].strip())
        ):
            start -= 1
        while start < i and not lines[start].strip():
            start += 1
        end = keys[n + 1][0] if n + 1 < len(keys) else len(lines)
        while end > i + 1 and (lines[end - 1].startswith("#") or not lines[end - 1].strip()):
            end -= 1
        out[key] = "\n".join(lines[start:end])
    return out


# ---------------------------------------------------------------------------
# generation
# ---------------------------------------------------------------------------


def _place_parts(place: str, default_state: str | None) -> tuple[str, str | None]:
    """(city, two-letter state or None). "Columbus, OH" carries its own
    state; a bare "Columbus" takes the location answer's state."""
    city, _, rest = place.partition(",")
    code = state_code(rest.strip()) if rest.strip() else None
    if code is None and default_state:
        code = state_code(default_state)
    return " ".join(city.split()), code


def _is_named_metro(bucket: str) -> bool:
    return not bucket.startswith(("other-", "remote"))


class _Places:
    """Where a non-remote job may be: the places named, plus the nearby towns
    kept (each with its own commute time). A nearby town that jobkit files
    under a different named metro ("Upper Arlington" is read as Arlington, VA)
    is kept with its state, so match_state keeps out that metro's postings
    that name another state; with no state to write, it is left out, because
    listing it would let in every job in that metro."""

    def __init__(self, loc: LocationAnswer):
        self.main = [p for p in loc.places if p.strip()]
        self.places = list(self.main)
        self.state = loc.state
        self.commute = dict(loc.commute_minutes)
        self.notes: list[str] = []
        self.left_out: list[tuple[str, str]] = []
        home = {location_bucket(p, False) for p in self.main}
        for place in self.main:
            bucket = location_bucket(place, False)
            metro = METRO_STATES.get(bucket)
            if metro is None:
                continue
            _, code = _place_parts(place, self.state)
            if code is None:
                self.notes.append(
                    f"{place!r} has no state, and jobkit files it under {bucket} by its "
                    f"spelling, so every job in {bucket} passes the location filter. Give "
                    "its state (locations.state) if it is not in that metro"
                )
            elif code not in metro:
                self.notes.append(
                    f"jobkit files {place!r} under {bucket} by its spelling. Its state ({code}) "
                    f"is written with it, so the location filter keeps only {bucket} jobs "
                    f"that name {code} (filters.locations.match_state)"
                )
        given_home_state: list[str] = []
        for town, minutes in loc.nearby.items():
            name = " ".join(town.split())
            if not name:
                continue
            bucket = location_bucket(name, False)
            _, code = _place_parts(name, self.state)
            if _is_named_metro(bucket) and bucket not in home:
                metro = METRO_STATES.get(bucket)
                if code is None or (metro is not None and code in metro):
                    self.left_out.append((name, bucket))
                    self.notes.append(
                        f"nearby town {name!r} was left out: jobkit files it under {bucket}, "
                        f"a different metro from {', '.join(self.main)}, and it has no state "
                        f"to tell them apart, so listing it would also let in jobs anywhere "
                        f"in {bucket}. Jobs that name {name} are rejected by the location "
                        "filter; give its state (locations.state, or the town as 'Town, ST') "
                        "to keep it"
                    )
                    continue
                self.notes.append(
                    f"jobkit files nearby town {name!r} under {bucket} by its spelling. Its "
                    f"state ({code}) is written with it, so the location filter keeps only "
                    f"{bucket} jobs that name {code} (filters.locations.match_state); a "
                    f"{bucket} posting that names no state at all also passes"
                )
            if "," not in name and code is not None:
                given_home_state.append(name)
            if name not in self.places:
                self.places.append(name)
            self.commute.setdefault(name, int(minutes))
        if given_home_state and self.state:
            home_code = state_code(self.state)
            self.notes.append(
                f"nearby towns written with your state ({home_code}): "
                f"{', '.join(given_home_state)}. A posting in one of them that names another "
                "state is rejected. If a town is across a state line, give it with its own "
                "state ('Town, ST') in nearby"
            )

    def written(self, place: str) -> str:
        """The place as filters.locations.allowed gets it: with its state
        ("Columbus, OH") when the place has none of its own and the location
        answer gives one, so a hybrid job limited to that state passes the
        remote check and match_state can tell Columbus, OH from Columbus, GA."""
        if "," in place or self.state is None:
            return place
        city, code = _place_parts(place, self.state)
        if code is None or state_code(city) is not None:
            return place
        return f"{city}, {code}"

    @property
    def allowed(self) -> list[str]:
        return [self.written(p) for p in self.places]

    @property
    def any_state(self) -> bool:
        return any(states_in(p) and not states_in(p.split(",")[0]) for p in self.allowed)


def _yearly(amount: float, comp: CompAnswer) -> int:
    if comp.unit == "hourly":
        return round(amount * float(comp.hours_per_week or 0) * 52)
    return round(amount)


def _hourly_note(amount: float, comp: CompAnswer, indent: str) -> list[str]:
    if comp.unit != "hourly":
        return []
    hours = float(comp.hours_per_week or 0)
    return [
        f"{indent}# hourly: {amount:g} an hour x {hours:g} hours a week x 52 weeks = "
        f"{_yearly(amount, comp)} a year"
    ]


class _Plan:
    """The resolved settings plus the notes for the human."""

    def __init__(self, prefs: Preferences, template: dict[str, Any]):
        self.prefs = prefs
        self.template = template
        self.notes: list[str] = []
        used: set[str] = set()
        self.tracks: list[tuple[TrackAnswer, str, str, list[str]]] = []
        for t in prefs.tracks:
            tid = t.id or _track_id(t.name, used)
            if t.id:
                if t.id in used:
                    raise SetupError(f"track id {t.id!r} is used twice")
                used.add(t.id)
            phrases = [p for p in (t.titles or [t.name]) if p.strip()]
            regex = t.title_regex or _track_regex(phrases, t.title_words)
            try:
                re.compile(regex)
            except re.error as e:
                raise SetupError(f"track {t.name!r}: title_regex does not compile: {e}") from e
            self.tracks.append((t, tid, regex, phrases))
            if t.pivot and t.min_score is None:
                self.notes.append(
                    f"track {tid} is a change of field with no min_score of its own, so the "
                    "global gate applies (gates.min_score_to_surface); 45-50 is usual for a "
                    "change of field, because years in the new title start at zero"
                )
        self.excludes: list[tuple[str, str]] = [
            (_phrase_regex([p]), f"you named '{p}'") for p in prefs.exclude_titles.items if p
        ]
        self.seniority = SENIORITY_EXCLUDES[prefs.seniority_floor.level]
        self.excludes += self.seniority
        self.includes = [_phrase_regex([p]) for p in prefs.include_titles.items if p.strip()]
        self.places = _Places(prefs.locations)
        self.notes += self.places.notes

    def _first_track(self, title: str) -> str | None:
        return next((i for _, i, r, _ in self.tracks if re.search(r, title)), None)

    def check_titles(self) -> None:
        """Every title the human named lands in a track (its own, unless an
        earlier one claims it first) and is not thrown away by an exclude;
        every should_match title matches the track and survives the excludes
        (a seniority floor that rejects one is named, so the person can drop
        it); no should_not_match title matches the track unless an exclude
        already rejects it."""
        problems: list[str] = []
        for t, tid, regex, phrases in self.tracks:
            for p in phrases:
                if not re.search(regex, p):
                    problems.append(f"track {tid}: its regex does not match its own title {p!r}")
                for pat, why in self.excludes:
                    if re.search(pat, p):
                        problems.append(
                            f"track {tid}: title {p!r} would be rejected by the exclude for {why}"
                        )
            for p in [*phrases, *t.should_match]:
                first = self._first_track(p)
                if first is not None and first != tid:
                    self.notes.append(
                        f"title {p!r} (track {tid}) is claimed first by track {first}: "
                        "first match wins, so list the more specific track first if that is wrong"
                    )
            for title in t.should_match:
                if not re.search(regex, title):
                    problems.append(
                        f"track {tid}: {title!r} should match, but the track's titles and title "
                        "words do not match it"
                    )
                for pat, why in self.excludes:
                    if not re.search(pat, title):
                        continue
                    if (pat, why) in self.seniority:
                        problems.append(
                            f"track {tid}: {title!r} should match, but the seniority floor "
                            f"({self.prefs.seniority_floor.level}) rejects it as {why}: lower "
                            "the floor, or drop this title"
                        )
                    else:
                        problems.append(
                            f"track {tid}: {title!r} should match, but the exclude for {why} "
                            "rejects it"
                        )
            for title in t.should_not_match:
                rejected = any(re.search(pat, title) for pat, _ in self.excludes)
                if re.search(regex, title) and not rejected:
                    problems.append(
                        f"track {tid}: {title!r} should not match, but it does: narrow the "
                        "track's titles or title words, or exclude it"
                    )
        if problems:
            raise SetupError("the answers contradict each other:\n- " + "\n- ".join(problems))

    def _phrases(self) -> list[str]:
        """Search phrases for the feeds and LinkedIn: every track's titles, then
        the titles the person said a track should match (seniority words and
        level numbers dropped, "Sr. Software Engineer III - Backend" searches
        "Software Engineer"), then the extra include titles. Duplicates, by
        case, are dropped; the callers cap the count."""
        phrases: list[str] = []
        for _, _, _, ps in self.tracks:
            phrases += ps
        for t, _, _, _ in self.tracks:
            phrases += [_search_phrase(x) for x in t.should_match]
        phrases += [p for p in self.prefs.include_titles.items if p.strip()]
        seen: dict[str, str] = {}
        for p in phrases:
            p = p.strip()
            if p and p.lower() not in seen:
                seen[p.lower()] = p
        return list(seen.values())

    @property
    def all_sales(self) -> bool:
        """Every track reads as a B2B software sales title (by its name or a
        title phrase), and none names retail, insurance, device or other
        sales outside software."""
        return bool(self.tracks) and all(
            any(_SALES.search(x) for x in [t.name, *ps])
            and not any(_NOT_SOFTWARE_SALES.search(x) for x in [t.name, *ps])
            for t, _, _, ps in self.tracks
        )

    def _place_names(self) -> list[tuple[str, str | None]]:
        state = self.prefs.locations.state
        return [_place_parts(p, state) for p in self.places.main]

    def _linkedin_locations(self) -> list[dict[str, str]]:
        loc = self.prefs.locations
        out: list[dict[str, str]] = []
        if loc.work_mode != "remote_only":
            # onsite_ok takes hybrid work too, so it searches every workplace
            workplace = "hybrid" if loc.work_mode == "hybrid" else "any"
            for city, code in self._place_names():
                place = f"{city}, {US_STATES[code]}" if code else city
                out.append({"location": place, "workplace": workplace})
        if loc.accept_remote:
            out.append({"location": "United States", "workplace": "remote"})
        return out

    def _adzuna_locations(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for city, code in self._place_names():
            if code is None:
                self.notes.append(
                    f"no state for {city!r}: Adzuna searches it as {city!r} alone, which can "
                    "match a town of that name in another state. Give the state "
                    "(locations.state) to narrow it"
                )
            out.append(
                {"where": f"{city}, {code}" if code else city, "distance_km": ADZUNA_DISTANCE_KM}
            )
        return out

    def _usajobs_locations(self) -> list[dict[str, Any]]:
        return [
            {
                "name": f"{city}, {US_STATES[code]}" if code else city,
                "radius_miles": USAJOBS_RADIUS_MILES,
            }
            for city, code in self._place_names()
        ]

    def feeds_block(self) -> list[str]:
        feeds = dict(self.template.get("feeds") or {})
        loc = self.prefs.locations
        phrases = self._phrases()
        keywords = phrases[:8]
        sales = self.all_sales
        local = loc.work_mode != "remote_only"
        lines = [
            "# ---- aggregator feeds -------------------------------------------------------",
            "# Discovery beyond your target companies. A feed row is never canonical: when",
            "# the same posting also arrives from a company's own ATS board, the ATS record",
            "# wins. Titles are pre-filtered with the tracks and include/exclude patterns.",
            "# Keyword searches use your track titles (one API call per phrase per run).",
            "feeds:",
        ]
        for name, raw in feeds.items():
            cfg = dict(raw or {})
            notes: list[str] = []
            if name == "adzuna":
                cfg["keywords"] = keywords
                cfg["enabled"] = bool(keywords)
                notes.append("your track titles")
                if local:
                    cfg["locations"] = self._adzuna_locations()
                    notes.append("your places")
                    if loc.accept_remote:
                        # remote work too: one search with no place as well
                        cfg["search_nationwide"] = True
                        notes.append("and nationwide, for remote jobs")
                    else:
                        cfg.pop("search_nationwide", None)
                else:
                    cfg.pop("locations", None)
                    cfg.pop("search_nationwide", None)
            if name == "usajobs":
                cfg["keywords"] = keywords
                cfg["remote_only"] = not local
                if local:
                    cfg["locations"] = self._usajobs_locations()
                else:
                    cfg.pop("locations", None)
                cfg["enabled"] = self.prefs.federal.wanted
                lines += _said(self.prefs.federal.said, "  ")
                notes.append(
                    "federal jobs: on" if self.prefs.federal.wanted else "federal jobs: off"
                )
            if name in _SALES_CATEGORIES:
                if sales:
                    cfg["categories"] = list(_SALES_CATEGORIES[name])
                    notes.append("every track is a sales title")
                else:
                    cfg["categories"] = []
                    notes.append("every category: the titles filter decides")
            if name in _REMOTE_BOARDS and not loc.accept_remote:
                cfg["enabled"] = False
                notes.append("remote-only board; you do not want remote work")
            if name == "getro":
                lines.append("  # getro: robots.txt disallows the search endpoint (hard rule 11)")
                cfg["enabled"] = False
            if name == "a16z":
                lines.append("  # a16z: off until its parser matches the board's new page format")
                cfg["enabled"] = False
            if name == "linkedin_guest":
                lines.append(
                    "  # linkedin_guest: LinkedIn logged-out search, discovery only; off until"
                )
                lines.append("  # you turn it on (CLAUDE.md hard rule 11, docs/why-no-linkedin.md)")
                cfg["enabled"] = False
                if phrases:
                    cfg["keywords"] = phrases[:LINKEDIN_KEYWORDS_MAX]
                cfg["locations"] = self._linkedin_locations()
                notes.append("your track titles and places")
            body = yaml.safe_dump(
                cfg, default_flow_style=True, width=10_000, sort_keys=False
            ).strip()
            comment = f"  # {'; '.join(notes)}" if notes else ""
            lines.append(f"  {name}: {body}{comment}")
        return lines

    def comp_sanity_block(self) -> list[str]:
        """One comp_sanity block: the template's range checks, plus the
        underpriced line at 1.25 x the lowest floor and the OTE flags only
        for OTE tracks."""
        d = CompSanity()
        tpl = self.template.get("comp_sanity") or {}
        if not isinstance(tpl, dict):
            tpl = {}
        comp = self.prefs.comp
        floors = [_yearly(t.comp_floor, comp) for t, _, _, _ in self.tracks if t.comp_floor]
        lowest = min(floors) if floors else _yearly(comp.default_floor or 0, comp)
        flag_missing = bool(
            tpl.get("flag_missing_range_when_required", d.flag_missing_range_when_required)
        )
        return [
            "# ---- comp sanity (sanity layer) -----------------------------------------------",
            "comp_sanity:",
            f"  max_range_width_pct: {tpl.get('max_range_width_pct', d.max_range_width_pct)}"
            '   # a wider published range is flagged "range-dodge"',
            f"  flag_missing_range_when_required: {'true' if flag_missing else 'false'}"
            "   # flag no range where pay-transparency law requires one",
            f"  # 1.25 x your lowest floor ({lowest:,}): with high demand in the JD, a published",
            '  # ceiling under this is flagged "underpriced"',
            f"  underpriced_below: {round(UNDERPRICED_FACTOR * lowest)}",
            f"  ote_unclear_from: {int(tpl.get('ote_unclear_from', d.ote_unclear_from))}"
            '   # a ceiling this high with no base/OTE basis is flagged "ote-basis-unclear"',
            "  ote_check: ote_tracks   # the OTE flags only for tracks whose floor basis is ote",
        ]


def render_filters(prefs: Preferences, template_text: str) -> tuple[str, list[str]]:
    """(filters.yaml text, notes for the human)."""
    template = yaml.safe_load(template_text) or {}
    blocks = _template_blocks(template_text)
    plan = _Plan(prefs, template)
    plan.check_titles()
    tf = template.get("filters") or {}
    loc = prefs.locations
    comp = prefs.comp
    # pivot and emphasis are read only by the general prompts (score_fit_general
    # has the career-change note, select_bullets_general the emphasis line), so
    # a sales search that sets either gets the general set
    steers = [t.name for t, _, _, _ in plan.tracks if t.pivot or t.emphasis]
    prompt_set = "sales" if plan.all_sales and not steers else "general"
    if plan.all_sales and steers:
        plan.notes.append(
            f"every track is a sales title, but {', '.join(steers)} is marked as a change "
            "of field or has an emphasis; only the general scoring and resume prompts read "
            "those, so prompt_set is general"
        )
    if prompt_set == "sales":
        plain = [tid for _, tid, _, _ in plan.tracks if tid not in SALES_EMPHASIS_TRACKS]
        if plain:
            plan.notes.append(
                f"track {', '.join(plain)} gets no sales emphasis when the resume is "
                "chosen: the sales resume prompt has lines only for "
                f"{', '.join(SALES_EMPHASIS_TRACKS)}. For a sales leadership role, give the "
                "track `id: sales_exec`"
            )
    out = [
        f"{GENERATED_MARK} — config/filters.yaml",
        "#",
        "# What jobkit looks for, and what it throws away before spending a token.",
        f"# Generated {today()} from your answers (data/setup/preferences.answers.yaml).",
        "# Every setting your answers decided quotes the answer that produced it: when a",
        "# filter rejects something surprising, that is the answer to revisit. Sections",
        "# with no quote are the defaults from config/filters.example.yaml, which",
        "# documents every key. Re-run the interview (run_intake) or edit by hand, then",
        "# `jobkit refilter` (MCP: refilter) to re-check open jobs.",
        "",
        "# The scoring prompts: general (field-neutral), or sales when every track is a",
        "# software sales title and none is a change of field or sets an emphasis.",
        f"prompt_set: {prompt_set}",
        "",
        "filters:",
        "  # ---- role tracks: first match wins ---------------------------------------",
        "  tracks:",
    ]
    for t, tid, regex, phrases in plan.tracks:
        out += _said(t.said, "    ")
        if not t.title_regex:
            out.append(f"    # titles: {', '.join(phrases)}")
            if t.title_words is not None:
                words = [w if isinstance(w, str) else " or ".join(w) for w in t.title_words.all]
                if words:
                    out.append(f"    # every title has: {'; '.join(words)}")
                if t.title_words.any:
                    out.append(f"    # and one of: {', '.join(t.title_words.any)}")
        if t.should_match:
            out.append(f"    # checked at setup, matches: {'; '.join(t.should_match)}")
        if t.should_not_match:
            out.append(f"    # checked at setup, does not match: {'; '.join(t.should_not_match)}")
        out += [
            f"    - id: {tid}",
            f"      active: {'true' if t.active else 'false'}",
            f"      title_regex: {_q(regex)}",
        ]
        if t.pivot:
            out.append("      pivot: true   # a change of field: scored as a career change")
        if t.min_score is not None:
            out.append(f"      min_score: {t.min_score}   # this track's queue gate")
        if t.emphasis:
            out.append(f"      emphasis: {_q(t.emphasis)}")
    out.append("  # roles outside every track (also count as a title match)")
    out += _said(prefs.include_titles.said, "  ")
    out.append(f"  include_titles: {_flow(plan.includes)}")
    out.append("  # any match rejects, even when a track matched")
    out += _said(prefs.exclude_titles.said, "  ")
    if prefs.seniority_floor.level != "any":
        out += _said(prefs.seniority_floor.said, "  ")
        out.append(f"  # seniority floor: {prefs.seniority_floor.level}")
    if plan.excludes:
        out.append("  exclude_titles:")
        for pat, why in plan.excludes:
            out.append(f"    - {_q(pat)}   # {why}")
    else:
        out.append("  exclude_titles: []")

    places = plan.places
    out.append("  # ---- locations ----------------------------------------------------------")
    out += _said(loc.said, "  ")
    nearby = [p for p in places.places if p not in places.main]
    if nearby:
        out.append(f"  # nearby towns you kept: {', '.join(nearby)}")
    for name, bucket in places.left_out:
        out.append(f"  # left out: {name} (jobkit files it under {bucket}, a different metro)")
    out += [
        "  locations:",
        f"    allowed: {_flow(places.allowed)}   # where a NON-remote posting may be",
        f"    accept_remote: {'true' if loc.accept_remote else 'false'}",
    ]
    if places.any_state:
        out += [
            "    # a posting in one of these towns must also be in its state: Columbus, OH",
            "    # keeps out Columbus, GA and Dublin, Ireland",
            "    match_state: true",
        ]

    out.append("  # ---- compensation -------------------------------------------------------")
    out.append("  # A posting is rejected only when it PUBLISHES a range whose ceiling is under")
    out.append("  # the floor; unpublished comp always passes to the next stage. Floors are")
    out.append("  # yearly.")
    out += _said(comp.said, "  ")
    out.append("  comp:")
    out.append("    # titles that match no track; also the line the answer bank's")
    out.append("    # salary-expectation figure is measured against (a track whose floor")
    out.append("    # is under it gets that field left blank and flagged)")
    out += _hourly_note(comp.default_floor or 0, comp, "    ")
    out.append(f"    floor: {_yearly(comp.default_floor or 0, comp)}")
    floors = [(t, tid) for t, tid, _, _ in plan.tracks if t.comp_floor]
    if floors:
        out.append("    track_floors:")
        for t, tid in floors:
            out += _hourly_note(t.comp_floor or 0, comp, "      ")
            out.append(
                f"      {tid}: {{ floor: {_yearly(t.comp_floor or 0, comp)}, "
                f"basis: {t.comp_basis} }}"
            )
    else:
        out.append("    track_floors: {}")
    if any(t.comp_basis == "ote" and t.comp_floor for t, _ in floors):
        share = comp.ote_base_share
        if share is None:
            share = float((tf.get("comp") or {}).get("ote_base_share", 0.6))
            out.append(f"    ote_base_share: {share}   # template default: base {share:.0%} of OTE")
        else:
            out.append(f"    ote_base_share: {share}")
    out.append(f"    currency: {comp.currency}")

    max_age = prefs.max_age_days or int((tf.get("freshness") or {}).get("max_age_days", 30))
    out += [
        "  freshness:",
        f"    max_age_days: {max_age}   # postings older than this are rejected",
    ]
    ind = prefs.industries
    if ind.wanted or ind.refused or ind.said:
        out.append("  # Industries: jobkit has no industry filter (postings carry no industry).")
        out.append("  # Recorded here and used to seed targets.yaml; refused companies you name")
        out.append("  # go in blocklist_companies.")
        out += _said(ind.said, "  ")
        if ind.wanted:
            out.append(f"  #   wanted: {', '.join(ind.wanted)}")
        if ind.refused:
            out.append(f"  #   refused: {', '.join(ind.refused)}")
    out += _said(prefs.blocklist_companies.said, "  ")
    out.append(
        f"  blocklist_companies: {_flow(prefs.blocklist_companies.items)}"
        "   # substrings of company names you will not work for"
    )
    out.append("")

    if "gates" in blocks:
        out += [blocks["gates"], ""]
    sched = dict(template.get("schedule") or {})
    out += [
        "# ---- schedule ----------------------------------------------------------------",
        '# When the nightly run happens (local time, a QUOTED "HH:MM"). `jobkit schedule',
        "# install` (macOS launchd) reads it; scheduled tasks in the Claude app keep",
        "# their own times.",
        "schedule:",
        f'  nightly_at: "{(prefs.nightly_at or sched.get("nightly_at", "02:00")).strip()}"',
        f"  stale_after_hours: {sched.get('stale_after_hours', 36)}",
        "",
    ]
    if "tracking" in blocks:
        out += [blocks["tracking"], ""]
    else:
        out += [
            "# ---- application tracking ---------------------------------------------------",
            "tracking:",
            "  # a submitted application with no recorded response is marked ghosted after",
            "  # this many days (a status change only; nothing is sent)",
            "  ghost_after_days: 30",
            "  # the scheduled daily run reads your email for employer replies (read-only)",
            "  email_check: true",
            "",
        ]

    forms = prefs.open_forms
    out += [
        "# ---- staging ------------------------------------------------------------------",
        *_said(forms.said, ""),
        "staging:",
        "  # true: the scheduled daily run ends by opening the filled application forms",
        "  # in a browser window for you to review and submit",
        f"  open_after_daily_run: {'true' if forms.after_daily_run else 'false'}",
        "",
    ]
    if prefs.alert_search is not None:
        out += alert_search_block(prefs.alert_search)

    rp = dict(template.get("remote_policy") or {})
    geo = (
        loc.acceptable_geo_restrictions
        if loc.acceptable_geo_restrictions is not None
        else list(rp.get("acceptable_geo_restrictions") or [])
    )
    home_state = state_code(loc.state) if loc.state else None
    if home_state and not plan.places.places and US_STATES[home_state] not in geo:
        # remote only: no place carries the state, so name it here, or a remote
        # job limited to residents of the person's own state is rejected
        geo = [*geo, US_STATES[home_state]]
    office_days = (
        0
        if loc.work_mode == "remote_only"
        else (5 if loc.max_office_days_per_week is None else loc.max_office_days_per_week)
    )
    commute = loc.max_commute_minutes or int(rp.get("max_commute_minutes", 60))
    out += [
        "# ---- remote reality check (sanity layer: an LLM reads the JD) ----------------",
        *_said(loc.said, ""),
        f"# work mode: {loc.work_mode}",
        "remote_policy:",
        f"  reject_onsite: {'false' if loc.work_mode == 'onsite_ok' else 'true'}",
        f"  max_office_days_per_week: {office_days}",
        f"  max_commute_minutes: {commute}   # hybrid over this (commute_table.yaml) is rejected",
        f"  relocation_cities: {_flow(loc.relocation_cities)}   # you would move there",
        "  # a posting restricted to a place passes when the restriction names one of",
        "  # these (your places and their US states are added automatically)",
        f"  acceptable_geo_restrictions: {_flow([str(g) for g in geo])}",
        "",
    ]
    for key in ("location_strategy", "resume", "self_review", "questions"):
        if key in blocks:
            out += [blocks[key], ""]
    out += plan.comp_sanity_block()
    out.append("")
    out += plan.feeds_block()
    out.append("")
    if "board_resolution" in blocks:
        out += [blocks["board_resolution"], ""]
    if "friction" in blocks:
        out += [blocks["friction"], ""]
    text = "\n".join(out).rstrip() + "\n"

    try:
        filters = Filters(raw=yaml.safe_load(text))
    except (yaml.YAMLError, ValueError) as e:
        raise SetupError(f"the generated filters.yaml would not load: {e}") from e
    return text, plan.notes + _comp_expectation_notes(plan, filters)


def alert_search_block(answer: AlertSearchAnswer) -> list[str]:
    """The `search` block of filters.yaml for the person's answer:
    alert_web_search always, max_searches_per_check only when they named a
    number (no number: no limit)."""
    lines = [
        "# ---- web search for alert jobs ----------------------------------------------",
        *_said(answer.said, ""),
        "search:",
        "  # true: a job from a job-alert email that is on no Greenhouse, Lever or Ashby",
        "  # board jobkit finds is searched for on the web by Claude (recent alerts only;",
        "  # it uses your Claude plan). A posting on Workday or a similar site is listed",
        "  # with its direct link for you to apply there.",
        f"  alert_web_search: {'true' if answer.enabled else 'false'}",
    ]
    if answer.per_check is not None:
        lines.append(f"  max_searches_per_check: {answer.per_check}   # leave out for no limit")
    return [*lines, ""]


def _comp_expectation_notes(plan: _Plan, filters: Filters) -> list[str]:
    """A note for each track whose jobs get the salary-expectation field left
    blank and flagged. Asked of the form-filling gate itself
    (comp_expectation_block), so the note and the form agree."""
    notes: list[str] = []
    for _t, tid, _regex, phrases in plan.tracks:
        title = next((p for p in phrases if filters.track_for(p) == tid), None)
        if title is None or comp_expectation_block(title, None, {}, filters) is None:
            continue
        floor = filters.comp_floor_for(title).floor
        notes.append(
            f"track {tid}: its floor ({floor:,}) is under your default floor "
            f"({filters.comp_floor:,}), so salary-expectation fields on its jobs are left "
            "blank and flagged for you: the answer bank holds one figure, measured against "
            "the default floor"
        )
    return notes


def render_commute(prefs: Preferences) -> tuple[str, list[str]]:
    loc = prefs.locations
    places = _Places(loc)
    notes: list[str] = []
    lines = [
        f"{GENERATED_MARK} — config/commute_table.yaml",
        "#",
        "# Door-to-door minutes from home to each place a hybrid job could be. When the",
        "# remote check finds a job is really HYBRID, its location bucket is looked up",
        "# here: over remote_policy.max_commute_minutes it is rejected; with no entry it",
        "# is rejected as an unknown commute (relocation cities excepted). Keys are",
        "# location buckets (config/commute_table.example.yaml lists them).",
        "",
        'version: "1.0"',
        "",
        "buckets:",
        "  remote: 0",
    ]
    lines += _said(loc.said, "  ")
    given = {k.strip().lower(): v for k, v in places.commute.items()}
    seen: set[str] = set()
    for place in places.places:
        bucket = location_bucket(place, False)
        if bucket.startswith("remote-") or bucket == "other-unknown" or bucket in seen:
            continue
        minutes = given.get(place.strip().lower())
        if minutes is None:
            notes.append(
                f"no commute time for {place} ({bucket}): a hybrid posting there is rejected "
                "as an unknown commute until you add one"
            )
            continue
        seen.add(bucket)
        lines.append(f"  {bucket}: {int(minutes)}   # {place}")
    return "\n".join(lines) + "\n", notes


AUTOAPPROVE_TEXT = f"""{GENERATED_MARK} — config/autoapprove.yaml
#
# Standing rules: let the queue pre-decide the obvious cases (SPEC.md §6.2).
# You start with NONE: nothing is approved or rejected without you until you
# accept a rule, one at a time. Rules only see jobs that passed the filters
# and the sanity checks; never duplicate a sanity control here (remote
# conflict, commute, underpriced). The conditions a rule can use are
# documented in config/autoapprove.example.yaml.

version: "1.0"

auto_approve: []

auto_reject: []
"""

# the reason on the one rule the interview writes; a file whose only rules
# carry it is the interview's own and may be rewritten
INTERVIEW_RULE_REASON = "clears-your-bar"


def interview_rule(min_score: int) -> dict[str, Any]:
    """The automatic-approval rule the interview writes on a yes."""
    return {
        "when": {"score_gte": min_score, "sanity_flags": "none", "sanity_checked": True},
        "reason": INTERVIEW_RULE_REASON,
    }


def render_autoapprove(answer: AutoApproveAnswer | None) -> str:
    """config/autoapprove.yaml for the person's answer: no rule for no (or no
    answer), the clears-your-bar rule for yes, and max_auto_approve_per_day
    only when they gave a number."""
    if answer is None or not answer.enabled:
        head = AUTOAPPROVE_TEXT
        if answer is not None:
            said = _said(answer.said, "")
            if said:
                head = head.replace('version: "1.0"', "\n".join([*said, 'version: "1.0"']), 1)
        return head
    lines = [
        f"{GENERATED_MARK} — config/autoapprove.yaml",
        "#",
        "# Standing rules: let the queue pre-decide the obvious cases (SPEC.md §6.2).",
        "# Rules only see jobs that passed the filters and the sanity checks. The",
        "# conditions a rule can use are documented in config/autoapprove.example.yaml.",
        "",
        'version: "1.0"',
        "",
        *_said(answer.said, ""),
        f"# Automatic approval: every job with a fit score of {answer.min_score} or more and",
        "# no sanity flag, once both sanity checks have run, is approved without asking,",
        "# tailored, and its form filled for you to review. Nothing is submitted: you",
        "# click submit.",
        "auto_approve:",
        f"  - when: {{ score_gte: {answer.min_score}, sanity_flags: none, sanity_checked: true }}",
        f'    reason: "{INTERVIEW_RULE_REASON}"',
        "",
        "auto_reject: []",
    ]
    if answer.per_day is not None:
        lines += [
            "",
            f"# at most {answer.per_day} approved automatically per day, highest fit first;",
            "# the rest wait in your queue for the next day's run",
            f"max_auto_approve_per_day: {answer.per_day}",
        ]
    return "\n".join(lines) + "\n"


def _interview_owned_rules(path: Path) -> bool:
    """True when autoapprove.yaml is the interview's own output: its marker
    line, no auto_reject rule, and no auto_approve rule but the interview's
    clears-your-bar rule. Rules anyone added are never rewritten silently."""
    if not is_generated(path):
        return False
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False
    if not isinstance(data, dict) or data.get("auto_reject"):
        return False
    approve = data.get("auto_approve") or []
    return isinstance(approve, list) and all(
        isinstance(r, dict) and r.get("reason") == INTERVIEW_RULE_REASON for r in approve
    )


def _has_interview_rule(path: Path) -> bool:
    """autoapprove.yaml holds the interview's clears-your-bar rule."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False
    approve = data.get("auto_approve") if isinstance(data, dict) else None
    return isinstance(approve, list) and any(
        isinstance(r, dict) and r.get("reason") == INTERVIEW_RULE_REASON for r in approve
    )


def _banned_phrases_text(files: SetupFiles, style: StyleAnswer | None) -> str:
    text = template_path(files, "config/banned_phrases.example.yaml").read_text(encoding="utf-8")
    head = [
        f"{GENERATED_MARK} — config/banned_phrases.yaml",
        "# Copied from config/banned_phrases.example.yaml: a good start for anyone.",
        "# Grow the lists as you notice your own drafts drifting.",
    ]
    if style is not None:
        text, n = re.subn(
            r"(?m)^(\s*estimate_style:\s*)\w+", rf"\g<1>{style.estimate_style}", text, count=1
        )
        if n:
            head += _said(style.said, "")
            head.append(f"# drift.estimate_style set to {style.estimate_style} from your answer.")
    return "\n".join(head) + "\n#\n" + text


def run_intake(
    answers: dict[str, Any], *, replace_existing: bool = False, root: Path | None = None
) -> str:
    """Validate the interview answers and write the config files."""
    files = setup_files(root)
    try:
        prefs = Preferences.model_validate(answers)
    except ValidationError as e:
        raise SetupError(f"answers incomplete or invalid:\n{e}") from e
    template_text = template_path(files, "config/filters.example.yaml").read_text(encoding="utf-8")
    filters_text, notes = render_filters(prefs, template_text)
    commute_text, commute_notes = render_commute(prefs)
    notes += commute_notes

    filters_path = files.config_file("filters.yaml")
    commute_path = files.config_file("commute_table.yaml")
    guard_overwrite(files, filters_path, replace_existing, "your existing filters")
    guard_overwrite(files, commute_path, replace_existing, "your existing commute table")

    written: list[str] = []
    kept: list[str] = []
    record = {"answers": prefs.model_dump(exclude_defaults=False), "set_on": today()}
    write_text_atomic(
        files.preferences_record,
        f"{GENERATED_MARK} — your intake interview answers.\n" + dump_yaml(record),
    )
    for path, text in ((filters_path, filters_text), (commute_path, commute_text)):
        prev = backup(files, path)
        write_text_atomic(path, text)
        written.append(files.rel(path) + (f" (previous kept at {files.rel(prev)})" if prev else ""))
    auto = files.config_file("autoapprove.yaml")
    choice = prefs.auto_approve
    if choice is None:
        # no answer: as before, an empty rule file when there is none
        if not auto.is_file():
            write_text_atomic(auto, AUTOAPPROVE_TEXT)
            written.append(files.rel(auto) + " (no standing rules yet)")
        else:
            kept.append(files.rel(auto))
    else:
        what = (
            f"automatic approval at a fit score of {choice.min_score} or more, "
            + (f"at most {choice.per_day} a day" if choice.per_day else "no daily limit")
            if choice.enabled
            else "no automatic approval"
        )
        # replace_existing never reaches here: rules someone added (approve or
        # reject) are theirs, and a rerun of setup keeps every one of them
        if not auto.is_file() or _interview_owned_rules(auto):
            auto_text = render_autoapprove(choice)
            try:
                max_auto_approve_per_day(yaml.safe_load(auto_text))
            except ValueError as e:  # pragma: no cover — the answer model checks first
                raise SetupError(f"the generated autoapprove.yaml would not load: {e}") from e
            prev = backup(files, auto)
            write_text_atomic(auto, auto_text)
            written.append(
                files.rel(auto)
                + f" ({what})"
                + (f" (previous kept at {files.rel(prev)})" if prev else "")
            )
        else:
            kept.append(files.rel(auto))
            if choice.enabled:
                rule = interview_rule(choice.min_score)
                notes.append(
                    f"{files.rel(auto)} already holds standing rules, so it was kept as it "
                    f"is and {what} was not written. To add it, put this rule under "
                    f"auto_approve: {{when: {{score_gte: {rule['when']['score_gte']}, "
                    "sanity_flags: none, sanity_checked: true}, "
                    f'reason: "{INTERVIEW_RULE_REASON}"}}'
                    + (
                        f", and the line max_auto_approve_per_day: {choice.per_day}"
                        if choice.per_day
                        else ""
                    )
                )
            elif _has_interview_rule(auto):
                notes.append(
                    f"{files.rel(auto)} already holds standing rules, so it was kept as it "
                    f'is. To stop automatic approval, delete the rule with reason "'
                    f'{INTERVIEW_RULE_REASON}" under auto_approve; your other rules stay'
                )
    banned = files.config_file("banned_phrases.yaml")
    if not banned.is_file() or (is_generated(banned) and prefs.writing_style is not None):
        write_text_atomic(banned, _banned_phrases_text(files, prefs.writing_style))
        written.append(files.rel(banned))
    else:
        kept.append(files.rel(banned))
        if prefs.writing_style is not None:
            notes.append(
                f"{files.rel(banned)} was kept as it is: set drift.estimate_style to "
                f"{prefs.writing_style.estimate_style} there by hand"
            )
    if prefs.industries.wanted:
        notes.append(
            "industries you want ("
            + ", ".join(prefs.industries.wanted)
            + ") have no filter: name companies in them for seed_targets"
        )
    lines = ["wrote: " + "; ".join(written)]
    if kept:
        lines.append("kept as they are: " + ", ".join(kept))
    lines += [f"note: {n}" for n in notes]
    lines.append("Next: seed_targets for the companies to watch.")
    return "\n".join(lines)
