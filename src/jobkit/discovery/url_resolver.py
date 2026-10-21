"""Pasted-URL → (ats, board, job id) resolver. The ingestion path for jobs the
user finds anywhere — LinkedIn email alerts, a friend's link, a VC board — with
zero scraping of the source: the URL itself carries the employer's ATS identity
(per-family URL shapes, idea credit: freehire's atsboard recognizer, MIT).

Only Greenhouse, Lever and Ashby are supported: jobkit reads their public
board APIs. Every other system listed here is recognised from the URL alone,
so jobkit can tell the person which system a posting is on. Nothing is
fetched from those systems (CLAUDE.md Hard Rules 9 and 11).

There are two pattern lists. `_ORIGINAL` is the set the board resolver has
always skipped on; `_RECOGNISED` holds everything added later. `resolve`
tries the original list first, so a URL it matched before resolves exactly
as before. `resolve_for_board_matching` reads only the original list:
companies change ATS, so a feed row whose apply link points at a newly
recognised system is still name-matched against Greenhouse, Lever and Ashby
boards."""

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
    system: str = ""  # display name of the hiring system ("Workday", "Taleo")


@dataclass(frozen=True)
class _Pattern:
    ats: str
    rx: re.Pattern[str]
    supported: bool
    system: str


SUPPORTED_SYSTEMS = ("Greenhouse", "Lever", "Ashby")


def unsupported_note(system: str) -> str:
    """The message for a posting on a hiring system jobkit does not read."""
    return (
        f"{system} posting: jobkit can only read job boards on Greenhouse, "
        "Lever and Ashby, so it can't fetch this one. Apply on the employer's site."
    )


def _p(ats: str, rx: str, supported: bool, system: str, flags: int = 0) -> _Pattern:
    return _Pattern(ats, re.compile(rx, flags), supported, system)


# The board resolver's skip reads exactly this list. Do not add to it: add new
# systems to _RECOGNISED (see the module docstring). These regexes are the
# ones in use before _RECOGNISED existed, unchanged.
_ORIGINAL: tuple[_Pattern, ...] = (
    _p(
        "greenhouse",
        r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_app\?for=)?"
        r"([a-z0-9]+)(?:/jobs/(\d+))?",
        True,
        "Greenhouse",
        re.I,
    ),
    _p("greenhouse", r"greenhouse\.io/embed/job_board\?for=([a-z0-9]+)", True, "Greenhouse", re.I),
    # '_' too: board resolution probes underscored tokens (discovery/board_resolver.py)
    _p("lever", r"jobs\.lever\.co/([a-zA-Z0-9_-]+)(?:/([0-9a-f-]{36}))?", True, "Lever"),
    _p("ashby", r"jobs\.ashbyhq\.com/([a-zA-Z0-9._\-]+)(?:/([0-9a-f-]{36}))?", True, "Ashby"),
    _p("workday", r"([a-z0-9-]+)\.(?:wd\d+\.)?myworkdayjobs\.com", False, "Workday", re.I),
    _p(
        "smartrecruiters",
        r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9]+)",
        False,
        "SmartRecruiters",
    ),
    _p("icims", r"careers?-?([a-z0-9]+)\.icims\.com", False, "iCIMS", re.I),
    _p("workable", r"apply\.workable\.com/([a-z0-9-]+)", False, "Workable", re.I),
    _p("recruitee", r"([a-z0-9-]+)\.recruitee\.com", False, "Recruitee", re.I),
    _p("rippling", r"ats\.rippling\.com/([a-z0-9-]+)", False, "Rippling", re.I),
)

# Host anchor for the newer patterns: the match must start at the URL's host,
# so a system's name inside a path or a query string never counts.
_H = r"(?:^|//)"
_SUB = r"(?:[a-z0-9-]+\.)*"
_PORT = r"(?::\d+)?"

