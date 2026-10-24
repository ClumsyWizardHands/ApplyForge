"""`jobkit run-all` stage runner (audit 2026-10-08 §3 item 5).

Each stage runs on its own: an exception in one stage is caught, its partial
uncommitted writes are rolled back, and the next stage still runs (the digest
is the last stage, so it is always written). A stage that returned normally
but whose every LLM call failed (the triage stages skip failed jobs and carry
on) counts as failed too. Cache hits do not count as answers here (D7): a
stage whose cached jobs were answered while every live call failed is still a
failed stage, and its error is the last failure's message, so the reason is
never dropped.

A backend that refuses every call (an expired Claude login, a rejected API
key, a used-up limit: llm.LLMBackendDown) makes each stage that tried an LLM
call `llm_backend_down`, with the reason as its error ("Claude login expired:
run claude, then /login"). llm.py fails every later call at once, so the run
does not spend minutes retrying a login that cannot work; the latch is
cleared when a run starts.

Every stage's outcome is recorded twice: a `run_all.stage` log line and a
`run_all.stage` events row (job_id NULL) whose JSON detail holds the run id,
stage, status (ok | error | llm_all_failed | llm_backend_down), seconds, LLM
calls answered (and of those, from the cache) and failed, the error, and a
short result summary. The digest banner and MCP get_queue / get_stats read
the latest run's failed stages from those rows.
A `run_all.start` event (run id, mode, pid, stage names) opens the run and a
`run_all.done` event (run id, mode, failed stages) closes it; a run with a
start and no done is still running, or was stopped. `mode` is `run-all` or
`nightly` (`jobkit nightly`, pipeline/nightly.py). The CLI exits non-zero when
any stage failed, so a scheduler sees the failure."""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from jobkit import background, llm
from jobkit import db as q
from jobkit.log import log

Stage = tuple[str, Callable[[sqlite3.Connection], object]]


@dataclass(frozen=True)
class StageOutcome:
    stage: str
    status: str  # ok | error | llm_all_failed | llm_backend_down
    seconds: float
    llm_answered: int  # live answers and cache hits
    llm_failed: int
    error: str | None
    summary: str
    llm_cached: int = 0  # of llm_answered, served from the cache

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def line(self) -> str:
        cached = f" ({self.llm_cached} from cache)" if self.llm_cached else ""
        llm_part = (
            f" [llm {self.llm_answered} answered{cached}, {self.llm_failed} failed]"
            if self.llm_answered or self.llm_failed
            else ""
        )
        if self.status in ("error", "llm_backend_down"):
            return f"{self.stage}: FAILED — {self.error}{llm_part}"
        if self.status == "llm_all_failed":
            why = f": {self.error}" if self.error else ""
            return f"{self.stage}: FAILED — every LLM call failed{why}{llm_part} ({self.summary})"
        return f"{self.stage}: {self.summary}{llm_part}"


def new_run_id() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S.%fZ")


def _record(conn: sqlite3.Connection, run_id: str, mode: str, o: StageOutcome) -> None:
    detail = {
        "run": run_id,
        "mode": mode,
        "stage": o.stage,
        "status": o.status,
        "seconds": o.seconds,
        "llm_answered": o.llm_answered,
        "llm_failed": o.llm_failed,
        "llm_cached": o.llm_cached,
        "error": o.error,
        "summary": o.summary,
    }
    conn.execute(
        q.RECORD_EVENT, {"job_id": None, "kind": "run_all.stage", "detail": json.dumps(detail)}
    )
    conn.commit()
    log("run_all.stage", **detail)


def _llm_verdict(status: str, error: str | None, live: int, failed: int) -> tuple[str, str | None]:
    """A stage's status and error once its LLM calls are counted: a backend
    that refuses every call (llm.backend_problem) makes any stage with a
    failed call `llm_backend_down`, with that reason; a stage that returned
    normally with failures and no live answer is `llm_all_failed`, with the
    last failure's message."""
    if not failed:
        return status, error
    problem = llm.backend_problem()
    if problem:
        if error and problem not in error:
            return "llm_backend_down", f"{problem} ({error})"[:500]
        return "llm_backend_down", problem
    if status == "ok" and not live:
        return "llm_all_failed", (llm.last_failure() or "")[:500] or None
    return status, error


