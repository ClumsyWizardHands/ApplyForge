"""Adversarial suite of 2026-10-23: every must-fail sentence below is a
fabrication or an unsourced claim, many of them built from the words and
shapes a cover letter's false positives used ("talk", "part", "point",
"work", "covering", "specialist", "qualified", "halves", a closing "I'd
point to", the target's name before a seat word). Each must fail, with the
kind named (Hard Rules 3 and 8).

The validators are main's (5341b76): the fix round's validator allowances
were reverted on 2026-10-23. A cover letter's soft false positives are
handled at the source: the letter is written again with the exact
violations fed back, and the prompt steers clear of those shapes
(pipeline/cover_feedback.py, prompts/cover_letter.md,
tests/test_claims_false_positives_1023.py). So two kinds of case here are
marked xfail(strict=True), tracked rather than hidden: a must-fail sentence
that also passes on main (HOLE, a pre-existing gap for future hardening) and
a must-pass sentence main flags (FALSE_POSITIVE; a regeneration rewrites
it). A strict xfail that starts passing fails the suite, so a fix shows up.

Same invented candidate, employers, target and posting as the false-positive
file. Offline and LLM-free."""

from __future__ import annotations

import re
from typing import Any

import pytest

from jobkit.validate.claims import validate_claims
from jobkit.validate.provenance import numeric_claims, validate_tailored
from tests.test_claims_false_positives_1023 import (
    CITED,
    CORPUS,
    JD,
    KNOWN,
    SPECIALIST_TITLE,
    TARGET,
    TITLE,
)

HOLE = "pre-existing on main 5341b76: future hardening"
FALSE_POSITIVE = (
    "false positive on main 5341b76 (validator allowance reverted 2026-10-23): a cover "
    "letter rewrites the sentence in its next draft"
)


def _xfail(reason: str) -> pytest.MarkDecorator:
    return pytest.mark.xfail(strict=True, reason=reason)


def _track(cases: list[Any], holes: list[Any], reason: str = HOLE) -> list[Any]:
    """`cases` as parametrize values, each one in `holes` marked xfail(strict)."""
    for h in holes:
        assert h in cases, h
    return [
        pytest.param(*(c if isinstance(c, tuple) else (c,)), marks=_xfail(reason))
        if c in holes
        else c
        for c in cases
    ]


# the posting, with a product name and a seat of its own
ADV_JD = (
    JD + " Our flagship product, Latex Cloud, serves hospitals nationwide. The role reports to "
    "the Chief Revenue Officer."
)

EXPERIENCE = "unsourced_experience_claim"
NUMBER = "fabricated_number"
EMPLOYER = "fabricated_employer"
PROPER = "unsourced_proper_noun"
TITLE_KIND = "never_claim_title"
CROSS = "cross_role_attribution"

CASES: list[tuple[str, str]] = [
    # ---- unsourced claims of experience, in the fixed words and shapes ----
    ("I'd bring the part of the work most leaders skip: coaching every rep.", EXPERIENCE),
    ("I'd point to my years of covering enterprise accounts.", EXPERIENCE),
    ("Can we talk about why I'm the expert you need?", EXPERIENCE),
    ("Can we agree I ran the best floor at Larkspur Health?", EXPERIENCE),
    ("When can we talk about the turnaround I led at Larkspur Health?", EXPERIENCE),
    ("Can we talk this week? I'd walk in with a decade of hospital sales experience.", EXPERIENCE),
    ("I'm experienced at covering inbound, expansion, and win-back for hospitals.", EXPERIENCE),
    ("With my background, the part I'd own on day one is coaching.", EXPERIENCE),
    ("Talk tracks are second nature to me after years of cold calling.", EXPERIENCE),
    ("I know the work of covering hospital accounts inside out.", EXPERIENCE),
    ("I'd point to team managers as my specialty.", EXPERIENCE),
    ("I'm a qualified demo machine.", EXPERIENCE),
    ("I'm qualified demos royalty.", EXPERIENCE),
    ("I specialized in covering inbound at Larkspur Health.", EXPERIENCE),
    ("I was a specialist pods supervisor at Larkspur Health.", EXPERIENCE),
    ("I have specialist pods experience in aviation.", EXPERIENCE),
    (
        "I promoted shift leads into managers, which is the part of the work I'd bring to "
        "Vandelay & Company first.",
        EXPERIENCE,
    ),
    ("The result I'd point to is one nobody planned for.", EXPERIENCE),
    (
        "Which is the part of the work I'd point to first: I built specialist pods for "
        "hospital systems in aviation.",
        EXPERIENCE,
    ),
    # ---- invented or altered figures ----
    (
        "At Larkspur Health I split the inside sales desk into three specialist pods, and "
        "pipeline coverage went from 61% to 97%.",
        NUMBER,
    ),
    ("I split the inside sales desk into four specialist pods at Larkspur Health.", NUMBER),
    ("At Corvane Analytics I booked 40 qualified demos a week.", NUMBER),
    ("I hired 40 reps in 14 months at Larkspur Health.", NUMBER),
    ("At Larkspur Health I halved ramp time for new reps.", NUMBER),
    ("At Larkspur Health I cut ramp time in half.", NUMBER),
    ("At Larkspur Health I doubled pipeline coverage for both halves of the team.", NUMBER),
    (
        "I'd point to the 40 reps I led, which is the part of the work I'd point to first.",
        NUMBER,
    ),
    # ---- claimed titles ----
    (
        "As VP of Sales at Larkspur Health, I split the inside sales desk into three specialist "
        "pods.",
        TITLE_KIND,
    ),
    ("I was the Chief Revenue Officer at Larkspur Health.", TITLE_KIND),
    (
        "As Head of Enablement at Larkspur Health, I promoted five shift leads into team managers.",
        PROPER,
    ),
    # ---- the target framed as an employer ----
    ("At Vandelay & Company I split the inside sales desk into three specialist pods.", EMPLOYER),
    ("I was a Vandelay & Company sales lead before Larkspur Health.", EMPLOYER),
    ("Vandelay & Company sales lead turned founder, I know this market.", EMPLOYER),
    ("Vandelay & Company AE turned sales leader.", EMPLOYER),
    (
        "I promoted five shift leads into team managers at Initech, which is the part of the "
        "work I'd point to first.",
        EMPLOYER,
    ),
    # ---- the posting's proper nouns used as the candidate's ----
    ("At Larkspur Health I sold Latex Cloud to hospitals.", PROPER),
    ("I'd point to my Latex Cloud rollout at Larkspur Health.", PROPER),
    # ---- one role's bullet credited to another ----
    ("At Corvane Analytics I split the inside sales desk into three specialist pods.", CROSS),
    ("At Larkspur Health I turned inbound chat leads into qualified demos.", CROSS),
]


def test_the_suite_is_large_enough() -> None:
    assert len(CASES) >= 25


@pytest.mark.parametrize(("text", "kind"), CASES)
def test_a_fabrication_still_fails(text: str, kind: str) -> None:
    out = validate_claims(
        text,
        list(CITED),
        CORPUS,
        target_company=TARGET,
        job_title=TITLE,
        jd_text=ADV_JD,
        known_companies=KNOWN,
        estimate_style="confident",
    )
    assert not out.ok
    assert kind in {v["kind"] for v in out.violations}, out.violations


@pytest.mark.parametrize(
    "text",
    [
        "I was a Customer Experience Manager at Larkspur Health.",
        "As a Customer Experience Manager, I promoted five shift leads into team managers.",
        "As Customer Experience Manager at Larkspur Health I ran a floor of 14 account managers.",
    ],
)
def test_the_job_title_claimed_as_held_still_fails(text: str) -> None:
    # a marker word inside the job title ("Experience") no longer makes the
    # sentence a claim of experience; the title's own check still fails it
    out = validate_claims(
        text,
        list(CITED),
        CORPUS,
        target_company=TARGET,
        job_title="Customer Experience Manager",
        jd_text=ADV_JD,
        known_companies=KNOWN,
        estimate_style="confident",
    )
    assert "claimed_job_title" in {v["kind"] for v in out.violations}, out.violations


# ---------------------------------------------------------------------------
# A word of duration copied from a bullet ("3 years in a row") is no excuse
# for "3 years in <invented domain>" (review 2026-10-23), in a letter or a
# drafted answer
# ---------------------------------------------------------------------------

YEARS_CORPUS: dict[str, Any] = {
    **CORPUS,
    "bullets": [
        *CORPUS["bullets"],
        {
            "id": "b_105",
            "role_id": "role_larkspur",
            "metric_confidence": "verified",
            "text_long": "Grew the Midwest territory 3 years in a row.",
            "metric": "3 years",
        },
    ],
}
YEARS_CITED = [*CITED, "b_105"]

YEARS_CASES = [
    "I have 3 years in medical device sales leadership.",
    "I've got 3 years in medical device sales.",
    "I've spent 3 years in oncology sales.",
    "My 3 years in oncology sales taught me how surgeons buy.",
    "I have 3 years in a row of oncology quota attainment.",
    "I am 3 years in a row the top oncology seller.",
    "For 3 years in a row I ran oncology sales.",
]


@pytest.mark.parametrize("style", ["hedged", "confident"])
@pytest.mark.parametrize("text", YEARS_CASES)
def test_years_copied_from_a_bullet_still_fail(text: str, style: str) -> None:
    out = validate_claims(
        text,
        YEARS_CITED,
        YEARS_CORPUS,
        target_company=TARGET,
        job_title=TITLE,
        jd_text=ADV_JD,
        known_companies=KNOWN,
        estimate_style=style,
    )
    assert EXPERIENCE in {v["kind"] for v in out.violations}, out.violations


def test_years_copied_from_a_bullet_fail_in_a_drafted_answer() -> None:
    out = validate_claims(
        "I have 3 years in oncology sales.",
        YEARS_CITED,
        YEARS_CORPUS,
        target_company=TARGET,
        job_title=TITLE,
        jd_text=ADV_JD,
        known_companies=KNOWN,
        estimate_style="hedged",
        question="How many years of oncology sales experience do you have?",
    )
    assert EXPERIENCE in {v["kind"] for v in out.violations}, out.violations


@pytest.mark.parametrize(
    ("text", "title", "question"),
    [
        (
            "I passed the Certified Financial Planner exam.",
            "Certified Financial Planner",
            "Do you hold the CFP designation?",
        ),
        (
            "I led the Specialist Partnerships team at Larkspur Health.",
            "Head of Specialist Partnerships, APAC",
            "Describe your partnerships experience.",
        ),
    ],
)
def test_a_title_word_carrying_a_claim_fails_in_a_drafted_answer(
    text: str, title: str, question: str
) -> None:
    out = validate_claims(
        text,
        list(CITED),
        CORPUS,
        target_company=TARGET,
        job_title=title,
        jd_text=ADV_JD,
        known_companies=KNOWN,
        estimate_style="hedged",
        question=question,
    )
    assert EXPERIENCE in {v["kind"] for v in out.violations}, out.violations


# ---------------------------------------------------------------------------
# estimate_style "confident" lets the approximation mark go, nothing more
# (review 2026-10-23): no added decimal places, no word of precision or
# verification next to an estimate
# ---------------------------------------------------------------------------

