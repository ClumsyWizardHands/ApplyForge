"""Board resolver review 2026-10-12, round 3 (discovery/board_resolver.py).

(a) A registry board, or one jobkit holds that the registry, discovery or
    add-url added, is only a board under the company's NAME: the registry is
    keyed by normalized name. It passes the same identity check as a probe
    hit (a Greenhouse company name that is the same name, or a domain of the
    company the board links to); without that the job is flagged
    board-match-unconfirmed. Only an owner-curated board (config/targets.yaml)
    and the board the apply URL names are trusted.
(b) The US named as a country with no remote signal ("US", "United States"
    with remote unknown, as Adzuna writes it) no longer agrees with
    "Remote - US": with the same title in a US metro and Remote-US, the
    resolver flags instead of picking the remote posting.

Offline: the MockTransport of tests/test_board_resolver.py, hand-built
payloads for the reviewer's repros."""

from __future__ import annotations

import asyncio
import copy
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from jobkit import db as q
from jobkit.discovery import board_resolver as br
from jobkit.discovery import ingest, registry
from tests.test_board_resolver import (
    CYBERDYNE_ENABLEMENT,
    FILTERS,
    Boards,
    _approve,
    ats_row,
    events,
    feed_row,
    job,
    own_board,
    run,
)

INITRODE_AE = "7311001"
NYC_AE = "7311002"
TYRELLIUM_AE = "7311003"


def gh_board(company: str, token: str, postings: list[tuple[str, str, str]]) -> dict[str, Any]:
    """A hand-built Greenhouse list payload: (id, title, location) postings."""
    return {
        "_recorded": "hand-built for the 2026-10-12 review repros",
        "meta": {"total": len(postings)},
        "jobs": [
            {
                "absolute_url": f"https://job-boards.greenhouse.io/{token}/jobs/{pid}",
                "internal_job_id": int(pid) + 1,
                "location": {"name": loc},
                "id": int(pid),
                "updated_at": "2026-10-02T10:14:02-05:00",
                "requisition_id": "1",
                "title": title,
                "company_name": company,
                "first_published": "2026-10-01T15:29:44-05:00",
                "language": "en",
                "application_deadline": None,
            }
            for pid, title, loc in postings
        ],
    }


INITRODE83 = gh_board(
    "Initrode Genomics",
    "initrode83",
    [(INITRODE_AE, "Enterprise Account Executive", "Remote - US")],
)


@pytest.fixture()
def initrode_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A registry entry under the name: lookup('Initrode') == ('greenhouse', 'initrode83')."""
    hits = {"initrode": ("greenhouse", "initrode83")}
    monkeypatch.setattr(registry, "lookup", lambda name: hits.get(name.strip().lower()))
    monkeypatch.setattr(registry, "_index", lambda: dict(hits))


@pytest.fixture()
def no_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "lookup", lambda name: None)


# ---- (a) registry and discovered boards need evidence ------------------------


def test_a_registry_board_of_another_company_is_unconfirmed_not_merged(
    conn: sqlite3.Connection, initrode_registry: None
) -> None:
    """The reviewer's repro: an approved 'Initrode' row (apply URL on gem.com,
    a shared host) and the registry's initrode83, whose Greenhouse name is
    'Initrode Genomics'. Before: merged into initrode83's posting, the approval
    moved to 'Initrode Genomics'. Now: flagged, nothing merged or added."""
    fid = feed_row(
        conn,
        "Initrode",
        "Enterprise Account Executive",
        "United States",
        remote=True,
        status="queued",
        apply_url="https://jobs.gem.com/initrodehealth/am9icG9zdDox",
    )
    _approve(conn, fid)
    boards = Boards(boards={("greenhouse", "initrode83"): INITRODE83})
    rep = run(conn, boards)
    assert rep.merged == 0 and rep.ambiguous == 1, rep.render()
    assert boards.tokens() == [("greenhouse", "initrode83")]  # read, never polled
    feed = job(conn, fid)
    assert feed["closed_at"] is None and feed["status"] == "approved"
    assert conn.execute("SELECT job_id FROM applications").fetchone()["job_id"] == fid
    assert conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0] == 0
    assert ats_row(conn, "greenhouse", "initrode83", INITRODE_AE) is None
    (flag,) = br.flags(conn)
    assert flag.job_id == fid and flag.key == br.UNCONFIRMED_KEY
    assert "the board registry names it" in flag.why
    assert "'Initrode Genomics'" in flag.why
    assert f"[{br.UNCONFIRMED_KEY}]" in flag.line()
    assert br.UNCONFIRMED_KEY in rep.render()


def test_a_registry_greenhouse_board_whose_name_does_not_fit_is_dropped(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'Tyrell' -> greenhouse/tyrell in the registry, but the board calls itself
    'Tyrellium Corp': dropped like a probe hit. Before: merged (rep.merged 1)."""
    monkeypatch.setattr(
        registry, "lookup", lambda name: ("greenhouse", "tyrell") if name == "Tyrell" else None
    )
    other = gh_board(
        "Tyrellium Corp", "tyrell", [(TYRELLIUM_AE, "Account Executive", "Remote - US")]
    )
    fid = feed_row(conn, "Tyrell", "Account Executive", "United States", remote=True)
    rep = run(conn, Boards(boards={("greenhouse", "tyrell"): other}))
    assert rep.merged == 0, rep.render()
    assert job(conn, fid)["closed_at"] is None
    assert ats_row(conn, "greenhouse", "tyrell", TYRELLIUM_AE) is None
    assert conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0] == 0


