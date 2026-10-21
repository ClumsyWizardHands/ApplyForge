"""Rescore (package 3, feature request 2026-10-09): when the corpus changes, the
fit scores made against the old one are stale. `jobkit rescore` (MCP rescore,
which runs it in the background) re-scores them and re-applies the gates.

Which jobs (the selection rules):

* open (posting not closed), with a score and extracted requirements;
* status queued or filtered, or rejected ONLY by the score gate: the job's
  newest rejection is the queue gate's `score_below_surface:` and no decision
  (human or standing rule) exists for it. A job rejected by the filter, the
  sanity layer or a standing rule, or by a human, is never revived here;
* a score-gate rejection is a candidate only while the deterministic filters
  still pass it under the current filters.yaml (filter.reject_reason_now):
  evaluation order is filters, then sanity, then standing rules, and putting
  a rejected job back in the queue must not skip the first layer. One that
  fails them now (a stale posting, a blocklisted company, a title or comp the
  config no longer accepts) is not re-scored and stays rejected; the real run
  records that filter-layer rejection (layer filter, the filter's reason), so
  its newest rejection is no longer the score gate's and no later rescore
  picks it up. A dry run lists them and writes nothing;
* approved jobs too, but their status is never touched: their new score is
  reported;
* by default, only stale scores: the newest `score_profile` event
  (triage.score_rows writes one per score) names another corpus hash, or
  another scoring prompt than the active prompt set's (score_fit or
  score_fit_general, triage.score_prompt_name), or, for score_fit_general,
  the same prompt at another version; or there is no event (a score from
  before the record). An event with no prompt name is a score_fit score
  (score_fit events never carry one), and a score_fit version is never
  compared, as before prompt sets: a sales score is stale only by corpus or
  by a switch to another prompt set.

Re-gating without re-scoring: a job rejected only by the score gate, whose
track sets its own `min_score` (filters.yaml tracks[].min_score), and whose
current score now meets that track's gate (it was rejected under another
gate) goes back to the queue with its score unchanged, after the same
deterministic-filter check as any revival. One `regated` event per job.

With explicit job ids (`--job-ids`; a confirmed corpus proposal starts that
for the jobs its question listed) those jobs are re-scored whatever their
hash, applied jobs included (score reported, status untouched). Any other
status (snoozed, new, rejected by another layer, closed) is skipped with the
reason.

After scoring: the queue gate (filters.yaml `gates.min_score_to_surface`, or
the job's track `min_score` when it sets one, read through
Filters.min_score_for) is applied to the
gate-eligible jobs — at or above it they are queued, below it a queued or
filtered job is rejected with the same score_below_surface reason run_queue
writes — and then the standing rules run over the queue, as on every
get_queue. One `rescored` event per job (old and new score, status before and
after, profile hash). A job with no usable JD is skipped, never re-scored.

The LLM calls are triage's score stage (Haiku, cached on the full rendered
prompt): this runs from the CLI or in the background, never in the MCP request
path."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from jobkit import db as q
from jobkit import llm
from jobkit.config import Filters, load_filters
from jobkit.log import log
from jobkit.normalize import jd_is_missing
from jobkit.pipeline import triage
from jobkit.pipeline.filter import reject_reason_now
from jobkit.rules import run_standing_rules

GATED = ("queued", "filtered")  # the queue gate applies; status may change
REPORT_ONLY_DEFAULT = ("approved",)  # re-scored when stale; status never changes
REPORT_ONLY_EXPLICIT = ("approved", "applied")  # with explicit job ids
EVENT = "rescored"
REGATE_EVENT = "regated"

# the newest score_profile event of a job (detail: profile_hash,
# prompt_version and, for a prompt other than score_fit, prompt)
SCORE_PROFILE_DETAIL = """
SELECT detail FROM events WHERE job_id = :job_id AND kind = 'score_profile'
ORDER BY id DESC LIMIT 1
"""
LEGACY_SCORE_PROMPT = triage.SALES_SCORE_PROMPT
_GATE_REASON = re.compile(r"<(\d+)$")


@dataclass(frozen=True)
class ScoringPrompt:
    """What a current score is made with: the corpus hash, the active prompt
    set's scoring prompt and its version."""

    profile_hash: str
    name: str
    version: str


def current_scoring(filters: Filters, profile_hash: str | None = None) -> ScoringPrompt:
    name = triage.score_prompt_name(filters)
    return ScoringPrompt(
        profile_hash or triage.current_profile_hash(), name, llm.PromptFile(name).version
    )