EST_CORPUS: dict[str, Any] = {
    **CORPUS,
    "bullets": [
        *CORPUS["bullets"],
        {
            "id": "b_107",
            "role_id": "role_larkspur",
            "metric_confidence": "estimate",
            "text_long": "Redesigned onboarding for new reps, cutting ramp time by ~30%.",
            "metric": "~30% ramp time",
        },
    ],
}


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("I cut ramp time by 30.0%.", NUMBER),
        ("I cut ramp time by precisely 30%.", "unhedged_estimate"),
        ("I cut ramp time by exactly 30%.", "unhedged_estimate"),
        ("I cut ramp time by a measured 30%.", "unhedged_estimate"),
        ("I cut ramp time by 30%, a verified figure.", "unhedged_estimate"),
        ("I cut ramp time by an audited 30%.", "unhedged_estimate"),
    ],
)
@_xfail(HOLE)
def test_confident_style_adds_no_precision_to_an_estimate(text: str, kind: str) -> None:
    out = validate_claims(
        text,
        ["b_107"],
        EST_CORPUS,
        target_company=TARGET,
        job_title=TITLE,
        jd_text=ADV_JD,
        known_companies=KNOWN,
        estimate_style="confident",
    )
    assert kind in {v["kind"] for v in out.violations}, out.violations


@pytest.mark.parametrize(
    "text", ["I cut ramp time by 30%.", "I cut ramp time by about 30%.", "I cut ramp time by ~30%."]
)
def test_confident_style_still_lets_the_mark_go(text: str) -> None:
    out = validate_claims(
        text,
        ["b_107"],
        EST_CORPUS,
        target_company=TARGET,
        job_title=TITLE,
        jd_text=ADV_JD,
        known_companies=KNOWN,
        estimate_style="confident",
    )
    assert out.ok, out.violations


@pytest.mark.parametrize(
    ("rendered", "kind"),
    [
        pytest.param(
            "Redesigned onboarding for new reps, cutting ramp time by precisely 30%.",
            "unhedged_estimate",
            marks=_xfail(HOLE),
        ),
        ("Redesigned onboarding for new reps, cutting ramp time by 30%.", None),
    ],
)
def test_confident_style_in_a_tailored_bullet(rendered: str, kind: str | None) -> None:
    out = validate_tailored(
        [{"bullet_id": "b_107", "rendered": rendered}], EST_CORPUS, estimate_style="confident"
    )
    if kind is None:
        assert out.ok, out.violations
    else:
        assert kind in {v["kind"] for v in out.violations}, out.violations


# ---------------------------------------------------------------------------
# Second review of 2026-10-23
# ---------------------------------------------------------------------------


def _run(text: str, title: str = TITLE, question: str | None = None, **kw: Any) -> Any:
    extra: dict[str, Any] = {"question": question} if question else {}
    return validate_claims(
        text,
        kw.get("cited", list(CITED)),
        kw.get("corpus", CORPUS),
        target_company=TARGET,
        job_title=title,
        jd_text=ADV_JD,
        known_companies=KNOWN,
        estimate_style=kw.get("style", "confident"),
        **extra,
    )


# The job title's marker word ("Licensed", "Certified", "Experience",
# "Specialist") is set aside only where the sentence names the role sought
# and claims nothing else: having held the sought role, pointed back to
# ("a role I held", "which I did", "the job I hold today"), is a claim.
LICENSED = "Licensed Insurance Agent"
SPECIALIST = "Head of Specialist Partnerships, APAC"


def _applying(title: str, tail: str, noun: str = "role") -> str:
    return f"I'm applying for the {title} {noun}{tail}"


HELD_SOUGHT_TITLE: list[tuple[str, str]] = [
    (LICENSED, _applying(LICENSED, ", a role I held at Larkspur Health.")),
    (LICENSED, _applying(LICENSED, ", which I did at Larkspur Health.")),
    (LICENSED, _applying(LICENSED, ", the job I hold today.")),
    (LICENSED, _applying(LICENSED, ", which I was at Larkspur Health.")),
    (LICENSED, "I've done the Licensed Insurance Agent job before."),
    (LICENSED, "I ran this Licensed Insurance Agent role's whole territory at Larkspur Health."),
    (
        LICENSED,
        "I'm applying for this Licensed Insurance Agent position: same job I held at "
        "Larkspur Health.",
    ),
    (
        LICENSED,
        "I'm a natural for the Licensed Insurance Agent role, having done it at Larkspur Health.",
    ),
    (SPECIALIST, _applying(SPECIALIST, ", a role I held at Larkspur Health.")),
    (SPECIALIST, _applying(SPECIALIST, ", a role I already hold at Larkspur Health.")),
    (SPECIALIST, _applying(SPECIALIST, ", a role I've held before.")),
    (SPECIALIST, _applying(SPECIALIST, " I held at Larkspur Health.")),
    (SPECIALIST, _applying(SPECIALIST, ", a role I was in for 14 months.")),
    (SPECIALIST, _applying(SPECIALIST, " because I know APAC hospital buyers.")),
    (
        "Senior Experience Manager",
        _applying("Senior Experience Manager", ", a role I held at Larkspur Health."),
    ),
    (
        "Senior Experience Manager",
        _applying("Senior Experience Manager", " because I ran hospital accounts."),
    ),
    (
        "Certified Sales Trainer",
        _applying("Certified Sales Trainer", ", which I held at Larkspur Health."),
    ),
    (
        "Certified Sales Trainer",
        _applying(
            "Certified Sales Trainer", ", a job I did at Larkspur Health for years.", "position"
        ),
    ),
    # no marker word at all: the title pointed back to as held
    (
        "Head of Partnerships, APAC",
        _applying("Head of Partnerships, APAC", ", a role I held at Larkspur Health."),
    ),
    (
        "Head of Partnerships, APAC",
        _applying("Head of Partnerships, APAC", ", which I held at Corvane Analytics."),
    ),
]


@pytest.mark.parametrize(
    ("title", "text"),
    _track(
        HELD_SOUGHT_TITLE, [c for c in HELD_SOUGHT_TITLE if c[0] == "Head of Partnerships, APAC"]
    ),
)
def test_the_sought_title_claimed_as_held_still_fails(title: str, text: str) -> None:
    out = _run(text, title)
    assert not out.ok, text
    assert {v["kind"] for v in out.violations} & {"claimed_job_title", EXPERIENCE}


def test_a_held_licensed_title_fails_in_a_drafted_license_answer() -> None:
    out = _run(
        "Yes, I'm applying for the Licensed Insurance Agent role, a role I held at Larkspur "
        "Health.",
        "Licensed Insurance Agent",
        question="Do you hold an active insurance license?",
        style="hedged",
    )
    assert not out.ok
    assert {v["kind"] for v in out.violations} & {"claimed_job_title", EXPERIENCE}


@pytest.mark.parametrize(
    ("title", "text"),
    [
        ("Licensed Insurance Agent", "I'm applying for the Licensed Insurance Agent role."),
        (
            "Licensed Insurance Agent",
            "I'm applying for the Licensed Insurance Agent role at Vandelay & Company.",
        ),
        (
            "Head of Specialist Partnerships, APAC",
            "I'm interested in your Head of Specialist Partnerships, APAC opening.",
        ),
    ],
)
@_xfail(FALSE_POSITIVE)
def test_naming_the_sought_title_alone_still_passes(title: str, text: str) -> None:
    out = _run(text, title)
    assert out.ok, out.violations


# A connective word next to a copied bullet phrase carries no claim of its
# own: "kept", a second employer, or "covering every …" does.
CONNECTIVE_CLAIMS = [
    "At Larkspur Health I split the inside sales desk into three specialist pods and kept all "
    "14 account managers.",
    "At Larkspur Health I split the inside sales desk into three specialist pods and kept every "
    "one of the 22 reps.",
    "At Larkspur Health I split the inside sales desk into three specialist pods, then kept "
    "pipeline coverage at 88%.",
    "I split the inside sales desk into three specialist pods, including win-back, and kept "
    "every rep.",
    "At Corvane Analytics I turned inbound chat leads into qualified demos, covering every one "
    "of them.",
    "I turned inbound chat leads into qualified demos at Corvane Analytics and later at "
    "Larkspur Health.",
    "At Corvane Analytics I turned inbound chat leads into qualified demos, and next at "
    "Larkspur Health.",
    "At Corvane Analytics I kept turning inbound chat leads into qualified demos, then also at "
    "Larkspur Health.",
]


@pytest.mark.parametrize("text", CONNECTIVE_CLAIMS)
def test_a_connective_carrying_a_claim_still_fails(text: str) -> None:
    out = _run(text)
    assert not out.ok, text


# A scheduling question passes only when it holds nothing but scheduling
# words: an achievement in its object is a claim.
SCHEDULING_CLAIMS = [
    "Can we talk about the oncology turnaround at Larkspur Health?",
    "Can we talk about the oncology turnaround?",
    "Could we meet to go over the oncology playbook written at Larkspur Health?",
    "Could we schedule time to discuss the hospital sales record at Larkspur Health?",
    "When could we meet to compare the oncology launch at Larkspur Health with yours?",
    "Can we talk about Larkspur Health, where the oncology team hit quota every quarter?",
]


@pytest.mark.parametrize("text", SCHEDULING_CLAIMS)
def test_a_question_carrying_an_achievement_still_fails(text: str) -> None:
    out = _run(text)
    assert not out.ok, text


@pytest.mark.parametrize(
    "text",
    [
        "Could we find time next week to talk about the role?",
        "Can we set up a quick call with your hiring manager this week?",
    ],
)
@_xfail(FALSE_POSITIVE)
def test_a_plain_scheduling_question_still_passes(text: str) -> None:
    out = _run(text)
    assert out.ok, out.violations


@pytest.mark.parametrize(
    "text",
    [
        "Vandelay & Company teams lead on hospital accounts, then Larkspur Health.",
        "Vandelay & Company reps partner to close hospital deals, then Larkspur Health, then "
        "Corvane Analytics.",
    ],
)
def test_a_people_noun_tagline_in_a_career_sequence_still_fails(text: str) -> None:
    out = _run(text)
    assert EMPLOYER in {v["kind"] for v in out.violations}, out.violations


# A figure rescaled without a finer unit adds no precision: "$1.2M" from
# "$1,200,000" passes; "$2.0M" from "$2M" does not.
SCALE_CORPUS: dict[str, Any] = {
    **CORPUS,
    "bullets": [
        *CORPUS["bullets"],
        {
            "id": "b_200",
            "role_id": "role_larkspur",
            "metric_confidence": "verified",
            "text_long": "Closed $1,200,000 in new hospital contracts in 2023.",
            "metric": "$1,200,000",
        },
        {
            "id": "b_201",
            "role_id": "role_larkspur",
            "metric_confidence": "verified",
            "text_long": "Grew the book to $2M in annual revenue.",
            "metric": "$2M",
        },
        {
            "id": "b_202",
            "role_id": "role_larkspur",
            "metric_confidence": "verified",
            "text_long": "Ran 1,200 discovery calls in 2022.",
            "metric": "1,200 calls",
        },
    ],
}
SCALE_CITED = ["b_200", "b_201", "b_202"]


@pytest.mark.parametrize(
    "text",
    [
        "At Larkspur Health I closed $1.2M in new hospital contracts in 2023.",
        "At Larkspur Health I closed $1.2 million in new hospital contracts in 2023.",
        "At Larkspur Health I ran 1.2K discovery calls in 2022.",
        "At Larkspur Health I grew the book to $2 million in annual revenue.",
        "At Larkspur Health I grew the book to $2,000,000 in annual revenue.",
    ],
)
def test_a_rescaled_figure_adds_no_precision(text: str) -> None:
    out = _run(text, cited=SCALE_CITED, corpus=SCALE_CORPUS)
    assert out.ok, out.violations


