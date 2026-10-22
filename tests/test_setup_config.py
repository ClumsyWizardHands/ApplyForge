"""Productizing (2026-10-12): the user's choices are out of git, the repo
ships templates, a missing config file says how to create it, location logic
reads config, the User-Agent contact is the operator's, and `jobkit doctor`
reports what a checkout still needs."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import stat as statmod
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from jobkit import cli, config, contact, doctor, mcp_server, rules
from jobkit import db as q
from jobkit.adapters.ats.base import get_json
from jobkit.config import PERSONAL_CONFIG_FILES, ConfigMissing, Filters, load_filters
from jobkit.pipeline import triage
from jobkit.pipeline.filter import _reject_reason
from jobkit.pipeline.triage import geo_restriction_acceptable
from jobkit.validate import authenticity, style
from tests.conftest import REPO_ROOT, TEST_CONFIG

TEMPLATES = REPO_ROOT / "config"


def _stem(name: str) -> str:
    return name.removesuffix(".yaml")


# ---------------------------------------------------------------------------
# templates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", PERSONAL_CONFIG_FILES)
def test_every_personal_config_file_has_a_template(name: str) -> None:
    tpl = TEMPLATES / f"{_stem(name)}.example.yaml"
    assert tpl.is_file(), f"no template for config/{name}"
    assert isinstance(yaml.safe_load(tpl.read_text()), dict)
    assert "TEMPLATE" in tpl.read_text()  # says what it is and how to make yours


def test_filters_template_loads_with_neutral_defaults() -> None:
    text = (TEMPLATES / "filters.example.yaml").read_text()
    f = load_filters(TEMPLATES / "filters.example.yaml")
    # one placeholder track for any field, which doctor warns about until edited
    assert f.track_ids == [doctor.PLACEHOLDER_TRACK_ID]
    assert doctor.PLACEHOLDER_REGEX_TEXT in f.tracks[0].title_regex
    assert f.prompt_set == "general" and f.prompt_set_explicit
    assert not f.has_sales_track
    # every per-track floor names a defined track (the loader checks it too)
    assert set(f.raw["filters"]["comp"]["track_floors"]) <= set(f.track_ids)
    # no place chosen for you: remote only until you list places
    assert f.allowed_locations == [] and f.relocation_cities == []
    assert f.accepted_location_buckets == config.REMOTE_BUCKETS
    assert f.nightly_at == (2, 0) and f.questions_per_day == 3
    assert f.self_review_max_rounds == 1 and f.min_score_to_surface == 65
    # no-reply bookkeeping is a plain status rule; nothing drafts follow-ups
    assert "followups" not in f.raw
    assert (f.ghost_after.days, f.ghost_after.source) == (30, "tracking")
    # one comp_sanity block: the range checks and the field-neutral knobs together
    assert text.count("\ncomp_sanity:") == 1
    assert f.comp_sanity.ote_check == "ote_tracks"
    assert f.comp_sanity.max_range_width_pct == 0.6
    # no sales search ships as the default
    feeds = f.raw["feeds"]
    assert feeds["remotive"]["categories"] == [] and feeds["consider"]["categories"] == []
    assert not re.search(r"(?i)account executive|head of sales|sales director", text)
    # a feed that breaks hard rule 11 ships disabled, and so does the
    # unaudited Chief of Staff feed and LinkedIn discovery
    assert feeds["getro"]["enabled"] is False
    assert feeds["cosnetwork"]["enabled"] is False
    assert f.linkedin_guest.enabled is False
    # new setups open the filled forms at the end of the daily run
    assert f.open_after_daily_run is True


def test_open_after_daily_run_is_off_when_absent_and_strict() -> None:
    # a config written before the key existed keeps its scheduled run as it was
    assert Filters(raw={}).open_after_daily_run is False
    assert Filters(raw={"staging": {}}).open_after_daily_run is False
    assert Filters(raw={"staging": {"open_after_daily_run": False}}).open_after_daily_run is False
    assert Filters(raw={"staging": {"open_after_daily_run": None}}).open_after_daily_run is False
    for bad in ("yes", 1):
        f = Filters(raw={"staging": {"open_after_daily_run": bad}})
        with pytest.raises(ValueError, match="staging.open_after_daily_run"):
            _ = f.open_after_daily_run
    with pytest.raises(ValueError, match="staging must be a mapping"):
        _ = Filters(raw={"staging": True}).open_after_daily_run
    # the test profile (like the owner's own config) does not set it
    assert load_filters().open_after_daily_run is False


def test_sales_example_keeps_the_sales_profile() -> None:
    f = load_filters(TEMPLATES / "filters.sales.example.yaml")
    assert {"account_executive", "sales_exec", "sdr_bdr_manager", "sdr_bdr"} == set(f.track_ids)
    assert f.prompt_set == "sales" and f.has_sales_track
    assert "followups" not in f.raw and f.ghost_after.source == "tracking"
    # the sales defaults of the comp-sanity knobs, in its one block
    text = (TEMPLATES / "filters.sales.example.yaml").read_text()
    assert text.count("\ncomp_sanity:") == 1
    assert f.comp_sanity == config.CompSanity()
    assert f.raw["feeds"]["remotive"]["categories"] == ["sales", "business"]
    assert f.raw["feeds"]["getro"]["enabled"] is False
    assert f.open_after_daily_run is True


def test_targets_template_loads() -> None:
    t = config.load_targets(TEMPLATES / "targets.example.yaml")
    assert t.companies and all(c.board for c in t.companies)


def _rule_job(**kw: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "title": "Account Executive",
        "title_class": "account_executive",
        "tier": 3,
        "fit_score": 60,
        "flags": [],
        "friction": 10,
        "remote": 1,
        "comp_min": None,
        "comp_max": None,
        "comp_basis": "unknown",
        "sanity_job_id": 1,
        "demand_score": 3,
    }
    job.update(kw)
    return job


def test_autoapprove_template_recommends_one_rule_and_no_limit() -> None:
    cfg = yaml.safe_load((TEMPLATES / "autoapprove.example.yaml").read_text())
    # the recommended start: approve what clears the bar, with no sanity flag
    assert cfg["auto_approve"] == [
        {
            "when": {"score_gte": 75, "sanity_flags": "none", "sanity_checked": True},
            "reason": "clears-your-bar",
        }
    ]
    # nothing is rejected without asking until you add a rule yourself (feed
    # companies are tier 3: an active tier-3 rule would reject feed jobs under
    # a track's lower min_score)
    assert cfg["auto_reject"] == []
    # no daily limit unless you set one: the key is only a commented-out line
    assert rules.MAX_PER_DAY_KEY not in cfg
    assert rules.max_auto_approve_per_day(cfg) is None
    text = (TEMPLATES / "autoapprove.example.yaml").read_text()
    limit = [ln for ln in text.splitlines() if rules.MAX_PER_DAY_KEY in ln]
    assert limit and all(ln.lstrip().startswith("#") for ln in limit)
    commented = yaml.safe_load(limit[-1].lstrip().lstrip("#").strip())
    assert rules.max_auto_approve_per_day(commented) is not None  # valid once uncommented
    rules._cond_matches(cfg["auto_approve"][0]["when"], _rule_job())  # known keys
    assert rules._cond_matches(cfg["auto_approve"][0]["when"], _rule_job(fit_score=75))
    assert not rules._cond_matches(
        cfg["auto_approve"][0]["when"], _rule_job(fit_score=90, flags=["remote-conflict: x"])
    )
    # a job whose remote or comp check did not finish is never approved by it
    for unchecked in ({"sanity_job_id": None, "demand_score": None}, {"demand_score": None}):
        assert not rules._cond_matches(
            cfg["auto_approve"][0]["when"], _rule_job(fit_score=90, **unchecked)
        )
    examples = [
        yaml.safe_load(line.strip().lstrip("#").strip().removeprefix("- when:").strip())
        for line in text.splitlines()
        if line.strip().startswith("# - when:")
    ]
    assert examples
    for when in examples:
        rules._cond_matches(when, _rule_job())  # an unknown key raises


def test_banned_phrases_template_has_every_pass() -> None:
    cfg = yaml.safe_load((TEMPLATES / "banned_phrases.example.yaml").read_text())
    for section in ("cover_letter", "resume", "structural", "cross_letter", "drift", "critic"):
        assert section in cfg, section
    assert style.estimate_style_from(cfg) in style.ESTIMATE_STYLES


def test_commute_template_counts_remote_as_zero() -> None:
    cfg = yaml.safe_load((TEMPLATES / "commute_table.example.yaml").read_text())
    assert cfg["buckets"] == {"remote": 0}


# A checkout owner's identifying details (name, employers, school, home town,
# phone): none may ship in any file a commit would hold. These scans name no
# one. Besides the digest markers below, a maintainer may list patterns in a
# local, gitignored file, docs/local/owner-markers.txt, one case-insensitive
# regular expression per line ("#" starts a comment line). Bound each name by
# any non-alphanumeric character, (?<![a-z0-9])name(?![a-z0-9]), so an
# identifier joined by "_" (`role_name`, `at_name`) is caught too, which `\b`
# would let through. Without the file only that scan skips.
# The gate runs these scans with JOBKIT_REQUIRE_OWNER_SCAN=1, which turns a
# missing file into a failure instead of a skip, and may point
# JOBKIT_OWNER_LOCAL_DIR at the owner checkout's docs/local/ to scan a tree
# (a worktree, a clean export) that has none of its own.
OWNER_LOCAL_DIR = Path(os.environ.get("JOBKIT_OWNER_LOCAL_DIR") or REPO_ROOT / "docs" / "local")
REQUIRE_OWNER_SCAN = os.environ.get("JOBKIT_REQUIRE_OWNER_SCAN") == "1"
OWNER_MARKERS_FILE = OWNER_LOCAL_DIR / "owner-markers.txt"


def _no_owner_file(what: str, path: Path) -> None:
    """Skip the calling scan without its local file, or fail under
    JOBKIT_REQUIRE_OWNER_SCAN=1, so a gate never reads a skip as a pass."""
    msg = f"no owner {what}: {path} is absent"
    if REQUIRE_OWNER_SCAN:
        pytest.fail(f"{msg} (JOBKIT_REQUIRE_OWNER_SCAN=1)")
    pytest.skip(msg)


def owner_markers(path: Path = OWNER_MARKERS_FILE) -> re.Pattern[str] | None:
    """The file's patterns as one case-insensitive regex; None without any."""
    if not path.is_file():
        return None
    lines = [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines:
        return None
    return re.compile("|".join(f"(?:{line})" for line in lines), re.IGNORECASE)


OWNER_MARKERS = owner_markers()


def require_owner_markers() -> re.Pattern[str]:
    """The checkout's owner markers, or skip the calling test without them."""
    if OWNER_MARKERS is None:
        _no_owner_file("markers", OWNER_MARKERS_FILE)
    assert OWNER_MARKERS is not None
    return OWNER_MARKERS


# A maintainer may also list markers as keyed BLAKE2b digests of their words,
# never as text, in a second local, gitignored file,
# docs/local/owner-marker-digests.txt: a "key:" line (the key every digest
# was made with; it stays local too), then "marker: <digest> ..." lines (the
# words of one marker, in a row) and "figure <group>: <digest> ..." lines (the
# generic figures of one career: a file holding two from different groups
# fails). marker_words decides what a word is: case-insensitive; "_", "-",
# "/", ".", "$", "%" and camelCase all split words, so `role_name`,
# `nameSurname` and "Name-Surname" are caught. Neither the list nor its key
# ships, so the shared tree cannot be read back into the names it guards.
# Without the file only that scan skips. To add a marker, append the output
# of `marker_key("the words", key)`.
OWNER_DIGESTS_FILE = OWNER_LOCAL_DIR / "owner-marker-digests.txt"

OwnerDigests = tuple[bytes, frozenset[tuple[str, ...]], tuple[tuple[int, tuple[str, ...]], ...]]


def marker_words(text: str) -> list[str]:
    """Lowercase runs of letters and runs of digits; every other character,
    and a camelCase or ACRONYMWord boundary, separates two words."""
    text = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z]{2})", " ", text)
    return re.findall(r"[a-z]+|[0-9]+", text.lower())


