"""Standing rules engine (SPEC §6.2). Evaluation order is fixed: deterministic
filters → sanity checks → standing rules. Rules only ever see jobs that already
survived the first two layers (status=queued). Auto-approved jobs are tailored
and staged without asking; auto-rejects are logged, reviewable, never dropped.

There is no daily limit on automatic approval unless the person sets one:
`max_auto_approve_per_day` in autoapprove.yaml (a whole number, 1 or more).
With it, at most that many jobs are approved automatically per local calendar
day, highest fit score first. A job over the limit stays queued, gets one
`auto_approve_waiting` event that day, and is tried again on the next run.
Without the key nothing here changes.

Three conditions keep the doubtful jobs for the person (owner decisions D3, D4 and D6, 2026-10-22):

* `blocking_flags: none` — no sanity flag that is a real deal-breaker:
  doubts about remote work or the location, and unrealistic pay. Which flag
  classes block is set in filters.yaml `auto_approve` (config.AutoApproveFlags);
  a flag of a class no list names blocks. Unlike `sanity_flags: none`, a
  missing pay range, role sprawl, an unclear base-vs-OTE basis or a wide
  range does not keep a job from being approved.
* `location_known: true` — the remote check ran and its verdict is clear:
  both what the posting states and what its fine print implies are remote,
  hybrid or onsite, never unclear.
* `employer_posting: true` — the job is the employer's own Greenhouse, Lever
  or Ashby posting, not a feed (aggregator) row that board resolution could
  not match to one. Such a row has no form to fill; it stays in the queue
  marked "no employer form found: apply on the employer's site (found via
  <link>)" (employer_site_note), and
  the person can still approve it by hand. A job on a stale board or a
  listed repost site is not an employer posting either (D5).

D4 and D6 say "never", so the last two hold whatever a rule says
(review 2026-10-23): run_standing_rules approves no job whose location is
not known or that is not an employer posting, even under a rule that lacks
those conditions (an owner's older rules). Such a job stays queued for the
person, logged as `standing_rules.held_for_person`; auto_reject rules still
apply to it. Writing the two conditions into a rule states this; leaving
them out does not weaken it."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from typing import Any

import yaml

from jobkit.adapters.ats.greenhouse import comp_basis
from jobkit.config import AutoApproveFlags, Filters, load_filters, read_config_yaml
from jobkit.discovery import reposters
from jobkit.links import strip_tracking
from jobkit.log import log

RULES_PATH = "config/autoapprove.yaml"

# the optional daily limit on automatic approvals; absent means no limit
MAX_PER_DAY_KEY = "max_auto_approve_per_day"
# the event a job over the daily limit gets (once per job per local day)
WAITING_EVENT = "auto_approve_waiting"

# automatic approvals decided on a local calendar day ('YYYY-MM-DD')
_COUNT_AUTO_APPROVED_ON = """
SELECT COUNT(*) AS n FROM decisions
WHERE auto = 1 AND decision = 'approve' AND date(decided_at, 'localtime') = :day
"""
# jobs that cleared an auto_approve rule on that day but were over the daily
# limit, and are still queued
_COUNT_WAITING_ON = """
SELECT COUNT(DISTINCT e.job_id) AS n FROM events e JOIN jobs j ON j.id = e.job_id
WHERE e.kind = :kind AND date(e.at, 'localtime') = :day AND j.status = 'queued'
"""
_WAITING_RECORDED_ON = """
SELECT 1 FROM events
WHERE job_id = :job_id AND kind = :kind AND date(at, 'localtime') = :day
"""
_TODAY = "SELECT date('now', 'localtime') AS day"


def max_auto_approve_per_day(rules: Mapping[str, Any]) -> int | None:
    """The optional daily limit on automatic approvals
    (`max_auto_approve_per_day`). None when the key is absent or empty: no
    limit, the default. A value that is not a whole number of 1 or more
    (true, 0, -3, 2.5, "five") raises ValueError."""
    value = rules.get(MAX_PER_DAY_KEY)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(
            f"{MAX_PER_DAY_KEY} in config/autoapprove.yaml must be a whole number of 1 or "
            f"more, got {value!r}. Delete the line for no daily limit."
        )
    return value


# the remote check's verdicts that say where the work happens
KNOWN_REMOTE_VERDICTS = frozenset({"remote", "hybrid", "onsite"})


def location_known(job: Mapping[str, Any]) -> bool:
    """The remote check ran (a sanity row) and neither its stated nor its
    actual verdict is unclear (or missing)."""
    if job.get("sanity_job_id") is None:
        return False
    return (
        job.get("remote_stated") in KNOWN_REMOTE_VERDICTS
        and job.get("remote_actual") in KNOWN_REMOTE_VERDICTS
    )


def is_employer_posting(job: Mapping[str, Any]) -> bool:
    """The job came from an employer's own board (it has a board). A feed
    (aggregator) row has none until board resolution merges it into the
    employer's posting (rule 10), which closes the feed row.

    Fail closed on a repost site (D5, review 2026-10-23): a board no longer
    polled (`board_status` 'stale': gone, or kept out as a repost site by
    discovery/reposters.keep_out) and a board or company on the built-in
    repost list (registries/reposters.yml) are not the employer's own
    posting, so a job on one already queued is never approved automatically."""
    if job.get("board_id") is None:
        return False
    if job.get("board_status") == "stale":
        return False
    ats, token = str(job.get("board_ats") or ""), str(job.get("board_token") or "")
    if ats and token and reposters.listed_board(ats, token):
        return False
    return reposters.named(str(job.get("company_name") or "")) is None


def employer_site_note(board_id: object, apply_url: object) -> str:
    """' · no employer form found: apply on the employer's site (found via
    <link>)' for a feed row with no employer posting found (no form jobkit
    can fill), else ''. The link is the feed's page for the job, not the
    employer's site. The queue (MCP get_queue) and the digest's review queue
    add it to the job's line."""
    if board_id is not None:
        return ""
    link = strip_tracking(str(apply_url or ""))  # no utm_* or click ids (SPEC §16.6)
    note = " · no employer form found: apply on the employer's site"
    return note + (f" (found via {link})" if link else "")