def score_is_stale(detail: str | None, now: ScoringPrompt) -> bool:
    """Whether a score_profile event (its JSON detail, None when there is
    none) describes a score made with something other than `now`."""
    if not detail:
        return True
    try:
        d = json.loads(detail)
    except ValueError:
        return True
    if not isinstance(d, dict) or d.get("profile_hash") != now.profile_hash:
        return True
    # an event with no prompt name is a score_fit (sales) score
    if d.get("prompt", LEGACY_SCORE_PROMPT) != now.name:
        return True
    if now.name == LEGACY_SCORE_PROMPT:
        # as before prompt sets: a score_fit.md version bump alone never made
        # a sales score stale (only the corpus does)
        return False
    return str(d.get("prompt_version")) != now.version


def recorded_gate(reason: str | None) -> int | None:
    """The gate a score_below_surface rejection was made under
    ("score_below_surface:58<65" -> 65)."""
    if not reason or not reason.startswith(triage.SCORE_GATE_PREFIX):
        return None
    m = _GATE_REASON.search(reason)
    return int(m.group(1)) if m else None


@dataclass(frozen=True)
class Candidate:
    job_id: int
    company: str
    title: str
    status: str
    old_score: int | None
    gated: bool  # the queue gate may change its status
    stale: bool  # its score predates the current corpus

    def brief(self) -> str:
        how = "status may change" if self.gated else "status unchanged"
        return (
            f"job {self.job_id} {self.company} — {self.title}: score {self.old_score}, "
            f"{self.status} ({how})"
        )


@dataclass(frozen=True)
class Rescored:
    job_id: int
    company: str
    title: str
    old: int | None
    new: int
    before: str
    after: str
    gated: bool

    def render(self) -> str:
        status = (
            f"{self.before} → {self.after}"
            if self.before != self.after
            else f"{self.before}" + ("" if self.gated else " (status never changed here)")
        )
        return f"job {self.job_id} {self.company} — {self.title}: {self.old} → {self.new}; {status}"


@dataclass
class RescoreReport:
    dry_run: bool
    profile_hash: str
    explicit: bool
    min_score: int
    candidates: list[Candidate] = field(default_factory=list)
    # score-gate rejections that meet their track's current gate: (job, gate)
    regated: list[tuple[Candidate, int]] = field(default_factory=list)
    track_gates: dict[str, int] = field(default_factory=dict)
    rescored: list[Rescored] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)
    # score-gate rejections the current filters reject: (job_id, filter reason)
    filter_rejected: list[tuple[int, str]] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    rules: dict[str, int] | None = None

    def render(self) -> str:
        which = (
            "the jobs given"
            if self.explicit
            else "open jobs scored against an older corpus or another scoring prompt"
        )
        lines: list[str] = []
        if self.dry_run:
            lines.append("DRY RUN — no LLM call, nothing changed.")
            lines.append(
                f"would re-score {len(self.candidates)} job(s) ({which}; corpus "
                f"{self.profile_hash}, queue gate {self.min_score}):"
            )
            lines += [f"  {c.brief()}" for c in self.candidates[:40]]
            if len(self.candidates) > 40:
                lines.append(f"  … and {len(self.candidates) - 40} more")
        else:
            lines.append(
                f"re-scored {len(self.rescored)} of {len(self.candidates)} job(s) ({which}; "
                f"corpus {self.profile_hash}, queue gate {self.min_score}):"
            )
            lines += [f"  {r.render()}" for r in self.rescored]
            if self.failed:
                lines.append(f"no usable score (left as they were): jobs {self.failed}")
            if self.rules is not None:
                lines.append(
                    f"standing rules: evaluated={self.rules.get('evaluated', 0)} "
                    f"auto_approved={self.rules.get('auto_approved', 0)} "
                    f"auto_rejected={self.rules.get('auto_rejected', 0)}"
                )
        if self.track_gates:
            lines.append(
                "track gates: " + ", ".join(f"{t} {g}" for t, g in sorted(self.track_gates.items()))
            )
        if self.regated:
            lines.append(
                ("would return" if self.dry_run else "returned")
                + " to the queue without re-scoring (their track's gate changed):"
            )
            lines += [f"  {c.brief()} meets gate {g}" for c, g in self.regated]
        if self.filter_rejected:
            lines.append(
                "stay rejected: rejected by the score gate, but the current filters reject them "
                + (
                    "(the real run records a filter rejection; not re-scored):"
                    if self.dry_run
                    else "(filter rejection recorded; not re-scored):"
                )
            )
            lines += [f"  job {j}: {why}" for j, why in self.filter_rejected]
        if self.skipped:
            lines.append("skipped:")
            lines += [f"  job {j}: {why}" for j, why in self.skipped]
        lines.append(
            "Approved and applied jobs keep their status; jobs rejected by the filter, the "
            "sanity layer, a standing rule or you are never revived, and a score-gate "
            "rejection is revived only if the current filters pass it."
        )
        return "\n".join(lines)


