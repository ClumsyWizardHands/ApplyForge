"""SQLite source of truth. Raw SQL as named queries — no ORM (SPEC §10)."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from jobkit import paths

# Absolute, from the project root (paths.HOME), never the working directory:
# a process started elsewhere used to create a fresh empty DB here and answer
# every question with zero (audit 2026-10-08 §2 item 7, §3 item 2).
DB_PATH = paths.HOME / "data" / "jobkit.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  normalized_name TEXT NOT NULL UNIQUE,
  domain TEXT,
  tier INTEGER NOT NULL DEFAULT 3,
  blocklisted INTEGER NOT NULL DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS boards (
  id INTEGER PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES companies(id),
  ats TEXT NOT NULL,
  token TEXT NOT NULL,
  etag TEXT,
  status TEXT NOT NULL DEFAULT 'active',      -- active | stale
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  confidence TEXT DEFAULT 'manual',           -- manual | crawl | probe
  verified_at TEXT,
  last_polled_at TEXT,
  UNIQUE(ats, token)
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  board_id INTEGER REFERENCES boards(id),
  company_id INTEGER NOT NULL REFERENCES companies(id),
  external_id TEXT NOT NULL,
  title TEXT NOT NULL,
  normalized_title TEXT NOT NULL,
  company_name TEXT NOT NULL,
  normalized_company TEXT NOT NULL,
  location_raw TEXT,
  location_bucket TEXT NOT NULL,
  cluster_key TEXT NOT NULL,
  remote INTEGER,                              -- 1/0/NULL(unknown)
  comp_min INTEGER, comp_max INTEGER, comp_currency TEXT,
  jd_html TEXT NOT NULL,
  jd_hash TEXT NOT NULL,
  apply_url TEXT NOT NULL,
  posted_at TEXT,
  -- new|filtered|queued|approved|rejected|snoozed|applied|closed
  status TEXT NOT NULL DEFAULT 'new',
  first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
  last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
  closed_at TEXT,
  UNIQUE(board_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_cluster ON jobs(cluster_key);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS sources (
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  source TEXT NOT NULL,
  source_url TEXT,
  is_canonical INTEGER NOT NULL DEFAULT 0,
  seen_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(job_id, source)
);

CREATE TABLE IF NOT EXISTS job_requirements (
  job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
  must_have TEXT, nice_to_have TEXT, implied TEXT, disqualifier TEXT,
  jd_markdown TEXT,
  extracted_at TEXT, prompt_version TEXT
);

CREATE TABLE IF NOT EXISTS sanity (
  job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
  remote_stated TEXT, remote_actual TEXT, remote_conflict INTEGER,
  geo_restriction TEXT, office_days_required INTEGER, commute_minutes INTEGER,
  comp_published INTEGER, comp_required_by_law INTEGER, range_width_pct REAL,
  demand_score INTEGER, comp_percentile INTEGER,
  value_verdict TEXT, value_rationale TEXT,
  flags TEXT, checked_at TEXT, prompt_version TEXT
);

CREATE TABLE IF NOT EXISTS scores (
  job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
  fit_score INTEGER, rationale TEXT, gaps TEXT,
  scored_at TEXT, prompt_version TEXT
);

CREATE TABLE IF NOT EXISTS form_specs (
  job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
  source TEXT, fields TEXT,
  field_count INTEGER, required_count INTEGER,
  novel_questions TEXT, novel_question_count INTEGER,
  drafted_answers TEXT, has_assessment INTEGER,
  friction_score INTEGER, captured_at TEXT
);

CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  -- approved|tailored|staged|submitted|replied|interview|offer|rejected|ghosted
  stage TEXT NOT NULL DEFAULT 'approved',
  resume_path TEXT, cover_path TEXT, gap_report_path TEXT, provenance_path TEXT,
  submitted_at TEXT, updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS autofill_runs (
  id INTEGER PRIMARY KEY,
  job_id INTEGER REFERENCES jobs(id),
  status TEXT, abort_reason TEXT,
  filled_fields TEXT, unfilled_fields TEXT, screenshot_path TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS authenticity_runs (
  id INTEGER PRIMARY KEY,
  job_id INTEGER REFERENCES jobs(id),
  artifact TEXT, verdict TEXT,
  lexical_flags TEXT, structural_flags TEXT, drift_flags TEXT,
  critic_notes TEXT, regeneration_count INTEGER,
  checked_at TEXT, prompt_version TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY,
  job_id INTEGER REFERENCES jobs(id),
  decision TEXT, reason TEXT,
  score_at_decision INTEGER, friction_at_decision INTEGER,
  auto INTEGER NOT NULL DEFAULT 0,
  decided_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY,
  company_id INTEGER REFERENCES companies(id),
  name TEXT, title TEXT, email TEXT, linkedin TEXT, notes TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  job_id INTEGER REFERENCES jobs(id),
  kind TEXT NOT NULL, detail TEXT,
  at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS discovery_queue (
  id INTEGER PRIMARY KEY,
  company_name TEXT NOT NULL,
  normalized_name TEXT NOT NULL UNIQUE,
  source TEXT,                                 -- which feed surfaced it
  first_seen_url TEXT,
  status TEXT NOT NULL DEFAULT 'pending',      -- pending | found | not_found
  attempted_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rejections (
  id INTEGER PRIMARY KEY,
  external_ref TEXT,                           -- ats/token/external_id when no jobs row exists
  job_id INTEGER REFERENCES jobs(id),
  layer TEXT NOT NULL,                         -- filter | sanity | standing_rule
  reason TEXT NOT NULL,
  at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class DatabaseMissing(FileNotFoundError):
    """No database file at the resolved path. Only `jobkit init` creates one."""


def connect(path: Path | None = None, *, create: bool = False) -> sqlite3.Connection:
    """Open the jobkit DB. A missing file is an error unless create=True (only
    `jobkit init` passes it): a wrong project root must fail loudly, not hand
    back an empty database that answers every question with zero."""
    p = path or DB_PATH
    if str(p) != ":memory:" and not p.exists():
        if not create:
            raise DatabaseMissing(
                f"no jobkit database at {p}. Run `jobkit init` to create one there, "
                f"or set JOBKIT_HOME to the directory that holds data/jobkit.db "
                f"(project root in use: {paths.HOME})."
            )
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# How long init_db waits for another process's write lock (ms). A process
# migrating the followups table holds that lock while it copies the whole
# database, which can take longer than sqlite3's default 5 s.
INIT_DB_BUSY_TIMEOUT_MS = 120_000


def init_db(conn: sqlite3.Connection) -> None:
    before = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.execute(f"PRAGMA busy_timeout = {max(int(before), INIT_DB_BUSY_TIMEOUT_MS)}")
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        migrate_followups(conn)
    finally:
        conn.execute(f"PRAGMA busy_timeout = {int(before)}")


# ---- one-time migration: the follow-up notes table goes ----
# Follow-up note drafting was removed. A database made before that still has
# a `followups` table (CREATE TABLE IF NOT EXISTS never dropped it). Its rows
# reference applications(id) with no ON DELETE while foreign keys are on, so
# left in place they would block folding duplicate application rows. Before
# the table is dropped, the whole database is copied to
# <name>.pre-followups.db next to it (never over an existing copy) and every
# row, drafts included, is kept as a 'followups_archived' event.

FOLLOWUPS_TABLE = "followups"
FOLLOWUPS_ARCHIVED_EVENT = "followups_archived"
FOLLOWUPS_BACKUP_TAG = "pre-followups"
# at most this many backup copies; rows found after that are archived as
# events with no further copy (an old open session can add rows repeatedly)
FOLLOWUPS_BACKUP_MAX = 5

TABLE_EXISTS = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"

# every follow-up row with the job it belongs to (NULL when its application
# or job is gone, so an orphan row is archived too)
ARCHIVE_FOLLOWUP_ROWS = """
SELECT f.*, j.id AS archived_job_id
FROM followups f
LEFT JOIN applications a ON a.id = f.application_id
LEFT JOIN jobs j ON j.id = a.job_id
ORDER BY f.id
"""


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(TABLE_EXISTS, {"name": name}).fetchone() is not None


def _main_db_file(conn: sqlite3.Connection) -> Path | None:
    """The file behind the connection's main database; None in memory."""
    for row in conn.execute("PRAGMA database_list").fetchall():
        if row[1] == "main":
            return Path(row[2]) if row[2] else None
    return None


def backup_path_for(db_file: Path, tag: str = FOLLOWUPS_BACKUP_TAG) -> Path:
    """<stem>.<tag>.db next to the database, or <stem>.<tag>-2.db (-3, ...)
    when that name is taken: an existing backup is never overwritten."""
    first = db_file.with_name(f"{db_file.stem}.{tag}.db")
    if not first.exists():
        return first
    n = 2
    while True:
        candidate = db_file.with_name(f"{db_file.stem}.{tag}-{n}.db")
        if not candidate.exists():
            return candidate
        n += 1


