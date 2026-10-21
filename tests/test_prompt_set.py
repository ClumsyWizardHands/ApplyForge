"""The prompt_set switch and the sales path of an existing config.

Golden tests: with `prompt_set` absent (the sales test profile in
tests/fixtures/config/, which has no prompt_set, no tracking block, no
per-track min_score/emphasis/pivot and no new comp_sanity knobs), the rendered
score prompt, the scoring profile, the comp-sanity flags, the queue gate and
the standing-rule verdicts are byte-identical to snapshots taken from the code
before prompt_set existed (tests/fixtures/golden/). Regenerate only on a
deliberate change to the sales path: JOBKIT_REGEN_GOLDEN=1 pytest
tests/test_prompt_set.py.

Offline: every LLM call is faked at llm.call_json."""

from __future__ import annotations

import copy
import datetime
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from jobkit import llm
from jobkit.config import (
    AdzunaPlace,
    CompSanity,
    Filters,
    GhostAfter,
    UsajobsPlace,
    load_filters,
    parse_adzuna_country,
    parse_adzuna_locations,
    parse_usajobs_locations,
)
from jobkit.pipeline import rescore, triage
from jobkit.rules import _title_class, run_standing_rules, unknown_title_classes
from tests.conftest import FIXTURES
from tests.test_claims import CORPUS
from tests.test_filter import _insert

GOLDEN = FIXTURES / "golden"
REGEN = os.environ.get("JOBKIT_REGEN_GOLDEN") == "1"
TODAY = datetime.date(2026, 10, 8)

JD = "<p>" + "You will own a book of accounts, run discovery and close new business. " * 12 + "</p>"


def _golden(name: str, value: Any) -> None:
    """Compare `value` (JSON-able, or a str) with tests/fixtures/golden/<name>;
    with JOBKIT_REGEN_GOLDEN=1, write it instead."""
    path = GOLDEN / name
    text = value if isinstance(value, str) else json.dumps(value, indent=1, sort_keys=True) + "\n"
    if REGEN:
        GOLDEN.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return
    assert path.is_file(), f"no golden file {path}; run with JOBKIT_REGEN_GOLDEN=1"
    assert text == path.read_text(), f"{name} differs from its golden snapshot"


class _FixedDate(datetime.date):
    @classmethod
    def today(cls) -> _FixedDate:
        return cls(TODAY.year, TODAY.month, TODAY.day)


@pytest.fixture()
def fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """triage reads datetime.date.today(); pin it so the profile's as-of month
    and years in role are stable."""

    class _DT:
        date = _FixedDate

    monkeypatch.setattr(triage, "datetime", _DT)


class FakeCalls:
    """Replaces llm.call_json: records (stage, cache_key, prompt), answers from
    `replies[stage]` (a dict, or a function of the prompt)."""

    def __init__(self, replies: dict[str, Any]) -> None:
        self.replies = replies
        self.calls: list[dict[str, str]] = []

    def __call__(
        self, conn: sqlite3.Connection, stage: str, prompt: str, *, cache_key: str | None = None
    ) -> dict[str, Any]:
        self.calls.append({"stage": stage, "cache_key": cache_key or "", "prompt": prompt})
        reply = self.replies[stage]
        out = reply(prompt) if callable(reply) else reply
        return dict(out)


def _corpus_with_older_role() -> dict[str, Any]:
    c = copy.deepcopy(CORPUS)
    c["roles"].append(
        {
            "id": "role_old",
            "company": "Halvorsen Outfitters",
            "title": "Store Manager",
            "start": "2012-06",
            "end": "2016-08",
        }
    )
    c["bullets"].append(
        {
            "id": "b_067",
            "role_id": "role_old",
            "metric_confidence": "estimate",
            "text_short": "Ran a 14-person outdoor-gear store; 6–8 seasonal hires each spring.",
        }
    )
    return c


SCORE_JOBS = (
    ("Enterprise Account Executive", "Acme", ["3+ yrs closing new business", "Salesforce"]),
    ("Director of Sales", "Initech", ["5+ yrs leading AEs", "board-level forecasting"]),
    ("Revenue Operations Manager", "Globex", ["Salesforce admin", "deal desk"]),
)


def _score_jobs(conn: sqlite3.Connection) -> list[int]:
    ids = []
    for i, (title, company, must) in enumerate(SCORE_JOBS):
        jid = _insert(
            conn,
            external_id=f"g{i}",
            title=title,
            company_name=company,
            jd_html=JD,
            jd_hash=f"golden-{i}",
        )
        conn.execute("UPDATE jobs SET status='filtered' WHERE id=?", (jid,))
        conn.execute(
            "INSERT INTO job_requirements (job_id, must_have, nice_to_have, implied, "
            "disqualifier) VALUES (?, ?, '[]', '[]', '[]')",
            (jid, json.dumps(must)),
        )
        ids.append(jid)
    conn.commit()
    return ids


SCORE_REPLY = {"score": 70, "rationale": "a. b.", "gaps": []}


# ---------------------------------------------------------------------------
# golden: the sales path with prompt_set absent
# ---------------------------------------------------------------------------