def _gate_eligible(r: sqlite3.Row) -> bool:
    if r["status"] in GATED:
        return True
    return (
        r["status"] == "rejected"
        and str(r["last_rejection"] or "").startswith(triage.SCORE_GATE_PREFIX)
        and not r["decided"]
    )


def _skip_reason(conn: sqlite3.Connection, job_id: int) -> str:
    row = conn.execute(q.JOB_STATUS, {"job_id": job_id}).fetchone()
    if row is None:
        return "no such job"
    if row["closed_at"]:
        return "posting closed"
    if row["status"] == "rejected":
        return "rejected by another layer or a decision, not only by the score gate"
    if conn.execute("SELECT 1 FROM scores WHERE job_id = ?", (job_id,)).fetchone() is None:
        return f"not scored yet ({row['status']}); triage scores it"
    return f"status {row['status']} is not re-scored"


@dataclass
class Selection:
    rows: list[sqlite3.Row]  # to score
    candidates: list[Candidate]  # one per row
    skipped: list[tuple[int, str]]  # (job_id, why)
    filter_rejected: list[tuple[int, str]]  # score-gate rejections the filters reject now
    # score-gate rejections, not stale, that meet their track's current gate
    regate: list[tuple[Candidate, int]] = field(default_factory=list)


def select(
    conn: sqlite3.Connection,
    job_ids: list[int] | None = None,
    profile_hash: str | None = None,
    filters: Filters | None = None,
) -> Selection:
    """The jobs to re-score under the selection rules above, the ones skipped
    with the reason, the score-gate rejections the current filters reject
    (never re-scored) and the ones to re-gate. Read-only."""
    filters = filters or load_filters()
    now = current_scoring(filters, profile_hash)
    wanted = set(job_ids or [])
    sel = Selection(rows=[], candidates=[], skipped=[], filter_rejected=[])
    seen: set[int] = set()
    for r in conn.execute(q.RESCORE_CANDIDATES).fetchall():
        jid = int(r["id"])
        if wanted and jid not in wanted:
            continue
        seen.add(jid)
        gated = _gate_eligible(r)
        report_only = REPORT_ONLY_EXPLICIT if wanted else REPORT_ONLY_DEFAULT
        if not gated and r["status"] not in report_only:
            if wanted:
                sel.skipped.append((jid, _skip_reason(conn, jid)))
            continue
        detail = conn.execute(SCORE_PROFILE_DETAIL, {"job_id": jid}).fetchone()
        stale = score_is_stale(detail["detail"] if detail else None, now)
        regate_to = None if (wanted or stale) else _regate_gate(r, gated, filters)
        if not wanted and not stale and regate_to is None:
            continue
        if r["status"] == "rejected":
            # a revival is a new way into the queue: the deterministic filters
            # come first (evaluation order), under today's filters.yaml
            verdict = reject_reason_now(dict(r), filters)
            if verdict:
                sel.filter_rejected.append((jid, verdict))
                continue
        cand = Candidate(
            job_id=jid,
            company=str(r["company_name"] or ""),
            title=str(r["title"] or ""),
            status=str(r["status"]),
            old_score=r["old_score"],
            gated=gated,
            stale=stale,
        )
        if regate_to is not None:
            sel.regate.append((cand, regate_to))
            continue
        if jd_is_missing(r["jd_html"]):
            sel.skipped.append((jid, "description too short to score (jd_missing)"))
            continue
        sel.rows.append(r)
        sel.candidates.append(cand)
    for jid in sorted(wanted - seen):
        sel.skipped.append((jid, _skip_reason(conn, jid)))
    return sel


def _regate_gate(r: sqlite3.Row, gated: bool, filters: Filters) -> int | None:
    """The track gate a score-gate rejection now meets, or None. Only for a
    job whose track sets its own min_score, rejected under another gate, with
    a score at or above the track's gate."""
    if not gated or r["status"] != "rejected" or r["old_score"] is None:
        return None
    track = filters.track_of(str(r["title"] or ""))
    if track is None or track.min_score is None:
        return None
    was = recorded_gate(r["last_rejection"])
    if was is None or was == track.min_score or int(r["old_score"]) < track.min_score:
        return None
    return track.min_score


