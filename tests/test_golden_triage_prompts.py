"""Golden prompts for the extract and comp-sanity stages on the sales path.

With `prompt_set` absent (the sales test profile in tests/fixtures/config/),
the requirement-extraction and comp-sanity prompts, and their cache keys, are
byte-identical to snapshots taken from the code before prompt_set existed
(62db5cb). Both stages were reworked this round (extract picks a prompt per
prompt_set; the comp check reads CompSanity), so these pin what the LLM is
sent, not only what comes back. Regenerate only on a deliberate change to the
sales path: JOBKIT_REGEN_GOLDEN=1 pytest tests/test_golden_triage_prompts.py.

Offline: every LLM call is faked at llm.call_json."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from jobkit import llm
from jobkit.pipeline import triage
from tests.test_filter import _insert
from tests.test_prompt_set import COMP_ROWS, JD, SCORE_JOBS, FakeCalls, _comp_rows, _golden

EXTRACT_REPLY = {"must_have": ["a"], "nice_to_have": [], "implied": [], "disqualifier": []}


def test_golden_extract_prompts(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    for i, (title, company, _must) in enumerate(SCORE_JOBS):
        jid = _insert(
            conn,
            external_id=f"e{i}",
            title=title,
            company_name=company,
            jd_html=JD,
            jd_hash=f"golden-extract-{i}",
        )
        conn.execute("UPDATE jobs SET status='filtered' WHERE id=?", (jid,))
    conn.commit()
    fake = FakeCalls({"extract": EXTRACT_REPLY})
    monkeypatch.setattr(llm, "call_json", fake)
    assert triage.run_extract(conn) == len(SCORE_JOBS)
    _golden("sales_extract_prompts.json", sorted(fake.calls, key=lambda c: c["cache_key"]))


def test_golden_comp_prompts(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    by_hash = _comp_rows(conn, COMP_ROWS)
    calls: list[dict[str, str]] = []

    def fake(
        c: sqlite3.Connection, stage: str, prompt: str, *, cache_key: str | None = None
    ) -> dict[str, Any]:
        assert cache_key
        calls.append({"stage": stage, "cache_key": cache_key, "prompt": prompt})
        return dict(by_hash[cache_key.split(":")[1]])

    monkeypatch.setattr(llm, "call_json", fake)
    triage.run_comp_check(conn)
    assert len(calls) == len(COMP_ROWS)
    _golden("sales_comp_prompts.json", sorted(calls, key=lambda c: c["cache_key"]))