def _word_digest(word: str, key: bytes) -> str:
    return hashlib.blake2b(word.encode(), digest_size=8, key=key).hexdigest()


def marker_key(phrase: str, key: bytes) -> tuple[str, ...]:
    """A marker as stored: the digest of each of its words, in order."""
    return tuple(_word_digest(w, key) for w in marker_words(phrase))


def owner_digests(path: Path = OWNER_DIGESTS_FILE) -> OwnerDigests | None:
    """The file's key, markers and figure groups; None without a key or any
    marker. A malformed line is an error, never a silently skipped marker."""
    if not path.is_file():
        return None
    key = b""
    markers: set[tuple[str, ...]] = set()
    figures: list[tuple[int, tuple[str, ...]]] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        head, _, rest = line.partition(":")
        if head == "key" and rest.strip():
            key = rest.strip().encode()
            continue
        words = tuple(rest.split())
        group = re.fullmatch(r"figure (\d+)", head)
        if (
            not words
            or not all(re.fullmatch(r"[0-9a-f]{16}", d) for d in words)
            or (head != "marker" and group is None)
        ):
            raise ValueError(f"{path.name}: bad line {line!r}")
        if group is None:
            markers.add(words)
        else:
            figures.append((int(group.group(1)), words))
    if not key or not (markers or figures):
        return None
    return key, frozenset(markers), tuple(figures)


