"""LLM layer: tier map, backend isolation, cache-key integrity. Both backends
are faked at their boundary (subprocess.run / the anthropic module) — no test
spawns `claude` or reaches the API (hard rule 12)."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from jobkit import llm

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)


# ---- tier map: one owner ----


def test_stage_tiers_and_model_ids() -> None:
    assert {s: llm.resolve_model(s) for s in llm.STAGE_TIER} == {
        "extract": "claude-haiku-5-5",
        "remote": "claude-haiku-5-5",
        "comp": "claude-haiku-5-5",
        "score": "claude-haiku-5-5",
        "tailor": "claude-opus-5-5",
        "cover": "claude-opus-5-5",
        "novel": "claude-opus-5-5",
        "self_review": "claude-opus-5-5",
        "claim_judge": "claude-opus-5-5",
        "critic": "claude-sonnet-5-5",
    }
    with pytest.raises(KeyError):
        llm.resolve_model("unknown-stage")


def test_prompt_frontmatter_tier_agrees_with_llm() -> None:
    """Prompt frontmatter repeats the tier as documentation; it must never
    disagree with STAGE_TIER, the one place the model is actually chosen.
    The system prompt is the one non-stage prompt: sent for every stage, so it
    names no stage tier and carries no model_tier."""
    for path in sorted((REPO_ROOT / "prompts").glob("*.md")):
        m = llm._FRONTMATTER.match(path.read_text())
        assert m, path
        meta = yaml.safe_load(m.group(1))
        assert str(meta["version"]), path.name
        if path.stem == llm.SYSTEM_PROMPT_NAME:
            assert meta["stage"] == llm.SYSTEM_PROMPT_NAME, path.name
            assert "model_tier" not in meta, path.name
            assert meta["stage"] not in llm.STAGE_TIER, path.name
            continue
        assert meta["stage"] in llm.STAGE_TIER, path.name
        if "model_tier" in meta:
            assert meta["model_tier"] == llm.STAGE_TIER[meta["stage"]], path.name


# ---- system prompt: a versioned prompt file, never an inline string ----


def _prompts_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private copy of prompts/ that a test may edit (the sandbox's prompts/
    is a symlink to the repo's)."""
    copy = tmp_path / "prompts_copy"
    copy.mkdir()
    for path in (REPO_ROOT / "prompts").glob("*.md"):
        (copy / path.name).write_text(path.read_text())
    monkeypatch.setattr(llm, "PROMPTS_DIR", copy)
    return copy


def test_system_prompt_comes_from_the_versioned_prompt_file() -> None:
    pf = llm.PromptFile(llm.SYSTEM_PROMPT_NAME)
    assert pf.version and pf.stage == "system"
    text = llm.system_prompt()
    assert text == pf.body.strip() and text
    assert not hasattr(llm, "SYSTEM_PROMPT")  # no inline copy left to drift


def test_system_prompt_edit_misses_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = _prompts_copy(tmp_path, monkeypatch)
    before = llm.cache_key_for("extract", "prompt", "k")
    path = copy / "system.md"
    path.write_text(path.read_text().rstrip("\n") + " Be brief.\n")
    assert llm.cache_key_for("extract", "prompt", "k") != before


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ("no frontmatter at all\n", "missing frontmatter"),
        ('---\nversion: "1.0"\nstage: extract\n---\nhello\n', "stage must be"),
        ('---\nversion: "1.0"\nstage: system\n---\n  \n', "empty system prompt"),
    ],
)
def test_bad_system_prompt_file_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str, match: str
) -> None:
    copy = _prompts_copy(tmp_path, monkeypatch)
    (copy / "system.md").write_text(content)
    with pytest.raises(ValueError, match=match):
        llm.system_prompt()


# ---- cache key ----


def test_cache_key_covers_every_input() -> None:
    base = llm.cache_key_for("extract", "prompt", "extract:h:1.0")
    assert base == llm.cache_key_for("extract", "prompt", "extract:h:1.0")
    stage, digest = base.split(":")
    assert stage == "extract" and len(digest) == 64
    assert base != llm.cache_key_for("extract", "prompt!", "extract:h:1.0")
    assert base != llm.cache_key_for("extract", "prompt", "extract:h:1.1")
    assert base != llm.cache_key_for("remote", "prompt", "extract:h:1.0")
    # same tier, different stage name still differs; separator blocks run-on collisions
    assert llm.cache_key_for("extract", "ab", "c") != llm.cache_key_for("extract", "a", "bc")


def test_bad_json_busts_the_effective_key(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = iter(["not json at all", '{"ok": true}'])
    monkeypatch.setattr(llm, "_call_claude_cli", lambda stage, prompt: (next(replies), {}))
    assert llm.call_json(conn, "extract", "p", cache_key="k") == {"ok": True}
    cached = conn.execute("SELECT response FROM llm_cache").fetchall()
    assert [r[0] for r in cached] == ['{"ok": true}']


def test_forget_drops_only_that_entry(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm, "_call_claude_cli", lambda stage, prompt: (prompt, {}))
    llm.call(conn, "cover", "letter A", cache_key="cover:k")
    llm.call(conn, "cover", "letter B", cache_key="cover:k")
    llm.forget(conn, "cover", "letter A", "cover:k")
    left = [r[0] for r in conn.execute("SELECT response FROM llm_cache")]
    assert left == ["letter B"]


# ---- CLI backend: isolated `claude -p` ----


def _envelope(result: str = '{"ok": 1}', **extra: Any) -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": result,
            "usage": {"input_tokens": 120, "output_tokens": 8},
            "modelUsage": {"claude-haiku-5-5": {"inputTokens": 120}},
            **extra,
        }
    )


