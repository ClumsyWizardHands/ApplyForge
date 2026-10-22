"""Job-alert emails and LinkedIn message notifications, read from the
person's own mailbox as part of the email check.

jobkit never connects to a mailbox, and the LinkedIn site is never fetched
for this. Claude reads the person's email through the email connector they
set up (email_targets suggests the search) and records what it finds here,
through two MCP tools (and the matching CLI commands, for debugging):

* record_job_alerts: the jobs listed in one job-alert email (LinkedIn's,
  or a job board's such as Indeed): for each, the company, title, location
  and whether the email shows an 'apply on company site' link. No link and
  no job id is ever passed or stored. Each title goes through the same title
  rules as LinkedIn guest discovery (linkedin_guest.card_verdict: company
  blocklist, exclude_titles, include_titles, paused tracks). Many jobs in an
  alert also live on the employer's own Greenhouse, Lever or Ashby board, so
  reading the alert is enough to find the real posting. A kept job whose
  employer posting jobkit already holds (same company, same title, a
  location that agrees) needs nothing. Every other kept job becomes a card
  for the board resolver, exactly like a LinkedIn guest card: a
  'job_alert.item' event with the company, title and location, a synthetic
  id ('alert-' and a hash of those three; never a job board's id) and a
  reference '<message id>#<n>'. The resolver (discovery/board_resolver.py)
  finds the same job, title AND location, on the employer's own board and
  ingests that posting; several or no agreeing postings are flagged for the
  person, as for guest cards. A job enters jobkit only as the employer's
  posting (CLAUDE.md rules 10 and 11). The company name goes to the
  discovery queue (source 'linkedin_email', or 'job_alert_email' for other
  senders) unless jobkit already polls the company's board; the card is
  matched there too, since the poll may not have run today, and an existing
  posting is never ingested twice. Recorded once per message id.
* record_linkedin_message: one LinkedIn message or InMail notification:
  the sender's company when shown, and a one-line summary in Claude's words
  that names the sender by role, never by name (the same one-line rule as
  record_email_response; the message text and the sender's name are never
  stored). Linked to an application or a queued job at
  that company when there is one. Never answered: nothing is sent.

Board lookup runs the same day: MCP finish_email_check starts `jobkit
same-day --resolve-boards` in the background when the check recorded any
cards (public board APIs only, jobkit's honest User-Agent). It runs board
lookup, then takes each posting it found through the filter, the sanity
checks, scoring, the queue and the standing rules (pipeline/same_day.py),
so a job that clears the person's bar is approved that day. The nightly
resolve-boards stage picks up any card it missed.

Both are listed in the digest ('Jobs from job-alert emails since the last
digest', with where each job was found, and 'LinkedIn messages since the
last digest'). Deterministic, no LLM.

A kept job board lookup could not match (no board, or no posting with that
title and location) is searched for on the web by Claude when the person
said yes to that in setup (track/alert_search.py); where each job stands
then includes the search result: found on the employer's board, a lead on
a system jobkit does not read (the system's name and 'apply on the
employer's site: <link>', no job row), or not found.
Whether the unattended scheduled run checks email at all is
tracking.email_check (MCP email_targets(scheduled=true)); asking Claude to
check always works."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

from jobkit import db as q
from jobkit.adapters import ADAPTERS
from jobkit.config import Filters, load_filters
from jobkit.discovery import registry
from jobkit.discovery.board_resolver import locations_agree, title_key
from jobkit.discovery.linkedin_guest import Card, card_verdict
from jobkit.log import log
from jobkit.normalize import normalize_company
from jobkit.pipeline.feeds import INSERT_REJECTION, QUEUE_DISCOVERY
from jobkit.track import responses

EV_EMAIL = "job_alert.email"
EV_ITEM = "job_alert.item"
EV_MESSAGE = "linkedin_message"

LINKEDIN = "linkedin"
QUEUE_SOURCE_LINKEDIN = "linkedin_email"
QUEUE_SOURCE_OTHER = "job_alert_email"
ITEM_KEYS = ("company", "title", "location", "company_site_link")
ITEMS_MAX = 50
COMPANY_MAX = 120
TITLE_MAX = 200
LOCATION_MAX = 120
CARD_ID_PREFIX = "alert-"

# a link, a bare domain, or a long number (a job board's posting id): none of
# them is ever stored
_LINKISH = re.compile(r"://|\bwww\.|\blinkedin\.com\b|\blnkd\.in\b|\d{7,}", re.I)
# a Gmail message or thread id: 16 hex characters
_GMAIL_ID = re.compile(r"[0-9a-f]{16}")
_URLISH = re.compile(r"://|\bwww\.|\blinkedin\.com\b|\blnkd\.in\b", re.I)
# a LinkedIn link anywhere in a mailbox link (a safe-link wrapper's query)
_LINKEDIN_ANYWHERE = re.compile(r"linkedin\.com|lnkd\.in", re.I)
LINKEDIN_ANYWHERE = _LINKEDIN_ANYWHERE  # alert_search.py refuses such a URL too
# a word starting with a letter, with no run of 7+ digits (a job id)
_SOURCE = re.compile(r"^(?!.*\d{7})[a-z][a-z0-9_-]{0,29}$")

SOURCE_LABEL = {
    LINKEDIN: "LinkedIn",
    "indeed": "Indeed",
    "glassdoor": "Glassdoor",
    "ziprecruiter": "ZipRecruiter",
    "wellfound": "Wellfound",
}


# ---- input ----------------------------------------------------------------------


@dataclass(frozen=True)
class AlertItem:
    company: str
    title: str
    location: str
    company_site_link: bool | None

    def head(self) -> str:
        return f"{self.company} — {self.title} ({self.location or 'no location'})"


def _text(value: Any, key: str, n: int, limit: int, *, required: bool) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"job {n}: {key} must be text")
    if len(value.strip().splitlines()) > 1:
        raise ValueError(f"job {n}: {key} must be one line")
    s = " ".join(value.split())
    if required and not s:
        raise ValueError(f"job {n}: {key} is required")
    if len(s) > limit:
        raise ValueError(f"job {n}: {key} is longer than {limit} characters")
    if _LINKISH.search(s):
        raise ValueError(
            f"job {n}: {key} holds a link or a long number; pass the {key} as plain text "
            "(never a link or a job board's id)"
        )
    return s


def parse_items(items: list[dict[str, Any]] | str) -> list[AlertItem]:
    """The jobs of one alert email, checked: a list of {company, title,
    location, company_site_link}, or that list as JSON. Any other key, a
    link or a long number in a field is refused (ValueError), so no link or
    job id can be stored."""
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except ValueError:
            raise ValueError(
                "items must be a JSON list of {company, title, location, company_site_link}"
            ) from None
    if not isinstance(items, list):
        raise ValueError("items must be a list of {company, title, location, company_site_link}")
    if len(items) > ITEMS_MAX:
        raise ValueError(f"at most {ITEMS_MAX} jobs per email")
    out: list[AlertItem] = []
    for n, raw in enumerate(items, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"job {n} must be an object with company, title, location")
        extra = sorted(str(k) for k in raw if k not in ITEM_KEYS)
        if extra:
            raise ValueError(
                f"job {n}: unexpected field(s) {', '.join(extra)}: pass only "
                f"{', '.join(ITEM_KEYS)} (never a link or a job id)"
            )
        site = raw.get("company_site_link")
        if site is not None and not isinstance(site, bool):
            raise ValueError(f"job {n}: company_site_link must be true, false or absent")
        out.append(
            AlertItem(
                company=_text(raw.get("company"), "company", n, COMPANY_MAX, required=True),
                title=_text(raw.get("title"), "title", n, TITLE_MAX, required=True),
                location=_text(raw.get("location"), "location", n, LOCATION_MAX, required=False),
                company_site_link=site,
            )
        )
    return out


def normalize_source(source: str) -> tuple[str, str]:
    """(source key, discovery-queue source): 'linkedin' -> linkedin_email,
    any other sender ('indeed', 'glassdoor', ...) -> job_alert_email."""
    key = (source or "").strip().lower()
    if not _SOURCE.match(key):
        raise ValueError(
            "source must be one short word naming the sender: linkedin, indeed, glassdoor, "
            "ziprecruiter, wellfound, ..."
        )
    return key, QUEUE_SOURCE_LINKEDIN if key == LINKEDIN else QUEUE_SOURCE_OTHER


def source_label(key: str) -> str:
    return SOURCE_LABEL.get(key, key.capitalize())


def card_id(item: AlertItem) -> str:
    """A stable id for one alert job (the same job in two alert emails gets
    the same id, so board resolution looks for it once). Made from the
    company, title and location; never a job board's id."""
    key = "|".join(
        (normalize_company(item.company), item.title.casefold(), item.location.casefold())
    )
    return CARD_ID_PREFIX + hashlib.sha1(key.encode()).hexdigest()[:16]


