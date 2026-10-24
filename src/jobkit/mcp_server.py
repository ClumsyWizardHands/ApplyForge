"""jobkit MCP server — the PRIMARY INTERFACE (SPEC §6.1). stdio transport.

Queue numbering is pinned: get_queue writes a number → job_id map to the
queue_pins table at render time; approve/reject/snooze resolve against that
pinned map, never against the queue's current order. New render, new map.
A pinned aggregator job merged since into the employer's own posting (the
approve-time board resolution, a poll, a pick) resolves to that ATS row,
which now carries its application (pipeline/dedup.follow_merges).

No LLM calls in the request path except novel-question answering (M3) and
tailoring (M4) — architecture invariant."""

from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from jobkit import background, paths
from jobkit import db as q
from jobkit import schedule as launchd
from jobkit.autofill import browser
from jobkit.autofill.answer_bank import human_answer_barred
from jobkit.log import log
from jobkit.onboarding import mcp_tools as onboarding_tools
from jobkit.pipeline import digest as digest_mod
from jobkit.pipeline import halts, nightly, questions
from jobkit.pipeline.dedup import follow_merges
from jobkit.pipeline.digest import freshness_banner, write_digest
from jobkit.rules import employer_site_note, run_standing_rules
from jobkit.track import job_alerts, responses
from jobkit.track.applications import record_submission
from jobkit.track.applications import repair as repair_application_rows

mcp = MCPServer("jobkit")

