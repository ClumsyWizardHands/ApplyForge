"""Regression tests for the 2026-09-02 external-audit fixes."""

from __future__ import annotations

import sqlite3

from jobkit.autofill.answer_bank import BankMatcher
from jobkit.autofill.browser import _identity_value, apply_url_allowed
from jobkit.pipeline.filter import unsnooze
from jobkit.validate.authenticity import _normalize_verdict
from tests.test_filter import _insert

BANK = {
    "identity": {
        "first_name": "Jordan",
        "last_name": "Rivera",
        "city": "Bristol",
        "state": "PA",
        "country": "United States",
        "email": "a@x.com",
        "phone": "+1",
        "linkedin": "li",
    },
}


def test_first_name_never_gets_full_name() -> None:
    m = BankMatcher(bank={"match_threshold": 0.9}, use_embeddings=False)
    assert m.match("First Name")["entry_id"] == "first_name"
    assert m.match("Legal First and Last Name")["entry_id"] == "name"
    assert m.match("Full name")["entry_id"] == "name"
    assert _identity_value("first_name", BANK) == "Jordan"
    assert _identity_value("name", BANK) == "Jordan Rivera"


def test_typed_address_components() -> None:
    m = BankMatcher(bank={"match_threshold": 0.9}, use_embeddings=False)
    assert m.match("City")["entry_id"] == "city"
    assert m.match("State")["entry_id"] == "state"
    assert m.match("Country")["entry_id"] == "country"
    assert m.match("ZIP / Postal Code")["entry_id"] == "postal_code"
    assert _identity_value("city", BANK) == "Bristol"
    assert _identity_value("postal_code", BANK) is None, "unset must stay unfilled, never guessed"


def test_apply_url_allowlist() -> None:
    assert apply_url_allowed("https://boards.greenhouse.io/acme/jobs/1", None)
    assert apply_url_allowed("https://jobs.ashbyhq.com/acme/x", None)
    assert apply_url_allowed("https://careers.acme.com/apply", "acme.com")
    assert not apply_url_allowed("https://evil-collector.example/apply", "acme.com")
    assert not apply_url_allowed("http://boards.greenhouse.io/acme", None), "https only"
    assert not apply_url_allowed("https://notgreenhouse.io.evil.example/x", None)


def test_critic_verdict_schema_constrained() -> None:
    assert _normalize_verdict("pass") == "pass"
    assert _normalize_verdict("FAIL") == "fail"
    # outside the schema = fail closed (audit 2026-10-08: missing used to mean pass)
    assert _normalize_verdict("looks fine to me") == "fail"
    assert _normalize_verdict(None) == "fail"
    assert _normalize_verdict("") == "fail"


def test_unsnooze_wakes_expired_only(conn: sqlite3.Connection) -> None:
    past = _insert(conn, external_id="s1")
    future = _insert(conn, external_id="s2")
    for jid, until in ((past, "2020-01-01"), (future, "2099-01-01")):
        conn.execute("UPDATE jobs SET status='snoozed' WHERE id=?", (jid,))
        conn.execute(
            "INSERT INTO events (job_id, kind, detail) VALUES (?, 'snoozed_until', ?)",
            (jid, until),
        )
    conn.commit()
    assert unsnooze(conn) == 1
    assert (
        conn.execute("SELECT status FROM jobs WHERE id=?", (past,)).fetchone()["status"] == "queued"
    )
    assert (
        conn.execute("SELECT status FROM jobs WHERE id=?", (future,)).fetchone()["status"]
        == "snoozed"
    )
