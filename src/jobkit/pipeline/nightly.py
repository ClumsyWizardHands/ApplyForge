"""`jobkit nightly`: the one unattended run a night (approved 2026-10-08).

It is run-all (poll, feeds, discovery, filter, triage, form specs, standing
rules, tailor, ghost-close) with one more stage before the digest:

* **stage**: every approved job whose application is tailored and has not
  been staged since (except one whose cover letter the claim judge never
  read: pipeline/letter_gate.py holds it for re-tailoring), in ONE browser
  process (`stage_jobs`: headed, the one
  persistent profile, at most 8 tabs at a time). Each batch's browser is
  closed when its forms are filled, so the profile lock is released for the
  morning. Staging fills a form and stops; it never clicks submit (hard rule
  1). MCP open_queue / `jobkit open-queue` reopens the forms for review.
* **digest**: last, so it reports the staging too.

Nobody watches the staging stage, so a job it stops on (needs_human: a
CAPTCHA, a government-ID or payment field, an assessment, no form, a
required question) must reach the human some other way (hard rule 2: detect,
abort, hand to the human). The stage's summary names each such job with its
company and reason; the digest, MCP get_queue and `jobkit schedule status`
list every job whose latest staging run stopped before filling anything
(pipeline/halts.py). Two halts that can be transient are retried on a later
night (db.NIGHTLY_STAGE_CANDIDATES): no-form-spec-captured once a form spec
exists, posting-not-found once more. A CAPTCHA or a hard stop never is.

A job whose staging raised (status failed: a dead apply URL, a page that
never loads) writes no autofill_runs row, so it is retried on a later night,
but only until db.NIGHTLY_STAGE_FAILURE_CAP failures since it was tailored:
each failure is recorded as a `nightly.stage_failed` event, and the failure
that reaches the cap hands the job to the human (the summary's "Needs you",
and pipeline/halts.py lists it with the error) instead of failing the stage.
Without the cap one broken posting failed the stage, and so the whole nightly
run, every night, and the "last successful nightly run" never advanced.

There is no submit stage and there will not be one: submitting is the human's
click (hard rule 1, SPEC §8; `gates.require_human_confirmation_of_submission`).

Every stage runs isolated (pipeline/runner.py) and the run is recorded with
mode `nightly`: `run_all.start` (with this process's pid), one
`run_all.stage` per stage, `run_all.done`. The digest banner reads those back
as "last successful nightly run". `jobkit nightly` exits 1 when any stage
failed, including a staging stage that left jobs unstaged (deferred: the
profile was locked, never capped since it is transient; failed: an error, below
the cap). One nightly at a time: a second one refuses while the first one's
process is alive."""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable
from typing import Any

from jobkit import db as q
from jobkit.autofill.browser import stage_jobs
from jobkit.log import log
from jobkit.pipeline import halts, letter_gate
from jobkit.pipeline.runner import (
    NIGHTLY,
    RunRecord,
    Stage,
    StageOutcome,
    ago,
    last_successful_nightly,
    latest_nightly,
    run_stages,
    stage_rows,
)


class NightlyBusy(RuntimeError):
    """Another nightly run is still running."""


class StagingIncomplete(RuntimeError):
    """Some jobs were not staged (deferred, or failed below the failure cap).
    Raised after every job has its result, so the stage is recorded as failed
    with the reasons."""


def stage_candidates(conn: sqlite3.Connection) -> list[int]:
    return [int(r["id"]) for r in conn.execute(q.NIGHTLY_STAGE_CANDIDATES)]


def _company(conn: sqlite3.Connection, job_id: int) -> str:
    brief = conn.execute(q.JOB_BRIEF, {"job_id": job_id}).fetchone()
    return str(brief["company_name"]) if brief is not None else "?"


