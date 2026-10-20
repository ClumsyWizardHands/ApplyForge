"""The project skills in .claude/skills/ ship with the repo: anyone who opens
the checkout in Claude gets the setup interview (jobkit-setup), the daily
routine (jobkit-daily) and the routine a Claude app scheduled task runs
(jobkit-scheduled-run). A skill that names a command or tool that does not
exist sends Claude down a dead end, so this checks every name they use:

  * every `jobkit <command> [<subcommand>] [--flags]` in an inline code span
    or a ```bash fenced block is a real CLI command with those flags (other
    fenced blocks, such as the scheduled task's prompt, are text);
  * every call written `tool(...)` anywhere in a skill is a jobkit MCP tool
    (or one of the Claude app's scheduled-task tools), and every `name=`
    argument is one of that tool's parameters.

It also checks that the scheduled run stays unattended-safe, that the setup
skill drives every setup step, that the human gates are stated, and that the
digest carries the follow-ups the scheduled run reports."""

from __future__ import annotations

import asyncio
import datetime
import re
import shlex
import sqlite3
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import typer
import yaml

from jobkit import cli, mcp_server
from jobkit.onboarding import mcp_tools
from jobkit.pipeline.digest import followup_lines, write_digest
from tests.conftest import REPO_ROOT
from tests.test_operations import _applied, _due_followups
from tests.test_setup_config import OWNER_MARKERS

SKILLS_DIR = REPO_ROOT / ".claude" / "skills"
SKILL_NAMES = ("jobkit-setup", "jobkit-daily", "jobkit-scheduled-run")
SKILL_FILES = sorted(SKILLS_DIR.rglob("*.md"))

# The Claude app's scheduled-task tools (not jobkit's), with their parameters.
CLAUDE_APP_TOOLS: dict[str, set[str]] = {
    "create_scheduled_task": {
        "taskId",
        "prompt",
        "description",
        "cronExpression",
        "fireAt",
        "title",
        "notifyOnCompletion",
    },
    "list_scheduled_tasks": set(),
    "update_scheduled_task": {
        "taskId",
        "prompt",
        "description",
        "cronExpression",
        "fireAt",
        "title",
        "enabled",
        "notifyOnCompletion",
    },
    "run_scheduled_task": {"taskId"},
}

FENCE = re.compile(r"^[ \t]*```([^\n`]*)\n(.*?)^[ \t]*```[ \t]*$", re.M | re.S)
SHELL_LANGS = {"bash", "sh", "shell", "zsh", "console"}
INLINE_CODE = re.compile(r"`([^`\n]+)`")
# `jobkit` as a command word: not part of a path, a name like jobkit-mcp or
# mcp__jobkit__x, or a quoted string; followed by a lowercase command name
CLI_WORD = re.compile(r"(?<![\w/.\-\"'])jobkit[ \t]+(?=[a-z])")
CALL = re.compile(r"(?<![\w.])([a-z_][a-z0-9_]*)\(([^()]*)\)")
QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")
KWARG = re.compile(r"(?<![\w\"'])([A-Za-z_]\w*)\s*=(?!=)")


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def _skill(name: str) -> str:
    return (SKILLS_DIR / name / "SKILL.md").read_text()


def _code_snippets(text: str) -> list[str]:
    """Inline code spans, plus every line of the shell-tagged fenced blocks."""
    snippets: list[str] = []

    def take(m: re.Match[str]) -> str:
        if m.group(1).strip().lower() in SHELL_LANGS:
            snippets.extend(m.group(2).splitlines())
        return ""

    prose = FENCE.sub(take, text)
    snippets += INLINE_CODE.findall(prose)
    return snippets


def cli_invocations(text: str) -> list[list[str]]:
    """Each `jobkit ...` command in the skill's code, as its argv after
    `jobkit` (up to a pipe, redirect or `;`/`&&`)."""
    out: list[list[str]] = []
    for snippet in _code_snippets(text):
        for m in CLI_WORD.finditer(snippet):
            # a separator or redirect starts after whitespace (`<path>` is a placeholder)
            rest = re.split(r"\s+(?:\|\||&&|\||;|\d?>>?)", snippet[m.end() :], maxsplit=1)[0]
            try:
                out.append(shlex.split(rest))
            except ValueError:
                out.append(rest.split())
    return out