# ---- recording an alert email ----------------------------------------------------


@dataclass
class AlertsRecorded:
    message_id: str
    source: str
    received_at: str
    kept: int = 0  # passed the title rules: queued_cards + already_in_db
    queued_cards: int = 0  # handed to board lookup (the employer's own posting)
    already_in_db: int = 0  # the employer's posting is already in jobkit
    dropped: int = 0  # by the title rules
    at_polled_board: int = 0  # cards at a company whose board jobkit already polls
    companies_queued: int = 0  # company names new to the discovery queue
    repeated: int = 0  # the same job listed twice in this email
    already: bool = False
    lines: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "kept": self.kept,
            "queued_cards": self.queued_cards,
            "already_in_db": self.already_in_db,
            "dropped": self.dropped,
            "at_polled_board": self.at_polled_board,
            "companies_queued": self.companies_queued,
            "repeated": self.repeated,
        }

    def render(self) -> str:
        what = f"{source_label(self.source)} job alert of {self.received_at[:10]}"
        counts = (
            f"kept {self.kept} ({self.queued_cards} for board lookup, {self.already_in_db} "
            f"already in jobkit), dropped {self.dropped} by the title rules"
        )
        if self.companies_queued:
            counts += f"; {self.companies_queued} company name(s) new to the discovery queue"
        if self.repeated:
            counts += f"; {self.repeated} listed twice"
        if self.already:
            return f"already recorded: this message is a {what}; nothing changed ({counts})"
        out = [f"recorded {what}: {counts}", *self.lines]
        if self.queued_cards:
            out.append(
                "Board lookup looks for these on the employers' own Greenhouse, Lever and "
                "Ashby boards when finish_email_check starts it; the nightly resolve-boards "
                "stage picks up any it misses."
            )
        return "\n".join(out)