PINS_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue_pins (
  n INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL,
  rendered_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _conn() -> sqlite3.Connection:
    conn = q.connect()
    q.init_db(conn)
    conn.executescript(PINS_SCHEMA)
    return conn


def _pinned(conn: sqlite3.Connection) -> dict[int, int]:
    """The pinned map n → job_id, each job followed through merges: an
    aggregator job approved as item n and merged into its ATS row afterwards
    (approve starts `jobkit tailor`, which resolves it first) is that ATS row,
    where its application and decisions moved."""
    pins: dict[int, int] = {}
    for r in conn.execute("SELECT n, job_id FROM queue_pins").fetchall():
        job_id = int(r["job_id"])
        current = follow_merges(conn, job_id)
        if current != job_id:
            log("mcp.pin_followed_merge", n=r["n"], feed_job_id=job_id, job_id=current)
        pins[int(r["n"])] = current
    return pins


def _resolve(conn: sqlite3.Connection, ids: str | list[int]) -> list[tuple[int, int]]:
    """Resolve 'all' / '>85' / [1,3,7] against the PINNED map → [(n, job_id)]."""
    pins = _pinned(conn)
    if not pins:
        raise ValueError("no pinned queue — call get_queue first")
    if isinstance(ids, str):
        s = ids.strip().lower()
        if s == "all":
            return sorted(pins.items())
        if s.startswith(">"):
            floor = int(s[1:])
            rows = conn.execute(
                f"""SELECT j.id FROM jobs j JOIN scores sc ON sc.job_id=j.id
                   WHERE j.id IN ({",".join("?" * len(pins))}) AND sc.fit_score > ?""",
                (*pins.values(), floor),
            ).fetchall()
            keep = {r["id"] for r in rows}
            return sorted((n, j) for n, j in pins.items() if j in keep)
        ids = [int(x) for x in s.replace(",", " ").split()]
    missing = [n for n in ids if n not in pins]
    if missing:
        raise ValueError(f"numbers {missing} not in the pinned queue (re-run get_queue)")
    return [(n, pins[n]) for n in ids]


def _parse_job_ids(job_ids: str) -> list[int]:
    try:
        return [int(x) for x in job_ids.replace(",", " ").split()]
    except ValueError:
        raise ValueError(f"job_ids must be numbers like '412, 418', got {job_ids!r}") from None


def _targets(conn: sqlite3.Connection, ids: str, job_ids: str) -> list[tuple[str, int]]:
    """[(label, job_id)] from pinned queue numbers (`ids`) and/or raw job ids
    (`job_ids`). Raw ids work for any job — approved, applied by hand, or no
    longer in the queue (get_queue pins only queued jobs). An unknown job id
    raises; nothing is half-applied."""
    out: list[tuple[str, int]] = []
    if ids.strip():
        out += [(f"#{n}", j) for n, j in _resolve(conn, ids)]
    if job_ids.strip():
        nums = _parse_job_ids(job_ids)
        missing = [j for j in nums if conn.execute(q.JOB_BRIEF, {"job_id": j}).fetchone() is None]
        if missing:
            raise ValueError(f"no job with id {missing}")
        for j in nums:
            current = follow_merges(conn, j)  # a merged aggregator row: its ATS row
            out.append((f"job {j}" if current == j else f"job {current} (was {j})", current))
    if not out:
        raise ValueError("give queue numbers (ids='1,3') or job ids (job_ids='412')")
    unique: dict[int, str] = {}
    for label, j in out:
        unique.setdefault(j, label)
    return [(label, j) for j, label in unique.items()]


def _parse_day(text: str) -> datetime.date:
    if not text.strip():
        return datetime.date.today()
    try:
        day = datetime.date.fromisoformat(text.strip())
    except ValueError:
        raise ValueError(f"submitted_on must be YYYY-MM-DD, got {text!r}") from None
    if day > datetime.date.today():
        raise ValueError(f"submitted_on {day} is in the future")
    return day


def _fmt_job_line(n: int, r: sqlite3.Row) -> str:
    comp = (
        f"${r['comp_min'] // 1000}–{r['comp_max'] // 1000}k"
        if r["comp_min"] and r["comp_max"]
        else "comp n/a"
    )
    remote = {1: "remote ✓", 0: "onsite/hybrid", None: "loc?"}[r["remote"]]
    flags = json.loads(r["flags"] or "[]") if r["flags"] else []
    gaps = ", ".join(json.loads(r["gaps"] or "[]")[:3]) or "none"
    fr = f" · friction {r['friction']}" if r["friction"] is not None else ""
    warn = f"  ⚠ {'; '.join(f[:80] for f in flags)}" if flags else ""
    # a feed job with no employer posting found: no form jobkit can fill,
    # never approved automatically; the person can still approve it
    where = employer_site_note(r["board_id"], r["apply_url"])
    return (
        f"{n}. {r['company_name']} — {r['title']} — {comp} — {remote} — "
        f"{r['fit_score']}{fr} — gaps: {gaps}{where}{warn}"
    )


@mcp.tool()
def get_queue(min_score: int = 0, limit: int = 25, max_friction: int = 100) -> str:
    """Numbered pending jobs with score, comp, remote verdict, friction, gaps,
    sanity flags. A job from a job feed whose employer posting was not found
    is marked "no employer form found: apply on the employer's site (found
    via <link>)", the link being the feed's page: it has no form to fill
    and is never approved automatically, but approve still takes it.
    Numbering is pinned until the next get_queue call. Opens
    with the freshness banner, then a warning naming every approved job a
    staging run stopped on before filling anything (CAPTCHA, ID/payment
    field, assessment, no form): those wait on you, and schedule_status or
    get_digest has their reasons, screenshots and apply URLs. Ends with
    today's questions for you (get_questions has why and which jobs)."""
    conn = _conn()
    from jobkit.pipeline.filter import unsnooze

    unsnooze(conn)  # snoozed jobs whose date passed rejoin the queue
    run_standing_rules(conn)  # pre-decide the obvious before rendering
    rows = conn.execute(
        """SELECT j.id, j.title, j.company_name, j.comp_min, j.comp_max, j.remote,
                  j.board_id, j.apply_url,
                  sc.fit_score, sc.gaps, s.flags, fs.friction_score AS friction
           FROM jobs j JOIN scores sc ON sc.job_id = j.id
           LEFT JOIN sanity s ON s.job_id = j.id
           LEFT JOIN form_specs fs ON fs.job_id = j.id
           WHERE j.status = 'queued' AND sc.fit_score >= ?
             AND COALESCE(fs.friction_score, 0) <= ?
           ORDER BY sc.fit_score DESC LIMIT ?""",
        (min_score, max_friction, limit),
    ).fetchall()
    conn.execute("DELETE FROM queue_pins")
    lines = []
    for n, r in enumerate(rows, 1):
        conn.execute("INSERT INTO queue_pins (n, job_id) VALUES (?, ?)", (n, r["id"]))
        lines.append(_fmt_job_line(n, r))
    conn.commit()
    # live freshness first: a stale queue must say so (audit 2026-10-08 §2 item 7)
    digest_head = freshness_banner(conn) + "\n"
    # then jobs a staging run (often the unattended nightly) stopped on: hard
    # rule 2 hands them to the human, and open_queue cannot reopen them
    halted = halts.queue_line(conn)
    if halted:
        digest_head += halted + "\n"
    # aggregator jobs whose employer posting could not be picked (none or
    # several agree on location, or a board found by name only is not
    # confirmed as the company's): the human picks (resolve_boards)
    from jobkit.discovery import board_resolver

    ambiguous = board_resolver.queue_line(conn)
    if ambiguous:
        digest_head += ambiguous + "\n"
    latest = sorted(digest_mod.DIGEST_DIR.glob("*.md"))
    if latest:
        digest_head += latest[-1].read_text().split("\n", 1)[0].strip() + " (latest digest)\n"
    digest_head += "\n"
    auto = conn.execute(
        "SELECT COUNT(*) c FROM decisions WHERE auto=1 AND decision='approve' "
        "AND decided_at > datetime('now', '-1 day')"
    ).fetchone()["c"]
    fyi = (
        f"\n(standing rules auto-approved {auto} in the last day — see get_digest)" if auto else ""
    )
    # facts only the human knows that would close gaps in these jobs (package 3)
    asks = questions.queue_lines(conn)
    ask_text = ("\n\n" + "\n".join(asks)) if asks else ""
    return digest_head + f"{len(rows)} jobs in queue:\n" + "\n".join(lines) + fyi + ask_text


@mcp.tool()
def get_job(n: int) -> str:
    """Full detail for queue item n — JD summary, requirements, comp analysis, flags."""
    conn = _conn()
    ((_, job_id),) = _resolve(conn, [n])
    pinned = conn.execute("SELECT job_id FROM queue_pins WHERE n = ?", (n,)).fetchone()
    r = conn.execute(
        """SELECT j.*, sc.fit_score, sc.rationale, sc.gaps, s.*, r.must_have,
                  r.nice_to_have, r.implied, r.disqualifier,
                  fs.friction_score, fs.novel_questions, fs.field_count
           FROM jobs j
           LEFT JOIN scores sc ON sc.job_id = j.id
           LEFT JOIN sanity s ON s.job_id = j.id
           LEFT JOIN job_requirements r ON r.job_id = j.id
           LEFT JOIN form_specs fs ON fs.job_id = j.id
           WHERE j.id = ?""",
        (job_id,),
    ).fetchone()
    parts = [
        f"# {r['company_name']} — {r['title']}",
        f"apply: {r['apply_url']}",
        f"score {r['fit_score']}: {r['rationale']}",
        f"location: {r['location_raw']} | remote stated/actual: "
        f"{r['remote_stated']}/{r['remote_actual']} | office days: {r['office_days_required']}",
        f"comp: {r['comp_min']}–{r['comp_max']} {r['comp_currency'] or ''} | "
        f"demand {r['demand_score']} | verdict: {r['value_verdict']}",
        f"flags: {r['flags']}",
        f"must-have: {r['must_have']}",
        f"gaps: {r['gaps']}",
        f"form: {r['field_count']} fields, friction {r['friction_score']}, "
        f"novel: {r['novel_questions']}",
    ]
    if pinned is not None and int(pinned["job_id"]) != job_id:
        parts.insert(
            1,
            f"(item {n} was aggregator job {pinned['job_id']}; it was merged into this job "
            f"{job_id}, the employer's own posting, which carries your decision now)",
        )
    # a posting the per-job ATS check closed, and the latest staging run's
    # read-back (fields the page changed after filling)
    parts += browser.staging_lines(conn, job_id)
    from jobkit.discovery import board_resolver

    parts += [f"⚠ {f.line()}" for f in board_resolver.flags(conn) if f.job_id == job_id]
    return "\n".join(parts)


@mcp.tool()
def approve(ids: str) -> str:
    """Approve queue items: '1,3,7' or 'all' or '>85'. Records the decision and
    creates the application row. (Tailor + render + stage attach at M4/M5.)"""
    conn = _conn()
    approved = []
    for n, job_id in _resolve(conn, ids):
        conn.execute("UPDATE jobs SET status='approved' WHERE id=?", (job_id,))
        if not conn.execute(
            "SELECT 1 FROM applications WHERE job_id=?", (job_id,)
        ).fetchone():  # idempotent: a re-approve must not spawn a duplicate application
            conn.execute(
                "INSERT INTO applications (job_id, stage) VALUES (?, 'approved')", (job_id,)
            )
        sc = conn.execute("SELECT fit_score FROM scores WHERE job_id=?", (job_id,)).fetchone()
        conn.execute(
            """INSERT INTO decisions (job_id, decision, reason, score_at_decision, auto)
               VALUES (?, 'approve', 'human', ?, 0)""",
            (job_id, sc["fit_score"] if sc else None),
        )
        approved.append(n)
    conn.commit()
    log("mcp.approve", items=approved)
    # `jobkit tailor` first matches an approved aggregator job to the
    # employer's own posting (discovery/board_resolver.before_tailoring)
    aggregator = [
        n
        for n, job_id in _resolve(conn, ids)
        if (r := conn.execute("SELECT board_id FROM jobs WHERE id=?", (job_id,)).fetchone())
        and r["board_id"] is None
    ]
    launch = background.spawn(conn, "tailor")  # get_artifacts / background_status poll it
    via = (
        f" Item(s) {aggregator} came from an aggregator: the run first looks for the "
        "employer's own posting (same title and location, on a board confirmed to be "
        "the company's) and moves your approval to it; the item numbers then reach that "
        "posting. When none or several fit, the job is flagged board-match-ambiguous "
        "(board-match-unconfirmed when the board is not confirmed to be the company's; "
        "resolve_boards lists them)."
        if aggregator
        else ""
    )
    return (
        f"approved {approved}. Tailoring: {launch.describe()}.{via} Check get_artifacts(n) "
        "in a couple of minutes, or background_status for the run itself."
    )


@mcp.tool()
def reject(ids: str, reason: str = "") -> str:
    """Dismiss queue items; the reason feeds filter tuning and calibration."""
    conn = _conn()
    done = []
    for n, job_id in _resolve(conn, ids):
        conn.execute("UPDATE jobs SET status='rejected' WHERE id=?", (job_id,))
        sc = conn.execute("SELECT fit_score FROM scores WHERE job_id=?", (job_id,)).fetchone()
        conn.execute(
            """INSERT INTO decisions (job_id, decision, reason, score_at_decision, auto)
               VALUES (?, 'reject', ?, ?, 0)""",
            (job_id, reason or "human", sc["fit_score"] if sc else None),
        )
        done.append(n)
    conn.commit()
    return f"rejected {done}" + (f" ({reason})" if reason else "")


@mcp.tool()
def snooze(ids: str, days: int = 7) -> str:
    """Send queue items back to the queue after N days."""
    conn = _conn()
    done = []
    until = (datetime.date.today() + datetime.timedelta(days=days)).isoformat()
    for n, job_id in _resolve(conn, ids):
        conn.execute("UPDATE jobs SET status='snoozed' WHERE id=?", (job_id,))
        conn.execute(
            "INSERT INTO events (job_id, kind, detail) VALUES (?, 'snoozed_until', ?)",
            (job_id, until),
        )
        conn.execute(
            "INSERT INTO decisions (job_id, decision, reason, auto) VALUES (?, 'snooze', ?, 0)",
            (job_id, until),
        )
        done.append(n)
    conn.commit()
    return f"snoozed {done} until {until}"


@mcp.tool()
def get_novel_questions(n: int) -> str:
    """Only the questions the Answer Bank couldn't cover for queue item n,
    plus any answers drafted overnight and the answers you gave (answer_novel)."""
    conn = _conn()
    ((_, job_id),) = _resolve(conn, [n])
    r = conn.execute(
        "SELECT novel_questions, drafted_answers FROM form_specs WHERE job_id=?", (job_id,)
    ).fetchone()
    if not r:
        return "no form spec captured for this job yet"
    # drafted_answers holds AI drafts and human answers; the events tell them apart
    human = {str(x[0]) for x in conn.execute(q.HUMAN_ANSWERED_FIELDS, {"job_id": job_id})}
    drafted = json.loads(r["drafted_answers"] or "{}")
    still_open = [
        oq
        for oq in json.loads(r["novel_questions"] or "[]")
        if str(oq.get("identity") if isinstance(oq, dict) else oq) not in human
    ]
    return (
        f"open questions: {json.dumps(still_open)}\n"
        f"drafted overnight: {json.dumps({k: v for k, v in drafted.items() if k not in human})}\n"
        f"answered by you (filled at staging as human_typed): "
        f"{json.dumps({k: v for k, v in drafted.items() if k in human})}"
    )


@mcp.tool()
def answer_novel(n: int, field_identity: str, answer: str) -> str:
    """Record the human's answer to a question on queue item n's form. Staging
    fills it as human_typed, including on a field the AI never drafts (a
    referrer's email, a salary expectation). Refused for EEO/demographic fields
    (only the Answer Bank's eeo block selects those, hard rule 4) and for
    compensation history (never answered, hard rule 5). Offered for promotion
    into the Answer Bank after repeated use."""
    conn = _conn()
    ((_, job_id),) = _resolve(conn, [n])
    r = conn.execute(
        "SELECT fields, drafted_answers FROM form_specs WHERE job_id=?", (job_id,)
    ).fetchone()
    if not r:
        return "no form spec captured for this job yet: answer not recorded"
    fields = json.loads(r["fields"] or "[]")
    field = next((f for f in fields if str(f.get("identity")) == field_identity), None)
    if field is None:
        known = [f"{f.get('identity')} ({str(f.get('label') or '')[:60]})" for f in fields[:40]]
        return (
            f"no field '{field_identity}' on this job's form: answer not recorded. fields: {known}"
        )
    label = str(field.get("label") or field_identity)
    barred = human_answer_barred(label, field.get("compliance_type"))
    if barred == "eeo-answer-bank-only":
        return (
            f"not recorded: '{label[:80]}' is an EEO/demographic field. It is filled only "
            "from the eeo block in data/answer_bank.yaml, or left blank (hard rule 4)."
        )
    if barred:
        return (
            f"not recorded: '{label[:80]}' asks for compensation history. It is never "
            "answered and stays blank and flagged (hard rule 5)."
        )
    drafted = json.loads(r["drafted_answers"] or "{}")
    drafted[field_identity] = answer
    conn.execute(
        "UPDATE form_specs SET drafted_answers=? WHERE job_id=?", (json.dumps(drafted), job_id)
    )
    conn.execute(
        "INSERT INTO events (job_id, kind, detail) VALUES (?, 'novel_answered', ?)",
        (job_id, field_identity),
    )
    conn.commit()
    return f"recorded answer for '{field_identity}' (source: human_typed)"


@mcp.tool()
def mark_submitted(ids: str = "", job_ids: str = "", submitted_on: str = "") -> str:
    """Log that the person submitted these applications. Each job then has
    one application row at stage 'submitted', dated from the submission day.
    With no reply recorded after tracking.ghost_after_days (default 30) it
    is marked 'ghosted'. `ids` are pinned queue numbers ('1,3'); `job_ids`
    are raw job ids ('412, 418') and work for any job, including one applied
    to by hand or no longer in the queue. `submitted_on` (YYYY-MM-DD, default
    today) back-dates a submission made earlier. A job already marked
    submitted is left unchanged."""
    conn = _conn()
    try:
        targets = _targets(conn, ids, job_ids)
        day = _parse_day(submitted_on)
    except ValueError as e:
        return f"nothing logged: {e}"
    submitted_at = (
        day.isoformat()
        if submitted_on.strip()
        else datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")
    )
    lines = []
    for label, job_id in targets:
        job = conn.execute(q.JOB_BRIEF, {"job_id": job_id}).fetchone()
        name = f"{label} {job['company_name']} — {job['title']}"
        # exactly one application row, at 'submitted' (track/applications.py),
        # whatever rows the job had before
        sub = record_submission(conn, job_id, submitted_at)
        folded = (
            f" ({len(sub.folded)} duplicate application row(s) folded into one)"
            if sub.folded
            else ""
        )
        if sub.already:
            lines.append(
                f"{name}: already marked submitted ({sub.submitted_at}), unchanged{folded}"
            )
            continue
        conn.execute(
            q.RECORD_EVENT,
            {
                "job_id": job_id,
                "kind": "marked_submitted",
                "detail": json.dumps({"submitted_on": day.isoformat()}),
            },
        )
        lines.append(f"{name}: submitted {day.isoformat()}{folded}")
    conn.commit()
    log("mcp.mark_submitted", job_ids=[j for _, j in targets], submitted_on=day.isoformat())
    return "\n".join(lines)


OUTCOME_STAGE = {
    "reply": "replied",
    "rejection": "rejected",
    "interview": "interview",
    "offer": "offer",
}


@mcp.tool()
def record_outcome(job_id: int, outcome: str, note: str = "") -> str:
    """Record an employer's response to a submitted application. `outcome` is
    'reply', 'rejection', 'interview' or 'offer'; `note` is free text (who
    wrote, what they said). Stored as an `outcome` event with the note; moves
    the application's stage (replied / rejected / interview / offer) so
    get_stats and calibration count it. An application with a response is
    never marked ghosted, and one already ghosted moves on. The job must be
    marked submitted first; for a manual application use
    mark_submitted(job_ids='N', submitted_on='YYYY-MM-DD')."""
    key = outcome.strip().lower()
    if key not in OUTCOME_STAGE:
        return f"not recorded: outcome must be one of {', '.join(OUTCOME_STAGE)}"
    conn = _conn()
    if conn.execute(q.JOB_BRIEF, {"job_id": job_id}).fetchone() is None:
        return f"not recorded: no job with id {job_id}"
    # an aggregator row merged into the employer's posting: its application
    # moved there (pipeline/dedup.follow_merges)
    given, job_id = job_id, follow_merges(conn, job_id)
    job = conn.execute(q.JOB_BRIEF, {"job_id": job_id}).fetchone()
    name = f"job {job_id} ({job['company_name']} — {job['title']})" + (
        f" (was {given})" if given != job_id else ""
    )
    app = conn.execute(q.APPLICATION_FOR_JOB, {"job_id": job_id}).fetchone()
    if app is None or not app["submitted_at"]:
        return (
            f"not recorded: {name} has no submitted application. Call "
            f"mark_submitted(job_ids='{job_id}', submitted_on='YYYY-MM-DD') first."
        )
    before = str(app["stage"])
    after = OUTCOME_STAGE[key]
    if key == "reply" and before in ("interview", "offer", "rejected"):
        after = before  # a later reply never undoes a stronger outcome
    conn.execute(q.SET_APPLICATION_STAGE, {"id": app["id"], "stage": after})
    conn.execute(
        q.RECORD_EVENT,
        {
            "job_id": job_id,
            "kind": "outcome",
            "detail": json.dumps(
                {
                    "outcome": key,
                    "note": note,
                    "application_id": app["id"],
                    "stage_before": before,
                    "stage_after": after,
                }
            ),
        },
    )
    conn.commit()
    log("mcp.record_outcome", job_id=job_id, outcome=key, stage=after)
    return f"recorded {key} for {name}: stage {before} -> {after}."


# ---- replies in the person's own mailbox (track/responses.py) ----
# These tools only record what the person's mailbox already shows. jobkit
# never connects to the mailbox: Claude reads it through the email connector
# the person set up, and nothing is ever sent or changed there.


@mcp.tool()
def email_targets(scheduled: bool = False) -> str:
    """Step 1 of an email check. Lists the submitted applications still
    waiting on news (id, company, title, ATS, apply host, submission date,
    stage), when the mailbox was last checked, and a suggested Gmail search
    (ATS sender domains and company names, after the last check). Also a
    second search, for job-alert emails (LinkedIn's and the job boards') and
    LinkedIn message notifications, what to take from each (for
    record_job_alerts and record_linkedin_message), and the LinkedIn
    messages recorded since the last check, for the summary. The last line
    says whether Claude searches the web for alert jobs board lookup cannot
    find (search.alert_web_search; then alert_jobs_to_search lists them).
    Read-only: use the search in the person's own mailbox through their
    email connector. Nothing is sent, and no email is changed or stored. The
    unattended scheduled run passes scheduled=true: then nothing is listed
    unless tracking.email_check is true in config/filters.yaml."""
    if scheduled:
        from jobkit.config import load_filters

        try:
            on = load_filters().email_check
        except (ValueError, FileNotFoundError) as e:
            # FileNotFoundError covers ConfigMissing: no config/filters.yaml yet
            return f"Email not checked: {e}"
        if not on:
            return (
                "Email not checked: tracking.email_check is off in config/filters.yaml. "
                "Skip the email check in this run."
            )
    from jobkit.track import alert_search

    return responses.email_targets(_conn()).render() + "\n" + alert_search.search_status()


@mcp.tool()
def match_email(sender: str, subject: str = "", snippet: str = "", received_at: str = "") -> str:
    """Step 2: which submitted application one email most likely belongs to.
    Give the sender ('Acme Recruiting <no-reply@greenhouse-mail.io>'), the
    subject, a short snippet, and the date it arrived. Returns the candidate
    applications ranked by a fixed rule (company name, company domain, the ATS
    the person applied through, title words), with the reasons, and says
    whether one is a clear match. Read-only: it changes nothing and nothing
    is sent. Without a clear match, ask the person before recording
    anything."""
    try:
        return responses.match_email(_conn(), sender, subject, snippet, received_at).render()
    except ValueError as e:
        return f"no match run: {e}"


@mcp.tool()
def record_email_response(
    application_id: int,
    outcome: str,
    received_at: str,
    message_id: str,
    link: str = "",
    summary: str = "",
) -> str:
    """Step 3: record what one email in the person's mailbox says about one
    submitted application. `outcome` is acknowledged (an automatic receipt),
    rejected, assessment, interview, offer or recruiter_question.
    `received_at` is when the email arrived; `message_id` is the mailbox's id
    for the message (a message is recorded once); `link` opens it in the
    mailbox; `summary` is ONE line in your words (cut to 160 characters; a
    summary with a line break is refused). Never pass the email's body: it
    is not stored. Stage rules: an
    acknowledgement never moves the stage; a reply moves a ghosted
    application on; an 'offer' stage is never changed here; the later-dated
    response decides the stage. Nothing is sent."""
    try:
        return responses.record_response(
            _conn(), application_id, outcome, received_at, message_id, link, summary
        ).render()
    except ValueError as e:
        return f"not recorded: {e}"


@mcp.tool()
def finish_email_check(checked_through: str = "") -> str:
    """Step 4: record that the person's mailbox was read through
    `checked_through` (ISO date-time; default now), so the next email_targets
    search starts there. Lists the jobs from job-alert emails recorded in
    this check and where each stands. When the check handed any of them to
    board lookup (record_job_alerts), starts `jobkit same-day
    --resolve-boards` in the background: board lookup on the employer's own
    Greenhouse, Lever or Ashby board (public board APIs only), then the
    postings it found go through the filter, the sanity checks, scoring, the
    queue and the standing rules, so one that clears the person's bar is
    approved today (background_status shows its report; the nightly run
    picks up any it misses). When web search for alert jobs is on, the last
    line says to call alert_jobs_to_search once that launch finishes.
    Read-only for the mailbox: nothing is sent."""
    from jobkit.track import alert_search

    conn = _conn()
    # read before the check is recorded: a config error never follows a
    # committed check (search_on reads safely, a broken config means off)
    searching = alert_search.search_on()
    try:
        check = responses.finish_email_check(conn, checked_through)
    except ValueError as e:
        return f"not recorded: {e}"
    text = check.render()
    if check.alert_cards_since:
        text += "\n" + _start_alert_board_lookup(conn, check.alert_cards_since)
    if searching:
        text += "\n" + alert_search.search_status()
    return text


def _start_alert_board_lookup(conn: sqlite3.Connection, cards: int) -> str:
    """Board lookup and same-day scoring for the jobs an email check took
    from job-alert emails: `jobkit same-day --resolve-boards`, in the
    background (no LLM here). Not started while a nightly run or board
    lookup is going: that run, or the next one, picks them up."""
    from jobkit.pipeline import same_day

    what = f"{cards} job(s) from job-alert emails"
    night = nightly.in_progress(conn)
    if night is not None:
        return f"board lookup for {what}: the nightly run in progress (run {night.run}) does it."
    busy = same_day.board_lookup_running(conn)
    if busy is not None:
        return (
            f"board lookup for {what}: `jobkit {' '.join(busy.args)}` is already running "
            f"(launch #{busy.event_id}); the nightly resolve-boards stage picks up any it misses."
        )
    try:
        launch = background.spawn(conn, same_day.COMMAND, same_day.RESOLVE_FLAG)
    except RuntimeError as e:
        return f"board lookup for {what} not started ({e}); the nightly run does it."
    return (
        f"board lookup for {what}: {launch.describe()}. Postings found on the employer's own "
        "board are scored in the same run, and the standing rules approve the ones that clear "
        "your bar; background_status shows the report."
    )


@mcp.tool()
def alert_jobs_to_search() -> str:
    """The jobs from recent job-alert emails (inside the person's freshness
    window, freshness.max_age_days) that board lookup could not find on a
    Greenhouse, Lever or Ashby board and that have no web search result
    yet, newest first: each with its alert reference, company, title,
    location, the alert it came from and a suggested web search. Only when
    search.alert_web_search is true in config/filters.yaml (otherwise it
    says search is off and lists nothing to search); search.max_searches_per_check,
    when the person chose one in setup, limits how many are listed per email
    check (recency is the limit otherwise). Search the web for each one and
    follow any public page that leads to the posting (a company careers
    page, an ATS board, a job site that links through); record the
    posting's own link (the employer's Greenhouse, Lever or Ashby posting,
    its apply page on Workday, iCIMS, Taleo and the like, or the posting on
    the employer's careers site), never a job site's copy. Never open a
    linkedin.com or lnkd.in page, skip those results without opening them
    (they have no apply link); never sign in; follow robots.txt within
    reason; stop at any CAPTCHA or bot check. Then call
    record_search_result for each. Read-only, deterministic, no LLM."""
    from jobkit.pipeline import same_day
    from jobkit.track import alert_search

    conn = _conn()
    busy = same_day.board_lookup_running(conn)
    running = f"`jobkit {' '.join(busy.args)}`, launch #{busy.event_id}" if busy else ""
    try:
        return alert_search.to_search(conn, lookup_running=running).render()
    except (ValueError, FileNotFoundError) as e:
        return f"nothing listed: {e}"


@mcp.tool()
def record_search_result(alert_ref: str, url: str = "", outcome: str = "found") -> str:
    """Record what a web search found for one job from alert_jobs_to_search
    (recorded once per alert_ref; a repeat changes nothing). outcome
    'found' with `url` the posting's apply link: a Greenhouse, Lever or
    Ashby link is read from the public board API now and the posting enters
    jobkit as the employer's own record (never twice); a new job is then
    scored in the background (`jobkit same-day <id>`: filter, sanity checks,
    score, queue, standing rules), so one that clears the person's bar is
    approved today (background_status shows it). A link on a system jobkit
    does not read (Workday, iCIMS, Taleo, SuccessFactors, ...) or on the
    employer's own careers site is kept as a lead: the system's name and
    the employer's apply link (tracking parameters removed), listed in the
    email-check summary and the digest as 'apply on the employer's site:
    <link>'. No job is added for a lead: a job from an alert enters jobkit
    only as the employer's Greenhouse, Lever or Ashby posting. outcome
    'not_found' with no url: recorded, and the job is not offered again. A
    link that names LinkedIn anywhere is refused (never open a linkedin.com
    or lnkd.in page; skip those results without opening them), and so is a
    job site's copy (a link on neither an application system's own host nor
    the employer's own domain): follow it to the posting's own link. No LLM
    here."""
    from jobkit.track import alert_search

    try:
        return alert_search.record_search_result(_conn(), alert_ref, url, outcome).render()
    except (ValueError, FileNotFoundError) as e:
        return f"not recorded: {e}"


@mcp.tool()
def record_job_alerts(
    items: list[dict[str, Any]] | str,
    message_id: str,
    received_at: str,
    source: str = "linkedin",
) -> str:
    """Record the jobs listed in one job-alert email in the person's mailbox
    (LinkedIn's job alerts, or a job board's such as Indeed). `items` is a
    list of {company, title, location, company_site_link}: the text of each
    job as the email shows it, and whether it shows an 'apply on company
    site' link. Never pass a link or a job id: a field holding one is
    refused. `message_id` is the mailbox's id for the email (an email is
    recorded once), `received_at` when it arrived, `source` the sender
    (linkedin, indeed, glassdoor, ziprecruiter, wellfound, ...). Each title
    goes through the person's title rules. A kept job whose employer posting
    jobkit already holds needs nothing; every other kept job goes to board
    lookup, which finds the same job (title AND location) on the employer's
    own Greenhouse, Lever or Ashby board and adds that posting (never a
    LinkedIn job); finish_email_check starts the lookup. The company name
    goes to the discovery queue unless jobkit already polls its board.
    Returns the counts (kept, for board lookup, already in jobkit, dropped)
    and one line per job. Deterministic, no LLM. Read-only for the mailbox:
    nothing is sent and no link is followed."""
    try:
        return job_alerts.record_job_alerts(
            _conn(), items, message_id, received_at, source
        ).render()
    except (ValueError, FileNotFoundError) as e:
        return f"not recorded: {e}"


@mcp.tool()
def record_linkedin_message(
    company: str,
    summary: str,
    received_at: str,
    message_id: str,
    link: str = "",
) -> str:
    """Record one LinkedIn message or InMail notification from the person's
    mailbox: the sender's company if the email shows it ('' if not), ONE
    line in your words about who wrote, by role and never by name ('a
    recruiter at Acme'), and what they want (cut to 160 characters; a line
    break is refused; never the message text), when it
    arrived, the mailbox's message id (recorded once) and a link that opens
    the email in the mailbox (never a LinkedIn link). Linked to an
    application or a queued job at that company when there is one, and
    listed in the digest. Read-only: nothing is sent and the message is
    never answered."""
    try:
        return job_alerts.record_linkedin_message(
            _conn(), company, summary, received_at, message_id, link
        ).render()
    except ValueError as e:
        return f"not recorded: {e}"


@mcp.tool()
def add_job_url(url: str) -> str:
    """Ingest a pasted job URL (email alert, DM, any board). Recognizes the
    employer ATS from the URL shape; a Greenhouse, Lever or Ashby board is
    polled now and the job is queued, then extracted and scored in the
    background (background_status shows it); it is in get_queue once scored.
    Other systems are named, and nothing is added."""
    import re
    import subprocess

    home = paths.resolve_home()
    argv = background.cli_argv("add-url", "--no-score", url)
    log("mcp.add_job_url", argv=argv)
    out = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=home,
        env=background.child_env(home),
    )
    text = (out.stdout + out.stderr).strip() or "no output"
    if out.returncode != 0:
        return f"add-url exited {out.returncode}:\n{text}"
    pending = re.search(r"^needs scoring: \[(\d+)\]$", out.stdout, re.M)
    if pending is None:
        return text
    job_id = int(pending.group(1))
    # LLM calls stay out of the MCP request path: scoring runs in the background
    launch = background.spawn(_conn(), "score-jobs", str(job_id), job_id=job_id)
    return f"{text}\nscoring: {launch.describe()}. The job shows in get_queue once it is scored."


