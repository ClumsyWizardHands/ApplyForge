"""Stage 2 — deterministic filters. Kill 90% of volume for free before spending
a token. Every rejection logged to `rejections` with layer=filter and a reason.

Location: remote-intl is rejected whatever the `remote` flag says. Postings
whose location is only country-wide US ("United States", "USA - West"), blank,
or "Distributed" are not rejected here: they pass to the LLM remote check
(triage stage 4), which reads the JD for the real policy and geo restriction.
A US city or state outside the accepted places (filters.locations.allowed and
remote_policy.relocation_cities in filters.yaml) is still a location rejection.

Paused tracks: a title whose first matching role track is set `active: false`
in filters.yaml is rejected as `track_paused:<id>`, after the title
include/exclude checks and before location and comp. No exception: a later
track or a non-track include pattern never releases it (owner decision
2026-10-10, config.Filters.paused_track_for). Re-enabling the track and
running `refilter` puts those jobs back through this stage.

Comp: the floor depends on the title's role track (config.Filters.comp_floor_for);
OTE-basis tracks accept a base (or unlabelled) figure at floor × ote_base_share,
and compare a posted OTE figure with the full floor.

`refilter` puts filter-layer rejections back through this stage after the
config changes (a retarget), because run_filters only ever reads status=new."""

from __future__ import annotations

import datetime
import json
import re
import sqlite3
from collections import Counter
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

from jobkit import db as q
from jobkit.adapters.ats.greenhouse import comp_basis
from jobkit.autofill.options import US_STATES, states_in
from jobkit.config import Filters, load_filters
from jobkit.log import log
from jobkit.normalize import (
    cluster_key,
    location_bucket,
    mentions_non_us,
    mentions_us,
    mentions_us_country,
    names_no_specific_place,
)

# Location buckets that say nothing about where the work is: blank, "Distributed".
# They go to the LLM remote check instead of being rejected here.
UNKNOWN_LOCATION_BUCKETS = {"other-unknown", "other-distributed"}


def defers_to_remote_check(bucket: str, location_raw: str | None) -> bool:
    """True for a posting the filter cannot place: a blank or "Distributed"
    location, or a country-wide US one ("United States", "US / Canada",
    "USA - West"). The LLM remote check decides those from the JD.

    A location that names a city or state is a place, so a non-metro one stays
    a location rejection: "Tulsa, Oklahoma", "San Mateo, CA United States" and
    "Phoenix, Arizona" are not deferred (review 2026-10-08: deferring every
    location that mentioned a US state sent hundreds of non-commute hybrid
    postings to the LLM and on to the queue). A location that names only non-US
    places is never deferred."""
    if not bucket.startswith("other-"):
        return False
    loc = location_raw or ""
    if not names_no_specific_place(loc):
        return False
    if mentions_us_country(loc):
        return True
    return bucket in UNKNOWN_LOCATION_BUCKETS and not mentions_non_us(loc)


def _wrong_state(location_raw: str, states: frozenset[str] | None) -> str | None:
    """With filters.locations.match_state: why a posting in an allowed town's
    bucket is somewhere else ("other-state" for "Columbus, GA" when "Columbus,
    OH" is allowed, "outside-us" for "Dublin, Ireland"), or None. A location
    that names no state and no foreign place passes, as before."""
    if states is None:
        return None
    named = states_in(location_raw)
    if named:
        return None if named & states else "other-state"
    # the town itself may share a foreign city's name ("Dublin"): look past it
    rest = location_raw.partition(",")[2]
    if mentions_non_us(rest) and not mentions_us(location_raw):
        return "outside-us"
    return None


_WORD = re.compile(r"[A-Za-z]+")


def matched_place_bucket(location_raw: str | None, filters: Filters) -> str | None:
    """With filters.locations.match_state: the bucket of the allowed place a
    location names in a format the bucket does not catch ("Columbus OH",
    "OH-Columbus", "US-OH-Columbus" for "Columbus, OH"): the town's words in
    order plus the state as an UPPERCASE code or its full name. None otherwise."""
    if not filters.match_state or not location_raw:
        return None
    words = _WORD.findall(location_raw)
    lower = [w.lower() for w in words]
    for town, states, bucket in filters.state_places:
        town_words = [w.lower() for w in _WORD.findall(town)]
        n = len(town_words)
        if not n or not any(lower[i : i + n] == town_words for i in range(len(lower) - n + 1)):
            continue
        text = " ".join(lower)
        for code in states:
            if code in words or re.search(rf"\b{US_STATES[code].lower()}\b", text):
                return bucket
    return None


def _location_reason(row: Mapping[str, Any], filters: Filters) -> str | None:
    """The accepted places come from config (Filters.accepted_location_buckets:
    filters.locations.allowed, remote_policy.relocation_cities, accept_remote).
    With filters.locations.match_state, a place written with its state also
    checks the posting's state (_wrong_state, matched_place_bucket)."""
    bucket = str(row["location_bucket"])
    if bucket == "remote-intl":
        return f"location:{bucket}"  # remote=1 does not make a non-US hire eligible
    if row["remote"] == 1 and filters.accept_remote:
        return None
    if bucket in filters.accepted_location_buckets:
        if filters.match_state:
            wrong = _wrong_state(str(row["location_raw"] or ""), filters.bucket_states(bucket))
            if wrong:
                return f"location:{bucket}:{wrong}"
        return None
    if matched_place_bucket(row["location_raw"], filters):
        return None
    if defers_to_remote_check(bucket, row["location_raw"]):
        return None
    return f"location:{bucket}"