def _backup_count(db_file: Path, tag: str = FOLLOWUPS_BACKUP_TAG) -> int:
    """How many <stem>.<tag>[-N].db copies sit next to the database."""
    first = f"{db_file.stem}.{tag}"
    return sum(
        1
        for p in db_file.parent.glob(f"{first}*.db")
        if p.name == f"{first}.db" or re.fullmatch(rf"{re.escape(first)}-\d+\.db", p.name)
    )


def _backup(db_file: Path, target: Path) -> None:
    """A full copy of the committed database through SQLite's backup API, read
    on a connection of its own: the caller holds the write lock (BEGIN
    IMMEDIATE), and a backup from that same connection would wait on itself.
    The copy takes the source's WAL journal mode; it is set back to a rollback
    journal before it is closed, so it is one self-contained file and opening
    it later leaves no -wal or -shm file next to it."""
    src = sqlite3.connect(db_file, timeout=30)
    dest = sqlite3.connect(target)
    try:
        src.backup(dest)
        dest.execute("PRAGMA journal_mode=DELETE").fetchone()
    except BaseException:
        dest.close()
        src.close()
        target.unlink(missing_ok=True)  # never leave a partial copy behind
        raise
    dest.close()
    src.close()


COUNT_FOLLOWUP_ROWS = "SELECT COUNT(*) FROM followups"


def migrate_followups(conn: sqlite3.Connection) -> int:
    """Archive and drop a leftover `followups` table; returns the number of
    rows archived. Does nothing when the table is absent, so it is safe on
    every connection and under several processes at once (the second one
    finds the table gone once it holds the write lock). Order: backup file,
    archive events, DROP, one commit. A failed backup raises and leaves the
    table as it was.

    Every run that finds rows first writes a full-file backup under a new
    name (-2, -3 ...), up to FOLLOWUPS_BACKUP_MAX copies; past that the rows
    are archived as events with no further copy, and that is logged. An MCP
    server still running the old code re-creates an empty `followups` table
    on every tool call (its schema has CREATE TABLE IF NOT EXISTS): an empty
    table holds nothing to keep, so it is dropped with no backup and no
    events, or an open old session would write a copy on every call."""
    if not _has_table(conn, FOLLOWUPS_TABLE):
        return 0
    from jobkit.log import log

    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not _has_table(conn, FOLLOWUPS_TABLE):
            conn.rollback()
            return 0
        if conn.execute(COUNT_FOLLOWUP_ROWS).fetchone()[0] == 0:
            conn.execute(f"DROP TABLE {FOLLOWUPS_TABLE}")
            conn.commit()
            log("db.followups_dropped_empty")
            return 0
        db_file = _main_db_file(conn)
        backup: Path | None = None
        if db_file is not None:
            if _backup_count(db_file) < FOLLOWUPS_BACKUP_MAX:
                backup = backup_path_for(db_file)
                _backup(db_file, backup)
            else:
                log("db.followups_backup_skipped", copies=FOLLOWUPS_BACKUP_MAX)
        rows = conn.execute(ARCHIVE_FOLLOWUP_ROWS).fetchall()
        for r in rows:
            row = dict(r)
            job_id = row.pop("archived_job_id")
            conn.execute(
                RECORD_EVENT,
                {
                    "job_id": job_id,
                    "kind": FOLLOWUPS_ARCHIVED_EVENT,
                    "detail": json.dumps(row, default=str, sort_keys=True),
                },
            )
        conn.execute(f"DROP TABLE {FOLLOWUPS_TABLE}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    log(
        "db.followups_archived",
        rows=len(rows),
        backup=str(backup) if backup is not None else None,
    )
    return len(rows)


# ---- named queries ----

UPSERT_COMPANY = """
INSERT INTO companies (name, normalized_name, domain, tier)
VALUES (:name, :normalized_name, :domain, :tier)
ON CONFLICT(normalized_name) DO UPDATE SET tier=excluded.tier, domain=excluded.domain
RETURNING id
"""

# Feeds and other discovery paths only ever ADD a company (default tier 3);
# they never change the tier or domain the user set in config/targets.yaml.
# (An aggregator pull once reset newly added tier-2 companies to tier 3,
# and the tier-3 standing rule then rejected their jobs.)
UPSERT_COMPANY_IF_NEW = """
INSERT INTO companies (name, normalized_name, domain, tier)
VALUES (:name, :normalized_name, :domain, :tier)
ON CONFLICT(normalized_name) DO UPDATE SET domain=COALESCE(companies.domain, excluded.domain)
RETURNING id
"""

UPSERT_BOARD = """
INSERT INTO boards (company_id, ats, token, verified_at)
VALUES (:company_id, :ats, :token, :verified_at)
ON CONFLICT(ats, token) DO UPDATE SET company_id=excluded.company_id
RETURNING id
"""

UPSERT_JOB = """
INSERT INTO jobs (board_id, company_id, external_id, title, normalized_title,
                  company_name, normalized_company, location_raw, location_bucket,
                  cluster_key, remote, comp_min, comp_max, comp_currency,
                  jd_html, jd_hash, apply_url, posted_at)
VALUES (:board_id, :company_id, :external_id, :title, :normalized_title,
        :company_name, :normalized_company, :location_raw, :location_bucket,
        :cluster_key, :remote, :comp_min, :comp_max, :comp_currency,
        :jd_html, :jd_hash, :apply_url, :posted_at)
ON CONFLICT(board_id, external_id) DO UPDATE SET
  title=excluded.title, normalized_title=excluded.normalized_title,
  location_raw=excluded.location_raw, location_bucket=excluded.location_bucket,
  cluster_key=excluded.cluster_key, remote=excluded.remote,
  comp_min=excluded.comp_min, comp_max=excluded.comp_max,
  comp_currency=excluded.comp_currency,
  jd_html=excluded.jd_html, jd_hash=excluded.jd_hash,
  apply_url=excluded.apply_url, posted_at=excluded.posted_at,
  last_seen_at=datetime('now'), closed_at=NULL
RETURNING id
"""

UPSERT_SOURCE = """
INSERT INTO sources (job_id, source, source_url, is_canonical)
VALUES (:job_id, :source, :source_url, :is_canonical)
ON CONFLICT(job_id, source) DO UPDATE SET seen_at=datetime('now'), source_url=excluded.source_url
"""

MARK_VANISHED = """
UPDATE jobs SET closed_at = datetime('now'), status = 'closed'
WHERE board_id = :board_id AND closed_at IS NULL
  AND external_id NOT IN (SELECT value FROM json_each(:seen_ids))
"""

# The per-job form fetch got a definite not-found from the ATS (404/410, or
# its not-found body): close the job as a vanished posting is closed, so it is
# never tailored or staged (autofill/form_spec.py, review 2026-10-12).
# Only a queued or approved job that is still open.
CLOSE_GONE_POSTING = """
UPDATE jobs SET closed_at = datetime('now'), status = 'closed'
WHERE id = :job_id AND closed_at IS NULL AND status IN ('queued', 'approved')
"""

# MCP get_job: the per-job check that closed the posting, if one did
POSTING_GONE_EVENT = """
SELECT detail, at FROM events WHERE job_id = :job_id AND kind = 'posting_gone'
ORDER BY id DESC LIMIT 1
"""

# MCP get_job: the job's latest staging run, with that run's read-back event
# (autofill/browser.stage_job writes it with the autofill_runs id)
LATEST_STAGING_RUN = """
SELECT ar.id, ar.status, ar.abort_reason, ar.unfilled_fields, ar.created_at,
       (SELECT e.detail FROM events e
        WHERE e.job_id = ar.job_id AND e.kind = 'readback'
          AND json_extract(e.detail, '$.run_id') = ar.id
        ORDER BY e.id DESC LIMIT 1) AS readback
FROM autofill_runs ar WHERE ar.job_id = :job_id ORDER BY ar.id DESC LIMIT 1
"""

SET_BOARD_POLLED = """
UPDATE boards SET etag=:etag, last_polled_at=datetime('now'),
  consecutive_failures=0, status='active' WHERE id=:board_id
"""

BUMP_BOARD_FAILURE = """
UPDATE boards SET consecutive_failures = consecutive_failures + 1,
  status = CASE WHEN consecutive_failures + 1 >= 3 THEN 'stale' ELSE status END
WHERE id = :board_id
"""

# ---- JD-missing guard (audit 2026-10-08 §2 item 2) ----
# A job whose description is too short to triage or tailor (normalize.jd_is_missing).

REJECT_JOB = "UPDATE jobs SET status = 'rejected' WHERE id = :job_id"

