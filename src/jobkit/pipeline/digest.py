"""Stage 13 — daily digest: out/digest/{date}.md. New queue entries, sanity
flags, stale boards, and the funnel with conversion rates at each step.

The digest opens with a freshness banner (audit 2026-10-08 §2 item 7): how
long ago the last successful board poll was, with a warning past
`schedule.stale_after_hours` (filters.yaml, default 36), then the last
successful nightly run (`jobkit nightly`) under the same threshold, a nightly
run that started and has not finished, and any stage of the last run (run-all
or nightly) that failed. MCP get_queue and get_stats print the same banner,
computed live. A 35-day-old queue must never look like a quiet market.

Next comes the Staging section (pipeline/halts.py): how many filled forms
wait for review in open-queue, and every job whose latest staging run
stopped before filling anything (CAPTCHA, a government-ID or payment field,
an assessment, no form), with the reason, the screenshot and the apply URL,
plus every job the nightly run stopped retrying after repeated staging
failures (a dead apply URL), with the error, and every tailored job whose
cover letter the claim judge never read (pipeline/letter_gate.py: not
staged until it is re-tailored). The nightly run stages
unattended, so this is where its halts reach the human (hard rule 2).

Then "Questions for you" (pipeline/questions.py): at most
`questions.max_per_day` facts only the human knows that would close gaps in
open jobs, each with why it matters and the jobs it helps, plus the answers
that wait for confirmation before they reach the corpus.

Then, when LinkedIn guest discovery is on (feeds.linkedin_guest) or ran that
day, one line: searches made, new postings found through the employer's ATS,
companies queued for board discovery, and whether LinkedIn blocked us
(discovery/linkedin_guest.py).

Then the board-resolution section (discovery/board_resolver.py), when a run
happened that day or a flag stands: aggregator jobs merged into the
employer's own posting, LinkedIn cards found on the employer's board, and
every job flagged board-match-ambiguous with its candidate postings and how
to pick one.

Then "Replies recorded since the last digest" (track/responses.py): employer
emails the person's mailbox showed and Claude recorded through MCP
record_email_response since the previous digest was written, each
with its outcome and the stage it left. Nothing when there are none.

The funnel ends, when there are any, with how many submitted applications
had no reply after tracking.ghost_after_days (the 'ghost-close' stage marks
them ghosted). After it, when any were set aside since the last digest, a
line counting feed jobs whose description was too short to score."""

from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path

from jobkit import db as q
from jobkit import paths
from jobkit.config import load_filters
from jobkit.discovery import board_resolver, linkedin_guest
from jobkit.pipeline import halts, letter_gate, questions
from jobkit.pipeline.runner import NIGHTLY, ago, last_successful_nightly, latest_nightly
from jobkit.track import responses

DIGEST_DIR = paths.HOME / "out" / "digest"


def _nightly_lines(conn: sqlite3.Connection, limit: float) -> list[str]:
    """The last successful nightly run (a warning past the threshold, or when
    nightly runs were attempted and none succeeded), and a nightly run that
    started and has not finished: the one writing this digest, or one that
    was stopped."""
    latest = latest_nightly(conn)
    if latest is None:
        return ["No nightly run on record: `jobkit schedule install` sets one up."]
    lines: list[str] = []
    running = latest.alive()
    ok = last_successful_nightly(conn)
    if ok is None:
        if not running:  # the first nightly, still running, has not failed yet
            lines.append(
                "⚠ No nightly run has succeeded yet. See `jobkit schedule status` (MCP "
                "schedule_status) and logs/jobkit.jsonl (run_all.stage)."
            )
    else:
        at, hours = ok
        seen = f"last successful nightly run {ago(hours)} ({at} UTC)"
        if hours > limit:
            lines.append(
                f"⚠ STALE NIGHTLY: {seen}, more than {limit:g} hours. Check "
                "`jobkit schedule status` (MCP schedule_status): is the Mac awake at "
                "night, and is the schedule installed?"
            )
        else:
            lines.append(seen[0].upper() + seen[1:] + ".")
    if running:
        lines.append(
            f"A nightly run started {latest.started} UTC is still running (pid "
            f"{latest.pid}); `jobkit schedule status` (MCP schedule_status) shows its "
            "result once it finishes."
        )
    elif not latest.done:
        lines.append(
            f"⚠ The nightly run started {latest.started} UTC did not finish (stopped, "
            "crashed, or the Mac shut down). See logs/nightly.err.log."
        )
    return lines


