#!/usr/bin/env python3
"""Record one live payload per ATS adapter into tests/fixtures/ (trimmed to 3 jobs).
Run manually when shapes change. Tests NEVER hit live APIs (CLAUDE.md rule 12)."""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

# your contact address for the polite User-Agent (as jobkit.contact does)
_CONTACT = os.environ.get("JOBKIT_CONTACT_EMAIL", "").strip()
UA = {"User-Agent": f"jobkit/0.1 (contact: {_CONTACT})" if _CONTACT else "jobkit/0.1"}
OUT = Path("tests/fixtures")

SOURCES = {
    "greenhouse_anthropic.json": "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs?content=true",
    "lever_palantir.json": "https://api.lever.co/v0/postings/palantir?mode=json",
    "ashby_ramp.json": "https://api.ashbyhq.com/posting-api/job-board/ramp?includeCompensation=true",
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, url in SOURCES.items():
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
            payload = json.loads(r.read())
        if isinstance(payload, dict) and "jobs" in payload:
            payload["jobs"] = payload["jobs"][:3]
        elif isinstance(payload, list):
            payload = payload[:3]
        (OUT / name).write_text(json.dumps(payload, indent=1))
        print("recorded", name)


if __name__ == "__main__":
    main()
