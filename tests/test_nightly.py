"""The nightly unattended run and its launchd schedule (approved 2026-10-08).

* `jobkit nightly`: run-all's stages, then staging of approved + tailored
  jobs (one browser process, closed when done), then the digest; recorded
  with mode nightly; exits 1 on a failed stage; one at a time; no submit stage;
* the digest banner's "last successful nightly run" line;
* `jobkit schedule install|uninstall|status` and the MCP tools, with
  launchctl, uv/claude lookup and the platform faked: no test loads an agent
  or writes outside the sandbox (LAUNCH_AGENTS_DIR is in SANDBOXED_PATHS);
* scripts/install_schedule.sh only forwards to the CLI;
* a job the unattended staging stops on reaches the human (hard rule 2): the
  stage summary, the digest, MCP get_queue and schedule status name it with
  the reason; transient halts are retried once, a CAPTCHA or hard stop never.

Offline, fixture DB only (hard rule 12)."""

from __future__ import annotations

import datetime
import json
import os
import plistlib
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from jobkit import background, schedule
from jobkit import db as q
from jobkit.autofill import answer_bank, browser
from jobkit.config import Filters, load_filters
from jobkit.pipeline import halts, nightly
from jobkit.pipeline.digest import freshness_banner, write_digest
from jobkit.pipeline.runner import last_successful_nightly, latest_nightly, run_stages
from tests.conftest import REPO_ROOT, TEST_CONFIG, SpawnRecorder
from tests.test_filter import _insert
from tests.test_operations import _board, _mcp, _Running
from tests.test_staging_browser import FakeEl, FakeFrame, FakePage

DEAD_PID = 2**22 + 12345  # no such process
TODAY = datetime.date.today().isoformat()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _tailored(
    conn: sqlite3.Connection,
    ext: str,
    *,
    tailored_sql: str = "datetime('now', '-1 hour')",
    resume: str | None = "out/resume.pdf",
    stage: str = "tailored",
) -> int:
    jid = _insert(conn, external_id=ext, title="Account Executive")
    conn.execute("UPDATE jobs SET status='approved' WHERE id=?", (jid,))
    conn.execute(
        "INSERT INTO applications (job_id, stage, resume_path, updated_at) "
        f"VALUES (?, ?, ?, {tailored_sql})",
        (jid, stage, resume),
    )
    conn.commit()
    return jid


def _staging_run(conn: sqlite3.Connection, jid: int, at_sql: str) -> None:
    conn.execute(
        f"INSERT INTO autofill_runs (job_id, status, created_at) VALUES (?, 'staged', {at_sql})",
        (jid,),
    )
    conn.commit()


def _nightly_events(
    conn: sqlite3.Connection,
    run: str,
    *,
    hours_ago: float,
    pid: int = DEAD_PID,
    failed: list[str] | None = None,
    done: bool = True,
) -> None:
    at = f"datetime('now', '-{hours_ago} hours')"
    start = {"run": run, "mode": "nightly", "pid": pid, "stages": ["poll"]}
    conn.execute(
        f"INSERT INTO events (kind, detail, at) VALUES ('run_all.start', ?, {at})",
        (json.dumps(start),),
    )
    if done:
        end = {"run": run, "mode": "nightly", "stages": 1, "failed": failed or []}
        conn.execute(
            f"INSERT INTO events (kind, detail, at) VALUES ('run_all.done', ?, {at})",
            (json.dumps(end),),
        )
    conn.commit()


def _sandbox_db() -> sqlite3.Connection:
    c = q.connect(create=True)
    q.init_db(c)
    return c


def _results(status: str, reason: str = "") -> Any:
    def fake(
        conn: sqlite3.Connection, ids: list[int], keep_open: bool = True, carry_forward: bool = True
    ) -> Any:
        _results.calls.append((list(ids), keep_open, carry_forward))  # type: ignore[attr-defined]
        return [{"job_id": j, "status": status, "reason": reason} for j in ids]

    _results.calls = []  # type: ignore[attr-defined]
    return fake


# ---------------------------------------------------------------------------
# config knob
# ---------------------------------------------------------------------------


def test_nightly_at_default_and_validation() -> None:
    assert load_filters().nightly_at == (2, 0)  # config/filters.yaml
    assert Filters(raw={}).nightly_at == (2, 0)
    assert Filters(raw={"schedule": {"nightly_at": "23:30"}}).nightly_at == (23, 30)
    assert Filters(raw={"schedule": {"nightly_at": "3:05"}}).nightly_at == (3, 5)
    for bad in ("24:00", "02:60", "2am", 120):  # 120: unquoted 2:00 in YAML 1.1
        with pytest.raises(ValueError, match="nightly_at"):
            _ = Filters(raw={"schedule": {"nightly_at": bad}}).nightly_at


@pytest.mark.parametrize(
    "path",
    [TEST_CONFIG / "filters.yaml", REPO_ROOT / "config" / "filters.example.yaml"],
    ids=["test-profile", "template"],
)
def test_schedule_block_has_no_unread_times(path: Path) -> None:
    sched = yaml.safe_load(path.read_text())["schedule"]
    # the per-stage times nothing read are gone: the nightly time has one owner
    assert not {"poll_tier_1_2", "poll_tier_3", "digest", "followup_check"} & set(sched)
    assert sched["nightly_at"] == "02:00"


# ---------------------------------------------------------------------------
# the nightly stage list and the staging stage
# ---------------------------------------------------------------------------


def test_nightly_is_run_all_then_staging_then_digest_and_never_submits() -> None:
    from jobkit import cli

    run_all = [n for n, _ in cli._run_all_stages()]
    names = [n for n, _ in nightly.nightly_stages(cli._run_all_stages())]
    assert names == [*run_all[:-1], "stage", "digest"]
    assert names[0] == "poll" and run_all[-1] == "digest"
    assert names.index("tailor") < names.index("stage")
    assert not [n for n in names if "submit" in n or "apply" in n]
    with pytest.raises(ValueError, match="no digest stage"):
        nightly.nightly_stages([("poll", lambda c: None)])


