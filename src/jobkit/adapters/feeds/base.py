"""FeedAdapter protocol (SPEC §11). Aggregator rows are never canonical."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from jobkit.contact import user_agent
from jobkit.models import NormalizedJob


@dataclass(frozen=True)
class SearchPlace:
    """One place to search around. `radius` is in the feed's own unit:
    kilometres for Adzuna (`distance`), miles for USAJOBS (`Radius`)."""

    name: str
    radius: int


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
    # places to search around (feeds.<feed>.locations). Empty: the feed sends
    # the same location-free request it always has.
    locations: tuple[SearchPlace, ...] = ()
    # Adzuna's country path segment (feeds.adzuna.country)
    country: str = "us"


def missing_keys(feed: object) -> list[str]:
    """The environment variables a key-gated feed needs and does not have.
    A feed declares them as `key_env`; one without that attribute needs none."""
    names: tuple[str, ...] = getattr(feed, "key_env", ())
    return [n for n in names if not os.environ.get(n)]


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
