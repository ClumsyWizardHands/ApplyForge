"""Stages 3-6 — extract, remote reality check, comp sanity, score. All Haiku,
all cached on the full rendered prompt + resolved model (llm.cache_key_for), all
writing to their own tables. Sanity-layer rejections are logged with
layer=sanity (one knob, one owner).

Before any LLM call, every stage refuses a job whose description text is under
normalize.MIN_JD_CHARS: rejected, layer=sanity, reason=jd_missing. A JD-less job
gives the model nothing to read but the title, and once let 133 feed jobs share
one cached answer (audit 2026-10-08 §2 item 2).

LLM calls run on a small thread pool (each worker gets its own SQLite
connection to the same DB for the cache); table writes stay on the caller's
connection. One bad response skips that job and is logged — it never kills
the batch."""

from __future__ import annotations

import datetime
import json
import math
import re
import sqlite3
from collections.abc import Callable, Collection, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml
from markdownify import markdownify

from jobkit import db as q
from jobkit import llm, paths
from jobkit.config import Filters, load_filters, read_config_yaml
from jobkit.log import log
from jobkit.normalize import JD_MISSING_REASON, jd_is_missing, jd_text_length
from jobkit.pipeline.filter import defers_to_remote_check, matched_place_bucket
from jobkit.validate.claims import never_claim_for_prompt
from jobkit.validate.provenance import constraints, corpus_hash

CORPUS_PATH = str(paths.HOME / "data" / "career_corpus.yaml")  # project root (paths.py)
COMMUTE_TABLE = "config/commute_table.yaml"
LLM_WORKERS = 4


def _int(v: object, default: int) -> int:
    """Defensive int coercion — models return null/strings where ints belong."""
    try:
        return int(v)  # type: ignore[call-overload,no-any-return]
    except (TypeError, ValueError):
        return default


def _jd_text(jd_html: str, limit: int = 12000) -> str:
    text = markdownify(jd_html) if "<" in jd_html else jd_html
    return text[:limit]


# prompt_set sales (the default): a candidate role counts toward "years in
# sales" when its title is a sales title: one the role-track classifier places
# (Filters.track_for, the one owner of what an SDR or AE is), or one that says
# "sales" ("Sales Manager", "Inside Sales Specialist"). A Program Manager role
# is not counted.
_SALES_WORD = re.compile(r"(?i)\bsales\b")
# prompt_set general: only roles whose title one of the person's tracks places
TARGET_TRACK_YEARS_LABEL = "YEARS IN ROLES ON THE TARGET TRACKS"


def month_index(ym: object) -> int | None:
    """'2019-06' -> a month count; None when the value is not YYYY-MM."""
    m = re.match(r"(\d{4})-(\d{1,2})", str(ym or ""))
    if not m:
        return None
    return int(m.group(1)) * 12 + int(m.group(2)) - 1


def _fmt_span(months: int) -> str:
    return f"{months} mo" if months < 12 else f"{months / 12:.1f} yrs"


