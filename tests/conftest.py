from __future__ import annotations

import importlib
import ipaddress
import json
import os
import shutil
import socket
import sqlite3
import stat as statmod
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parent.parent
# The config every test runs against (the "test profile"). The repo's own
# config/*.yaml are personal, gitignored and absent on a fresh clone: tests
# never read them.
TEST_CONFIG = FIXTURES / "config"

# Something on this machine stamps macOS UF_HIDDEN onto venv .pth files, and
# Python 3.12+ silently skips hidden .pth files — which breaks the editable
# install. Self-heal here so the dev loop never dies on it again.
_sp = (
    Path(sys.prefix)
    / "lib"
    / f"python{sys.version_info.major}.{sys.version_info.minor}"
    / "site-packages"
)
if sys.platform == "darwin" and _sp.is_dir():
    _UF_HIDDEN = getattr(statmod, "UF_HIDDEN", 0)
    for _pth in _sp.glob("*.pth"):
        if getattr(os.lstat(_pth), "st_flags", 0) & _UF_HIDDEN:
            subprocess.run(["chflags", "nohidden", str(_pth)], check=False)

from jobkit import db as q  # noqa: E402 — must come after the .pth self-heal

# ---------------------------------------------------------------------------
# Test sandbox (CLAUDE.md hard rule 12). Every test runs inside its own
# throwaway JOBKIT_HOME under tmp_path and can never reach the user's data:
#
#   * cwd is the sandbox, so any remaining cwd-relative path in src/ (there are
#     several hard-coded "data/..." strings) resolves inside it;
#   * every module-level path constant that writes data or reads personal data
#     is redirected to the sandbox explicitly (SANDBOXED_PATHS), so the guard
#     still holds once those constants become absolute;
#   * sqlite3.connect refuses the production DB, by any spelling of its path;
#   * spawning a process or opening a non-loopback socket raises — a test that
#     needs a spawn uses the `spawned` recorder fixture below.
#
# The sandbox mirrors the repo layout: config/ is a private copy (tests may
# write it) of the shipped templates (config/*.example.yaml) plus the test
# profile (tests/fixtures/config/*.yaml) — never the repo's own config/*.yaml,
# which are personal, gitignored and absent on a fresh clone. prompts/
# registries/ templates/ are read-only symlinks, and data/ logs/ out/ start
# empty. data/career_corpus.yaml and data/answer_bank.yaml are deliberately
# absent: a test that needs them writes a fixture there.
# ---------------------------------------------------------------------------

PRODUCTION_DB = REPO_ROOT / "data" / "jobkit.db"

# (module, attribute, path inside the sandbox). A new path constant in src/
# that writes anything or reads personal data MUST be added here; a renamed
# one fails every test with AttributeError until this table is updated.
SANDBOXED_PATHS: tuple[tuple[str, str, str], ...] = (
    ("jobkit.db", "DB_PATH", "data/jobkit.db"),
    ("jobkit.log", "LOG_PATH", "logs/jobkit.jsonl"),
    ("jobkit.pipeline.tailor", "OUT_DIR", "out"),
    ("jobkit.pipeline.tailor", "CORPUS_PATH", "data/career_corpus.yaml"),
    ("jobkit.pipeline.triage", "CORPUS_PATH", "data/career_corpus.yaml"),
    ("jobkit.pipeline.digest", "DIGEST_DIR", "out/digest"),
    ("jobkit.pipeline.poll", "RAW_DIR", "data/raw"),
    ("jobkit.autofill.browser", "PROFILE_DIR", "data/browser_profile"),
    ("jobkit.autofill.browser", "SHOT_DIR", "out/screenshots"),
    # the recruiter-named resume / cover letter copies staging uploads
    ("jobkit.autofill.browser", "UPLOAD_DIR", "out/uploads"),
    ("jobkit.autofill.answer_bank", "BANK_PATH", "data/answer_bank.yaml"),
    # banned_phrases.yaml: authenticity config and the estimate_style knob
    ("jobkit.validate.style", "CONFIG_PATH", "config/banned_phrases.yaml"),
    # ~/Library/LaunchAgents: `jobkit schedule install` writes the nightly plist here
    ("jobkit.schedule", "LAUNCH_AGENTS_DIR", "LaunchAgents"),
    # questions for the human: confirm_proposal writes the corpus; answers land
    # in the proposals file first (package 3)
    ("jobkit.pipeline.questions", "CORPUS_PATH", "data/career_corpus.yaml"),
    ("jobkit.pipeline.questions", "PROPOSALS_PATH", "data/corpus_proposals.yaml"),
)
_LINKED_DIRS = ("prompts", "registries", "templates")


def _production_db_files() -> set[Path]:
    base = PRODUCTION_DB.resolve()
    return {base, *(base.with_name(base.name + sfx) for sfx in ("-wal", "-shm", "-journal"))}


def refuse_production_db(database: object) -> None:
    """Raise if `database` (as passed to sqlite3.connect) names the live DB."""
    if isinstance(database, bytes):
        database = os.fsdecode(database)
    raw = str(database)
    if raw in ("", ":memory:"):
        return
    if raw.startswith("file:"):
        raw = unquote(urlparse(raw).path)
        if not raw or raw == ":memory:":
            return
    try:
        resolved = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError):
        return
    if resolved in _production_db_files():
        raise RuntimeError(
            f"test opened the PRODUCTION database {resolved} (hard rule 12). "
            "Use the `conn` fixture or the sandboxed jobkit.db.DB_PATH."
        )


_real_sqlite_connect = sqlite3.connect


