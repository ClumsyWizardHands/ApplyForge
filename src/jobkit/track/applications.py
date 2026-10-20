"""One application row per job, and a submission that always leaves it at
stage 'submitted' with its follow-ups.

Diagnosis (2026-10-15, read only): an aggregator row (Himalayas, Adzuna)
whose apply URL was pointed at the employer's posting before staging
(apply_url_resolved events) was marked submitted (a marked_submitted event:
an application row at stage 'submitted' with its follow-ups). The next
nightly poll of the employer's board found the feed row naming its posting
and merged it into the ATS row (pipeline/dedup.merge_feed_row: a merged_into
event): the application row, its follow-ups and the human decision moved to
the ATS row, which is 'applied'; the feed row was closed. Nothing was lost,
but a lookup by the feed row's id (a LEFT JOIN from the feed row, or MCP
record_outcome on its id) found no application, so the submission looked
unrecorded.

Paths that could really leave a submitted job without one 'submitted'
application row, fixed here and in their callers:
* mark_submitted read only the newest application row: a job with two rows
  (an auto-approve that inserted a second one, a merge) got the newest
  marked and the older left as it was, or a second submission and a second
  set of follow-ups;
* re-tailoring a submitted job (`jobkit tailor --job-id N`, MCP retailor)
  set the newest row's stage back to 'tailored' or 'approved';
* record_outcome did not follow a merged feed row to its ATS row.

record_submission is what MCP mark_submitted runs. repair (CLI `jobkit
repair-applications [--dry-run]`, MCP repair_applications) finds and fixes
rows left inconsistent before this: a recorded submission with no submitted
application, several application rows on one job, a submitted row whose
stage went back, a submitted row with no follow-ups, and an application left
on a merged feed row. A job marked 'applied' with no record of when it was
submitted is reported, never invented."""

from __future__ import annotations

import datetime
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from jobkit import db as q
from jobkit.log import log
from jobkit.pipeline.dedup import follow_merges
from jobkit.track.followups import Scheduled, schedule_followups

SUBMITTED = "submitted"
# stages before a submission: a row with submitted_at in one went backwards
PRE_SUBMIT_STAGES = ("approved", "tailored", "staged")
# a job status a submitted application corrects to 'applied'
PRE_APPLIED_STATUSES = ("new", "filtered", "queued", "approved", "snoozed")
_PATHS = ("resume_path", "cover_path", "gap_report_path", "provenance_path")


def _rows(conn: sqlite3.Connection, job_id: int) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(q.APPLICATIONS_OF_JOB, {"job_id": job_id})]


def _followups(conn: sqlite3.Connection, application_id: int) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(q.FOLLOWUPS_OF_APPLICATION, {"application_id": application_id})
    ]