def rescore(
    conn: sqlite3.Connection,
    job_ids: list[int] | None = None,
    *,
    dry_run: bool = False,
    filters: Filters | None = None,
) -> RescoreReport:
    """Re-score the jobs the selection rules (module docstring) pick, or the
    given job_ids; then re-apply the queue gate to the gate-eligible ones and
    run the standing rules. A score-gate rejection the current filters reject
    is not re-scored: it stays rejected and a filter-layer rejection is
    recorded for it. dry_run lists the jobs, makes no LLM call and writes
    nothing."""
    filters = filters or load_filters()
    min_score = filters.min_score_to_surface
    phash = triage.current_profile_hash()
    sel = select(conn, job_ids, phash, filters)
    rows, cands = sel.rows, sel.candidates
    report = RescoreReport(
        dry_run=dry_run,
        profile_hash=phash,
        explicit=bool(job_ids),
        min_score=min_score,
        candidates=cands,
        regated=sel.regate,
        track_gates={t.id: t.min_score for t in filters.tracks if t.min_score is not None},
        skipped=sel.skipped,
        filter_rejected=sel.filter_rejected,
    )
    if dry_run:
        log(
            "rescore.dry_run",
            candidates=len(cands),
            regate=len(sel.regate),
            skipped=len(sel.skipped),
            filter_rejected=len(sel.filter_rejected),
        )
        return report
    for jid, reason in sel.filter_rejected:
        # status is already rejected; the rejection log now names the layer
        # that decides today, so no later rescore treats it as score-gated
        conn.execute(q.RECORD_FILTER_REJECTION, {"job_id": jid, "reason": reason})
        log("rescore.filter_rejected", job_id=jid, reason=reason)
    for c, gate in sel.regate:
        conn.execute(q.SET_JOB_STATUS, {"job_id": c.job_id, "status": "queued"})
        conn.execute(
            q.RECORD_EVENT,
            {
                "job_id": c.job_id,
                "kind": REGATE_EVENT,
                "detail": json.dumps({"score": c.old_score, "gate": gate}),
            },
        )
        log("rescore.regated", job_id=c.job_id, score=c.old_score, gate=gate)
    conn.commit()
    if not rows:
        if sel.regate:
            report.rules = run_standing_rules(conn)
        log(
            "rescore.done",
            rescored=0,
            candidates=0,
            regated=len(sel.regate),
            filter_rejected=len(sel.filter_rejected),
        )
        return report
    scores = triage.score_rows(
        conn, rows, stage_label="rescore", profile_hash=phash, filters=filters
    )
    report.failed = [c.job_id for c in cands if c.job_id not in scores]
    for c in cands:
        if c.job_id not in scores or not c.gated:
            continue
        new = scores[c.job_id]
        gate = filters.min_score_for_title(c.title)
        if new >= gate:
            conn.execute(q.SET_JOB_STATUS, {"job_id": c.job_id, "status": "queued"})
        elif c.status in GATED:
            conn.execute(q.SET_JOB_STATUS, {"job_id": c.job_id, "status": "rejected"})
            conn.execute(
                q.RECORD_SCORE_GATE_REJECTION,
                {"job_id": c.job_id, "reason": triage.score_gate_reason(new, gate)},
            )
    conn.commit()
    if sel.regate or any(c.gated for c in cands if c.job_id in scores):
        report.rules = run_standing_rules(conn)
    for c in cands:
        if c.job_id not in scores:
            continue
        row = conn.execute(q.JOB_STATUS, {"job_id": c.job_id}).fetchone()
        after = str(row["status"]) if row else c.status
        r = Rescored(
            job_id=c.job_id,
            company=c.company,
            title=c.title,
            old=c.old_score,
            new=scores[c.job_id],
            before=c.status,
            after=after,
            gated=c.gated,
        )
        report.rescored.append(r)
        conn.execute(
            q.RECORD_EVENT,
            {
                "job_id": c.job_id,
                "kind": EVENT,
                "detail": json.dumps(
                    {
                        "old": r.old,
                        "new": r.new,
                        "status_before": r.before,
                        "status_after": r.after,
                        "profile_hash": phash,
                        "explicit": bool(job_ids),
                    }
                ),
            },
        )
    conn.commit()
    log(
        "rescore.done",
        rescored=len(report.rescored),
        candidates=len(cands),
        regated=len(sel.regate),
        failed=len(report.failed),
        filter_rejected=len(sel.filter_rejected),
        requeued=sum(1 for r in report.rescored if r.before != "queued" and r.after == "queued"),
        dropped=sum(1 for r in report.rescored if r.before == "queued" and r.after == "rejected"),
    )
    return report


def recent(conn: sqlite3.Connection, limit: int = 10) -> list[str]:
    """The newest rescore results, one line each (for MCP rescore)."""
    out = []
    for r in conn.execute(q.RECENT_RESCORES, {"limit": limit}):
        try:
            d = json.loads(r["detail"] or "{}")
        except ValueError:
            continue
        status = (
            f"{d.get('status_before')} → {d.get('status_after')}"
            if d.get("status_before") != d.get("status_after")
            else str(d.get("status_after"))
        )
        out.append(
            f"[{r['at']}] job {r['job_id']} {r['company_name']} — {r['title']}: "
            f"{d.get('old')} → {d.get('new')}; {status}"
        )
    return out