@_xfail(HOLE)
def test_a_figure_stated_to_a_finer_unit_still_fails() -> None:
    out = _run(
        "At Larkspur Health I grew the book to $2.0M in annual revenue.",
        cited=SCALE_CITED,
        corpus=SCALE_CORPUS,
    )
    assert NUMBER in {v["kind"] for v in out.violations}, out.violations


@pytest.mark.parametrize(
    ("bid", "rendered", "added"),
    [
        ("b_200", "Closed $1.2M in new hospital contracts in 2023.", False),
        ("b_202", "Ran 1.2K discovery calls in 2022.", False),
        ("b_201", "Grew the book to $2.0M in annual revenue.", True),
    ],
)
def test_a_rescaled_figure_in_a_tailored_bullet(bid: str, rendered: str, added: bool) -> None:
    # a tailored bullet keeps its figure as written: main's digit check fails
    # "1.2" from "1,200,000" and "2.0" from "$2,000,000" alike, so only the
    # finer unit is asserted here
    out = validate_tailored(
        [{"bullet_id": bid, "rendered": rendered}], SCALE_CORPUS, estimate_style="confident"
    )
    if added:
        assert NUMBER in {v["kind"] for v in out.violations}, out.violations


# Known gaps of the rule layer (present on main 5341b76 as well), recorded
# so a fix shows up: the LLM claim judge is the only check behind them today.
@pytest.mark.xfail(strict=True, reason="a changed count of a non-person noun is not caught")
def test_gap_a_changed_count_of_pods() -> None:
    out = _run("At Larkspur Health I split the inside sales desk into five specialist pods.")
    assert not out.ok


# ---------------------------------------------------------------------------
# Third review of 2026-10-23: shapes that got past the fixes above. Each
# failed on main 5341b76 (or, for the first title-run cases, passes nowhere
# now) and must fail.
# ---------------------------------------------------------------------------

CFP = "Certified Financial Planner"
REGIONAL = "Regional Sales Director"

# the job title named twice: the role sought, then a seat held
SECOND_TITLE_RUN: list[tuple[str, str]] = (
    [
        (
            title,
            f"I'm applying for {lead} {title} {noun}, {before} {title} {held} at Larkspur Health.",
        )
        for title in (LICENSED, CFP, SPECIALIST, REGIONAL)
        for lead, noun, before, held in (
            ("your", "opening", "coming from a", "position"),
            ("the", "role", "currently in a", "role"),
            ("the", "role", "fresh off a", "role"),
            ("the", "role", "already in a", "role"),
        )
    ]
    + [
        (title, f"I'm applying for the {title} role from a {title} role at Larkspur Health.")
        for title in (LICENSED, CFP, REGIONAL)
    ]
    + [
        (LICENSED, f"I'm applying for the {LICENSED} role, currently in a {LICENSED} role."),
        (
            LICENSED,
            f"I'm applying for the {LICENSED} role, a {LICENSED} role player at Larkspur Health.",
        ),
        (LICENSED, f"I'm applying for the {LICENSED} role to keep doing the {LICENSED} job well."),
    ]
)


@pytest.mark.parametrize(
    ("title", "text"), _track(SECOND_TITLE_RUN, [c for c in SECOND_TITLE_RUN if c[0] == REGIONAL])
)
def test_a_second_run_of_the_job_title_is_a_seat_held(title: str, text: str) -> None:
    out = _run(text, title)
    assert not out.ok, text
    assert {v["kind"] for v in out.violations} & {"claimed_job_title", EXPERIENCE}


@pytest.mark.parametrize("style", ["hedged", "confident"])
def test_a_second_run_of_the_job_title_fails_in_a_drafted_answer(style: str) -> None:
    out = _run(
        f"I'm applying for your {LICENSED} opening, coming from a {LICENSED} position at "
        "Larkspur Health.",
        LICENSED,
        question="Do you hold an active insurance license?",
        style=style,
    )
    assert not out.ok
    assert {v["kind"] for v in out.violations} & {"claimed_job_title", EXPERIENCE}


@pytest.mark.parametrize(
    ("title", "text"),
    [
        (LICENSED, f"For the {LICENSED} role at Vandelay & Company, I'd start by listening."),
        (SPECIALIST, f"I'm interested in your {SPECIALIST} opening."),
        (CFP, f"I'm applying for the {CFP} role at Vandelay & Company."),
    ],
)
@_xfail(FALSE_POSITIVE)
def test_the_role_sought_named_once_still_passes(title: str, text: str) -> None:
    out = _run(text, title)
    assert out.ok, out.violations


# "kept" goes unsourced only right before a cited -ing word: a second "kept"
# with an object of its own is a retention claim
KEPT_CLAIMS = [
    "At Corvane Analytics I kept booking 15–20 qualified demos a week and kept every qualified "
    "demo.",
    "At Corvane Analytics I kept booking 15–20 qualified demos a week and kept the inbound chat "
    "leads.",
    "At Corvane Analytics I kept booking 15–20 qualified demos a week and kept the team.",
    # an -ing word no cited bullet has, though its root is a bullet's
    "At Corvane Analytics I kept turning inbound chat leads into qualified demos.",
]


@pytest.mark.parametrize("text", KEPT_CLAIMS)
def test_a_second_kept_is_still_a_claim(text: str) -> None:
    out = _run(text)
    assert EXPERIENCE in {v["kind"] for v in out.violations}, out.violations


# "covering", "including", "spanning" with a scope no bullet states: a
# quantifier, a comparison, another bullet's words, or a list that is no run
# of the bullet before it
SCOPE_CLAIMS = [
    "At Corvane Analytics I turned inbound chat leads into qualified demos, covering most of them.",
    "At Corvane Analytics I turned inbound chat leads into qualified demos, covering many of them.",
    "I booked 15–20 qualified demos a week, covering most of the team.",
    "At Larkspur Health I split the inside sales desk into three specialist pods, spanning most "
    "of the floor.",
    "At Larkspur Health I split the inside sales desk into three specialist pods, covering most "
    "of the desk, and pipeline coverage went from 61% to 88%.",
    "At Larkspur Health I split the inside sales desk into three specialist pods, covering more "
    "inbound than any pod before.",
    "At Larkspur Health I split the inside sales desk into three specialist pods, covering much "
    "of the expansion.",
    "At Larkspur Health I split the inside sales desk into three specialist pods, covering any "
    "win-back.",
    "At Larkspur Health I split the inside sales desk into three specialist pods, covering, "
    "every one, inbound.",
    "At Larkspur Health I split the inside sales desk into three specialist pods, covering "
    "nearly all inbound.",
    "At Larkspur Health I split the inside sales desk into three specialist pods covering "
    "inbound, expansion, and win-back, including the 14 account managers.",
    "At Larkspur Health I split the inside sales desk into three specialist pods, spanning "
    "inbound through win-back.",
]


@pytest.mark.parametrize("text", SCOPE_CLAIMS)
def test_a_scope_no_bullet_states_still_fails(text: str) -> None:
    out = _run(text)
    assert EXPERIENCE in {v["kind"] for v in out.violations}, out.violations


# an identity claim or a repeated figure gets no connective
CERT_CORPUS: dict[str, Any] = {
    **CORPUS,
    "bullets": [
        *CORPUS["bullets"],
        {
            "id": "b_108",
            "role_id": "role_larkspur",
            "metric_confidence": "verified",
            "text_long": "Onboarded 12 certified installers across the Northeast region.",
            "metric": "12 installers",
        },
    ],
}


@pytest.mark.parametrize(
    "text",
    [
        "Earlier I was one of 12 certified installers across the Northeast region.",
        "At Larkspur Health I onboarded 12 certified installers, and later 12 certified "
        "installers across the Northeast region again.",
        "At Larkspur Health I onboarded 12 certified installers across the Northeast region, "
        "covering the region anyway.",
        "At Larkspur Health I onboarded 12 certified installers, spanning the Northeast region "
        "then more.",
    ],
)
def test_a_connective_beside_an_identity_or_a_repeat_still_fails(text: str) -> None:
    out = _run(text, cited=["b_108"], corpus=CERT_CORPUS)
    assert not out.ok, text


@_xfail(FALSE_POSITIVE)
def test_a_copied_scope_list_still_passes() -> None:
    # the shape of the real letter of 2026-10-22: every item after
    # "covering" is the bullet's own run
    out = _run(
        "At Larkspur Health I split the inside sales desk into three specialist pods, covering "
        "inbound, expansion, and win-back, and pipeline coverage rose from 61% to 88%."
    )
    assert out.ok, out.violations


# the target's people tied to the candidate's employer or seat
@pytest.mark.parametrize(
    "text",
    [
        "Vandelay & Company reps partner with Larkspur Health's former director of sales.",
        "Vandelay & Company reps lead to Director of Sales at Larkspur Health.",
        "Vandelay & Company teams partner with organizations across industries; Larkspur "
        "Health's did not.",
        "Vandelay & Company sellers lead to the Director of Sales seat at Larkspur Health.",
    ],
)
def test_the_targets_people_tied_to_the_candidates_seat_still_fail(text: str) -> None:
    out = _run(text)
    assert EMPLOYER in {v["kind"] for v in out.violations}, out.violations


def test_each_halves_is_the_verb() -> None:
    out = _run(
        "I redesigned onboarding at Larkspur Health; each halves ramp time for new reps.",
        cited=["b_107"],
        corpus=EST_CORPUS,
    )
    assert NUMBER in {v["kind"] for v in out.violations}, out.violations


@pytest.mark.parametrize(
    "text",
    [
        "I cut ramp time by a documented 30%.",
        "I cut ramp time by a tracked 30%.",
        "I cut ramp time by an actual 30%.",
        "I cut ramp time by 30% (a tracked, reported figure).",
        "I cut ramp time by 30%, as recorded in Salesforce.",
        "I cut ramp time by 30% to the day.",
        "I cut ramp time by 30% on the dot.",
    ],
)
@_xfail(HOLE)
def test_a_word_of_record_states_an_estimate_as_checked(text: str) -> None:
    out = _run(text, cited=["b_107"], corpus=EST_CORPUS)
    assert "unhedged_estimate" in {v["kind"] for v in out.violations}, out.violations


# ---------------------------------------------------------------------------
# Fourth review of 2026-10-23: the same shapes with an employer of the
# candidate's own (Quillfin Audio) after the second run of the job title, a
# second "kept" beside a bullet that says "booked", and a quantifier right
# after "covering"/"spanning". Every sentence must fail; the last field
# records whether it already failed on main 5341b76 (checked by running this
# list against an archive of 5341b76: all failed there except the Regional
# Sales Director ones, which passed on main and fail now).
# ---------------------------------------------------------------------------

QR_CORPUS: dict[str, Any] = {
    **CORPUS,
    "roles": [
        *CORPUS["roles"],
        {
            "id": "role_quillfin",
            "company": "Quillfin Audio",
            "title": "Account Executive",
            "start": "2014-01",
            "end": "2017-01",
        },
        {
            "id": "role_ridgeline",
            "company": "Ridgeline Metrics",
            "title": "Business Development Representative",
            "start": "2013-06",
            "end": "2014-01",
        },
    ],
    "bullets": [
        *CORPUS["bullets"],
        {
            "id": "b_301",
            "role_id": "role_ridgeline",
            "metric_confidence": "verified",
            "text_long": "Booked 7–12 qualified meetings a month toward typical $600K+ platform "
            "deals and supported the close.",
            "metric": "7–12 qualified meetings a month",
        },
        {
            "id": "b_302",
            "role_id": "role_quillfin",
            "metric_confidence": "verified",
            "text_long": "Sold studio monitoring software to 40 independent recording studios.",
            "metric": "40 studios",
        },
        {
            # the same, with a "booking" a cited "kept" may stand before
            "id": "b_304",
            "role_id": "role_ridgeline",
            "metric_confidence": "verified",
            "text_long": "Booked 7–12 qualified meetings a month toward typical $600K+ platform "
            "deals, booking each one for the account executive, and supported the close.",
            "metric": "7–12 qualified meetings a month",
        },
    ],
}
QR_CITED = [*CITED, "b_301", "b_302"]


