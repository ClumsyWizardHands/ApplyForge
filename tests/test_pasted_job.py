"""A pasted Greenhouse, Lever or Ashby link reaches the queue: `jobkit add-url`
polls the board, queues the job, then extracts and scores it (the filter and
triage stages read only status 'filtered', and get_queue lists only scored
jobs). MCP add_job_url runs add-url with --no-score and starts
`jobkit score-jobs <id>` in the background, so no LLM call lands in the MCP
request path. Recorded fixtures and a faked LLM; temp DB only."""

from __future__ import annotations

import functools
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from jobkit import background, cli, llm, mcp_server
from tests.conftest import load_fixture
from tests.test_claims import CORPUS

GH_JOB_ID = "4461450008"  # "Account Executive, Startups" in greenhouse_anthropic.json
URL = f"https://job-boards.greenhouse.io/anthropic/jobs/{GH_JOB_ID}"


def _transport() -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/boards/anthropic/jobs":
            return httpx.Response(200, json=load_fixture("greenhouse_anthropic.json"))
        return httpx.Response(404, json={})

    return httpx.MockTransport(handle)


class _Llm:
    def __init__(self) -> None:
        self.stages: list[str] = []

    def __call__(
        self, conn: sqlite3.Connection, stage: str, prompt: str, *, cache_key: str | None = None
    ) -> dict[str, Any]:
        self.stages.append(stage)
        if stage == "extract":
            return {
                "must_have": ["2+ yrs closing new business"],
                "nice_to_have": [],
                "implied": [],
                "disqualifier": [],
            }
        assert stage == "score", stage
        return {"score": 74, "rationale": "a. b.", "gaps": ["startup sales"]}


@pytest.fixture()
def pasted(conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch) -> _Llm:
    (jobkit_home / "data" / "career_corpus.yaml").write_text(yaml.safe_dump(CORPUS))
    fake = _Llm()
    monkeypatch.setattr(llm, "call_json", fake)
    monkeypatch.setattr(cli, "_db", lambda: conn)
    monkeypatch.setattr(
        cli, "ingest_url", functools.partial(cli.ingest_url, transport=_transport())
    )
    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    conn.executescript(mcp_server.PINS_SCHEMA)
    # a tier-1 target: the fixture's standing rules reject an AE at a tier-3 company
    conn.execute(
        "INSERT INTO companies (id, name, normalized_name, tier) "
        "VALUES (1, 'Anthropic', 'anthropic', 1)"
    )
    conn.execute(
        "INSERT INTO boards (company_id, ats, token) VALUES (1, 'greenhouse', 'anthropic')"
    )
    conn.commit()
    return fake


def _job_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT id FROM jobs WHERE external_id = ?", (GH_JOB_ID,)).fetchone()
    assert row is not None
    return int(row[0])


def test_add_url_queues_scores_and_lists_the_job(
    conn: sqlite3.Connection, pasted: _Llm, monkeypatch: pytest.MonkeyPatch
) -> None:
    res = CliRunner().invoke(cli.app, ["add-url", URL])
    assert res.exit_code == 0, res.output
    jid = _job_id(conn)
    assert f"queued: [{jid}]" in res.output and f"scored: [{jid}] fit 74" in res.output
    assert pasted.stages == ["extract", "score"]
    status = conn.execute("SELECT status FROM jobs WHERE id = ?", (jid,)).fetchone()[0]
    assert status == "queued"

    queue = mcp_server.get_queue()
    line = next(x for x in queue.splitlines() if "Account Executive, Startups" in x)
    n = line.split(".", 1)[0].strip()
    spawned: list[tuple[str, ...]] = []

    def fake_spawn(c: sqlite3.Connection, *args: str, job_id: int | None = None) -> Any:
        spawned.append(args)
        return background.Launch(1, 1, tuple(args), Path("x.log"))

    monkeypatch.setattr(background, "spawn", fake_spawn)
    mcp_server.approve(n)
    assert conn.execute("SELECT status FROM jobs WHERE id = ?", (jid,)).fetchone()[0] == (
        "approved"
    )
    assert spawned == [("tailor",)]


def test_add_url_no_score_names_the_job_and_score_jobs_scores_it(
    conn: sqlite3.Connection, pasted: _Llm
) -> None:
    res = CliRunner().invoke(cli.app, ["add-url", "--no-score", URL])
    assert res.exit_code == 0, res.output
    jid = _job_id(conn)
    assert f"needs scoring: [{jid}]" in res.output
    assert pasted.stages == []
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 0

    res = CliRunner().invoke(cli.app, ["score-jobs", str(jid)])
    assert res.exit_code == 0, res.output
    assert f"scored: [{jid}] fit 74" in res.output
    # a second run leaves the score alone and calls nothing
    res = CliRunner().invoke(cli.app, ["score-jobs", str(jid)])
    assert f"not scored: [{jid}]" in res.output and pasted.stages == ["extract", "score"]


def test_mcp_add_job_url_scores_in_the_background(
    conn: sqlite3.Connection, pasted: _Llm, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        assert argv[-2:] == ["--no-score", URL]
        out = CliRunner().invoke(cli.app, argv[argv.index("add-url") :])
        return subprocess.CompletedProcess(argv, out.exit_code, out.output, "")

    spawned: list[tuple[tuple[str, ...], int | None]] = []

    def fake_spawn(c: sqlite3.Connection, *args: str, job_id: int | None = None) -> Any:
        spawned.append((args, job_id))
        return background.Launch(7, 1, tuple(args), Path("x.log"))

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(background, "spawn", fake_spawn)
    out = mcp_server.add_job_url(URL)
    jid = _job_id(conn)
    assert spawned == [(("score-jobs", str(jid)), jid)]
    assert "launch #7" in out and "get_queue once it is scored" in out
    assert pasted.stages == []  # nothing scored in the request itself


def test_mcp_add_job_url_on_another_system_starts_nothing(
    conn: sqlite3.Connection, pasted: _Llm, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://initech.taleo.net/careersection/2/jobdetail.ftl?job=2600123&lang=en"

    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        out = CliRunner().invoke(cli.app, argv[argv.index("add-url") :])
        return subprocess.CompletedProcess(argv, out.exit_code, out.output, "")

    def no_spawn(*a: Any, **k: Any) -> Any:
        raise AssertionError("nothing to score")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(background, "spawn", no_spawn)
    out = mcp_server.add_job_url(url)
    assert "Taleo posting: " in out and "pasting details" not in out