# idempotent: tailoring retries an approved job every run, one row is enough
RECORD_SANITY_REJECTION_ONCE = """
INSERT INTO rejections (job_id, layer, reason)
SELECT :job_id, 'sanity', :reason
WHERE NOT EXISTS (
  SELECT 1 FROM rejections WHERE job_id = :job_id AND layer = 'sanity' AND reason = :reason
)
"""

# ---- one-off cleanup: `jobkit purge-empty-jd-cache` ----

LLM_CACHE_EXISTS = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'llm_cache'"

COUNT_CACHE_ROWS_CONTAINING = (
    "SELECT COUNT(*) AS n FROM llm_cache WHERE instr(cache_key, :needle) > 0"
)

DELETE_CACHE_ROWS_CONTAINING = "DELETE FROM llm_cache WHERE instr(cache_key, :needle) > 0"

ALL_JOB_JDS = "SELECT id, status, jd_html FROM jobs"

SCORED_JOB_IDS = "SELECT job_id FROM scores"

DELETE_SCORE = "DELETE FROM scores WHERE job_id = :job_id"

# a queued job whose score was reset goes back to 'filtered' so the next triage
# run re-scores it — or, with no JD, rejects it as jd_missing. Human-decided
# statuses (approved, applied, snoozed, rejected) are left alone.
REQUEUE_UNSCORED = "UPDATE jobs SET status = 'filtered' WHERE id = :job_id AND status = 'queued'"

RECORD_EVENT = "INSERT INTO events (job_id, kind, detail) VALUES (:job_id, :kind, :detail)"

# ---- human answers to novel questions (review 2026-10-08, wp5-autofill) ----

# Field identities the human answered through the MCP answer_novel tool. That
# tool writes into form_specs.drafted_answers, the same dict the overnight
# drafter fills, so this event (detail = field identity) is what tells a human
# answer from an AI draft at staging. Only answers recorded AFTER the current
# capture count: a capture replaces drafted_answers wholesale, so an older
# event no longer describes what is stored (strict '>': a same-second tie is
# treated as an AI draft, the safe side).
HUMAN_ANSWERED_FIELDS = """
SELECT DISTINCT e.detail FROM events e JOIN form_specs fs ON fs.job_id = e.job_id
WHERE e.job_id = :job_id AND e.kind = 'novel_answered'
  AND (fs.captured_at IS NULL OR e.at > fs.captured_at)
"""

# ---- claims check for letters and drafted answers (audit 2026-10-08 §2 item 3) ----

# every company the pipeline has seen: a letter naming one of these that is not
# the target (and not one of the candidate's own employers) is a hard fail
KNOWN_COMPANY_NAMES = "SELECT name FROM companies"

# fact-check failures recorded by tailoring and form capture, newest first
PROVENANCE_EVENTS_FOR_JOB = """
SELECT kind, detail, at FROM events
WHERE job_id = :job_id AND kind IN ('cover_provenance_failed', 'novel_provenance_failed')
ORDER BY id DESC LIMIT 5
"""

# The latest cover-letter failure tailoring recorded for a job (fact check, or
# authenticity after two regenerations). Its detail carries the attempt key
# (jd hash : corpus hash : prompt versions): tailor_approved does not retry the
# job until one of those changes, or the human asks (MCP retailor).
LAST_COVER_FAILURE_FOR_JOB = """
SELECT kind, detail, at FROM events
WHERE job_id = :job_id AND kind IN ('cover_provenance_failed', 'cover_authenticity_failed')
ORDER BY id DESC LIMIT 1
"""

# ---- re-judging stored letters and answers (claim-judge review 2026-10-10) ----
# pipeline/rejudge.py: the claim judge reads what is drafted from the day it
# shipped; these find what was stored before and can still reach a form.

# one per job `jobkit rejudge` checked: JSON detail with the judge prompt
# version, the letter (its cover_path) and whether it passed, and the answers
# (field identities) that passed and failed
CLAIMS_REJUDGED_EVENT = "claims_rejudged"

# Stored cover letters that can still be staged: each job's newest
# application, tailored with a letter, on a job not applied to and never
# submitted (any application of the job with submitted_at set).
REJUDGE_LETTERS = """
SELECT a.id AS app_id, a.job_id, a.cover_path, a.provenance_path, a.updated_at,
       j.title, j.company_name, j.jd_html, r.jd_markdown
FROM applications a
JOIN jobs j ON j.id = a.job_id
LEFT JOIN job_requirements r ON r.job_id = j.id
WHERE a.id = (SELECT MAX(id) FROM applications WHERE job_id = a.job_id)
  AND a.stage IN ('tailored', 'staged') AND a.cover_path IS NOT NULL
  AND j.status != 'applied'
  AND NOT EXISTS (SELECT 1 FROM applications x
                  WHERE x.job_id = a.job_id AND x.submitted_at IS NOT NULL)
ORDER BY a.job_id
"""

# Form specs holding stored answers, on jobs not applied to and never submitted.
REJUDGE_FORM_SPECS = """
SELECT fs.job_id, fs.fields, fs.drafted_answers, j.title, j.company_name, j.jd_html
FROM form_specs fs
JOIN jobs j ON j.id = fs.job_id
WHERE COALESCE(fs.drafted_answers, '{}') NOT IN ('{}', '')
  AND j.status != 'applied'
  AND NOT EXISTS (SELECT 1 FROM applications x
                  WHERE x.job_id = fs.job_id AND x.submitted_at IS NOT NULL)
ORDER BY fs.job_id
"""

# Did an earlier rejudge pass this letter under this judge version, since the
# application last changed? (A re-tailored letter names its judge version in
# provenance.json instead.)
LETTER_PASSED_REJUDGE = f"""
SELECT 1 FROM events
WHERE job_id = :job_id AND kind = '{CLAIMS_REJUDGED_EVENT}'
  AND json_extract(detail, '$.judge') = :judge
  AND json_extract(detail, '$.letter') = :letter
  AND json_extract(detail, '$.letter_ok') = 1
  AND at >= COALESCE(:since, '')
LIMIT 1
"""

# ---- letters never judged are not final (review 2026-10-10, pipeline/letter_gate.py) ----

# one per stored letter found unjudged: JSON detail {cover, why}. Recorded once
# per letter (job + cover path) since its application last changed.
COVER_NEEDS_RETAILOR_EVENT = "cover_needs_retailor"

# the job's newest application, with what letter_gate reads
APPLICATION_LETTER_FOR_JOB = """
SELECT id AS app_id, job_id, stage, cover_path, provenance_path, updated_at
FROM applications WHERE job_id = :job_id ORDER BY id DESC LIMIT 1
"""

# Letters staging would attach: each open job's newest application, tailored
# with a letter, never submitted, on a job not applied to, rejected or closed.
STAGEABLE_LETTERS = """
SELECT a.id AS app_id, a.job_id, a.stage, a.cover_path, a.provenance_path, a.updated_at,
       j.company_name, j.title, j.apply_url
FROM applications a
JOIN jobs j ON j.id = a.job_id
WHERE a.id = (SELECT MAX(id) FROM applications WHERE job_id = a.job_id)
  AND a.stage = 'tailored' AND a.cover_path IS NOT NULL AND a.submitted_at IS NULL
  AND j.status NOT IN ('applied', 'closed', 'rejected') AND j.closed_at IS NULL
  AND NOT EXISTS (SELECT 1 FROM applications x
                  WHERE x.job_id = a.job_id AND x.submitted_at IS NOT NULL)
ORDER BY a.job_id
"""

# Did any rejudge (any judge prompt version) pass this very letter since the
# application last changed?
LETTER_PASSED_ANY_REJUDGE = f"""
SELECT 1 FROM events
WHERE job_id = :job_id AND kind = '{CLAIMS_REJUDGED_EVENT}'
  AND json_extract(detail, '$.letter') = :letter
  AND json_extract(detail, '$.letter_ok') = 1
  AND at >= COALESCE(:since, '')
LIMIT 1
"""

# When this letter was first found unjudged (since its application last changed)
COVER_NEEDS_RETAILOR_AT = f"""
SELECT at FROM events
WHERE job_id = :job_id AND kind = '{COVER_NEEDS_RETAILOR_EVENT}'
  AND json_extract(detail, '$.cover') = :cover
  AND at >= COALESCE(:since, '')
ORDER BY id LIMIT 1
"""

# A stored letter the re-check failed: the application goes back to
# 'approved' with no letter, the state a letter that failed when it was
# drafted is left in (pipeline/tailor.py). Only if it is still that letter.
REJECT_STORED_COVER = """
UPDATE applications SET stage = 'approved', cover_path = NULL, updated_at = datetime('now')
WHERE id = :id AND stage IN ('tailored', 'staged') AND cover_path = :cover_path
  AND submitted_at IS NULL
"""

REJUDGE_FORM_SPEC = """
SELECT fields, drafted_answers, novel_questions, field_count, required_count, has_assessment
FROM form_specs WHERE job_id = :job_id
"""