def _profile_summary(
    *,
    corpus: dict[str, Any] | None = None,
    filters: Filters | None = None,
    today: datetime.date | None = None,
) -> str:
    """Candidate profile for the scoring prompt, from the corpus (audit
    2026-10-08 §2 item 6: the old profile had bullet text and tags only, so
    the scorer guessed tenure and seniority). Every role with its title,
    company, dates and years in role, its bullets grouped under it; total
    years in sales roles (prompt_set general: years in roles on the target
    tracks, counting only titles a track places); education; skill depth; the
    never-claim lists.

    Years are computed as of `today`, which is printed in the profile: the
    rendered prompt (and so the score cache key, llm.cache_key_for) changes
    whenever the corpus or the as-of month changes."""
    if corpus is None:
        with open(CORPUS_PATH) as f:
            corpus = yaml.safe_load(f)
    assert corpus is not None
    filters = filters or load_filters()
    today = today or datetime.date.today()
    now = today.year * 12 + today.month - 1

    roles = list(corpus.get("roles") or [])
    by_role: dict[str, list[dict[str, Any]]] = {}
    for b in corpus.get("bullets") or []:
        by_role.setdefault(str(b.get("role_id") or ""), []).append(b)

    def bullet_line(b: dict[str, Any]) -> str:
        tags = ", ".join(str(t) for t in (b.get("tags") or [])[:4])
        text = str(b.get("text_short") or b.get("text_long") or "").strip()
        return f"  - {text}" + (f" [{tags}]" if tags else "")

    general = filters.prompt_set == "general"
    lines: list[str] = []
    sales_months: set[int] = set()
    sales_titles: list[str] = []
    starts: list[int] = []
    for role in roles:
        start = month_index(role.get("start"))
        end_raw = role.get("end")
        end = month_index(end_raw) if end_raw else now
        current = not end_raw
        span = ""
        if start is not None and end is not None and end >= start:
            span = f" · {_fmt_span(end - start)}"
            starts.append(start)
        title = str(role.get("title") or "").strip()
        dates = f"{role.get('start') or '?'} – {'present (current role)' if current else end_raw}"
        lines.append(f"- {title} · {str(role.get('company') or '').strip()} · {dates}{span}")
        lines += [bullet_line(b) for b in by_role.pop(str(role.get("id") or ""), [])]
        track = filters.track_for(title) if title else None
        if general:
            counted = track is not None
        else:
            counted = bool(title) and (track is not None or bool(_SALES_WORD.search(title)))
        if counted:
            sales_titles.append(f"{title} [{track}]" if general else title)
            if start is not None and end is not None:
                sales_months.update(range(start, end))
    orphans = [b for bs in by_role.values() for b in bs]
    if orphans:
        lines.append("- (no role recorded)")
        lines += [bullet_line(b) for b in orphans]

    out = [
        f"ROLES (most recent first; years in role computed as of {today:%Y-%m}):",
        *lines,
        f"{TARGET_TRACK_YEARS_LABEL if general else 'TOTAL YEARS IN SALES ROLES'}: "
        f"{len(sales_months) / 12:.1f} "
        f"(titles counted: {'; '.join(sales_titles) or 'none'})",
    ]
    if starts:
        out.append(f"CAREER SPAN: {_fmt_span(now - min(starts))} since the earliest role")
    edu = [
        " · ".join(str(e.get(k)) for k in ("degree", "school", "years") if e.get(k))
        for e in corpus.get("education") or []
    ]
    if edu:
        out.append("EDUCATION: " + "; ".join(edu))
    skills = ", ".join(f"{k}: {v}" for k, v in (corpus.get("skill_index") or {}).items())
    if skills:
        out.append(f"SKILL DEPTH: {skills}")
    cons = constraints(corpus)
    if any(cons.values()):
        out.append(f"NEVER CLAIM (hard constraint): {never_claim_for_prompt(corpus)}")
    return "\n".join(out)


def _pmap(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    fn: Callable[[sqlite3.Connection, sqlite3.Row], dict[str, Any]],
    stage: str,
) -> list[tuple[sqlite3.Row, dict[str, Any]]]:
    """Run fn(worker_conn, row) across a thread pool. Rows whose LLM response
    is unusable are skipped and logged — one bad reply never kills the batch."""
    db_file = conn.execute("PRAGMA database_list").fetchone()["file"]

    def work(row: sqlite3.Row) -> tuple[sqlite3.Row, dict[str, Any] | None]:
        wconn = q.connect(Path(db_file))
        try:
            return row, fn(wconn, row)
        except Exception as e:  # noqa: BLE001 — isolate per-job failures
            log(f"{stage}.job_failed", job_id=row["id"], error=str(e)[:200])
            return row, None
        finally:
            wconn.close()

    with ThreadPoolExecutor(max_workers=LLM_WORKERS) as ex:
        results = list(ex.map(work, rows))
    return [(row, data) for row, data in results if data is not None]


def record_jd_missing(
    conn: sqlite3.Connection, job_id: int, stage: str, jd_html: str | None, *, reject_job: bool
) -> None:
    """Log a jd_missing refusal: one rejection row (layer=sanity), and — in
    triage, where no human has decided yet — the job's status set to rejected so
    it stops taking slots in every later run. Caller commits."""
    if reject_job:
        conn.execute(q.REJECT_JOB, {"job_id": job_id})
    conn.execute(q.RECORD_SANITY_REJECTION_ONCE, {"job_id": job_id, "reason": JD_MISSING_REASON})
    log(
        "jd_missing.refused",
        job_id=job_id,
        stage=stage,
        jd_chars=jd_text_length(jd_html or ""),
    )