def record_job_alerts(
    conn: sqlite3.Connection,
    items: list[dict[str, Any]] | str,
    message_id: str,
    received_at: str,
    source: str = LINKEDIN,
    filters: Filters | None = None,
) -> AlertsRecorded:
    """Record the jobs listed in one job-alert email (see the module
    docstring). Idempotent by message id. Raises ValueError on bad input,
    with nothing written."""
    mid = clean_message_id(message_id)
    when = responses._stamp(responses.parse_when(received_at))
    key, queue_source = normalize_source(source)
    parsed = parse_items(items)

    seen = conn.execute(q.JOB_ALERT_EMAIL_BY_MESSAGE_ID, {"message_id": mid}).fetchone()
    if seen is not None:
        d: dict[str, Any] = json.loads(seen["detail"] or "{}")
        counts = d.get("counts") or {}
        rec = AlertsRecorded(
            message_id=mid,
            source=str(d.get("source") or key),
            received_at=str(d.get("received_at") or when),
            already=True,
        )
        for k in rec.counts():
            setattr(rec, k, int(counts.get(k, 0)))
        return rec

    filters = filters or load_filters()
    rec = AlertsRecorded(message_id=mid, source=key, received_at=when)
    done: set[str] = set()
    for n, item in enumerate(parsed, 1):
        cid = card_id(item)
        if cid in done:
            rec.repeated += 1
            continue
        done.add(cid)
        ref = f"{mid}#{n}"
        outcome, card = _item_outcome(conn, filters, item, queue_source, ref)
        if outcome.startswith("rejected:"):
            rec.dropped += 1
        else:
            rec.kept += 1
            if card:
                rec.queued_cards += 1
                rec.at_polled_board += int(outcome.startswith("known_board:"))
                rec.companies_queued += int(outcome.startswith("queued_company:"))
            else:
                rec.already_in_db += 1
        conn.execute(
            q.RECORD_EVENT,
            {
                "job_id": int(outcome.split(":", 1)[1]) if outcome.startswith("in_db:") else None,
                "kind": EV_ITEM,
                "detail": json.dumps(
                    {
                        # the shape of a linkedin_guest.seen card: the board
                        # resolver reads id, company, title and location of
                        # every item with card = 1 (db.JOB_ALERT_CARDS_SINCE)
                        "id": cid,
                        "ref": ref,
                        "card": int(card),
                        "outcome": outcome,
                        "company": item.company,
                        "title": item.title,
                        "location": item.location,
                        "company_site_link": item.company_site_link,
                        "source": key,
                        "queue_source": queue_source,
                        "message_id": mid,
                        "received_at": when,
                    },
                    sort_keys=True,
                ),
            },
        )
        rec.lines.append(f"- {item.head()}: {_describe(outcome)}")
    conn.execute(
        q.RECORD_EVENT,
        {
            "job_id": None,
            "kind": EV_EMAIL,
            "detail": json.dumps(
                {"message_id": mid, "received_at": when, "source": key, "counts": rec.counts()},
                sort_keys=True,
            ),
        },
    )
    conn.commit()
    log("job_alerts.recorded", message_id=mid, source=key, **rec.counts())
    return rec