def _held_after_sought(title: str) -> list[str]:
    return [
        f"I'm applying for your {title} opening, coming from a {title} position at Quillfin Audio.",
        f"I'm applying for the {title} role, currently in a {title} role at Quillfin Audio.",
        f"I'm applying for the {title} role, fresh off a {title} role at Quillfin Audio.",
        f"I'm applying for the {title} role from a {title} role at Quillfin Audio.",
    ]


# (job title, sentence, failed on main 5341b76)
MUST_FAIL_QR: list[tuple[str, str, bool]] = [
    *((t, s, True) for t in (LICENSED, CFP, SPECIALIST) for s in _held_after_sought(t)),
    # no marker word in this title: each of these passed on main
    *((REGIONAL, s, False) for s in _held_after_sought(REGIONAL)),
    # a plain "in a" before the second run
    (
        LICENSED,
        f"I'm applying for the {LICENSED} role, in a {LICENSED} role at Quillfin Audio.",
        True,
    ),
    (
        REGIONAL,
        f"I'm applying for the {REGIONAL} role, in a {REGIONAL} role at Quillfin Audio.",
        False,
    ),
    # a second "kept", beside a bullet that says "booked"
    (
        TITLE,
        "At Ridgeline Metrics I kept booking 7–12 qualified meetings a month and kept every "
        "$600K+ platform deal.",
        True,
    ),
    (
        TITLE,
        "At Ridgeline Metrics I kept booking 7–12 qualified meetings a month and kept the $600K+ "
        "platform deals.",
        True,
    ),
    (
        TITLE,
        "At Ridgeline Metrics I kept booking 7–12 qualified meetings a month and kept enterprise "
        "accounts.",
        True,
    ),
    # a quantifier right after a scope connective
    (
        TITLE,
        "At Corvane Analytics I turned inbound chat leads into qualified demos, covering most of "
        "them.",
        True,
    ),
    (
        TITLE,
        "At Corvane Analytics I turned inbound chat leads into qualified demos, covering many of "
        "them.",
        True,
    ),
    (TITLE, "I booked 15–20 qualified demos a week, covering most of the team.", True),
    (
        TITLE,
        "At Larkspur Health I split the inside sales desk into three specialist pods, spanning "
        "most of the floor.",
        True,
    ),
]


def _qr(text: str, title: str, cited: list[str] = QR_CITED, **kw: Any) -> Any:
    return _run(text, title, cited=cited, corpus=QR_CORPUS, **kw)


@pytest.mark.parametrize(
    ("title", "text", "on_main"), _track(MUST_FAIL_QR, [c for c in MUST_FAIL_QR if not c[2]])
)
def test_fourth_review_sentences_fail(title: str, text: str, on_main: bool) -> None:
    out = _qr(text, title)
    assert not out.ok, text
    assert {v["kind"] for v in out.violations} & {"claimed_job_title", EXPERIENCE}


@pytest.mark.parametrize("style", ["hedged", "confident"])
@pytest.mark.parametrize(
    ("title", "text"),
    _track(
        [(t, s) for t, s, _ in MUST_FAIL_QR if t in (LICENSED, CFP, SPECIALIST, REGIONAL)],
        [(t, s) for t, s, on_main in MUST_FAIL_QR if not on_main],
    ),
)
def test_a_seat_held_after_the_role_sought_fails_in_a_drafted_answer(
    title: str, text: str, style: str
) -> None:
    out = _qr(text, title, question="Do you hold an active insurance license?", style=style)
    assert not out.ok, text
    assert {v["kind"] for v in out.violations} & {"claimed_job_title", EXPERIENCE}


@_xfail(FALSE_POSITIVE)
def test_the_role_sought_named_once_passes_with_quillfin_in_the_corpus() -> None:
    for text in (
        f"I'm applying for the {LICENSED} role.",
        f"I'm interested in your {LICENSED} opening.",
        f"I'm applying for your {REGIONAL} opening at Vandelay & Company.",
    ):
        out = _qr(text, LICENSED if LICENSED in text else REGIONAL)
        assert out.ok, (text, out.violations)


KEPT_BOOKING = "At Ridgeline Metrics I kept booking 7–12 qualified meetings a month."


@_xfail(FALSE_POSITIVE)
def test_the_kept_before_a_cited_booking_goes_unsourced() -> None:
    # b_304 says "booking": the "kept" right before it may go unsourced
    assert _qr(KEPT_BOOKING, TITLE, cited=["b_304"]).ok


@pytest.mark.parametrize(
    "tail",
    [
        "and kept every $600K+ platform deal.",
        "and kept the $600K+ platform deals.",
        "and kept enterprise accounts.",
    ],
)
def test_a_second_kept_beside_a_cited_booking_is_unsourced(tail: str) -> None:
    # the second "kept" (a retention claim) may not go unsourced
    text = f"{KEPT_BOOKING[:-1]} {tail}"
    out = _qr(text, TITLE, cited=["b_304"])
    assert EXPERIENCE in {v["kind"] for v in out.violations}, (text, out.violations)


@pytest.mark.parametrize(
    "tail",
    [
        "covering most of them",
        "covering the rest of them",
        "covering the remainder",
        "covering 100% of inbound",
        "including nearly every inbound lead",
        "spanning the whole desk",
        "covering inbound; most of it",
        "covering, each one, inbound",
    ],
)
def test_a_quantifier_after_a_scope_connective_gets_no_allowance(tail: str) -> None:
    out = _qr(
        f"At Larkspur Health I split the inside sales desk into three specialist pods, {tail}.",
        TITLE,
    )
    assert EXPERIENCE in {v["kind"] for v in out.violations}, out.violations


@_xfail(HOLE)
def test_a_second_run_after_a_plain_in_a_is_a_claimed_title() -> None:
    # "in a" names a seat held only before the title's second run; the
    # violation names the title itself, not only the marker word
    out = _qr(
        f"I'm applying for the {REGIONAL} role, in a {REGIONAL} role at Quillfin Audio.", REGIONAL
    )
    assert "claimed_job_title" in {v["kind"] for v in out.violations}, out.violations


def test_a_first_run_after_in_a_is_the_role_sought() -> None:
    assert _qr(f"I'm interested in a {REGIONAL} role at Vandelay & Company.", REGIONAL).ok


# ---------------------------------------------------------------------------
# Fix round 2 of 2026-10-23: each sentence below failed on main 5341b76 and
# passed after the first fix round. Each must fail again.
# ---------------------------------------------------------------------------

# a claim in front of a scheduling question: the words before the modal
SCHEDULING_LEAD_CLAIMS = [
    "Given the oncology turnaround at Vandelay & Company, can we talk this week?",
    "Fresh off the oncology turnaround at Larkspur Health, can we talk this week?",
    "Former oncology team lead at Larkspur Health here, so can we talk this week?",
    "Top oncology closer at Larkspur Health, so could we meet next week?",
    "As the top hospital closer at Larkspur Health, can we talk this week?",
    "Now leading the oncology desk at Larkspur Health, can we talk this week?",
    "Still running the oncology desk at Larkspur Health, could we meet next week?",
    "After record quarters at Larkspur Health, can we talk this week?",
    "Off a quota-topping year at Larkspur Health, can we talk this week?",
    "Since the hospital buyers at Larkspur Health still ask for the oncology playbook, can we "
    "talk this week?",
    "Given the hospital Salesforce rollout, can we talk about the role this week?",
]
SCHEDULING_CLAIMS.extend(SCHEDULING_LEAD_CLAIMS)


@pytest.mark.parametrize("text", SCHEDULING_LEAD_CLAIMS)
def test_a_claim_before_a_scheduling_question_still_fails(text: str) -> None:
    out = _run(text)
    assert EXPERIENCE in {v["kind"] for v in out.violations}, out.violations


def test_the_target_before_a_scheduling_question_is_a_fabricated_employer() -> None:
    out = _run(SCHEDULING_LEAD_CLAIMS[0])
    assert EMPLOYER in {v["kind"] for v in out.violations}, out.violations


def test_a_claim_before_a_scheduling_question_fails_in_a_drafted_answer() -> None:
    out = _run(
        SCHEDULING_LEAD_CLAIMS[0],
        question="Why are you interested in this role?",
        style="hedged",
    )
    assert EXPERIENCE in {v["kind"] for v in out.violations}, out.violations


@pytest.mark.parametrize(
    "text",
    [
        "So when can we talk this week?",
        "Thanks, can we talk this week?",
        "And could we meet next week?",
    ],
)
@_xfail(FALSE_POSITIVE)
def test_a_scheduling_question_after_an_opener_still_passes(text: str) -> None:
    out = _run(text)
    assert out.ok, out.violations


# the target's people "partner with"/"lead on" only in the posting's own
# words: a word of tenure after them makes the target a past employer
TAGLINE_TENURE_CLAIMS = [
    "Vandelay & Company reps partner with hospital buyers, a former seat.",
    "Vandelay & Company reps partner with hospital buyers, an old seat.",
    "Vandelay & Company teams lead on oncology, a past employer.",
    "Vandelay & Company teams lead on oncology, a previous employer.",
    "Vandelay & Company teams lead with oncology, which is where the career started.",
    "Vandelay & Company teams lead with oncology, where the career began.",
    "Vandelay & Company teams lead on oncology - the alma mater.",
    "Vandelay & Company teams partner with hospital buyers: the first job out of school.",
    "Vandelay & Company AEs lead to oncology, three years running.",
]


@pytest.mark.parametrize("text", TAGLINE_TENURE_CLAIMS)
def test_a_people_noun_tagline_with_a_word_of_tenure_still_fails(text: str) -> None:
    out = _run(text)
    assert EMPLOYER in {v["kind"] for v in out.violations}, out.violations


# the sought title's marker word set aside only when the rest of the
# sentence is the applying frame: anything else is checked word by word
SOUGHT_TITLE_PADDED: list[tuple[str, str]] = [
    (CFP, _applying(CFP, ", still advising retiree households.")),
    (CFP, _applying(CFP, ", currently advising retiree households.")),
    (CFP, _applying(CFP, ", a fiduciary since the first day.")),
    (CFP, _applying(CFP, ", and I'd keep the same retiree households.")),
    (CFP, _applying(CFP, " and would keep advising the same retiree households.")),
    (LICENSED, _applying(LICENSED, ", and I'd keep renewing the same auto policies.")),
    (LICENSED, _applying(LICENSED, ", and I'd keep serving the same auto clients.")),
    (LICENSED, _applying(LICENSED, ", the same seat as at Larkspur Health.")),
    (LICENSED, _applying(LICENSED, ", and the auto book would follow.")),
    (SPECIALIST, _applying(SPECIALIST, " and I'd keep growing the APAC hospital partners.")),
    (
        "Senior Experience Manager",
        _applying("Senior Experience Manager", ", and I'd keep running hospital patient programs."),
    ),
    (
        "Certified Sales Trainer",
        "I'm applying for your Certified Sales Trainer opening; the account managers would keep "
        "using the same coaching playbook.",
    ),
]


