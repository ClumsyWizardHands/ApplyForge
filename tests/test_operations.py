"""Operations and MCP truthfulness (audit 2026-10-08 §2 items 7-8, §3 items 2, 5, 6).

* one project root: absolute data paths, no silent empty DB, .env loaded;
* a staleness banner on the digest, get_queue and get_stats;
* run-all stages isolated, recorded, and failing the exit code;
* follow-ups: drafted through MCP, mark_submitted by job id, no
  hiring-manager note without a contact, outcomes recorded;
* background launches recorded with pid, output log and real exit status; one
  batched staging process; open_queue includes needs_human;
* ATS-over-feed merge, on poll and as a one-off.

Offline, fixture DB only (hard rule 12): no spawn runs, no LLM is called."""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

import jobkit
from jobkit import background, llm, paths
from jobkit import db as q
from jobkit.config import Board, Company, Targets, load_filters
from jobkit.pipeline.dedup import merge_feed_dupes, posting_key
from jobkit.pipeline.digest import freshness_banner, write_digest
from jobkit.pipeline.poll import seed_targets, upsert_jobs
from jobkit.pipeline.runner import run_stages
from jobkit.track.followups import process_followups
from tests.conftest import REPO_ROOT, SpawnRecorder, load_fixture
from tests.test_filter import _insert

TODAY = datetime.date.today()


def _days_ago(n: int) -> str:
    return (TODAY - datetime.timedelta(days=n)).isoformat()


