"""Pasted-URL → (ats, board, job id) resolver. The ingestion path for jobs the
user finds anywhere — LinkedIn email alerts, a friend's link, a VC board — with
zero scraping of the source: the URL itself carries the employer's ATS identity
(per-family URL shapes, idea credit: freehire's atsboard recognizer, MIT)."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ResolvedUrl:
    ats: str  # greenhouse | lever | ashby | workday | smartrecruiters | ...
    token: str  # board/tenant token ("" when unknown)
    external_id: str  # job id when present in the URL ("" otherwise)
    supported: bool  # True → direct API adapters can take over
    note: str = ""


_PATTERNS: list[tuple[str, re.Pattern[str], bool]] = [
    (
        "greenhouse",
        re.compile(
            r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_app\?for=)?"
            r"([a-z0-9]+)(?:/jobs/(\d+))?",
            re.I,
        ),
        True,
    ),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board\?for=([a-z0-9]+)", re.I), True),
    # '_' too: board resolution probes underscored tokens (discovery/board_resolver.py)
    ("lever", re.compile(r"jobs\.lever\.co/([a-zA-Z0-9_-]+)(?:/([0-9a-f-]{36}))?"), True),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([a-zA-Z0-9._\-]+)(?:/([0-9a-f-]{36}))?"), True),
    ("workday", re.compile(r"([a-z0-9-]+)\.(?:wd\d+\.)?myworkdayjobs\.com", re.I), False),
    (
        "smartrecruiters",
        re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9]+)"),
        False,
    ),
    ("icims", re.compile(r"careers?-?([a-z0-9]+)\.icims\.com", re.I), False),
    ("workable", re.compile(r"apply\.workable\.com/([a-z0-9-]+)", re.I), False),
    ("recruitee", re.compile(r"([a-z0-9-]+)\.recruitee\.com", re.I), False),
    ("rippling", re.compile(r"ats\.rippling\.com/([a-z0-9-]+)", re.I), False),
]


def resolve(url: str) -> ResolvedUrl | None:
    for ats, pat, supported in _PATTERNS:
        m = pat.search(url)
        if m:
            groups = m.groups()
            token = groups[0] or ""
            external_id = (groups[1] if len(groups) > 1 else "") or ""
            note = "" if supported else "manual-path ATS: staging via DOM/Claude-in-Chrome"
            return ResolvedUrl(
                ats=ats, token=token, external_id=external_id, supported=supported, note=note
            )
    return None
