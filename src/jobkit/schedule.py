"""`jobkit schedule install|uninstall|status`: the nightly run under macOS
launchd (approved 2026-10-08; replaces scripts/install_schedule.sh).

One LaunchAgent, `com.jobkit.nightly`, in ~/Library/LaunchAgents. It runs

    <absolute uv> run --directory <project root> jobkit nightly

at `schedule.nightly_at` (config/filters.yaml, default "02:00", local time),
with WorkingDirectory = the project root, its output in logs/nightly.out.log
and logs/nightly.err.log, and an explicit PATH: launchd starts jobs with a
bare PATH, so the directories of `uv` and `claude` (the LLM backend without
ANTHROPIC_API_KEY) are put first, then /usr/bin:/bin:/usr/sbin:/sbin. Both
binaries are resolved with shutil.which at install time. JOBKIT_HOME pins
the project root, and PYTHONPATH=<root>/src keeps `import jobkit` working
even when macOS hides the editable install's .pth file.

The old script ran `UV_NO_EDITABLE=1 uv run --no-sync`: a frozen install that
ignored source edits. Neither is used here.

install refuses a project root (or its data/config/logs/out/.env, through
symlinks) under ~/Desktop, ~/Documents, ~/Downloads or iCloud Drive: macOS
privacy protection stops launchd jobs from reading them (audit 2026-10-08 §2
item 7: an agent there exited 78 on every run). launchd does not wake the
Mac: a run missed while it slept happens at the next wake, and one missed
while it was shut down is skipped.

Only macOS is supported; elsewhere every subcommand says so (run
`jobkit nightly` from cron or a systemd timer yourself). launchctl is called
through _launchctl only, which the tests replace: no test loads an agent."""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from jobkit import paths
from jobkit.config import load_filters
from jobkit.db import OPEN_QUEUE_JOBS
from jobkit.log import log
from jobkit.pipeline import nightly

LABEL = "com.jobkit.nightly"
# the two agents scripts/install_schedule.sh installed before 2026-10-08; the
# nightly replaces them, so install and uninstall remove them
LEGACY_LABELS = ("com.jobkit.morning", "com.jobkit.followups")
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
LAUNCHCTL = "/bin/launchctl"
SYSTEM_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
OUT_LOG = Path("logs") / "nightly.out.log"
ERR_LOG = Path("logs") / "nightly.err.log"
# folders macOS privacy protection (TCC) keeps launchd jobs out of
PROTECTED_DIRS = ("Desktop", "Documents", "Downloads", "Library/Mobile Documents")
# what the nightly run reads or writes under the root
CHECKED_UNDER_ROOT = (
    ".",
    "src",
    "config",
    "prompts",
    "templates",
    "data",
    "data/jobkit.db",
    "data/career_corpus.yaml",
    "data/answer_bank.yaml",
    "data/browser_profile",
    "logs",
    "out",
    ".env",
)


class ScheduleError(RuntimeError):
    """The schedule cannot be installed or removed as asked; nothing changed
    unless the message says so."""


class ScheduleUnsupported(ScheduleError):
    """Not macOS: the LaunchAgent doesn't apply."""


def _platform() -> str:
    return sys.platform


def require_macos() -> None:
    plat = _platform()
    if plat != "darwin":
        if plat == "win32":
            how = "a scheduled task in the Claude desktop app or Windows Task Scheduler"
        else:
            how = "cron or a systemd user timer"
        raise ScheduleUnsupported(
            f"jobkit schedule sets up a macOS LaunchAgent, so it doesn't apply on {plat}. "
            f"Run `jobkit nightly` once a day, from the project root, with {how}."
        )


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    """The only place launchctl runs (tests replace this function)."""
    return subprocess.run(
        [LAUNCHCTL, *args], capture_output=True, text=True, timeout=30, check=False
    )


def _domain() -> str:
    return f"gui/{os.getuid()}"


def plist_path(label: str = LABEL) -> Path:
    return LAUNCH_AGENTS_DIR / f"{label}.plist"


def protected_location(path: Path) -> str | None:
    """The protected folder `path` resolves into (symlinks followed), or None."""
    home = Path.home()
    resolved = path.resolve()
    for name in PROTECTED_DIRS:
        base = home / name
        for candidate in {base, base.resolve()}:
            if resolved.is_relative_to(candidate):
                return str(base)
    return None