# Recognised only (supported=False). Named groups `token` and `id` capture the
# tenant and the job id where the URL carries them. Nothing here is fetched.
_RECOGNISED: tuple[_Pattern, ...] = (
    _p(
        "workday",
        _H + _SUB + r"myworkdaysite\.com/(?:[a-z]{2}-[a-z]{2}/)?recruiting/(?P<token>[a-z0-9_-]+)",
        False,
        "Workday",
        re.I,
    ),
    _p("workday", _H + _SUB + r"myworkdaysite\.com", False, "Workday", re.I),
    # iCIMS hosts other than careers-*: jobs-acme.icims.com, uscareers-acme.icims.com
    _p(
        "icims",
        _H + r"(?:jobs-)?(?P<token>[a-z0-9-]+)\.icims\.com(?:/jobs/(?P<id>\d+))?",
        False,
        "iCIMS",
        re.I,
    ),
    _p(
        "taleo",
        _H + r"(?P<token>[a-z0-9-]+)\.taleo\.net(?:/[^?#]*\?(?:[^#]*&)?job=(?P<id>[A-Za-z0-9]+))?",
        False,
        "Taleo",
        re.I,
    ),
    _p(
        "oracle_hcm",
        _H
        + r"(?P<token>[a-z0-9-]+)\."
        + _SUB
        + r"oraclecloud\.com/hcmUI/CandidateExperience(?:/[^?#]*?/job/(?P<id>\d+))?",
        False,
        "Oracle Cloud HCM",
        re.I,
    ),
    _p(
        "successfactors",
        _H
        + _SUB
        + r"(?:successfactors|sapsf)\.(?:com|eu)"
        + r"(?:/[^?#]*\?(?:[^#]*&)?company=(?P<token>[A-Za-z0-9_-]+))?",
        False,
        "SAP SuccessFactors",
        re.I,
    ),
    _p(
        "ukg",
        _H + r"recruiting\d*\.ultipro\.com/(?P<token>[A-Za-z0-9]+)",
        False,
        "UKG (UltiPro)",
        re.I,
    ),
    _p("ukg", _H + r"(?P<token>[a-z0-9-]+)\.rec\.pro\.ukg\.net", False, "UKG", re.I),
    _p("adp", _H + r"(?:workforcenow|myjobs|recruiting)\.adp\.com", False, "ADP", re.I),
    _p(
        "paylocity",
        _H + r"recruiting\.paylocity\.com/recruiting/jobs/details/(?P<id>\d+)",
        False,
        "Paylocity",
        re.I,
    ),
    _p("paylocity", _H + r"recruiting\.paylocity\.com", False, "Paylocity", re.I),
    _p(
        "bamboohr",
        _H + r"(?P<token>[a-z0-9-]+)\.bamboohr\.com/(?:careers|jobs)(?:/(?P<id>\d+))?",
        False,
        "BambooHR",
        re.I,
    ),
    _p(
        "jobvite",
        _H + r"jobs\.jobvite\.com/(?P<token>[A-Za-z0-9_-]+)(?:/job/(?P<id>[A-Za-z0-9]+))?",
        False,
        "Jobvite",
    ),
    _p("jobvite", _H + r"(?:app|hire)\.jobvite\.com", False, "Jobvite", re.I),
    _p(
        "jazzhr",
        _H + r"(?P<token>[a-z0-9-]+)\.applytojob\.com(?:/apply/(?P<id>[A-Za-z0-9]+))?",
        False,
        "JazzHR",
        re.I,
    ),
    _p(
        "breezy",
        _H + r"(?P<token>[a-z0-9-]+)\.breezy\.hr(?:/p/(?P<id>[a-z0-9]+))?",
        False,
        "Breezy HR",
        re.I,
    ),
    # NEOGOV runs governmentjobs.com (cities, counties, states) and schooljobs.com
    _p(
        "neogov",
        _H
        + r"(?:www\.)?(?:governmentjobs|schooljobs)\.com/careers/(?P<token>[A-Za-z0-9_-]+)"
        + r"(?:/jobs/(?P<id>\d+))?",
        False,
        "NEOGOV",
        re.I,
    ),
    _p("neogov", _H + r"(?:www\.)?(?:governmentjobs|schooljobs)\.com", False, "NEOGOV", re.I),
    _p(
        "applitrack",
        _H + r"(?:www\.)?applitrack\.com/(?P<token>[A-Za-z0-9_-]+)",
        False,
        "Frontline AppliTrack",
        re.I,
    ),
    _p(
        "schoolspring",
        _H + _SUB + r"schoolspring\.com(?:/[^?#]*\?(?:[^#]*&)?jobid=(?P<id>\d+))?",
        False,
        "SchoolSpring",
        re.I,
    ),
    _p(
        "usajobs",
        _H + r"(?:www\.)?usajobs\.gov" + _PORT + r"(?:/(?:job|GetJob/ViewDetails)/(?P<id>\d+))?",
        False,
        "USAJOBS",
        re.I,
    ),
    _p("paycom", _H + _SUB + r"paycomonline\.net/v4/ats", False, "Paycom", re.I),
    _p("paycor", _H + _SUB + r"recruitingbypaycor\.com", False, "Paycor", re.I),
    _p(
        "dayforce",
        _H + r"jobs\.dayforcehcm\.com/(?:[a-z]{2}-[a-z]{2}/)?(?P<token>[A-Za-z0-9_-]+)",
        False,
        "Dayforce",
        re.I,
    ),
    _p(
        "cornerstone",
        _H + r"(?P<token>[a-z0-9-]+)\.csod\.com/ux/ats",
        False,
        "Cornerstone",
        re.I,
    ),
    _p("avature", _H + r"(?P<token>[a-z0-9-]+)\.avature\.net", False, "Avature", re.I),
    _p("brassring", _H + _SUB + r"brassring\.com", False, "BrassRing", re.I),
    _p(
        "applicantpro",
        _H + r"(?P<token>[a-z0-9-]+)\.applicantpro\.com",
        False,
        "ApplicantPro",
        re.I,
    ),
    _p(
        "zoho_recruit",
        _H + r"(?P<token>[a-z0-9-]+)\.zohorecruit\.(?:com|eu|in)",
        False,
        "Zoho Recruit",
        re.I,
    ),
    _p("teamtailor", _H + r"(?P<token>[a-z0-9-]+)\.teamtailor\.com", False, "Teamtailor", re.I),
    _p(
        "personio",
        _H + r"(?P<token>[a-z0-9-]+)\.jobs\.personio\.(?:de|com)",
        False,
        "Personio",
        re.I,
    ),
    _p("pinpoint", _H + r"(?P<token>[a-z0-9-]+)\.pinpointhq\.com", False, "Pinpoint", re.I),
)