def test_sales_fixture_has_no_new_keys() -> None:
    """The golden tests prove an existing user's path only while the fixture,
    a config written before these keys existed, carries none of them."""
    f = load_filters()
    assert "prompt_set" not in f.raw and "tracking" not in f.raw
    for t in f.raw["filters"]["tracks"]:
        assert not {"pivot", "min_score", "emphasis"} & set(t), t
    assert not {"underpriced_below", "ote_unclear_from", "ote_check"} & set(f.raw["comp_sanity"])


def test_golden_profile_summary() -> None:
    filters = load_filters()
    out = {
        "corpus": triage._profile_summary(corpus=CORPUS, filters=filters, today=TODAY),
        "older_role": triage._profile_summary(
            corpus=_corpus_with_older_role(), filters=filters, today=TODAY
        ),
    }
    _golden("sales_profile_summary.json", out)


def test_golden_score_prompts(
    conn: sqlite3.Connection,
    jobkit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixed_today: None,
) -> None:
    """run_score (triage) and score_rows with no prompt passed (rescore and
    extract_and_score) send byte-identical prompts under identical caller cache
    keys."""
    (jobkit_home / "data" / "career_corpus.yaml").write_text(yaml.safe_dump(CORPUS))
    fake = FakeCalls({"score": SCORE_REPLY})
    monkeypatch.setattr(llm, "call_json", fake)
    ids = _score_jobs(conn)
    assert triage.run_score(conn) == 3
    run_score_calls = sorted(fake.calls, key=lambda c: c["cache_key"])
    fake.calls.clear()
    rows = conn.execute(
        """SELECT j.*, r.must_have, r.nice_to_have, r.implied, r.disqualifier
           FROM jobs j JOIN job_requirements r ON r.job_id = j.id ORDER BY j.id"""
    ).fetchall()
    assert set(triage.score_rows(conn, rows, stage_label="rescore")) == set(ids)
    default_calls = sorted(fake.calls, key=lambda c: c["cache_key"])
    assert default_calls == run_score_calls
    _golden("sales_score_prompts.json", run_score_calls)
    events = [
        json.loads(r["detail"])
        for r in conn.execute(
            "SELECT detail FROM events WHERE kind = ? ORDER BY id", (triage.SCORE_PROFILE_EVENT,)
        )
    ]
    # the profile hash depends on the corpus text only; the rest is pinned,
    # in the original shape (only a score_fit_general event names its prompt)
    assert all(set(e) == {"profile_hash", "prompt_version"} for e in events)
    _golden(
        "sales_score_events.json",
        [{k: v for k, v in e.items() if k != "profile_hash"} for e in events],
    )


COMP_ROWS: tuple[tuple[str, dict[str, Any], dict[str, Any]], ...] = (
    # (title, job columns, canned comp reply)
    (
        "Enterprise Account Executive",
        {"comp_min": 90000, "comp_max": 150000, "location_raw": "Remote, US"},
        {"demand_score": 80, "breadth_roles": 1, "comp_basis": "base", "ote_realism": "n/a"},
    ),
    (
        "Director of Sales",
        {"comp_min": 200000, "comp_max": 260000, "location_raw": "Chicago, IL", "remote": 0},
        {"demand_score": 60, "breadth_roles": 1, "comp_basis": "unclear", "ote_realism": "n/a"},
    ),
    (
        "Manager, Sales Development",
        {"comp_min": 100000, "comp_max": 200000, "location_raw": "Austin, TX", "remote": 0},
        {
            "demand_score": 70,
            "breadth_roles": 3,
            "comp_basis": "ote",
            "ote_realism": "inflated",
            "ote_rationale": "uncapped commission doing all the work",
        },
    ),
    (
        "Account Executive, Mid-Market",
        {"comp_min": None, "comp_max": None, "location_raw": "New York, NY", "remote": 0},
        {"demand_score": 50, "breadth_roles": 1, "comp_basis": "unclear", "ote_realism": "n/a"},
    ),
    (
        "Head of Revenue",
        {"comp_min": 180000, "comp_max": 210000, "location_raw": "Remote, US"},
        {"demand_score": 90, "breadth_roles": 2, "comp_basis": "base", "ote_realism": "realistic"},
    ),
)


def _comp_rows(
    conn: sqlite3.Connection, rows: tuple[tuple[str, dict[str, Any], dict[str, Any]], ...]
) -> dict[str, Any]:
    by_hash: dict[str, dict[str, Any]] = {}
    for i, (title, cols, reply) in enumerate(rows):
        jid = _insert(
            conn,
            external_id=f"c{i}",
            title=title,
            jd_html=JD,
            jd_hash=f"comp-{i}",
            **{"remote": 1, **cols},
        )
        conn.execute("UPDATE jobs SET status='filtered' WHERE id=?", (jid,))
        conn.execute("INSERT INTO sanity (job_id, flags) VALUES (?, '[]')", (jid,))
        by_hash[f"comp-{i}"] = reply
    conn.commit()
    return by_hash


