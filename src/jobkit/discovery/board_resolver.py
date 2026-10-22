"""Aggregator and LinkedIn jobs -> the employer's own board posting.

An aggregator row is never canonical (hard rule 10), and Adzuna's apply
hand-off page blocks automated browsers, so a job found on a feed is only
worth tailoring and staging once it is matched to the employer's own
Greenhouse, Lever or Ashby posting. Done by hand, that lookup is easy to get
wrong: a board may hold two postings with the same title in different cities
(picking by title alone takes the wrong one), and a board's token may not be
the company's name (a `<name>-careers` token that a plain `<name>` probe
misses). This module does it for every aggregator job that passes the filter
(the nightly / run-all stage `resolve-boards`, after discovery and before
filter, so triage scores the employer's JD), for every LinkedIn card whose
company went to the discovery queue (rule 11's exception: a posting enters
only when the same job is found on the company's own board), for every job
listed in one of the person's job-alert emails (track/job_alerts.py: the same
card shape, with a synthetic id; read from the email, never from LinkedIn),
and again at approve time (`jobkit tailor`, which MCP approve starts,
resolves approved aggregator jobs first).

Board lookup, per company, in this order:

1. tier A: the boards jobkit already has for the company, the board the
   apply URL names, and the board registry (registries/*.yml) under the name
   or its domain label. Only an owner-curated board (config/targets.yaml;
   polled first when not polled in the last day) and the apply-URL board are
   trusted, the apply-URL board only for the row whose own apply URL names
   it (for another row of the company it is checked like a probe hit). A
   registry board, or a board jobkit holds that the registry, discovery,
   add-url or an earlier match added, is read from its list API and checked
   like a probe hit (see below);
2. only when tier 1 found no board at all: a slug probe of the Greenhouse,
   Lever and Ashby list APIs: first the name itself (spaces dropped,
   hyphenated, underscored) and its domain label ("Soylent.ai" -> soylent), then
   those with the suffixes and prefixes careers, -careers, jobs, -jobs, hq,
   -hq, inc, io, ai, app, co, the. The second set is probed only when the
   first matched nothing. A 404 is "no such board". The result (the boards
   found, or none) is cached as a `board_probe` event: a company is probed at
   most once per `board_resolution.probe_cache_days`; within that window only
   the boards found are read again. A company whose own board jobkit knows is
   never probed: a posting missing from that board (closed, or retitled by the
   aggregator) is `no_title_match`, because the probe could only reach other
   companies' boards (tyrellhq, thetyrell, ... for Tyrell). A known board that
   cannot be polled is an error, retried next run, not a reason to probe.

Posting match: the same title (case, punctuation and "&"/"and" aside: never
the normalized title, which drops qualifiers such as ", Mid-Market &
Enterprise Sales") AND a location that agrees (place_keys: the same metro,
the same city in the same state or country, the same state, or both
US-wide: "Remote - US", "United States"). Places are read more strictly than
normalize.location_bucket, the coarse dedup key: whole words, never a
substring ("Mansfield, TX" is not "sf"), and a state or country the text
names must fit the metro ("Redmond, Washington" is not Washington DC,
"Cambridge, UK" is not Boston, "London, Ontario" is not London). A bare
"Remote" next to a place takes that place's scope ("London / Remote" is not
US-wide). Exactly one such posting across the boards of a tier: it is picked.
Title matches but none or several agree on location: nothing is picked, and
the job is flagged `board_match_ambiguous` for the human (digest, MCP
get_queue and resolve_boards list the candidates; `jobkit resolve-boards
--job-id N --pick <posting id>` takes their choice). A title is never enough
on its own.

A board the probe found, the registry named, or that jobkit holds without
the user having listed it in config/targets.yaml is only a board under the
company's NAME: another company can own that token. Its posting is picked
only when something on the board confirms the company: Greenhouse reports
the board's company name, and it is the same name ("Tyrell" = "Tyrell";
"Ember" is not "Ember Grill"); or the board links to a domain of the company
(a posting or apply URL, a link with a scheme or "www.", an e-mail address; a domain
written as a word in prose does not count): the domain the company row has,
the company name when it is a domain ("Soylent.ai"), or the host of the apply
URL when it spells the company name ("ember.ai", "getember.com" for Ember).
A shared host never counts (notion.site, github.com, google.com,
airtable.com, gem.com, adp.com, ...; SHARED_HOSTS), nor a job site's,
aggregator's or ATS's: another company's board can link its own page there.
Lever and Ashby report no company name, so for them only a domain confirms.
Without that, the posting that would have been picked is flagged
`board-match-unconfirmed` instead (a `board_match_ambiguous` event whose
detail says so), nothing is ingested and no boards row is added until the
human picks it. A description that merely names the company is not
evidence: another "Ember" names itself too. A Greenhouse board whose own
company name does not fit at all ("Tyrell" vs "Tyrellium Corp") is ignored.
On a confirmed match the board is added as a boards row (confidence 'probe')
and polled every night from then on; a probe that found nothing marks the
company's discovery-queue row not_found.

On a match the posting is ingested through discovery/ingest.py (the board is
added if new and polled now) and the feed row is merged into the ATS row by
pipeline/dedup.merge_feed_row: sources move over, the feed row is closed, a
human's approval or snooze moves to the ATS row unless that row has decisions
of its own (then the pair is held for the human). The poll itself merges a
feed row whose apply URL names a posting on the board (a `gh_jid` link);
that counts as this run's merge too. An ATS row that took a human status
that way is extracted and scored at once (triage.extract_and_score), since
the triage stages only read 'filtered' rows. A queue number or job id the
human holds for the feed row reaches the ATS row afterwards
(dedup.follow_merges: MCP queue numbers, `jobkit tailor --job-id`). A
LinkedIn card stores no job row and no LinkedIn URL: a match ingests the
employer's posting, and its outcome is a `board_resolve.card` event.

Requests: only the public board list APIs (and ingest's poll of a matched
board), with jobkit's honest User-Agent, one at a time with
`board_resolution.delay_seconds` (>= 1 s) between them, at most
`board_resolution.max_requests_per_run` per run. What a run has written is
committed before each request, so no SQLite write lock is held across a
request or the delay (MCP and `tailor` write to the same DB meanwhile). HTTP
429 stops the run's probing; any other failure leaves the company unprobed
(not cached), so the next run tries again. The careers-page crawl of SPEC §2.4 does not exist in
jobkit and is not done here: a known domain only adds its label as a slug."""

from __future__ import annotations

import asyncio
import datetime
import html
import json
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

import httpx

from jobkit import db as q
from jobkit.adapters import ADAPTERS
from jobkit.config import BoardResolution, Filters, load_filters, load_targets
from jobkit.contact import user_agent
from jobkit.discovery import ingest, registry
from jobkit.discovery.url_resolver import resolve as resolve_url
from jobkit.discovery.url_resolver import resolve_for_board_matching
from jobkit.log import log
from jobkit.normalize import (
    location_bucket,
    mentions_non_us,
    mentions_us,
    mentions_us_country,
    names_no_specific_place,
    normalize_company,
)
from jobkit.pipeline.dedup import follow_merges, merge_feed_row, merged_target, posting_key
from jobkit.pipeline.filter import reject_reason_now

ATS_ORDER = ("greenhouse", "lever", "ashby")
# the list endpoints the probe reads (no JD content where the API allows it)
LIST_URLS: dict[str, str] = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
    "lever": "https://api.lever.co/v0/postings/{token}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{token}",
}
# what a token can look like per ATS (url_resolver's shapes, lowercased)
_TOKEN_SHAPE: dict[str, re.Pattern[str]] = {
    "greenhouse": re.compile(r"[a-z0-9]{2,60}"),
    "lever": re.compile(r"[a-z0-9][a-z0-9_-]{1,59}"),
    "ashby": re.compile(r"[a-z0-9][a-z0-9._-]{1,59}"),
}
SUFFIXES = ("careers", "-careers", "jobs", "-jobs", "hq", "-hq", "inc", "io", "ai", "app", "co")
PREFIXES = ("the",)
_LEGAL_WORDS = frozenset(
    {
        "inc",
        "incorporated",
        "llc",
        "ltd",
        "limited",
        "corp",
        "corporation",
        "co",
        "company",
        "gmbh",
        "plc",
        "pbc",
        "lp",
        "llp",
        "sa",
        "ag",
        "bv",
    }
)
# a company name that is itself a domain: "Soylent.ai", "Monday.com"
_NAME_DOMAIN = re.compile(
    r"\s*([a-z0-9][a-z0-9-]*)\.(?:ai|io|com|co|app|dev|so|xyz|tech|net|org|us|hq|inc|health"
    r"|finance|money|tv|gg|me|sh|to|vc|cx|fm|ly)\s*"
)
# a human status an ATS row can take in a merge that needs its requirements and
# score: approved (tailoring reads them), snoozed (the queue needs a score)
CARRIED_NEEDS_TRIAGE = ("approved", "snoozed")
GUEST_SOURCE = "linkedin_guest"
CARD_WINDOW_DAYS = 14  # LinkedIn cards seen this recently (linkedin_guest.SEEN_WINDOW_DAYS)
CARD_FLAG_DAYS = 7  # a LinkedIn card's flag is listed this long (it has no job row to close)
FLAG_KEY = "board-match-ambiguous"
# the flag when one posting would be picked but nothing confirms its board is
# the company's (a registry, discovered or probe-found board)
UNCONFIRMED_KEY = "board-match-unconfirmed"
US_WIDE = "us-wide"
# the US named as a country with nothing else said and no remote signal
# ("United States", "US" with remote unknown): the job could be anywhere in
# the US, a metro office or remote, so it agrees only with another such
# location, never with "Remote - US" or a metro (review 2026-10-12: Adzuna's
# bare "US" picked the Remote-US posting over the New York one)
US_COUNTRY = "us-country"
# Boards the company is known to own: an owner-curated board in
# config/targets.yaml ('known') and the board the apply URL names ('url').
# Every other board is only a board under the company's NAME and needs
# evidence that it is the company's (_evidence): a board from the registry
# ('registry'), one jobkit holds that discovery, add-url or an earlier match
# added ('discovered'), or one the slug probe found ('probe', or 'cache' when
# read again from the probe cache). Review 2026-10-12: the registry is keyed
# by normalized name only, so 'Initrode' reached Initrode Genomics' board.
# A 'url' board is trusted only for the row whose OWN apply URL names it
# (_Run._trusts); for any other row of the company it is a board under the
# company's name like the rest (review 2026-10-15: row B 'Lumon' was merged
# into greenhouse/lumonhealth, 'Lumon Health Inc', because row A's URL named it).
TRUSTED_ORIGINS = ("known", "url")
_ORIGIN_SAYS = {
    "url": "another row's apply URL names it",
    "registry": "the board registry names it",
    "discovered": "jobkit holds it, but it is not in config/targets.yaml",
    "probe": "the slug probe found it",
    "cache": "the slug probe found it",
}
ATS_LABEL = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby"}
# the registrable-domain label of job sites, aggregators and ATS hosts: an
# apply URL there says nothing about the employer's own domain
JOB_SITE_LABELS = frozenset(
    {
        "4dayweek",
        "a16z",
        "adzuna",
        "angel",
        "applytojob",
        "arbeitnow",
        "ashbyhq",
        "bamboohr",
        "breezy",
        "builtin",
        "careerbuilder",
        "consider",
        "cosnetwork",
        "dice",
        "dover",
        "getro",
        "glassdoor",
        "greenhouse",
        "himalayas",
        "icims",
        "indeed",
        "jazzhr",
        "jobvite",
        "jooble",
        "lever",
        "linkedin",
        "lnkd",
        "monster",
        "myworkdayjobs",
        "otta",
        "personio",
        "pinpointhq",
        "recruitee",
        "remote",
        "remoteok",
        "remotive",
        "rippling",
        "simplyhired",
        "smartrecruiters",
        "talent",
        "teamtailor",
        "usajobs",
        "weworkremotely",
        "wellfound",
        "workable",
        "workatastartup",
        "workday",
        "ycombinator",
        "ziprecruiter",
    }
)
# Registrable domains that many companies share: hosted pages, forms, code
# hosts, payroll and HR suites, link shorteners, mail and cloud hosting. Another
# company's board can link one of its own pages there ("emberfleet.notion.site"
# and "emberhq.notion.site" are both notion.site), so a shared domain is never
# evidence of whose board it is.
SHARED_HOSTS = frozenset(
    {
        # pages, docs and site builders
        "notion.site",
        "notion.so",
        "super.site",
        "coda.io",
        "craft.do",
        "gitbook.io",
        "webflow.io",
        "wixsite.com",
        "wix.com",
        "squarespace.com",
        "carrd.co",
        "framer.website",
        "framer.app",
        "softr.app",
        "bubbleapps.io",
        "canva.site",
        "medium.com",
        "substack.com",
        "wordpress.com",
        "blogspot.com",
        "linktr.ee",
        # code hosts and app hosting
        "github.com",
        "github.io",
        "githubusercontent.com",
        "gitlab.com",
        "gitlab.io",
        "bitbucket.org",
        "herokuapp.com",
        "vercel.app",
        "netlify.app",
        "pages.dev",
        "web.app",
        "firebaseapp.com",
        "amazonaws.com",
        "cloudfront.net",
        "azurewebsites.net",
        "onrender.com",
        # Google, Microsoft, forms and file sharing
        "google.com",
        "goo.gl",
        "forms.gle",
        "googleusercontent.com",
        "withgoogle.com",
        "youtube.com",
        "youtu.be",
        "office.com",
        "microsoft.com",
        "sharepoint.com",
        "live.com",
        "airtable.com",
        "typeform.com",
        "tally.so",
        "jotform.com",
        "formstack.com",
        "surveymonkey.com",
        "hsforms.com",
        "hubspot.com",
        "dropbox.com",
        "box.com",
        "docsend.com",
        "calendly.com",
        "loom.com",
        "vimeo.com",
        "zoom.us",
        # recruiting, payroll and HR suites not in JOB_SITE_LABELS
        "gem.com",
        "adp.com",
        "paylocity.com",
        "ultipro.com",
        "ukg.com",
        "oraclecloud.com",
        "taleo.net",
        "successfactors.com",
        "successfactors.eu",
        "paycomonline.net",
        "paychex.com",
        "paycor.com",
        "dayforcehcm.com",
        "csod.com",
        "avature.net",
        "brassring.com",
        "applicantpro.com",
        "applicantstack.com",
        "hrmdirect.com",
        "zohorecruit.com",
        "zoho.com",
        "recruiterbox.com",
        "trinethire.com",
        "gusto.com",
        "justworks.com",
        "eightfold.ai",
        "phenompeople.com",
        # social, shorteners and mail
        "twitter.com",
        "x.com",
        "facebook.com",
        "instagram.com",
        "t.co",
        "bit.ly",
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "yahoo.com",
        "icloud.com",
        "proton.me",
        "protonmail.com",
    }
)
# words a startup puts around its name in a domain: getember.com, emberhq.com
_DOMAIN_PREFIXES = ("get", "try", "join", "use", "go", "hey", "the")
_DOMAIN_SUFFIXES = ("hq", "app", "ai", "io", "labs", "inc", "co", "team", "careers", "jobs")
# a host where a board LINKS somewhere: after a URL scheme, "www." or the "@"
# of an e-mail address. A domain written as a word in a description ("we use
# Salesforce and Hyperion.io") is not a link and is no evidence (review
# 2026-10-12: a company named as a domain was confirmed by another board's
# job description).
_HOST_IN_LINK = re.compile(
    r"(?:\b[a-z][a-z0-9+.-]*://(?:[^@/?#\s]*@)?|(?<![a-z0-9.-])www\.|[a-z0-9._%+-]@)"
    r"([a-z0-9-]+(?:\.[a-z0-9-]+)+)"
)
_URL_HOST = re.compile(r"^(?:[a-z][a-z0-9+.-]*://)?(?:[^@/?#\s]*@)?([^/?#:\s]+)")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
_SECOND_LEVEL = frozenset({"ac", "co", "com", "edu", "gov", "ltd", "net", "org", "plc"})

