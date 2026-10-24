"""Onboarding 2.1-2.6 — interview answers → config/filters.yaml and its
companions. Each interview-driven setting quotes the answer that produced it;
the generated file loads with the pipeline's own loader; a config the
interview did not write is never replaced silently."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from jobkit import config
from jobkit.autofill.answer_bank import BankMatcher, comp_expectation_block
from jobkit.config import Filters, load_filters, read_config_yaml
from jobkit.onboarding import mcp_tools
from jobkit.onboarding.answer_bank_builder import build_answer_bank
from jobkit.onboarding.files import GENERATED_MARK, SetupError
from jobkit.onboarding.intake import run_intake
from jobkit.pipeline.filter import matched_place_bucket
from jobkit.pipeline.filter import reject_reason_now as _reject_reason
from jobkit.pipeline.triage import commute_minutes, geo_restriction_acceptable
from tests.conftest import FIXTURES

ANSWERS: dict[str, Any] = {
    "tracks": [
        {
            "name": "Customer Success Manager",
            "titles": ["Customer Success Manager", "Customer Success Lead"],
            "comp_floor": 95000,
            "said": "CSM roles, at least 95k base",
        },
        {
            "name": "Implementation Manager",
            "comp_floor": 110000,
            "comp_basis": "ote",
            "said": "implementation if OTE is 110k+",
        },
    ],
    "exclude_titles": {"items": ["Collections"], "said": "never collections"},
    "seniority_floor": {"level": "experienced", "said": "nothing junior or entry level"},
    "locations": {
        "work_mode": "hybrid",
        "places": ["Denver", "Boulder"],
        "max_office_days_per_week": 2,
        "max_commute_minutes": 40,
        "commute_minutes": {"Denver": 20},
        "relocation_cities": ["Austin"],
        "said": "remote, or hybrid in Denver or Boulder, 2 days max",
    },
    "comp": {"default_floor": 90000, "said": "nothing under 90k"},
    "industries": {"wanted": ["health tech"], "refused": ["gambling"], "said": "no gambling"},
    "blocklist_companies": {"items": ["Harbor Health"], "said": "not going back there"},
}


@pytest.fixture()
def fresh(jobkit_home: Path) -> Path:
    """A clone with no personal config (the sandbox ships the test profile)."""
    for name in ("filters", "targets", "autoapprove", "banned_phrases", "commute_table"):
        (jobkit_home / "config" / f"{name}.yaml").unlink()
    return jobkit_home


def _job(title: str, location: str = "Remote", remote: bool | None = True) -> dict[str, Any]:
    return {
        "title": title,
        "company_name": "Acme",
        "location_raw": location,
        "remote": None if remote is None else int(remote),
        "posted_at": None,
        "comp_min": None,
        "comp_max": None,
        "comp_currency": None,
        "jd_html": "",
    }


def test_generated_filters_load_and_filter(fresh: Path) -> None:
    out = run_intake(ANSWERS)
    path = fresh / "config" / "filters.yaml"
    assert path.read_text().startswith(GENERATED_MARK)
    f = load_filters(path)
    assert [t.id for t in f.tracks] == ["customer_success_manager", "implementation_manager"]
    assert f.track_for("Senior Customer Success Manager, Mid-Market") == "customer_success_manager"
    assert f.track_for("Implementation Manager") == "implementation_manager"
    assert f.comp_floor == 90000
    cf = f.comp_floor_for("Implementation Manager")
    assert (cf.floor, cf.basis) == (110000, "ote")
    assert f.allowed_locations == ["Denver", "Boulder"] and f.relocation_cities == ["Austin"]
    assert f.accept_remote
    assert f.raw["filters"]["blocklist_companies"] == ["Harbor Health"]
    assert f.raw["remote_policy"]["max_office_days_per_week"] == 2
    # the letter-drafts setting is copied from the template, with its default
    assert f.cover_attempts == 5 and f.raw["tailoring"] == {"cover_attempts": 5}
    # the seniority floor and the named exclusion reject; the tracks pass
    assert _reject_reason(_job("Junior Customer Success Manager"), f) is not None
    assert _reject_reason(_job("Associate Customer Success Manager"), f) is not None
    assert _reject_reason(_job("Collections Specialist"), f) is not None
    assert _reject_reason(_job("Customer Success Lead"), f) is None
    assert _reject_reason(_job("Customer Success Manager", "Denver, CO", False), f) is None
    assert _reject_reason(_job("Customer Success Manager", "Chicago, IL", False), f) is not None
    assert "no commute time for Boulder" in out


def test_every_interview_setting_quotes_the_answer(fresh: Path) -> None:
    run_intake(ANSWERS)
    text = (fresh / "config" / "filters.yaml").read_text()
    for said in (
        "CSM roles, at least 95k base",
        "implementation if OTE is 110k+",
        "never collections",
        "nothing junior or entry level",
        "remote, or hybrid in Denver or Boulder, 2 days max",
        "nothing under 90k",
        "not going back there",
        "no gambling",
    ):
        assert f'# you said: "{said}"' in text
    # no industry knob is invented: recorded as a comment only
    assert "industries" not in yaml.safe_load(text)["filters"]
    assert "#   refused: gambling" in text


def test_companion_files(fresh: Path) -> None:
    run_intake(ANSWERS)
    table = read_config_yaml(fresh / "config" / "commute_table.yaml")
    assert commute_minutes(table, "denver-metro") == 20
    assert commute_minutes(table, "other-boulder") is None
    rules = read_config_yaml(fresh / "config" / "autoapprove.yaml")
    assert rules["auto_approve"] == [] and rules["auto_reject"] == []  # none until accepted
    banned = read_config_yaml(fresh / "config" / "banned_phrases.yaml")
    assert banned["cover_letter"]["hard_reject"]
    record = yaml.safe_load((fresh / "data" / "setup" / "preferences.answers.yaml").read_text())
    assert record["answers"]["tracks"][0]["said"] == "CSM roles, at least 95k base"


def test_feeds_search_the_users_titles(fresh: Path) -> None:
    run_intake(ANSWERS)
    feeds = load_filters(fresh / "config" / "filters.yaml").raw["feeds"]
    assert feeds["adzuna"]["keywords"][:2] == ["Customer Success Manager", "Customer Success Lead"]
    assert "account executive" not in feeds["adzuna"]["keywords"]
    # not a sales search: no sales-only categories
    assert feeds["remotive"]["categories"] == [] and feeds["consider"]["categories"] == []
    assert feeds["getro"]["enabled"] is False  # hard rule 11


def test_remote_only(fresh: Path) -> None:
    answers = copy.deepcopy(ANSWERS)
    answers["locations"] = {"work_mode": "remote_only", "said": "remote only"}
    run_intake(answers)
    f = load_filters(fresh / "config" / "filters.yaml")
    assert f.allowed_locations == []
    assert f.raw["remote_policy"]["max_office_days_per_week"] == 0
    assert f.raw["remote_policy"]["reject_onsite"] is True


@pytest.mark.parametrize(
    "change, message",
    [
        ({"locations": {"work_mode": "remote_only", "places": ["Denver"]}}, "takes no places"),
        (
            {"locations": {"work_mode": "hybrid", "places": ["Denver"], "max_commute_minutes": 30}},
            "max_office_days",
        ),
        ({"locations": {"work_mode": "onsite_ok"}}, "needs at least one place"),
        ({"tracks": []}, "tracks"),
        ({"nightly_at": "2am"}, "HH:MM"),
        ({"unknown_section": 1}, "unknown_section"),
        (
            {"tracks": [{"name": "Associate Account Manager"}]},
            "would be rejected by the exclude",
        ),
        ({"tracks": [{"name": "X", "title_regex": "(?i)(unclosed"}]}, "does not compile"),
        # no default floor: refused (asked), never written as `floor: 0`
        ({"comp": {"said": "no floor in mind"}}, "comp.default_floor: ask the floor"),
        ({"comp": {"default_floor": 0}}, "comp.default_floor: ask the floor"),
    ],
)
def test_bad_answers_write_nothing(fresh: Path, change: dict[str, Any], message: str) -> None:
    answers = {**copy.deepcopy(ANSWERS), **change}
    with pytest.raises(SetupError, match=message):
        run_intake(answers)
    assert not (fresh / "config" / "filters.yaml").exists()


def test_existing_config_is_protected(jobkit_home: Path) -> None:
    # the sandbox's test profile stands in for a config written by hand
    path = jobkit_home / "config" / "filters.yaml"
    before = path.read_text()
    with pytest.raises(SetupError, match="not written by the setup interview"):
        run_intake(ANSWERS)
    assert path.read_text() == before
    run_intake(ANSWERS, replace_existing=True)
    kept = list((jobkit_home / "data" / "setup" / "backup").glob("filters.yaml.*"))
    assert len(kept) == 1 and kept[0].read_text() == before
    # hand-written companions are never replaced
    auto = jobkit_home / "config" / "autoapprove.yaml"
    assert not auto.read_text().startswith(GENERATED_MARK)


def test_rerun_rewrites_its_own_files(fresh: Path) -> None:
    run_intake(ANSWERS)
    answers = copy.deepcopy(ANSWERS)
    answers["comp"]["default_floor"] = 100000
    run_intake(answers)  # its own output: no replace_existing needed
    assert load_filters(fresh / "config" / "filters.yaml").comp_floor == 100000


def _bank(home: Path) -> dict[str, Any]:
    data = yaml.safe_load((home / "data" / "answer_bank.yaml").read_text())
    assert isinstance(data, dict)
    return data


def test_intake_and_answer_bank_agree_on_comp_expectation(fresh: Path) -> None:
    """The filters run_intake writes and the bank build_answer_bank writes,
    read by the form-filling gate: the salary-expectation figure goes on a
    job in a track whose floor is at or above the default floor."""
    run_intake(ANSWERS)
    build_answer_bank({"comp_expectation": {"policy": "number", "amount": 120000, "floor": 100000}})
    bank = _bank(fresh)
    f = load_filters(fresh / "config" / "filters.yaml")
    assert f.comp_floor == 90000
    # both tracks' floors (95000 base, 110000 OTE) are at or above the default
    for title in ("Customer Success Manager", "Customer Success Lead", "Implementation Manager"):
        assert comp_expectation_block(title, None, bank, f) is None, title
    matcher = BankMatcher(bank, use_embeddings=False)
    hit = matcher.match(
        "What are your salary expectations?",
        comp_expectation_block=comp_expectation_block("Customer Success Manager", None, bank, f),
    )
    assert hit == {"kind": "screening", "entry_id": "comp_expectation", "source": "answer_bank"}
    # the posting's own ceiling under the bank's floor still blocks
    assert comp_expectation_block("Customer Success Manager", 90000, bank, f) is not None


def test_track_floor_under_the_default_is_named(fresh: Path) -> None:
    answers = copy.deepcopy(ANSWERS)
    answers["comp"]["default_floor"] = 100000
    out = run_intake(answers)
    # Customer Success Manager's 95000 is under the 100000 default: the gate
    # blanks its salary-expectation field, and run_intake says so
    assert "track customer_success_manager: its floor (95,000) is under your default" in out
    assert "track implementation_manager" not in out
    build_answer_bank({"comp_expectation": {"policy": "number", "amount": 120000}})
    f = load_filters(fresh / "config" / "filters.yaml")
    block = comp_expectation_block("Customer Success Manager", None, _bank(fresh), f)
    assert block == (
        "comp-expectation-needs-human:track-customer_success_manager-floor-95000-below-100000"
    )
    assert comp_expectation_block("Implementation Manager", None, _bank(fresh), f) is None


def test_writing_style_answer(fresh: Path) -> None:
    answers = {**ANSWERS, "writing_style": {"estimate_style": "confident", "said": "state it"}}
    run_intake(answers)
    banned = read_config_yaml(fresh / "config" / "banned_phrases.yaml")
    assert banned["drift"]["estimate_style"] == "confident"


def test_mcp_wrapper(fresh: Path) -> None:
    import json

    out = mcp_tools.run_intake(json.dumps(ANSWERS))
    assert "config/filters.yaml" in out


# ---------------------------------------------------------------------------
# any field: personas rendered from the neutral template (all fictional)
# ---------------------------------------------------------------------------

PERSONAS = FIXTURES / "onboarding_personas"


def _persona(name: str) -> dict[str, Any]:
    data = yaml.safe_load((PERSONAS / f"{name}.answers.yaml").read_text())
    assert isinstance(data, dict)
    return data


def _filters(home: Path) -> Filters:
    return load_filters(home / "config" / "filters.yaml")


def test_the_template_is_the_neutral_one(fresh: Path) -> None:
    # the sandbox ships the repo's templates: intake renders from the neutral one
    template = yaml.safe_load((fresh / "config" / "filters.example.yaml").read_text())
    assert template["prompt_set"] == "general"


def test_nurse_persona(fresh: Path) -> None:
    out = run_intake(_persona("rn"))
    text = (fresh / "config" / "filters.yaml").read_text()
    f = _filters(fresh)
    assert f.prompt_set == "general"
    assert "followups" not in f.raw and "followups:" not in text
    assert f.ghost_after.days == 30
    # titles a nurse search must not drop
    for title in ("Staff Nurse, ICU", "Clinical Nurse II", "Registered Nurse - Med/Surg"):
        assert f.track_for(title) == "registered_nurse", title
        assert _reject_reason(_job(title, "Dublin, OH", False), f) is None, title
    assert f.track_for("Nurse Practitioner") is None
    assert _reject_reason(_job("Travel Nurse RN"), f) is not None
    # hourly pay becomes a yearly floor, with the arithmetic quoted
    assert f.comp_floor == 40 * 36 * 52
    assert f.comp_floor_for("Staff Nurse").floor == 42 * 36 * 52 == 78624
    assert "# hourly: 42 an hour x 36 hours a week x 52 weeks = 78624 a year" in text
    assert f.comp_sanity.underpriced_below == round(1.25 * 78624)
    assert f.comp_sanity.ote_check == "ote_tracks"
    assert text.count("\ncomp_sanity:") == 1
    assert f.comp_sanity.max_range_width_pct == 0.6  # the range checks kept
    # the nearby towns kept are places with their own commute. Each place
    # carries the state, so the filter checks it (match_state) and a job
    # limited to Ohio residents passes the remote check. Upper Arlington is
    # filed under dc-metro by its spelling; its state keeps the DC jobs out
    assert f.allowed_locations == [
        "Columbus, OH",
        "Dublin, OH",
        "Westerville, OH",
        "Upper Arlington, OH",
    ]
    assert f.match_state
    assert geo_restriction_acceptable("must reside in Ohio", f.acceptable_geo_restrictions)
    assert "'Upper Arlington' under dc-metro" in out
    assert "nearby towns written with your state (OH): Dublin, Westerville, Upper Arlington" in out
    for where, reason in (
        ("Upper Arlington, OH", None),
        ("Arlington, VA", "location:dc-metro:other-state"),
        ("Washington, DC", "location:dc-metro:other-state"),
        ("Dublin, CA", "location:other-dublin:other-state"),
    ):
        assert _reject_reason(_job("Registered Nurse", where, False), f) == reason, where
    table = read_config_yaml(fresh / "config" / "commute_table.yaml")
    assert commute_minutes(table, "other-dublin") == 20
    assert commute_minutes(table, "other-westerville") == 25
    assert commute_minutes(table, "dc-metro") == 15
    # feeds: places with the state, remote-only boards off, federal on
    feeds = f.raw["feeds"]
    assert f.adzuna_locations == (config.AdzunaPlace("Columbus, OH", 40),)
    assert feeds["adzuna"]["enabled"] is True
    assert feeds["adzuna"]["keywords"][:2] == ["Registered Nurse", "Staff Nurse"]
    assert f.usajobs_locations == (config.UsajobsPlace("Columbus, Ohio", 25),)
    assert feeds["usajobs"]["enabled"] is True and feeds["usajobs"]["remote_only"] is False
    assert feeds["usajobs"]["full_description"] is True
    for board in ("arbeitnow", "remotive", "remoteok", "himalayas"):
        assert feeds[board]["enabled"] is False, board
    li = f.linkedin_guest
    assert li.enabled is False
    assert li.keywords == ("Registered Nurse", "Staff Nurse", "Clinical Nurse", "RN")
    assert [(x.location, x.workplace) for x in li.locations] == [("Columbus, Ohio", "any")]
    assert feeds["remotive"]["categories"] == [] and feeds["consider"]["categories"] == []


def test_engineer_persona(fresh: Path) -> None:
    run_intake(_persona("engineer"))
    f = _filters(fresh)
    assert f.prompt_set == "general"
    for title in (
        "Backend Engineer (Senior)",
        "Sr. Software Engineer",
        "Senior Back-End Engineer",
        "Software Engineer III - Backend",
        "Senior Backend Developer",
        "Staff Platform Engineer",
    ):
        assert f.track_for(title) == "backend_engineer", title
        assert _reject_reason(_job(title), f) is None, title
    for title in ("Frontend Engineer", "Sales Engineer", "Account Executive"):
        assert f.track_for(title) is None, title
    track = f.track("backend_engineer")
    assert track is not None and track.emphasis == (
        "APIs and services I designed and ran in production"
    )
    assert not track.pivot and track.min_score is None
    feeds = f.raw["feeds"]
    assert "locations" not in feeds["adzuna"] and feeds["usajobs"]["remote_only"] is True
    assert feeds["usajobs"]["enabled"] is False  # federal jobs not asked for
    assert feeds["remotive"]["enabled"] is True
    assert [(x.location, x.workplace) for x in f.linkedin_guest.locations] == [
        ("United States", "remote")
    ]


def test_teacher_moving_into_instructional_design(fresh: Path) -> None:
    run_intake(_persona("teacher"))
    f = _filters(fresh)
    assert f.prompt_set == "general"
    track = f.track("instructional_designer")
    assert track is not None and track.pivot and track.min_score == 50
    assert track.emphasis == "curriculum I designed and the Canvas modules I built"
    assert 45 <= f.min_score_for("instructional_designer") <= 50
    assert f.min_score_for(None) == 65
    for title in ("Learning Designer", "Curriculum Designer", "Senior Learning Designer"):
        assert f.track_for(title) == "instructional_designer", title
    # the state given as a name still gives Adzuna its "City, ST"
    assert f.adzuna_locations == (config.AdzunaPlace("Indianapolis, IN", 40),)
    assert f.allowed_locations == ["Indianapolis, IN"]
    assert geo_restriction_acceptable("Indiana residents only", f.acceptable_geo_restrictions)
    workplaces = [(x.location, x.workplace) for x in f.linkedin_guest.locations]
    assert workplaces == [("Indianapolis, Indiana", "hybrid"), ("United States", "remote")]
    # hybrid near Indianapolis, and remote too: Adzuna also searches with no place
    from jobkit.adapters.feeds.adzuna import _plan
    from jobkit.pipeline.feeds import _spec_for

    spec = _spec_for("adzuna", f)
    assert spec is not None
    plan = _plan(spec)
    assert {place.name if place else None for _, place in plan} == {"Indianapolis, IN", None}


def test_a_change_of_field_without_its_own_gate_is_named(fresh: Path) -> None:
    answers = _persona("teacher")
    del answers["tracks"][0]["min_score"]
    out = run_intake(answers)
    assert "track instructional_designer is a change of field" in out
    assert "45-50" in out


def test_sales_persona_keeps_the_sales_prompts_and_categories(fresh: Path) -> None:
    run_intake(_persona("sales"))
    f = _filters(fresh)
    assert f.prompt_set == "sales"
    feeds = f.raw["feeds"]
    # written explicitly: the neutral template has no categories to copy
    assert feeds["remotive"]["categories"] == ["sales", "business"]
    assert feeds["consider"]["categories"] == ["sales"]
    assert f.track_for("Enterprise Account Executive") == "account_executive"


def test_sales_track_names_get_the_sales_prompt_ids(fresh: Path) -> None:
    """A track named "AE" or "SDR/BDR" gets the id the sales resume prompt and
    the founding-AE carve-out read; a sales track with no emphasis line is named."""
    answers = _persona("sales")
    answers["tracks"][0]["name"] = "AE"
    answers["tracks"].append({"name": "SDR/BDR", "comp_floor": 80000, "said": "SDR too"})
    out = run_intake(answers)
    f = _filters(fresh)
    assert [t.id for t in f.tracks] == ["account_executive", "sales_manager", "sdr_bdr"]
    assert "track sales_manager gets no sales emphasis" in out
    assert "id: sales_exec" in out


@pytest.mark.parametrize(
    "names",
    [
        ["Revenue Cycle Specialist"],
        ["Patient Account Manager"],
        ["Account Executive", "Registered Nurse"],  # one non-sales track: general
    ],
)
def test_only_an_all_sales_search_is_a_sales_search(fresh: Path, names: list[str]) -> None:
    answers = copy.deepcopy(ANSWERS)
    answers["tracks"] = [{"name": n} for n in names]
    answers["exclude_titles"] = {"items": []}
    answers["seniority_floor"] = {"level": "any"}
    run_intake(answers)
    f = _filters(fresh)
    assert f.prompt_set == "general"
    assert f.raw["feeds"]["remotive"]["categories"] == []


def test_title_phrases_match_hyphens_slashes_and_sr(fresh: Path) -> None:
    answers = copy.deepcopy(ANSWERS)
    answers["tracks"] = [{"name": "Senior Back End Engineer"}]
    answers["exclude_titles"] = {"items": []}
    run_intake(answers)
    f = _filters(fresh)
    for title in ("Sr. Back-End Engineer", "Senior Backend Engineer", "Sr Back/End Engineer"):
        assert f.track_for(title) == "senior_back_end_engineer", title
    assert f.track_for("Senior Frontend Engineer") is None


@pytest.mark.parametrize(
    "track, message",
    [
        (
            {"name": "Software Engineer", "should_match": ["Software Engineer II"]},
            r"seniority floor \(senior\) rejects it as level I / II titles",
        ),
        (
            {"name": "Software Engineer", "should_match": ["Backend Developer"]},
            "should match, but",
        ),
        (
            {"name": "Engineer", "should_not_match": ["Sales Engineer"]},
            "should not match, but it does",
        ),
    ],
)
def test_example_titles_are_checked_and_never_written(
    fresh: Path, track: dict[str, Any], message: str
) -> None:
    answers = copy.deepcopy(ANSWERS)
    answers["tracks"] = [track]
    answers["exclude_titles"] = {"items": []}
    answers["seniority_floor"] = {"level": "senior"}
    with pytest.raises(SetupError, match=message):
        run_intake(answers)
    assert not (fresh / "config" / "filters.yaml").exists()


def test_example_titles_are_comments_not_keys(fresh: Path) -> None:
    run_intake(_persona("engineer"))
    raw = yaml.safe_load((fresh / "config" / "filters.yaml").read_text())
    track = raw["filters"]["tracks"][0]
    assert "should_match" not in track and "should_not_match" not in track
    assert "title_words" not in track


@pytest.mark.parametrize(
    "change, message",
    [
        ({"comp": {"default_floor": 40, "unit": "hourly"}}, "hours_per_week"),
        ({"locations": {"work_mode": "remote_only", "nearby": {"Dublin": 10}}}, "nearby"),
        ({"locations": {"work_mode": "remote_only", "accept_remote": False}}, "accept_remote"),
        (
            {
                "locations": {
                    "work_mode": "onsite_ok",
                    "places": ["Columbus"],
                    "state": "Narnia",
                    "max_commute_minutes": 30,
                }
            },
            "not a US state",
        ),
        ({"tracks": [{"name": "X", "min_score": 120}]}, "min_score"),
        ({"tracks": [{"name": "X", "emphasis": "x" * 201}]}, "emphasis"),
    ],
)
def test_new_answers_are_validated(fresh: Path, change: dict[str, Any], message: str) -> None:
    answers = {**copy.deepcopy(ANSWERS), **change}
    with pytest.raises(SetupError, match=message):
        run_intake(answers)
    assert not (fresh / "config" / "filters.yaml").exists()


def test_existing_config_is_still_protected_for_a_new_field(jobkit_home: Path) -> None:
    path = jobkit_home / "config" / "filters.yaml"
    before = path.read_text()
    with pytest.raises(SetupError, match="not written by the setup interview"):
        run_intake(_persona("rn"))
    assert path.read_text() == before


def test_hourly_pay_in_the_answer_bank(fresh: Path) -> None:
    """A nurse's '$42 an hour' goes in as her wording, with no yearly floor
    to block her own salary field."""
    run_intake(_persona("rn"))
    with pytest.raises(SetupError, match="hourly pay goes in the person's own words"):
        build_answer_bank(
            {"comp_expectation": {"policy": "number", "amount": 42, "unit": "hourly"}}
        )
    with pytest.raises(SetupError, match="leave it out for hourly pay"):
        build_answer_bank(
            {
                "comp_expectation": {
                    "policy": "text",
                    "text": "$42 an hour",
                    "unit": "hourly",
                    "floor": 42,
                }
            }
        )
    build_answer_bank(
        {"comp_expectation": {"policy": "text", "text": "$42 an hour", "unit": "hourly"}}
    )
    bank = _bank(fresh)
    entry = next(s for s in bank["screening"] if s["id"] == "comp_expectation")
    assert entry["answer"] == "$42 an hour" and entry["floor"] is None
    f = _filters(fresh)
    # the field is filled, whatever (yearly) range the posting shows
    for comp_max in (None, 70000, 120000):
        assert comp_expectation_block("Staff Nurse", comp_max, bank, f) is None, comp_max


@pytest.mark.parametrize(
    "persona, title, location, kept",
    [
        # a job that works with students or graduates is not the seat itself
        ("rn", "Registered Nurse, Student Health Services", "Columbus, OH", True),
        ("rn", "Staff Nurse - Student Health", "Columbus, OH", True),
        ("rn", "Clinical Nurse Co-op Preceptor", "Columbus, OH", True),
        ("rn", "Student Nurse Registered Nurse Extern", "Columbus, OH", False),
        ("rn", "Registered Nurse Internship", "Columbus, OH", False),
        ("engineer", "Senior Backend Engineer - Graduate Admissions Platform", "Remote", True),
        ("engineer", "Senior Software Engineer, Student Loans", "Remote", True),
        # level I / II with senior, staff, lead or principal is a senior role
        ("engineer", "Senior Software Engineer II", "Remote", True),
        ("engineer", "Senior Backend Engineer, Level 2", "Remote", True),
        ("engineer", "Backend Engineer (Senior) II", "Remote", True),
        ("engineer", "Software Engineer II", "Remote", False),
        ("engineer", "Graduate Software Engineer", "Remote", False),
        ("engineer", "New Grad Backend Engineer", "Remote", False),
    ],
)
def test_the_seniority_floor_excludes_only_the_seat(
    fresh: Path, persona: str, title: str, location: str, kept: bool
) -> None:
    run_intake(_persona(persona))
    f = _filters(fresh)
    remote = location == "Remote"
    reason = _reject_reason(_job(title, location, remote), f)
    if kept:
        assert reason is None, (title, reason)
    else:
        assert reason is not None and reason.startswith("exclude_title"), (title, reason)


def test_a_sales_search_with_a_change_of_field_gets_the_general_prompts(fresh: Path) -> None:
    """pivot and emphasis are read only by the general prompts, so a teacher
    moving into SDR work is not scored with the sales career anchors."""
    answers = {
        "tracks": [
            {
                "name": "Sales Development Representative",
                "titles": ["Sales Development Representative", "SDR", "BDR"],
                "pivot": True,
                "min_score": 50,
                "emphasis": "classroom results and parent outreach",
                "said": "moving from teaching into SDR",
            }
        ],
        "locations": {"work_mode": "remote_only", "said": "remote"},
        "comp": {"default_floor": 55000, "said": "55k"},
    }
    out = run_intake(answers)
    f = _filters(fresh)
    assert f.prompt_set == "general"
    assert f.tracks[0].pivot and f.tracks[0].emphasis
    # still a sales search for the feeds
    assert f.raw["feeds"]["remotive"]["categories"] != []
    assert "prompt_set is general" in " ".join(out.split())


def test_a_plain_sales_search_keeps_the_sales_prompts(fresh: Path) -> None:
    run_intake(_persona("sales"))
    assert _filters(fresh).prompt_set == "sales"


def test_searches_also_use_the_example_titles(fresh: Path) -> None:
    """The feeds and LinkedIn search the titles a track should match too, with
    seniority words and levels dropped, so discovery is as wide as the filter."""
    run_intake(_persona("engineer"))
    f = _filters(fresh)
    assert f.raw["feeds"]["adzuna"]["keywords"] == [
        "Backend Engineer",
        "Software Engineer",
        "Back-End Engineer",
        "Backend Developer",
    ]
    assert f.linkedin_guest.keywords[:2] == ("Backend Engineer", "Software Engineer")


def test_a_written_together_title_matches_its_spaced_and_hyphenated_forms(fresh: Path) -> None:
    run_intake(_persona("teacher"))
    f = _filters(fresh)
    for title in ("eLearning Developer", "E-Learning Developer", "E Learning Developer"):
        assert f.track_for(title) == "instructional_designer", title


def test_the_state_keeps_out_other_towns_of_the_same_name(fresh: Path) -> None:
    run_intake(_persona("rn"))
    f = _filters(fresh)
    title = "Registered Nurse"
    for place in ("Columbus, OH", "Columbus, Ohio, United States", "Dublin, OH", "Dublin"):
        assert _reject_reason(_job(title, place, False), f) is None, place
    # the bucket is the same, so only the state tells these apart
    assert _reject_reason(_job(title, "Columbus, GA", False), f) == (
        "location:other-columbus:other-state"
    )
    assert (
        _reject_reason(_job(title, "Dublin, CA", False), f) == "location:other-dublin:other-state"
    )
    assert _reject_reason(_job(title, "Dublin, Ireland", False), f) == (
        "location:other-dublin:outside-us"
    )
    # ATS spellings the bucket misses are matched to the allowed place
    for place in ("Columbus OH", "OH-Columbus", "US-OH-Columbus", "Westerville Ohio"):
        assert _reject_reason(_job(title, place, False), f) is None, place
        assert matched_place_bucket(place, f) in {"other-columbus", "other-westerville"}
    assert _reject_reason(_job(title, "Columbus GA", False), f) is not None


def test_a_remote_only_state_is_an_acceptable_restriction(fresh: Path) -> None:
    answers = _persona("engineer")
    answers["locations"]["state"] = "OR"
    run_intake(answers)
    f = _filters(fresh)
    assert "Oregon" in f.raw["remote_policy"]["acceptable_geo_restrictions"]
    assert geo_restriction_acceptable(
        "must live in Oregon or Washington", f.acceptable_geo_restrictions
    )
    assert not f.match_state


def test_a_place_filed_under_a_metro_by_its_spelling(fresh: Path) -> None:
    answers = _persona("rn")
    answers["locations"]["places"] = ["Mansfield"]
    answers["locations"]["nearby"] = {}
    answers["locations"]["commute_minutes"] = {"Mansfield": 10}
    out = run_intake(answers)
    assert "jobkit files 'Mansfield' under sf-bay by its spelling" in out
    f = _filters(fresh)
    assert f.allowed_locations == ["Mansfield, OH"] and f.match_state
    assert _reject_reason(_job("Registered Nurse", "Mansfield, OH", False), f) is None
    assert _reject_reason(_job("Registered Nurse", "San Francisco, CA", False), f) == (
        "location:sf-bay:other-state"
    )
    # without a state the person is told every sf-bay job passes
    del answers["locations"]["state"]
    out = run_intake(answers)
    assert "'Mansfield' has no state" in out


def test_without_match_state_places_match_by_town_as_before(fresh: Path) -> None:
    f = Filters(raw={"filters": {"locations": {"allowed": ["Columbus, OH"]}}})
    assert not f.match_state
    assert _reject_reason(_job("Registered Nurse", "Columbus, GA", False), f) is None
    assert _reject_reason(_job("Registered Nurse", "Columbus OH", False), f) == (
        "location:other-columbusoh"
    )


# ---- places across a state line, and a state given as a place ----


def test_a_nearby_town_can_carry_its_own_state(fresh: Path) -> None:
    """A kept town across a state line ('Covington, KY' near Cincinnati, OH)
    keeps its own state, so match_state lets its postings in; a bare nearby
    town gets the home state, and the note names each one."""
    answers = _persona("rn")
    answers["locations"].update(
        places=["Cincinnati"],
        state="OH",
        nearby={"Covington, KY": 15, "Mason": 25},
        commute_minutes={"Cincinnati": 15},
    )
    out = run_intake(answers)
    f = _filters(fresh)
    assert f.allowed_locations == ["Cincinnati, OH", "Covington, KY", "Mason, OH"]
    assert "nearby towns written with your state (OH): Mason." in out
    for where, reason in (
        ("Covington, KY", None),
        ("Covington, GA", "location:other-covington:other-state"),
        ("Mason, OH", None),
        ("Mason, MI", "location:other-mason:other-state"),
    ):
        assert _reject_reason(_job("Registered Nurse", where, False), f) == reason, where
    table = read_config_yaml(fresh / "config" / "commute_table.yaml")
    assert commute_minutes(table, "other-covington") == 15


def test_a_nearby_town_in_another_metro_with_no_state_is_left_out(fresh: Path) -> None:
    answers = _persona("rn")
    answers["locations"]["state"] = None
    answers["locations"]["places"] = ["Columbus"]
    out = run_intake(answers)
    f = _filters(fresh)
    assert "Upper Arlington" not in " ".join(f.allowed_locations)
    assert "nearby town 'Upper Arlington' was left out" in out
    assert "dc-metro" not in f.accepted_location_buckets


@pytest.mark.parametrize("place", ["Ohio", "OH", "Ohio, OH"])
def test_a_state_is_not_a_place(fresh: Path, place: str) -> None:
    answers = _persona("rn")
    answers["locations"].update(places=[place], nearby={}, commute_minutes={})
    with pytest.raises(SetupError, match="is a state, not a city"):
        run_intake(answers)


def test_the_state_after_a_comma_is_written_as_its_code(fresh: Path) -> None:
    """A state after the comma in any case or spelling is checked and written
    back as its two-letter code, in places, nearby towns and commute times."""
    answers = _persona("rn")
    answers["locations"].update(
        places=["Cincinnati, ohio"],
        state=None,
        nearby={"Covington, ky": 15, "Newport, Kentucky": 20},
        commute_minutes={"Cincinnati, oh": 15},
    )
    run_intake(answers)
    f = _filters(fresh)
    assert f.allowed_locations == ["Cincinnati, OH", "Covington, KY", "Newport, KY"]
    assert _reject_reason(_job("Registered Nurse", "Covington, KY", False), f) is None


def test_a_state_code_check_keeps_dc_and_drops_a_trailing_usa() -> None:
    from jobkit.onboarding.intake import _with_state_code

    assert _with_state_code("Washington, D.C.", "places") == "Washington, DC"
    assert _with_state_code("Columbus, OH, USA", "places") == "Columbus, OH"
    assert _with_state_code("Columbus", "places") == "Columbus"


@pytest.mark.parametrize(
    "where, place",
    [
        ("places", "Toronto, ON"),
        ("places", "Columbus, Ohioo"),
        ("nearby", "Covington, KY 41011"),
        ("nearby", "Paris, France"),
    ],
)
def test_a_place_with_a_state_that_is_not_a_us_state_is_refused(
    fresh: Path, where: str, place: str
) -> None:
    answers = _persona("rn")
    if where == "places":
        answers["locations"].update(places=[place], nearby={}, commute_minutes={})
    else:
        answers["locations"].update(places=["Cincinnati"], nearby={place: 15})
    with pytest.raises(SetupError, match=rf"locations\.{where}: .* is not a US state"):
        run_intake(answers)


@pytest.mark.parametrize("place", ["New York", "Washington, DC"])
def test_cities_named_like_states_are_places(fresh: Path, place: str) -> None:
    answers = _persona("rn")
    answers["locations"].update(places=[place], state=None, nearby={}, commute_minutes={place: 30})
    run_intake(answers)
    assert _filters(fresh).allowed_locations[0].startswith(place.split(",")[0])


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Retail Sales Associate", "general"),
        ("Medical Device Sales Representative", "general"),
        ("Insurance Sales Agent", "general"),
        ("Insurance Agent", "general"),
        ("Medical Device Sales Rep", "general"),
        ("Solar Sales Consultant", "general"),
        ("Advertising Sales Executive", "general"),
        ("Staffing Sales Manager", "general"),
        ("Account Executive", "sales"),
        ("Enterprise Account Executive", "sales"),
        ("Associate Account Executive", "sales"),
        ("Sales Development Representative", "sales"),
    ],
)
def test_only_software_sales_gets_the_sales_prompts(fresh: Path, title: str, expected: str) -> None:
    """The sales prompt set scores against SaaS account-executive anchors, so
    retail, device, insurance, solar, advertising and staffing sales get the
    general set; an associate-level software AE keeps the sales set."""
    answers = _persona("sales")
    answers["tracks"] = [{"name": title, "titles": [title], "said": title}]
    run_intake(answers)
    assert _filters(fresh).prompt_set == expected


def test_an_agency_partnerships_title_keeps_a_sales_track(fresh: Path) -> None:
    """'Agency' is not 'agent': a partnerships title in a software sales track
    does not move the search to the general prompt set."""
    answers = _persona("sales")
    titles = ["Agency Partnerships Manager", "Account Executive"]
    answers["tracks"] = [{"name": titles[0], "titles": titles, "said": titles[0]}]
    run_intake(answers)
    assert _filters(fresh).prompt_set == "sales"


@pytest.mark.parametrize(
    "title",
    [
        "Account Executive",
        "Enterprise Account Executive",
        "Associate Account Executive",
        "Sales Development Representative",
        "Agency Partnerships Manager",
        "Account Executive, Automotive",
        "Account Executive, Retail",
        "Account Executive, Mortgage Technology",
        "Customer Success Agent",
        "Route Optimization Sales Engineer",
    ],
)
def test_software_sales_titles_are_not_read_as_other_selling(title: str) -> None:
    from jobkit.onboarding.intake import _NOT_SOFTWARE_SALES

    assert _NOT_SOFTWARE_SALES.search(title) is None


@pytest.mark.parametrize(
    "title",
    [
        "Retail Sales Associate",
        "Insurance Agent",
        "Medical Device Sales Rep",
        "Solar Sales Consultant",
        "Advertising Sales Executive",
        "Staffing Sales Manager",
        "Car Sales Consultant",
        "Auto Sales Associate",
        "Route Sales Driver",
        "Wireless Sales Consultant",
        "Media Sales Account Executive",
        "Mortgage Loan Officer",
        "Mortgage Account Executive",
        "Wholesale Account Executive",
        "Wholesale Sales Representative",
    ],
)
def test_other_selling_is_read_as_not_software(title: str) -> None:
    from jobkit.onboarding.intake import _NOT_SOFTWARE_SALES

    assert _NOT_SOFTWARE_SALES.search(title) is not None


# ---- automatic approval and opening the forms (2026-10-22) -------------------


def _auto(home: Path) -> dict[str, Any]:
    data = read_config_yaml(home / "config" / "autoapprove.yaml")
    assert isinstance(data, dict)
    return data


def test_yes_to_auto_approval_writes_one_rule_and_no_limit(fresh: Path) -> None:
    from jobkit import rules

    out = run_intake({**ANSWERS, "auto_approve": {"enabled": True, "said": "yes, approve them"}})
    text = (fresh / "config" / "autoapprove.yaml").read_text()
    assert text.startswith(GENERATED_MARK)
    assert '# you said: "yes, approve them"' in text
    cfg = _auto(fresh)
    assert cfg["auto_approve"] == [
        {
            "when": {
                "score_gte": 75,
                "sanity_checked": True,
                "blocking_flags": "none",
                "location_known": True,
                "employer_posting": True,
            },
            "reason": "clears-your-bar",
        }
    ]
    assert cfg["auto_reject"] == []
    # no daily limit unless they name one: the key is not written at all
    assert rules.MAX_PER_DAY_KEY not in cfg and rules.MAX_PER_DAY_KEY not in text
    assert "automatic approval at a fit score of 75 or more, no daily limit" in out
    # the rule uses only known condition keys
    checked = {
        "fit_score": 80,
        "flags": ["no-range-where-required"],  # not a deal-breaker
        "sanity_job_id": 1,
        "demand_score": 3,
        "remote_stated": "remote",
        "remote_actual": "remote",
        "board_id": 4,
    }
    assert rules._cond_matches(cfg["auto_approve"][0]["when"], checked)
    # a job whose sanity checks did not finish is never approved by it
    unchecked = {**checked, "sanity_job_id": None, "demand_score": None}
    assert not rules._cond_matches(cfg["auto_approve"][0]["when"], unchecked)
    # nor one with a deal-breaker flag, an unclear location or no employer posting
    for held in (
        {"flags": ["remote-conflict: says remote, needs 4 office days"]},
        {"remote_actual": "unclear"},
        {"board_id": None},
    ):
        assert not rules._cond_matches(cfg["auto_approve"][0]["when"], {**checked, **held})


def test_a_daily_limit_is_written_only_when_given(
    fresh: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import rules

    answers = {**ANSWERS, "auto_approve": {"enabled": True, "min_score": 82, "per_day": 6}}
    out = run_intake(answers)
    cfg = _auto(fresh)
    assert cfg["auto_approve"][0]["when"] == {
        "score_gte": 82,
        "sanity_checked": True,
        "blocking_flags": "none",
        "location_known": True,
        "employer_posting": True,
    }
    assert cfg[rules.MAX_PER_DAY_KEY] == 6
    assert "at most 6 a day" in out
    monkeypatch.setattr(rules, "RULES_PATH", str(fresh / "config" / "autoapprove.yaml"))
    assert rules.max_auto_approve_per_day(rules._load_rules()) == 6


def test_no_to_auto_approval_writes_no_rule(fresh: Path) -> None:
    run_intake({**ANSWERS, "auto_approve": {"enabled": False, "said": "I'll pick myself"}})
    cfg = _auto(fresh)
    assert cfg["auto_approve"] == [] and cfg["auto_reject"] == []
    assert "max_auto_approve_per_day" not in cfg
    assert "I'll pick myself" in (fresh / "config" / "autoapprove.yaml").read_text()


def test_rerun_rewrites_the_interviews_own_rule(fresh: Path) -> None:
    run_intake({**ANSWERS, "auto_approve": {"enabled": True, "per_day": 4}})
    run_intake({**ANSWERS, "auto_approve": {"enabled": True, "min_score": 70}})
    cfg = _auto(fresh)
    assert cfg["auto_approve"][0]["when"]["score_gte"] == 70
    assert "max_auto_approve_per_day" not in cfg
    run_intake({**ANSWERS, "auto_approve": {"enabled": False}})
    assert _auto(fresh)["auto_approve"] == []
    # no answer on a rerun: the file is kept as it is
    run_intake({**ANSWERS, "auto_approve": {"enabled": True}})
    out = run_intake(ANSWERS)
    assert "kept as they are: config/autoapprove.yaml" in out
    assert _auto(fresh)["auto_approve"][0]["reason"] == "clears-your-bar"


def test_rules_someone_added_are_kept_even_with_replace_existing(jobkit_home: Path) -> None:
    """The test profile's autoapprove.yaml stands in for the owner's: approve
    and reject rules accepted one by one. The interview never rewrites it,
    not even when the person says yes to replacing their filters."""
    auto = jobkit_home / "config" / "autoapprove.yaml"
    before = auto.read_text()
    rules_before = yaml.safe_load(before)
    assert rules_before["auto_approve"] and rules_before["auto_reject"]
    for name in ("filters", "commute_table"):  # let the interview write these
        (jobkit_home / "config" / f"{name}.yaml").unlink()
    out = run_intake({**ANSWERS, "auto_approve": {"enabled": True, "per_day": 3}})
    assert auto.read_text() == before
    assert "already holds standing rules, so it was kept as it is" in out
    assert "score_gte: 75" in out and "max_auto_approve_per_day: 3" in out
    assert "replace_existing" not in out
    # no to automatic approval leaves it alone too
    run_intake({**ANSWERS, "auto_approve": {"enabled": False}})
    assert auto.read_text() == before
    # replace_existing replaces the filters, never the rules: yes or no
    for choice in ({"enabled": True}, {"enabled": False}):
        out = run_intake({**ANSWERS, "auto_approve": choice}, replace_existing=True)
        assert auto.read_text() == before, choice
        assert "config/autoapprove.yaml" in out.split("kept as they are: ")[1]
    assert _auto(jobkit_home)["auto_approve"] == rules_before["auto_approve"]
    assert _auto(jobkit_home)["auto_reject"] == rules_before["auto_reject"]
    assert not list((jobkit_home / "data" / "setup" / "backup").glob("autoapprove.yaml.*"))


def test_a_no_names_the_rule_to_delete_when_others_were_added(jobkit_home: Path) -> None:
    """A file with the clears-your-bar rule plus a rule someone added: a no
    keeps the file and says which rule to delete."""
    auto = jobkit_home / "config" / "autoapprove.yaml"
    raw = yaml.safe_load(auto.read_text())
    raw["auto_approve"].append(
        {"when": {"score_gte": 75, "sanity_flags": "none"}, "reason": "clears-your-bar"}
    )
    auto.write_text(yaml.safe_dump(raw, sort_keys=False))
    before = auto.read_text()
    out = run_intake({**ANSWERS, "auto_approve": {"enabled": False}}, replace_existing=True)
    assert auto.read_text() == before
    assert 'delete the rule with reason "clears-your-bar" under auto_approve' in out


@pytest.mark.parametrize(
    ("auto", "message"),
    [
        ({"enabled": True, "per_day": 0}, "per_day must be 1 or more"),
        ({"enabled": True, "per_day": -1}, "per_day must be 1 or more"),
        ({"enabled": True, "per_day": True}, "per_day"),
        ({"enabled": True, "per_day": 2.5}, "per_day"),
        ({"enabled": False, "per_day": 5}, "leave it out when enabled is false"),
        ({"enabled": True, "min_score": 120}, "min_score must be 0-100"),
        ({"enabled": True, "min_score": True}, "min_score"),
        ({"enabled": True, "min_score": "80"}, "min_score"),
        ({"min_score": 80}, "enabled"),
    ],
)
def test_bad_auto_approve_answers_write_nothing(
    fresh: Path, auto: dict[str, Any], message: str
) -> None:
    with pytest.raises(SetupError, match=message):
        run_intake({**ANSWERS, "auto_approve": auto})
    assert not (fresh / "config" / "autoapprove.yaml").exists()
    assert not (fresh / "config" / "filters.yaml").exists()


def test_forms_open_after_the_daily_run_by_default(fresh: Path) -> None:
    run_intake(ANSWERS)
    path = fresh / "config" / "filters.yaml"
    assert load_filters(path).open_after_daily_run is True
    run_intake({**ANSWERS, "open_forms": {"after_daily_run": False, "said": "I'll open them"}})
    text = path.read_text()
    assert load_filters(path).open_after_daily_run is False
    assert '# you said: "I\'ll open them"' in text
    assert "\nstaging:\n" in text


def test_mcp_run_intake_takes_the_new_answers(fresh: Path) -> None:
    import json

    answers = {
        **ANSWERS,
        "auto_approve": {"enabled": True, "per_day": 2},
        "open_forms": {"after_daily_run": True},
    }
    out = mcp_tools.run_intake(json.dumps(answers))
    assert "at most 2 a day" in out
    assert _auto(fresh)["max_auto_approve_per_day"] == 2
    assert load_filters(fresh / "config" / "filters.yaml").open_after_daily_run is True


# ---- web search for alert jobs (2026-10-22) -----------------------------------


def test_no_search_answer_writes_no_search_block(fresh: Path) -> None:
    run_intake(ANSWERS)
    path = fresh / "config" / "filters.yaml"
    assert "\nsearch:\n" not in path.read_text()
    assert load_filters(path).alert_web_search is False
    assert load_filters(path).max_searches_per_check is None


def test_yes_to_search_writes_it_on_with_no_limit(fresh: Path) -> None:
    run_intake({**ANSWERS, "alert_search": {"enabled": True, "said": "yes, go find them"}})
    path = fresh / "config" / "filters.yaml"
    text = path.read_text()
    assert '# you said: "yes, go find them"' in text
    assert "alert_web_search: true" in text and "max_searches_per_check" not in text
    f = load_filters(path)
    assert f.alert_web_search is True and f.max_searches_per_check is None


def test_a_search_limit_is_written_only_when_given(fresh: Path) -> None:
    run_intake({**ANSWERS, "alert_search": {"enabled": True, "per_check": 8}})
    f = load_filters(fresh / "config" / "filters.yaml")
    assert f.alert_web_search is True and f.max_searches_per_check == 8
    run_intake({**ANSWERS, "alert_search": {"enabled": False, "said": "no searching"}})
    f = load_filters(fresh / "config" / "filters.yaml")
    assert f.alert_web_search is False and f.max_searches_per_check is None


@pytest.mark.parametrize(
    ("search", "message"),
    [
        ({"enabled": True, "per_check": 0}, "per_check must be 1 or more"),
        ({"enabled": True, "per_check": True}, "per_check"),
        ({"enabled": True, "per_check": "5"}, "per_check"),
        ({"enabled": False, "per_check": 5}, "leave it out when enabled is false"),
        ({"enabled": "yes"}, "enabled"),
        ({"per_check": 3}, "enabled"),
    ],
)
def test_bad_search_answers_write_nothing(
    fresh: Path, search: dict[str, Any], message: str
) -> None:
    with pytest.raises(SetupError, match=message):
        run_intake({**ANSWERS, "alert_search": search})
    assert not (fresh / "config" / "filters.yaml").exists()


def test_mcp_run_intake_takes_the_search_answer(fresh: Path) -> None:
    import json

    mcp_tools.run_intake(json.dumps({**ANSWERS, "alert_search": {"enabled": True}}))
    assert load_filters(fresh / "config" / "filters.yaml").alert_web_search is True


# ---- scoring limit and the auto-approve flag lists (owner decisions D2, D3) ----


def test_no_scoring_limit_unless_the_person_names_one(fresh: Path) -> None:
    run_intake(ANSWERS)
    filters = load_filters(fresh / "config" / "filters.yaml")
    assert filters.max_jobs_per_stage is None
    # the flag lists that hold a job back from automatic approval come with
    # their documentation, at the owner's defaults
    assert filters.auto_approve_flags == config.AutoApproveFlags(
        blocking=config.AUTO_APPROVE_BLOCKING_FLAGS,
        non_blocking=filters.auto_approve_flags.non_blocking,
    )
    assert set(filters.auto_approve_flags.non_blocking) == set(
        config.AUTO_APPROVE_NON_BLOCKING_FLAGS
    )
    text = (fresh / "config" / "filters.yaml").read_text()
    assert "blocking_flags: none" in text  # the comment naming the rule condition


def test_a_scoring_limit_is_written_when_given(fresh: Path) -> None:
    run_intake({**ANSWERS, "scoring_limit": {"per_stage": 40, "said": "40 a night is plenty"}})
    text = (fresh / "config" / "filters.yaml").read_text()
    assert '# you said: "40 a night is plenty"' in text
    assert load_filters(fresh / "config" / "filters.yaml").max_jobs_per_stage == 40
    assert [ln for ln in text.splitlines() if ln.startswith("triage:")] == ["triage:"]


@pytest.mark.parametrize("bad", [0, -2, True, "40"])
def test_a_bad_scoring_limit_is_refused(fresh: Path, bad: object) -> None:
    with pytest.raises(SetupError, match="per_stage"):
        run_intake({**ANSWERS, "scoring_limit": {"per_stage": bad}})


def test_the_note_for_a_kept_rule_file_names_the_new_rule(fresh: Path) -> None:
    (fresh / "config" / "autoapprove.yaml").write_text(
        'version: "1.0"\nauto_approve:\n  - when: { tier: 1 }\n    reason: "mine"\n'
    )
    out = run_intake({**ANSWERS, "auto_approve": {"enabled": True, "min_score": 78}})
    assert (
        "{ score_gte: 78, sanity_checked: true, blocking_flags: none, location_known: true, "
        "employer_posting: true }" in out
    )
