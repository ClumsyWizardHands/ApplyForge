"""Phase 2.5 — seed the target list (ONBOARDING.md). MCP: seed_targets.

Given company names (and optionally a domain or a careers/board URL), find
each company's public Greenhouse, Lever or Ashby board and verify it against
the documented public endpoint (SPEC §2.1, the same adapters the nightly poll
uses), then write config/targets.yaml entries. Strategies, in the order of
SPEC §2.4:

  1. a pasted board URL (discovery.url_resolver);
  2. the bundled registry of known boards (discovery.registry, registries/*.yml);
  3. a slug probe: a few tokens derived from the name and domain, tried on
     each ATS's public board endpoint, one request at a time.

Nothing else is fetched: no careers-page crawling, no gated site (hard rule
11). A company with no board found is reported (with what was tried) and,
unless the caller says otherwise, recorded without a board (kept, not
polled): its tier still applies to postings the feeds find there.

An existing config/targets.yaml is never rewritten: new companies are
appended to it as text, so its comments and order stay; companies already in
it (same name, or same board) are skipped."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from jobkit.adapters import ADAPTERS
from jobkit.config import Targets
from jobkit.discovery import registry
from jobkit.discovery.url_resolver import resolve as resolve_url
from jobkit.normalize import normalize_company
from jobkit.onboarding.files import (
    GENERATED_MARK,
    SetupError,
    dump_yaml,
    load_yaml,
    setup_files,
    today,
    write_text_atomic,
)

PROBE_ATS = ("greenhouse", "lever", "ashby")
PROBE_DELAY_S = 0.3  # between probe requests: polite, never a burst
MAX_COMPANIES = 80


@dataclass(frozen=True)
class CompanyAnswer:
    name: str
    domain: str | None = None
    url: str | None = None  # careers page or board URL the human pasted
    tier: int = 2
    said: str = ""


@dataclass(frozen=True)
class Verdict:
    ok: bool
    detail: str
    maybe: bool = False  # Lever 200 [] : an empty board or a wrong token
    jobs: int | None = None


@dataclass
class Found:
    company: CompanyAnswer
    ats: str | None = None
    token: str | None = None
    how: str = ""
    verdict: Verdict | None = None
    tried: list[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return self.verdict is not None and self.verdict.ok

    @property
    def detail(self) -> str:
        return self.verdict.detail if self.verdict is not None else ""


def parse_companies(items: list[Any]) -> list[CompanyAnswer]:
    """Companies from the interview: a name, or {name, domain?, url?, tier?, said?}."""
    out: list[CompanyAnswer] = []
    errors: list[str] = []
    for i, item in enumerate(items):
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            errors.append(f"[{i}]: each company needs a name")
            continue
        unknown = sorted(set(item) - {"name", "domain", "url", "tier", "said"})
        if unknown:
            errors.append(f"[{i}] {item.get('name')}: unknown field(s) {unknown}")
            continue
        tier = item.get("tier", 2)
        if tier not in (1, 2, 3):
            errors.append(f"[{i}] {item['name']}: tier must be 1, 2 or 3")
            continue
        domain = str(item.get("domain") or "").strip().lower() or None
        if domain:
            domain = re.sub(r"^https?://", "", domain).removeprefix("www.").split("/")[0]
        out.append(
            CompanyAnswer(
                name=" ".join(str(item["name"]).split()),
                domain=domain,
                url=str(item.get("url") or "").strip() or None,
                tier=int(tier),
                said=" ".join(str(item.get("said") or "").split()),
            )
        )
    if errors:
        raise SetupError("companies not understood:\n- " + "\n- ".join(errors))
    if len(out) > MAX_COMPANIES:
        raise SetupError(f"{len(out)} companies: give at most {MAX_COMPANIES} per call")
    return out


def probe_slugs(company: CompanyAnswer) -> list[str]:
    """Board tokens to try, from the domain stem and the name."""
    words = normalize_company(company.name).split()
    out: list[str] = []
    if company.domain:
        out.append(company.domain.split(".")[0])
    if words:
        out += ["".join(words), "-".join(words)]
    return [s for s in dict.fromkeys(out) if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,60}", s)]


def _job_count(ats: str, payload: Any) -> int | None:
    if ats == "lever":
        return len(payload) if isinstance(payload, list) else None
    if isinstance(payload, dict) and isinstance(payload.get("jobs"), list):
        return len(payload["jobs"])
    return None


async def verify_board(client: httpx.AsyncClient, ats: str, token: str) -> Verdict:
    """Fetch the board from its public endpoint. A 200 with a jobs list is a
    board; for Lever, 200 with an empty list cannot be told from a wrong
    token (docs/endpoints_verified.md), so it is a 'maybe'."""
    adapter = ADAPTERS[ats]
    try:
        res = await adapter.fetch_with(client, token, None)
    except httpx.HTTPStatusError as e:
        return Verdict(False, f"HTTP {e.response.status_code}")
    except (httpx.HTTPError, ValueError) as e:
        return Verdict(False, f"no answer ({type(e).__name__})")
    count = _job_count(ats, res.payload)
    if count is None:
        return Verdict(False, "not a job board response")
    if ats == "lever" and count == 0:
        return Verdict(False, "0 postings (empty board or wrong token)", maybe=True, jobs=0)
    return Verdict(True, f"{count} open posting(s)", jobs=count)


async def discover(
    company: CompanyAnswer,
    client: httpx.AsyncClient,
    *,
    probe: bool = True,
    delay: float = PROBE_DELAY_S,
) -> Found:
    found = Found(company=company)
    candidates: list[tuple[str, str, str]] = []
    if company.url:
        hit = resolve_url(company.url)
        if hit and hit.supported and hit.token:
            candidates.append((hit.ats, hit.token, "your URL"))
        elif hit:
            found.tried.append(f"URL is a {hit.ats} board: not polled by jobkit ({hit.note})")
        else:
            found.tried.append("URL names no known ATS board")
    reg = registry.lookup(company.name)
    if reg and reg[0] in ADAPTERS:
        candidates.append((reg[0], reg[1], "registry"))
    if probe:
        candidates += [(ats, s, "probe") for s in probe_slugs(company) for ats in PROBE_ATS]
    maybe: tuple[str, str, str, Verdict] | None = None
    seen: set[tuple[str, str]] = set()
    for ats, token, how in candidates:
        if (ats, token) in seen:
            continue
        seen.add((ats, token))
        if how == "probe" and delay:
            await asyncio.sleep(delay)
        v = await verify_board(client, ats, token)
        found.tried.append(f"{ats}:{token} ({how}) → {v.detail}")
        if v.ok:
            found.ats, found.token, found.how, found.verdict = ats, token, how, v
            return found
        if v.maybe and maybe is None:
            maybe = (ats, token, how, v)
    if maybe is not None:
        found.ats, found.token, found.how, found.verdict = maybe
    return found


async def discover_all(
    companies: list[CompanyAnswer],
    client: httpx.AsyncClient | None = None,
    *,
    probe: bool = True,
    delay: float = PROBE_DELAY_S,
) -> list[Found]:
    if client is None:
        async with httpx.AsyncClient(follow_redirects=True) as own:
            return await discover_all(companies, own, probe=probe, delay=delay)
    return [await discover(c, client, probe=probe, delay=delay) for c in companies]


# ---------------------------------------------------------------------------
# targets.yaml
# ---------------------------------------------------------------------------

TARGETS_HEADER = f"""{GENERATED_MARK} — config/targets.yaml
#
# The companies whose public job boards jobkit polls directly, from your answers
# (data/setup/targets.answers.yaml). Each entry quotes what you said and how
# its board was found. What counts as a hit (titles, places, pay) is in
# config/filters.yaml; aggregator feeds find postings at companies not listed.
#
# tier: 1 = would take the call tomorrow · 2 = strong fit · 3 = opportunistic
# board: the public ATS board (greenhouse | lever | ashby) and its token. A
# company without a board is recorded (its tier applies) but not polled.
# Add companies with seed_targets, or by hand in the same shape.