def tool_calls(text: str) -> list[tuple[str, list[str]]]:
    """Each `name(...)` call in the skill, with the `arg=` names it passes."""
    return [(m.group(1), KWARG.findall(QUOTED.sub('""', m.group(2)))) for m in CALL.finditer(text)]


@cache
def _cli_root() -> Any:
    """The Typer app as a command tree (a group has `.commands`; Typer
    vendors its own click, so this is duck-typed)."""
    return typer.main.get_command(cli.app)


@cache
def _mcp_tools() -> dict[str, frozenset[str]]:
    tools = asyncio.run(mcp_server.mcp.list_tools())
    return {t.name: frozenset((t.input_schema or {}).get("properties", {})) for t in tools}


def _command_path(argv: list[str]) -> tuple[str, Any, list[str], str]:
    """('setup ingest', the command, its remaining args, ''), or the command
    None and why ('not a jobkit command', 'needs a subcommand')."""
    cmd = _cli_root()
    path: list[str] = []
    rest = list(argv)
    while hasattr(cmd, "commands"):
        if not rest or rest[0].startswith("-"):
            return " ".join(path), None, rest, "needs a subcommand"
        name = rest.pop(0)
        path.append(name)
        sub = cmd.commands.get(name)
        if sub is None:
            return " ".join(path), None, rest, "is not a jobkit command"
        cmd = sub
    return " ".join(path), cmd, rest, ""


def _cli_problems(text: str) -> list[str]:
    problems: list[str] = []
    for argv in cli_invocations(text):
        path, cmd, rest, why = _command_path(argv)
        if cmd is None:
            problems.append(f"`jobkit {path}` {why}")
            continue
        known = {o for p in cmd.params for o in (*p.opts, *p.secondary_opts)} | {"--help"}
        for token in rest:
            flag = token.split("=", 1)[0]
            if flag.startswith("-") and flag not in known:
                problems.append(f"`jobkit {path}` has no option {flag}")
    return problems


def _tool_problems(text: str) -> list[str]:
    tools = {**_mcp_tools(), **{k: frozenset(v) for k, v in CLAUDE_APP_TOOLS.items()}}
    problems: list[str] = []
    for name, kwargs in tool_calls(text):
        if name not in tools:
            problems.append(f"{name}() is not a jobkit MCP tool")
            continue
        problems += [f"{name}() has no parameter {k!r}" for k in kwargs if k not in tools[name]]
    return problems


# ---------------------------------------------------------------------------
# the skills ship, and are well-formed
# ---------------------------------------------------------------------------


def test_the_three_project_skills_ship() -> None:
    for name in SKILL_NAMES:
        assert (SKILLS_DIR / name / "SKILL.md").is_file(), name
    assert SKILL_FILES, "no skill files found"


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_skill_frontmatter(name: str) -> None:
    text = _skill(name)
    assert text.startswith("---\n")
    meta = yaml.safe_load(text.split("---\n", 2)[1])
    assert meta["name"] == name
    assert re.fullmatch(r"[a-z0-9-]{1,64}", meta["name"])
    description = meta["description"]
    assert isinstance(description, str) and 50 <= len(description) <= 1024


@pytest.mark.parametrize("path", SKILL_FILES, ids=_rel)
def test_every_jobkit_command_named_in_a_skill_exists(path: Path) -> None:
    assert _cli_problems(path.read_text()) == []


@pytest.mark.parametrize("path", SKILL_FILES, ids=_rel)
def test_every_mcp_tool_named_in_a_skill_exists(path: Path) -> None:
    assert _tool_problems(path.read_text()) == []


@pytest.mark.parametrize("path", SKILL_FILES, ids=_rel)
def test_skills_hold_no_owner_identifiers_or_machine_paths(path: Path) -> None:
    text = path.read_text()
    assert re.findall(r"/Users/|/home/|[A-Z]:\\Users", text) == []
    # the checkout owner's identifiers, when this checkout lists them
    # (docs/local/owner-markers.txt; see tests/test_setup_config.py)
    if OWNER_MARKERS is not None:
        assert sorted({m.group(0) for m in OWNER_MARKERS.finditer(text)}) == []