OWNER_DIGESTS = owner_digests()


def digest_owner_hits(text: str, digests: OwnerDigests) -> list[str]:
    """The text's words that spell a digest marker (or two figure groups)."""
    key, markers, figures = digests
    words = marker_words(text)
    cache: dict[str, str] = {}
    hashed = [cache.get(w) or cache.setdefault(w, _word_digest(w, key)) for w in words]
    by_first: dict[str, list[tuple[tuple[str, ...], int | None]]] = {}
    for marker in markers:
        by_first.setdefault(marker[0], []).append((marker, None))
    for group, marker in figures:
        by_first.setdefault(marker[0], []).append((marker, group))
    hits: set[str] = set()
    figure_hits: dict[int, str] = {}
    for i, d in enumerate(hashed):
        for marker, group in by_first.get(d, ()):
            if tuple(hashed[i : i + len(marker)]) == marker:
                found = " ".join(words[i : i + len(marker)])
                if group is None:
                    hits.add(found)
                else:
                    figure_hits.setdefault(group, found)
    if len(figure_hits) >= 2:
        hits.update(figure_hits.values())
    return sorted(hits)


def test_digest_markers_match_words_not_substrings(tmp_path: Path) -> None:
    key = b"a-made-up-key"
    markers = frozenset(
        {marker_key("pat", key), marker_key("doe", key), marker_key("555 0199", key)}
    )
    text = "role_pat, PatDoe, at-DOE: call (555) 0199. A patter, a doer and 555-0100 are fine."
    assert digest_owner_hits(text, (key, markers, ())) == ["555 0199", "doe", "pat"]
    figures = ((1, marker_key("$40K", key)), (2, marker_key("$2.5M", key)))
    assert digest_owner_hits("a $40K month", (key, markers, figures)) == []
    assert digest_owner_hits("a $40K month, $2.5M a year", (key, markers, figures)) == [
        "2 5 m",
        "40 k",
    ]
    # another key never matches: the digests are only as good as the local key
    assert digest_owner_hits(text, (b"another-key", markers, ())) == []
    # the file: a key line, marker lines, figure lines; the list is local
    assert _local_note("docs/local/owner-marker-digests.txt")
    f = tmp_path / "owner-marker-digests.txt"
    f.write_text("# no key, no markers\n\n")
    assert owner_digests(f) is None
    assert owner_digests(tmp_path / "absent.txt") is None
    lines = [
        "key: a-made-up-key",
        "marker: " + " ".join(marker_key("pat", key)),
        "figure 1: " + " ".join(marker_key("$40K", key)),
    ]
    f.write_text("\n".join(lines) + "\n")
    assert owner_digests(f) == (key, frozenset({marker_key("pat", key)}), figures[:1])
    f.write_text("key: a-made-up-key\nmarker: pat\n")
    with pytest.raises(ValueError, match="bad line"):
        owner_digests(f)


