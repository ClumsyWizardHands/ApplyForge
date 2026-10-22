"""`jobkit setup-status` (CLI) and MCP `setup_status`: which onboarding steps
are done, in order, and the next action. Read-only.

The order is the interview's: resume → corpus → preferences (filters and the
config written with them) → target companies → answer bank → database → MCP
registration → daily runs. A step is `done`, `partial` (started, with what is
left), or `todo`; the daily-runs step is `check` when jobkit cannot see it
(scheduled tasks live in the Claude app, not in this checkout).

For the full machine check (Python, uv, Chromium, LLM backend, checkout
location) see `jobkit doctor`."""

from __future__ import annotations

import plistlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobkit import config, doctor
from jobkit import schedule as launchd
from jobkit.onboarding.answer_bank_builder import answer_status, load_record
from jobkit.onboarding.corpus_confirm import (
    CONFIDENCES,
    THIN_BULLETS,
    THIN_VERIFIED,
    figure_counts,
    is_thin,
    promotion_blockers,
)
from jobkit.onboarding.files import SetupFiles, load_yaml, setup_files
from jobkit.onboarding.targets import feed_only_said
from jobkit.rules import max_auto_approve_per_day


@dataclass(frozen=True)
class Step:
    name: str
    state: str  # done | partial | todo | check
    detail: str
    next_action: str = ""
    warning: str = ""  # shown under a step that is done but worth knowing about


@dataclass
class SetupStatus:
    root: Path
    steps: list[Step] = field(default_factory=list)

    @property
    def next_step(self) -> Step | None:
        return next((s for s in self.steps if s.state in ("todo", "partial")), None)

    @property
    def complete(self) -> bool:
        return self.next_step is None

    def render(self) -> str:
        lines = [f"jobkit setup — {self.root}"]
        for i, s in enumerate(self.steps, 1):
            lines.append(f"{i}. [{s.state:^7}] {s.name}: {s.detail}")
            if s.warning:
                lines.append(f"             ! {s.warning}")
            if s.state != "done" and s.next_action:
                lines.append(f"             → {s.next_action}")
        nxt = self.next_step
        if nxt:
            lines.append(f"\nNext: {nxt.name} — {nxt.next_action}")
        else:
            lines.append("\nSetup complete. `jobkit doctor` checks the machine side.")
        return "\n".join(lines)


def _safe_yaml(path: Path) -> tuple[Any, str]:
    try:
        return load_yaml(path), ""
    except Exception as e:  # noqa: BLE001 — a status report never raises
        return None, f"does not parse: {' '.join(str(e).split())[:120]}"


def _resume_step(files: SetupFiles) -> Step:
    if files.corpus_draft.is_file() or files.corpus.is_file():
        what = "draft" if files.corpus_draft.is_file() else "corpus"
        return Step("resume", "done", f"read ({what} present)")
    return Step(
        "resume",
        "todo",
        "not read yet",
        "ingest_resume(path) — CLI: jobkit setup ingest <resume.pdf|docx|html|txt>",
    )


def _corpus_step(files: SetupFiles) -> Step:
    action = "corpus_draft_status, then confirm_bullets / update_corpus_draft, then promote_corpus"
    if files.corpus.is_file():
        data, err = _safe_yaml(files.corpus)
        if err or not isinstance(data, dict):
            return Step(
                "career corpus", "partial", f"data/career_corpus.yaml {err or 'is empty'}", action
            )
        bullets = [b for b in data.get("bullets") or [] if isinstance(b, dict)]
        unset = [b for b in bullets if b.get("metric_confidence") not in CONFIDENCES]
        if not bullets:
            return Step("career corpus", "partial", "the corpus has no bullets", action)
        if unset:
            return Step(
                "career corpus",
                "partial",
                f"{len(unset)} of {len(bullets)} bullets have no metric_confidence",
                "set metric_confidence and evidence for each (yours to set, never defaulted)",
            )
        figures, verified = figure_counts(bullets)
        thin = (
            f" — thin (under {THIN_BULLETS} bullets or {THIN_VERIFIED:.0%} of figures verified)"
            if is_thin(bullets)
            else ""
        )
        return Step(
            "career corpus",
            "done",
            f"{len(bullets)} bullets, {verified} of {figures} figures verified{thin}",
        )
    if files.corpus_draft.is_file():
        data, err = _safe_yaml(files.corpus_draft)
        if err or not isinstance(data, dict):
            return Step("career corpus", "partial", f"draft {err or 'is empty'}", action)
        for key in ("roles", "bullets", "education", "positioning"):
            data.setdefault(key, [])
        data.setdefault("identity", {})
        blockers = promotion_blockers(data)
        pending = sum(1 for b in data["bullets"] if b.get("status") in (None, "pending"))
        return Step(
            "career corpus",
            "partial",
            f"draft: {pending} of {len(data['bullets'])} bullets still to confirm, "
            f"{len(blockers)} item(s) before promotion",
            action,
        )
    return Step("career corpus", "todo", "no draft yet", "read the resume first")


