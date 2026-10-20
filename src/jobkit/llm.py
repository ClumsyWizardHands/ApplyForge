"""All LLM calls go through here (CLAUDE.md conventions): retries, token logging,
model tier per stage, and a content-hash cache so nothing is paid for twice.

Backend: headless Claude Code (`claude -p`) using the user's Claude subscription —
no API key required. If ANTHROPIC_API_KEY exists in the environment, the
anthropic SDK is used instead. Spec §10 assumed the SDK; the CLI backend is a
recorded amendment (2026-08-18).

The CLI backend runs isolated (audit 2026-10-08 §3 item 4): a throwaway working
directory, no MCP servers, no tools, no user customizations, a short system
prompt (prompts/system.md). Without that every call inherited CLAUDE.md, the user's MCP servers
and skills — about 42k input tokens and 17-36 s per triage call.

Cache integrity (audit 2026-10-08 §2 item 2): the effective cache key is
sha256(stage, resolved model, caller cache_key, system prompt, rendered prompt).
A corpus edit, a prompt edit or a model change all reach the rendered prompt or
the model, so each one misses the cache; two jobs only share a cached answer
when the model would have seen byte-identical input."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from jobkit.log import log

PROMPTS_DIR = Path("prompts")

# ---- model choice: the ONE owner (CLAUDE.md "one knob, one owner") ----
# stage -> tier. Haiku for triage; Opus for everything an employer reads
# (tailor, cover, novel answers — followups reuse "cover") and for the
# self-review of those drafts (pipeline/self_review.py) and for the claim
# judge that gates every letter and drafted answer (validate/claim_judge.py);
# Sonnet for the critic.
STAGE_TIER: dict[str, str] = {
    "extract": "haiku",
    "remote": "haiku",
    "comp": "haiku",
    "score": "haiku",
    "tailor": "opus",
    "cover": "opus",
    "novel": "opus",
    "self_review": "opus",
    "claim_judge": "opus",
    "critic": "sonnet",
}
# tier -> full model id. The SDK backend sends the full id; the CLI backend
# sends the tier alias, which the CLI resolves. The full id is what enters the
# cache key and the llm_cache.model column, so bumping it here misses the cache.
TIER_MODEL_ID: dict[str, str] = {
    "haiku": "claude-haiku-5-5",
    "sonnet": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
}
# Opus resumes and cover letters were truncated at the old 2000 (audit §3.4).
# Below the SDK's non-streaming ceiling, so a plain messages.create still works.
SDK_MAX_TOKENS = 8000

# Same system prompt on both backends, read from prompts/system.md (versioned
# frontmatter like every other prompt; CLAUDE.md: never inline a prompt string).
# On the CLI it replaces Claude Code's coding-agent prompt. Part of the cache
# key: editing it changes the input. Not a stage, so it has no STAGE_TIER entry.
SYSTEM_PROMPT_NAME = "system"

# `claude -p` isolation. Every flag verified against `claude --help` (2.1.280):
#   --strict-mcp-config + --mcp-config {}: no MCP servers from any user config
#   --tools "": no built-in tools
#   --safe-mode: no CLAUDE.md, skills, plugins, hooks or custom agents
#   --no-session-persistence: 600 triage calls must not write 600 transcripts
# plus cwd = a fresh temp dir, so there is no project CLAUDE.md or memory to find.
EMPTY_MCP_CONFIG = '{"mcpServers": {}}'
CLI_ISOLATION_FLAGS: tuple[str, ...] = (
    "--strict-mcp-config",
    "--mcp-config",
    EMPTY_MCP_CONFIG,
    "--tools",
    "",
    "--safe-mode",
    "--no-session-persistence",
)
CLI_TIMEOUT_SECONDS = 300

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_cache (
  cache_key TEXT PRIMARY KEY,
  stage TEXT NOT NULL,
  model TEXT NOT NULL,
  response TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# ---- named queries (llm_cache is owned by this module) ----
CACHE_GET = "SELECT response FROM llm_cache WHERE cache_key = ?"
CACHE_PUT = (
    "INSERT OR REPLACE INTO llm_cache (cache_key, stage, model, response) VALUES (?, ?, ?, ?)"
)
CACHE_DELETE = "DELETE FROM llm_cache WHERE cache_key = ?"

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)

# ---- per-process call tally (audit 2026-10-08 §3 item 5) ----
# `jobkit run-all` reads it around each stage: a stage whose every LLM call
# failed is a failed stage, even though the stage skips the failed jobs and
# returns normally. A cache hit counts as answered.
_TALLY_LOCK = threading.Lock()
_TALLY = {"answered": 0, "failed": 0}


def call_tally() -> tuple[int, int]:
    """(answered, failed) LLM calls made by this process so far."""
    with _TALLY_LOCK:
        return _TALLY["answered"], _TALLY["failed"]


def _count(outcome: str) -> None:
    with _TALLY_LOCK:
        _TALLY[outcome] += 1


class PromptFile:
    def __init__(self, name: str) -> None:
        raw = (PROMPTS_DIR / f"{name}.md").read_text()
        m = _FRONTMATTER.match(raw)
        if not m:
            raise ValueError(f"prompt {name} missing frontmatter")
        import yaml

        meta = yaml.safe_load(m.group(1))
        self.version: str = str(meta["version"])
        self.stage: str = str(meta["stage"])
        self.body: str = raw[m.end() :]

    def render(self, **kwargs: str) -> str:
        out = self.body
        for k, v in kwargs.items():
            out = out.replace("{" + k + "}", v)
        return out


def system_prompt() -> str:
    """The system prompt text, read at call time like every stage prompt. A file
    without frontmatter, or whose stage is not "system", raises (fail loudly)."""
    pf = PromptFile(SYSTEM_PROMPT_NAME)
    if pf.stage != SYSTEM_PROMPT_NAME:
        raise ValueError(f"prompts/{SYSTEM_PROMPT_NAME}.md: stage must be {SYSTEM_PROMPT_NAME!r}")
    text = pf.body.strip()
    if not text:
        raise ValueError(f"prompts/{SYSTEM_PROMPT_NAME}.md: empty system prompt")
    return text


def resolve_model(stage: str) -> str:
    """Full model id for a stage. An unknown stage raises KeyError (fail loudly)."""
    return TIER_MODEL_ID[STAGE_TIER[stage]]


def cache_key_for(stage: str, prompt: str, cache_key: str) -> str:
    """The key llm_cache actually stores: sha256 over stage, resolved model, the
    caller's key, the system prompt and the full rendered prompt. The caller's
    key still namespaces entries but can no longer, on its own, make two
    different inputs share one answer (the empty-JD collision)."""
    h = hashlib.sha256()
    for part in (stage, resolve_model(stage), cache_key, system_prompt(), prompt):
        h.update(part.encode("utf-8"))
        h.update(b"\x1f")  # separator: ("ab", "c") must not hash like ("a", "bc")
    return f"{stage}:{h.hexdigest()}"


def forget(conn: sqlite3.Connection, stage: str, prompt: str, cache_key: str) -> None:
    """Drop one cached response — the one call() would serve for these inputs.
    Used when a cached answer failed validation and must not be served again."""
    conn.executescript(CACHE_SCHEMA)
    conn.execute(CACHE_DELETE, (cache_key_for(stage, prompt, cache_key),))
    conn.commit()


def cli_argv(stage: str) -> list[str]:
    """The isolated `claude -p` command line for a stage (tier alias as model)."""
    return [
        "claude",
        "-p",
        "--model",
        STAGE_TIER[stage],
        "--output-format",
        "json",
        *CLI_ISOLATION_FLAGS,
        "--system-prompt",
        system_prompt(),
    ]


def _cli_envelope(stdout: str) -> dict[str, Any]:
    """The `--output-format json` result object. Tolerates a notice line printed
    before it: falls back to the last line that parses as a JSON object."""
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        parsed = None
        for line in reversed(stdout.strip().splitlines()):
            try:
                parsed = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    if not isinstance(parsed, dict):
        raise RuntimeError(f"claude -p printed no JSON result: {stdout[:200]!r}")
    return parsed


def _call_claude_cli(stage: str, prompt: str) -> tuple[str, dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="jobkit-llm-") as scratch:
        proc = subprocess.run(
            cli_argv(stage),
            input=prompt,
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_SECONDS,
            cwd=scratch,
        )
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p failed: {proc.stderr[:300]}")
    envelope = _cli_envelope(proc.stdout)
    if envelope.get("is_error") or str(envelope.get("subtype", "success")).startswith("error"):
        # an error envelope is not an answer — raise so it is retried, never cached
        raise RuntimeError(f"claude -p returned an error: {str(envelope.get('result'))[:300]}")
    usage: dict[str, Any] = dict(envelope.get("usage") or {})
    served_by = list((envelope.get("modelUsage") or {}).keys())
    if served_by:
        usage["served_by"] = served_by
    return str(envelope.get("result", "")), usage


def _call_sdk(stage: str, prompt: str) -> tuple[str, dict[str, Any]]:
    import anthropic

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=resolve_model(stage),
        max_tokens=SDK_MAX_TOKENS,
        system=system_prompt(),
        messages=[{"role": "user", "content": prompt}],
    )
    if msg.stop_reason in ("max_tokens", "refusal"):
        # a truncated or refused reply is not an answer — raise, never cache it
        raise RuntimeError(f"{stage}: response ended with stop_reason={msg.stop_reason}")
    text = "".join(b.text for b in msg.content if b.type == "text")
    return text, {"input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens}


def call(
    conn: sqlite3.Connection,
    stage: str,
    prompt: str,
    *,
    cache_key: str | None = None,
    retries: int = 2,
) -> str:
    """One LLM call. Cached when cache_key is given (under cache_key_for(), not
    the raw key); retried on failure."""
    conn.executescript(CACHE_SCHEMA)
    model = resolve_model(stage)
    key = cache_key_for(stage, prompt, cache_key) if cache_key else None
    if key:
        row = conn.execute(CACHE_GET, (key,)).fetchone()
        if row:
            _count("answered")
            return str(row["response"])
    sdk = bool(os.environ.get("ANTHROPIC_API_KEY"))
    backend = _call_sdk if sdk else _call_claude_cli
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            t0 = time.monotonic()
            text, usage = backend(stage, prompt)
            log(
                "llm.call",
                stage=stage,
                model=model,
                backend="sdk" if sdk else "cli",
                seconds=round(time.monotonic() - t0, 1),
                usage=usage,
                cached=False,
            )
            if key:
                conn.execute(CACHE_PUT, (key, stage, model, text))
                conn.commit()
            _count("answered")
            return text
        except Exception as e:  # noqa: BLE001 — retry then surface
            last_err = e
            log("llm.retry", stage=stage, attempt=attempt, error=str(e)[:200])
            time.sleep(2 * (attempt + 1))
    _count("failed")
    raise RuntimeError(f"llm call failed after {retries + 1} attempts: {last_err}")


def _extract_json(text: str) -> dict[str, Any]:
    """Parse the first JSON object out of a model response, tolerating code
    fences and trailing prose (raw_decode stops at the object's real end)."""
    cleaned = re.sub(r"```(?:json)?", "", text)
    start = cleaned.find("{")
    if start < 0:
        raise ValueError(f"no JSON object in response: {text[:200]}")
    parsed, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    if not isinstance(parsed, dict):
        raise ValueError("response is not a JSON object")
    return parsed


def call_json(
    conn: sqlite3.Connection, stage: str, prompt: str, *, cache_key: str | None = None
) -> dict[str, Any]:
    """Call and parse a JSON object. A malformed response busts its own cache
    entry and gets one fresh retry — a bad reply must never poison the cache."""
    text = call(conn, stage, prompt, cache_key=cache_key)
    try:
        return _extract_json(text)
    except (ValueError, json.JSONDecodeError):
        log("llm.bad_json", stage=stage, sample=text[:200])
        if cache_key:
            forget(conn, stage, prompt, cache_key)
        text = call(conn, stage, prompt, cache_key=cache_key)
        return _extract_json(text)
