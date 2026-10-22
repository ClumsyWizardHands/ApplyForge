"""LinkedIn guest (logged-out) job search, for discovery only.

CLAUDE.md hard rule 11 has one exception: LinkedIn's logged-out
("guest") job search, for discovery only. This module is that exception and
nothing wider:

* httpx only: no browser, no Playwright, never logged in. A cookie jar that
  refuses every cookie (none is stored, none is sent), no proxies
  (trust_env=False), redirects never followed, and an honest User-Agent that
  names jobkit and a contact: JOBKIT_CONTACT_EMAIL, else the answer bank's
  identity.email. No contact, no run. Never a browser User-Agent.
* A few requests a day. `feeds.linkedin_guest` in config/filters.yaml owns the
  switch (default off), the queries, the caps (search requests and posting
  pages per LOCAL calendar day) and the delay between requests (randomized
  between min_delay_seconds and twice that). The caps are counted with
  `linkedin_guest.request` events, written before each request, so they hold
  across runs and processes. The config can lower them, never raise them past
  the ceilings in config.py.
* Stop on block. HTTP 429, 999, 403 or 401, any redirect (LinkedIn sends a
  guest it refuses to the authwall, login or a checkpoint), a CAPTCHA/challenge
  page at any status (LinkedIn's own, or Cloudflare's: a cf-mitigated header,
  challenge-platform/cf-chl/Turnstile markers, a "Just a moment..." page,
  usually HTTP 503), or an HTTP 200 search page with no job card that is not
  the empty no-results page ends the run at once, writes a
  `linkedin_guest.blocked` event, and LinkedIn is skipped until the next local
  calendar day. The day's block is read again
  before every request, not only at the start of a run, so a block any run
  recorded stops this one too. Nothing retries around a block, and a CAPTCHA
  is never solved (hard rule 2). Any other failure (network error, 5xx) ends
  the run without a retry; the next run may try again.
* One run at a time. A run holds an exclusive lock (flock) on a file next to
  the database for its whole length. Another run that finds it held (a
  `jobkit linkedin-discover` during the nightly's linkedin-guest stage, say)
  sends nothing and reports status `busy`. The delay holds across runs too:
  the first request of a run waits out what is left of it since the last
  request of any run.
* No LinkedIn job rows. A card becomes a jobkit job only when its posting
  page carries the employer's apply link and that link resolves to a
  supported ATS: then it enters as the employer's own ATS record, through the
  `jobkit add-url` path (discovery/ingest.py: that board is polled now), so the
  ATS record is the only record (hard rule 10). Otherwise only the company
  name goes to discovery_queue (source `linkedin_guest`, no URL). That
  includes a company the board registry knows by name: this module never adds
  or polls a board on a name match; the discovery stage that runs next
  (registry.resolve_discovery_queue) adds it, and the next poll reads it. No
  LinkedIn URL is stored anywhere.

Live shapes, verified 2026-10-12 (docs/why-no-linkedin.md):

* Search: GET /jobs-guest/jobs/api/seeMoreJobPostings/search with keywords,
  location, f_WT (workplace: 1 on-site, 2 remote, 3 hybrid), f_TPR=r<seconds>
  (posted within), sortBy=DD, start (offset) -> 200 text/html fragment of up
  to 10 <li> cards: .base-search-card[data-entity-urn="urn:li:jobPosting:<id>"],
  .base-search-card__title, .base-search-card__subtitle (company),
  .job-search-card__location, time.job-search-card__listdate[datetime].
* Posting: GET /jobs-guest/jobs/api/jobPosting/<id> -> 200 fragment with the
  title, company, location and criteria. The Apply button of an off-site
  posting (tracking name public_jobs_apply-link-offsite_...) opens a sign-in
  modal: the employer's apply URL is NOT in the logged-out page. Older guest
  pages carried it in <code id="applyUrl"> (a LinkedIn externalApply link
  whose url= parameter is the employer URL); parse_posting still reads that
  shape if it returns. So posting pages are off by default
  (max_detail_requests_per_day: 0), and every card that passes the title
  pre-filter at a company jobkit does not already poll ends as a company name
  in the discovery queue.

One run: take the run lock (held: status busy) -> today's searches (keyword x
location, rotated by day so every pair is covered, within the search budget
left) -> cards, deduplicated -> per card: seen in the last 14 days
(`linkedin_guest.seen` events): skip -> title pre-filter (blocklist,
exclude_titles, include_titles, paused track: the rules and order of
pipeline/filter.py; a rejection is logged and recorded like a feed rejection)
-> the company already has an active board: nothing to do (it is polled every
night) -> the registry knows the company's board: the company name to
discovery_queue (no posting page spent on it) -> otherwise, when posting pages
are allowed, the posting page: an employer URL on a supported ATS goes through
add-url, anything else -> the company name to discovery_queue. Each run writes
a `linkedin_guest.run` event; the digest sums the day's runs."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import http.cookiejar
import json
import os
import random
import re
import sqlite3
import sys
import time
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import yaml
from bs4 import BeautifulSoup, Tag

from jobkit import db as q
from jobkit import paths
from jobkit.adapters import ADAPTERS
from jobkit.autofill import answer_bank
from jobkit.config import Filters, LinkedInGuest, LinkedInLocation, load_filters
from jobkit.discovery import ingest, registry
from jobkit.discovery.url_resolver import resolve as resolve_url
from jobkit.log import log
from jobkit.normalize import normalize_company, normalize_title
from jobkit.pipeline.feeds import INSERT_REJECTION, QUEUE_DISCOVERY

SOURCE = "linkedin_guest"
HOST = "https://www.linkedin.com"
SEARCH_PATH = "/jobs-guest/jobs/api/seeMoreJobPostings/search"
POSTING_PATH = "/jobs-guest/jobs/api/jobPosting/{id}"
PAGE_SIZE = 10  # cards per search response (verified 2026-10-12)
SEEN_WINDOW_DAYS = 14
BLOCK_STATUSES = frozenset({401, 403, 429, 999})
CONTACT_ENV = "JOBKIT_CONTACT_EMAIL"
LOCK_SUFFIX = ".linkedin_guest.lock"
# How long a run retries a held lock before it reports busy. Only rules out
# linkedin_discover's dry-run probe (it holds the lock for microseconds); a
# real run holds it for seconds at least (the delay between requests).
LOCK_WAIT_SECONDS = 1.0

EV_REQUEST = "linkedin_guest.request"
EV_BLOCKED = "linkedin_guest.blocked"
EV_ERROR = "linkedin_guest.error"
EV_SEEN = "linkedin_guest.seen"
EV_RUN = "linkedin_guest.run"


# ---------------------------------------------------------------------------
# the client: honest, cookie-less, proxy-less, never follows a redirect
# ---------------------------------------------------------------------------


def contact() -> tuple[str, str] | None:
    """(contact, where it came from) for the User-Agent, or None."""
    env = os.environ.get(CONTACT_ENV, "").strip()
    if env:
        return env, CONTACT_ENV
    try:
        with open(answer_bank.BANK_PATH) as f:
            bank = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return None
    identity = bank.get("identity") if isinstance(bank, dict) else None
    email = identity.get("email") if isinstance(identity, dict) else None
    if isinstance(email, str) and email.strip():
        return email.strip(), "answer bank identity.email"
    return None


def user_agent(contact: str) -> str:
    return (
        "jobkit/0.1 (personal job-search tool; logged-out job discovery, a few requests "
        f"a day; contact: {contact})"
    )


class _RefuseCookies(http.cookiejar.DefaultCookiePolicy):
    """Store no cookie, send no cookie."""

    def set_ok(self, cookie: http.cookiejar.Cookie, request: urllib.request.Request) -> bool:
        return False

    def return_ok(self, cookie: http.cookiejar.Cookie, request: urllib.request.Request) -> bool:
        return False


def make_client(contact: str, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        base_url=HOST,
        headers={
            "User-Agent": user_agent(contact),
            "Accept": "text/html",
            "Accept-Language": "en-US,en;q=0.8",
        },
        cookies=http.cookiejar.CookieJar(policy=_RefuseCookies()),
        follow_redirects=False,
        trust_env=False,  # no proxy from the environment
        timeout=20.0,
        transport=transport,
    )


_CHALLENGE = re.compile(
    r"captcha|checkpoint/challenge|challengeId|authwall"
    # Cloudflare, which LinkedIn sits behind: the challenge script and its
    # tokens, Turnstile, and the interstitial / block page titles
    r"|challenge-platform|cf[-_]chl|cf-turnstile|challenges\.cloudflare\.com"
    r"|<title>[^<]*(?:security verification|security check|sign in|sign up|log in|login"
    r"|just a moment|attention required)",
    re.I,
)
_COMMENT = re.compile(r"<!--.*?-->", re.S)


def block_reason(resp: httpx.Response) -> str | None:
    """Why this response means LinkedIn is refusing us, or None. Any redirect
    counts: the guest endpoints answer 200 directly (2026-10-12), and a
    refused guest is redirected to the authwall, login or a checkpoint. The
    body of every response is checked for a challenge, whatever its status: a
    Cloudflare challenge usually comes back as HTTP 503 (or 403/429)."""
    status = resp.status_code
    if status in BLOCK_STATUSES:
        return f"HTTP {status}"
    if 300 <= status < 400:
        loc = urlsplit(resp.headers.get("location", ""))
        where = f"{loc.hostname or ''}{loc.path}" or "no location"
        return f"HTTP {status} redirect to {where}"
    mitigated = resp.headers.get("cf-mitigated", "").strip()
    if mitigated:
        return f"HTTP {status} Cloudflare {mitigated[:20]} (cf-mitigated header)"
    m = _CHALLENGE.search(resp.text)
    if m:
        return f"challenge page, HTTP {status} ({m.group(0)[:40]!r})"
    return None


def no_cards_reason(html: str) -> str | None:
    """For an HTTP 200 search response that parsed to no card: why it is taken
    for a block, or None when it is the empty page. Only an empty body (blank,
    or HTML comments only) means "no results": that is what the endpoint has
    returned past the last result (prior knowledge, not re-checked live on
    2026-10-12). Any other page without a .base-search-card is a suspected
    block page (an interstitial the challenge patterns missed), never a
    0-card success, so no further search is spent against it."""
    if not _COMMENT.sub("", html).strip():
        return None
    if BeautifulSoup(html, "html.parser").select_one(".base-search-card") is not None:
        return None  # cards whose shape changed: not a block
    return "search page with no job card and not empty (suspected block page)"


# ---------------------------------------------------------------------------
# parsing (recorded shapes: tests/fixtures/linkedin_guest/)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Card:
    li_id: str  # LinkedIn posting id
    title: str
    company: str
    location: str
    listed: str | None  # YYYY-MM-DD


@dataclass(frozen=True)
class Posting:
    li_id: str
    title: str
    company: str
    location: str
    apply_type: str  # offsite | easy_apply | unknown
    employer_url: str | None  # only when the logged-out page carries it


_URN = re.compile(r"urn:li:jobPosting:(\d+)")
_VIEW_ID = re.compile(r"/jobs/view/(?:[^/?#]*-)?(\d{6,})(?:[/?#]|$)")
_OFFSITE = re.compile(r"apply-link-offsite", re.I)
_EASY = re.compile(r"apply-link-(?:simple|easy)|\beasy[-_ ]apply\b", re.I)


def _text(el: object) -> str:
    return " ".join(el.get_text(" ", strip=True).split()) if isinstance(el, Tag) else ""


def _attr(el: Tag, name: str) -> str:
    value = el.get(name)
    return " ".join(value) if isinstance(value, list) else str(value or "")


def _card_id(el: Tag) -> str:
    for node in (el, *el.select("[data-entity-urn]")):
        m = _URN.search(_attr(node, "data-entity-urn"))
        if m:
            return m.group(1)
    link = el.select_one("a.base-card__full-link") or (el if el.name == "a" else None)
    if link is not None:
        m = _VIEW_ID.search(_attr(link, "href"))
        if m:
            return m.group(1)
    return ""


def parse_cards(html: str) -> list[Card]:
    """The job cards of one search response, in order, one per posting id."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[Card] = []
    seen: set[str] = set()
    for el in soup.select(".base-search-card"):
        li_id = _card_id(el)
        title = _text(el.select_one(".base-search-card__title"))
        company = _text(el.select_one(".base-search-card__subtitle"))
        if not li_id or not title or not company or li_id in seen:
            continue
        seen.add(li_id)
        when = el.select_one("time")
        listed = _attr(when, "datetime")[:10] if isinstance(when, Tag) else ""
        out.append(
            Card(
                li_id=li_id,
                title=title,
                company=company,
                location=_text(el.select_one(".job-search-card__location")),
                listed=listed or None,
            )
        )
    return out


