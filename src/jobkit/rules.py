"""Standing rules engine (SPEC §6.2). Evaluation order is fixed: deterministic
filters → sanity checks → standing rules. Rules only ever see jobs that already
survived the first two layers (status=queued). Auto-approved jobs are tailored
and staged without asking; auto-rejects are logged, reviewable, never dropped."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from jobkit.adapters.ats.greenhouse import comp_basis
from jobkit.config import Filters, load_filters, read_config_yaml
from jobkit.log import log

RULES_PATH = "config/autoapprove.yaml"

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
        else:
            raise ValueError(f"unknown standing-rule key: {key} (one knob, one owner)")
    return True


def _load_rules() -> dict[str, Any]:
    """config/autoapprove.yaml; a missing file is a ConfigMissing error."""
    rules: dict[str, Any] = read_config_yaml(RULES_PATH) or {}
    return rules


def run_standing_rules(conn: sqlite3.Connection) -> dict[str, int]:
    """Apply autoapprove.yaml to the queue. Returns counts."""
    rules = _load_rules()
    filters = load_filters()
    unknown = unknown_title_classes(rules, filters)
    if unknown:
        log(
            "standing_rules.unknown_title_class",
            title_classes=unknown,
            tracks=filters.track_ids,
            note="a rule naming one of these never matches",
        )
    rows = conn.execute(
        """SELECT j.id, j.title, j.remote, j.comp_min, j.comp_max, c.tier,
                  sc.fit_score, s.flags, fs.friction_score AS friction,
                  CASE WHEN j.comp_max IS NOT NULL THEN j.jd_html END AS jd_html
           FROM jobs j
           JOIN companies c ON c.id = j.company_id
           LEFT JOIN scores sc ON sc.job_id = j.id
           LEFT JOIN sanity s ON s.job_id = j.id
           LEFT JOIN form_specs fs ON fs.job_id = j.id
           WHERE j.status = 'queued'"""
    ).fetchall()
    stats = {"evaluated": len(rows), "auto_approved": 0, "auto_rejected": 0}
    for row in rows:
        job = dict(row)
        job["flags"] = json.loads(row["flags"] or "[]") if row["flags"] else []
        job["title_class"] = _title_class(row["title"], filters)
        job["comp_basis"] = comp_basis(row["jd_html"], row["comp_min"], row["comp_max"])
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
        for rule in rules.get("auto_approve", []):
            if _cond_matches(rule["when"], job):
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