_TAGS = re.compile(r"<[^>]+>")
_WORK_MODE = re.compile(
    r"\b(remote|hybrid|on-?site|in[- ]office|office|wfh|work from home|flexible|hq)\b", re.I
)
_SEGMENTS = re.compile(r"[;|\n/•·&]|\s+or\s+", re.I)
_REMOTE = re.compile(r"\b(remote|wfh|work from home)\b", re.I)
# regions wider than the US that normalize reads as the US: never US-wide here
_AMERICAS = re.compile(
    r"\b(north america|americas|america|amer|noram|namer|latam|latin america"
    r"|south america|central america)\b",
    re.I,
)
# words around a work mode that name no place: "Fully remote", "US-based",
# "Remote-friendly", "Home office"
_FILLER = re.compile(r"\b(fully|friendly|first|home|only|based|preferred|optional)\b", re.I)
_GLOBAL = re.compile(r"\b(worldwide|world|global|globally|anywhere|international)\b", re.I)
_STRIP = " ,.-–—()[]:"


# ---------------------------------------------------------------------------
# slug variants
# ---------------------------------------------------------------------------


def name_words(name: str) -> list[str]:
    """The company name as lowercase words, a trailing legal form ("Inc",
    "LLC") and a leading "The" dropped: "The Acme Co." -> ["acme"]."""
    s = html.unescape(name or "").lower().replace("&", " and ")
    s = re.sub(r"['’`]", "", s)
    words = re.findall(r"[a-z0-9]+", s)
    while len(words) > 1 and words[-1] in _LEGAL_WORDS:
        words.pop()
    if len(words) > 1 and words[0] == "the":
        words.pop(0)
    return words


def domain_label(domain: str | None) -> str | None:
    """'cyberdyne' for 'cyberdyne.com', 'https://www.cyberdyne.com/'."""
    text = (domain or "").strip().lower()
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text).split("/")[0].split(":")[0]
    text = re.sub(r"^www\.", "", text)
    parts = [p for p in text.split(".") if p]
    if len(parts) < 2 or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", parts[0]):
        return None
    return parts[0]


def name_label(name: str) -> str | None:
    """The domain label of a company name that is a domain: 'Soylent.ai' -> 'soylent'."""
    m = _NAME_DOMAIN.fullmatch((name or "").lower())
    return m.group(1) if m else None


def _add(out: list[str], token: str) -> None:
    if token and token not in out:
        out.append(token)


def slug_tiers(name: str, domain: str | None = None) -> tuple[list[str], list[str]]:
    """The board tokens to probe for a company, in two tiers: the name itself
    and its domain label, then those with the usual suffixes and prefixes.
    'Cyberdyne' -> (['cyberdyne'], ['cyberdynecareers',
    'cyberdyne-careers', ...]); 'Soylent.ai' -> (['soylentai', 'soylent',
    'soylent-ai', 'soylent_ai'], [...])."""
    words = name_words(name)
    own = name_label(name)
    labels = [lb for lb in (domain_label(domain), own) if lb]
    compact = "".join(words)
    first: list[str] = []
    for token in (compact, *labels, "-".join(words), "_".join(words)):
        _add(first, token)
    second: list[str] = []
    # suffixes go on the name, or on its label when the name is a domain
    # ("soylent-careers", not "soylentai-careers"), and on a known domain's label
    cores: list[str] = []
    for core in (own or compact, domain_label(domain) or ""):
        _add(cores, core)
    for core in cores:
        for suffix in SUFFIXES:
            if not core.endswith(suffix.lstrip("-")):
                _add(second, core + suffix)
        for prefix in PREFIXES:
            if not core.startswith(prefix):
                _add(second, prefix + core)
    return first, [t for t in second if t not in first]


def tokens_for(ats: str, tokens: list[str]) -> list[str]:
    """The tokens of a tier this ATS can have (Greenhouse: no '-' or '_')."""
    shape = _TOKEN_SHAPE[ats]
    return [t for t in tokens if shape.fullmatch(t)]


def names_compatible(company: str, board_company: str) -> bool:
    """Could a board that calls itself `board_company` be `company`'s? Blank
    on either side: no evidence against. Otherwise one compact name must start
    with the other (at least 3 characters) or their domain labels agree:
    'Soylent.ai' fits 'Soylent', 'Ember' fits 'Ember Grill' (the title and
    location still decide), 'Tyrell' does not fit 'Tyrellium Corp'."""
    a, b = "".join(name_words(company)), "".join(name_words(board_company))
    if not a or not b:
        return True
    label_a, label_b = name_label(company) or a, name_label(board_company) or b
    if label_a == label_b:
        return True
    short, long_ = sorted((a, b), key=len)
    if len(short) < 3:
        return a == b
    if a == b:
        return True
    # a whole-word prefix: "ember" + "grill", not "tyrell" + "ium"
    words_long = name_words(board_company if long_ == b else company)
    joined = ""
    for w in words_long:
        joined += w
        if joined == short:
            return True
        if len(joined) >= len(short):
            break
    return False


def same_company_name(company: str, board_company: str) -> bool:
    """The same company name, legal form and a leading 'The' aside, or the
    same domain label: 'Tyrell' = 'Tyrell', 'Soylent.ai' = 'Soylent', 'Acme' =
    'Acme, Inc.'. Unlike names_compatible, 'Ember' is not 'Ember Grill'."""
    a, b = "".join(name_words(company)), "".join(name_words(board_company))
    if not a or not b:
        return False
    return a == b or (name_label(company) or a) == (name_label(board_company) or b)


# ---------------------------------------------------------------------------
# company evidence on a board: domains
# ---------------------------------------------------------------------------


def registrable_domain(text: str | None) -> str | None:
    """The registrable domain of a host or URL: 'ember.ai' for
    'https://careers.ember.ai/jobs', 'acme.co.uk' for 'jobs.acme.co.uk'.
    None when it is not a host name."""
    m = _URL_HOST.match((text or "").strip().lower())
    if m is None:
        return None
    parts = m.group(1).strip(".").split(".")
    if len(parts) < 2 or not all(_LABEL.fullmatch(p) for p in parts):
        return None
    if not re.fullmatch(r"[a-z]{2,24}", parts[-1]):
        return None
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _strings(value: Any) -> list[str]:
    """Every string in a decoded JSON payload."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def domains_in(payload: Any) -> frozenset[str]:
    """The registrable domains a board payload links to: posting and apply
    URLs, links in descriptions (a URL with a scheme or "www."), e-mail
    addresses. A bare domain written in prose is not counted."""
    out: set[str] = set()
    for text in _strings(payload):
        for m in _HOST_IN_LINK.finditer(html.unescape(text).lower()):
            d = registrable_domain(m.group(1))
            if d is not None:
                out.add(d)
    return frozenset(out)


def is_shared_host(domain: str) -> bool:
    """A registrable domain many companies use (SHARED_HOSTS), or a job
    site's, aggregator's or ATS's (JOB_SITE_LABELS): never one company's own."""
    return domain in SHARED_HOSTS or domain.split(".")[0] in JOB_SITE_LABELS


def domain_fits_name(company: str, domain: str) -> bool:
    """Does the domain's label spell the company name? 'ember.ai' and
    'getember.com' fit 'Ember'; 'vandelay.com' fits 'Vandelay AI' (a whole-word
    start of the name); 'soylent.ai' fits 'Soylent.ai'. 'emberfleet.com' and
    'notion.site' do not fit 'Ember'."""
    label = domain.split(".")[0].replace("-", "")
    words = name_words(company)
    compact = "".join(words)
    if len(compact) < 2 or not label:
        return False
    own = (name_label(company) or "").replace("-", "")
    if label in (compact, own):
        return True
    if len(compact) >= 3 and (
        any(label == p + compact for p in _DOMAIN_PREFIXES)
        or any(label == compact + s for s in _DOMAIN_SUFFIXES)
    ):
        return True
    joined = ""
    for w in words[:-1]:
        joined += w
        if joined == label:
            return len(label) >= 3
    return False


def company_domains(company: str, domain: str | None, apply_url: str = "") -> frozenset[str]:
    """What the feed row knows of the company's own domain: its company
    row's domain (set by the user in targets.yaml), the company name when it
    is a domain ('Soylent.ai'), and the apply URL's host when that host spells
    the company name (domain_fits_name). A shared host (is_shared_host:
    notion.site, github.com, google.com, an ATS, an aggregator) never counts:
    another company's board can link its own page there."""
    out: set[str] = set()
    named = _NAME_DOMAIN.fullmatch((company or "").lower())
    for raw in (domain, named.group(0).strip() if named else None):
        d = registrable_domain(raw)
        if d is not None and not is_shared_host(d):
            out.add(d)
    host = registrable_domain(apply_url)
    if host is not None and not is_shared_host(host) and domain_fits_name(company, host):
        out.add(host)
    return frozenset(out)


