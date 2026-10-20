"""BoardAdapter protocol (SPEC §11) + shared HTTP fetch with ETag support."""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from jobkit.contact import user_agent
from jobkit.models import FetchResult, FormSpec, NormalizedJob, RawField


class BoardAdapter(Protocol):
    ats_name: str

    def board_url(self, token: str) -> str: ...
    async def fetch(self, token: str, etag: str | None) -> FetchResult: ...
    async def fetch_with(
        self, client: httpx.AsyncClient, token: str, etag: str | None
    ) -> FetchResult: ...
    def parse(self, payload: Any) -> list[NormalizedJob]: ...
    def detect_token(self, html: str) -> str | None: ...
    def form_spec(self, job: NormalizedJob) -> FormSpec | None: ...
    def field_identity(self, field: RawField) -> str: ...
    def compliance_fields(self, spec: FormSpec) -> list[str]: ...


async def get_json(
    client: httpx.AsyncClient, url: str, etag: str | None, *, method: str = "GET"
) -> FetchResult:
    """Shared conditional GET. 304 → not_modified; 429/5xx raise for tenacity upstream."""
    headers = {"User-Agent": user_agent(), "Accept": "application/json"}
    if etag:
        headers["If-None-Match"] = etag
    resp = await client.request(method, url, headers=headers, timeout=30.0)
    if resp.status_code == 304:
        return FetchResult(status=304, payload=None, etag=etag, not_modified=True)
    resp.raise_for_status()
    return FetchResult(status=resp.status_code, payload=resp.json(), etag=resp.headers.get("etag"))
