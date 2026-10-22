"""`jobkit same-day`: score the jobs found from job-alert emails today, so a
job that clears the person's bar is approved the same day instead of after
the next nightly run.

Two kinds of job come here, each the employer's own ATS record (CLAUDE.md
rule 10):

* a job the board resolver ingested for a job-alert card (the employer's
  posting on its Greenhouse, Lever or Ashby board; track/job_alerts.py).
  MCP finish_email_check starts `jobkit same-day --resolve-boards`, which
  runs board lookup first and then takes every such job not scored yet
  (db.SAME_DAY_ALERT_JOBS);
* a job Claude found by web search (track/alert_search.py): MCP
  record_search_result ingests it and starts `jobkit same-day <job id>`.

Each job goes through the nightly run's own stages, limited to these job
ids: filter, extract, the remote and comp checks, score, the queue gate.
Then the form-spec stage (friction feeds the standing rules, as in the
nightly run; it takes queued jobs without a form spec, up to
FORM_SPEC_LIMIT) and the standing rules (config/autoapprove.yaml, with the
person's optional daily limit). A job that clears an auto_approve rule is
approved here; tailoring and staging it is the next step (MCP approve's
background tailoring, or the nightly run). Nothing is submitted.

This runs only in a background launch (background.spawn), never in the MCP
request path: extraction, the checks and scoring call the LLM. Every stage
runs on its own (an exception is logged, its writes rolled back, and the
next stage still runs). The run is recorded as a `same_day.done` event, and
the last line of its output is a one-line summary, which MCP
background_status shows."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from jobkit import db as q
from jobkit.log import log

COMMAND = "same-day"
RESOLVE_FLAG = "--resolve-boards"
DONE_EVENT = "same_day.done"
# alert-card jobs ingested this recently are picked up by a run without ids
CANDIDATE_WINDOW = "-2 days"
# the nightly run's own limit for the form-spec stage
FORM_SPEC_LIMIT = 50

StageFn = Callable[[sqlite3.Connection], object]


def alert_card_jobs(conn: sqlite3.Connection) -> list[int]:
    """Jobs the board resolver ingested for a job-alert card recently that
    are not scored yet (new or filtered, open)."""
    return [
        int(r["job_id"]) for r in conn.execute(q.SAME_DAY_ALERT_JOBS, {"window": CANDIDATE_WINDOW})
    ]


def stages(job_ids: Sequence[int]) -> list[tuple[str, StageFn]]:
    """The nightly run's filter-to-rules stages, limited to these jobs (the
    form-spec and rules stages read the whole queue, as they do at night)."""
    from jobkit.autofill.form_spec import capture_form_specs
    from jobkit.pipeline.filter import run_filters
    from jobkit.pipeline.triage import (
        run_comp_check,
        run_extract,
        run_queue,
        run_remote_check,
        run_score,
    )
    from jobkit.rules import run_standing_rules

    ids = list(job_ids)

    def form_specs(c: sqlite3.Connection) -> object:
        queued = [j for j in ids if (_state(c, j) or {}).get("status") in ("queued", "approved")]
        if not queued:
            return "no job of this run is queued: skipped"
        return capture_form_specs(c, FORM_SPEC_LIMIT)

    return [
        ("filter", lambda c: run_filters(c, job_ids=ids)),
        ("extract", lambda c: run_extract(c, job_ids=ids)),
        ("remote", lambda c: run_remote_check(c, job_ids=ids)),
        ("comp", lambda c: run_comp_check(c, job_ids=ids)),
        ("score", lambda c: run_score(c, job_ids=ids)),
        ("queue", lambda c: run_queue(c, job_ids=ids, require_sanity=True)),
        ("form-specs", form_specs),
        ("rules", run_standing_rules),
    ]


def _state(conn: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
    row = conn.execute(q.SAME_DAY_JOB_STATE, {"job_id": job_id}).fetchone()
    return dict(row) if row is not None else None


@dataclass(frozen=True)
class JobResult:
    job_id: int
    company: str
    title: str
    status: str
    fit_score: int | None
    auto: bool  # the latest decision on it was a standing rule's
    rejected_by: str

    @property
    def outcome(self) -> str:
        """approved | review | rejected | not_scored | <status>"""
        if self.status == "approved":
            return "approved"
        if self.status == "queued":
            return "review"
        if self.status == "rejected":
            return "rejected"
        if self.status in ("new", "filtered") and self.fit_score is None:
            return "not_scored"
        return self.status

    def line(self) -> str:
        head = f"job {self.job_id} {self.company} — {self.title}"
        fit = f"fit {self.fit_score}" if self.fit_score is not None else "no score"
        what = self.outcome
        if what == "approved":
            how = "approved automatically" if self.auto else "approved"
            return f"{head}: {how} ({fit}); tailoring and form filling come next"
        if what == "review":
            return f"{head}: in your review queue ({fit})"
        if what == "rejected":
            return f"{head}: rejected ({self.rejected_by or 'see logs/jobkit.jsonl'})"
        if what == "not_scored":
            return f"{head}: not scored (a stage failed or the description is missing)"
        return f"{head}: status {self.status} ({fit})"


@dataclass
class SameDayReport:
    job_ids: list[int]
    resolve_lines: list[str] = field(default_factory=list)
    stage_lines: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    jobs: list[JobResult] = field(default_factory=list)

    def summary(self) -> str:
        if not self.job_ids:
            return "same-day: no job from job-alert emails or web search waits for scoring"
        counts: dict[str, int] = {}
        for j in self.jobs:
            counts[j.outcome] = counts.get(j.outcome, 0) + 1
        names = {
            "approved": "approved",
            "review": "in your review queue",
            "rejected": "rejected",
            "not_scored": "not scored",
        }
        parts = [f"{n} {names.get(k, k)}" for k, n in sorted(counts.items())]
        text = f"same-day: {len(self.job_ids)} job(s): {', '.join(parts)}"
        if self.failed:
            text += f"; failed stage(s): {', '.join(self.failed)}"
        return text

    def render(self) -> str:
        return "\n".join(
            [*self.resolve_lines, *self.stage_lines, *(j.line() for j in self.jobs), self.summary()]
        )


def run(
    conn: sqlite3.Connection,
    job_ids: Sequence[int] = (),
    *,
    resolve_boards: bool = False,
    resolver: Callable[[sqlite3.Connection], Any] | None = None,
    stage_list: Callable[[Sequence[int]], list[tuple[str, StageFn]]] | None = None,
    echo: Callable[[str], None] | None = None,
) -> SameDayReport:
    """Board lookup first (resolve_boards), then the stages for `job_ids`
    plus, with resolve_boards or with no ids, every recent alert-card job
    not scored yet. Never raises for a stage: a failed one is reported
    (SameDayReport.failed)."""
    say = echo or (lambda _line: None)
    rep = SameDayReport(job_ids=[])
    if resolve_boards:
        if resolver is None:
            from jobkit.discovery import board_resolver

            resolver = board_resolver.resolve
        try:
            out = resolver(conn)
            text = out.render() if hasattr(out, "render") else str(out)
            rep.resolve_lines = [ln for ln in str(text).splitlines() if ln.strip()]
        except Exception as e:  # noqa: BLE001 — scoring still runs for what is there
            with contextlib.suppress(sqlite3.Error):
                conn.rollback()
            rep.resolve_lines = [f"board lookup: FAILED — {type(e).__name__}: {e}"[:300]]
            rep.failed.append("resolve-boards")
        for ln in rep.resolve_lines:
            say(ln)
    # a launch for a search result's job leaves the alert-card jobs to the
    # board-lookup launch, so two launches never score the same job
    pending = alert_card_jobs(conn) if resolve_boards or not job_ids else []
    ids: list[int] = []
    for j in [*job_ids, *pending]:
        if int(j) not in ids:
            ids.append(int(j))
    rep.job_ids = ids
    if ids:
        for name, fn in (stage_list or stages)(ids):
            try:
                result = str(fn(conn))[:200]
                line = f"{name}: {result}"
            except Exception as e:  # noqa: BLE001 — one stage must not stop the rest
                with contextlib.suppress(sqlite3.Error):
                    conn.rollback()
                rep.failed.append(name)
                line = f"{name}: FAILED — {type(e).__name__}: {e}"[:300]
                log("same_day.stage_failed", stage=name, error=str(e)[:300], job_ids=ids)
            rep.stage_lines.append(line)
            say(line)
        for j in ids:
            st = _state(conn, j)
            if st is None:
                continue
            rep.jobs.append(
                JobResult(
                    job_id=j,
                    company=str(st["company_name"] or "?"),
                    title=str(st["title"] or "?"),
                    status=str(st["status"]),
                    fit_score=None if st["fit_score"] is None else int(st["fit_score"]),
                    auto=bool(st["auto_decided"]),
                    rejected_by=str(st["rejected_by"] or ""),
                )
            )
    detail = {
        "job_ids": ids,
        "failed": rep.failed,
        "jobs": {str(j.job_id): j.outcome for j in rep.jobs},
    }
    conn.execute(q.RECORD_EVENT, {"job_id": None, "kind": DONE_EVENT, "detail": json.dumps(detail)})
    conn.commit()
    log("same_day.done", **detail)
    for res in rep.jobs:
        say(res.line())
    say(rep.summary())
    return rep


# ---- starting it in the background (MCP finish_email_check, record_search_result) ----


def board_lookup_running(conn: sqlite3.Connection) -> Any:
    """The running launch that does board lookup for alert cards (`jobkit
    resolve-boards`, `jobkit same-day --resolve-boards`, a nightly or a
    run-all started from the MCP server), or None."""
    from jobkit import background

    for s in background.statuses(conn, limit=25):
        if not s.running or not s.args:
            continue
        if s.args[0] in ("resolve-boards", "nightly", "run-all"):
            return s
        if s.args[0] == COMMAND and RESOLVE_FLAG in s.args:
            return s
    return None


def start(
    conn: sqlite3.Connection, job_ids: Sequence[int] = (), *, resolve_boards: bool = False
) -> str:
    """Start `jobkit same-day [--resolve-boards] [ids]` in the background and
    say so in one line. Not started while a nightly run is in progress (it
    does the same stages), nor, with resolve_boards, while board lookup is
    already running."""
    from jobkit import background
    from jobkit.pipeline import nightly

    night = nightly.in_progress(conn)
    if night is not None:
        return (
            f"same-day scoring not started: the nightly run in progress (run {night.run}) "
            "scores new jobs; schedule_status shows it."
        )
    if resolve_boards:
        busy = board_lookup_running(conn)
        if busy is not None:
            return (
                f"same-day scoring not started: `jobkit {' '.join(busy.args)}` is already "
                f"running (launch #{busy.event_id}); the nightly run scores anything it misses."
            )
    args = [COMMAND, *([RESOLVE_FLAG] if resolve_boards else []), *(str(j) for j in job_ids)]
    try:
        launch = background.spawn(
            conn, *args, job_id=int(job_ids[0]) if len(job_ids) == 1 else None
        )
    except RuntimeError as e:
        return f"same-day scoring not started ({e}); the nightly run scores these jobs."
    return (
        f"same-day scoring: {launch.describe()}. background_status shows when it is done; "
        "its last line says which jobs were approved."
    )
