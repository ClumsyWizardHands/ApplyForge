"""Feeds and discovery never change a tier the user set in targets.yaml: a
feed pull that reset tier-2 companies to tier 3 would let a tier-3 standing
rule reject their jobs."""

from __future__ import annotations

from jobkit import db as q


def _company(conn, name: str):  # type: ignore[no-untyped-def]
    return conn.execute(
        "SELECT tier, domain FROM companies WHERE normalized_name=?", (name.lower(),)
    ).fetchone()


def test_feed_upsert_keeps_the_user_tier_and_domain() -> None:
    conn = q.connect(create=True)
    q.init_db(conn)
    conn.execute(
        q.UPSERT_COMPANY,
        {"name": "Globex", "normalized_name": "globex", "domain": "globex.com", "tier": 2},
    )
    conn.execute(
        q.UPSERT_COMPANY_IF_NEW,
        {"name": "Globex", "normalized_name": "globex", "domain": None, "tier": 3},
    )
    row = _company(conn, "globex")
    assert (row["tier"], row["domain"]) == (2, "globex.com")


def test_feed_upsert_adds_an_unknown_company_at_tier_3() -> None:
    conn = q.connect(create=True)
    q.init_db(conn)
    conn.execute(
        q.UPSERT_COMPANY_IF_NEW,
        {"name": "Acme", "normalized_name": "acme", "domain": None, "tier": 3},
    )
    assert _company(conn, "acme")["tier"] == 3


def test_targets_upsert_still_sets_the_tier() -> None:
    conn = q.connect(create=True)
    q.init_db(conn)
    for tier in (3, 2):
        conn.execute(
            q.UPSERT_COMPANY,
            {
                "name": "Tyrell",
                "normalized_name": "tyrell",
                "domain": "tyrell.example",
                "tier": tier,
            },
        )
    assert _company(conn, "tyrell")["tier"] == 2