@mcp.tool()
def linkedin_discover(confirm: bool = False) -> str:
    """LinkedIn's logged-out ("guest") job search, for discovery only (CLAUDE.md
    hard rule 11 exception, 2026-10-12; off unless feeds.linkedin_guest.enabled
    in config/filters.yaml). Never logged in, no cookies, an honest User-Agent,
    a few searches a day (caps in filters.yaml), and it stops for the day on any
    block or challenge (never solved). A LinkedIn card becomes a job only when
    its posting page carries an employer apply link on a supported ATS (that
    board is polled now, the add-url path); otherwise only its company name goes
    to the discovery queue, whose stage finds the board. No LinkedIn job row is
    stored. Without confirm=true: today's plan, budget used, block state and
    results so far, with no request to LinkedIn. With confirm=true: runs
    `jobkit linkedin-discover` in the background (background_status shows its
    report); refused while a LinkedIn run, a nightly or a run-all is in
    progress. The nightly run includes it when it is enabled."""
    from jobkit.discovery import linkedin_guest as lg

    conn = _conn()
    try:
        plan = lg.run(conn, dry_run=True)
    except ValueError as e:
        return f"feeds.linkedin_guest in config/filters.yaml is invalid: {e}"
    if not confirm:
        return plan.render() + (
            "\n\nCall linkedin_discover(confirm=true) to run it now in the background."
        )
    from jobkit.config import load_filters

    if not load_filters().linkedin_guest.enabled:
        return (
            "not started: feeds.linkedin_guest.enabled is false in config/filters.yaml. "
            "It is your switch; set it to true to allow LinkedIn guest discovery."
        )
    # One run at a time (the run itself also takes a lock and reports busy).
    # A nightly or run-all counts as busy for its whole length: each has a
    # linkedin-guest stage, and a scheduled nightly is not a launch from here.
    holder = lg.running_now(conn)
    if holder is not None:
        return (
            f"not started: a LinkedIn guest run is in progress ({holder}). "
            "linkedin_discover() shows today's results so far."
        )
    night = nightly.in_progress(conn)
    if night is not None:
        return (
            f"not started: the nightly run is in progress (run {night.run}, pid {night.pid}, "
            f"started {night.started} UTC), and it includes LinkedIn guest discovery. "
            "schedule_status shows its stages."
        )
    busy = background.running(conn, ("linkedin-discover", "nightly", "run-all"))
    if busy is not None:
        return (
            f"not started: `jobkit {' '.join(busy.args)}` is still running (launch "
            f"#{busy.event_id}, pid {busy.pid}), and it includes LinkedIn guest discovery. "
            "background_status shows it."
        )
    launch = background.spawn(conn, "linkedin-discover")
    return (
        f"LinkedIn guest discovery: {launch.describe()}. It waits a few seconds between "
        "requests; background_status shows its report when it finishes."
    )