def check_root(root: Path) -> None:
    """Refuse a project root launchd cannot read (see the module docstring)."""
    for rel in CHECKED_UNDER_ROOT:
        p = root / rel
        where = protected_location(p)
        if where is None:
            continue
        what = "the project root" if rel == "." else f"{p} (resolves to {p.resolve()})"
        raise ScheduleError(
            f"not installed: {what} is under {where}. macOS stops launchd jobs from "
            "reading ~/Desktop, ~/Documents, ~/Downloads and iCloud Drive, so the nightly "
            "run would fail every night (audit 2026-10-08 §2 item 7). Move the checkout "
            "(and its data) somewhere like ~/code/jobkit, then install from there."
        )


def awake_warning(hour: int, minute: int) -> str:
    return (
        f"⚠ The Mac must be awake at {hour:02d}:{minute:02d}, ideally plugged in: launchd "
        "does not wake it. A run missed while it sleeps happens at the next wake; one "
        "missed while it is shut down is skipped. Stay logged in (a locked screen is "
        "fine): staging opens a browser window."
    )


@dataclass(frozen=True)
class Plan:
    """What install writes: the plist, and the warnings to print."""

    root: Path
    uv: str
    claude: str | None
    hour: int
    minute: int
    plist: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

    def xml(self) -> bytes:
        return plistlib.dumps(self.plist)

    def describe(self) -> list[str]:
        env = self.plist["EnvironmentVariables"]
        return [
            f"  agent: {LABEL} -> {plist_path()}",
            f"  runs: daily at {self.hour:02d}:{self.minute:02d} local time "
            "(schedule.nightly_at in config/filters.yaml)",
            f"  command: {' '.join(self.plist['ProgramArguments'])}",
            f"  working directory: {self.plist['WorkingDirectory']}",
            f"  PATH: {env['PATH']}",
            f"  output: {self.plist['StandardOutPath']}",
            f"  errors: {self.plist['StandardErrorPath']}",
        ]


def _unique(items: list[str]) -> list[str]:
    out: list[str] = []
    for i in items:
        if i and i not in out:
            out.append(i)
    return out


def plan(root: Path | None = None) -> Plan:
    """Build the agent for this machine. Raises ScheduleError when it cannot
    be installed (not macOS, a protected root, no uv, a bad nightly_at)."""
    require_macos()
    root = (root or paths.resolve_home()).resolve()
    check_root(root)
    uv = shutil.which("uv") or os.environ.get("UV", "")
    if not uv or not Path(uv).is_file():
        raise ScheduleError(
            "not installed: `uv` is not on PATH. Install uv (https://docs.astral.sh/uv/) "
            "or put it on PATH, then run `jobkit schedule install` again."
        )
    uv = str(Path(uv).absolute())  # the PATH entry, not its target: survives upgrades
    found = shutil.which("claude")
    claude = str(Path(found).absolute()) if found else None
    try:
        hour, minute = load_filters().nightly_at
    except ValueError as e:
        raise ScheduleError(f"not installed: {e}") from e
    path_dirs = _unique(
        [str(Path(uv).parent), str(Path(claude).parent) if claude else "", *SYSTEM_PATH]
    )
    plist: dict[str, Any] = {
        "Label": LABEL,
        "ProgramArguments": [uv, "run", "--directory", str(root), "jobkit", "nightly"],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {
            "PATH": ":".join(path_dirs),
            "JOBKIT_HOME": str(root),
            "PYTHONPATH": str(root / "src"),
        },
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(root / OUT_LOG),
        "StandardErrorPath": str(root / ERR_LOG),
        "RunAtLoad": False,
    }
    warnings = [awake_warning(hour, minute)]
    if claude is None and not os.environ.get("ANTHROPIC_API_KEY"):
        warnings.append(
            "⚠ `claude` is not on PATH and ANTHROPIC_API_KEY is not set (shell or .env): "
            "every LLM stage of the nightly run will fail. Put one of them in place, then "
            "install again."
        )
    return Plan(root, uv, claude, hour, minute, plist, warnings)


def _bootout(label: str) -> bool:
    """Unload a loaded agent. True when it was loaded."""
    return _launchctl("bootout", f"{_domain()}/{label}").returncode == 0


