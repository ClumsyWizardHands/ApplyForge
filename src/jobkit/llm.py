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
when the model would have seen byte-identical input.

Backend failures (D7, 2026-10-23). `claude -p --output-format json` reports a
failed call (an expired login among them) as an error envelope on STDOUT and
exits 1 with stderr empty; the old code kept only stderr, so 3,212 failures
were logged as "claude -p failed: " with no reason. classify_cli_result()
reads the envelope first (result, api_error_status, api_error), then stderr,
then stdout, and never returns an empty message. A failure that no retry can
fix stops at once:

* an expired or missing login (or a rejected ANTHROPIC_API_KEY on the SDK
  backend) or a used-up usage limit raises LLMBackendDown. It is not retried,
  and it latches for this process (backend_problem()): every later call fails
  at once with the same message ("Claude login expired: run claude, then
  /login") instead of trying 3 times per job. A latched call is refused for
  LATCH_RETRY_SECONDS; after that one caller tries the backend live again
  (and re-latches on failure), so a long-lived process (the MCP server)
  recovers within a minute of the person logging in again. A live answer
  or an "ok" from probe_cli() (jobkit doctor) clears the latch at once, and
  pipeline/runner.py clears it when a run starts. The reason stays readable
  (backend_problem()) for LATCH_SECONDS unless cleared.
* a model the CLI does not know raises LLMModelUnknown ("run claude
  update"), latched for that tier only.

A call answered by a different model than the tier's (an old CLI serves old
models under the same alias) logs `llm.model_mismatch` once per stage.

The per-process tally separates live answers from cache hits: a stage whose
every live call failed is a failed stage even when cache hits answered some
jobs (on 10-22 they hid a dead backend)."""

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from jobkit.log import log

PROMPTS_DIR = Path("prompts")

# ---- model choice: the ONE owner (CLAUDE.md "one knob, one owner") ----
# stage -> tier. Haiku for triage; Opus for everything an employer reads
# (tailor, cover, novel answers), for the self-review of those drafts
# (pipeline/self_review.py) and for the claim judge that gates every letter
# and drafted answer (validate/claim_judge.py); Sonnet for the critic.
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
# `jobkit run-all` reads it around each stage: a stage whose every live LLM
# call failed is a failed stage, even though the stage skips the failed jobs
# and returns normally. Cache hits are counted apart (D7): they answer a job
# but say nothing about whether the backend works.
_TALLY_LOCK = threading.Lock()
_TALLY = {"live": 0, "cached": 0, "failed": 0}
_LAST_FAILURE: list[str] = []  # the newest failed call's message (at most one)


class CallCounts(NamedTuple):
    live: int  # answered by the backend
    cached: int  # answered from llm_cache
    failed: int


def call_counts() -> CallCounts:
    """LLM calls made by this process so far: live answers, cache hits, failures."""
    with _TALLY_LOCK:
        return CallCounts(_TALLY["live"], _TALLY["cached"], _TALLY["failed"])


def call_tally() -> tuple[int, int]:
    """(answered, failed) LLM calls made by this process so far; answered
    counts live answers and cache hits."""
    c = call_counts()
    return c.live + c.cached, c.failed


def last_failure() -> str | None:
    """The message of the newest failed call in this process, or None."""
    with _TALLY_LOCK:
        return _LAST_FAILURE[0] if _LAST_FAILURE else None


def _count(outcome: str) -> None:
    with _TALLY_LOCK:
        _TALLY[outcome] += 1


def _count_failure(message: str) -> None:
    with _TALLY_LOCK:
        _TALLY["failed"] += 1
        _LAST_FAILURE[:] = [message]


# ---- backend failures that no retry fixes (D7) ----

LOGIN_EXPIRED = "Claude login expired: run claude, then /login"
API_KEY_REJECTED = (
    "ANTHROPIC_API_KEY was rejected: put a valid key in .env, or remove it to use the claude CLI"
)
USAGE_LIMIT = "Claude usage limit reached: jobkit can call Claude again once the limit resets"
MODEL_UPDATE_FIX = "run claude update"


def model_unknown_message(alias: str) -> str:
    return f"Claude Code does not know the model {alias!r}: {MODEL_UPDATE_FIX}"


class LLMBackendDown(RuntimeError):
    """The backend refuses every call: login expired, key rejected, usage
    limit reached. Never retried; latched for the process (backend_problem)."""


class LLMModelUnknown(RuntimeError):
    """The claude CLI does not know the tier's model (an old CLI). Never
    retried; latched for that tier."""


# How long a latched failure is reported (backend_problem) unless cleared.
LATCH_SECONDS = 15 * 60
# How long a latched failure refuses calls before one caller tries live again.
LATCH_RETRY_SECONDS = 60
ALL_TIERS = "*"
_LATCH_LOCK = threading.Lock()
# scope (ALL_TIERS or a tier) -> (exception class, message, monotonic time)
_LATCH: dict[str, tuple[type[RuntimeError], str, float]] = {}


def _latch(scope: str, kind: type[RuntimeError], message: str) -> None:
    with _LATCH_LOCK:
        _LATCH[scope] = (kind, message, time.monotonic())


def _latched(scope: str) -> tuple[type[RuntimeError], str] | None:
    with _LATCH_LOCK:
        entry = _LATCH.get(scope)
        if entry is None:
            return None
        kind, message, at = entry
        if time.monotonic() - at > LATCH_SECONDS:
            del _LATCH[scope]
            return None
        return kind, message


def _refusing(scope: str) -> tuple[type[RuntimeError], str] | None:
    """The latched failure a call in `scope` is refused with, or None. Past
    LATCH_RETRY_SECONDS the first caller is let through to try the backend
    live (the latch's time is renewed, so others still wait)."""
    with _LATCH_LOCK:
        entry = _LATCH.get(scope)
        if entry is None:
            return None
        kind, message, at = entry
        now = time.monotonic()
        if now - at > LATCH_SECONDS:
            del _LATCH[scope]
            return None
        if now - at >= LATCH_RETRY_SECONDS:
            _LATCH[scope] = (kind, message, now)
            return None
        return kind, message


def _unlatch(*scopes: str) -> None:
    with _LATCH_LOCK:
        for scope in scopes:
            _LATCH.pop(scope, None)


def backend_problem() -> str | None:
    """Why every LLM call in this process now fails at once (an expired
    login, a rejected key, a used-up limit), or None."""
    hit = _latched(ALL_TIERS)
    return hit[1] if hit else None


def reset_backend_problem() -> None:
    """Forget every latched failure (a new run tries the backend again)."""
    with _LATCH_LOCK:
        _LATCH.clear()


@dataclass(frozen=True)
class CliFailure:
    kind: str  # auth | usage_limit | model | other
    message: str  # what the person reads; never empty
    detail: str  # what the CLI said, for the log


_AUTH_RE = re.compile(
    r"Failed to authenticate|OAuth (?:session expired|token (?:has )?(?:expired|revoked))|"
    r"Not logged in|Please run /login|Invalid API key|authentication_error|"
    r"authentication_failed",
    re.I,
)
_USAGE_RE = re.compile(r"usage limit reached|hit your (?:usage |session |weekly )?limit", re.I)
_MODEL_RE = re.compile(
    r"unrecognized model|unknown model|model\b.{0,60}\b(?:not found|is not recognized|"
    r"does not exist|is not available)|not_found_error.{0,120}model",
    re.I | re.S,
)


def model_warning(stderr: str) -> str | None:
    """The claude CLI's unknown-model warning in its stderr, as one line, or None."""
    for line in stderr.splitlines():
        if _MODEL_RE.search(line):
            return " ".join(line.split())[:200]
    return None


def _flat(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return str(value)


def classify_cli_result(
    returncode: int, stdout: str, stderr: str, alias: str = ""
) -> CliFailure | None:
    """None when `claude -p --output-format json` answered; otherwise what went
    wrong. The envelope on stdout is read first (that is where the CLI puts an
    auth failure, with exit 1 and stderr empty), then stderr, then stdout."""
    try:
        envelope: dict[str, Any] | None = _cli_envelope(stdout)
    except RuntimeError:
        envelope = None
    errored = envelope is None or bool(envelope.get("is_error"))
    if envelope is not None and str(envelope.get("subtype", "success")).startswith("error"):
        errored = True
    if returncode == 0 and not errored:
        return None
    parts: list[str] = []
    status: Any = None
    if envelope is not None:
        status = envelope.get("api_error_status")
        for k in ("result", "api_error_code", "api_error", "error"):
            text = _flat(envelope.get(k)).strip()
            if text and text not in parts:
                parts.append(text)
        if status:
            parts.append(f"status {status}")
    err = stderr.strip()
    out = stdout.strip()
    detail = " | ".join(parts) or err[-300:] or out[-300:]
    if not detail:
        detail = f"claude -p exited {returncode} with no output"
    detail = " ".join(detail.split())[:500]
    hay = f"{detail}\n{err}"
    if str(status) == "401" or _AUTH_RE.search(hay):
        return CliFailure("auth", LOGIN_EXPIRED, detail)
    if _USAGE_RE.search(hay):
        return CliFailure("usage_limit", USAGE_LIMIT, detail)
    if _MODEL_RE.search(hay):
        return CliFailure("model", model_unknown_message(alias or "?"), detail)
    if envelope is None and returncode == 0:
        return CliFailure("other", f"claude -p printed no JSON result: {detail[:200]}", detail)
    if returncode == 0:
        return CliFailure("other", f"claude -p returned an error: {detail[:300]}", detail)
    return CliFailure("other", f"claude -p failed (exit {returncode}): {detail[:300]}", detail)


_MODEL_WARNED: set[str] = set()


def _check_served(stage: str, served_by: list[str], stderr: str) -> None:
    """Log once per stage per process when the CLI answered with another
    model than the tier's, or warned that it does not know the model."""
    expected = resolve_model(stage)
    warning = model_warning(stderr)
    mismatch = bool(served_by) and not any(s.startswith(expected) for s in served_by)
    if not (warning or mismatch) or stage in _MODEL_WARNED:
        return
    _MODEL_WARNED.add(stage)
    log(
        "llm.model_mismatch",
        stage=stage,
        alias=STAGE_TIER[stage],
        requested=expected,
        served_by=served_by,
        warning=warning,
        fix=MODEL_UPDATE_FIX,
    )


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
        try:
            proc = subprocess.run(
                cli_argv(stage),
                input=prompt,
                capture_output=True,
                text=True,
                timeout=CLI_TIMEOUT_SECONDS,
                cwd=scratch,
            )
        except subprocess.TimeoutExpired:
            # str(TimeoutExpired) holds the whole argv, system prompt included
            raise RuntimeError(f"claude -p timed out after {CLI_TIMEOUT_SECONDS}s") from None
    failure = classify_cli_result(proc.returncode, proc.stdout, proc.stderr, STAGE_TIER[stage])
    if failure is not None:
        log(
            "llm.cli_error",
            stage=stage,
            kind=failure.kind,
            exit=proc.returncode,
            detail=failure.detail,
            stderr=" ".join(proc.stderr.split())[:300],
        )
        if failure.kind in ("auth", "usage_limit"):
            raise LLMBackendDown(failure.message)
        if failure.kind == "model":
            raise LLMModelUnknown(failure.message)
        # an error envelope is not an answer — raise so it is retried, never cached
        raise RuntimeError(failure.message)
    envelope = _cli_envelope(proc.stdout)
    usage: dict[str, Any] = dict(envelope.get("usage") or {})
    served_by = [str(m) for m in (envelope.get("modelUsage") or {})]
    if served_by:
        usage["served_by"] = served_by
    _check_served(stage, served_by, proc.stderr)
    return str(envelope.get("result", "")), usage


def _call_sdk(stage: str, prompt: str) -> tuple[str, dict[str, Any]]:
    import anthropic

    client = anthropic.Anthropic()
    rejected = getattr(anthropic, "AuthenticationError", None)
    try:
        msg = client.messages.create(
            model=resolve_model(stage),
            max_tokens=SDK_MAX_TOKENS,
            system=system_prompt(),
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        if isinstance(rejected, type) and isinstance(e, rejected):
            raise LLMBackendDown(API_KEY_REJECTED) from e
        raise
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
    the raw key); retried on failure, except a failure no retry fixes
    (LLMBackendDown, LLMModelUnknown): that one is raised at once and latched,
    so later calls in this process fail at once with the same message."""
    conn.executescript(CACHE_SCHEMA)
    model = resolve_model(stage)
    key = cache_key_for(stage, prompt, cache_key) if cache_key else None
    if key:
        row = conn.execute(CACHE_GET, (key,)).fetchone()
        if row:
            _count("cached")
            return str(row["response"])
    tier = STAGE_TIER[stage]
    latched = _refusing(ALL_TIERS) or _refusing(tier)
    if latched is not None:
        kind, message = latched
        _count_failure(message)
        raise kind(message)
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
            _count("live")
            _unlatch(ALL_TIERS, tier)  # the backend answers again
            return text
        except (LLMBackendDown, LLMModelUnknown) as e:
            # no retry fixes these: stop now, and stop every later call too
            scope = ALL_TIERS if isinstance(e, LLMBackendDown) else tier
            _latch(scope, type(e), str(e))
            log("llm.backend_down", stage=stage, scope=scope, error=str(e))
            _count_failure(str(e))
            raise
        except Exception as e:  # noqa: BLE001 — retry then surface
            last_err = e
            log("llm.retry", stage=stage, attempt=attempt, error=str(e)[:300])
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    message = f"llm call failed after {retries + 1} attempts: {last_err}"
    _count_failure(message)
    raise RuntimeError(message)


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


# ---- `jobkit doctor`: one tiny live call (D7) ----

PROBE_PROMPT_NAME = "doctor_probe"
PROBE_STAGE = "extract"  # the haiku tier, the cheapest
PROBE_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class ProbeResult:
    kind: str  # ok | auth | usage_limit | model | timeout | other
    message: str
    served_by: tuple[str, ...] = ()
    warning: str | None = None  # the CLI's unknown-model warning, from stderr
    version: str = ""  # `claude --version`, when it answered


def _cli_version() -> str:
    try:
        proc = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=10, check=False
        )
    except Exception:  # noqa: BLE001 — the version is a nicety
        return ""
    return " ".join(proc.stdout.split())[:80] if proc.returncode == 0 else ""


def probe_cli(timeout: float = PROBE_TIMEOUT_SECONDS) -> ProbeResult:
    """One tiny `claude -p` call, isolated like every other (cli_argv) with the
    haiku tier, to tell whether the CLI is logged in and which model answers.
    No cache, no tally, no retry: doctor reads the result. Never raises. An
    "ok" clears a latched backend failure (backend_problem)."""
    try:
        argv = cli_argv(PROBE_STAGE)
        prompt = PromptFile(PROBE_PROMPT_NAME).render()
    except (OSError, ValueError, KeyError) as e:
        return ProbeResult("other", f"could not build the check call: {e}")
    try:
        with tempfile.TemporaryDirectory(prefix="jobkit-doctor-") as scratch:
            proc = subprocess.run(
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=scratch,
                check=False,
            )
    except subprocess.TimeoutExpired:
        return ProbeResult("timeout", f"claude did not answer in {timeout:g}s")
    except Exception as e:  # noqa: BLE001 — a diagnostic reports, it never raises
        return ProbeResult("other", f"claude could not be started: {e}")
    warning = model_warning(proc.stderr)
    version = _cli_version()
    failure = classify_cli_result(
        proc.returncode, proc.stdout, proc.stderr, STAGE_TIER[PROBE_STAGE]
    )
    if failure is not None:
        return ProbeResult(failure.kind, failure.message, (), warning, version)
    envelope = _cli_envelope(proc.stdout)
    served = tuple(str(m) for m in (envelope.get("modelUsage") or {}))
    _unlatch(ALL_TIERS)  # logged in again: later calls in this process go live
    return ProbeResult("ok", "answered", served, warning, version)