def _blocking(job: dict[str, Any], flags_cfg: AutoApproveFlags | None) -> list[str]:
    if "blocking_flags" in job:
        return list(job["blocking_flags"])
    cfg = flags_cfg or load_filters().auto_approve_flags
    return cfg.blocking_in(list(job.get("flags") or []))


def _true_or_false(key: str, want: object) -> bool:
    if not isinstance(want, bool):
        raise ValueError(f"standing-rule key {key} takes true or false, got {want!r}")
    return want


_FOUNDING_PAT = re.compile(r"\b(founding|first)\b", re.I)

# The one track with a carve-out (see _title_class). An SDR/BDR title that
# mentions an AE ("SDR (AE track)") is placed by its own, earlier track.
AE_TRACK = "account_executive"


def _title_class(title: str, filters: Filters | None = None) -> str | None:
    """The standing-rules title class: the title's track id, read from the
    filters.yaml track classifier (Filters.track_for), the one owner of what
    each track is. A title on no track has no class (None).

    One carve-out, for account_executive only: a founding or first AE has no
    class, so the AE rules never auto-reject the good deal. An SDR/BDR title
    keeps its own track id and is never account_executive."""
    track = (filters or load_filters()).track_for(title)
    if track == AE_TRACK and _FOUNDING_PAT.search(title):
        return None
    return track


def title_classes_in(rules: dict[str, Any]) -> list[str]:
    """Every `title_class` value named in an autoapprove.yaml mapping
    (auto_approve and auto_reject, any_of included), in order, deduplicated."""
    found: list[str] = []

    def walk(when: Any) -> None:
        if not isinstance(when, dict):
            return
        for key, want in when.items():
            if key == "title_class":
                found.append(str(want))
            elif key == "any_of" and isinstance(want, list):
                for w in want:
                    walk(w)

    for section in ("auto_approve", "auto_reject"):
        for rule in rules.get(section) or []:
            if isinstance(rule, dict):
                walk(rule.get("when"))
    return list(dict.fromkeys(found))


def unknown_title_classes(rules: dict[str, Any], filters: Filters) -> list[str]:
    """The title_class values that name no track id. Such a rule never
    matches; run_standing_rules logs a warning and `jobkit doctor` reports it."""
    ids = set(filters.track_ids)
    return [c for c in title_classes_in(rules) if c not in ids]