def _remove_legacy() -> list[str]:
    removed: list[str] = []
    for label in LEGACY_LABELS:
        p = plist_path(label)
        if not p.exists():
            continue
        _bootout(label)
        p.unlink(missing_ok=True)
        removed.append(f"removed the old {label} agent (scripts/install_schedule.sh)")
        log("schedule.legacy_removed", label=label, plist=str(p))
    return removed


def install(root: Path | None = None, dry_run: bool = False) -> str:
    """Write the plist and load it with launchctl (replacing an installed
    one). dry_run only describes it. Raises ScheduleError when it cannot."""
    p = plan(root)
    if dry_run:
        return "\n".join(
            [f"dry run, nothing installed. `jobkit schedule install` would write {LABEL}:"]
            + p.describe()
            + p.warnings
            + ["", p.xml().decode()]
        )
    (p.root / OUT_LOG).parent.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    notes = _remove_legacy()
    _bootout(LABEL)  # an installed one is replaced; not loaded is fine
    target = plist_path()
    target.write_bytes(p.xml())
    res = _launchctl("bootstrap", _domain(), str(target))
    if res.returncode != 0:
        time.sleep(1)  # bootout can take a moment to let go of the label
        res = _launchctl("bootstrap", _domain(), str(target))
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip()[:300]
        log("schedule.install_failed", code=res.returncode, error=err, plist=str(target))
        raise ScheduleError(
            f"wrote {target}, but `launchctl bootstrap {_domain()} {target}` failed "
            f"(exit {res.returncode}): {err}. Nothing is scheduled; fix that and run "
            "`jobkit schedule install` again, or `jobkit schedule uninstall` to remove it."
        )
    log(
        "schedule.installed",
        label=LABEL,
        plist=str(target),
        argv=p.plist["ProgramArguments"],
        hour=p.hour,
        minute=p.minute,
        path=p.plist["EnvironmentVariables"]["PATH"],
    )
    return "\n".join(
        [f"installed {LABEL}: `jobkit nightly` runs daily at {p.hour:02d}:{p.minute:02d}"]
        + p.describe()
        + [f"  {n}" for n in notes]
        + p.warnings
        + [
            "To check it under launchd now (a full nightly run: polls, LLM calls, staging): "
            f"launchctl kickstart {_domain()}/{LABEL}"
        ]
    )


def uninstall(dry_run: bool = False) -> str:
    """Unload and delete the nightly agent (and the old script's agents).
    dry_run only lists what would be removed."""
    require_macos()
    targets = [plist_path(label) for label in (LABEL, *LEGACY_LABELS)]
    present = [t for t in targets if t.exists()]
    if dry_run:
        if not present:
            return f"nothing to remove: {LABEL} is not installed ({plist_path()} does not exist)"
        return "dry run, nothing removed. would unload and delete:\n" + "\n".join(
            f"  {t}" for t in present
        )
    lines: list[str] = []
    for t in targets:
        label = t.stem
        was_loaded = _bootout(label)
        if t.exists():
            t.unlink()
            lines.append(f"removed {label}: unloaded and deleted {t}")
        elif was_loaded:
            lines.append(f"removed {label}: unloaded (it had no plist at {t})")
    log("schedule.uninstalled", removed=lines)
    if not lines:
        return f"nothing to remove: {LABEL} is not installed"
    return "\n".join(lines)


_LAUNCHD_FIELDS = {
    "state": re.compile(r"^\s*state = (.+)$", re.M),
    "runs": re.compile(r"^\s*runs = (\d+)\s*$", re.M),
    "last exit code": re.compile(r"^\s*last exit code = (.+)$", re.M),
}


def launchd_state(label: str = LABEL) -> dict[str, str] | None:
    """What launchd reports for a loaded agent (state, runs, last exit code),
    or None when it is not loaded. Read-only (`launchctl print`)."""
    res = _launchctl("print", f"{_domain()}/{label}")
    if res.returncode != 0:
        return None
    out: dict[str, str] = {}
    for key, rx in _LAUNCHD_FIELDS.items():
        m = rx.search(res.stdout or "")
        if m:
            out[key] = m.group(1).strip()
    return out


