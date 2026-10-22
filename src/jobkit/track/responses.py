"""Employer replies to submitted applications, read from the person's own
mailbox.

jobkit never connects to a mailbox. Claude reads the person's email through
the email connector the person has set up, decides what each message is, and
records it here through four MCP tools (and the matching CLI commands, for
debugging):

* email_targets: the submitted applications worth looking for (id, company,
  title, ATS, apply host, submission time, stage), when the mailbox was last
  checked, and a suggested Gmail search for the ATS senders and company
  names since then;
* match_email: the applications one message most likely belongs to, ranked,
  with the reasons (company name in the sender, subject or snippet, the
  company's own domain, the ATS the person applied through, the title
  words). Deterministic, no LLM;
* record_response: one message's outcome for one application (acknowledged,
  rejected, assessment, interview, offer, recruiter_question);
* finish_email_check: the time the mailbox was checked through, so the next
  search starts there.

email_targets also suggests a second search, for job-alert emails
(LinkedIn's and the job boards') and LinkedIn message notifications since
the last check, and lists the LinkedIn messages recorded since then. Those
are recorded through record_job_alerts and record_linkedin_message
(track/job_alerts.py).

What is stored, as an 'email_response' event: the outcome, the day and time
the email arrived, its message id, a link to it, and a one-line summary the
caller writes (cut to SUMMARY_MAX characters). The body of an email is never
stored. Nothing is ever sent.

Stage rules, applied by record_response:

* a message id is recorded once; recording it again changes nothing;
* an acknowledgement (the automatic "we received your application") never
  changes the stage;
* a reply moves a 'ghosted' application on, like any other stage;
* a stage of 'offer' is never changed automatically (MCP record_outcome
  changes it when the person says so);
* when two responses disagree, the later-dated one decides the stage: an
  email dated before a response already recorded is kept, but leaves the
  stage alone;
* an assessment or a recruiter's question is a reply (stage 'replied'), and a
  reply never undoes a stronger outcome (interview, offer, rejected), the same
  rule MCP record_outcome follows."""

from __future__ import annotations

import datetime
import email.utils
import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from jobkit import db as q
from jobkit.log import log
from jobkit.normalize import normalize_company

EVENT = "email_response"
CHECK_EVENT = "email_check"

# outcome -> the application stage it moves to (None: never moves it)
OUTCOME_STAGE: dict[str, str | None] = {
    "acknowledged": None,
    "rejected": "rejected",
    "assessment": "replied",
    "interview": "interview",
    "offer": "offer",
    "recruiter_question": "replied",
}
OUTCOMES = tuple(OUTCOME_STAGE)
# stages a plain reply never undoes
STRONGER_THAN_REPLY = ("interview", "offer", "rejected")
# stages still worth looking for in the mailbox
OPEN_STAGES = ("submitted", "ghosted", "replied", "interview")

SUMMARY_MAX = 160
LINK_MAX = 500
MESSAGE_ID_MAX = 300
QUERY_COMPANY_MAX = 40
STAMP = "%Y-%m-%d %H:%M:%S"

# ATS key -> the domains its candidate email comes from
ATS_SENDER_DOMAINS: dict[str, tuple[str, ...]] = {
    "greenhouse": ("greenhouse.io", "greenhouse-mail.io"),
    "lever": ("lever.co",),
    "ashby": ("ashbyhq.com",),
    "workday": ("myworkday.com", "myworkdayjobs.com"),
    "icims": ("icims.com",),
    "smartrecruiters": ("smartrecruiters.com",),
    "workable": ("workable.com",),
}
# video-interview and scheduling services that write on an employer's behalf
SERVICE_SENDER_DOMAINS: dict[str, tuple[str, ...]] = {"hireflix": ("hireflix.com",)}
SEARCH_DOMAINS: tuple[str, ...] = tuple(
    d
    for domains in (*ATS_SENDER_DOMAINS.values(), *SERVICE_SENDER_DOMAINS.values())
    for d in domains
)