# Every file a commit would hold: the tracked files and the untracked ones
# .gitignore lets through. Tests never spawn a process (tests/conftest.py), so
# instead of asking git, the scan walks the checkout and applies the root
# .gitignore itself (_gitignored; the self-check below holds it to git's
# rules for the patterns this repository uses). Two files are exempt:
# docs/persona-mapping.md (the example persona's reference, gitignored) and
# the claims validator's list of common English words, which holds ordinary
# words such as given names and place names that any owner's markers may
# share; it is never edited to suit one person. Recorded public board
# payloads and the board registries are scanned too.
OWNER_SCAN_EXEMPT = frozenset({"docs/persona-mapping.md", "src/jobkit/validate/common_words.txt"})


def _gitignore_rules(text: str) -> list[tuple[bool, bool, str]]:
    """(negated, directory only, pattern) for each rule of a .gitignore."""
    rules = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        line = line.removeprefix("!")
        dir_only = line.endswith("/")
        rules.append((negated, dir_only, line.rstrip("/")))
    return rules


def _gitignored(rel: str, is_dir: bool, rules: list[tuple[bool, bool, str]]) -> bool:
    """Git's verdict for the repo-relative path `rel`: the last rule that
    matches wins. A pattern holding "/" is anchored at the root and its "*"
    stops at "/"; any other pattern matches the path's last part."""
    ignored = False
    for negated, dir_only, pattern in rules:
        if dir_only and not is_dir:
            continue
        if "/" in pattern:
            anchored = pattern.lstrip("/")
            hit = rel.count("/") == anchored.count("/") and fnmatch.fnmatchcase(rel, anchored)
        else:
            hit = fnmatch.fnmatchcase(rel.rsplit("/", 1)[-1], pattern)
        if hit:
            ignored = not negated
    return ignored


def _committable_files() -> list[Path]:
    rules = _gitignore_rules((REPO_ROOT / ".gitignore").read_text())
    found: list[Path] = []
    for top, dirs, names in os.walk(REPO_ROOT):
        here = Path(top)
        dirs[:] = sorted(
            d
            for d in dirs
            if d != ".git"
            and not (here / d).is_symlink()
            and not _gitignored((here / d).relative_to(REPO_ROOT).as_posix(), True, rules)
        )
        for name in sorted(names):
            path = here / name
            rel = path.relative_to(REPO_ROOT).as_posix()
            if path.is_symlink() or rel in OWNER_SCAN_EXEMPT or name == ".git":
                continue
            if not _gitignored(rel, False, rules):
                found.append(path)
    return found


def test_gitignore_reading_matches_git() -> None:
    rules = _gitignore_rules((REPO_ROOT / ".gitignore").read_text())
    for rel, is_dir, ignored in (
        ("data/jobkit.db", False, True),
        ("data/answer_bank.yaml", False, True),
        ("data/answer_bank.example.yaml", False, False),
        ("config/filters.yaml", False, True),
        ("config/filters.example.yaml", False, False),
        ("docs/local", True, True),
        ("docs/audit-2026-10-08.md", False, True),
        ("docs/persona-mapping.md", False, True),
        ("docs/why-no-linkedin.md", False, False),
        (".claude/settings.local.json", False, True),
        (".claude/worktrees", True, True),
        (".claude/skills", True, False),
        ("src/jobkit/__pycache__", True, True),
        ("src/jobkit/.claude", True, True),
        (".venv", True, True),
        (".env", False, True),
        (".env.example", False, False),
        ("logs", True, True),
        ("tests/fixtures/greenhouse_jobs.json", False, False),
    ):
        assert _gitignored(rel, is_dir, rules) is ignored, rel
    files = {p.relative_to(REPO_ROOT).as_posix() for p in _committable_files()}
    assert {
        "README.md",
        "tests/test_setup_config.py",
        ".claude/skills/jobkit-setup/SKILL.md",
    } <= files
    assert not {f for f in files if f.startswith(("data/", "config/")) and ".example." not in f}


def _file_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


def test_committable_files_hold_no_local_owner_digests() -> None:
    if OWNER_DIGESTS is None:
        _no_owner_file("digests", OWNER_DIGESTS_FILE)
    files = _committable_files()
    assert any(p.name == "test_setup_config.py" for p in files)
    hits = {
        p.relative_to(REPO_ROOT).as_posix(): found
        for p in files
        for found in [digest_owner_hits(_file_text(p), OWNER_DIGESTS)]
        if found
    }
    assert hits == {}, "the original owner's details are in these files (use Jordan Rivera)"


def test_committable_files_hold_no_local_owner_markers() -> None:
    markers = require_owner_markers()
    hits = {
        p.relative_to(REPO_ROOT).as_posix(): sorted({m.group(0) for m in markers.finditer(t)})
        for p in _committable_files()
        for t in [_file_text(p)]
        if markers.search(t)
    }
    assert hits == {}, "a local working note belongs in docs/local/, which is gitignored"


def test_owner_markers_are_local_and_bounded(tmp_path: Path) -> None:
    # the markers file is a local working note: it never ships
    assert _local_note("docs/local/owner-markers.txt")
    assert owner_markers(tmp_path / "absent.txt") is None
    f = tmp_path / "owner-markers.txt"
    f.write_text("# comment lines and blank lines are skipped\n\n")
    assert owner_markers(f) is None
    f.write_text("# a made-up owner\n(?<![a-z0-9])(pat|doe)(?![a-z0-9])\n555[-. ]0199\n")
    rx = owner_markers(f)
    assert rx is not None
    text = "role_pat, at_DOE: call 555-0199. A patter, a doer and 555-0100 are fine."
    assert [m.group(0) for m in rx.finditer(text)] == ["pat", "DOE", "555-0199"]