def _installed_lines() -> list[str]:
    target = plist_path()
    if not target.exists():
        return [f"installed: no ({target} does not exist; `jobkit schedule install` sets it up)"]
    try:
        data = plistlib.loads(target.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError) as e:
        return [f"installed: ⚠ {target} exists but cannot be read ({e}); install again"]
    when = data.get("StartCalendarInterval") or {}
    hour, minute = int(when.get("Hour", -1)), int(when.get("Minute", 0))
    argv = [str(a) for a in data.get("ProgramArguments") or []]
    lines = [
        f"installed: yes ({target})",
        f"  runs: daily at {hour:02d}:{minute:02d} local time",
        f"  command: {' '.join(argv)}",
        f"  output: {data.get('StandardOutPath')}  errors: {data.get('StandardErrorPath')}",
    ]
    try:
        want = load_filters().nightly_at
    except (OSError, ValueError) as e:
        lines.append(f"  ⚠ cannot read schedule.nightly_at: {e}")
    else:
        if want != (hour, minute):
            lines.append(
                f"  ⚠ config/filters.yaml schedule.nightly_at is {want[0]:02d}:{want[1]:02d} "
                f"but the agent runs at {hour:02d}:{minute:02d}: run `jobkit schedule install` "
                "again"
            )
    root = str(paths.resolve_home())
    if data.get("WorkingDirectory") != root:
        lines.append(
            f"  ⚠ the agent runs in {data.get('WorkingDirectory')}, not this checkout "
            f"({root}): run `jobkit schedule install` from the checkout it should run"
        )
    if argv and not Path(argv[0]).is_file():
        lines.append(f"  ⚠ {argv[0]} no longer exists: run `jobkit schedule install` again")
    return lines


def status(conn: sqlite3.Connection | None) -> tuple[str, bool]:
    """(report, supported). Read-only: the plist, `launchctl print`, and the
    nightly run record in the DB (when there is one), with the jobs a staging
    run stopped on that wait on the human (pipeline/halts.py), and last the
    open_forms_line the scheduled run reads to decide whether to call
    open_queue."""
    lines: list[str] = []
    supported = _platform() == "darwin"
    if not supported:
        try:
            require_macos()
        except ScheduleUnsupported as e:
            lines.append(str(e))
    else:
        lines.append(f"schedule: launchd agent {LABEL}")
        lines += _installed_lines()
        state = launchd_state()
        if state is None:
            lines.append("loaded in launchd: no")
        else:
            parts = ", ".join(f"{k} = {v}" for k, v in state.items())
            lines.append(f"loaded in launchd: yes ({parts or 'no details'})")
        legacy = [label for label in LEGACY_LABELS if plist_path(label).exists()]
        if legacy:
            lines.append(
                f"⚠ old agents from scripts/install_schedule.sh are still installed: "
                f"{', '.join(legacy)}. They run the pipeline again at 07:00/09:00; "
                "`jobkit schedule install` (or uninstall) removes them."
            )
    if conn is None:
        lines.append("last nightly run: no database (run `jobkit init`)")
    else:
        lines.append(nightly.report(conn))
    lines.append(open_forms_line(conn))
    return "\n".join(lines), supported


OPEN_FORMS_PREFIX = "open forms after the daily run:"


def open_forms_line(conn: sqlite3.Connection | None) -> str:
    """One line for the scheduled run: whether it ends by opening the filled
    forms (staging.open_after_daily_run in config/filters.yaml) and, when it
    does, how many applications open_queue would open now. Read-only."""
    try:
        on = load_filters().open_after_daily_run
    except (ValueError, FileNotFoundError, yaml.YAMLError) as e:
        # FileNotFoundError covers ConfigMissing: no config/filters.yaml yet;
        # YAMLError a filters.yaml that does not parse
        return f"{OPEN_FORMS_PREFIX} off ({e})"
    if not on:
        return f"{OPEN_FORMS_PREFIX} off (staging.open_after_daily_run)"
    if conn is None:
        return f"{OPEN_FORMS_PREFIX} on; no database, nothing to open"
    n = len(conn.execute(OPEN_QUEUE_JOBS).fetchall())
    return (
        f"{OPEN_FORMS_PREFIX} on; {n} application{'s' if n != 1 else ''} ready to open (open_queue)"
    )