def _cond_matches(when: dict[str, Any], job: dict[str, Any]) -> bool:
    for key, want in when.items():
        if key == "any_of":
            if not any(_cond_matches(w, job) for w in want):
                return False
        elif key == "tier":
            if job["tier"] != want:
                return False
        elif key == "score_gte":
            if job["fit_score"] is None or job["fit_score"] < want:
                return False
        elif key == "score_lt":
            if job["fit_score"] is None or job["fit_score"] >= want:
                return False
        elif key == "sanity_flags":
            if want == "none" and job["flags"]:
                return False
        elif key == "sanity_checked":
            # both sanity checks finished: the remote check wrote the row and
            # the comp check its demand_score. A job whose check failed has
            # no flags to read, so `sanity_flags: none` alone would pass it.
            checked = job.get("sanity_job_id") is not None and job.get("demand_score") is not None
            if checked != bool(want):
                return False
        elif key == "friction_lte":
            if job["friction"] is None or job["friction"] > want:
                return False
        elif key == "remote":
            if bool(job["remote"]) != bool(want):
                return False
        elif key == "comp_min_gte":
            # an OTE minimum says nothing about base: an auto-approve on it
            # would tailor and stage on a number that is not salary
            if job["comp_min"] is None or job["comp_min"] < want:
                return False
            if job.get("comp_basis") == "ote":
                return False
        elif key == "comp_max_lt":
            if job["comp_max"] is None or job["comp_max"] >= want:
                return False
        elif key == "title_class":
            cls = job["title_class"] if "title_class" in job else _title_class(job["title"])
            if cls != want:
                return False
        elif key == "flag_present":
            if not any(str(want) in f for f in job["flags"]):
                return False
        elif key == "comp_unpublished":
            if bool(job["comp_max"] is None) != bool(want):
                return False
        elif key == "blocking_flags":
            # only the flags filters.yaml auto_approve counts as deal-breakers
            if want != "none":
                raise ValueError(f"standing-rule key blocking_flags takes only none, got {want!r}")
            if _blocking(job, None):
                return False
        elif key == "location_known":
            if location_known(job) != _true_or_false(key, want):
                return False
        elif key == "employer_posting":
            if is_employer_posting(job) != _true_or_false(key, want):
                return False
        else:
            raise ValueError(f"unknown standing-rule key: {key} (one knob, one owner)")
    return True


def _load_rules() -> dict[str, Any]:
    """config/autoapprove.yaml; a missing file is a ConfigMissing error, and a
    bad max_auto_approve_per_day a ValueError."""
    rules: dict[str, Any] = read_config_yaml(RULES_PATH) or {}
    if isinstance(rules, dict):
        max_auto_approve_per_day(rules)
    return rules


def _today(conn: sqlite3.Connection) -> str:
    return str(conn.execute(_TODAY).fetchone()["day"])


def _record_waiting(conn: sqlite3.Connection, job_id: int, day: str, cap: int) -> None:
    """One auto_approve_waiting event per job per local day: get_queue runs
    the rules too, so the same job can be over the limit several times."""
    args = {"job_id": job_id, "kind": WAITING_EVENT, "day": day}
    if conn.execute(_WAITING_RECORDED_ON, args).fetchone():
        return
    conn.execute(
        "INSERT INTO events (job_id, kind, detail) VALUES (?, ?, ?)",
        (job_id, WAITING_EVENT, f"daily limit of {cap} reached; tried again on the next run"),
    )


def waiting_for_tomorrow(conn: sqlite3.Connection, day: str | None = None) -> int:
    """How many still-queued jobs cleared an auto_approve rule on `day` (local
    'YYYY-MM-DD', default today) but were over the daily limit."""
    day = day or _today(conn)
    return int(conn.execute(_COUNT_WAITING_ON, {"kind": WAITING_EVENT, "day": day}).fetchone()["n"])


def auto_approved_on(conn: sqlite3.Connection, day: str | None = None) -> int:
    """How many jobs the standing rules approved on `day` (local 'YYYY-MM-DD',
    default today)."""
    day = day or _today(conn)
    return int(conn.execute(_COUNT_AUTO_APPROVED_ON, {"day": day}).fetchone()["n"])


def digest_lines(conn: sqlite3.Connection, day: str | None = None) -> list[str]:
    """The digest's "Automatic approval" section. Empty unless the person set
    a daily limit (max_auto_approve_per_day): then how many jobs were approved
    automatically that day against the limit, and how many more cleared their
    bar and wait in the queue for the next day's run. A missing or unreadable
    autoapprove.yaml (missing, a YAML syntax error, a bad limit) gives nothing
    here: the rules stage reports it, and the digest is still written."""
    try:
        rules = read_config_yaml(RULES_PATH) or {}
        cap = max_auto_approve_per_day(rules) if isinstance(rules, dict) else None
    except (OSError, ValueError, yaml.YAMLError):
        return []
    if cap is None:
        return []
    day = day or _today(conn)
    approved = auto_approved_on(conn, day)
    waiting = waiting_for_tomorrow(conn, day)
    line = f"Approved automatically today: {approved} (your daily limit is {cap})."
    if waiting:
        line += (
            f" {waiting} more cleared your bar and wait for tomorrow's run. They are in "
            "your queue, and you can approve any of them now."
        )
    return ["## Automatic approval", line, ""]