# Docs every invitee receives (and a new user's Claude reads first): the
# top-level docs and every file under docs/ but local working notes. Globbed,
# not listed, so a new doc is scanned the day it lands, and so is a note left
# outside docs/local/, which `git add -A` would commit.
TOP_LEVEL_DOCS = ("README.md", "CLAUDE.md", "SPEC.md", "ONBOARDING.md")


def _local_notes_rules() -> list[str]:
    """The .gitignore rules for local working notes: its lines under docs/."""
    lines = (REPO_ROOT / ".gitignore").read_text().splitlines()
    return [line.strip() for line in lines if line.strip().startswith("docs/")]


def _local_note(rel: str) -> bool:
    """True when the repo-relative path `rel` is a local working note that
    .gitignore keeps out of git. A rule ending in "/" covers a folder; any
    other rule is a glob whose "*" stops at "/", as in .gitignore."""
    for rule in _local_notes_rules():
        if rule.endswith("/"):
            if rel.startswith(rule):
                return True
        elif rel.count("/") == rule.count("/") and fnmatch.fnmatchcase(rel, rule):
            return True
    return False


def _shipped_docs() -> list[Path]:
    under_docs = [
        p
        for p in sorted((REPO_ROOT / "docs").rglob("*"))
        if p.is_file()
        and p.name != ".DS_Store"
        and not _local_note(p.relative_to(REPO_ROOT).as_posix())
    ]
    return [*(REPO_ROOT / name for name in TOP_LEVEL_DOCS), *under_docs]


SHIPPED_DOCS = _shipped_docs()


def test_local_notes_stay_out_and_tracked_docs_ship() -> None:
    assert "docs/local/" in _local_notes_rules()
    # the original owner's dated working notes (audit findings, residual
    # issues, a proposed corpus diff) and anything in docs/local/
    for rel in (
        "docs/local/notes.md",
        "docs/local/2026-10/corpus.diff",
        "docs/audit-2026-10-08.md",
        "docs/audit-2026-10-08-findings.json",
        "docs/residual-issues-2026-10-08.json",
        "docs/corpus-proposed-2026-10-08.diff",
    ):
        assert _local_note(rel), rel
    shipped = {p.relative_to(REPO_ROOT).as_posix() for p in SHIPPED_DOCS}
    assert {
        *TOP_LEVEL_DOCS,
        "docs/endpoints_verified.md",
        "docs/research-2026-09-02.md",
        "docs/why-no-linkedin.md",
    } <= shipped


def test_data_templates_are_a_fictional_person() -> None:
    corpus = yaml.safe_load((REPO_ROOT / "data" / "career_corpus.example.yaml").read_text())
    bank = yaml.safe_load((REPO_ROOT / "data" / "answer_bank.example.yaml").read_text())
    assert corpus["identity"]["name"] == "Jordan Rivera"
    assert bank["identity"]["first_name"] == "Jordan"
    # placeholder addresses: never used as the User-Agent contact
    assert contact._usable(bank["identity"]["email"]) is None
    # hard rule 6: a bullet's confidence and evidence are the human's; the
    # template shows them set, never a default the draft could inherit
    for b in corpus["bullets"]:
        assert b["metric_confidence"] in ("verified", "estimate", "none")
        assert b["evidence"]


# ---------------------------------------------------------------------------
# a missing personal config file
# ---------------------------------------------------------------------------


def _expected(name: str) -> str:
    return (
        f"config/{name} not found — run the jobkit setup interview "
        f'(say "set up jobkit" in Claude) or copy config/{_stem(name)}.example.yaml'
    )


READERS: dict[str, Callable[[], object]] = {
    "filters.yaml": load_filters,
    "targets.yaml": config.load_targets,
    "autoapprove.yaml": rules._load_rules,
    "banned_phrases.yaml": style.configured_estimate_style,
    "commute_table.yaml": lambda: triage._commute_minutes("nyc-metro", {}),
}


@pytest.mark.parametrize("name", PERSONAL_CONFIG_FILES)
def test_missing_config_names_the_interview_and_the_template(jobkit_home: Path, name: str) -> None:
    (jobkit_home / "config" / name).unlink()
    with pytest.raises(ConfigMissing) as exc:
        READERS[name]()
    assert str(exc.value) == _expected(name)
    assert isinstance(exc.value, FileNotFoundError)  # old handlers still catch it


def test_authenticity_config_missing(jobkit_home: Path) -> None:
    (jobkit_home / "config" / "banned_phrases.yaml").unlink()
    with pytest.raises(ConfigMissing, match="copy config/banned_phrases.example.yaml"):
        authenticity.load_config()


