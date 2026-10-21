"""Onboarding 2.5 — find and verify public Greenhouse / Lever / Ashby boards
through the documented public endpoints only, then append them to
config/targets.yaml. Offline: every request goes to a mock transport."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from jobkit.config import load_targets
from jobkit.onboarding.files import GENERATED_MARK, SetupError
from jobkit.onboarding.targets import parse_companies, probe_slugs, seed_targets

GREENHOUSE = {"acme": 3, "1800contacts": 5}
LEVER: dict[str, int] = {"globex": 0}  # 200 [] : empty board or wrong token
ASHBY = {"Umbrella": 2}
PUBLIC_HOSTS = {"boards-api.greenhouse.io", "api.lever.co", "api.ashbyhq.com"}


class Boards:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, parts = request.url.host, request.url.path.split("/")
        self.calls.append(f"{host}{request.url.path}")
        if host == "boards-api.greenhouse.io" and parts[3] in GREENHOUSE:
            return httpx.Response(
                200, json={"jobs": [{"id": i} for i in range(GREENHOUSE[parts[3]])]}
            )
        if host == "api.lever.co" and parts[3] in LEVER:
            return httpx.Response(200, json=[{"id": i} for i in range(LEVER[parts[3]])])
        if host == "api.ashbyhq.com" and parts[3] in ASHBY:
            return httpx.Response(200, json={"jobs": [{"id": i} for i in range(ASHBY[parts[3]])]})
        return httpx.Response(404, json={"error": "not found"})


@pytest.fixture()
def boards() -> Boards:
    return Boards()


def _client(boards: Boards) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(boards))


COMPANIES: list[Any] = [
    {
        "name": "Acme Robotics",
        "domain": "https://www.acme.com/careers",
        "tier": 1,
        "said": "dream job",
    },
    {"name": "Umbrella", "url": "https://jobs.ashbyhq.com/Umbrella", "tier": 2},
    "Globex",
    {"name": "Nowhere Widgets", "tier": 3},
    {"name": "1-800 Contacts", "tier": 2},
]


@pytest.fixture()
def fresh(jobkit_home: Path) -> Path:
    (jobkit_home / "config" / "targets.yaml").unlink()
    return jobkit_home


def test_discovers_verifies_and_writes(fresh: Path, boards: Boards) -> None:
    out = seed_targets(COMPANIES, client=_client(boards), delay=0)
    path = fresh / "config" / "targets.yaml"
    text = path.read_text()
    assert text.startswith(GENERATED_MARK)
    t = load_targets(path)
    by_name = {c.name: c for c in t.companies}
    acme = by_name["Acme Robotics"]
    assert (acme.tier, acme.domain) == (1, "acme.com")
    assert acme.board and (acme.board.ats, acme.board.token) == ("greenhouse", "acme")
    umbrella = by_name["Umbrella"].board
    assert umbrella and (umbrella.ats, umbrella.token) == ("ashby", "Umbrella")
    contacts = by_name["1-800 Contacts"].board
    assert contacts and (contacts.ats, contacts.token) == ("greenhouse", "1800contacts")
    # a Lever 200 [] cannot be told from a wrong token: kept, but marked unverified
    globex = by_name["Globex"].board
    assert globex and globex.ats == "lever"
    assert "NOT verified" in text
    # nothing found: recorded without a board (not polled), and reported
    assert by_name["Nowhere Widgets"].board is None
    assert "NOT FOUND: Nowhere Widgets" in out
    assert "3 board(s) verified, 1 unverified, 1 not found" in out
    assert '# you said: "dream job"' in text
    # only the documented public board endpoints were asked
    assert {c.split("/")[0] for c in boards.calls} <= PUBLIC_HOSTS


def test_url_and_registry_come_before_probing(fresh: Path, boards: Boards) -> None:
    seed_targets(COMPANIES[1:2] + COMPANIES[4:], client=_client(boards), delay=0)
    assert boards.calls == [
        "api.ashbyhq.com/posting-api/job-board/Umbrella",
        "boards-api.greenhouse.io/v1/boards/1800contacts/jobs",
    ]


def test_record_keeps_what_was_tried(fresh: Path, boards: Boards) -> None:
    seed_targets(["Nowhere Widgets"], client=_client(boards), delay=0)
    record = yaml.safe_load((fresh / "data" / "setup" / "targets.answers.yaml").read_text())
    entry = record["companies"][0]
    assert entry["board"] is None
    assert any("greenhouse:nowherewidgets" in t for t in entry["tried"])


def test_skip_not_found(fresh: Path, boards: Boards) -> None:
    seed_targets(
        ["Nowhere Widgets", "Acme"], client=_client(boards), delay=0, include_not_found=False
    )
    names = [c.name for c in load_targets(fresh / "config" / "targets.yaml").companies]
    assert names == ["Acme"]


def test_existing_targets_are_appended_never_rewritten(jobkit_home: Path, boards: Boards) -> None:
    path = jobkit_home / "config" / "targets.yaml"
    before = path.read_text()
    existing = load_targets(path).companies
    seed_targets(["Acme", existing[0].name], client=_client(boards), delay=0)
    after = path.read_text()
    assert after.startswith(before.rstrip("\n"))  # every existing line kept, in order
    t = load_targets(path)
    assert len(t.companies) == len(existing) + 1  # the duplicate was skipped
    assert t.companies[-1].name == "Acme"


def test_no_network_without_a_name(jobkit_home: Path, boards: Boards) -> None:
    with pytest.raises(SetupError):
        seed_targets([], client=_client(boards), delay=0)
    with pytest.raises(SetupError, match="tier must be"):
        seed_targets([{"name": "Acme", "tier": 7}], client=_client(boards), delay=0)
    assert boards.calls == []


def test_probe_slugs() -> None:
    (c,) = parse_companies([{"name": "Acme Robotics, Inc.", "domain": "www.acme-robotics.io"}])
    assert probe_slugs(c) == ["acme-robotics", "acmerobotics"]


def test_workday_url_is_reported_not_polled(fresh: Path, boards: Boards) -> None:
    out = seed_targets(
        [{"name": "Initech", "url": "https://initech.wd5.myworkdayjobs.com/en-US/careers"}],
        client=_client(boards),
        delay=0,
        probe=False,
    )
    assert "NOT FOUND: Initech" in out and "workday" in out
    assert boards.calls == []


# ---------------------------------------------------------------------------
# feed-only discovery: employers with no board jobkit can poll
# ---------------------------------------------------------------------------

FEED_ONLY = "the hospitals I want all use Workday; I'll rely on the feeds"


def test_feed_only_needs_no_company_and_no_network(fresh: Path, boards: Boards) -> None:
    out = seed_targets([], feed_only_said=FEED_ONLY, client=_client(boards), delay=0)
    assert boards.calls == []
    assert "discovery comes from the feeds" in out
    path = fresh / "config" / "targets.yaml"
    assert path.read_text().startswith(GENERATED_MARK)
    assert load_targets(path).companies == []
    assert yaml.safe_load(path.read_text())["feed_only_said"] == FEED_ONLY
    record = yaml.safe_load((fresh / "data" / "setup" / "targets.answers.yaml").read_text())
    assert record["feed_only_said"] == FEED_ONLY


def test_feed_only_then_companies_keeps_both(fresh: Path, boards: Boards) -> None:
    seed_targets(None, feed_only_said=FEED_ONLY)
    seed_targets(["Acme"], client=_client(boards), delay=0)
    path = fresh / "config" / "targets.yaml"
    data = yaml.safe_load(path.read_text())
    assert data["feed_only_said"] == FEED_ONLY
    assert [c.name for c in load_targets(path).companies] == ["Acme"]
    record = yaml.safe_load((fresh / "data" / "setup" / "targets.answers.yaml").read_text())
    assert record["feed_only_said"] == FEED_ONLY  # kept across calls
    # said again: the one line is replaced, the companies stay
    seed_targets([], feed_only_said="feeds only, for now")
    text = path.read_text()
    assert text.count("feed_only_said:") == 1
    assert yaml.safe_load(text)["feed_only_said"] == "feeds only, for now"
    assert [c.name for c in load_targets(path).companies] == ["Acme"]


def test_feed_only_on_a_hand_written_file(jobkit_home: Path) -> None:
    path = jobkit_home / "config" / "targets.yaml"
    before = load_targets(path).companies
    seed_targets([], feed_only_said=FEED_ONLY)
    assert load_targets(path).companies == before
    assert yaml.safe_load(path.read_text())["feed_only_said"] == FEED_ONLY


def test_no_company_and_no_feed_only_is_refused(fresh: Path) -> None:
    with pytest.raises(SetupError, match="feed_only_said"):
        seed_targets([])
    assert not (fresh / "config" / "targets.yaml").exists()
