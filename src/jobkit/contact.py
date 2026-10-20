"""The polite User-Agent jobkit sends to the public job-board endpoints.

Public ATS and feed APIs ask automated clients to identify themselves with a
way to reach the operator. The contact address is the person running this
checkout, never one baked into the code:

1. JOBKIT_CONTACT_EMAIL (environment or .env), else
2. identity.email in data/answer_bank.yaml (skipped while it is a template
   placeholder such as you@example.com), else
3. no address: a generic jobkit User-Agent.

Read at request time, not import time: the CLI and the MCP server load .env
after the adapters are imported (paths.enter_home)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

import yaml

PRODUCT = "jobkit/0.1"
GENERIC_USER_AGENT = f"{PRODUCT} (personal job-search tool)"
CONTACT_ENV = "JOBKIT_CONTACT_EMAIL"

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
# RFC 2606 reserved domains: what the templates use as placeholders
_PLACEHOLDER_DOMAINS = re.compile(r"@(?:[^@]*\.)?example\.(?:com|org|net)$", re.I)

_bank_cache: tuple[str, int, str | None] | None = None


def _usable(address: object) -> str | None:
    text = str(address or "").strip()
    if not _EMAIL.match(text) or _PLACEHOLDER_DOMAINS.search(text):
        return None
    return text


def _bank_email() -> str | None:
    """identity.email from the answer bank; None when there is no bank, it does
    not parse, or the address is a placeholder. Cached per file version."""
    global _bank_cache
    from jobkit.autofill import answer_bank  # lazy: keeps the adapters' imports light

    path = Path(answer_bank.BANK_PATH)
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    if _bank_cache is not None and _bank_cache[:2] == (str(path), mtime):
        return _bank_cache[2]
    try:
        bank = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        bank = {}
    identity = bank.get("identity") if isinstance(bank, dict) else None
    email = _usable(identity.get("email")) if isinstance(identity, dict) else None
    _bank_cache = (str(path), mtime, email)
    return email


def contact_email(environ: Mapping[str, str] | None = None) -> str | None:
    """The operator's contact address, or None (see the module docstring)."""
    env = os.environ if environ is None else environ
    return _usable(env.get(CONTACT_ENV)) or _bank_email()


def user_agent() -> str:
    """The User-Agent header value for every board and feed request."""
    email = contact_email()
    return f"{PRODUCT} (job-search tool; contact: {email})" if email else GENERIC_USER_AGENT