def _config_ok(files: SetupFiles, name: str) -> tuple[bool, str]:
    path = files.config_file(name)
    if not path.is_file():
        return False, "missing"
    try:
        if name == "filters.yaml":
            config.load_filters(path)
        elif name == "targets.yaml":
            config.load_targets(path)
        else:
            data = config.read_config_yaml(path)
            if not isinstance(data, dict):
                return False, "empty"
            if name == "autoapprove.yaml":
                max_auto_approve_per_day(data)
    except Exception as e:  # noqa: BLE001
        return False, f"does not load ({' '.join(str(e).split())[:100]})"
    return True, "ok"


def _preferences_step(files: SetupFiles) -> Step:
    ok, why = _config_ok(files, "filters.yaml")
    action = "run_intake(answers) — CLI: jobkit setup intake <answers.yaml>"
    if not ok:
        return Step("preferences (config/filters.yaml)", "todo", f"filters.yaml {why}", action)
    others = [
        f"{n} {w}"
        for n in ("commute_table.yaml", "autoapprove.yaml", "banned_phrases.yaml")
        for good, w in [_config_ok(files, n)]
        if not good
    ]
    if others:
        return Step(
            "preferences (config/filters.yaml)",
            "partial",
            "filters.yaml ok; " + ", ".join(others),
            action + " (writes the missing files), or copy config/<name>.example.yaml",
        )
    return Step(
        "preferences (config/filters.yaml)",
        "done",
        "filters and companion config load; " + automation_summary(files),
    )


def automation_summary(files: SetupFiles) -> str:
    """How much of the daily run is automatic: the auto_approve rules (and a
    daily limit, only when one is set) and whether the run opens the filled
    forms. Read-only; assumes both files load."""
    rules = config.read_config_yaml(files.config_file("autoapprove.yaml")) or {}
    approve = [r for r in rules.get("auto_approve") or [] if isinstance(r, dict)]
    if approve:
        auto = f"automatic approval: {len(approve)} rule{'s' if len(approve) != 1 else ''}"
        cap = max_auto_approve_per_day(rules)
        if cap is not None:
            auto += f", at most {cap} a day"
    else:
        auto = "automatic approval: off"
    try:
        on = config.load_filters(files.config_file("filters.yaml")).open_after_daily_run
        forms = "on" if on else "off"
    except ValueError as e:
        forms = f"not read ({e})"
    return f"{auto}; forms open after the daily run: {forms}"


def _targets_step(files: SetupFiles) -> Step:
    action = (
        "seed_targets(companies) — CLI: jobkit setup targets <name> [<name> ...]; when the "
        "person's employers have no board jobkit can poll: seed_targets(feed_only_said=<their "
        'words>) — CLI: jobkit setup targets --feed-only "<their words>"'
    )
    ok, why = _config_ok(files, "targets.yaml")
    if not ok:
        return Step("target companies (config/targets.yaml)", "todo", f"targets.yaml {why}", action)
    path = files.config_file("targets.yaml")
    t = config.load_targets(path)
    boards = sum(1 for c in t.companies if c.board)
    if not boards:
        said = feed_only_said(path)
        if said:
            return Step(
                "target companies (config/targets.yaml)",
                "done",
                f'{len(t.companies)} companies; feeds only (you said: "{said[:120]}")',
                warning="0 polled boards; postings come from the feeds",
            )
        return Step(
            "target companies (config/targets.yaml)",
            "partial",
            f"{len(t.companies)} companies, none with a board to poll",
            action,
        )
    return Step(
        "target companies (config/targets.yaml)",
        "done",
        f"{len(t.companies)} companies, {boards} with a public board",
    )