def freshness_banner(conn: sqlite3.Connection, stale_after_hours: float | None = None) -> str:
    """A few lines: the last successful board poll and how long ago it was (a
    warning past the threshold, or when no poll ever succeeded), the last
    successful nightly run (same threshold), a nightly run that has not
    finished, and the stages of the latest run (run-all or nightly) that
    failed, if any."""
    limit = load_filters().stale_after_hours if stale_after_hours is None else stale_after_hours
    lines: list[str] = []
    row = conn.execute(q.LAST_SUCCESSFUL_POLL).fetchone()
    if row is None or row["at"] is None:
        lines.append(
            "⚠ STALE DATA: no successful board poll on record. Nothing here reflects "
            "current postings; run `jobkit run-all` (or `jobkit poll`)."
        )
    else:
        hours = float(row["hours"])
        seen = f"last successful poll {ago(hours)} ({row['at']} UTC)"
        if hours > limit:
            lines.append(
                f"⚠ STALE DATA: {seen}, more than {limit:g} hours. Nothing newer is in "
                "the queue; run `jobkit nightly` (or `jobkit run-all`) and check that "
                "the schedule is installed (`jobkit schedule status`)."
            )
        else:
            lines.append(seen[0].upper() + seen[1:] + ".")
    lines.extend(_nightly_lines(conn, limit))
    failed = conn.execute(q.LAST_RUN_FAILED_STAGES).fetchall()
    if failed:
        why = {"llm_all_failed": "all LLM calls failed"}
        parts = [f"{r['stage']} ({why.get(r['status'], 'error')})" for r in failed]
        which = "nightly run" if failed[0]["mode"] == NIGHTLY else "run-all"
        lines.append(
            f"⚠ The last {which} ({failed[0]['at']} UTC) had failed stages: "
            f"{', '.join(parts)}. See logs/jobkit.jsonl (run_all.stage)."
        )
    return "\n".join(lines)


