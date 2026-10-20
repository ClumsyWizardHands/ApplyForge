"""Background processes started from the MCP server (audit 2026-10-08 §3 item 6).

The MCP tools that start work in the background (approve -> tailor, retailor,
stage_forms, restage, open_queue, run_followups) used to run
`uv run --no-sync jobkit …` with all output thrown away and return canned
success text. `--no-sync` also meant they ran a frozen install that ignored
source edits.

Every launch now:

* runs `python -m jobkit.cli …` with this process's own interpreter and the
  same jobkit source first on PYTHONPATH, so the child runs the code the
  server runs;
* runs in the project root, with JOBKIT_HOME set to it;
* writes its stdout and stderr to logs/spawn/<timestamp>-<command>.log;
* is recorded as a `spawn` event (args, pid, log path) and a `mcp.spawn` log
  line.

Later launches use the same path: MCP rescore(confirm=true) and a confirmed
corpus proposal (MCP confirm_proposal, `jobkit confirm-proposal`) both start
`jobkit rescore`.

statuses() reports each recent launch as it is: running, or its exit code
(recorded once, as a `spawn_exit` event), with the tail of its output. A launch
this server no longer holds (the server restarted) is judged by its pid: still
alive, or ended with the exit code unknown."""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import jobkit
import jobkit.log as joblog
from jobkit import db as q
from jobkit import paths
from jobkit.log import log

# launches this process started and can still reap, keyed by log path (unique
# per launch); the exit code is read from here
_CHILDREN: dict[str, subprocess.Popen[bytes]] = {}

TAIL_LINES = 6
# A launch this server no longer holds is judged by its pid only while it is
# younger than this: an older pid may have been reused by another process.
# (A long-open staging browser is still caught by the profile-lock check.)
PID_TRUST_HOURS = 12


def cli_argv(*args: str) -> list[str]:
    """The command line for `jobkit <args>` run with this interpreter."""
    return [sys.executable, "-m", "jobkit.cli", *args]


def child_env(home: Path) -> dict[str, str]:
    """This environment, plus the project root and the jobkit source this
    process imported first on PYTHONPATH (works even when the editable
    install's .pth file is skipped)."""
    src = str(Path(jobkit.__file__).resolve().parent.parent)
    env = dict(os.environ)
    rest = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([src, rest]) if rest else src
    env["JOBKIT_HOME"] = str(home)
    env.pop("UV_NO_EDITABLE", None)
    return env


def spawn_log_dir() -> Path:
    return joblog.LOG_PATH.parent / "spawn"


@dataclass(frozen=True)
class Launch:
    event_id: int
    pid: int
    args: tuple[str, ...]
    log_path: Path

    def describe(self) -> str:
        return (
            f"started `jobkit {' '.join(self.args)}` as launch #{self.event_id} "
            f"(pid {self.pid}); output: {self.log_path}"
        )


def spawn(conn: sqlite3.Connection, *args: str, job_id: int | None = None) -> Launch:
    """Start `jobkit <args>` in the background and record it. A launch that
    cannot start raises (and is logged); it never reports success."""
    home = paths.resolve_home()
    logdir = spawn_log_dir()
    logdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    log_path = logdir / f"{stamp}-{args[0] if args else 'jobkit'}.log"
    argv = cli_argv(*args)
    try:
        with log_path.open("ab") as out:
            out.write(f"$ jobkit {' '.join(args)}\n".encode())
            out.flush()
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                cwd=home,
                env=child_env(home),
                start_new_session=True,  # survives the MCP server; a staged browser stays up
            )
    except OSError as e:
        log("mcp.spawn_failed", args=list(args), error=str(e)[:200], log_path=str(log_path))
        raise RuntimeError(f"could not start `jobkit {' '.join(args)}`: {e}") from e
    detail = {"args": list(args), "pid": proc.pid, "log": str(log_path)}
    cur = conn.execute(
        q.RECORD_EVENT, {"job_id": job_id, "kind": "spawn", "detail": json.dumps(detail)}
    )
    conn.commit()
    event_id = int(cur.lastrowid or 0)
    _CHILDREN[str(log_path)] = proc
    log(
        "mcp.spawn", launch=event_id, pid=proc.pid, argv=argv, cwd=str(home), log_path=str(log_path)
    )
    return Launch(event_id=event_id, pid=proc.pid, args=tuple(args), log_path=log_path)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # signal 0: existence check only
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _age_hours(at: str) -> float:
    """Hours since an events.at timestamp (SQLite datetime('now'), UTC)."""
    try:
        then = datetime.datetime.strptime(at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.UTC)
    except ValueError:
        return float("inf")
    return (datetime.datetime.now(datetime.UTC) - then).total_seconds() / 3600


