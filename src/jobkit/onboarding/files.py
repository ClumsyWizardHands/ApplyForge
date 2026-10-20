"""Where onboarding reads and writes (ONBOARDING.md, the setup interview).

Every path is resolved at call time from the project root
(paths.resolve_home(): JOBKIT_HOME, else this checkout), so the test sandbox
and a second checkout each get their own files. Nothing here is a module-level
path constant.

Files the setup interview writes (all personal and gitignored: data/* and
config/*.yaml, CLAUDE.md hard rule 13):

  data/career_corpus.draft.yaml   the corpus DRAFT: resume text only (hard rule 6)
  data/resume_critique.md         what to improve; never folded into the draft
  data/setup/resume_source.txt    the text the draft was taken from (the
                                  verbatim check runs against it)
  data/setup/*.answers.yaml       the interview record: what the human said
  data/setup/backup/              a file the interview replaced, kept here
                                  (config/*.yaml.bak would not be gitignored)
  data/career_corpus.yaml         the confirmed corpus (promote_corpus)
  data/answer_bank.yaml           the answer bank (build_answer_bank)
  config/filters.yaml, commute_table.yaml, autoapprove.yaml,
  banned_phrases.yaml             from the preferences interview (run_intake)
  config/targets.yaml             from board discovery (seed_targets)

A file the interview did not write (it has no GENERATED_MARK line) is never
replaced unless the caller passes replace_existing=True, and then the old
copy goes to data/setup/backup/ first: an existing setup keeps working."""

from __future__ import annotations

import datetime
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from jobkit import paths

# First-line marker of every file the setup interview writes. A file without
# it was written by hand (or by an older setup) and is protected.
GENERATED_MARK = "# Written by the jobkit setup interview"


class SetupError(ValueError):
    """A setup step cannot go ahead; the message says what the human must do."""


@dataclass(frozen=True)
class SetupFiles:
    root: Path

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def setup_dir(self) -> Path:
        return self.data / "setup"

    @property
    def backup_dir(self) -> Path:
        return self.setup_dir / "backup"

    # ---- corpus ----
    @property
    def corpus(self) -> Path:
        return self.data / "career_corpus.yaml"

    @property
    def corpus_draft(self) -> Path:
        return self.data / "career_corpus.draft.yaml"

    @property
    def critique(self) -> Path:
        return self.data / "resume_critique.md"

    @property
    def resume_text(self) -> Path:
        return self.setup_dir / "resume_source.txt"

    # ---- answer bank ----
    @property
    def bank(self) -> Path:
        return self.data / "answer_bank.yaml"

    @property
    def bank_record(self) -> Path:
        return self.setup_dir / "answer_bank.answers.yaml"

    # ---- config ----
    @property
    def preferences_record(self) -> Path:
        return self.setup_dir / "preferences.answers.yaml"

    @property
    def targets_record(self) -> Path:
        return self.setup_dir / "targets.answers.yaml"

    def config_file(self, name: str) -> Path:
        return self.config / name

    # ---- infrastructure ----
    @property
    def db(self) -> Path:
        return self.data / "jobkit.db"

    @property
    def mcp_json(self) -> Path:
        return self.root / ".mcp.json"

    def rel(self, path: Path) -> str:
        """`path` relative to the root, for messages."""
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)


def setup_files(root: Path | None = None) -> SetupFiles:
    return SetupFiles((root or paths.resolve_home()).resolve())


def _checkout_root() -> Path | None:
    """The jobkit checkout this package was loaded from (templates live there
    even when JOBKIT_HOME points somewhere else)."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "config").is_dir():
            return parent
    return None


def template_path(files: SetupFiles, rel: str) -> Path:
    """A tracked template (config/<name>.example.yaml, data/<name>.example.yaml):
    the root's copy when it has one, else the checkout's."""
    candidates = [files.root / rel]
    checkout = _checkout_root()
    if checkout is not None:
        candidates.append(checkout / rel)
    for c in candidates:
        if c.is_file():
            return c
    raise SetupError(f"template {rel} not found (is this a jobkit checkout?)")


def load_yaml(path: Path) -> Any:
    """yaml.safe_load of a file; None when it does not exist."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return yaml.safe_load(text)


def is_generated(path: Path) -> bool:
    """True when the file starts with the setup interview's marker line."""
    try:
        with path.open(encoding="utf-8") as fh:
            return fh.readline().startswith(GENERATED_MARK)
    except OSError:
        return False


def backup(files: SetupFiles, path: Path) -> Path | None:
    """Copy `path` into data/setup/backup/ before it is replaced. A file the
    interview wrote is kept as one rolling copy (<name>.prev); any other file
    gets a timestamped copy that is never overwritten. Returns the copy, or
    None when there was nothing to keep."""
    if not path.is_file():
        return None
    files.backup_dir.mkdir(parents=True, exist_ok=True)
    if is_generated(path):
        dest = files.backup_dir / f"{path.name}.prev"
    else:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = files.backup_dir / f"{path.name}.{stamp}"
        n = 1
        while dest.exists():
            n += 1
            dest = files.backup_dir / f"{path.name}.{stamp}-{n}"
    shutil.copy2(path, dest)
    return dest


def guard_overwrite(files: SetupFiles, path: Path, replace_existing: bool, what: str) -> None:
    """Refuse to replace a file the setup interview did not write, unless
    the human asked for it (replace_existing). A generated file may be
    rewritten: it is the interview's own output."""
    if path.is_file() and not is_generated(path) and not replace_existing:
        raise SetupError(
            f"{files.rel(path)} already exists and was not written by the setup interview "
            f"({what}). It is left as it is. To replace it, ask again with "
            "replace_existing=true; the current file is copied to data/setup/backup/ first."
        )


def write_text_atomic(path: Path, text: str) -> None:
    """Write `text` to `path` via a temp file in the same directory and a
    rename, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def dump_yaml(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


def today() -> str:
    return datetime.date.today().isoformat()