# job-alert emails and LinkedIn message notifications (track/job_alerts.py):
# the person's own mail, read; the LinkedIn site is never fetched
LINKEDIN_ALERT_SENDERS = ("jobalerts-noreply@linkedin.com", "jobs-noreply@linkedin.com")
LINKEDIN_MESSAGE_SENDERS = ("messages-noreply@linkedin.com", "inmail-hit-reply@linkedin.com")
JOB_BOARD_ALERT_DOMAINS = ("indeed.com", "glassdoor.com", "ziprecruiter.com", "wellfound.com")
ALERT_SENDERS: tuple[str, ...] = (
    *LINKEDIN_ALERT_SENDERS,
    *LINKEDIN_MESSAGE_SENDERS,
    *JOB_BOARD_ALERT_DOMAINS,
)
ALERT_FIRST_DAYS = 7  # how far back the first alert search goes

# words too common in titles to tell two applications apart
_TITLE_STOP = frozenset(
    {"a", "an", "and", "the", "of", "for", "to", "in", "on", "at", "with", "or", "i", "ii"}
    | {"iii", "iv", "sr", "jr", "senior", "junior", "lead", "remote", "hybrid", "onsite"}
    | {"us", "usa"}
)
_WORD = re.compile(r"[a-z0-9]+")


# ---- time ------------------------------------------------------------------------


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def parse_when(text: str) -> datetime.datetime:
    """An ISO date or date-time ('2026-10-14', '2026-10-14T09:30:00Z') or an
    email Date header ('Tue, 14 Oct 2026 09:30:00 -0400'), as UTC. A time
    with no zone is taken as UTC. Raises ValueError."""
    s = (text or "").strip()
    if not s:
        raise ValueError("a date is required (YYYY-MM-DD, ISO date-time or an email Date header)")
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = email.utils.parsedate_to_datetime(s)
        except (TypeError, ValueError, IndexError):
            raise ValueError(
                f"not a date: {s[:60]!r} (use YYYY-MM-DD, an ISO date-time or an email Date header)"
            ) from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    dt = dt.astimezone(datetime.UTC)
    if dt > _now() + datetime.timedelta(days=1):
        raise ValueError(f"{s[:60]!r} is in the future")
    return dt


def _stamp(dt: datetime.datetime) -> str:
    return dt.strftime(STAMP)


# ---- what to look for ------------------------------------------------------------