# captured_at is left alone: HUMAN_ANSWERED_FIELDS compares the human's
# answers with it
UPDATE_REJUDGED_FORM_SPEC = """
UPDATE form_specs SET fields = :fields, drafted_answers = :drafted_answers,
  novel_questions = :novel_questions, novel_question_count = :novel_question_count,
  friction_score = :friction_score
WHERE job_id = :job_id
"""

# ---- self-review of drafted artifacts (package 2, feature request 2026-10-09) ----

# what a self-review of one job's artifacts sees besides the draft: the JD's
# extracted requirements, the scorer's gap report, and the JD hash (cost guard)
SELF_REVIEW_JOB_CONTEXT = """
SELECT j.id, j.title, j.company_name, j.jd_hash, r.must_have, r.nice_to_have, r.implied, sc.gaps
FROM jobs j
LEFT JOIN job_requirements r ON r.job_id = j.id
LEFT JOIN scores sc ON sc.job_id = j.id
WHERE j.id = :job_id
"""

# one row per self-review (detail JSON: artifact, round, verdict, outcome), newest first
SELF_REVIEW_EVENTS_FOR_JOB = """
SELECT detail, at FROM events WHERE job_id = :job_id AND kind = 'self_review'
ORDER BY id DESC LIMIT 40
"""

# facts the self-review asked the human for (detail JSON: key, question, why,
# fact_needed, job_id, artifact); a key is raised once per job
OPEN_QUESTIONS_FOR_JOB = """
SELECT detail, at FROM events WHERE job_id = :job_id AND kind = 'question_open' ORDER BY id
"""

OPEN_QUESTION_KEYS_FOR_JOB = """
SELECT json_extract(detail, '$.key') FROM events
WHERE job_id = :job_id AND kind = 'question_open'
"""

# ---- questions for the human, corpus proposals, rescore (package 3, 2026-10-09) ----
# pipeline/questions.py aggregates what to ask; pipeline/rescore.py re-scores.

# Open jobs the questions are aggregated over: queued or approved, posting open,
# with the fit scorer's gap report.
OPEN_JOBS_WITH_GAPS = """
SELECT j.id, j.company_name, j.title, j.status, sc.fit_score, sc.gaps
FROM jobs j JOIN scores sc ON sc.job_id = j.id
WHERE j.status IN ('queued', 'approved') AND j.closed_at IS NULL
ORDER BY j.id
"""

# The self-review's questions (question_open events) of those same open jobs.
OPEN_SELF_REVIEW_QUESTIONS = """
SELECT e.job_id, e.detail, j.company_name, j.title, j.status, sc.fit_score
FROM events e
JOIN jobs j ON j.id = e.job_id
LEFT JOIN scores sc ON sc.job_id = j.id
WHERE e.kind = 'question_open' AND j.status IN ('queued', 'approved') AND j.closed_at IS NULL
ORDER BY e.id
"""

# Question keys the human has settled: answered (a corpus proposal was made)
# or declined ("no" — never asked again), and declines the human took back
# (undecline: the key is open again). In event order; job_id is NULL on these.
SETTLED_QUESTION_KEYS = """
SELECT kind, json_extract(detail, '$.key') AS key FROM events
WHERE kind IN ('question_answered', 'question_declined', 'question_undeclined')
ORDER BY id
"""

# Keys put in front of the human on one local day (the daily cap counts them).
QUESTIONS_ASKED_ON_DAY = """
SELECT json_extract(detail, '$.key') AS key FROM events
WHERE kind = 'question_asked' AND json_extract(detail, '$.day') = :day
ORDER BY id
"""

# Rescore. A score's profile is recorded as a `score_profile` event (detail:
# profile_hash, prompt_version) each time a job is scored; a score with no such
# event predates the record and counts as stale. Every open job with a score
# and extracted requirements, with what the selection rules need: its status,
# the newest rejection reason, and whether any decision (human or standing
# rule) exists. The location, comp and posted_at columns ride along because a
# score-gate rejection is put back in the queue only if the deterministic
# filters still pass it (filter.reject_reason_now).
RESCORE_CANDIDATES = """
SELECT j.id, j.status, j.title, j.company_name, j.jd_html, j.jd_hash,
       j.location_raw, j.location_bucket, j.remote, j.comp_min, j.comp_max,
       j.comp_currency, j.posted_at,
       r.must_have, r.nice_to_have, r.implied, r.disqualifier,
       sc.fit_score AS old_score,
       (SELECT json_extract(e.detail, '$.profile_hash') FROM events e
        WHERE e.job_id = j.id AND e.kind = 'score_profile'
        ORDER BY e.id DESC LIMIT 1) AS scored_profile,
       (SELECT x.reason FROM rejections x WHERE x.job_id = j.id
        ORDER BY x.id DESC LIMIT 1) AS last_rejection,
       EXISTS (SELECT 1 FROM decisions d WHERE d.job_id = j.id) AS decided
FROM jobs j
JOIN scores sc ON sc.job_id = j.id
JOIN job_requirements r ON r.job_id = j.id
WHERE j.closed_at IS NULL
ORDER BY j.id
"""

JOB_STATUS = "SELECT id, status, closed_at FROM jobs WHERE id = :job_id"

# the queue gate's rejection, as run_queue writes it (layer filter)
RECORD_SCORE_GATE_REJECTION = """
INSERT INTO rejections (job_id, layer, reason) VALUES (:job_id, 'filter', :reason)
"""

# a deterministic-filter rejection written outside the filter pass: rescore
# records one when a score-gate rejection no longer passes the current filters
RECORD_FILTER_REJECTION = """
INSERT INTO rejections (job_id, layer, reason) VALUES (:job_id, 'filter', :reason)
"""

# the newest rescore results (detail: old, new, status_before, status_after, ...)
RECENT_RESCORES = """
SELECT e.job_id, e.detail, e.at, j.company_name, j.title
FROM events e JOIN jobs j ON j.id = e.job_id
WHERE e.kind = 'rescored'
ORDER BY e.id DESC LIMIT :limit
"""

# the newest application row for a job (stage + artifact paths)
LATEST_APPLICATION_FOR_JOB = """
SELECT id, stage, resume_path, cover_path FROM applications
WHERE job_id = :job_id ORDER BY id DESC LIMIT 1
"""

# ---- refilter after a retarget (audit 2026-10-08 §2 item 5) ----

# Open, still-rejected jobs whose LATEST rejection is filter-layer with a reason
# the config can reverse (title include/exclude, a paused track, comp floor,
# location; filter.REFILTER_REASON_CLASSES is the same set). A job a
# human or a later layer has since moved on (queued, approved, sanity-rejected)
# is not a candidate. Nor is any job with a `decisions` row: the MCP reject tool
# writes only there (no rejections row), so a job refilter revived and the
# human then rejected still has the old filter rejection as its latest one, and
# resetting it would override a human-gate decision (review 2026-10-08).
# jd_html rides along only for jobs with published comp: the comp floor reads
# the pay basis (base vs OTE) back from it.
REFILTER_CANDIDATES = """
SELECT j.id, j.title, j.company_name, j.location_raw, j.location_bucket, j.remote,
       j.comp_min, j.comp_max, j.comp_currency, j.posted_at, r.reason,
       CASE WHEN j.comp_max IS NOT NULL THEN j.jd_html END AS jd_html
FROM jobs j
JOIN rejections r ON r.id = (SELECT MAX(id) FROM rejections WHERE job_id = j.id)
WHERE j.closed_at IS NULL AND j.status = 'rejected' AND r.layer = 'filter'
  AND (r.reason = 'no_include_title_match' OR r.reason GLOB 'exclude_title:*'
       OR r.reason GLOB 'track_paused:*'
       OR r.reason GLOB 'comp_below_floor:*' OR r.reason GLOB 'location:*')
  AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.job_id = j.id)
ORDER BY j.id
"""

# back to 'new' so run_filters re-evaluates the job; the bucket and cluster key
# are recomputed with the current geo rules on the way. A job with a human or
# standing-rule decision is never reset (same guard as REFILTER_CANDIDATES).
RESET_REJECTED_TO_NEW = """
UPDATE jobs SET status = 'new', location_bucket = :location_bucket, cluster_key = :cluster_key
WHERE id = :job_id AND status = 'rejected' AND closed_at IS NULL
  AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.job_id = jobs.id)
"""

# Open jobs held by a paused role track (filters.yaml `active: false`): still
# rejected, latest rejection `track_paused:<id>`, no decision — exactly the
# ones a re-enable + refilter would put back through the filter. Per reason.
PAUSED_TRACK_HOLDS = """
SELECT r.reason, COUNT(*) AS n
FROM jobs j
JOIN rejections r ON r.id = (SELECT MAX(id) FROM rejections WHERE job_id = j.id)
WHERE j.closed_at IS NULL AND j.status = 'rejected' AND r.layer = 'filter'
  AND r.reason GLOB 'track_paused:*'
  AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.job_id = j.id)
GROUP BY r.reason
"""

