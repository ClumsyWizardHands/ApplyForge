"""Ashby posting-api adapter. Shapes verified live 2026-08-18.
GET works with ?includeCompensation=true; tokens are case-sensitive."""

from __future__ import annotations

import re
from typing import Any

import httpx

from jobkit.adapters.ats.base import get_json
from jobkit.models import FetchResult, FormSpec, NormalizedJob, RawField

_TOKEN_RE = re.compile(r"jobs\.ashbyhq\.com/([a-zA-Z0-9.-]+)")


class AshbyAdapter:
    ats_name = "ashby"

    def board_url(self, token: str) -> str:
        return f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"

    async def fetch(self, token: str, etag: str | None) -> FetchResult:
        async with httpx.AsyncClient() as client:
            return await self.fetch_with(client, token, etag)

    async def fetch_with(
        self, client: httpx.AsyncClient, token: str, etag: str | None
    ) -> FetchResult:
        return await get_json(client, self.board_url(token), etag)

    def parse(self, payload: Any) -> list[NormalizedJob]:
        jobs: list[NormalizedJob] = []
        for j in payload.get("jobs", []):
            if j.get("isListed") is False:
                continue
            comp_min, comp_max, currency = _compensation(j.get("compensation"))
            jobs.append(
                NormalizedJob(
                    ats=self.ats_name,
                    board_token="",
                    external_id=str(j["id"]),
                    title=j.get("title", ""),
                    company_name="",
                    location_raw=j.get("location", "") or "",
                    remote=bool(j.get("isRemote")) if j.get("isRemote") is not None else None,
                    jd_html=j.get("descriptionHtml", "") or j.get("descriptionPlain", "") or "",
                    apply_url=j.get("applyUrl") or j.get("jobUrl", ""),
                    posted_at=(j.get("publishedAt") or "")[:10] or None,
                    comp_min=comp_min,
                    comp_max=comp_max,
                    comp_currency=currency,
                    extra={
                        "department": j.get("department"),
                        "team": j.get("team"),
                        "employmentType": j.get("employmentType"),
                        "workplaceType": j.get("workplaceType"),
                    },
                )
            )
        return jobs

    def detect_token(self, html: str) -> str | None:
        m = _TOKEN_RE.search(html)
        return m.group(1) if m else None

    def form_spec(self, job: NormalizedJob) -> FormSpec | None:
        return None  # M2b: application form definition; fields identified by `path`

    def field_identity(self, field: RawField) -> str:
        return field.identity  # Ashby identifies fields by their `path` property

    def compliance_fields(self, spec: FormSpec) -> list[str]:
        return [f.identity for f in spec.fields if f.compliance_type]


_SHORT_PERIOD = re.compile(r"HOUR|DAY|WEEK|MONTH|QUARTER", re.I)


def _is_annual(interval: Any) -> bool:
    """Ashby's `interval` is '1 YEAR', '1 HOUR', '1 MONTH' and so on. Only a
    period shorter than a year disqualifies a range; a missing or other value
    keeps the earlier reading as annual."""
    return not (isinstance(interval, str) and _SHORT_PERIOD.search(interval))


def _compensation(comp: Any) -> tuple[int | None, int | None, str | None]:
    """Ashby compensation blob → (min, max, currency) for an annual salary if
    present. An hourly, monthly or other non-annual range is left unpublished
    (None), as the Greenhouse and Lever adapters do: the pay floors are yearly,
    so $38-52 an hour must not be read as $52 a year."""
    if not isinstance(comp, dict):
        return None, None, None
    for summary in comp.get("summaryComponents") or []:
        if summary.get("compensationType") != "Salary":
            continue
        if not _is_annual(summary.get("interval")):
            continue
        lo, hi = summary.get("minValue"), summary.get("maxValue")
        return (int(lo) if lo else None, int(hi) if hi else None, summary.get("currencyCode"))
    return None, None, None