def _same_title(a: str, b: str) -> bool:
    return " ".join(a.casefold().split()) == " ".join(b.casefold().split())


def _is_linkedin(host: str) -> bool:
    host = host.lower().rstrip(".")
    return host in ("linkedin.com", "lnkd.in") or host.endswith(".linkedin.com")


def employer_url(raw: str) -> str | None:
    """The employer's URL from a LinkedIn apply link: a direct off-site URL, or
    the url= target of an externalApply redirect. None for anything on
    LinkedIn itself (a LinkedIn URL never leaves this module)."""
    text = raw.strip().strip('"').strip()
    if not text:
        return None
    parts = urlsplit(text)
    if _is_linkedin(parts.hostname or ""):
        target = parse_qs(parts.query).get("url", [""])[0].strip()
        if not target:
            return None
        text, parts = target, urlsplit(target)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if _is_linkedin(parts.hostname):
        return None
    return text


def parse_posting(html: str, li_id: str) -> Posting:
    soup = BeautifulSoup(html, "html.parser")
    url: str | None = None
    code = soup.find("code", id="applyUrl")
    if isinstance(code, Tag):
        url = employer_url("".join(str(c) for c in code.contents))
    if url or _OFFSITE.search(html):
        apply_type = "offsite"
    elif _EASY.search(html):
        apply_type = "easy_apply"
    else:
        apply_type = "unknown"
    return Posting(
        li_id=li_id,
        title=_text(soup.select_one(".top-card-layout__title, .topcard__title")),
        company=_text(soup.select_one(".topcard__org-name-link"))
        or _text(soup.select_one(".topcard__flavor")),
        location=_text(soup.select_one(".topcard__flavor--bullet")),
        apply_type=apply_type,
        employer_url=url,
    )