@mcp.tool()
def resolve_boards(job_id: int = 0, confirm: bool = False, pick: str = "") -> str:
    """Match aggregator jobs (Adzuna, Himalayas, ...) and LinkedIn cards whose
    company was queued to the employer's own Greenhouse/Lever/Ashby posting:
    the company's boards in jobkit, the registry, then a slug probe of the
    public board APIs (name variants such as acme, acme-careers;
    each company probed at most once per board_resolution.probe_cache_days).
    The probe runs only for a company with no board in jobkit or the
    registry. A posting is picked only when its title is the same AND its
    location agrees (same metro, or both US-wide remote), and, on a board not
    listed in config/targets.yaml and not named by the apply URL (a registry,
    discovered or probe-found board), when the board is confirmed to be the
    company's (Greenhouse reports the same company name, or the board links
    to the company's domain; Lever and Ashby report no name): it is ingested
    and the feed row merged into it (the ATS row wins, hard rule 10; your
    approval moves with it, and queue numbers follow). None or several
    agreeing: nothing is picked and the job is flagged board-match-ambiguous;
    an unconfirmed board: board-match-unconfirmed. Without
    confirm=true: what a run would look at and every standing flag with its
    candidate postings, from the DB only (no request). confirm=true: runs
    `jobkit resolve-boards [--job-id N]` in the background (background_status
    shows its report). pick=<posting id or URL> with job_id=N: your choice for
    a flagged job, merged by a background `jobkit resolve-boards --pick`
    (background_status shows it). The nightly run includes the resolve-boards
    stage, and approve resolves approved aggregator jobs before tailoring."""
    from jobkit.discovery import board_resolver

    conn = _conn()
    if pick.strip():
        if not job_id:
            return "pick needs job_id: the aggregator job the posting is for"
        # in the background: a merge that carries your approval extracts and
        # scores the ATS row (LLM calls stay out of the MCP request path)
        launch = background.spawn(
            conn, "resolve-boards", "--job-id", str(job_id), "--pick", pick.strip(), job_id=job_id
        )
        log("mcp.resolve_boards_pick", job_id=job_id, pick=pick.strip())
        return (
            f"your pick for job {job_id}: {launch.describe()}. background_status shows "
            "whether it merged."
        )
    try:
        plan = board_resolver.overview(conn)
    except ValueError as e:
        return f"board_resolution in config/filters.yaml is invalid: {e}"
    if not confirm:
        return plan + "\n\nCall resolve_boards(confirm=true) to run it now in the background."
    night = nightly.in_progress(conn)
    if night is not None:
        return (
            f"not started: the nightly run is in progress (run {night.run}, pid {night.pid}), "
            "and it includes the resolve-boards stage. schedule_status shows its stages."
        )
    busy = background.running(conn, ("resolve-boards", "nightly", "run-all"))
    if busy is not None:
        return (
            f"not started: `jobkit {' '.join(busy.args)}` is still running (launch "
            f"#{busy.event_id}, pid {busy.pid}). background_status shows it."
        )
    args = ["resolve-boards", *(["--job-id", str(job_id)] if job_id else [])]
    launch = background.spawn(conn, *args, job_id=job_id or None)
    return (
        f"board resolution: {launch.describe()}. It waits about a second between "
        "requests; background_status shows its report when it finishes."
    )