def _bank_step(files: SetupFiles) -> Step:
    action = "answer_bank_questions, then build_answer_bank(answers)"
    record = load_record(files)
    if not files.bank.is_file():
        state = "partial" if record else "todo"
        return Step("answer bank (data/answer_bank.yaml)", state, "not written yet", action)
    if record:
        _, missing = answer_status(record)
        if missing:
            return Step(
                "answer bank (data/answer_bank.yaml)",
                "partial",
                f"{len(missing)} required answer(s) unset: {', '.join(missing)}",
                action,
            )
        return Step("answer bank (data/answer_bank.yaml)", "done", "every required answer set")
    data, err = _safe_yaml(files.bank)
    if err or not isinstance(data, dict):
        return Step("answer bank (data/answer_bank.yaml)", "partial", err or "empty", action)
    screening = {str(s.get("id")): s for s in data.get("screening") or [] if isinstance(s, dict)}
    unset = [
        k for k in ("work_auth", "sponsorship") if (screening.get(k) or {}).get("answer") is None
    ]
    if unset:
        return Step(
            "answer bank (data/answer_bank.yaml)",
            "partial",
            f"written by hand; unset: {', '.join(unset)}",
            action,
        )
    return Step("answer bank (data/answer_bank.yaml)", "done", "present (written by hand)")


def _db_step(files: SetupFiles) -> Step:
    if files.db.is_file():
        return Step("database", "done", "data/jobkit.db present")
    return Step("database", "todo", "not created", "uv run jobkit init")


def _mcp_step(files: SetupFiles) -> Step:
    check = doctor.check_mcp(doctor.current_env(files.root))
    if check.ok:
        return Step("MCP registration (.mcp.json)", "done", check.detail)
    return Step(
        "MCP registration (.mcp.json)",
        "todo",
        check.detail,
        "cp .mcp.example.json .mcp.json and set --directory to this checkout, or: "
        f"claude mcp add --scope project jobkit -- uv run --directory {files.root} jobkit-mcp",
    )


def _runs_step(files: SetupFiles) -> Step:
    """The macOS launchd agent when it runs THIS checkout; otherwise the
    daily runs are scheduled tasks in the Claude app, which jobkit cannot see."""
    action = (
        "create the daily scheduled tasks in the Claude app (the setup skill does it), or "
        "`uv run jobkit schedule install` for the macOS launchd agent"
    )
    plist = launchd.plist_path()
    if plist.is_file():
        try:
            data = plistlib.loads(plist.read_bytes())
            where = str(data.get("WorkingDirectory") or "")
        except Exception:  # noqa: BLE001 — unreadable: report it, never raise
            where = ""
        if where and Path(where).resolve() == files.root:
            return Step("daily runs", "done", "launchd agent installed (`jobkit schedule status`)")
        return Step(
            "daily runs",
            "check",
            f"the launchd agent runs another checkout ({where or 'unreadable plist'})",
            action,
        )
    return Step("daily runs", "check", "jobkit cannot see the Claude app's scheduled tasks", action)


def setup_status(*, root: Path | None = None) -> SetupStatus:
    files = setup_files(root)
    status = SetupStatus(root=files.root)
    steps: list[Callable[[], Step]] = [
        lambda: _resume_step(files),
        lambda: _corpus_step(files),
        lambda: _preferences_step(files),
        lambda: _targets_step(files),
        lambda: _bank_step(files),
        lambda: _db_step(files),
        lambda: _mcp_step(files),
        lambda: _runs_step(files),
    ]
    for step in steps:
        try:
            status.steps.append(step())
        except Exception as e:  # noqa: BLE001 — a status report never raises
            status.steps.append(Step("(check failed)", "partial", f"{type(e).__name__}: {e}"))
    return status
