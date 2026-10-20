"""One project root for every data path (audit 2026-10-08 §3 item 2).

The root is JOBKIT_HOME when that is set. Otherwise it is the jobkit checkout
this package was loaded from: the nearest parent directory of this file that
holds a pyproject.toml for the `jobkit` project. Both an editable install
(src/jobkit) and a .venv inside the checkout find it. Only a wheel installed
outside any checkout falls back to the working directory — and even then no
database is created by accident (db.connect refuses; only `jobkit init`
creates one).

HOME is resolved once, at import. Data paths that are written (the DB, the
log, out/, data/raw, the browser profile) are built from it, so they no longer
depend on the directory a process was started from. The CLI and the MCP
server also call enter_home() at start: it changes into the root, so the
read-only relative paths (config/, prompts/, templates/, registries/) and the
relative artifact paths stored in older DB rows resolve, and it loads .env."""

from __future__ import annotations

import os
import re
from pathlib import Path

_JOBKIT_PROJECT = re.compile(r"""^\s*name\s*=\s*["']jobkit["']\s*$""", re.M)


def resolve_home() -> Path:
    """The project root, read now: JOBKIT_HOME, else the enclosing jobkit
    checkout, else the working directory."""
    env = os.environ.get("JOBKIT_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    for parent in Path(__file__).resolve().parents:
        pyproject = parent / "pyproject.toml"
        try:
            if pyproject.is_file() and _JOBKIT_PROJECT.search(pyproject.read_text()):
                return parent
        except OSError:
            continue
    return Path.cwd().resolve()


HOME = resolve_home()


def enter_home() -> Path:
    """Entry-point setup for the CLI and the MCP server: change into the
    project root and load its .env (python-dotenv). Variables already in the
    environment win over .env. Returns the root."""
    from dotenv import load_dotenv

    home = resolve_home()
    os.chdir(home)
    load_dotenv(home / ".env", override=False)
    return home
