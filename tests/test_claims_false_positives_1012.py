"""Four false positives of the claims validator (review 2026-10-12), each
reproduced in a test letter below, and the near-miss fabrications that must
still fail:

(1) a hyphenated title the corpus holds ("Mid-Market Account Executive") split
    into an unsourced "Mid";
(2) a forward courtesy ("I'll bring a short agenda") was read as possession
    of experience;
(3) an aspiration ("I want to build that program at Tailspin") joined to a
    generic statement before it ("A partner program built with care keeps …")
    was read as a tenure claim on the participle "built" (and the "stay" of
    "resellers who … stay");
(4) a generic reason closing a claim ("…, because a program only grows when
    the people running it can teach") had its own words checked as the
    claim's.

Offline and LLM-free: validate_claims is a pure function."""

from __future__ import annotations

import re
from typing import Any

import pytest

from jobkit.validate.claims import validate_claims

# the roles and bullets the letters cite. "ledgerline" is a lowercase brand
# name (the persona's spelling); the held hyphenated title is Marlow Payroll's.
CORPUS: dict[str, Any] = {
    "identity": {"name": "Test Candidate", "email": "candidate@example.com"},
    "roles": [
        {
            "id": "role_haulmark",
            "company": "Haulmark",
            "title": "Regional Sales Manager",
            "start": "2024-03",
        },
        {
            "id": "role_ledgerline",
            "company": "ledgerline",
            "title": "Head of Revenue Operations",
            "start": "2021-11",
            "end": "2024-03",
        },
        {
            "id": "role_marlow",
            "company": "Marlow Payroll",
            "title": "Mid-Market Account Executive",
            "start": "2018-05",
            "end": "2021-11",
        },
        {
            "id": "role_keyward",
            "company": "Keyward Security",
            "title": "Inside Sales Representative",
            "start": "2016-08",
            "end": "2018-05",
        },
    ],
    "education": [
        {"school": "Lakeside State University", "degree": "B.S.", "years": "2008 – 2012"}
    ],
    "skill_index": {"Salesforce": "hands-on", "Gong": "hands-on"},
    "constraints": {
        "never_claim_titles": [
            "VP",
            "Vice President",
            "SVP",
            "Chief Revenue Officer",
            "CRO",
            "Head of Sales",
            "Director of Sales",
            "General Manager",
        ],
        "never_claim_phrases": ["treasury management sales"],
    },
    "bullets": [
        {
            "id": "b_053",
            "role_id": "role_ledgerline",
            "metric_confidence": "estimate",
            "text_long": "Stood up a deal desk that reviews every non-standard quote for 30+ "
            "sellers, bringing the average discount down from ~19% to ~12%.",
            "text_short": "Stood up a deal desk for 30+ sellers; average discount ~19% → ~12%.",
            "metric": "30+ sellers; average discount ~19% → ~12%",
        },
        {
            "id": "b_052",
            "role_id": "role_haulmark",
            "metric_confidence": "verified",
            "text_long": "Led a team of 9 AEs, reporting to the General Manager, "
            "and built the channel-partner program from 3 to 17 reseller partners in ~18 months.",
            "text_short": "Led 9 AEs; reported to the General Manager; grew channel partners "
            "3 → 17 in ~18 months.",
            "metric": "9 AEs; 3 → 17 reseller partners in ~18 months",
        },
        {
            "id": "b_047",
            "role_id": "role_haulmark",
            "metric_confidence": "verified",
            "text_long": "Grew the Northeast region from ~$3.2M to ~$5.6M in annual bookings, "
            "adding 41 new carrier accounts and routing ~18% of new bookings through reseller "
            "partners by 2025.",
            "metric": "~$3.2M → ~$5.6M annual bookings; 41 new accounts; ~18% partner-sourced",
        },
        {
            "id": "b_055",
            "role_id": "role_ledgerline",
            "metric_confidence": "verified",
            "text_long": "Trained the account managers to work every renewal from one "
            "Salesforce queue, raising the renewal rate from ~44% to ~71%; the same queue "
            "cleared 55–70 credit-limit requests a week.",
            "metric": "renewal rate ~44% → ~71%; 55–70 credit-limit requests a week",
        },
        {
            "id": "b_044",
            "role_id": "role_haulmark",
            "metric_confidence": "estimate",
            "text_long": "Turned a small pilot into a remote onboarding program for new "
            "carriers (2–3 carriers go live a month), cutting time to first dispatch by ~40%.",
            "metric": "2–3 carriers live/month; time to first dispatch -~40%",
        },
        {
            "id": "b_058",
            "role_id": "role_marlow",
            "metric_confidence": "verified",
            "text_long": "Made President's Club in 2019 and 2020, closing 124% and then 131% of "
            "annual quota on payroll deals.",
            "text_short": "President's Club 2019 and 2020 (124%, then 131% of annual quota).",
            "metric": "President's Club 2019, 2020; 124% and 131% of annual quota",
        },
    ],
}