class FakeClaude:
    """Stands in for subprocess.run: records each `claude -p` call (argv, kwargs,
    and the state of its cwd while it ran) and answers with `stdout`."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.stdout = _envelope()

    def run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cwd = Path(kwargs["cwd"])
        self.calls.append(
            {
                "argv": list(args),
                "kwargs": kwargs,
                "cwd": cwd,
                "cwd_existed": cwd.is_dir(),
                "cwd_listing": sorted(p.name for p in cwd.iterdir()),
            }
        )
        return subprocess.CompletedProcess(args, 0, stdout=self.stdout, stderr="")


@pytest.fixture()
def fake_claude(monkeypatch: pytest.MonkeyPatch) -> FakeClaude:
    fake = FakeClaude()
    monkeypatch.setattr(llm.subprocess, "run", fake.run)
    return fake


@pytest.mark.parametrize(
    ("stage", "alias"), [("extract", "haiku"), ("tailor", "opus"), ("critic", "sonnet")]
)
def test_cli_backend_runs_isolated(
    conn: sqlite3.Connection, fake_claude: FakeClaude, stage: str, alias: str
) -> None:
    text = llm.call(conn, stage, "the prompt", cache_key="k")
    assert text == '{"ok": 1}'
    (rec,) = fake_claude.calls
    argv = rec["argv"]
    assert argv[:2] == ["claude", "-p"]
    assert argv[argv.index("--model") + 1] == alias  # CLI gets the tier alias
    assert argv[argv.index("--output-format") + 1] == "json"
    # no MCP servers from any user/project config
    assert "--strict-mcp-config" in argv
    assert json.loads(argv[argv.index("--mcp-config") + 1]) == {"mcpServers": {}}
    # no built-in tools
    assert argv[argv.index("--tools") + 1] == ""
    # no CLAUDE.md / skills / plugins / hooks; no session transcripts
    assert "--safe-mode" in argv and "--no-session-persistence" in argv
    assert argv[argv.index("--system-prompt") + 1] == llm.system_prompt()
    # the prompt goes on stdin, never on the command line
    assert rec["kwargs"]["input"] == "the prompt" and "the prompt" not in argv
    # cwd: a fresh, empty temp dir (not the repo, not the caller's cwd), removed after
    cwd = rec["cwd"]
    assert rec["cwd_existed"] and rec["cwd_listing"] == []
    assert cwd.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
    assert cwd.resolve() != Path.cwd().resolve()
    assert not cwd.resolve().is_relative_to(REPO_ROOT)
    assert not cwd.exists()


def test_cli_error_envelope_is_never_cached(
    conn: sqlite3.Connection, fake_claude: FakeClaude, no_sleep: None
) -> None:
    fake_claude.stdout = _envelope("API Error: overloaded", is_error=True)
    with pytest.raises(RuntimeError, match="failed after 3 attempts"):
        llm.call(conn, "extract", "p", cache_key="k")
    assert conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] == 0


def test_cli_tolerates_a_notice_line_before_the_json(
    conn: sqlite3.Connection, fake_claude: FakeClaude
) -> None:
    fake_claude.stdout = "notice: running in safe mode\n" + _envelope("{}")
    assert llm.call(conn, "extract", "p") == "{}"


# ---- SDK backend ----


@pytest.fixture()
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A stand-in `anthropic` module; selected because ANTHROPIC_API_KEY is set."""
    state: dict[str, Any] = {"requests": [], "stop_reason": "end_turn"}

    class _Messages:
        def create(self, **kw: Any) -> SimpleNamespace:
            state["requests"].append(kw)
            return SimpleNamespace(
                stop_reason=state["stop_reason"],
                content=[SimpleNamespace(type="text", text='{"ok": 2}')],
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            )

    mod = types.ModuleType("anthropic")
    mod.Anthropic = lambda: SimpleNamespace(messages=_Messages())  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-test-value")
    return state


@pytest.mark.parametrize(
    ("stage", "model"),
    [("score", "claude-haiku-5-5"), ("cover", "claude-opus-5-5"), ("critic", "claude-sonnet-5-5")],
)
def test_sdk_backend_sends_full_ids_and_room_to_write(
    conn: sqlite3.Connection, fake_sdk: dict[str, Any], stage: str, model: str
) -> None:
    assert llm.call(conn, stage, "the prompt", cache_key="k") == '{"ok": 2}'
    (req,) = fake_sdk["requests"]
    assert req["model"] == model
    assert req["max_tokens"] == llm.SDK_MAX_TOKENS == 8000
    assert req["system"] == llm.system_prompt()
    assert req["messages"] == [{"role": "user", "content": "the prompt"}]
    stored = conn.execute("SELECT model FROM llm_cache").fetchone()[0]
    assert stored == model


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal"])
def test_sdk_truncated_or_refused_reply_is_never_cached(
    conn: sqlite3.Connection, fake_sdk: dict[str, Any], no_sleep: None, stop_reason: str
) -> None:
    fake_sdk["stop_reason"] = stop_reason
    with pytest.raises(RuntimeError):
        llm.call(conn, "cover", "p", cache_key="k")
    assert conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] == 0