def _item_outcome(
    conn: sqlite3.Connection, filters: Filters, item: AlertItem, queue_source: str, ref: str
) -> tuple[str, bool]:
    """(outcome, whether the job goes to board lookup as a card).
    linkedin_guest's card path, without LinkedIn: the title rules first. A
    job whose employer posting jobkit already holds (same company, same
    title, a location that agrees) needs nothing. Any other kept job becomes
    a card for the board resolver; its company name goes to the discovery
    queue unless jobkit already polls the company's board (the resolver
    still matches the card there: the poll may not have run today)."""
    card = Card(
        li_id="", title=item.title, company=item.company, location=item.location, listed=None
    )
    reason = card_verdict(card, filters)
    norm = normalize_company(item.company)
    if reason is None and (
        not norm or conn.execute(q.COMPANY_BLOCKLISTED, {"normalized_name": norm}).fetchone()
    ):
        reason = "blocklisted_company" if norm else "no_company_name"
    if reason:
        conn.execute(INSERT_REJECTION, {"external_ref": f"{queue_source}/{ref}", "reason": reason})
        log("job_alerts.rejected", ref=ref, reason=reason)
        return f"rejected:{reason}", False
    have = _posting_in_db(conn, norm, item)
    if have is not None:
        return f"in_db:{have}", False
    known = conn.execute(q.ACTIVE_BOARD_FOR_COMPANY, {"normalized_name": norm}).fetchone()
    if known is not None and known["ats"] in ADAPTERS:
        return f"known_board:{known['ats']}/{known['token']}", True
    hit = registry.lookup(item.company)
    why = f"registry_board:{hit[0]}/{hit[1]}" if hit is not None else "no_registry_board"
    cur = conn.execute(
        QUEUE_DISCOVERY,
        {
            "company_name": item.company,
            "normalized_name": norm,
            "source": queue_source,
            "url": None,  # only the company name
        },
    )
    return f"{'queued_company' if cur.rowcount else 'company_already_queued'}:{why}", True


def _posting_in_db(conn: sqlite3.Connection, norm: str, item: AlertItem) -> int | None:
    """The id of the employer's own posting jobkit already holds for this
    job: a row from the company's Greenhouse/Lever/Ashby board with the same
    title (board_resolver.title_key) and a location that agrees
    (board_resolver.locations_agree), open rows first. None when there is
    none, or the alert names no location."""
    if not item.location:
        return None
    want = title_key(item.title)
    for r in conn.execute(q.ATS_JOBS_FOR_COMPANY, {"normalized_name": norm}):
        remote = None if r["remote"] is None else bool(r["remote"])
        if title_key(str(r["title"])) == want and locations_agree(
            str(r["location_raw"] or ""), item.location, remote, None
        ):
            return int(r["id"])
    return None