def _guarded_sqlite_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
    refuse_production_db(database)
    return _real_sqlite_connect(database, *args, **kwargs)


def _refuse_spawn(*args: Any, **kwargs: Any) -> Any:
    cmd = args[0] if args else kwargs.get("args")
    raise RuntimeError(
        f"test tried to spawn a process: {cmd!r}. Tests never run real commands "
        "(no `jobkit tailor`, no `claude -p`, no browser). Request the `spawned` "
        "fixture to record the spawn instead."
    )


class _RefusingPopen(subprocess.Popen):  # type: ignore[type-arg]
    """Replaces subprocess.Popen (and so subprocess.run). A subclass, not a bare
    function, so `isinstance` and import-time `Popen[bytes]` annotations still work."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _refuse_spawn(*args, **kwargs)


_LOOPBACK_NAMES = {None, "", "localhost", "localhost.localdomain"}


def _is_local_host(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


def _check_address(address: Any) -> None:
    if isinstance(address, tuple) and address and not _is_local_host(address[0]):
        raise RuntimeError(
            f"test tried to open a network connection to {address!r}. Tests run "
            "offline against recorded fixtures (hard rule 12)."
        )


_real_sock_connect = socket.socket.connect
_real_sock_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


def _guarded_sock_connect(self: socket.socket, address: Any) -> None:
    _check_address(address)
    _real_sock_connect(self, address)


def _guarded_sock_connect_ex(self: socket.socket, address: Any) -> int:
    _check_address(address)
    return _real_sock_connect_ex(self, address)


def _guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
    if not _is_local_host(host):
        try:
            ipaddress.ip_address(str(host))  # a literal IP needs no DNS; connect() decides
        except ValueError:
            raise RuntimeError(
                f"test tried to resolve {host!r}. Tests run offline against recorded "
                "fixtures (hard rule 12)."
            ) from None
    return _real_getaddrinfo(host, *args, **kwargs)


@pytest.fixture(autouse=True)
def jobkit_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Autouse: run every test inside a throwaway JOBKIT_HOME. Request it by
    name to get the sandbox root (e.g. to drop a fixture corpus in data/)."""
    home = tmp_path / "jobkit_home"
    for d in ("data", "logs", "out"):
        (home / d).mkdir(parents=True)
    config_dir = home / "config"
    config_dir.mkdir()
    for template in sorted((REPO_ROOT / "config").glob("*.example.yaml")):
        shutil.copy2(template, config_dir / template.name)
    for profile in sorted(TEST_CONFIG.glob("*.yaml")):
        shutil.copy2(profile, config_dir / profile.name)
    for d in _LINKED_DIRS:
        (home / d).symlink_to(REPO_ROOT / d, target_is_directory=True)

    for mod_name, attr, rel in SANDBOXED_PATHS:
        mod = importlib.import_module(mod_name)
        original = getattr(mod, attr)  # AttributeError: update SANDBOXED_PATHS
        monkeypatch.setattr(mod, attr, type(original)(home / rel))

    monkeypatch.chdir(home)
    monkeypatch.setenv("JOBKIT_HOME", str(home))
    # an un-mocked LLM call must fail here, not reach the API or `claude -p`
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # the User-Agent contact comes from the sandbox (contact.py), never the shell
    monkeypatch.delenv("JOBKIT_CONTACT_EMAIL", raising=False)
    monkeypatch.setattr(sqlite3, "connect", _guarded_sqlite_connect)
    monkeypatch.setattr(subprocess, "Popen", _RefusingPopen)
    monkeypatch.setattr(os, "system", _refuse_spawn)
    monkeypatch.setattr(socket.socket, "connect", _guarded_sock_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_sock_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    # the "Claude login expired" latch is process-wide (llm.py): no test inherits it
    from jobkit import llm

    llm.reset_backend_problem()
    yield home
    llm.reset_backend_problem()


class SpawnRecorder:
    """Records every subprocess spawn (Popen, run, check_output) as an argv list.
    Nothing runs: each spawn looks like a process that exited 0 with no output."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        calls = self.calls

        class _RecordedPopen(subprocess.Popen):  # type: ignore[type-arg]
            def __init__(self, args: Any, *rest: Any, **kwargs: Any) -> None:
                calls.append(
                    [os.fsdecode(args)]
                    if isinstance(args, str | bytes | os.PathLike)
                    else [os.fsdecode(a) if isinstance(a, bytes) else str(a) for a in args]
                )
                self.args = args
                self.pid = 0
                self.returncode = 0
                self.stdin = self.stdout = self.stderr = None
                self._text = any(kwargs.get(k) for k in ("text", "universal_newlines", "encoding"))

            def communicate(self, input: Any = None, timeout: float | None = None) -> Any:
                return ("", "") if self._text else (b"", b"")

            def wait(self, timeout: float | None = None) -> int:
                return 0

            def poll(self) -> int:
                return 0

            def send_signal(self, sig: int) -> None:
                return None

            def kill(self) -> None:
                return None

            def terminate(self) -> None:
                return None

            def __exit__(self, *exc: object) -> None:
                return None

            def __del__(self) -> None:
                return None

        self.popen = _RecordedPopen


@pytest.fixture()
def spawned(jobkit_home: Path, monkeypatch: pytest.MonkeyPatch) -> SpawnRecorder:
    """Opt-in: record subprocess spawns (Popen and run) instead of refusing them."""
    rec = SpawnRecorder()
    monkeypatch.setattr(subprocess, "Popen", rec.popen)
    return rec


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = q.connect(tmp_path / "test.db", create=True)
    q.init_db(c)
    return c


def load_fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())