# ---- operations: freshness, run records, spawns (audit 2026-10-08 §2 item 7, §3 items 5-6) ----

# The last successful board poll (a 200 or a 304 sets last_polled_at), and how
# many hours ago that was. SQLite's clock on both sides, so both are UTC.
LAST_SUCCESSFUL_POLL = """
SELECT MAX(last_polled_at) AS at,
       (julianday('now') - julianday(MAX(last_polled_at))) * 24.0 AS hours
FROM boards WHERE last_polled_at IS NOT NULL
"""

# Stages of the most recent `jobkit run-all` that did not finish ok. Each stage
# writes one `run_all.stage` event (job_id NULL) whose JSON detail carries the
# run id, the stage name, its status and the error.
LAST_RUN_FAILED_STAGES = """
SELECT json_extract(e.detail, '$.stage') AS stage,
       json_extract(e.detail, '$.status') AS status,
       json_extract(e.detail, '$.error') AS error,
       json_extract(e.detail, '$.mode') AS mode,
       e.at
FROM events e
WHERE e.kind = 'run_all.stage'
  AND json_extract(e.detail, '$.run') = (
      SELECT json_extract(detail, '$.run') FROM events
      WHERE kind = 'run_all.stage' ORDER BY id DESC LIMIT 1)
  AND json_extract(e.detail, '$.status') != 'ok'
ORDER BY e.id
"""

# ---- the nightly run (`jobkit nightly`; approved 2026-10-08) ----
# A nightly run is a run-all run with mode 'nightly': `run_all.start` (run id,
# mode, pid, stage names), one `run_all.stage` per stage, then `run_all.done`
# (run id, mode, failed stages). No done event: still running, or stopped.

# The newest nightly run: run id, pid, start time, and its done detail and
# finish time (both NULL while it runs, or when it was stopped).
LATEST_NIGHTLY_RUN = """
WITH s AS (
  SELECT id, json_extract(detail, '$.run') AS run, json_extract(detail, '$.pid') AS pid, at
  FROM events
  WHERE kind = 'run_all.start' AND json_extract(detail, '$.mode') = 'nightly'
  ORDER BY id DESC LIMIT 1
)
SELECT s.run, s.pid, s.at AS started, d.detail AS done_detail, d.at AS finished
FROM s LEFT JOIN events d
  ON d.id = (SELECT MIN(x.id) FROM events x
             WHERE x.id > s.id AND x.kind = 'run_all.done'
               AND json_extract(x.detail, '$.run') = s.run)
"""

# The newest nightly run that finished with every stage ok, and how many hours
# ago it finished (SQLite's clock, UTC).
LAST_SUCCESSFUL_NIGHTLY = """
SELECT json_extract(detail, '$.run') AS run, at,
       (julianday('now') - julianday(at)) * 24.0 AS hours
FROM events
WHERE kind = 'run_all.done' AND json_extract(detail, '$.mode') = 'nightly'
  AND json_array_length(json_extract(detail, '$.failed')) = 0
ORDER BY id DESC LIMIT 1
"""

# Every stage outcome recorded for one run, in order.
RUN_STAGE_OUTCOMES = """
SELECT json_extract(detail, '$.stage') AS stage,
       json_extract(detail, '$.status') AS status,
       json_extract(detail, '$.seconds') AS seconds,
       json_extract(detail, '$.error') AS error,
       json_extract(detail, '$.summary') AS summary,
       at
FROM events
WHERE kind = 'run_all.stage' AND json_extract(detail, '$.run') = :run
ORDER BY id
"""

# A nightly staging attempt that raised on a job (stage_jobs status 'failed':
# page.goto timed out or hit net::ERR_NAME_NOT_RESOLVED on a dead apply URL,
# the browser errored on the page) writes no autofill_runs row. Uncapped, such
# a job was a candidate every night, failed the 'stage' stage every night and
# kept `jobkit nightly` from ever succeeding again. So the nightly staging
# stage (pipeline/nightly.stage_tailored) records one event per failure
# (kind NIGHTLY_STAGE_FAILED_EVENT, job_id set, detail = the error), and after
# NIGHTLY_STAGE_FAILURE_CAP of them since the job was last tailored the job is
# no longer a candidate: STAGING_FAILURES lists it for the human with the
# error (pipeline/halts.py), and that failure no longer fails the stage.
# A 'deferred' job (profile locked, browser closed) is transient and has no cap.
NIGHTLY_STAGE_FAILED_EVENT = "nightly.stage_failed"
NIGHTLY_STAGE_FAILURE_CAP = 2

# Approved jobs the nightly run stages: newest application tailored (artifacts
# written), not submitted, posting still open, and no staging run since it was
# last tailored. A job staged on an earlier night is not reopened every night;
# one that was deferred or failed (no autofill_runs row) is tried again, a
# failed one until NIGHTLY_STAGE_FAILURE_CAP failures since it was tailored.
# Two halts that can be transient are retried (pipeline/halts.py): a
# no-form-spec-captured halt once a form spec with fields exists, and a
# posting-not-found halt (a slow page render) once more, so twice in all per
# tailoring. A CAPTCHA, a government-ID/payment field, an embedded assessment
# or an apply URL off the allowlist is never retried here (hard rule 2: the
# human takes it over; STAGING_HALTS lists it for them).
NIGHTLY_STAGE_CANDIDATES = f"""
SELECT j.id FROM jobs j
JOIN applications a ON a.id = (SELECT MAX(id) FROM applications WHERE job_id = j.id)
LEFT JOIN form_specs fs ON fs.job_id = j.id
LEFT JOIN autofill_runs ar
  ON ar.id = (SELECT MAX(x.id) FROM autofill_runs x
              WHERE x.job_id = j.id AND x.created_at >= COALESCE(a.updated_at, ''))
WHERE j.status = 'approved' AND j.closed_at IS NULL
  AND a.stage = 'tailored' AND a.resume_path IS NOT NULL AND a.submitted_at IS NULL
  AND (ar.id IS NULL
       OR (ar.status = 'needs_human' AND ar.abort_reason = 'no-form-spec-captured'
           AND COALESCE(fs.fields, '[]') NOT IN ('[]', ''))
       OR (ar.status = 'needs_human' AND ar.abort_reason = 'posting-not-found'
           AND (SELECT COUNT(*) FROM autofill_runs x
                WHERE x.job_id = j.id AND x.created_at >= COALESCE(a.updated_at, '')
                  AND x.abort_reason = 'posting-not-found') < 2))
  AND (SELECT COUNT(*) FROM events e
       WHERE e.job_id = j.id AND e.kind = '{NIGHTLY_STAGE_FAILED_EVENT}'
         AND e.at >= COALESCE(a.updated_at, '')) < {NIGHTLY_STAGE_FAILURE_CAP}
ORDER BY j.id
"""

# How many nightly staging failures a job has had since its newest application
# was last tailored (re-tailoring starts the count over).
NIGHTLY_STAGE_FAILURES_FOR_JOB = f"""
SELECT COUNT(*) AS failures FROM events e
WHERE e.job_id = :job_id AND e.kind = '{NIGHTLY_STAGE_FAILED_EVENT}'
  AND e.at >= COALESCE((SELECT updated_at FROM applications
                        WHERE job_id = :job_id ORDER BY id DESC LIMIT 1), '')
"""

# Background processes the MCP server started (one `spawn` event each, JSON
# detail: args, pid, log, started), newest first, with the exit code when one
# was recorded (`spawn_exit` event whose detail names the spawn event id).
RECENT_SPAWNS = """
SELECT s.id, s.job_id, s.detail, s.at,
       (SELECT x.detail FROM events x
        WHERE x.kind = 'spawn_exit'
          AND json_extract(x.detail, '$.spawn_event') = s.id
        ORDER BY x.id DESC LIMIT 1) AS exit_detail
FROM events s
WHERE s.kind = 'spawn'
ORDER BY s.id DESC LIMIT :limit
"""

# Jobs whose latest staging run left a form for the human to finish and submit:
# 'staged' (every required field filled) or 'needs_human' with at least one
# field filled (a required question is left for the human). A run that halted
# before filling anything (CAPTCHA, posting gone) is not reopened here. Jobs
# already submitted are excluded. Fully staged first, then cheapest friction.
OPEN_QUEUE_JOBS = """
SELECT ar.job_id, ar.status, j.company_name, j.title, fs.friction_score
FROM autofill_runs ar
JOIN jobs j ON j.id = ar.job_id
LEFT JOIN form_specs fs ON fs.job_id = ar.job_id
WHERE ar.id = (SELECT MAX(id) FROM autofill_runs WHERE job_id = ar.job_id)
  AND (ar.status = 'staged'
       OR (ar.status = 'needs_human' AND COALESCE(ar.filled_fields, '[]') NOT IN ('[]', '')))
  AND j.status NOT IN ('applied', 'closed', 'rejected')
  AND NOT EXISTS (SELECT 1 FROM applications a
                  WHERE a.job_id = ar.job_id AND a.submitted_at IS NOT NULL)
ORDER BY (ar.status = 'needs_human'), COALESCE(fs.friction_score, 100), ar.job_id
"""