def test_a_board_the_discovery_stage_added_is_checked_like_a_probe_hit(
    conn: sqlite3.Connection, initrode_registry: None
) -> None:
    """The discovery stage (registry.resolve_discovery_queue) adds initrode83
    for 'Initrode' as a boards row. On the next run it comes back through the
    company's boards, verified or not: it is read from its list API and
    still needs evidence."""
    fid = feed_row(conn, "Initrode", "Enterprise Account Executive", "United States", remote=True)
    stats = registry.resolve_discovery_queue(conn)
    assert stats["found"] == 1
    conn.execute("UPDATE boards SET verified_at = date('now'), last_polled_at = datetime('now')")
    conn.commit()
    boards = Boards(boards={("greenhouse", "initrode83"): INITRODE83})
    rep = run(conn, boards)
    assert rep.merged == 0 and rep.ambiguous == 1 and rep.probed == 0, rep.render()
    assert boards.tokens() == [("greenhouse", "initrode83")]
    (flag,) = br.flags(conn)
    assert flag.job_id == fid and flag.key == br.UNCONFIRMED_KEY
    assert "not in config/targets.yaml" in flag.why
    assert job(conn, fid)["closed_at"] is None


def test_a_discovered_board_with_the_same_greenhouse_name_still_merges(
    conn: sqlite3.Connection, no_registry: None
) -> None:
    boards = Boards()
    fid = feed_row(
        conn,
        "Tyrell",
        "Sales Enablement Manager, Mid-Market & Enterprise Sales",
        "United States",
        remote=True,
    )
    asyncio.run(ingest.poll_board(conn, "greenhouse", "tyrell", "Tyrell", boards.transport))
    rep = run(conn, boards)
    assert rep.merged == 1, rep.render()
    (ev,) = events(conn, q.BOARD_RESOLVED_EVENT)
    assert ev["job_id"] == fid and ev["board"] == "discovered"
    assert "Greenhouse names the company 'Tyrell'" in ev["evidence"]


def test_an_owner_curated_board_is_trusted_without_evidence(conn: sqlite3.Connection) -> None:
    """cyberdyne-careers in config/targets.yaml: Ashby reports no company
    name and the row knows no domain, but the user listed the board."""
    own_board("Cyberdyne", "ashby", "cyberdyne-careers")
    boards = Boards()
    fid = feed_row(conn, "Cyberdyne", "Director of Sales Enablement", "Boston, Massachusetts")
    asyncio.run(
        ingest.poll_board(conn, "ashby", "cyberdyne-careers", "Cyberdyne", boards.transport)
    )
    rep = run(conn, boards)
    assert rep.merged == 1 and rep.ambiguous == 0, rep.render()
    target = ats_row(conn, "ashby", "cyberdyne-careers", CYBERDYNE_ENABLEMENT)
    assert target is not None and rep.merged_into == {fid: int(target["id"])}
    (ev,) = events(conn, q.BOARD_RESOLVED_EVENT)
    assert ev["board"] == "known"