def _refuse_missing_jd(
    conn: sqlite3.Connection, rows: list[sqlite3.Row], stage: str
) -> list[sqlite3.Row]:
    """Reject (layer=sanity, reason=jd_missing) and drop every row whose JD is
    too short to read. Runs before the thread pool, so no LLM call is made."""
    keep: list[sqlite3.Row] = []
    refused = 0
    for row in rows:
        if jd_is_missing(row["jd_html"]):
            record_jd_missing(conn, row["id"], stage, row["jd_html"], reject_job=True)
            refused += 1
        else:
            keep.append(row)
    if refused:
        conn.commit()
    return keep


# ---- stage 3: extract ----


def run_extract(
    conn: sqlite3.Connection, limit: int | None = None, job_ids: Collection[int] | None = None
) -> int:
    only, ids = q.only_jobs(job_ids)
    rows = conn.execute(
        f"""SELECT j.* FROM jobs j LEFT JOIN job_requirements r ON r.job_id = j.id
           WHERE j.status = 'filtered' AND r.job_id IS NULL AND j.closed_at IS NULL{only}
           ORDER BY j.first_seen_at DESC LIMIT ?""",
        (*ids, limit or 10**9),
    ).fetchall()
    rows = _refuse_missing_jd(conn, rows, "extract")
    return _extract_rows(conn, rows)


# The requirement-extraction prompt of each prompt_set. The sales one keeps its
# original name and cache key; the general one has mixed-field examples.
EXTRACT_PROMPTS = {"sales": "extract_requirements", "general": "extract_requirements_general"}


def extract_prompt_name(filters: Filters | None = None) -> str:
    """The extraction prompt file (prompts/<name>.md) for the active prompt set."""
    return EXTRACT_PROMPTS[(filters or load_filters()).prompt_set]


