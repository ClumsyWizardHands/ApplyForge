"""Aggregator / LinkedIn jobs -> the employer's own board posting
(discovery/board_resolver.py). Offline: a MockTransport serves the board
payloads in tests/fixtures/board_resolver/ (hand-built in the public APIs'
shapes) and answers every other token with the APIs' 404 bodies.

Three fictional companies cover the three board shapes: Tyrell (Himalayas;
Greenhouse `tyrell`, which reports the company's name), Soylent.ai (Adzuna;
Ashby `soylent`, two "Sales Director" postings: London and New York) and
Cyberdyne (Adzuna; Ashby `cyberdyne-careers`, which a plain `cyberdyne`
probe misses). The 'ember' boards in the collision tests are hand-built
too: two unrelated fictional companies, Ember and Ember Grill, share the
name."""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from jobkit import db as q
from jobkit.config import Filters, parse_board_resolution
from jobkit.contact import user_agent
from jobkit.discovery import board_resolver as br
from jobkit.discovery import ingest, registry
from jobkit.models import NormalizedJob
from jobkit.normalize import normalize_company
from jobkit.pipeline.feeds import ingest_feed_jobs
from tests.conftest import FIXTURES, SpawnRecorder

FIX = FIXTURES / "board_resolver"
NOT_FOUND = json.loads((FIX / "not_found.json").read_text())
SOYLENT_LONDON = "3f1c2a9e-5b7d-4e21-9c84-1a2b3c4d5e01"
SOYLENT_NEW_YORK = "3f1c2a9e-5b7d-4e21-9c84-1a2b3c4d5e03"
CYBERDYNE_ENABLEMENT = "7d2e4b6a-8c1f-4a3e-b5d7-2c4e6f8a0b04"
TYRELL_ENABLEMENT = "5102000309"
TYRELL_TITLE = "Sales Enablement Manager, Mid-Market & Enterprise Sales"
FILTERS = Filters(raw={})  # no title, location or comp rules; board_resolution defaults
GH_PARAGRAPH = (
    "You will own sales enablement for the mid-market and enterprise sales teams, "
    "build onboarding and certification programs, run call reviews and coach reps on "
    "discovery, qualification and negotiation. "
)
GH_CONTENT = f"&lt;p&gt;{GH_PARAGRAPH * 4}&lt;/p&gt;"  # Greenhouse escapes its HTML


def load(name: str) -> Any:
    return json.loads((FIX / name).read_text())


RECORDED: dict[tuple[str, str], Any] = {
    ("greenhouse", "tyrell"): load("greenhouse_tyrell.json"),
    ("ashby", "soylent"): load("ashby_soylent.json"),
    ("ashby", "cyberdyne-careers"): load("ashby_cyberdyne_careers.json"),
}


class Boards:
    """A MockTransport standing in for the Greenhouse, Lever and Ashby APIs."""

    def __init__(
        self,
        boards: dict[tuple[str, str], Any] | None = None,
        status: dict[tuple[str, str], int] | None = None,
    ) -> None:
        self.boards = {**copy.deepcopy(RECORDED), **(boards or {})}
        self.status = status or {}
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, parts = request.url.host, request.url.path.split("/")
        ats = {
            "boards-api.greenhouse.io": "greenhouse",
            "api.lever.co": "lever",
            "api.ashbyhq.com": "ashby",
        }.get(host)
        assert ats is not None, f"unexpected host {host}: only the public board APIs"
        assert "linkedin" not in str(request.url)
        key = (ats, parts[3].lower())
        if key in self.status:
            return httpx.Response(self.status[key], json={})
        payload = self.boards.get(key)
        if payload is None:
            body = NOT_FOUND[ats]
            if isinstance(body, str):
                return httpx.Response(404, text=body)
            return httpx.Response(404, json=body)
        if ats == "greenhouse" and request.url.params.get("content") == "true":
            payload = copy.deepcopy(payload)
            for j in payload["jobs"]:
                j["content"] = GH_CONTENT
        return httpx.Response(200, json=payload)

    def tokens(self) -> list[tuple[str, str]]:
        """(ats, token) of every request, in order."""
        ats_of = {
            "boards-api.greenhouse.io": "greenhouse",
            "api.lever.co": "lever",
            "api.ashbyhq.com": "ashby",
        }
        return [(ats_of[r.url.host], r.url.path.split("/")[3]) for r in self.requests]


def feed_row(
    conn: sqlite3.Connection,
    company: str,
    title: str,
    location: str,
    *,
    source: str = "adzuna",
    status: str = "filtered",
    remote: bool | None = None,
    apply_url: str = "",
    ext: str = "",
) -> int:
    """An aggregator row as pipeline/feeds.py stores it, then set to `status`."""
    ext = ext or f"{company}|{title}|{location}"
    job = NormalizedJob(
        ats=source,
        board_token="",
        external_id=ext,
        title=title,
        company_name=company,
        location_raw=location,
        remote=remote,
        jd_html="<p>An aggregator's snippet of the job description.</p>",
        apply_url=apply_url or f"https://www.adzuna.com/land/ad/{abs(len(ext)) * 7919}",
        posted_at=None,
    )
    ingest_feed_jobs(conn, source, [job], FILTERS)
    row = conn.execute(
        "SELECT id FROM jobs WHERE board_id IS NULL AND external_id = ?", (ext,)
    ).fetchone()
    conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, row["id"]))
    conn.commit()
    return int(row["id"])


def run(
    conn: sqlite3.Connection,
    boards: Boards,
    filters: Filters = FILTERS,
    sleeps: list[float] | None = None,
    **kw: Any,
) -> br.Report:
    record = sleeps if sleeps is not None else []
    return br.resolve(
        conn,
        filters,
        transport=boards.transport,
        ats_transport=boards.transport,
        sleep=record.append,
        **kw,
    )


def job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    row: sqlite3.Row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return row


def ats_row(conn: sqlite3.Connection, ats: str, token: str, external_id: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        """SELECT j.* FROM jobs j JOIN boards b ON b.id = j.board_id
           WHERE b.ats = ? AND lower(b.token) = ? AND j.external_id = ?""",
        (ats, token, external_id),
    ).fetchone()
    return row