# Jobs whose latest staging run halted before filling anything ('needs_human'
# with no field filled): a CAPTCHA or bot check, a government-ID or payment
# field, an embedded assessment, no form on the page, no form spec, an apply
# URL off the allowlist. OPEN_QUEUE_JOBS leaves these out (nothing is filled
# to review), so the digest, get_queue and schedule status list them
# (pipeline/halts.py) until the job is submitted, rejected or closed, or a
# later staging run gets past the halt. Newest halt first.
STAGING_HALTS = """
SELECT ar.job_id, ar.abort_reason, ar.screenshot_path, ar.created_at,
       j.company_name, j.title, j.apply_url
FROM autofill_runs ar
JOIN jobs j ON j.id = ar.job_id
WHERE ar.id = (SELECT MAX(id) FROM autofill_runs WHERE job_id = ar.job_id)
  AND ar.status = 'needs_human'
  AND COALESCE(ar.filled_fields, '[]') IN ('[]', '')
  AND j.status NOT IN ('applied', 'closed', 'rejected') AND j.closed_at IS NULL
  AND NOT EXISTS (SELECT 1 FROM applications a
                  WHERE a.job_id = ar.job_id AND a.submitted_at IS NOT NULL)
ORDER BY ar.id DESC
"""

# Jobs the nightly run stopped retrying after NIGHTLY_STAGE_FAILURE_CAP staging
# failures since they were last tailored (NIGHTLY_STAGE_CANDIDATES leaves them
# out), with the newest error and when it happened. Like STAGING_HALTS, listed
# for the human (pipeline/halts.py) until the job is submitted, rejected or
# closed, re-tailored (the count starts over), or a staging run at or after the
# newest failure writes a row; that row then puts the job in STAGING_HALTS or
# OPEN_QUEUE_JOBS instead, so it is never dropped. Newest failure first.
STAGING_FAILURES = f"""
WITH f AS (
  SELECT e.job_id, COUNT(*) AS failures, MAX(e.id) AS last_id
  FROM events e
  JOIN applications a ON a.id = (SELECT MAX(id) FROM applications WHERE job_id = e.job_id)
  WHERE e.kind = '{NIGHTLY_STAGE_FAILED_EVENT}' AND e.at >= COALESCE(a.updated_at, '')
  GROUP BY e.job_id
  HAVING COUNT(*) >= {NIGHTLY_STAGE_FAILURE_CAP}
)
SELECT f.job_id, f.failures, le.detail AS error, le.at,
       j.company_name, j.title, j.apply_url
FROM f
JOIN events le ON le.id = f.last_id
JOIN jobs j ON j.id = f.job_id
WHERE j.status NOT IN ('applied', 'closed', 'rejected') AND j.closed_at IS NULL
  AND NOT EXISTS (SELECT 1 FROM applications a
                  WHERE a.job_id = f.job_id AND a.submitted_at IS NOT NULL)
  AND NOT EXISTS (SELECT 1 FROM autofill_runs ar
                  WHERE ar.job_id = f.job_id AND ar.created_at >= le.at)
ORDER BY le.id DESC
"""

# ---- submissions and outcomes (audit 2026-10-08 §2 item 8) ----

JOB_BRIEF = """
SELECT id, company_id, company_name, title, status FROM jobs WHERE id = :job_id
"""

# newest application row with its submission state
APPLICATION_FOR_JOB = """
SELECT id, stage, submitted_at FROM applications
WHERE job_id = :job_id ORDER BY id DESC LIMIT 1
"""

INSERT_APPLICATION = "INSERT INTO applications (job_id, stage) VALUES (:job_id, :stage)"

MARK_APPLICATION_SUBMITTED = """
UPDATE applications SET stage = 'submitted', submitted_at = :submitted_at,
  updated_at = datetime('now')
WHERE id = :id
"""

SET_APPLICATION_STAGE = """
UPDATE applications SET stage = :stage, updated_at = datetime('now') WHERE id = :id
"""

SET_JOB_STATUS = "UPDATE jobs SET status = :status WHERE id = :job_id"

# ---- one application row per job (track/applications.py) ----
# every application row of a job, oldest first
APPLICATIONS_OF_JOB = """
SELECT id, job_id, stage, resume_path, cover_path, gap_report_path, provenance_path,
       submitted_at, updated_at
FROM applications WHERE job_id = :job_id ORDER BY id
"""

DELETE_APPLICATION = "DELETE FROM applications WHERE id = :id"

MOVE_APPLICATION = "UPDATE applications SET job_id = :job_id WHERE id = :id"

# the kept row takes another row's artifact paths only where it has none
FILL_APPLICATION_PATHS = """
UPDATE applications SET
  resume_path = COALESCE(resume_path, :resume_path),
  cover_path = COALESCE(cover_path, :cover_path),
  gap_report_path = COALESCE(gap_report_path, :gap_report_path),
  provenance_path = COALESCE(provenance_path, :provenance_path),
  updated_at = datetime('now')
WHERE id = :id
"""

# every submission the human recorded (MCP mark_submitted), oldest first
MARKED_SUBMITTED_EVENTS = """
SELECT job_id, detail, at FROM events WHERE kind = 'marked_submitted' AND job_id IS NOT NULL
ORDER BY id
"""

# job ids that hold an application row, or were marked applied
JOBS_WITH_APPLICATION_STATE = """
SELECT job_id AS id FROM applications
UNION SELECT id FROM jobs WHERE status = 'applied'
ORDER BY id
"""

# application rows on a closed aggregator row that was merged into an ATS row
APPLICATIONS_ON_MERGED_FEED_ROWS = """
SELECT a.id, a.job_id FROM applications a JOIN jobs j ON j.id = a.job_id
WHERE j.board_id IS NULL AND j.closed_at IS NOT NULL
  AND EXISTS (SELECT 1 FROM events e WHERE e.job_id = j.id AND e.kind = 'merged_into')
ORDER BY a.id
"""

# ---- no reply after N days (track/applications.close_ghosted) ----
# submitted applications with no response on record, submitted :days or more
# days ago. A response is an outcome event (MCP record_outcome) or an email
# response other than an automatic acknowledgement (track/responses.py).
# submitted_at is a bare local date when the person gave the day, or a UTC
# timestamp when mark_submitted stamped "now"; a timestamp counts from its
# local day (an evening submission in a US time zone is already the next day
# in UTC), a bare date as written.
GHOST_CANDIDATES = """
SELECT a.id, a.job_id, a.submitted_at, j.company_name, j.title
FROM applications a JOIN jobs j ON j.id = a.job_id
WHERE a.stage = 'submitted' AND a.submitted_at IS NOT NULL
  AND (CASE WHEN length(a.submitted_at) = 10 THEN a.submitted_at
            ELSE date(a.submitted_at, 'localtime') END)
      <= date('now', '-' || :days || ' days')
  AND NOT EXISTS (
    SELECT 1 FROM events e
    WHERE e.job_id = a.job_id
      AND (e.kind = 'outcome'
           OR (e.kind = 'email_response'
               AND json_extract(e.detail, '$.outcome') != 'acknowledged')))
ORDER BY a.submitted_at, a.id
"""

SET_APPLICATION_GHOSTED = """
UPDATE applications SET stage = 'ghosted', updated_at = datetime('now')
WHERE id = :id AND stage = 'submitted'
"""

COUNT_GHOSTED_APPLICATIONS = "SELECT COUNT(*) AS n FROM applications WHERE stage = 'ghosted'"

# feed jobs (no employer board: Adzuna, USAJOBS, the remote boards) that triage
# rejected because their description was too short to score (layer sanity,
# jd_missing), recorded at or after :since ('' counts every one). Only jobs
# whose status is 'rejected': a job the person approved that tailoring later
# refused for its short description has the same rejection row, but it keeps
# its approved status and the person has already seen it. Nor any job with a
# human decision: one approved, refused by tailoring and then rejected through
# the MCP reject tool is 'rejected' too, but the person scored and saw it
COUNT_FEED_JOBS_SHORT_JD = """
SELECT COUNT(DISTINCT r.job_id) AS n FROM rejections r
JOIN jobs j ON j.id = r.job_id
WHERE j.board_id IS NULL AND j.status = 'rejected'
  AND r.layer = 'sanity' AND r.reason = 'jd_missing'
  AND r.at >= :since
  AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.job_id = j.id AND d.auto = 0)
"""