# ---------------------------------------------------------------------------
# posting match: same title AND an agreeing location
# ---------------------------------------------------------------------------


def title_key(title: str) -> str:
    """The title for an exact comparison: tags, case, punctuation and '&' vs
    'and' aside. 'Sales Director ' == 'Sales Director'; 'Sales Enablement
    Manager, Mid-Market & Enterprise Sales' != '..., Customer Success & ...'."""
    s = html.unescape(_TAGS.sub(" ", title or "")).lower().replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9+#]+", s))


def place_keys(location_raw: str | None, remote: bool | None = None) -> frozenset[str]:
    """The places a location names, one key each: a metro ('nyc-metro',
    'london-metro'), another city with the region it names
    ('other-sanmateo|us|ca'), a state on its own ('region-us|tx'), 'us-wide'
    for remote in the US ("Remote - US", or "United States" with the remote
    flag), 'us-country' for the US named as a country with no remote signal
    ("United States", "US", "USA - West": the job could be in a metro office
    or remote, so it agrees only with another such location; review
    2026-10-12), 'remote-global'. A wider region ("Americas", "North
    America", "AMER") is not the US. A blank or work-mode-only location
    ("Hybrid") names no place and agrees with nothing, whatever the remote
    flag says. Several places split on ; | / • & or 'or',
    and a comma list names each metro in it ("SF, NYC, Seattle").

    Stricter than normalize.location_bucket, which is a coarse dedup key and
    is not used for places here: a city is read from its own comma part, as
    whole words ("Mansfield, TX" is not "sf"), and the state or country the
    text names must fit it ("Cambridge, UK" is not Boston, "Redmond,
    Washington" is not Washington DC, "London, Ontario" is not London). A
    remote segment is us-wide when it names the US (or a US place), never
    when it names a place outside it; a work mode on its own ("Remote" in
    "London / Remote") takes its scope from the whole location, and is
    us-wide only when that names the US or no place at all (bare "Remote").
    A region or a country outside the US on its own ("EMEA", "Canada") names
    no place. A place the reader cannot place, with the remote flag, counts
    as location_bucket reads it."""
    text = html.unescape(location_raw or "")
    segs = [s.strip(_STRIP) for s in _SEGMENTS.split(text)]
    segs = [s for s in segs if s]
    places = [
        " ".join(_FILLER.sub(" ", _WORK_MODE.sub(" ", s)).split()).strip(_STRIP) for s in segs
    ]
    # the US as a country is US-wide remote only when the row says remote
    # (its remote flag or a remote word); otherwise US_COUNTRY
    remote_said = bool(remote) or _REMOTE.search(text) is not None
    found = [
        (own - {US_COUNTRY}) | {US_WIDE} if remote_said and US_COUNTRY in own else own
        for own in (_place_keys_of(p) for p in places)
    ]
    every: set[str] = set().union(*found)
    # no place at all, only work modes: "Remote", "Remote / Hybrid", "Distributed"
    bare = all(not p or names_no_specific_place(p) for p in places)
    # ... and not a wide region either ("Americas", "North America", "AMER")
    bare = bare and not _AMERICAS.search(text)
    keys: set[str] = set()
    for seg, place, own in zip(segs, places, found, strict=True):
        keys |= own
        if _REMOTE.search(seg):
            scope = _remote_scope(seg, place, own, text, every, bare)
            if scope:
                keys.add(scope)
    # a remote flag with a place the reader cannot place: the coarse bucket.
    # A blank or work-mode-only location ("", "Hybrid") names no place and
    # never agrees, whatever the flag says (review 2026-10-12).
    has_place = any(p and not names_no_specific_place(p) for p in places)
    if not keys and remote and has_place and not _AMERICAS.search(text):
        bucket = location_bucket(location_raw or "", True)
        if bucket == "remote-us":
            keys.add(US_WIDE)
        elif bucket == "remote-global":
            keys.add("remote-global")
    return frozenset(keys)


def _place_keys_of(place: str) -> set[str]:
    """The keys of one segment's place (its work mode removed)."""
    if not place:
        return set()
    if _GLOBAL.search(place) and names_no_specific_place(_GLOBAL.sub(" ", place)):
        return {"remote-global"}  # "Worldwide", "Anywhere", "Global"
    found = _city_keys(place)
    if names_no_specific_place(place):
        # only countries and regions, or cities normalize knows by name
        # ("London", "Toronto, Canada"): their metros, or the US as a whole
        metros = {k for k in found if k in _METRO_HOME}
        if metros:
            return metros
        # "North America", "Americas", "AMER" are wider than the US
        return {US_COUNTRY} if mentions_us_country(_AMERICAS.sub(" ", place)) else set()
    return found


def _key_country(key: str) -> str:
    """The country a place key is in ('' when it names none)."""
    if key in _METRO_HOME:
        return _METRO_HOME[key][0]
    if key in (US_WIDE, US_COUNTRY):
        return "us"
    if key.startswith("other-"):
        return key.split("|")[1]
    if key.startswith("region-"):
        return key.removeprefix("region-").split("|")[0]
    return ""


def _remote_scope(
    seg: str, place: str, own: set[str], whole: str, every: set[str], bare: bool
) -> str | None:
    """Where a segment that says 'remote' is open: us-wide, remote-global, or
    None (outside the US, or not said). "Remote - US", "SF, NY, Remote" and
    "Remote - TX" name the US; "Remote - Tbilisi" names another place. A
    segment that is only a work mode ("Remote" in "London / Remote") is read
    with the whole location: us-wide only when it names the US, or names no
    place at all (bare "Remote")."""

    def in_us(text: str, keys: set[str]) -> bool:
        # a wide region ("Remote - Americas", "North America") is not the US
        return mentions_us(_AMERICAS.sub(" ", text)) or any(_key_country(k) == "us" for k in keys)

    def abroad(text: str, keys: set[str]) -> bool:
        return mentions_non_us(text) or any(_key_country(k) not in ("", "us") for k in keys)

    if in_us(seg, own):
        return US_WIDE
    if abroad(seg, own):
        return None
    if _GLOBAL.search(seg):
        return "remote-global"
    if place and not names_no_specific_place(place):
        return None  # a place jobkit cannot place: not the US
    if in_us(whole, every):
        return US_WIDE
    if abroad(whole, every):
        return None
    if _GLOBAL.search(whole):
        return "remote-global"
    return US_WIDE if bare else None


def locations_agree(
    a: str | None, b: str | None, a_remote: bool | None = None, b_remote: bool | None = None
) -> bool:
    """Same metro, the same city in the same region, the same state, or both
    US-wide remote."""
    return bool(place_keys(a, a_remote) & place_keys(b, b_remote))


# ---- cities, states and countries in a place ----

_US_STATES: dict[str, str] = {
    "alabama": "al",
    "alaska": "ak",
    "arizona": "az",
    "arkansas": "ar",
    "california": "ca",
    "colorado": "co",
    "connecticut": "ct",
    "delaware": "de",
    "district of columbia": "dc",
    "florida": "fl",
    "georgia": "ga",
    "hawaii": "hi",
    "idaho": "id",
    "illinois": "il",
    "indiana": "in",
    "iowa": "ia",
    "kansas": "ks",
    "kentucky": "ky",
    "louisiana": "la",
    "maine": "me",
    "maryland": "md",
    "massachusetts": "ma",
    "michigan": "mi",
    "minnesota": "mn",
    "mississippi": "ms",
    "missouri": "mo",
    "montana": "mt",
    "nebraska": "ne",
    "nevada": "nv",
    "new hampshire": "nh",
    "new jersey": "nj",
    "new mexico": "nm",
    "new york": "ny",
    "north carolina": "nc",
    "north dakota": "nd",
    "ohio": "oh",
    "oklahoma": "ok",
    "oregon": "or",
    "pennsylvania": "pa",
    "rhode island": "ri",
    "south carolina": "sc",
    "south dakota": "sd",
    "tennessee": "tn",
    "texas": "tx",
    "utah": "ut",
    "vermont": "vt",
    "virginia": "va",
    "washington": "wa",
    "west virginia": "wv",
    "wisconsin": "wi",
    "wyoming": "wy",
}
_US_CODES = frozenset(_US_STATES.values())
_CA_PROVINCES: dict[str, str] = {
    "alberta": "ab",
    "british columbia": "bc",
    "manitoba": "mb",
    "new brunswick": "nb",
    "newfoundland and labrador": "nl",
    "nova scotia": "ns",
    "ontario": "on",
    "prince edward island": "pe",
    "quebec": "qc",
    "québec": "qc",
    "saskatchewan": "sk",
}
_CA_CODES = frozenset(_CA_PROVINCES.values())
_COUNTRIES: dict[str, str] = {
    "us": "us",
    "usa": "us",
    "united states": "us",
    "united states of america": "us",
    "uk": "uk",
    "gb": "uk",
    "united kingdom": "uk",
    "great britain": "uk",
    "britain": "uk",
    "england": "uk",
    "scotland": "uk",
    "wales": "uk",
    "northern ireland": "uk",
    "canada": "canada",
    **{
        c: c.replace(" ", "")
        for c in (
            "ireland",
            "germany",
            "france",
            "spain",
            "portugal",
            "italy",
            "netherlands",
            "belgium",
            "switzerland",
            "austria",
            "sweden",
            "norway",
            "denmark",
            "finland",
            "iceland",
            "poland",
            "czech republic",
            "czechia",
            "slovakia",
            "hungary",
            "romania",
            "bulgaria",
            "greece",
            "croatia",
            "serbia",
            "slovenia",
            "ukraine",
            "estonia",
            "latvia",
            "lithuania",
            "turkey",
            "israel",
            "uae",
            "united arab emirates",
            "saudi arabia",
            "qatar",
            "egypt",
            "india",
            "pakistan",
            "bangladesh",
            "sri lanka",
            "malaysia",
            "indonesia",
            "philippines",
            "vietnam",
            "thailand",
            "china",
            "taiwan",
            "japan",
            "south korea",
            "korea",
            "australia",
            "new zealand",
            "mexico",
            "brazil",
            "argentina",
            "chile",
            "colombia",
            "peru",
            "costa rica",
            "uruguay",
            "south africa",
            "nigeria",
            "kenya",
            "ghana",
            "morocco",
            "cyprus",
            "malta",
        )
    },
}
# regions wider than a country, or that name none: read, never a conflict
_WIDE_REGIONS = frozenset(
    {
        "north america",
        "america",
        "americas",
        "amer",
        "europe",
        "eu",
        "emea",
        "apac",
        "apj",
        "asia",
        "latam",
        "latin america",
        "south america",
        "central america",
        "middle east",
        "mena",
        "nordics",
        "dach",
        "benelux",
        "oceania",
        "africa",
    }
)
# a part naming an administrative area, not a city ("Suffolk County", "Toronto
# Division"); "<name> County" or "Parish" is the US way of writing it
_AREA_WORDS = re.compile(
    r"\b(county|parish|borough|division|district|municipality|province|prefecture"
    r"|governorate|mandal|oblast|metropolitan area|metro area|area)$"
)
_US_AREA_WORDS = re.compile(r"\b(county|parish)$")
# a metro's home: (country, the states or provinces it spans; empty: any)
_METRO_HOME: dict[str, tuple[str, frozenset[str]]] = {
    "nyc-metro": ("us", frozenset({"ny", "nj"})),
    "sf-bay": ("us", frozenset({"ca"})),
    "seattle-metro": ("us", frozenset({"wa"})),
    "boston-metro": ("us", frozenset({"ma"})),
    "la-metro": ("us", frozenset({"ca"})),
    "chicago-metro": ("us", frozenset({"il"})),
    "austin-metro": ("us", frozenset({"tx"})),
    "denver-metro": ("us", frozenset({"co"})),
    "dc-metro": ("us", frozenset({"dc", "va", "md"})),
    "miami-metro": ("us", frozenset({"fl"})),
    "atlanta-metro": ("us", frozenset({"ga"})),
    "dallas-metro": ("us", frozenset({"tx"})),
    "london-metro": ("uk", frozenset()),
    "toronto-metro": ("canada", frozenset({"on"})),
}
# a city part, whole, that names a metro (normalize._METROS' places, spelled out)
_METRO_CITIES: dict[str, str] = {
    "new york": "nyc-metro",
    "new york city": "nyc-metro",
    "nyc": "nyc-metro",
    "manhattan": "nyc-metro",
    "brooklyn": "nyc-metro",
    "jersey city": "nyc-metro",
    "hoboken": "nyc-metro",
    "san francisco": "sf-bay",
    "sf": "sf-bay",
    "bay area": "sf-bay",
    "sf bay area": "sf-bay",
    "san francisco bay area": "sf-bay",
    "south san francisco": "sf-bay",
    "palo alto": "sf-bay",
    "mountain view": "sf-bay",
    "seattle": "seattle-metro",
    "boston": "boston-metro",
    "los angeles": "la-metro",
    "santa monica": "la-metro",
    "chicago": "chicago-metro",
    "austin": "austin-metro",
    "denver": "denver-metro",
    "washington dc": "dc-metro",
    "dc": "dc-metro",
    "district of columbia": "dc-metro",
    "miami": "miami-metro",
    "atlanta": "atlanta-metro",
    "dallas": "dallas-metro",
    "dallas fort worth": "dallas-metro",
    "london": "london-metro",
    "city of london": "london-metro",
    "toronto": "toronto-metro",
}
# city names other places share: the metro only when the text names its state
# ("Washington, DC"; "Cambridge, MA"); "Washington" or "Cambridge" alone is not
_METRO_CITIES_NEEDING_STATE: dict[str, str] = {
    "washington": "dc-metro",
    "arlington": "dc-metro",
    "cambridge": "boston-metro",
    "newark": "nyc-metro",
    "bellevue": "seattle-metro",
    "oakland": "sf-bay",
}
_CITY_NOISE = re.compile(
    r"^(?:greater|metro|downtown)\s+|\s+(?:metropolitan area|metro area|area|metro|metroplex)$"
)
_PLACE_WORD = re.compile(r"[^\W\d_]+")
_PART_STOPWORDS = frozenset({"in", "within", "the", "of", "and", "near", "across", "any", "all"})
_DASH = re.compile(r"\s+[-–—]\s+")