def run_stages(
    conn: sqlite3.Connection,
    stages: list[Stage],
    run_id: str | None = None,
    echo: Callable[[str], None] | None = None,
    mode: str = "run-all",
) -> list[StageOutcome]:
    """Run every stage in order, isolated from the others, and record each
    outcome. Returns the outcomes; none of them raises."""
    run_id = run_id or new_run_id()
    names = [name for name, _ in stages]
    start = {"run": run_id, "mode": mode, "pid": os.getpid(), "stages": names}
    conn.execute(
        q.RECORD_EVENT, {"job_id": None, "kind": "run_all.start", "detail": json.dumps(start)}
    )
    conn.commit()
    log("run_all.start", **start)
    llm.reset_backend_problem()  # a new run tries the backend again
    outcomes: list[StageOutcome] = []
    for name, fn in stages:
        before = llm.call_counts()
        t0 = time.monotonic()
        error: str | None = None
        summary = ""
        try:
            summary = str(fn(conn))[:300]
            status = "ok"
        except Exception as e:  # noqa: BLE001 — one stage must not abort the run
            status = "error"
            error = f"{type(e).__name__}: {e}"[:500]
            with contextlib.suppress(sqlite3.Error):
                conn.rollback()  # drop this stage's uncommitted partial writes
        after = llm.call_counts()
        live = after.live - before.live
        cached = after.cached - before.cached
        failed = after.failed - before.failed
        status, error = _llm_verdict(status, error, live, failed)
        outcome = StageOutcome(
            stage=name,
            status=status,
            seconds=round(time.monotonic() - t0, 1),
            llm_answered=live + cached,
            llm_failed=failed,
            error=error,
            summary=summary,
            llm_cached=cached,
        )
        _record(conn, run_id, mode, outcome)
        if echo:
            echo(outcome.line())
        outcomes.append(outcome)
    failed_stages = [o.stage for o in outcomes if not o.ok]
    conn.execute(
        q.RECORD_EVENT,
        {
            "job_id": None,
            "kind": "run_all.done",
            "detail": json.dumps(
                {"run": run_id, "mode": mode, "stages": len(outcomes), "failed": failed_stages}
            ),
        },
    )
    conn.commit()
    log("run_all.done", run=run_id, mode=mode, stages=len(outcomes), failed=failed_stages)
    return outcomes


# ---- reading run records back (digest banner, `jobkit schedule status`) ----

NIGHTLY = "nightly"


def ago(hours: float) -> str:
    """'3 hours ago' / '2 days ago' for a span in hours."""
    hours = max(hours, 0.0)
    if hours < 1:
        return "less than an hour ago"
    if hours < 48:
        n = round(hours)
        return f"{n} hour{'s' if n != 1 else ''} ago"
    return f"{int(hours // 24)} days ago"


@dataclass(frozen=True)
class RunRecord:
    """One recorded run: its start event and, once it finished, its done event."""

    run: str
    pid: int
    started: str  # UTC, SQLite datetime('now') format
    finished: str | None  # None: still running, or stopped before it finished
    failed: tuple[str, ...]  # failed stages (empty until it finished)

    @property
    def done(self) -> bool:
        return self.finished is not None

    @property
    def exit_code(self) -> int | None:
        """The exit code `jobkit nightly` returned: 1 when a stage failed."""
        if not self.done:
            return None
        return 1 if self.failed else 0

    def alive(self) -> bool:
        """Not finished and its process still running. An old start event's
        pid is not trusted (it may have been reused by another process)."""
        return (
            not self.done
            and background._pid_alive(self.pid)
            and background._age_hours(self.started) < background.PID_TRUST_HOURS
        )


def latest_nightly(conn: sqlite3.Connection) -> RunRecord | None:
    """The newest nightly run, finished or not; None when none is recorded."""
    row = conn.execute(q.LATEST_NIGHTLY_RUN).fetchone()
    if row is None:
        return None
    failed: tuple[str, ...] = ()
    if row["done_detail"]:
        failed = tuple(str(s) for s in json.loads(row["done_detail"]).get("failed") or ())
    return RunRecord(
        run=str(row["run"]),
        pid=int(row["pid"] or 0),
        started=str(row["started"]),
        finished=str(row["finished"]) if row["finished"] else None,
        failed=failed,
    )


def last_successful_nightly(conn: sqlite3.Connection) -> tuple[str, float] | None:
    """(finished at UTC, hours ago) of the newest nightly run whose every
    stage was ok, or None."""
    row = conn.execute(q.LAST_SUCCESSFUL_NIGHTLY).fetchone()
    if row is None or row["at"] is None:
        return None
    return str(row["at"]), float(row["hours"])


def stage_rows(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    """The stage outcomes recorded for one run, in order."""
    return list(conn.execute(q.RUN_STAGE_OUTCOMES, {"run": run_id}).fetchall())
