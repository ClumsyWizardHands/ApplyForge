"""The test sandbox itself (tests/conftest.py, hard rule 12): no test may reach
the user's DB, log, outputs, or personal YAML, spawn a real process, or touch
the network. If one of these fails, every other test result is suspect."""

from __future__ import annotations

import importlib
import os
import socket
import sqlite3
import subprocess
from pathlib import Path

import pytest

from jobkit import db as q
from jobkit.log import log
from tests.conftest import (
    PRODUCTION_DB,
    REPO_ROOT,
    SANDBOXED_PATHS,
    SpawnRecorder,
    refuse_production_db,
)


@pytest.mark.parametrize(("mod_name", "attr", "rel"), SANDBOXED_PATHS)
def test_path_constants_point_into_sandbox(
    jobkit_home: Path, mod_name: str, attr: str, rel: str
) -> None:
    value = Path(getattr(importlib.import_module(mod_name), attr))
    assert value.is_absolute()
    assert value == jobkit_home / rel
    assert not value.resolve().is_relative_to(REPO_ROOT)


def test_cwd_is_sandbox(jobkit_home: Path) -> None:
    assert Path.cwd() == jobkit_home
    # the hard-coded relative personal paths in src/ resolve into the sandbox
    assert not Path("data/career_corpus.yaml").exists()
    assert not Path("data/answer_bank.yaml").exists()
    assert Path("config").is_dir() and not Path("config").is_symlink()  # a private copy
    assert Path("prompts").is_dir() and Path("templates").is_dir()


def test_log_writes_land_in_sandbox(jobkit_home: Path) -> None:
    log("isolation.probe", n=1)
    assert "isolation.probe" in (jobkit_home / "logs" / "jobkit.jsonl").read_text()


def test_default_db_is_sandboxed(jobkit_home: Path) -> None:
    c = q.connect(create=True)  # only `jobkit init` creates a DB outside tests
    q.init_db(c)
    c.close()
    assert (jobkit_home / "data" / "jobkit.db").exists()


@pytest.mark.parametrize(
    "spelling",
    [
        PRODUCTION_DB,
        str(PRODUCTION_DB),
        os.fsencode(PRODUCTION_DB),
        PRODUCTION_DB.parent / ".." / "data" / "jobkit.db",
        f"file:{PRODUCTION_DB}?mode=ro",
        PRODUCTION_DB.with_name("jobkit.db-wal"),
    ],
    ids=["path", "str", "bytes", "dotdot", "uri", "wal"],
)
def test_production_db_is_refused(spelling: object) -> None:
    with pytest.raises(RuntimeError, match="PRODUCTION database"):
        refuse_production_db(spelling)


def test_sqlite_connect_refuses_production_db() -> None:
    # read-only URI: even if the guard were broken, nothing could be written
    with pytest.raises(RuntimeError, match="PRODUCTION database"):
        sqlite3.connect(f"file:{PRODUCTION_DB}?mode=ro", uri=True)


def test_sandbox_and_memory_dbs_allowed(tmp_path: Path) -> None:
    refuse_production_db(":memory:")
    refuse_production_db(tmp_path / "x.db")
    refuse_production_db("data/jobkit.db")  # relative: resolves into the sandbox


def test_subprocess_refused() -> None:
    with pytest.raises(RuntimeError, match="spawn a process"):
        subprocess.run(["true"], check=False)
    with pytest.raises(RuntimeError, match="spawn a process"):
        subprocess.Popen(["true"])
    with pytest.raises(RuntimeError, match="spawn a process"):
        os.system("true")


def test_spawn_recorder(spawned: SpawnRecorder) -> None:
    out = subprocess.run(["uv", "run", "jobkit", "tailor"], capture_output=True, text=True)
    subprocess.Popen(["jobkit", "stage", "--job-id", "1"])
    assert out.returncode == 0
    assert spawned.calls == [
        ["uv", "run", "jobkit", "tailor"],
        ["jobkit", "stage", "--job-id", "1"],
    ]


def test_network_refused() -> None:
    with pytest.raises(RuntimeError, match="offline"):
        socket.getaddrinfo("boards-api.greenhouse.io", 443)
    # TEST-NET-1 (RFC 5737): unroutable even if the guard were broken
    with pytest.raises(RuntimeError, match="offline"):
        socket.create_connection(("192.0.2.1", 9), timeout=0.01)


def test_loopback_allowed() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    cli = socket.create_connection(srv.getsockname(), timeout=1)
    cli.close()
    srv.close()


def test_llm_api_key_unset() -> None:
    assert "ANTHROPIC_API_KEY" not in os.environ