_ALL: tuple[_Pattern, ...] = _ORIGINAL + _RECOGNISED

# every system the resolver can name, as ats id -> display name (the first
# pattern's name when an ats has more than one host family)
KNOWN_SYSTEMS: dict[str, str] = {}
for _pat in _ALL:
    KNOWN_SYSTEMS.setdefault(_pat.ats, _pat.system)
UNSUPPORTED_ATS: frozenset[str] = frozenset(p.ats for p in _ALL if not p.supported)


def _match(patterns: tuple[_Pattern, ...], url: str) -> ResolvedUrl | None:
    for p in patterns:
        m = p.rx.search(url)
        if not m:
            continue
        if p.rx.groupindex:
            named = m.groupdict()
            token, external_id = named.get("token") or "", named.get("id") or ""
        else:
            groups = m.groups()
            token = (groups[0] if groups else "") or ""
            external_id = (groups[1] if len(groups) > 1 else "") or ""
        return ResolvedUrl(
            ats=p.ats,
            token=token,
            external_id=external_id,
            supported=p.supported,
            note="" if p.supported else unsupported_note(p.system),
            system=p.system,
        )
    return None


def resolve(url: str) -> ResolvedUrl | None:
    """The hiring system a URL names, or None. Greenhouse, Lever and Ashby
    come back supported; every other recognised system comes back with
    supported=False and a note naming it."""
    return _match(_ALL, url)


def resolve_for_board_matching(url: str) -> ResolvedUrl | None:
    """`resolve` limited to the systems the board resolver has always skipped
    on (`_ORIGINAL`). A row whose apply link is on a newly recognised system
    gets None here, so the board resolver still name-matches it."""
    return _match(_ORIGINAL, url)
