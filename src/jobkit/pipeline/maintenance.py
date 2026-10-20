"""One-off data repairs. Each one is a named CLI command and MCP tool, safe to
run twice, logged, and recorded as an `events` row (job_id NULL).

purge_empty_jd_cache (audit 2026-10-08 §2 item 2): before cache keys covered
the full prompt, every LLM cache key was built from jd_hash alone, and every
job with an empty JD hashed to sha256("") = e3b0c442…. One job's extract,
remote, comp, score, bullet selection and cover letter were served to every
other JD-less job. This deletes those cache rows and resets the scores of
jobs whose JD is too short to read, so nothing downstream keeps serving the
copied answer."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from jobkit import db as q
from jobkit.log import log
from jobkit.normalize import MIN_JD_CHARS, jd_is_missing

# sha256 of the empty string, as it appears inside the legacy cache keys
EMPTY_JD_HASH_PREFIX = hashlib.sha256(b"").hexdigest()[:8]  # "e3b0c442"


@dataclass
class PurgeReport:
    dry_run: bool
    cache_rows: int  # llm_cache rows whose key contains the empty-JD hash
    short_jd_jobs: int  # jobs whose description text is under MIN_JD_CHARS
    scores_reset: int  # of those, scores rows deleted
    requeued: int  # of those, status queued -> filtered (re-triage rejects them)

    def render(self) -> str:
        verb = "would" if self.dry_run else "did"
        head = "DRY RUN — nothing changed.\n" if self.dry_run else ""
        return head + (
            f"llm_cache rows keyed on the empty-JD hash ({EMPTY_JD_HASH_PREFIX}): "
            f"{self.cache_rows} ({verb} delete)\n"
            f"jobs with under {MIN_JD_CHARS} characters of JD: {self.short_jd_jobs}\n"
            f"scores reset: {self.scores_reset} ({verb} delete)\n"
            f"queued -> filtered: {self.requeued} (the next triage run rejects them "
            "as jd_missing)\n"
            "Approved, applied and snoozed jobs keep their status. Artifacts already "
            "generated in out/ are not touched."
        )


def purge_empty_jd_cache(conn: sqlite3.Connection, *, dry_run: bool = False) -> PurgeReport:
    """Delete llm_cache rows whose key contains the empty-string sha256 prefix,
    and reset scores for jobs whose JD is under MIN_JD_CHARS. dry_run counts only."""
    has_cache = conn.execute(q.LLM_CACHE_EXISTS).fetchone() is not None
    needle = {"needle": EMPTY_JD_HASH_PREFIX}
    cache_rows = (
        int(conn.execute(q.COUNT_CACHE_ROWS_CONTAINING, needle).fetchone()["n"]) if has_cache else 0
    )
    short = [
        (int(r["id"]), str(r["status"]))
        for r in conn.execute(q.ALL_JOB_JDS)
        if jd_is_missing(r["jd_html"])
    ]
    scored = {int(r["job_id"]) for r in conn.execute(q.SCORED_JOB_IDS)}
    to_reset = [{"job_id": jid} for jid, _ in short if jid in scored]
    to_requeue = [{"job_id": jid} for jid, status in short if status == "queued"]
    report = PurgeReport(
        dry_run=dry_run,
        cache_rows=cache_rows,
        short_jd_jobs=len(short),
        scores_reset=len(to_reset),
        requeued=len(to_requeue),
    )
    if not dry_run:
        if has_cache:
            conn.execute(q.DELETE_CACHE_ROWS_CONTAINING, needle)
        conn.executemany(q.DELETE_SCORE, to_reset)
        conn.executemany(q.REQUEUE_UNSCORED, to_requeue)
        detail = {
            "cache_rows": report.cache_rows,
            "short_jd_jobs": report.short_jd_jobs,
            "scores_reset": report.scores_reset,
            "requeued": report.requeued,
        }
        conn.execute(
            q.RECORD_EVENT,
            {"job_id": None, "kind": "purge_empty_jd_cache", "detail": json.dumps(detail)},
        )
        conn.commit()
    log(
        "maintenance.purge_empty_jd_cache",
        dry_run=dry_run,
        cache_rows=report.cache_rows,
        short_jd_jobs=report.short_jd_jobs,
        scores_reset=report.scores_reset,
        requeued=report.requeued,
    )
    return report