Region = tuple[str, str]  # (country, state or province); "" when not named


def _city_core(city: str) -> str:
    """'greater boston area' -> 'boston'."""
    prev = None
    while prev != city:
        prev, city = city, _CITY_NOISE.sub("", city).strip()
    return city


def _metro_of(city: str) -> tuple[str, bool] | None:
    """(metro, whether the city's state must be named) for a city part."""
    for name in (city, _city_core(city)):
        if name in _METRO_CITIES:
            return _METRO_CITIES[name], False
        if name in _METRO_CITIES_NEEDING_STATE:
            return _METRO_CITIES_NEEDING_STATE[name], True
    return None


def _region_tokens(part: str) -> list[Region] | None:
    """The states, provinces and countries a part names, when that is all it
    names ('ma usa', 'california', 'suffolk county', 'emea'); None when it
    names something else (a city)."""
    if _AREA_WORDS.search(part):
        return [("us", "")] if _US_AREA_WORDS.search(part) else [("", "")]
    words = part.split()
    out: list[Region] = []
    i = 0
    while i < len(words):
        for n in (4, 3, 2, 1):
            if len(words) - i < n:
                continue
            w = " ".join(words[i : i + n])
            if w in _US_STATES or w in _US_CODES:
                out.append(("us", _US_STATES.get(w, w)))
            elif w in _CA_PROVINCES or w in _CA_CODES:
                out.append(("canada", _CA_PROVINCES.get(w, w)))
            elif w in _COUNTRIES:
                out.append((_COUNTRIES[w], ""))
            elif w in _WIDE_REGIONS:
                out.append(("", ""))
            else:
                continue
            i += n
            break
        else:
            return None
    return out


def _region(tokens: list[Region]) -> Region | None:
    """One (country, state) from a place's tokens; None when they contradict
    each other ("Austin, TX, Ontario")."""
    if any(c == "canada" for c, _ in tokens):
        tokens = [t for t in tokens if t != ("us", "ca")]  # CA: Canada here
    countries = {c for c, _ in tokens if c}
    states = {s for _, s in tokens if s}
    if len(countries) > 1 or len(states) > 1:
        return None
    country, state = next(iter(countries), ""), next(iter(states), "")
    if state and not country:
        country = "canada" if state in _CA_CODES else "us"
    return country, state


def _read_part(part: str) -> tuple[str, list[Region]]:
    """(city, region tokens) of one comma part: ('', [...]) for a part that
    names only states and countries. 'cambridge ma usa' -> ('cambridge',
    [('us', 'ma'), ('us', '')]); 'us san francisco' (from "US-San
    Francisco") -> ('san francisco', [('us', '')])."""
    if _metro_of(part) is not None:
        return part, []
    tokens = _region_tokens(part)
    if tokens is not None:
        return "", tokens
    words = part.split()
    if words and words[0] in _COUNTRIES:  # a country first: "us wa seattle"
        for k in range(len(words) - 1, 0, -1):
            lead = _region_tokens(" ".join(words[:k]))
            if lead is not None:
                city, tail = _read_part(" ".join(words[k:]))
                if city:
                    return city, [*lead, *tail]
    for k in range(1, len(words)):  # the region last: "san francisco ca"
        last = _region_tokens(" ".join(words[k:]))
        if last is not None:
            return " ".join(words[:k]), last
    return part, []


def _part_words(part: str) -> str:
    """A comma part as lowercase words, leading prepositions dropped ("in
    the us" -> "us"; "IN" on its own is Indiana, and "District of Columbia"
    keeps its "of")."""
    words = _PLACE_WORD.findall(part)
    while len(words) > 1 and words[0] in _PART_STOPWORDS:
        words = words[1:]
    return " ".join(words)


def _state_fits(city: str, tokens: list[Region], state: str) -> bool:
    """Can `state` be the state of the city read so far? "Brooklyn, New
    York": yes; "Seattle, New York" (a list of metros): no."""
    have = _region(tokens)
    if have is None or (have[1] and have[1] != state):
        return False
    metro = _metro_of(city)
    if metro is None:
        return True
    home_country, home_states = _METRO_HOME[metro[0]]
    return home_country == "us" and state in home_states


def _city_keys(place: str) -> set[str]:
    """The keys of the cities (or, with none, the state) a place names. Comma
    parts after a city are its region ("Brooklyn, New York, USA"); a metro
    after one starts the next place ("San Francisco, CA, New York City, NY");
    any other part after a city (a district, an unknown city) is not a place,
    and the region parts after it still count for the city: "Boston,
    Lincolnshire, UK" is not Boston, MA. A city whose parts name two states or
    countries gets no key ("Dallas, TX, Edmonton, AB")."""
    text = _DASH.sub(",", html.unescape(place).lower().replace(".", ""))
    text = re.sub(r"[()\[\]]", ",", text)
    parts = [_part_words(p) for p in text.split(",")]
    # (city, or '' for a region on its own; the region tokens read after it)
    groups: list[tuple[str, list[Region]]] = []
    for part in (p for p in parts if p):
        city, tokens = _read_part(part)
        cur = groups[-1] if groups else None
        if not city:
            if cur is None:
                groups.append(("", list(tokens)))
            else:
                cur[1].extend(tokens)
            continue
        if cur is not None and cur[0]:
            state = _US_STATES.get(part) or (part if part in _US_CODES else "")
            if state and _state_fits(cur[0], cur[1], state):
                cur[1].append(("us", state))  # "Brooklyn, New York": the state
                continue
            if _metro_of(city) is None:
                # a district or a county ("Paris, Île-de-France", "Sydney, NSW",
                # "Boston, Lincolnshire, UK"): not a place, and not read for a
                # region ("New South Wales" is not Wales); the region parts
                # after it are still the city's
                continue
        groups.append((city, list(tokens)))
    keys: set[str] = set()
    has_city = any(city for city, _ in groups)
    for city, tokens in groups:
        region = _region(tokens)
        if region is None:
            continue  # it names two states or countries: no place to agree on
        country, state = region
        if not city:
            if state and not has_city:
                keys.add(f"region-{country}|{state}")  # "Texas", "Maryland, USA"
            continue
        metro = _metro_of(city)
        if metro is not None:
            name, needs_state = metro
            home_country, home_states = _METRO_HOME[name]
            fits = (not country or country == home_country) and (
                not state or not home_states or state in home_states
            )
            if fits and (state or not needs_state):
                keys.add(name)
                continue
        core = "".join(_PLACE_WORD.findall(_city_core(city)))
        if core:
            keys.add(f"other-{core}|{country}|{state}")
    return keys


@dataclass(frozen=True)
class Posting:
    """One open posting on an employer board."""

    ats: str
    token: str
    external_id: str
    title: str
    location: str
    remote: bool | None
    url: str
    job_id: int | None = None  # its jobs row, when jobkit already stores it

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.ats, self.token.lower(), self.external_id.lower())

    def describe(self) -> str:
        return (
            f"{self.title.strip()} — {self.location.strip() or 'no location'} "
            f"({self.ats}/{self.token}, posting {self.external_id})"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "ats": self.ats,
            "token": self.token,
            "external_id": self.external_id,
            "title": self.title.strip(),
            "location": self.location.strip(),
            "url": self.url,
        }


@dataclass(frozen=True)
class Decision:
    """matched (one posting picked) | ambiguous (title matches, but none or
    several agree on location) | unconfirmed (one posting would be picked, but
    it is on a probe-found board nothing confirms is the company's) |
    no_title_match."""

    outcome: str
    chosen: Posting | None
    candidates: tuple[Posting, ...]  # the postings with the same title
    agreeing: int = 0
    exact: bool = False  # picked because the apply URL names its id
    reason: str = ""  # why it is flagged, when not the location count

    def why(self, location: str) -> str:
        if self.reason:
            return self.reason
        if self.outcome != "ambiguous":
            return self.outcome
        n = len(self.candidates)
        if self.agreeing == 0:
            return (
                f"{n} posting(s) with this title, none in a location that agrees with "
                f"{location.strip() or '(no location)'!r}"
            )
        return (
            f"{self.agreeing} postings with this title in a location that agrees with "
            f"{location.strip() or '(no location)'!r}"
        )


def decide(
    title: str,
    location: str,
    remote: bool | None,
    postings: list[Posting],
    exact_id: str | None = None,
) -> Decision:
    """Pick the posting a feed row or card is, or none. A posting id the apply
    URL names wins outright; otherwise the same title AND an agreeing location,
    and exactly one such posting. Never by title alone."""
    unique: dict[tuple[str, str, str], Posting] = {}
    for p in postings:
        unique.setdefault(p.key, p)
    pool = list(unique.values())
    if exact_id:
        hits = [p for p in pool if p.external_id.lower() == exact_id.lower()]
        if len(hits) == 1:
            return Decision("matched", hits[0], tuple(hits), 1, exact=True)
    key = title_key(title)
    same = [p for p in pool if key and title_key(p.title) == key]
    if not same:
        return Decision("no_title_match", None, ())
    mine = place_keys(location, remote)
    agree = [p for p in same if mine & place_keys(p.location, p.remote)]
    if len(agree) == 1:
        return Decision("matched", agree[0], tuple(same), 1)
    return Decision("ambiguous", None, tuple(same), len(agree))


# ---------------------------------------------------------------------------
# what a run looks at
# ---------------------------------------------------------------------------


