"""Stage 12 — follow-up drafts. Generated ahead of the due date; SENDING IS
ALWAYS MANUAL (SPEC stage 12). +30d close_as_ghosted auto-closes.

The schedule is `followups:` in config/filters.yaml (one knob, one owner):
days after submit, kind, and `only_if_contact_known`. A kind with that flag
(hiring_manager_note) is scheduled only when a contact is stored for the
company, and a pending one whose company still has no contact is skipped
rather than drafted (audit 2026-10-08 §2 item 8)."""

from __future__ import annotations

import datetime
import json
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass

import yaml

from jobkit import db as q
from jobkit import llm
from jobkit.config import Filters, load_filters
from jobkit.log import log

# kinds the pipeline drafts for the human to send; close_as_ghosted is bookkeeping
DRAFTED_KINDS = ("status_check", "hiring_manager_note")
GHOST_KIND = "close_as_ghosted"
NO_CONTACT_REASON = "no-contact-known"


@dataclass(frozen=True)
class FollowupSpec:
    days_after_submit: int
    kind: str
    only_if_contact_known: bool = False


# SPEC §9 defaults, used only when filters.yaml has no `followups:` block
DEFAULT_SCHEDULE = (
    FollowupSpec(7, "status_check"),
    FollowupSpec(14, "hiring_manager_note", only_if_contact_known=True),
    FollowupSpec(30, GHOST_KIND),
)


def followup_schedule(filters: Filters | None = None) -> tuple[FollowupSpec, ...]:
    """The follow-up schedule from filters.yaml `followups:`. A malformed entry
    raises (fail at use, loudly), never silently drops a follow-up."""
    raw = (filters or load_filters()).raw.get("followups")
    if raw is None:
        return DEFAULT_SCHEDULE
    if not isinstance(raw, list):
        raise ValueError("filters.yaml followups: must be a list")
    out: list[FollowupSpec] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict) or "kind" not in entry or "days_after_submit" not in entry:
            raise ValueError(f"filters.yaml followups[{i}] needs kind and days_after_submit")
        out.append(
            FollowupSpec(
                days_after_submit=int(entry["days_after_submit"]),
                kind=str(entry["kind"]),
                only_if_contact_known=bool(entry.get("only_if_contact_known", False)),
            )
        )
    return tuple(out)


def _contact_gated_kinds(schedule: tuple[FollowupSpec, ...]) -> set[str]:
    return {s.kind for s in schedule if s.only_if_contact_known}


def company_has_contact(conn: sqlite3.Connection, company_id: int | None) -> bool:
    if company_id is None:
        return False
    return conn.execute(q.COMPANY_HAS_CONTACT, {"company_id": company_id}).fetchone() is not None


@dataclass
class Scheduled:
    due: list[tuple[str, str]]  # (kind, due date)
    skipped: list[tuple[str, str]]  # (kind, reason)

    def describe(self) -> str:
        parts = [f"{kind} {due}" for kind, due in self.due]
        parts += [f"{kind} not scheduled ({why})" for kind, why in self.skipped]
        return "; ".join(parts) or "nothing scheduled"


def schedule_followups(
    conn: sqlite3.Connection,
    application_id: int,
    company_id: int | None,
    submitted_on: datetime.date,
    filters: Filters | None = None,
    *,
    skip_kinds: Collection[str] = (),
) -> Scheduled:
    """Insert the follow-up rows for one submitted application, dated from the
    submission day. A contact-gated kind is skipped when no contact is known.
    `skip_kinds`: kinds the application already has (never scheduled twice)."""
    schedule = followup_schedule(filters)
    has_contact = company_has_contact(conn, company_id)
    out = Scheduled(due=[], skipped=[])
    for spec in schedule:
        if spec.kind in skip_kinds:
            continue
        if spec.only_if_contact_known and not has_contact:
            out.skipped.append((spec.kind, NO_CONTACT_REASON))
            continue
        due = (submitted_on + datetime.timedelta(days=spec.days_after_submit)).isoformat()
        conn.execute(
            q.INSERT_FOLLOWUP, {"application_id": application_id, "kind": spec.kind, "due_at": due}
        )
        out.due.append((spec.kind, due))
    return out