def _mcp(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> Any:
    from jobkit import mcp_server

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    monkeypatch.setattr(mcp_server, "run_standing_rules", lambda c: {"evaluated": 0})
    conn.executescript(mcp_server.PINS_SCHEMA)
    return mcp_server


def _board(conn: sqlite3.Connection, polled_sql: str | None) -> None:
    conn.execute(
        "INSERT INTO companies (id, name, normalized_name) VALUES (1, 'Acme', 'acme') "
        "ON CONFLICT DO NOTHING"
    )
    conn.execute(
        "INSERT INTO boards (company_id, ats, token, last_polled_at) VALUES "
        f"(1, 'greenhouse', 'acme', {polled_sql or 'NULL'})"
    )
    conn.commit()


# ---------------------------------------------------------------------------
# one project root, no silent empty DB, .env loaded
# ---------------------------------------------------------------------------


def test_home_is_jobkit_home_when_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JOBKIT_HOME", str(tmp_path))
    assert paths.resolve_home() == tmp_path.resolve()


def test_home_is_the_checkout_without_jobkit_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOBKIT_HOME")
    monkeypatch.chdir("/")  # the working directory no longer matters
    assert paths.resolve_home() == REPO_ROOT


def test_connect_never_creates_a_db_by_accident(tmp_path: Path) -> None:
    missing = tmp_path / "elsewhere" / "data" / "jobkit.db"
    with pytest.raises(q.DatabaseMissing, match="jobkit init"):
        q.connect(missing)
    assert not missing.exists() and not missing.parent.exists()
    q.connect(missing, create=True).close()
    assert missing.exists()
    q.connect(Path(":memory:")).close()  # in-memory needs no file


def test_cli_refuses_missing_db_and_init_creates_it(jobkit_home: Path) -> None:
    from jobkit.cli import app

    db_file = jobkit_home / "data" / "jobkit.db"
    runner = CliRunner()
    res = runner.invoke(app, ["stats"])
    assert res.exit_code == 1
    assert "no jobkit database" in res.output and "jobkit init" in res.output
    assert not db_file.exists()
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert db_file.exists()
    res = runner.invoke(app, ["stats"])
    assert res.exit_code == 0, res.output
    assert res.output.startswith("⚠ STALE DATA: no successful board poll")


def test_cli_loads_dotenv_without_overriding_the_shell(
    jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit.cli import app

    # setenv-then-delenv registers the undo, so nothing leaks past this test
    for var in ("JOBKIT_TEST_FROM_DOTENV", "JOBKIT_TEST_SHELL_WINS"):
        monkeypatch.setenv(var, "x")
        monkeypatch.delenv(var)
    monkeypatch.setenv("JOBKIT_TEST_SHELL_WINS", "shell")
    (jobkit_home / ".env").write_text(
        "JOBKIT_TEST_FROM_DOTENV=from-dotenv\nJOBKIT_TEST_SHELL_WINS=dotenv\n"
    )
    assert CliRunner().invoke(app, ["init"]).exit_code == 0
    assert os.environ["JOBKIT_TEST_FROM_DOTENV"] == "from-dotenv"
    assert os.environ["JOBKIT_TEST_SHELL_WINS"] == "shell"


def test_mcp_main_enters_the_root_and_loads_dotenv(
    jobkit_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from jobkit import mcp_server

    monkeypatch.setenv("JOBKIT_TEST_MCP_DOTENV", "x")
    monkeypatch.delenv("JOBKIT_TEST_MCP_DOTENV")
    (jobkit_home / ".env").write_text("JOBKIT_TEST_MCP_DOTENV=1\n")
    ran: list[bool] = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda *a, **k: ran.append(True))
    monkeypatch.chdir(tmp_path)  # Claude Code started the server somewhere else
    mcp_server.main()
    assert ran and Path.cwd() == jobkit_home.resolve()
    assert os.environ["JOBKIT_TEST_MCP_DOTENV"] == "1"


def _mcp_server_cfg(path: Path) -> dict[str, Any]:
    cfg: dict[str, Any] = json.loads(path.read_text())["mcpServers"]["jobkit"]
    return cfg


def test_mcp_example_config_runs_from_source() -> None:
    cfg = _mcp_server_cfg(REPO_ROOT / ".mcp.example.json")
    args = cfg["args"]
    assert cfg["command"] == "uv" and args[0] == "run" and args[-1] == "jobkit-mcp"
    assert "--no-sync" not in args and "--directory" in args
    assert "UV_NO_EDITABLE" not in cfg.get("env", {})


def test_mcp_config_is_gitignored() -> None:
    # it holds an absolute path for one machine; only the example is tracked
    lines = (REPO_ROOT / ".gitignore").read_text().splitlines()
    assert ".mcp.json" in [line.strip() for line in lines]


def test_mcp_config_runs_from_source_in_the_repo() -> None:
    cfg_path = REPO_ROOT / ".mcp.json"
    if not cfg_path.exists():
        pytest.skip(".mcp.json is machine-local (gitignored)")
    cfg = _mcp_server_cfg(cfg_path)
    args = cfg["args"]
    assert "--no-sync" not in args and "--directory" in args
    assert "UV_NO_EDITABLE" not in cfg.get("env", {})
    assert Path(args[args.index("--directory") + 1]).is_dir()


# ---------------------------------------------------------------------------
# staleness banner
# ---------------------------------------------------------------------------


def test_banner_when_nothing_was_ever_polled(conn: sqlite3.Connection) -> None:
    assert freshness_banner(conn, 36).startswith("⚠ STALE DATA: no successful board poll")


def test_banner_warns_past_the_threshold(conn: sqlite3.Connection) -> None:
    _board(conn, "datetime('now', '-35 days')")
    banner = freshness_banner(conn, 36)
    assert banner.startswith("⚠ STALE DATA: last successful poll 35 days ago")
    assert "more than 36 hours" in banner


def test_banner_quiet_when_fresh(conn: sqlite3.Connection) -> None:
    _board(conn, "datetime('now', '-2 hours')")
    banner = freshness_banner(conn, 36)
    assert banner.startswith("Last successful poll 2 hours ago")
    assert "⚠" not in banner


def test_banner_threshold_comes_from_filters_yaml(conn: sqlite3.Connection) -> None:
    assert load_filters().stale_after_hours == 36
    _board(conn, "datetime('now', '-40 hours')")
    assert freshness_banner(conn).startswith("⚠ STALE DATA: last successful poll 40 hours ago")


def test_digest_queue_and_stats_lead_with_the_banner(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _board(conn, "datetime('now', '-35 days')")
    lines = write_digest(conn).read_text().splitlines()
    assert lines[0].startswith("# jobkit digest") and lines[2].startswith("⚠ STALE DATA")
    mcp = _mcp(conn, monkeypatch)
    assert mcp.get_queue().startswith("⚠ STALE DATA: last successful poll 35 days ago")
    assert mcp.get_stats().startswith("⚠ STALE DATA: last successful poll 35 days ago")


# ---------------------------------------------------------------------------
# run-all: isolated stages, recorded outcomes, non-zero exit
# ---------------------------------------------------------------------------


def _failing_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(stage: str, prompt: str) -> tuple[str, dict[str, Any]]:
        raise RuntimeError("backend down")

    monkeypatch.setattr(llm, "_call_claude_cli", down)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)


def test_run_stages_isolates_and_records(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _failing_llm(monkeypatch)
    ran: list[str] = []

    def boom(c: sqlite3.Connection) -> object:
        ran.append("b")
        c.execute("INSERT INTO companies (name, normalized_name) VALUES ('Half', 'half')")
        raise RuntimeError("missing prompt file")

    def triage_like(c: sqlite3.Connection) -> object:
        ran.append("c")
        with contextlib.suppress(RuntimeError):  # like _pmap: a failed job is skipped
            llm.call(c, "score", "rate this job")
        return {"scored": 0}

    out = run_stages(
        conn,
        [
            ("a", lambda c: ran.append("a") or "fine"),
            ("b", boom),
            ("c", triage_like),
            ("d", lambda c: ran.append("d") or "after"),
        ],
        run_id="run-1",
    )
    assert ran == ["a", "b", "c", "d"], "a failing stage must not stop the rest"
    assert [o.status for o in out] == ["ok", "error", "llm_all_failed", "ok"]
    assert "missing prompt file" in (out[1].error or "")
    assert (out[2].llm_answered, out[2].llm_failed) == (0, 1)
    # the failed stage's partial write was rolled back
    assert conn.execute("SELECT COUNT(*) FROM companies WHERE name='Half'").fetchone()[0] == 0
    rows = [
        json.loads(r["detail"])
        for r in conn.execute("SELECT detail FROM events WHERE kind='run_all.stage' ORDER BY id")
    ]
    assert [(r["stage"], r["status"]) for r in rows] == [
        ("a", "ok"),
        ("b", "error"),
        ("c", "llm_all_failed"),
        ("d", "ok"),
    ]
    assert {r["run"] for r in rows} == {"run-1"}
    done = conn.execute("SELECT detail FROM events WHERE kind='run_all.done'").fetchone()
    assert json.loads(done["detail"])["failed"] == ["b", "c"]
    banner = freshness_banner(conn, 36)
    assert "had failed stages: b (error), c (all LLM calls failed)" in banner


def test_stage_with_some_llm_answers_is_ok(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = iter([RuntimeError("flaky"), RuntimeError("flaky"), RuntimeError("flaky")])

    def backend(stage: str, prompt: str) -> tuple[str, dict[str, Any]]:
        err = next(calls, None)
        if err:
            raise err
        return '{"ok": true}', {}

    monkeypatch.setattr(llm, "_call_claude_cli", backend)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    def two_jobs(c: sqlite3.Connection) -> object:
        for _ in range(2):
            with contextlib.suppress(RuntimeError):
                llm.call(c, "score", "rate")
        return "done"

    (out,) = run_stages(conn, [("score", two_jobs)])
    assert out.status == "ok" and (out.llm_answered, out.llm_failed) == (1, 1)


def test_cli_run_all_runs_every_stage_and_exits_nonzero(
    jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import cli

    c = q.connect(create=True)
    q.init_db(c)
    c.close()

    def poll_down(conn: sqlite3.Connection) -> object:
        raise ConnectionError("network unreachable")

    monkeypatch.setattr(
        cli, "_run_all_stages", lambda: [("poll", poll_down), ("digest", write_digest)]
    )
    res = CliRunner().invoke(cli.app, ["run-all"])
    assert res.exit_code == 1
    assert "poll: FAILED — ConnectionError: network unreachable" in res.output
    assert "1 stage(s) failed: poll" in res.output
    digest = (jobkit_home / "out" / "digest" / f"{TODAY.isoformat()}.md").read_text()
    assert "had failed stages: poll (error)" in digest  # the digest still ran, and says so

    monkeypatch.setattr(cli, "_run_all_stages", lambda: [("digest", write_digest)])
    assert CliRunner().invoke(cli.app, ["run-all"]).exit_code == 0


def test_run_all_stage_list_includes_followups_before_digest() -> None:
    from jobkit import cli

    names = [n for n, _ in cli._run_all_stages()]
    assert names[0] == "poll" and names[-1] == "digest"
    assert names.index("followups") == len(names) - 2


# ---------------------------------------------------------------------------
# submissions, follow-ups, outcomes
# ---------------------------------------------------------------------------


def _applied(conn: sqlite3.Connection, ext: str = "s1", stage: str = "tailored") -> int:
    jid = _insert(conn, external_id=ext, title="Account Executive")
    conn.execute("UPDATE jobs SET status='approved' WHERE id=?", (jid,))
    conn.execute("INSERT INTO applications (job_id, stage) VALUES (?, ?)", (jid, stage))
    conn.commit()
    return jid


def _followups(conn: sqlite3.Connection, jid: int) -> dict[str, tuple[str, str]]:
    return {
        r["kind"]: (r["due_at"], r["status"])
        for r in conn.execute(
            "SELECT f.kind, f.due_at, f.status FROM followups f "
            "JOIN applications a ON a.id=f.application_id WHERE a.job_id=?",
            (jid,),
        )
    }


def test_mark_submitted_by_job_id_without_a_pinned_queue(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = _mcp(conn, monkeypatch)
    jid = _applied(conn)
    day = _days_ago(10)
    out = mcp.mark_submitted(job_ids=str(jid), submitted_on=day)
    assert f"job {jid} Acme — Account Executive: submitted {day}" in out
    assert "hiring_manager_note not scheduled (no-contact-known)" in out
    app = conn.execute("SELECT stage, submitted_at FROM applications WHERE job_id=?", (jid,))
    assert tuple(app.fetchone()) == ("submitted", day)
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "applied"
    base = datetime.date.fromisoformat(day)
    assert _followups(conn, jid) == {
        "status_check": ((base + datetime.timedelta(days=7)).isoformat(), "pending"),
        "close_as_ghosted": ((base + datetime.timedelta(days=30)).isoformat(), "pending"),
    }
    # idempotent: a second call changes nothing and schedules nothing new
    assert "already marked submitted" in mcp.mark_submitted(job_ids=str(jid))
    assert conn.execute("SELECT COUNT(*) FROM followups").fetchone()[0] == 2
    ev = conn.execute("SELECT detail FROM events WHERE kind='marked_submitted'").fetchall()
    assert len(ev) == 1 and json.loads(ev[0]["detail"])["submitted_on"] == day


def test_mark_submitted_schedules_the_note_when_a_contact_is_known(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = _mcp(conn, monkeypatch)
    jid = _applied(conn)
    conn.execute("INSERT INTO contacts (company_id, name) VALUES (1, 'Pat Hiring')")
    mcp.mark_submitted(job_ids=str(jid))
    due = _followups(conn, jid)
    assert set(due) == {"status_check", "hiring_manager_note", "close_as_ghosted"}
    assert due["hiring_manager_note"][0] == (TODAY + datetime.timedelta(days=14)).isoformat()


def test_mark_submitted_still_takes_pinned_numbers(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = _mcp(conn, monkeypatch)
    jid = _applied(conn)
    conn.execute("INSERT INTO queue_pins (n, job_id) VALUES (4, ?)", (jid,))
    assert "#4 Acme" in mcp.mark_submitted("4")


def test_mark_submitted_rejects_bad_input_and_changes_nothing(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = _mcp(conn, monkeypatch)
    jid = _applied(conn)
    assert "no job with id [999]" in mcp.mark_submitted(job_ids=f"{jid}, 999")
    future = (TODAY + datetime.timedelta(days=1)).isoformat()
    assert "in the future" in mcp.mark_submitted(job_ids=str(jid), submitted_on=future)
    assert "YYYY-MM-DD" in mcp.mark_submitted(job_ids=str(jid), submitted_on="09/04")
    assert "give queue numbers" in mcp.mark_submitted()
    assert conn.execute("SELECT COUNT(*) FROM followups").fetchone()[0] == 0
    assert conn.execute("SELECT submitted_at FROM applications").fetchone()[0] is None


def _write_corpus(home: Path) -> None:
    corpus = {"bullets": [{"id": "b_001", "text_short": "Grew pipeline ~30%."}]}
    (home / "data" / "career_corpus.yaml").write_text(yaml.safe_dump(corpus))


def _due_followups(conn: sqlite3.Connection, jid: int, kinds: dict[str, int]) -> None:
    app_id = conn.execute("SELECT id FROM applications WHERE job_id=?", (jid,)).fetchone()[0]
    conn.execute(
        "UPDATE applications SET stage='submitted', submitted_at=? WHERE id=?",
        (_days_ago(20), app_id),
    )
    conn.execute("UPDATE jobs SET status='applied' WHERE id=?", (jid,))
    for kind, offset in kinds.items():
        conn.execute(
            "INSERT INTO followups (application_id, kind, due_at) VALUES (?, ?, ?)",
            (app_id, kind, (TODAY + datetime.timedelta(days=offset)).isoformat()),
        )
    conn.commit()


def test_process_followups_skips_notes_with_no_contact_and_drafts_the_rest(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, jobkit_home: Path
) -> None:
    _write_corpus(jobkit_home)
    jid = _applied(conn)
    _due_followups(
        conn, jid, {"status_check": -5, "hiring_manager_note": -1, "close_as_ghosted": 9}
    )
    prompts: list[str] = []

    def fake_call_json(c: Any, stage: str, prompt: str, **kw: Any) -> dict[str, Any]:
        prompts.append(prompt)
        return {"subject": "Timeline?", "body": "Still keen.\nAny update?"}

    monkeypatch.setattr(llm, "call_json", fake_call_json)
    stats = process_followups(conn)
    assert stats == {"drafted": 1, "ghost_closed": 0, "skipped_no_contact": 1, "failed": 0}
    assert len(prompts) == 1 and "status_check" in prompts[0]
    rows = {r["kind"]: r for r in conn.execute("SELECT kind, status, draft FROM followups")}
    assert rows["hiring_manager_note"]["status"] == "skipped"
    assert rows["hiring_manager_note"]["draft"] is None
    assert json.loads(rows["status_check"]["draft"])["subject"] == "Timeline?"
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='followup_skipped'").fetchone()[0]


def test_followup_prompt_carries_the_estimate_style_and_the_name(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, jobkit_home: Path
) -> None:
    corpus = {
        "identity": {"name": "Jordan Rivera"},
        "bullets": [{"id": "b_001", "text_short": "Grew pipeline ~30%."}],
    }
    (jobkit_home / "data" / "career_corpus.yaml").write_text(yaml.safe_dump(corpus))
    jid = _applied(conn)
    _due_followups(conn, jid, {"status_check": -1})
    prompts: list[str] = []

    def fake_call_json(c: Any, stage: str, prompt: str, **kw: Any) -> dict[str, Any]:
        prompts.append(prompt)
        return {"subject": "s", "body": "b"}

    monkeypatch.setattr(llm, "call_json", fake_call_json)
    monkeypatch.setattr("jobkit.validate.style.configured_estimate_style", lambda: "confident")
    process_followups(conn)
    assert len(prompts) == 1
    assert "CANDIDATE NAME: Jordan Rivera" in prompts[0]
    assert "ESTIMATE STYLE: confident" in prompts[0]
    assert "{name}" not in prompts[0] and "{estimate_style}" not in prompts[0]


def test_process_followups_one_failed_draft_does_not_stop_the_others(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, jobkit_home: Path
) -> None:
    _write_corpus(jobkit_home)
    a, b = _applied(conn, "f1"), _applied(conn, "f2")
    _due_followups(conn, a, {"status_check": -3})
    _due_followups(conn, b, {"status_check": -2})
    replies: list[Any] = [RuntimeError("llm down"), {"subject": "S", "body": "B"}]

    def flaky(c: Any, stage: str, prompt: str, **kw: Any) -> dict[str, Any]:
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return dict(r)

    monkeypatch.setattr(llm, "call_json", flaky)
    stats = process_followups(conn)
    assert (stats["drafted"], stats["failed"]) == (1, 1)


def test_get_followups_shows_drafts_and_starts_drafting_the_rest(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, spawned: SpawnRecorder
) -> None:
    mcp = _mcp(conn, monkeypatch)
    a, b = _applied(conn, "g1"), _applied(conn, "g2")
    _due_followups(conn, a, {"status_check": -4})
    _due_followups(conn, b, {"status_check": -1})
    conn.execute(
        "UPDATE followups SET draft=? WHERE id=1",
        (json.dumps({"subject": "Quick check-in", "body": "Line one.\nLine two."}),),
    )
    conn.commit()
    out = mcp.get_followups()
    assert "[1] status_check" in out and "Subject: Quick check-in" in out
    assert "    Line two." in out
    assert "[2] status_check" in out and "(no draft yet)" in out
    assert spawned.calls == [background.cli_argv("followups")]
    assert "follow-ups pipeline: started `jobkit followups`" in out


def test_get_followups_with_nothing_due_starts_nothing(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, spawned: SpawnRecorder
) -> None:
    mcp = _mcp(conn, monkeypatch)
    jid = _applied(conn)
    _due_followups(conn, jid, {"status_check": 3})
    assert mcp.get_followups() == "no follow-ups due"
    assert spawned.calls == []


class _Running:
    pid = 4242

    def poll(self) -> None:
        return None


def test_run_followups_launches_once(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, spawned: SpawnRecorder
) -> None:
    mcp = _mcp(conn, monkeypatch)
    first = mcp.run_followups()
    assert "started `jobkit followups`" in first
    launch_log = json.loads(
        conn.execute("SELECT detail FROM events WHERE kind='spawn'").fetchone()["detail"]
    )["log"]
    monkeypatch.setitem(background._CHILDREN, launch_log, _Running())  # still drafting
    assert "already running" in mcp.run_followups()
    assert spawned.calls == [background.cli_argv("followups")]


def test_mark_followup_sent(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _mcp(conn, monkeypatch)
    jid = _applied(conn)
    _due_followups(conn, jid, {"status_check": -1})
    assert mcp.mark_followup_sent("1, 77") == "marked sent: [1]; not pending (unchanged): [77]"
    assert conn.execute("SELECT status FROM followups WHERE id=1").fetchone()[0] == "done"
    assert mcp.get_followups() == "no follow-ups due"


def test_record_outcome(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = _mcp(conn, monkeypatch)
    jid = _applied(conn)
    assert "has no submitted application" in mcp.record_outcome(jid, "reply")
    assert "mark_submitted(job_ids=" in mcp.record_outcome(jid, "reply")
    mcp.mark_submitted(job_ids=str(jid), submitted_on=_days_ago(12))
    assert "outcome must be one of" in mcp.record_outcome(jid, "ghosted")
    assert "no job with id 999" in mcp.record_outcome(999, "reply")

    out = mcp.record_outcome(jid, "Interview", "phone screen with the VP, Tue")
    assert "stage submitted -> interview" in out and "2 pending follow-up(s) skipped" in out
    assert {s for _, s in _followups(conn, jid).values()} == {"skipped"}
    ev = json.loads(conn.execute("SELECT detail FROM events WHERE kind='outcome'").fetchone()[0])
    assert ev["outcome"] == "interview" and ev["note"] == "phone screen with the VP, Tue"
    # a later reply never undoes a stronger outcome
    assert "stage interview -> interview" in mcp.record_outcome(jid, "reply")
    stats = mcp.get_stats()
    assert "replied: 1" in stats


# ---------------------------------------------------------------------------
# background launches, staging, open_queue
# ---------------------------------------------------------------------------


class _CapturingPopen:
    calls: list[dict[str, Any]] = []

    def __init__(self, args: list[str], **kwargs: Any) -> None:
        type(self).calls.append({"args": args, **kwargs})
        self.pid = 31337

    def poll(self) -> int:
        return 3


def test_spawn_records_command_pid_log_and_exit_code(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, jobkit_home: Path
) -> None:
    _CapturingPopen.calls = []
    monkeypatch.setattr(subprocess, "Popen", _CapturingPopen)
    launch = background.spawn(conn, "tailor", "--job-id", "7", job_id=None)
    (call,) = _CapturingPopen.calls
    assert call["args"] == [sys.executable, "-m", "jobkit.cli", "tailor", "--job-id", "7"]
    assert call["cwd"] == jobkit_home and call["stderr"] == subprocess.STDOUT
    assert call["env"]["JOBKIT_HOME"] == str(jobkit_home)
    # the source this process imported: REPO_ROOT/src under an editable
    # install, site-packages under a non-editable one (CI: UV_NO_EDITABLE=1)
    first = Path(call["env"]["PYTHONPATH"].split(os.pathsep)[0])
    assert first == Path(jobkit.__file__).resolve().parent.parent
    assert (first / "jobkit" / "__init__.py").is_file()
    assert "UV_NO_EDITABLE" not in call["env"]
    assert launch.log_path.parent == jobkit_home / "logs" / "spawn"
    assert launch.log_path.read_text().startswith("$ jobkit tailor --job-id 7")
    ev = json.loads(conn.execute("SELECT detail FROM events WHERE kind='spawn'").fetchone()[0])
    assert ev == {"args": ["tailor", "--job-id", "7"], "pid": 31337, "log": str(launch.log_path)}
    log_lines = (jobkit_home / "logs" / "jobkit.jsonl").read_text()
    assert '"event": "mcp.spawn"' in log_lines and '"pid": 31337' in log_lines

    (st,) = background.statuses(conn)
    assert st.state == "exited" and st.code == 3
    assert "FAILED (exit 3)" in st.render() and "$ jobkit tailor" in st.render()
    background.statuses(conn)  # the exit is recorded once
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='spawn_exit'").fetchone()[0] == 1


def test_spawn_from_an_earlier_server_is_judged_by_pid(conn: sqlite3.Connection) -> None:
    def spawn_event(pid: int, at: str) -> None:
        detail = json.dumps({"args": ["stage"], "pid": pid, "log": "/nonexistent.log"})
        conn.execute("INSERT INTO events (kind, detail, at) VALUES ('spawn', ?, ?)", (detail, at))

    spawn_event(2**22 + 12345, "2026-01-01 00:00:00")  # no such process
    (st,) = background.statuses(conn)
    assert st.state == "ended" and "exit code unknown" in st.render()
    now = datetime.datetime.now(datetime.UTC)
    spawn_event(os.getpid(), (now - datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"))
    assert background.statuses(conn)[0].state == "running"  # alive and recent
    spawn_event(os.getpid(), (now - datetime.timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S"))
    assert background.statuses(conn)[0].state == "ended"  # alive but old: pid likely reused


def test_stage_forms_is_one_batched_keep_open_process(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, spawned: SpawnRecorder
) -> None:
    mcp = _mcp(conn, monkeypatch)
    a, b = _applied(conn, "t1"), _applied(conn, "t2")
    conn.execute("INSERT INTO queue_pins (n, job_id) VALUES (1, ?)", (a,))
    out = mcp.stage_forms("1", job_ids=f"{b}, {a}")
    assert spawned.calls == [background.cli_argv("stage", "--job-ids", f"{a},{b}", "--keep-open")]
    assert f"staging jobs [{a}, {b}] in ONE headed browser" in out
    assert "nothing staged: give queue numbers" in mcp.stage_forms()


def test_staging_refused_while_another_staging_runs(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, spawned: SpawnRecorder
) -> None:
    mcp = _mcp(conn, monkeypatch)
    jid = _applied(conn)
    mcp.stage_forms(job_ids=str(jid))
    launch_log = json.loads(
        conn.execute("SELECT detail FROM events WHERE kind='spawn'").fetchone()["detail"]
    )["log"]
    monkeypatch.setitem(background._CHILDREN, launch_log, _Running())
    out = mcp.stage_forms(job_ids=str(jid))
    assert out.startswith("nothing staged: staging is already running")
    conn.execute("INSERT INTO queue_pins (n, job_id) VALUES (1, ?)", (jid,))
    assert mcp.restage(1).startswith("nothing staged: staging is already running")
    assert len(spawned.calls) == 1


def test_staging_refused_while_a_browser_holds_the_profile(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, spawned: SpawnRecorder
) -> None:
    mcp = _mcp(conn, monkeypatch)
    jid = _applied(conn)
    monkeypatch.setattr(mcp.browser, "profile_lock_holder", lambda p: 5150)
    assert "a browser (pid 5150) holds the staging profile" in mcp.stage_forms(job_ids=str(jid))
    assert spawned.calls == []


def _run(conn: sqlite3.Connection, jid: int, status: str, filled: str) -> None:
    conn.execute(
        "INSERT INTO autofill_runs (job_id, status, filled_fields) VALUES (?, ?, ?)",
        (jid, status, filled),
    )
    conn.commit()


def test_open_queue_includes_needs_human_with_fields_filled(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, spawned: SpawnRecorder
) -> None:
    mcp = _mcp(conn, monkeypatch)
    assert mcp.open_queue().startswith("nothing to open")
    assert spawned.calls == []
    ready, staged, halted, sent = (_applied(conn, f"o{i}") for i in range(4))
    _run(conn, ready, "needs_human", '[{"identity": "email"}]')
    _run(conn, staged, "staged", '[{"identity": "email"}]')
    _run(conn, halted, "needs_human", "[]")  # stopped before filling anything
    _run(conn, sent, "staged", '[{"identity": "email"}]')
    conn.execute("UPDATE applications SET submitted_at=datetime('now') WHERE job_id=?", (sent,))
    conn.commit()
    out = mcp.open_queue()
    assert out.startswith("opening 2 application(s)")
    assert f"job {staged}:" in out and f"job {ready}:" in out
    assert f"job {halted}:" not in out and f"job {sent}:" not in out
    assert out.index(f"job {staged}:") < out.index(f"job {ready}:")  # staged first
    assert spawned.calls == [background.cli_argv("open-queue")]


def test_cli_open_queue_opens_needs_human(
    jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import cli

    c = q.connect(create=True)
    q.init_db(c)
    jid = _applied(c)
    _run(c, jid, "needs_human", '[{"identity": "email"}]')
    c.close()
    opened: list[list[int]] = []
    monkeypatch.setattr(cli, "stage_jobs", lambda conn, ids, keep_open: opened.append(ids) or [])
    res = CliRunner().invoke(cli.app, ["open-queue"])
    assert res.exit_code == 0, res.output
    assert opened == [[jid]]


def test_background_status_tool(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, spawned: SpawnRecorder
) -> None:
    mcp = _mcp(conn, monkeypatch)
    assert mcp.background_status() == "no background launches recorded"
    mcp.run_followups()
    out = mcp.background_status()
    assert "`jobkit followups`" in out and "finished ok (exit 0)" in out and "log: " in out


def test_approve_reports_the_real_launch(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, spawned: SpawnRecorder
) -> None:
    mcp = _mcp(conn, monkeypatch)
    jid = _insert(conn, external_id="ap1")
    conn.execute("UPDATE jobs SET status='queued' WHERE id=?", (jid,))
    conn.execute("INSERT INTO queue_pins (n, job_id) VALUES (1, ?)", (jid,))
    out = mcp.approve("1")
    assert "started `jobkit tailor` as launch #" in out and "logs/spawn/" in out
    assert spawned.calls == [background.cli_argv("tailor")]


# ---------------------------------------------------------------------------
# ATS-over-feed merge (hard rule 10)
# ---------------------------------------------------------------------------

GH = load_fixture("greenhouse_anthropic.json")
assert isinstance(GH, dict)
GH_JOB = GH["jobs"][0]


def _gh_board(conn: sqlite3.Connection) -> dict[str, Any]:
    targets = Targets(
        companies=[
            Company(name="Anthropic", tier=1, board=Board(ats="greenhouse", token="anthropic"))
        ]
    )
    return seed_targets(conn, targets)[0]


def _feed_row(
    conn: sqlite3.Connection, ext: str, url: str, *, source: str = "getro", status: str = "queued"
) -> int:
    jid = _insert(
        conn,
        external_id=ext,
        title=GH_JOB["title"],
        company_name="Anthropic",
        normalized_company="anthropic",
        apply_url=url,
        jd_html="",
    )
    conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, jid))
    conn.execute(
        "INSERT INTO sources (job_id, source, source_url, is_canonical) VALUES (?, ?, ?, 0)",
        (jid, source, url),
    )
    conn.commit()
    return jid


def _ats_id(conn: sqlite3.Connection, ext: str) -> int:
    return int(
        conn.execute(
            "SELECT id FROM jobs WHERE board_id IS NOT NULL AND external_id=?", (ext,)
        ).fetchone()[0]
    )


def test_posting_key() -> None:
    assert posting_key(GH_JOB["absolute_url"] + "?utm_source=getro") == (
        "greenhouse",
        "anthropic",
        str(GH_JOB["id"]),
    )
    assert posting_key("https://careers.acme.com/jobs?gh_jid=123&x=1") == ("greenhouse", "", "123")
    lever = "https://jobs.lever.co/Palantir/ac978161-6f46-4f6b-ad9e-a258e642751c/apply"
    assert posting_key(lever) == ("lever", "palantir", "ac978161-6f46-4f6b-ad9e-a258e642751c")
    assert posting_key("https://remotive.com/j/agg-123") is None
    assert posting_key("https://boards.greenhouse.io/anthropic") is None  # a board, not a job


def test_ats_poll_merges_the_earlier_feed_row(conn: sqlite3.Connection) -> None:
    board = _gh_board(conn)
    feed = _feed_row(conn, "getro-1", GH_JOB["absolute_url"] + "?gh_src=getro")
    other = _feed_row(conn, "getro-2", "https://job-boards.greenhouse.io/other/jobs/1", source="x")
    upsert_jobs(conn, board, GH)
    conn.commit()
    ats = _ats_id(conn, str(GH_JOB["id"]))
    row = conn.execute("SELECT status, closed_at FROM jobs WHERE id=?", (feed,)).fetchone()
    assert row["status"] == "closed" and row["closed_at"] is not None
    open_twins = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE closed_at IS NULL AND title=? AND id != ?",
        (GH_JOB["title"], other),  # `other` is a different posting with the same title
    ).fetchone()[0]
    assert open_twins == 1, "one live row per posting: the ATS row"
    sources = dict(
        conn.execute("SELECT source, is_canonical FROM sources WHERE job_id=?", (ats,)).fetchall()
    )
    assert sources == {"greenhouse": 1, "getro": 0}
    assert not conn.execute("SELECT 1 FROM sources WHERE job_id=?", (feed,)).fetchone()
    kinds = {
        r["kind"]: json.loads(r["detail"])
        for r in conn.execute("SELECT kind, detail FROM events WHERE kind LIKE 'merged_%'")
    }
    assert kinds["merged_into"] == {"ats_job_id": ats}
    assert kinds["merged_from"] == {"feed_job_id": feed, "carried_status": None}
    # the ATS row keeps its own pipeline status; a feed row for another board is untouched
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (ats,)).fetchone()[0] == "new"
    assert conn.execute("SELECT closed_at FROM jobs WHERE id=?", (other,)).fetchone()[0] is None
    # re-polling is a no-op
    upsert_jobs(conn, board, GH)
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='merged_into'").fetchone()[0] == 1


def test_merge_carries_the_humans_work_to_a_new_ats_row(conn: sqlite3.Connection) -> None:
    board = _gh_board(conn)
    feed = _feed_row(conn, "getro-1", GH_JOB["absolute_url"], status="approved")
    conn.execute("INSERT INTO applications (job_id, stage) VALUES (?, 'tailored')", (feed,))
    conn.execute(
        "INSERT INTO decisions (job_id, decision, reason, auto) VALUES (?, 'approve', 'human', 0)",
        (feed,),
    )
    conn.execute(
        "INSERT INTO decisions (job_id, decision, reason, auto) VALUES (?, 'reject', 'r5', 1)",
        (feed,),
    )
    conn.commit()
    upsert_jobs(conn, board, GH)
    ats = _ats_id(conn, str(GH_JOB["id"]))
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (ats,)).fetchone()[0] == "approved"
    assert conn.execute("SELECT job_id FROM applications").fetchone()[0] == ats
    moved = conn.execute("SELECT job_id, auto FROM decisions ORDER BY id").fetchall()
    assert [tuple(r) for r in moved] == [(ats, 0), (feed, 1)]  # only the human decision moves


def test_merge_holds_a_pair_where_both_rows_carry_state(conn: sqlite3.Connection) -> None:
    board = _gh_board(conn)
    upsert_jobs(conn, board, GH)
    ats = _ats_id(conn, str(GH_JOB["id"]))
    conn.execute("INSERT INTO applications (job_id, stage) VALUES (?, 'tailored')", (ats,))
    feed = _feed_row(conn, "getro-1", GH_JOB["absolute_url"], status="approved")
    conn.execute("INSERT INTO applications (job_id, stage) VALUES (?, 'tailored')", (feed,))
    conn.commit()
    report = merge_feed_dupes(conn)
    assert report.held == [(feed, ats)] and report.merged == []
    assert "held for you" in report.render()
    assert conn.execute("SELECT closed_at FROM jobs WHERE id=?", (feed,)).fetchone()[0] is None
    assert conn.execute("SELECT job_id FROM applications ORDER BY id").fetchall()[1][0] == feed
    merge_feed_dupes(conn)
    upsert_jobs(conn, board, GH)
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='merge_held'").fetchone()[0] == 1


def test_merge_feed_dupes_one_off_over_existing_rows(conn: sqlite3.Connection) -> None:
    board = _gh_board(conn)
    upsert_jobs(conn, board, GH)  # ATS rows first; the feed copies came in before the fix
    conn.commit()
    second = GH["jobs"][1]
    a = _feed_row(conn, "getro-1", GH_JOB["absolute_url"])
    b = _feed_row(conn, "cons-2", f"https://www.anthropic.com/careers?gh_jid={second['id']}")
    _feed_row(conn, "rem-3", "https://remotive.com/j/agg-3")
    dry = merge_feed_dupes(conn, dry_run=True)
    assert dry.examined == 2 and len(dry.merged) == 2 and dry.render().startswith("DRY RUN")
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE status='closed'").fetchone()[0] == 0
    real = merge_feed_dupes(conn)
    assert sorted(real.merged) == sorted(
        [(a, _ats_id(conn, str(GH_JOB["id"]))), (b, _ats_id(conn, str(second["id"])))]
    )
    again = merge_feed_dupes(conn)
    assert again.examined == 0 and again.merged == []


def test_merge_feed_dupes_cli_and_mcp_are_dry_until_confirmed(
    jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import cli, mcp_server

    c = q.connect(create=True)
    q.init_db(c)
    upsert_jobs(c, _gh_board(c), GH)
    c.commit()
    feed = _feed_row(c, "getro-1", GH_JOB["absolute_url"])
    res = CliRunner().invoke(cli.app, ["merge-feed-dupes", "--dry-run"])
    assert res.exit_code == 0 and "would merge into their ATS row: 1" in res.output
    monkeypatch.setattr(mcp_server, "_conn", lambda: c)
    assert mcp_server.merge_feed_dupes().startswith("DRY RUN")
    assert c.execute("SELECT closed_at FROM jobs WHERE id=?", (feed,)).fetchone()[0] is None
    assert "merged into their ATS row: 1" in mcp_server.merge_feed_dupes(confirm=True)
    assert c.execute("SELECT status FROM jobs WHERE id=?", (feed,)).fetchone()[0] == "closed"