def _needs_you(conn: sqlite3.Connection, results: list[dict[str, Any]]) -> list[str]:
    """'job 7 Acme: captcha-or-turnstile-visible' for every needs_human result:
    the halt reason, or the required questions left open (open_queue)."""
    out: list[str] = []
    for r in results:
        if r.get("status") != "needs_human":
            continue
        jid = int(r.get("job_id") or 0)
        # values the form changed after filling (autofill/browser read-back)
        changed = list(r.get("overwritten_after_fill") or [])
        why = r.get("reason") or (
            f"{r.get('required_open', '?')} required question(s) open"
            + (
                f", {len(changed)} value(s) the form changed after filling ({', '.join(changed)})"
                if changed
                else ""
            )
            + ", open_queue reopens it"
        )
        out.append(f"job {jid} {_company(conn, jid)}: {why}")
    return out


def _one_line(text: str, limit: int = 200) -> str:
    """A Playwright error is several lines (the call log); keep one short line."""
    return " ".join(text.split())[:limit]


def record_failures(conn: sqlite3.Connection, results: list[dict[str, Any]]) -> dict[int, int]:
    """Record one `nightly.stage_failed` event per failed result and return
    {job_id: failures since it was last tailored} for the jobs that have now
    reached db.NIGHTLY_STAGE_FAILURE_CAP: the nightly run stops retrying them
    (db.NIGHTLY_STAGE_CANDIDATES) and pipeline/halts.py hands them to the
    human. A deferred result is transient and is never recorded."""
    capped: dict[int, int] = {}
    for r in results:
        jid = int(r.get("job_id") or 0)
        if r.get("status") != "failed" or not jid:
            continue
        reason = _one_line(str(r.get("reason") or "")) or "unknown error"
        conn.execute(
            q.RECORD_EVENT,
            {"job_id": jid, "kind": q.NIGHTLY_STAGE_FAILED_EVENT, "detail": reason},
        )
        conn.commit()
        n = int(conn.execute(q.NIGHTLY_STAGE_FAILURES_FOR_JOB, {"job_id": jid}).fetchone()[0])
        gave_up = n >= q.NIGHTLY_STAGE_FAILURE_CAP
        log("nightly.stage_failed", job_id=jid, reason=reason, failures=n, gave_up=gave_up)
        if gave_up:
            capped[jid] = n
    return capped


def stage_tailored(conn: sqlite3.Connection) -> str:
    """The nightly staging stage: stage every approved, tailored, unsubmitted
    job not staged since it was tailored (and the transient halts retried,
    db.NIGHTLY_STAGE_CANDIDATES), in one browser process that closes when it
    is done (keep_open=False), with no carry-forward between jobs (no human is
    in the session, SPEC §7.1). A job left needs_human (CAPTCHA, required
    question) is an outcome, not a failure, and the summary names each one
    with its company and reason (the digest and get_queue list them too,
    pipeline/halts.py); a deferred job fails the stage, and so does a failed
    one until its failure reaches db.NIGHTLY_STAGE_FAILURE_CAP: that one is
    handed to the human the same way (record_failures) and the nightly run
    stops retrying it, so one dead posting cannot fail every night.

    A candidate whose cover letter the claim judge never read is not staged
    at all (pipeline/letter_gate.py): it is marked (a cover_needs_retailor
    event), named under "Needs you", and skipped every night until it is
    re-tailored. That is an outcome, not a stage failure."""
    unjudged = {p.job_id: p for p in letter_gate.sweep(conn)}
    candidates = stage_candidates(conn)
    held = [j for j in candidates if j in unjudged]
    ids = [j for j in candidates if j not in unjudged]
    held_lines = [
        f"job {j} {_company(conn, j)}: {letter_gate.REASON} ({unjudged[j].why}); not "
        f"staged until re-tailored (MCP retailor, `jobkit tailor --job-id {j}`)"
        for j in held
    ]
    if held:
        log("nightly.letters_held", job_ids=held)
    if not ids:
        if held_lines:
            return f"nothing new to stage. Needs you: {'; '.join(held_lines)}"
        return "nothing new to stage"
    results = stage_jobs(conn, ids, keep_open=False, carry_forward=False)
    capped = record_failures(conn, results)
    needs_you = held_lines + _needs_you(conn, results)
    for r in results:
        jid = int(r.get("job_id") or 0)
        if jid in capped:  # handed to the human: no longer a stage failure
            needs_you.append(
                f"job {jid} {_company(conn, jid)}: staging failed {capped[jid]} times, "
                f"not retried: {_one_line(str(r.get('reason') or ''), 120)}"
            )
    statuses = [
        "failed (left for you)" if int(r.get("job_id") or 0) in capped else str(r.get("status"))
        for r in results
    ]
    counts = Counter(statuses)
    summary = ", ".join(f"{n} {st}" for st, n in sorted(counts.items()))
    log("nightly.staged", job_ids=ids, counts=dict(counts), needs_human=needs_you)
    handed = f". Needs you: {'; '.join(needs_you)}" if needs_you else ""
    bad = [r for r, st in zip(results, statuses, strict=True) if st in ("deferred", "failed")]
    if bad:
        reasons = "; ".join(f"job {r.get('job_id')}: {r.get('reason', '')}" for r in bad[:5])
        raise StagingIncomplete(
            f"{len(bad)} of {len(ids)} job(s) not staged ({summary}): {reasons}{handed}"
        )
    return f"{len(ids)} job(s): {summary}{handed}"


