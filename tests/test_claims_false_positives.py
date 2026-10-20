"""Three narrow false positives of the claims validator (review 2026-10-10),
each reproduced in a test letter below, and the near-miss fabrications that
must still fail:

(a) a common word capitalized only because it starts a sentence ("Success
    shows up later, …", "People budget against it, …", "Pick any afternoon
    …") was read as an unsourced proper noun;
(b) a cited move restated with a neutral change verb ("renewal rate climbed
    from 44% to 71%") failed the experience-claim check on the verb;
(c) "peaked at $110K" for a source's "~$110K at peak" was an added "max"
    bound.

The second review of 2026-10-10 found three false acceptances in (a) and (b),
each tested below: a name the JD writes at the head of a title-cased run
("Oracle Cloud Infrastructure") or also in lowercase ("Slack" and "slack")
passed at the start of a sentence; a subset of the words before a move was
enough of a subject ("CAC climbed from 17% to 26%" beside "cutting CAC by
lifting win rate from ~17% to ~26%"); and a subordinator ended the words
after a move ("…climbed from 44% to 71% that week").

The third review of 2026-10-10 found one more in (b): the subject was compared
on its content words of three letters or more, so "AI renewal rate climbed
from 44% to 71%" passed as b_055's move. Every word is compared now.

Offline and LLM-free: validate_claims is a pure function."""

from __future__ import annotations

import re
from importlib.resources import files
from typing import Any

import pytest

from jobkit.validate.claims import validate_claims
from jobkit.validate.common_words import is_common_word
from jobkit.validate.provenance import numeric_claims

# the bullets the three letters cited, as the corpus states them
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
    "skill_index": {"Salesforce": "hands-on", "Gong": "hands-on", "MEDDIC": "coached on it"},
    "constraints": {
        "never_claim_titles": ["VP", "Vice President", "CRO", "Head of Sales", "General Manager"],
        "never_claim_phrases": ["treasury management sales"],
    },
    "bullets": [
        {
            "id": "b_050",
            "role_id": "role_haulmark",
            "metric_confidence": "verified",
            "text_long": "Sold a fall capacity add-on to 17 regional carriers; add-on bookings "
            "grew from ~$15K/week in spring to $45–60K/week through the fall, peaking at "
            "~$110K.",
            "text_short": "Fall capacity add-on, 17 carriers: ~$15K/wk in spring → $45–60K/wk "
            "in the fall, peaking at ~$110K.",
            "metric": "~$15K/week → $45–60K/week, peaking at ~$110K; 17 carriers",
        },
        {
            "id": "b_055",
            "role_id": "role_ledgerline",
            "metric_confidence": "verified",
            "text_long": "Trained the account managers to work every renewal from one "
            "Salesforce queue, raising the renewal rate from ~44% to ~71%; the same queue "
            "cleared 55–70 credit-limit requests a week.",
            "text_short": "Trained the account managers on one Salesforce renewal queue; renewal "
            "rate ~44% → ~71%; 55–70 credit-limit requests cleared a week.",
            "metric": "renewal rate ~44% → ~71%; 55–70 credit-limit requests a week",
        },
        {
            "id": "b_044",
            "role_id": "role_haulmark",
            "metric_confidence": "estimate",
            "text_long": "Turned a small pilot into a remote onboarding program for new "
            "carriers, cutting time to first dispatch by ~40% (2–3 carriers go live a month).",
            "text_short": "Remote onboarding for new carriers; time to first dispatch cut ~40%.",
            "metric": "time to first dispatch -~40%; 2–3 carriers live/month",
        },
        {
            "id": "b_041",
            "role_id": "role_keyward",
            "metric_confidence": "estimate",
            "text_long": "Closed free trials from small IT teams, 12–15 new accounts a "
            "month, and signed multi-site accounts at Nakatomi, Ashgrove and Pemberton Mills.",
            "text_short": "Free trials to paid plans, 12–15 new accounts/mo; signed "
            "Nakatomi, Ashgrove, Pemberton Mills.",
            "metric": "12–15 new accounts/month",
        },
    ],
}