def test_unreadable_targets_trust_no_board(jobkit_home: Path) -> None:
    (jobkit_home / "config" / "targets.yaml").write_text("companies: [")
    assert br.owner_boards() == frozenset()
    (jobkit_home / "config" / "targets.yaml").write_text(
        yaml.safe_dump(
            {"companies": [{"name": "Acme", "board": {"ats": "ashby", "token": "Acme-Co"}}]}
        )
    )
    assert br.owner_boards() == frozenset({("ashby", "acme-co")})


def test_a_domain_written_in_prose_is_not_evidence(
    conn: sqlite3.Connection, no_registry: None
) -> None:
    """Review 2026-10-12: a company named as a domain ('Hyperion.io') was
    confirmed by an unrelated Ashby board 'hyperion' whose JD names that
    domain in prose."""
    other = {
        "_recorded": "hand-built: an unrelated 'Hyperion Health' board under the token 'hyperion'",
        "apiVersion": "1",
        "jobs": [
            {
                "id": "1f2e3d4c-0000-4000-8000-000000000001",
                "title": "Account Executive",
                "location": "New York",
                "secondaryLocations": [],
                "isListed": True,
                "isRemote": False,
                "jobUrl": "https://jobs.ashbyhq.com/hyperion/1f2e3d4c-0000-4000-8000-000000000001",
                "applyUrl": (
                    "https://jobs.ashbyhq.com/hyperion/1f2e3d4c-0000-4000-8000-000000000001/"
                    "application"
                ),
                "descriptionHtml": "<p>Hyperion Health sells care plans. You will live in "
                "Salesforce and Hyperion.io every day.</p>",
                "descriptionPlain": "Hyperion Health sells care plans. You will live in "
                "Salesforce and Hyperion.io every day.",
            }
        ],
    }
    fid = feed_row(conn, "Hyperion.io", "Account Executive", "New York, NY")
    rep = run(conn, Boards(boards={("ashby", "hyperion"): other}))
    assert rep.merged == 0 and rep.ambiguous == 1, rep.render()
    (flag,) = br.flags(conn)
    assert flag.job_id == fid and flag.key == br.UNCONFIRMED_KEY
    assert br.domains_in(other) == frozenset({"ashbyhq.com"})
    # a link or an e-mail address still counts
    assert br.domains_in({"d": "Apply at https://www.hyperion.io/careers or jobs@hyperion.io"}) == {
        "hyperion.io"
    }


# ---- (b) a country-only US location ------------------------------------------


def _gh(pid: str, title: str, location: str, remote: bool | None = None) -> br.Posting:
    return br.Posting("greenhouse", "acme", pid, title, location, remote, f"https://x/{pid}")


NYC_AND_REMOTE = [
    _gh(NYC_AE, "Enterprise Account Executive", "New York, NY", False),
    _gh(INITRODE_AE, "Enterprise Account Executive", "Remote - US", True),
]


def test_a_bare_us_row_with_a_metro_and_a_remote_us_posting_is_flagged() -> None:
    """The reviewer's repro: decide(..., 'US', None, [NYC, Remote-US])
    returned 'matched' with the Remote-US posting."""
    d = br.decide("Enterprise Account Executive", "US", None, NYC_AND_REMOTE)
    assert d.outcome == "ambiguous" and d.chosen is None and d.agreeing == 0
    assert len(d.candidates) == 2


@pytest.mark.parametrize(
    ("location", "remote", "chosen"),
    [
        ("US", True, INITRODE_AE),  # remote flag: US-wide, the remote posting
        ("United States (Remote)", None, INITRODE_AE),  # a remote word
        ("New York, NY", None, NYC_AE),  # the metro
        ("US", None, None),  # the US as a country, nothing else said: flagged
        ("United States", False, None),
    ],
)
def test_a_country_only_us_location_needs_a_remote_signal(
    location: str, remote: bool | None, chosen: str | None
) -> None:
    d = br.decide("Enterprise Account Executive", location, remote, NYC_AND_REMOTE)
    assert (d.chosen.external_id if d.chosen else None) == chosen, d


