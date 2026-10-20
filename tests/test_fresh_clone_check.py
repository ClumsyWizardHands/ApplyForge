"""scripts/fresh_clone_check.sh stays true to what ships (2026-10-12).

The script clones the repository and runs uv, so it is not run here (the test
sandbox refuses a spawn). These checks keep its expectations in step with the
repository instead: every file it requires a clone to hold exists and covers
every personal config file; every personal or machine-local path is one it
flags in a clone, and nothing that ships is; and it only ever reads the
checkout it runs in, writing nothing but its own temporary folder."""

from __future__ import annotations

import os
import re

import pytest

from jobkit.config import PERSONAL_CONFIG_FILES
from tests.conftest import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "fresh_clone_check.sh"
TEXT = SCRIPT.read_text()
CODE = "\n".join(line for line in TEXT.splitlines() if not line.lstrip().startswith("#"))


def _shell_value(name: str) -> str:
    """The value of a NAME='...' or NAME="..." assignment in the script."""
    m = re.search(rf"^{name}=(['\"])(.*?)\1$", TEXT, re.M | re.S)
    assert m, f"no {name}= in {SCRIPT.name}"
    return m.group(2)


PERSONAL = re.compile(_shell_value("PERSONAL_RE"))
SHIPPED = re.compile(_shell_value("SHIPPED_RE"))
REQUIRED = _shell_value("REQUIRED").split()


def _flagged(path: str) -> bool:
    """What the script's personal_paths() reports, for one repo-relative path."""
    return bool(PERSONAL.search(path)) and not SHIPPED.search(path)


def test_script_is_an_executable_bash_script() -> None:
    assert TEXT.startswith("#!/usr/bin/env bash\n")
    assert os.access(SCRIPT, os.X_OK)


@pytest.mark.parametrize("path", REQUIRED)
def test_every_required_file_exists(path: str) -> None:
    assert (REPO_ROOT / path).is_file(), path


def test_a_clone_must_hold_every_personal_config_template() -> None:
    # Step 4 also derives the files doctor must name from these entries.
    for name in PERSONAL_CONFIG_FILES:
        assert f"config/{name.removesuffix('.yaml')}.example.yaml" in REQUIRED, name
    for name in ("career_corpus", "answer_bank"):
        assert f"data/{name}.example.yaml" in REQUIRED
    for skill in sorted((REPO_ROOT / ".claude" / "skills").glob("*/SKILL.md")):
        assert str(skill.relative_to(REPO_ROOT)) in REQUIRED, skill.parent.name


PERSONAL_PATHS = [
    *(f"config/{name}" for name in PERSONAL_CONFIG_FILES),
    "config/comp_benchmarks.yaml",
    "data/career_corpus.yaml",
    "data/career_corpus.draft.yaml",
    "data/answer_bank.yaml",
    "data/resume_critique.md",
    "data/setup/intake.answers.yaml",
    "data/jobkit.db",
    "data/jobkit.db-wal",
    "data/raw/greenhouse/acme.json",
    "data/browser_profile/Default/Cookies",
    ".env",
    ".mcp.json",
    "logs/jobkit.jsonl",
    "out/digest/2026-10-12.md",
    ".claude/settings.local.json",
    # local working notes (.gitignore): docs/local/ and the dated names
    "docs/local/notes.md",
    "docs/audit-2026-10-08.md",
    "docs/audit-2026-10-08-findings.json",
    "docs/residual-issues-2026-10-08.json",
    "docs/corpus-proposed-2026-10-08.diff",
]


@pytest.mark.parametrize("path", PERSONAL_PATHS)
def test_personal_and_local_paths_are_flagged(path: str) -> None:
    assert _flagged(path), path


def test_every_gitignored_docs_rule_is_flagged() -> None:
    # .gitignore keeps local working notes out of docs/; a clone, or the
    # history, holding one is reported as a personal path too
    lines = (REPO_ROOT / ".gitignore").read_text().splitlines()
    rules = [line.strip() for line in lines if line.strip().startswith("docs/")]
    assert "docs/local/" in rules
    for rule in rules:
        example = rule.replace("*", "x") + ("notes.md" if rule.endswith("/") else "")
        assert _flagged(example), rule


def _shipped_paths() -> list[str]:
    globs = ("config/*.example.yaml", "data/*.example.yaml", ".claude/skills/*/SKILL.md")
    found = {str(p.relative_to(REPO_ROOT)) for g in globs for p in REPO_ROOT.glob(g)}
    return sorted(
        found
        | set(REQUIRED)
        | {
            "tests/fixtures/config/filters.yaml",  # the tracked test profile
            "src/jobkit/db.py",
            "src/jobkit/onboarding/status.py",
            "scripts/fresh_clone_check.sh",
            # tracked docs ship
            "README.md",
            "CLAUDE.md",
            "docs/endpoints_verified.md",
            "docs/research-2026-09-02.md",
            "docs/why-no-linkedin.md",
        }
    )


@pytest.mark.parametrize("path", _shipped_paths())
def test_shipped_files_are_not_flagged(path: str) -> None:
    assert not _flagged(path), path


def test_reads_but_never_changes_this_checkout() -> None:
    # git against the checkout itself ($ROOT): read-only subcommands only
    on_root = re.findall(r'\bgit -C "\$ROOT" ([a-z-]+)', CODE)
    assert on_root and set(on_root) <= {"rev-parse", "symbolic-ref", "log", "ls-files"}
    # git with an explicit target: the checkout, the snapshot or the clone
    targets = set(re.findall(r'\bgit -C "(\$[A-Z]+)"', CODE))
    assert targets <= {"$ROOT", "$SNAP", "$CLONE"}
    # git without -C: init (of the snapshot), clone (into the clone), and
    # ls-files (the read-only listing of this checkout)
    bare = set(re.findall(r"(?<![\w-])git (?!-C)([a-z][a-z-]*)", CODE))
    assert bare <= {"init", "clone", "ls-files"}
    assert re.findall(r'\bgit init -q "([^"]+)"', CODE) == ["$SNAP"]
    # the snapshot is only ever the temporary folder's
    assert re.findall(r"^\s*SNAP=(.*)$", CODE, re.M) == ['"$TMP/snapshot"']
    # writes go to the temporary folder: extraction into the snapshot only,
    # and the only removal is of the temporary folder
    assert re.findall(r'tar -xf - -C "([^"]+)"', CODE) == ["$SNAP"]
    assert re.findall(r"\brm -rf? (\S+)", CODE) == ['"$TMP"']
    assert not re.search(r"\bgit\b[^\n]*\b(push|reset|stash|checkout -- |clean)\b", CODE)