TAILSPIN_JD = (
    "Tailspin sells scheduling software to independent fitness studios and is hiring a "
    "Channel Sales Manager, Studios to stand up its first reseller team. You will recruit, "
    "coach, and support partner managers and work with Marketing and Sales Operations."
)
WONKA_JD = (
    "Wonka is hiring a Sales Operations Manager to support its regional account teams. You "
    "will run the weekly pipeline review, keep the CRM clean, and work with Finance on "
    "quotas."
)
KNOWN = ["Tailspin", "Wonka", "Lumon", "Pilot", "Notion"]

TAILSPIN = {"target_company": "Tailspin", "job_title": "Channel Sales Manager, Studios"}
WONKA = {"target_company": "Wonka", "job_title": "Sales Operations Manager"}

TAILSPIN_CITED = ["b_052", "b_044", "b_053"]
WONKA_CITED = ["b_047", "b_055", "b_044", "b_053"]
MARLOW_CITED = ["b_058"]


def _check(text: str, cited: list[str], jd: str, job: dict[str, str]) -> list[dict[str, str]]:
    out = validate_claims(
        text,
        cited,
        CORPUS,
        jd_text=jd,
        known_companies=KNOWN,
        estimate_style="confident",  # the test profile's style
        **job,
    )
    assert out.ok == (not out.violations)
    return out.violations


def _kinds(violations: list[dict[str, str]]) -> set[str]:
    return {v["kind"] for v in violations}


def _flagged(violations: list[dict[str, str]], kind: str, word: str) -> bool:
    return any(v["kind"] == kind and re.search(rf"\b{word}\b", v["detail"]) for v in violations)


# ---------------------------------------------------------------------------
# (1) a hyphenated title the corpus holds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # two drafts of one letter's opening
        "At Marlow Payroll, where I was a Mid-Market Account Executive, I made President's Club "
        "in 2019 and 2020: 124% and then 131% of annual quota.",
        "Before Haulmark, I was a Mid-Market Account Executive at Marlow Payroll, and I made "
        "President's Club in 2019 and 2020.",
        # the held title, or a contiguous part of it, in any capitalization
        "At Marlow Payroll, where I was a Mid-market Account Executive, I made President's Club "
        "in 2019 and 2020.",
        "As a Mid-Market Account Executive at Marlow Payroll, I made President's Club in 2019 "
        "and 2020.",
        "As a Mid-Market rep at Marlow Payroll, I made President's Club in 2019 and 2020.",
    ],
)
def test_a_held_hyphenated_title_passes(text: str) -> None:
    assert _check(text, [*MARLOW_CITED, "b_052"], TAILSPIN_JD, TAILSPIN) == []


def test_a_held_title_is_sourced_whatever_is_cited() -> None:
    # every role's title is the candidate's own vocabulary, not only a cited
    # bullet's: "Mid-Market" needs no Marlow Payroll bullet to say it (b_058
    # never does)
    text = "Before Haulmark, I was a Mid-Market Account Executive at Marlow Payroll."
    assert _check(text, ["b_052", "b_058"], TAILSPIN_JD, TAILSPIN) == []


@pytest.mark.parametrize(
    ("text", "detail"),
    [
        # a title no corpus role holds: the never-claim list names a Director
        # of Sales; the run "Mid-Market Account Director" is not a held title
        (
            "As Mid-Market Account Director at Marlow Payroll, I made President's Club in 2019 "
            "and 2020.",
            "Director",
        ),
        (
            "As Mid-Market Account Executive and Director at Marlow Payroll, I made President's "
            "Club in 2019 and 2020.",
            "Director",
        ),
        (
            "As Mid-Market Account Executive, Director at Marlow Payroll, I made President's "
            "Club in 2019 and 2020.",
            "Director",
        ),
        # a hyphenated title the corpus does not hold
        ("As Mid-Level Account Executive at Marlow Payroll, I made President's Club.", None),
    ],
)
def test_a_title_the_corpus_does_not_hold_fails(text: str, detail: str | None) -> None:
    violations = _check(text, ["b_058", "b_052"], TAILSPIN_JD, TAILSPIN)
    assert _flagged(violations, "unsourced_proper_noun", "Mid"), violations
    if detail:
        assert any(detail in v["detail"] for v in violations), violations