# the same jobs, newest first, with what the person needs to look one up
LIST_FEED_JOBS_SHORT_JD = """
SELECT j.id, j.title, j.company_name, j.location_raw, j.apply_url, MAX(r.at) AS at
FROM rejections r
JOIN jobs j ON j.id = r.job_id
WHERE j.board_id IS NULL AND j.status = 'rejected'
  AND r.layer = 'sanity' AND r.reason = 'jd_missing'
  AND r.at >= :since
  AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.job_id = j.id AND d.auto = 0)
GROUP BY j.id
ORDER BY at DESC, j.id DESC
LIMIT :limit
"""

# ---- email response tracking (track/responses.py) ----
# every submitted application, with what an email from the employer or its
# ATS could name: the company, its domain, the title, the ATS and apply link
SUBMITTED_APPLICATIONS = """
SELECT a.id AS application_id, a.job_id, a.stage, a.submitted_at,
       j.title, j.company_name, j.apply_url, c.domain AS company_domain, b.ats
FROM applications a
JOIN jobs j ON j.id = a.job_id
JOIN companies c ON c.id = j.company_id
LEFT JOIN boards b ON b.id = j.board_id
WHERE a.submitted_at IS NOT NULL
ORDER BY a.submitted_at, a.id
"""

APPLICATION_BY_ID = """
SELECT a.id, a.job_id, a.stage, a.submitted_at, j.company_name, j.title
FROM applications a JOIN jobs j ON j.id = a.job_id WHERE a.id = :id
"""

EMAIL_RESPONSE_BY_MESSAGE_ID = """
SELECT id, job_id, detail FROM events
WHERE kind = 'email_response' AND json_extract(detail, '$.message_id') = :message_id
ORDER BY id LIMIT 1
"""

# the latest-dated response already recorded for an application: email
# responses that can move a stage (by the day the email arrived) and outcomes
# the person recorded (by when they recorded it), as 'YYYY-MM-DD HH:MM:SS'
LATEST_RESPONSE_FOR_APPLICATION = """
SELECT MAX(d) AS latest FROM (
  SELECT json_extract(detail, '$.received_at') AS d FROM events
  WHERE kind = 'email_response'
    AND json_extract(detail, '$.application_id') = :application_id
    AND json_extract(detail, '$.outcome') != 'acknowledged'
  UNION ALL
  SELECT at AS d FROM events
  WHERE kind = 'outcome' AND json_extract(detail, '$.application_id') = :application_id
)
"""

LAST_EMAIL_CHECK = """
SELECT id, detail, at FROM events WHERE kind = 'email_check' ORDER BY id DESC LIMIT 1
"""

COUNT_EMAIL_RESPONSES_AFTER = """
SELECT COUNT(*) AS n FROM events WHERE kind = 'email_response' AND id > :after_id
"""

# email responses recorded at or after :since ('YYYY-MM-DD HH:MM:SS' UTC)
EMAIL_RESPONSES_SINCE = """
SELECT e.id, e.job_id, e.detail, e.at, j.company_name, j.title
FROM events e LEFT JOIN jobs j ON j.id = e.job_id
WHERE e.kind = 'email_response' AND e.at >= :since
ORDER BY e.id
"""

# idempotent event: written once per (job, kind, detail)
RECORD_EVENT_ONCE = """
INSERT INTO events (job_id, kind, detail)
SELECT :job_id, :kind, :detail
WHERE NOT EXISTS (
  SELECT 1 FROM events WHERE job_id IS :job_id AND kind = :kind AND detail IS :detail
)
"""

# ---- ATS-over-feed merge (hard rule 10; audit 2026-10-08 §2 item 8) ----

# open aggregator rows (no board) — the merge candidates
OPEN_FEED_ROWS = """
SELECT id, apply_url FROM jobs WHERE board_id IS NULL AND closed_at IS NULL ORDER BY id
"""

# the same, narrowed to apply URLs that could name this board: its token
# appears in the URL, or it is a Greenhouse gh_jid link on a custom domain
OPEN_FEED_ROWS_FOR_BOARD = """
SELECT id, apply_url FROM jobs
WHERE board_id IS NULL AND closed_at IS NULL
  AND (instr(lower(apply_url), lower(:token)) > 0
       OR (:ats = 'greenhouse' AND instr(apply_url, 'gh_jid=') > 0))
ORDER BY id
"""

# the ATS row for one posting; an empty token matches any board of that ATS
# (a gh_jid link names the job id but not the board)
ATS_JOB_FOR_POSTING = """
SELECT j.id FROM jobs j JOIN boards b ON b.id = j.board_id
WHERE b.ats = :ats AND (:token = '' OR lower(b.token) = :token)
  AND lower(j.external_id) = :external_id
ORDER BY j.id LIMIT 1
"""

JOB_MERGE_STATE = """
SELECT j.id, j.board_id, j.status, j.closed_at,
       (SELECT COUNT(*) FROM applications a WHERE a.job_id = j.id) AS applications,
       (SELECT COUNT(*) FROM decisions d WHERE d.job_id = j.id AND d.auto = 0) AS human_decisions,
       (SELECT COUNT(*) FROM decisions d WHERE d.job_id = j.id) AS decisions
FROM jobs j WHERE j.id = :job_id
"""

COPY_SOURCES_TO_JOB = """
INSERT INTO sources (job_id, source, source_url, is_canonical, seen_at)
SELECT :to_id, source, source_url, 0, seen_at FROM sources WHERE job_id = :from_id
ON CONFLICT(job_id, source) DO NOTHING
"""

DELETE_SOURCES_FOR_JOB = "DELETE FROM sources WHERE job_id = :job_id"

MOVE_APPLICATIONS = "UPDATE applications SET job_id = :to_id WHERE job_id = :from_id"

MOVE_HUMAN_DECISIONS = "UPDATE decisions SET job_id = :to_id WHERE job_id = :from_id AND auto = 0"

# unsnooze reads snoozed_until events by job id
MOVE_SNOOZE_EVENTS = """
UPDATE events SET job_id = :to_id WHERE job_id = :from_id AND kind = 'snoozed_until'
"""

CLOSE_MERGED_FEED_ROW = """
UPDATE jobs SET status = 'closed', closed_at = datetime('now')
WHERE id = :job_id AND board_id IS NULL
"""

# the ATS row a closed feed row was merged into (its newest merged_into event);
# no row: the job is not a closed, merged feed row
MERGED_INTO_TARGET = """
SELECT CAST(json_extract(e.detail, '$.ats_job_id') AS INTEGER) AS ats_job_id
FROM events e JOIN jobs j ON j.id = e.job_id
WHERE e.job_id = :job_id AND e.kind = 'merged_into'
  AND j.board_id IS NULL AND j.closed_at IS NOT NULL
  AND json_extract(e.detail, '$.ats_job_id') IS NOT NULL
ORDER BY e.id DESC LIMIT 1
"""

# ---- one ATS board, polled now (`jobkit add-url`, LinkedIn guest discovery) ----
# discovery/ingest.py. An existing board keeps its company row (and so the
# tier the user set); a new one is added at tier 3 (UPSERT_COMPANY_IF_NEW).

BOARD_BY_ATS_TOKEN = """
SELECT b.id AS board_id, b.company_id, c.name AS company_name, b.ats, b.token
FROM boards b JOIN companies c ON c.id = b.company_id
WHERE b.ats = :ats AND b.token = :token
"""

# a board added by add-url or discovery is verified by its first good poll
SET_BOARD_VERIFIED_IF_UNSET = """
UPDATE boards SET verified_at = date('now') WHERE id = :board_id AND verified_at IS NULL
"""

JOB_ON_BOARD = """
SELECT id, title, company_name, status FROM jobs
WHERE board_id = :board_id AND external_id = :external_id
"""

# open jobs on one board with this normalized title (a LinkedIn card's title)
OPEN_JOBS_ON_BOARD_WITH_TITLE = """
SELECT id, title, company_name, status FROM jobs
WHERE board_id = :board_id AND normalized_title = :normalized_title AND closed_at IS NULL
ORDER BY id
"""

MAX_JOB_ID = "SELECT COALESCE(MAX(id), 0) AS n FROM jobs"

# the job a human pasted is put in front of them (add-url only, never discovery)
QUEUE_PASTED_JOB = """
UPDATE jobs SET status = 'queued' WHERE id = :job_id AND status IN ('new', 'filtered', 'rejected')
"""

# ---- LinkedIn guest discovery (discovery/linkedin_guest.py; rule 11 exception) ----
# Every row is an event (job_id NULL unless a card matched an ATS job); no
# LinkedIn job row is ever stored. `day` in each detail is the LOCAL calendar
# day, so the daily caps and the block hold until local midnight.

