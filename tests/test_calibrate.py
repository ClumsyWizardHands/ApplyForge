"""M6 acceptance: after 50 seeded decisions, calibration surfaces at least one
real filter pattern, one standing-rule candidate, and the top corpus gap — as
numbered proposals requiring explicit acceptance. Offline, seeded data."""

from __future__ import annotations

import json
import sqlite3

from jobkit.onboarding.calibrate import calibrate
from tests.test_filter import _insert


def _seed_50_decisions(conn: sqlite3.Connection) -> None:
    # 20 hands-on approvals of tier-1 remote jobs scoring 85-93 (rule candidate)
    for i in range(20):
        jid = _insert(conn, external_id=f"a{i}", title=f"Director of Sales {i}", remote=1)
        conn.execute("UPDATE jobs SET status='approved' WHERE id=?", (jid,))
        score = 85 + (i % 9)
        conn.execute(
            "INSERT INTO scores (job_id, fit_score, gaps) VALUES (?, ?, ?)",
            (
                jid,
                score,
                json.dumps(
                    ["Kubernetes production experience"]
                    if i % 2 == 0
                    else ["developer-tools domain"]
                ),
            ),
        )
        conn.execute(
            """INSERT INTO decisions (job_id, decision, reason, score_at_decision, auto)
               VALUES (?, 'approve', 'human', ?, 0)""",
            (jid, score),
        )

    # 18 hands-on rejections all citing on-call rotations (filter pattern)
    for i in range(18):
        jid = _insert(conn, external_id=f"r{i}", title=f"Sales Lead {i}", remote=1)
        conn.execute("UPDATE jobs SET status='rejected' WHERE id=?", (jid,))
        conn.execute(
            "INSERT INTO scores (job_id, fit_score, gaps) VALUES (?, ?, '[]')", (jid, 70 + (i % 5))
        )
        conn.execute(
            """INSERT INTO decisions (job_id, decision, reason, score_at_decision, auto)
               VALUES (?, 'reject', 'requires weekend oncall rotation', ?, 0)""",
            (jid, 70 + (i % 5)),
        )

    # 12 auto decisions to reach 50
    for i in range(12):
        jid = _insert(conn, external_id=f"z{i}", title=f"AE {i}")
        conn.execute(
            """INSERT INTO decisions (job_id, decision, reason, score_at_decision, auto)
               VALUES (?, 'reject', 'ae-ceiling-too-low', 60, 1)""",
            (jid,),
        )
    conn.execute("UPDATE companies SET tier=1 WHERE id=1")
    conn.commit()


def test_calibration_surfaces_all_three_signals(conn: sqlite3.Connection) -> None:
    _seed_50_decisions(conn)
    cal = calibrate(conn)
    assert cal.decisions == 50
    text = cal.render()
    # numbered proposals
    assert "1." in text and "2." in text
    # (a) a real filter pattern from repeated rejection reasons
    assert any("oncall" in p or "rotation" in p for p in cal.proposals), cal.proposals
    # (b) a standing-rule candidate from repeated approvals
    assert any("auto_approve" in p and "tier-1" in p for p in cal.proposals), cal.proposals
    # (c) the top corpus gap
    assert any("kubernetes" in p.lower() for p in cal.proposals), cal.proposals
    # explicit-acceptance framing, never auto-applied
    assert "explicit per-item acceptance" in text


def test_calibration_proposes_nothing_from_thin_data(conn: sqlite3.Connection) -> None:
    cal = calibrate(conn)
    assert cal.decisions == 0
    assert "meaningful at 50+" in cal.render()
