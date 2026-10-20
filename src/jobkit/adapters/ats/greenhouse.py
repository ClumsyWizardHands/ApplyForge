"""Greenhouse board adapter. Shapes verified live 2026-08-18 (docs/endpoints_verified.md).
Note: questions[] lives on per-job detail (?questions=true), NOT the list payload.

Pay: the list payload has no structured comp field, but boards that use
Greenhouse pay transparency render it into `content` as
<div class="pay-input"><div class="title">…</div><div class="pay-range">
<span>$120,000</span><span class="divider">&mdash;</span><span>$150,000 USD</span>
</div></div> (verified against raw board snapshots: 2,481 of 5,916
jobs). `pay_range` reads those blocks; anything it cannot read as an annual USD
range is left unpublished (None), never guessed. With a base and an OTE block,
comp is the block of the same kind as the floor the title is judged against
(config.Filters.comp_floor_for(title).basis, owned by filters.yaml): the OTE
block for an OTE-floor track (SDR/BDR, AE), the base block otherwise.
`comp_basis` tells later stages when a stored range is an OTE figure."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any

import httpx

from jobkit.adapters.ats.base import get_json
from jobkit.config import Filters, load_filters
from jobkit.models import FetchResult, FormSpec, NormalizedJob, RawField

_TOKEN_RE = re.compile(
    r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9]+)", re.I
)


class GreenhouseAdapter:
    ats_name = "greenhouse"

    def board_url(self, token: str) -> str:
        return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"

    async def fetch(self, token: str, etag: str | None) -> FetchResult:
        async with httpx.AsyncClient() as client:
            return await self.fetch_with(client, token, etag)

    async def fetch_with(
        self, client: httpx.AsyncClient, token: str, etag: str | None
    ) -> FetchResult:
        return await get_json(client, self.board_url(token), etag)

    def parse(self, payload: Any, filters: Filters | None = None) -> list[NormalizedJob]:
        """`filters` decides which pay block a posting with both a base and an
        OTE block stores (see `choose_pay_range`). It is loaded from filters.yaml only
        when a posting needs it, and at most once per payload."""
        jobs: list[NormalizedJob] = []
        cfg = filters
        for j in payload.get("jobs", []):
            location = (j.get("location") or {}).get("name", "") or ""
            title = j.get("title", "") or ""
            blocks = pay_blocks(j.get("content") or "")
            prefer = "base"
            if _mixed_basis(blocks):
                cfg = cfg or load_filters()
                prefer = cfg.comp_floor_for(title).basis
            comp_min, comp_max, currency = _as_columns(choose_pay_range(blocks, prefer))
            jobs.append(
                NormalizedJob(
                    ats=self.ats_name,
                    board_token=str(payload.get("_token", "")),
                    external_id=str(j["id"]),
                    title=title,
                    company_name=j.get("company_name", ""),
                    location_raw=location,
                    remote=True if "remote" in location.lower() else None,
                    jd_html=j.get("content", "") or "",
                    apply_url=j.get("absolute_url", ""),
                    posted_at=(j.get("first_published") or j.get("updated_at") or "")[:10] or None,
                    comp_min=comp_min,
                    comp_max=comp_max,
                    comp_currency=currency,
                    extra={
                        "internal_job_id": j.get("internal_job_id"),
                        "departments": [d.get("name") for d in j.get("departments", [])],
                    },
                )
            )
        return jobs

    def detect_token(self, html: str) -> str | None:
        m = _TOKEN_RE.search(html)
        return m.group(1) if m else None

    def form_spec(self, job: NormalizedJob) -> FormSpec | None:
        return None  # M2b: per-job ?questions=true fetch

    def field_identity(self, field: RawField) -> str:
        return field.identity  # Greenhouse uses question[] name, e.g. "first_name"

    def compliance_fields(self, spec: FormSpec) -> list[str]:
        return [f.identity for f in spec.fields if f.compliance_type]


# ---- pay transparency ----

_PAY_INPUT_SPLIT = re.compile(r'<div class="pay-input">')
_PAY_TITLE = re.compile(r'<div class="title">(.*?)</div>', re.S)
_PAY_RANGE = re.compile(r'<div class="pay-range">(.*?)</div>', re.S)
_TAG = re.compile(r"<[^>]+>")
_USD_AMOUNT = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)")
_CURRENCY_CODE = re.compile(r"\b([A-Z]{3})\s*$")
# a block titled per month/week/day is not an annual figure, whatever its size
_NOT_ANNUAL = re.compile(r"\b(month|monthly|week|weekly|day|daily)\b", re.I)
# below this an amount is hourly (DoorDash's "$19.37 — $32.50") or a placeholder
# ("$1 — $1"). Magnitude, not the block title, decides: Verkada titles annual
# ranges "Estimated Hourly Pay Range".
_MIN_ANNUAL = 10_000
# The block title says what the range is. OTE is tested first: an OTE title
# often also says "base" ("total on-target earnings (base + commissions)").
_OTE_TITLE = re.compile(
    r"\b(ote|on[\s-]?target|total (target )?(cash|compensation|comp|earnings)"
    r"|target (total )?(cash|compensation|earnings)"
    r"|base\s*(\+|plus|and)\s*(commissions?|variable|bonus))\b",
    re.I,
)
_BASE_TITLE = re.compile(r"\b(base|salary)\b", re.I)
# which block a posting's comp comes from, best first, keyed by the basis of the
# floor the title is judged against. Base floor: base, then a range whose title
# does not say (most are base: "Tier 1 Pay Range"), then OTE. OTE floor: OTE,
# then base, then unlabelled.
_BASIS_PREFERENCE: dict[str, tuple[str, ...]] = {
    "base": ("base", "unknown", "ote"),
    "ote": ("ote", "base", "unknown"),
}


@dataclass(frozen=True)
class PayRange:
    """One annual USD pay-transparency block."""

    lo: int
    hi: int
    basis: str  # "base" | "ote" | "unknown" (the block title does not say)


def _block_basis(title: str) -> str:
    if _OTE_TITLE.search(title):
        return "ote"
    if _BASE_TITLE.search(title):
        return "base"
    return "unknown"


def pay_blocks(content: str) -> list[PayRange]:
    """Every annual USD range in the pay-transparency blocks of a Greenhouse
    `content` field, in document order. Monthly/weekly/daily blocks, hourly
    amounts and non-USD ranges are skipped."""
    text = html.unescape(content or "")
    out: list[PayRange] = []
    for chunk in _PAY_INPUT_SPLIT.split(text)[1:]:
        rng = _PAY_RANGE.search(chunk)
        if not rng:
            continue
        title_m = _PAY_TITLE.search(chunk[: rng.start()])
        title = " ".join(html.unescape(_TAG.sub(" ", title_m.group(1))).split()) if title_m else ""
        if _NOT_ANNUAL.search(title):
            continue
        body = " ".join(html.unescape(_TAG.sub(" ", rng.group(1))).split())
        code = _CURRENCY_CODE.search(body)
        if not code or code.group(1) != "USD":
            continue  # CAD/MXN/TWD also use "$": only an explicit USD range counts
        amounts = [round(float(a.replace(",", ""))) for a in _USD_AMOUNT.findall(body)]
        if not amounts or max(amounts) < _MIN_ANNUAL:
            continue
        out.append(PayRange(lo=min(amounts), hi=max(amounts), basis=_block_basis(title)))
    return out


def _mixed_basis(blocks: list[PayRange]) -> bool:
    """An OTE block next to a base or unlabelled one: the only case where the
    stored range depends on the title's floor."""
    kinds = {b.basis for b in blocks}
    return "ote" in kinds and len(kinds) > 1


