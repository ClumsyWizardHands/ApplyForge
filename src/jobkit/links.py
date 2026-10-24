"""Links shown to the person or stored: tracking parameters removed.

SPEC §16.6: a link jobkit shows or keeps carries no tracking parameter. One
rule for every place that does so (track/alert_search.py for alert leads,
rules.employer_site_note for feed rows with no employer form): drop utm_*,
the click and campaign ids any site adds (gclid, fbclid, gh_src, ...),
LinkedIn's trk, refId, trackingId, lipi and li_fat_id, any value starting
"li-" or naming LinkedIn, and the fragment."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# LinkedIn's tracking parameters, dropped whatever their value
TRACKING_KEYS = frozenset({"trk", "refid", "trackingid", "lipi", "li_fat_id"})
# click and campaign ids any site adds
CLICK_KEYS = frozenset(
    {
        "gclid",
        "dclid",
        "fbclid",
        "msclkid",
        "yclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
        "gh_src",
    }
)
_LINKEDIN_PARAM = re.compile(r"linkedin|lnkd", re.I)


def tracking_param(key: str, value: str) -> bool:
    """True when the query parameter key=value is a tracking parameter."""
    k = key.lower()
    return (
        k.startswith("utm_")
        or k in TRACKING_KEYS
        or k in CLICK_KEYS
        or value.lower().startswith("li-")
        or _LINKEDIN_PARAM.search(f"{key}={value}") is not None
    )


def strip_tracking(url: str) -> str:
    """`url` without tracking parameters or fragment. Text that is not an
    http(s) link comes back stripped of surrounding space, unchanged."""
    text = (url or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return text
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not tracking_param(k, v)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