def _extract_rows(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> int:
    """Extract and store the requirements of these jobs (JD-less ones already
    refused by the caller). Commits; returns how many were stored."""
    name = extract_prompt_name()
    prompt = llm.PromptFile(name)
    # the sales prompt's cache key is unchanged; the general one names itself
    key_head = "extract" if name == EXTRACT_PROMPTS["sales"] else f"extract-{name}"

    def fetch(wconn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        return llm.call_json(
            wconn,
            "extract",
            prompt.render(title=row["title"], jd=_jd_text(row["jd_html"])),
            cache_key=f"{key_head}:{row['jd_hash']}:{prompt.version}",
        )

    done = 0
    for row, data in _pmap(conn, rows, fetch, "extract"):
        conn.execute(
            """INSERT OR REPLACE INTO job_requirements
               (job_id, must_have, nice_to_have, implied, disqualifier, jd_markdown,
                extracted_at, prompt_version)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?)""",
            (
                row["id"],
                json.dumps(data.get("must_have", [])),
                json.dumps(data.get("nice_to_have", [])),
                json.dumps(data.get("implied", [])),
                json.dumps(data.get("disqualifier", [])),
                _jd_text(row["jd_html"]),
                prompt.version,
            ),
        )
        done += 1
    conn.commit()
    log("extract.done", jobs=done, of=len(rows))
    return done


# ---- stage 4: remote reality check ----


# Older commute tables (written before the table was keyed by location bucket)
# give Manhattan sub-areas, not nyc-metro: an nyc-metro posting then uses the
# midtown time, as it always did. A table with an nyc-metro entry wins.
_LEGACY_BUCKET_ALIASES = {"nyc-metro": "manhattan-midtown"}


def commute_minutes(table: Mapping[str, Any], bucket: str) -> int | None:
    """Minutes from home to a location bucket in a loaded commute table
    (`buckets`, keyed by location bucket: nyc-metro, chicago-metro,
    other-columbus, ...). None when the table has no entry. `jobkit doctor`
    uses this to warn about an allowed place the table does not cover."""
    buckets = table.get("buckets", {}) or {}
    if bucket in ("remote-us", "remote-global"):
        return 0
    val = buckets.get(bucket)
    if val is None and bucket in _LEGACY_BUCKET_ALIASES:
        val = buckets.get(_LEGACY_BUCKET_ALIASES[bucket])
    return int(val) if val is not None else None


def _commute_minutes(bucket: str, remote_cfg: dict[str, Any]) -> int | None:
    """Minutes from home to a posting's location bucket, from
    config/commute_table.yaml. A missing table is a ConfigMissing error
    (config.read_config_yaml)."""
    return commute_minutes(read_config_yaml(COMMUTE_TABLE) or {}, bucket)


def geo_restriction_acceptable(geo: str, acceptable: list[str]) -> bool:
    """Is a geo restriction the LLM found ("US only", "EST hours", "Austria")
    one the candidate meets? `acceptable` is Filters.acceptable_geo_restrictions:
    the configured entries plus the candidate's own places (allowed locations,
    relocation cities and their states) — no place is hardcoded here. Each
    entry must appear as a whole word or phrase: substring matching let
    "Austria" pass on "us" and "Netherlands" on "et". Periods are ignored on
    both sides, so "U.S", "U.S." and "US" are one word."""
    gl = geo.lower().replace(".", "")
    for entry in acceptable:
        e = str(entry).strip().lower().replace(".", "")
        if e and re.search(rf"(?<![a-z0-9]){re.escape(e)}(?![a-z0-9])", gl):
            return True
    return False


def run_remote_check(
    conn: sqlite3.Connection, limit: int | None = None, job_ids: Collection[int] | None = None
) -> dict[str, int]:
    filters = load_filters()
    remote_cfg = filters.raw.get("remote_policy", {})
    relocation_buckets = filters.relocation_buckets
    acceptable_geo = filters.acceptable_geo_restrictions
    prompt = llm.PromptFile("remote_check")
    only, ids = q.only_jobs(job_ids)
    rows = conn.execute(
        f"""SELECT j.* FROM jobs j LEFT JOIN sanity s ON s.job_id = j.id
           WHERE j.status = 'filtered' AND s.job_id IS NULL AND j.closed_at IS NULL{only}
           ORDER BY j.first_seen_at DESC LIMIT ?""",
        (*ids, limit or 10**9),
    ).fetchall()
    rows = _refuse_missing_jd(conn, rows, "remote")

    def fetch(wconn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        return llm.call_json(
            wconn,
            "remote",
            prompt.render(
                title=row["title"], location=row["location_raw"] or "", jd=_jd_text(row["jd_html"])
            ),
            cache_key=f"remote:{row['jd_hash']}:{prompt.version}",
        )

    stats = {"checked": 0, "conflicts": 0, "rejected": 0}
    for row, data in _pmap(conn, rows, fetch, "remote"):
        flags: list[str] = []
        conflict = 1 if data.get("remote_conflict") else 0
        if conflict:
            flags.append(f"remote-conflict: {str(data.get('evidence', ''))[:160]}")
            stats["conflicts"] += 1
        actual = data.get("remote_actual", "unclear")
        days = _int(data.get("office_days_required"), -1)
        days_val = days if days >= 0 else None
        bucket = row["location_bucket"]
        # with filters.locations.match_state, "Columbus OH" counts as the
        # allowed "Columbus, OH" (filter.matched_place_bucket): use its bucket
        place_bucket = matched_place_bucket(row["location_raw"], filters)
        if place_bucket and _commute_minutes(bucket, remote_cfg) is None:
            bucket = place_bucket
        commute = _commute_minutes(bucket, remote_cfg)
        relocation = bucket in relocation_buckets
        # the filter passed this posting because its location only says "US"
        # or nothing: the office, if any, cannot be placed from the location
        unplaced = defers_to_remote_check(row["location_bucket"], row["location_raw"])
        reject: str | None = None
        if actual == "onsite" and remote_cfg.get("reject_onsite", True):
            reject = "sanity:onsite"
        elif days >= 0 and days > _int(remote_cfg.get("max_office_days_per_week"), 3):
            reject = f"sanity:office_days:{days}"
        elif (
            actual == "hybrid"
            and commute is not None
            and commute > int(remote_cfg.get("max_commute_minutes", 60))
            and not relocation
        ):
            reject = f"sanity:commute:{commute}min"
        elif actual == "hybrid" and commute is None and not relocation and not unplaced:
            # a named city with no commute-table entry (an ATS-flagged remote job
            # in "Tulsa, Oklahoma" that is really hybrid): not within commute
            reject = "sanity:commute:unknown"
        if unplaced and actual == "hybrid" and commute is None:
            flags.append("hybrid-office-unknown: confirm the office is within commute")
        elif unplaced and actual not in ("remote", "hybrid", "onsite"):
            flags.append(
                "remote-policy-unclear: the location names no office; "
                "confirm remote or an office within commute"
            )
        geo = data.get("geo_restriction")
        if geo and not geo_restriction_acceptable(str(geo), acceptable_geo):
            reject = reject or f"sanity:geo:{geo}"
        conn.execute(
            """INSERT OR REPLACE INTO sanity
               (job_id, remote_stated, remote_actual, remote_conflict, geo_restriction,
                office_days_required, commute_minutes, flags, checked_at, prompt_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)""",
            (
                row["id"],
                data.get("remote_stated"),
                actual,
                conflict,
                geo,
                days_val,
                commute,
                json.dumps(flags),
                prompt.version,
            ),
        )
        if reject:
            conn.execute("UPDATE jobs SET status='rejected' WHERE id=?", (row["id"],))
            conn.execute(
                "INSERT INTO rejections (job_id, layer, reason) VALUES (?, 'sanity', ?)",
                (row["id"], reject),
            )
            stats["rejected"] += 1
        stats["checked"] += 1
    conn.commit()
    log("remote.done", **stats)
    return stats


# ---- stage 5: comp sanity ----


def run_comp_check(
    conn: sqlite3.Connection, limit: int | None = None, job_ids: Collection[int] | None = None
) -> dict[str, int]:
    filters = load_filters()
    cs = filters.comp_sanity
    prompt = llm.PromptFile("comp_sanity")
    only, ids = q.only_jobs(job_ids)
    rows = conn.execute(
        f"""SELECT j.* FROM jobs j JOIN sanity s ON s.job_id = j.id
           WHERE j.status = 'filtered' AND s.demand_score IS NULL AND j.closed_at IS NULL{only}
           ORDER BY j.first_seen_at DESC LIMIT ?""",
        (*ids, limit or 10**9),
    ).fetchall()
    rows = _refuse_missing_jd(conn, rows, "comp")

    def fetch(wconn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        comp_str = (
            f"{row['comp_min']}-{row['comp_max']} {row['comp_currency']}"
            if row["comp_min"] or row["comp_max"]
            else "not published"
        )
        return llm.call_json(
            wconn,
            "comp",
            prompt.render(title=row["title"], comp=comp_str, jd=_jd_text(row["jd_html"])),
            cache_key=f"comp:{row['jd_hash']}:{prompt.version}",
        )

    stats = {"checked": 0, "flagged": 0}
    for row, data in _pmap(conn, rows, fetch, "comp"):
        published = 1 if (row["comp_min"] or row["comp_max"]) else 0
        width = None
        if row["comp_min"] and row["comp_max"] and row["comp_min"] > 0:
            width = (row["comp_max"] - row["comp_min"]) / row["comp_min"]
        required = (
            1
            if (
                row["remote"] == 1
                or any(
                    s.lower() in (row["location_raw"] or "").lower()
                    for s in (
                        "california",
                        "colorado",
                        "new york",
                        "washington",
                        "jersey",
                        "illinois",
                        "massachusetts",
                        "minnesota",
                        "maryland",
                        "hawaii",
                        "vermont",
                    )
                )
            )
            else 0
        )
        srow = conn.execute("SELECT flags FROM sanity WHERE job_id=?", (row["id"],)).fetchone()
        flags = json.loads(srow["flags"] or "[]") if srow else []
        demand = _int(data.get("demand_score"), 0)
        verdict = "unknown"
        if published and width is not None and width > cs.max_range_width_pct:
            flags.append(f"range-dodge: width {width:.0%}")
        if required and not published and cs.flag_missing_range_when_required:
            flags.append("no-range-where-required")
        if (
            published
            and demand >= 75
            and row["comp_max"]
            and row["comp_max"] < cs.underpriced_below
        ):
            verdict = "underpriced"
            flags.append(
                f"underpriced: demand {demand} (roles={data.get('breadth_roles')}) "
                f"at ceiling ${row['comp_max']:,}"
            )
        elif published:
            verdict = "fair"
        if _int(data.get("breadth_roles"), 1) >= 3:
            flags.append(f"role-sprawl: {data.get('breadth_roles')} jobs in one posting")
        basis = str(data.get("comp_basis") or "unclear")
        realism = str(data.get("ote_realism") or "n/a")
        # comp_sanity.ote_check: "always", or only titles whose floor is OTE
        ote_applies = (
            cs.ote_check == "always" or filters.comp_floor_for(row["title"]).basis == "ote"
        )
        if not ote_applies:
            pass
        elif realism == "inflated":
            flags.append(f"ote-unrealistic: {str(data.get('ote_rationale', ''))[:120]}")
        elif published and basis == "unclear" and (row["comp_max"] or 0) >= cs.ote_unclear_from:
            flags.append("ote-basis-unclear: big number, base-vs-OTE not stated")
        conn.execute(
            """UPDATE sanity SET comp_published=?, comp_required_by_law=?,
               range_width_pct=?, demand_score=?, value_verdict=?, value_rationale=?,
               flags=? WHERE job_id=?""",
            (
                published,
                required,
                width,
                demand,
                verdict,
                json.dumps(data.get("demand_factors", []))[:500],
                json.dumps(flags),
                row["id"],
            ),
        )
        if flags:
            stats["flagged"] += 1
        stats["checked"] += 1
    conn.commit()
    log("comp.done", **stats)
    return stats


# ---- stage 6: score ----

# One `score_profile` event per score written (detail: profile_hash,
# prompt_version, prompt): which corpus and which scoring prompt a score was
# made with. pipeline/rescore.py re-scores the jobs whose newest one names
# another corpus hash or another prompt. A score with no such event predates
# the record: stale. An event without "prompt" was written by score_fit.
SCORE_PROFILE_EVENT = "score_profile"
NO_CORPUS = "no-corpus"

# The scoring prompt of each prompt_set (Filters.prompt_set, absent = sales)
SALES_SCORE_PROMPT = "score_fit"
SCORE_PROMPTS = {"sales": SALES_SCORE_PROMPT, "general": "score_fit_general"}
GENERAL_SCORE_PROMPT = SCORE_PROMPTS["general"]
NO_PIVOT_NOTE = "none"
_SLOT = re.compile(r"\{([a-z_]+)\}")


def score_prompt_name(filters: Filters | None = None) -> str:
    """The scoring prompt file (prompts/<name>.md) for the active prompt set."""
    return SCORE_PROMPTS[(filters or load_filters()).prompt_set]


def render_once(pf: llm.PromptFile, **values: str) -> str:
    """Fill a prompt's {slots} in one pass: a value is never scanned again, so
    text the person wrote (corpus bullets, track ids, emphasis) that happens
    to contain "{title}" or "{pivot_note}" is sent as written. Every slot in
    the prompt must be given (an unfilled one would reach the model as
    literal text); raises ValueError naming the missing ones."""
    missing = sorted({m for m in _SLOT.findall(pf.body)} - set(values))
    if missing:
        raise ValueError(f"prompt slots not filled: {missing}")
    return _SLOT.sub(lambda m: values.get(m.group(1), m.group(0)), pf.body)


def pivot_note(title: str, filters: Filters) -> str:
    """The CAREER-CHANGE NOTE for the general score prompt: names the title's
    track when that track is marked `pivot: true`, else "none"."""
    track = filters.track_of(title)
    if track is None or not track.pivot:
        return NO_PIVOT_NOTE
    return (
        f"this job is on the candidate's {track.id} track, which they marked as a "
        "career change: they are moving into this kind of role from other work. "
        "Apply step 5 and the CAREER-CHANGE BAND."
    )


def current_profile_hash() -> str:
    """The corpus hash the scoring profile is built from (provenance.corpus_hash
    of data/career_corpus.yaml, the same hash tailoring keys on). "no-corpus"
    when there is no corpus file."""
    try:
        text = Path(CORPUS_PATH).read_text()
    except OSError:
        return NO_CORPUS
    return corpus_hash(text)


def score_rows(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    *,
    stage_label: str = "score",
    prompt: llm.PromptFile | None = None,
    profile: str | None = None,
    profile_hash: str | None = None,
    filters: Filters | None = None,
) -> dict[int, int]:
    """Score each row (a job with its extracted requirements: id, title,
    company_name, jd_hash, must_have, nice_to_have, implied, disqualifier)
    against the current profile. Writes `scores` and one score_profile event
    per job, commits, and returns {job_id: score}. A job whose reply has no
    usable score is skipped and logged. Callers refuse JD-less jobs first.

    The prompt is chosen here, so triage, rescore and extract_and_score all
    score with the active prompt set's file (score_prompt_name). prompt_set
    sales renders score_fit.md exactly as before; general fills
    score_fit_general.md in one pass (render_once) with each job's
    CAREER-CHANGE NOTE."""
    filters = filters or load_filters()
    pname = score_prompt_name(filters)
    if prompt is not None:
        pname = GENERAL_SCORE_PROMPT if "{pivot_note}" in prompt.body else SCORE_PROMPTS["sales"]
    pf: llm.PromptFile = prompt or llm.PromptFile(pname)
    general = pname == GENERAL_SCORE_PROMPT
    prof: str = _profile_summary() if profile is None else profile
    phash = profile_hash or current_profile_hash()

    def fetch(wconn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        reqs = {
            k: json.loads(row[k] or "[]")
            for k in ("must_have", "nice_to_have", "implied", "disqualifier")
        }
        if general:
            return llm.call_json(
                wconn,
                "score",
                render_once(
                    pf,
                    title=str(row["title"] or ""),
                    company=str(row["company_name"] or ""),
                    requirements=json.dumps(reqs, indent=1),
                    profile=prof,
                    pivot_note=pivot_note(str(row["title"] or ""), filters),
                ),
                cache_key=f"score:{row['jd_hash']}:{pname}:{pf.version}",
            )
        return llm.call_json(
            wconn,
            "score",
            pf.render(
                title=row["title"],
                company=row["company_name"],
                requirements=json.dumps(reqs, indent=1),
                profile=prof,
            ),
            cache_key=f"score:{row['jd_hash']}:{pf.version}",
        )

    out: dict[int, int] = {}
    for row, data in _pmap(conn, rows, fetch, stage_label):
        raw_score = _int(data.get("score"), -1)
        if raw_score < 0:
            log(f"{stage_label}.no_score", job_id=row["id"])
            continue
        score = max(0, min(100, raw_score))
        conn.execute(
            """INSERT OR REPLACE INTO scores
               (job_id, fit_score, rationale, gaps, scored_at, prompt_version)
               VALUES (?, ?, ?, ?, datetime('now'), ?)""",
            (
                row["id"],
                score,
                str(data.get("rationale", ""))[:500],
                json.dumps(data.get("gaps", [])),
                pf.version,
            ),
        )
        # the sales prompt's event keeps its original shape (no "prompt" key);
        # rescore reads a missing name as score_fit
        detail: dict[str, str] = {"profile_hash": phash, "prompt_version": pf.version}
        if pname != SALES_SCORE_PROMPT:
            detail["prompt"] = pname
        conn.execute(
            q.RECORD_EVENT,
            {"job_id": row["id"], "kind": SCORE_PROFILE_EVENT, "detail": json.dumps(detail)},
        )
        out[int(row["id"])] = score
    conn.commit()
    return out


def run_score(
    conn: sqlite3.Connection, limit: int | None = None, job_ids: Collection[int] | None = None
) -> int:
    profile = _profile_summary()
    only, ids = q.only_jobs(job_ids)
    rows = conn.execute(
        f"""SELECT j.*, r.must_have, r.nice_to_have, r.implied, r.disqualifier
           FROM jobs j JOIN job_requirements r ON r.job_id = j.id
           LEFT JOIN scores sc ON sc.job_id = j.id
           WHERE j.status = 'filtered' AND sc.job_id IS NULL AND j.closed_at IS NULL{only}
           ORDER BY j.first_seen_at DESC LIMIT ?""",
        (*ids, limit or 10**9),
    ).fetchall()
    rows = _refuse_missing_jd(conn, rows, "score")
    done = len(score_rows(conn, rows, profile=profile))
    log("score.done", jobs=done, of=len(rows))
    return done


def extract_and_score(conn: sqlite3.Connection, job_ids: list[int]) -> dict[int, int]:
    """Extract and score these jobs whatever their status. For an ATS row that
    took a human decision (approved, snoozed) from an aggregator row in a merge
    (discovery/board_resolver.py): it has no requirements and no score of its
    own, and the filter/triage stages only read status 'filtered'. A job whose
    JD is too short is skipped and logged, never rejected (a human decided
    it). Returns {job_id: score} for the jobs scored."""
    if not job_ids:
        return {}
    marks = ",".join("?" * len(job_ids))
    todo = conn.execute(
        f"""SELECT j.* FROM jobs j LEFT JOIN job_requirements r ON r.job_id = j.id
            WHERE j.id IN ({marks}) AND r.job_id IS NULL""",
        job_ids,
    ).fetchall()
    readable = [r for r in todo if not jd_is_missing(r["jd_html"])]
    for r in todo:
        if jd_is_missing(r["jd_html"]):
            log("jd_missing.skipped", job_id=r["id"], stage="extract_and_score")
    _extract_rows(conn, readable)
    rows = conn.execute(
        f"""SELECT j.*, r.must_have, r.nice_to_have, r.implied, r.disqualifier
            FROM jobs j JOIN job_requirements r ON r.job_id = j.id
            LEFT JOIN scores sc ON sc.job_id = j.id
            WHERE j.id IN ({marks}) AND sc.job_id IS NULL""",
        job_ids,
    ).fetchall()
    rows = [r for r in rows if not jd_is_missing(r["jd_html"])]
    return score_rows(conn, rows) if rows else {}


# ---- stage 8: queue ----

# The queue gate's rejection reason. pipeline/rescore.py reads it back: a job
# whose newest rejection starts with it was rejected only by the score gate.
SCORE_GATE_PREFIX = "score_below_surface:"


def score_gate_reason(score: int, min_score: int) -> str:
    return f"{SCORE_GATE_PREFIX}{score}<{min_score}"


def run_queue(
    conn: sqlite3.Connection,
    filters: Filters | None = None,
    job_ids: Collection[int] | None = None,
    *,
    require_sanity: bool = False,
) -> int:
    """The queue gate: a scored, filtered job at or above its gate is queued,
    one below it is rejected (score_below_surface). The gate is the job's
    track min_score when the track sets one, else gates.min_score_to_surface
    (Filters.min_score_for). `job_ids`: only these jobs (pipeline/same_day.py).
    `require_sanity` (only the same-day run, pipeline/same_day.py): a job
    whose remote or comp check has not finished (no sanity row, or no demand
    score, as when its LLM call failed) is left filtered for the nightly run,
    so the same-day standing rules never see an empty flag list that no
    check wrote. `jobkit triage` and run-all/nightly keep today's behaviour
    and queue such a job."""
    filters = filters or load_filters()
    only, ids = q.only_jobs(job_ids)
    rows = conn.execute(
        f"""SELECT j.id, j.title, j.jd_html, sc.fit_score,
                  s.job_id AS sanity_job_id, s.demand_score FROM jobs j
           JOIN scores sc ON sc.job_id = j.id
           LEFT JOIN sanity s ON s.job_id = j.id
           WHERE j.status = 'filtered' AND j.closed_at IS NULL{only}""",
        ids,
    ).fetchall()
    if require_sanity:
        held = [r for r in rows if r["sanity_job_id"] is None or r["demand_score"] is None]
        if held:
            log(
                "queue.sanity_incomplete",
                job_ids=[int(r["id"]) for r in held],
                note="left filtered until the remote and comp checks finish",
            )
            rows = [r for r in rows if r not in held]
    # a score written before the JD guard existed must not promote a JD-less job
    rows = _refuse_missing_jd(conn, rows, "queue")
    queued = 0
    for row in rows:
        min_score = filters.min_score_for_title(str(row["title"] or ""))
        if row["fit_score"] >= min_score:
            conn.execute("UPDATE jobs SET status='queued' WHERE id=?", (row["id"],))
            queued += 1
        else:
            conn.execute("UPDATE jobs SET status='rejected' WHERE id=?", (row["id"],))
            conn.execute(
                q.RECORD_SCORE_GATE_REJECTION,
                {"job_id": row["id"], "reason": score_gate_reason(row["fit_score"], min_score)},
            )
    conn.commit()
    log("queue.done", queued=queued)
    return queued


def rank_correlation(pairs: list[tuple[int, int]]) -> float:
    """Spearman rank correlation for the M2 acceptance check (score vs hand labels)."""
    n = len(pairs)
    if n < 2:
        return 0.0

    def ranks(xs: list[int]) -> list[float]:
        order = sorted(range(n), key=lambda i: xs[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and xs[order[j + 1]] == xs[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    sx = math.sqrt(sum((v - mx) ** 2 for v in rx))
    sy = math.sqrt(sum((v - my) ** 2 for v in ry))
    return cov / (sx * sy) if sx and sy else 0.0