# ---------------------------------------------------------------------------
# what to ask for today
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Query:
    keywords: str
    location: LinkedInLocation
    start: int = 0

    def params(self, cfg: LinkedInGuest) -> dict[str, str]:
        p = {
            "keywords": self.keywords,
            "location": self.location.location,
            "f_TPR": f"r{cfg.posted_within_days * 86400}",
            "sortBy": "DD",
            "start": str(self.start),
        }
        if self.location.f_wt:
            p["f_WT"] = self.location.f_wt
        return p

    def describe(self) -> str:
        page = f", results from #{self.start + 1}" if self.start else ""
        return f'"{self.keywords}" in {self.location.location} ({self.location.workplace}{page})'


def plan_searches(cfg: LinkedInGuest, day: datetime.date, used: int) -> list[Query]:
    """The searches still allowed today, in order. Pairs rotate by day so
    every keyword x location pair is searched when the daily cap is smaller
    than their number; with a larger cap, later passes take the next page."""
    combos = [(kw, loc) for loc in cfg.locations for kw in cfg.keywords]
    offset = day.toordinal() * cfg.max_search_requests_per_day
    out: list[Query] = []
    for k in range(max(used, 0), cfg.max_search_requests_per_day):
        kw, loc = combos[(offset + k) % len(combos)]
        out.append(Query(kw, loc, start=(k // len(combos)) * PAGE_SIZE))
    return out


def card_verdict(card: Card, filters: Filters) -> str | None:
    """The title pre-filter: the reason the filter would reject this card's
    title (pipeline/filter.py's rules and order), or None. A paused track
    rejects here: no job row exists to hold it."""
    f = filters.raw.get("filters", {}) or {}
    for name in f.get("blocklist_companies", []) or []:
        if str(name).lower() in card.company.lower():
            return f"blocklist_company:{name}"
    for pat in filters.exclude_titles:
        if re.search(pat, card.title):
            return f"exclude_title:{pat[:40]}"
    if filters.include_titles and not any(re.search(p, card.title) for p in filters.include_titles):
        return "no_include_title_match"
    paused = filters.paused_track_for(card.title)
    if paused:
        return f"track_paused:{paused}"
    return None


# ---------------------------------------------------------------------------
# a run
# ---------------------------------------------------------------------------


_COUNTS = (
    "searches",
    "cards",
    "seen_before",
    "title_rejected",
    "known_board",
    "details",
    "deferred",
    "boards_polled",
    "boards_added",
    "board_jobs_new",
    "new_via_ats",
    "queued_companies",
)


@dataclass
class Report:
    """What one run did. status: ok | blocked | error (the run stopped) |
    disabled | busy | blocked_today | no_contact | no_budget (nothing was
    asked) | dry_run. busy: another run holds the run lock."""

    day: str
    status: str = "ok"
    note: str = ""
    searches: int = 0
    cards: int = 0
    seen_before: int = 0
    title_rejected: int = 0
    known_board: int = 0
    details: int = 0
    deferred: int = 0
    boards_polled: int = 0
    boards_added: int = 0
    board_jobs_new: int = 0
    new_via_ats: int = 0
    queued_companies: int = 0
    blocked: str = ""
    plan: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {k: int(getattr(self, k)) for k in _COUNTS}

    def summary(self) -> str:
        if self.status in (
            "disabled",
            "busy",
            "blocked_today",
            "no_contact",
            "no_budget",
            "dry_run",
        ):
            return f"linkedin guest: {self.status}" + (f" ({self.note})" if self.note else "")
        line = (
            f"linkedin guest: searched {self.searches}, {self.cards} card(s), "
            f"new via ATS {self.new_via_ats}, queued companies {self.queued_companies}, "
            f"blocked: {self.blocked or 'no'}"
        )
        if self.status == "error":
            line += f"; stopped: {self.note}"
        return line

    def __str__(self) -> str:
        return self.summary()

    def render(self) -> str:
        out = [f"LinkedIn guest discovery, {self.day}: {self.status}"]
        if self.note:
            out.append(f"  {self.note}")
        out += [f"  {p}" for p in self.plan]
        if self.status != "dry_run" and (self.searches or self.blocked):
            out.append(
                f"  searches {self.searches} -> {self.cards} card(s): {self.seen_before} seen "
                f"in the last {SEEN_WINDOW_DAYS} days, {self.title_rejected} rejected by the "
                f"pre-filter, {self.known_board} at companies already polled"
            )
            out.append(
                f"  boards polled now {self.boards_polled} ({self.boards_added} added, "
                f"{self.board_jobs_new} new posting(s)); new via ATS {self.new_via_ats}; "
                f"posting pages {self.details}, deferred to tomorrow {self.deferred}; "
                f"companies queued for board discovery {self.queued_companies}"
            )
            out.append(f"  blocked: {self.blocked or 'no'}")
        out += [f"  - {ln}" for ln in self.lines]
        return "\n".join(out)


class _Blocked(Exception):
    pass


class _BlockedEarlier(_Blocked):
    """A block another run recorded for today, found before a request."""


class _Stopped(Exception):
    pass


def _event(
    conn: sqlite3.Connection, kind: str, detail: dict[str, Any], job_id: int | None = None
) -> None:
    conn.execute(q.RECORD_EVENT, {"job_id": job_id, "kind": kind, "detail": json.dumps(detail)})
    conn.commit()


def requests_on_day(conn: sqlite3.Connection, day: str, kind: str) -> int:
    return int(conn.execute(q.LINKEDIN_REQUESTS_ON_DAY, {"day": day, "type": kind}).fetchone()["n"])


def block_on_day(conn: sqlite3.Connection, day: str) -> str | None:
    """The reason LinkedIn refused us on this local day, or None."""
    row = conn.execute(q.LINKEDIN_BLOCK_ON_DAY, {"day": day}).fetchone()
    if row is None:
        return None
    detail = json.loads(row["detail"] or "{}")
    return f"{detail.get('reason', 'blocked')} at {row['at']} UTC"


# ---- one run at a time ----


def lock_path(conn: sqlite3.Connection) -> Path:
    """The run lock: a file next to the database the budget and the blocks are
    counted in, by its resolved path (a symlinked checkout or data/ still names
    one file). An in-memory database: under the project root."""
    row = conn.execute(q.MAIN_DATABASE_FILE).fetchone()
    if row is not None and row["file"]:
        db_file = Path(str(row["file"])).resolve()
        return db_file.with_name(db_file.name + LOCK_SUFFIX)
    return paths.resolve_home() / "data" / f"jobkit{LOCK_SUFFIX}"


if sys.platform == "win32":
    import msvcrt

    # Windows has no flock: lock one byte past the pid text, so a probe can
    # still read who holds it. The lock ends with the handle, like flock.
    _WIN_LOCK_OFFSET = 1 << 20

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _read_head(fd: int) -> bytes:
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, 32)

    def _write_head(fd: int, data: bytes) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, data)

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _read_head(fd: int) -> bytes:
        return os.pread(fd, 32, 0)

    def _write_head(fd: int, data: bytes) -> None:
        os.pwrite(fd, data, 0)


def _holder(fd: int) -> str:
    raw = _read_head(fd).decode(errors="replace").strip()
    return f"pid {raw}" if raw.isdigit() else "another process"


@contextlib.contextmanager
def run_lock(conn: sqlite3.Connection, wait: float | None = None) -> Iterator[str | None]:
    """Hold the exclusive run lock for the block. Yields None when this
    process holds it, else who does ("pid N"); a held lock is retried for
    `wait` seconds (default LOCK_WAIT_SECONDS). An flock ends with its
    descriptor, so a run that dies releases it. The file stays: unlinking a
    lock file races with the next opener."""
    path = lock_path(conn)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + (LOCK_WAIT_SECONDS if wait is None else wait)
        held = _try_lock(fd)
        while not held and time.monotonic() < deadline:
            time.sleep(0.1)
            held = _try_lock(fd)
        if held:
            os.ftruncate(fd, 0)
            _write_head(fd, f"{os.getpid()}\n".encode())
        yield None if held else _holder(fd)
    finally:
        os.close(fd)


def running_now(conn: sqlite3.Connection) -> str | None:
    """Who holds the run lock right now ("pid N"), or None. Creates and
    writes nothing (the dry run and the MCP tool ask it)."""
    try:
        fd = os.open(lock_path(conn), os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        return None if _try_lock(fd) else _holder(fd)
    finally:
        os.close(fd)  # releases the probe's lock, when it took one


class _Run:
    def __init__(
        self,
        conn: sqlite3.Connection,
        filters: Filters,
        cfg: LinkedInGuest,
        rep: Report,
        client: httpx.Client,
        sleep: Callable[[float], None],
        rand: Callable[[], float],
        ats_transport: httpx.AsyncBaseTransport | None,
    ) -> None:
        self.conn, self.filters, self.cfg, self.rep = conn, filters, cfg, rep
        self.client, self.sleep, self.rand = client, sleep, rand
        self.ats_transport = ats_transport
        self.requests = 0
        self.polled: dict[tuple[str, str], tuple[ingest.BoardPoll, int]] = {}

    # ---- requests ----

    def _left(self, kind: str) -> int:
        cap = (
            self.cfg.max_search_requests_per_day
            if kind == "search"
            else self.cfg.max_detail_requests_per_day
        )
        return cap - requests_on_day(self.conn, self.rep.day, kind)

    def _space_from_last_run(self) -> None:
        """The delay holds across runs: wait out what is left of it since the
        last request of any run. `at` has whole seconds, so that request is
        taken to be up to 1 s later than recorded."""
        row = self.conn.execute(q.LINKEDIN_SECONDS_SINCE_LAST_REQUEST).fetchone()
        if row is None or row["s"] is None:
            return
        delay = self.cfg.min_delay_seconds * (1.0 + self.rand())
        wait = min(delay - (float(row["s"]) - 1.0), 2 * self.cfg.min_delay_seconds + 1.0)
        if wait > 0:
            self.sleep(wait)

    def _get(self, path: str, params: dict[str, str] | None, kind: str) -> httpx.Response:
        if self.requests:
            self.sleep(self.cfg.min_delay_seconds * (1.0 + self.rand()))
        else:
            self._space_from_last_run()
        # Read again before every request, after the wait: a block recorded
        # today by any run (the run lock should rule one out) stops this one.
        earlier = block_on_day(self.conn, self.rep.day)
        if earlier:
            log("linkedin_guest.blocked_earlier", day=self.rep.day, type=kind, block=earlier)
            raise _BlockedEarlier(earlier)
        self.requests += 1
        # counted before it is sent: a crash mid-request still counts
        _event(self.conn, EV_REQUEST, {"day": self.rep.day, "type": kind, "path": path})
        try:
            resp = self.client.get(path, params=params)
        except httpx.HTTPError as e:
            why = f"{kind} request failed: {type(e).__name__}: {e}"[:300]
            _event(self.conn, EV_ERROR, {"day": self.rep.day, "type": kind, "error": why})
            log("linkedin_guest.error", type=kind, path=path, error=why)
            raise _Stopped(why) from e
        log("linkedin_guest.request", type=kind, path=path, status=resp.status_code)
        reason = block_reason(resp)
        if reason:
            raise self._block(kind, path, resp.status_code, reason)
        return resp

    def _block(self, kind: str, path: str, status: int, reason: str) -> _Blocked:
        """Record today's block (LinkedIn is skipped until tomorrow) and return
        the exception that stops the run."""
        detail = {
            "day": self.rep.day,
            "status": status,
            "reason": reason,
            "type": kind,
            "path": path,
        }
        _event(self.conn, EV_BLOCKED, detail)
        log("linkedin_guest.blocked", **detail)
        return _Blocked(reason)

    def _unexpected(self, kind: str, resp: httpx.Response) -> _Stopped:
        why = f"{kind}: unexpected HTTP {resp.status_code}"
        _event(self.conn, EV_ERROR, {"day": self.rep.day, "type": kind, "error": why})
        log("linkedin_guest.error", type=kind, status=resp.status_code)
        return _Stopped(why)

    # ---- the run ----

    def go(self, queries: list[Query]) -> None:
        cards: dict[str, Card] = {}
        for qy in queries:
            if self._left("search") <= 0:  # another process spent it meanwhile
                break
            resp = self._get(SEARCH_PATH, qy.params(self.cfg), "search")
            self.rep.searches += 1
            if resp.status_code != 200:
                raise self._unexpected("search", resp)
            found = parse_cards(resp.text)
            suspect = None if found else no_cards_reason(resp.text)
            if suspect:
                raise self._block("search", SEARCH_PATH, resp.status_code, suspect)
            self.rep.plan.append(f"searched {qy.describe()}: {len(found)} card(s)")
            for c in found:
                cards.setdefault(c.li_id, c)
        self.rep.cards = len(cards)
        window = f"-{SEEN_WINDOW_DAYS} days"
        seen = {str(r["id"]) for r in self.conn.execute(q.LINKEDIN_SEEN_SINCE, {"window": window})}
        for card in cards.values():
            if card.li_id in seen:
                self.rep.seen_before += 1
                continue
            self._card(card)

    def _seen(self, card: Card, outcome: str, job_id: int | None = None) -> None:
        detail = {
            "day": self.rep.day,
            "id": card.li_id,
            "outcome": outcome,
            "company": card.company,
            "title": card.title,
            # board resolution (discovery/board_resolver.py) matches the
            # card's title AND location on the employer's board
            "location": card.location,
        }
        _event(self.conn, EV_SEEN, detail, job_id=job_id)
        log("linkedin_guest.card", job_id=job_id, **detail)
        self.rep.lines.append(f"{card.company} — {card.title}: {outcome}")

    def _card(self, card: Card) -> None:
        reason = card_verdict(card, self.filters)
        norm = normalize_company(card.company)
        if reason is None and (
            not norm
            or self.conn.execute(q.COMPANY_BLOCKLISTED, {"normalized_name": norm}).fetchone()
        ):
            reason = "blocklisted_company" if norm else "no_company_name"
        if reason:
            self.conn.execute(
                INSERT_REJECTION, {"external_ref": f"{SOURCE}/{card.li_id}", "reason": reason}
            )
            self.rep.title_rejected += 1
            self._seen(card, f"rejected:{reason}")
            return
        known = self.conn.execute(q.ACTIVE_BOARD_FOR_COMPANY, {"normalized_name": norm}).fetchone()
        if known is not None and known["ats"] in ADAPTERS:
            self.rep.known_board += 1
            self._seen(card, f"known_board:{known['ats']}/{known['token']}")
            return
        hit = registry.lookup(card.company)
        if hit is not None and hit[0] in ADAPTERS:
            # A match by company name alone adds and polls nothing here (rule
            # 11's exception: only an apply link that resolves to the ATS brings
            # a posting in). The name is queued; the discovery stage, next in
            # the run, adds this board from the same registry and the next poll
            # reads it. No posting page is spent on it.
            self._queue(card, f"registry_board:{hit[0]}/{hit[1]}")
            return
        if self.cfg.max_detail_requests_per_day <= 0:
            self._queue(card, "no_registry_board")
            return
        if self._left("detail") <= 0:
            self.rep.deferred += 1  # not marked seen: tomorrow's budget takes it
            return
        resp = self._get(POSTING_PATH.format(id=card.li_id), None, "detail")
        self.rep.details += 1
        if resp.status_code in (404, 410):
            self._seen(card, "posting_gone")
            return
        if resp.status_code != 200:
            raise self._unexpected("detail", resp)
        posting = parse_posting(resp.text, card.li_id)
        if posting.employer_url:
            res = resolve_url(posting.employer_url)
            if res is not None and res.supported and res.token and res.ats in ADAPTERS:
                self._via_board(card, res.ats, res.token, res.external_id)
                return
            self._queue(card, f"offsite:{res.ats if res else 'unrecognized'}")
            return
        self._queue(card, posting.apply_type)

    def _via_board(self, card: Card, ats: str, token: str, external_id: str) -> None:
        """The add-url path, for an employer apply URL from the posting page
        that url_resolver recognizes (never for a name match): poll that board
        now (once per run), then find this card's posting on it, by the URL's
        job id when it names one, else by title."""
        key = (ats, token)
        if key not in self.polled:
            before = int(self.conn.execute(q.MAX_JOB_ID).fetchone()["n"])
            polled = asyncio.run(
                ingest.poll_board(self.conn, ats, token, card.company, self.ats_transport)
            )
            self.polled[key] = (polled, before)
            self.rep.boards_polled += 1
            self.rep.boards_added += int(polled.board_added)
            self.rep.board_jobs_new += polled.jobs_new
        polled, before = self.polled[key]
        where = f"{ats}/{token}"
        if not polled.ok:
            self._seen(card, f"ats_poll_failed:{where}")
            return
        match = (
            ingest.job_on_board(self.conn, polled.board_id, external_id) if external_id else None
        )
        if match is None:
            rows = self.conn.execute(
                q.OPEN_JOBS_ON_BOARD_WITH_TITLE,
                {"board_id": polled.board_id, "normalized_title": normalize_title(card.title)},
            ).fetchall()
            # the normalized title drops trailing qualifiers ("Account Executive,
            # Startups" -> "account executive"): the same raw title wins
            exact = [r for r in rows if _same_title(str(r["title"]), card.title)]
            match = (exact or rows)[0] if rows else None
        if match is None:
            self._seen(card, f"ats_no_title_match:{where}")
            return
        new = int(match["id"]) > before
        self.rep.new_via_ats += int(new)
        self._seen(card, f"ats_{'new' if new else 'known'}:{where}", job_id=int(match["id"]))

    def _queue(self, card: Card, why: str) -> None:
        cur = self.conn.execute(
            QUEUE_DISCOVERY,
            {
                "company_name": card.company,
                "normalized_name": normalize_company(card.company),
                "source": SOURCE,
                "url": None,  # only the company name (rule 11 exception)
            },
        )
        if cur.rowcount:
            self.rep.queued_companies += 1
            self._seen(card, f"queued_company:{why}")
        else:
            self._seen(card, f"company_already_queued:{why}")


def _dry_run(
    conn: sqlite3.Connection, rep: Report, cfg: LinkedInGuest, day: datetime.date
) -> Report:
    rep.status = "dry_run"
    rep.note = "no request to LinkedIn, nothing written"
    used_search = requests_on_day(conn, rep.day, "search")
    used_detail = requests_on_day(conn, rep.day, "detail")
    who = contact()
    blocked = block_on_day(conn, rep.day)
    rep.plan.append(
        "enabled: yes"
        if cfg.enabled
        else "enabled: NO (feeds.linkedin_guest.enabled is false: nothing runs until it is true)"
    )
    if blocked:
        rep.plan.append(f"blocked today: {blocked}; LinkedIn is skipped until tomorrow")
    holder = running_now(conn)
    if holder:
        rep.plan.append(f"running now: a LinkedIn guest run holds the run lock ({holder})")
    rep.plan.append(
        f"contact for the User-Agent: from {who[1]}"
        if who
        else f"contact for the User-Agent: NONE (set {CONTACT_ENV}); nothing runs without one"
    )
    rep.plan.append(
        f"today: {used_search}/{cfg.max_search_requests_per_day} searches and "
        f"{used_detail}/{cfg.max_detail_requests_per_day} posting pages used; "
        f"{cfg.min_delay_seconds:g}-{2 * cfg.min_delay_seconds:g}s between requests"
    )
    queries = plan_searches(cfg, day, used_search)
    rep.plan += [f"would search {qy.describe()}" for qy in queries] or ["no search left today"]
    rep.plan += digest_lines(conn, cfg, rep.day, heading=False)
    return rep


def _locked(
    conn: sqlite3.Connection,
    filters: Filters,
    cfg: LinkedInGuest,
    rep: Report,
    day: datetime.date,
    transport: httpx.BaseTransport | None,
    ats_transport: httpx.AsyncBaseTransport | None,
    sleep: Callable[[float], None],
    rand: Callable[[], float],
) -> None:
    """The run itself, with the run lock held: block, contact and budget are
    read under the lock, so no other run can change them meanwhile."""
    blocked = block_on_day(conn, rep.day)
    who = contact()
    queries = plan_searches(cfg, day, requests_on_day(conn, rep.day, "search"))
    if blocked:
        rep.status, rep.note = "blocked_today", f"{blocked}; skipped until tomorrow"
    elif who is None:
        rep.status, rep.note = "no_contact", f"set {CONTACT_ENV} for the User-Agent"
    elif not queries:
        rep.status, rep.note = "no_budget", "today's searches are used"
    else:
        with make_client(who[0], transport) as client:
            try:
                _Run(conn, filters, cfg, rep, client, sleep, rand, ats_transport).go(queries)
            except _BlockedEarlier as b:
                rep.status, rep.blocked = "blocked", str(b)
                rep.note = "a block was recorded today before the next request: stopped"
            except _Blocked as b:
                rep.status, rep.blocked = "blocked", str(b)
                rep.note = "LinkedIn refused a request: stopped, skipped until tomorrow"
            except _Stopped as s:
                rep.status, rep.note = "error", str(s)


def run(
    conn: sqlite3.Connection,
    filters: Filters | None = None,
    *,
    dry_run: bool = False,
    transport: httpx.BaseTransport | None = None,
    ats_transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
    today: datetime.date | None = None,
) -> Report:
    """One LinkedIn guest discovery run (see the module docstring). Never
    raises for a block or a failed request: the Report says what happened.
    One run at a time: while another holds the run lock, this one sends
    nothing and reports busy. `dry_run` makes no request and writes nothing.
    `transport` and `ats_transport` are for tests (recorded fixtures)."""
    filters = filters or load_filters()
    cfg = filters.linkedin_guest
    day = today or datetime.date.today()
    rep = Report(day=day.isoformat())
    if dry_run:
        return _dry_run(conn, rep, cfg, day)
    if not cfg.enabled:
        rep.status, rep.note = "disabled", "feeds.linkedin_guest.enabled is false"
        return rep
    with run_lock(conn) as holder:
        if holder is not None:
            rep.status = "busy"
            rep.note = (
                f"another LinkedIn guest run is in progress ({holder}); not starting a second"
            )
        else:
            _locked(conn, filters, cfg, rep, day, transport, ats_transport, sleep, rand)
    conn.commit()
    _event(conn, EV_RUN, {"day": rep.day, "status": rep.status, "note": rep.note, **rep.counts()})
    log("linkedin_guest.run", day=rep.day, status=rep.status, note=rep.note, **rep.counts())
    return rep


def digest_lines(
    conn: sqlite3.Connection,
    cfg: LinkedInGuest | None = None,
    day: str | None = None,
    heading: bool = True,
) -> list[str]:
    """The day's LinkedIn line (searched / new via ATS / queued companies /
    blocked), summed over the day's runs. Nothing when the feature is off and
    nothing ran that day."""
    try:
        cfg = cfg or load_filters().linkedin_guest
    except ValueError as e:
        return ["## LinkedIn guest discovery", f"⚠ config error: {e}", ""] if heading else []
    day = day or datetime.date.today().isoformat()
    runs = [
        json.loads(r["detail"] or "{}") for r in conn.execute(q.LINKEDIN_RUNS_ON_DAY, {"day": day})
    ]
    blocked = block_on_day(conn, day)
    searched = requests_on_day(conn, day, "search")
    if not cfg.enabled and not runs and not searched:
        return []
    new = sum(int(r.get("new_via_ats", 0)) for r in runs)
    queued = sum(int(r.get("queued_companies", 0)) for r in runs)
    line = (
        f"LinkedIn today: searched {searched} (cap {cfg.max_search_requests_per_day}), "
        f"new via ATS {new}, companies queued for board discovery {queued}, "
        f"blocked: {blocked or 'no'}"
        + ("" if cfg.enabled else " (feeds.linkedin_guest is off)")
        + "."
    )
    if not heading:
        return [line]
    return ["## LinkedIn guest discovery (logged-out, discovery only)", line, ""]