@mcp.tool()
def get_stats(period: str = "all") -> str:
    """Data freshness (last successful poll, failed run-all stages), then the
    funnel with conversion rates, employer rejections, how many
    applications had no reply after tracking.ghost_after_days, and the feed
    jobs not scored because their description was too short (title, company,
    location and link, newest first)."""
    conn = _conn()
    out = [freshness_banner(conn), ""]
    prev: int | None = None
    for label, sql in [
        ("seen", "SELECT COUNT(*) c FROM jobs"),
        (
            "queued_ever",
            "SELECT COUNT(*) c FROM jobs WHERE status IN ('queued','approved','applied')",
        ),
        ("approved", "SELECT COUNT(*) c FROM jobs WHERE status IN ('approved','applied')"),
        ("submitted", "SELECT COUNT(*) c FROM jobs WHERE status='applied'"),
        (
            "replied",
            "SELECT COUNT(*) c FROM applications WHERE stage IN ('replied','interview','offer')",
        ),
    ]:
        v = conn.execute(sql).fetchone()["c"]
        rate = f" ({v / prev:.0%})" if prev else ""
        out.append(f"{label}: {v}{rate}")
        prev = v or prev
    rejections = conn.execute(
        "SELECT COUNT(*) c FROM applications WHERE stage='rejected'"
    ).fetchone()["c"]
    out.append(f"employer rejections recorded: {rejections}")
    from jobkit.config import load_filters

    ghosted = conn.execute(q.COUNT_GHOSTED_APPLICATIONS).fetchone()["n"]
    out.append(f"no reply after {load_filters().ghost_after_days} days: {ghosted}")
    short = conn.execute(q.COUNT_FEED_JOBS_SHORT_JD, {"since": ""}).fetchone()["n"]
    if short:
        out.append(f"feed jobs not scored (description too short): {short}")
        limit = digest_mod.STATS_SHORT_FEED_JD_LISTED
        if short > limit:
            out.append(f"the newest {limit}; open one on its site to read the full posting:")
        out += digest_mod.short_feed_jd_rows(conn, "", limit)
    return "\n".join(out)


@mcp.tool()
def get_digest(date: str = "") -> str:
    """The daily digest — freshness, staging (forms waiting for review, jobs a
    staging run stopped on that wait on you), auto-approvals, sanity flags,
    funnel, rejection reasons."""
    conn = _conn()
    path = write_digest(conn, date or None)
    return path.read_text()