def _comp_reason(row: Mapping[str, Any], filters: Filters) -> str | None:
    comp_max = row["comp_max"]
    if comp_max is None:
        return None  # unpublished comp is never rejected here (comp.reject_when_unpublished)
    currency = row["comp_currency"]
    if currency and str(currency).upper() != filters.comp_currency.upper():
        return None  # floors are in comp.currency; no FX conversion
    floor = filters.comp_floor_for(row["title"])
    if not floor.floor:
        return None
    # what the stored figure is: a posted OTE never gets the base-to-OTE allowance
    basis = comp_basis(row.get("jd_html"), row.get("comp_min"), int(comp_max))
    if floor.passes(int(comp_max), basis):
        return None
    posted = ":posted-ote" if basis == "ote" else ""
    effective = floor.effective_floor_for(basis)
    return f"comp_below_floor:{comp_max}<{effective}:{floor.describe()}{posted}"


def _reject_reason(row: Mapping[str, Any], filters: Filters) -> str | None:
    f = filters.raw.get("filters", {})
    title = row["title"]

    for name in f.get("blocklist_companies", []) or []:
        if name.lower() in row["company_name"].lower():
            return f"blocklist_company:{name}"

    for pat in filters.exclude_titles:
        if re.search(pat, title):
            return f"exclude_title:{pat[:40]}"
    if filters.include_titles and not any(re.search(p, title) for p in filters.include_titles):
        return "no_include_title_match"

    paused = filters.paused_track_for(title)
    if paused:
        return f"track_paused:{paused}"

    reason = _location_reason(row, filters)
    if reason:
        return reason

    reason = _comp_reason(row, filters)
    if reason:
        return reason

    if row["posted_at"]:
        try:
            age = (datetime.date.today() - datetime.date.fromisoformat(row["posted_at"])).days
            if age > filters.max_age_days:
                return f"stale:{age}d"
        except ValueError:
            pass
    return None


def reject_reason_now(row: Mapping[str, Any], filters: Filters) -> str | None:
    """The filter verdict on a stored job today: the current filters.yaml,
    and its location bucket recomputed with the current rules (as refilter
    does; nothing is written). For code outside the filter pass that would
    put a rejected job back in the queue (pipeline/rescore.py): evaluation
    order is deterministic filters first, so a job that fails them now —
    stale, blocklisted, a title or comp the config no longer accepts — must
    not reach the queue by another way."""
    current = dict(row)
    remote = None if current.get("remote") is None else bool(current["remote"])
    current["location_bucket"] = location_bucket(current.get("location_raw") or "", remote)
    return _reject_reason(current, filters)


def unsnooze(conn: sqlite3.Connection) -> int:
    """Wake snoozed jobs whose snoozed_until date has passed."""
    cur = conn.execute(
        """UPDATE jobs SET status='queued' WHERE status='snoozed' AND id IN (
             SELECT e.job_id FROM events e
             WHERE e.kind='snoozed_until' AND e.detail <= date('now')
               AND e.id = (SELECT MAX(id) FROM events
                           WHERE job_id = e.job_id AND kind='snoozed_until'))"""
    )
    conn.commit()
    if cur.rowcount:
        log("unsnooze.done", woken=cur.rowcount)
    return cur.rowcount


def run_filters(
    conn: sqlite3.Connection,
    filters: Filters | None = None,
    job_ids: Collection[int] | None = None,
) -> dict[str, int]:
    """Move status=new jobs to 'filtered' (passed) or 'rejected' (logged).
    `job_ids`: only these jobs (pipeline/same_day.py)."""
    filters = filters or load_filters()
    stats = {"examined": 0, "passed": 0, "rejected": 0}
    only, ids = q.only_jobs(job_ids, "jobs")
    rows = conn.execute(
        f"SELECT * FROM jobs WHERE status = 'new' AND closed_at IS NULL{only}", ids
    ).fetchall()
    for row in rows:
        stats["examined"] += 1
        reason = _reject_reason(dict(row), filters)
        if reason:
            conn.execute("UPDATE jobs SET status='rejected' WHERE id=?", (row["id"],))
            conn.execute(
                "INSERT INTO rejections (job_id, layer, reason) VALUES (?, 'filter', ?)",
                (row["id"], reason),
            )
            stats["rejected"] += 1
        else:
            conn.execute("UPDATE jobs SET status='filtered' WHERE id=?", (row["id"],))
            stats["passed"] += 1
    conn.commit()
    log("filter.done", **stats)
    return stats


# ---- refilter (audit 2026-10-08 §2 item 5) ----