def test_cli_reports_missing_config_in_one_line(
    jobkit_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    c = q.connect(create=True)
    q.init_db(c)
    c.close()
    (jobkit_home / "config" / "filters.yaml").unlink()
    monkeypatch.setattr(sys, "argv", ["jobkit", "refilter", "--dry-run"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.strip() == f"jobkit: {_expected('filters.yaml')}"
    assert "Traceback" not in err


def test_console_script_is_the_wrapper() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]
    assert project["scripts"]["jobkit"] == "jobkit.cli:main"


# ---------------------------------------------------------------------------
# what ships, and what never does
# ---------------------------------------------------------------------------


def test_gitignore_keeps_personal_files_out() -> None:
    lines = {line.strip() for line in (REPO_ROOT / ".gitignore").read_text().splitlines()}
    for pattern in (
        ".env",
        "data/*",
        "!data/*.example.yaml",
        "config/*.yaml",
        "!config/*.example.yaml",
        "out/",
        "logs/",
        ".mcp.json",
        "*.db",
        ".claude/*",
        "!.claude/skills/",
        "docs/local/",
    ):
        assert pattern in lines, pattern


def test_build_excludes_personal_files() -> None:
    build = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["tool"]["hatch"]["build"]
    for pattern in ("/data/*", "/config/*.yaml", "/out", "/logs", "*.db", "/.claude/*", "/.env"):
        assert pattern in build["exclude"], pattern
    assert "!/.claude/skills" in build["exclude"]  # project skills ship


def test_sandbox_config_is_templates_plus_test_profile(jobkit_home: Path) -> None:
    cfg = jobkit_home / "config"
    for name in PERSONAL_CONFIG_FILES:
        assert (cfg / name).read_bytes() == (TEST_CONFIG / name).read_bytes()
        assert (cfg / f"{_stem(name)}.example.yaml").is_file()


# ---------------------------------------------------------------------------
# location logic reads config
# ---------------------------------------------------------------------------


def _filters(locations: dict[str, Any], **top: Any) -> Filters:
    return Filters(raw={"filters": {"locations": locations}, **top})


def test_place_buckets_come_from_the_posting_normalizer() -> None:
    f = _filters(
        {"allowed": ["Brooklyn", "Columbus"]},
        remote_policy={"relocation_cities": ["Austin"]},
    )
    assert f.accepted_location_buckets == {
        "nyc-metro",
        "other-columbus",
        "austin-metro",
        "remote-us",
        "remote-global",
    }
    assert f.relocation_buckets == {"austin-metro"}


def test_accept_remote_false_drops_remote_postings() -> None:
    f = _filters({"allowed": ["Chicago"], "accept_remote": False})
    assert f.accepted_location_buckets == {"chicago-metro"}
    remote_job = {
        "title": "Account Executive",
        "company_name": "Acme",
        "location_raw": "Remote - US",
        "location_bucket": "remote-us",
        "remote": 1,
        "comp_max": None,
        "posted_at": None,
    }
    assert _reject_reason(remote_job, f) == "location:remote-us"
    with pytest.raises(ValueError, match="accept_remote must be true or false"):
        _ = _filters({"accept_remote": "yes"}).accept_remote


def test_no_metro_is_hardcoded_in_the_filter() -> None:
    job = {
        "title": "Account Executive",
        "company_name": "Acme",
        "location_raw": "Seattle, WA",
        "location_bucket": "seattle-metro",
        "remote": 0,
        "comp_max": None,
        "posted_at": None,
    }
    assert _reject_reason(job, _filters({"allowed": ["Chicago"]})) == "location:seattle-metro"
    assert _reject_reason(job, _filters({"allowed": ["Bellevue"]})) is None


def test_geo_restrictions_add_your_places_and_their_states() -> None:
    f = _filters(
        {"allowed": ["Columbus, OH", "Portland, OR"]},
        remote_policy={"acceptable_geo_restrictions": ["US"]},
    )
    acc = f.acceptable_geo_restrictions
    assert {"US", "Columbus, OH", "Ohio", "Portland, OR", "Oregon"} <= set(acc)
    # postal codes that are English words never stand alone
    assert "OR" not in acc and "OH" not in acc
    assert geo_restriction_acceptable("Must reside in Ohio", acc)
    assert not geo_restriction_acceptable("Canada or Mexico", acc)


def test_test_profile_geography_comes_from_config() -> None:
    # what filter.ACCEPT_BUCKETS, triage's relocation buckets and _HOME_GEO
    # once hard-coded, the test profile now states in config/filters.yaml
    f = load_filters()
    assert {
        "other-philadelphia",
        "other-bristol",
        "chicago-metro",
        "other-raleigh",
        "remote-us",
        "remote-global",
    } <= f.accepted_location_buckets
    assert "nyc-metro" not in f.accepted_location_buckets
    assert f.relocation_buckets == {"chicago-metro", "other-raleigh"}
    for geo in (
        "Must be based in Pennsylvania",
        "NJ residents only",
        "Pennsylvania or New Jersey",
        "Chicago, IL",
        "North Carolina",
    ):
        assert geo_restriction_acceptable(geo, f.acceptable_geo_restrictions), geo
    assert not geo_restriction_acceptable("Must be based in NY", f.acceptable_geo_restrictions)


def test_commute_table_keys_are_location_buckets(jobkit_home: Path) -> None:
    # the test profile's table is keyed by bucket
    assert triage._commute_minutes("other-philadelphia", {}) == 40
    assert triage._commute_minutes("nyc-metro", {}) == 105
    assert triage._commute_minutes("remote-us", {}) == 0
    assert triage._commute_minutes("chicago-metro", {}) is None
    table = jobkit_home / "config" / "commute_table.yaml"
    # an older table, written before bucket keys, names Manhattan sub-areas:
    # nyc-metro uses its midtown time
    table.write_text("buckets:\n  manhattan-midtown: 40\n  other-columbus: 15\n")
    assert triage._commute_minutes("nyc-metro", {}) == 40
    table.write_text("buckets:\n  nyc-metro: 30\n  manhattan-midtown: 40\n  other-columbus: 15\n")
    assert triage._commute_minutes("nyc-metro", {}) == 30  # a direct entry wins
    assert triage._commute_minutes("other-columbus", {}) == 15


# ---------------------------------------------------------------------------
# User-Agent contact
# ---------------------------------------------------------------------------


def _write_bank(home: Path, email: str) -> None:
    (home / "data" / "answer_bank.yaml").write_text(yaml.safe_dump({"identity": {"email": email}}))


def test_user_agent_contact_order(jobkit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(contact.CONTACT_ENV, raising=False)
    assert contact.user_agent() == contact.GENERIC_USER_AGENT  # nothing configured
    _write_bank(jobkit_home, "jordan.rivera@example.com")
    assert contact.user_agent() == contact.GENERIC_USER_AGENT  # a template placeholder
    _write_bank(jobkit_home, "pat@pat-lee.dev")
    os.utime(jobkit_home / "data" / "answer_bank.yaml", ns=(1, 1))  # new file version
    assert contact.user_agent() == "jobkit/0.1 (job-search tool; contact: pat@pat-lee.dev)"
    monkeypatch.setenv(contact.CONTACT_ENV, "ops@pat-lee.dev")
    assert "contact: ops@pat-lee.dev" in contact.user_agent()  # the env var wins


async def test_board_requests_carry_the_operator_contact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(contact.CONTACT_ENV, "ops@pat-lee.dev")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["User-Agent"])
        return httpx.Response(200, json={"jobs": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await get_json(client, "https://boards-api.greenhouse.io/v1/boards/x/jobs", None)
    assert seen == ["jobkit/0.1 (job-search tool; contact: ops@pat-lee.dev)"]


# ---------------------------------------------------------------------------
# jobkit doctor
# ---------------------------------------------------------------------------


def _site_packages(tmp_path: Path, revision: str = "1234") -> Path:
    sp = tmp_path / "site-packages"
    spec = sp / "playwright" / "driver" / "package" / "browsers.json"
    spec.parent.mkdir(parents=True)
    spec.write_text(json.dumps({"browsers": [{"name": "chromium", "revision": revision}]}))
    return sp


def _env(
    root: Path,
    tmp_path: Path,
    *,
    tools: dict[str, str] | None = None,
    environ: dict[str, str] | None = None,
    platform: str = "darwin",
    python: tuple[int, int] = (3, 12),
) -> doctor.Env:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    found = {"uv": "/opt/bin/uv", "claude": "/opt/bin/claude"} if tools is None else tools
    return doctor.Env(
        root=root.resolve(),
        home=home,
        platform=platform,
        environ=environ or {},
        which=found.get,
        python=python,
        site_packages=_site_packages(tmp_path),
    )


def _ready(root: Path, env: doctor.Env) -> None:
    """Everything doctor checks, in place."""
    chromium = env.home / "Library" / "Caches" / "ms-playwright" / "chromium-1234"
    chromium.mkdir(parents=True)
    for name in ("career_corpus.yaml", "answer_bank.yaml"):
        (root / "data" / name).write_text("identity: {}\n")
    c = q.connect(create=True)
    q.init_db(c)
    c.close()
    server = {"command": "uv", "args": ["run", "--directory", str(root), "jobkit-mcp"]}
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"jobkit": server}}))