def events(conn: sqlite3.Connection, kind: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT job_id, detail FROM events WHERE kind = ? ORDER BY id", (kind,))
    return [{"job_id": r["job_id"], **json.loads(r["detail"])} for r in rows]


# registries/*.yml is public board data and names none of the fictional
# companies above: the tests' registry knows them by normalized name, as the
# real one knows a company, and reads the real registry for any other name
FICTIONAL_REGISTRY: dict[str, tuple[str, str]] = {
    "tyrell": ("greenhouse", "tyrell"),
    "soylent": ("ashby", "soylent"),
    "cyberdyne": ("ashby", "cyberdyne-careers"),
}


@pytest.fixture(autouse=True)
def fictional_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    real = registry.lookup
    monkeypatch.setattr(
        registry,
        "lookup",
        lambda name: FICTIONAL_REGISTRY.get(normalize_company(name)) or real(name),
    )


@pytest.fixture()
def no_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A company the board registry does not know: only the slug probe can find it."""
    monkeypatch.setattr(registry, "lookup", lambda name: None)


# ---------------------------------------------------------------------------
# slug variants, title and location
# ---------------------------------------------------------------------------


def test_slug_tiers_name_first_then_suffixes() -> None:
    first, second = br.slug_tiers("Cyberdyne")
    assert first == ["cyberdyne"]
    assert second[:2] == ["cyberdynecareers", "cyberdyne-careers"]
    assert {"cyberdynejobs", "cyberdynehq", "cyberdyneio", "thecyberdyne"} <= set(second)
    assert "cyberdyne-careers" not in br.tokens_for("greenhouse", second)  # no '-' there
    assert "cyberdyne-careers" in br.tokens_for("ashby", second)
    first, second = br.slug_tiers("Soylent.ai")
    assert first == ["soylentai", "soylent", "soylent-ai", "soylent_ai"]
    assert "soylent-careers" in second and "soylentai-careers" not in second
    first, _ = br.slug_tiers("The Ember Company, Inc.", domain="https://www.emberapp.io/careers")
    assert first[:2] == ["ember", "emberapp"]
    assert br.slug_tiers("Acme & Sons LLC")[0][0] == "acmeandsons"


def test_url_resolver_reads_underscored_tokens_the_probe_tries() -> None:
    from jobkit.discovery.url_resolver import resolve

    uid = "0f5f0b2a-1234-4c3d-9e8f-aaaaaaaaaaaa"
    hit = resolve(f"https://jobs.lever.co/acme_corp/{uid}/apply")
    assert hit is not None and (hit.token, hit.external_id) == ("acme_corp", uid)
    hit = resolve(f"https://jobs.ashbyhq.com/acme_corp/{uid}")
    assert hit is not None and (hit.token, hit.external_id) == ("acme_corp", uid)


def test_title_key_is_exact_not_the_normalized_title() -> None:
    assert br.title_key("Sales Director ") == br.title_key("sales  director")
    assert br.title_key("Sales &amp; Marketing") == br.title_key("Sales and Marketing")
    # normalize_title would make both "sales enablement manager"
    assert br.title_key(TYRELL_TITLE) != br.title_key(
        "Sales Enablement Manager, Customer Success & Support"
    )
    assert br.title_key("Account Executive") != br.title_key("Senior Account Executive")


@pytest.mark.parametrize(
    ("a", "b", "agree"),
    [
        ("New York City, New York", "New York", True),
        ("New York City, New York", "London", False),
        ("London", "London", True),
        # review 2026-10-12: the US as a country with no remote signal is not
        # "Remote - US" (it could be a metro office); it agrees only with the same
        ("United States", "Remote - US", False),
        ("US", "Remote - US", False),
        ("United States", "USA", True),
        ("United States, North America", "USA - WEST", True),
        ("Remote - US", "New York, NY", False),  # remote vs a metro
        ("New York/ Remote", "Remote - USA", True),
        ("Round Rock, TX", "Austin (Hybrid); Round Rock, TX (Hybrid)", True),
        ("Chicago, Illinois", "New York", False),
        ("", "New York", False),  # no location: nothing agrees
        ("Canada", "Canada", False),  # a country outside the US names no place
        ("Remote - Worldwide", "Anywhere", True),
    ],
)
def test_locations_agree(a: str, b: str, agree: bool) -> None:
    assert br.locations_agree(a, b) is agree


# review 2026-10-12: a bare "Remote" next to a place outside the US read as
# us-wide on its own ("London / Remote" agreed with "United States")
@pytest.mark.parametrize(
    ("a", "b", "agree"),
    [
        ("United States", "London / Remote", False),
        ("United States", "Remote | EMEA", False),
        ("United States", "Berlin or Remote", False),
        ("Remote - US", "Berlin | Remote", False),
        ("United States", "Toronto or Remote", False),
        ("United States", "Munich / Remote", False),
        ("United States", "Remote - European Union", False),
        ("United States", "Ontario - Remote", False),
        ("United States", "Remote - Tbilisi", False),  # a place jobkit does not know
        ("London", "London / Remote", True),  # the place itself still agrees
        ("United States", "Remote", True),  # bare remote: us-wide, as before
        ("United States", "Remote / Hybrid", True),
        ("United States", "New York / Remote", True),  # names the US
        ("United States", "Remote - US", True),
    ],
)
def test_a_bare_remote_takes_the_scope_of_the_place_beside_it(a: str, b: str, agree: bool) -> None:
    # the "United States" side is a remote row (Himalayas' remote flag): a
    # country-only location with no remote signal is US_COUNTRY (see below)
    assert br.locations_agree(a, b, a_remote=True) is agree, (
        br.place_keys(a, True),
        br.place_keys(b),
    )


# review 2026-10-12: the match gate used normalize.location_bucket's substring
# metro table, a coarse dedup key ('washington' -> DC, 'sf' in "Mansfield")
@pytest.mark.parametrize(
    ("a", "b", "agree"),
    [
        ("Redmond, Washington, United States", "Washington, DC", False),
        ("Cambridge, UK", "Boston, MA", False),
        ("Cambridge, England, United Kingdom", "Cambridge, MA", False),
        ("Mansfield, TX", "San Francisco, CA", False),
        ("London, Ontario, Canada", "London, UK", False),
        ("Arlington, TX", "Washington, DC", False),
        ("Newark, DE", "New York, NY", False),
        ("Boston, Lincolnshire, UK", "Boston, MA", False),
        ("Toronto, Jefferson County", "Toronto", False),  # a US county: Toronto, Ohio
        ("Spokane, Washington", "Washington, DC", False),
        ("Washington", "Washington, DC", False),  # the state, or DC: not said
        ("Cambridge", "Cambridge, MA", False),
        ("Vancouver, BC", "Vancouver, WA", False),
        ("Portland, ME", "Portland, OR", False),
        ("Round Rock", "Round Rock, TX", False),  # one names the state, one does not
        # what must still agree
        ("Washington, D.C.", "Washington, DC", True),
        ("Washington, District of Columbia", "Washington DC", True),
        ("Arlington, VA", "Washington, DC", True),
        ("Cambridge, MA, USA, Middlesex County, MA, USA, Massachusetts, USA", "Boston, MA", True),
        ("London", "London, England, United Kingdom", True),
        ("Toronto", "Toronto, Ontario, Canada", True),
        ("Bellevue, WA", "Seattle", True),
        ("Brooklyn, NY", "New York", True),
        ("Redmond, WA, USA, King County, WA, USA, Washington, USA", "Redmond, Washington", True),
        ("San Francisco, CA, New York City, NY", "New York", True),  # a comma list
        ("SF, NYC, Seattle", "Seattle, WA", True),
        ("US-San Francisco", "San Francisco, CA", True),
        ("Greater Boston Area", "Boston", True),
    ],
)
def test_places_are_whole_words_and_a_named_region_must_fit(a: str, b: str, agree: bool) -> None:
    assert br.locations_agree(a, b) is agree, (br.place_keys(a), br.place_keys(b))


def _p(ext: str, title: str, location: str) -> br.Posting:
    return br.Posting("ashby", "soylent", ext, title, location, None, f"https://x/{ext}")


def test_decide_never_picks_by_title_alone() -> None:
    both = [
        _p(SOYLENT_LONDON, "Sales Director", "London"),
        _p(SOYLENT_NEW_YORK, "Sales Director ", "New York"),
        _p("pe", "Senior Product Engineer, Revenue Engine", "London"),
    ]
    ny = br.decide("Sales Director", "New York City, New York", None, both)
    assert ny.outcome == "matched" and ny.chosen and ny.chosen.external_id == SOYLENT_NEW_YORK
    ldn = br.decide("Sales Director", "London", None, both)
    assert ldn.outcome == "matched" and ldn.chosen and ldn.chosen.external_id == SOYLENT_LONDON
    chi = br.decide("Sales Director", "Chicago, Illinois", None, both)
    assert chi.outcome == "ambiguous" and chi.chosen is None and len(chi.candidates) == 2
    assert "none in a location that agrees" in chi.why("Chicago, Illinois")
    # one posting with the title, in another place: still not picked
    one = br.decide("Sales Director", "Chicago, Illinois", None, both[1:])
    assert one.outcome == "ambiguous" and one.chosen is None
    two = br.decide(
        "Account Executive",
        "New York, NY",
        None,
        [_p("a", "Account Executive", "New York"), _p("b", "Account Executive", "NYC")],
    )
    assert two.outcome == "ambiguous" and two.agreeing == 2
    assert br.decide("Head of Sales", "New York", None, both).outcome == "no_title_match"
    # a posting id the apply URL names wins outright
    exact = br.decide("Sales Director", "Chicago", None, both, exact_id=SOYLENT_LONDON)
    assert exact.outcome == "matched" and exact.exact


def test_names_compatible() -> None:
    assert br.names_compatible("Soylent.ai", "Soylent")
    assert br.names_compatible("Tyrell", "Tyrell")
    assert br.names_compatible("Ember", "Ember Grill")  # title and location still decide
    assert not br.names_compatible("Tyrell", "Tyrellium Corp")  # a prefix, not a word
    assert br.names_compatible("Tyrell", "")  # no name reported: no evidence against


# ---------------------------------------------------------------------------
# the three board shapes
# ---------------------------------------------------------------------------


def test_soylent_nyc_row_resolves_to_the_new_york_posting(conn: sqlite3.Connection) -> None:
    """The registry knows 'Soylent' (the domain label of 'Soylent.ai'). A
    lookup by title alone would pick London; the resolver picks New York."""
    fid = feed_row(conn, "Soylent.ai", "Sales Director", "New York City, New York")
    boards = Boards()
    rep = run(conn, boards)
    assert rep.merged == 1 and rep.ambiguous == 0, rep.render()
    ny = ats_row(conn, "ashby", "soylent", SOYLENT_NEW_YORK)
    assert ny is not None and ny["location_raw"] == "New York"
    assert rep.merged_into == {fid: int(ny["id"])}
    feed = job(conn, fid)
    assert feed["status"] == "closed" and feed["closed_at"] is not None
    src = conn.execute(
        "SELECT source, is_canonical FROM sources WHERE job_id = ?", (ny["id"],)
    ).fetchall()
    assert {(r["source"], r["is_canonical"]) for r in src} == {("ashby", 1), ("adzuna", 0)}
    (ev,) = events(conn, q.BOARD_RESOLVED_EVENT)
    assert ev["job_id"] == fid and ev["external_id"] == SOYLENT_NEW_YORK
    assert ev["board"] == "registry"
    # the London posting came in with the board, but nothing was merged into it
    london = ats_row(conn, "ashby", "soylent", SOYLENT_LONDON)
    assert london is not None and london["status"] == "new"
    dq = conn.execute("SELECT status FROM discovery_queue WHERE normalized_name='soylentai'")
    assert dq.fetchone()["status"] == "found"
    # honest User-Agent on every request, one at a time with the delay between
    assert all(r.headers["user-agent"] == user_agent() for r in boards.requests)


def test_soylent_london_row_resolves_to_the_london_posting(conn: sqlite3.Connection) -> None:
    fid = feed_row(conn, "Soylent.ai", "Sales Director", "London", source="himalayas")
    rep = run(conn, Boards())
    london = ats_row(conn, "ashby", "soylent", SOYLENT_LONDON)
    assert london is not None and rep.merged_into == {fid: int(london["id"])}


def test_soylent_by_slug_probe_stops_after_the_name_tier(
    conn: sqlite3.Connection, no_registry: None
) -> None:
    ny_id = feed_row(conn, "Soylent.ai", "Sales Director", "New York City, New York")
    ldn_id = feed_row(conn, "Soylent.ai", "Sales Director", "London")
    boards = Boards()
    sleeps: list[float] = []
    rep = run(conn, boards, sleeps=sleeps)
    assert rep.merged == 2 and rep.probed == 1, rep.render()
    assert rep.merged_into[ny_id] == ats_row(conn, "ashby", "soylent", SOYLENT_NEW_YORK)["id"]
    assert rep.merged_into[ldn_id] == ats_row(conn, "ashby", "soylent", SOYLENT_LONDON)["id"]
    probes = [t for t in boards.tokens() if t != ("ashby", "soylent")]
    assert ("ashby", "soylent-careers") not in probes  # the suffix tier was not needed
    assert ("greenhouse", "soylent") in probes and ("lever", "soylent-ai") in probes
    board = conn.execute("SELECT confidence FROM boards WHERE token = 'soylent'").fetchone()
    assert board["confidence"] == "probe"
    assert sleeps and all(s == 1.0 for s in sleeps) and len(sleeps) == len(boards.requests) - 1


def test_cyberdyne_found_as_cyberdyne_careers_by_the_probe(
    conn: sqlite3.Connection, no_registry: None
) -> None:
    """The probe finds `cyberdyne-careers`, but Ashby reports no company
    name and the Adzuna row knows no domain: nothing confirms the board is
    Cyberdyne's, so its posting is flagged for the human, not picked, and
    no boards row is added until they pick it."""
    fid = feed_row(conn, "Cyberdyne", "Director of Sales Enablement", "Boston, Massachusetts")
    boards = Boards()
    rep = run(conn, boards)
    assert rep.merged == 0 and rep.ambiguous == 1, rep.render()
    tokens = boards.tokens()
    # the plain name answered 404 on all three ATSs first
    assert tokens[:3] == [
        ("greenhouse", "cyberdyne"),
        ("lever", "cyberdyne"),
        ("ashby", "cyberdyne"),
    ]
    assert ("ashby", "cyberdyne-careers") in tokens
    (probe,) = events(conn, q.BOARD_PROBE_EVENT)
    assert probe["company"] == "cyberdyne"
    assert probe["found"] == [["ashby", "cyberdyne-careers"]]
    assert conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0] == 0
    (flag,) = br.flags(conn)
    assert flag.job_id == fid and "found under the company's name" in flag.why
    assert [c["external_id"] for c in flag.candidates] == [CYBERDYNE_ENABLEMENT]
    # the human picks it
    merged, text = br.pick(conn, fid, CYBERDYNE_ENABLEMENT, ats_transport=boards.transport)
    assert merged, text
    target = ats_row(conn, "ashby", "cyberdyne-careers", CYBERDYNE_ENABLEMENT)
    assert target is not None and job(conn, fid)["closed_at"] is not None
    assert br.flags(conn) == []


# a board whose every posting links the company's own domain, as many do
CYBERDYNE_LINK = (
    "Need an accommodation for your interview? Details: https://go.cyberdyne.com/careers/"
    "interview-accommodations.pdf. We’re glad you’re here."
)


def _cyberdyne_with_link() -> Any:
    payload = load("ashby_cyberdyne_careers.json")
    for j in payload["jobs"]:
        j["descriptionPlain"] += "\n" + CYBERDYNE_LINK
    return payload


def test_a_probe_board_naming_the_companys_domain_is_picked(
    conn: sqlite3.Connection, no_registry: None
) -> None:
    fid = feed_row(conn, "Cyberdyne", "Director of Sales Enablement", "Boston, Massachusetts")
    conn.execute("UPDATE companies SET domain = 'https://www.cyberdyne.com/' WHERE id = 1")
    conn.commit()
    boards = Boards(boards={("ashby", "cyberdyne-careers"): _cyberdyne_with_link()})
    rep = run(conn, boards)
    assert rep.merged == 1 and rep.ambiguous == 0, rep.render()
    target = ats_row(conn, "ashby", "cyberdyne-careers", CYBERDYNE_ENABLEMENT)
    assert target is not None and rep.merged_into == {fid: int(target["id"])}
    (ev,) = events(conn, q.BOARD_RESOLVED_EVENT)
    assert ev["board"] == "probe" and ev["evidence"] == "the board names cyberdyne.com"
    board = conn.execute("SELECT confidence FROM boards").fetchone()
    assert board["confidence"] == "probe"


def test_an_apply_url_on_the_employers_site_confirms_a_probe_board(
    conn: sqlite3.Connection, no_registry: None
) -> None:
    """Himalayas links some jobs to the employer's careers page: its host is
    the company's domain. An aggregator's or ATS's host never is."""
    boards = Boards(boards={("ashby", "cyberdyne-careers"): _cyberdyne_with_link()})
    fid = feed_row(conn, "Cyberdyne", "Director of Sales Enablement", "Boston, Massachusetts")
    rep = run(conn, boards)
    assert rep.merged == 0 and rep.ambiguous == 1, rep.render()  # adzuna.com says nothing
    conn.execute(
        "UPDATE jobs SET apply_url = ? WHERE id = ?",
        ("https://www.cyberdyne.com/careers/job-openings/?role=sales-enablement", fid),
    )
    rep = run(conn, boards)
    target = ats_row(conn, "ashby", "cyberdyne-careers", CYBERDYNE_ENABLEMENT)
    assert target is not None and rep.merged_into == {fid: int(target["id"])}, rep.render()
    assert br.flags(conn) == []  # the resolution clears the earlier flag


def test_a_greenhouse_probe_board_with_the_same_company_name_is_picked(
    conn: sqlite3.Connection, no_registry: None
) -> None:
    fid = feed_row(conn, "Tyrell", TYRELL_TITLE, "United States", source="himalayas", remote=True)
    boards = Boards()
    rep = run(conn, boards)
    target = ats_row(conn, "greenhouse", "tyrell", TYRELL_ENABLEMENT)
    assert target is not None and rep.merged_into == {fid: int(target["id"])}, rep.render()
    (ev,) = events(conn, q.BOARD_RESOLVED_EVENT)
    assert ev["evidence"] == "Greenhouse names the company 'Tyrell'"
    assert boards.tokens()[:1] == [("greenhouse", "tyrell")]


def test_company_evidence_helpers() -> None:
    assert br.registrable_domain("https://careers.ember.ai/jobs?x=1") == "ember.ai"
    assert br.registrable_domain("jobs.acme.co.uk") == "acme.co.uk"
    assert br.registrable_domain("Acme Inc") is None
    assert br.registrable_domain("1.5em") is None
    assert br.company_domains("Soylent.ai", None) == {"soylent.ai"}
    assert br.company_domains("Tyrell", None, "https://www.adzuna.com/land/ad/1") == frozenset()
    assert (
        br.company_domains("Tyrell", None, "https://job-boards.greenhouse.io/tyrell") == frozenset()
    )
    assert br.company_domains(
        "Tyrell", "tyrell.example", "https://www.tyrell.example/careers?gh_jid=1"
    ) == {"tyrell.example"}
    found = br.domains_in(
        {
            "jobs": [
                {
                    "d": "Write to jobs@ember.ai or see &lt;a href=&quot;"
                    "https://www.ember.ai/x&quot;&gt;"
                }
            ]
        }
    )
    assert found == {"ember.ai"}
    assert br.same_company_name("Soylent.ai", "Soylent")
    assert br.same_company_name("Acme", "Acme, Inc.")
    assert not br.same_company_name("Ember", "Ember Grill")
    assert not br.same_company_name("Tyrell", "")


def test_shared_and_unrelated_hosts_are_not_the_companys_domain() -> None:
    """Review 2026-10-12: only a host specific to the employer is evidence.
    Shared hosts never count, whoever's page they serve; an apply URL's
    host counts only when it spells the company name."""
    for url in (
        "https://emberhq.notion.site/Account-Executive-7a21",
        "https://www.notion.so/ember/Jobs-1",
        "https://ember.github.io/careers",
        "https://github.com/ember/jobs",
        "https://docs.google.com/forms/d/e/1FAIpQ/viewform",
        "https://forms.gle/AbC123",
        "https://airtable.com/shrXYZ",
        "https://ember.typeform.com/to/abc",
        "https://tally.so/r/abc",
        "https://jobs.gem.com/ember/123",
        "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=1",
        "https://recruiting.ultipro.com/EMB1000/JobBoard/abc",
        "https://edel.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/1",
        "https://ember.taleo.net/careersection/2/jobdetail.ftl?job=1",
        "https://www.linkedin.com/jobs/view/1",
        "https://emberfleet.com/careers",  # a domain, but not one that spells "Ember"
    ):
        assert br.company_domains("Ember", None, url) == frozenset(), url
    assert br.company_domains("Ember", "ember.notion.site") == frozenset()
    assert br.company_domains("Ember", None, "https://careers.ember.ai/jobs/1") == {"ember.ai"}
    assert br.company_domains("Ember", None, "https://www.getember.com/careers") == {"getember.com"}
    assert br.company_domains("Vandelay AI", None, "https://vandelay.com/careers/1") == {
        "vandelay.com"
    }
    assert br.company_domains("The Trade Desk", None, "https://thetradedesk.com/x") == {
        "thetradedesk.com"
    }
    assert br.domain_fits_name("Soylent.ai", "soylent.ai")
    assert not br.domain_fits_name("Tyrell", "tyrellium.com")
    assert not br.domain_fits_name("Ember", "notion.site")
    assert br.is_shared_host("notion.site") and br.is_shared_host("adzuna.com")
    assert not br.is_shared_host("cyberdyne.com")


def test_cyberdyne_through_the_registry_is_unconfirmed_without_evidence(
    conn: sqlite3.Connection,
) -> None:
    """Review 2026-10-12: a registry board is only a board under the
    company's name (the registry is keyed by normalized name). Ashby reports
    no company name and the Adzuna row knows no domain, so the posting is
    flagged board-match-unconfirmed, not merged, and no probe runs."""
    fid = feed_row(conn, "Cyberdyne", "Director of Sales Enablement", "Boston, Massachusetts")
    boards = Boards()
    rep = run(conn, boards)
    assert rep.merged == 0 and rep.ambiguous == 1 and rep.probed == 0, rep.render()
    assert boards.tokens() == [("ashby", "cyberdyne-careers")]
    assert conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0] == 0
    (flag,) = br.flags(conn)
    assert flag.job_id == fid and flag.key == br.UNCONFIRMED_KEY
    assert "the board registry names it" in flag.why
    assert br.UNCONFIRMED_KEY in flag.line()
    assert job(conn, fid)["closed_at"] is None


