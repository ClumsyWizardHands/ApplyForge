"""Phase 3 — calibration (ONBOARDING.md). The intake captured what you SAID you
wanted; the decisions log records what you ACTUALLY want. This reconciles them.

Pure SQL/aggregation — no LLM. Proposals are numbered and NEVER applied;
the human accepts each one by editing config (or telling Claude to).
Calibration proposes; the human disposes (hard rule 7)."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class Calibration:
    decisions: int = 0
    proposals: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        head = f"Calibration over {self.decisions} decisions"
        if self.decisions < 50:
            head += " (meaningful at 50+ — treat these as early signals)"
        lines = [head, ""]
        for i, p in enumerate(self.proposals, 1):
            lines.append(f"{i}. {p}")
        if self.notes:
            lines += ["", "Observations:"] + [f"- {n}" for n in self.notes]
        lines += ["", "No change is applied without your explicit per-item acceptance."]
        return "\n".join(lines)


def _word_patterns(reasons: list[str], min_count: int = 5) -> list[tuple[str, int]]:
    words: Counter[str] = Counter()
    for r in reasons:
        for w in re.findall(r"[a-z-]{4,}", r.lower()):
            if w not in ("human", "reason", "score", "sanity", "standing"):
                words[w] += 1
    return [(w, c) for w, c in words.most_common(8) if c >= min_count]


def calibrate(conn: sqlite3.Connection) -> Calibration:
    cal = Calibration()
    cal.decisions = conn.execute("SELECT COUNT(*) c FROM decisions").fetchone()["c"]

    # 1) consistent human rejections the filters missed → filter proposals
    rej = conn.execute(
        """SELECT d.reason, j.title FROM decisions d JOIN jobs j ON j.id=d.job_id
           WHERE d.decision='reject' AND d.auto=0"""
    ).fetchall()
    for word, count in _word_patterns([r["reason"] or "" for r in rej]):
        cal.proposals.append(
            f"You've rejected {count} postings citing '{word}'. Add a matching "
            f"exclude/filter entry to config/filters.yaml?"
        )

    # 2) score calibration: approvals vs rejections by band
    bands = conn.execute(
        """SELECT decision, AVG(score_at_decision) avg_s, COUNT(*) n,
                  MIN(score_at_decision) lo, MAX(score_at_decision) hi
           FROM decisions WHERE score_at_decision IS NOT NULL AND auto=0
           GROUP BY decision"""
    ).fetchall()
    stats = {r["decision"]: r for r in bands}
    app, rjc = stats.get("approve"), stats.get("reject")
    if app and app["n"] >= 5 and (app["hi"] - app["lo"]) <= 12:
        cal.proposals.append(
            f"Your {app['n']} approvals cluster at {app['lo']}-{app['hi']} — the rubric "
            f"is compressed. Re-anchor prompts/score_fit.md with three of your own "
            f"recent decisions as worked examples?"
        )
    if app and rjc and app["avg_s"] is not None and rjc["avg_s"] is not None:
        cal.notes.append(
            f"avg score at approval {app['avg_s']:.0f} vs rejection {rjc['avg_s']:.0f} "
            f"({app['n']} approvals / {rjc['n']} rejections)"
        )

    # 3) standing-rule candidates from repeated hands-on approvals
    pattern_rows = conn.execute(
        """SELECT c.tier, j.remote, COUNT(*) n, MIN(d.score_at_decision) min_s
           FROM decisions d JOIN jobs j ON j.id=d.job_id
           JOIN companies c ON c.id=j.company_id
           WHERE d.decision='approve' AND d.auto=0 AND d.score_at_decision IS NOT NULL
           GROUP BY c.tier, j.remote HAVING n >= 8"""
    ).fetchall()
    for r in pattern_rows:
        remote_txt = "remote" if r["remote"] == 1 else "any-location"
        cal.proposals.append(
            f"You've approved every tier-{r['tier']} {remote_txt} posting scoring "
            f">={r['min_s']} without hesitation, {r['n']} times. Add an auto_approve "
            f"rule for that pattern to config/autoapprove.yaml?"
        )

    # 4) dead targets
    dead = conn.execute(
        """SELECT c.name FROM companies c JOIN boards b ON b.company_id=c.id
           WHERE c.tier <= 2 AND NOT EXISTS (
             SELECT 1 FROM jobs j WHERE j.company_id=c.id
               AND j.first_seen_at > datetime('now','-90 days'))"""
    ).fetchall()
    if dead:
        names = ", ".join(r["name"] for r in dead[:6])
        cal.proposals.append(
            f"{len(dead)} tier-1/2 companies posted nothing in 90 days ({names}"
            f"{'…' if len(dead) > 6 else ''}). Drop to tier 3 or remove?"
        )

    # 5) top corpus gap across all scored jobs
    gap_counter: Counter[str] = Counter()
    for g in conn.execute("SELECT gaps FROM scores WHERE gaps IS NOT NULL AND gaps != '[]'"):
        for item in json.loads(g["gaps"]):
            key = str(item)[:70].lower().strip()
            gap_counter[key] += 1
    if gap_counter:
        top, n = gap_counter.most_common(1)[0]
        total = conn.execute("SELECT COUNT(*) c FROM scores").fetchone()["c"]
        cal.proposals.append(
            f"Top corpus gap: '{top}' appeared in {n} of {total} scored postings. "
            f"Add corpus evidence for it (or accept it as out of scope)?"
        )

    # 6) funnel diagnosis
    queued = conn.execute(
        "SELECT COUNT(*) c FROM jobs WHERE status IN ('queued','approved','applied')"
    ).fetchone()["c"]
    rejected_h = conn.execute(
        "SELECT COUNT(*) c FROM decisions WHERE decision='reject' AND auto=0"
    ).fetchone()["c"]
    if queued and rejected_h > queued:
        cal.notes.append("queue-to-approve rejection rate is high — filters may be too loose")
    return cal