def test_the_checks_catch_a_wrong_name() -> None:
    """The scanners above are not vacuous: they see commands, flags, tools
    and arguments, and reject ones that do not exist."""
    good = (
        "Run `uv run --directory REPO jobkit stage --job-id 7 --keep-open` and "
        '`uv run jobkit setup ingest "<path>"`, then mark_submitted(job_ids="7").'
    )
    assert _cli_problems(good) == [] and _tool_problems(good) == []
    assert cli_invocations(good) == [
        ["stage", "--job-id", "7", "--keep-open"],
        ["setup", "ingest", "<path>"],
    ]
    bad = (
        "`uv run jobkit stage --open` `jobkit sync` `jobkit setup interview` `jobkit schedule`\n"
        "```bash\nuv run jobkit nightly --fast > logs/x.log 2>&1\n```\n"
        'mark_submited(job_ids="7") then mark_submitted(job_id="7")'
    )
    assert _cli_problems(bad) == [
        "`jobkit nightly` has no option --fast",  # fenced shell blocks are read first
        "`jobkit stage` has no option --open",
        "`jobkit sync` is not a jobkit command",
        "`jobkit setup interview` is not a jobkit command",
        "`jobkit schedule` needs a subcommand",
    ]
    assert _tool_problems(bad) == [
        "mark_submited() is not a jobkit MCP tool",
        "mark_submitted() has no parameter 'job_id'",
    ]
    # prose and a text block (the scheduled task's prompt) are not commands
    prose = "jobkit finds jobs.\n```\nDaily jobkit run. The jobkit checkout is REPO.\n```\n"
    assert cli_invocations(prose) == []


# ---------------------------------------------------------------------------
# what each skill must do
# ---------------------------------------------------------------------------


def _calls(name: str) -> set[str]:
    return {tool for tool, _ in tool_calls(_skill(name))}


def _commands(name: str) -> set[str]:
    return {_command_path(argv)[0] for argv in cli_invocations(_skill(name))}


def test_setup_skill_drives_every_setup_step() -> None:
    calls = _calls("jobkit-setup")
    assert {fn.__name__ for fn in mcp_tools.TOOLS} <= calls
    assert {"doctor", "run_nightly", "get_queue", "schedule_install"} <= calls
    # the Claude app scheduled task, created only after checking for an existing one
    assert {"list_scheduled_tasks", "create_scheduled_task", "update_scheduled_task"} <= calls
    commands = _commands("jobkit-setup")
    assert {
        "doctor",
        "init",
        "setup-status",
        "setup ingest",
        "setup draft",
        "setup confirm",
        "setup update-draft",
        "setup promote",
        "setup intake",
        "setup targets",
        "setup questions",
        "setup answers",
        "nightly",
        "schedule install",
    } <= commands


def test_setup_skill_schedules_the_scheduled_run_skill() -> None:
    """The scheduled task's prompt only points at the scheduled-run skill, so
    the Claude app task stays short and the routine updates with the repo."""
    text = _skill("jobkit-setup")
    prompt = re.search(r"```\n[ \t]*(Daily jobkit run\.[^\n]*)\n[ \t]*```", text)
    assert prompt is not None
    assert "REPO/.claude/skills/jobkit-scheduled-run/SKILL.md" in prompt.group(1)
    assert (SKILLS_DIR / "jobkit-scheduled-run" / "SKILL.md").is_file()
    assert len(prompt.group(1)) < 300


def test_daily_skill_covers_the_routine() -> None:
    calls = _calls("jobkit-daily")
    assert {
        "get_queue",
        "get_job",
        "approve",
        "reject",
        "snooze",
        "get_artifacts",
        "stage_forms",
        "open_queue",
        "get_novel_questions",
        "answer_novel",
        "mark_submitted",
        "record_outcome",
        "get_questions",
        "answer_question",
        "confirm_proposal",
        "get_followups",
        "mark_followup_sent",
    } <= calls
    assert {"stage", "open-queue"} <= _commands("jobkit-daily")
    assert "jobkit stage --job-id 412 --keep-open" in _skill("jobkit-daily")