def test_cyberdyne_through_the_registry_with_its_domain_is_merged(
    conn: sqlite3.Connection,
) -> None:
    fid = feed_row(conn, "Cyberdyne", "Director of Sales Enablement", "Boston, Massachusetts")
    conn.execute("UPDATE companies SET domain = 'cyberdyne.com' WHERE id = 1")
    conn.commit()
    boards = Boards(boards={("ashby", "cyberdyne-careers"): _cyberdyne_with_link()})
    rep = run(conn, boards)
    assert rep.merged == 1 and rep.probed == 0, rep.render()
    (ev,) = events(conn, q.BOARD_RESOLVED_EVENT)
    assert ev["job_id"] == fid and ev["board"] == "registry"
    assert "cyberdyne.com" in ev["evidence"]


def test_tyrell_from_himalayas(conn: sqlite3.Connection) -> None:
    """'United States' (Himalayas) and 'Remote - US' (Greenhouse) both mean
    US-wide; the sibling posting with the same normalized title is not it."""
    fid = feed_row(
        conn,
        "Tyrell",
        TYRELL_TITLE,
        "United States",
        source="himalayas",
        remote=True,
        apply_url="https://himalayas.app/companies/tyrell/jobs/sales-enablement-manager",
    )
    rep = run(conn, Boards())
    target = ats_row(conn, "greenhouse", "tyrell", TYRELL_ENABLEMENT)
    assert target is not None and rep.merged_into == {fid: int(target["id"])}
    assert target["jd_html"] == GH_CONTENT  # the employer's JD, from the poll