@mcp.tool()
def tune_filters() -> str:
    """Rejection-reason analysis with suggested config edits. Proposals only —
    the human edits config (calibration proposes; the human disposes)."""
    conn = _conn()
    rows = conn.execute(
        "SELECT layer, reason, COUNT(*) n FROM rejections GROUP BY layer, reason "
        "ORDER BY n DESC LIMIT 12"
    ).fetchall()
    lines = ["Top rejection reasons (layer: reason × count):"]
    lines += [f"- {r['layer']}: {r['reason'][:70]} × {r['n']}" for r in rows]
    from jobkit.config import load_filters

    paused = load_filters().paused_tracks
    if paused:
        held = {str(r["reason"]): int(r["n"]) for r in conn.execute(q.PAUSED_TRACK_HOLDS)}
        lines.append("\nPaused role tracks (filters.yaml tracks: active: false):")
        lines += [
            f"- {t}: {held.get(f'track_paused:{t}', 0)} open postings held as track_paused:{t}"
            for t in paused
        ]
        lines.append("  To resume one: set its active: true, then refilter(confirm=true).")
    lines.append("\nProposals (apply by editing config/filters.yaml yourself):")
    lines.append("1. If a location bucket dominates, consider adding it to allowed metros.")
    lines.append("2. If no_include_title_match kills titles you wanted, widen include_titles.")
    lines.append("3. After editing, call refilter(confirm=true) to re-check rejected open jobs.")
    return "\n".join(lines)


@mcp.tool()
def calibrate() -> str:
    """Decision-log analysis -> numbered proposals. Calibration proposes; the
    human disposes — nothing is applied without per-item acceptance."""
    from jobkit.onboarding.calibrate import calibrate as _cal

    return _cal(_conn()).render()


@mcp.tool()
def corpus_health() -> str:
    """Bullet count, metric coverage, tag distribution."""
    from collections import Counter

    import yaml

    from jobkit.pipeline import triage

    with open(triage.CORPUS_PATH) as fh:
        corpus = yaml.safe_load(fh)

    bullets = corpus["bullets"]
    conf = Counter(b["metric_confidence"] for b in bullets)
    tags = Counter(t for b in bullets for t in b["tags"])
    return (
        f"bullets: {len(bullets)} (spec bar: 30 — thin corpus, grown by calibration)\n"
        f"confidence: {dict(conf)}\n"
        f"with metric: {sum(1 for b in bullets if b.get('metric'))}/{len(bullets)}\n"
        f"top tags: {tags.most_common(10)}\n"
        f"stories: {len(corpus.get('stories', []))} | voice samples: "
        f"{len(corpus.get('voice_samples', []))} | positioning: "
        f"{len(corpus.get('positioning', []))}"
    )


@mcp.tool()
def get_artifacts(n: int) -> str:
    """Paths to resume PDF, cover letter, gap report, provenance for queue item n."""
    conn = _conn()
    ((_, job_id),) = _resolve(conn, [n])
    r = conn.execute(
        """SELECT resume_path, cover_path, gap_report_path, provenance_path, stage
           FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1""",
        (job_id,),
    ).fetchone()
    if not r:
        return "no application row — approve it first"
    if not r["resume_path"]:
        return f"stage={r['stage']}; artifacts not yet generated (tailoring lands in M4)"
    lines = [
        f"{k}: {r[k]}" for k in ("resume_path", "cover_path", "gap_report_path", "provenance_path")
    ]
    review_md = Path(str(r["resume_path"])).parent / "self_review.md"
    if review_md.exists():
        lines.append(f"self_review: {review_md} (get_self_review({n}) for its questions)")
    if not r["cover_path"]:
        lines.append(
            "cover letter REJECTED by the fact check — not rendered; see "
            "cover_rejected.md next to the resume, or review_artifacts for why"
        )
    if r["stage"] == "approved":
        last = conn.execute(q.LAST_COVER_FAILURE_FOR_JOB, {"job_id": job_id}).fetchone()
        try:
            rejudged = bool(last and json.loads(last["detail"] or "{}").get("rejudge"))
        except (ValueError, AttributeError):
            rejudged = False
        if rejudged:
            # pipeline/rejudge.py: no attempt key, so tailor_approved retries once
            lines.append(
                "not advancing to staging: a letter written before the claim judge failed "
                "its re-check (rejudge). The next tailor run re-tailors an approved job "
                f"once, with the judge; or call retailor({n}) now"
            )
        else:
            lines.append(
                f"not advancing to staging: the cover letter failed. Background runs will not "
                f"re-tailor it until the JD, corpus or prompts change or tailoring.cover_attempts "
                f"rises — edit the corpus, then "
                f"call retailor({n})"
            )
    return "\n".join(lines)


@mcp.tool()
def retailor(n: int) -> str:
    """Re-run tailoring (resume + cover letter) for queue item n. Background
    runs skip a job whose cover letter failed (fact check, or authenticity on
    every one of its tailoring.cover_attempts drafts, default 5) until its JD,
    the corpus or the prompts change, or the draft count rises;
    this retries it now, e.g. after editing the corpus. Only for an
    application that has not been staged yet."""
    conn = _conn()
    ((_, job_id),) = _resolve(conn, [n])
    app = conn.execute(q.LATEST_APPLICATION_FOR_JOB, {"job_id": job_id}).fetchone()
    if app is None:
        return "no application row — approve it first"
    if app["stage"] not in ("approved", "tailored"):
        return f"stage={app['stage']}: already staged or further along — not re-tailoring"
    launch = background.spawn(conn, "tailor", "--job-id", str(job_id), job_id=job_id)
    log("mcp.retailor", item=n, job_id=job_id)
    return (
        f"re-tailoring item {n} in the background — {launch.describe()}. Check "
        f"get_artifacts({n}) and review_artifacts({n}) in a couple of minutes."
    )


@mcp.tool()
def figures(bullet_id: str = "", rendered: str = "") -> str:
    """How numbers render on resumes, letters and drafted answers — the
    estimate_style set in config/banned_phrases.yaml (drift.estimate_style:
    "confident" lets "~"/"about" go but keeps value, unit, ranges, "+" and
    "up to"; "hedged" makes estimates keep a hedge). With no arguments: every
    corpus bullet's own text checked under it. With bullet_id: that bullet.
    With bullet_id and rendered: whether that rendering passes. Read-only; to
    change the style, the human edits the config."""
    from jobkit.pipeline.tailor import figures as _figures

    return _figures(bullet_id, rendered)


@mcp.tool()
def ghost_close(confirm: bool = False) -> str:
    """Mark submitted applications with no reply after tracking.ghost_after_days
    (config/filters.yaml, default 30) as ghosted: a status change only, no
    LLM, nothing sent. The nightly run does this. Without confirm=true this is
    a dry run that lists them: show the list to the human and call again with
    confirm=true only if they say so."""
    from jobkit.track.applications import close_ghosted

    return close_ghosted(_conn(), dry_run=not confirm).render()


@mcp.tool()
def purge_empty_jd_cache(confirm: bool = False) -> str:
    """One-off repair for the empty-JD cache collision: delete LLM cache rows
    keyed on the empty-JD hash (e3b0c442) and reset scores of jobs whose JD is
    under 500 characters. Without confirm=true this is a dry run that only
    counts — show the counts to the human and call again with confirm=true."""
    from jobkit.pipeline.maintenance import purge_empty_jd_cache as _purge

    return _purge(_conn(), dry_run=not confirm).render()


@mcp.tool()
def refilter(confirm: bool = False) -> str:
    """Re-run the deterministic filter on open jobs it rejected for a title,
    paused-track, comp-floor or location reason — use after the role tracks
    (including a track's `active:` pause switch), comp floors or geo rules in
    config/filters.yaml change. Resets those jobs to new (one
    `refiltered` event each) and runs the filter pass; passed jobs reach LLM
    triage on the next pipeline run. A job with a decision (a human reject,
    approve or snooze, or a standing rule) is never reset. Without confirm=true
    this is a dry run that only counts — show the counts to the human and call
    again with confirm=true."""
    from jobkit.pipeline.filter import refilter as _refilter

    return _refilter(_conn(), dry_run=not confirm).render()


def _staging_blocker(conn: sqlite3.Connection) -> str | None:
    """Why a new staging process must not start now, or None. The persistent
    browser profile admits one process: a second one fails on the lock."""
    busy = background.running(conn, ("stage", "open-queue"))
    if busy is not None:
        return (
            f"staging is already running (launch #{busy.event_id}, pid {busy.pid}: "
            f"`jobkit {' '.join(busy.args)}`). One browser profile, one process: close "
            "that window's tabs, then try again. background_status shows it."
        )
    holder = browser.profile_lock_holder(browser.PROFILE_DIR)
    if holder is not None:
        return (
            f"a browser (pid {holder}) holds the staging profile. Quit it (Cmd+Q), then try again."
        )
    return None


def _stage_in_background(conn: sqlite3.Connection, job_ids: list[int]) -> str:
    blocker = _staging_blocker(conn)
    if blocker:
        return f"nothing staged: {blocker}"
    launch = background.spawn(
        conn,
        "stage",
        "--job-ids",
        ",".join(str(j) for j in job_ids),
        "--keep-open",
        job_id=job_ids[0] if len(job_ids) == 1 else None,
    )
    return (
        f"staging jobs {job_ids} in ONE headed browser, at most 8 tabs at a time — "
        f"{launch.describe()}. Each form is left open for review; close the tabs when "
        "you are done. Screenshots land in out/screenshots/. background_status shows "
        "the run's result per job."
    )