@pytest.mark.parametrize(("title", "text"), SOUGHT_TITLE_PADDED)
def test_a_claim_beside_the_sought_title_still_fails(title: str, text: str) -> None:
    out = _run(text, title)
    assert EXPERIENCE in {v["kind"] for v in out.violations}, out.violations


@pytest.mark.parametrize("style", ["hedged", "confident"])
@pytest.mark.parametrize(
    "text",
    _track(["Yes.", "Yes, I'm applying for the Certified Financial Planner role."], ["Yes."]),
)
def test_yes_to_a_designation_question_fails(text: str, style: str) -> None:
    out = _run(text, CFP, question="Do you hold the CFP designation?", style=style)
    assert EXPERIENCE in {v["kind"] for v in out.violations}, out.violations


# "earlier", "next", "later" as a comparison or a ranking
CONNECTIVE_RANKING_CLAIMS = [
    "I turned inbound chat leads into qualified demos earlier than any team.",
    "I turned inbound chat leads into qualified demos earlier than the team.",
    "I turned inbound chat leads into qualified demos earlier than any rep.",
    "I turned inbound chat leads into qualified demos next to none.",
    "I turned inbound chat leads into qualified demos, booking 15–20 qualified demos a week, "
    "earlier than any team.",
]
CONNECTIVE_CLAIMS.extend(CONNECTIVE_RANKING_CLAIMS)


@pytest.mark.parametrize("text", CONNECTIVE_RANKING_CLAIMS)
def test_a_temporal_word_as_a_ranking_still_fails(text: str) -> None:
    out = _run(text)
    assert EXPERIENCE in {v["kind"] for v in out.violations}, out.violations


# a scope after the list ends, or a list lent by another bullet
SCOPE_BLEND_CLAIMS = [
    "I split the inside sales desk into three specialist pods, covering inbound, expansion, and "
    "win-back (every rep on the floor).",
    "I split the inside sales desk into three specialist pods, covering inbound, expansion, and "
    "win-back; every rep on the floor.",
    "I ran hiring and onboarding for the three specialist pods, covering inbound, expansion, "
    "and win-back.",
    "I split the inside sales desk into three specialist pods, covering 22 reps in 14 months.",
]
SCOPE_CLAIMS.extend(SCOPE_BLEND_CLAIMS)


@pytest.mark.parametrize("text", SCOPE_BLEND_CLAIMS)
def test_a_scope_after_the_list_or_from_another_bullet_still_fails(text: str) -> None:
    out = _run(text)
    assert EXPERIENCE in {v["kind"] for v in out.violations}, out.violations


# A credential title, a corpus of a second invented candidate (Quillmark
# Logistics, Ternwell Software) and a posting naming the title
FREIGHT_CORPUS: dict[str, Any] = {
    "identity": {"name": "Robin Ashgrove", "email": "robin@example.com"},
    "roles": [
        {
            "id": "role_quill",
            "company": "Quillmark Logistics",
            "title": "Regional Sales Manager",
            "start": "2020-03",
            "end": "2024-08",
        },
        {
            "id": "role_tern",
            "company": "Ternwell Software",
            "title": "Account Executive",
            "start": "2016-06",
            "end": "2020-03",
        },
    ],
    "education": [{"school": "Hollins Bay College", "degree": "B.S.", "years": "2012 - 2016"}],
    "skill_index": {"Salesforce": "hands-on"},
    "constraints": {"never_claim_titles": ["Director of Sales"], "never_claim_phrases": []},
    "bullets": [
        {
            "id": "c1",
            "role_id": "role_quill",
            "metric_confidence": "verified",
            "text_long": "Split the carrier desk into three specialist pods (truckload, "
            "intermodal, and drayage); on-time quote rate rose from 70% to 91%.",
            "metric": "on-time quote rate 70% to 91%",
        },
        {
            "id": "c2",
            "role_id": "role_tern",
            "metric_confidence": "verified",
            "text_long": "Turned web chat inquiries into qualified demos, booking 10–12 "
            "qualified demos a week for the team.",
            "metric": "10–12 qualified demos a week",
        },
        {
            "id": "c3",
            "role_id": "role_quill",
            "metric_confidence": "estimate",
            "text_long": "Coached 9 new account managers on the quoting tool, cutting ramp "
            "time by about 30%.",
            "metric": "~30% ramp time reduction",
        },
        {
            "id": "c4",
            "role_id": "role_quill",
            "metric_confidence": "verified",
            "text_long": "Onboarded 12 certified customs brokers as channel partners in the "
            "Midwest region.",
            "metric": "12 certified customs brokers",
        },
        {
            "id": "c5",
            "role_id": "role_tern",
            "metric_confidence": "verified",
            "text_long": "Ran weekly pipeline reviews for the account executive team.",
        },
    ],
}
FREIGHT_CITED = ["c1", "c2", "c3", "c4", "c5"]
FREIGHT_TARGET = "Brightwater Freight"
FREIGHT_TITLE = "Certified Freight Specialist"
BROKER_TITLE = "Licensed Customs Broker"
FREIGHT_JD = (
    "Brightwater Freight is hiring a Certified Freight Specialist and a Senior Manager, Carrier "
    "Sales. Our teams partner with shippers across North America. You will lead the carrier "
    "desk and partner with Account Executives. The Licensed Customs Broker files entries for "
    "importers."
)


def _freight(
    text: str, title: str = FREIGHT_TITLE, question: str | None = None, style: str = "confident"
) -> Any:
    extra: dict[str, Any] = {"question": question} if question else {}
    return validate_claims(
        text,
        FREIGHT_CITED,
        FREIGHT_CORPUS,
        target_company=FREIGHT_TARGET,
        job_title=title,
        jd_text=FREIGHT_JD,
        known_companies=[FREIGHT_TARGET, "Fenmore Systems"],
        estimate_style=style,
        **extra,
    )


FREIGHT_PADDED: list[tuple[str, str]] = [
    (
        FREIGHT_TITLE,
        "I'm applying for the Certified Freight Specialist position as the top-producing broker "
        "in the Midwest region.",
    ),
    (
        FREIGHT_TITLE,
        "I'm applying for the Certified Freight Specialist role after doing this exact job at "
        "Quillmark Logistics.",
    ),
    (
        FREIGHT_TITLE,
        "I'm applying for the Certified Freight Specialist role, and I'll renew it in March.",
    ),
    (
        FREIGHT_TITLE,
        "I'm applying for the Certified Freight Specialist role with the designation already in "
        "hand.",
    ),
    (
        FREIGHT_TITLE,
        "I'm applying for the Certified Freight Specialist role with a full book of shipper "
        "relationships ready to move over.",
    ),
    (
        FREIGHT_TITLE,
        "I'm applying for the Certified Freight Specialist role, coming off back-to-back "
        "quarters over quota.",
    ),
    (
        FREIGHT_TITLE,
        "I'm applying for the Certified Freight Specialist role to keep leading the Midwest "
        "region's carrier desk.",
    ),
    (
        FREIGHT_TITLE,
        "I'm applying for the Certified Freight Specialist role, still the only one on the "
        "Quillmark Logistics desk.",
    ),
    (
        BROKER_TITLE,
        "I'm applying for the Licensed Customs Broker role at Brightwater Freight after passing "
        "the broker exam on the first try.",
    ),
    (
        BROKER_TITLE,
        "I'm applying for the Licensed Customs Broker role at Brightwater Freight with a clean "
        "audit history.",
    ),
    (
        BROKER_TITLE,
        "I'm applying for the Licensed Customs Broker role at Brightwater Freight and can file "
        "entries from the first week.",
    ),
]


@pytest.mark.parametrize("style", ["hedged", "confident"])
@pytest.mark.parametrize(("title", "text"), FREIGHT_PADDED)
def test_a_claim_after_a_credential_title_sought_still_fails(
    title: str, text: str, style: str
) -> None:
    out = _freight(text, title, style=style)
    assert EXPERIENCE in {v["kind"] for v in out.violations}, out.violations


@pytest.mark.parametrize(
    ("title", "question", "text"),
    [
        (
            FREIGHT_TITLE,
            "Are you a Certified Freight Specialist?",
            "Yes, and I'm applying for the Certified Freight Specialist role at Brightwater "
            "Freight.",
        ),
        (
            FREIGHT_TITLE,
            "Do you currently hold the Certified Freight Specialist credential?",
            "Yes. I'm applying for the Certified Freight Specialist role.",
        ),
        (
            BROKER_TITLE,
            "Do you hold an active Licensed Customs Broker license?",
            "Yes, and I'm applying for the Licensed Customs Broker role.",
        ),
        pytest.param(
            FREIGHT_TITLE, "Are you a Certified Freight Specialist?", "Yes.", marks=_xfail(HOLE)
        ),
        pytest.param(
            BROKER_TITLE, "Are you FINRA Series 7 registered?", "Yes, I am.", marks=_xfail(HOLE)
        ),
    ],
)
def test_yes_to_a_credential_question_fails(title: str, question: str, text: str) -> None:
    out = _freight(text, title, question=question, style="hedged")
    assert EXPERIENCE in {v["kind"] for v in out.violations}, out.violations


@pytest.mark.parametrize(
    ("question", "text", "ok"),
    [
        ("Are you a Certified Freight Specialist?", "No.", True),
        ("Do you hold the Certified Freight Specialist credential?", "No, I do not.", True),
        # a cited bullet holding every word asked about sources a "yes"; one
        # missing a word does not
        ("Have you onboarded certified customs brokers in the Midwest region?", "Yes.", True),
        pytest.param(
            "Are you certified in customs brokerage for the Midwest region?",
            "Yes.",
            False,
            marks=_xfail(HOLE),
        ),
    ],
)
def test_a_credential_question_answered_plainly(question: str, text: str, ok: bool) -> None:
    out = _freight(text, question=question, style="hedged")
    assert out.ok is ok, out.violations


@pytest.mark.parametrize(
    ("title", "text"),
    [
        (FREIGHT_TITLE, "I'm applying for the Certified Freight Specialist role."),
        (
            FREIGHT_TITLE,
            "I'm applying for the Certified Freight Specialist role at Brightwater Freight.",
        ),
        (
            FREIGHT_TITLE,
            "For the Certified Freight Specialist role, I'd start with the carrier desk.",
        ),
        (BROKER_TITLE, "I'm interested in your Licensed Customs Broker opening."),
    ],
)
@_xfail(FALSE_POSITIVE)
def test_the_credential_title_sought_alone_still_passes(title: str, text: str) -> None:
    out = _freight(text, title)
    assert out.ok, out.violations


# a connective in a sentence no single cited bullet sources: another role's
# title, or two bullets blended
@pytest.mark.parametrize(
    "text",
    [
        "Earlier, as an Account Executive, I split the carrier desk into three specialist pods.",
        "Anyway, I onboarded 12 certified customs brokers and the 9 new account managers.",
    ],
)
def test_a_connective_beside_a_blend_of_bullets_still_fails(text: str) -> None:
    out = _freight(text, "Senior Manager, Carrier Sales")
    assert EXPERIENCE in {v["kind"] for v in out.violations}, out.violations


# ---------------------------------------------------------------------------
# Fix round 3 of 2026-10-23: each sentence below failed on main 5341b76 (or,
# for the credential questions, passed on both trees) and passed after fix
# round 2. Each must fail.
# ---------------------------------------------------------------------------