def test_stage_candidates(conn: sqlite3.Connection) -> None:
    fresh = _tailored(conn, "fresh")
    staged_since = _tailored(conn, "staged", tailored_sql="datetime('now', '-2 hours')")
    _staging_run(conn, staged_since, "datetime('now', '-1 hour')")
    retailored = _tailored(conn, "retailored")
    _staging_run(conn, retailored, "datetime('now', '-3 hours')")  # staged before re-tailoring
    submitted = _tailored(conn, "submitted")
    conn.execute(
        "UPDATE applications SET submitted_at=datetime('now') WHERE job_id=?", (submitted,)
    )
    closed = _tailored(conn, "closed")
    conn.execute("UPDATE jobs SET closed_at=datetime('now') WHERE id=?", (closed,))
    _tailored(conn, "cover-failed", stage="approved")  # not tailored: never staged
    _tailored(conn, "no-resume", resume=None)
    applied = _tailored(conn, "applied")
    conn.execute("UPDATE jobs SET status='applied' WHERE id=?", (applied,))
    reapproved = _tailored(conn, "reapproved")
    conn.execute("INSERT INTO applications (job_id, stage) VALUES (?, 'approved')", (reapproved,))
    conn.commit()
    assert nightly.stage_candidates(conn) == [fresh, retailored]


def test_stage_tailored_one_closing_browser_and_needs_human_is_not_a_failure(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[int], bool, bool]] = []

    def fake(
        c: sqlite3.Connection, ids: list[int], keep_open: bool = True, carry_forward: bool = True
    ) -> Any:
        calls.append((list(ids), keep_open, carry_forward))
        return [
            {"job_id": ids[0], "status": "staged"},
            {"job_id": ids[1], "status": "needs_human", "reason": "captcha"},
        ]

    monkeypatch.setattr(nightly, "stage_jobs", fake)
    assert nightly.stage_tailored(conn) == "nothing new to stage"
    assert calls == []  # no browser when there is nothing to stage
    a, b = _tailored(conn, "a"), _tailored(conn, "b")
    assert nightly.stage_tailored(conn) == (
        f"2 job(s): 1 needs_human, 1 staged. Needs you: job {b} Acme: captcha"
    )
    # one call: one browser process, closed when done, no carry-forward unattended
    assert calls == [([a, b], False, False)]


def test_stage_tailored_fails_when_jobs_are_deferred(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        nightly, "stage_jobs", _results("deferred", "profile-locked-by-pid-77: quit that browser")
    )
    jid = _tailored(conn, "d")
    with pytest.raises(nightly.StagingIncomplete, match=f"job {jid}: profile-locked-by-pid-77"):
        nightly.stage_tailored(conn)
    (out,) = run_stages(conn, [("stage", nightly.stage_tailored)], mode="nightly")
    assert out.status == "error" and "1 of 1 job(s) not staged (1 deferred)" in (out.error or "")


# ---------------------------------------------------------------------------
# `jobkit nightly`
# ---------------------------------------------------------------------------