@dataclass
class Item:
    """One job to resolve: an open aggregator row, or a LinkedIn card (which
    has no job row)."""

    kind: str  # job | card
    company: str
    norm: str
    title: str
    location: str
    remote: bool | None
    job_id: int | None = None
    status: str = ""
    apply_url: str = ""
    li_id: str = ""
    domain: str | None = None
    url_board: tuple[str, str] | None = None  # a board the apply URL names (no job id)
    exact_id: str | None = None  # a posting id the apply URL names (gh_jid)
    # a card's origin: LinkedIn guest discovery, or a job listed in one of the
    # person's job-alert emails (track/job_alerts.py: 'linkedin_email',
    # 'job_alert_email'; its li_id is a synthetic 'alert-...' id)
    source: str = GUEST_SOURCE

    def ref(self) -> str:
        if self.kind == "job":
            return f"job {self.job_id}"
        return card_label(self.source, self.li_id)

    def head(self) -> str:
        return (
            f"{self.ref()} {self.company} — {self.title} ({self.location.strip() or 'no location'})"
        )


def card_label(source: str, li_id: str) -> str:
    """'LinkedIn card <id>' for a LinkedIn guest card; 'job-alert email card
    <id>' for a job listed in one of the person's job-alert emails."""
    if source == GUEST_SOURCE:
        return f"LinkedIn card {li_id}"
    return f"job-alert email card {li_id}"


def _job_item(r: sqlite3.Row) -> Item:
    remote = None if r["remote"] is None else bool(r["remote"])
    return Item(
        kind="job",
        company=str(r["company_name"]),
        norm=normalize_company(str(r["company_name"])),
        title=str(r["title"]),
        location=str(r["location_raw"] or ""),
        remote=remote,
        job_id=int(r["id"]),
        status=str(r["status"]),
        apply_url=str(r["apply_url"] or ""),
        domain=r["company_domain"],
    )


def _is_open_feed_row(r: sqlite3.Row | None) -> bool:
    return r is not None and r["board_id"] is None and r["closed_at"] is None


# ---------------------------------------------------------------------------
# a run
# ---------------------------------------------------------------------------

_COUNTS = (
    "jobs",
    "cards",
    "companies",
    "requests",
    "probed",
    "cache_hits",
    "merged",
    "ingested",
    "held",
    "ambiguous",
    "no_title_match",
    "no_board",
    "deferred",
    "errors",
    "skipped",
)


@dataclass
class Report:
    """What one run did. status: ok | disabled | stopped (HTTP 429 or the
    request cap ended the probing; the rest waits for the next run)."""

    dry_run: bool = False
    status: str = "ok"
    note: str = ""
    jobs: int = 0
    cards: int = 0
    companies: int = 0
    requests: int = 0
    probed: int = 0
    cache_hits: int = 0
    merged: int = 0
    ingested: int = 0  # LinkedIn cards whose employer posting entered jobkit
    held: int = 0
    ambiguous: int = 0
    no_title_match: int = 0
    no_board: int = 0
    deferred: int = 0
    errors: int = 0
    skipped: int = 0
    lines: list[str] = field(default_factory=list)
    merged_into: dict[int, int] = field(default_factory=dict)  # feed row id -> ATS row id

    def counts(self) -> dict[str, int]:
        return {k: int(getattr(self, k)) for k in _COUNTS}

    def summary(self) -> str:
        if self.status == "disabled":
            return f"resolve boards: disabled ({self.note})"
        verb = "would merge" if self.dry_run else "merged"
        line = (
            f"resolve boards: {self.jobs} aggregator job(s), {self.cards} LinkedIn card(s); "
            f"{verb} {self.merged} into the employer's posting"
            + (f", {self.ingested} card(s) ingested" if self.ingested else "")
            + f", {self.ambiguous} flagged ({FLAG_KEY} or {UNCONFIRMED_KEY}), "
            f"{self.no_title_match} without a "
            f"matching title, {self.no_board} without a board; {self.requests} request(s)"
        )
        if self.held:
            line += f", {self.held} held (both rows carry decisions)"
        if self.deferred or self.errors:
            line += f", {self.deferred} deferred, {self.errors} error(s)"
        if self.status == "stopped":
            line += f"; stopped: {self.note}"
        return line

    def __str__(self) -> str:
        return self.summary()

    def render(self) -> str:
        head = "DRY RUN — nothing written.\n" if self.dry_run else ""
        out = [head + self.summary()]
        out += [f"  - {ln}" for ln in self.lines]
        return "\n".join(out)


class _Stop(Exception):
    """No more requests this run (HTTP 429, or the request cap)."""


class _Failed(Exception):
    """One board could not be read (network error, unexpected status or body)."""


@dataclass
class _Board:
    ats: str
    token: str
    origin: str  # known | url | registry | discovered | cache | probe
    postings: list[Posting]
    company: str = ""  # the board's own company name, when the API reports one
    domains: frozenset[str] = frozenset()  # registrable domains the board names