# short invented postings with the capitals that matter: "Customer Success"
# only ever title-cased, "People teams" beside a lowercase "people"
BDR_JD = (
    "Lumon wants a Business Development Representative to prospect into regional hospital "
    "groups. Partner Marketing supplies the target lists, and Solutions and Customer Success "
    "join every first meeting. Strong BDRs here are promoted to Account Executive."
)
OPS_JD = (
    "Lumon needs a Revenue Strategy Lead to build the annual plan with the CRO. Quota models "
    "come from Field Operations, forecasts from Strategy & Analytics and hiring plans from the "
    "People teams, so the job is half people and half spreadsheets. Account plans live in "
    "Notion."
)
ENABLEMENT_JD = (
    "Tyrell wants one person to own onboarding and coaching for its sales floor. You will "
    "build the courses new account executives take before their first deal."
)
KNOWN = ["Lumon", "Tyrell", "Haulmark", "Pilot", "Notion"]

BDR = {"target_company": "Lumon", "job_title": "Business Development Representative"}
OPS = {"target_company": "Lumon", "job_title": "Revenue Strategy Lead"}
ENABLEMENT = {"target_company": "Tyrell", "job_title": "Sales Enablement Lead"}


def _check(
    text: str,
    cited: list[str],
    jd: str,
    job: dict[str, str],
    *,
    corpus: dict[str, Any] | None = None,
    known: list[str] | None = None,
) -> list[dict[str, str]]:
    out = validate_claims(
        text,
        cited,
        CORPUS if corpus is None else corpus,
        jd_text=jd,
        known_companies=KNOWN if known is None else known,
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
# (a) a common word at the start of a sentence is not a proper noun
# ---------------------------------------------------------------------------


def test_success_at_the_start_of_a_sentence_passes() -> None:
    # the JD capitalizes "Customer Success"
    text = (
        "Volume alone says little about a new rep. Success shows up later, when a closer "
        "inherits the account and the notes still make sense."
    )
    assert _check(text, ["b_041"], BDR_JD, BDR) == []


def test_people_at_the_start_of_a_sentence_passes() -> None:
    # the JD has "People teams" and also "people"
    text = (
        "A renewal number is a promise to the rest of the company. People budget against it, "
        "so a guess costs real money."
    )
    assert _check(text, ["b_055"], OPS_JD, OPS) == []


def test_pick_at_the_start_of_a_sentence_is_no_proper_noun() -> None:
    assert _check("Pick any afternoon that suits you.", ["b_055"], ENABLEMENT_JD, ENABLEMENT) == []
    # a whole closing sentence: "Pick" is no proper noun. The sentence may
    # still fail other checks that this fix does not touch
    closing = (
        "Pick any afternoon that suits you, and I'll show you how the renewal queue is set up."
    )
    assert "unsourced_proper_noun" not in _kinds(
        _check(closing, ["b_055"], ENABLEMENT_JD, ENABLEMENT)
    )


def test_a_letter_body_restating_two_bullets_passes() -> None:
    text = (
        "A renewal number is a promise to the rest of the company. People budget against it, "
        "so a guess costs real money.\n\n"
        "At ledgerline, I trained the account managers to work every renewal from one "
        "Salesforce queue. The renewal rate went from 44% to 71%, and the same queue cleared "
        "55–70 credit-limit requests a week. At Haulmark I sold a fall capacity add-on to 17 "
        "regional carriers. Add-on bookings grew from $15K a week in spring to $45–60K a week "
        "through the fall and peaked at $110K."
    )
    assert _check(text, ["b_055", "b_050"], OPS_JD, OPS) == []


@pytest.mark.parametrize(
    ("text", "word"),
    [
        # an unknown company is a name wherever it stands
        ("Brightwave hired me to rebuild its sales team.", "Brightwave"),
        ("Zentrix was where I first carried a quota.", "Zentrix"),
        # a common word the JD uses as a name ("Notion", never "notion")
        ("Notion is where I kept every account plan.", "Notion"),
        # a common word that is a known company (the DB's companies)
        ("Pilot hired me to run its sales team.", "Pilot"),
        # a title-cased run of the JD, written whole
        ("Field Operations gave me my best accounts.", "Field"),
    ],
)
def test_a_name_at_the_start_of_a_sentence_still_fails(text: str, word: str) -> None:
    violations = _check(text, ["b_055"], OPS_JD, OPS)
    assert _flagged(violations, "unsourced_proper_noun", word), violations


def test_a_word_after_the_head_of_a_title_cased_run_alone_is_the_word() -> None:
    # OPS_JD has "Field Operations" and "Strategy & Analytics": a word after
    # the head of the run, alone, is not that name
    assert _check("Operations reps need clean data.", ["b_055"], OPS_JD, OPS) == []
    assert _check("Analytics alone settles nothing.", ["b_055"], OPS_JD, OPS) == []


def test_the_head_of_a_title_cased_run_alone_is_a_name() -> None:
    # second review 2026-10-10: the head of a run is a name, as before the
    # first review ("Oracle" of "Oracle Cloud Infrastructure"). "Field" of
    # "Field Operations" is one too: a false rejection the rules allow
    violations = _check("Field reps need clean data.", ["b_055"], OPS_JD, OPS)
    assert _flagged(violations, "unsourced_proper_noun", "Field"), violations


@pytest.mark.parametrize(
    ("jd", "text", "cited", "word"),
    [
        # the head of a title-cased run of common words
        (
            "You will sell into Oracle Cloud Infrastructure teams.",
            "Oracle was one of my accounts at Keyward Security.",
            ["b_041"],
            "Oracle",
        ),
        (
            "Shoppers check out with Apple Pay Later at most of our merchants.",
            "Apple opened with me at Keyward Security.",
            ["b_041"],
            "Apple",
        ),
        # a capital the JD also writes in lowercase, not before a team word
        (
            "We work async in Slack and keep slack in the schedule.",
            "Slack ran my whole team at ledgerline.",
            ["b_055"],
            "Slack",
        ),
    ],
)
def test_a_name_of_the_jd_at_the_start_of_a_sentence_fails(
    jd: str, text: str, cited: list[str], word: str
) -> None:
    # second review 2026-10-10: each passed after the first review. No DB
    # company backs them (known_companies is empty), as in production, where
    # jobs.company_name has none of these
    violations = _check(text, cited, jd, BDR, known=[])
    assert _flagged(violations, "unsourced_proper_noun", word), violations


# third review 2026-10-10: a sentence-initial word counted as common when the
# JD or the corpus wrote it in lowercase anywhere — a domain, a handle, a
# product written lowercase. Only the bundled list makes a word common now.
LOWERCASE_NAME_JD = (
    "Lumon is hiring a Revenue Strategy Lead. Apply through brightwave, our recruiting "
    "partner, or email zentrix@lumon.example. Our reps log every call in gong."
)


@pytest.mark.parametrize(
    ("text", "word"),
    [
        ("Brightwave hired me to run sales.", "Brightwave"),
        ("Zentrix was where I first carried a quota.", "Zentrix"),
    ],
)
def test_a_lowercase_spelling_in_the_jd_never_makes_a_name_common(text: str, word: str) -> None:
    assert not is_common_word(word)
    violations = _check(text, ["b_055"], LOWERCASE_NAME_JD, OPS, known=[])
    assert _flagged(violations, "unsourced_proper_noun", word), violations


def test_a_lowercase_spelling_in_the_corpus_never_makes_a_name_common() -> None:
    corpus = {
        **CORPUS,
        "bullets": [
            *CORPUS["bullets"],
            {
                "id": "b_099",
                "role_id": "role_ledgerline",
                "metric_confidence": "verified",
                "text_long": "Moved the team's call notes out of brightwave into Salesforce.",
            },
        ],
    }
    violations = _check("Brightwave hired me to run sales.", ["b_055"], OPS_JD, OPS, corpus=corpus)
    assert _flagged(violations, "unsourced_proper_noun", "Brightwave"), violations


def test_common_words_at_the_start_of_a_sentence_still_pass_on_the_list_alone() -> None:
    # the three sentence openings above, against a JD that writes none of the words
    bare_jd = "Lumon is hiring a Revenue Strategy Lead."
    assert _check("Success shows up later.", ["b_041"], bare_jd, BDR) == []
    assert _check("Pick any afternoon that suits you.", ["b_055"], bare_jd, ENABLEMENT) == []
    assert _check("People budget against it.", ["b_055"], bare_jd, OPS) == []


def test_common_word_list() -> None:
    for w in ("Success", "people", "Pick", "Calls", "Climbed", "Booking", "Isn't", "Don't"):
        assert is_common_word(w), w
    for w in ("Brightwave", "Contoso", "Oscorp", "London", "Google", "O'Brien", "AI"):
        assert not is_common_word(w), w
    # the list is one place: lowercase words, one per line, sorted, no repeats
    text = files("jobkit.validate").joinpath("common_words.txt").read_text(encoding="utf-8")
    words = [w for w in text.splitlines() if not w.startswith("#")]
    assert all(re.fullmatch(r"[a-z]+", w) for w in words)
    assert words == sorted(set(words))


# ---------------------------------------------------------------------------
# (b) a cited move restated with a neutral change verb
# ---------------------------------------------------------------------------

ORG = "I trained the account managers to work every renewal from one Salesforce queue"


@pytest.mark.parametrize(
    "move",
    [
        "climbed from 44% to 71%",
        "grew from 44% to 71%",
        "rose from 44% to 71%",
        "increased from 44% to 71%",
        "improved from 44% to 71%",
        "went from 44% to 71%",
        "climbed to 71%",
    ],
)
def test_a_cited_move_restated_with_a_change_verb_passes(move: str) -> None:
    text = f"{ORG}, and renewal rate {move}."
    assert _check(text, ["b_055"], ENABLEMENT_JD, ENABLEMENT) == []


@pytest.mark.parametrize(
    ("move", "kinds", "words"),
    [
        # unsourced numbers
        (
            "renewal rate climbed from 50% to 95%",
            {"fabricated_number", "unsourced_experience_claim"},
            ["climbed"],
        ),
        ("renewal rate rose 20 points", {"fabricated_number"}, ["rose", "points"]),
        # the bullet's figures, the wrong way round
        ("renewal rate climbed from 71% to 44%", {"unsourced_experience_claim"}, ["climbed"]),
        ("renewal rate increased to 44%", {"unsourced_experience_claim"}, ["increased"]),
        # another subject: b_055 has "credit-limit" and "requests", not before the move
        (
            "credit-limit requests climbed from 44% to 71%",
            {"unsourced_experience_claim"},
            ["climbed"],
        ),
        # part of the move's words is not its subject (second review 2026-10-10)
        ("rate climbed from 44% to 71%", {"unsourced_experience_claim"}, ["climbed"]),
        ("win rate climbed from 44% to 71%", {"unsourced_experience_claim"}, ["climbed", "win"]),
        # the candidate as the subject: a causative verb needs its own source
        ("I grew renewal rate from 44% to 71%", {"unsourced_experience_claim"}, ["grew"]),
        # a verb not on the list
        ("renewal rate jumped from 44% to 71%", {"unsourced_experience_claim"}, ["jumped"]),
        # only the verb is excused
        (
            "renewal rate climbed from 44% to 71% overnight",
            {"unsourced_experience_claim"},
            ["overnight"],
        ),
        # …and only for the move itself: b_055 has "week", not this timeline
        (
            "renewal rate climbed from 44% to 71% in a week",
            {"unsourced_experience_claim"},
            ["climbed"],
        ),
        # a subordinator does not end the words after the move (second review
        # 2026-10-10): every word up to the next punctuation mark is checked
        (
            "renewal rate climbed from 44% to 71% that week",
            {"unsourced_experience_claim"},
            ["climbed"],
        ),
        (
            "renewal rate climbed from 44% to 71% after a week",
            {"unsourced_experience_claim"},
            ["climbed"],
        ),
        (
            "renewal rate climbed to 71% that quarter",
            {"unsourced_experience_claim"},
            ["climbed"],
        ),
        (
            "renewal rate rose from 44% to 71% before Q3",
            {"unsourced_experience_claim"},
            ["rose"],
        ),
        # nor does "and": "did so" and "that" are function words, "week" is b_055's
        (
            "renewal rate climbed from 44% to 71% and did so that week",
            {"unsourced_experience_claim"},
            ["climbed"],
        ),
        # a timeline anywhere in the sentence, not only after the move: b_055's
        # "week" and the role's 2024 would source these words
        (
            "within a week, renewal rate climbed from 44% to 71%",
            {"unsourced_experience_claim"},
            ["climbed"],
        ),
        (
            "each week, renewal rate climbed from 44% to 71%",
            {"unsourced_experience_claim"},
            ["climbed"],
        ),
        (
            "by 2024, renewal rate climbed from 44% to 71%",
            {"unsourced_experience_claim"},
            ["climbed"],
        ),
        (
            "in March, renewal rate climbed from 44% to 71%",
            {"unsourced_experience_claim"},
            ["climbed"],
        ),
        # a negation or a modal is no restatement of the move
        (
            "renewal rate never climbed from 44% to 71%",
            {"unsourced_experience_claim"},
            ["climbed"],
        ),
        (
            "renewal rate didn't climb from 44% to 71%",
            {"unsourced_experience_claim"},
            ["climb"],
        ),
        (
            "renewal rate would climb from 44% to 71%",
            {"unsourced_experience_claim"},
            ["climb"],
        ),
    ],
)
def test_a_change_verb_never_carries_an_unsourced_move(
    move: str, kinds: set[str], words: list[str]
) -> None:
    violations = _check(f"{ORG}, and {move}.", ["b_055"], ENABLEMENT_JD, ENABLEMENT)
    assert kinds <= _kinds(violations), violations
    for w in words:
        assert _flagged(violations, "unsourced_experience_claim", w), (w, violations)


def test_a_figures_own_rate_beside_a_restated_move_passes() -> None:
    # "a week" is the rate of 55–70 (b_055's "a week"), not a timeline
    text = (
        f"{ORG}, and renewal rate climbed from 44% to 71%, and the same queue cleared "
        "55–70 credit-limit requests a week."
    )
    assert _check(text, ["b_055"], ENABLEMENT_JD, ENABLEMENT) == []


# a bullet whose clause names one metric (CAC) and moves another (win rate)
CAC_CORPUS: dict[str, Any] = {
    **CORPUS,
    "bullets": [
        *CORPUS["bullets"],
        {
            "id": "b_x",
            "role_id": "role_ledgerline",
            "metric_confidence": "verified",
            "text_long": "Trained the account managers to work every renewal from one "
            "Salesforce queue, cutting CAC by lifting win rate from ~17% to ~26%.",
        },
        {
            "id": "b_y",
            "role_id": "role_ledgerline",
            "metric_confidence": "verified",
            "text_long": "Trained the account managers to work every renewal from one "
            "Salesforce queue, lifting win rate for enterprise deals from ~17% to ~26%.",
        },
    ],
}


@pytest.mark.parametrize(
    ("cited", "move", "verb"),
    [
        # second review 2026-10-10: CAC is named before the move, not moved
        (["b_x"], "CAC climbed from 17% to 26%", "climbed"),
        (["b_x"], "CAC went from 17% to 26%", "went"),
        # the words after "for" are not the move's subject: win rate is
        (["b_y"], "enterprise deals climbed from 17% to 26%", "climbed"),
        (["b_y"], "win rate climbed from 17% to 26%", "climbed"),
    ],
)
def test_a_change_verb_needs_the_moves_own_subject(cited: list[str], move: str, verb: str) -> None:
    text = f"{ORG}, and {move}."
    violations = _check(text, cited, ENABLEMENT_JD, ENABLEMENT, corpus=CAC_CORPUS)
    assert _flagged(violations, "unsourced_experience_claim", verb), violations


@pytest.mark.parametrize(
    ("cited", "move"),
    [
        # "by lifting win rate": the verb after "by" opens the move's words
        (["b_x"], "win rate climbed from 17% to 26%"),
        (["b_x"], "win rate went from 17% to 26%"),
        (["b_y"], "win rate for enterprise deals climbed from 17% to 26%"),
    ],
)
def test_a_change_verb_with_the_moves_own_subject_passes(cited: list[str], move: str) -> None:
    text = f"{ORG}, and {move}."
    assert _check(text, cited, ENABLEMENT_JD, ENABLEMENT, corpus=CAC_CORPUS) == []


@pytest.mark.parametrize(
    "subject",
    [
        # third review 2026-10-10: compared on content words of three letters
        # or more, these were b_055's "renewal rate" and passed; nothing
        # else in the validator flags "AI" or "AE"
        "AI renewal rate",
        "AE renewal rate",
        "AI-only renewal rate",
        # a stop word is a qualifier too
        "only renewal rate",
        "all renewal rate",
        "other renewal rate",
        # a determiner, then a qualifier
        "the AI renewal rate",
        # "my" moves a team's metric onto the candidate
        "my renewal rate",
    ],
)
def test_a_restated_moves_subject_is_compared_word_for_word(subject: str) -> None:
    violations = _check(
        f"{ORG}, and {subject} climbed from 44% to 71%.", ["b_055"], ENABLEMENT_JD, ENABLEMENT
    )
    assert _flagged(violations, "unsourced_experience_claim", "climbed"), violations


@pytest.mark.parametrize("determiner", ["the", "our", "its"])
def test_a_restated_moves_subject_may_open_with_a_determiner(determiner: str) -> None:
    text = f"{ORG}, and {determiner} renewal rate climbed from 44% to 71%."
    assert _check(text, ["b_055"], ENABLEMENT_JD, ENABLEMENT) == []


def test_a_change_verb_used_causatively_elsewhere_is_not_excused() -> None:
    text = (
        "I grew the Salesforce queue and trained the account managers to work every renewal "
        "from it, and renewal rate grew from 44% to 71%."
    )
    violations = _check(text, ["b_055"], ENABLEMENT_JD, ENABLEMENT)
    assert _flagged(violations, "unsourced_experience_claim", "grew"), violations


# ---------------------------------------------------------------------------
# (c) peak / peaked / at peak are one qualifier
# ---------------------------------------------------------------------------

USAGE = "Add-on bookings grew to $45–60K a week through the fall, {peak}."


@pytest.mark.parametrize("peak", ["peaked at $110K", "peaking at ~$110K", "~$110K at peak"])
def test_a_peak_restates_the_sources_peak(peak: str) -> None:
    # "peaked at $110K" for b_050's "peaking at ~$110K"
    assert _check(USAGE.format(peak=peak), ["b_050"], OPS_JD, OPS) == []


@pytest.mark.parametrize(
    ("peak", "kinds"),
    [
        ("peaked at $150K", {"fabricated_number"}),
        # a peak is not a "max" bound, and it is not dropped
        ("topped out at $110K", {"dropped_qualifier"}),
        ("up to $110K", {"dropped_qualifier"}),
        ("reaching $110K", {"dropped_qualifier"}),
    ],
)
def test_a_peak_is_never_another_figure(peak: str, kinds: set[str]) -> None:
    violations = _check(USAGE.format(peak=peak), ["b_050"], OPS_JD, OPS)
    assert kinds <= _kinds(violations), violations


# review 2026-10-10 (second pass): the peak equivalence holds only for a peak
# on the same figure, value and unit exact, with no other qualifier. A peak
# phrase after the number stopped the scan, so a bound stacked behind it
# ("~$110K at peak or more") was never read and the figure passed.
@pytest.mark.parametrize(
    ("peak", "kinds"),
    [
        ("over $110K at peak", {"added_qualifier"}),
        ("peaked above $110K", {"dropped_qualifier"}),
        ("peaked at $150K", {"fabricated_number"}),
        ("peaked at over $110K", {"added_qualifier"}),
        ("~$110K at peak or more", {"added_qualifier"}),
        ("$110K at peak and above", {"added_qualifier"}),
        ("$110K peak and up", {"added_qualifier"}),
        ("$110K at peak, tops", {"added_qualifier"}),
        ("$110K at peak or less", {"added_qualifier"}),
        ("$110K-plus at peak", {"added_qualifier"}),
        ("peaked at nearly $110K", {"added_qualifier"}),
    ],
)
def test_a_peak_never_hides_another_qualifier(peak: str, kinds: set[str]) -> None:
    violations = _check(USAGE.format(peak=peak), ["b_050"], OPS_JD, OPS)
    assert kinds <= _kinds(violations), violations


def test_peaked_at_restates_at_peak_only_on_the_same_figure() -> None:
    assert _check(USAGE.format(peak="peaked at $110K"), ["b_050"], OPS_JD, OPS) == []
    # a peak on another of the bullet's figures is not its peak: $60K is the
    # top of a plain range, $15K the starting point
    text = "Add-on bookings grew from $15K a week, peaking at $60K a week."
    violations = _check(text, ["b_050"], OPS_JD, OPS)
    assert _kinds(violations) & {"collapsed_range", "added_qualifier"}, violations
    text = "Add-on bookings peaked at $15K a week and grew to $45–60K a week."
    assert "added_qualifier" in _kinds(_check(text, ["b_050"], OPS_JD, OPS))


def test_stacked_bounds_after_the_number_are_all_read() -> None:
    def bounds(text: str) -> frozenset[str]:
        return next(c for c in numeric_claims(text) if c.kind == "currency").bounds

    assert bounds("~$110K at peak or more") == {"peak", "min"}
    assert bounds("$110K peak, tops.") == {"peak", "max"}
    assert bounds("$110K-plus at peak") == {"peak", "min"}
    # the figure grows with each phrase written right after it
    fig = next(c for c in numeric_claims("~$110K at peak or more.") if c.kind == "currency")
    assert fig.figure == "~$110K at peak or more"
    # a bare "peak" words away is still nothing, and does not stop a phrase
    # right after it from counting
    assert next(iter(numeric_claims("Led 9 AEs through peak season"))).bounds == frozenset()
    assert next(iter(numeric_claims("Led 9 AEs or more at peak"))).bounds == {"min", "peak"}


def test_peak_bounds_in_the_parser() -> None:
    def bounds(text: str) -> frozenset[str]:
        return next(c for c in numeric_claims(text) if c.kind == "currency").bounds

    assert bounds("peaked at $110K") == {"peak"}
    assert bounds("~$110K at peak") == {"peak"}
    assert bounds("(~$110K peak)") == {"peak"}
    assert bounds("~$110K peaks") == {"peak"}
    assert bounds("up to $110K") == {"max"}
    # a bare "peak" binds only right after the number
    assert next(iter(numeric_claims("Led 9 AEs through peak season"))).bounds == frozenset()