def own_board(company: str, ats: str, token: str) -> None:
    """Add a board to the sandbox's config/targets.yaml: owner-curated."""
    path = Path("config") / "targets.yaml"
    data = yaml.safe_load(path.read_text()) or {}
    data.setdefault("companies", []).append(
        {"name": company, "tier": 2, "board": {"ats": ats, "token": token}}
    )
    path.write_text(yaml.safe_dump(data))


def test_a_board_jobkit_already_polls_needs_no_request(conn: sqlite3.Connection) -> None:
    own_board("Tyrell", "greenhouse", "tyrell")  # owner-curated: trusted, read from the DB
    boards = Boards()
    import asyncio

    # Adzuna's remote "United States" row came first; the board's poll then
    # stored the ATS row beside it. The resolver sees both as US-wide.
    fid = feed_row(conn, "Tyrell", TYRELL_TITLE, "United States", source="adzuna", remote=True)
    polled = asyncio.run(
        ingest.poll_board(conn, "greenhouse", "tyrell", "Tyrell", boards.transport)
    )
    assert polled.ok
    boards.requests.clear()
    rep = run(conn, boards)
    assert boards.requests == []  # polled in the last day: its rows are read from the DB
    target = ats_row(conn, "greenhouse", "tyrell", TYRELL_ENABLEMENT)
    assert target is not None and rep.merged_into == {fid: int(target["id"])}


def test_an_apply_url_naming_the_posting_goes_through_add_url(conn: sqlite3.Connection) -> None:
    fid = feed_row(
        conn,
        "Tyrell",
        TYRELL_TITLE,
        "United States",
        source="himalayas",
        apply_url=f"https://job-boards.greenhouse.io/tyrell/jobs/{TYRELL_ENABLEMENT}",
    )
    boards = Boards()
    rep = run(conn, boards)
    target = ats_row(conn, "greenhouse", "tyrell", TYRELL_ENABLEMENT)
    assert target is not None and rep.merged_into == {fid: int(target["id"])}
    assert boards.tokens() == [("greenhouse", "tyrell")]  # the poll only, no probe


# ---------------------------------------------------------------------------
# ambiguous: flagged, never picked
# ---------------------------------------------------------------------------


def test_ambiguous_is_flagged_not_picked_then_the_human_picks(
    conn: sqlite3.Connection, jobkit_home: Path
) -> None:
    fid = feed_row(conn, "Soylent.ai", "Sales Director", "Chicago, Illinois")
    boards = Boards()
    rep = run(conn, boards)
    assert rep.ambiguous == 1 and rep.merged == 0, rep.render()
    assert job(conn, fid)["closed_at"] is None  # nothing merged
    assert conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0] == 0  # nothing ingested
    (flag,) = br.flags(conn)
    assert flag.job_id == fid
    assert {c["external_id"] for c in flag.candidates} == {SOYLENT_LONDON, SOYLENT_NEW_YORK}
    assert br.FLAG_KEY in "\n".join(br.digest_lines(conn))
    assert SOYLENT_NEW_YORK in "\n".join(br.digest_lines(conn))
    line = br.queue_line(conn)
    assert line is not None and br.FLAG_KEY in line
    # the next run finds the same: one flag event, not two
    run(conn, boards)
    assert len(events(conn, q.BOARD_MATCH_AMBIGUOUS_EVENT)) == 1
    # the human picks New York
    merged, text = br.pick(conn, fid, SOYLENT_NEW_YORK, ats_transport=boards.transport)
    assert merged, text
    ny = ats_row(conn, "ashby", "soylent", SOYLENT_NEW_YORK)
    assert ny is not None and job(conn, fid)["status"] == "closed"
    assert br.flags(conn) == [] and br.queue_line(conn) is None
    assert not br.pick(conn, fid, SOYLENT_LONDON, ats_transport=boards.transport)[0]


def test_two_postings_agreeing_on_location_are_flagged(conn: sqlite3.Connection) -> None:
    payload = load("ashby_soylent.json")
    payload["jobs"][0]["location"] = "New York"  # London -> a second New York posting
    fid = feed_row(conn, "Soylent.ai", "Sales Director", "New York City, New York")
    rep = run(conn, Boards(boards={("ashby", "soylent"): payload}))
    assert rep.ambiguous == 1 and rep.merged == 0
    (flag,) = br.flags(conn)
    assert flag.job_id == fid and "2 postings with this title in a location that agrees" in flag.why


