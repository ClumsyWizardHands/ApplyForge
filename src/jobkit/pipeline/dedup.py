"""ATS-over-feed merge (hard rule 10; audit 2026-10-08 §2 item 8).

A posting can reach the DB from an aggregator feed first (a row with no board)
and from its employer's ATS board later. feeds.py dedups only in the other
order (ATS row first, feed copy second), so the later ATS row used to become a
second live row for the same requisition.

Here the ATS row wins. An open feed row whose apply URL names the same ATS
posting — (ats, board token, job id), read with the same resolver `add-url`
uses, or a Greenhouse `gh_jid` link — is merged into the ATS row:

* the feed row's sources move to the ATS row as non-canonical provenance;
* a human's work on the feed row (an application, a human decision) moves to
  the ATS row, which takes the feed row's human-gate status (approved,
  applied, rejected, snoozed) — but only when the ATS row has no application
  and no decision of its own. When both rows carry state, nothing is merged:
  the pair is reported (`merge_held` event) for the human, because a merge
  must never choose between two human records;
* the feed row is closed (status `closed`), never deleted: its scores, form
  spec and artifacts stay as history;
* a `merged_into` event on the feed row and a `merged_from` event on the ATS
  row record the pair.

poll.upsert_jobs runs this for every ATS posting it stores, so it holds for
future polls; `jobkit merge-feed-dupes` (MCP `merge_feed_dupes`) runs it once
over the rows already in the DB."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from jobkit import db as q
from jobkit.discovery.url_resolver import resolve
from jobkit.log import log

_GH_JID = re.compile(r"[?&]gh_jid=(\d+)")

# statuses that record a human gate decision; anything else is pipeline state
# the ATS row recomputes from its own (real) JD
CARRIED_STATUSES = ("approved", "applied", "rejected", "snoozed")

PostingKey = tuple[str, str, str]  # (ats, board token or "" if unknown, external id), lowercased


def posting_key(url: str | None) -> PostingKey | None:
    """The ATS posting an apply URL points at, or None when the URL does not
    name one (an aggregator's own page, a board without a job id)."""
    if not url:
        return None
    hit = resolve(url)
    if hit is not None and hit.supported and hit.external_id:
        return (hit.ats, hit.token.lower(), hit.external_id.lower())
    m = _GH_JID.search(url)
    if m:
        return ("greenhouse", "", m.group(1))
    return None


def feed_rows_for_board(conn: sqlite3.Connection, ats: str, token: str) -> dict[str, list[int]]:
    """Open feed rows whose apply URL names a posting on this board, as
    external id -> feed row ids. One query per board poll."""
    out: dict[str, list[int]] = {}
    tok = token.lower()
    for r in conn.execute(q.OPEN_FEED_ROWS_FOR_BOARD, {"ats": ats, "token": token}):
        key = posting_key(r["apply_url"])
        if key is None or key[0] != ats or key[1] not in ("", tok):
            continue
        out.setdefault(key[2], []).append(int(r["id"]))
    return out


def _event(conn: sqlite3.Connection, job_id: int, kind: str, detail: dict[str, object]) -> None:
    conn.execute(
        q.RECORD_EVENT_ONCE,
        {"job_id": job_id, "kind": kind, "detail": json.dumps(detail, sort_keys=True)},
    )


def merge_feed_row(
    conn: sqlite3.Connection, feed_id: int, ats_id: int, *, dry_run: bool = False
) -> str:
    """Merge one feed row into the ATS row for the same posting. Returns
    'merged', 'held' (both rows carry human or decision state: left alone,
    reported) or 'skipped' (not an open feed row / not an ATS row)."""
    if feed_id == ats_id:
        return "skipped"
    feed = conn.execute(q.JOB_MERGE_STATE, {"job_id": feed_id}).fetchone()
    ats = conn.execute(q.JOB_MERGE_STATE, {"job_id": ats_id}).fetchone()
    if (
        feed is None
        or ats is None
        or feed["board_id"] is not None
        or feed["closed_at"] is not None
        or ats["board_id"] is None
    ):
        return "skipped"
    feed_human = feed["applications"] > 0 or feed["human_decisions"] > 0
    ats_state = ats["applications"] > 0 or ats["decisions"] > 0
    if feed_human and ats_state:
        if not dry_run:
            _event(conn, feed_id, "merge_held", {"ats_job_id": ats_id})
            log("dedup.merge_held", feed_job_id=feed_id, ats_job_id=ats_id)
        return "held"
    if dry_run:
        return "merged"
    pair = {"from_id": feed_id, "to_id": ats_id}
    conn.execute(q.COPY_SOURCES_TO_JOB, pair)
    conn.execute(q.DELETE_SOURCES_FOR_JOB, {"job_id": feed_id})
    carried: str | None = None
    if feed_human:
        conn.execute(q.MOVE_APPLICATIONS, pair)
        conn.execute(q.MOVE_HUMAN_DECISIONS, pair)
        conn.execute(q.MOVE_SNOOZE_EVENTS, pair)
        if feed["status"] in CARRIED_STATUSES:
            carried = str(feed["status"])
            conn.execute(q.SET_JOB_STATUS, {"job_id": ats_id, "status": carried})
    conn.execute(q.CLOSE_MERGED_FEED_ROW, {"job_id": feed_id})
    _event(conn, feed_id, "merged_into", {"ats_job_id": ats_id})
    _event(conn, ats_id, "merged_from", {"feed_job_id": feed_id, "carried_status": carried})
    log("dedup.feed_merged", feed_job_id=feed_id, ats_job_id=ats_id, carried_status=carried)
    return "merged"


def merged_target(conn: sqlite3.Connection, job_id: int) -> int | None:
    """The ATS row a closed feed row was merged into, or None (an open row,
    an ATS row, or a feed row closed without a merge)."""
    row = conn.execute(q.MERGED_INTO_TARGET, {"job_id": job_id}).fetchone()
    return int(row["ats_job_id"]) if row is not None and row["ats_job_id"] else None


def follow_merges(conn: sqlite3.Connection, job_id: int) -> int:
    """The row that carries a job now: a merged feed row's ATS row (an
    aggregator job approved from the queue and merged into the employer's
    posting afterwards), else the job itself. A pinned queue number or a job
    id the human gave before the merge still reaches the application."""
    seen = {job_id}
    current = job_id
    while (target := merged_target(conn, current)) is not None and target not in seen:
        seen.add(target)
        current = target
    return current


@dataclass
class MergeReport:
    dry_run: bool
    examined: int = 0  # open feed rows whose apply URL names an ATS posting
    merged: list[tuple[int, int]] = field(default_factory=list)  # (feed id, ATS id)
    held: list[tuple[int, int]] = field(default_factory=list)

    def render(self) -> str:
        verb = "would merge" if self.dry_run else "merged"
        lines = [
            ("DRY RUN — nothing changed. " if self.dry_run else "")
            + f"open feed rows naming an ATS posting: {self.examined}",
            f"{verb} into their ATS row: {len(self.merged)}"
            + (f" — {', '.join(f'{f}->{a}' for f, a in self.merged[:40])}" if self.merged else ""),
        ]
        if self.merged[40:]:
            lines.append(f"  (+{len(self.merged) - 40} more)")
        if self.held:
            lines.append(
                f"held for you (both rows carry an application or a decision; nothing "
                f"changed): {', '.join(f'{f}/{a}' for f, a in self.held)}. Decide which "
                "row to keep with mark_submitted / reject on the job ids."
            )
        return "\n".join(lines)


def merge_feed_dupes(conn: sqlite3.Connection, *, dry_run: bool = False) -> MergeReport:
    """One-off repair over the rows already stored: every open feed row whose
    apply URL names an ATS posting that has its own row is merged into it.
    Safe to run twice (merged rows are closed and no longer examined)."""
    report = MergeReport(dry_run=dry_run)
    for r in conn.execute(q.OPEN_FEED_ROWS).fetchall():
        key = posting_key(r["apply_url"])
        if key is None:
            continue
        ats_row = conn.execute(
            q.ATS_JOB_FOR_POSTING, {"ats": key[0], "token": key[1], "external_id": key[2]}
        ).fetchone()
        if ats_row is None:
            continue
        report.examined += 1
        feed_id, ats_id = int(r["id"]), int(ats_row["id"])
        outcome = merge_feed_row(conn, feed_id, ats_id, dry_run=dry_run)
        if outcome == "merged":
            report.merged.append((feed_id, ats_id))
        elif outcome == "held":
            report.held.append((feed_id, ats_id))
    if not dry_run:
        conn.commit()
    log(
        "dedup.merge_feed_dupes",
        dry_run=dry_run,
        examined=report.examined,
        merged=len(report.merged),
        held=len(report.held),
    )
    return report
