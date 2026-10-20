"""A stored cover letter the claim judge never read is not final (review
2026-10-10). The claim judge (validate/claim_judge.py) reads every letter
tailoring writes from the day it shipped; letters tailored before then, or
whose record of the judge is missing, were only ever checked by the rules.
Such a letter is never attached or staged: staging (autofill/browser.py
stage_job, every path: `jobkit stage`, MCP stage_forms/restage/open_queue)
halts the job with reason REASON before the browser touches anything, and
the nightly run (pipeline/nightly.py) does not stage it at all.

A letter counts as judged (final) when either:

* its provenance.json, in the letter's own folder and for its job, names the
  claim judge prompt it was drafted under (`claim_judge_prompt`) and its
  fact check passed (`cover_fact_check.ok`): the judge read it when it was
  drafted, under any judge version; or
* `jobkit rejudge` (MCP rejudge) passed this very letter after its
  application last changed (a `claims_rejudged` event with letter_ok).

Anything else (no provenance.json, one without the judge, a fact check that
did not pass, a provenance.json of another folder or job) is not judged. Such
a letter is marked once with a `cover_needs_retailor` event (detail: the
cover path and why) and listed for the human with what to do: re-tailor the
job (MCP retailor, `jobkit tailor --job-id <id>`), which drafts and judges a
new letter, or have `jobkit rejudge` check the stored one. The list reaches
the human through pipeline/halts.py (the digest's Staging section, MCP
get_queue, schedule status) and the nightly stage's summary. Nothing here
changes the schema or touches the letter's files."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from jobkit import db as q
from jobkit.log import log

EVENT = q.COVER_NEEDS_RETAILOR_EVENT
# the staging halt reason (autofill_runs.abort_reason) and the halts.py key
REASON = "cover-letter-needs-retailoring"
MEANING = (
    "its cover letter was never read by the claim judge (tailored before the judge "
    "existed, or no record of it), so it is not attached or staged; re-tailor it (MCP "
    "retailor, `jobkit tailor --job-id <id>`) or have `jobkit rejudge` check the letter"
)


@dataclass(frozen=True)
class Pending:
    """A stageable job whose letter is not judged."""

    job_id: int
    company: str
    title: str
    apply_url: str
    cover_path: str
    why: str
    since: str  # the application's updated_at: when it was last tailored
    flagged_at: str | None  # when the cover_needs_retailor event was recorded


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def why_not_final(conn: sqlite3.Connection, app: sqlite3.Row | Mapping[str, Any]) -> str | None:
    """None when the application's letter may be attached as final (it has
    no letter, or the claim judge passed this letter: module docstring),
    otherwise why not. `app` carries job_id, cover_path, provenance_path and
    updated_at (db.APPLICATION_LETTER_FOR_JOB, db.STAGEABLE_LETTERS)."""
    if not app["cover_path"]:
        return None
    cover = Path(str(app["cover_path"]))
    prov_path = (
        Path(str(app["provenance_path"]))
        if app["provenance_path"]
        else cover.parent / "provenance.json"
    )
    prov = _read_json(prov_path)
    own = (
        prov is not None
        and prov_path.parent == cover.parent
        and str(prov.get("job_id", app["job_id"])) == str(app["job_id"])
    )
    judge = str((prov or {}).get("claim_judge_prompt") or "").strip()
    check = (prov or {}).get("cover_fact_check")
    fact_ok = isinstance(check, dict) and check.get("ok") is True
    if own and judge and fact_ok:
        return None
    rejudged = conn.execute(
        q.LETTER_PASSED_ANY_REJUDGE,
        {"job_id": app["job_id"], "letter": str(app["cover_path"]), "since": app["updated_at"]},
    ).fetchone()
    if rejudged is not None:
        return None
    if prov is None:
        return f"{prov_path} is missing or unreadable: no record that the claim judge read it"
    if not own:
        return f"{prov_path} is not this letter's own record (another folder or job)"
    if not judge:
        return "tailored before the claim judge read cover letters"
    return "its fact check did not pass"


def check_job(conn: sqlite3.Connection, job_id: int) -> tuple[str, sqlite3.Row] | None:
    """(why, application) when the job's newest application holds a letter
    that is not judged, else None."""
    app = conn.execute(q.APPLICATION_LETTER_FOR_JOB, {"job_id": job_id}).fetchone()
    if app is None:
        return None
    why = why_not_final(conn, app)
    return None if why is None else (why, app)


def _flagged_at(conn: sqlite3.Connection, app: sqlite3.Row | Mapping[str, Any]) -> str | None:
    row = conn.execute(
        q.COVER_NEEDS_RETAILOR_AT,
        {"job_id": app["job_id"], "cover": str(app["cover_path"]), "since": app["updated_at"]},
    ).fetchone()
    return None if row is None else str(row["at"])


def flag(conn: sqlite3.Connection, app: sqlite3.Row | Mapping[str, Any], why: str) -> bool:
    """Record the cover_needs_retailor event for this letter, once since its
    application last changed. True when it was recorded now."""
    if _flagged_at(conn, app) is not None:
        return False
    conn.execute(
        q.RECORD_EVENT,
        {
            "job_id": app["job_id"],
            "kind": EVENT,
            "detail": json.dumps({"cover": str(app["cover_path"]), "why": why}),
        },
    )
    conn.commit()
    log("letter_gate.needs_retailor", job_id=app["job_id"], cover=str(app["cover_path"]), why=why)
    return True


def pending(conn: sqlite3.Connection) -> list[Pending]:
    """Every stageable letter that is not judged (db.STAGEABLE_LETTERS),
    by job id. Read-only."""
    out: list[Pending] = []
    for r in conn.execute(q.STAGEABLE_LETTERS).fetchall():
        why = why_not_final(conn, r)
        if why is None:
            continue
        out.append(
            Pending(
                job_id=int(r["job_id"]),
                company=str(r["company_name"] or "?"),
                title=str(r["title"] or "?"),
                apply_url=str(r["apply_url"] or ""),
                cover_path=str(r["cover_path"]),
                why=why,
                since=str(r["updated_at"] or ""),
                flagged_at=_flagged_at(conn, r),
            )
        )
    return out


def sweep(conn: sqlite3.Connection) -> list[Pending]:
    """pending(), with each letter marked (flag) on the way: the nightly
    staging stage and the digest call it."""
    out: list[Pending] = []
    for item in pending(conn):
        app = {"job_id": item.job_id, "cover_path": item.cover_path, "updated_at": item.since}
        recorded = flag(conn, app, item.why)
        out.append(replace(item, flagged_at=_flagged_at(conn, app)) if recorded else item)
    return out
