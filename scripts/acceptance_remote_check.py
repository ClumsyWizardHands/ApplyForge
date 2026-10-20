#!/usr/bin/env python3
"""M2 acceptance: 10 hand-built deceptive postings, >=8 must be caught by the
remote reality check. LIVE LLM run (not a unit test — tests stay offline).
Run: uv run python scripts/acceptance_remote_check.py"""

from __future__ import annotations

import sqlite3

from jobkit import llm

# Each tuple: (name, stated location field, JD body). All ten are deceptive.
DECEPTIVE = [
    (
        "remote-but-3-days",
        "Remote",
        "This is a remote role. Note: team members are expected in our NYC office "
        "three days per week for collaboration.",
    ),
    (
        "remote-first-50-miles",
        "Remote - US",
        "We are remote-first! Candidates must reside within 50 miles of San "
        "Francisco and attend weekly onsite Wednesdays.",
    ),
    (
        "remote-actually-hybrid",
        "Remote",
        "Work from home flexibility. This is a hybrid position based out of our "
        "Austin headquarters.",
    ),
    (
        "remote-until-90-days",
        "Fully Remote",
        "Start fully remote. After your first 90 days you will transition to four "
        "days per week in the office.",
    ),
    (
        "wfa-commutable-london",
        "Work from anywhere",
        "Work from anywhere culture. You must be within commutable distance of our "
        "London office for regular in-person sessions.",
    ),
    (
        "hybrid-actually-onsite",
        "Hybrid - Chicago",
        "Hybrid schedule. In practice the team is onsite five days a week and we "
        "expect the same from this hire.",
    ),
    (
        "remote-ca-only",
        "Remote",
        "Remote position. Applicants must be current California residents; "
        "out-of-state candidates cannot be considered.",
    ),
    (
        "remote-leadership-presence",
        "Remote optional",
        "Remote optional. Leadership strongly values consistent in-person presence "
        "and meets daily in the Denver office.",
    ),
    (
        "remote-est-in-office",
        "Remote (US)",
        "100% remote! Mandatory in-office anchor days are Tuesday through Thursday "
        "at our Boston hub.",
    ),
    (
        "remote-quarterly-plus-monthly",
        "Remote",
        "A remote role with quarterly offsites. Additionally, all hands are held "
        "monthly at HQ and attendance is required in person.",
    ),
]


def main() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    prompt = llm.PromptFile("remote_check")
    hits = 0
    for name, loc, jd in DECEPTIVE:
        data = llm.call_json(
            conn, "remote", prompt.render(title="Director of Sales", location=loc, jd=jd)
        )
        conflict = bool(data.get("remote_conflict"))
        geo = data.get("geo_restriction")
        stated, actual = data.get("remote_stated"), data.get("remote_actual")
        caught = conflict or (stated != actual) or (name == "remote-ca-only" and geo)
        hits += bool(caught)
        print(
            f"{'CAUGHT' if caught else 'MISSED':6} {name}: stated={stated} "
            f"actual={actual} conflict={conflict} geo={geo}"
        )
    print(f"\n{hits}/10 deceptive postings detected (acceptance: >=8)")
    raise SystemExit(0 if hits >= 8 else 1)


if __name__ == "__main__":
    main()
