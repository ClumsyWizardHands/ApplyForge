"""FeedAdapter protocol (SPEC §11). Aggregator rows are never canonical."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import httpx

from jobkit.contact import user_agent
from jobkit.models import NormalizedJob


@dataclass(frozen=True)
class SearchSpec:
    keywords: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    remote_only: bool = False
    max_days_old: int = 21
    # lowest comp_max the filter accepts (Filters.lowest_comp_floor); feeds
    # with a source-side salary filter use it so they never drop what the
    # filter would keep. None = no source-side salary filter.
    salary_min: int | None = None
    extra: dict[str, str] = field(default_factory=dict)


class FeedAdapter(Protocol):
    source_name: str
    requires_key: bool

    async def search(self, client: httpx.AsyncClient, query: SearchSpec) -> list[NormalizedJob]: ...


async def feed_get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    resp = await client.get(
        url, headers={"User-Agent": user_agent(), "Accept": "application/json"}, timeout=30.0
    )
    resp.raise_for_status()
    return resp
