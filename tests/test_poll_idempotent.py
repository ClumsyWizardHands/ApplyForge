"""M1 acceptance in miniature: polling twice creates zero duplicates, closed_at
works, and JD text survives the round trip. Offline — fixtures only."""

from __future__ import annotations

import json
import sqlite3

from jobkit.config import Board, Company, Targets
from jobkit.pipeline.poll import seed_targets, upsert_jobs
from tests.conftest import load_fixture

TARGETS = Targets(
    companies=[
        Company(
            name="Anthropic",
            domain="anthropic.com",
            tier=1,
            board=Board(ats="greenhouse", token="anthropic"),
        ),
        Company(
            name="Palantir",
            domain="palantir.com",
            tier=3,
            board=Board(ats="lever", token="palantir"),
        ),
        Company(name="Ramp", domain="ramp.com", tier=1, board=Board(ats="ashby", token="ramp")),
    ]
)

FIXTURE_BY_ATS = {
    "greenhouse": "greenhouse_anthropic.json",
    "lever": "lever_palantir.json",
    "ashby": "ashby_ramp.json",
}


def _poll_once(conn: sqlite3.Connection) -> None:
    for board in seed_targets(conn, TARGETS):
        upsert_jobs(conn, board, load_fixture(FIXTURE_BY_ATS[board["ats"]]))
    conn.commit()


def test_double_poll_zero_duplicates(conn: sqlite3.Connection) -> None:
    _poll_once(conn)
    first = conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]
    assert first > 0
    _poll_once(conn)
    second = conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]
    assert second == first, "re-poll must create zero duplicates"
    dupes = conn.execute(
        "SELECT board_id, external_id, COUNT(*) c FROM jobs "
        "GROUP BY board_id, external_id HAVING c > 1"
    ).fetchall()
    assert not dupes


def test_full_jd_text_stored(conn: sqlite3.Connection) -> None:
    _poll_once(conn)
    short = conn.execute("SELECT COUNT(*) c FROM jobs WHERE length(jd_html) <= 200").fetchone()["c"]
    assert short == 0, "every stored job must carry full JD text"


def test_vanished_posting_marked_closed(conn: sqlite3.Connection) -> None:
    _poll_once(conn)
    board = next(b for b in seed_targets(conn, TARGETS) if b["ats"] == "greenhouse")
    payload = load_fixture(FIXTURE_BY_ATS["greenhouse"])
    assert isinstance(payload, dict)
    removed = payload["jobs"].pop()  # one posting vanishes
    upsert_jobs(conn, board, payload)
    conn.commit()
    row = conn.execute(
        "SELECT closed_at, status FROM jobs WHERE board_id=? AND external_id=?",
        (board["board_id"], str(removed["id"])),
    ).fetchone()
    assert row["closed_at"] is not None and row["status"] == "closed"
    # and it reopens if it comes back (repost)
    payload["jobs"].append(removed)
    upsert_jobs(conn, board, payload)
    conn.commit()
    row = conn.execute(
        "SELECT closed_at FROM jobs WHERE board_id=? AND external_id=?",
        (board["board_id"], str(removed["id"])),
    ).fetchone()
    assert row["closed_at"] is None


def test_sources_provenance(conn: sqlite3.Connection) -> None:
    _poll_once(conn)
    rows = conn.execute("SELECT COUNT(*) c FROM sources WHERE is_canonical = 1").fetchone()
    assert rows["c"] == conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]


def test_cluster_key_populated(conn: sqlite3.Connection) -> None:
    _poll_once(conn)
    empty = conn.execute(
        "SELECT COUNT(*) c FROM jobs WHERE cluster_key = '' OR cluster_key IS NULL"
    ).fetchone()["c"]
    assert empty == 0
    sample = conn.execute("SELECT cluster_key FROM jobs LIMIT 1").fetchone()["cluster_key"]
    assert len(json.dumps(sample.split("|"))) > 2 and sample.count("|") == 2