@mcp.tool()
def stage_forms(ids: str = "", job_ids: str = "") -> str:
    """Playwright: open, autofill from the answer bank + drafts, highlight open
    questions, screenshot, and leave each form open on the review screen.
    Leaves submit to the person. `ids` are pinned queue numbers ('1,3'), `job_ids`
    raw job ids ('412, 418'). All of them run in ONE background process
    (`jobkit stage --job-ids … --keep-open`); the browser profile admits one
    process, so a call while staging is running is refused."""
    conn = _conn()
    try:
        targets = _targets(conn, ids, job_ids)
    except ValueError as e:
        return f"nothing staged: {e}"
    return _stage_in_background(conn, [j for _, j in targets])


@mcp.tool()
def open_queue() -> str:
    """Open every application ready for you — latest staging run 'staged', or
    'needs_human' with fields filled and a question left for you — as tabs,
    fully staged first, then cheapest friction. One background process
    (`jobkit open-queue`); the browser stays open until you close the tabs.
    You click submit."""
    conn = _conn()
    rows = conn.execute(q.OPEN_QUEUE_JOBS).fetchall()
    if not rows:
        return (
            "nothing to open: no job's latest staging run is staged, or needs_human with "
            "fields filled. Stage forms first (stage_forms), or see background_status."
        )
    blocker = _staging_blocker(conn)
    if blocker:
        return f"nothing opened: {blocker}"
    launch = background.spawn(conn, "open-queue")
    listing = "\n".join(
        f"- job {r['job_id']}: {r['company_name']} — {r['title']} ({r['status']})" for r in rows
    )
    return f"opening {len(rows)} application(s) — {launch.describe()}:\n{listing}"


@mcp.tool()
def restage(n: int) -> str:
    """Re-run staging for queue item n from the cached form spec (one
    background process, form left open for review)."""
    conn = _conn()
    ((_, job_id),) = _resolve(conn, [n])
    return _stage_in_background(conn, [job_id])


@mcp.tool()
def background_status(limit: int = 8) -> str:
    """What the background processes started from here (tailor, stage,
    open-queue, nightly) actually did, newest first: the command, pid,
    running or exit code, and the last lines of its output log
    (logs/spawn/)."""
    rows = background.statuses(_conn(), limit=max(1, min(limit, 50)))
    if not rows:
        return "no background launches recorded"
    return "\n".join(r.render() for r in rows)


@mcp.tool()
def run_nightly() -> str:
    """Start the nightly run now, in the background (`jobkit nightly`, what
    the launchd schedule runs at 02:00): poll, feeds, filter, triage, form
    specs, standing rules, tailor approved, ghost-close, then stage every
    approved + tailored job not staged since (one headed browser, closed when
    done), then the digest. It takes a while. Nothing is ever submitted:
    staged forms wait for you (open_queue). schedule_status shows the run
    stage by stage and its exit; background_status shows its output. Refused
    while another nightly run is running."""
    conn = _conn()
    busy = nightly.in_progress(conn)
    if busy is not None:
        return (
            f"not started: a nightly run is already running (run {busy.run}, pid {busy.pid}, "
            f"started {busy.started} UTC). schedule_status shows its stages."
        )
    launched = background.running(conn, ("nightly",))
    if launched is not None:
        return (
            f"not started: a nightly run started from here is still running (launch "
            f"#{launched.event_id}, pid {launched.pid}). background_status shows it."
        )
    launch = background.spawn(conn, "nightly")
    out = (
        f"nightly run: {launch.describe()}. It runs every stage (poll through tailor, "
        "ghost-close, staging, digest); schedule_status shows each stage as it finishes and "
        "the exit (1 if any stage failed). Nothing is submitted."
    )
    holder = browser.profile_lock_holder(browser.PROFILE_DIR)
    if holder is not None:
        out += (
            f"\n⚠ A browser (pid {holder}) holds the staging profile: the staging stage will "
            "defer every job and fail unless you quit it (Cmd+Q) before then."
        )
    return out


@mcp.tool()
def schedule_status() -> str:
    """Read-only: is the nightly launchd agent (com.jobkit.nightly) installed
    and loaded, when it runs, launchd's last exit code, the last nightly run
    stage by stage plus the last successful one, and every job a staging run
    stopped on before filling anything (reason, screenshot, apply URL), which
    waits on you. Off macOS: says the LaunchAgent is macOS-only and still
    shows the nightly run record."""
    try:
        conn: sqlite3.Connection | None = _conn()
    except q.DatabaseMissing:
        conn = None
    text, _ = launchd.status(conn)
    return text


@mcp.tool()
def schedule_install(confirm: bool = False) -> str:
    """Install (or replace) the macOS LaunchAgent that runs `jobkit nightly`
    every night at schedule.nightly_at (config/filters.yaml, default 02:00).
    This creates a persistent scheduled job, so without confirm=true it is a
    dry run: it shows the agent (command, PATH, logs) and the warnings
    (the Mac must be awake then) and installs nothing. Show that to the human
    and call again with confirm=true only on their explicit yes. Refuses a
    checkout under ~/Desktop, ~/Documents, ~/Downloads or iCloud Drive, which
    launchd cannot read."""
    try:
        text = launchd.install(dry_run=not confirm)
    except launchd.ScheduleError as e:
        return str(e)
    return text + ("" if confirm else "\n\nCall schedule_install(confirm=true) to install it.")


@mcp.tool()
def schedule_uninstall(confirm: bool = False) -> str:
    """Unload and delete the nightly LaunchAgent (and the old agents
    scripts/install_schedule.sh installed). Without confirm=true it is a dry
    run that lists what would be removed; call again with confirm=true on the
    human's explicit yes."""
    try:
        text = launchd.uninstall(dry_run=not confirm)
    except launchd.ScheduleError as e:
        return str(e)
    return text


@mcp.tool()
def merge_feed_dupes(confirm: bool = False) -> str:
    """One-off repair for duplicate postings: merge every open aggregator-feed
    row whose apply URL names an ATS posting into that posting's ATS row (the
    ATS record is canonical, hard rule 10). Its sources and any human work
    (application, decisions) move to the ATS row; the feed row is closed, not
    deleted. A pair where both rows carry an application or a decision is
    listed and left alone. New polls do this automatically. Without
    confirm=true this is a dry run that only counts — show the counts to the
    human and call again with confirm=true."""
    from jobkit.pipeline.dedup import merge_feed_dupes as _merge

    return _merge(_conn(), dry_run=not confirm).render()


@mcp.tool()
def repair_applications(confirm: bool = False) -> str:
    """Find application rows a submission left inconsistent and fix them:
    a job marked submitted with no application row saying so, several
    application rows on one job, a submitted row whose stage went back
    (re-tailoring), an application left on a merged aggregator row. A job
    'applied' with no record of the day is listed for you, never guessed.
    Without confirm=true this is a dry run that only lists what it would do:
    show it to the person and call again with confirm=true
    (`jobkit repair-applications [--dry-run]`)."""
    return repair_application_rows(_conn(), dry_run=not confirm).render()


@mcp.tool()
def review_artifacts(n: int) -> str:
    """Authenticity verdict + flagged spans for queue item n's artifacts."""
    conn = _conn()
    ((_, job_id),) = _resolve(conn, [n])
    rows = conn.execute(
        """SELECT artifact, verdict, lexical_flags, structural_flags, drift_flags,
                  critic_notes, checked_at
           FROM authenticity_runs WHERE job_id=? ORDER BY id DESC LIMIT 4""",
        (job_id,),
    ).fetchall()
    facts = conn.execute(q.PROVENANCE_EVENTS_FOR_JOB, {"job_id": job_id}).fetchall()
    if not rows and not facts:
        return "no authenticity runs yet — artifacts not generated"
    out = []
    for r in rows:
        out.append(f"{r['artifact']} [{r['checked_at']}]: {r['verdict']}")
        for k in ("lexical_flags", "structural_flags", "drift_flags"):
            if r[k] and r[k] != "[]":
                out.append(f"  {k}: {r[k][:300]}")
        if r["critic_notes"]:
            out.append(f"  critic: {r['critic_notes'][:300]}")
    # fact-check failures (cover letter / drafted answers): nothing was written as final
    for f in facts:
        out.append(f"{f['kind']} [{f['at']}]: REJECTED — {f['detail'][:600]}")
    return "\n".join(out)


@mcp.tool()
def get_self_review(n: int = 0, job_id: int = 0, full: bool = False) -> str:
    """The self-review of queue item n's drafted artifacts (or any job by
    job_id): for the resume selection, the cover letter and each drafted form
    answer, the verdict (good/revise), whether a resume revision was accepted
    or discarded (a revision is kept only if it passes every check; a letter
    or answer is never revised, its review is reported only), the bullets
    added or dropped, and the questions it raised for you: facts missing from
    the corpus that would close a real gap. full=true adds the job's
    self_review.md (what it asked itself, every change and why). Read-only."""
    from jobkit.pipeline import self_review
    from jobkit.pipeline import tailor as tailor_mod

    conn = _conn()
    if job_id:
        target = job_id
    elif n:
        ((_, target),) = _resolve(conn, [n])
    else:
        return "give a queue number (n=3) or a job id (job_id=412)"
    return self_review.summary(conn, target, tailor_mod.OUT_DIR, full=full)