def _soylent(keep: str, location: str) -> Any:
    """The recorded Soylent board with one "Sales Director" posting left, at
    `location` (the aggregator's US row is stale: the employer closed it)."""
    payload = load("ashby_soylent.json")
    drop = SOYLENT_NEW_YORK if keep == SOYLENT_LONDON else SOYLENT_LONDON
    payload["jobs"] = [j for j in payload["jobs"] if j["id"] != drop]
    for j in payload["jobs"]:
        if j["id"] == keep:
            j["location"] = location
    return payload


def test_a_us_row_is_not_merged_into_a_london_or_remote_posting(conn: sqlite3.Connection) -> None:
    """Review 2026-10-12 (picking by title alone): the trusted board's only
    "Sales Director" is "London / Remote". A "United States" row is flagged,
    not merged into it."""
    fid = feed_row(conn, "Soylent.ai", "Sales Director", "United States")
    rep = run(
        conn, Boards(boards={("ashby", "soylent"): _soylent(SOYLENT_LONDON, "London / Remote")})
    )
    assert rep.merged == 0 and rep.ambiguous == 1, rep.render()
    assert job(conn, fid)["closed_at"] is None
    (flag,) = br.flags(conn)
    assert flag.job_id == fid and "none in a location that agrees" in flag.why
    assert [c["external_id"] for c in flag.candidates] == [SOYLENT_LONDON]


def test_a_washington_state_row_is_not_merged_into_a_dc_posting(conn: sqlite3.Connection) -> None:
    """Review 2026-10-12: Adzuna writes "City, State", and "washington" read
    as DC. A Redmond, WA row is flagged with the DC posting as its candidate."""
    board = {("ashby", "soylent"): _soylent(SOYLENT_NEW_YORK, "Washington, DC")}
    fid = feed_row(conn, "Soylent.ai", "Sales Director", "Redmond, Washington, United States")
    rep = run(conn, Boards(boards=board))
    assert rep.merged == 0 and rep.ambiguous == 1, rep.render()
    assert job(conn, fid)["closed_at"] is None
    (flag,) = br.flags(conn)
    assert flag.job_id == fid
    assert [c["external_id"] for c in flag.candidates] == [SOYLENT_NEW_YORK]


def test_a_dc_row_is_still_merged_into_the_dc_posting(conn: sqlite3.Connection) -> None:
    board = {("ashby", "soylent"): _soylent(SOYLENT_NEW_YORK, "Washington, DC")}
    fid = feed_row(conn, "Soylent.ai", "Sales Director", "Washington, District of Columbia")
    rep = run(conn, Boards(boards=board))
    target = ats_row(conn, "ashby", "soylent", SOYLENT_NEW_YORK)
    assert target is not None and rep.merged_into == {fid: int(target["id"])}, rep.render()


def test_a_flag_clears_when_the_board_no_longer_has_the_title(conn: sqlite3.Connection) -> None:
    fid = feed_row(conn, "Soylent.ai", "Sales Director", "Chicago, Illinois")
    run(conn, Boards())
    assert [f.job_id for f in br.flags(conn)] == [fid]
    payload = load("ashby_soylent.json")
    payload["jobs"] = [j for j in payload["jobs"] if "Sales Director" not in j["title"]]
    rep = run(conn, Boards(boards={("ashby", "soylent"): payload}))
    assert rep.no_title_match == 1 and br.flags(conn) == []


# ---------------------------------------------------------------------------
# name collisions
# ---------------------------------------------------------------------------

EMBER = {
    "_recorded": "hand-built: an unrelated company's Greenhouse board under the token 'ember'",
    "jobs": [
        {
            "id": 101,
            "title": "Line Cook",
            "location": {"name": "New York, NY"},
            "company_name": "Ember Grill",
            "absolute_url": "https://job-boards.greenhouse.io/ember/jobs/101",
        },
        {
            "id": 102,
            "title": "Shift Manager",
            "location": {"name": "New York, NY"},
            "company_name": "Ember Grill",
            "absolute_url": "https://job-boards.greenhouse.io/ember/jobs/102",
        },
    ],
    "meta": {"total": 2},
}


def test_an_unrelated_board_under_the_name_is_not_matched(conn: sqlite3.Connection) -> None:
    fid = feed_row(conn, "Ember", "Account Executive", "New York, NY")
    boards = Boards(boards={("greenhouse", "ember"): EMBER})
    rep = run(conn, boards)
    assert rep.merged == 0 and rep.ambiguous == 0 and rep.no_title_match == 1, rep.render()
    assert job(conn, fid)["closed_at"] is None
    assert conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE board_id IS NOT NULL").fetchone()[0] == 0
    # the whole probe ran (no title match on the name tier), and was cached
    assert ("ashby", "ember-careers") in boards.tokens()
    (probe,) = events(conn, q.BOARD_PROBE_EVENT)
    assert probe["found"] == [["greenhouse", "ember"]]


def test_a_board_whose_own_name_does_not_fit_is_ignored(
    conn: sqlite3.Connection, no_registry: None
) -> None:
    other = copy.deepcopy(RECORDED[("greenhouse", "tyrell")])
    for j in other["jobs"]:
        j["company_name"] = "Tyrellium Corp"
    feed_row(conn, "Tyrell", TYRELL_TITLE, "United States", source="himalayas", remote=True)
    rep = run(conn, Boards(boards={("greenhouse", "tyrell"): other}))
    assert rep.merged == 0 and rep.ambiguous == 0, rep.render()


# ---------------------------------------------------------------------------
# the probe cache, pacing and failures
# ---------------------------------------------------------------------------


def test_a_company_is_probed_once_per_cache_window(
    conn: sqlite3.Connection, no_registry: None
) -> None:
    feed_row(conn, "Nobody Ltd", "Account Executive", "New York, NY")
    boards = Boards()
    rep = run(conn, boards)
    assert rep.no_board == 1 and rep.probed == 1
    first = len(boards.requests)
    assert first > 10  # both tiers, all three ATSs
    dq = conn.execute("SELECT status, attempted_at FROM discovery_queue").fetchone()
    assert dq["status"] == "not_found" and dq["attempted_at"] is not None
    feed_row(conn, "Nobody Ltd", "Sales Director", "New York, NY")
    rep = run(conn, boards)
    assert rep.cache_hits == 1 and rep.probed == 0 and len(boards.requests) == first
    conn.execute("UPDATE events SET at = datetime('now', '-15 days') WHERE kind = 'board_probe'")
    rep = run(conn, boards)
    assert rep.probed == 1 and len(boards.requests) == 2 * first


def test_cached_boards_are_read_again_for_new_rows(
    conn: sqlite3.Connection, no_registry: None
) -> None:
    feed_row(conn, "Ember", "Account Executive", "New York, NY")
    boards = Boards(boards={("greenhouse", "ember"): EMBER})
    run(conn, boards)
    n = len(boards.requests)
    fid = feed_row(conn, "Ember", "Line Cook", "Brooklyn, NY")
    rep = run(conn, boards)
    # the cached board only, read once
    assert boards.tokens()[n:] == [("greenhouse", "ember")]
    assert rep.cache_hits == 1 and rep.probed == 0
    # its title and place agree with a posting there, but the board calls
    # itself "Ember Grill", not "Ember": flagged for the human, not picked
    assert rep.merged == 0 and rep.ambiguous == 1, rep.render()
    (flag,) = br.flags(conn)
    assert flag.job_id == fid and "it calls itself 'Ember Grill', not 'Ember'" in flag.why
    assert conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0] == 0


def test_http_429_stops_the_run_and_caches_nothing(
    conn: sqlite3.Connection, no_registry: None
) -> None:
    feed_row(conn, "Cyberdyne", "Director of Sales Enablement", "Boston, Massachusetts")
    boards = Boards(status={("greenhouse", "cyberdyne"): 429})
    rep = run(conn, boards)
    assert rep.status == "stopped" and rep.deferred == 1 and len(boards.requests) == 1
    assert events(conn, q.BOARD_PROBE_EVENT) == []


def test_a_failed_request_leaves_the_company_unprobed(
    conn: sqlite3.Connection, no_registry: None
) -> None:
    feed_row(conn, "Nobody Ltd", "Account Executive", "New York, NY")
    rep = run(conn, Boards(status={("lever", "nobody"): 503}))
    assert rep.errors == 1 and events(conn, q.BOARD_PROBE_EVENT) == []


def test_a_cached_board_that_fails_is_an_error_not_a_crash(
    conn: sqlite3.Connection, no_registry: None
) -> None:
    feed_row(conn, "Ember", "Account Executive", "New York, NY")
    run(conn, Boards(boards={("greenhouse", "ember"): EMBER}))
    feed_row(conn, "Ember", "Sales Director", "New York, NY")
    rep = run(conn, Boards(status={("greenhouse", "ember"): 502}))
    assert rep.errors == 2 and rep.cache_hits == 1  # both open Ember rows: next run


