"""Staging halts that wait on the human (hard rule 2: detect, abort, hand to
the human; SPEC §7.1: the job stays with the reason attached, never dropped).

A staging run that halts before filling anything (a CAPTCHA or bot check, a
government-ID or payment field, an embedded assessment, no form on the page,
no captured form spec, an apply URL off the allowlist) records an
autofill_runs row with status needs_human and nothing filled. open-queue
cannot reopen such a job, because nothing is filled to review, and the
nightly run does not stage it again. So a halt during the unattended nightly
run would reach nobody. This module lists every such job (db.STAGING_HALTS)
with the reason, the screenshot and the apply URL for:

* the digest (a "Staging" section),
* MCP get_queue (one line under the freshness banner),
* `jobkit schedule status` and MCP schedule_status (pipeline/nightly.report),
* the nightly staging stage's own summary (pipeline/nightly.stage_tailored).

A job leaves the list when it is submitted, rejected or closed, or when a
later staging run gets past the halt.

The nightly run retries two halts that can be transient
(db.NIGHTLY_STAGE_CANDIDATES): no-form-spec-captured once a form spec with
fields exists, and posting-not-found (a slow page render) one more time. A
CAPTCHA, a government-ID or payment field, an embedded assessment and an
apply URL off the allowlist are never retried automatically.

A nightly staging attempt that raised instead (page.goto timed out or hit
net::ERR_NAME_NOT_RESOLVED on a dead apply URL) writes no autofill_runs row.
The nightly run retries it until db.NIGHTLY_STAGE_FAILURE_CAP failures since
the job was last tailored, then stops; the job is listed here too
(db.STAGING_FAILURES) with reason 'staging-failed:<the newest error>', until
it is submitted, rejected, closed or re-tailored, or a staging run writes a
row for it.

A tailored job whose cover letter the claim judge never read
(pipeline/letter_gate.py) is listed too, reason
'cover-letter-needs-retailoring:<why>', whether or not a staging run has
tried it: the nightly run skips it and staging halts on it, so this is where
it reaches the human, until the job is re-tailored (or `jobkit rejudge`
passes the letter), submitted, rejected or closed."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from jobkit import db as q
from jobkit.pipeline import letter_gate

# the reason prefix of a job the nightly run stopped retrying after repeated
# staging failures (db.STAGING_FAILURES): 'staging-failed:<the newest error>'
STAGING_FAILED = "staging-failed"

# abort reason (or its prefix, before ':') -> what it means for the human
MEANING: dict[str, str] = {
    "captcha-or-turnstile-visible": "a CAPTCHA or bot check is on the page; it is never "
    "solved or retried automatically",
    # autofill/browser.CAPTCHA_PROVOKED; the control follows the colon
    "captcha-provoked-by-fill": "a CAPTCHA appeared after staging clicked the control named "
    "after the colon (a checkbox, radio, button or dropdown); staging stopped there, never "
    "touched it, and does not retry it",
    "requests-government-id-or-payment": "the form asks for a government ID or payment "
    "details; never filled automatically",
    "embedded-assessment-detected": "the form has an embedded assessment or timed test",
    "posting-not-found": "no application form rendered: the posting closed, or the page was slow",
    "no-form-spec-captured": "no form spec has been captured for this job yet",
    "apply-url-not-allowlisted": "the apply URL is not on an allowlisted host",
    "form-host-not-allowlisted": "the form redirected to a host that is not allowlisted",
    STAGING_FAILED: f"staging raised an error on {q.NIGHTLY_STAGE_FAILURE_CAP} nightly runs "
    "(the apply URL did not load, or the browser failed on the page); the nightly run "
    "stopped retrying it",
    letter_gate.REASON: letter_gate.MEANING,
}

HOW_TO = (
    "Nothing was filled or submitted. Finish each one yourself at its apply URL and "
    'submit it, then record it with MCP mark_submitted(job_ids="<id>"); or, once the '
    "cause is gone, stage it again (`jobkit stage --job-id <id>`, MCP "
    'stage_forms(job_ids="<id>")).'
)


@dataclass(frozen=True)
class Halt:
    job_id: int
    company: str
    title: str
    reason: str
    at: str
    screenshot: str | None
    apply_url: str
    next_nightly_retries: bool  # the next nightly run stages it again

    def meaning(self) -> str:
        return MEANING.get(self.reason.split(":", 1)[0], "staging stopped before filling")

    def brief(self) -> str:
        """'job 7 Acme (captcha-or-turnstile-visible)'"""
        again = ", the next nightly run retries it" if self.next_nightly_retries else ""
        reason = self.reason if len(self.reason) <= 80 else self.reason[:79] + "…"
        return f"job {self.job_id} {self.company} ({reason}{again})"

    def line(self) -> str:
        """One full line: job, reason and what it means, when, screenshot, URL."""
        again = " The next nightly run tries it again." if self.next_nightly_retries else ""
        shot = f"screenshot {self.screenshot}" if self.screenshot else "no screenshot"
        return (
            f"job {self.job_id} · {self.company} — {self.title}: {self.reason} "
            f"({self.meaning()}). Halted {self.at} UTC; {shot}; apply at "
            f"{self.apply_url or 'n/a'}.{again}"
        )


def failed(conn: sqlite3.Connection) -> list[Halt]:
    """Every job the nightly run stopped retrying after repeated staging
    failures (db.STAGING_FAILURES), with the newest error, newest first."""
    return [
        Halt(
            job_id=int(r["job_id"]),
            company=str(r["company_name"] or "?"),
            title=str(r["title"] or "?"),
            reason=f"{STAGING_FAILED}:{r['error'] or 'unknown error'}",
            at=str(r["at"] or "?"),
            screenshot=None,
            apply_url=str(r["apply_url"] or ""),
            next_nightly_retries=False,
        )
        for r in conn.execute(q.STAGING_FAILURES)
    ]


def unjudged_letters(conn: sqlite3.Connection) -> list[Halt]:
    """Every stageable job whose cover letter the claim judge never read
    (letter_gate.pending): staging halts on it and the nightly run skips it
    until it is re-tailored. Read-only."""
    return [
        Halt(
            job_id=p.job_id,
            company=p.company,
            title=p.title,
            reason=f"{letter_gate.REASON}:{p.why}",
            at=p.flagged_at or p.since or "?",
            screenshot=None,
            apply_url=p.apply_url,
            next_nightly_retries=False,
        )
        for p in letter_gate.pending(conn)
    ]


def waiting(conn: sqlite3.Connection) -> list[Halt]:
    """Every job that still waits on the human after staging stopped on it:
    its latest staging run halted before filling anything, the nightly run
    stopped retrying it after repeated failures, or its cover letter waits
    for re-tailoring (unjudged_letters). Newest first in each kind, the
    failures first, then the letters (a job is listed once)."""
    retry = {int(r["id"]) for r in conn.execute(q.NIGHTLY_STAGE_CANDIDATES)}
    gave_up = failed(conn)
    seen = {h.job_id for h in gave_up}
    letters = [h for h in unjudged_letters(conn) if h.job_id not in seen]
    seen |= {h.job_id for h in letters}
    halted: list[Halt] = []
    for r in conn.execute(q.STAGING_HALTS):
        jid = int(r["job_id"])
        if jid in seen:
            continue
        reason = str(r["abort_reason"] or "unknown")
        if reason.split(":", 1)[0] == letter_gate.REASON:
            # staging halted on the letter: listed while it is still not
            # judged, gone once it is (re-tailored, or passed by rejudge)
            still = letter_gate.check_job(conn, jid)
            if still is None:
                continue
            reason = f"{letter_gate.REASON}:{still[0]}"
        halted.append(
            Halt(
                job_id=jid,
                company=str(r["company_name"] or "?"),
                title=str(r["title"] or "?"),
                reason=reason,
                at=str(r["created_at"] or "?"),
                screenshot=str(r["screenshot_path"]) if r["screenshot_path"] else None,
                apply_url=str(r["apply_url"] or ""),
                next_nightly_retries=jid in retry,
            )
        )
    return [*gave_up, *letters, *halted]


def queue_line(conn: sqlite3.Connection, shown: int = 5) -> str | None:
    """One warning line for MCP get_queue, or None when nothing is halted."""
    halts = waiting(conn)
    if not halts:
        return None
    names = "; ".join(h.brief() for h in halts[:shown])
    more = f"; and {len(halts) - shown} more" if len(halts) > shown else ""
    return (
        f"⚠ {len(halts)} job(s) stopped in staging and wait on you: {names}{more}. "
        "schedule_status or get_digest lists the reasons, screenshots and apply URLs."
    )


def report_lines(conn: sqlite3.Connection) -> list[str]:
    """Lines for `jobkit schedule status`: a header and one line per halt."""
    halts = waiting(conn)
    if not halts:
        return ["jobs halted in staging, waiting on you: none"]
    return [
        f"⚠ jobs halted in staging, waiting on you ({len(halts)}):",
        *(f"    {h.line()}" for h in halts),
        f"  {HOW_TO}",
    ]


def digest_lines(conn: sqlite3.Connection) -> list[str]:
    """The digest's Staging section: forms ready to review, then every halt.
    Empty when there is neither."""
    halts = waiting(conn)
    # a form staged before its letter was found unjudged is not ready: the
    # letter waits for re-tailoring (letter_gate), and reopening it halts
    held = {h.job_id for h in halts if h.reason.startswith(f"{letter_gate.REASON}:")}
    ready = [r for r in conn.execute(q.OPEN_QUEUE_JOBS).fetchall() if r["job_id"] not in held]
    if not ready and not halts:
        return []
    lines = ["## Staging"]
    if ready:
        staged = sum(1 for r in ready if r["status"] == "staged")
        lines.append(
            f"{len(ready)} form(s) filled and waiting for your review ({staged} fully staged, "
            f"{len(ready) - staged} with a question left for you): `jobkit open-queue` "
            "(MCP open_queue). You click submit."
        )
    if halts:
        if ready:
            lines.append("")
        lines.append(f"### ⚠ Stopped before filling anything: waiting on you ({len(halts)})")
        lines.append(HOW_TO)
        lines.extend(f"- {h.line()}" for h in halts)
    lines.append("")
    return lines