def run_comp_fixture(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    rows: tuple[tuple[str, dict[str, Any], dict[str, Any]], ...] = COMP_ROWS,
) -> dict[str, Any]:
    """Run the comp-sanity stage over `rows`; {title: {flags, verdict}}."""
    by_hash = _comp_rows(conn, rows)

    def fake(
        c: sqlite3.Connection, stage: str, prompt: str, *, cache_key: str | None = None
    ) -> dict[str, Any]:
        assert stage == "comp" and cache_key
        return dict(by_hash[cache_key.split(":")[1]])

    monkeypatch.setattr(llm, "call_json", fake)
    triage.run_comp_check(conn)
    out: dict[str, Any] = {}
    for r in conn.execute(
        "SELECT j.title, s.flags, s.value_verdict FROM jobs j JOIN sanity s ON s.job_id = j.id"
    ):
        out[r["title"]] = {"flags": json.loads(r["flags"]), "verdict": r["value_verdict"]}
    return out


def test_golden_comp_sanity_flags(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _golden("sales_comp_sanity.json", run_comp_fixture(conn, monkeypatch))


def test_golden_queue_gate(conn: sqlite3.Connection) -> None:
    for i, (title, score) in enumerate(
        (
            ("Enterprise Account Executive", 64),
            ("Director of Sales", 65),
            ("Manager, Sales Development", 66),
            ("Revenue Operations Manager", 40),
            ("Account Executive", 90),
        )
    ):
        jid = _insert(conn, external_id=f"q{i}", title=title, jd_html=JD, jd_hash=f"q-{i}")
        conn.execute("UPDATE jobs SET status='filtered' WHERE id=?", (jid,))
        conn.execute("INSERT INTO scores (job_id, fit_score) VALUES (?, ?)", (jid, score))
    conn.commit()
    triage.run_queue(conn)
    out = {
        r["title"]: {"status": r["status"], "reasons": r["reasons"]}
        for r in conn.execute(
            """SELECT j.title, j.status, (SELECT group_concat(layer || ':' || reason, '|')
                 FROM rejections x WHERE x.job_id = j.id) AS reasons FROM jobs j"""
        )
    }
    _golden("sales_queue_gate.json", out)


RULE_TITLES = (
    "Enterprise Account Executive",
    "Founding Account Executive",
    "First AE",
    "Senior Sales Executive",
    "Account Executive (SDR to AE track)",
    "SDR",
    "Sales Development Representative",
    "Manager, Sales Development",
    "BDR Team Lead",
    "Director of Sales",
    "VP of Revenue",
    "Head of Sales",
    "Revenue Operations Manager",
    "Deal Desk Analyst",
    "Software Engineer",
)


def test_golden_title_classes() -> None:
    """Sales classes stay as they were (carve-out included). The one intended
    change: a title whose track is neither an SDR/BDR track nor
    account_executive now gets its track id where it used to get None
    (sales_exec in this fixture). Nothing else moves."""
    filters = load_filters()
    now = {t: _title_class(t, filters) for t in RULE_TITLES}
    path = GOLDEN / "sales_title_classes.json"
    if REGEN:
        _golden("sales_title_classes.json", now)
        return
    before = json.loads(path.read_text())
    for title, cls in now.items():
        if cls == before[title]:
            continue
        track = filters.track_for(title)
        assert before[title] is None and cls == track, (title, before[title], cls)
        assert track not in {"account_executive", "sdr_bdr", "sdr_bdr_manager"}, title


def test_golden_standing_rule_verdicts(conn: sqlite3.Connection) -> None:
    """The fixture autoapprove.yaml (title_class: account_executive only)
    decides exactly as before."""
    cases = (
        ("Enterprise Account Executive", 1, 90, 30, 170000, 200000),
        ("Enterprise Account Executive", 3, 90, 30, 170000, 200000),
        ("Account Executive", 2, 80, 30, 100000, 150000),
        ("Founding Account Executive", 2, 80, 30, 100000, 150000),
        ("Director of Sales", 1, 85, 40, 200000, 260000),
        ("Director of Sales", 3, 70, 40, 200000, 260000),
        ("Head of Sales", 2, 96, 10, 210000, 260000),
        ("Manager, Sales Development", 2, 75, 50, None, None),
    )
    for i, (title, tier, score, friction, cmin, cmax) in enumerate(cases):
        conn.execute(
            "INSERT OR IGNORE INTO companies (id, name, normalized_name, tier) VALUES (?, ?, ?, ?)",
            (100 + i, f"Co{i}", f"co{i}", tier),
        )
        jid = _insert(
            conn,
            external_id=f"s{i}",
            company_id=100 + i,
            title=title,
            comp_min=cmin,
            comp_max=cmax,
            jd_html=JD,
        )
        conn.execute("UPDATE jobs SET status='queued' WHERE id=?", (jid,))
        conn.execute("INSERT INTO scores (job_id, fit_score) VALUES (?, ?)", (jid, score))
        conn.execute("INSERT INTO sanity (job_id, flags) VALUES (?, '[]')", (jid,))
        conn.execute(
            "INSERT INTO form_specs (job_id, friction_score) VALUES (?, ?)", (jid, friction)
        )
    conn.commit()
    stats = run_standing_rules(conn)
    verdicts = [
        {
            "title": r["title"],
            "company": r["company_name"],
            "status": r["status"],
            "decision": r["decision"],
            "reason": r["reason"],
        }
        for r in conn.execute(
            """SELECT j.title, c.name AS company_name, j.status, d.decision, d.reason
               FROM jobs j JOIN companies c ON c.id = j.company_id
               LEFT JOIN decisions d ON d.job_id = j.id ORDER BY j.id"""
        )
    ]
    _golden("sales_rule_verdicts.json", {"stats": stats, "verdicts": verdicts})


# ---------------------------------------------------------------------------
# the config contract: prompt_set, per-track keys, tracking, comp_sanity, feeds
# ---------------------------------------------------------------------------

PERSONAS = FIXTURES / "personas"


def persona(name: str) -> Filters:
    return Filters(raw=yaml.safe_load((PERSONAS / f"{name}.yaml").read_text()))


def use_persona(jobkit_home: Path, name: str) -> Filters:
    """Make the persona the sandbox's config/filters.yaml (for code that loads
    the config itself) and return it."""
    (jobkit_home / "config" / "filters.yaml").write_text((PERSONAS / f"{name}.yaml").read_text())
    return load_filters()


def _with(**raw: Any) -> Filters:
    base = yaml.safe_load((FIXTURES / "config" / "filters.yaml").read_text())
    base.update(raw)
    return Filters(raw=base)


def test_prompt_set_values() -> None:
    assert load_filters().prompt_set == "sales"
    assert not load_filters().prompt_set_explicit
    assert _with(prompt_set="general").prompt_set == "general"
    assert _with(prompt_set="sales").prompt_set == "sales"
    with pytest.raises(ValueError, match="prompt_set must be one of"):
        _with(prompt_set="nursing")
    for name in ("engineer", "rn_columbus", "teacher_to_id"):
        assert persona(name).prompt_set == "general", name


def test_track_options_parse_and_fail_loudly() -> None:
    t = persona("teacher_to_id").track("instructional_designer")
    assert t is not None and t.pivot and t.min_score == 50
    assert t.emphasis and t.emphasis.startswith("Units I designed")
    plain = load_filters().track("account_executive")
    assert plain is not None
    assert (plain.pivot, plain.min_score, plain.emphasis) == (False, None, None)

    def tracks(**extra: Any) -> Filters:
        return Filters(raw={"filters": {"tracks": [{"id": "x", "title_regex": "(?i)x", **extra}]}})

    for bad, msg in (
        ({"pivot": "yes"}, "pivot must be true or false"),
        ({"min_score": 101}, "min_score must be a whole number 0-100"),
        ({"min_score": "60"}, "min_score must be a whole number 0-100"),
        ({"emphasis": "a\nb"}, "emphasis must be one line"),
        ({"emphasis": "x" * 201}, "emphasis must be one line"),
    ):
        with pytest.raises(ValueError, match=msg):
            tracks(**bad)


def test_min_score_for() -> None:
    rn = persona("rn_columbus")
    assert rn.min_score_for("registered_nurse") == 55
    assert rn.min_score_for("nurse_educator") == 65
    assert rn.min_score_for(None) == 65
    assert rn.min_score_for_title("Clinical Nurse II") == 55
    assert rn.min_score_for_title("Phlebotomist") == 65
    sales = load_filters()
    assert {sales.min_score_for(t) for t in sales.track_ids} == {65}


def test_ghost_after_days_sources() -> None:
    # the sales fixture keeps the legacy followups block, as older configs do
    legacy = load_filters().ghost_after
    assert (legacy.days, legacy.source) == (30, "followups")
    blocks = yaml.safe_load((FIXTURES / "config" / "filters.yaml").read_text())
    blocks["followups"] = [{"days_after_submit": 45, "kind": "close_as_ghosted"}]
    assert Filters(raw=blocks).ghost_after_days == 45
    blocks["tracking"] = {"ghost_after_days": 20}
    assert Filters(raw=blocks).ghost_after == GhostAfter(20, "tracking")
    assert persona("engineer").ghost_after_days == 21
    assert persona("teacher_to_id").ghost_after == GhostAfter(30, "default")
    for bad in (0, 400, "30", True):
        with pytest.raises(ValueError, match="ghost_after_days"):
            _with(tracking={"ghost_after_days": bad})


def test_comp_sanity_defaults_are_the_old_constants() -> None:
    cs = load_filters().comp_sanity
    assert (cs.underpriced_below, cs.ote_unclear_from, cs.ote_check) == (160000, 200000, "always")
    assert (cs.max_range_width_pct, cs.flag_missing_range_when_required) == (0.6, True)
    assert Filters(raw={}).comp_sanity == CompSanity()
    eng = persona("engineer").comp_sanity
    assert (eng.underpriced_below, eng.ote_check) == (187500, "ote_tracks")
    with pytest.raises(ValueError, match="ote_check"):
        _with(comp_sanity={"ote_check": "sometimes"})
    with pytest.raises(ValueError, match="underpriced_below"):
        _with(comp_sanity={"underpriced_below": -1})


def test_feed_location_keys() -> None:
    rn = persona("rn_columbus")
    assert rn.adzuna_locations == (AdzunaPlace("Columbus, OH", 40),)
    assert rn.adzuna_country == "us"
    assert rn.usajobs_locations == (UsajobsPlace("Columbus, Ohio", 25),)
    sales = load_filters()
    assert sales.adzuna_locations == () and sales.usajobs_locations == ()
    assert sales.adzuna_country == "us"
    assert parse_adzuna_locations({"locations": [{"where": "Dayton, OH"}]}) == (
        AdzunaPlace("Dayton, OH", 40),
    )
    assert parse_usajobs_locations(None) == ()
    with pytest.raises(ValueError, match="needs a where"):
        _with(feeds={"adzuna": {"locations": [{"distance_km": 10}]}})
    with pytest.raises(ValueError, match="radius_miles"):
        _with(feeds={"usajobs": {"locations": [{"name": "Dayton, OH", "radius_miles": 900}]}})
    with pytest.raises(ValueError, match="two-letter"):
        parse_adzuna_country({"country": "usa"})


def test_has_sales_track() -> None:
    assert load_filters().has_sales_track
    for name in ("engineer", "rn_columbus", "teacher_to_id"):
        assert not persona(name).has_sales_track, name


# ---------------------------------------------------------------------------
# the general score prompt
# ---------------------------------------------------------------------------


def test_general_score_prompt_contract() -> None:
    p = llm.PromptFile("score_fit_general")
    assert p.version == "1.0" and p.stage == "score"
    for slot in ("{profile}", "{title}", "{company}", "{requirements}", "{pivot_note}"):
        assert p.body.count(slot) == 1, slot
    # same output contract as score_fit.md
    assert '"score": 0-100' in p.body and '"rationale": "exactly two sentences"' in p.body
    assert "starts with 'overqualified:'" in p.body
    # the career-change gap sits after real gaps and before an overqualified one
    assert 'after the unmet must-haves and before any "overqualified:"' in p.body
    for field_example in ("(software)", "(nursing)", "(teaching)", "(sales)"):
        assert field_example in p.body, field_example
    assert "CAREER-CHANGE BAND" in p.body and "Do not cluster at 70-85" in p.body
    sales = llm.PromptFile("score_fit")
    assert sales.version == "1.5"  # untouched


def test_render_once_fills_every_slot_in_one_pass() -> None:
    p = llm.PromptFile("score_fit_general")
    out = triage.render_once(
        p,
        profile="I wrote {title} on a slide and {pivot_note} on another",
        title="Instructional Designer",
        company="Acme {company}",
        requirements="[]",
        pivot_note="none",
    )
    assert "I wrote {title} on a slide and {pivot_note} on another" in out
    assert "COMPANY: Acme {company}" in out
    assert "JOB TITLE: Instructional Designer" in out
    with pytest.raises(ValueError, match="pivot_note"):
        triage.render_once(p, profile="", title="", company="", requirements="")


TEACHER_CORPUS: dict[str, Any] = {
    "roles": [
        {
            "id": "r1",
            "company": "Maple Grove Middle School",
            "title": "Science Teacher",
            "start": "2018-08",
        },
        {
            "id": "r0",
            "company": "Brightway Tutoring",
            "title": "Tutor",
            "start": "2016-01",
            "end": "2018-06",
        },
    ],
    "bullets": [
        {
            "id": "b1",
            "role_id": "r1",
            "text_short": "Designed 6 units from state learning objectives; wrote the tests.",
        },
        {"id": "b2", "role_id": "r1", "text_short": "Ran three courses in the school's LMS."},
    ],
}

ENGINEER_CORPUS: dict[str, Any] = {
    "roles": [
        {"id": "e2", "company": "Northwind", "title": "Software Engineer", "start": "2021-03"},
        {
            "id": "e1",
            "company": "Contoso",
            "title": "QA Analyst",
            "start": "2018-01",
            "end": "2021-03",
        },
        {
            "id": "e0",
            "company": "Fabrikam Outlet",
            "title": "Sales Associate",
            "start": "2016-01",
            "end": "2018-01",
        },
    ],
    "bullets": [{"id": "x1", "role_id": "e2", "text_short": "Built the billing API in Go."}],
}


def test_engineer_profile_counts_only_target_track_titles() -> None:
    eng = persona("engineer")
    profile = triage._profile_summary(corpus=ENGINEER_CORPUS, filters=eng, today=TODAY)
    # 2021-03 to 2026-10 is 67 months; QA and the retail sales job are not counted
    assert (
        "YEARS IN ROLES ON THE TARGET TRACKS: 5.6 "
        "(titles counted: Software Engineer [backend_engineer])" in profile
    )
    assert "TOTAL YEARS IN SALES ROLES" not in profile
    # the same corpus under the sales path counts the retail "Sales Associate"
    sales = triage._profile_summary(corpus=ENGINEER_CORPUS, filters=load_filters(), today=TODAY)
    assert "TOTAL YEARS IN SALES ROLES: 2.0 (titles counted: Sales Associate)" in sales


def _general_score(
    conn: sqlite3.Connection,
    jobkit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    corpus: dict[str, Any],
    titles: tuple[str, ...],
) -> FakeCalls:
    use_persona(jobkit_home, name)
    (jobkit_home / "data" / "career_corpus.yaml").write_text(yaml.safe_dump(corpus))
    fake = FakeCalls({"score": SCORE_REPLY})
    monkeypatch.setattr(llm, "call_json", fake)
    for i, title in enumerate(titles):
        jid = _insert(conn, external_id=f"p{i}", title=title, jd_html=JD, jd_hash=f"p-{i}")
        conn.execute("UPDATE jobs SET status='filtered' WHERE id=?", (jid,))
        conn.execute(
            "INSERT INTO job_requirements (job_id, must_have) VALUES (?, ?)",
            (jid, json.dumps(["write assessments"])),
        )
    conn.commit()
    return fake


def test_teacher_pivot_track_gets_the_career_change_note(
    conn: sqlite3.Connection,
    jobkit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixed_today: None,
) -> None:
    fake = _general_score(
        conn,
        jobkit_home,
        monkeypatch,
        "teacher_to_id",
        TEACHER_CORPUS,
        ("Instructional Designer", "Science Teacher"),
    )
    assert triage.run_score(conn) == 2
    by_title = {c["prompt"].split("JOB TITLE: ")[1].split("\n")[0]: c for c in fake.calls}
    pivot = by_title["Instructional Designer"]
    assert (
        "CAREER-CHANGE NOTE: this job is on the candidate's instructional_designer track"
        in (pivot["prompt"])
    )
    assert pivot["cache_key"] == "score:p-0:score_fit_general:1.0"
    teacher = by_title["Science Teacher"]
    assert "CAREER-CHANGE NOTE: none\n" in teacher["prompt"]
    for c in fake.calls:
        assert "ANCHOR 90" not in c["prompt"]  # the sales anchors are not sent
        assert "{" + "pivot_note}" not in c["prompt"]
        assert "YEARS IN ROLES ON THE TARGET TRACKS" in c["prompt"]
    events = [
        json.loads(r["detail"])
        for r in conn.execute("SELECT detail FROM events WHERE kind = 'score_profile'")
    ]
    assert {(e["prompt"], e["prompt_version"]) for e in events} == {("score_fit_general", "1.0")}


def test_rescore_and_extract_and_score_use_the_general_prompt(
    conn: sqlite3.Connection,
    jobkit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixed_today: None,
) -> None:
    """score_rows chooses the prompt, so the two callers that pass none
    (rescore, extract_and_score) score with the active prompt set too."""
    fake = _general_score(
        conn, jobkit_home, monkeypatch, "teacher_to_id", TEACHER_CORPUS, ("Curriculum Designer",)
    )
    rows = conn.execute(
        """SELECT j.*, r.must_have, r.nice_to_have, r.implied, r.disqualifier
           FROM jobs j JOIN job_requirements r ON r.job_id = j.id"""
    ).fetchall()
    triage.score_rows(conn, rows, stage_label="rescore")
    assert "score_fit_general" in fake.calls[0]["cache_key"]
    assert "CAREER-CHANGE NOTE: this job is on" in fake.calls[0]["prompt"]


def test_rn_track_min_score_overrides_the_global_gate(
    conn: sqlite3.Connection, jobkit_home: Path
) -> None:
    rn = use_persona(jobkit_home, "rn_columbus")
    for i, (title, score) in enumerate((("Clinical Nurse II", 58), ("Nurse Educator", 58))):
        jid = _insert(conn, external_id=f"n{i}", title=title, jd_html=JD, jd_hash=f"n-{i}")
        conn.execute("UPDATE jobs SET status='filtered' WHERE id=?", (jid,))
        conn.execute("INSERT INTO scores (job_id, fit_score) VALUES (?, ?)", (jid, score))
    conn.commit()
    assert triage.run_queue(conn, rn) == 1
    rows = {
        r["title"]: (r["status"], r["reason"])
        for r in conn.execute(
            """SELECT j.title, j.status, x.reason FROM jobs j
               LEFT JOIN rejections x ON x.job_id = j.id"""
        )
    }
    assert rows["Clinical Nurse II"] == ("queued", None)
    assert rows["Nurse Educator"] == ("rejected", "score_below_surface:58<65")


def test_ote_tracks_suppresses_ote_flags_for_a_base_paid_track(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ote_check: ote_tracks — an engineer's $230K posting with an unclear
    basis, or one the model calls inflated, raises no OTE flag; the sales
    profile's OTE track still does."""
    use_persona(jobkit_home, "engineer")
    by_hash = {}
    for i, (title, reply) in enumerate(
        (
            ("Senior Backend Engineer", {"comp_basis": "unclear", "ote_realism": "n/a"}),
            ("Platform Engineer", {"comp_basis": "ote", "ote_realism": "inflated"}),
        )
    ):
        jid = _insert(
            conn,
            external_id=f"o{i}",
            title=title,
            comp_min=200000,
            comp_max=230000,
            jd_html=JD,
            jd_hash=f"o-{i}",
        )
        conn.execute("UPDATE jobs SET status='filtered' WHERE id=?", (jid,))
        conn.execute("INSERT INTO sanity (job_id, flags) VALUES (?, '[]')", (jid,))
        by_hash[f"o-{i}"] = {"demand_score": 50, "breadth_roles": 1, **reply}
    conn.commit()

    def fake(
        c: sqlite3.Connection, stage: str, prompt: str, *, cache_key: str | None = None
    ) -> dict[str, Any]:
        assert cache_key
        return dict(by_hash[cache_key.split(":")[1]])

    monkeypatch.setattr(llm, "call_json", fake)
    triage.run_comp_check(conn)
    flags = [json.loads(r["flags"]) for r in conn.execute("SELECT flags FROM sanity")]
    assert flags == [[], []]


def test_ote_tracks_keeps_ote_flags_for_an_ote_track(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = yaml.safe_load((FIXTURES / "config" / "filters.yaml").read_text())
    raw["comp_sanity"]["ote_check"] = "ote_tracks"
    (jobkit_home / "config" / "filters.yaml").write_text(yaml.safe_dump(raw))
    ote_ae = (
        "Account Executive",
        {"comp_min": 120000, "comp_max": 240000},
        {
            "demand_score": 50,
            "breadth_roles": 1,
            "comp_basis": "ote",
            "ote_realism": "inflated",
            "ote_rationale": "half the OTE is uncapped accelerators",
        },
    )
    flags = run_comp_fixture(conn, monkeypatch, (*COMP_ROWS, ote_ae))
    # account_executive has an OTE floor: its flag stays
    assert (
        "ote-unrealistic: half the OTE is uncapped accelerators"
        in (flags["Account Executive"]["flags"])
    )
    # Manager, Sales Development is on sdr_bdr_manager (a base floor): no OTE flag now
    assert not any(f.startswith("ote-") for f in flags["Manager, Sales Development"]["flags"])
    # Director of Sales is sales_exec (base): its ote-basis-unclear is gone too
    assert flags["Director of Sales"]["flags"] == []


def test_underpriced_threshold_is_a_knob(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = yaml.safe_load((FIXTURES / "config" / "filters.yaml").read_text())
    raw["comp_sanity"]["underpriced_below"] = 140000
    (jobkit_home / "config" / "filters.yaml").write_text(yaml.safe_dump(raw))
    flags = run_comp_fixture(conn, monkeypatch)
    # $150K ceiling is no longer under the threshold
    assert flags["Enterprise Account Executive"]["verdict"] == "fair"


def test_ote_unclear_threshold_is_a_knob(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Director of Sales publishes a $260K ceiling with no base-vs-OTE basis:
    flagged at the default 200000, not when the threshold is above it."""
    raw = yaml.safe_load((FIXTURES / "config" / "filters.yaml").read_text())
    raw["comp_sanity"]["ote_unclear_from"] = 300000
    (jobkit_home / "config" / "filters.yaml").write_text(yaml.safe_dump(raw))
    flags = run_comp_fixture(conn, monkeypatch)
    assert not any("ote-basis-unclear" in f for f in flags["Director of Sales"]["flags"])
    conn.execute("DELETE FROM sanity")
    conn.execute("DELETE FROM jobs")
    conn.commit()
    raw["comp_sanity"]["ote_unclear_from"] = 250000
    (jobkit_home / "config" / "filters.yaml").write_text(yaml.safe_dump(raw))
    flags = run_comp_fixture(conn, monkeypatch)
    assert (
        "ote-basis-unclear: big number, base-vs-OTE not stated"
        in (flags["Director of Sales"]["flags"])
    )


# ---------------------------------------------------------------------------
# standing rules on any track
# ---------------------------------------------------------------------------


def test_title_class_for_non_sales_tracks() -> None:
    rn = persona("rn_columbus")
    assert _title_class("Clinical Nurse II", rn) == "registered_nurse"
    assert _title_class("Nurse Educator", rn) == "nurse_educator"
    assert _title_class("Phlebotomist", rn) is None
    teacher = persona("teacher_to_id")
    assert _title_class("First Instructional Designer", teacher) == "instructional_designer"
    # the founding/first carve-out is the AE track's alone
    assert _title_class("Founding Account Executive") is None


def test_rn_standing_rule_matches_its_track(conn: sqlite3.Connection, jobkit_home: Path) -> None:
    use_persona(jobkit_home, "rn_columbus")
    (jobkit_home / "config" / "autoapprove.yaml").write_text(
        yaml.safe_dump(
            {
                "auto_approve": [
                    {"when": {"title_class": "registered_nurse", "score_gte": 70}, "reason": "rn"}
                ],
                "auto_reject": [
                    {"when": {"title_class": "school_nurse", "score_lt": 100}, "reason": "x"}
                ],
            }
        )
    )
    for i, title in enumerate(("Staff Nurse, ICU", "Nurse Educator")):
        jid = _insert(conn, external_id=f"rn{i}", title=title, jd_html=JD)
        conn.execute("UPDATE jobs SET status='queued' WHERE id=?", (jid,))
        conn.execute("INSERT INTO scores (job_id, fit_score) VALUES (?, 80)", (jid,))
    conn.commit()
    stats = run_standing_rules(conn)
    # the unknown school_nurse rule never matches; the RN rule does
    assert stats == {"evaluated": 2, "auto_approved": 1, "auto_rejected": 0}
    status = dict(conn.execute("SELECT title, status FROM jobs").fetchall())
    assert status == {"Staff Nurse, ICU": "approved", "Nurse Educator": "queued"}
    log = (jobkit_home / "logs" / "jobkit.jsonl").read_text()
    assert "standing_rules.unknown_title_class" in log and "school_nurse" in log
    assert unknown_title_classes(
        yaml.safe_load((jobkit_home / "config" / "autoapprove.yaml").read_text()),
        persona("rn_columbus"),
    ) == ["school_nurse"]


# ---------------------------------------------------------------------------
# rescore: prompt staleness and re-gating
# ---------------------------------------------------------------------------


def test_score_staleness_names_the_prompt() -> None:
    now_sales = rescore.ScoringPrompt("h", "score_fit", "1.5")
    now_general = rescore.ScoringPrompt("h", "score_fit_general", "1.0")
    legacy = json.dumps({"profile_hash": "h", "prompt_version": "1.1"})
    # an event from before prompt names: a score_fit score, version not compared
    assert not rescore.score_is_stale(legacy, now_sales)
    assert rescore.score_is_stale(legacy, now_general)
    named = json.dumps({"profile_hash": "h", "prompt_version": "1.5", "prompt": "score_fit"})
    assert not rescore.score_is_stale(named, now_sales)
    # as before prompt sets, a score_fit.md version bump alone leaves sales scores
    assert not rescore.score_is_stale(named, rescore.ScoringPrompt("h", "score_fit", "1.6"))
    assert not rescore.score_is_stale(legacy, rescore.ScoringPrompt("h", "score_fit", "1.6"))
    assert rescore.score_is_stale(named, rescore.ScoringPrompt("other", "score_fit", "1.5"))
    general = json.dumps(
        {"profile_hash": "h", "prompt_version": "1.0", "prompt": "score_fit_general"}
    )
    assert not rescore.score_is_stale(general, now_general)
    assert rescore.score_is_stale(general, rescore.ScoringPrompt("h", "score_fit_general", "1.1"))
    assert rescore.score_is_stale(general, now_sales)
    assert rescore.score_is_stale(None, now_sales)
    assert rescore.recorded_gate("score_below_surface:58<65") == 65
    assert rescore.recorded_gate("sanity:onsite") is None


def _rejected_by_gate(conn: sqlite3.Connection, title: str, score: int, gate: int) -> int:
    jid = _insert(
        conn,
        external_id=title,
        title=title,
        jd_html=JD,
        jd_hash=f"r-{title}",
        location_raw="Remote, US",
        remote=1,
    )
    conn.execute("UPDATE jobs SET status='rejected' WHERE id=?", (jid,))
    conn.execute("INSERT INTO scores (job_id, fit_score) VALUES (?, ?)", (jid, score))
    conn.execute("INSERT INTO job_requirements (job_id, must_have) VALUES (?, '[]')", (jid,))
    conn.execute(
        "INSERT INTO rejections (job_id, layer, reason) VALUES (?, 'filter', ?)",
        (jid, triage.score_gate_reason(score, gate)),
    )
    return jid


def test_switching_prompt_set_makes_sales_scores_stale(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (jobkit_home / "data" / "career_corpus.yaml").write_text(yaml.safe_dump(TEACHER_CORPUS))
    use_persona(jobkit_home, "teacher_to_id")
    jid = _insert(conn, external_id="s", title="Science Teacher", jd_html=JD, jd_hash="s")
    conn.execute("UPDATE jobs SET status='queued' WHERE id=?", (jid,))
    conn.execute("INSERT INTO scores (job_id, fit_score) VALUES (?, 70)", (jid,))
    conn.execute("INSERT INTO job_requirements (job_id, must_have) VALUES (?, '[]')", (jid,))
    conn.execute(
        "INSERT INTO events (job_id, kind, detail) VALUES (?, 'score_profile', ?)",
        (
            jid,
            json.dumps({"profile_hash": triage.current_profile_hash(), "prompt_version": "1.5"}),
        ),
    )
    conn.commit()
    dry = rescore.rescore(conn, dry_run=True)
    assert [c.job_id for c in dry.candidates] == [jid]


def test_track_gate_change_regates_a_rejected_job(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The teacher lowered the instructional_designer gate to 50: a job the
    old 65 gate rejected at 58 comes back to the queue, with no LLM call."""
    (jobkit_home / "data" / "career_corpus.yaml").write_text(yaml.safe_dump(TEACHER_CORPUS))
    filters = use_persona(jobkit_home, "teacher_to_id")
    monkeypatch.setattr(rescore, "run_standing_rules", lambda c: {"evaluated": 0})
    monkeypatch.setattr(llm, "call_json", FakeCalls({}))  # any call raises KeyError
    now = rescore.current_scoring(filters)
    detail = json.dumps(
        {"profile_hash": now.profile_hash, "prompt_version": now.version, "prompt": now.name}
    )
    up = _rejected_by_gate(conn, "Instructional Designer", 58, 65)
    low = _rejected_by_gate(conn, "Learning Designer", 45, 65)
    other = _rejected_by_gate(conn, "Science Teacher", 58, 60)  # its track has no min_score
    for jid in (up, low, other):
        conn.execute(
            "INSERT INTO events (job_id, kind, detail) VALUES (?, 'score_profile', ?)",
            (jid, detail),
        )
    conn.commit()
    dry = rescore.rescore(conn, dry_run=True)
    assert [(c.job_id, g) for c, g in dry.regated] == [(up, 50)] and dry.candidates == []
    assert "instructional_designer 50" in dry.render()
    report = rescore.rescore(conn)
    assert [(c.job_id, g) for c, g in report.regated] == [(up, 50)]
    status = dict(conn.execute("SELECT id, status FROM jobs").fetchall())
    assert status == {up: "queued", low: "rejected", other: "rejected"}
    ev = conn.execute("SELECT detail FROM events WHERE kind = 'regated'").fetchone()
    assert json.loads(ev["detail"]) == {"score": 58, "gate": 50}
    # done once: nothing left to re-gate
    assert rescore.rescore(conn, dry_run=True).regated == []