def _tail(path: Path, lines: int = TAIL_LINES) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(fh.tell() - 4096, 0))
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return [ln for ln in text.splitlines() if ln.strip()][-lines:]


@dataclass(frozen=True)
class SpawnStatus:
    event_id: int
    args: tuple[str, ...]
    pid: int
    started: str
    log_path: Path
    state: str  # running | exited | ended (exit code unknown)
    code: int | None

    @property
    def running(self) -> bool:
        return self.state == "running"

    def render(self) -> str:
        if self.state == "running":
            what = "running"
        elif self.code is None:
            what = "ended (exit code unknown: started before this MCP server; see the log)"
        else:
            what = "finished ok (exit 0)" if self.code == 0 else f"FAILED (exit {self.code})"
        lines = [
            f"#{self.event_id} `jobkit {' '.join(self.args)}` — started {self.started} UTC, "
            f"pid {self.pid}: {what}",
            f"    log: {self.log_path}",
        ]
        lines += [f"    | {ln[:200]}" for ln in _tail(self.log_path)]
        return "\n".join(lines)


def statuses(conn: sqlite3.Connection, limit: int = 8) -> list[SpawnStatus]:
    """The most recent launches, newest first, each with its real state. An
    exit code seen for the first time is recorded as a `spawn_exit` event."""
    out: list[SpawnStatus] = []
    for r in conn.execute(q.RECENT_SPAWNS, {"limit": limit}).fetchall():
        d = json.loads(r["detail"] or "{}")
        event_id = int(r["id"])
        pid = int(d.get("pid") or 0)
        log_path = Path(str(d.get("log") or ""))
        code: int | None = None
        if r["exit_detail"]:
            code = json.loads(r["exit_detail"]).get("code")
            state = "exited"
        elif str(log_path) in _CHILDREN:
            rc = _CHILDREN[str(log_path)].poll()
            if rc is None:
                state = "running"
            else:
                code, state = rc, "exited"
                _CHILDREN.pop(str(log_path), None)
                conn.execute(
                    q.RECORD_EVENT,
                    {
                        "job_id": r["job_id"],
                        "kind": "spawn_exit",
                        "detail": json.dumps({"spawn_event": event_id, "pid": pid, "code": rc}),
                    },
                )
                log("mcp.spawn_exit", launch=event_id, pid=pid, code=rc)
        elif _pid_alive(pid) and _age_hours(str(r["at"])) < PID_TRUST_HOURS:
            state = "running"
        else:
            state = "ended"
        out.append(
            SpawnStatus(
                event_id=event_id,
                args=tuple(str(a) for a in d.get("args") or ()),
                pid=pid,
                started=str(r["at"]),
                log_path=log_path,
                state=state,
                code=code,
            )
        )
    conn.commit()
    return out


def running(conn: sqlite3.Connection, commands: tuple[str, ...]) -> SpawnStatus | None:
    """The newest launch of one of these jobkit commands that is still running."""
    for s in statuses(conn, limit=25):
        if s.running and s.args and s.args[0] in commands:
            return s
    return None