def _describe(outcome: str) -> str:
    kind, _, rest = outcome.partition(":")
    if kind == "rejected":
        return f"dropped by the title rules ({rest})"
    if kind == "in_db":
        return f"already in jobkit as job {rest} (the employer's own posting)"
    if kind == "known_board":
        return f"board lookup matches it on the company's board ({rest}), already polled"
    if kind == "queued_company":
        return "company queued; board lookup looks for this job on the employer's own board"
    return "company already queued; board lookup looks for this job on the employer's own board"


# ---- recording a LinkedIn message ------------------------------------------------


@dataclass
class MessageRecorded:
    message_id: str
    company: str
    summary: str
    received_at: str
    job_id: int | None = None
    application_id: int | None = None
    about: str = ""
    already: bool = False

    def render(self) -> str:
        who = f" ({self.company})" if self.company else ""
        about = f"; about {self.about}" if self.about else ""
        if self.already:
            return f"already recorded: the LinkedIn message{who}; nothing changed{about}"
        return (
            f"recorded the LinkedIn message{who}, {self.received_at[:10]}: "
            f"{self.summary}{about}. Nothing was sent; any answer is yours to write on LinkedIn."
        )


def _one_line(value: str, key: str, limit: int, *, required: bool) -> str:
    text = (value or "").strip()
    if len(text.splitlines()) > 1:
        raise ValueError(f"{key} must be one line")
    s = " ".join(text.split())
    if required and not s:
        raise ValueError(f"{key} is required")
    if len(s) > limit:
        raise ValueError(f"{key} is longer than {limit} characters")
    if _URLISH.search(s):
        raise ValueError(f"{key} holds a link; pass plain text")
    return s


def _message_link(link: str) -> str:
    """The mailbox link, refused when it is LinkedIn's or carries a LinkedIn
    link anywhere (a safe-link or redirect wrapper's query, a fragment)."""
    url = responses._clean_link(link)
    host = (urlsplit(url).hostname or "").lower() if url else ""
    if host and (host in ("linkedin.com", "lnkd.in") or host.endswith(".linkedin.com")):
        raise ValueError(
            "link must open the email in the person's mailbox, not LinkedIn (no LinkedIn "
            "link is stored)"
        )
    if url and _LINKEDIN_ANYWHERE.search(unquote(unquote(url))):
        raise ValueError(
            "link carries a LinkedIn link; pass the link that opens the email in the person's "
            "mailbox (no LinkedIn link is stored)"
        )
    return url


def clean_message_id(message_id: str) -> str:
    """The mailbox's id for a message (a Gmail id, or an RFC Message-ID like
    '<abc@mail.linkedin.com>'), refused when it is a link or shaped like one,
    or a bare job id (a run of 7 or more digits outside those two shapes): a
    LinkedIn link or job id is never stored, not even as an id."""
    mid = responses._clean_message_id(message_id)
    low = mid.lower()
    rfc = mid.startswith("<") and mid.endswith(">") and "@" in mid
    if (
        "://" in low
        or "www." in low
        or "lnkd.in" in low
        or "linkedin.com/" in low
        or (not (mid.startswith("<") and mid.endswith(">")) and "/" in mid)
        # a bare job id (a Gmail id is 16 hex characters, so any 7-digit run
        # outside that shape or an RFC <...@...> id is refused)
        or (not rfc and not _GMAIL_ID.fullmatch(low) and re.search(r"\d{7,}", mid))
    ):
        raise ValueError(
            "message_id must be the mailbox's id for the email (a Gmail id or its Message-ID), "
            "not a link"
        )
    return mid