LINKEDIN_REQUESTS_ON_DAY = """
SELECT COUNT(*) AS n FROM events
WHERE kind = 'linkedin_guest.request'
  AND json_extract(detail, '$.day') = :day AND json_extract(detail, '$.type') = :type
"""

LINKEDIN_BLOCK_ON_DAY = """
SELECT detail, at FROM events
WHERE kind = 'linkedin_guest.blocked' AND json_extract(detail, '$.day') = :day
ORDER BY id DESC LIMIT 1
"""

# LinkedIn posting ids handled within the window (e.g. '-14 days'; UTC clock)
LINKEDIN_SEEN_SINCE = """
SELECT DISTINCT json_extract(detail, '$.id') AS id FROM events
WHERE kind = 'linkedin_guest.seen' AND at >= datetime('now', :window)
"""

LINKEDIN_RUNS_ON_DAY = """
SELECT detail FROM events
WHERE kind = 'linkedin_guest.run' AND json_extract(detail, '$.day') = :day
ORDER BY id
"""

# seconds since the last LinkedIn request of any run (NULL: none yet); `at`
# has whole seconds, so this can read up to 1 s long
LINKEDIN_SECONDS_SINCE_LAST_REQUEST = """
SELECT (julianday('now') - julianday(MAX(at))) * 86400.0 AS s FROM events
WHERE kind = 'linkedin_guest.request'
"""

# the file behind this connection's main database ('' for an in-memory one)
MAIN_DATABASE_FILE = "SELECT file FROM pragma_database_list WHERE name = 'main'"

# a company jobkit already polls: an active board of a supported ATS
ACTIVE_BOARD_FOR_COMPANY = """
SELECT b.ats, b.token FROM boards b JOIN companies c ON c.id = b.company_id
WHERE c.normalized_name = :normalized_name AND b.status = 'active'
ORDER BY b.id LIMIT 1
"""

COMPANY_BLOCKLISTED = """
SELECT 1 FROM companies WHERE normalized_name = :normalized_name AND blocklisted = 1
"""

# ---- board resolution: aggregator / LinkedIn jobs -> the employer's own posting ----
# discovery/board_resolver.py. The slug-probe cache, the board-match-ambiguous
# flags and the per-card outcomes are events (no schema change); a board found
# by the probe is a boards row with confidence 'probe' once one of its postings
# matched.

BOARD_PROBE_EVENT = "board_probe"  # job_id NULL; detail: company, found boards, requests
BOARD_MATCH_AMBIGUOUS_EVENT = "board_match_ambiguous"  # the flag for the human
BOARD_MATCH_CLEARED_EVENT = "board_match_cleared"  # a later run no longer finds it ambiguous
BOARD_RESOLVED_EVENT = "board_resolved"  # on the feed row: merged into its ATS row
BOARD_RESOLVE_CARD_EVENT = "board_resolve.card"  # one LinkedIn card's outcome
BOARD_RESOLVE_RUN_EVENT = "board_resolve.run"  # one run's counts

# the open aggregator rows a run looks at, approved first: a status the filter
# passed, or 'new' (the run asks filter.reject_reason_now first)
FEED_ROWS_TO_RESOLVE = """
SELECT j.*, c.domain AS company_domain
FROM jobs j LEFT JOIN companies c ON c.id = j.company_id
WHERE j.board_id IS NULL AND j.closed_at IS NULL
  AND j.status IN ('new', 'filtered', 'queued', 'approved', 'snoozed')
ORDER BY CASE j.status WHEN 'approved' THEN 0 WHEN 'queued' THEN 1 WHEN 'snoozed' THEN 2
                       WHEN 'filtered' THEN 3 ELSE 4 END, j.id
"""

# one row by id, whatever its status (`jobkit resolve-boards --job-id N`)
FEED_ROW_FOR_RESOLVE = """
SELECT j.*, c.domain AS company_domain
FROM jobs j LEFT JOIN companies c ON c.id = j.company_id
WHERE j.id = :job_id
"""

# every active board jobkit has for a company; fresh: polled in the last day
ACTIVE_BOARDS_FOR_COMPANY = """
SELECT b.id AS board_id, b.company_id, c.name AS company_name, b.ats, b.token,
       (b.last_polled_at IS NOT NULL AND b.last_polled_at >= datetime('now', '-1 day')) AS fresh
FROM boards b JOIN companies c ON c.id = b.company_id
WHERE c.normalized_name = :normalized_name AND b.status = 'active'
ORDER BY b.id
"""

OPEN_POSTINGS_ON_BOARD = """
SELECT id, external_id, title, location_raw, remote, apply_url FROM jobs
WHERE board_id = :board_id AND closed_at IS NULL
ORDER BY id
"""

# the newest slug probe of a company inside the cache window (e.g. '-14 days')
LATEST_BOARD_PROBE = """
SELECT detail, at FROM events
WHERE kind = 'board_probe' AND json_extract(detail, '$.company') = :normalized_name
  AND at >= datetime('now', :window)
ORDER BY id DESC LIMIT 1
"""

SET_BOARD_CONFIDENCE = "UPDATE boards SET confidence = :confidence WHERE id = :board_id"

# a board by ATS and token, whatever the token's case (Greenhouse and Ashby
# tokens answer in any case; boards.token is compared exactly)
BOARD_BY_ATS_TOKEN_NOCASE = """
SELECT b.id AS board_id, b.company_id, c.name AS company_name, b.ats, b.token
FROM boards b JOIN companies c ON c.id = b.company_id
WHERE b.ats = :ats AND lower(b.token) = lower(:token)
ORDER BY b.id LIMIT 1
"""

# a resolution outcome on the company's discovery-queue row; 'found' is never
# turned back into 'not_found'
MARK_DISCOVERY_ATTEMPT = """
UPDATE discovery_queue SET status = :status, attempted_at = datetime('now')
WHERE normalized_name = :normalized_name AND NOT (status = 'found' AND :status = 'not_found')
"""

# the job's newest flag and newest clear (ids; NULL when none)
BOARD_FLAG_STATE = """
SELECT
  (SELECT MAX(id) FROM events WHERE job_id = :job_id AND kind = 'board_match_ambiguous') AS flag_id,
  (SELECT MAX(id) FROM events WHERE job_id = :job_id
     AND kind IN ('board_match_cleared', 'board_resolved')) AS clear_id
"""

EVENT_DETAIL = "SELECT detail FROM events WHERE id = :event_id"

# open aggregator rows whose newest board-match-ambiguous flag stands (no later
# clear or resolution), with that flag
BOARD_MATCH_FLAGS = """
SELECT j.id AS job_id, j.company_name, j.title, j.location_raw, j.status, e.detail, e.at
FROM events e JOIN jobs j ON j.id = e.job_id
WHERE e.kind = 'board_match_ambiguous'
  AND e.id = (SELECT MAX(id) FROM events WHERE job_id = e.job_id AND kind = 'board_match_ambiguous')
  AND NOT EXISTS (SELECT 1 FROM events x WHERE x.job_id = e.job_id AND x.id > e.id
                  AND x.kind IN ('board_match_cleared', 'board_resolved'))
  AND j.board_id IS NULL AND j.closed_at IS NULL
  AND j.status IN ('new', 'filtered', 'queued', 'approved', 'snoozed')
ORDER BY j.id
"""

# board-match-ambiguous flags on LinkedIn cards (no job row) inside a window
CARD_FLAGS_SINCE = """
SELECT detail, at FROM events
WHERE kind = 'board_match_ambiguous' AND job_id IS NULL AND at >= datetime('now', :window)
ORDER BY id
"""

# LinkedIn cards whose company went to the discovery queue, inside a window
LINKEDIN_QUEUED_CARDS_SINCE = """
SELECT id, detail FROM events
WHERE kind = 'linkedin_guest.seen' AND at >= datetime('now', :window)
  AND (json_extract(detail, '$.outcome') LIKE 'queued_company:%'
       OR json_extract(detail, '$.outcome') LIKE 'company_already_queued:%')
ORDER BY id
"""

# LinkedIn card ids a resolution run already handled inside a window
CARDS_RESOLVED_SINCE = """
SELECT DISTINCT json_extract(detail, '$.li_id') AS li_id FROM events
WHERE kind = 'board_resolve.card' AND at >= datetime('now', :window)
"""

PROBES_SINCE = """
SELECT COUNT(DISTINCT json_extract(detail, '$.company')) AS n FROM events
WHERE kind = 'board_probe' AND at >= datetime('now', :window)
"""

# the resolution runs of one local day (the digest sums them)
BOARD_RESOLVE_RUNS_ON_DAY = """
SELECT detail FROM events
WHERE kind = 'board_resolve.run' AND date(at, 'localtime') = :day
ORDER BY id
"""
