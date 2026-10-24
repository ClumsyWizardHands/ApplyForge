"""D7: a failed `claude -p` call always says why, an expired login stops the
run at once with the fix, and the run, the digest and `jobkit schedule
status` say "Claude login expired: run claude, then /login".

The real CLI reports an auth failure as an error envelope on STDOUT with exit
code 1 and an empty stderr (claude-code 2.1.x, print mode). The old code read
only stderr, so every failure was logged as "claude -p failed: " with nothing
after the colon. subprocess.run is faked at llm's boundary: no test spawns
`claude` (hard rule 12)."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import types
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from jobkit import llm
from jobkit import log as logmod
from jobkit.pipeline import nightly
from jobkit.pipeline.digest import freshness_banner
from jobkit.pipeline.runner import NIGHTLY, run_stages

AUTH_TEXT = "Failed to authenticate: OAuth session expired and could not be refreshed"


def _envelope(result: str = '{"ok": 1}', **extra: Any) -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": result,
            "usage": {"input_tokens": 12, "output_tokens": 2},
            "modelUsage": {"claude-haiku-5-5": {"inputTokens": 12}},
            **extra,
        }
    )


def _auth_envelope() -> str:
    """What the CLI prints when its login expired (exit 1, stderr empty)."""
    return _envelope(
        AUTH_TEXT,
        is_error=True,
        api_error_status=401,
        api_error_code="authentication_failed",
        modelUsage={},
    )


class FakeCli:
    """Stands in for subprocess.run: answers each `claude -p` call with the
    next (returncode, stdout, stderr), repeating the last one."""

    def __init__(self, *replies: tuple[int, str, str]) -> None:
        self.replies = list(replies) or [(0, _envelope(), "")]
        self.calls: list[list[str]] = []

    def run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        i = min(len(self.calls) - 1, len(self.replies) - 1)
        code, out, err = self.replies[i]
        return subprocess.CompletedProcess(args, code, stdout=out, stderr=err)


@pytest.fixture(autouse=True)
def fresh_llm_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """No latch or model warning carried in from another test, none carried
    out; sleeps are recorded, never slept."""
    llm.reset_backend_problem()
    llm._MODEL_WARNED.clear()
    slept: list[float] = []
    monkeypatch.setattr(llm.time, "sleep", slept.append)
    yield slept
    llm.reset_backend_problem()
    llm._MODEL_WARNED.clear()


def _use(monkeypatch: pytest.MonkeyPatch, fake: FakeCli) -> FakeCli:
    monkeypatch.setattr(llm.subprocess, "run", fake.run)
    return fake


def _log_events(name: str) -> list[dict[str, Any]]:
    path = logmod.LOG_PATH
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("event") == name]


# ---- the expired login: never retried, never cached, latched ----


def test_expired_login_fails_once_with_the_fix_and_is_never_retried(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, fresh_llm_state: list[float]
) -> None:
    fake = _use(monkeypatch, FakeCli((1, _auth_envelope(), "")))
    before = llm.call_counts()
    with pytest.raises(llm.LLMBackendDown) as caught:
        llm.call(conn, "extract", "a job", cache_key="k")
    assert str(caught.value) == llm.LOGIN_EXPIRED == "Claude login expired: run claude, then /login"
    assert len(fake.calls) == 1, "an expired login is not retried"
    assert fresh_llm_state == [], "and nothing waits on it"
    assert conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] == 0
    assert llm.backend_problem() == llm.LOGIN_EXPIRED
    # every later call in this process fails at once, without spawning claude
    for stage in ("remote", "score", "cover"):
        with pytest.raises(llm.LLMBackendDown, match="run claude, then /login"):
            llm.call(conn, stage, "another job")
    assert len(fake.calls) == 1
    after = llm.call_counts()
    assert (after.failed - before.failed, after.live - before.live) == (4, 0)
    assert llm.last_failure() == llm.LOGIN_EXPIRED
    # the CLI's own words are kept in the log (they were lost before)
    (err,) = _log_events("llm.cli_error")
    assert err["kind"] == "auth" and err["exit"] == 1
    assert "OAuth session expired" in err["detail"]


def test_a_cached_answer_is_still_served_while_the_login_is_expired(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _use(
        monkeypatch, FakeCli((0, _envelope("cached answer"), ""), (1, _auth_envelope(), ""))
    )
    assert llm.call(conn, "extract", "job A", cache_key="a") == "cached answer"
    with pytest.raises(llm.LLMBackendDown):
        llm.call(conn, "extract", "job B", cache_key="b")
    before = llm.call_counts()
    assert llm.call(conn, "extract", "job A", cache_key="a") == "cached answer"
    after = llm.call_counts()
    assert (after.cached - before.cached, after.live - before.live) == (1, 0)
    assert len(fake.calls) == 2


def test_the_latch_expires_so_a_long_lived_process_recovers(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _use(monkeypatch, FakeCli((1, _auth_envelope(), ""), (0, _envelope("back"), "")))
    with pytest.raises(llm.LLMBackendDown):
        llm.call(conn, "extract", "p")
    monkeypatch.setattr(llm, "LATCH_SECONDS", -1)  # the person logged in again since
    assert llm.call(conn, "extract", "p") == "back"
    assert llm.backend_problem() is None and len(fake.calls) == 2


def test_a_passing_doctor_check_clears_the_latch_at_once(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # review 2026-10-23: after /login, the MCP server kept refusing tailor
    # calls for 15 minutes while doctor in the same process said logged in
    fake = _use(monkeypatch, FakeCli((1, _auth_envelope(), ""), (0, _envelope("back"), "")))
    with pytest.raises(llm.LLMBackendDown):
        llm.call(conn, "tailor", "p")
    assert llm.probe_cli().kind == "ok"
    assert llm.backend_problem() is None
    assert llm.call(conn, "tailor", "p") == "back"
    assert len([c for c in fake.calls if "-p" in c]) == 3


def test_a_latched_call_tries_live_again_after_the_back_off(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _use(monkeypatch, FakeCli((1, _auth_envelope(), ""), (0, _envelope("back"), "")))
    with pytest.raises(llm.LLMBackendDown):
        llm.call(conn, "tailor", "p")
    # within the back-off: refused without spawning claude
    with pytest.raises(llm.LLMBackendDown):
        llm.call(conn, "tailor", "p")
    assert len(fake.calls) == 1
    monkeypatch.setattr(llm, "LATCH_RETRY_SECONDS", -1)  # a minute has passed
    assert llm.call(conn, "tailor", "p") == "back"
    assert llm.backend_problem() is None and len(fake.calls) == 2


def test_a_failed_live_retry_latches_again(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _use(monkeypatch, FakeCli((1, _auth_envelope(), "")))
    with pytest.raises(llm.LLMBackendDown):
        llm.call(conn, "tailor", "p")
    monkeypatch.setattr(llm, "LATCH_RETRY_SECONDS", -1)
    with pytest.raises(llm.LLMBackendDown, match="run claude, then /login"):
        llm.call(conn, "tailor", "p")
    assert len(fake.calls) == 2, "one live try, not three"
    assert llm.backend_problem() == llm.LOGIN_EXPIRED


@pytest.mark.parametrize(
    ("code", "stdout", "stderr"),
    [
        (1, _auth_envelope(), ""),
        (1, "", "Not logged in · Please run /login"),
        (1, _envelope("OAuth token revoked", is_error=True), ""),
        (1, _envelope("API Error: 401 something", is_error=True, api_error_status=401), ""),
        (0, _envelope(AUTH_TEXT, is_error=True), ""),
        (1, "notice: safe mode\n" + _auth_envelope(), ""),
    ],
)
def test_every_spelling_of_a_lost_login_is_auth(code: int, stdout: str, stderr: str) -> None:
    failure = llm.classify_cli_result(code, stdout, stderr, "haiku")
    assert failure is not None and failure.kind == "auth"
    assert failure.message == llm.LOGIN_EXPIRED


def test_a_good_answer_is_not_a_failure() -> None:
    assert llm.classify_cli_result(0, _envelope(), "") is None
    assert llm.classify_cli_result(0, "notice line\n" + _envelope(), "a stderr notice") is None


# ---- every other failure says why ----


def test_exit_1_with_no_output_is_never_an_empty_message(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _use(monkeypatch, FakeCli((1, "", "")))
    with pytest.raises(RuntimeError) as caught:
        llm.call(conn, "extract", "p")
    msg = str(caught.value)
    assert "claude -p exited 1 with no output" in msg
    assert not msg.rstrip().endswith(":")
    assert len(fake.calls) == 3, "an unexplained failure is still retried"
    assert llm.backend_problem() is None
    assert "exited 1 with no output" in (llm.last_failure() or "")


def test_the_envelope_reason_is_kept_when_stderr_is_empty(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    overloaded = _envelope("API Error: 529 Overloaded", is_error=True, api_error_status=529)
    _use(monkeypatch, FakeCli((1, overloaded, "")))
    with pytest.raises(RuntimeError, match="529 Overloaded") as caught:
        llm.call(conn, "score", "p")
    assert not isinstance(caught.value, llm.LLMBackendDown)
    assert llm.backend_problem() is None, "an overload is transient: not latched"


def test_stderr_is_used_when_stdout_has_no_envelope(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, FakeCli((2, "", "error: unexpected argument '--frobnicate'")))
    with pytest.raises(RuntimeError, match="unexpected argument"):
        llm.call(conn, "extract", "p")


def test_a_timeout_names_the_limit_not_the_command_line(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def hang(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(llm.subprocess, "run", hang)
    with pytest.raises(RuntimeError) as caught:
        llm.call(conn, "extract", "p")
    msg = str(caught.value)
    assert f"timed out after {llm.CLI_TIMEOUT_SECONDS}s" in msg
    assert "--system-prompt" not in msg and llm.system_prompt()[:40] not in msg


def test_a_used_up_limit_stops_the_run_like_a_lost_login(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    limit = _envelope("Claude AI usage limit reached|1761000000", is_error=True)
    fake = _use(monkeypatch, FakeCli((1, limit, "")))
    with pytest.raises(llm.LLMBackendDown, match="usage limit"):
        llm.call(conn, "extract", "p")
    with pytest.raises(llm.LLMBackendDown):
        llm.call(conn, "score", "p")
    assert len(fake.calls) == 1


# ---- an old CLI: unknown model, or another model served ----


def test_an_unknown_model_is_not_retried_and_stops_only_that_tier(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    unknown = _envelope(
        'API Error: 404 {"type":"error","error":{"type":"not_found_error",'
        '"message":"model: claude-haiku-5-5"}}',
        is_error=True,
        api_error_status=404,
    )
    fake = _use(monkeypatch, FakeCli((1, unknown, ""), (0, _envelope("opus ok"), "")))
    with pytest.raises(llm.LLMModelUnknown, match="run claude update"):
        llm.call(conn, "extract", "p")
    assert len(fake.calls) == 1
    with pytest.raises(llm.LLMModelUnknown):
        llm.call(conn, "score", "p")  # the same tier (haiku): at once
    assert len(fake.calls) == 1
    assert llm.call(conn, "cover", "p") == "opus ok"  # another tier still runs
    assert llm.backend_problem() is None


def test_a_different_served_model_is_logged_once_per_stage(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _envelope(modelUsage={"claude-haiku-4-5-20251001": {"inputTokens": 12}})
    _use(monkeypatch, FakeCli((0, old, "")))
    for prompt in ("a", "b", "c"):
        assert llm.call(conn, "extract", prompt) == '{"ok": 1}'
    (warned,) = _log_events("llm.model_mismatch")
    assert warned["requested"] == "claude-haiku-5-5" and warned["alias"] == "haiku"
    assert warned["served_by"] == ["claude-haiku-4-5-20251001"]
    assert warned["fix"] == "run claude update"


def test_the_cli_unrecognized_model_warning_is_logged(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    warn = "Warning: unrecognized model 'haiku'; using the default model\n"
    _use(monkeypatch, FakeCli((0, _envelope(), warn)))
    llm.call(conn, "extract", "p")
    (warned,) = _log_events("llm.model_mismatch")
    assert "unrecognized model" in warned["warning"]


def test_the_right_model_logs_no_mismatch(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    dated = _envelope(modelUsage={"claude-haiku-5-5-20261001": {"inputTokens": 1}})
    _use(monkeypatch, FakeCli((0, dated, "")))
    llm.call(conn, "extract", "p")
    assert _log_events("llm.model_mismatch") == []


# ---- SDK backend: a rejected key ----


def test_a_rejected_api_key_is_backend_down(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    class AuthenticationError(Exception):
        pass

    tries: list[int] = []

    class _Messages:
        def create(self, **kw: Any) -> SimpleNamespace:
            tries.append(1)
            raise AuthenticationError("invalid x-api-key")

    mod = types.ModuleType("anthropic")
    mod.Anthropic = lambda: SimpleNamespace(messages=_Messages())  # type: ignore[attr-defined]
    mod.AuthenticationError = AuthenticationError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-test-value")
    with pytest.raises(llm.LLMBackendDown, match="ANTHROPIC_API_KEY was rejected"):
        llm.call(conn, "score", "p")
    assert tries == [1]
    assert llm.backend_problem() == llm.API_KEY_REJECTED


# ---- the run, the digest and `jobkit schedule status` ----


def _triage_like(stage: str, *keys: str) -> Any:
    """A stage like triage: each job's failure is skipped, the stage returns."""

    def run(c: sqlite3.Connection) -> object:
        done = 0
        for key in keys:
            try:
                llm.call(c, stage, f"job {key}", cache_key=key)
                done += 1
            except RuntimeError:
                continue
        return f"{done} of {len(keys)} jobs"

    return run