class _Run:
    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: BoardResolution,
        rep: Report,
        client: httpx.Client,
        sleep: Callable[[float], None],
        ats_transport: httpx.AsyncBaseTransport | None,
        dry_run: bool,
    ) -> None:
        self.conn, self.cfg, self.rep, self.client = conn, cfg, rep, client
        self.sleep, self.ats_transport, self.dry_run = sleep, ats_transport, dry_run
        self.sent = 0
        self.stopped: str | None = None
        self.fetched: dict[tuple[str, str], _Board | None] = {}
        self.polled: dict[tuple[str, str], ingest.BoardPoll] = {}
        self.carried: list[int] = []  # ATS rows that took a human status in a merge
        self.owner = owner_boards()  # (ats, token) the user listed in targets.yaml

    # ---- requests ----

    def _pace(self) -> None:
        """Before every request: commit what this run has written so far
        (events, flags, merges), so no write transaction, and so no SQLite
        write lock, is held across the delay or the request. A run sends up to
        `max_requests_per_run` requests a second apart, and MCP get_queue,
        approve and a background `tailor` write to the same DB with a 5 s busy
        timeout."""
        if not self.dry_run and self.conn.in_transaction:
            self.conn.commit()
        if self.stopped:
            raise _Stop(self.stopped)
        if self.sent >= self.cfg.max_requests_per_run:
            self.stopped = f"request cap reached ({self.cfg.max_requests_per_run} per run)"
            raise _Stop(self.stopped)
        if self.sent:
            self.sleep(self.cfg.delay_seconds)
        self.sent += 1
        self.rep.requests = self.sent

    def fetch(self, ats: str, token: str, origin: str) -> _Board | None:
        """The board's open postings (None: no such board, HTTP 404). Read
        once per run. The board comes back under the origin asked for now,
        never the origin of an earlier read: a board one row's apply URL
        named ('url') is not trusted when another company's probe reaches
        the same token ('probe')."""
        key = (ats, token.lower())
        if key in self.fetched:
            cached = self.fetched[key]
            if cached is None or cached.origin == origin:
                return cached
            return replace(cached, origin=origin)
        self._pace()
        url = LIST_URLS[ats].format(token=token)
        try:
            resp = self.client.get(url)
        except httpx.HTTPError as e:
            raise _Failed(f"{ats}/{token}: {type(e).__name__}") from e
        log("board_resolve.request", ats=ats, token=token, status=resp.status_code)
        if resp.status_code == 404:
            self.fetched[key] = None
            return None
        if resp.status_code == 429:
            self.stopped = f"{ats} answered HTTP 429 (rate limited)"
            raise _Stop(self.stopped)
        if resp.status_code != 200:
            raise _Failed(f"{ats}/{token}: HTTP {resp.status_code}")
        try:
            payload = resp.json()
            jobs = ADAPTERS[ats].parse(payload)
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise _Failed(f"{ats}/{token}: unreadable board ({type(e).__name__})") from e
        company = ""
        if isinstance(payload, dict):
            for j in payload.get("jobs") or []:
                if isinstance(j, dict) and j.get("company_name"):
                    company = str(j["company_name"])
                    break
        board = _Board(
            ats=ats,
            token=token,
            origin=origin,
            company=company,
            domains=domains_in(payload),
            postings=[
                Posting(
                    ats=ats,
                    token=token,
                    external_id=j.external_id,
                    title=j.title,
                    location=j.location_raw,
                    remote=j.remote,
                    url=j.apply_url,
                )
                for j in jobs
            ],
        )
        self.fetched[key] = board
        return board

    def poll(self, ats: str, token: str, company: str) -> ingest.BoardPoll:
        """discovery/ingest.py's poll of one board (added when new), once per run."""
        key = (ats, token.lower())
        if key not in self.polled:
            self._pace()
            self.polled[key] = asyncio.run(
                ingest.poll_board(self.conn, ats, token, company, self.ats_transport)
            )
        return self.polled[key]

    # ---- boards for a company ----

    def _known(self, item: Item, failed: list[str]) -> list[_Board]:
        """The active boards jobkit has for the company. An owner-curated
        board (config/targets.yaml, origin 'known'): its open postings from
        the DB, after a poll when the board was not polled in the last day (a
        dry run reads the list API instead). Any other board (the registry,
        discovery, add-url or an earlier match added it; origin
        'discovered'): read from its list API, which reports the board's own
        company name and domains, so its identity is checked like a probe
        hit. A board whose poll or read failed is named in `failed`."""
        out: list[_Board] = []
        for r in self.conn.execute(q.ACTIVE_BOARDS_FOR_COMPANY, {"normalized_name": item.norm}):
            ats, token = str(r["ats"]), str(r["token"])
            if ats not in ADAPTERS:
                continue
            if (ats, token.lower()) not in self.owner:
                try:
                    found = self.fetch(ats, token, "discovered")
                except _Failed as e:
                    failed.append(f"{ats}/{token} ({e})")
                    continue
                if found is None:
                    failed.append(f"{ats}/{token} (no such board: HTTP 404)")
                else:
                    out.append(found)
                continue
            if not r["fresh"]:
                if self.dry_run:
                    board = self.fetch(ats, token, "known")
                    if board is not None:
                        out.append(board)
                    continue
                if not self.poll(ats, token, item.company).ok:
                    failed.append(f"{ats}/{token}")
                    continue
            rows = self.conn.execute(q.OPEN_POSTINGS_ON_BOARD, {"board_id": r["board_id"]})
            out.append(
                _Board(
                    ats=ats,
                    token=token,
                    origin="known",
                    postings=[
                        Posting(
                            ats=ats,
                            token=token,
                            external_id=str(p["external_id"]),
                            title=str(p["title"]),
                            location=str(p["location_raw"] or ""),
                            remote=None if p["remote"] is None else bool(p["remote"]),
                            url=str(p["apply_url"] or ""),
                            job_id=int(p["id"]),
                        )
                        for p in rows
                    ],
                )
            )
        return out

    def _registry(self, items: list[Item]) -> list[tuple[str, str]]:
        """Registry boards under the company name, or its domain label."""
        found: list[tuple[str, str]] = []
        names: list[str] = []
        for it in items:
            for n in (it.company, name_label(it.company), domain_label(it.domain)):
                if n and n not in names:
                    names.append(n)
        for n in names:
            hit = registry.lookup(n)
            if hit is not None and hit[0] in ADAPTERS and hit not in found:
                found.append(hit)
        return found

    def _trusted(self, items: list[Item], failed: list[str]) -> list[_Board]:
        """Tier A: the company's boards in jobkit, the boards its apply URLs
        name, and its registry boards. A board of the company's in jobkit
        that could not be polled is named in `failed`. A board no item
        trusts (_trusts) passes the same name guard as a probe hit: a
        Greenhouse board whose own company name does not fit is dropped.
        _decide_all applies the guard again for each item, so a board one
        row's apply URL names is checked for every other row."""
        boards = self._known(items[0], failed)
        have = {(b.ats, b.token.lower()) for b in boards}
        pairs = [(*it.url_board, "url") for it in items if it.url_board]
        pairs += [(ats, token, "registry") for ats, token in self._registry(items)]
        for ats, token, origin in pairs:
            if (ats, token.lower()) in have:
                continue
            have.add((ats, token.lower()))
            board = self.fetch(ats, token, origin)
            if board is not None:
                boards.append(board)
        kept = [b for b in boards if any(self._fits(it, b) for it in items)]
        ids = {id(b) for b in kept}
        self._guarded(items[0], [b for b in boards if id(b) not in ids])  # logs the mismatches
        return kept

    def _probe(self, items: list[Item], seen: set[tuple[str, str]]) -> tuple[list[Item], bool]:
        """Tier B: the cached probe result, or a fresh slug probe (name tier,
        then the suffix/prefix tier only for what is still unmatched). Returns
        the items still without a title match, and whether any board was
        found. Raises _Stop when the run must stop sending requests."""
        open_items = list(items)
        norm, name, domain = items[0].norm, items[0].company, items[0].domain
        cached = self._cached_probe(norm)
        boards: list[_Board] = []
        if cached is not None:
            self.rep.cache_hits += 1
            for ats, token in cached.get("found") or []:
                if ats in ADAPTERS and (ats, str(token).lower()) not in seen:
                    board = self.fetch(ats, str(token), "cache")
                    if board is not None:
                        boards.append(board)
            open_items = self._decide_all(open_items, self._guarded(items[0], boards))
            return open_items, bool(boards)
        self.rep.probed += 1
        complete, tried = True, 0
        for tier in slug_tiers(name, domain):
            for ats in ATS_ORDER:
                for token in tokens_for(ats, tier):
                    if (ats, token) in seen:
                        continue
                    seen.add((ats, token))
                    tried += 1
                    try:
                        board = self.fetch(ats, token, "probe")
                    except _Failed as e:
                        complete = False
                        log("board_resolve.probe_failed", company=name, error=str(e))
                        continue
                    if board is not None:
                        boards.append(board)
            open_items = self._decide_all(open_items, self._guarded(items[0], boards))
            if not open_items:
                break  # everything decided: the suffix tier is not needed
        if complete:
            found = [[b.ats, b.token] for b in boards]
            self._event(
                None,
                q.BOARD_PROBE_EVENT,
                {"company": norm, "name": name, "found": found, "tokens_tried": tried},
            )
            log("board_resolve.probed", company=name, found=found, tried=tried)
        elif open_items:
            self._error(open_items, "some board requests failed; the probe is retried next run")
            return [], bool(boards)
        return open_items, bool(boards)

    def _cached_probe(self, norm: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            q.LATEST_BOARD_PROBE,
            {"normalized_name": norm, "window": f"-{self.cfg.probe_cache_days} days"},
        ).fetchone()
        if row is None:
            return None
        detail = json.loads(row["detail"] or "{}")
        return detail if isinstance(detail, dict) else None

    # ---- one company ----

    def company(self, items: list[Item]) -> None:
        """Resolve every item of one company: tier A (trusted boards), then
        tier B (the slug probe) only when tier A found no board at all: the
        probe reaches boards under the company's name, which for a company
        whose own board jobkit knows can only be other companies' (tyrellhq,
        thetyrell, ... for Tyrell). An item is matched, flagged, or left with its
        outcome."""
        self.rep.companies += 1
        norm = items[0].norm
        before = self.rep.merged + self.rep.ingested
        unpolled: list[str] = []
        try:
            trusted = self._trusted(items, unpolled)
        except _Stop:
            self._defer(items)
            return
        except _Failed as e:
            self._error(items, str(e))
            return
        open_items = self._decide_all(list(items), trusted)
        probed_any = False
        if open_items and not trusted and not unpolled:
            try:
                open_items, probed_any = self._probe(open_items, set())
            except _Stop:
                self._defer(open_items)
                return
            except _Failed as e:  # a cached board could not be read
                self._error(open_items, str(e))
                return
        if open_items and unpolled:
            self._error(
                open_items,
                f"the company's board {', '.join(unpolled)} could not be polled (no probe "
                "for a company whose board jobkit knows)",
            )
            return
        for it in open_items:
            if trusted:
                self._outcome(
                    it,
                    "no_title_match",
                    "no posting with this title on the company's own board(s) "
                    f"({', '.join(sorted({f'{b.ats}/{b.token}' for b in trusted}))}); "
                    "no slug probe for a company whose board jobkit knows",
                )
            elif probed_any:
                self._outcome(
                    it,
                    "no_title_match",
                    "no posting with this title on the board(s) the probe found",
                )
            else:
                self._outcome(it, "no_board", "no Greenhouse, Lever or Ashby board found")
        if self.rep.merged + self.rep.ingested > before:
            self._mark_discovery(norm, "found")
        elif not (trusted or probed_any) and open_items:
            self._mark_discovery(norm, "not_found")

    @staticmethod
    def _trusts(item: Item, board: _Board) -> bool:
        """The board is known to be this item's company's: an owner-curated
        board ('known'), or the board this item's OWN apply URL names. A
        board another row's apply URL names ('url') is not: that row's URL
        says nothing about this row (review 2026-10-15)."""
        if board.origin == "known":
            return True
        if board.origin != "url" or item.url_board is None:
            return False
        ats, token = item.url_board
        return (ats, token.lower()) == (board.ats, board.token.lower())

    def _fits(self, item: Item, board: _Board) -> bool:
        """The board may hold this item's posting: the item trusts it, or
        its own company name fits the item's (names_compatible)."""
        return self._trusts(item, board) or names_compatible(item.company, board.company)

    def _guarded(self, item: Item, boards: list[_Board]) -> list[_Board]:
        """Boards this item does not trust (found by name: probe, registry,
        discovered, or another row's apply URL) whose own company name does
        not fit at all are dropped ("Tyrell" vs "Tyrellium Corp"); one that fits
        but is not the same name, or reports none, stays, and _evidence
        decides whether its posting may be picked."""
        keep: list[_Board] = []
        for b in boards:
            if self._fits(item, b):
                keep.append(b)
            else:
                log(
                    "board_resolve.name_mismatch",
                    company=item.company,
                    board=f"{b.ats}/{b.token}",
                    board_company=b.company,
                )
        return keep

    def _evidence(self, it: Item, board: _Board) -> tuple[bool, str]:
        """Is this board the company's? (True, what confirms it) for an
        owner-curated board or the board this item's own apply URL names
        (_trusts), a Greenhouse board that reports the same company name, or
        a board that names a domain of the company; otherwise (False, why
        not). A board the registry named, one discovery or add-url added,
        one the probe found, or one another row's apply URL names is
        otherwise only a board under the company's name, and another company
        can own that token."""
        if self._trusts(it, board):
            return True, f"{board.origin} board"
        if board.company and same_company_name(it.company, board.company):
            return True, f"{ATS_LABEL[board.ats]} names the company {board.company!r}"
        shared = sorted(company_domains(it.company, it.domain, it.apply_url) & board.domains)
        if shared:
            return True, f"the board names {shared[0]}"
        if board.company:
            return False, f"it calls itself {board.company!r}, not {it.company!r}"
        return False, (
            f"{ATS_LABEL[board.ats]} reports no company name, and no domain of "
            f"{it.company!r} appears on the board"
        )

    def _decide_all(self, items: list[Item], boards: list[_Board]) -> list[Item]:
        """Decide every item over these boards; return those with no title
        match (they go on to the next tier). A posting on a board this item
        does not trust (probe-found, registry, discovered, or another row's
        apply URL) that nothing confirms is the company's is flagged, never
        picked; a board whose own name does not fit the item's company at
        all is not looked at for that item."""
        if not boards:
            return items
        by_key = {(b.ats, b.token.lower()): b for b in boards}
        rest: list[Item] = []
        for it in items:
            postings = [p for b in boards if self._fits(it, b) for p in b.postings]
            d = decide(it.title, it.location, it.remote, postings, it.exact_id)
            if d.outcome == "no_title_match":
                rest.append(it)
                continue
            if d.outcome != "matched" or d.chosen is None:
                self._flag(it, d)
                continue
            board = by_key.get((d.chosen.ats, d.chosen.token.lower()))
            if board is None:  # cannot happen: every posting comes from a board here
                ok, evidence = False, "the posting's board is unknown"
            elif d.exact:  # a Greenhouse job id is unique across Greenhouse
                ok, evidence = True, "the apply URL names this posting id"
            else:
                ok, evidence = self._evidence(it, board)
            if ok and board is not None:
                self._act(it, d.chosen, board.origin, evidence, self._trusts(it, board))
                continue
            where = f"{d.chosen.ats}/{d.chosen.token}"
            log(
                "board_resolve.unconfirmed",
                ref=it.ref(),
                company=it.company,
                board=where,
                why=evidence,
            )
            self._flag(
                it,
                replace(
                    d,
                    outcome="unconfirmed",
                    chosen=None,
                    reason=(
                        f"the one posting with this title in a location that agrees is on "
                        f"{where}, a board found under the company's name only "
                        f"({_ORIGIN_SAYS.get(board.origin if board else '', 'by name')}); "
                        f"nothing on it confirms it belongs to {it.company} ({evidence}), so "
                        "it was not picked"
                    ),
                ),
            )
        return rest

    # ---- outcomes ----

    def _event(self, job_id: int | None, kind: str, detail: dict[str, Any]) -> None:
        if self.dry_run:
            return
        self.conn.execute(
            q.RECORD_EVENT,
            {"job_id": job_id, "kind": kind, "detail": json.dumps(detail, sort_keys=True)},
        )

    def _card_event(self, it: Item, outcome: str, job_id: int | None = None, **extra: Any) -> None:
        detail = {
            "li_id": it.li_id,
            "company": it.company,
            "title": it.title,
            "location": it.location,
            "outcome": outcome,
            **extra,
        }
        self._event(job_id, q.BOARD_RESOLVE_CARD_EVENT, detail)

    def _outcome(self, it: Item, outcome: str, why: str) -> None:
        setattr(self.rep, outcome, getattr(self.rep, outcome) + 1)
        self.rep.lines.append(f"{it.head()}: {why}")
        if it.kind == "card":
            self._card_event(it, outcome)
        else:
            self._clear_flag(it)
        log("board_resolve.outcome", ref=it.ref(), company=it.company, outcome=outcome)

    def _defer(self, items: list[Item]) -> None:
        for it in items:
            self.rep.deferred += 1
            self.rep.lines.append(f"{it.head()}: deferred ({self.stopped}); next run")
        self.rep.status = "stopped"
        self.rep.note = self.stopped or ""

    def _error(self, items: list[Item], why: str) -> None:
        for it in items:
            self.rep.errors += 1
            self.rep.lines.append(f"{it.head()}: not resolved ({why}); next run tries again")
            log("board_resolve.error", ref=it.ref(), error=why)

    def _mark_discovery(self, norm: str, status: str) -> None:
        if not self.dry_run and norm:
            self.conn.execute(q.MARK_DISCOVERY_ATTEMPT, {"status": status, "normalized_name": norm})

    def _flag_state(self, job_id: int) -> str | None:
        """The detail of the job's standing flag, or None."""
        st = self.conn.execute(q.BOARD_FLAG_STATE, {"job_id": job_id}).fetchone()
        if st is None or st["flag_id"] is None:
            return None
        if st["clear_id"] is not None and st["clear_id"] > st["flag_id"]:
            return None
        row = self.conn.execute(q.EVENT_DETAIL, {"event_id": st["flag_id"]}).fetchone()
        return str(row["detail"]) if row is not None else None

    def _clear_flag(self, it: Item) -> None:
        if self.dry_run or it.job_id is None:
            return
        if self._flag_state(it.job_id) is not None:
            self._event(it.job_id, q.BOARD_MATCH_CLEARED_EVENT, {"reason": "no longer ambiguous"})

    def _flag(self, it: Item, d: Decision) -> None:
        self.rep.ambiguous += 1
        why = d.why(it.location)
        key = UNCONFIRMED_KEY if d.outcome == "unconfirmed" else FLAG_KEY
        detail: dict[str, Any] = {
            "company": it.company,
            "title": it.title,
            "location": it.location,
            "why": why,
            "flag": key,
            "candidates": [p.as_dict() for p in d.candidates],
        }
        listed = "; ".join(p.describe() for p in d.candidates)
        self.rep.lines.append(f"{it.head()}: {key}: {why}. Candidates: {listed}")
        log("board_resolve.ambiguous", ref=it.ref(), company=it.company, why=why)
        if self.dry_run:
            return
        if it.kind == "card":
            detail.update(source=it.source, li_id=it.li_id)
            self._event(None, q.BOARD_MATCH_AMBIGUOUS_EVENT, detail)
            self._card_event(it, "ambiguous", candidates=len(d.candidates))
            return
        assert it.job_id is not None
        text = json.dumps(detail, sort_keys=True)
        if self._flag_state(it.job_id) != text:
            self._event(it.job_id, q.BOARD_MATCH_AMBIGUOUS_EVENT, detail)

    def _act(self, it: Item, p: Posting, origin: str, evidence: str, trusted: bool) -> None:
        where = f"{p.ats}/{p.token}"
        how = f"{p.title.strip()} — {p.location.strip() or 'no location'} ({where}, {origin} board)"
        if not trusted:
            how = f"{how[:-1]}: {evidence})"
        if self.dry_run:
            if it.kind == "card":
                self.rep.ingested += 1
                self.rep.lines.append(f"{it.head()}: would ingest {how}")
            else:
                self.rep.merged += 1
                self.rep.lines.append(f"{it.head()}: would merge into {how}")
            return
        ats_id = p.job_id
        if ats_id is None:
            existing = self.conn.execute(
                q.BOARD_BY_ATS_TOKEN_NOCASE, {"ats": p.ats, "token": p.token}
            ).fetchone()
            token = str(existing["token"]) if existing is not None else p.token
            try:
                polled = self.poll(p.ats, token, it.company)
            except _Stop:
                self._defer([it])
                return
            if not polled.ok:
                self._error([it], f"{where} could not be polled")
                return
            if polled.board_added and origin in ("probe", "cache"):
                self.conn.execute(
                    q.SET_BOARD_CONFIDENCE, {"confidence": "probe", "board_id": polled.board_id}
                )
            row = ingest.job_on_board(self.conn, polled.board_id, p.external_id)
            if row is None:
                self._error([it], f"posting {p.external_id} was not in {where}'s feed")
                return
            ats_id = int(row["id"])
        if it.kind == "card":
            self.rep.ingested += 1
            self.rep.lines.append(f"{it.head()}: the employer's posting is job {ats_id}: {how}")
            self._card_event(it, "ingested", job_id=ats_id, ats=p.ats, token=p.token)
            log("board_resolve.card_ingested", li_id=it.li_id, ats_job_id=ats_id)
            return
        assert it.job_id is not None
        result = merge_feed_row(self.conn, it.job_id, ats_id)
        detail: dict[str, Any] = {
            "ats_job_id": ats_id,
            "ats": p.ats,
            "token": p.token,
            "external_id": p.external_id,
            "title": p.title.strip(),
            "location": p.location.strip(),
            "board": origin,
            "evidence": evidence,
        }
        if result == "skipped":
            # the board poll above (pipeline/poll.upsert_jobs) merges a feed row
            # whose apply URL names a posting on the board, e.g. a gh_jid link
            target = merged_target(self.conn, it.job_id)
            if target is not None:
                result = "merged"
                if target != ats_id:
                    how = f"the posting its apply URL names (merged by the poll of {where})"
                    detail = {"ats_job_id": target, "board": origin, "by": "poll"}
                    ats_id = target
        if result == "merged":
            self.rep.merged += 1
            self.rep.merged_into[it.job_id] = ats_id
            self.rep.lines.append(f"{it.head()}: merged into job {ats_id}: {how}")
            self._event(it.job_id, q.BOARD_RESOLVED_EVENT, detail)
            status = self.conn.execute(q.JOB_STATUS, {"job_id": ats_id}).fetchone()
            if status is not None and status["status"] in CARRIED_NEEDS_TRIAGE:
                self.carried.append(ats_id)
            log("board_resolve.merged", feed_job_id=it.job_id, ats_job_id=ats_id, board=where)
        elif result == "held":
            self.rep.held += 1
            self.rep.lines.append(
                f"{it.head()}: matched job {ats_id} ({how}), but both rows carry an "
                "application or a decision: held for you (merge_held event)"
            )
        else:
            self.rep.skipped += 1
            self.rep.lines.append(f"{it.head()}: matched job {ats_id}, nothing to merge")

    # ---- the apply URL names the posting ----

    def via_url(self, it: Item) -> None:
        """The add-url path: poll the board the apply URL names; the poll
        merges the feed row (pipeline/dedup.py), or it is merged here."""
        assert it.job_id is not None
        if self.dry_run:
            self.rep.merged += 1
            self.rep.lines.append(
                f"{it.head()}: would ingest the posting its apply URL names and merge into it"
            )
            return
        try:
            self._pace()
        except _Stop:
            self._defer([it])
            return
        res = asyncio.run(
            ingest.ingest_url(self.conn, it.apply_url, it.company, transport=self.ats_transport)
        )
        if res.job is None:
            if res.status == "job_not_in_feed":
                self._outcome(it, "no_title_match", "the posting its apply URL names is closed")
            else:
                self._error([it], f"{res.status} for {it.apply_url}")
            return
        ats_id = int(res.job["id"])
        state = self.conn.execute(q.JOB_MERGE_STATE, {"job_id": it.job_id}).fetchone()
        if state is not None and state["closed_at"] is None:
            merge_feed_row(self.conn, it.job_id, ats_id)
            state = self.conn.execute(q.JOB_MERGE_STATE, {"job_id": it.job_id}).fetchone()
        if state is not None and state["closed_at"] is not None:
            ats_id = merged_target(self.conn, it.job_id) or ats_id
            self.rep.merged += 1
            self.rep.merged_into[it.job_id] = ats_id
            self.rep.lines.append(f"{it.head()}: merged into job {ats_id} (its apply URL)")
            self._event(it.job_id, q.BOARD_RESOLVED_EVENT, {"ats_job_id": ats_id, "board": "url"})
            row = self.conn.execute(q.JOB_STATUS, {"job_id": ats_id}).fetchone()
            if row is not None and row["status"] in CARRIED_NEEDS_TRIAGE:
                self.carried.append(ats_id)
        else:
            self.rep.held += 1
            self.rep.lines.append(
                f"{it.head()}: its apply URL names job {ats_id}, but both rows carry an "
                "application or a decision: held for you"
            )


