"""No fixed cap on how many jobs are scored per run (owner decision D2, 2026-10-22).

Every job that passes the filters is extracted, checked and scored in the
run: run-all and the nightly run (cli._run_all_stages) and same-day scoring
had a fixed 100 (and `jobkit triage` a default of 25) per LLM stage. A limit
is now the person's choice, filters.yaml triage.max_jobs_per_stage (absent:
none), and `jobkit triage --limit N` overrides it for one run.

The LLM boundary is a fake backend (tests/test_triage.py's canned answers
over the real llm.call). Fictional companies only; offline."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest
from typer.testing import CliRunner

from jobkit import cli, llm
from jobkit.config import Filters, load_filters, parse_max_jobs_per_stage
from jobkit.pipeline import same_day, triage
from tests.conftest import REPO_ROOT
from tests.test_filter import FILTERS, _insert
from tests.test_triage import CANNED, REAL_JD

OLD_CAP = 100  # the fixed per-stage number run-all and the nightly run used


@pytest.fixture()
def llm_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake_backend(stage: str, prompt: str) -> tuple[str, dict[str, Any]]:
        calls.append(stage)
        return json.dumps(CANNED[stage]), {}

    monkeypatch.setattr(llm, "_call_claude_cli", fake_backend)
    monkeypatch.setattr(triage, "_profile_summary", lambda: "EXPERIENCE: test profile")
    monkeypatch.setattr(triage, "load_filters", lambda: FILTERS)
    return calls


def _with_limit(monkeypatch: pytest.MonkeyPatch, n: int) -> None:
    raw = {**FILTERS.raw, "triage": {"max_jobs_per_stage": n}}
    monkeypatch.setattr(triage, "load_filters", lambda: Filters(raw=raw))


def _filtered_jobs(conn: sqlite3.Connection, n: int, prefix: str = "job") -> list[int]:
    ids = []
    for i in range(n):
        jid = _insert(
            conn, external_id=f"{prefix}-{i}", jd_html=REAL_JD, jd_hash=f"{prefix}-hash-{i}"
        )
        conn.execute("UPDATE jobs SET status = 'filtered' WHERE id = ?", (jid,))
        ids.append(jid)
    conn.commit()
    return ids


def _scored(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0])


def _llm_stages(stages: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    return [(n, fn) for n, fn in stages if n in ("extract", "remote", "comp", "score")]


def test_the_nightly_and_run_all_stages_score_every_filtered_job(
    conn: sqlite3.Connection, llm_calls: list[str]
) -> None:
    """More jobs than the old fixed cap pass the filters: all are scored in
    the one run, with no limit configured."""
    n = OLD_CAP + 5
    _filtered_jobs(conn, n)
    for _name, fn in _llm_stages(cli._run_all_stages()):
        fn(conn)
    assert _scored(conn) == n
    assert llm_calls.count("score") == n
    assert int(conn.execute("SELECT COUNT(*) FROM sanity").fetchone()[0]) == n


def test_an_optional_limit_in_filters_yaml_is_honoured(
    conn: sqlite3.Connection, llm_calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _filtered_jobs(conn, 7)
    _with_limit(monkeypatch, 3)
    for _name, fn in _llm_stages(cli._run_all_stages()):
        fn(conn)
    assert llm_calls.count("extract") == 3
    assert _scored(conn) == 3
    # the next run takes the next ones
    for _name, fn in _llm_stages(cli._run_all_stages()):
        fn(conn)
    assert _scored(conn) == 6


def test_same_day_scoring_has_no_cap_and_honours_the_limit(
    conn: sqlite3.Connection, llm_calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _filtered_jobs(conn, 4)
    for _name, fn in _llm_stages(same_day.stages(ids)):
        fn(conn)
    assert _scored(conn) == 4
    more = _filtered_jobs(conn, 3, prefix="later")
    _with_limit(monkeypatch, 1)
    for _name, fn in _llm_stages(same_day.stages(more)):
        fn(conn)
    assert _scored(conn) == 5


def test_jobkit_triage_has_no_default_limit_and_limit_overrides(
    conn: sqlite3.Connection, llm_calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _filtered_jobs(conn, 30)  # more than the old --limit default of 25
    monkeypatch.setattr(cli, "_db", lambda: conn)
    monkeypatch.setattr(cli, "run_queue", lambda c: {"queued": 0})
    runner = CliRunner()
    res = runner.invoke(cli.app, ["triage", "--limit", "2"])
    assert res.exit_code == 0, res.output
    assert _scored(conn) == 2
    res = runner.invoke(cli.app, ["triage"])
    assert res.exit_code == 0, res.output
    assert _scored(conn) == 30
    # the flag also overrides a configured limit
    _filtered_jobs(conn, 4, prefix="extra")
    _with_limit(monkeypatch, 1)
    res = runner.invoke(cli.app, ["triage", "--limit", "3"])
    assert res.exit_code == 0, res.output
    assert _scored(conn) == 33
    res = runner.invoke(cli.app, ["triage"])  # the configured limit
    assert res.exit_code == 0, res.output
    assert _scored(conn) == 34
    assert runner.invoke(cli.app, ["triage", "--limit", "0"]).exit_code != 0


def test_the_stage_limit_setting_is_validated() -> None:
    assert parse_max_jobs_per_stage(None) is None
    assert parse_max_jobs_per_stage({}) is None
    assert parse_max_jobs_per_stage({"max_jobs_per_stage": None}) is None
    assert parse_max_jobs_per_stage({"max_jobs_per_stage": 40}) == 40
    for bad in (0, -1, True, 2.5, "40"):
        with pytest.raises(ValueError, match="max_jobs_per_stage"):
            parse_max_jobs_per_stage({"max_jobs_per_stage": bad})
    with pytest.raises(ValueError, match="triage"):
        parse_max_jobs_per_stage(["max_jobs_per_stage"])
    # a bad value fails when filters.yaml loads, not mid-run
    with pytest.raises(ValueError, match="max_jobs_per_stage"):
        Filters(raw={**FILTERS.raw, "triage": {"max_jobs_per_stage": 0}})
    assert FILTERS.max_jobs_per_stage is None


@pytest.mark.parametrize("name", ["filters.example.yaml", "filters.sales.example.yaml"])
def test_the_templates_set_no_limit(name: str) -> None:
    assert load_filters(REPO_ROOT / "config" / name).max_jobs_per_stage is None