def _host(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def ats_of(ats: str | None, apply_url: str | None) -> str | None:
    """The ATS an application went through: the board's, else the one its
    apply link points at."""
    if ats:
        return str(ats).lower()
    host = _host(apply_url) or ""
    for key, domains in ATS_SENDER_DOMAINS.items():
        if any(host == d or host.endswith("." + d) for d in domains):
            return key
    return None


@dataclass(frozen=True)
class Target:
    application_id: int
    job_id: int
    company: str
    title: str
    ats: str | None
    apply_host: str | None
    submitted_at: str
    stage: str

    def line(self) -> str:
        via = f" via {self.ats}" if self.ats else ""
        host = f" ({self.apply_host})" if self.apply_host else ""
        return (
            f"- application {self.application_id} (job {self.job_id}): {self.company} — "
            f"{self.title}{via}{host}, submitted {self.submitted_at[:10]}, stage {self.stage}"
        )


@dataclass
class EmailTargets:
    targets: list[Target]
    last_checked_through: str | None
    search_after: datetime.date | None
    query: str
    sender_domains: tuple[str, ...] = SEARCH_DOMAINS
    companies_left_out: int = 0
    # job-alert emails and LinkedIn messages (track/job_alerts.py)
    alert_after: datetime.date | None = None
    alert_query: str = ""
    alert_senders: tuple[str, ...] = ALERT_SENDERS
    alert_emails_recorded: int = 0
    linkedin_messages: list[str] = field(default_factory=list)

    def render(self) -> str:
        return "\n".join([*self._application_lines(), "", *self._alert_lines()])

    def _alert_lines(self) -> list[str]:
        lines = [
            f"Job alerts and LinkedIn messages: suggested Gmail search: {self.alert_query}",
            "For a job-alert email (LinkedIn, Indeed, Glassdoor, ZipRecruiter, Wellfound), "
            "open it and list each job's company, title and location, and whether it shows "
            "an 'apply on company site' link. Do not open or follow any link in it. Call "
            "record_job_alerts once per email with those jobs, its message id, the date it "
            "arrived and the sender (source: linkedin, indeed, ...). jobkit keeps the jobs "
            "that pass the title rules and looks for each one on the employer's own "
            "Greenhouse, Lever or Ashby board; finish_email_check starts that lookup.",
            "For a LinkedIn message or InMail notification, note the sender's company if "
            "shown, and one line in your words about who wrote (by role, never by name) and "
            "what they want; call record_linkedin_message. Never reply.",
        ]
        if self.alert_emails_recorded or self.linkedin_messages:
            lines.append(
                f"Since the last check: {self.alert_emails_recorded} job-alert email(s) and "
                f"{len(self.linkedin_messages)} LinkedIn message(s) recorded."
            )
        if self.linkedin_messages:
            lines += ["LinkedIn messages to include in the summary:", *self.linkedin_messages]
        return lines

    def _application_lines(self) -> list[str]:
        if not self.targets:
            return [
                "No submitted application is waiting on a reply (stages "
                f"{', '.join(OPEN_STAGES)}), so there is nothing to look for. Mark "
                "applications with mark_submitted first."
            ]
        checked = (
            f"Mailbox last checked through {self.last_checked_through} UTC."
            if self.last_checked_through
            else "The mailbox has not been checked before."
        )
        lines = [
            f"{len(self.targets)} submitted application(s) to look for. {checked}",
            *[t.line() for t in self.targets],
            "",
            f"Suggested Gmail search: {self.query}",
        ]
        if self.companies_left_out:
            lines.append(
                f"({self.companies_left_out} more company name(s) left out of the search to "
                "keep it short; search for them separately if needed.)"
            )
        lines += [
            "",
            "Work from each message's sender, subject, date and snippet (open a "
            "message only when the person is present and those don't settle it); "
            "nothing is sent and nothing is changed in the mailbox. Call match_email "
            "to find its application, then record_email_response with the outcome "
            "and a one-line summary. Call finish_email_check when done.",
        ]
        return lines


def last_check(conn: sqlite3.Connection) -> str | None:
    """The time the mailbox was last checked through ('YYYY-MM-DD HH:MM:SS'
    UTC), or None."""
    row = conn.execute(q.LAST_EMAIL_CHECK).fetchone()
    if row is None:
        return None
    try:
        through = json.loads(row["detail"] or "{}").get("checked_through")
    except (ValueError, AttributeError):
        through = None
    return str(through or row["at"])


def _quoted(name: str) -> str:
    return '"' + name.replace('"', "").strip() + '"'


def alert_query(after: datetime.date | None) -> str:
    """after:YYYY/MM/DD {from:<alert or LinkedIn sender> ...}"""
    query = "{" + " ".join(f"from:{s}" for s in ALERT_SENDERS) + "}"
    return (f"after:{after.strftime('%Y/%m/%d')} " if after else "") + query


def gmail_query(after: datetime.date | None, companies: list[str]) -> str:
    """after:YYYY/MM/DD {from:<ATS domain> ... "Company" ...}: Gmail treats
    the braces as OR."""
    terms = [f"from:{d}" for d in SEARCH_DOMAINS] + [_quoted(c) for c in companies if c.strip()]
    query = "{" + " ".join(terms) + "}"
    return (f"after:{after.strftime('%Y/%m/%d')} " if after else "") + query


def email_targets(conn: sqlite3.Connection) -> EmailTargets:
    """The submitted applications still waiting on news (stage submitted,
    ghosted, replied or interview), the last check time, and a Gmail search
    from a day before the last check (or from the first submission)."""
    targets = [
        Target(
            application_id=int(r["application_id"]),
            job_id=int(r["job_id"]),
            company=str(r["company_name"]),
            title=str(r["title"]),
            ats=ats_of(r["ats"], r["apply_url"]),
            apply_host=_host(r["apply_url"]),
            submitted_at=str(r["submitted_at"]),
            stage=str(r["stage"]),
        )
        for r in conn.execute(q.SUBMITTED_APPLICATIONS)
        if r["stage"] in OPEN_STAGES
    ]
    through = last_check(conn)
    after: datetime.date | None = None
    if through:
        after = datetime.date.fromisoformat(through[:10]) - datetime.timedelta(days=1)
    elif targets:
        after = min(datetime.date.fromisoformat(t.submitted_at[:10]) for t in targets)
    companies: list[str] = []
    for t in targets:
        if t.company not in companies:
            companies.append(t.company)
    left_out = max(0, len(companies) - QUERY_COMPANY_MAX)
    from jobkit.track import job_alerts  # it builds on this module

    check_row = conn.execute(q.LAST_EMAIL_CHECK).fetchone()
    after_id = int(check_row["id"]) if check_row is not None else 0
    alert_after = (
        datetime.date.fromisoformat(through[:10]) - datetime.timedelta(days=1)
        if through
        else _now().date() - datetime.timedelta(days=ALERT_FIRST_DAYS)
    )
    return EmailTargets(
        targets=targets,
        last_checked_through=through,
        search_after=after,
        query=gmail_query(after, companies[:QUERY_COMPANY_MAX]) if targets else "",
        companies_left_out=left_out,
        alert_after=alert_after,
        alert_query=alert_query(alert_after),
        alert_emails_recorded=job_alerts.alert_emails_after(conn, after_id),
        linkedin_messages=job_alerts.messages_after(conn, after_id),
    )


# ---- which application a message belongs to --------------------------------------


@dataclass
class Candidate:
    application_id: int
    job_id: int
    company: str
    title: str
    stage: str
    submitted_at: str
    score: int
    reasons: list[str] = field(default_factory=list)

    def line(self) -> str:
        return (
            f"- application {self.application_id} (job {self.job_id}): {self.company} — "
            f"{self.title}, stage {self.stage}, submitted {self.submitted_at[:10]}: score "
            f"{self.score} ({'; '.join(self.reasons)})"
        )


@dataclass
class EmailMatch:
    candidates: list[Candidate]
    sender_ats: str | None

    @property
    def best(self) -> Candidate | None:
        """The one clear match: a score of at least CLEAR_SCORE and at least
        CLEAR_MARGIN ahead of the next candidate."""
        if not self.candidates:
            return None
        top = self.candidates[0]
        if top.score < CLEAR_SCORE:
            return None
        if len(self.candidates) > 1 and top.score - self.candidates[1].score < CLEAR_MARGIN:
            return None
        return top

    def render(self) -> str:
        if not self.candidates:
            return (
                "No submitted application matches this message (no company name, company "
                "domain or title found). Skip it, or ask the person which application it is."
            )
        lines = [c.line() for c in self.candidates]
        best = self.best
        if best is not None:
            lines.append(f"Clear match: application {best.application_id}.")
        else:
            lines.append(
                "No clear match: ask the person which application this is before recording it."
            )
        return "\n".join(lines)


CLEAR_SCORE = 50
CLEAR_MARGIN = 20
MATCH_LIMIT = 5


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _phrase_in(phrase: list[str], words: list[str]) -> bool:
    n = len(phrase)
    if not n:
        return False
    return any(words[i : i + n] == phrase for i in range(len(words) - n + 1))


def _sender_ats(domain: str) -> str | None:
    for key, domains in {**ATS_SENDER_DOMAINS, **SERVICE_SENDER_DOMAINS}.items():
        if any(domain == d or domain.endswith("." + d) for d in domains):
            return key
    return None


def _company_domain_match(domain: str, company_domain: str | None, compact: str) -> str | None:
    if not domain or _sender_ats(domain):
        return None
    cd = (company_domain or "").lower().strip().removeprefix("www.")
    if cd and (domain == cd or domain.endswith("." + cd)):
        return "sent from the company's domain"
    labels = [lb for lb in re.split(r"[.\-]", domain) if lb]
    if compact and len(compact) >= 3 and compact in labels[:-1]:
        return "the sender's domain names the company"
    return None


def match_email(
    conn: sqlite3.Connection,
    sender: str,
    subject: str = "",
    snippet: str = "",
    received_at: str = "",
) -> EmailMatch:
    """Rank the submitted applications one message could belong to, best
    first, with the reasons. Deterministic: company name (in the sender's
    name, the subject or the snippet), the company's domain, the ATS the
    person applied through, the title words, and whether the message came
    after the submission. A candidate needs a company or title signal; the
    ATS alone matches nothing."""
    name, address = email.utils.parseaddr(sender or "")
    address = address.lower()
    domain = address.rsplit("@", 1)[-1] if "@" in address else ""
    sender_ats = _sender_ats(domain)
    name_words = _words(name)
    body_words = _words(f"{subject} {snippet}")
    received: datetime.datetime | None = None
    if received_at.strip():
        received = parse_when(received_at)

    out: list[Candidate] = []
    for r in conn.execute(q.SUBMITTED_APPLICATIONS):
        reasons: list[str] = []
        score = 0
        company_words = normalize_company(str(r["company_name"])).split()
        compact = "".join(company_words)
        signal = False
        if _phrase_in(company_words, body_words):
            score += 50
            reasons.append("company name in the subject or snippet")
            signal = True
        if _phrase_in(company_words, name_words):
            score += 40
            reasons.append("company name in the sender's name")
            signal = True
        why = _company_domain_match(domain, r["company_domain"], compact)
        if why:
            score += 50
            reasons.append(why)
            signal = True
        title_words = [w for w in _words(str(r["title"])) if w not in _TITLE_STOP]
        if title_words:
            present = sum(1 for w in title_words if w in body_words)
            if present == len(title_words):
                score += 25
                reasons.append("the job title")
                signal = True
            elif present * 2 >= len(title_words):
                score += 10
                reasons.append("most of the title's words")
        if not signal:
            continue
        app_ats = ats_of(r["ats"], r["apply_url"])
        if sender_ats and app_ats and sender_ats == app_ats:
            score += 15
            reasons.append(f"sent through {sender_ats}, where this application went")
        elif sender_ats in SERVICE_SENDER_DOMAINS:
            reasons.append(f"sent through {sender_ats} for the employer")
        submitted = str(r["submitted_at"])
        if received is not None and received.date().isoformat() < submitted[:10]:
            score -= 30
            reasons.append("received before this application was submitted")
        out.append(
            Candidate(
                application_id=int(r["application_id"]),
                job_id=int(r["job_id"]),
                company=str(r["company_name"]),
                title=str(r["title"]),
                stage=str(r["stage"]),
                submitted_at=submitted,
                score=score,
                reasons=reasons,
            )
        )
    # best score first; on a tie, the more recent application
    out.sort(key=lambda c: (-c.score, -_ordinal(c.submitted_at), c.application_id))
    return EmailMatch(candidates=out[:MATCH_LIMIT], sender_ats=sender_ats)


def _ordinal(stamp: str) -> int:
    try:
        return datetime.date.fromisoformat(stamp[:10]).toordinal()
    except ValueError:
        return 0


# ---- recording a response --------------------------------------------------------


def normalize_outcome(outcome: str) -> str:
    key = re.sub(r"[\s\-]+", "_", (outcome or "").strip().lower())
    if key not in OUTCOME_STAGE:
        raise ValueError(f"outcome must be one of {', '.join(OUTCOMES)}, got {outcome!r}")
    return key


def one_line_summary(summary: str) -> str:
    """The caller's summary, cut to SUMMARY_MAX characters. It must be one
    line: a summary with a line break in it is refused (ValueError), since
    that is the shape of an email body pasted in, and no body is stored."""
    text = (summary or "").strip()
    if len(text.splitlines()) > 1:
        raise ValueError(
            "summary must be one line (no line breaks): say in your own words what the "
            "email means, not its text"
        )
    s = " ".join(text.split())
    if len(s) > SUMMARY_MAX:
        s = s[: SUMMARY_MAX - 1].rstrip() + "…"
    return s


def _clean_message_id(message_id: str) -> str:
    mid = (message_id or "").strip()
    if not mid:
        raise ValueError(
            "message_id is required: it is what keeps a message from being counted twice"
        )
    if len(mid) > MESSAGE_ID_MAX or any(ch.isspace() for ch in mid):
        raise ValueError(
            f"message_id must be one token of at most {MESSAGE_ID_MAX} characters (the "
            "message's id, not its text)"
        )
    return mid


def _clean_link(link: str) -> str:
    url = (link or "").strip()
    if not url:
        return ""
    if len(url) > LINK_MAX or any(ch.isspace() for ch in url):
        raise ValueError(f"link must be one URL of at most {LINK_MAX} characters")
    if urlsplit(url).scheme not in ("https", "http"):
        raise ValueError("link must be an http(s) URL to the message")
    return url


@dataclass
class Recorded:
    application_id: int
    job_id: int
    company: str
    title: str
    outcome: str
    received_at: str
    stage_before: str
    stage_after: str
    already: bool = False
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        name = f"application {self.application_id} ({self.company} — {self.title})"
        if self.already:
            head = f"already recorded: this message is {self.outcome} for {name}; nothing changed"
        else:
            head = (
                f"recorded {self.outcome} ({self.received_at[:10]}) for {name}: stage "
                f"{self.stage_before} -> {self.stage_after}"
            )
        return head + "".join(f"; {n}" for n in self.notes)


def record_response(
    conn: sqlite3.Connection,
    application_id: int,
    outcome: str,
    received_at: str,
    message_id: str,
    link: str = "",
    summary: str = "",
) -> Recorded:
    """Record one email's outcome for one submitted application (see the
    module docstring for the stage rules). Stores only the outcome, the
    arrival time, the message id, the link and the one-line summary. Raises
    ValueError on bad input, with nothing written."""
    key = normalize_outcome(outcome)
    mid = _clean_message_id(message_id)
    url = _clean_link(link)
    line = one_line_summary(summary)
    when = _stamp(parse_when(received_at))

    seen = conn.execute(q.EMAIL_RESPONSE_BY_MESSAGE_ID, {"message_id": mid}).fetchone()
    if seen is not None:
        detail: dict[str, Any] = json.loads(seen["detail"] or "{}")
        prior = conn.execute(q.APPLICATION_BY_ID, {"id": detail.get("application_id")}).fetchone()
        rec = Recorded(
            application_id=int(detail.get("application_id") or 0),
            job_id=int(seen["job_id"] or 0),
            company=str(prior["company_name"]) if prior else "?",
            title=str(prior["title"]) if prior else "?",
            outcome=str(detail.get("outcome")),
            received_at=str(detail.get("received_at") or ""),
            stage_before=str(detail.get("stage_before") or ""),
            stage_after=str(detail.get("stage_after") or ""),
            already=True,
        )
        if rec.application_id != int(application_id):
            rec.notes.append(
                f"it was recorded for application {rec.application_id}, not {application_id}"
            )
        return rec

    app = conn.execute(q.APPLICATION_BY_ID, {"id": int(application_id)}).fetchone()
    if app is None:
        raise ValueError(f"no application with id {application_id} (email_targets lists them)")
    if not app["submitted_at"]:
        raise ValueError(
            f"application {application_id} is not marked submitted: call "
            f"mark_submitted(job_ids='{app['job_id']}', submitted_on='YYYY-MM-DD') first"
        )

    before = str(app["stage"])
    target = OUTCOME_STAGE[key]
    after = before
    notes: list[str] = []
    latest_row = conn.execute(
        q.LATEST_RESPONSE_FOR_APPLICATION, {"application_id": int(application_id)}
    ).fetchone()
    latest = str(latest_row["latest"]) if latest_row and latest_row["latest"] else None
    if target is None:
        notes.append("an acknowledgement does not change the stage")
    elif before == "offer" and target != "offer":
        notes.append(
            "stage 'offer' is never changed automatically; use record_outcome if it changed"
        )
    elif latest is not None and when < latest:
        notes.append(f"a later response ({latest[:10]}) is already recorded, so the stage stays")
    elif target == "replied" and before in STRONGER_THAN_REPLY:
        notes.append(f"a reply does not undo stage '{before}'")
    else:
        after = target
    if before == "ghosted" and after != before:
        notes.append("it had been marked ghosted (no reply); now it has one")
    if str(app["submitted_at"])[:10] > when[:10]:
        notes.append(
            f"this email is dated before the recorded submission ({str(app['submitted_at'])[:10]})"
        )

    if after != before:
        conn.execute(q.SET_APPLICATION_STAGE, {"id": int(application_id), "stage": after})
    conn.execute(
        q.RECORD_EVENT,
        {
            "job_id": int(app["job_id"]),
            "kind": EVENT,
            "detail": json.dumps(
                {
                    "application_id": int(application_id),
                    "outcome": key,
                    "received_at": when,
                    "message_id": mid,
                    "link": url,
                    "summary": line,
                    "stage_before": before,
                    "stage_after": after,
                },
                sort_keys=True,
            ),
        },
    )
    conn.commit()
    log(
        "responses.recorded",
        application_id=int(application_id),
        job_id=int(app["job_id"]),
        outcome=key,
        stage_before=before,
        stage_after=after,
    )
    return Recorded(
        application_id=int(application_id),
        job_id=int(app["job_id"]),
        company=str(app["company_name"]),
        title=str(app["title"]),
        outcome=key,
        received_at=when,
        stage_before=before,
        stage_after=after,
        notes=notes,
    )


# ---- the end of a check ----------------------------------------------------------


@dataclass
class Check:
    checked_through: str
    previous: str | None
    recorded_since: int
    alert_emails_since: int = 0
    linkedin_messages_since: int = 0
    alert_cards_since: int = 0  # alert jobs handed to board lookup (job_alerts.py)
    alert_jobs: list[str] = field(default_factory=list)  # job_alerts.items_after
    # leads web search kept since the previous check (alert_search.lead_lines_after)
    leads: list[str] = field(default_factory=list)

    def render(self) -> str:
        prev = f" (previous check: through {self.previous} UTC)" if self.previous else ""
        more = ""
        if self.alert_emails_since or self.linkedin_messages_since:
            more = (
                f", {self.alert_emails_since} job-alert email(s) and "
                f"{self.linkedin_messages_since} LinkedIn message(s)"
            )
        head = (
            f"mailbox checked through {self.checked_through} UTC{prev}; "
            f"{self.recorded_since} response(s){more} recorded since the previous check"
        )
        lines = [head]
        if self.alert_jobs:
            lines += ["Jobs from job-alert emails in this check:", *self.alert_jobs]
        if self.leads:
            lines += [
                "Found by web search on sites jobkit does not read (apply there yourself):",
                *self.leads,
            ]
        return "\n".join(lines)


def finish_email_check(conn: sqlite3.Connection, checked_through: str = "") -> Check:
    """Record that the mailbox was read through `checked_through` (default:
    now), as an 'email_check' event. The next email_targets search starts a
    day before it."""
    when = _stamp(parse_when(checked_through) if checked_through.strip() else _now())
    previous = last_check(conn)
    prev_row = conn.execute(q.LAST_EMAIL_CHECK).fetchone()
    after_id = int(prev_row["id"]) if prev_row is not None else 0
    recorded = int(
        conn.execute(q.COUNT_EMAIL_RESPONSES_AFTER, {"after_id": after_id}).fetchone()[0]
    )
    from jobkit.track import job_alerts  # it builds on this module

    alerts = job_alerts.alert_emails_after(conn, after_id)
    messages = len(job_alerts.messages_after(conn, after_id))
    cards = job_alerts.alert_cards_after(conn, after_id)
    alert_jobs = job_alerts.items_after(conn, after_id)
    from jobkit.track import alert_search  # it builds on this module

    leads = alert_search.lead_lines_after(conn, after_id)
    conn.execute(
        q.RECORD_EVENT,
        {"job_id": None, "kind": CHECK_EVENT, "detail": json.dumps({"checked_through": when})},
    )
    conn.commit()
    log(
        "responses.check_finished",
        checked_through=when,
        recorded=recorded,
        alert_emails=alerts,
        linkedin_messages=messages,
        alert_cards=cards,
    )
    return Check(
        checked_through=when,
        previous=previous,
        recorded_since=recorded,
        alert_emails_since=alerts,
        linkedin_messages_since=messages,
        alert_cards_since=cards,
        alert_jobs=alert_jobs,
        leads=leads,
    )


# ---- the digest ------------------------------------------------------------------


def digest_lines(conn: sqlite3.Connection, since: str) -> list[str]:
    """'Replies recorded since the last digest': every email response
    recorded at or after `since` ('YYYY-MM-DD HH:MM:SS' UTC), then the
    job-alert and LinkedIn message sections (job_alerts.digest_lines). Empty
    when there is nothing."""
    from jobkit.track import job_alerts  # it builds on this module

    rows = conn.execute(q.EMAIL_RESPONSES_SINCE, {"since": since}).fetchall()
    if not rows:
        return job_alerts.digest_lines(conn, since)
    lines = [f"## Replies recorded since the last digest ({len(rows)})"]
    for r in rows:
        d = json.loads(r["detail"] or "{}")
        moved = (
            f"stage {d.get('stage_before')} -> {d.get('stage_after')}"
            if d.get("stage_before") != d.get("stage_after")
            else f"stage {d.get('stage_after')}"
        )
        summary = f": {d['summary']}" if d.get("summary") else ""
        lines.append(
            f"- {r['company_name'] or '?'} — {r['title'] or '?'} (job {r['job_id']}): "
            f"{d.get('outcome')}, received {str(d.get('received_at') or '')[:10]}, {moved}{summary}"
        )
    lines.append("")
    return lines + job_alerts.digest_lines(conn, since)