def test_a_never_claim_title_still_fails() -> None:
    violations = _check("I was VP of Sales at Haulmark.", ["b_052"], TAILSPIN_JD, TAILSPIN)
    assert "never_claim_title" in _kinds(violations), violations
    assert _flagged(violations, "unsourced_proper_noun", "VP"), violations


@pytest.mark.parametrize(
    "text",
    [
        "Acme-Corp hired me to stand up a deal desk.",
        "At Acme-Corp, I stood up a deal desk.",
    ],
)
def test_a_hyphenated_unknown_name_still_fails(text: str) -> None:
    violations = _check(text, ["b_053"], TAILSPIN_JD, TAILSPIN)
    assert _flagged(violations, "unsourced_proper_noun", "Acme"), violations


def test_a_held_title_stays_at_its_own_employer() -> None:
    # "Mid-Market Account Executive" is Marlow Payroll's title, not Haulmark's
    text = "At Haulmark, where I was a Mid-Market Account Executive, I led a team of 9 AEs."
    violations = _check(text, ["b_052"], TAILSPIN_JD, TAILSPIN)
    assert "misattributed_claim" in _kinds(violations), violations


# ---------------------------------------------------------------------------
# (2) a forward courtesy is no possession of experience
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Choose a slot that suits you, and I'll bring a short agenda.",
        "I'll bring a few specific ideas.",
        "I'll have an agenda.",
        "I can bring a rough plan.",
        "I'll come with a few questions.",
        # after a short imperative scheduling clause and "and"/"then"/"so"
        "Suggest a day that works for you, and I'll bring a few ideas.",
        "Set up a call and I'll have an agenda.",
        "Choose a slot next week, and then I'll bring a short agenda.",
    ],
)
def test_a_forward_courtesy_passes(text: str) -> None:
    assert _check(text, WONKA_CITED, WONKA_JD, WONKA) == []


@pytest.mark.parametrize(
    ("text", "words"),
    [
        # an "about/on/for …" after the courtesy's noun can carry a claim of
        # its own, so no words may follow the noun: each of these passed while
        # the courtesy took up to 8 words after it
        (
            "I'll come with questions about franchise licensing after running it at Haulmark.",
            ["franchise", "licensing"],
        ),
        (
            "I'll come with ideas on franchisee onboarding after a casino sales stint.",
            ["casino", "stint"],
        ),
        ("I'll bring notes on franchise licensing wins at Haulmark.", ["franchise", "wins"]),
        (
            "I'll come with ideas about aviation sales after flying charters at Haulmark.",
            ["aviation", "charters"],
        ),
        ("I'll come with ideas about aviation sales after flying charters.", ["charters"]),
        # a false rejection taken over a false acceptance: the same tail with
        # no claim in it is read as one too
        ("I'll bring a few specific ideas about your pipeline.", ["ideas"]),
        ("I'll have an agenda for the call.", ["agenda"]),
    ],
)
def test_words_after_a_courtesy_are_a_claim(text: str, words: list[str]) -> None:
    violations = _check(text, WONKA_CITED, WONKA_JD, WONKA)
    for w in words:
        assert _flagged(violations, "unsourced_experience_claim", w), (w, violations)


@pytest.mark.parametrize(
    ("text", "word"),
    [
        ("I'll come with deep aviation experience.", "aviation"),
        ("I'd bring years of franchise leadership.", "franchise"),
        ("I'll come with a book of enterprise relationships.", "relationships"),
        # the courtesy holds only for a sentence that closes on it
        (
            "Choose a slot that suits you, and I'll bring a short agenda and a book of "
            "enterprise relationships.",
            "relationships",
        ),
        (
            "Choose a slot that suits you, and I'll bring a short agenda, and a book of "
            "enterprise relationships.",
            "relationships",
        ),
        ("I'll come with ideas from my years at Haulmark.", "ideas"),
        # only a short list of plain adjectives
        ("I'll come with seasoned questions.", "seasoned"),
        ("I'll come with insider ideas.", "insider"),
        ("I'll bring battle-tested ideas.", "tested"),
        # a first person after the courtesy is a claim
        ("I'll come with questions about the deals I signed at Haulmark.", "signed"),
    ],
)
def test_possession_after_a_courtesy_verb_still_fails(text: str, word: str) -> None:
    violations = _check(text, WONKA_CITED, WONKA_JD, WONKA)
    assert _flagged(violations, "unsourced_experience_claim", word), violations


