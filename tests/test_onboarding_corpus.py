"""Onboarding 1.3-1.6 — the human confirms the draft; promote_corpus writes
data/career_corpus.yaml only when every entry is confirmed. metric_confidence
and evidence come from the human and are never defaulted (hard rule 6)."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from jobkit import mcp_server
from jobkit.config import load_filters
from jobkit.onboarding import mcp_tools
from jobkit.onboarding.corpus_confirm import (
    confirm_bullets,
    draft_status,
    promote_corpus,
    update_draft,
)
from jobkit.onboarding.files import GENERATED_MARK, SetupError
from jobkit.onboarding.ingest import ingest_resume
from jobkit.pipeline import triage
from jobkit.validate.claims import source_registry
from tests.conftest import FIXTURES, REPO_ROOT

TXT = FIXTURES / "resumes" / "sample_resume.txt"

DECISIONS: list[dict[str, Any]] = [
    {"id": "b_001", "action": "keep", "metric_confidence": "verified", "evidence": "CS report"},
    {
        "id": "b_002",
        "action": "keep",
        "metric_confidence": "estimate",
        "evidence": "onboarding tracker",
        "text_short": "Cut onboarding from 60 to 35 days.",
    },
    {
        "id": "b_003",
        "action": "edit",
        "text_long": "Ran quarterly business reviews with executive sponsors at 20 accounts.",
        "metric": "20 accounts",
        "metric_confidence": "estimate",
        "evidence": "my calendar",
    },
    {"id": "b_004", "action": "drop"},
    {"id": "b_005", "action": "keep", "metric_confidence": "verified", "evidence": "dashboard"},
    {"id": "b_006", "action": "keep", "metric_confidence": "verified", "evidence": "help desk"},
    {"id": "b_007", "action": "keep", "metric_confidence": "none"},
    {"id": "b_008", "action": "keep", "metric_confidence": "verified", "evidence": "HR list"},
]
CONFIRM_REST: dict[str, Any] = {
    "identity": {"display_location": "Denver, CO", "home_city": "Denver, CO"},
    "roles": [{"id": "role_fernbrook_software"}, {"id": "role_harbor_health_systems"}],
    "education": [{"index": 0}],
    "positioning": [{"id": "pos_resume_summary", "archetype": "customer success leadership"}],
}


def _draft(home: Path) -> dict[str, Any]:
    data = yaml.safe_load((home / "data" / "career_corpus.draft.yaml").read_text())
    assert isinstance(data, dict)
    return data


@pytest.fixture()
def drafted(jobkit_home: Path) -> Path:
    ingest_resume(TXT)
    return jobkit_home


def test_status_walks_bullets_five_at_a_time(drafted: Path) -> None:
    status = draft_status()
    assert [b["id"] for b in status.batch] == ["b_001", "b_002", "b_003", "b_004", "b_005"]
    text = status.render()
    assert "metric_confidence (verified | estimate | none)" in text
    assert "display_location" in text and "Roles to confirm" in text


@pytest.mark.parametrize(
    "bad, message",
    [
        ({"id": "b_001", "action": "keep", "evidence": "x"}, "metric_confidence must be set"),
        (
            {"id": "b_001", "action": "keep", "metric_confidence": "verified"},
            "evidence is required",
        ),
        ({"id": "b_001", "action": "keep", "metric_confidence": "none"}, "states a figure"),
        (
            {"id": "b_001", "action": "keep", "metric_confidence": "unverified", "evidence": "x"},
            "metric_confidence must be set",
        ),
        (
            {"id": "b_007", "action": "keep", "metric_confidence": "none", "text_long": "Better."},
            "use action edit",
        ),
        ({"id": "b_007", "action": "edit", "metric_confidence": "none"}, "text_long"),
        ({"id": "b_999", "action": "drop"}, "unknown bullet"),
        ({"id": "b_007", "action": "approve"}, "action must be one of"),
        ({"id": "b_007", "action": "keep", "metric_confidence": "none", "conf": 1}, "unknown"),
    ],
)
def test_bad_decisions_apply_nothing(drafted: Path, bad: dict[str, Any], message: str) -> None:
    good = {"id": "b_008", "action": "keep", "metric_confidence": "verified", "evidence": "HR"}
    with pytest.raises(SetupError, match=message):
        confirm_bullets([good, bad])
    # all or nothing: the valid decision was not applied either
    b8 = next(b for b in _draft(drafted)["bullets"] if b["id"] == "b_008")
    assert b8["status"] == "pending" and b8["metric_confidence"] is None


@pytest.mark.parametrize(
    "bad, messages",
    [
        # an edit that adds a figure: `metric` was None in the draft, the new
        # wording states 45%
        (
            {
                "id": "b_003",
                "action": "edit",
                "text_long": "Responsible for quarterly business reviews with executive "
                "sponsors, lifting revenue 45%.",
                "metric_confidence": "none",
            },
            ["b_003: the bullet states a figure (45%)", "b_003: evidence is required"],
        ),
        # a keep that clears `metric` while the text still states both figures
        (
            {"id": "b_001", "action": "keep", "metric": None, "metric_confidence": "none"},
            [
                "b_001: metric cannot be cleared while the wording states figures (48; $3.2M)",
                "b_001: the bullet states a figure (48; $3.2M)",
                "b_001: evidence is required",
            ],
        ),
        # clearing `metric` is refused even with a confidence and evidence:
        # the wording still states the figures
        (
            {
                "id": "b_001",
                "action": "keep",
                "metric": None,
                "metric_confidence": "verified",
                "evidence": "CS report",
            },
            ["metric cannot be cleared while the wording states figures"],
        ),
        # a figure added only in the short variant counts too
        (
            {
                "id": "b_004",
                "action": "keep",
                "metric_confidence": "none",
                "text_short": "Led the renewal forecast (94% accurate).",
            },
            ["b_004: the bullet states a figure (94%)"],
        ),
    ],
)
def test_figure_guard_reads_the_wording(
    drafted: Path, bad: dict[str, Any], messages: list[str]
) -> None:
    """The figure guard re-reads text_long and text_short: a figure the
    wording states needs a confidence other than none and evidence, whatever
    the caller passed as `metric` (hard rule 6)."""
    with pytest.raises(SetupError) as exc:
        confirm_bullets([bad])
    for message in messages:
        assert message in str(exc.value)
    b = next(b for b in _draft(drafted)["bullets"] if b["id"] == bad["id"])
    assert b["status"] == "pending" and b["metric_confidence"] is None


def test_edit_takes_the_figures_of_its_new_wording(drafted: Path) -> None:
    confirm_bullets(
        [
            # new wording with a new figure and no `metric`: the metric follows it
            {
                "id": "b_003",
                "action": "edit",
                "text_long": "Ran quarterly business reviews with 12 executive sponsors.",
                "metric_confidence": "estimate",
                "evidence": "my calendar",
            },
            # an edit that removes the figures may clear the metric
            {
                "id": "b_008",
                "action": "edit",
                "text_long": "Trained new support hires.",
                "metric": None,
                "metric_confidence": "none",
            },
            # an edit with no `metric` keeps none of the old wording's figures
            {
                "id": "b_005",
                "action": "edit",
                "text_long": "Led a pilot of in-app guides.",
                "metric_confidence": "none",
            },
        ]
    )
    bullets = {b["id"]: b for b in _draft(drafted)["bullets"]}
    assert bullets["b_003"]["metric"] == "12"
    assert bullets["b_008"]["metric"] is None
    assert bullets["b_005"]["metric"] is None and bullets["b_005"]["evidence"] is None


def test_promotion_rereads_the_wording(drafted: Path) -> None:
    """A draft bullet whose text states a figure while it carries
    metric_confidence none and no evidence (the draft edited by hand, or
    written before this check) is not promoted."""
    confirm_bullets(DECISIONS)
    update_draft(CONFIRM_REST)
    draft = _draft(drafted)
    b7 = next(b for b in draft["bullets"] if b["id"] == "b_007")
    b7["text_long"] = "Helped migrate 3,000 knowledge base articles to a new help center."
    (drafted / "data" / "career_corpus.draft.yaml").write_text(
        yaml.safe_dump(draft, sort_keys=False)
    )
    with pytest.raises(SetupError) as exc:
        promote_corpus()
    text = str(exc.value)
    assert "bullet b_007: states a figure (3,000) but metric_confidence is none" in text
    assert "bullet b_007: states a figure (3,000) but has no evidence" in text
    assert not (drafted / "data" / "career_corpus.yaml").exists()


def test_decisions_record_the_humans_values(drafted: Path) -> None:
    confirm_bullets(DECISIONS)
    bullets = {b["id"]: b for b in _draft(drafted)["bullets"]}
    assert bullets["b_001"]["status"] == "kept"
    assert bullets["b_001"]["metric_confidence"] == "verified"
    assert bullets["b_001"]["evidence"] == "CS report"
    assert bullets["b_002"]["text_short"] == "Cut onboarding from 60 to 35 days."
    edited = bullets["b_003"]
    assert edited["status"] == "edited" and edited["metric"] == "20 accounts"
    assert (
        edited["original_text"]
        == "Responsible for quarterly business reviews with executive sponsors."
    )
    assert edited["text_short"] == edited["text_long"]
    assert bullets["b_004"]["status"] == "dropped"
    assert bullets["b_007"]["evidence"] is None and bullets["b_007"]["metric_confidence"] == "none"


def test_promote_refuses_until_everything_is_confirmed(drafted: Path) -> None:
    with pytest.raises(SetupError) as exc:
        promote_corpus()
    text = str(exc.value)
    assert "8 bullet(s) not confirmed" in text
    assert "identity not confirmed" in text and "display_location" in text
    assert "role role_fernbrook_software" in text and "education[0]" in text
    confirm_bullets(DECISIONS)
    with pytest.raises(SetupError, match="identity not confirmed"):
        promote_corpus()
    assert not (drafted / "data" / "career_corpus.yaml").exists()


def test_promoted_corpus(drafted: Path) -> None:
    confirm_bullets(DECISIONS)
    update_draft(CONFIRM_REST)
    report = promote_corpus()
    path = drafted / "data" / "career_corpus.yaml"
    assert path.read_text().startswith(GENERATED_MARK)
    corpus = yaml.safe_load(path.read_text())
    ids = [b["id"] for b in corpus["bullets"]]
    assert ids == ["b_001", "b_002", "b_003", "b_005", "b_006", "b_007", "b_008"]
    for b in corpus["bullets"]:
        assert b["metric_confidence"] in ("verified", "estimate", "none")
        assert set(b) == {
            "id",
            "role_id",
            "text_long",
            "text_short",
            "tags",
            "metric",
            "metric_confidence",
            "evidence",
        }
        if b["metric"]:
            assert b["evidence"]
    assert corpus["identity"]["display_location"] == "Denver, CO"
    assert corpus["roles"][0] == {
        "id": "role_fernbrook_software",
        "company": "Fernbrook Software",
        "title": "Customer Success Manager",
        "start": "2021-06",
        "end": None,
        "location": "Denver, CO",
    }
    # no draft bookkeeping leaks into the corpus
    text = path.read_text()
    for key in ("status:", "header_lines", "confirmed:", "notes:", "original_text"):
        assert key not in text
    # honest about a thin corpus
    assert "THIN CORPUS: 7 bullets" in report


def test_promoted_corpus_works_downstream(drafted: Path) -> None:
    confirm_bullets(DECISIONS)
    update_draft(CONFIRM_REST)
    promote_corpus()
    corpus = yaml.safe_load((drafted / "data" / "career_corpus.yaml").read_text())
    profile = triage._profile_summary(
        corpus=corpus, filters=load_filters(), today=datetime.date(2026, 10, 12)
    )
    assert "Customer Success Manager · Fernbrook Software" in profile
    registry = source_registry(corpus)
    assert "b_001" in registry
    health = mcp_server.corpus_health()
    assert "bullets: 7" in health


def test_existing_corpus_is_protected(drafted: Path) -> None:
    confirm_bullets(DECISIONS)
    update_draft(CONFIRM_REST)
    corpus = drafted / "data" / "career_corpus.yaml"
    corpus.write_text("# my own corpus\ncorpus_version: '1.0'\nbullets: []\n")
    with pytest.raises(SetupError, match="not written by the setup interview"):
        promote_corpus()
    assert corpus.read_text().startswith("# my own corpus")
    promote_corpus(replace_existing=True)
    backups = list((drafted / "data" / "setup" / "backup").glob("career_corpus.yaml.*"))
    assert len(backups) == 1 and backups[0].read_text().startswith("# my own corpus")
    assert corpus.read_text().startswith(GENERATED_MARK)


def test_role_and_identity_answers_are_validated(drafted: Path) -> None:
    with pytest.raises(SetupError, match="YYYY-MM"):
        update_draft({"roles": [{"id": "role_harbor_health_systems", "start": "Aug 2017"}]})
    with pytest.raises(SetupError, match="unknown role"):
        update_draft({"roles": [{"id": "role_nope"}]})
    with pytest.raises(SetupError, match="unknown section"):
        update_draft({"bullets": []})
    update_draft(
        {
            "roles": [
                {
                    "id": "role_harbor_health_systems",
                    "title": "Support Specialist II",
                    "end": "2021-06",
                }
            ],
            "add_roles": [
                {
                    "company": "Juniper Clinic",
                    "title": "Front Desk Lead",
                    "start": "2015-05",
                    "end": "2017-07",
                }
            ],
            "skill_index": {"Gainsight": "expert", "SQL": "working"},
            "constraints": {"never_claim_titles": ["Director of Customer Success"]},
            "voice_samples": [{"source": "email to a customer", "text": "Here is what we fixed."}],
        }
    )
    draft = _draft(drafted)
    harbor = next(r for r in draft["roles"] if r["id"] == "role_harbor_health_systems")
    assert harbor["title"] == "Support Specialist II" and harbor["end"] == "2021-06"
    assert harbor["confirmed"]
    added = next(r for r in draft["roles"] if r["company"] == "Juniper Clinic")
    assert added["confirmed"] and added["start"] == "2015-05"
    assert draft["skill_index"] == {"Gainsight": "expert", "SQL": "working"}
    assert draft["voice_samples"][0]["id"] == "v_001"


def test_bullet_can_move_to_another_role(drafted: Path) -> None:
    confirm_bullets(
        [
            {
                "id": "b_007",
                "action": "keep",
                "metric_confidence": "none",
                "role_id": "role_fernbrook_software",
                "tags": ["Knowledge Base", "support"],
            }
        ]
    )
    b7 = next(b for b in _draft(drafted)["bullets"] if b["id"] == "b_007")
    assert b7["role_id"] == "role_fernbrook_software"
    assert b7["tags"] == ["knowledge-base", "support"]


def test_mcp_wrappers_take_json_strings(drafted: Path) -> None:
    import json

    out = mcp_tools.confirm_bullets(json.dumps(DECISIONS))
    assert "applied:" in out
    out = mcp_tools.update_corpus_draft(json.dumps(CONFIRM_REST))
    assert "Ready: promote_corpus" in out
    assert "wrote data/career_corpus.yaml" in mcp_tools.promote_corpus()
    with pytest.raises(SetupError):
        mcp_tools.confirm_bullets("{not json")


# ---------------------------------------------------------------------------
# an ended role is never promoted as current (end null reads as "Present")
# ---------------------------------------------------------------------------

HARBOR = "role_harbor_health_systems"


@pytest.fixture()
def year_only(jobkit_home: Path) -> Path:
    """The second role's dates are years only ("2017 – 2021"): the parser
    leaves start and end unset and notes it, rather than invent a month."""
    ingest_resume(text=TXT.read_text().replace("Aug 2017 – May 2021", "2017 – 2021"))
    harbor = next(r for r in _draft(jobkit_home)["roles"] if r["id"] == HARBOR)
    assert (harbor["start"], harbor["end"], harbor["current"]) == (None, None, False)
    confirm_bullets(DECISIONS)
    rest = {k: v for k, v in CONFIRM_REST.items() if k != "roles"}
    update_draft(rest | {"roles": [{"id": "role_fernbrook_software"}]})
    return jobkit_home


def test_a_year_only_end_confirmed_with_only_a_start_is_refused(year_only: Path) -> None:
    with pytest.raises(SetupError, match="end date not set") as exc:
        update_draft({"roles": [{"id": HARBOR, "start": "2017-08"}]})
    assert "current: true" in str(exc.value) and "2017 – 2021" in str(exc.value)
    harbor = next(r for r in _draft(year_only)["roles"] if r["id"] == HARBOR)
    assert not harbor["confirmed"] and harbor["start"] is None  # nothing applied
    with pytest.raises(SetupError, match=f"role {HARBOR} .* not confirmed"):
        promote_corpus()
    assert not (year_only / "data" / "career_corpus.yaml").exists()


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        ({"end": None}, "no end date given"),  # a null end is not "current"
        ({"end": ""}, "no end date given"),
        ({"current": False}, "no end date given"),
        ({"current": True, "end": "2021-05"}, "contradict"),
        ({"current": "yes"}, "current must be true or false"),
        ({"end": "2021"}, "YYYY-MM"),
    ],
)
def test_an_end_must_be_a_month_or_an_explicit_current(
    year_only: Path, answer: dict[str, Any], message: str
) -> None:
    with pytest.raises(SetupError, match=message):
        update_draft({"roles": [{"id": HARBOR, "start": "2017-08", **answer}]})


def test_the_humans_end_or_current_answer_is_promoted_as_given(year_only: Path) -> None:
    update_draft({"roles": [{"id": HARBOR, "start": "2017-08", "end": "2021-05"}]})
    promote_corpus()
    corpus = yaml.safe_load((year_only / "data" / "career_corpus.yaml").read_text())
    harbor = next(r for r in corpus["roles"] if r["id"] == HARBOR)
    assert (harbor["start"], harbor["end"]) == ("2017-08", "2021-05")
    # and a role the person says they still hold, said explicitly
    update_draft({"roles": [{"id": HARBOR, "current": True}]})
    harbor = next(r for r in _draft(year_only)["roles"] if r["id"] == HARBOR)
    assert (harbor["end"], harbor["current"]) == (None, True)
    update_draft({"roles": [{"id": HARBOR, "end": "present"}]})  # the word, said
    update_draft({"roles": [{"id": HARBOR, "end": "2021-05"}]})
    harbor = next(r for r in _draft(year_only)["roles"] if r["id"] == HARBOR)
    assert (harbor["end"], harbor["current"]) == ("2021-05", False)


def test_a_draft_saved_before_the_end_rule_cannot_promote_a_role_as_current(
    year_only: Path,
) -> None:
    """A role confirmed with end null and current false (how an earlier
    version left a year-only role) is blocked at promotion, not written as
    a current role."""
    path = year_only / "data" / "career_corpus.draft.yaml"
    draft = yaml.safe_load(path.read_text())
    harbor = next(r for r in draft["roles"] if r["id"] == HARBOR)
    harbor.update({"start": "2017-08", "end": None, "current": False, "confirmed": True})
    path.write_text(yaml.safe_dump(draft, sort_keys=False))
    with pytest.raises(SetupError, match=f"role {HARBOR} .*end date not set"):
        promote_corpus()
    assert not (year_only / "data" / "career_corpus.yaml").exists()


def test_an_added_role_needs_an_end_or_current(drafted: Path) -> None:
    role = {"company": "Juniper Clinic", "title": "Front Desk Lead", "start": "2015-05"}
    with pytest.raises(SetupError, match="no end date given"):
        update_draft({"add_roles": [role]})
    assert not any(r["company"] == "Juniper Clinic" for r in _draft(drafted)["roles"])
    update_draft({"add_roles": [role | {"current": True}]})
    added = next(r for r in _draft(drafted)["roles"] if r["company"] == "Juniper Clinic")
    assert (added["end"], added["current"]) == (None, True)


# ---------------------------------------------------------------------------
# display location: a city, never empty (every tailored header shows it)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_an_empty_display_location_is_refused(drafted: Path, empty: str | None) -> None:
    with pytest.raises(SetupError, match="display_location must be a city"):
        update_draft({"identity": {"display_location": empty, "home_city": "Denver, CO"}})
    ident = _draft(drafted)["identity"]
    assert ident["display_location"] is None and not ident["confirmed"]


def test_a_promoted_corpus_tailors_on_a_fresh_config(drafted: Path) -> None:
    """The fresh-user path: filters.yaml from the template has no
    location_strategy default, so the header city comes from the corpus.
    A draft saved with an empty display location (an earlier version took
    "" for none) is blocked at promotion instead of failing every tailor."""
    from jobkit.config import Filters
    from jobkit.pipeline import tailor

    confirm_bullets(DECISIONS)
    update_draft(CONFIRM_REST)
    path = drafted / "data" / "career_corpus.draft.yaml"
    draft = yaml.safe_load(path.read_text())
    draft["identity"]["display_location"] = ""
    path.write_text(yaml.safe_dump(draft, sort_keys=False))
    with pytest.raises(SetupError, match="display_location not set"):
        promote_corpus()
    update_draft({"identity": {"display_location": "Denver, CO"}})
    promote_corpus()
    corpus = yaml.safe_load((drafted / "data" / "career_corpus.yaml").read_text())
    template = yaml.safe_load((REPO_ROOT / "config" / "filters.example.yaml").read_text())
    assert not (template.get("location_strategy") or {}).get("default")
    job = {"location_raw": "Austin, TX", "remote": 0, "location_bucket": "other-tx"}
    assert tailor._display_location(job, Filters(raw=template), corpus) == "Denver, CO"


# ---------------------------------------------------------------------------
# figures verified, and skills from the resume
# ---------------------------------------------------------------------------


def test_thin_corpus_counts_only_bullets_that_state_a_figure(drafted: Path) -> None:
    confirm_bullets(DECISIONS)
    update_draft(CONFIRM_REST)
    report = promote_corpus()
    # b_007 states no figure, so it does not count against the verified share
    assert "4 of 6 figures verified" in report
    assert "THIN CORPUS: 7 bullets, 4 of 6 figures verified" in report


def _bullets(n: int, figures: int, verified: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(n):
        figure = i < figures
        out.append(
            {
                "text_long": f"Cared for {i + 2} patients a shift." if figure else "Charted care.",
                "metric_confidence": ("verified" if i < verified else "estimate")
                if figure
                else "none",
            }
        )
    return out


@pytest.mark.parametrize(
    "n, figures, verified, thin",
    [
        (30, 0, 0, False),  # duty bullets with no number do not make a corpus thin
        (30, 4, 4, False),
        (30, 10, 3, True),  # 30% of the figures verified
        (29, 0, 0, True),  # the bullet count still counts
    ],
)
def test_is_thin(n: int, figures: int, verified: int, thin: bool) -> None:
    from jobkit.onboarding.corpus_confirm import figure_counts, is_thin

    bullets = _bullets(n, figures, verified)
    assert figure_counts(bullets) == (figures, verified)
    assert is_thin(bullets) is thin


@pytest.fixture()
def engineer(jobkit_home: Path) -> Path:
    ingest_resume(FIXTURES / "onboarding_personas" / "engineer.txt")
    return jobkit_home


def test_resume_skills_reach_skill_index_only_when_confirmed(engineer: Path) -> None:
    from jobkit.onboarding.corpus_confirm import build_corpus, load_draft
    from jobkit.onboarding.files import setup_files

    draft = _draft(engineer)
    assert draft["skills_on_resume"] == [
        "Go",
        "Python",
        "PostgreSQL",
        "Kubernetes",
        "AWS",
        "Terraform",
    ]
    assert draft["skill_index"] == {}  # nothing carried over by itself
    status = draft_status().render()
    assert "Skills on the resume not decided yet (6): Go, Python" in status
    out = update_draft(
        {
            "resume_skills": [
                {"name": "go", "depth": "daily since 2017"},  # spelled as the resume does
                {"name": "Terraform", "drop": True},
            ]
        }
    )
    assert "not decided yet (4): Python, PostgreSQL, Kubernetes, AWS" in out
    draft = _draft(engineer)
    assert draft["skill_index"] == {"Go": "daily since 2017"}
    assert draft["skills_declined"] == ["Terraform"]
    corpus = build_corpus(load_draft(setup_files()))
    assert corpus["skill_index"] == {"Go": "daily since 2017"}


@pytest.mark.parametrize(
    "item, message",
    [
        ({"name": "Rust", "depth": "some"}, "not a skill the resume lists"),
        ({"name": "Python"}, "depth is the person's own words"),
        ({"name": "Python", "depth": "  "}, "depth is the person's own words"),
        ({"name": "Python", "depth": "daily", "level": 3}, "unknown field"),
    ],
)
def test_resume_skills_are_refused_without_the_persons_answer(
    engineer: Path, item: dict[str, Any], message: str
) -> None:
    before = _draft(engineer)
    with pytest.raises(SetupError, match=message):
        update_draft({"resume_skills": [item]})
    assert _draft(engineer) == before


def test_licences_and_projects_are_offered_and_reach_the_profile(jobkit_home: Path) -> None:
    """A licence line from the resume's other sections is offered like a
    listed skill; kept, it is in skill_index under the resume's wording, so
    the scorer's profile shows it. Nothing is carried over by itself."""
    from jobkit.onboarding.corpus_confirm import build_corpus, load_draft
    from jobkit.onboarding.files import setup_files

    ingest_resume(FIXTURES / "onboarding_personas" / "rn.txt")
    line = "Registered Nurse, Ohio Board of Nursing, active"
    assert _draft(jobkit_home)["skill_index"] == {}
    status = draft_status().render()
    assert "Licences, certifications and projects on the resume not decided yet" in status
    assert f"[LICENSES & CERTIFICATIONS] {line}" in status
    update_draft({"resume_skills": [{"name": line, "depth": "active, renewed in 2025"}]})
    assert f"] {line};" not in draft_status().render()
    corpus = build_corpus(load_draft(setup_files()))
    assert corpus["skill_index"] == {line: "active, renewed in 2025"}