companies:
"""


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def entry_text(f: Found, indent: str = "  ") -> str:
    """One company entry, its list dash at `indent` (matching the file)."""
    c = f.company
    pad = indent + "  "
    lines: list[str] = [""]
    if c.said:
        said = c.said.replace('"', "'")[:200]
        lines.append(f'{indent}# you said: "{said}"')
    if f.verified and f.ats:
        lines.append(f"{indent}# board: {f.ats} via {f.how}, verified {today()}: {f.detail}")
    elif f.ats:
        lines.append(f"{indent}# board: {f.ats} via {f.how}, NOT verified: {f.detail}")
    else:
        lines.append(f"{indent}# no public Greenhouse/Lever/Ashby board found: kept, not polled")
    lines.append(f"{indent}- name: {_q(c.name)}")
    if c.domain:
        lines.append(f"{pad}domain: {_q(c.domain)}")
    lines.append(f"{pad}tier: {c.tier}")
    if f.ats and f.token:
        lines.append(f"{pad}board: {{ ats: {f.ats}, token: {_q(f.token)} }}")
    return "\n".join(lines)


def _list_indent(text: str) -> str:
    """The indentation of the companies list's dashes in an existing file."""
    after = text.split("\ncompanies:", 1)[-1]
    m = re.search(r"(?m)^([ \t]*)- ", after)
    return m.group(1) if m else "  "