def _funnel(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    c = conn.execute
    seen = c("SELECT COUNT(*) n FROM jobs").fetchone()["n"]
    rejected = c("SELECT COUNT(*) n FROM jobs WHERE status='rejected'").fetchone()["n"]
    queued = c("SELECT COUNT(*) n FROM jobs WHERE status='queued'").fetchone()["n"]
    approved = c("SELECT COUNT(*) n FROM jobs WHERE status='approved'").fetchone()["n"]
    applied = c("SELECT COUNT(*) n FROM jobs WHERE status='applied'").fetchone()["n"]
    return [
        ("seen", seen),
        ("rejected", rejected),
        ("queued", queued),
        ("approved", approved),
        ("submitted", applied),
    ]


def previous_digest_time(date: str) -> str:
    """When the newest digest before `date` was written ('YYYY-MM-DD
    HH:MM:SS' UTC, from its file), or the start of the day a week before
    `date` when there is none."""
    earlier = sorted(p for p in DIGEST_DIR.glob("????-??-??.md") if p.stem < date and p.is_file())
    if earlier:
        written = datetime.datetime.fromtimestamp(earlier[-1].stat().st_mtime, datetime.UTC)
        return written.strftime("%Y-%m-%d %H:%M:%S")
    week = datetime.date.fromisoformat(date) - datetime.timedelta(days=7)
    return f"{week.isoformat()} 00:00:00"


SHORT_FEED_JD_LISTED = 10


def short_feed_jd_rows(
    conn: sqlite3.Connection, since: str = "", limit: int = SHORT_FEED_JD_LISTED
) -> list[str]:
    """One line per feed job rejected since `since` for a description too
    short to score (status still rejected), newest first: title, company,
    location and link."""
    rows = conn.execute(q.LIST_FEED_JOBS_SHORT_JD, {"since": since, "limit": limit}).fetchall()
    out = []
    for r in rows:
        where = f", {r['location_raw']}" if r["location_raw"] else ""
        out.append(f"- {r['title']}, {r['company_name']}{where}: {r['apply_url']}")
    return out


def short_feed_jd_lines(conn: sqlite3.Connection, since: str) -> list[str]:
    """The feed jobs rejected since `since` because their description was
    too short to score (jd_missing), listed with their links. Feeds such as
    Adzuna send only a snippet, so without this section those jobs vanish
    unseen. An approved job that tailoring refused for the same reason is not
    counted: it was never kept out of the queue."""
    n = conn.execute(q.COUNT_FEED_JOBS_SHORT_JD, {"since": since}).fetchone()["n"]
    if not n:
        return []
    lines = [
        "## Feed jobs not scored",
        f"{n} job{'s' if n != 1 else ''} from the job feeds came with too short a "
        "description to score, so they were not queued. If one looks right, open it "
        "on the feed's site and apply on the employer's site.",
        *short_feed_jd_rows(conn, since),
    ]
    if n > SHORT_FEED_JD_LISTED:
        lines.append(
            f"- and {n - SHORT_FEED_JD_LISTED} more (get_stats lists the newest "
            f"{STATS_SHORT_FEED_JD_LISTED})"
        )
    lines.append("")
    return lines


STATS_SHORT_FEED_JD_LISTED = 25


def write_digest(conn: sqlite3.Connection, date: str | None = None) -> Path:
    date = date or datetime.date.today().isoformat()
    lines = [f"# jobkit digest — {date}", "", freshness_banner(conn), ""]
    # staging first: forms waiting for the human's click, and jobs a staging
    # run (often the unattended nightly) stopped on and handed to the human,
    # with every cover letter the claim judge never read marked on the way
    # (a cover_needs_retailor event; halts.py lists them)
    letter_gate.sweep(conn)
    lines.extend(halts.digest_lines(conn))
    # then the day's questions for the human (capped by questions.max_per_day)
    # and the answers waiting for their confirmation (pipeline/questions.py)
    lines.extend(questions.digest_lines(conn))
    # LinkedIn guest discovery: searched / new via ATS / queued companies /
    # blocked, for the day (nothing when it is off and nothing ran)
    lines.extend(linkedin_guest.digest_lines(conn, day=date))
    # aggregator jobs matched to the employer's own posting today, and every
    # job flagged board-match-ambiguous (nothing picked: the human picks)
    lines.extend(board_resolver.digest_lines(conn, day=date))

    queue = conn.execute(
        """SELECT j.id, j.title, j.company_name, j.comp_min, j.comp_max, j.remote,
                  sc.fit_score, sc.gaps, s.flags
           FROM jobs j JOIN scores sc ON sc.job_id = j.id
           LEFT JOIN sanity s ON s.job_id = j.id
           WHERE j.status = 'queued'
           ORDER BY sc.fit_score DESC LIMIT 25"""
    ).fetchall()
    lines.append(f"## Review queue ({len(queue)} shown, sorted by fit)")
    for r in queue:
        comp = (
            f"${r['comp_min'] // 1000}–{r['comp_max'] // 1000}k"
            if r["comp_min"] and r["comp_max"]
            else "comp n/a"
        )
        remote = {1: "remote", 0: "onsite/hybrid", None: "loc?"}[r["remote"]]
        gaps = ", ".join(json.loads(r["gaps"] or "[]")[:3]) or "none"
        flags = json.loads(r["flags"] or "[]") if r["flags"] else []
        flag_str = f"  ⚠ {'; '.join(flags)}" if flags else ""
        lines.append(
            f"- **{r['fit_score']}** · {r['company_name']} — {r['title']} · "
            f"{comp} · {remote} · gaps: {gaps}{flag_str}"
        )
    lines.append("")
    # employer replies recorded from the person's mailbox since the last digest
    lines.extend(responses.digest_lines(conn, previous_digest_time(date)))

    flagged = conn.execute(
        """SELECT j.company_name, j.title, s.flags FROM sanity s
           JOIN jobs j ON j.id = s.job_id
           WHERE s.flags != '[]' AND j.status IN ('filtered', 'queued')
           LIMIT 15"""
    ).fetchall()
    if flagged:
        lines.append("## Sanity flags")
        for r in flagged:
            for fl in json.loads(r["flags"]):
                lines.append(f"- {r['company_name']} — {r['title']}: {fl}")
        lines.append("")

    stale = conn.execute("SELECT ats, token FROM boards WHERE status='stale'").fetchall()
    if stale:
        lines.append("## Stale boards")
        lines.extend(f"- {r['ats']}/{r['token']}" for r in stale)
        lines.append("")

    lines.append("## Funnel")
    funnel = _funnel(conn)
    prev = None
    for name, n in funnel:
        rate = f" ({n / prev:.0%} of prior)" if prev else ""
        lines.append(f"- {name}: {n}{rate}")
        prev = n or prev
    ghosted = conn.execute(q.COUNT_GHOSTED_APPLICATIONS).fetchone()["n"]
    if ghosted:
        lines.append(f"- no reply after {load_filters().ghost_after_days} days: {ghosted}")
    lines.append("")
    lines.extend(short_feed_jd_lines(conn, previous_digest_time(date)))

    top_rejects = conn.execute(
        """SELECT reason, COUNT(*) n FROM rejections
           GROUP BY reason ORDER BY n DESC LIMIT 8"""
    ).fetchall()
    lines.append("## Top rejection reasons")
    lines.extend(f"- {r['n']}× {r['reason']}" for r in top_rejects)

    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    path = DIGEST_DIR / f"{date}.md"
    path.write_text("\n".join(lines) + "\n")
    return path