def record_linkedin_message(
    conn: sqlite3.Connection,
    company: str,
    summary: str,
    received_at: str,
    message_id: str,
    link: str = "",
) -> MessageRecorded:
    """Record one LinkedIn message or InMail notification: the sender's
    company, a one-line summary (the sender by role, never by name), when it
    arrived, the message id and a link to the email. Linked to the newest application (else a
    queued or approved job) at that company. Idempotent by message id. Never
    replies. Raises ValueError on bad input, with nothing written."""
    mid = clean_message_id(message_id)
    firm = _one_line(company, "company", COMPANY_MAX, required=False)
    if _LINKISH.search(firm):
        raise ValueError("company holds a long number; pass the company's name")
    line = responses.one_line_summary(summary)
    if not line:
        raise ValueError("summary is required: one line in your words about what the message says")
    if _URLISH.search(line):
        raise ValueError("summary holds a link; say in your words what the message is about")
    if _LINKISH.search(line):
        raise ValueError(
            "summary holds a long number (a job or message id); say in words what the "
            "message is about"
        )
    url = _message_link(link)
    when = responses._stamp(responses.parse_when(received_at))

    seen = conn.execute(q.LINKEDIN_MESSAGE_BY_MESSAGE_ID, {"message_id": mid}).fetchone()
    if seen is not None:
        d: dict[str, Any] = json.loads(seen["detail"] or "{}")
        return MessageRecorded(
            message_id=mid,
            company=str(d.get("company") or ""),
            summary=str(d.get("summary") or ""),
            received_at=str(d.get("received_at") or ""),
            job_id=seen["job_id"],
            application_id=d.get("application_id"),
            about=_about(d),
            already=True,
        )

    match = _match_company(conn, firm) if firm else None
    detail: dict[str, Any] = {
        "message_id": mid,
        "received_at": when,
        "company": firm,
        "summary": line,
        "link": url,
        "application_id": None,
    }
    job_id: int | None = None
    if match is not None:
        job_id = int(match["job_id"])
        detail.update(
            application_id=match["application_id"],
            stage=match["stage"],
            job_company=str(match["company_name"]),
            job_title=str(match["title"]),
            job_status=str(match["status"]),
        )
    conn.execute(
        q.RECORD_EVENT,
        {"job_id": job_id, "kind": EV_MESSAGE, "detail": json.dumps(detail, sort_keys=True)},
    )
    conn.commit()
    log(
        "job_alerts.linkedin_message",
        message_id=mid,
        job_id=job_id,
        application_id=detail["application_id"],
    )
    return MessageRecorded(
        message_id=mid,
        company=firm,
        summary=line,
        received_at=when,
        job_id=job_id,
        application_id=detail["application_id"],
        about=_about({**detail, "job_id": job_id}),
    )


def _match_company(conn: sqlite3.Connection, company: str) -> sqlite3.Row | None:
    norm = normalize_company(company)
    if not norm:
        return None
    for r in conn.execute(q.JOBS_WITH_APPLICATION_OR_QUEUED):
        if normalize_company(str(r["company_name"] or "")) == norm:
            row: sqlite3.Row = r
            return row
    return None


def _about(d: dict[str, Any]) -> str:
    if not d.get("job_title"):
        return ""
    job = f"{d.get('job_company')} — {d.get('job_title')}"
    if d.get("application_id"):
        return f"application {d['application_id']} ({job}, stage {d.get('stage')})"
    return f"job {d.get('job_id')} ({job}, {d.get('job_status')})"


# ---- the email check and the digest ----------------------------------------------


def messages_after(conn: sqlite3.Connection, after_id: int) -> list[str]:
    """One line per LinkedIn message recorded after an event id (the last
    email check), for email_targets' summary."""
    out: list[str] = []
    for r in conn.execute(q.LINKEDIN_MESSAGES_AFTER, {"after_id": after_id}):
        d = json.loads(r["detail"] or "{}")
        out.append(_message_line(d, r["job_id"]))
    return out


def alert_emails_after(conn: sqlite3.Connection, after_id: int) -> int:
    return int(conn.execute(q.COUNT_JOB_ALERT_EMAILS_AFTER, {"after_id": after_id}).fetchone()[0])


def _message_line(d: dict[str, Any], job_id: int | None) -> str:
    who = "LinkedIn message" + (f" ({d['company']})" if d.get("company") else "")
    about = _about({**d, "job_id": job_id})
    return f"- {who}, received {str(d.get('received_at') or '')[:10]}: {d.get('summary') or ''}" + (
        f" [{about}]" if about else ""
    )


def alert_cards_after(conn: sqlite3.Connection, after_id: int) -> int:
    """Alert jobs handed to board lookup after an event id (the last email
    check): finish_email_check starts the resolver when there are any."""
    return int(conn.execute(q.COUNT_JOB_ALERT_CARDS_AFTER, {"after_id": after_id}).fetchone()[0])