# The filter-layer reasons a config change can reverse (db.REFILTER_CANDIDATES
# selects the same set). Blocklist and stale rejections are not reset: neither
# depends on the retargeted config. track_paused is reversed by setting the
# track `active: true` again.
REFILTER_REASON_CLASSES = (
    "no_include_title_match",
    "exclude_title",
    "track_paused",
    "comp_below_floor",
    "location",
)


def _reason_class(reason: str) -> str:
    return reason.split(":", 1)[0]


@dataclass
class RefilterReport:
    dry_run: bool
    candidates: int  # open, rejected jobs whose latest rejection is a resettable filter reason
    by_reason: dict[str, int] = field(default_factory=dict)
    would_pass: int = 0  # of the candidates, how many the current config passes
    rebucketed: int = 0  # candidates whose location bucket changes under the current rules
    unchanged: int = 0  # current config gives the same rejection: left alone
    reset: int = 0  # status rejected -> new
    filter_stats: dict[str, int] | None = None  # the run_filters pass that followed
    paused_tracks: list[str] = field(default_factory=list)  # tracks set active: false

    def render(self) -> str:
        verb = "would" if self.dry_run else "did"
        head = "DRY RUN — nothing changed.\n" if self.dry_run else ""
        lines = [
            f"open jobs rejected by the filter on a title, paused-track, comp or location "
            f"reason: {self.candidates}",
            *(f"  {k}: {v}" for k, v in sorted(self.by_reason.items())),
            f"pass the current config: {self.would_pass}",
            f"location bucket recomputed (new geo rules): {self.rebucketed} ({verb} update)",
            f"same rejection under the current config, left alone: {self.unchanged}",
            f"reset to new: {self.reset} ({verb} reset)",
        ]
        if self.filter_stats is not None:
            s = self.filter_stats
            lines.append(
                f"filter pass: examined={s['examined']} passed={s['passed']} "
                f"rejected={s['rejected']} (passed jobs go to LLM triage on the next run)"
            )
        if self.paused_tracks:
            lines.append(
                f"paused tracks (filters.yaml active: false): {', '.join(self.paused_tracks)} "
                "— their postings stay rejected as track_paused:<id>; set active: true and "
                "refilter again to restore them"
            )
        lines.append(
            "Blocklist and stale rejections, sanity and standing-rule rejections, jobs "
            "with a human or standing-rule decision, and closed jobs are not touched."
        )
        return head + "\n".join(lines)


def refilter(
    conn: sqlite3.Connection,
    filters: Filters | None = None,
    *,
    dry_run: bool = False,
    run_filter_pass: bool = True,
) -> RefilterReport:
    """Put open jobs whose latest rejection is a filter-layer title /
    paused-track / comp / location rejection back to status 'new', recompute
    their location bucket with the current rules, log a `refiltered` event per
    job, then (unless run_filter_pass is False) run the filter pass so they are
    re-evaluated against the current config. A job the current config would
    reject with the same reason is left alone, so a second run changes nothing
    (a job held as track_paused stays held while its track is paused). dry_run
    reports counts and changes nothing."""
    filters = filters or load_filters()
    rows = [
        r
        for r in conn.execute(q.REFILTER_CANDIDATES).fetchall()
        if _reason_class(str(r["reason"])) in REFILTER_REASON_CLASSES
    ]
    report = RefilterReport(
        dry_run=dry_run,
        candidates=len(rows),
        by_reason=dict(Counter(_reason_class(str(r["reason"])) for r in rows)),
        paused_tracks=filters.paused_tracks,
    )
    updates: list[dict[str, Any]] = []
    for r in rows:
        remote = None if r["remote"] is None else bool(r["remote"])
        bucket = location_bucket(r["location_raw"] or "", remote)
        current = dict(r)
        if bucket != r["location_bucket"]:
            report.rebucketed += 1
            current["location_bucket"] = bucket
        verdict = _reject_reason(current, filters)
        if verdict is None:
            report.would_pass += 1
        elif verdict == r["reason"] and bucket == r["location_bucket"]:
            report.unchanged += 1  # resetting would only re-log the same rejection
            continue
        updates.append(
            {
                "job_id": r["id"],
                "location_bucket": bucket,
                "cluster_key": cluster_key(
                    r["company_name"], r["title"], r["location_raw"] or "", remote
                ),
                "detail": json.dumps({"previous_reason": r["reason"]}),
            }
        )
    if dry_run:
        report.reset = len(updates)
        log("refilter.dry_run", candidates=report.candidates, would_pass=report.would_pass)
        return report
    for u in updates:
        cur = conn.execute(q.RESET_REJECTED_TO_NEW, u)
        if cur.rowcount:
            report.reset += 1
            conn.execute(
                q.RECORD_EVENT, {"job_id": u["job_id"], "kind": "refiltered", "detail": u["detail"]}
            )
    conn.commit()
    log(
        "refilter.done",
        candidates=report.candidates,
        reset=report.reset,
        rebucketed=report.rebucketed,
        **{f"reason_{k}": v for k, v in report.by_reason.items()},
    )
    if run_filter_pass:
        report.filter_stats = run_filters(conn, filters)
    return report
