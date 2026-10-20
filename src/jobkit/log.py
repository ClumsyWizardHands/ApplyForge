"""Structured JSONL logging to logs/jobkit.jsonl (CLAUDE.md conventions)."""

from __future__ import annotations

import json
import time
from typing import Any

from jobkit import paths

LOG_PATH = paths.HOME / "logs" / "jobkit.jsonl"  # project root, not the working directory


def log(event: str, **fields: Any) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