def _existing(path: Path) -> tuple[set[str], set[tuple[str, str]], str | None]:
    """(normalized names, (ats, token) boards, text) of an existing targets file."""
    if not path.is_file():
        return set(), set(), None
    text = path.read_text(encoding="utf-8")
    try:
        data = Targets.model_validate(yaml.safe_load(text) or {})
    except Exception as e:  # noqa: BLE001 — any load error is reported the same way
        raise SetupError(
            f"config/targets.yaml does not load ({' '.join(str(e).split())[:200]}); fix it "
            "first (config/targets.example.yaml documents it)"
        ) from e
    names = {normalize_company(c.name) for c in data.companies}
    boards = {(c.board.ats, c.board.token.lower()) for c in data.companies if c.board}
    return names, boards, text


def write_targets(
    results: list[Found], *, include_not_found: bool = True, root: Path | None = None
) -> tuple[list[Found], list[Found]]:
    """Append the results to config/targets.yaml (creating it when missing).
    Returns (added, skipped as already present)."""
    files = setup_files(root)
    path = files.config_file("targets.yaml")
    names, boards, text = _existing(path)
    if text is not None:
        last_key = [m.group(1) for m in re.finditer(r"(?m)^([A-Za-z_][\w-]*):", text)]
        if not last_key or last_key[-1] != "companies":
            raise SetupError(
                "config/targets.yaml has keys after `companies:`; new entries cannot be "
                "appended safely. Add them by hand (the report lists each board)."
            )
    added: list[Found] = []
    skipped: list[Found] = []
    for f in results:
        key = normalize_company(f.company.name)
        board = (f.ats, (f.token or "").lower()) if f.ats else None
        if key in names or (board and board in boards):
            skipped.append(f)
            continue
        if not f.ats and not include_not_found:
            continue
        names.add(key)
        if board:
            boards.add((board[0], board[1]))
        added.append(f)
    if not added:
        return added, skipped
    indent = _list_indent(text) if text is not None else "  "
    body = "\n".join(entry_text(f, indent) for f in added) + "\n"
    if text is None:
        new_text = TARGETS_HEADER + body
    else:
        new_text = text.rstrip("\n")
        if re.search(r"(?m)^companies:\s*\[\]\s*$", new_text):
            new_text = re.sub(r"(?m)^companies:\s*\[\]\s*$", "companies:", new_text)
        new_text += "\n" + body
    try:
        Targets.model_validate(yaml.safe_load(new_text) or {})
    except Exception as e:  # noqa: BLE001
        raise SetupError(f"the updated targets.yaml would not load: {e}") from e
    write_text_atomic(path, new_text)
    return added, skipped


def _record(files_root: Path | None, companies: list[CompanyAnswer], results: list[Found]) -> None:
    files = setup_files(files_root)
    prev = load_yaml(files.targets_record)
    entries = list((prev or {}).get("companies") or []) if isinstance(prev, dict) else []
    for c, f in zip(companies, results, strict=True):
        entries.append(
            {
                "name": c.name,
                "domain": c.domain,
                "url": c.url,
                "tier": c.tier,
                "said": c.said,
                "board": {"ats": f.ats, "token": f.token, "verified": f.verified}
                if f.ats
                else None,
                "tried": f.tried,
                "on": today(),
            }
        )
    write_text_atomic(
        files.targets_record,
        f"{GENERATED_MARK} — companies you named, and what discovery found.\n"
        + dump_yaml({"companies": entries}),
    )


def seed_targets(
    companies: list[Any],
    *,
    probe: bool = True,
    include_not_found: bool = True,
    client: httpx.AsyncClient | None = None,
    delay: float = PROBE_DELAY_S,
    root: Path | None = None,
) -> str:
    """Discover and verify each company's board, append them to
    config/targets.yaml, and report what was found and what was not."""
    parsed = parse_companies(companies)
    if not parsed:
        raise SetupError("name at least one company")
    results = asyncio.run(discover_all(parsed, client, probe=probe, delay=delay))
    _record(root, parsed, results)
    added, skipped = write_targets(results, include_not_found=include_not_found, root=root)
    lines = []
    verified = [f for f in results if f.verified]
    unverified = [f for f in results if f.ats and not f.verified]
    missing = [f for f in results if not f.ats]
    lines.append(
        f"{len(verified)} board(s) verified, {len(unverified)} unverified, "
        f"{len(missing)} not found; {len(added)} added to config/targets.yaml, "
        f"{len(skipped)} already there"
    )
    for f in verified:
        lines.append(f"  found: {f.company.name} → {f.ats}:{f.token} ({f.how}; {f.detail})")
    for f in unverified:
        lines.append(
            f"  unverified: {f.company.name} → {f.ats}:{f.token} ({f.detail}); "
            "check the careers page, or paste the board URL"
        )
    for f in missing:
        tail = "recorded without a board" if include_not_found else "not added"
        lines.append(
            f"  NOT FOUND: {f.company.name} ({tail}). Tried: "
            + ("; ".join(f.tried) if f.tried else "nothing to try")
            + ". Paste its careers or board URL to retry; Workday and other ATSs are not polled."
        )
    for f in skipped:
        lines.append(f"  already in targets.yaml: {f.company.name}")
    return "\n".join(lines)