def test_a_bare_us_row_never_takes_a_lone_remote_us_posting() -> None:
    d = br.decide("Enterprise Account Executive", "US", None, NYC_AND_REMOTE[1:])
    assert d.outcome == "ambiguous" and d.chosen is None
    # ... but agrees with a posting that is the US as a country too
    same = [_gh("9", "Enterprise Account Executive", "United States")]
    assert br.decide("Enterprise Account Executive", "US", None, same).outcome == "matched"


@pytest.mark.parametrize(
    ("location", "remote"),
    [
        ("", True),  # no location: never agrees, whatever the flag
        ("Hybrid", True),  # a work mode alone names no place
        ("Remote - Americas", None),  # wider than the US
        ("North America", None),
        ("AMER", True),
    ],
)
def test_a_location_that_names_no_us_place_is_flagged(location: str, remote: bool | None) -> None:
    postings = [
        _gh("1", "AE", "Remote - US", True),
        _gh("2", "AE", "Remote - EMEA", True),
    ]
    d = br.decide("AE", location, remote, postings)
    assert d.outcome == "ambiguous" and d.chosen is None, (
        location,
        br.place_keys(location, remote),
    )


def test_end_to_end_a_bare_us_feed_row_is_flagged_not_merged(
    conn: sqlite3.Connection, no_registry: None
) -> None:
    """Adzuna writes 'US' with remote NULL when it has no city: the job is
    flagged with both postings as candidates, never merged into either."""
    own_board("Acme", "greenhouse", "acme")
    payload = gh_board(
        "Acme",
        "acme",
        [
            (NYC_AE, "Enterprise Account Executive", "New York, NY"),
            (INITRODE_AE, "Enterprise Account Executive", "Remote - US"),
        ],
    )
    boards = Boards(boards={("greenhouse", "acme"): copy.deepcopy(payload)})
    fid = feed_row(conn, "Acme", "Enterprise Account Executive", "US")
    asyncio.run(ingest.poll_board(conn, "greenhouse", "acme", "Acme", boards.transport))
    rep = run(conn, boards)
    assert rep.merged == 0 and rep.ambiguous == 1, rep.render()
    (flag,) = br.flags(conn)
    assert flag.job_id == fid and flag.key == br.FLAG_KEY
    assert sorted(c["external_id"] for c in flag.candidates) == sorted([NYC_AE, INITRODE_AE])
    assert job(conn, fid)["closed_at"] is None


# ---- (c) the apply-URL board is trusted only by the row whose URL names it ----
#
# Review 2026-10-15: _trusted built the 'url' boards from every item of the
# company group, and _guarded / _evidence trusted origin 'url' for all of
# them. Row A's apply URL names greenhouse/lumonhealth; row B ('Lumon', no URL
# evidence) was merged into that board's posting although the board calls
# itself 'Lumon Health Inc'. Row B must now pass the same checks as a board
# found by name: flagged board-match-unconfirmed, never merged.

LUMON_AE = "7311004"
LUMON_PD = "7311005"
LUMON_HEALTH = gh_board(
    "Lumon Health Inc", "lumonhealth", [(LUMON_AE, "Account Executive", "New York, NY")]
)


def test_another_rows_apply_url_board_is_not_trusted_for_this_row(
    conn: sqlite3.Connection, no_registry: None
) -> None:
    a = feed_row(
        conn,
        "Lumon",
        "Product Designer",
        "Austin, TX",
        apply_url="https://boards.greenhouse.io/lumonhealth",
    )
    b = feed_row(conn, "Lumon", "Account Executive", "New York, NY")
    boards = Boards(boards={("greenhouse", "lumonhealth"): copy.deepcopy(LUMON_HEALTH)})
    rep = run(conn, boards)
    assert rep.merged == 0 and rep.merged_into == {}, rep.render()
    assert job(conn, b)["closed_at"] is None and job(conn, a)["closed_at"] is None
    assert ats_row(conn, "greenhouse", "lumonhealth", LUMON_AE) is None
    (flag,) = br.flags(conn)
    assert flag.job_id == b and flag.key == br.UNCONFIRMED_KEY
    assert "'Lumon Health Inc'" in flag.why
    assert "another row's apply URL names it" in flag.why