# What an unattended run may do: start the nightly run and read its results.
# Approving, staging on request, answering, confirming, scheduling, sending:
# none of that happens with nobody watching.
SCHEDULED_RUN_COMMANDS = {"nightly", "digest", "doctor", "stats", "setup-status", "schedule status"}
SCHEDULED_RUN_TOOLS = {
    "run_nightly",
    "get_digest",
    "schedule_status",
    "background_status",
    "get_stats",
    "doctor",
    "setup_status",
}


def test_scheduled_run_is_unattended_safe() -> None:
    commands = _commands("jobkit-scheduled-run")
    assert {"nightly", "digest", "schedule status"} <= commands
    assert commands <= SCHEDULED_RUN_COMMANDS
    assert _calls("jobkit-scheduled-run") <= SCHEDULED_RUN_TOOLS


GATES = {
    "jobkit-setup": (
        "never default",
        "approve before tailoring",
        "the person submits",
        "never clicks submit",
        "facts only from the corpus",
        "salary history is never answered",
        "eeo questions are optional",
    ),
    "jobkit-daily": (
        "the person approves",
        "the person submits",
        "you never click",
        "facts only from the corpus",
        "it never sends",
    ),
    "jobkit-scheduled-run": (
        "never approves",
        "never submits",
        "the person clicks submit",
        "never answers a question",
        "never sends",
        "never solves",
    ),
}


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_skills_state_the_human_gates(name: str) -> None:
    text = " ".join(_skill(name).lower().split())
    assert [g for g in GATES[name] if g not in text] == []


# ---------------------------------------------------------------------------
# the digest carries the follow-ups the scheduled run reports
# ---------------------------------------------------------------------------


def test_digest_lists_due_followups_and_who_sends_them(conn: sqlite3.Connection) -> None:
    jid = _applied(conn)
    # due 3, 2 and 1 days ago (a day's margin: DUE_FOLLOWUPS compares with the UTC date)
    _due_followups(
        conn, jid, {"status_check": -3, "hiring_manager_note": -2, "close_as_ghosted": -1}
    )
    conn.execute(
        "UPDATE followups SET draft=? WHERE kind='status_check'",
        ('{"subject": "Checking in", "body": "Any update?"}',),
    )
    ids = {r["kind"]: r["id"] for r in conn.execute("SELECT id, kind FROM followups")}
    future = (datetime.date.today() + datetime.timedelta(days=5)).isoformat()
    conn.execute(
        "INSERT INTO followups (application_id, kind, due_at) "
        "SELECT application_id, 'status_check', ? FROM followups LIMIT 1",
        (future,),
    )
    conn.commit()
    three_days_ago = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()

    lines = followup_lines(conn)
    assert lines[0] == "## Follow-ups due (3)"
    assert lines[1] == (
        f"- [{ids['status_check']}] status_check · due {three_days_ago} · "
        f"Acme — Account Executive (job {jid}): draft ready"
    )
    assert lines[2].startswith(f"- [{ids['hiring_manager_note']}] hiring_manager_note")
    assert lines[2].endswith(": no draft yet (the follow-ups stage drafts it)")
    assert lines[3].startswith(f"- [{ids['close_as_ghosted']}] close_as_ghosted")
    assert lines[3].endswith(": no reply: the follow-ups stage closes it as ghosted")
    assert "jobkit never sends" in lines[4] and "mark_followup_sent" in lines[4]
    assert lines[5] == "" and len(lines) == 6
    assert future not in "\n".join(lines)  # not due yet

    digest = write_digest(conn).read_text()
    assert "\n## Follow-ups due (3)\n" in digest
    assert digest.index("## Review queue") < digest.index("## Follow-ups due")
    assert digest.index("## Follow-ups due") < digest.index("## Funnel")


def test_digest_has_no_followups_section_when_none_is_due(conn: sqlite3.Connection) -> None:
    jid = _applied(conn)
    _due_followups(conn, jid, {"status_check": 4})
    assert followup_lines(conn) == []
    assert "Follow-ups due" not in write_digest(conn).read_text()
