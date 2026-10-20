"""Lever postings adapter. Shapes verified live 2026-08-18.
Caveat: 200 with [] is indistinguishable from a wrong token — treat as unverified.

Pay: postings with pay transparency carry
salaryRange = {"min": 120000, "max": 155000, "currency": "USD",
"interval": "per-year-salary"} (raw board snapshots: 27 of 459). Only the
per-year-salary interval is read; hourly or monthly ranges stay unpublished."""

from __future__ import annotations

import re
from typing import Any

import httpx

from jobkit.adapters.ats.base import get_json
from jobkit.models import FetchResult, FormSpec, NormalizedJob, RawField

_TOKEN_RE = re.compile(r"jobs\.lever\.co/([a-zA-Z0-9-]+)")


class LeverAdapter:
    ats_name = "lever"

    def board_url(self, token: str) -> str:
        return f"https://api.lever.co/v0/postings/{token}?mode=json"

    async def fetch(self, token: str, etag: str | None) -> FetchResult:
        async with httpx.AsyncClient() as client:
            return await self.fetch_with(client, token, etag)

    async def fetch_with(
        self, client: httpx.AsyncClient, token: str, etag: str | None
    ) -> FetchResult:
        return await get_json(client, self.board_url(token), etag)

    def parse(self, payload: Any) -> list[NormalizedJob]:
        jobs: list[NormalizedJob] = []
        for j in payload:
            cats = j.get("categories") or {}
            location = cats.get("location", "") or ""
            wp = (j.get("workplaceType") or "").lower()
            comp_min, comp_max, currency = _salary_range(j.get("salaryRange"))
            jobs.append(
                NormalizedJob(
                    ats=self.ats_name,
                    board_token="",
                    external_id=str(j["id"]),
                    title=j.get("text", ""),
                    company_name="",
                    location_raw=location,
                    remote=True
                    if wp == "remote"
                    else (False if wp in ("onsite", "hybrid") else None),
                    jd_html=j.get("description", "") or j.get("descriptionPlain", "") or "",
                    apply_url=j.get("applyUrl") or j.get("hostedUrl", ""),
                    posted_at=_ms_to_date(j.get("createdAt")),
                    comp_min=comp_min,
                    comp_max=comp_max,
                    comp_currency=currency,
                    extra={
                        "team": cats.get("team"),
                        "commitment": cats.get("commitment"),
                        "workplaceType": wp or None,
                    },
                )
            )
        return jobs

    def detect_token(self, html: str) -> str | None:
        m = _TOKEN_RE.search(html)
        return m.group(1) if m else None

    def form_spec(self, job: NormalizedJob) -> FormSpec | None:
        return None  # M2b: posting-application-questions endpoint

    def field_identity(self, field: RawField) -> str:
        return field.identity

    def compliance_fields(self, spec: FormSpec) -> list[str]:
        return [f.identity for f in spec.fields if f.compliance_type]


def _salary_range(sr: Any) -> tuple[int | None, int | None, str | None]:
    """Lever salaryRange → (min, max, currency) for an annual range, else Nones."""
    if not isinstance(sr, dict) or sr.get("interval") != "per-year-salary":
        return None, None, None
    lo, hi = _amount(sr.get("min")), _amount(sr.get("max"))
    if lo is None and hi is None:
        return None, None, None
    currency = str(sr["currency"]).upper() if sr.get("currency") else None
    return lo, hi, currency


def _amount(v: Any) -> int | None:
    try:
        n = round(float(v))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _ms_to_date(ms: int | None) -> str | None:
    if not ms:
        return None
    import datetime

    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.UTC).strftime("%Y-%m-%d")