def test_the_row_whose_apply_url_names_the_board_still_trusts_it(
    conn: sqlite3.Connection, no_registry: None
) -> None:
    """Row A's own apply URL names the board: trusted without evidence, as
    before, though the board's Greenhouse name is not the row's."""
    payload = gh_board(
        "Lumon Health Inc",
        "lumonhealth",
        [
            (LUMON_AE, "Account Executive", "New York, NY"),
            (LUMON_PD, "Product Designer", "Austin, TX"),
        ],
    )
    a = feed_row(
        conn,
        "Lumon",
        "Product Designer",
        "Austin, TX",
        apply_url="https://boards.greenhouse.io/lumonhealth",
    )
    b = feed_row(conn, "Lumon", "Account Executive", "New York, NY")
    rep = run(conn, Boards(boards={("greenhouse", "lumonhealth"): payload}))
    target = ats_row(conn, "greenhouse", "lumonhealth", LUMON_PD)
    assert target is not None and rep.merged_into == {a: int(target["id"])}, rep.render()
    (ev,) = events(conn, q.BOARD_RESOLVED_EVENT)
    assert ev["job_id"] == a and ev["board"] == "url"
    (flag,) = br.flags(conn)
    assert flag.job_id == b and flag.key == br.UNCONFIRMED_KEY
    assert job(conn, b)["closed_at"] is None


def test_another_rows_apply_url_board_with_the_same_name_merges_on_evidence(
    conn: sqlite3.Connection, no_registry: None
) -> None:
    """Row B is not trusted by row A's URL, but a Greenhouse board that
    reports the same company name is evidence, as for any board found by
    name."""
    payload = gh_board("Lumon", "lumonhealth", [(LUMON_AE, "Account Executive", "New York, NY")])
    feed_row(
        conn,
        "Lumon",
        "Product Designer",
        "Austin, TX",
        apply_url="https://boards.greenhouse.io/lumonhealth",
    )
    b = feed_row(conn, "Lumon", "Account Executive", "New York, NY")
    rep = run(conn, Boards(boards={("greenhouse", "lumonhealth"): payload}))
    target = ats_row(conn, "greenhouse", "lumonhealth", LUMON_AE)
    assert target is not None and rep.merged_into == {b: int(target["id"])}, rep.render()
    (ev,) = events(conn, q.BOARD_RESOLVED_EVENT)
    assert ev["job_id"] == b and ev["board"] == "url"
    assert "Greenhouse names the company 'Lumon'" in ev["evidence"]


def test_another_rows_apply_url_board_whose_name_does_not_fit_is_not_used(
    conn: sqlite3.Connection, no_registry: None
) -> None:
    """A board whose own name does not fit row B's company at all is
    dropped for row B, as a probe hit would be: no candidate, no merge."""
    payload = gh_board("Zeta Corp", "zeta", [(LUMON_AE, "Account Executive", "New York, NY")])
    feed_row(
        conn,
        "Lumon",
        "Product Designer",
        "Austin, TX",
        apply_url="https://boards.greenhouse.io/zeta",
    )
    b = feed_row(conn, "Lumon", "Account Executive", "New York, NY")
    rep = run(conn, Boards(boards={("greenhouse", "zeta"): payload}))
    assert rep.merged == 0 and rep.ambiguous == 0, rep.render()
    assert br.flags(conn) == []
    assert job(conn, b)["closed_at"] is None
    assert ats_row(conn, "greenhouse", "zeta", LUMON_AE) is None


def test_a_board_read_for_one_origin_is_not_trusted_under_another(
    conn: sqlite3.Connection,
) -> None:
    """fetch() reads a board once per run. The cached board must not carry
    the origin of its first read ('url', trusted) to a later read under the
    company's name only ('probe')."""
    rep = br.Report()
    with br.make_client(
        Boards(boards={("greenhouse", "lumonhealth"): LUMON_HEALTH}).transport
    ) as c:
        r = br._Run(conn, FILTERS.board_resolution, rep, c, lambda s: None, None, dry_run=True)
        first = r.fetch("greenhouse", "lumonhealth", "url")
        again = r.fetch("greenhouse", "lumonhealth", "probe")
    assert first is not None and again is not None
    assert first.origin == "url" and again.origin == "probe"
    assert again.postings == first.postings and rep.requests == 1