def test_the_request_cap_defers_the_rest(conn: sqlite3.Connection, no_registry: None) -> None:
    filters = Filters(raw={"board_resolution": {"max_requests_per_run": 2}})
    feed_row(conn, "Nobody Ltd", "Account Executive", "New York, NY")
    boards = Boards()
    rep = run(conn, boards, filters=filters)
    assert len(boards.requests) == 2 and rep.status == "stopped" and rep.deferred == 1


def test_no_write_lock_is_held_across_a_request_or_the_delay(
    conn: sqlite3.Connection, no_registry: None
) -> None:
    """Review 2026-10-12: a run kept its writes (probe events, flags, merges)
    uncommitted while it sent requests a second apart, so MCP get_queue,
    approve and a background `tailor` (5 s busy timeout) failed with
    'database is locked'. Every request and every delay now starts with
    nothing uncommitted, and another connection can take the write lock."""
    feed_row(conn, "Nobody Ltd", "Account Executive", "New York, NY")
    feed_row(conn, "Soylent.ai", "Sales Director", "New York City, New York")
    feed_row(conn, "Somebody Else", "Account Executive", "New York, NY")
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    held: list[str] = []

    def check(where: str) -> None:
        if conn.in_transaction:
            held.append(f"{where}: uncommitted writes")
        other = sqlite3.connect(path, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")  # the write lock another writer needs
            other.rollback()
        except sqlite3.OperationalError as e:
            held.append(f"{where}: {e}")
        finally:
            other.close()

    class Watched(Boards):
        def handle(self, request: httpx.Request) -> httpx.Response:
            check(f"GET {request.url}")
            return super().handle(request)

    boards = Watched()
    rep = br.resolve(
        conn,
        FILTERS,
        transport=boards.transport,
        ats_transport=boards.transport,
        sleep=lambda s: check("delay"),
    )
    assert rep.merged == 1 and rep.no_board == 2 and rep.probed == 3, rep.render()
    assert len(boards.requests) > 60  # two whole probes, one name tier, the poll
    assert held == []


def test_dry_run_reads_and_writes_nothing(conn: sqlite3.Connection) -> None:
    fid = feed_row(conn, "Soylent.ai", "Sales Director", "New York City, New York")
    before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    rep = run(conn, Boards(), dry_run=True)
    assert rep.merged == 1 and "would merge" in rep.render()
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
    assert conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0] == 0
    assert job(conn, fid)["closed_at"] is None


def test_disabled_does_nothing(conn: sqlite3.Connection) -> None:
    feed_row(conn, "Soylent.ai", "Sales Director", "New York City, New York")
    boards = Boards()
    rep = run(conn, boards, filters=Filters(raw={"board_resolution": {"enabled": False}}))
    assert rep.status == "disabled" and boards.requests == []


def test_new_rows_the_filter_rejects_are_not_looked_at(conn: sqlite3.Connection) -> None:
    filters = Filters(raw={"filters": {"exclude_titles": ["(?i)director"]}})
    feed_row(conn, "Soylent.ai", "Sales Director", "New York City, New York", status="new")
    boards = Boards()
    rep = run(conn, boards, filters=filters)
    assert rep.jobs == 0 and boards.requests == []


# ---------------------------------------------------------------------------
# approve time: the approval moves to the ATS row
# ---------------------------------------------------------------------------