def nightly_stages(run_all_stages: list[Stage]) -> list[Stage]:
    """run-all's stages with staging inserted before the digest, which stays
    last so it reports the staging."""
    body = [s for s in run_all_stages if s[0] != "digest"]
    digest = [s for s in run_all_stages if s[0] == "digest"]
    if not digest:
        raise ValueError("run-all's stage list has no digest stage")
    return [*body, ("stage", stage_tailored), *digest]


def in_progress(conn: sqlite3.Connection) -> RunRecord | None:
    """The nightly run that is still running, or None."""
    latest = latest_nightly(conn)
    return latest if latest is not None and latest.alive() else None


def run_nightly(
    conn: sqlite3.Connection,
    stages: list[Stage],
    echo: Callable[[str], None] | None = None,
) -> list[StageOutcome]:
    """Run the nightly stages, recorded with mode 'nightly'. Raises NightlyBusy
    (and runs nothing) while another nightly run is still running."""
    busy = in_progress(conn)
    if busy is not None:
        log("nightly.busy", other_run=busy.run, other_pid=busy.pid)
        raise NightlyBusy(
            f"another nightly run is still running (run {busy.run}, pid {busy.pid}, "
            f"started {busy.started} UTC); not starting a second one"
        )
    return run_stages(conn, stages, echo=echo, mode=NIGHTLY)


def report(conn: sqlite3.Connection) -> str:
    """The latest nightly run, stage by stage, the last successful one, and
    the jobs a staging run stopped on that wait on the human."""
    latest = latest_nightly(conn)
    if latest is None:
        return "\n".join(["last nightly run: none on record", *halts.report_lines(conn)])
    lines = [f"last nightly run: started {latest.started} UTC (run {latest.run}, pid {latest.pid})"]
    if latest.done:
        result = f"failed stages: {', '.join(latest.failed)}" if latest.failed else "every stage ok"
        lines.append(f"  finished {latest.finished} UTC: exit {latest.exit_code}, {result}")
    elif latest.alive():
        lines.append("  still running")
    else:
        lines.append(
            "  did not finish: its process is gone (stopped, crashed, or the Mac shut down); "
            "see logs/nightly.err.log"
        )
    for r in stage_rows(conn, latest.run):
        status = str(r["status"])
        what = str(r["error"] or "") if status == "error" else str(r["summary"] or "")
        if status == "llm_all_failed":
            what = f"every LLM call failed; {what}"
        lines.append(f"    {r['stage']}: {status} ({r['seconds']}s) {what[:160]}".rstrip())
    ok = last_successful_nightly(conn)
    lines.append(
        f"last successful nightly run: {ok[0]} UTC ({ago(ok[1])})"
        if ok
        else "last successful nightly run: none on record"
    )
    lines.extend(halts.report_lines(conn))
    return "\n".join(lines)