def _resolution(conn: sqlite3.Connection, d: dict[str, Any], job_id: int | None) -> str:
    """Where one kept alert job stands: already in jobkit, found on the
    employer's own board (which one, and the job id), flagged, not found, or
    still waiting for board lookup."""
    outcome = str(d.get("outcome") or "")
    if outcome.startswith("in_db:"):
        return f"already in jobkit as job {job_id or outcome[6:]} (the employer's own posting)"
    row = conn.execute(q.CARD_OUTCOME_BY_ID, {"card_id": str(d.get("id") or "")}).fetchone()
    if row is None:
        return "waiting for board lookup (the next resolve-boards run)"
    got: dict[str, Any] = json.loads(row["detail"] or "{}")
    kind = str(got.get("outcome") or "")
    if kind == "ingested":
        where = f"{got.get('ats')}/{got.get('token')}"
        return f"found on the employer's own board ({where}): job {row['job_id']}"
    if kind == "ambiguous":
        return (
            "flagged: more than one posting could be it, or the board is not confirmed as "
            "the company's (resolve_boards lists the candidates)"
        )
    from jobkit.track import alert_search  # it builds on this module

    searched = alert_search.result_for(conn, str(d.get("id") or ""))
    if searched:
        return searched
    if kind == "no_board":
        return "no Greenhouse, Lever or Ashby board found for the company"
    return "no posting with this title and location on the employer's own board"


def item_lines(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> tuple[list[str], int, int]:
    """(one line per kept alert job: company, title, location, the email it
    came from and where it stands; how many were dropped by the title rules;
    how many emails) for job_alert.item rows. A job listed in two emails is
    listed once."""
    kept: list[str] = []
    dropped = 0
    emails: set[str] = set()
    listed: set[str] = set()
    for r in rows:
        d = json.loads(r["detail"] or "{}")
        emails.add(str(d.get("message_id") or ""))
        if str(d.get("outcome") or "").startswith("rejected:"):
            dropped += 1
            continue
        cid = str(d.get("id") or "")
        if cid in listed:
            continue
        listed.add(cid)
        where = str(d.get("location") or "") or "no location"
        kept.append(
            f"- {d.get('company')} — {d.get('title')} ({where}), "
            f"{source_label(str(d.get('source') or ''))} alert "
            f"{str(d.get('received_at') or '')[:10]}: {_resolution(conn, d, r['job_id'])}"
        )
    return kept, dropped, len(emails)


def items_after(conn: sqlite3.Connection, after_id: int) -> list[str]:
    """The kept alert jobs recorded after an event id (the last email
    check), for finish_email_check's summary."""
    kept, _, _ = item_lines(
        conn, conn.execute(q.JOB_ALERT_ITEMS_AFTER, {"after_id": after_id}).fetchall()
    )
    return kept


def digest_lines(conn: sqlite3.Connection, since: str) -> list[str]:
    """'Jobs from job-alert emails since the last digest' and 'LinkedIn
    messages since the last digest': what was recorded at or after `since`
    ('YYYY-MM-DD HH:MM:SS' UTC). Empty when nothing was."""
    from jobkit.track import alert_search  # it builds on this module

    lines: list[str] = []
    rows = conn.execute(q.JOB_ALERT_ITEMS_SINCE, {"since": since}).fetchall()
    kept, dropped, emails = item_lines(conn, rows)
    if kept or dropped:
        lines.append(
            f"## Jobs from job-alert emails since the last digest ({len(kept)} kept, "
            f"from {emails} email(s))"
        )
        lines += kept
        if dropped:
            lines.append(f"- {dropped} more dropped by the title rules")
        lines.append("")
    # web search results for alert jobs an earlier digest already listed
    listed = {str(json.loads(r["detail"] or "{}").get("id") or "") for r in rows}
    lines += alert_search.digest_lines(conn, since, listed)
    msgs = conn.execute(q.LINKEDIN_MESSAGES_SINCE, {"since": since}).fetchall()
    if msgs:
        lines.append(f"## LinkedIn messages since the last digest ({len(msgs)})")
        lines += [_message_line(json.loads(r["detail"] or "{}"), r["job_id"]) for r in msgs]
        lines.append("")
    return lines
