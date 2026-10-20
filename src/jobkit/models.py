"""Core data models. NormalizedJob is the contract every adapter must emit."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class NormalizedJob:
    """One job posting, normalized across ATSs. Adapter output contract."""

    ats: str  # greenhouse | lever | ashby | ...
    board_token: str
    external_id: str  # the ATS's own id for this posting
    title: str
    company_name: str
    location_raw: str  # as the ATS reported it
    remote: bool | None  # True/False if the ATS says; None if unknown
    jd_html: str  # full JD content (HTML or plaintext)
    apply_url: str
    posted_at: str | None  # ISO date if the ATS provides one
    comp_min: int | None = None
    comp_max: int | None = None
    comp_currency: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # ATS-specific leftovers


@dataclass(frozen=True)
class FetchResult:
    """Result of one board fetch. not_modified means the ETag matched (304)."""

    status: int
    payload: Any | None
    etag: str | None
    not_modified: bool = False


@dataclass(frozen=True)
class RawField:
    """One application-form field as the ATS describes it (M2b)."""

    identity: str
    label: str
    type: str
    required: bool
    options: tuple[str, ...] = ()
    compliance_type: str | None = None


@dataclass(frozen=True)
class FormSpec:
    """Application form structure for one job (M2b)."""

    source: str  # api_questions | dom_scrape
    fields: tuple[RawField, ...]