@mcp.tool()
def get_questions(show_all: bool = False) -> str:
    """Questions for the human: facts missing from the corpus that would close
    gaps in open queued/approved jobs — the self-review's questions plus gaps
    the fit scorer named repeatedly (tools, methodologies, experience types),
    deduplicated, ranked by how many open jobs each would help, then their fit
    scores. Each shows why it matters and which jobs it helps. At most
    questions.max_per_day (config/filters.yaml, default 3) per day;
    show_all=true lists every open one. Also lists answers waiting for
    confirm_proposal. Read-only apart from recording what was asked today."""
    from jobkit.pipeline import questions

    return questions.render(_conn(), all_=show_all)


@mcp.tool()
def answer_question(
    key: str,
    answer: str = "",
    evidence: str = "",
    metric_confidence: str = "",
    role_id: str = "",
    kind: str = "",
    decline: bool = False,
) -> str:
    """Record the human's answer to question `key` (from get_questions), in
    their own words. An answer that is only a "no" ("no", "never used it", "I
    have never worked in healthcare", "not that I recall"), or decline=true
    when the human says they decline, is recorded as a decline: the question is
    not asked again and the gap stays a gap (undecline_question takes it back).
    An answer that may be a no or a fact ("Never below 95% on
    patient-satisfaction surveys", "No, but I worked in a clinic") is refused:
    relay that to the human, never reword it yourself. A tool or method answer
    is refused unless it is one plain sentence of the human's own use:
    "[<tool>:] [yes,] I used <tool>" (or I've used, I have used, I use, I ran;
    "it" for the tool), then only how often, how long, at which of their corpus
    companies and for what ("I used Clari weekly at Haulmark for two years").
    "Yes, daily", "Yes, my manager", "My manager used Clari", "Clari? Only
    heard of it" and "Only HubSpot" are refused; kind="skill" is refused for
    anything but a tool or method question. An answer to any other question
    that names a tool or method ("Yes, I trained 40 new hires at a clinic; the
    team used Clari") is refused too: a tool enters only through its own
    question (tool-<x>), never inside another answer; never cut the tool out of
    the answer yourself. Pass the human's words exactly; never add a "yes", an
    "I used", a company or a tool name they did not say, and never rephrase a
    refused answer into the accepted form yourself: ask the human. Any other
    answer becomes a corpus PROPOSAL
    (data/corpus_proposals.yaml), NOT a corpus edit: a tool or method answer
    proposes a skill_index item, anything else a bullet whose text is the
    answer. `evidence` (where it is defensible from) and `metric_confidence`
    (verified | estimate | none) must come from the human, never from you; a
    bullet also needs the corpus `role_id`. They can be given here or to
    confirm_proposal. Never fill them in yourself."""
    from jobkit.pipeline import questions

    return questions.answer(
        _conn(),
        key,
        answer,
        evidence=evidence,
        metric_confidence=metric_confidence,
        role_id=role_id,
        kind=kind,
        decline=decline,
    ).text


@mcp.tool()
def undecline_question(key: str) -> str:
    """Take back the human's decline of question `key` (call only when they
    ask): the question is open again, can be answered with answer_question,
    and returns to get_questions while an open job still lists it. Nothing is
    written to the corpus."""
    from jobkit.pipeline import questions

    return questions.undecline(_conn(), key).text


@mcp.tool()
def confirm_proposal(
    proposal_id: int, evidence: str = "", metric_confidence: str = "", role_id: str = ""
) -> str:
    """The human gate for a corpus proposal: call only on the human's explicit
    confirmation of that proposal. Writes it into data/career_corpus.yaml (a
    skill_index item or a bullet, the human's text; a skill item is shown
    next to the human's whole answer, verbatim) with the evidence and
    metric_confidence the human set — refused while either (or a bullet's
    role_id) is unset; they are never defaulted. Then re-scores the jobs its
    question listed in the background (background_status shows it; rescore()
    shows the results)."""
    from jobkit.pipeline import questions

    return questions.confirm(
        _conn(),
        proposal_id,
        evidence=evidence,
        metric_confidence=metric_confidence,
        role_id=role_id,
    ).text


@mcp.tool()
def rescore(job_ids: str = "", confirm: bool = False) -> str:
    """Re-score jobs whose fit score predates the current corpus — open
    queued, filtered and approved jobs, and jobs rejected only by the score
    gate — or the given job_ids ('12, 15'), then re-apply the queue gate and
    the standing rules. Approved and applied jobs keep their status (their new
    score is reported). A score-gate rejection the current filters reject
    (stale posting, blocklisted company, title or comp) stays rejected and is
    not re-scored. Without confirm=true this is a dry run listing the
    jobs (no LLM call), followed by the latest rescore results; with
    confirm=true it runs in the background (`jobkit rescore`)."""
    from jobkit.pipeline import rescore as rescore_mod

    conn = _conn()
    try:
        ids = _parse_job_ids(job_ids) if job_ids.strip() else []
    except ValueError as e:
        return f"nothing re-scored: {e}"
    if not confirm:
        out = rescore_mod.rescore(conn, ids or None, dry_run=True).render()
        last = rescore_mod.recent(conn)
        if last:
            out += "\n\nLatest rescore results:\n" + "\n".join(f"  {ln}" for ln in last)
        return out + "\n\nCall rescore(confirm=true) to run it in the background."
    busy = background.running(conn, ("rescore",))
    if busy is not None:
        return (
            f"not started: a rescore is already running (launch #{busy.event_id}, pid "
            f"{busy.pid}). background_status shows it."
        )
    args = ["rescore", *(["--job-ids", ",".join(str(j) for j in ids)] if ids else [])]
    launch = background.spawn(conn, *args)
    return (
        f"rescore: {launch.describe()}. background_status shows its output; rescore() "
        "lists the new scores once it finishes."
    )


@mcp.tool()
def rejudge(job_ids: str = "", confirm: bool = False) -> str:
    """Check the stored cover letters and drafted answers the claim judge
    never read (written before it existed, or under an older judge prompt),
    on every job not submitted or applied to, or on job_ids ('12, 15'): the
    rule-based fact check, then the claim judge. A letter that fails goes to
    cover_rejected.md and its application back to approved (never staged); an
    answer that fails goes back to the open questions (get_novel_questions)
    with the reasons. Answers the human gave are never judged. Without
    confirm=true this is a dry run listing what would be checked (no LLM
    call); with confirm=true it runs `jobkit rejudge` in the background."""
    from jobkit.pipeline import rejudge as rejudge_mod

    conn = _conn()
    try:
        ids = _parse_job_ids(job_ids) if job_ids.strip() else []
    except ValueError as e:
        return f"nothing re-judged: {e}"
    if not confirm:
        out = rejudge_mod.rejudge(conn, ids or None, dry_run=True).render()
        return out + "\n\nCall rejudge(confirm=true) to run it in the background."
    busy = background.running(conn, ("rejudge",))
    if busy is not None:
        return (
            f"not started: a rejudge is already running (launch #{busy.event_id}, pid "
            f"{busy.pid}). background_status shows it."
        )
    args = ["rejudge", *(["--job-ids", ",".join(str(j) for j in ids)] if ids else [])]
    launch = background.spawn(conn, *args)
    return (
        f"rejudge: {launch.describe()}. background_status shows its report; "
        "review_artifacts(n) and get_novel_questions(n) show what failed, per job."
    )


@mcp.tool()
def doctor() -> str:
    """Read-only setup check for this jobkit checkout: Python 3.12 and uv,
    Playwright's Chromium, the LLM backend (ANTHROPIC_API_KEY or the `claude`
    CLI; with the CLI, one tiny call checks the login: an expired one is fixed
    by running `claude`, then /login, and an unknown or old model by
    `claude update`), the checkout not under an iCloud-synced folder, every personal config
    file (config/*.yaml), the career corpus and answer bank, the database and
    the MCP registration, plus two warnings (a User-Agent contact email, and a
    commute time for every allowed place). Each problem comes with its exact
    fix; show them to the human. A missing config file is fixed by the setup
    interview or its template, never by copying it out of git history (that is
    another person's config). Changes nothing. Call it after setup and
    whenever another tool reports a missing file."""
    from jobkit.doctor import run_checks

    return run_checks().render()


# The setup interview (ONBOARDING.md): setup_status, ingest_resume,
# corpus_draft_status, confirm_bullets, update_corpus_draft, promote_corpus,
# answer_bank_questions, build_answer_bank, run_intake, seed_targets.
onboarding_tools.register(mcp)


def main() -> None:
    paths.enter_home()  # project root as cwd + .env loaded, whatever launched us
    mcp.run()


if __name__ == "__main__":
    main()