def run_standing_rules(conn: sqlite3.Connection) -> dict[str, int]:
    """Apply autoapprove.yaml to the queue. Returns counts. With a daily
    limit set (max_auto_approve_per_day), jobs are tried highest fit score
    first, approvals stop at the limit for the local day, and the counts gain
    `waiting_for_tomorrow`: jobs that cleared an auto_approve rule and stay
    queued."""
    rules = _load_rules()
    cap = max_auto_approve_per_day(rules)
    filters = load_filters()
    flags_cfg = filters.auto_approve_flags
    unknown = unknown_title_classes(rules, filters)
    if unknown:
        log(
            "standing_rules.unknown_title_class",
            title_classes=unknown,
            tracks=filters.track_ids,
            note="a rule naming one of these never matches",
        )
    rows = conn.execute(
        """SELECT j.id, j.title, j.remote, j.comp_min, j.comp_max, c.tier, j.board_id,
                  j.company_name, b.status AS board_status, b.ats AS board_ats,
                  b.token AS board_token,
                  sc.fit_score, s.flags, s.job_id AS sanity_job_id, s.demand_score,
                  s.remote_stated, s.remote_actual,
                  fs.friction_score AS friction,
                  CASE WHEN j.comp_max IS NOT NULL THEN j.jd_html END AS jd_html
           FROM jobs j
           JOIN companies c ON c.id = j.company_id
           LEFT JOIN boards b ON b.id = j.board_id
           LEFT JOIN scores sc ON sc.job_id = j.id
           LEFT JOIN sanity s ON s.job_id = j.id
           LEFT JOIN form_specs fs ON fs.job_id = j.id
           WHERE j.status = 'queued'"""
    ).fetchall()
    stats = {"evaluated": len(rows), "auto_approved": 0, "auto_rejected": 0}
    day = ""
    approved_today = 0
    if cap is not None:
        # the limit goes to the best jobs: highest fit first, unscored last
        rows = sorted(
            rows,
            key=lambda r: (r["fit_score"] is None, -(r["fit_score"] or 0), r["id"]),
        )
        day = _today(conn)
        approved_today = auto_approved_on(conn, day)
        stats["waiting_for_tomorrow"] = 0
    for row in rows:
        job = dict(row)
        job["flags"] = json.loads(row["flags"] or "[]") if row["flags"] else []
        job["title_class"] = _title_class(row["title"], filters)
        job["comp_basis"] = comp_basis(row["jd_html"], row["comp_min"], row["comp_max"])
        job["blocking_flags"] = flags_cfg.blocking_in(job["flags"])
        decided = False
        for rule in rules.get("auto_reject", []):
            if _cond_matches(rule["when"], job):
                conn.execute("UPDATE jobs SET status='rejected' WHERE id=?", (row["id"],))
                conn.execute(
                    "INSERT INTO rejections (job_id, layer, reason) VALUES (?, 'standing_rule', ?)",
                    (row["id"], rule.get("reason", "standing-rule")),
                )
                conn.execute(
                    """INSERT INTO decisions (job_id, decision, reason, score_at_decision,
                       friction_at_decision, auto) VALUES (?, 'reject', ?, ?, ?, 1)""",
                    (row["id"], rule.get("reason", ""), row["fit_score"], row["friction"]),
                )
                stats["auto_rejected"] += 1
                decided = True
                break
        if decided:
            continue
        if not (location_known(job) and is_employer_posting(job)):
            # D4/D6 hold whatever a rule says: a job whose location is not
            # known, or with no employer form, is never approved automatically
            # (review 2026-10-23: a rule without these conditions approved
            # one); it stays queued for the person
            if rules.get("auto_approve"):
                log(
                    "standing_rules.held_for_person",
                    job_id=row["id"],
                    location_known=location_known(job),
                    employer_posting=is_employer_posting(job),
                )
            continue
        for rule in rules.get("auto_approve", []):
            if _cond_matches(rule["when"], job):
                if cap is not None and approved_today >= cap:
                    # over the person's daily limit: left queued for the next run
                    _record_waiting(conn, row["id"], day, cap)
                    log(
                        "standing_rules.auto_approve_waiting",
                        job_id=row["id"],
                        fit_score=row["fit_score"],
                        reason=rule.get("reason", ""),
                        max_per_day=cap,
                    )
                    stats["waiting_for_tomorrow"] += 1
                    break
                approved_today += 1
                conn.execute("UPDATE jobs SET status='approved' WHERE id=?", (row["id"],))
                # one application row per job: never a second one
                if not conn.execute(
                    "SELECT 1 FROM applications WHERE job_id=?", (row["id"],)
                ).fetchone():
                    conn.execute(
                        "INSERT INTO applications (job_id, stage) VALUES (?, 'approved')",
                        (row["id"],),
                    )
                conn.execute(
                    """INSERT INTO decisions (job_id, decision, reason, score_at_decision,
                       friction_at_decision, auto) VALUES (?, 'approve', ?, ?, ?, 1)""",
                    (row["id"], rule.get("reason", ""), row["fit_score"], row["friction"]),
                )
                stats["auto_approved"] += 1
                break
    conn.commit()
    log("standing_rules.done", **stats)
    return stats