def choose_pay_range(blocks: list[PayRange], prefer: str = "base") -> PayRange | None:
    """The one range a posting's comp is taken from, or None.

    `prefer` is the basis of the comp floor the posting's title is judged
    against (config.Filters.comp_floor_for(title).basis). It only matters when
    a posting has an OTE block and a base (or unlabelled) block:

    * base floor (the default, sdr_bdr_manager, sales_exec): the base range
      wins (review 2026-10-08), so a base floor is judged on base and an OTE
      number never passes it: Account Manager, CPG has base max $126K and OTE
      max $168.5K, under a $140K default base floor.
    * OTE floor (sdr_bdr, account_executive): the OTE range wins (review
      2026-10-08, second pass). comp_basis then reads it back as "ote", so the
      filter compares it with the full OTE floor, a comp_min_gte standing rule
      refuses it, and comp_max_lt rules judge an AE on the OTE number
      autoapprove.yaml describes. Stored as base, an AE with base $150K and
      OTE $300K was auto-rejected by an AE rule with a comp_max_lt above $150K.

    Among ranges of the same kind (one per geographic tier) the highest ceiling
    is used, so min and max come from one coherent range: the top tier (the
    tier of the highest-cost metros). A posting with only an OTE range keeps
    it, with basis "ote"."""
    if prefer not in _BASIS_PREFERENCE:
        raise ValueError(f"pay range preference must be one of {sorted(_BASIS_PREFERENCE)}")
    for basis in _BASIS_PREFERENCE[prefer]:
        same = [b for b in blocks if b.basis == basis]
        if same:
            return max(same, key=lambda b: b.hi)
    return None


def pay_range(content: str, prefer: str = "base") -> PayRange | None:
    """`choose_pay_range` over the pay-transparency blocks of `content`."""
    return choose_pay_range(pay_blocks(content), prefer)


def _as_columns(best: PayRange | None) -> tuple[int | None, int | None, str | None]:
    """(min, max, "USD") for a chosen range, or (None, None, None)."""
    if best is None:
        return None, None, None
    return best.lo, best.hi, "USD"


def comp_basis(jd_html: str | None, comp_min: int | None, comp_max: int | None) -> str:
    """Whether a stored comp range is a "base" or an "ote" figure, or "unknown".

    The jobs table has no basis column (the schema is frozen), so the basis is
    read back from the stored JD: the pay-transparency block whose range equals
    comp_min/comp_max says what it is. A range no block produced (Lever
    salaryRange, a feed) and an ambiguous match are "unknown"; callers treat
    "unknown" as they always have (as base)."""
    if comp_max is None or not jd_html or "pay-input" not in jd_html:
        return "unknown"
    bases = {
        b.basis
        for b in pay_blocks(jd_html)
        if b.hi == comp_max and (comp_min is None or b.lo == comp_min)
    }
    if bases == {"ote"}:
        return "ote"
    if bases == {"base"}:
        return "base"
    return "unknown"