def _approve(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute("UPDATE jobs SET status = 'approved' WHERE id = ?", (job_id,))
    conn.execute("INSERT INTO applications (job_id, stage) VALUES (?, 'approved')", (job_id,))
    conn.execute(
        "INSERT INTO decisions (job_id, decision, reason, auto) VALUES (?, 'approve', 'human', 0)",
        (job_id,),
    )
    conn.commit()


def test_approve_time_moves_the_approval_to_the_ats_row(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit.pipeline import triage

    triaged: list[list[int]] = []

    def fake_triage(c: sqlite3.Connection, ids: list[int]) -> dict[int, int]:
        triaged.append(list(ids))
        return {}

    monkeypatch.setattr(triage, "extract_and_score", fake_triage)
    fid = feed_row(conn, "Soylent.ai", "Sales Director", "New York City, New York", status="queued")
    _approve(conn, fid)
    other = feed_row(conn, "Cyberdyne", "Director of Sales Enablement", "Boston, Massachusetts")
    boards = Boards()
    rep, mapped = br.before_tailoring(
        conn, transport=boards.transport, ats_transport=boards.transport, sleep=lambda s: None
    )
    assert rep is not None and mapped is None and rep.merged == 1
    ny = ats_row(conn, "ashby", "soylent", SOYLENT_NEW_YORK)
    assert ny is not None and ny["status"] == "approved"
    app = conn.execute("SELECT job_id, stage FROM applications").fetchone()
    assert app["job_id"] == ny["id"] and app["stage"] == "approved"
    dec = conn.execute("SELECT job_id FROM decisions WHERE decision = 'approve'").fetchone()
    assert dec["job_id"] == ny["id"]
    assert triaged == [[int(ny["id"])]]  # requirements and score from the employer's JD
    assert job(conn, other)["closed_at"] is None  # only approved jobs at approve time
    # tailor --job-id N on the (now merged) feed row tailors the ATS row
    fid2 = feed_row(conn, "Tyrell", TYRELL_TITLE, "United States", source="himalayas", remote=True)
    _approve(conn, fid2)
    rep2, mapped2 = br.before_tailoring(
        conn, fid2, transport=boards.transport, ats_transport=boards.transport, sleep=lambda s: 0
    )
    tyrell = ats_row(conn, "greenhouse", "tyrell", TYRELL_ENABLEMENT)
    assert tyrell is not None and mapped2 == tyrell["id"]
    assert br.before_tailoring(conn, int(tyrell["id"])) == (None, int(tyrell["id"]))


def test_no_aggregator_job_no_request(conn: sqlite3.Connection) -> None:
    assert br.before_tailoring(conn) == (None, None)


def test_an_ats_row_with_its_own_decision_is_held(conn: sqlite3.Connection) -> None:
    import asyncio

    boards = Boards()
    fid = feed_row(conn, "Tyrell", TYRELL_TITLE, "United States", source="adzuna", remote=True)
    _approve(conn, fid)
    asyncio.run(ingest.poll_board(conn, "greenhouse", "tyrell", "Tyrell", boards.transport))
    target = ats_row(conn, "greenhouse", "tyrell", TYRELL_ENABLEMENT)
    assert target is not None
    conn.execute(
        "INSERT INTO decisions (job_id, decision, reason, auto) VALUES (?, 'reject', 'r', 1)",
        (target["id"],),
    )
    conn.commit()
    rep = run(conn, boards)
    assert rep.held == 1 and rep.merged == 0
    assert job(conn, fid)["closed_at"] is None
    assert events(conn, "merge_held")[0]["ats_job_id"] == target["id"]


# ---------------------------------------------------------------------------
# LinkedIn cards (rule 11's exception: no LinkedIn row, no LinkedIn URL)
# ---------------------------------------------------------------------------


def _seen_card(conn: sqlite3.Connection, li_id: str, company: str, title: str, loc: str) -> None:
    detail = {
        "day": "2026-10-12",
        "id": li_id,
        "outcome": "queued_company:no_registry_board",
        "company": company,
        "title": title,
        "location": loc,
    }
    conn.execute(
        "INSERT INTO events (kind, detail) VALUES ('linkedin_guest.seen', ?)", (json.dumps(detail),)
    )
    conn.commit()


def test_a_linkedin_card_enters_as_the_employers_posting(conn: sqlite3.Connection) -> None:
    _seen_card(conn, "4100000014", "Soylent.ai", "Sales Director", "New York, NY")
    _seen_card(conn, "4100000015", "Soylent.ai", "Sales Director", "Chicago, IL")
    boards = Boards()
    rep = run(conn, boards)
    assert rep.cards == 2 and rep.ingested == 1 and rep.ambiguous == 1, rep.render()
    ny = ats_row(conn, "ashby", "soylent", SOYLENT_NEW_YORK)
    assert ny is not None
    outcomes = {e["li_id"]: e for e in events(conn, q.BOARD_RESOLVE_CARD_EVENT)}
    assert outcomes["4100000014"]["outcome"] == "ingested"
    assert outcomes["4100000014"]["job_id"] == ny["id"]
    assert outcomes["4100000015"]["outcome"] == "ambiguous"
    assert [f.li_id for f in br.flags(conn)] == ["4100000015"]
    # nothing anywhere holds a LinkedIn URL or a LinkedIn job row
    assert not conn.execute("SELECT id FROM jobs WHERE apply_url LIKE '%linkedin%'").fetchall()
    assert not conn.execute(
        "SELECT job_id FROM sources WHERE source LIKE '%linkedin%' OR source_url LIKE '%linkedin%'"
    ).fetchall()
    # a card is handled once per cache window
    n = len(boards.requests)
    assert run(conn, boards).cards == 0 and len(boards.requests) == n


# ---------------------------------------------------------------------------
# wiring: stage order, config, CLI, MCP, digest
# ---------------------------------------------------------------------------


def test_stage_runs_after_discovery_before_filter() -> None:
    from jobkit import cli

    names = [n for n, _ in cli._run_all_stages()]
    assert names.index("resolve-boards") == names.index("discovery") + 1
    assert names.index("filter") == names.index("resolve-boards") + 1
    assert names.index("linkedin-guest") < names.index("resolve-boards")


def test_config_defaults_and_limits() -> None:
    cfg = parse_board_resolution(None)
    assert cfg.enabled and cfg.probe_cache_days == 14 and cfg.delay_seconds == 1.0
    with pytest.raises(ValueError, match="delay_seconds must be at least 1"):
        parse_board_resolution({"delay_seconds": 0.2})
    with pytest.raises(ValueError, match="max_requests_per_run"):
        parse_board_resolution({"max_requests_per_run": 5000})
    with pytest.raises(ValueError, match="probe_cache_days"):
        parse_board_resolution({"probe_cache_days": 0})
    from jobkit.config import load_filters

    assert load_filters().board_resolution.enabled  # the shipped config parses


def test_cli(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    from jobkit import cli

    monkeypatch.setattr(cli, "_db", lambda: conn)
    res = CliRunner().invoke(cli.app, ["resolve-boards", "--pick", "x"])
    assert res.exit_code == 1 and "--pick needs --job-id" in res.output
    res = CliRunner().invoke(cli.app, ["resolve-boards", "--dry-run"])
    assert res.exit_code == 0 and "0 aggregator job(s)" in res.output
    res = CliRunner().invoke(cli.app, ["resolve-boards", "--job-id", "999", "--pick", "abc"])
    assert res.exit_code == 1 and "not an open aggregator row" in res.output


def test_mcp_tool(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, spawned: SpawnRecorder
) -> None:
    from jobkit import mcp_server

    conn.executescript(mcp_server.PINS_SCHEMA)
    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    fid = feed_row(conn, "Soylent.ai", "Sales Director", "Chicago, Illinois")
    run(conn, Boards())
    out = mcp_server.resolve_boards()
    assert "a run would look at 1 aggregator job(s)" in out and br.FLAG_KEY in out
    assert SOYLENT_NEW_YORK in out and spawned.calls == []
    out = mcp_server.resolve_boards(confirm=True)
    assert "resolve-boards" in out and spawned.calls[-1][-1] == "resolve-boards"
    mcp_server.resolve_boards(job_id=fid, pick=SOYLENT_NEW_YORK)
    assert spawned.calls[-1][-4:] == ["--job-id", str(fid), "--pick", SOYLENT_NEW_YORK]
    assert "pick needs job_id" in mcp_server.resolve_boards(pick="x")
    queue = mcp_server.get_queue()
    assert br.FLAG_KEY in queue


def test_digest_section(conn: sqlite3.Connection) -> None:
    from jobkit.pipeline.digest import write_digest

    feed_row(conn, "Soylent.ai", "Sales Director", "Chicago, Illinois")  # ambiguous
    feed_row(conn, "Soylent.ai", "Sales Director", "New York City, New York")  # merged
    # registry board, nothing confirms it: unconfirmed
    feed_row(conn, "Cyberdyne", "Director of Sales Enablement", "Boston, Massachusetts")
    run(conn, Boards())
    text = write_digest(conn).read_text()
    assert "## Aggregator jobs → the employer's own board" in text
    assert "1 aggregator job(s) merged" in text
    assert f"[{br.FLAG_KEY}]" in text and f"[{br.UNCONFIRMED_KEY}]" in text


# ---------------------------------------------------------------------------
# a board found by name only, the probe after a trusted board, merges made by
# a board poll, and queue numbers after a merge
# ---------------------------------------------------------------------------

EMBER_ASHBY_AE = "5b7c1d2e-3f40-4a51-8b62-7c83d94ea0f1"
EMBER_ASHBY = {
    "_recorded": "hand-built: another fictional company's Ashby board under the token 'ember'",
    "apiVersion": "1",
    "jobs": [
        {
            "id": EMBER_ASHBY_AE,
            "title": "Account Executive",
            "department": "Sales",
            "team": "Sales",
            "employmentType": "FullTime",
            "location": "Remote - US",
            "secondaryLocations": [],
            "publishedAt": "2026-10-01T09:00:00.000+00:00",
            "isListed": True,
            "isRemote": True,
            "workplaceType": "Remote",
            "address": {"postalAddress": {"addressCountry": "United States"}},
            "jobUrl": f"https://jobs.ashbyhq.com/ember/{EMBER_ASHBY_AE}",
            "applyUrl": f"https://jobs.ashbyhq.com/ember/{EMBER_ASHBY_AE}/application",
            # it names itself "Ember" too: a name in a description is no evidence
            "descriptionHtml": "<p>Ember builds route planning for delivery fleets. "
            "Learn more at emberfleet.com.</p>",
            "descriptionPlain": "Ember builds route planning for delivery fleets. "
            "Learn more at emberfleet.com.",
        }
    ],
}


def _board_as(payload: Any, token: str) -> Any:
    out = copy.deepcopy(payload)
    for j in out["jobs"]:
        for k in ("jobUrl", "applyUrl"):
            j[k] = j[k].replace("/ember/", f"/{token}/")
    return out


def test_an_unrelated_ashby_board_with_the_title_and_place_is_flagged_not_merged(
    conn: sqlite3.Connection, no_registry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ashby reports no company name: a board found under the token 'ember'
    with an 'Account Executive — Remote - US' posting may be another Ember's.
    The approved Ember row is flagged with the candidate, not merged: the
    approval stays on it, no ATS row or boards row is added, and nothing is
    polled nightly under Ember's name."""
    from jobkit.pipeline import triage

    triaged: list[list[int]] = []
    monkeypatch.setattr(triage, "extract_and_score", lambda c, ids: triaged.append(ids) or {})
    fid = feed_row(
        conn, "Ember", "Account Executive", "United States", status="queued", remote=True
    )
    _approve(conn, fid)
    boards = Boards(boards={("ashby", "ember"): EMBER_ASHBY})
    rep, mapped = br.before_tailoring(
        conn, fid, transport=boards.transport, ats_transport=boards.transport, sleep=lambda s: 0
    )
    assert rep is not None and rep.merged == 0 and rep.ambiguous == 1, rep.render()
    assert mapped == fid and triaged == []
    feed = job(conn, fid)
    assert feed["closed_at"] is None and feed["status"] == "approved"
    assert conn.execute("SELECT job_id FROM applications").fetchone()["job_id"] == fid
    assert conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE board_id IS NOT NULL").fetchone()[0] == 0
    assert ("ashby", "ember") in boards.tokens()  # read through the list API only
    (flag,) = br.flags(conn)
    assert flag.job_id == fid
    assert [c["external_id"] for c in flag.candidates] == [EMBER_ASHBY_AE]
    assert "Ashby reports no company name" in flag.why
    assert f"resolve-boards --job-id {fid} --pick" in flag.line()
    # the cached probe result is read again next run: still flagged, once
    rep = run(conn, boards)
    assert rep.cache_hits == 1 and rep.merged == 0 and rep.ambiguous == 1
    assert len(events(conn, q.BOARD_MATCH_AMBIGUOUS_EVENT)) == 1


def test_an_unconfirmed_board_flags_a_linkedin_card_too(
    conn: sqlite3.Connection, no_registry: None
) -> None:
    _seen_card(conn, "4100000099", "Ember", "Account Executive", "United States (Remote)")
    rep = run(conn, Boards(boards={("ashby", "ember"): EMBER_ASHBY}))
    assert rep.ingested == 0 and rep.ambiguous == 1, rep.render()
    assert conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0] == 0
    assert [(f.li_id, f.key) for f in br.flags(conn)] == [("4100000099", br.UNCONFIRMED_KEY)]


def test_a_shared_host_on_both_sides_confirms_nothing(
    conn: sqlite3.Connection, no_registry: None
) -> None:
    """Review 2026-10-12: the Ember row's apply URL is a Notion page, and
    another company's board under the token 'ember' links its own Notion
    handbook. notion.site is everyone's: the approved row is flagged, not
    merged into the other company's posting, and no board is saved under
    Ember's name."""
    other = copy.deepcopy(EMBER_ASHBY)
    for j in other["jobs"]:
        j["descriptionPlain"] += " Our handbook: https://emberfleet.notion.site/handbook"
        j["descriptionHtml"] += (
            '<p><a href="https://emberfleet.notion.site/handbook">Handbook</a></p>'
        )
    fid = feed_row(
        conn,
        "Ember",
        "Account Executive",
        "United States",
        source="himalayas",
        status="queued",
        remote=True,
        apply_url="https://emberhq.notion.site/Account-Executive-7a21",
    )
    _approve(conn, fid)
    rep = run(conn, Boards(boards={("ashby", "ember"): other}))
    assert rep.merged == 0 and rep.ambiguous == 1, rep.render()
    feed = job(conn, fid)
    assert feed["closed_at"] is None and feed["status"] == "approved"
    assert conn.execute("SELECT job_id FROM applications").fetchone()["job_id"] == fid
    assert conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE board_id IS NOT NULL").fetchone()[0] == 0
    (flag,) = br.flags(conn)
    assert flag.job_id == fid and "no domain of 'Ember' appears on the board" in flag.why


def test_a_trusted_board_without_the_title_means_no_probe(conn: sqlite3.Connection) -> None:
    """The registry knows greenhouse/tyrell. Its board has no 'Account
    Executive', so the row is no_title_match: the suffix tier would only reach
    other companies' boards (a hand-built ashby/tyrellhq has the title and a
    US-wide location), and it is not probed."""
    fid = feed_row(conn, "Tyrell", "Account Executive", "United States")
    boards = Boards(boards={("ashby", "tyrellhq"): _board_as(EMBER_ASHBY, "tyrellhq")})
    rep = run(conn, boards)
    assert rep.merged == 0 and rep.no_title_match == 1 and rep.probed == 0, rep.render()
    assert "no slug probe for a company whose board jobkit knows" in rep.render()
    assert job(conn, fid)["closed_at"] is None
    assert boards.tokens() == [("greenhouse", "tyrell")]  # the registry board only
    assert conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0] == 0
    assert events(conn, q.BOARD_PROBE_EVENT) == []


def test_a_known_board_without_the_title_means_no_probe(
    conn: sqlite3.Connection, no_registry: None
) -> None:
    import asyncio

    own_board("Tyrell", "greenhouse", "tyrell")  # owner-curated: read from the DB
    boards = Boards(boards={("ashby", "tyrellhq"): _board_as(EMBER_ASHBY, "tyrellhq")})
    asyncio.run(ingest.poll_board(conn, "greenhouse", "tyrell", "Tyrell", boards.transport))
    fid = feed_row(conn, "Tyrell", "Account Executive", "United States")
    boards.requests.clear()
    rep = run(conn, boards)
    assert rep.merged == 0 and rep.no_title_match == 1, rep.render()
    assert boards.requests == []  # polled in the last day; no probe
    assert job(conn, fid)["closed_at"] is None
    assert conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0] == 1
    # the known board cannot be polled: an error for the next run, not a probe
    conn.execute("UPDATE boards SET last_polled_at = datetime('now', '-3 days')")
    conn.commit()
    failing = Boards(status={("greenhouse", "tyrell"): 403})
    rep = run(conn, failing)
    assert rep.errors == 1 and rep.probed == 0 and rep.merged == 0, rep.render()
    assert failing.tokens() == [("greenhouse", "tyrell")]
    assert events(conn, q.BOARD_PROBE_EVENT) == []


def test_a_gh_jid_apply_url_merged_by_the_poll_counts_as_merged(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The apply URL names only a gh_jid. The poll of the matched board merges
    and closes the feed row first (pipeline/poll.upsert_jobs); the run still
    reports the merge, records board_resolved, scores the ATS row that took
    the approval, and hands its id to `jobkit tailor`."""
    from jobkit.pipeline import triage

    triaged: list[list[int]] = []

    def fake_triage(c: sqlite3.Connection, ids: list[int]) -> dict[int, int]:
        triaged.append(list(ids))
        return {}

    monkeypatch.setattr(triage, "extract_and_score", fake_triage)
    fid = feed_row(
        conn,
        "Tyrell",
        TYRELL_TITLE,
        "United States",
        source="himalayas",
        status="queued",
        apply_url=f"https://www.tyrell.example/careers?gh_jid={TYRELL_ENABLEMENT}",
    )
    _approve(conn, fid)
    boards = Boards()
    rep, mapped = br.before_tailoring(
        conn, fid, transport=boards.transport, ats_transport=boards.transport, sleep=lambda s: 0
    )
    target = ats_row(conn, "greenhouse", "tyrell", TYRELL_ENABLEMENT)
    assert target is not None and target["status"] == "approved"
    assert rep is not None and rep.merged == 1 and rep.skipped == 0, rep.render()
    assert mapped == target["id"] and rep.merged_into == {fid: int(target["id"])}
    (ev,) = events(conn, q.BOARD_RESOLVED_EVENT)
    assert ev["job_id"] == fid and ev["ats_job_id"] == target["id"]
    assert ev["evidence"] == "the apply URL names this posting id"
    assert triaged == [[int(target["id"])]]
    app = conn.execute("SELECT job_id FROM applications").fetchone()
    assert app["job_id"] == target["id"]
    # `jobkit tailor --job-id <feed row>` later: the ATS row, no request
    boards.requests.clear()
    assert br.before_tailoring(conn, fid) == (None, int(target["id"]))
    assert boards.requests == []


def test_a_pick_merged_by_the_poll_counts_as_merged(conn: sqlite3.Connection) -> None:
    """A gh_jid row whose posting was not on the boards looked at, then
    picked by URL: the poll merges it, and the pick reports the merge."""
    fid = feed_row(
        conn,
        "Tyrell Systems",
        "Enablement Lead",
        "Chicago, IL",
        apply_url=f"https://www.tyrell.example/careers?gh_jid={TYRELL_ENABLEMENT}",
    )
    boards = Boards()
    merged, text = br.pick(
        conn,
        fid,
        f"https://job-boards.greenhouse.io/tyrell/jobs/{TYRELL_ENABLEMENT}",
        ats_transport=boards.transport,
    )
    target = ats_row(conn, "greenhouse", "tyrell", TYRELL_ENABLEMENT)
    assert merged and target is not None, text
    assert f"merged into job {target['id']}" in text
    (ev,) = events(conn, q.BOARD_RESOLVED_EVENT)
    assert ev["job_id"] == fid and ev["ats_job_id"] == target["id"]


def test_mcp_queue_numbers_follow_the_approve_time_merge(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, spawned: SpawnRecorder
) -> None:
    """get_queue -> approve(1) -> the background tailor run merges the
    aggregator row into the employer's posting -> item 1 is that ATS row for
    every tool that takes a queue number (and for a raw job id)."""
    from jobkit import mcp_server
    from jobkit.pipeline import triage

    monkeypatch.setattr(triage, "extract_and_score", lambda c, ids: {})
    conn.executescript(mcp_server.PINS_SCHEMA)
    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    fid = feed_row(conn, "Soylent.ai", "Sales Director", "New York City, New York", status="queued")
    conn.execute("INSERT INTO scores (job_id, fit_score, gaps) VALUES (?, 88, '[]')", (fid,))
    conn.commit()
    assert "1. Soylent.ai — Sales Director" in mcp_server.get_queue()
    assert "approved [1]" in mcp_server.approve("1")
    assert spawned.calls[-1][-1] == "tailor"
    # the background `jobkit tailor` resolves the approved aggregator row first
    boards = Boards()
    rep, _ = br.before_tailoring(
        conn, transport=boards.transport, ats_transport=boards.transport, sleep=lambda s: 0
    )
    ny = ats_row(conn, "ashby", "soylent", SOYLENT_NEW_YORK)
    assert ny is not None and rep is not None and rep.merged_into == {fid: int(ny["id"])}
    ats_id = str(ny["id"])
    out = mcp_server.get_artifacts(1)
    assert "no application row" not in out and "stage=approved" in out
    out = mcp_server.retailor(1)
    assert "no application row" not in out
    assert spawned.calls[-1][-3:] == ["tailor", "--job-id", ats_id]
    detail = mcp_server.get_job(1)
    assert "jobs.ashbyhq.com/Soylent" in detail and f"was aggregator job {fid}" in detail
    assert "no authenticity runs yet" in mcp_server.review_artifacts(1)
    mcp_server.restage(1)
    assert spawned.calls[-1][-4:] == ["stage", "--job-ids", ats_id, "--keep-open"]
    mcp_server.stage_forms(ids="1")
    assert spawned.calls[-1][-4:] == ["stage", "--job-ids", ats_id, "--keep-open"]
    mcp_server.stage_forms(job_ids=str(fid))
    assert spawned.calls[-1][-4:] == ["stage", "--job-ids", ats_id, "--keep-open"]
    assert mcp_server._resolve(conn, "all") == [(1, int(ny["id"]))]