def test_a_run_with_an_expired_login_says_so_everywhere(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # one job answered earlier (cached), then the login expired
    fake = _use(monkeypatch, FakeCli((0, _envelope("cached"), "")))
    llm.call(conn, "extract", "job a", cache_key="a")
    fake.replies = [(1, _auth_envelope(), "")]
    spawned_before = len(fake.calls)
    echoed: list[str] = []
    out = run_stages(
        conn,
        [
            ("poll", lambda c: "3 boards"),
            ("extract", _triage_like("extract", "a", "b", "c")),
            ("remote", _triage_like("remote", "d", "e")),
            ("digest", lambda c: "written"),
        ],
        run_id="run-login",
        echo=echoed.append,
        mode=NIGHTLY,
    )
    assert [o.status for o in out] == ["ok", "llm_backend_down", "llm_backend_down", "ok"]
    extract, remote = out[1], out[2]
    assert extract.error == remote.error == llm.LOGIN_EXPIRED
    assert (extract.llm_answered, extract.llm_cached, extract.llm_failed) == (1, 1, 2)
    assert len(fake.calls) - spawned_before == 1, "one try, then every call fails at once"
    assert any(
        "extract: FAILED — Claude login expired: run claude, then /login" in e for e in echoed
    )
    rows = [
        json.loads(r["detail"])
        for r in conn.execute("SELECT detail FROM events WHERE kind='run_all.stage' ORDER BY id")
    ]
    assert rows[1]["error"] == llm.LOGIN_EXPIRED and rows[1]["llm_cached"] == 1

    banner = freshness_banner(conn, 36)
    first = banner.splitlines()[0]
    assert first.startswith("⚠ Claude login expired: run claude, then /login.")
    assert "extract (LLM backend down)" in banner

    report = nightly.report(conn)
    assert report.splitlines()[0] == "⚠ Claude login expired: run claude, then /login"
    assert "extract: llm_backend_down" in report and "remote: llm_backend_down" in report


def test_cache_hits_no_longer_hide_a_backend_whose_every_live_call_failed(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _use(monkeypatch, FakeCli((0, _envelope("cached"), "")))
    llm.call(conn, "extract", "job a", cache_key="a")
    fake.replies = [(1, _envelope("API Error: 500 Internal", is_error=True), "")]
    (out,) = run_stages(conn, [("extract", _triage_like("extract", "a", "b"))])
    assert out.status == "llm_all_failed"
    assert out.error and "500 Internal" in out.error
    assert "every LLM call failed: llm call failed after 3 attempts" in out.line()
    assert "500 Internal" in freshness_banner(conn, 36)


def test_a_new_run_tries_the_backend_again(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _use(monkeypatch, FakeCli((1, _auth_envelope(), "")))
    with pytest.raises(llm.LLMBackendDown):
        llm.call(conn, "extract", "p")
    fake.replies = [(0, _envelope("logged in again"), "")]
    (out,) = run_stages(conn, [("extract", _triage_like("extract", "x"))])
    assert out.status == "ok" and out.llm_answered == 1
    assert "login expired" not in freshness_banner(conn, 36)


def test_a_stage_without_llm_calls_is_not_failed_by_the_latch(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, FakeCli((1, _auth_envelope(), "")))
    out = run_stages(
        conn,
        [("extract", _triage_like("extract", "a")), ("ghost-close", lambda c: "0 closed")],
    )
    assert [o.status for o in out] == ["llm_backend_down", "ok"]
