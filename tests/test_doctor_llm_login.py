"""D7: `jobkit doctor` checks the claude CLI with one tiny live call: an
expired login fails with "run claude, then /login", a call that hangs fails
with the timeout, and an old CLI (an unknown-model warning, or an answer from
another model than jobkit's haiku) is a warning with "claude update".

Tests never make the call: each passes a fake probe in Env.llm_probe, and an
Env without one reports "login not checked". The probe itself (llm.probe_cli)
is exercised against a faked subprocess.run (hard rule 12)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from jobkit import doctor, llm

CLAUDE_AT = "/usr/local/bin/claude"


def _env(
    root: Path,
    probe: Callable[[], llm.ProbeResult] | None,
    environ: dict[str, str] | None = None,
    claude: str | None = CLAUDE_AT,
) -> doctor.Env:
    return doctor.Env(
        root=root.resolve(),
        home=root / "home",
        platform="darwin",
        environ=environ or {},
        which=lambda name: claude if name == "claude" else None,
        python=(3, 12),
        site_packages=None,
        llm_probe=probe,
    )


def _checks(env: doctor.Env) -> list[doctor.Check]:
    out = doctor.check_llm(env)
    return out if isinstance(out, list) else [out]


def _probe(result: llm.ProbeResult) -> Callable[[], llm.ProbeResult]:
    calls: list[int] = []

    def run() -> llm.ProbeResult:
        calls.append(1)
        assert len(calls) == 1, "doctor makes one call, not more"
        return result

    return run


def test_logged_in_and_the_right_model(jobkit_home: Path) -> None:
    ok = llm.ProbeResult("ok", "answered", ("claude-haiku-5-5",), None, "2.1.400 (Claude Code)")
    (check,) = _checks(_env(jobkit_home, _probe(ok)))
    assert check.ok and check.name == "llm backend"
    assert "logged in, answered with claude-haiku-5-5" in check.detail
    assert "2.1.400" in check.detail


def test_expired_login_fails_with_the_fix(jobkit_home: Path) -> None:
    expired = llm.ProbeResult("auth", llm.LOGIN_EXPIRED)
    (check,) = _checks(_env(jobkit_home, _probe(expired)))
    assert not check.ok and check.required
    assert "Claude login expired" in check.detail
    assert "/login" in check.fix and "claude" in check.fix
    report = doctor.Report(root=jobkit_home, checks=[check])
    assert not report.ok and "fix: run `claude` in a terminal, then type /login" in report.render()


def test_a_call_that_hangs_fails_with_the_timeout(jobkit_home: Path) -> None:
    hung = llm.ProbeResult("timeout", "claude did not answer in 60s")
    (check,) = _checks(_env(jobkit_home, _probe(hung)))
    assert not check.ok and check.required and "did not answer in 60s" in check.detail


def test_an_old_cli_serving_an_old_model_is_a_warning(jobkit_home: Path) -> None:
    old = llm.ProbeResult("ok", "answered", ("claude-haiku-4-5-20251001",))
    login, model = _checks(_env(jobkit_home, _probe(old)))
    assert login.ok
    assert not model.ok and not model.required, "a warning, never a failure"
    assert "served claude-haiku-4-5-20251001 for 'haiku', expected claude-haiku-5-5" in (
        model.detail
    )
    assert model.fix == "claude update"
    assert doctor.Report(root=jobkit_home, checks=[login, model]).ok


def test_the_cli_unrecognized_model_warning_is_a_warning(jobkit_home: Path) -> None:
    warned = llm.ProbeResult(
        "ok", "answered", ("claude-haiku-5-5",), "Warning: unrecognized model 'haiku'"
    )
    _login, model = _checks(_env(jobkit_home, _probe(warned)))
    assert not model.ok and not model.required
    assert "unrecognized model" in model.detail and model.fix == "claude update"


def test_an_unknown_model_error_fails_with_claude_update(jobkit_home: Path) -> None:
    unknown = llm.ProbeResult("model", llm.model_unknown_message("haiku"))
    (check,) = _checks(_env(jobkit_home, _probe(unknown)))
    assert not check.ok and check.fix == "claude update"


def test_any_other_failure_says_what_the_cli_said(jobkit_home: Path) -> None:
    other = llm.ProbeResult("other", "claude -p failed (exit 1): API Error: 500 Internal")
    (check,) = _checks(_env(jobkit_home, _probe(other)))
    assert not check.ok and "500 Internal" in check.detail


def test_no_probe_reports_not_checked(jobkit_home: Path) -> None:
    (check,) = _checks(_env(jobkit_home, None))
    assert check.ok and "login not checked" in check.detail


def test_api_key_and_missing_cli_make_no_call(jobkit_home: Path) -> None:
    def never() -> llm.ProbeResult:
        raise AssertionError("no probe with an API key, or without the CLI")

    (keyed,) = _checks(_env(jobkit_home, never, {"ANTHROPIC_API_KEY": "dummy-test-value"}))
    assert keyed.ok and "ANTHROPIC_API_KEY" in keyed.detail
    (missing,) = _checks(_env(jobkit_home, never, claude=None))
    assert not missing.ok and "no `claude` CLI" in missing.detail


def test_run_checks_includes_the_model_warning(jobkit_home: Path) -> None:
    old = llm.ProbeResult("ok", "answered", ("claude-haiku-4-5-20251001",))
    report = doctor.run_checks(_env(jobkit_home, _probe(old)))
    names = [c.name for c in report.checks]
    assert names.count("llm backend") == 1 and "claude model" in names
    assert "claude update" in report.render()


# ---- the probe: one isolated haiku call, never cached, never raising ----


def _envelope(**extra: Any) -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "OK",
            "modelUsage": {"claude-haiku-5-5": {"inputTokens": 9}},
            **extra,
        }
    )


class FakeRun:
    def __init__(self, code: int, stdout: str, stderr: str = "") -> None:
        self.reply = (code, stdout, stderr)
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(args), kwargs))
        if args[1:] == ["--version"]:
            return subprocess.CompletedProcess(args, 0, stdout="2.1.400 (Claude Code)\n", stderr="")
        code, out, err = self.reply
        return subprocess.CompletedProcess(args, code, stdout=out, stderr=err)


def test_probe_makes_one_isolated_haiku_call(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRun(0, _envelope())
    monkeypatch.setattr(llm.subprocess, "run", fake)
    before = llm.call_counts()
    result = llm.probe_cli()
    assert result.kind == "ok" and result.served_by == ("claude-haiku-5-5",)
    assert result.version == "2.1.400 (Claude Code)"
    calls = [c for c in fake.calls if c[0][1:] != ["--version"]]
    ((argv, kwargs),) = calls
    assert argv[:2] == ["claude", "-p"] and argv[argv.index("--model") + 1] == "haiku"
    assert "--safe-mode" in argv and argv[argv.index("--tools") + 1] == ""
    assert kwargs["timeout"] == llm.PROBE_TIMEOUT_SECONDS == 60
    assert kwargs["input"] == llm.PromptFile(llm.PROBE_PROMPT_NAME).render()
    assert llm.call_counts() == before, "the probe is not a pipeline call"


def test_probe_reads_an_expired_login_from_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    expired = _envelope(
        result="Failed to authenticate: OAuth session expired and could not be refreshed",
        is_error=True,
        api_error_status=401,
        modelUsage={},
    )
    monkeypatch.setattr(llm.subprocess, "run", FakeRun(1, expired, ""))
    result = llm.probe_cli()
    assert result.kind == "auth" and result.message == llm.LOGIN_EXPIRED


def test_probe_catches_the_model_warning_in_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    warn = "Warning: unrecognized model 'haiku'. Run claude update.\n"
    monkeypatch.setattr(llm.subprocess, "run", FakeRun(0, _envelope(), warn))
    result = llm.probe_cli()
    assert result.kind == "ok" and result.warning and "unrecognized model" in result.warning


def test_probe_times_out_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    def hang(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout", 0))

    monkeypatch.setattr(llm.subprocess, "run", hang)
    result = llm.probe_cli(timeout=5)
    assert result.kind == "timeout" and result.message == "claude did not answer in 5s"


def test_probe_never_raises_when_the_spawn_is_refused() -> None:
    # the test sandbox refuses every spawn: the probe reports it, it does not raise
    result = llm.probe_cli()
    assert result.kind == "other" and "could not be started" in result.message