# a second invented employer of the same candidate's, with bullets that give
# part of a scope or a figure to someone else
R3_CORPUS: dict[str, Any] = {
    **CORPUS,
    "bullets": [
        *CORPUS["bullets"],
        {
            "id": "b_201",
            "role_id": "role_larkspur",
            "metric_confidence": "verified",
            "text_long": "Booked 15–20 qualified demos a week on the Ohio desk; the Kentucky "
            "desk, run by another manager, booked 40 a week.",
            "metric": "15–20 qualified demos a week",
        },
        {
            "id": "b_202",
            "role_id": "role_larkspur",
            "metric_confidence": "verified",
            "text_long": "Split the Ohio renewal desk into three specialist pods; the Kentucky "
            "renewal desk and the hospital accounts stayed with a partner agency.",
        },
        {
            "id": "b_204",
            "role_id": "role_larkspur",
            "metric_confidence": "verified",
            "text_long": "Rebuilt the onboarding playbook for new reps.",
        },
        {
            "id": "b_205",
            "role_id": "role_larkspur",
            "metric_confidence": "verified",
            "text_long": "Supported licensed agents with quoting for Ohio auto policies, booking "
            "12 qualified renewals a month for the Indiana licensed producers.",
            "metric": "12 qualified renewals a month",
        },
    ],
}
R3_CITED = ["b_201", "b_202", "b_204", "b_205"]


def _r3(text: str, cited: list[str] | None = None, **kw: Any) -> Any:
    return _run(text, cited=cited or R3_CITED, corpus=R3_CORPUS, **kw)


# "halves" as the verb after "both" or a determiner (x0.5)
HALVES_VERB_BULLETS = [
    "Rebuilt the onboarding playbook for new reps, which both halves ramp time and lifts win "
    "rates.",
    "Rebuilt the onboarding playbook for new reps that both halves ramp time and lifts quota.",
    "Rebuilt the onboarding playbook for new reps; these halves ramp time.",
    "Rebuilt the onboarding playbook; its halves ramp time.",
]


@pytest.mark.parametrize("rendered", HALVES_VERB_BULLETS)
def test_halves_as_the_verb_fails_in_a_tailored_bullet(rendered: str) -> None:
    out = validate_tailored(
        [{"bullet_id": "b_204", "rendered": rendered}], R3_CORPUS, estimate_style="confident"
    )
    assert NUMBER in {v["kind"] for v in out.violations}, out.violations


@pytest.mark.parametrize(
    "text",
    [
        "At Larkspur Health I rebuilt the onboarding playbook, which both halves ramp time and "
        "lifts win rates.",
        "The new playbook both halves ramp time and lifts win rates.",
        "At Larkspur Health I rebuilt the onboarding playbook; these halves ramp time.",
    ],
)
def test_halves_as_the_verb_fails_in_a_letter(text: str) -> None:
    out = _r3(text, cited=["b_204"])
    assert NUMBER in {v["kind"] for v in out.violations}, out.violations


# "covering"/"spanning" naming a scope the same bullet gives to someone else
R3_SCOPE_STRETCH: list[tuple[str, list[str]]] = [
    (
        "I split the Ohio renewal desk into three specialist pods, covering the Kentucky renewal "
        "desk and the hospital accounts.",
        ["b_202"],
    ),
    (
        "I split the Ohio renewal desk into three specialist pods, spanning the Kentucky renewal "
        "desk.",
        ["b_202"],
    ),
    (
        "I split the Ohio renewal desk into three specialist pods spanning the Kentucky renewal "
        "desk.",
        ["b_202"],
    ),
    (
        "I split the Ohio renewal desk into three specialist pods, covering the hospital accounts.",
        ["b_202"],
    ),
    (
        "I split the Ohio renewal desk into three specialist pods, covering a partner agency.",
        ["b_202"],
    ),
    (
        "Next, I split the Ohio renewal desk into three specialist pods, covering the Kentucky "
        "renewal desk.",
        ["b_202"],
    ),
    (
        "Then I split the Ohio renewal desk into three specialist pods, spanning the Kentucky "
        "renewal desk.",
        ["b_202"],
    ),
    ("I booked 15–20 qualified demos a week, covering the Kentucky desk.", ["b_201"]),
    (
        "I booked 15–20 qualified demos a week on the Ohio desk, covering the Kentucky desk and "
        "the Ohio desk.",
        ["b_201"],
    ),
    ("I booked 12 qualified renewals a month, covering Indiana.", ["b_205"]),
]


@pytest.mark.parametrize(("text", "cited"), R3_SCOPE_STRETCH)
def test_a_scope_from_another_clause_of_the_bullet_fails(text: str, cited: list[str]) -> None:
    out = _r3(text, cited=cited)
    assert not out.ok, out.violations


# "earlier"/"later"/"next"/"anyway" attaching another person's figure or desk
R3_TEMPORAL: list[str] = [
    "I booked 15–20 qualified demos a week, and later 40 a week.",
    "I booked 15–20 qualified demos a week, and next 40 a week.",
    "Anyway, I booked 15–20 qualified demos a week and then 40 a week.",
    "I booked 40 qualified demos a week earlier.",
    "I booked 40 qualified demos a week later.",
    "I booked 15–20 qualified demos a week on the Ohio desk and later the Kentucky desk.",
]


@pytest.mark.parametrize("text", R3_TEMPORAL)
def test_a_temporal_word_attaching_another_figure_fails(text: str) -> None:
    out = _r3(text, cited=["b_201"])
    assert not out.ok, out.violations


# a credential asked in any shape: a "yes" or "licensed" claims it, and a
# bullet about someone else's license sources nothing
LICENSED_AGENT = "Licensed Insurance Agent"
R3_CREDENTIAL_ANSWERS: list[tuple[str, str]] = [
    ("Please confirm you hold an active producer license.", "Yes."),
    ("Please confirm you hold an active producer license.", "Yes, active."),
    ("Please confirm you hold an active producer license.", "Yes, I'm licensed."),
    ("Licensed in Ohio?", "Yes."),
    ("Is your producer license active?", "Yes."),
    ("License status", "Licensed."),
    ("CFP designation?", "Yes."),
    ("Registered rep (Series 6)?", "Yes"),
    ("Do you hold an active producer license?", "Yes."),
]


@pytest.mark.parametrize("style", ["hedged", "confident"])
@pytest.mark.parametrize(("question", "text"), R3_CREDENTIAL_ANSWERS)
@_xfail(HOLE)
def test_a_credential_claimed_in_any_question_shape_fails(
    question: str, text: str, style: str
) -> None:
    out = _r3(text, title=LICENSED_AGENT, question=question, style=style)
    assert EXPERIENCE in {v["kind"] for v in out.violations}, out.violations


@pytest.mark.parametrize(
    "question", ["Please confirm you hold an active producer license.", "License status"]
)
def test_no_to_a_credential_question_passes(question: str) -> None:
    out = _r3("No.", title=LICENSED_AGENT, question=question)
    assert out.ok, out.violations


# the Kestrel Ports corpus (a third invented candidate)
KESTREL_CORPUS: dict[str, Any] = {
    "identity": {"name": "Morgan Vale", "email": "morgan@example.com"},
    "roles": [
        {
            "id": "role_kp",
            "company": "Kestrel Ports",
            "title": "Operations Manager",
            "start": "2019-01",
            "end": "2024-05",
        },
        {
            "id": "role_ab",
            "company": "Ambergate Freight",
            "title": "Account Executive",
            "start": "2015-08",
            "end": "2018-12",
        },
    ],
    "education": [{"school": "Coastal State University", "degree": "B.A.", "years": "2011 – 2015"}],
    "skill_index": {"Salesforce": "hands-on"},
    "constraints": {"never_claim_titles": ["Director of Operations"], "never_claim_phrases": []},
    "bullets": [
        {
            "id": "b_301",
            "role_id": "role_kp",
            "metric_confidence": "verified",
            "text_long": "Onboarded 12 certified customs brokers for the Midwest desk in 2022 and "
            "audited carrier contracts for the Southeast and Gulf desks.",
            "metric": "12 certified customs brokers",
        },
        {
            "id": "b_302",
            "role_id": "role_ab",
            "metric_confidence": "verified",
            "text_long": "Turned inbound web leads into qualified demos, booking 15–20 qualified "
            "demos a week across the team.",
            "metric": "15–20 qualified demos a week",
        },
        {
            "id": "b_303",
            "role_id": "role_kp",
            "metric_confidence": "verified",
            "text_long": "Split the inbound desk into three specialist pods (enterprise, "
            "mid-market, and SMB); win rate rose from 18% to 24%.",
            "metric": "win rate 18% → 24%",
        },
        {
            "id": "b_304",
            "role_id": "role_kp",
            "metric_confidence": "estimate",
            "text_long": "Cut dwell time for the specialist pods' reefer loads by roughly 30% "
            "with two analysts.",
            "metric": "~30% dwell time",
        },
        {
            "id": "b_305",
            "role_id": "role_ab",
            "metric_confidence": "verified",
            "text_long": "Ran hiring and onboarding for the inbound team, hiring 9 reps in 10 "
            "months, and kept training new reps on Salesforce.",
            "metric": "9 reps in 10 months",
        },
    ],
}
KESTREL_CITED = ["b_301", "b_302", "b_303", "b_304", "b_305"]


def _kestrel(text: str) -> Any:
    return validate_claims(
        text,
        KESTREL_CITED,
        KESTREL_CORPUS,
        target_company="Northwind Freight",
        job_title="Certified Freight Specialist",
        jd_text=(
            "Northwind Freight is hiring a Certified Freight Specialist. Our teams partner with "
            "shippers across North America. You will lead the brokerage desk, coach a team of "
            "specialists, and partner with Solutions Engineering."
        ),
        known_companies=["Northwind Freight", "Contoso"],
        estimate_style="confident",
    )


KESTREL_MUST_FAIL = [
    # covering/spanning a scope from the bullet's other clause
    "I onboarded 12 certified customs brokers, covering the Midwest, Southeast and Gulf desks.",
    "At Kestrel Ports I onboarded 12 certified customs brokers, covering the Southeast and Gulf "
    "desks.",
    "I onboarded 12 certified customs brokers, spanning the Midwest desk and the Southeast and "
    "Gulf desks.",
    "I onboarded 12 certified customs brokers, covering carrier contracts.",
    "I onboarded 12 certified customs brokers, covering contracts for the Southeast and Gulf "
    "desks.",
    "I onboarded 12 certified customs brokers in 2022, covering the Southeast and Gulf desks.",
    "Then I onboarded 12 certified customs brokers, covering the Southeast and Gulf desks.",
    # an identity claim ("as one of") or a recombination behind a connective
    "Earlier, as one of 12 certified customs brokers, I audited carrier contracts for the "
    "Southeast and Gulf desks.",
    "Earlier, I onboarded 12 certified customs brokers for the Southeast and Gulf desks.",
    "Anyway, I onboarded 12 certified customs brokers and audited the Southeast desk.",
    # a fabrication main caught only on the word now excused
    "I led 12 certified customs brokers on the Gulf desks, which is what I'd highlight.",
    "My audits cut dwell time for both halves of the desk by 30%.",
]


@pytest.mark.parametrize("text", KESTREL_MUST_FAIL)
def test_fix_round_three_kestrel_sentences_fail(text: str) -> None:
    out = _kestrel(text)
    assert not out.ok, out.violations


@pytest.mark.parametrize(
    "question",
    ["Have you been a licensed agent?", "Have you worked as a licensed producer in Ohio?"],
)
@_xfail(HOLE)
def test_someone_elses_license_sources_no_held_license(question: str) -> None:
    out = _r3("Yes.", title=LICENSED_AGENT, question=question)
    assert EXPERIENCE in {v["kind"] for v in out.violations}, out.violations