def _by_name(report: doctor.Report) -> dict[str, doctor.Check]:
    return {c.name: c for c in report.checks}


def test_doctor_on_a_fresh_clone_lists_each_fix(jobkit_home: Path, tmp_path: Path) -> None:
    report = doctor.run_checks(_env(jobkit_home, tmp_path, tools={"uv": "/opt/bin/uv"}))
    checks = _by_name(report)
    assert not report.ok
    assert checks["python"].ok and checks["uv"].ok
    assert not checks["playwright chromium"].ok
    assert checks["playwright chromium"].fix == "uv run playwright install chromium"
    assert not checks["llm backend"].ok and "ANTHROPIC_API_KEY" in checks["llm backend"].fix
    for name in PERSONAL_CONFIG_FILES:  # the sandbox has the test profile
        assert checks[f"config/{name}"].ok
    corpus = checks["data/career_corpus.yaml"]
    assert not corpus.ok and "set up jobkit" in corpus.fix
    assert checks["database"].fix == "uv run jobkit init"
    mcp = checks["mcp server"]
    assert not mcp.ok and str(jobkit_home.resolve()) in mcp.fix
    assert not checks["contact email"].ok and not checks["contact email"].required
    text = report.render()
    assert "[FAIL] data/career_corpus.yaml" in text and "problem(s) to fix" in text


def test_doctor_ready(jobkit_home: Path, tmp_path: Path) -> None:
    env = _env(jobkit_home, tmp_path)
    _ready(jobkit_home, env)
    report = doctor.run_checks(env)
    assert report.failures == [], report.render()
    assert report.ok and "Ready." in report.render()


def test_doctor_llm_backend_from_api_key(jobkit_home: Path, tmp_path: Path) -> None:
    env = _env(
        jobkit_home, tmp_path, tools={"uv": "/opt/bin/uv"}, environ={"ANTHROPIC_API_KEY": "x"}
    )
    assert doctor.check_llm(env).ok


def test_doctor_flags_python_and_stale_chromium(jobkit_home: Path, tmp_path: Path) -> None:
    env = _env(jobkit_home, tmp_path, python=(3, 13))
    assert not doctor.check_python(env).ok
    (env.home / "Library" / "Caches" / "ms-playwright" / "chromium-1000").mkdir(parents=True)
    check = doctor.check_playwright(env)
    assert not check.ok and "not the revision" in check.detail