def _keeper(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The row to keep of several: the first submission (earliest
    submitted_at), else the newest row (the latest tailoring's paths)."""
    submitted = [r for r in rows if r["submitted_at"]]
    if submitted:
        return min(submitted, key=lambda r: (str(r["submitted_at"]), int(r["id"])))
    return rows[-1]


def consolidate(
    conn: sqlite3.Connection, job_id: int, *, dry_run: bool = False
) -> tuple[dict[str, Any] | None, list[int]]:
    """Fold a job's application rows into one: (the row kept, the ids folded
    into it). The kept row takes the others' artifact paths where it has
    none and their follow-ups of kinds it lacks; their other follow-ups and
    the rows themselves are deleted, and an 'applications_consolidated'
    event keeps every removed row and follow-up as it was."""
    rows = _rows(conn, job_id)
    if len(rows) <= 1:
        return (rows[0] if rows else None), []
    keep = _keeper(rows)
    others = [r for r in rows if r["id"] != keep["id"]]
    if dry_run:
        return keep, [int(r["id"]) for r in others]
    kinds = {f["kind"] for f in _followups(conn, int(keep["id"]))}
    moved: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for r in sorted(others, key=lambda r: int(r["id"]), reverse=True):  # newest paths first
        conn.execute(q.FILL_APPLICATION_PATHS, {"id": keep["id"], **{k: r[k] for k in _PATHS}})
        for f in _followups(conn, int(r["id"])):
            if f["kind"] in kinds:
                dropped.append(f)
                conn.execute(q.DELETE_FOLLOWUP, {"id": f["id"]})
            else:
                kinds.add(f["kind"])
                moved.append(f)
                conn.execute(q.MOVE_FOLLOWUP, {"id": f["id"], "application_id": keep["id"]})
        conn.execute(q.DELETE_APPLICATION, {"id": r["id"]})
    conn.execute(
        q.RECORD_EVENT,
        {
            "job_id": job_id,
            "kind": "applications_consolidated",
            "detail": json.dumps(
                {
                    "kept": keep["id"],
                    "removed": others,
                    "followups_moved": moved,
                    "followups_dropped": dropped,
                },
                default=str,
            ),
        },
    )
    log("applications.consolidated", job_id=job_id, kept=keep["id"], removed=len(others))
    return keep, [int(r["id"]) for r in others]


@dataclass
class Submission:
    """What record_submission did for one job."""

    job_id: int
    application_id: int
    already: bool  # it was marked submitted before: left as it was
    submitted_at: str
    scheduled: Scheduled
    folded: list[int] = field(default_factory=list)  # application rows folded into it


def _day_of(stamp: str) -> datetime.date:
    return datetime.date.fromisoformat(str(stamp)[:10])


def record_submission(
    conn: sqlite3.Connection,
    job_id: int,
    company_id: int | None,
    day: datetime.date,
    submitted_at: str,
) -> Submission:
    """The human submitted this job's application (MCP mark_submitted).
    Afterwards the job (followed through merges) has exactly one application
    row, at stage 'submitted' with submitted_at, and its follow-ups
    (track/followups.py). Already submitted: its date and stage stay, and
    follow-ups are scheduled only if it has none at all. The caller commits."""
    job_id = follow_merges(conn, job_id)
    keep, folded = consolidate(conn, job_id)
    if keep is None:
        app_id = int(
            conn.execute(q.INSERT_APPLICATION, {"job_id": job_id, "stage": SUBMITTED}).lastrowid
            or 0
        )
        keep = {"id": app_id, "stage": SUBMITTED, "submitted_at": None}
    app_id = int(keep["id"])
    existing = {f["kind"] for f in _followups(conn, app_id)}
    if keep["submitted_at"]:
        stamp = str(keep["submitted_at"])
        sched = (
            Scheduled(due=[], skipped=[])
            if existing
            else schedule_followups(conn, app_id, company_id, _day_of(stamp))
        )
        if keep["stage"] in PRE_SUBMIT_STAGES:  # a re-tailor set it back
            conn.execute(q.SET_APPLICATION_STAGE, {"id": app_id, "stage": SUBMITTED})
        return Submission(job_id, app_id, True, stamp, sched, folded)
    conn.execute(q.MARK_APPLICATION_SUBMITTED, {"id": app_id, "submitted_at": submitted_at})
    conn.execute(q.SET_JOB_STATUS, {"job_id": job_id, "status": "applied"})
    sched = schedule_followups(conn, app_id, company_id, day, skip_kinds=existing)
    return Submission(job_id, app_id, False, submitted_at, sched, folded)


# ---- repair --------------------------------------------------------------------


@dataclass
class Finding:
    job_id: int
    kind: str
    detail: str
    fixed: bool = False  # repaired (or, in a dry run, would be)


@dataclass
class RepairReport:
    dry_run: bool
    checked: int = 0
    findings: list[Finding] = field(default_factory=list)

    def render(self) -> str:
        head = "DRY RUN — nothing changed. " if self.dry_run else ""
        verb = "would fix" if self.dry_run else "fixed"
        fixable = [f for f in self.findings if f.fixed]
        manual = [f for f in self.findings if not f.fixed]
        lines = [
            f"{head}checked {self.checked} job(s) with an application or an 'applied' "
            f"status; {verb} {len(fixable)}, {len(manual)} left for you"
        ]
        lines += [f"- job {f.job_id}: {f.kind}: {f.detail} [{verb}]" for f in fixable]
        lines += [f"- job {f.job_id}: {f.kind}: {f.detail} [needs you]" for f in manual]
        if not self.findings:
            lines.append(
                "every submitted job has exactly one application row at a submitted stage "
                "with its follow-ups"
            )
        return "\n".join(lines)


def _submissions(conn: sqlite3.Connection) -> dict[int, str]:
    """job id now carrying it (followed through merges) -> the day the human
    said it was submitted (the newest marked_submitted event)."""
    out: dict[int, str] = {}
    for r in conn.execute(q.MARKED_SUBMITTED_EVENTS).fetchall():
        try:
            day = str(json.loads(r["detail"] or "{}").get("submitted_on") or "")
            datetime.date.fromisoformat(day)
        except (ValueError, AttributeError, TypeError):
            day = str(r["at"])[:10]
        out[follow_merges(conn, int(r["job_id"]))] = day
    return out


def repair(conn: sqlite3.Connection, *, dry_run: bool = False) -> RepairReport:
    """Find (and unless dry_run, fix) application rows left inconsistent; see
    the module docstring. Safe to run twice."""
    rep = RepairReport(dry_run=dry_run)

    # 1. an application left on a feed row that was merged into an ATS row
    for r in conn.execute(q.APPLICATIONS_ON_MERGED_FEED_ROWS).fetchall():
        feed, carrier = int(r["job_id"]), follow_merges(conn, int(r["job_id"]))
        if carrier == feed:
            continue
        if _rows(conn, carrier):
            rep.findings.append(
                Finding(
                    feed,
                    "application-on-merged-row",
                    f"application {r['id']} stayed on this closed aggregator row, and job "
                    f"{carrier} (its employer posting) has an application too: decide which "
                    "to keep",
                )
            )
            continue
        rep.findings.append(
            Finding(
                carrier,
                "application-on-merged-row",
                f"application {r['id']} stayed on aggregator row {feed}, merged into this job: "
                "moved here",
                fixed=True,
            )
        )
        if not dry_run:
            conn.execute(q.MOVE_APPLICATION, {"id": r["id"], "job_id": carrier})

    submissions = _submissions(conn)
    ids = {int(r["id"]) for r in conn.execute(q.JOBS_WITH_APPLICATION_STATE)} | set(submissions)
    for job_id in sorted(ids):
        rep.checked += 1
        job = conn.execute(q.JOB_BRIEF, {"job_id": job_id}).fetchone()
        if job is None:
            continue
        rows = _rows(conn, job_id)
        # 2. several application rows
        if len(rows) > 1:
            keep = _keeper(rows)
            rep.findings.append(
                Finding(
                    job_id,
                    "several-applications",
                    f"{len(rows)} application rows ({', '.join(str(r['id']) for r in rows)}): "
                    f"folded into {keep['id']}",
                    fixed=True,
                )
            )
            if not dry_run:
                consolidate(conn, job_id)
            rows = [keep]
        submitted = [r for r in rows if r["submitted_at"]]
        day = submissions.get(job_id)
        # 3. a recorded submission with no submitted application
        if not submitted and day:
            rep.findings.append(
                Finding(
                    job_id,
                    "submission-not-recorded",
                    f"marked submitted on {day}, but no application row says so: recorded "
                    "as submitted that day, with its follow-ups",
                    fixed=True,
                )
            )
            if not dry_run:
                record_submission(
                    conn, job_id, job["company_id"], datetime.date.fromisoformat(day), day
                )
            continue
        if not submitted:
            if job["status"] == "applied":
                rep.findings.append(
                    Finding(
                        job_id,
                        "applied-without-submission",
                        "status 'applied', but no submitted application and no record of the "
                        "day: record it with mark_submitted(job_ids="
                        f"'{job_id}', submitted_on='YYYY-MM-DD')",
                    )
                )
            continue
        app = submitted[0]
        # 4. a submitted row whose stage went back (re-tailoring)
        if app["stage"] in PRE_SUBMIT_STAGES:
            rep.findings.append(
                Finding(
                    job_id,
                    "stage-went-back",
                    f"application {app['id']} was submitted {app['submitted_at']} but is at "
                    f"stage '{app['stage']}': set back to 'submitted'",
                    fixed=True,
                )
            )
            if not dry_run:
                conn.execute(q.SET_APPLICATION_STAGE, {"id": app["id"], "stage": SUBMITTED})
        # 5. a submitted row with no follow-ups (none once the employer answered)
        if app["stage"] in (SUBMITTED, *PRE_SUBMIT_STAGES) and not _followups(conn, int(app["id"])):
            rep.findings.append(
                Finding(
                    job_id,
                    "no-followups",
                    f"application {app['id']} was submitted {app['submitted_at']} with no "
                    "follow-ups: scheduled from that day",
                    fixed=True,
                )
            )
            if not dry_run:
                schedule_followups(
                    conn, int(app["id"]), job["company_id"], _day_of(str(app["submitted_at"]))
                )
        # 6. a submitted job still listed as not applied
        if job["status"] in PRE_APPLIED_STATUSES:
            rep.findings.append(
                Finding(
                    job_id,
                    "status-not-applied",
                    f"status '{job['status']}' with a submitted application: set to 'applied'",
                    fixed=True,
                )
            )
            if not dry_run:
                conn.execute(q.SET_JOB_STATUS, {"job_id": job_id, "status": "applied"})
    if not dry_run:
        for f in rep.findings:
            if f.fixed:
                conn.execute(
                    q.RECORD_EVENT,
                    {
                        "job_id": f.job_id,
                        "kind": "application_repaired",
                        "detail": json.dumps({"kind": f.kind, "detail": f.detail}),
                    },
                )
        conn.commit()
    log(
        "applications.repair",
        dry_run=dry_run,
        checked=rep.checked,
        fixed=sum(1 for f in rep.findings if f.fixed),
        manual=sum(1 for f in rep.findings if not f.fixed),
    )
    return rep