def test_cli_nightly_runs_every_stage_records_the_run_and_the_digest_says_so(
    jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import cli

    c = _sandbox_db()
    jid = _tailored(c, "n1")
    c.close()
    ran: list[str] = []
    monkeypatch.setattr(
        cli,
        "_run_all_stages",
        lambda: [("poll", lambda conn: ran.append("poll") or "ok"), ("digest", write_digest)],
    )
    monkeypatch.setattr(nightly, "stage_jobs", _results("staged"))
    res = CliRunner().invoke(cli.app, ["nightly"])
    assert res.exit_code == 0, res.output
    lines = res.output.splitlines()
    assert [ln.split(":")[0] for ln in lines] == ["poll", "stage", "digest", "nightly"]
    assert lines[-1] == "nightly: all 3 stages ok"
    assert ran == ["poll"] and _results.calls == [([jid], False, False)]  # type: ignore[attr-defined]

    conn = q.connect()
    kinds = [
        r["kind"]
        for r in conn.execute("SELECT kind FROM events WHERE kind LIKE 'run_all.%' ORDER BY id")
    ]
    assert kinds == ["run_all.start", *["run_all.stage"] * 3, "run_all.done"]
    start = json.loads(
        conn.execute("SELECT detail FROM events WHERE kind='run_all.start'").fetchone()[0]
    )
    assert start["mode"] == "nightly" and start["pid"] == os.getpid()
    assert start["stages"] == ["poll", "stage", "digest"]
    done = json.loads(
        conn.execute("SELECT detail FROM events WHERE kind='run_all.done'").fetchone()[0]
    )
    assert done == {"run": start["run"], "mode": "nightly", "stages": 3, "failed": []}
    rec = latest_nightly(conn)
    assert rec is not None and rec.done and rec.exit_code == 0

    # the digest is the run's last stage: it says the run is in progress
    digest = (jobkit_home / "out" / "digest" / f"{TODAY}.md").read_text()
    assert "A nightly run started" in digest and "is still running" in digest
    # read back afterwards, the run is the last successful nightly
    banner = freshness_banner(conn, 36)
    assert "Last successful nightly run less than an hour ago" in banner
    assert "still running" not in banner and "⚠ STALE NIGHTLY" not in banner


def test_cli_nightly_exits_nonzero_on_a_failed_stage_and_still_writes_the_digest(
    jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import cli

    c = _sandbox_db()
    _tailored(c, "n2")
    c.close()

    def poll_down(conn: sqlite3.Connection) -> object:
        raise ConnectionError("network unreachable")

    monkeypatch.setattr(
        cli, "_run_all_stages", lambda: [("poll", poll_down), ("digest", write_digest)]
    )
    monkeypatch.setattr(nightly, "stage_jobs", _results("deferred", "browser-launch-failed: x"))
    res = CliRunner().invoke(cli.app, ["nightly"])
    assert res.exit_code == 1
    assert "nightly: 2 stage(s) failed: poll, stage" in res.output
    digest = (jobkit_home / "out" / "digest" / f"{TODAY}.md").read_text()
    assert "⚠ The last nightly run" in digest
    assert "had failed stages: poll (error), stage (error)" in digest
    conn = q.connect()
    rec = latest_nightly(conn)
    assert rec is not None and rec.failed == ("poll", "stage") and rec.exit_code == 1
    banner = freshness_banner(conn, 36)
    assert "⚠ No nightly run has succeeded yet" in banner


def test_cli_nightly_refuses_while_another_nightly_runs(
    jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import cli

    c = _sandbox_db()
    _nightly_events(c, "other", hours_ago=1, pid=os.getpid(), done=False)  # alive, recent
    c.close()
    monkeypatch.setattr(cli, "_run_all_stages", lambda: [("digest", write_digest)])
    res = CliRunner().invoke(cli.app, ["nightly"])
    assert res.exit_code == 1
    assert "another nightly run is still running (run other" in res.output
    conn = q.connect()
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='run_all.start'").fetchone()[0] == 1

    # a run whose process is gone does not block the next one
    conn.execute("DELETE FROM events")
    conn.commit()
    _nightly_events(conn, "crashed", hours_ago=1, pid=DEAD_PID, done=False)
    monkeypatch.setattr(nightly, "stage_jobs", _results("staged"))
    assert CliRunner().invoke(cli.app, ["nightly"]).exit_code == 0


# ---------------------------------------------------------------------------
# digest banner: last successful nightly
# ---------------------------------------------------------------------------


def test_banner_without_any_nightly_run(conn: sqlite3.Connection) -> None:
    _board(conn, "datetime('now', '-2 hours')")
    banner = freshness_banner(conn, 36)
    assert "No nightly run on record: `jobkit schedule install` sets one up." in banner
    assert "⚠" not in banner


def test_banner_last_successful_nightly_fresh_then_stale(conn: sqlite3.Connection) -> None:
    _nightly_events(conn, "r1", hours_ago=40)
    banner = freshness_banner(conn, 36)
    assert "⚠ STALE NIGHTLY: last successful nightly run 40 hours ago" in banner
    assert "more than 36 hours" in banner and "jobkit schedule status" in banner
    _nightly_events(conn, "r2", hours_ago=5, failed=["poll"])  # a failed one is not success
    assert "last successful nightly run 40 hours ago" in freshness_banner(conn, 36)
    _nightly_events(conn, "r3", hours_ago=3)
    banner = freshness_banner(conn, 36)
    assert "Last successful nightly run 3 hours ago" in banner and "STALE NIGHTLY" not in banner


def test_banner_names_a_nightly_run_that_did_not_finish(conn: sqlite3.Connection) -> None:
    _nightly_events(conn, "ok", hours_ago=30)
    _nightly_events(conn, "killed", hours_ago=4, pid=DEAD_PID, done=False)
    banner = freshness_banner(conn, 36)
    assert "Last successful nightly run 30 hours ago" in banner
    assert "⚠ The nightly run started" in banner and "did not finish" in banner


# ---------------------------------------------------------------------------
# launchd schedule (everything faked: platform, uv/claude lookup, launchctl)
# ---------------------------------------------------------------------------


@dataclass
class FakeLaunchctl:
    calls: list[list[str]] = field(default_factory=list)
    replies: dict[str, list[tuple[int, str, str]]] = field(default_factory=dict)

    DEFAULTS = {
        "bootout": (3, "", "Boot-out failed: 3: No such process"),
        "bootstrap": (0, "", ""),
        "print": (113, "", 'Could not find service "com.jobkit.nightly" in domain'),
    }

    def __call__(self, *args: str) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        queue = self.replies.get(args[0])
        rc, out, err = queue.pop(0) if queue else self.DEFAULTS[args[0]]
        return subprocess.CompletedProcess([schedule.LAUNCHCTL, *args], rc, out, err)

    def verbs(self) -> list[str]:
        return [c[0] for c in self.calls]


@dataclass
class Mac:
    user: Path
    uv: Path
    claude: Path
    found: dict[str, str]
    launchctl: FakeLaunchctl


@pytest.fixture()
def mac(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Mac:
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setenv("HOME", str(user))  # Path.home(): the protected folders
    monkeypatch.delenv("UV", raising=False)  # `uv run pytest` sets it
    uv = tmp_path / "local" / "bin" / "uv"
    claude = tmp_path / "homebrew" / "bin" / "claude"
    for f in (uv, claude):
        f.parent.mkdir(parents=True)
        f.write_text("#!/bin/sh\n")
        f.chmod(0o755)
    found = {"uv": str(uv), "claude": str(claude)}
    monkeypatch.setattr(schedule.shutil, "which", lambda name, *a, **k: found.get(name))
    monkeypatch.setattr(schedule, "_platform", lambda: "darwin")
    fake = FakeLaunchctl()
    monkeypatch.setattr(schedule, "_launchctl", fake)
    monkeypatch.setattr(schedule.time, "sleep", lambda s: None)
    return Mac(user, uv, claude, found, fake)


def _set_nightly_at(home: Path, value: str) -> None:
    cfg = home / "config" / "filters.yaml"
    data = yaml.safe_load(cfg.read_text())
    data["schedule"]["nightly_at"] = value
    cfg.write_text(yaml.safe_dump(data, sort_keys=False))


def test_launch_agents_dir_is_sandboxed(jobkit_home: Path) -> None:
    assert jobkit_home / "LaunchAgents" == schedule.LAUNCH_AGENTS_DIR
    assert schedule.plist_path() == jobkit_home / "LaunchAgents" / "com.jobkit.nightly.plist"


def test_plan_builds_the_agent(mac: Mac, jobkit_home: Path) -> None:
    root = jobkit_home.resolve()
    p = schedule.plan()
    pl = p.plist
    assert pl["Label"] == "com.jobkit.nightly"
    assert pl["ProgramArguments"] == [
        str(mac.uv),
        "run",
        "--directory",
        str(root),
        "jobkit",
        "nightly",
    ]
    assert pl["WorkingDirectory"] == str(root)
    assert pl["StartCalendarInterval"] == {"Hour": 2, "Minute": 0}
    env = pl["EnvironmentVariables"]
    assert env["PATH"] == f"{mac.uv.parent}:{mac.claude.parent}:/usr/bin:/bin:/usr/sbin:/sbin"
    assert env["JOBKIT_HOME"] == str(root) and env["PYTHONPATH"] == str(root / "src")
    assert pl["StandardOutPath"] == str(root / "logs" / "nightly.out.log")
    assert pl["StandardErrorPath"] == str(root / "logs" / "nightly.err.log")
    assert pl["RunAtLoad"] is False
    # the old script's frozen-install flags are gone
    assert "--no-sync" not in pl["ProgramArguments"] and "UV_NO_EDITABLE" not in env
    assert plistlib.loads(p.xml()) == pl  # a valid plist, paths XML-escaped
    assert any("The Mac must be awake at 02:00" in w for w in p.warnings)


def test_plan_reads_nightly_at_and_warns_without_claude(
    mac: Mac, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_nightly_at(jobkit_home, "03:15")
    del mac.found["claude"]
    p = schedule.plan()
    assert p.plist["StartCalendarInterval"] == {"Hour": 3, "Minute": 15}
    assert (
        p.plist["EnvironmentVariables"]["PATH"] == f"{mac.uv.parent}:/usr/bin:/bin:/usr/sbin:/sbin"
    )
    assert any("`claude` is not on PATH" in w for w in p.warnings)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    assert not any("claude" in w for w in schedule.plan().warnings)


def test_plan_without_uv_refuses(mac: Mac) -> None:
    del mac.found["uv"]
    with pytest.raises(schedule.ScheduleError, match="`uv` is not on PATH"):
        schedule.plan()


@pytest.mark.parametrize(
    "folder", ["Desktop", "Documents", "Downloads", "Library/Mobile Documents"]
)
def test_install_refuses_a_root_launchd_cannot_read(mac: Mac, folder: str) -> None:
    root = mac.user / folder / "jobkit"
    root.mkdir(parents=True)
    with pytest.raises(schedule.ScheduleError, match=f"the project root is under .*{folder}"):
        schedule.install(root)
    assert mac.launchctl.calls == [] and not schedule.plist_path().exists()


def test_install_refuses_data_symlinked_into_desktop(mac: Mac, tmp_path: Path) -> None:
    root = tmp_path / "code" / "jobkit"
    root.mkdir(parents=True)
    real_data = mac.user / "Desktop" / "jobkit-data"
    real_data.mkdir(parents=True)
    (root / "data").symlink_to(real_data, target_is_directory=True)
    with pytest.raises(schedule.ScheduleError, match=r"data \(resolves to .*Desktop"):
        schedule.plan(root)


def test_install_accepts_a_desktop_symlink_to_a_readable_checkout(mac: Mac, tmp_path: Path) -> None:
    real = tmp_path / "code" / "jobkit"
    real.mkdir(parents=True)
    link = mac.user / "Desktop" / "jobkit"
    link.parent.mkdir(parents=True)
    link.symlink_to(real, target_is_directory=True)
    p = schedule.plan(link)
    assert p.plist["WorkingDirectory"] == str(real.resolve())


def test_install_writes_the_plist_and_bootstraps_it(mac: Mac, jobkit_home: Path) -> None:
    legacy = schedule.plist_path("com.jobkit.morning")
    legacy.parent.mkdir(parents=True)
    legacy.write_text("<plist/>")
    out = schedule.install()
    uid = os.getuid()
    target = schedule.plist_path()
    assert mac.launchctl.calls == [
        ["bootout", f"gui/{uid}/com.jobkit.morning"],
        ["bootout", f"gui/{uid}/com.jobkit.nightly"],
        ["bootstrap", f"gui/{uid}", str(target)],
    ]
    assert not legacy.exists()
    data = plistlib.loads(target.read_bytes())
    assert data["Label"] == "com.jobkit.nightly"
    assert data["ProgramArguments"][-2:] == ["jobkit", "nightly"]
    assert (jobkit_home / "logs").is_dir()
    assert out.startswith("installed com.jobkit.nightly: `jobkit nightly` runs daily at 02:00")
    assert "removed the old com.jobkit.morning agent" in out
    assert "The Mac must be awake at 02:00" in out
    assert f"launchctl kickstart gui/{uid}/com.jobkit.nightly" in out
    log = (jobkit_home / "logs" / "jobkit.jsonl").read_text()
    assert '"event": "schedule.installed"' in log


def test_install_dry_run_changes_nothing(mac: Mac) -> None:
    out = schedule.install(dry_run=True)
    assert out.startswith("dry run, nothing installed")
    assert "<key>Label</key>" in out and "com.jobkit.nightly" in out
    assert mac.launchctl.calls == [] and not schedule.plist_path().exists()


def test_install_retries_bootstrap_once_then_fails_loudly(mac: Mac) -> None:
    mac.launchctl.replies["bootstrap"] = [(5, "", "Bootstrap failed: 5: Input/output error")]
    schedule.install()  # the retry succeeds
    assert mac.launchctl.verbs().count("bootstrap") == 2
    mac.launchctl.calls.clear()
    mac.launchctl.replies["bootstrap"] = [(5, "", "Bootstrap failed: 5"), (5, "", "again")]
    with pytest.raises(schedule.ScheduleError, match=r"launchctl bootstrap .* failed \(exit 5\)"):
        schedule.install()


def test_uninstall(mac: Mac) -> None:
    assert schedule.uninstall().startswith("nothing to remove")
    schedule.install()
    dry = schedule.uninstall(dry_run=True)
    assert dry.startswith("dry run, nothing removed") and str(schedule.plist_path()) in dry
    assert schedule.plist_path().exists()
    mac.launchctl.calls.clear()
    mac.launchctl.replies["bootout"] = [(0, "", "")]  # the nightly agent was loaded
    out = schedule.uninstall()
    assert out.startswith("removed com.jobkit.nightly: unloaded and deleted")
    assert not schedule.plist_path().exists()
    assert mac.launchctl.verbs() == ["bootout"] * 3  # nightly + the two legacy labels


PRINT_OUTPUT = """gui/501/com.jobkit.nightly = {
\tactive count = 0
\tpath = /Users/x/Library/LaunchAgents/com.jobkit.nightly.plist
\ttype = LaunchAgent
\tstate = not running

\tprogram = /Users/x/.local/bin/uv
\truns = 3
\tlast exit code = 0
}
"""


def test_status_reports_install_launchd_and_the_last_run(
    mac: Mac, jobkit_home: Path, conn: sqlite3.Connection
) -> None:
    text, supported = schedule.status(conn)
    assert supported
    assert "installed: no" in text and "loaded in launchd: no" in text
    assert "last nightly run: none on record" in text
    schedule.install()
    _nightly_events(conn, "run-a", hours_ago=26)
    stages = [("poll", lambda c: "12 boards"), ("stage", lambda c: "2 job(s)")]
    run_stages(conn, stages, mode="nightly")
    mac.launchctl.calls.clear()
    mac.launchctl.replies["print"] = [(0, PRINT_OUTPUT, "")]
    text, _ = schedule.status(conn)
    assert mac.launchctl.verbs() == ["print"]  # read-only
    assert f"installed: yes ({schedule.plist_path()})" in text
    assert "runs: daily at 02:00 local time" in text
    assert "loaded in launchd: yes (state = not running, runs = 3, last exit code = 0)" in text
    assert "last nightly run: started" in text and "exit 0, every stage ok" in text
    assert "    poll: ok (" in text and "12 boards" in text
    assert "last successful nightly run: " in text and "less than an hour ago" in text
    _set_nightly_at(jobkit_home, "04:00")
    text, _ = schedule.status(conn)
    assert "schedule.nightly_at is 04:00 but the agent runs at 02:00" in text


def test_status_names_a_failed_and_a_stopped_run(mac: Mac, conn: sqlite3.Connection) -> None:
    _nightly_events(conn, "bad", hours_ago=2, failed=["stage"])
    text, _ = schedule.status(conn)
    assert "exit 1, failed stages: stage" in text
    assert "last successful nightly run: none on record" in text
    _nightly_events(conn, "gone", hours_ago=1, done=False)
    assert "did not finish: its process is gone" in schedule.status(conn)[0]


def test_other_platforms_are_not_supported_yet(
    jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import cli

    monkeypatch.setattr(schedule, "_platform", lambda: "linux")
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(schedule, "_launchctl", lambda *a: calls.append(a))
    for fn in (schedule.install, schedule.uninstall, schedule.plan):
        with pytest.raises(schedule.ScheduleUnsupported, match="not supported yet on linux"):
            fn()
    _sandbox_db().close()
    runner = CliRunner()
    for args in (["install"], ["uninstall"], ["status"]):
        res = runner.invoke(cli.app, ["schedule", *args])
        assert res.exit_code == 1 and "not supported yet on linux" in res.output, args
    assert (
        "last nightly run: none on record" in runner.invoke(cli.app, ["schedule", "status"]).output
    )
    assert calls == []


def test_cli_schedule_commands(
    mac: Mac, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import cli

    runner = CliRunner()
    res = runner.invoke(cli.app, ["schedule", "install", "--dry-run"])
    assert res.exit_code == 0 and "dry run, nothing installed" in res.output
    assert mac.launchctl.calls == []
    res = runner.invoke(cli.app, ["schedule", "install"])
    assert res.exit_code == 0 and "installed com.jobkit.nightly" in res.output
    res = runner.invoke(cli.app, ["schedule", "status"])  # works with no database
    assert res.exit_code == 0 and "installed: yes" in res.output
    assert "no database (run `jobkit init`)" in res.output
    res = runner.invoke(cli.app, ["schedule", "uninstall"])
    assert res.exit_code == 0 and not schedule.plist_path().exists()
    desk = mac.user / "Desktop" / "jobkit"
    desk.mkdir(parents=True)
    monkeypatch.setenv("JOBKIT_HOME", str(desk))
    res = runner.invoke(cli.app, ["schedule", "install"])
    assert res.exit_code == 1 and "is under" in res.output and "Desktop" in res.output


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


def test_mcp_run_nightly_starts_one_background_run(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, spawned: SpawnRecorder
) -> None:
    mcp = _mcp(conn, monkeypatch)
    monkeypatch.setattr(mcp.browser, "profile_lock_holder", lambda p: None)
    out = mcp.run_nightly()
    assert "started `jobkit nightly` as launch #" in out and "Nothing is submitted" in out
    assert spawned.calls == [background.cli_argv("nightly")]
    launch_log = json.loads(
        conn.execute("SELECT detail FROM events WHERE kind='spawn'").fetchone()["detail"]
    )["log"]
    monkeypatch.setitem(background._CHILDREN, launch_log, _Running())  # still starting up
    assert mcp.run_nightly().startswith("not started: a nightly run started from here")
    assert len(spawned.calls) == 1


def test_mcp_run_nightly_refused_while_a_nightly_runs(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, spawned: SpawnRecorder
) -> None:
    mcp = _mcp(conn, monkeypatch)
    _nightly_events(conn, "launchd-run", hours_ago=0.5, pid=os.getpid(), done=False)
    out = mcp.run_nightly()
    assert out.startswith("not started: a nightly run is already running (run launchd-run")
    assert spawned.calls == []


def test_mcp_run_nightly_warns_when_a_browser_holds_the_profile(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, spawned: SpawnRecorder
) -> None:
    mcp = _mcp(conn, monkeypatch)
    monkeypatch.setattr(mcp.browser, "profile_lock_holder", lambda p: 5150)
    out = mcp.run_nightly()
    assert "A browser (pid 5150) holds the staging profile" in out
    assert spawned.calls == [background.cli_argv("nightly")]


def test_mcp_schedule_tools(
    mac: Mac, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp = _mcp(conn, monkeypatch)
    status = mcp.schedule_status()
    assert "installed: no" in status and "last nightly run: none on record" in status
    assert mac.launchctl.verbs() == ["print"]
    mac.launchctl.calls.clear()
    dry = mcp.schedule_install()
    assert dry.startswith("dry run, nothing installed") and "confirm=true" in dry
    assert mac.launchctl.calls == [] and not schedule.plist_path().exists()
    assert mcp.schedule_install(confirm=True).startswith("installed com.jobkit.nightly")
    assert schedule.plist_path().exists()
    assert mcp.schedule_uninstall().startswith("dry run, nothing removed")
    assert schedule.plist_path().exists()
    assert mcp.schedule_uninstall(confirm=True).startswith("removed com.jobkit.nightly")
    assert not schedule.plist_path().exists()
    monkeypatch.setattr(schedule, "_platform", lambda: "win32")
    assert "not supported yet on win32" in mcp.schedule_install(confirm=True)
    assert "not supported yet on win32" in mcp.schedule_status()


# ---------------------------------------------------------------------------
# the retired script
# ---------------------------------------------------------------------------


def test_install_schedule_script_only_forwards_to_the_cli() -> None:
    script = (REPO_ROOT / "scripts" / "install_schedule.sh").read_text()
    code = [ln for ln in script.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert code[-1] == 'exec uv run --directory "$REPO" jobkit schedule install "$@"'
    body = "\n".join(code)
    assert "UV_NO_EDITABLE" not in body and "--no-sync" not in body and "launchctl" not in body


# ---------------------------------------------------------------------------
# staging halts reach the human (hard rule 2: detect, abort, hand to the human)
# ---------------------------------------------------------------------------

ASHBY_URL = "https://jobs.ashbyhq.com/initech/x/application"
FORM_FIELDS = [
    {"identity": "_systemfield_name", "label": "Name", "required": True, "match": {}},
    {"identity": "_systemfield_email", "label": "Email", "required": True, "match": {}},
]
TURNSTILE = FakeEl(True, {"x": 10.0, "y": 10.0, "width": 300.0, "height": 65.0}, {})


def _ashby_tailored(conn: sqlite3.Connection, ext: str) -> int:
    """An approved, tailored job with a captured form spec and an allowlisted
    apply URL, so real staging gets as far as the page."""
    conn.execute(
        "INSERT INTO companies (id, name, normalized_name, domain) "
        "VALUES (9, 'Initech', 'initech', 'initech.example') ON CONFLICT DO NOTHING"
    )
    jid = _tailored(conn, ext)
    conn.execute(
        "UPDATE jobs SET company_id=9, company_name='Initech', apply_url=? WHERE id=?",
        (ASHBY_URL, jid),
    )
    conn.execute(
        "INSERT INTO form_specs (job_id, source, fields, drafted_answers) "
        "VALUES (?, 'api_questions', ?, '{}')",
        (jid, json.dumps(FORM_FIELDS)),
    )
    conn.commit()
    return jid


def _captcha_stage_jobs(calls: list[list[int]]) -> Any:
    """The nightly's stage_jobs, running the REAL stage_job on a fake page whose
    form sits under a visible Turnstile challenge."""

    def fake(
        conn: sqlite3.Connection, ids: list[int], keep_open: bool = True, carry_forward: bool = True
    ) -> list[dict[str, Any]]:
        calls.append(list(ids))
        out = []
        for jid in ids:
            frame = FakeFrame(
                url=ASHBY_URL,
                ids=("_systemfield_email",),
                widgets={"turnstile-widget": [TURNSTILE]},
            )
            out.append(browser.stage_job(conn, FakePage([frame]), jid))
        return out

    return fake


def _halt_row(
    conn: sqlite3.Connection, jid: int, reason: str, filled: str = "[]", status: str = "needs_human"
) -> None:
    conn.execute(
        "INSERT INTO autofill_runs (job_id, status, abort_reason, filled_fields, "
        "unfilled_fields, screenshot_path) VALUES (?, ?, ?, ?, '[]', ?)",
        (jid, status, reason, filled, f"out/screenshots/staged-{jid}.png"),
    )
    conn.commit()


def test_a_nightly_captcha_halt_reaches_the_human(
    jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import cli

    (jobkit_home / "data" / "answer_bank.yaml").write_text(
        yaml.safe_dump({"identity": {"first_name": "Pat", "email": "pat@example.com"}})
    )
    answer_bank.load_bank.cache_clear()
    c = _sandbox_db()
    jid = _ashby_tailored(c, "cap")
    assert nightly.stage_candidates(c) == [jid]
    c.close()
    calls: list[list[int]] = []
    monkeypatch.setattr(cli, "_run_all_stages", lambda: [("digest", write_digest)])
    monkeypatch.setattr(nightly, "stage_jobs", _captcha_stage_jobs(calls))
    try:
        res = CliRunner().invoke(cli.app, ["nightly"])
    finally:
        answer_bank.load_bank.cache_clear()
    assert res.exit_code == 0, res.output  # a halt is an outcome, not a failed stage
    assert calls == [[jid]]
    # the stage summary names the job, its company and the reason
    assert (
        f"stage: 1 job(s): 1 needs_human. Needs you: job {jid} Initech: "
        "captcha-or-turnstile-visible" in res.output
    )

    conn = q.connect()
    shot = str(jobkit_home / "out" / "screenshots" / f"staged-{jid}.png")
    run = conn.execute(
        "SELECT status, abort_reason, filled_fields, screenshot_path FROM autofill_runs "
        "WHERE job_id=?",
        (jid,),
    ).fetchone()
    assert tuple(run) == ("needs_human", "captcha-or-turnstile-visible", "[]", shot)

    # the digest the nightly wrote names the job, the reason, the screenshot and the URL
    digest = (jobkit_home / "out" / "digest" / f"{TODAY}.md").read_text()
    assert "### ⚠ Stopped before filling anything: waiting on you (1)" in digest
    assert (
        f"- job {jid} · Initech — Account Executive: captcha-or-turnstile-visible "
        "(a CAPTCHA or bot check is on the page" in digest
    )
    assert f"screenshot {shot}" in digest and f"apply at {ASHBY_URL}" in digest
    assert 'mark_submitted(job_ids="<id>")' in digest
    assert "tries it again" not in digest  # a CAPTCHA is never retried automatically
    assert digest.index("## Staging") < digest.index("## Review queue")

    # neither the next nightly nor open-queue picks it up: the listing is the hand-off
    assert nightly.stage_candidates(conn) == []
    assert conn.execute(q.OPEN_QUEUE_JOBS).fetchall() == []
    mcp = _mcp(conn, monkeypatch)
    queue = mcp.get_queue()
    assert (
        f"⚠ 1 job(s) stopped in staging and wait on you: job {jid} Initech "
        "(captcha-or-turnstile-visible)." in queue
    )
    assert queue.index("stopped in staging") < queue.index("jobs in queue:")
    monkeypatch.setattr(schedule, "_platform", lambda: "linux")  # no launchctl in tests
    for report in (nightly.report(conn), mcp.schedule_status()):
        assert "⚠ jobs halted in staging, waiting on you (1):" in report
        assert f"job {jid} · Initech — Account Executive: captcha-or-turnstile-visible" in report
        assert "stage: ok" in report and f"job {jid} Initech: captcha" in report

    # applied to by hand and recorded: the job leaves every list
    conn.execute("UPDATE applications SET submitted_at=datetime('now') WHERE job_id=?", (jid,))
    conn.commit()
    assert halts.waiting(conn) == [] and halts.queue_line(conn) is None
    assert "## Staging" not in write_digest(conn).read_text()
    assert "jobs halted in staging, waiting on you: none" in nightly.report(conn)
    assert "stopped in staging" not in mcp.get_queue()


@pytest.mark.parametrize(
    ("reason", "retried"),
    [
        ("captcha-or-turnstile-visible", False),
        ("requests-government-id-or-payment", False),
        ("embedded-assessment-detected", False),
        ("apply-url-not-allowlisted:https://x.com", False),
        ("form-host-not-allowlisted:forms.example", False),
        ("posting-not-found", True),
    ],
)
def test_only_transient_halts_are_retried_by_the_nightly(
    conn: sqlite3.Connection, reason: str, retried: bool
) -> None:
    jid = _tailored(conn, "h")
    _halt_row(conn, jid, reason)
    assert nightly.stage_candidates(conn) == ([jid] if retried else [])
    (halt,) = halts.waiting(conn)
    assert halt.next_nightly_retries is retried
    assert ("the next nightly run retries it" in halt.brief()) is retried
    assert ("The next nightly run tries it again." in halt.line()) is retried


def test_posting_not_found_is_retried_once_per_tailoring(conn: sqlite3.Connection) -> None:
    jid = _tailored(conn, "slow")
    _halt_row(conn, jid, "posting-not-found")
    assert nightly.stage_candidates(conn) == [jid]  # maybe the page was slow
    _halt_row(conn, jid, "posting-not-found")
    assert nightly.stage_candidates(conn) == []  # twice: left for the human
    (halt,) = halts.waiting(conn)
    assert halt.reason == "posting-not-found" and not halt.next_nightly_retries
    # re-tailoring starts over
    conn.execute(
        "UPDATE applications SET updated_at=datetime('now', '+1 minute') WHERE job_id=?", (jid,)
    )
    conn.commit()
    assert nightly.stage_candidates(conn) == [jid]


def test_no_form_spec_halt_is_retried_once_a_spec_exists(conn: sqlite3.Connection) -> None:
    jid = _tailored(conn, "nospec")
    _halt_row(conn, jid, "no-form-spec-captured")
    assert nightly.stage_candidates(conn) == []  # still nothing to fill
    conn.execute(
        "INSERT INTO form_specs (job_id, source, fields) VALUES (?, 'dom_scrape', '[]')", (jid,)
    )
    conn.commit()
    assert nightly.stage_candidates(conn) == []  # an empty stub is not a spec
    conn.execute("UPDATE form_specs SET fields=? WHERE job_id=?", (json.dumps(FORM_FIELDS), jid))
    conn.commit()
    assert nightly.stage_candidates(conn) == [jid]
    assert halts.waiting(conn)[0].next_nightly_retries


def test_staging_halts_lists_only_jobs_waiting_on_the_human(conn: sqlite3.Connection) -> None:
    assert halts.digest_lines(conn) == []
    assert halts.report_lines(conn) == ["jobs halted in staging, waiting on you: none"]
    halted = _tailored(conn, "halted")
    _halt_row(conn, halted, "embedded-assessment-detected")
    question = _tailored(conn, "question")  # filled, one question left: open-queue's job
    _halt_row(conn, question, "required-fields-open:1", filled='[{"identity": "email"}]')
    staged = _tailored(conn, "staged")
    _halt_row(conn, staged, "", status="staged")
    got_past = _tailored(conn, "got-past")  # halted, then a later run staged it
    _halt_row(conn, got_past, "posting-not-found")
    _halt_row(conn, got_past, "", status="staged")
    for ext, sql in (
        ("submitted", "UPDATE applications SET submitted_at=datetime('now') WHERE job_id=?"),
        ("rejected", "UPDATE jobs SET status='rejected' WHERE id=?"),
        ("closed", "UPDATE jobs SET closed_at=datetime('now') WHERE id=?"),
    ):
        jid = _tailored(conn, ext)
        _halt_row(conn, jid, "captcha-or-turnstile-visible")
        conn.execute(sql, (jid,))
    conn.commit()

    assert [h.job_id for h in halts.waiting(conn)] == [halted]
    lines = halts.digest_lines(conn)
    assert lines[0] == "## Staging"
    assert lines[1].startswith(
        "3 form(s) filled and waiting for your review (2 fully staged, 1 with a question "
        "left for you): `jobkit open-queue`"
    )
    assert "### ⚠ Stopped before filling anything: waiting on you (1)" in lines
    (row,) = [ln for ln in lines if ln.startswith("- job ")]
    assert row.startswith(
        f"- job {halted} · Acme — Account Executive: embedded-assessment-detected "
        "(the form has an embedded assessment or timed test). Halted "
    )
    assert f"screenshot out/screenshots/staged-{halted}.png; apply at https://x.com." in row
    # schedule status lists it even with no nightly run on record (a manual staging run)
    report = nightly.report(conn)
    assert report.startswith("last nightly run: none on record\n")
    assert "⚠ jobs halted in staging, waiting on you (1):" in report
    assert f"job {halted} · Acme" in report and f"job {question} " not in report


def test_stage_failure_still_names_the_jobs_left_for_the_human(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake(
        c: sqlite3.Connection, ids: list[int], keep_open: bool = True, carry_forward: bool = True
    ) -> Any:
        return [
            {"job_id": ids[0], "status": "failed", "reason": "boom"},
            {"job_id": ids[1], "status": "needs_human", "reason": "posting-not-found"},
            {"job_id": ids[2], "status": "needs_human", "required_open": 2},
        ]

    monkeypatch.setattr(nightly, "stage_jobs", fake)
    a, b, c = _tailored(conn, "a"), _tailored(conn, "b"), _tailored(conn, "c")
    with pytest.raises(nightly.StagingIncomplete) as err:
        nightly.stage_tailored(conn)
    msg = str(err.value)
    assert msg.startswith(f"1 of 3 job(s) not staged (1 failed, 2 needs_human): job {a}: boom")
    assert msg.endswith(
        f". Needs you: job {b} Acme: posting-not-found; "
        f"job {c} Acme: 2 required question(s) open, open_queue reopens it"
    )


# ---------------------------------------------------------------------------
# a job whose staging keeps raising (no autofill_runs row) is capped
# ---------------------------------------------------------------------------

DEAD_URL_ERROR = (
    "Page.goto: net::ERR_NAME_NOT_RESOLVED at https://dead.example/apply\n"
    'Call log:\n  - navigating to "https://dead.example/apply", waiting until'
)


def _failures(conn: sqlite3.Connection, jid: int) -> list[str]:
    return [
        str(r[0])
        for r in conn.execute(
            "SELECT detail FROM events WHERE job_id=? AND kind=? ORDER BY id",
            (jid, q.NIGHTLY_STAGE_FAILED_EVENT),
        )
    ]


def test_a_job_that_fails_twice_is_handed_to_the_human_and_the_nightly_goes_green(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(nightly, "stage_jobs", _results("failed", DEAD_URL_ERROR))
    jid = _tailored(conn, "dead")
    stage = [("stage", nightly.stage_tailored)]

    # night 1: below the cap, the failure fails the stage and the job is retried
    (out,) = run_stages(conn, stage, mode="nightly")
    assert out.status == "error" and "1 of 1 job(s) not staged (1 failed)" in (out.error or "")
    assert _failures(conn, jid) == [" ".join(DEAD_URL_ERROR.split())]  # one line
    assert nightly.stage_candidates(conn) == [jid]
    assert halts.waiting(conn) == []
    assert last_successful_nightly(conn) is None

    # night 2: the failure that reaches the cap hands the job to the human
    (out,) = run_stages(conn, stage, mode="nightly")
    assert out.status == "ok", out.error
    assert out.summary.startswith(
        f"1 job(s): 1 failed (left for you). Needs you: job {jid} Acme: staging failed "
        "2 times, not retried: Page.goto: net::ERR_NAME_NOT_RESOLVED at https://dead.example"
    )
    assert "\n" not in out.summary
    assert len(_failures(conn, jid)) == q.NIGHTLY_STAGE_FAILURE_CAP == 2
    assert last_successful_nightly(conn) is not None  # the health signal recovers

    # night 3: no longer a candidate, so no browser opens on it
    assert nightly.stage_candidates(conn) == []
    (out,) = run_stages(conn, stage, mode="nightly")
    assert out.status == "ok" and out.summary == "nothing new to stage"
    calls = _results.calls  # type: ignore[attr-defined]
    assert [ids for ids, _, _ in calls] == [[jid], [jid]]

    # ...and it waits on the human with the error, everywhere halts are listed
    (halt,) = halts.waiting(conn)
    assert halt.job_id == jid and not halt.next_nightly_retries and halt.screenshot is None
    assert halt.reason.startswith("staging-failed:Page.goto: net::ERR_NAME_NOT_RESOLVED")
    assert "the nightly run stopped retrying it" in halt.meaning()
    assert halt.line().startswith(
        f"job {jid} · Acme — Account Executive: staging-failed:Page.goto: "
        "net::ERR_NAME_NOT_RESOLVED"
    )
    assert "apply at https://x.com." in halt.line()
    assert len(halt.brief()) < 120  # the get_queue line stays short
    digest = "\n".join(halts.digest_lines(conn))
    assert "### ⚠ Stopped before filling anything: waiting on you (1)" in digest
    assert f"- job {jid} · Acme — Account Executive: staging-failed:" in digest
    assert "⚠ jobs halted in staging, waiting on you (1):" in nightly.report(conn)
    queue = _mcp(conn, monkeypatch).get_queue()
    assert f"⚠ 1 job(s) stopped in staging and wait on you: job {jid} Acme (staging-failed:" in (
        queue
    )

    # re-tailoring starts the count over: the nightly tries it again
    conn.execute(
        "UPDATE applications SET updated_at=datetime('now', '+1 minute') WHERE job_id=?", (jid,)
    )
    conn.commit()
    assert nightly.stage_candidates(conn) == [jid]
    assert halts.waiting(conn) == []


def test_deferred_staging_is_never_capped(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(nightly, "stage_jobs", _results("deferred", "profile-locked-by-pid-77"))
    jid = _tailored(conn, "locked")
    for _ in range(3):
        (out,) = run_stages(conn, [("stage", nightly.stage_tailored)], mode="nightly")
        assert out.status == "error" and "1 deferred" in (out.error or "")
    assert _failures(conn, jid) == []
    assert nightly.stage_candidates(conn) == [jid]  # transient: always retried
    assert halts.waiting(conn) == []


def test_one_capped_job_does_not_hide_another_jobs_failure(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    dead, new = _tailored(conn, "dead"), _tailored(conn, "new")
    conn.execute(
        "INSERT INTO events (job_id, kind, detail) VALUES (?, ?, 'net::ERR_NAME_NOT_RESOLVED')",
        (dead, q.NIGHTLY_STAGE_FAILED_EVENT),
    )
    conn.commit()
    monkeypatch.setattr(nightly, "stage_jobs", _results("failed", "Timeout 60000ms exceeded"))
    with pytest.raises(nightly.StagingIncomplete) as err:
        nightly.stage_tailored(conn)
    msg = str(err.value)
    # the dead posting reached the cap and is handed over; the new failure still counts
    assert msg.startswith(
        f"1 of 2 job(s) not staged (1 failed, 1 failed (left for you)): "
        f"job {new}: Timeout 60000ms exceeded"
    )
    assert f"Needs you: job {dead} Acme: staging failed 2 times, not retried: " in msg
    assert nightly.stage_candidates(conn) == [new]
    assert [h.job_id for h in halts.waiting(conn)] == [dead]


def test_failed_list_yields_to_a_later_staging_run_and_lists_each_job_once(
    conn: sqlite3.Connection,
) -> None:
    def fail_twice(jid: int) -> None:
        for _ in range(2):
            conn.execute(
                "INSERT INTO events (job_id, kind, detail) VALUES (?, ?, 'boom')",
                (jid, q.NIGHTLY_STAGE_FAILED_EVENT),
            )
        conn.commit()

    # halted on an earlier night, then failed twice: listed once, as the failure
    earlier = _tailored(conn, "earlier")
    conn.execute(
        "INSERT INTO autofill_runs (job_id, status, abort_reason, filled_fields, created_at) "
        "VALUES (?, 'needs_human', 'posting-not-found', '[]', datetime('now', '-30 minutes'))",
        (earlier,),
    )
    fail_twice(earlier)
    (halt,) = halts.waiting(conn)
    assert halt.job_id == earlier and halt.reason == "staging-failed:boom"
    assert nightly.stage_candidates(conn) == []  # the cap holds over the posting-not-found retry

    # a staging run after the failures (the human staged it by hand) takes it
    # off the failed list; its own outcome decides where it shows up
    _halt_row(conn, earlier, "posting-not-found")
    (halt,) = halts.waiting(conn)
    assert halt.reason == "posting-not-found" and not halt.next_nightly_retries
    _halt_row(conn, earlier, "", status="staged")
    assert halts.waiting(conn) == []

    # submitted, rejected or closed: off the list
    for ext, sql in (
        ("submitted", "UPDATE applications SET submitted_at=datetime('now') WHERE job_id=?"),
        ("rejected", "UPDATE jobs SET status='rejected' WHERE id=?"),
        ("closed", "UPDATE jobs SET closed_at=datetime('now') WHERE id=?"),
    ):
        jid = _tailored(conn, ext)
        fail_twice(jid)
        conn.execute(sql, (jid,))
    conn.commit()
    assert halts.waiting(conn) == []

    # failures from before the newest tailoring do not count
    old = _tailored(conn, "old")
    fail_twice(old)
    conn.execute(
        "UPDATE applications SET updated_at=datetime('now', '+1 minute') WHERE job_id=?", (old,)
    )
    conn.commit()
    assert halts.waiting(conn) == [] and nightly.stage_candidates(conn) == [old]