def test_the_candidates_own_license_sources_a_yes() -> None:
    corpus = {
        **R3_CORPUS,
        "bullets": [
            *R3_CORPUS["bullets"],
            {
                "id": "b_206",
                "role_id": "role_larkspur",
                "metric_confidence": "verified",
                "text_long": "Licensed P&C producer in Ohio; quoted auto policies for renewals.",
            },
        ],
    }
    for question in ("Licensed in Ohio?", "Please confirm you hold an active producer license."):
        out = _run("Yes.", LICENSED_AGENT, question=question, cited=["b_206"], corpus=corpus)
        assert out.ok, (question, out.violations)


# ---------------------------------------------------------------------------
# fix round 4 (2026-10-23): regressions against main 5341b76 found by review
# ---------------------------------------------------------------------------

R4_PODS = "I split the inside sales desk into three specialist pods"
R4_DEMOS = "I booked 15–20 qualified demos a week"
R4_TEAM_QUESTION = "Describe a team structure you built."

# (1) a connective's other forms share its root, but only "covering",
# "including" and "spanning" themselves are granted (and checked)
R4_CONNECTIVE_FORMS: list[tuple[str, str | None]] = [
    (f"{R4_PODS} that covered more of the inside sales desk than any desk before.", None),
    (f"{R4_PODS}, which covered most of the inside sales desk.", None),
    (f"{R4_PODS}, which covered every inside sales desk.", None),
    (f"{R4_PODS} that covered all of the inside sales desk.", None),
    (f"{R4_PODS}, which covers much of the inside sales desk.", None),
    (f"{R4_PODS}, which spanned most of the inside sales desk.", None),
    (f"{R4_PODS}, which included every inside sales desk.", None),
    (f"{R4_PODS} that cover every inside sales desk today.", None),
    (f"{R4_DEMOS}, which covered most of the team.", None),
    (f"{R4_DEMOS}, which included every one of the team.", None),
    ("I covered the inside sales desk with three specialist pods.", None),
    (f"{R4_PODS}, which covered the expansion desk.", None),
    (f"{R4_PODS}, anyways.", None),
    (f"{R4_PODS} that covered more of the inside sales desk than any desk before.", "Q"),
    (f"{R4_PODS}, which covered most of the inside sales desk.", "Q"),
    (f"{R4_PODS}, which spans every inside sales desk.", "Q"),
]


@pytest.mark.parametrize(("text", "asked"), R4_CONNECTIVE_FORMS)
def test_another_form_of_a_connective_is_checked(text: str, asked: str | None) -> None:
    out = _run(text, question=R4_TEAM_QUESTION if asked else None)
    assert EXPERIENCE in {v["kind"] for v in out.violations}, out.violations


# (2) the applying frame holds no claim to hold the seat or to work at the
# target, whatever marker word the job title has
LICENSED_AGENT_R4 = "Licensed Insurance Agent"
CFP_R4 = "Certified Financial Planner"
R4_FRAME_CLAIMS: list[tuple[str, str]] = [
    (
        LICENSED_AGENT_R4,
        "I'm applying for the Licensed Insurance Agent role, and I'm happy in the role here at "
        "Vandelay & Company.",
    ),
    (
        LICENSED_AGENT_R4,
        "I'm applying for the Licensed Insurance Agent role, and I'm glad to be in the role.",
    ),
    (
        LICENSED_AGENT_R4,
        "I'm here at Vandelay & Company and applying for the Licensed Insurance Agent role.",
    ),
    (
        LICENSED_AGENT_R4,
        "I'm applying for the Licensed Insurance Agent role, and we're here at Vandelay & Company.",
    ),
    (
        CFP_R4,
        "I'm applying for the Certified Financial Planner role, and I'm happy in the position "
        "at Vandelay & Company.",
    ),
    (CFP_R4, "I'm applying for the Certified Financial Planner role, happy in the role."),
    (
        CFP_R4,
        "I'm applying for the Certified Financial Planner role, and I'm happy here at Vandelay "
        "& Company.",
    ),
    (CFP_R4, "I'm applying for the Certified Financial Planner role, glad to be in this role."),
    (
        CFP_R4,
        "I'm applying for the Certified Financial Planner role and I'm happy in this job here.",
    ),
]


@pytest.mark.parametrize(("title", "text"), R4_FRAME_CLAIMS)
def test_the_applying_frame_holds_no_seat_or_tenure(title: str, text: str) -> None:
    out = _run(text, title)
    assert not out.ok, out.violations


R4_ANSWER_CLAIMS: list[tuple[str, str, str]] = [
    (
        LICENSED_AGENT_R4,
        "Describe your current position.",
        "I'm applying for the Licensed Insurance Agent role, and I'm happy in the role.",
    ),
    (
        LICENSED_AGENT_R4,
        "What is your current role and employer?",
        "I'm applying for the Licensed Insurance Agent role.",
    ),
    (
        "Medicare Specialist",
        "Do you have Medicare Specialist experience?",
        "I do! I'm applying for the Medicare Specialist role.",
    ),
    (
        "Medicare Specialist",
        "Do you have Medicare Specialist experience?",
        "Absolutely. I'm applying for the Medicare Specialist role.",
    ),
    (
        "Medicare Specialist",
        "Do you have Medicare Specialist experience?",
        "I'm applying for the Medicare Specialist role. I do.",
    ),
    (
        CFP_R4,
        "Do you have financial planning experience?",
        "Yes. I'm applying for the Certified Financial Planner role.",
    ),
    (
        "Senior Specialist, Partnerships",
        "Do you have senior partnerships experience?",
        "Yes. I'm applying for the Senior Specialist, Partnerships role.",
    ),
    (
        "Senior Specialist, Partnerships",
        "Have you managed partnerships at a senior level?",
        "I do. I'm applying for the Senior Specialist, Partnerships role.",
    ),
]


@pytest.mark.parametrize(("title", "question", "text"), R4_ANSWER_CLAIMS)
def test_the_sought_title_in_a_drafted_answer_is_a_claim(
    title: str, question: str, text: str
) -> None:
    out = _run(text, title, question=question)
    assert EXPERIENCE in {v["kind"] for v in out.violations}, out.violations


@pytest.mark.parametrize(
    ("title", "text"),
    [
        (LICENSED_AGENT_R4, "I'm applying for the Licensed Insurance Agent role."),
        (
            LICENSED_AGENT_R4,
            "I'm applying for the Licensed Insurance Agent role at Vandelay & Company.",
        ),
        (CFP_R4, "I'm interested in your Certified Financial Planner opening."),
        (
            CFP_R4,
            "For the Certified Financial Planner role at Vandelay & Company, I'd start by "
            "listening.",
        ),
    ],
)
@_xfail(FALSE_POSITIVE)
def test_the_sought_title_in_a_letter_still_passes(title: str, text: str) -> None:
    out = _run(text, title)
    assert not {v["kind"] for v in out.violations} & {EXPERIENCE}, out.violations


# (3) "kept" before a cited -ing word stretches no past bullet into the
# present, into a run with no end or into solo work
R4_CORPUS: dict[str, Any] = {
    "identity": {"name": "Riley Fenwick", "email": "riley@example.com"},
    "roles": [
        {
            "id": "role_quill",
            "company": "Quillmark Logistics",
            "title": "Regional Sales Manager",
            "start": "2020-03",
            "end": "2024-08",
        },
        {
            "id": "role_tamb",
            "company": "Tamberlane Software",
            "title": "Account Executive",
            "start": "2016-05",
            "end": "2020-02",
        },
    ],
    "education": [
        {"school": "Northfield College", "degree": "B.S. Business", "years": "2012 – 2016"}
    ],
    "skill_index": {"HubSpot": "hands-on"},
    "constraints": {
        "never_claim_titles": ["VP of Sales", "Head of Sales"],
        "never_claim_phrases": [],
    },
    "bullets": [
        {
            "id": "b_1",
            "role_id": "role_quill",
            "metric_confidence": "verified",
            "text_long": "Grew the Midwest territory from 40 to 62 active shipper accounts in two "
            "years.",
            "metric": "40 to 62 accounts",
        },
        {
            "id": "b_2",
            "role_id": "role_quill",
            "metric_confidence": "estimate",
            "text_long": "Cut quote turnaround from about 3 days to about 1 day by building a "
            "shared pricing sheet (refrigerated, flatbed, and dry van lanes).",
            "metric": "~3 days to ~1 day",
        },
        {
            "id": "b_3",
            "role_id": "role_quill",
            "metric_confidence": "verified",
            "text_long": "Coached four new account executives through their first quarter on the "
            "team.",
            "metric": "four AEs",
        },
        {
            "id": "b_7",
            "role_id": "role_quill",
            "metric_confidence": "verified",
            "text_long": "Built two specialized lane desks (reefer and hazmat) for the Midwest "
            "territory.",
            "metric": "two desks",
        },
        {
            "id": "b_8",
            "role_id": "role_tamb",
            "metric_confidence": "verified",
            "text_long": "Converted inbound trial users into qualified opportunities, sourcing "
            "30–35 qualified opportunities a month.",
            "metric": "30–35 a month",
        },
    ],
}
R4_CITED = ["b_1", "b_2", "b_3", "b_7", "b_8"]


def _r4(text: str) -> Any:
    return validate_claims(
        text,
        list(R4_CITED),
        R4_CORPUS,
        target_company="Brightwater Freight",
        job_title="Director of Sales",
        jd_text=(
            "Brightwater Freight is hiring a Director of Sales to lead a team of account "
            "executives. Our teams partner with shippers across industries."
        ),
        known_companies=["Brightwater Freight", "Acme Corp"],
        estimate_style="confident",
    )


R4_SOURCING = "sourcing 30–35 qualified opportunities a month"
R4_KEPT_CLAIMS = [
    f"I kept {R4_SOURCING} ever since.",
    f"I've kept {R4_SOURCING} since then.",
    f"Since then I've kept {R4_SOURCING}.",
    f"I kept {R4_SOURCING} until now.",
    f"I kept {R4_SOURCING} to this day.",
    f"I kept {R4_SOURCING}, as I do today.",
    f"I kept {R4_SOURCING}, and I still do.",
    f"I still kept {R4_SOURCING}.",
    f"I kept {R4_SOURCING} every month.",
    f"I kept {R4_SOURCING} all year.",
    f"I kept {R4_SOURCING} through every quarter I was there.",
    f"I kept {R4_SOURCING} in 2019 and 2020.",
    f"I've kept {R4_SOURCING} on my own.",
    f"I kept {R4_SOURCING} without any help.",
]


@pytest.mark.parametrize("text", R4_KEPT_CLAIMS)
def test_kept_stretches_no_bullet(text: str) -> None:
    out = _r4(text)
    assert not out.ok, out.violations


@pytest.mark.parametrize(
    "text",
    [
        "At Corvane Analytics I kept booking 15–20 qualified demos a week, as I do today.",
        "I kept booking 15–20 qualified demos a week until now.",
        "At Corvane Analytics I kept booking 15–20 qualified demos a week every week.",
        "At Corvane Analytics I kept booking 15–20 qualified demos a week to this day.",
    ],
)
def test_kept_stretches_no_bullet_with_the_repo_corpus(text: str) -> None:
    out = _run(text)
    assert not out.ok, out.violations


@pytest.mark.parametrize(
    "text",
    [
        f"At Tamberlane Software I kept {R4_SOURCING}.",
        f"I kept {R4_SOURCING}.",
    ],
)
@_xfail(FALSE_POSITIVE)
def test_kept_before_the_bullets_own_clause_still_passes(text: str) -> None:
    out = _r4(text)
    assert not {v["kind"] for v in out.violations} & {EXPERIENCE}, out.violations