def _feed_items(
    conn: sqlite3.Connection,
    filters: Filters,
    job_ids: list[int] | None,
    approved_only: bool,
    rep: Report,
) -> list[Item]:
    if job_ids is not None:
        rows: list[sqlite3.Row] = []
        for jid in job_ids:
            r = conn.execute(q.FEED_ROW_FOR_RESOLVE, {"job_id": jid}).fetchone()
            if not _is_open_feed_row(r):
                rep.skipped += 1
                rep.lines.append(
                    f"job {jid}: not an open aggregator row (it has a board, is closed, "
                    "or does not exist): nothing to resolve"
                )
                continue
            assert r is not None
            rows.append(r)
    else:
        rows = conn.execute(q.FEED_ROWS_TO_RESOLVE).fetchall()
    items: list[Item] = []
    for r in rows:
        if approved_only and r["status"] != "approved":
            continue
        if job_ids is None and r["status"] == "new" and reject_reason_now(dict(r), filters):
            continue  # the filter will reject it: no request is spent on it
        items.append(_job_item(r))
    return items


def _card_items(conn: sqlite3.Connection, cfg: BoardResolution) -> list[Item]:
    done = {
        str(r["li_id"])
        for r in conn.execute(
            q.CARDS_RESOLVED_SINCE,
            {"window": f"-{max(cfg.probe_cache_days, CARD_WINDOW_DAYS)} days"},
        )
    }
    items: list[Item] = []
    window = {"window": f"-{CARD_WINDOW_DAYS} days"}
    # LinkedIn guest cards first, then the jobs listed in job-alert emails
    # (the same shape; their source names the email)
    for query, alert in ((q.LINKEDIN_QUEUED_CARDS_SINCE, False), (q.JOB_ALERT_CARDS_SINCE, True)):
        for r in conn.execute(query, window):
            d = json.loads(r["detail"] or "{}")
            li_id = str(d.get("id") or "")
            company, title = str(d.get("company") or ""), str(d.get("title") or "")
            if not li_id or li_id in done or not company or not title:
                continue
            done.add(li_id)
            items.append(
                Item(
                    kind="card",
                    company=company,
                    norm=normalize_company(company),
                    title=title,
                    location=str(d.get("location") or ""),
                    remote=None,
                    li_id=li_id,
                    source=str(d.get("queue_source") or "job_alert_email")
                    if alert
                    else GUEST_SOURCE,
                )
            )
    return items


def owner_boards() -> frozenset[tuple[str, str]]:
    """(ats, lowercased token) of every board the user listed in
    config/targets.yaml: the only boards in jobkit trusted to be the
    company's without evidence. A config that will not load trusts none
    (fail closed: every board then needs evidence)."""
    try:
        targets = load_targets()
    except Exception as e:  # noqa: BLE001 — no owner list: nothing is trusted
        log("board_resolve.targets_unavailable", error=str(e)[:150])
        return frozenset()
    return frozenset(
        (c.board.ats, c.board.token.lower()) for c in targets.companies if c.board is not None
    )


def make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """The probe client: jobkit's honest User-Agent, no proxy from the
    environment, no redirects followed."""
    return httpx.Client(
        headers={"User-Agent": user_agent(), "Accept": "application/json"},
        follow_redirects=False,
        trust_env=False,
        timeout=20.0,
        transport=transport,
    )


