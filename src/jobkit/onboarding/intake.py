"""Phase 2.1-2.6 — intake interview answers → config (ONBOARDING.md). MCP:
run_intake.

Claude asks the questions (against the corpus: ONBOARDING.md says why); this
module turns the human's answers into config/filters.yaml and
config/commute_table.yaml, and writes config/autoapprove.yaml (no rules: each
standing rule is accepted one at a time, later) and
config/banned_phrases.yaml (the template's lists) when those are missing.

Every interview-driven setting in filters.yaml carries a comment quoting the
answer that produced it, so when a filter rejects something surprising the
comment says which answer to revisit. Sections the interview does not cover
(gates, follow-ups, tailoring, comp sanity, friction) are copied from
config/filters.example.yaml with its documentation.

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
from jobkit.config import Filters
from jobkit.normalize import location_bucket
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

# ---------------------------------------------------------------------------
# the answers
# ---------------------------------------------------------------------------


class _Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    said: str = ""  # the human's own words; quoted in the config comment


class TrackAnswer(_Answer):
    name: str
    id: str | None = None
    titles: list[str] = Field(default_factory=list)  # title phrases; default [name]
    title_regex: str | None = None  # expert override of the phrases
    comp_floor: int | None = None
    comp_basis: Literal["base", "ote"] = "base"
    active: bool = True


class ListAnswer(_Answer):
    items: list[str] = Field(default_factory=list)


class SeniorityAnswer(_Answer):
    level: Literal["any", "no_internships", "experienced", "senior"] = "any"


class LocationAnswer(_Answer):
    work_mode: Literal["remote_only", "hybrid", "onsite_ok"]
    accept_remote: bool = True
    places: list[str] = Field(default_factory=list)  # where a non-remote posting may be
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
    default_floor: int | None = None
    currency: str = "USD"
    ote_base_share: float | None = None


class IndustryAnswer(_Answer):
    wanted: list[str] = Field(default_factory=list)
    refused: list[str] = Field(default_factory=list)


class StyleAnswer(_Answer):
    estimate_style: Literal["hedged", "confident"]


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
    max_age_days: int | None = None
    nightly_at: str | None = None
    writing_style: StyleAnswer | None = None

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


def _phrase_regex(phrases: list[str]) -> str:
    """(?i) whole-phrase alternation; spaces match any whitespace."""
    alts = [r"\s+".join(re.escape(w) for w in p.split()) for p in phrases if p.strip()]
    return r"(?i)(?<!\w)(?:" + "|".join(alts) + r")(?!\w)"


_INTERN = r"(?i)\b(?:intern|internship|apprentice(?:ship)?|co-?op|student)\b"
_JUNIOR = r"(?i)\b(?:junior|jr\.?|entry[- ]level|graduate|new grad|trainee)\b"
_ASSOCIATE = (
    r"(?i)\bassociate\b(?!\s+(?:director|vice president|vp|general counsel|partner"
    r"|principal|manager|creative director))"
)
_LEVEL_I_II = r"(?i)(?<![\w-])(?:i|ii|1|2)\s*$"
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
_SALES = re.compile(
    r"(?i)\b(?:sales|account executive|account manager|business development|sdr|bdr|revenue"
    r"|ae|gtm|go.to.market)\b"
)


def _track_id(name: str, used: set[str]) -> str:
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
            regex = t.title_regex or _phrase_regex(phrases)
            try:
                re.compile(regex)
            except re.error as e:
                raise SetupError(f"track {t.name!r}: title_regex does not compile: {e}") from e
            self.tracks.append((t, tid, regex, phrases))
        self.excludes: list[tuple[str, str]] = [
            (_phrase_regex([p]), f"you named '{p}'") for p in prefs.exclude_titles.items if p
        ]
        self.excludes += SENIORITY_EXCLUDES[prefs.seniority_floor.level]
        self.includes = [_phrase_regex([p]) for p in prefs.include_titles.items if p.strip()]

    def check_titles(self) -> None:
        """Every title the human named lands in a track (its own, unless an
        earlier one claims it first) and is not thrown away by an exclude."""
        problems: list[str] = []
        for _t, tid, regex, phrases in self.tracks:
            for p in phrases:
                if not re.search(regex, p):
                    problems.append(f"track {tid}: its regex does not match its own title {p!r}")
                for pat, why in self.excludes:
                    if re.search(pat, p):
                        problems.append(
                            f"track {tid}: title {p!r} would be rejected by the exclude for {why}"
                        )
                first = next((i for _, i, r, _ in self.tracks if re.search(r, p)), tid)
                if first != tid:
                    self.notes.append(
                        f"title {p!r} (track {tid}) is claimed first by track {first}: "
                        "first match wins, so list the more specific track first if that is wrong"
                    )
        if problems:
            raise SetupError("the answers contradict each other:\n- " + "\n- ".join(problems))

    def feeds_block(self) -> list[str]:
        feeds = dict(self.template.get("feeds") or {})
        phrases: list[str] = []
        for _, _, _, ps in self.tracks:
            phrases += ps
        phrases += [p for p in self.prefs.include_titles.items if p.strip()]
        keywords = list(dict.fromkeys(p.strip() for p in phrases))[:8]
        sales = any(_SALES.search(p) for p in phrases + [t.name for t, _, _, _ in self.tracks])
        lines = [
            "# ---- aggregator feeds -------------------------------------------------------",
            "# Discovery beyond your target companies. A feed row is never canonical: when",
            "# the same posting also arrives from a company's own ATS board, the ATS record",
            "# wins. Titles are pre-filtered with the tracks and include/exclude patterns.",
            "# Keyword searches use your track titles (one API call per phrase per run).",
            "feeds:",
        ]
        for name, cfg in feeds.items():
            cfg = dict(cfg or {})
            comment = ""
            if name in ("adzuna", "usajobs") and "keywords" in cfg:
                cfg["keywords"] = keywords
                comment = "  # your track titles"
            if name == "remotive" and not sales:
                cfg["categories"] = []
                comment = "  # every category: the titles filter decides"
            if name == "consider":
                if sales:
                    cfg["categories"] = ["sales"]
                else:
                    cfg["categories"] = []
                    comment = "  # all job types (a category here searches sales roles only)"
            if name == "getro":
                lines.append("  # getro: robots.txt disallows the search endpoint (hard rule 11)")
                cfg["enabled"] = False
            if name == "a16z":
                lines.append("  # a16z: experimental parser, currently broken")
                cfg["enabled"] = False
            if name == "linkedin_guest":
                lines.append(
                    "  # linkedin_guest: LinkedIn logged-out search, discovery only; off until"
                )
                lines.append("  # you turn it on (CLAUDE.md hard rule 11, docs/why-no-linkedin.md)")
                cfg["enabled"] = False
            body = yaml.safe_dump(
                cfg, default_flow_style=True, width=10_000, sort_keys=False
            ).strip()
            lines.append(f"  {name}: {body}{comment}")
        return lines


def render_filters(prefs: Preferences, template_text: str) -> tuple[str, list[str]]:
    """(filters.yaml text, notes for the human)."""
    template = yaml.safe_load(template_text) or {}
    blocks = _template_blocks(template_text)
    plan = _Plan(prefs, template)
    plan.check_titles()
    tf = template.get("filters") or {}
    loc = prefs.locations
    comp = prefs.comp
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
        "filters:",
        "  # ---- role tracks: first match wins ---------------------------------------",
        "  tracks:",
    ]
    for t, tid, regex, phrases in plan.tracks:
        out += _said(t.said, "    ")
        if not t.title_regex:
            out.append(f"    # titles: {', '.join(phrases)}")
        out += [
            f"    - id: {tid}",
            f"      active: {'true' if t.active else 'false'}",
            f"      title_regex: {_q(regex)}",
        ]
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

    out.append("  # ---- locations ----------------------------------------------------------")
    out += _said(loc.said, "  ")
    out += [
        "  locations:",
        f"    allowed: {_flow(loc.places)}   # where a NON-remote posting may be",
        f"    accept_remote: {'true' if loc.accept_remote else 'false'}",
    ]

    out.append("  # ---- compensation -------------------------------------------------------")
    out.append("  # A posting is rejected only when it PUBLISHES a range whose ceiling is under")
    out.append("  # the floor; unpublished comp always passes to the next stage.")
    out += _said(comp.said, "  ")
    out.append("  comp:")
    out.append("    # titles that match no track; also the line the answer bank's")
    out.append("    # salary-expectation figure is measured against (a track whose floor")
    out.append("    # is under it gets that field left blank and flagged)")
    out.append(f"    floor: {comp.default_floor}")
    floors = [(t, tid) for t, tid, _, _ in plan.tracks if t.comp_floor]
    if floors:
        out.append("    track_floors:")
        for t, tid in floors:
            out.append(f"      {tid}: {{ floor: {t.comp_floor}, basis: {t.comp_basis} }}")
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

    for key in ("gates",):
        if key in blocks:
            out += [blocks[key], ""]
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
    if "followups" in blocks:
        out += [blocks["followups"], ""]

    rp = dict(template.get("remote_policy") or {})
    geo = (
        loc.acceptable_geo_restrictions
        if loc.acceptable_geo_restrictions is not None
        else list(rp.get("acceptable_geo_restrictions") or [])
    )
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
    for key in ("location_strategy", "resume", "self_review", "questions", "comp_sanity"):
        if key in blocks:
            out += [blocks[key], ""]
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
    given = {k.strip().lower(): v for k, v in loc.commute_minutes.items()}
    seen: set[str] = set()
    for place in loc.places:
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
    if not auto.is_file():
        write_text_atomic(auto, AUTOAPPROVE_TEXT)
        written.append(files.rel(auto) + " (no standing rules yet)")
    else:
        kept.append(files.rel(auto))
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
