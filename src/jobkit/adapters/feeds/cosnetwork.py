"""chiefofstaff.network — official Webflow RSS feed (robots-allowed, verified
2026-09-02). Highest chief-of-staff density anywhere. Detail pages carry the
external apply link; fetched lazily for the handful that pass filters."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import httpx

from jobkit.adapters.feeds.base import SearchSpec
from jobkit.contact import user_agent
from jobkit.models import NormalizedJob

_TITLE_SPLIT = re.compile(r"\s+(?:at|@|\||—|–)\s+", re.I)


class CosNetworkFeed:
    source_name = "cosnetwork"
    requires_key = False

    async def search(self, client: httpx.AsyncClient, query: SearchSpec) -> list[NormalizedJob]:
        r = await client.get(
            "https://www.chiefofstaff.network/jobs/rss.xml",
            headers={"User-Agent": user_agent()},
            timeout=30.0,
        )
        r.raise_for_status()
        root = ET.fromstring(r.text)
        jobs: list[NormalizedJob] = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            desc = (item.findtext("description") or "").strip()
            if not title or not link:
                continue
            # titles often read "Chief of Staff at Acme" — split company out
            parts = _TITLE_SPLIT.split(title, maxsplit=1)
            job_title = parts[0].strip()
            company = parts[1].strip() if len(parts) > 1 else "via chiefofstaff.network"
            jobs.append(
                NormalizedJob(
                    ats=self.source_name,
                    board_token="",
                    external_id=link.rstrip("/").rsplit("/", 1)[-1],
                    title=job_title,
                    company_name=company,
                    location_raw="Remote" if "remote" in (title + desc).lower() else "",
                    remote=True if "remote" in (title + desc).lower() else None,
                    jd_html=desc,
                    apply_url=link,  # board detail page; employer link resolved at review
                    posted_at=_rss_date(pub),
                    extra={"board_page": link},
                )
            )
        return jobs


def _rss_date(pub: str) -> str | None:
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(pub).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return None