def resolve(
    conn: sqlite3.Connection,
    filters: Filters | None = None,
    *,
    job_ids: list[int] | None = None,
    approved_only: bool = False,
    cards: bool = True,
    dry_run: bool = False,
    transport: httpx.BaseTransport | None = None,
    ats_transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Report:
    """One resolution run (see the module docstring). `job_ids`: only these
    aggregator rows (any status); `approved_only`: only approved ones (the
    approve-time pass); `cards`: also the LinkedIn cards whose company was
    queued (the nightly stage). `dry_run` reads the board APIs and writes
    nothing. Never raises for a failed request: the Report says what
    happened. `transport` / `ats_transport` are for tests."""
    filters = filters or load_filters()
    cfg = filters.board_resolution
    rep = Report(dry_run=dry_run)
    if not cfg.enabled:
        rep.status, rep.note = "disabled", "board_resolution.enabled is false"
        return rep
    items = _feed_items(conn, filters, job_ids, approved_only, rep)
    if cards and job_ids is None and not approved_only:
        items += _card_items(conn, cfg)
    rep.jobs = sum(1 for it in items if it.kind == "job")
    rep.cards = sum(1 for it in items if it.kind == "card")
    if items:
        with make_client(transport) as client:
            run = _Run(conn, cfg, rep, client, sleep, ats_transport, dry_run)
            by_company: dict[str, list[Item]] = {}
            for it in items:
                if (
                    not it.norm
                    or conn.execute(q.COMPANY_BLOCKLISTED, {"normalized_name": it.norm}).fetchone()
                ):
                    rep.skipped += 1
                    rep.lines.append(f"{it.head()}: no company name, or blocklisted: skipped")
                    continue
                # pinned to the systems this skip has always used: a row whose
                # link is on a newly recognised system (Taleo, Jobvite, ...) is
                # still name-matched, because companies change ATS
                hit = resolve_for_board_matching(it.apply_url) if it.kind == "job" else None
                if hit is not None and not hit.supported:
                    rep.skipped += 1
                    rep.lines.append(
                        f"{it.head()}: its apply URL is on {hit.ats}, which jobkit does not "
                        "poll: left as it is"
                    )
                    continue
                if (
                    hit is not None
                    and hit.token
                    and hit.ats in ADAPTERS
                    and hit.external_id
                    and not run.stopped
                ):
                    run.via_url(it)
                    continue
                if hit is not None and hit.token and hit.ats in ADAPTERS:
                    it.url_board = (hit.ats, hit.token)
                pk = posting_key(it.apply_url) if it.kind == "job" else None
                if pk is not None:
                    it.exact_id = pk[2]
                by_company.setdefault(it.norm, []).append(it)
            for group in by_company.values():
                if run.stopped:
                    run._defer(group)
                    continue
                run.company(group)
            if run.carried and not dry_run:
                _triage_carried(conn, run.carried, rep)
    if not dry_run:
        conn.execute(
            q.RECORD_EVENT,
            {
                "job_id": None,
                "kind": q.BOARD_RESOLVE_RUN_EVENT,
                "detail": json.dumps({"status": rep.status, **rep.counts()}),
            },
        )
        conn.commit()
    log("board_resolve.run", dry_run=dry_run, status=rep.status, note=rep.note, **rep.counts())
    return rep


def _triage_carried(conn: sqlite3.Connection, job_ids: list[int], rep: Report) -> None:
    """An ATS row that took a human status in a merge has no requirements or
    score: extract and score it now (it is past the triage stages)."""
    from jobkit.pipeline import triage

    conn.commit()
    try:
        scored = triage.extract_and_score(conn, sorted(set(job_ids)))
    except Exception as e:  # noqa: BLE001 — the merge stands; tailoring copes without
        log("board_resolve.triage_failed", job_ids=job_ids, error=str(e)[:300])
        rep.lines.append(f"extract/score of merged job(s) {sorted(set(job_ids))} failed: {e}")
        return
    if scored:
        rep.lines.append(
            "extracted and scored the merged job(s) that kept your decision: "
            + ", ".join(f"job {j} ({s})" for j, s in sorted(scored.items()))
        )


def run_stage(conn: sqlite3.Connection) -> Report:
    """The nightly / run-all stage `resolve-boards`."""
    return resolve(conn)


def before_tailoring(
    conn: sqlite3.Connection,
    job_id: int | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    ats_transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[Report | None, int | None]:
    """The approve-time pass (`jobkit tailor`, which MCP approve starts): an
    approved aggregator job still pointing at the aggregator is matched to
    the employer's posting first, so tailoring and staging use the ATS row.
    `job_id` (`tailor --job-id N`): that job only, and the id to tailor comes
    back (the ATS row's, when it was merged now or earlier). No aggregator
    job: no request, (None, job_id)."""
    if job_id:
        row = conn.execute(q.FEED_ROW_FOR_RESOLVE, {"job_id": job_id}).fetchone()
        if not _is_open_feed_row(row):
            # an aggregator row merged earlier (nightly, a poll, a pick):
            # its ATS row carries the application now
            return None, follow_merges(conn, job_id)
        rep = resolve(
            conn,
            job_ids=[job_id],
            cards=False,
            transport=transport,
            ats_transport=ats_transport,
            sleep=sleep,
        )
        return rep, follow_merges(conn, job_id)
    approved = [
        r for r in conn.execute(q.FEED_ROWS_TO_RESOLVE).fetchall() if r["status"] == "approved"
    ]
    if not approved:
        return None, None
    rep = resolve(
        conn,
        approved_only=True,
        cards=False,
        transport=transport,
        ats_transport=ats_transport,
        sleep=sleep,
    )
    return rep, None


# ---------------------------------------------------------------------------
# the human's pick, and what is flagged for them
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Flag:
    job_id: int | None  # None: a LinkedIn card
    company: str
    title: str
    location: str
    why: str
    candidates: list[dict[str, Any]]
    at: str
    li_id: str = ""
    key: str = FLAG_KEY  # board-match-ambiguous | board-match-unconfirmed
    source: str = GUEST_SOURCE  # a card's origin (Item.source)

    def line(self) -> str:
        cands = "; ".join(
            f"posting {c.get('external_id')}: {c.get('title')} — {c.get('location') or '?'} "
            f"({c.get('ats')}/{c.get('token')}) {c.get('url') or ''}".rstrip()
            for c in self.candidates
        )
        if self.job_id is not None:
            how = (
                f"pick one: `jobkit resolve-boards --job-id {self.job_id} --pick <posting id>` "
                f"(MCP resolve_boards(job_id={self.job_id}, pick=...))"
            )
            head = f"job {self.job_id} {self.company} — {self.title}"
        else:
            how = "if one is the job, add it with add_job_url(url) (`jobkit add-url`)"
            head = f"{card_label(self.source, self.li_id)}: {self.company} — {self.title}"
        return (
            f"{head} ({self.location or 'no location'}) [{self.key}]: {self.why}. "
            f"Candidates: {cands}. {how}"
        )


def flags(conn: sqlite3.Connection) -> list[Flag]:
    """Standing board-match-ambiguous flags: open aggregator rows, then
    LinkedIn cards flagged in the last CARD_FLAG_DAYS days."""
    out: list[Flag] = []
    for r in conn.execute(q.BOARD_MATCH_FLAGS):
        d = json.loads(r["detail"] or "{}")
        out.append(
            Flag(
                job_id=int(r["job_id"]),
                company=str(r["company_name"]),
                title=str(r["title"]),
                location=str(r["location_raw"] or ""),
                why=str(d.get("why") or ""),
                candidates=list(d.get("candidates") or []),
                at=str(r["at"]),
                key=str(d.get("flag") or FLAG_KEY),
            )
        )
    for r in conn.execute(q.CARD_FLAGS_SINCE, {"window": f"-{CARD_FLAG_DAYS} days"}):
        d = json.loads(r["detail"] or "{}")
        out.append(
            Flag(
                job_id=None,
                company=str(d.get("company") or ""),
                title=str(d.get("title") or ""),
                location=str(d.get("location") or ""),
                why=str(d.get("why") or ""),
                candidates=list(d.get("candidates") or []),
                at=str(r["at"]),
                li_id=str(d.get("li_id") or ""),
                key=str(d.get("flag") or FLAG_KEY),
                source=str(d.get("source") or GUEST_SOURCE),
            )
        )
    return out


def queue_line(conn: sqlite3.Connection) -> str | None:
    """One warning line for MCP get_queue, or None."""
    n = len(flags(conn))
    if not n:
        return None
    return (
        f"⚠ {n} aggregator job(s) or LinkedIn card(s) flagged {FLAG_KEY} or "
        f"{UNCONFIRMED_KEY}: a board has the title, but no posting was picked (none or "
        "several agree on location, or the board was found by name only and nothing on "
        "it confirms it is the company's). resolve_boards() lists the candidates; pick with "
        "resolve_boards(job_id=N, pick=<posting id>)."
    )


def digest_lines(conn: sqlite3.Connection, day: str | None = None) -> list[str]:
    """The digest section: today's runs summed, and every standing flag."""
    standing = flags(conn)
    day = day or datetime.date.today().isoformat()
    runs = [
        json.loads(r["detail"] or "{}")
        for r in conn.execute(q.BOARD_RESOLVE_RUNS_ON_DAY, {"day": day})
    ]
    total = {
        k: sum(int(r.get(k, 0)) for r in runs)
        for k in ("merged", "ingested", "no_board", "no_title_match", "errors", "deferred")
    }
    if not standing and not any(total.values()):
        return []  # nothing resolved, nothing waiting: no section
    lines = ["## Aggregator jobs → the employer's own board"]
    if any(total.values()):
        line = (
            f"Today: {total['merged']} aggregator job(s) merged into the employer's posting, "
            f"{total['ingested']} LinkedIn card(s) found on the employer's board, "
            f"{total['no_title_match']} with no posting of that title on the board, "
            f"{total['no_board']} with no Greenhouse/Lever/Ashby board found."
        )
        if total["errors"] or total["deferred"]:
            line += (
                f" {total['errors']} failed and {total['deferred']} were deferred (a request "
                "failed, HTTP 429 or the request cap): the next run tries again."
            )
        lines.append(line)
    if standing:
        lines.append(
            f"⚠ {len(standing)} flagged {FLAG_KEY} or {UNCONFIRMED_KEY} (nothing was "
            "picked; a title alone never decides, and a board found by name only must be "
            "confirmed):"
        )
        lines.extend(f"- {f.line()}" for f in standing)
    lines.append("")
    return lines


def overview(conn: sqlite3.Connection, filters: Filters | None = None) -> str:
    """What a run would look at and what is flagged, from the DB alone (no
    request): MCP resolve_boards without confirm."""
    filters = filters or load_filters()
    cfg = filters.board_resolution
    lines = [
        "board resolution: "
        + ("on" if cfg.enabled else "OFF (board_resolution.enabled is false)")
        + f"; a company is slug-probed at most once per {cfg.probe_cache_days} days, "
        f"{cfg.delay_seconds:g}s between requests, at most {cfg.max_requests_per_run} per run"
    ]
    rep = Report(dry_run=True)
    items = _feed_items(conn, filters, None, False, rep) + _card_items(conn, cfg)
    window = f"-{cfg.probe_cache_days} days"
    probed = int(conn.execute(q.PROBES_SINCE, {"window": window}).fetchone()["n"])
    lines.append(
        f"a run would look at {sum(1 for i in items if i.kind == 'job')} aggregator job(s) "
        f"and {sum(1 for i in items if i.kind == 'card')} LinkedIn card(s); "
        f"{probed} compan(ies) probed in the last {cfg.probe_cache_days} days (cached)"
    )
    lines += [f"  - {it.head()} [{it.status or 'card'}]" for it in items[:30]]
    if len(items) > 30:
        lines.append(f"  (+{len(items) - 30} more)")
    standing = flags(conn)
    if standing:
        lines.append(f"⚠ flagged {FLAG_KEY} or {UNCONFIRMED_KEY} ({len(standing)}):")
        lines.extend(f"  - {f.line()}" for f in standing)
    else:
        lines.append(f"flagged {FLAG_KEY} or {UNCONFIRMED_KEY}: none")
    return "\n".join(lines)


def pick(
    conn: sqlite3.Connection,
    job_id: int,
    choice: str,
    *,
    ats_transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[bool, str]:
    """The human's pick for an aggregator job: one of its flagged candidates
    (by posting id or URL), or any Greenhouse/Lever/Ashby posting URL. The
    board is polled through discovery/ingest.py and the feed row merged into
    the posting's ATS row. Returns (merged, what happened)."""
    row = conn.execute(q.FEED_ROW_FOR_RESOLVE, {"job_id": job_id}).fetchone()
    if not _is_open_feed_row(row):
        return False, f"job {job_id} is not an open aggregator row: nothing to pick for"
    assert row is not None
    choice = choice.strip()
    target: tuple[str, str, str] | None = None
    for f in flags(conn):
        if f.job_id != job_id:
            continue
        for c in f.candidates:
            if choice and choice in (str(c.get("external_id")), str(c.get("url"))):
                target = (str(c["ats"]), str(c["token"]), str(c["external_id"]))
    if target is None:
        hit = resolve_url(choice)
        if hit is None or not hit.supported or not hit.external_id or hit.ats not in ADAPTERS:
            return False, (
                f"{choice!r} is neither a posting id listed for job {job_id} nor a "
                "Greenhouse/Lever/Ashby posting URL"
            )
        target = (hit.ats, hit.token, hit.external_id)
    ats, token, external_id = target
    existing = conn.execute(q.BOARD_BY_ATS_TOKEN_NOCASE, {"ats": ats, "token": token}).fetchone()
    if existing is not None:
        token = str(existing["token"])
    polled = asyncio.run(
        ingest.poll_board(conn, ats, token, str(row["company_name"]), ats_transport)
    )
    if not polled.ok:
        return False, f"{ats}/{token} could not be polled (see logs/jobkit.jsonl)"
    job = ingest.job_on_board(conn, polled.board_id, external_id)
    if job is None:
        return False, f"posting {external_id} is not on {ats}/{token} (closed or unlisted)"
    ats_id = int(job["id"])
    result = merge_feed_row(conn, job_id, ats_id)
    if result == "skipped" and (merged_to := merged_target(conn, job_id)) is not None:
        # the poll merged it (pipeline/poll.upsert_jobs: its apply URL names a
        # posting on this board)
        result, ats_id = "merged", merged_to
    if result != "merged":
        conn.commit()
        return False, (
            f"job {job_id} -> job {ats_id}: {result} (both rows carry an application or a "
            "decision: decide which to keep)"
            if result == "held"
            else f"job {job_id} -> job {ats_id}: {result}"
        )
    conn.execute(
        q.RECORD_EVENT,
        {
            "job_id": job_id,
            "kind": q.BOARD_RESOLVED_EVENT,
            "detail": json.dumps(
                {
                    "ats_job_id": ats_id,
                    "ats": ats,
                    "token": token,
                    "external_id": external_id,
                    "board": "picked by you",
                },
                sort_keys=True,
            ),
        },
    )
    conn.commit()
    log("board_resolve.picked", feed_job_id=job_id, ats_job_id=ats_id, ats=ats, token=token)
    rep = Report()
    status = conn.execute(q.JOB_STATUS, {"job_id": ats_id}).fetchone()
    if status is not None and status["status"] in CARRIED_NEEDS_TRIAGE:
        _triage_carried(conn, [ats_id], rep)
    extra = f" {rep.lines[0]}" if rep.lines else ""
    return True, (
        f"job {job_id} merged into job {ats_id}: {job['title']} ({ats}/{token}, posting "
        f"{external_id}).{extra}"
    )