def test_doctor_refuses_an_icloud_checkout(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / "Desktop" / "jobkit"
    root.mkdir(parents=True)
    env = doctor.Env(
        root=root,
        home=home,
        platform="darwin",
        environ={},
        which=lambda name: None,
        python=(3, 12),
        site_packages=None,
    )
    check = doctor.check_location(env)
    assert not check.ok and "hidden flag" in check.detail
    assert "chflags -R nohidden" in check.fix and "~/code/jobkit" in check.fix


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS file flags")
def test_doctor_finds_a_hidden_pth(jobkit_home: Path, tmp_path: Path) -> None:
    env = _env(jobkit_home, tmp_path)
    assert env.site_packages is not None
    pth = env.site_packages / "_jobkit.pth"
    pth.write_text(str(REPO_ROOT / "src"))
    assert doctor.check_hidden_pth(env) == doctor.Check("venv .pth files", True, "none hidden")
    os.chflags(pth, statmod.UF_HIDDEN)
    check = doctor.check_hidden_pth(env)
    assert check is not None and not check.ok and "_jobkit.pth" in check.detail


def test_doctor_missing_and_broken_config(jobkit_home: Path, tmp_path: Path) -> None:
    (jobkit_home / "config" / "targets.yaml").unlink()
    (jobkit_home / "config" / "filters.yaml").write_text(
        "filters:\n  tracks:\n    - { id: x, title_regex: '(' }\n"
    )
    checks = _by_name(doctor.run_checks(_env(jobkit_home, tmp_path)))
    targets = checks["config/targets.yaml"]
    assert not targets.ok and targets.detail == "not found"
    assert "cp config/targets.example.yaml config/targets.yaml" in targets.fix
    assert "git show" not in targets.fix  # a fresh clone: never hand it someone's history
    filters = checks["config/filters.yaml"]
    assert not filters.ok and "bad title_regex" in filters.detail


def test_doctor_warns_about_an_allowed_place_without_a_commute_time(
    jobkit_home: Path, tmp_path: Path
) -> None:
    """A place outside the metro table buckets as other-<town>: a commute table
    with no entry for it rejects every hybrid posting there as
    sanity:commute:unknown. Doctor names the bucket to add (a warning)."""
    env = _env(jobkit_home, tmp_path)
    check = doctor.check_commute_coverage(env)
    # the test profile allows two places its table does not cover (the two
    # states); the towns it lists and the relocation cities (commute exempt)
    # are fine
    assert check is not None and not check.ok and not check.required
    assert "other-pennsylvania" in check.detail and "other-newjersey" in check.detail
    for covered in ("other-bristol", "other-philadelphia", "chicago", "raleigh"):
        assert covered not in check.detail, covered
    assert "  other-pennsylvania: <minutes>" in check.fix.splitlines()
    table = jobkit_home / "config" / "commute_table.yaml"
    data = yaml.safe_load(table.read_text())
    data["buckets"] |= {"other-pennsylvania": 50, "other-newjersey": 30}
    table.write_text(yaml.safe_dump(data))
    check = doctor.check_commute_coverage(env)
    assert check is not None and check.ok
    assert triage._commute_minutes("other-pennsylvania", {}) == 50  # the remote check agrees


def test_doctor_commute_check_on_templates_and_missing_files(
    jobkit_home: Path, tmp_path: Path
) -> None:
    env = _env(jobkit_home, tmp_path)
    for name in ("filters", "commute_table"):
        (jobkit_home / "config" / f"{name}.yaml").write_text(
            (TEMPLATES / f"{name}.example.yaml").read_text()
        )
    check = doctor.check_commute_coverage(env)
    assert check is None or check.ok
    (jobkit_home / "config" / "commute_table.yaml").unlink()
    assert doctor.check_commute_coverage(env) is None  # check_config_files reports it


def test_doctor_never_offers_config_from_git_history(
    jobkit_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A brand-new clone runs `jobkit init` before the interview, so a database
    says nothing about whose checkout this is. History holds another person's
    config; the only fixes are the interview and the template."""

    def no_subprocess(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"doctor ran a command: {args}")

    monkeypatch.setattr(subprocess, "run", no_subprocess)
    monkeypatch.setattr(subprocess, "Popen", no_subprocess)
    (jobkit_home / "config" / "autoapprove.yaml").unlink()
    c = q.connect(create=True)  # `jobkit init` already ran, as README's setup order has it
    q.init_db(c)
    c.close()
    report = doctor.run_checks(_env(jobkit_home, tmp_path))
    fix = _by_name(report)["config/autoapprove.yaml"].fix
    assert fix.splitlines() == [
        doctor.INTERVIEW,
        "or: cp config/autoapprove.example.yaml config/autoapprove.yaml  (then edit it)",
    ]
    text = report.render()
    assert "git show" not in text and "git log" not in text


def test_doctor_mcp_config_pointing_elsewhere(jobkit_home: Path, tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    server = {"command": "uv", "args": ["run", "--directory", str(other), "jobkit-mcp"]}
    (jobkit_home / ".mcp.json").write_text(json.dumps({"mcpServers": {"jobkit": server}}))
    check = doctor.check_mcp(_env(jobkit_home, tmp_path))
    assert not check.ok and "not this checkout" in check.detail


def test_doctor_cli_and_mcp(jobkit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # the real machine's tools are only looked up; the sandbox has no corpus
    result = CliRunner().invoke(cli.app, ["doctor"])
    assert result.exit_code == 1
    assert "jobkit doctor — checkout" in result.output
    assert "data/career_corpus.yaml" in result.output
    text = mcp_server.doctor()
    assert text.startswith("jobkit doctor — checkout") and "fix:" in text


def test_doctor_never_raises(
    jobkit_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def check_db(env: doctor.Env) -> doctor.Check:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(doctor, "check_db", check_db)
    checks = _by_name(doctor.run_checks(_env(jobkit_home, tmp_path)))
    assert not checks["db"].ok and "check crashed: disk I/O error" in checks["db"].detail