def skip_uncontactable(conn: sqlite3.Connection, filters: Filters | None = None) -> int:
    """Pending contact-gated follow-ups (hiring_manager_note) whose company has
    no stored contact: marked skipped, never drafted. Returns how many."""
    gated = _contact_gated_kinds(followup_schedule(filters))
    skipped = 0
    for r in conn.execute(q.PENDING_FOLLOWUPS_WITH_COMPANY).fetchall():
        if r["kind"] in gated and not company_has_contact(conn, r["company_id"]):
            conn.execute(q.SET_FOLLOWUP_STATUS, {"id": r["id"], "status": "skipped"})
            conn.execute(
                q.RECORD_EVENT,
                {
                    "job_id": r["job_id"],
                    "kind": "followup_skipped",
                    "detail": json.dumps({"followup_id": r["id"], "reason": NO_CONTACT_REASON}),
                },
            )
            skipped += 1
    return skipped


def process_followups(conn: sqlite3.Connection) -> dict[str, int]:
    """Close ghosted applications at their due date, skip contact-gated
    follow-ups with no contact, and draft the human-sent kinds up to 2 days
    ahead of due. One failed draft does not stop the others (it is retried on
    the next run: its draft stays empty)."""
    from jobkit.pipeline import triage  # the sandboxed, root-anchored corpus path
    from jobkit.validate.style import configured_estimate_style

    stats = {"drafted": 0, "ghost_closed": 0, "skipped_no_contact": 0, "failed": 0}

    # auto-close ghosted at +30d (draft-free bookkeeping)
    ghosted = conn.execute(
        """SELECT f.id, f.application_id FROM followups f
           WHERE f.kind='close_as_ghosted' AND f.status='pending'
             AND f.due_at <= date('now')"""
    ).fetchall()
    for g in ghosted:
        conn.execute("UPDATE followups SET status='done' WHERE id=?", (g["id"],))
        conn.execute(
            "UPDATE applications SET stage='ghosted', updated_at=datetime('now') WHERE id=?",
            (g["application_id"],),
        )
        stats["ghost_closed"] += 1

    stats["skipped_no_contact"] = skip_uncontactable(conn)
    conn.commit()

    rows = conn.execute(
        """SELECT f.id, f.kind, j.title, j.company_name FROM followups f
           JOIN applications a ON a.id = f.application_id
           JOIN jobs j ON j.id = a.job_id
           WHERE f.status='pending' AND f.draft IS NULL
             AND f.kind IN ('status_check','hiring_manager_note')
             AND f.due_at <= date('now', '+2 days')"""
    ).fetchall()
    if rows:
        # draft the human-sent kinds up to 2 days ahead of due
        with open(triage.CORPUS_PATH) as fh:
            corpus = yaml.safe_load(fh)
        bullets = "\n".join(f"- {b['text_short'].strip()}" for b in corpus["bullets"][:12])
        name = str((corpus.get("identity") or {}).get("name") or "").strip()
        style = configured_estimate_style()
        prompt = llm.PromptFile("followup")
        for r in rows:
            try:
                data = llm.call_json(
                    conn,
                    "cover",
                    prompt.render(
                        kind=r["kind"],
                        title=r["title"],
                        company=r["company_name"],
                        bullets=bullets,
                        name=name,
                        estimate_style=style,
                    ),
                    cache_key=f"followup:{r['id']}:{prompt.version}",
                )
            except Exception as e:  # noqa: BLE001 — isolate per-follow-up failures
                log("followups.draft_failed", followup_id=r["id"], error=str(e)[:200])
                stats["failed"] += 1
                continue
            conn.execute(
                "UPDATE followups SET draft=? WHERE id=?",
                (
                    json.dumps({"subject": data.get("subject", ""), "body": data.get("body", "")}),
                    r["id"],
                ),
            )
            conn.commit()
            stats["drafted"] += 1
    conn.commit()
    log("followups.done", **stats)
    return stats