@pytest.mark.parametrize(
    ("text", "words"),
    [
        # a claim BEFORE the courtesy (review 2026-10-12): each failed at HEAD,
        # where "I'll come with"/"bring"/"have" made the sentence a claim, and
        # passed while the courtesy was set aside whatever came before it. No
        # cited bullet says franchise, casino or procurement
        (
            "With deep franchise relationships, I'll bring a short agenda.",
            ["franchise", "deep"],
        ),
        (
            "With deep franchise relationships, I'll bring a few ideas.",
            ["franchise", "relationships"],
        ),
        ("With deep franchise relationships, I'll have a plan.", ["franchise", "relationships"]),
        (
            "I know casino procurement cold, and I'll bring a short agenda.",
            ["casino", "procurement"],
        ),
        (
            "I ran franchise licensing at Haulmark, so I'll bring a short agenda.",
            ["franchise", "licensing"],
        ),
        ("Having run franchise licensing, I'll bring a short agenda.", ["franchise"]),
        # a sentence joined to the courtesy's by a back-reference (_units)
        (
            "I ran franchise licensing at Haulmark. With that pipeline, I'll bring a few ideas.",
            ["franchise", "licensing"],
        ),
        (
            "Choose a slot that suits you, and I'll bring a short agenda. That pipeline can wait.",
            ["pipeline"],
        ),
        # a lead-in that is not a short imperative scheduling clause: a
        # capital, a first person, a verb or a word off the list. False
        # rejections taken over a false acceptance (Hard Rule 8)
        ("Choose a slot at Haulmark, and I'll bring a short agenda.", ["agenda"]),
        ("Send me a slot, and I'll bring a short agenda.", ["agenda"]),
        ("Choose franchise licensing veterans, and I'll bring a short agenda.", ["franchise"]),
        ("Happy to talk, and I'll bring a few ideas.", ["ideas"]),
    ],
)
def test_a_claim_beside_a_courtesy_still_fails(text: str, words: list[str]) -> None:
    violations = _check(text, WONKA_CITED, WONKA_JD, WONKA)
    for w in words:
        assert _flagged(violations, "unsourced_experience_claim", w), (w, violations)


def test_a_closing_paragraph_ending_on_a_courtesy_passes() -> None:
    text = (
        "Renewals are where a sales team learns whether customers meant it. At ledgerline I "
        "trained the account managers to work every renewal from one Salesforce queue, and the "
        "renewal rate went from 44% to 71%. For this role, that means a weekly review built on "
        "numbers the account teams already trust. I'd like to hear which review you would "
        "change first. Choose a slot that suits you, and I'll bring a short agenda."
    )
    assert _check(text, WONKA_CITED, WONKA_JD, WONKA) == []


# ---------------------------------------------------------------------------
# (3) an aspiration joined to a generic statement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "A partner program built with care keeps the resellers who sell and stay. I want to "
        "build that program at Tailspin.",
        "I want to build that program at Tailspin. A partner program built with care keeps the "
        "resellers who sell and stay.",
        "I want to build that program at Tailspin.",
    ],
)
def test_an_aspiration_beside_a_generic_statement_passes(text: str) -> None:
    assert _check(text, TAILSPIN_CITED, TAILSPIN_JD, TAILSPIN) == []


@pytest.mark.parametrize(
    "text",
    [
        "I built that program at Tailspin.",
        "I helped build the reseller program at Tailspin before.",
        "Tailspin was where I built my first program.",
        # the generic statement excuses no claim in the target's own sentence
        "A partner program built with care keeps the resellers who sell and stay. I built "
        "that program at Tailspin.",
        "A partner program built with care keeps the resellers who sell and stay. I helped "
        "build the reseller program at Tailspin before.",
        "A partner program built with care needs exactly that. I want to build that program "
        "at Tailspin again.",
        # a sentence with a first person, the target, "your" or a word of
        # re-contact is no generic statement
        "The program I built with care keeps resellers. I want to build that program at Tailspin.",
        "I built a program with care. I want to build that program at Tailspin.",
        "Built a program with care. I want to build that program at Tailspin.",
        "A program built at Tailspin keeps resellers. I want to build that program at Tailspin.",
        "A partner program built with care needs your support. I want to build that program at "
        "Tailspin.",
        "Some programs are worth coming back to. I want to build that program at Tailspin.",
    ],
)
def test_a_tenure_claim_at_the_target_still_fails(text: str) -> None:
    violations = _check(text, TAILSPIN_CITED, TAILSPIN_JD, TAILSPIN)
    assert "fabricated_employer" in _kinds(violations), violations


