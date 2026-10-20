"""Provenance validator (SPEC §7). Every rendered bullet maps to a bullet_id;
every number appears in that bullet's own text/metric WITH ITS UNIT; every
proper noun appears somewhere in the corpus; never_claim constraints hold;
estimate-confidence metrics stay hedged next to the number. Violation → hard
fail, no PDF. NEVER relax this check.

Audit 2026-10-08 §2 item 3 closed these holes (each one let an embellished
claim through as ok=True):
- numbers were compared as bare digits, so 20% matched ~$20K and #1 matched $75K+;
- a hedge anywhere in the bullet ("40%, over target") counted as hedging the number;
- number words ("team of twelve"), rank claims ("#1 rep") and multipliers
  ("doubling") were never treated as numeric claims;
- 2-letter titles (VP) were never examined, and never_claim_titles matched by
  substring, so "CRO" fired inside "across";
- "/" stayed inside proper-noun tokens, so b_066's own short text failed.

The review of that fix (wp2) closed three more:
- "my"/"our" before a never-claim title read as someone else's title ("In my
  General Manager seat"), and seat-taking verbs ("took", "filled", "covered")
  were not claim verbs;
- plural and approximate quantities ("millions", "hundreds of deals", "tens of
  millions", "a decade", "a year and a half", "half") were not numeric claims;
- a verified bullet could drop its source's hedge ("~$5.6M" → "$5.6M"); only
  estimate bullets were checked. Now any number the source only ever states
  hedged must stay hedged (dropped_hedge), as in letters.

The second review of wp2 found the never-claim title check weaker than the
pre-audit substring match, which rejected every rendered occurrence. Any title
after a determiner read as someone else's unless a listed claim verb sat right
before it, so "Owned the General Manager role", "effectively the General
Manager" and "the org's General Manager for ~18 months" passed. A mention is
now the candidate's own by default. It is someone else's only on an explicit
person signal ("the general manager's office", "our VP of Sales asked").
In a resume bullet, a sourced title must also keep the source's own words
around it. Naming the job being applied for no longer excuses a claim on it
("At Haulmark I was the VP of Sales" fails for a VP of Sales job).

The numeric-claim parser and the never-claim helpers here are shared with
validate/claims.py, which applies the same rules to cover letters and drafted
form answers.

How a figure's approximation mark is checked depends on one knob,
config/banned_phrases.yaml `drift.estimate_style` (validate/style.py, 2026-10-08):
- "hedged" — the rules above: an estimate keeps a hedge next to every number
  (unhedged_estimate), and a number its source only states hedged stays hedged
  (dropped_hedge);
- "confident" — "~"/"about"/"approximately"/"roughly"/"around" may go, but the
  figure must otherwise be its source's: a range stays that range
  (collapsed_range, changed_range) and "+", "up to", "under", "over", "or
  more", "-plus", "and up" stay where the source has them and are never added,
  before or after the number (dropped_qualifier, added_qualifier). A
  comparative verb or phrase before the number is a bound too ("exceeding
  $8M", "topping $8M", "beyond $8M", "crossing the $75K mark", "as much as
  $1.8M", "capped at 9"), and so is any "<word>er than" (review of
  render-confident, 2026-10-08). A peak is a bound of its own, the same
  before or after the number: "peaked at $110K" states "~$110K at peak" and
  "(~$110K peak)", and neither "up to $110K" nor a bare "$110K" does (review
  2026-10-10).
In both, the value and unit must be in the source (fabricated_number), so an
inflated value or added precision ("$8.3M" from "$8M") always fails, and a
rate keeps its source's period (review of render-confident): "$1.8M/month" for
"$1.8M/yr" is changed_period, "$22K/month" for "$22K" is added_period, "5–7 deals"
for "5–7 deals/month" is dropped_period. A figure's period is the one written
next to it ("/day", "/mo", "a year", "per month", "annually", "run-rate",
"ARR", "YoY"), hyphenated onto the figure included ("$22K-a-month",
"$1.8M-per-year": 2026-10-15); a figure with none may also take one the
rendered text states elsewhere, never one it does not state."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from jobkit.validate import style as estimate_styles

_NUM = re.compile(r"\d[\d,.]*")  # legacy bare-digit scan, kept as an extra net
# Capitalized tokens of 2+ characters. "/" ends a token (b_066's
# "network/backup/VoIP" yields "VoIP"), so does any other separator.
_PROPER = re.compile(r"(?<![\w'’])[A-Z][A-Za-z0-9+&.'’-]*[A-Za-z0-9+]")

# Hedges, counted only when they sit directly before the number ("~$20K",
# "up to 9") or directly after it ("10+", "$75K+"). Same vocabulary as before
# the audit — only the adjacency requirement is new, so nothing that failed
# before can pass now.
_HEDGE_BEFORE = re.compile(r"(?:~|\b(?:roughly|about|approximately|up to|under|over))\s*$", re.I)
_HEDGE_AFTER = re.compile(r"^\s?\+")
# estimate_style "confident": the qualifiers right before a number, in two
# kinds. An approximation mark may be dropped or added; a bound ("up to",
# "under", "over", "nearly", "+" after the number) is part of the figure and
# must match the source. Matched leftmost-first, so "no more than" is a "max"
# bound, not "more than", "just under" is "near", not "under", and "not
# exceeding" is "max", not the verb "exceeding". The lexicon is read on both
# sides, so a corpus bullet rendered as written always parses the same.
_APPROX_BEFORE = re.compile(
    r"(?:~|≈|\b(?:roughly|about|approximately|approx\.?|around|some))\s*$", re.I
)
_BOUND_WORDS: dict[str, tuple[str, ...]] = {
    "max": (
        "up to",
        "no more than",
        "not more than",
        "at most",
        "under",
        "less than",
        "fewer than",
        "below",
        # review of render-confident: the bound words an LLM reaches for
        "as much as",
        "as many as",
        "as high as",
        "as large as",
        "as big as",
        "capped at",
        "a maximum of",
        "maximum of",
        "a max of",
        "max of",
        "not exceeding",
        "not to exceed",
        "not over",
        "south of",
        "topping out at",
        "topped out at",
        "maxing out at",
        "maxed out at",
    ),
    # a peak is its own bound (review 2026-10-10): "peaked at $110K" states a
    # source's "~$110K at peak" / "(~$110K peak)" and was rejected as an added
    # "max" bound. "Up to $110K" or "under $110K" is not a peak, so a peak
    # matches only a peak, before or after the number (_BOUND_AFTER_WORDS)
    "peak": (
        "peaked at",
        "peaking at",
        "peaks at",
        "a peak of",
        "peak of",
        "at peak",
        "at its peak",
        "at the peak",
    ),
    "min": (
        "over",
        "more than",
        "at least",
        "no less than",
        "not less than",
        "no fewer than",
        "upwards of",
        "upward of",
        "north of",
        "above",
        "in excess of",
        "just over",
        "beyond",
        "past",  # not "the past 3 years": see _NOT_A_BOUND
        "a minimum of",
        "minimum of",
        "at minimum",
        "at a minimum",
        "as little as",
        "as few as",
        "as low as",
    ),
    "near": (
        "nearly",
        "almost",
        "close to",
        "near",
        "just under",
        "just shy of",
        "shy of",
        "just short of",
        "short of",
        "nearing",
        "approaching",
    ),
}
_BOUND_CLASS = {w: cls for cls, words in _BOUND_WORDS.items() for w in words}
# Verbs that put the figure below what is claimed ("pipeline exceeding $8M",
# "topped $8M", "crossing the $75K mark", "beat an $8M target"): a "min" bound,
# with or without a determiner between the verb and the figure. Only inflected
# forms of "top", "best", "better" — "top 10%" is a rank, not a bound.
_BOUND_VERBS = (
    r"exceed(?:s|ed|ing)?|surpass(?:es|ed|ing)?|outstrip(?:s|ped|ping)?"
    r"|outpac(?:e|es|ed|ing)|outperform(?:s|ed|ing)?|eclips(?:e|es|ed|ing)"
    r"|tops|topp(?:ed|ing)|beat(?:s|ing)?|best(?:ed|ing)|better(?:ed|ing)"
    r"|pass(?:es|ed|ing)|cross(?:es|ed|ing)|clear(?:s|ed|ing)|broke|break(?:s|ing)"
)
_DETERMINER = r"(?:a|an|the|my|our|its|their|his|her|this|that)"
# Fail closed on comparatives: any "<word>er than" right before a figure is a
# bound ("greater than", "higher than", "better than", "faster than"), and
# "no"/"not" before it flips it. The direction is known for the words below;
# any other comparative is its own class, "cmp", so it matches no source
# bound and is never dropped silently. A blanket "any word before a figure"
# rule is not possible: real corpora say "generating $75K", "spanning 4",
# "adding 5" — plain verbs right before a figure.
_CMP_CLASS = {
    "greater": "min",
    "higher": "min",
    "larger": "min",
    "bigger": "min",
    "better": "min",
    "longer": "min",
    "lower": "max",
    "smaller": "max",
    "fewer": "max",
    "shorter": "max",
}
_NOT_COMPARATIVE = {"other", "rather"}
_FLIP = {"min": "max", "max": "min"}
_BOUND_BEFORE = re.compile(
    r"(?:(?P<sym><|≤|≥|(?<![-=])>)|\b(?P<word>"
    + "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in _BOUND_CLASS)
    + r")|\b(?P<verb>"
    + _BOUND_VERBS
    + r")(?:\s+"
    + _DETERMINER
    + r")?|\b(?:(?P<neg>no|not)\s+)?(?P<cmp>[a-z]+er)\s+than)\s*$",
    re.I,
)
_SYM_CLASS = {"<": "max", "≤": "max", ">": "min", "≥": "min"}
# "past" is a time phrase, not a bound, after a determiner ("in the past 3
# years", "my past 2 roles") or before a unit of time ("past 12 months").
_DETERMINER_BEFORE = re.compile(rf"\b{_DETERMINER}\s+$", re.I)
_TIME_UNIT_AFTER = re.compile(
    r"[-\s]*(?:years?|months?|quarters?|weeks?|days?|hours?|decades?|yrs?|mos?)\b", re.I
)


def _phrase_alt(words: Iterable[str]) -> str:
    return "|".join(
        re.escape(w).replace(r"\ ", r"[^\S\n]+") for w in sorted(words, key=len, reverse=True)
    )


# Bounds written AFTER the number (review of render-confident): "$8M or more",
# "$8M-plus", "$8M and up", "9 AEs or fewer", "$8M, tops." Up to three words
# may sit between the number and the phrase ("9 AEs or more"). "plus" before
# another figure or an article is a conjunction ("$75K plus 3 renewals"), and
# "and up to" / "or more than" / "or more importantly" are not bounds on the
# number before them. at least / at most / max / tops count only at the end of
# a clause ("$8M at least."), where they cannot belong to a later figure.
_BOUND_AFTER_WORDS: dict[str, tuple[str, ...]] = {
    "min": (
        "or more",
        "or higher",
        "or greater",
        "or above",
        "or better",
        "and up",
        "and upward",
        "and upwards",
        "and above",
        "and over",
    ),
    "max": ("or less", "or fewer", "or lower", "or below", "or under", "and under", "and below"),
    # "~$110K at peak", "(~$110K peak)", "~$110K peaks" (review 2026-10-10). A
    # bare "peak" counts only right after the number: "9 AEs through peak
    # season" puts no bound on 9 (_qualifiers)
    "peak": ("at peak", "at its peak", "at the peak", "peak", "peaks"),
}
_BARE_PEAK = frozenset({"peak", "peaks"})
_BOUND_AFTER_FINAL: dict[str, tuple[str, ...]] = {
    "min": ("at least", "at minimum", "at a minimum", "minimum"),
    "max": ("at most", "at the most", "at a maximum", "maximum", "max", "tops"),
}
_AFTER_CLASS = {
    w: cls
    for table in (_BOUND_AFTER_WORDS, _BOUND_AFTER_FINAL)
    for cls, words in table.items()
    for w in words
} | {"plus": "min"}
_NOT_A_FIGURE_AHEAD = r"(?![^\S\n]*[~≈$€£]?\d)"
_BOUND_AFTER = re.compile(
    r"(?P<gap>(?:[^\S\n]+/?[A-Za-z][\w'’/-]*){0,3}?)"
    r"(?:-(?P<hyphen>plus)(?![\w-])"
    r"|[^\S\n]+(?P<plus>plus)(?![\w-])(?![^\S\n]+(?:a|an|the|to|than)\b)"
    + _NOT_A_FIGURE_AHEAD
    + r"|[^\S\n]*,?[^\S\n]+(?P<word>"
    + _phrase_alt(w for ws in _BOUND_AFTER_WORDS.values() for w in ws)
    + r")(?![\w-])(?![^\S\n]+(?:to|than|importantly|all)\b)"
    + _NOT_A_FIGURE_AHEAD
    + r"|[^\S\n]*,?[^\S\n]+(?P<final>"
    + _phrase_alt(w for ws in _BOUND_AFTER_FINAL.values() for w in ws)
    + r")(?=[^\S\n]*(?:[.,;:!?)\]]|$)))",
    re.I,
)

# The period of a rate (review of render-confident): "5–7 deals/month", "$1.8M a
# year", "$8M in pipeline annually", "used daily by 40+ people", "~$5.6M
# run-rate", "$1.3M ARR", "30% YoY". "$1.8M/month" is not "$1.8M/yr", so a figure's
# period is part of the figure, like its unit. "a year" after a preposition is
# a duration, not a rate ("in under a year", "within a year", "in a day"), and
# "a quarter of", "a year of" are not periods at all.
_PERIOD_CLASS = {
    "hr": "hour",
    "hrs": "hour",
    "hour": "hour",
    "hourly": "hour",
    "day": "day",
    "daily": "day",
    "wk": "week",
    "week": "week",
    "weekly": "week",
    "mo": "month",
    "mos": "month",
    "mth": "month",
    "month": "month",
    "monthly": "month",
    "qtr": "quarter",
    "quarter": "quarter",
    "quarterly": "quarter",
    "yr": "year",
    "year": "year",
    "annum": "year",
    "annual": "year",
    "annually": "year",
    "yearly": "year",
    "yoy": "year",
    "arr": "year",
    "mom": "month",
    "mrr": "month",
    "qoq": "quarter",
}
# A rate hyphenated onto its figure keeps its period (2026-10-15): "launched
# at $12K-a-week" was read as "$12K" with no period and failed dropped_period
# against a bullet's "~$12K/week". The hyphen counts only right after a
# figure's digit, magnitude letter or "%" ("$12K-a-week",
# "$3M-per-year", "20-a-day", "5%-a-month"); after a word it is no figure's
# period ("a once-a-month review", "one-a-day"), as before.
_FIGURE_HYPHEN = r"(?:(?<=\d)|(?<=\d[KkMmBb])|(?<=\d[Mm][Mm])|(?<=\d[Bb]n)|(?<=%))-"
_PERIOD_MARK = re.compile(
    r"/[^\S\n]?(?P<slash>(?i:hrs?|hour|day|wk|week|mos?|mth|month|qtr|quarter|yr|year|annum))"
    r"(?![A-Za-z])"
    rf"|(?:(?<![\w-])|{_FIGURE_HYPHEN})(?P<art>(?i:per|an?|each|every))(?:[^\S\n]+|-)(?P<unit>"
    r"(?i:hour|day|week|month|quarter|year|annum))(?![\w-])"
    r"|(?<![\w-])(?P<adv>(?i:hourly|daily|weekly|monthly|quarterly|annually|annual|yearly))"
    r"(?![\w-])"
    r"|(?<![\w-])(?P<over>(?i:hour|day|week|month|quarter|year))(?:[^\S\n]+|-)(?i:over)"
    r"(?:[^\S\n]+|-)(?i:hour|day|week|month|quarter|year)(?![\w-])"
    r"|(?<![\w-])(?P<abbr>YoY|YOY|MoM|QoQ|QOQ|ARR|MRR)(?![\w-])"
    r"|(?<![\w-])(?P<run>(?i:run(?:[^\S\n]|-)?rate))(?![\w-])"
)
_DURATION_BEFORE = re.compile(
    r"\b(?:under|within|in|inside|after|before|over|for|than|of|about|around|nearly|almost"
    r"|half|just|less|more|last|past|first|next|same)[^\S\n]+$",
    re.I,
)
_OF_AFTER = re.compile(r"[^\S\n]+of\b", re.I)
_PERIOD_LABEL = {
    "hour": "per hour",
    "day": "per day",
    "week": "per week",
    "month": "per month",
    "quarter": "per quarter",
    "year": "per year",
    "run_rate": "as a run-rate",
}
# Between a figure and its period: at most four words on the same clause
# ("5–7 deals/month", "$8M in enterprise pipeline annually", "$8M, monthly").
# Before a figure: at most three ("used daily by 40+ people", "monthly partner
# bookings from ~$20K"). Punctuation that ends a clause, another number or a line
# break in between means the period is not this figure's.
_GAP_WORD = r"/?[A-Za-z][\w'’&/-]*"
_AFTER_GAP = re.compile(rf"(?:[^\S\n]*{_GAP_WORD}){{0,4}}(?:[^\S\n]*,)?[^\S\n]*")
_BEFORE_GAP = re.compile(rf"[^\S\n]*(?:{_GAP_WORD}[^\S\n]*){{0,3}}")
# "~$20K → $75K+ monthly", "monthly partner bookings from ~$20K to $75K+": one period
_TRANSITION = re.compile(r"[^\S\n]*(?:→|->|–>|—>|=>|\bto\b)[^\S\n]*")
# the pre-audit sentence-level hedge test, kept only as a backstop
_LEGACY_HEDGES = ("~", "roughly", "about", "approximately", "up to", "under ", "over ", "+")
_LEGACY_RANGE = re.compile(r"\d\s*[–—-]\s*\$?\d")

# sentence-position capitals and generic words that aren't factual claims
_PROPER_ALLOW = {
    "The",
    "Built",
    "Led",
    "Shipped",
    "Grew",
    "Owned",
    "Ran",
    "Rebuilt",
    "Replaced",
    "Designed",
    "Introduced",
    "Restructured",
    "Shape",
    "Briefed",
    "Trained",
    "Closed",
    "Sold",
    "Sourced",
    "Rose",
    "Cut",
    "Top",
    "Account",
    "Sales",
    "Chief",
    "Director",
    "Manager",
    "Executive",
    "Founding",
    "AEs",
    "AE",
    "ARR",
    "MRR",
    "OTE",
    "GTM",
    "AI",
    "B2B",
    "B2C",
    "SaaS",
    "CRM",
    "CEO",
    "CTO",
    "COO",
    "IT",
    "US",
    "USA",
    "FDA",
    "RAG",
    "API",
    "LLM",
    "SMB",
    "New",
    "York",
    "Jersey",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
}


@dataclass
class ProvenanceResult:
    ok: bool
    violations: list[dict[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# numeric claims (value + unit)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NumClaim:
    """One numeric claim. `kind` is the unit class: currency, percent,
    multiplier, rank, year or plain. `value` is scaled ($1.3M → 1300000).

    `hedged` is the estimate_style "hedged" test (a hedge next to it, or one
    end of a range). The rest is what estimate_style "confident" compares:
    `approx` (an approximation mark next to it), `bounds` (the bound classes
    next to it: "max" for up to/under/</or less, "min" for over/+/>/or more,
    "near" for nearly/almost, "peak" for peaked at/at peak/peak), `range_keys`
    (the keys of both ends when it is one end of a range like "6–10") and
    `figure` (the figure as written, qualifiers and range included: "up to
    $1.8M", "6–10", "~$5.6M").

    `period` is checked in every style: the period of a rate ("day" for
    "5–7 deals/month", "year" for "$8M … annually", "run_rate" for "~$5.6M
    run-rate"), "" for none; two periods on one figure are joined with "+"."""

    value: float
    kind: str
    text: str
    start: int
    end: int
    hedged: bool = False
    approx: bool = False
    bounds: frozenset[str] = frozenset()
    range_keys: tuple[tuple[str, float], tuple[str, float]] | None = None
    figure: str = ""
    period: str = ""

    @property
    def key(self) -> tuple[str, float]:
        return (self.kind, round(self.value, 6))

    def describe(self) -> str:
        return f"{self.text!r} ({self.kind})"


_DIGITS = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
_ORDINAL = re.compile(r"(?:st|nd|rd|th)\b")
_PERCENT = re.compile(r"\s?(?:%|percent\b|per cent\b|pct\b)", re.I)
_MAG_ATTACHED = re.compile(r"(?:[kK]|[mM]{1,2}|[bB]n?)(?![A-Za-z])")
_MAG_WORD = re.compile(r"\s(?:thousand|million|billion)\b", re.I)
_MULT = re.compile(r"(?:[xX×](?![A-Za-z])|\s?-?fold\b|\s+times\b)", re.I)
# "1 million dollars", "a million-dollar quarter"
_DOLLARS = re.compile(r"[\s-](?:dollars?|USD)\b", re.I)
_RANK_PREFIX = re.compile(r"(?:#\s?|\bNo\.\s?)$")
_RANGE_GAP = re.compile(r"^\s*[–—-]\s*$")
_MAGNITUDE = {"k": 1e3, "m": 1e6, "mm": 1e6, "b": 1e9, "bn": 1e9}
_MAG_WORDS = {"thousand": 1e3, "million": 1e6, "billion": 1e9}

_SMALL = {
    "zero": 0,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_SCALES = {"hundred": 100.0, "thousand": 1e3, "million": 1e6, "billion": 1e9}
_NUMWORD = "|".join([*_SMALL, *_SCALES, "dozen"])
# "one" alone is too common to be a claim; it counts only before a scale word
_WORDNUM = re.compile(
    rf"\b(?:(?:a|an|one)[\s-]+(?=(?:dozen|hundred|thousand|million|billion)\b))?"
    rf"(?:{_NUMWORD})(?:[\s-]+(?:and[\s-]+)?(?:{_NUMWORD}))*\b",
    re.I,
)
_FOLDWORD = re.compile(
    r"\b(two|three|four|five|six|seven|eight|nine|ten|twenty|hundred)fold\b", re.I
)
_MULT_VERB = re.compile(
    r"\b(doubl(?:e|ed|es|ing)|tripl(?:e|ed|es|ing)|quadrupl(?:e|ed|es|ing)|twice"
    r"|halv(?:e|ed|es|ing))\b",
    re.I,
)
_MULT_VERB_VALUE = {"doubl": 2.0, "tripl": 3.0, "quadrupl": 4.0, "twice": 2.0, "halv": 0.5}
_RANK_WORD = re.compile(
    r"\b(?:number one|ranked (?:first|1st)|first place"
    r"|top(?:[\s-]performing)?[\s-](?:rep|performer|seller|producer|closer|ae|salesperson))\b",
    re.I,
)

# Vague and fractional quantities (audit review wp2): "millions in revenue",
# "hundreds of deals", "tens of millions", "a decade", "a year and a half".
# These are the usual ways a letter inflates scale or tenure, so each is a
# numeric claim that must appear in a cited source. They get their own unit
# classes ("vague", "decade") so they can only match a source that says the
# same thing — "a decade" is never sourced by "40+ daily users".
_VAGUE_SCALE = {
    "tens": 10.0,
    "dozens": 12.0,
    "hundreds": 100.0,
    "thousands": 1e3,
    "millions": 1e6,
    "billions": 1e9,
}
_VAGUE = re.compile(
    r"\b(?:multi-?(?P<multi>million|billion)"
    r"|(?:(?P<pre>tens|dozens|hundreds)\s+of\s+)?"
    r"(?P<scale>tens(?=\s+of\b)|dozens|hundreds|thousands|millions|billions))\b",
    re.I,
)
_SMALL_ALT = "|".join(_SMALL)
_DECADE = re.compile(
    rf"\b(?:(?P<n>half[\s-]+an?|an?|one|{_SMALL_ALT}|\d+(?:\.\d+)?)\+?[\s-]+)?decade(?P<pl>s)?\b",
    re.I,
)
_AND_A_HALF = re.compile(
    rf"\b(?P<n>an?|one|{_SMALL_ALT}|\d+)"
    r"(?:[\s-]+(?P<u>years?|months?|quarters?|weeks?|days?|decades?))?"
    r"[\s-]+and[\s-]+a[\s-]+half\b",
    re.I,
)
# "half" as a quantity; "first half of 2023" and "half-hearted" are not claims
_HALF = re.compile(
    r"\bhalf\b(?!-(?:hearted|baked|time|way)\b)"
    r"(?:[\s-]+an?\b(?:\s+(?:year|month|quarter|week|day)\b)?)?",
    re.I,
)
_HALF_NOT_QTY = re.compile(
    r"\b(?:first|second|back|front|latter|former|other|better|bottom|top|1st|2nd)[\s-]+$", re.I
)
_FRACTION = re.compile(
    r"\b(?:(?P<third>(?:a|one)[\s-]+third)|(?P<thirds>two[\s-]+thirds)"
    r"|(?P<quarters>three[\s-]+quarters))\b(?=\s+of\b|\s*[.,;:!?)]|\s*$)",
    re.I,
)
_FRACTION_VALUE = {"third": 1 / 3, "thirds": 2 / 3, "quarters": 0.75}


def _count_word(w: str) -> float:
    w = re.sub(r"\s+", " ", w.strip().lower())
    if w in ("a", "an", "one"):
        return 1.0
    if w.startswith("half"):
        return 0.5
    if w in _SMALL:
        return float(_SMALL[w])
    return float(w)


@dataclass
class _Raw:
    start: int
    end: int
    num: float
    cur: bool = False
    pct: bool = False
    mult: bool = False
    rank: bool = False
    mag: float = 1.0
    is_year_shape: bool = False
    unit: str = ""  # an explicit unit class ("vague", "decade") overrides the flags

    def claim(self, text: str, hedged: bool) -> NumClaim:
        if self.unit:
            kind = self.unit
        elif self.rank:
            kind = "rank"
        elif self.pct:
            kind = "percent"
        elif self.mult:
            kind = "multiplier"
        elif self.cur:
            kind = "currency"
        elif self.is_year_shape and self.mag == 1.0 and 1900 <= self.num <= 2099:
            kind = "year"
        else:
            kind = "plain"
        return NumClaim(
            self.num * self.mag, kind, text[self.start : self.end], self.start, self.end, hedged
        )


def _digit_claims(text: str) -> list[_Raw]:
    out: list[_Raw] = []
    for m in _DIGITS.finditer(text):
        s, e = m.start(), m.end()
        if s and (text[s - 1].isalpha() or text[s - 1] in "_"):
            continue  # embedded in a word: B2B, Q3, H1, SOC2
        raw = _Raw(s, e, float(m.group().replace(",", "")))
        raw.is_year_shape = len(m.group()) == 4 and "," not in m.group() and "." not in m.group()
        if s and text[s - 1] in "$€£":
            raw.cur, raw.start = True, s - 1
        rp = _RANK_PREFIX.search(text, 0, raw.start)
        if rp:
            raw.rank, raw.start = True, rp.start()
        pos = e
        if mo := _ORDINAL.match(text, pos):
            raw.end = mo.end()
            out.append(raw)
            continue
        if mp := _PERCENT.match(text, pos):
            raw.pct, pos = True, mp.end()
        else:
            if mm := _MAG_ATTACHED.match(text, pos):
                raw.mag, pos = _MAGNITUDE[mm.group().lower()], mm.end()
            elif mw := _MAG_WORD.match(text, pos):
                raw.mag, pos = _MAG_WORDS[mw.group().strip().lower()], mw.end()
            if mx := _MULT.match(text, pos):
                raw.mult, pos = True, mx.end()
            elif md := _DOLLARS.match(text, pos):
                raw.cur, pos = True, md.end()
        raw.end = pos
        out.append(raw)
    return out


def _wordnum_value(phrase: str) -> float:
    total, current = 0.0, 0.0
    for w in re.split(r"[\s-]+", phrase.lower()):
        if w in ("a", "an", "one"):
            current = max(current, 1.0)
        elif w == "and":
            continue
        elif w in _SMALL:
            current += _SMALL[w]
        elif w == "dozen":
            current = max(current, 1.0) * 12
        elif w == "hundred":
            current = max(current, 1.0) * 100
        elif w in _SCALES:
            total += max(current, 1.0) * _SCALES[w]
            current = 0.0
    return total + current


def _word_claims(text: str) -> list[_Raw]:
    out: list[_Raw] = []
    for m in _WORDNUM.finditer(text):
        raw = _Raw(m.start(), m.end(), _wordnum_value(m.group()))
        pos = m.end()
        if mp := _PERCENT.match(text, pos):
            raw.pct, pos = True, mp.end()
        elif mx := _MULT.match(text, pos):
            raw.mult, pos = True, mx.end()
        elif md := _DOLLARS.match(text, pos):
            raw.cur, pos = True, md.end()
        raw.end = pos
        out.append(raw)
    for m in _FOLDWORD.finditer(text):
        v = _SMALL.get(m.group(1).lower(), 100 if m.group(1).lower() == "hundred" else 0)
        out.append(_Raw(m.start(), m.end(), float(v), mult=True))
    for m in _MULT_VERB.finditer(text):
        stem = next(k for k in _MULT_VERB_VALUE if m.group(1).lower().startswith(k))
        out.append(_Raw(m.start(), m.end(), _MULT_VERB_VALUE[stem], mult=True))
    for m in _RANK_WORD.finditer(text):
        out.append(_Raw(m.start(), m.end(), 1.0, rank=True))
    return out + _quantity_word_claims(text)


def _quantity_word_claims(text: str) -> list[_Raw]:
    """Plural magnitudes, decades and fractions (see _VAGUE above)."""
    out: list[_Raw] = []
    for m in _VAGUE.finditer(text):
        if m.group("multi"):
            value = 1e6 if m.group("multi").lower() == "million" else 1e9
        else:
            value = _VAGUE_SCALE[m.group("scale").lower()]
            if m.group("pre"):
                value *= _VAGUE_SCALE[m.group("pre").lower()]
        out.append(_Raw(m.start(), m.end(), value, unit="vague"))
    for m in _DECADE.finditer(text):
        # bare "decades" is an unstated count (0); bare "decade" is one
        n = _count_word(m.group("n")) if m.group("n") else (0.0 if m.group("pl") else 1.0)
        out.append(_Raw(m.start(), m.end(), n, unit="decade"))
    for m in _AND_A_HALF.finditer(text):
        unit = "decade" if (m.group("u") or "").lower().startswith("decade") else ""
        out.append(_Raw(m.start(), m.end(), _count_word(m.group("n")) + 0.5, unit=unit))
    for m in _HALF.finditer(text):
        if _HALF_NOT_QTY.search(text, 0, m.start()):
            continue
        in_half = re.search(r"\bin\s+$", text[: m.start()], re.I) is not None
        out.append(_Raw(m.start(), m.end(), 0.5, mult=in_half))  # "cut in half" ~ "halved"
    for m in _FRACTION.finditer(text):
        part = next(k for k in _FRACTION_VALUE if m.group(k))
        out.append(_Raw(m.start(), m.end(), _FRACTION_VALUE[part]))
    return out


def numeric_claims(text: str) -> list[NumClaim]:
    """Every numeric claim in `text`, with its unit class and whether a hedge
    sits next to it (or it is one end of a range like "$60–75K")."""
    # longest match first at each position: "two decades" is one claim, not "two"
    raws = sorted(_digit_claims(text) + _word_claims(text), key=lambda r: (r.start, -r.end))
    # a match inside an earlier one is the same claim: "million" in "2.5 million"
    kept: list[_Raw] = []
    for r in raws:
        if kept and r.start < kept[-1].end:
            continue
        kept.append(r)
    in_range: set[int] = set()
    pair_of: dict[int, tuple[int, int]] = {}
    for i in range(len(kept) - 1):
        a, b = kept[i], kept[i + 1]
        if _RANGE_GAP.match(text[a.end : b.start]):
            in_range.update((i, i + 1))
            pair_of[i] = pair_of[i + 1] = (i, i + 1)
            # units are shared across a range: "$60–75K" is $60K–$75K
            if b.pct and not a.pct:
                a.pct = True
            if b.mag != 1.0 and a.mag == 1.0 and not a.pct:
                a.mag = b.mag
            if a.cur and not b.cur:
                b.cur = True
            elif b.cur and not a.cur:
                a.cur = True
    claims: list[NumClaim] = []
    quals = [_qualifiers(text, r.start, r.end) for r in kept]
    for i, r in enumerate(kept):
        hedged = (
            i in in_range
            or bool(_HEDGE_BEFORE.search(text[: r.start]))
            or bool(_HEDGE_AFTER.match(text[r.end :]))
        )
        claims.append(r.claim(text, hedged))
    spans: list[tuple[int, int]] = []
    for i in range(len(claims)):
        # the range is one figure: "~$55–70K", "6–10", "$60–75K+"
        lo, hi = pair_of.get(i, (i, i))
        spans.append((quals[lo][2], quals[hi][3]))
    periods = _figure_periods(text, spans, [c.kind for c in claims])
    out: list[NumClaim] = []
    for i, c in enumerate(claims):
        approx, bounds, _qs, _qe = quals[i]
        range_keys = None
        if i in pair_of:
            lo, hi = pair_of[i]
            range_keys = (claims[lo].key, claims[hi].key)
        out.append(
            replace(
                c,
                approx=approx,
                bounds=bounds,
                range_keys=range_keys,
                figure=text[spans[i][0] : spans[i][1]].strip(),
                period=periods[i],
            )
        )
    return out


def _qualifiers(text: str, start: int, end: int) -> tuple[bool, frozenset[str], int, int]:
    """(approx, bound classes, figure start, figure end) for the number at
    text[start:end]: the approximation marks and bounds right before it
    ("up to ~$1.8M", "<~6%"), a "+" right after it ("10+"), and a bound phrase
    after it ("$8M or more", "$8M-plus", "$8M and up", "9 AEs or fewer")."""
    approx = False
    bounds: set[str] = set()
    pos = start
    for _ in range(4):  # "up to ~", "under ~", "<~": a few stacked qualifiers
        head = text[:pos]
        if m := _APPROX_BEFORE.search(head):
            approx, pos = True, m.start()
        elif m := _BOUND_BEFORE.search(head):
            cls = _bound_class(m, text, end)
            if cls is None:
                break
            bounds.add(cls)
            pos = m.start()
        else:
            break
    qend = end
    if plus := _HEDGE_AFTER.match(text[end:]):
        bounds.add("min")
        qend = end + plus.end()
    # Every bound phrase after the number counts, stacked ones included.
    # Review 2026-10-10: the scan stopped at the first phrase, so in "~$110K
    # at peak or more", "$110K peak and up" and "$110K at peak, tops" only the
    # peak was read and the figure passed as the source's "~$110K at peak". A
    # peak is equivalent to a peak only; it never hides another qualifier.
    # Only the first phrase may sit a few words away ("9 AEs or more"); a
    # later one must follow the one before it directly.
    cursor = qend
    for i in range(4):
        after = _BOUND_AFTER.match(text, cursor)
        if after is None or (i and after.group("gap")):
            break
        phrase = re.sub(
            r"\s+",
            " ",
            next(after.group(g) for g in ("hyphen", "plus", "word", "final") if after.group(g)),
        ).lower()
        cursor = after.end()
        if after.group("gap") and phrase in _BARE_PEAK:
            # "9 AEs through peak season": a bare peak words away from the
            # number is not about it. Only a phrase right after it may follow.
            continue
        bounds.add(_AFTER_CLASS[phrase])
        if not after.group("gap") and after.start() == qend:
            # "$8M or more" is the figure; in "9 AEs or more" the words
            # between stay out of it, so a period there is still found
            qend = after.end()
    return approx, frozenset(bounds), pos, qend


def _bound_class(m: re.Match[str], text: str, end: int) -> str | None:
    """The bound class of a _BOUND_BEFORE match right before the number
    ending at text[end], or None when the words are not a bound ("rather
    than 3", "in the past 3 years")."""
    if m.group("sym"):
        return _SYM_CLASS[m.group("sym")]
    if m.group("verb"):
        return "min"
    if cmp := m.group("cmp"):
        word = cmp.lower()
        if word in _NOT_COMPARATIVE:
            return None
        cls = _CMP_CLASS.get(word, "cmp")
        return _FLIP.get(cls, cls) if m.group("neg") else cls
    word = re.sub(r"\s+", " ", m.group("word")).lower()
    if word == "past" and (
        _DETERMINER_BEFORE.search(text, 0, m.start()) or _TIME_UNIT_AFTER.match(text, end)
    ):
        return None
    return _BOUND_CLASS[word]


def _period_marks(text: str) -> list[tuple[int, int, str]]:
    """(start, end, period class) of every rate period in `text`."""
    out: list[tuple[int, int, str]] = []
    for m in _PERIOD_MARK.finditer(text):
        if m.group("run"):
            cls = "run_rate"
        elif m.group("art"):
            if m.group("art").lower() in ("a", "an") and (
                _DURATION_BEFORE.search(text, 0, m.start()) or _OF_AFTER.match(text, m.end())
            ):
                continue  # "in under a year", "within a year", "a year of"
            cls = _PERIOD_CLASS[m.group("unit").lower()]
        else:
            word = m.group("slash") or m.group("adv") or m.group("over") or m.group("abbr")
            cls = _PERIOD_CLASS[word.lower()]
        out.append((m.start(), m.end(), cls))
    return out


def text_periods(text: str) -> frozenset[str]:
    """Every rate period `text` states anywhere, tied to a figure or not
    ("Briefed the CEO monthly" → {"month"})."""
    return frozenset(cls for _s, _e, cls in _period_marks(text))


def mask_periods(text: str) -> str:
    """`text` with every rate period blanked out, its offsets kept. Slice it
    after masking, never before: a period hyphenated onto its figure
    ("$22K-a-month") is one only with the figure in view (_FIGURE_HYPHEN)."""
    return _PERIOD_MARK.sub(lambda m: " " * len(m.group()), text)


def _figure_periods(text: str, spans: Sequence[tuple[int, int]], kinds: Sequence[str]) -> list[str]:
    """The period of each figure (spans[i] is claim i's figure, a range's two
    ends sharing one span). A period goes to the figure right before it (at
    most four words back, same clause), else to the one right after it (at
    most three words ahead); "from ~$20K to $75K+ monthly" gives both ends of
    the move the same period. A year ("in 2021") is never a rate."""
    figs = sorted({s for s, k in zip(spans, kinds, strict=True) if k != "year"})
    got: dict[tuple[int, int], set[str]] = {f: set() for f in figs}
    for ms, me, cls in _period_marks(text):
        if any(fs < me and ms < fe for fs, fe in spans):
            continue  # inside a figure: "half a year", "a year and a half"
        before = [f for f in figs if f[1] <= ms]
        after = [f for f in figs if f[0] >= me]
        if before and _AFTER_GAP.fullmatch(text, before[-1][1], ms):
            got[before[-1]].add(cls)
        elif after and _BEFORE_GAP.fullmatch(text, me, after[0][0]):
            got[after[0]].add(cls)
    masked = mask_periods(text)
    for a, b in zip(figs, figs[1:], strict=False):
        between = masked[a[1] : b[0]]
        if _TRANSITION.fullmatch(between) and bool(got[a]) != bool(got[b]):
            got[a] = got[b] = got[a] | got[b]
    out: list[str] = []
    for s, k in zip(spans, kinds, strict=True):
        found = set(got.get(s, set())) if k != "year" else set()
        if "run_rate" in found:
            found.discard("year")  # "an annual run-rate" is a run-rate
        out.append("+".join(sorted(found)))
    return out


def _period_text(period: str) -> str:
    return " and ".join(_PERIOD_LABEL[p] for p in period.split("+")) if period else "with no period"


def states_period(c: NumClaim, o: NumClaim, rendered_periods: frozenset[str]) -> bool:
    """True when source occurrence `o` states c's period: the same one, or
    — when the parser tied no period to c — one the rendered text states
    elsewhere (the text's own period, read as c's)."""
    if o.period == c.period:
        return True
    return not c.period and bool(o.period) and set(o.period.split("+")) <= rendered_periods


def period_mismatch(
    c: NumClaim, occurrences: Sequence[NumClaim], rendered_periods: frozenset[str]
) -> tuple[str, str] | None:
    """Every estimate_style: None when one of the source's own `occurrences`
    of c's value and unit states c's period (states_period). Otherwise
    (kind, detail): "$1.8M/month" for "$1.8M/yr" is changed_period, "$22K/month" for
    "$22K" is added_period, "5–7 deals" for "5–7 deals/month" is dropped_period.
    A year, or a figure the source lacks entirely (fabricated_number), is not
    this check's business."""
    if c.kind == "year" or not occurrences:
        return None
    if any(states_period(c, o, rendered_periods) for o in occurrences):
        return None
    written = " or ".join(
        dict.fromkeys(f"{o.figure or o.text!r} {_period_text(o.period)}" for o in occurrences)
    )
    shown = repr(c.figure or c.text)
    if not c.period:
        return (
            "dropped_period",
            f"{shown} drops the period the source states ({written}); a rate keeps its period",
        )
    if not any(o.period for o in occurrences):
        return (
            "added_period",
            f"{shown} {_period_text(c.period)} adds a period the source does not state ({written})",
        )
    return (
        "changed_period",
        f"{shown} {_period_text(c.period)} is not the source's period ({written}); a rate "
        "keeps its period",
    )


def figure_mismatch(c: NumClaim, occurrences: Sequence[NumClaim]) -> tuple[str, str] | None:
    """estimate_style "confident": None when one of the source's own
    `occurrences` of c's value and unit states it the way c does — the same
    range (or none) and the same bounds, approximation marks aside. Otherwise
    (kind, detail). Callers pass only the occurrences that state c's period
    (states_period), so the range and bounds come from one source figure. A
    year, or a figure the source lacks entirely (that is a fabricated_number),
    is not this check's business."""
    if c.kind == "year" or not occurrences:
        return None
    if any(o.range_keys == c.range_keys and o.bounds == c.bounds for o in occurrences):
        return None
    written = " or ".join(dict.fromkeys(repr(o.figure or o.text) for o in occurrences))
    shown = repr(c.figure or c.text)
    same_range = [o for o in occurrences if o.range_keys == c.range_keys]
    if not same_range:
        if c.range_keys is None:
            return (
                "collapsed_range",
                f"{shown} is one end of a range in the source ({written}); keep the whole range",
            )
        return "changed_range", f"{shown} is not a range the source states ({written})"
    if any(o.bounds < c.bounds for o in same_range):
        return (
            "added_qualifier",
            f"{shown} adds a bound the source does not have ({written}); state it as written",
        )
    return (
        "dropped_qualifier",
        f"{shown} drops or changes the source's qualifier ({written}); keep "
        '"+", "up to", "under", "over" exactly as written',
    )


def claim_keys(text: str) -> set[tuple[str, float]]:
    return {c.key for c in numeric_claims(text)}


# ---------------------------------------------------------------------------
# vocabulary and proper nouns
# ---------------------------------------------------------------------------


def _norm_numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in _NUM.findall(text)}


def _walk_text(x: Any) -> str:
    if isinstance(x, dict):
        return " ".join(_walk_text(v) for v in x.values())
    if isinstance(x, list):
        return " ".join(_walk_text(v) for v in x)
    return str(x)


def text_vocab(text: str) -> set[str]:
    """Lowercased tokens; "/"-joined tokens also contribute each part, and a
    trailing "." is tolerated ("B.A." ~ "b.a")."""
    toks = set(re.findall(r"[a-z0-9+./-]+", text.lower()))
    for t in list(toks):
        if "/" in t:
            toks.update(p for p in t.split("/") if p)
    toks.update(t.rstrip(".") for t in list(toks))
    return toks


def _corpus_vocab(corpus: dict[str, Any]) -> set[str]:
    """Every word that appears anywhere in the corpus (lowercased) — except the
    constraints block: a never-claim title must not become sourced vocabulary
    just because it is listed as forbidden."""
    return text_vocab(_walk_text({k: v for k, v in corpus.items() if k != "constraints"}))


def proper_tokens(text: str) -> list[tuple[str, int]]:
    """Capitalized tokens with their offsets; possessives stripped."""
    out = []
    for m in _PROPER.finditer(text):
        w = re.sub(r"['’]s?$", "", m.group())
        if len(w) >= 2:
            out.append((w, m.start()))
    return out


def corpus_hash(corpus_text: str) -> str:
    return hashlib.sha256(corpus_text.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# never-claim constraints
# ---------------------------------------------------------------------------


def constraints(corpus: dict[str, Any]) -> dict[str, list[str]]:
    """constraints.* lists; an absent block or key is an empty list."""
    cons = corpus.get("constraints") or {}
    return {
        k: [str(t) for t in (cons.get(k) or []) if str(t).strip()]
        for k in ("never_claim_titles", "never_claim_tools", "never_claim_phrases")
    }


def _title_pattern(title: str) -> re.Pattern[str]:
    t = title.strip()
    if re.fullmatch(r"[A-Z]{2,4}", t):
        # acronym titles also cover their Senior/Executive/Associate forms
        # (VP → SVP, EVP, AVP), which the old substring match used to catch
        return re.compile(rf"\b[SEA]?{t}\b", re.I)
    words = [re.escape(w) for w in re.split(r"[\s-]+", t) if w]
    return re.compile(r"\b" + r"[\s-]+".join(words) + r"\b", re.I)


def phrase_pattern(phrase: str) -> re.Pattern[str]:
    words = [re.escape(w) for w in re.split(r"[\s-]+", phrase.strip()) if w]
    return re.compile(r"\b" + r"[\s-]+".join(words) + r"\b", re.I)


# Never-claim titles: "own" is the default, "other" the narrow exception.
#
# Second review of wp2: the earlier rule read a title after any determiner as
# someone else's unless a listed claim verb sat right before it, so "Owned the
# General Manager role", "effectively the General Manager", "the org's General
# Manager for ~18 months" and "the general manager's duties were mine" all
# passed. A mention is now classified as
#   "claim" — an explicit claim cue: a claim verb (adverbs allowed: "was
#             effectively the"), bare "As", "promoted me to", a past-tense seat
#             verb ("ran the VP function"), "my/our <title>", "<title> was me";
#   "own"   — no signal that it is anyone else's title (the default), or a role
#             noun after it ("the General Manager role"), or "<title>'s duties";
#   "other" — an explicit person signal: "<title>'s office", "reported to the
#             VP", "our VP of Sales asked", "the VP, who…".
# The job being applied for may be named in a letter ("the VP of Sales role"):
# see title_mentions for when that excuses an "own"/"other" mention.

_DET = r"(?i:the|a|an|our|my|his|her|their|your|its|whose)"
_MODIFIER = (
    r"(?:[A-Z][\w-]*|(?i:former|interim|acting|new|then|previous|prior|outgoing|incoming"
    r"|old|first|first-ever|founding|global|regional|national|senior|deputy|associate"
    r"|assistant|executive))"
)
# A determiner or possessive ("the", "our", "the company's"), then at most two
# modifiers that are capitalized or title-ish ("the Global", "our interim"),
# right before the title.
_DETERMINER_BEFORE = re.compile(
    rf"\b(?:(?:{_DET}\s+)?[A-Za-z]+['’]s|{_DET})\s+(?:{_MODIFIER}\s+){{0,2}}$"
)
# "of Sales", "of sales & marketing" after a title ("VP of Sales asked")
_OF_TAIL = re.compile(
    r"\s+of\s+(?!(?i:the|a|an|my|our|his|her|their|its)\b)[A-Za-z][\w&-]*"
    r"(?:\s+(?:&\s+|and\s+)?[A-Z][\w&-]*)*"
)
# Up to two of these may sit between a claim cue and the title: "was
# effectively the General Manager", "was, in effect, the VP".
_ADVERB = (
    r"(?:[a-z]+ly|in\s+effect|de\s+facto|in\s+all\s+but\s+name|basically|then|later|also"
    r"|still|now|soon|already|briefly|eventually|once|again|acting|interim)"
)
# An explicit claim cue right before the determiner, or right before a bare
# title: the title is the candidate's own and nothing excuses it.
_CLAIM_CUES = (
    r"was|am|is|are|were|been|be|being|became|become|becomes|becoming|remain(?:ed|s)?"
    r"|serve[sd]?|serving|(?<!such\s)(?<!well\s)as|held|hold|holds|holding"
    r"|promoted(?:\s+me)?\s+(?:to|into)|rose\s+(?:to|into)|rising\s+to|named|appointed"
    r"|hired\s+as|elected|made|stepped\s+into|stepped\s+in\s+as|moved\s+into"
    r"|took\s+over\s+(?:as|from)|taking\s+over\s+(?:as|from)|stood\s+in\s+as"
    r"|filled\s+in\s+as|doubled\s+as|act(?:ed|ing)?\s+(?:as|in)|function(?:ed|ing)?\s+(?:as|in)"
    r"|operat(?:ed|ing|e)\s+(?:as|at|in)|perform(?:ed|ing)?\s+at|work(?:ed|ing)?\s+(?:as|at|in)"
    r"|serv(?:ed|ing)\s+in|sat\s+in|been\s+in|was\s+in"
    r"|(?:role|title|position|job|duties|responsibilities|work|seat|hat|chair|function|remit"
    r"|mandate|capacity|post|shoes)\s+of"
    r"|stepped\s+up\s+(?:to|into)|step(?:s|ping)?\s+up\s+(?:to|into)|moved\s+up\s+(?:to|into)"
    r"|grew\s+into|succeed(?:ed|ing|s)?|replac(?:ed|ing|e|es)|effectively|essentially"
    r"|de\s+facto|in\s+effect|basically|practically|functionally|unofficially|officially"
    r"|informally|acting|interim|(?:more|kind|sort|something|bit|much)\s+of"
    r"|(?:place|stead|lieu)\s+of"
)
_CLAIM_BEFORE = re.compile(rf"\b(?:{_CLAIM_CUES})(?:,?\s+{_ADVERB}){{0,2}},?\s+$", re.I)
# Verbs that put the candidate in the seat ("took the General Manager seat",
# "ran the VP function") — unless what follows makes the title a person the
# candidate acted on ("took the VP through the deck", "covered the General
# Manager at the board meeting").
_SEAT_BEFORE_DET = re.compile(
    r"\b(?:t(?:ook|ake|akes|aking)(?:\s+over|\s+on)?|fill(?:ed|s|ing)?|cover(?:ed|s|ing)?"
    r"|assum(?:ed|e|es|ing)|inherit(?:ed|s|ing)?|occup(?:ied|y|ies|ying)|earn(?:ed|s|ing)?"
    r"|w(?:ore|ear|ears|earing)|backfill(?:ed|s|ing)?|accept(?:ed|s|ing)?|own(?:ed|s|ing)?"
    r"|r(?:an|un|uns|unning)|d(?:id|oes|oing)|carr(?:ied|y|ies|ying)|handl(?:ed|e|es|ing)"
    r"|absorb(?:ed|s|ing)?|shar(?:ed|e|es|ing))\s+$",
    re.I,
)
# …in the past tense that is a claim about the candidate's history, not the job
_SEAT_PAST = re.compile(
    r"\b(?:took(?:\s+over|\s+on)?|filled|covered|assumed|inherited|occupied|earned|wore"
    r"|backfilled|accepted|owned|ran|did|carried|handled|absorbed|shared)\s+$",
    re.I,
)
_SEAT_PERSON_AFTER = re.compile(
    r"^(?:\s+(?:through|to|into|around|on)\s+(?:the|a|an|her|his|their|my|our)\b"
    r"|\s+(?:during|while|until)\s+(?:her|his|their|she|he|they)\b"
    r"|\s*,?\s+(?:who|whom|whose)\b)",
    re.I,
)
# a first-person possessive right before the title: "my General Manager seat"
_FIRST_POSS_BEFORE = re.compile(rf"\b(?i:my|our)\s+(?:{_MODIFIER}\s+){{0,2}}$")
# a relation word before the determiner makes the title someone the candidate
# relates to: "reported to my VP", "partnered with the CRO", "on behalf of the VP"
_RELATION_BEFORE = re.compile(
    r"\b(?:to|with|for|under|alongside|beside|by|from|behind|than|of|into|about|against"
    r"|toward|towards|supporting|supported|briefing|briefed|coaching|coached|told|asked"
    r"|thanked|met|convinced|persuaded|helped)\s+$",
    re.I,
)
# a role noun right after the title makes it a seat, never a person
_ROLE_NOUNS = (
    r"role|seat|title|position|job|post|chair|hat|function|capacity|duties|responsibilities"
    r"|remit|mandate|portfolio|scope|workload|tenure|years|days|stint|promotion|level|spot"
    r"|slot|vacancy|opening|shoes|track"
)
_ROLE_NOUN_AFTER = re.compile(rf"^[\s-]+(?:{_ROLE_NOUNS})\b", re.I)
_POSSESSIVE_AFTER = re.compile(r"^(?:['’]s\b|s['’])")
# "<title>'s duties", "the general manager's job" — the seat, not the person
_POSS_ROLE_AFTER = re.compile(
    rf"^(?:['’]s|s['’])\s+(?:(?:own|old|former|entire|whole|full|day-to-day|daily|core)\s+)?"
    rf"(?:{_ROLE_NOUNS}|work|number|quota|book|targets?|p&l|successor|replacement)\b",
    re.I,
)
# "the General Manager was me", "the VP of Sales role was mine"
_SELF_AFTER = re.compile(
    rf"^(?:[\s-]+[\w~$%+&'’-]+(?:\.\d+)?){{0,4}}?\s+(?:was|were|is|became|fell\s+to|went\s+to"
    rf"|passed\s+to|landed\s+(?:on|with)|belonged\s+to)(?:\s*,[^,.;:!?\n]{{1,30}},)?"
    rf"(?:,?\s+{_ADVERB}){{0,2}},?\s+(?:me|mine|myself)\b",
    re.I,
)
# an explicit person continuation: "our VP of Sales asked", "my VP, who…",
# "the CRO, Dana, asked…". No prepositions: "the General Manager for ~18 months"
# is a claim, not a person.
_PERSON_VERBS = (
    r"left|leaves|leaving|quit|quits|resigned|departed|retired|returned|went|came|asked"
    r"|told|said|says|wanted|needed|hired|promoted|gave|let|made|moved|joined|delegated"
    r"|assigned|handed|tapped|trusted|relied|approved|signed|called|described|was|were|is"
    r"|had|has|would|will|could|can|should|might|may|did|does|took|stepped|set|owns|owned"
    r"|ran|runs|reports|reported|expects|expected|wants|needs|decided|chose|picked|brought"
    r"|sent|invited|introduced|recommended|praised|credited"
)
_PERSON_AFTER = re.compile(
    r"^(?:\s+(?i:at\s+the\s+time|at\s+that\s+point|back\s+then|then)|\s+at\s+[A-Z][\w&-]*)?"
    r"(?:\s*,?\s+(?i:who|whom|whose)\b"
    rf"|\s*,\s*[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s*,?\s+(?i:{_PERSON_VERBS})\b"
    r"|\s+and\s+(?:I|me)\b"
    rf"|\s+(?i:{_PERSON_VERBS})\b)"
)
_CLAUSE_END = re.compile(r"[.;:!?\n]")


def _classify_title(clause: str, rest: str) -> str:
    """ "claim", "own" or "other" for one title occurrence, from the clause text
    before it and the text after it (an "of Sales" tail already removed)."""
    if _POSSESSIVE_AFTER.match(rest):
        # "the general manager's office" is another person's; "…'s duties" is the seat
        return "own" if _POSS_ROLE_AFTER.match(rest) else "other"
    det = _DETERMINER_BEFORE.search(clause)
    pre = clause if det is None else clause[: det.start()]
    if _CLAIM_BEFORE.search(pre) or _SELF_AFTER.match(rest):
        return "claim"  # "I was the VP", "As VP", "effectively the VP", "the VP was me"
    if det is None:
        return "own"  # bare title: "General Manager for ~18 months"
    first_poss = _FIRST_POSS_BEFORE.search(clause)
    seat = _SEAT_BEFORE_DET.search(pre)
    if _ROLE_NOUN_AFTER.match(rest):
        # "my General Manager seat", "ran the VP function" claim it outright;
        # "the Head of Sales role" may still be the job being applied for
        if first_poss and not _RELATION_BEFORE.search(clause[: first_poss.start()]):
            return "claim"
        return "claim" if seat and _SEAT_PAST.search(pre) else "own"
    if seat:
        if _SEAT_PERSON_AFTER.match(rest):
            return "other"  # "took the VP through the deck"
        return "claim" if _SEAT_PAST.search(pre) else "own"
    if _RELATION_BEFORE.search(pre) or _PERSON_AFTER.match(rest):
        return "other"  # "reported to my VP", "our VP of Sales asked"
    return "claim" if first_poss else "own"


def _target_names(target_company: str) -> list[str]:
    words = target_company.split()
    names = [target_company.strip()] if target_company.strip() else []
    if len(words) > 1 and len(words[0]) >= 4 and words[0][0].isupper():
        names.append(words[0])
    return names


def _target_linked(clause: str, rest: str, target_company: str) -> bool:
    """The occurrence names the target's job: "your VP of Sales", "this Head of
    Sales role", "Oscorp's VP of Sales", "the VP of Sales role at Oscorp"."""
    if re.search(rf"\b(?i:your|this)\s+(?:{_MODIFIER}\s+){{0,2}}$", clause):
        return True
    names = _target_names(target_company)
    if not names:
        return False
    alt = "|".join(re.escape(n) for n in names)
    if re.search(rf"\b(?:{alt})(?:['’]s)?\s+(?:{_MODIFIER}\s+){{0,2}}$", clause, re.I):
        return True
    return bool(
        re.match(
            rf"^(?:[\s-]+(?:{_ROLE_NOUNS}|team|search))?\s+(?:at|with|for|on|in)\s+"
            rf"(?:the\s+)?(?:{alt})\b",
            rest,
            re.I,
        )
    )


def _mentions(
    text: str,
    title: str,
    job_title: str = "",
    *,
    target_company: str = "",
    candidate_context: bool = False,
    first_person: bool = False,
) -> list[tuple[str, str, int, int]]:
    """(surface, who, start, end) per occurrence; who is "own", "other" or
    "job" (the job being applied for, which needs no source)."""
    in_job = bool(job_title.strip()) and bool(_title_pattern(title).search(job_title))
    out: list[tuple[str, str, int, int]] = []
    for m in _title_pattern(title).finditer(text):
        s, e = m.start(), m.end()
        clause = _CLAUSE_END.split(text[:s])[-1]
        after = text[e:]
        tail = _OF_TAIL.match(after)
        rest = after[tail.end() :] if tail else after
        who = _classify_title(clause, rest)
        if in_job and who != "claim":
            # The job's own title, named as the job: allowed only when the
            # sentence ties it to the target ("your VP of Sales", "the VP of
            # Sales role at Oscorp") or makes no claim about the candidate.
            # A sentence that names a candidate employer or states a candidate
            # fact gets no excuse ("At Haulmark I was the VP of Sales" fails
            # even when applying for VP of Sales), and neither does another
            # person's title in a first-person sentence ("when our VP of Sales
            # left") — that one needs a source.
            linked = _target_linked(clause, rest, target_company)
            if linked or (not candidate_context and (who == "own" or not first_person)):
                who = "job"
        out.append((m.group(), "own" if who == "claim" else who, s, e))
    return out


def target_job_spans(
    text: str, corpus: dict[str, Any], job_title: str, target_company: str
) -> list[tuple[int, int]]:
    """Character spans of never-claim titles that name the target's job and
    are not claimed: "your VP of Sales role", "the VP of Sales role at Oscorp".
    claims.py uses them so its proper-noun check agrees with this one."""
    spans: list[tuple[int, int]] = []
    if not job_title.strip():
        return spans
    for t in constraints(corpus)["never_claim_titles"]:
        if not _title_pattern(t).search(job_title):
            continue
        for m in _title_pattern(t).finditer(text):
            clause = _CLAUSE_END.split(text[: m.start()])[-1]
            after = text[m.end() :]
            tail = _OF_TAIL.match(after)
            rest = after[tail.end() :] if tail else after
            if _classify_title(clause, rest) != "claim" and _target_linked(
                clause, rest, target_company
            ):
                spans.append((m.start(), m.end() + (tail.end() if tail else 0)))
    return spans


def title_mentions(
    text: str,
    title: str,
    job_title: str = "",
    *,
    target_company: str = "",
    candidate_context: bool = False,
    first_person: bool = False,
) -> list[tuple[str, str]]:
    """Occurrences of a never-claim title, each classified as "own" (the
    candidate's title — the default: "As VP", "I was effectively the VP", "my
    General Manager seat", "the General Manager role was mine", "the org's
    General Manager for ~18 months"), "other" (another person's: "the general
    manager's office", "our VP of Sales asked", "reported to my VP") or "job"
    (the job being applied for: "your VP of Sales role")."""
    return [
        (surface, who)
        for surface, who, _s, _e in _mentions(
            text,
            title,
            job_title,
            target_company=target_company,
            candidate_context=candidate_context,
            first_person=first_person,
        )
    ]


_CTX_WORD = re.compile(r"[a-z0-9]+")


def _keeps_source_context(
    text: str, start: int, end: int, title: str, sources: Sequence[str], width: int = 2
) -> bool:
    """True when the `width` words on each side of text[start:end] are exactly
    the words around the same title in one of `sources` ("reporting to the |
    general manager | 's office"): the source's own phrase, not a paraphrase."""

    def window(s: str, a: int, b: int) -> tuple[list[str], list[str]]:
        toks = [(t.group(), t.start(), t.end()) for t in _CTX_WORD.finditer(s.lower())]
        return (
            [w for w, _ts, te in toks if te <= a][-width:],
            [w for w, ts, _te in toks if ts >= b][:width],
        )

    want = window(text, start, end)
    pat = _title_pattern(title)
    return any(
        window(src, m.start(), m.end()) == want for src in sources for m in pat.finditer(src)
    )


def never_claim_violations(
    text: str,
    corpus: dict[str, Any],
    *,
    source_text: str,
    ref: str,
    job_title: str = "",
    target_company: str = "",
    candidate_context: bool = False,
    first_person: bool = False,
    source_phrases: Sequence[str] | None = None,
) -> list[dict[str, str]]:
    """Titles rendered as the candidate's own → never_claim_title. Another
    person's title is allowed only when the source says it ("the general
    manager's office" in b_052); otherwise → unsourced_title_mention. With
    `source_phrases` (resume bullets) it must also keep the source's own words
    around it — a paraphrase of a sourced title is never_claim_title, as it
    was before the audit. The job being applied for needs no source (see
    title_mentions). Any never-claim phrase → never_claim_phrase."""
    cons = constraints(corpus)
    out: list[dict[str, str]] = []
    for t in cons["never_claim_titles"]:
        sourced = bool(_title_pattern(t).search(source_text))
        for surface, who, s, e in _mentions(
            text,
            t,
            job_title,
            target_company=target_company,
            candidate_context=candidate_context,
            first_person=first_person,
        ):
            if who == "job":
                continue
            if who == "own" or (
                source_phrases is not None
                and sourced
                and not _keeps_source_context(text, s, e, t, source_phrases)
            ):
                out.append({"bullet_id": ref, "kind": "never_claim_title", "detail": surface})
            elif not sourced:
                out.append({"bullet_id": ref, "kind": "unsourced_title_mention", "detail": surface})
    for p in cons["never_claim_phrases"]:
        if phrase_pattern(p).search(text):
            out.append({"bullet_id": ref, "kind": "never_claim_phrase", "detail": p})
    return out


# ---------------------------------------------------------------------------
# skills line
# ---------------------------------------------------------------------------


def _norm_skill(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().casefold()


def skill_index_keys(corpus: dict[str, Any]) -> list[str]:
    return [str(k) for k in (corpus.get("skill_index") or {})]


def validate_skills_line(skills: Iterable[object], corpus: dict[str, Any]) -> list[dict[str, str]]:
    """The skills line may use ONLY skill_index keys (case/whitespace aside)."""
    keys = {_norm_skill(k) for k in skill_index_keys(corpus)}
    out: list[dict[str, str]] = []
    for s in skills:
        text = str(s)
        if _norm_skill(text) not in keys:
            out.append({"bullet_id": "skills_line", "kind": "unindexed_skill", "detail": text})
        out += never_claim_violations(text, corpus, source_text="", ref="skills_line")
    return out


def canonical_skills(skills: Iterable[object], corpus: dict[str, Any]) -> list[str]:
    """Map a validated skills line to the exact skill_index spelling."""
    by_norm = {_norm_skill(k): k for k in skill_index_keys(corpus)}
    return [by_norm[_norm_skill(str(s))] for s in skills if _norm_skill(str(s)) in by_norm]


# ---------------------------------------------------------------------------
# resume bullets
# ---------------------------------------------------------------------------


def _hedge_violations(
    bid: str,
    rendered: str,
    claims: Sequence[NumClaim],
    src_claims: Sequence[NumClaim],
    confidence: object,
) -> list[dict[str, str]]:
    """estimate_style "hedged" (the rule before 2026-10-08, unchanged)."""
    out: list[dict[str, str]] = []
    # estimate-confidence: every number keeps a hedge right next to it. The
    # pre-audit sentence-level test stays as a backstop, so nothing it
    # rejected can pass now.
    if confidence == "estimate":
        flat = [c.describe() for c in claims if not c.hedged and c.kind != "year"]
        low_r = rendered.lower()
        if (
            not flat
            and _NUM.search(rendered)
            and not any(h in low_r for h in _LEGACY_HEDGES)
            and not _LEGACY_RANGE.search(rendered)
        ):
            flat = [rendered[:40]]
        if flat:
            out.append(
                {
                    "bullet_id": bid,
                    "kind": "unhedged_estimate",
                    "detail": f"{flat} in: {rendered[:80]}",
                }
            )
    else:
        # any confidence: a number the source only ever states hedged
        # ("~$5.6M", "up to 9", "10+") keeps its hedge — the same rule
        # validate/claims.py applies to letters (audit review wp2)
        dropped: list[str] = []
        for c in claims:
            occ = [o for o in src_claims if o.key == c.key]
            if c.kind != "year" and not c.hedged and occ and all(o.hedged for o in occ):
                dropped.append(c.describe())
        if dropped:
            out.append(
                {
                    "bullet_id": bid,
                    "kind": "dropped_hedge",
                    "detail": f"{dropped} hedged in source, flat in: {rendered[:80]}",
                }
            )
    return out


def _period_violations(
    bid: str, rendered: str, claims: Sequence[NumClaim], src_claims: Sequence[NumClaim]
) -> list[dict[str, str]]:
    """Every estimate_style: a rate keeps its source's period (period_mismatch)."""
    periods = text_periods(rendered)
    out: list[dict[str, str]] = []
    for c in claims:
        hit = period_mismatch(c, [o for o in src_claims if o.key == c.key], periods)
        if not hit:
            continue
        v = {"bullet_id": bid, "kind": hit[0], "detail": f"{hit[1]} in: {rendered[:80]}"}
        if v not in out:  # a range's two ends are one figure
            out.append(v)
    return out


def _figure_violations(
    bid: str, rendered: str, claims: Sequence[NumClaim], src_claims: Sequence[NumClaim]
) -> list[dict[str, str]]:
    """estimate_style "confident", any metric_confidence: each figure states
    the range and bounds of a source occurrence with its own period; only an
    approximation mark may differ. A figure no occurrence states with its
    period is _period_violations' to report."""
    periods = text_periods(rendered)
    out: list[dict[str, str]] = []
    for c in claims:
        same = [o for o in src_claims if o.key == c.key and states_period(c, o, periods)]
        hit = figure_mismatch(c, same)
        if hit:
            out.append(
                {"bullet_id": bid, "kind": hit[0], "detail": f"{hit[1]} in: {rendered[:80]}"}
            )
    return out


def figure_violations(
    rendered: str, source_text: str, confidence: object, estimate_style: str
) -> list[dict[str, str]]:
    """The figure part of validate_tailored for one rendering of one source,
    under `estimate_style`: a value or unit the source lacks
    (fabricated_number, so inflation and added precision always fail), a
    changed, added or dropped period, then the style's own rule. Shared with
    the authenticity review (pass 3)."""
    style = estimate_styles.check_style(estimate_style)
    src_claims = numeric_claims(source_text)
    src_keys = {c.key for c in src_claims}
    claims = numeric_claims(rendered)
    out = [
        {"bullet_id": "", "kind": "fabricated_number", "detail": f"{c.describe()} not in source"}
        for c in claims
        if c.key not in src_keys
    ]
    out += _period_violations("", rendered, claims, src_claims)
    if style == estimate_styles.HEDGED:
        return out + _hedge_violations("", rendered, claims, src_claims, confidence)
    return out + _figure_violations("", rendered, claims, src_claims)


def validate_tailored(
    selected: Sequence[dict[str, Any]],
    corpus: dict[str, Any],
    *,
    skills_line: Sequence[object] | None = None,
    estimate_style: str | None = None,
) -> ProvenanceResult:
    """Validate the tailor's output against the corpus. Fail closed.
    `estimate_style` None means the configured one (validate/style.py)."""
    style = estimate_styles.resolve(estimate_style)
    bullets = {b["id"]: b for b in corpus.get("bullets", [])}
    never_tools = [t.lower() for t in constraints(corpus)["never_claim_tools"]]
    vocab = _corpus_vocab(corpus)
    violations: list[dict[str, str]] = []

    for item in selected:
        bid = item.get("bullet_id", "")
        rendered = str(item.get("rendered", ""))
        src = bullets.get(bid)
        if src is None:
            violations.append(
                {"bullet_id": bid, "kind": "unknown_bullet_id", "detail": rendered[:80]}
            )
            continue
        source_text = "\n".join(
            str(src.get(k) or "") for k in ("text_long", "text_short", "metric")
        )

        # every number in the rendered bullet must exist in its own source:
        # (1) the legacy bare-digit check, kept as an extra net …
        extra_nums = _norm_numbers(rendered) - _norm_numbers(source_text)
        if extra_nums:
            violations.append(
                {
                    "bullet_id": bid,
                    "kind": "fabricated_number",
                    "detail": f"{sorted(extra_nums)} not in source",
                }
            )
        # (2) … and value + unit: 20% is not ~$20K, #1 is not $75K+, "twelve" is 12
        src_claims = numeric_claims(source_text)
        src_keys = {c.key for c in src_claims}
        claims = numeric_claims(rendered)
        unsourced = [c.describe() for c in claims if c.key not in src_keys]
        if unsourced:
            violations.append(
                {
                    "bullet_id": bid,
                    "kind": "fabricated_number",
                    "detail": f"{unsourced} not in source (value+unit)",
                }
            )
        # (3) … and the period of a rate, in every style: "$1.8M/month" is not
        # "$1.8M/yr" (review of render-confident)
        violations += _period_violations(bid, rendered, claims, src_claims)

        # the figure as written: hedges (style "hedged") or ranges and bounds
        # (style "confident") — see the module docstring
        if style == estimate_styles.HEDGED:
            violations += _hedge_violations(
                bid, rendered, claims, src_claims, src.get("metric_confidence")
            )
        else:
            violations += _figure_violations(bid, rendered, claims, src_claims)

        # proper nouns must exist somewhere in the corpus
        for word, _pos in proper_tokens(rendered):
            if word in _PROPER_ALLOW:
                continue
            if word.lower() not in vocab:
                violations.append(
                    {"bullet_id": bid, "kind": "unsourced_proper_noun", "detail": word}
                )

        # never_claim constraints: a sourced never-claim title survives only in
        # its source's own words ("reporting to the general manager's office")
        violations += never_claim_violations(
            rendered,
            corpus,
            source_text=source_text,
            ref=bid,
            source_phrases=[str(src.get(k) or "") for k in ("text_long", "text_short")],
        )
        low = rendered.lower()
        for t in never_tools:
            if t in low:
                violations.append({"bullet_id": bid, "kind": "never_claim_tool", "detail": t})

    if skills_line is not None:
        violations += validate_skills_line(skills_line, corpus)

    return ProvenanceResult(ok=not violations, violations=violations)


# ---------------------------------------------------------------------------
# figures report (CLI `jobkit figures`, MCP `figures`)
# ---------------------------------------------------------------------------

_STYLE_RULE = {
    estimate_styles.CONFIDENT: (
        'numbers render plainly: "~", "about", "approximately", "roughly", "around" may '
        "be dropped, even on an estimate. Value, unit, period and precision must match "
        'the source ("$2M/yr" never "$2M/month"), a range stays the whole range, and '
        '"+", "up to", "under", "over", "or more" stay where the source has them (and '
        "are never added)."
    ),
    estimate_styles.HEDGED: (
        "every number of an estimate-confidence bullet keeps a hedge right next to it "
        '("~30%", "up to 15", "10+", a range), and a number its source only states '
        'hedged keeps that hedge. Value, unit and period match the source ("$2M/yr" '
        'never "$2M/month").'
    ),
}


def figures_report(
    corpus: dict[str, Any],
    estimate_style: str | None = None,
    *,
    bullet_id: str = "",
    rendered: str = "",
) -> str:
    """The configured estimate_style, what it means, and the provenance check
    under it: of one rendering of `bullet_id` when `rendered` is given, of
    that bullet's own long and short text when only `bullet_id` is, else of
    every corpus bullet's own text (a bullet whose own words fail here fails
    every resume that uses it). Read-only: no LLM, no DB."""
    style = estimate_styles.resolve(estimate_style)
    bullets = {str(b.get("id")): b for b in corpus.get("bullets") or []}
    lines = [
        f"estimate_style: {style} (config/banned_phrases.yaml drift.estimate_style)",
        f"rule: {_STYLE_RULE[style]}",
    ]
    if bullet_id and bullet_id not in bullets:
        return "\n".join([*lines, f"no bullet {bullet_id!r} in the corpus"])
    if rendered and not bullet_id:
        return "\n".join([*lines, "a rendering needs its bullet id (bullet_id)"])
    if rendered:
        checks = [(bullet_id, "rendered", rendered)]
    else:
        ids = [bullet_id] if bullet_id else list(bullets)
        checks = [
            (bid, variant, str(bullets[bid].get(f"text_{variant}") or "").strip())
            for bid in ids
            for variant in ("long", "short")
            if str(bullets[bid].get(f"text_{variant}") or "").strip()
        ]
    estimates = sorted(b for b, v in bullets.items() if v.get("metric_confidence") == "estimate")
    if not bullet_id:
        lines.append(f"estimate-confidence bullets ({len(estimates)}): {', '.join(estimates)}")
    failed = 0
    for bid, variant, text in checks:
        res = validate_tailored(
            [{"bullet_id": bid, "rendered": text}], corpus, estimate_style=style
        )
        if res.ok:
            if bullet_id:
                lines.append(f"ok    {bid} {variant}: {text}")
            continue
        failed += 1
        lines.append(f"FAIL  {bid} {variant}: {text}")
        lines += [f"        {v['kind']}: {v['detail']}" for v in res.violations]
    if not bullet_id:
        lines.append(
            f"corpus text as written: {len(checks) - failed}/{len(checks)} variants pass"
            + ("" if failed else " — every bullet can be rendered word for word")
        )
    return "\n".join(lines)