# (4) a temporal connective mid-sentence adds no second, invented event
R4_DESKS = "I built two specialized lane desks (reefer and hazmat) for the Midwest territory"
R4_TEMPORAL_EVENTS = [
    "Earlier I built two specialized lane desks (reefer and hazmat) for the Midwest territory, "
    "and later I built four.",
    "I built two specialized lane desks (reefer and hazmat), and later I built 4.",
    f"{R4_DESKS}, and later built two more.",
    f"{R4_DESKS}, and later built more.",
    f"{R4_DESKS}, and later built another.",
    "Earlier, I built two specialized lane desks (reefer and hazmat) for the Midwest territory "
    "and later built them again for the territory.",
    "I converted inbound trial users into qualified opportunities, and later converted every "
    "one of them.",
    "I converted inbound trial users into qualified opportunities, and later I converted them all.",
]


@pytest.mark.parametrize("text", R4_TEMPORAL_EVENTS)
def test_a_temporal_connective_adds_no_second_event(text: str) -> None:
    out = _r4(text)
    assert not out.ok, out.violations


# (5) a last list item opening a new clause adds nothing to the bullet's
# next clause
@pytest.mark.parametrize(
    "tail",
    ["across the team", "while hiring 22 reps", "on Salesforce", "for 14 months"],
)
def test_a_new_clause_after_a_scope_list_adds_nothing(tail: str) -> None:
    text = (
        f"{R4_PODS}, covering inbound, expansion, and win-back, and pipeline coverage rose "
        f"from 61% to 88% {tail}."
    )
    out = _run(text)
    assert not out.ok, out.violations


# ---------------------------------------------------------------------------
# Near misses of the false-positive letters of 2026-10-22/23, moved here from
# tests/test_claims_false_positives_1023.py (which now tests the repair by
# deletion): fabrications in the shapes of those letters' flagged sentences.
# Each fails on main 5341b76 and must keep failing.
# ---------------------------------------------------------------------------


def _check(text: str, title: str = TITLE) -> list[dict[str, str]]:
    out = validate_claims(
        text,
        list(CITED),
        CORPUS,
        target_company=TARGET,
        job_title=title,
        jd_text=JD,
        known_companies=KNOWN,
        estimate_style="confident",
    )
    assert out.ok == (not out.violations)
    return out.violations


def _kinds(violations: list[dict[str, str]]) -> set[str]:
    return {v["kind"] for v in violations}


def _flagged(violations: list[dict[str, str]], kind: str, word: str) -> bool:
    return any(v["kind"] == kind and re.search(rf"\b{word}", v["detail"]) for v in violations)


@pytest.mark.parametrize(
    ("text", "word"),
    [
        # not inside a noun phrase: a subject, a copula, an adverb before it
        ("I'm qualified demos royalty.", "royalty"),
        ("I am qualified demo material.", "material"),
        ("I specialist pods in aviation at Larkspur Health.", "aviation"),
        ("At Larkspur Health I built highly specialist pods for aviation.", "aviation"),
        # a person or role noun after the copied pair
        ("I was a specialist pods supervisor at Larkspur Health.", "supervisor"),
        ("I was a specialist pods lead for aviation at Larkspur Health.", "aviation"),
        # a pair no cited bullet has
        ("I'm a qualified electrician.", "electrician"),
        ("I have specialized expertise in aviation.", "aviation"),
        ("I specialized in aviation finance at Larkspur Health.", "aviation"),
        # another marker in the same sentence
        ("I have specialist pods experience in aviation.", "aviation"),
        ("With years of experience in qualified demos, I'd lead your BDRs.", "experience"),
        ("I'm a seasoned leader of specialist pods in aviation.", "aviation"),
        # a title word outside the job title
        ("I'm a Certified Specialist in aviation.", "aviation"),
    ],
)
def test_a_marker_not_copied_from_a_bullet_still_fails(text: str, word: str) -> None:
    violations = _check(text)
    assert _flagged(violations, "unsourced_experience_claim", word), violations


def test_a_title_word_outside_the_title_is_still_a_marker() -> None:
    # "Specialist" is a title word only inside the job title's run
    violations = _check("I'm a seasoned Specialist in aviation sales.", SPECIALIST_TITLE)
    assert _flagged(violations, "unsourced_experience_claim", "aviation"), violations


@pytest.mark.parametrize(
    ("text", "word"),
    [
        # the job title's words used for anything but the role sought
        ("I led the Specialist Partnerships team at Larkspur Health.", "partnerships"),
        ("At Larkspur Health I ran Specialist Partnerships for the hospital segment.", "hospital"),
        ("Specialist Partnerships is where I spent my career.", "spent"),
        (
            "I'm applying for the Head of Specialist Partnerships, APAC role, having run "
            "Specialist Partnerships at Larkspur Health.",
            "partnerships",
        ),
    ],
)
def test_a_title_word_not_naming_the_role_sought_still_fails(text: str, word: str) -> None:
    violations = _check(text, SPECIALIST_TITLE)
    assert _flagged(violations, "unsourced_experience_claim", word), violations


@pytest.mark.parametrize(
    "text",
    [
        "I passed the Certified Financial Planner exam.",
        "I studied for and passed the Certified Financial Planner boards.",
    ],
)
def test_a_credential_named_by_the_title_still_fails(text: str) -> None:
    kinds = _kinds(_check(text, "Certified Financial Planner"))
    assert "unsourced_experience_claim" in kinds, kinds


@pytest.mark.parametrize(
    ("with_marker", "without_marker"),
    [
        (
            "At Larkspur Health I split the inside sales desk into three specialist pods, and "
            "pipeline coverage went from 61% to 97%.",
            "At Larkspur Health I split the inside sales desk into three pods, and pipeline "
            "coverage went from 61% to 97%.",
        ),
        (
            "At Corvane Analytics I booked 40 qualified demos a week.",
            "At Corvane Analytics I booked 40 demos a week.",
        ),
        (
            "At Larkspur Health I turned inbound chat leads into qualified demos.",
            "At Larkspur Health I turned inbound chat leads into demos.",
        ),
    ],
)
def test_a_copied_marker_is_checked_at_least_like_the_same_sentence_without_it(
    with_marker: str, without_marker: str
) -> None:
    # the copied word is no excuse: every check that fails the sentence
    # without it fails it with it
    got = _kinds(_check(with_marker))
    assert got, with_marker
    assert _kinds(_check(without_marker)) <= got


@pytest.mark.parametrize(
    ("text", "word"),
    [
        # invented content beside a copied phrase (reviews of 2026-10-23)
        (
            "At Larkspur Health I split the inside sales desk into three specialist pods, "
            "covering inbound, expansion, and oncology, and pipeline coverage went from 61% "
            "to 88%.",
            "oncology",
        ),
        (
            "At Larkspur Health I split the inside sales desk into three specialist pods, "
            "covering hospital accounts.",
            "hospital",
        ),
        (
            "At Larkspur Health I split the inside sales desk into three specialist pods, "
            "covering enterprise, mid-market and SMB.",
            "enterprise",
        ),
        (
            "I split the inside sales desk into three specialist pods of oncology nurses.",
            "oncology",
        ),
        (
            "I split the inside sales desk into three specialist pods and personally closed "
            "every hospital deal.",
            "hospital",
        ),
        ("Three specialist pods of nurses reported to me at Larkspur Health.", "nurses"),
        (
            "I split the inside sales desk into three specialist pods for the federal government.",
            "federal",
        ),
        (
            "At Larkspur Health I led the specialist pods in aviation, defense and banking.",
            "aviation",
        ),
        ("I've managed three specialist pods of aviation engineers.", "aviation"),
        (
            "I ran the specialist pods for the aviation vertical, the largest in Larkspur Health "
            "history.",
            "aviation",
        ),
        (
            "Earlier, at Corvane Analytics, I turned oncology referrals into qualified demos.",
            "oncology",
        ),
        (
            "At Corvane Analytics I kept booking 15–20 qualified demos a week with surgeons.",
            "surgeons",
        ),
    ],
)
def test_a_copied_marker_with_invented_content_still_fails(text: str, word: str) -> None:
    violations = _check(text)
    assert _flagged(violations, "unsourced_experience_claim", word), violations


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Can we agree I ran the best floor at Larkspur Health?", "unsourced_experience_claim"),
        ("Can we talk about the 40 reps I've coached?", "unsourced_experience_claim"),
        (
            "When can we talk about the turnaround I led at Larkspur Health?",
            "unsourced_experience_claim",
        ),
        ("Can we talk about why I'm the expert you need?", "unsourced_experience_claim"),
        # a claim in the next sentence is read on its own
        (
            "When can we talk? I'd walk in with a decade of hospital sales experience.",
            "unsourced_experience_claim",
        ),
    ],
)
def test_b_a_claim_inside_a_question_still_fails(text: str, kind: str) -> None:
    assert kind in _kinds(_check(text)), _check(text)


@pytest.mark.parametrize(
    "text",
    [
        # another first-person word implies a relationship (review 2026-10-23)
        "Can we meet with the hospital buyers who know me?",
        "Could we meet with the surgeons who trust me?",
        "Can we talk with the hospital buyers on my list?",
    ],
)
def test_b_a_scheduling_question_with_another_first_person_word_is_a_claim(text: str) -> None:
    assert "unsourced_experience_claim" in _kinds(_check(text)), _check(text)


@pytest.mark.parametrize(
    "text",
    [
        "I promoted shift leads into managers, which is the part of the work I'd bring to "
        "Vandelay & Company first.",
        "I promoted shift leads, which is what I'd be doing at Vandelay & Company since I led "
        "there.",
        "I'd be the rare director who has run a plumbing shop.",
        "I'd point to the 40 reps I led, which is the part of the work I'd point to first.",
        "The result I'd point to is one nobody planned for.",
        "I'd point to my years of covering enterprise accounts.",
    ],
)
def test_c_other_modal_claims_still_fail(text: str) -> None:
    assert _check(text) != []


@pytest.mark.parametrize(
    ("text", "word"),
    [
        # invented content before a closing stance clause (review 2026-10-23)
        ("I ran the oncology team, which is what I'd highlight.", "oncology"),
        (
            "I personally closed every hospital deal, and that is the result I would single "
            "out most.",
            "hospital",
        ),
        (
            "I also promoted five shift leads into team managers and later ran a nursing "
            "school, which is the part of the work I'd point to first.",
            "nursing",
        ),
    ],
)
def test_c_invented_content_before_a_stance_clause_still_fails(text: str, word: str) -> None:
    violations = _check(text)
    assert _flagged(violations, "unsourced_experience_claim", word), violations


@pytest.mark.parametrize(
    "text",
    [
        "Vandelay & Company AE turned sales leader.",
        "Vandelay & Company Senior AE turned sales leader.",
        "Vandelay & Company sales lead turned founder, I know this market.",
        "I was a Vandelay & Company sales lead before Larkspur Health.",
        # a seat word read as a verb only after a plural people noun
        # (review 2026-10-23)
        "Vandelay & Company teams lead turned founder.",
        "Vandelay & Company lead on hospital accounts, then Larkspur Health.",
        "Vandelay & Company partner across industries turned sales director.",
    ],
)
def test_d_a_tagline_still_fails(text: str) -> None:
    assert "fabricated_employer" in _kinds(_check(text)), _check(text)


@pytest.mark.parametrize(
    "text",
    ["It halves ramp time.", "I halved ramp time.", "We cut ramp time in half.", "halving churn"],
)
def test_e_halving_is_still_a_figure(text: str) -> None:
    assert any(c.value == 0.5 for c in numeric_claims(text))