# ---------------------------------------------------------------------------
# (4) a generic reason closing a claim
# ---------------------------------------------------------------------------

DEVELOPED = "I also turned a small pilot into a remote onboarding program for new carriers"


def test_a_generic_reason_is_no_part_of_the_claim() -> None:
    # "running" and "teach" are the reason's words. The reason has no first
    # person, no past tense, no name or figure, and its subject ("a program")
    # is a word of the cited b_052
    text = f"{DEVELOPED}, because a program only grows when the people running it can teach."
    assert _check(text, TAILSPIN_CITED, TAILSPIN_JD, TAILSPIN) == []


@pytest.mark.parametrize(
    ("reason", "words"),
    [
        # a first person anywhere in the reason
        ("a program I built only grows when the people running it can teach", ["running", "teach"]),
        (
            "a program only grows when the people running it can teach, and I made sure mine could",
            ["running", "teach", "made"],
        ),
        # a past tense
        ("a program only grew when the people running it could teach", ["running", "teach"]),
        # a subject no cited bullet names
        ("a franchisee only thrives when the people around it can lead", ["franchisee"]),
        # a name or a figure
        ("a program at Haulmark only grows when the people running it can teach", ["teach"]),
        ("a program of 9 only grows when the people running it can teach", ["teach"]),
        # a word of experience, or any other marker of a claim of its own
        ("a program only grows when its manager has aviation experience", ["aviation"]),
        ("a program only grows when its manager brings franchise depth", ["franchise", "depth"]),
        ("a program only grows when its manager can lead as a former sommelier", ["sommelier"]),
    ],
)
def test_a_reason_that_is_not_generic_is_still_checked(reason: str, words: list[str]) -> None:
    violations = _check(f"{DEVELOPED}, because {reason}.", TAILSPIN_CITED, TAILSPIN_JD, TAILSPIN)
    for w in words:
        assert _flagged(violations, "unsourced_experience_claim", w), (w, violations)


def test_a_generic_reason_excuses_nothing_in_the_claim() -> None:
    reason = "because a program only grows when the people running it can teach"
    # the claim's own words are still checked…
    violations = _check(
        f"I can coach franchise licensing, {reason}.", TAILSPIN_CITED, TAILSPIN_JD, TAILSPIN
    )
    assert _flagged(violations, "unsourced_experience_claim", "franchise"), violations
    # …and a claim with nothing left to source still fails
    violations = _check(f"I'd bring that, {reason}.", TAILSPIN_CITED, TAILSPIN_JD, TAILSPIN)
    assert any(
        v["kind"] == "unsourced_experience_claim" and "names nothing" in v["detail"]
        for v in violations
    ), violations


def test_a_letter_body_with_a_held_title_and_a_generic_reason_passes() -> None:
    text = (
        "At Haulmark I led a team of 9 AEs, and I built the channel-partner program from 3 to "
        "17 reseller partners in 18 months. New carriers go live faster together than alone, "
        "so I turned a small pilot into a remote onboarding program, 2–3 carriers a month. It "
        "cut time to first dispatch by 40%. I also built the channel-partner program for "
        "reseller partners, because a program only grows when the people running it can "
        "teach.\n\n"
        "Before Haulmark I was Head of Revenue Operations at ledgerline, where I stood up a "
        "deal desk that reviews every non-standard quote for 30+ sellers, and before that a "
        "Mid-Market Account Executive at Marlow Payroll. A first reseller team needs that "
        "discipline from its first quote. I'd like to help Tailspin set it up. "
        "Let's set up a conversation."
    )
    assert _check(text, [*TAILSPIN_CITED, *MARLOW_CITED], TAILSPIN_JD, TAILSPIN) == []


# review 2026-10-12: a "generic" sentence that points at a place ("there",
# "here") is not generic, so it cannot launder a tenure claim at the target
POINTING_LAUNDER = [
    "The program left behind there still needs that. I want to build that program at Tailspin.",
    "The partner program left there still needs that. I want to lead that program at Tailspin.",
    "The program built there with care was the best job of a career. I want to build that "
    "program at Tailspin.",
    "The program built here with care still needs that. I want to build that program at Tailspin.",
    "A program built there last year still needs that. I want to build that program at Tailspin.",
    "A program built with care there is worth rebuilding. I want to build that program at "
    "Tailspin.",
]


@pytest.mark.parametrize("text", POINTING_LAUNDER)
def test_a_pointing_word_keeps_a_sentence_from_being_generic(text: str) -> None:
    assert "fabricated_employer" in _kinds(_check(text, TAILSPIN_CITED, TAILSPIN_JD, TAILSPIN))
