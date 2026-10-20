"""Three false positives of the claims validator, each seen in a cover letter
rejected before 2026-10-15, and the near-miss fabrications that must still
fail:

(1) a contraction at the start of a sentence ("Let's set up a conversation.")
    failed as an unsourced proper noun "Let": the posting writes
    "✨ Let’s make…", a capital after an emoji, which made "let" one of the
    JD's names;
(2) the target's own name failed unsourced_tool in every sentence of a letter
    to a company whose name is also a dialer of questions.TOPICS ("I'd like
    to join Aircall.");
(3) a rate hyphenated onto its figure ("launched at $12K-a-week") was
    read with no period and failed dropped_period against b_050's
    "~$12K/week".

The candidate is the example persona (Jordan Rivera's employers and
figures); every posting below is invented for these tests, not a company's
own text.

Offline and LLM-free: validate_claims is a pure function."""

from __future__ import annotations

import re
from typing import Any

import pytest

from jobkit.validate.claims import validate_claims
from jobkit.validate.provenance import numeric_claims, text_periods

# the roles and bullets the letters cite (the example persona's)
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
    ],
    "education": [
        {"school": "Lakeside State University", "degree": "B.S.", "years": "2008 – 2012"}
    ],
    "skill_index": {"Salesforce": "hands-on", "Gong": "hands-on"},
    "constraints": {
        "never_claim_titles": ["VP of Sales", "Chief Revenue Officer"],
        "never_claim_phrases": ["treasury management sales"],
    },
    "bullets": [
        {
            "id": "b_052",
            "role_id": "role_haulmark",
            "metric_confidence": "verified",
            "text_long": "Led a team of 9 Account Executives and built the channel-partner "
            "program from 3 to 17 reseller partners in ~18 months.",
            "metric": "9 AEs; 3 → 17 reseller partners in ~18 months",
        },
        {
            "id": "b_050",
            "role_id": "role_haulmark",
            "metric_confidence": "verified",
            "text_long": "Priced and launched a weekend-pickup surcharge with the Customer "
            "Success and Client Services leads, growing surcharge revenue from ~$12K/week at "
            "launch to $30–40K/week by October.",
            "text_short": "Weekend-pickup surcharge: ~$12K/wk at launch → $30–40K/wk by October.",
            "metric": "~$12K/week → $30–40K/week",
        },
        {
            "id": "b_053",
            "role_id": "role_ledgerline",
            "metric_confidence": "verified",
            "text_long": "Stood up a deal desk that reviews every non-standard quote for 30+ "
            "sellers, bringing the average discount down from ~19% to ~12%.",
            "metric": "30+ sellers; average discount ~19% → ~12%",
        },
    ],
}

# an invented posting with the shape of the one the letter's validator read:
# a capital after an emoji or a bullet is no sentence start (_strict_initial)
PIPER_JD = (
    "Pied Piper is hiring a Senior Manager, Revenue Strategy. You will own the revenue "
    "plan and partner with Sales and Finance.\n"
    "We'd love to hear from you—you could be the person we’re looking for.\n"
    "✨ Let’s make file storage feel effortless.\n"
    "• You’ll partner with Sales leadership on pricing.\n"
    "• Don’t tick every box? Apply anyway."
)
# the same posting beside a name that is also a common word, run-initial in
# the JD ("Oracle Cloud Infrastructure"): review 2026-10-10
ORACLE_JD = PIPER_JD + "\nYou will sell into accounts like Oracle Cloud Infrastructure."
# an invented posting for a company whose name is also a dialer of
# questions.TOPICS (any such tool works; the company facts and figures are
# made up for these tests)
AIRCALL_JD = (
    "Aircall makes a cloud phone and dialer for sales and support teams. More than 2,000 "
    "companies in 30 countries take their calls on it. We grew revenue 80% last year, and "
    "the sales team is hiring faster than it can train. As Director of Sales Enablement, you "
    "will support the Aircall revenue team with onboarding, coaching and call reviews."
)
KNOWN = ["Pied Piper", "Aircall", "Wonka", "Oracle", "Orum"]

PIPER = {"target_company": "Pied Piper", "job_title": "Senior Manager, Revenue Strategy"}
AIRCALL = {"target_company": "Aircall", "job_title": "Director of Sales Enablement"}
WONKA = {"target_company": "Wonka", "job_title": "Partner Sales Manager, Wholesale"}


def _check(
    text: str, jd: str, job: dict[str, str], cited: tuple[str, ...] = ("b_052",)
) -> list[dict[str, str]]:
    out = validate_claims(
        text,
        list(cited),
        CORPUS,
        jd_text=jd,
        known_companies=KNOWN,
        estimate_style="confident",  # the style the letters were checked with
        **job,
    )
    assert out.ok == (not out.violations)
    return out.violations


def _kinds(violations: list[dict[str, str]]) -> set[str]:
    return {v["kind"] for v in violations}


def _flagged(violations: list[dict[str, str]], kind: str, word: str) -> bool:
    return any(v["kind"] == kind and re.search(rf"\b{word}", v["detail"]) for v in violations)


# ---------------------------------------------------------------------------
# (1) a contraction at the start of a sentence is never a proper noun
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # both drafts of the letter
        "Let's set up a conversation.",
        "Let's set up the conversation.",
        # the curly apostrophe the posting itself uses
        "Let’s set up a conversation.",
        # other contractions the posting capitalizes after a bullet
        "Don’t hesitate to reach out.",
        "Don't hesitate to reach out.",
        "You’ll get a fast reply.",
        # and ones it never writes
        "Here's the short version.",
        "That's all for now.",
        # after a sentence that starts the letter
        "Thank you for reading. Let's set up a conversation.",
    ],
)
def test_an_initial_contraction_passes(text: str) -> None:
    assert _check(text, PIPER_JD, PIPER) == []


@pytest.mark.parametrize(
    ("text", "jd", "word"),
    [
        # an unknown capitalized name at the start of a sentence
        ("Brightwave hired me.", PIPER_JD, "Brightwave"),
        # …with a possessive or a clitic on it
        ("Brightwave's team hired me.", PIPER_JD, "Brightwave"),
        ("Brightwave'll hire me.", PIPER_JD, "Brightwave"),
        # a common word the JD names, with a possessive or a clitic: the list
        # of contractions is closed
        ("Oracle was one of my accounts.", ORACLE_JD, "Oracle"),
        ("Oracle's team was one of my accounts.", ORACLE_JD, "Oracle"),
        ("Oracle'd be my first call.", ORACLE_JD, "Oracle"),
        ("Oracle’ll be my first call.", ORACLE_JD, "Oracle"),
    ],
)
def test_a_name_at_the_start_of_a_sentence_still_fails(text: str, jd: str, word: str) -> None:
    violations = _check(text, jd, PIPER)
    assert _flagged(violations, "unsourced_proper_noun", word), violations


@pytest.mark.xfail(
    strict=True,
    reason=(
        "known false rejection (review 2026-10-12): a capital after the posting's emoji "
        "('✨ Let’s make…') reads as mid-sentence, so 'Let' is one of the posting's names and "
        "'Let me know what works.' fails unsourced_proper_noun. Not a fabrication: when "
        "_prose_names/_strict_initial is fixed this passes and the xfail must go."
    ),
)
def test_let_me_know_is_an_accepted_false_rejection() -> None:
    assert _check("Let me know what works.", PIPER_JD, PIPER) == []


def test_a_capitalized_contraction_past_the_start_is_still_judged() -> None:
    # only a sentence-initial capital is position: mid-sentence "Brightwave's"
    # is a name wherever it stands
    violations = _check("I joined Brightwave's sales team.", PIPER_JD, PIPER)
    assert _flagged(violations, "unsourced_proper_noun", "Brightwave"), violations


# ---------------------------------------------------------------------------
# (2) the target's own name is not an unsourced tool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # the kinds of sentence that failed: the job sought, a company fact, a
        # reason after an opening phrase, an aspiration
        "I'm applying for the Director of Sales Enablement role at Aircall.",
        "Aircall grew revenue 80% last year.",
        "For the Director of Sales Enablement role at Aircall, the first project would be a "
        "library of recorded calls.",
        "I'd like to join Aircall.",
        # the job sought, an aspiration, a sentence about the company
        "I saw the Aircall posting and wanted to write.",
        "I'd welcome the chance to build the enablement function at Aircall.",
        "I'm excited about what Aircall is building.",
        "Aircall makes a cloud phone and dialer for sales and support teams.",
        "Aircall's AI dialer caught my eye.",
    ],
)
def test_the_target_named_as_the_company_passes(text: str) -> None:
    assert _check(text, AIRCALL_JD, AIRCALL) == []


@pytest.mark.parametrize(
    "text",
    [
        # a tool the candidate claims to have used
        "I used Aircall daily at Haulmark.",
        "At Haulmark my SDRs dialed with Aircall every day.",
        "Haulmark ran its outbound floor on Aircall.",
        "Aircall was my dialer.",
        "Aircall has been my daily dialer.",
        "Aircall powered the floor I ran.",
        "Aircall is the dialer I know best.",
        "Aircall is the dialer my old team lived in.",
        "I'd love to keep using Aircall.",
        # a job sought or an aspiration with a claim in it
        "I'm applying for the role at Aircall, whose dialer I used at Haulmark.",
        "I'm applying for the role at Aircall because I ran Aircall at Haulmark.",
        # no first person, but history, a pointer or a person brought in
        "Aircall was the team's dialer for years.",
        "Aircall has been the floor's daily dialer.",
        "Aircall is what the team there dialed on.",
        "Aircall gets a leader who has run Aircall floors.",
        "Aircall would get a leader fluent in Aircall.",
        "Aircall still speaks to me.",
        "Aircall caught my eye years ago when I first dialed with it.",
        # the target's people in its product: not opened with the target
        "Your reps live in Aircall.",
    ],
)
def test_the_target_as_a_tool_used_still_fails(text: str) -> None:
    violations = _check(text, AIRCALL_JD, AIRCALL)
    assert _flagged(violations, "unsourced_tool", "Aircall"), violations


def test_the_target_tool_message_says_how_to_name_the_company() -> None:
    violations = _check("I used Aircall daily at Haulmark.", AIRCALL_JD, AIRCALL)
    detail = next(v["detail"] for v in violations if v["kind"] == "unsourced_tool")
    assert "company applied to" in detail and "the role at Aircall" in detail


def test_another_tool_beside_the_target_still_fails() -> None:
    violations = _check(
        "I'd like to join Aircall and bring Orum's dialing playbook.", AIRCALL_JD, AIRCALL
    )
    assert _flagged(violations, "unsourced_tool", "Orum"), violations
    assert not _flagged(violations, "unsourced_tool", "Aircall"), violations


@pytest.mark.parametrize(
    ("text", "job"),
    [
        # Aircall is not the target: any mention is a tool, as before
        ("I'd like to join Wonka and roll out Aircall.", WONKA),
        ("Wonka's reps live in Aircall.", WONKA),
    ],
)
def test_a_tool_that_is_not_the_target_still_fails(text: str, job: dict[str, str]) -> None:
    violations = _check(text, AIRCALL_JD, job)
    assert _flagged(violations, "unsourced_tool", "Aircall"), violations


@pytest.mark.parametrize(
    ("target", "text"),
    [
        # the target's name less a legal or domain suffix is the tool's name
        ("Aircall, Inc.", "I'd like to join Aircall, Inc."),
        ("Dialpad.ai", "I'm applying for the Enterprise Sales Manager role at Dialpad.ai."),
    ],
)
def test_a_suffixed_target_name_is_the_tool(target: str, text: str) -> None:
    corpus = {**CORPUS, "skill_index": {"Salesforce": "hands-on"}}
    out = validate_claims(
        text,
        ["b_052"],
        corpus,
        target_company=target,
        job_title="Enterprise Sales Manager",
        known_companies=KNOWN,
        estimate_style="confident",
    )
    assert not any(v["kind"] == "unsourced_tool" for v in out.violations), out.violations


def test_a_longer_target_name_is_no_tool() -> None:
    # "Apollo Global Management" is not the Apollo prospecting tool: its first
    # word stays a tool the candidate must have a skill_index key for
    out = validate_claims(
        "I'd like to join Apollo.",
        ["b_052"],
        CORPUS,
        target_company="Apollo Global Management",
        job_title="Enterprise Sales Manager",
        known_companies=KNOWN,
        estimate_style="confident",
    )
    assert any(v["kind"] == "unsourced_tool" for v in out.violations), out.violations


def test_the_target_in_a_tenure_claim_fails_twice() -> None:
    # a tenure cue fails the sentence as fabricated_employer, and the tool
    # stays unsourced: the exception never runs beside a tenure claim
    violations = _check("I previously worked at Aircall.", AIRCALL_JD, AIRCALL)
    assert "fabricated_employer" in _kinds(violations), violations
    assert _flagged(violations, "unsourced_tool", "Aircall"), violations


@pytest.mark.parametrize(
    "text",
    [
        # the next sentence hands the target's product to the candidate
        "I'd like to join Aircall. I used it daily at Haulmark.",
        "Aircall's AI dialer caught my eye. My team lived in it at Haulmark.",
        "I'm applying for the Director of Sales Enablement role at Aircall. I rolled it out to 9 "
        "AEs.",
        "Aircall makes a cloud phone and dialer for sales and support teams. I've used their "
        "dialer for years.",
    ],
)
def test_a_pronoun_handing_the_target_to_the_candidate_fails(text: str) -> None:
    violations = _check(text, AIRCALL_JD, AIRCALL)
    assert _flagged(violations, "unsourced_tool", "Aircall"), violations
    detail = next(v["detail"] for v in violations if v["kind"] == "unsourced_tool")
    assert "pointing back" in detail


@pytest.mark.parametrize(
    "text",
    [
        # the next sentence only aspires, or has no pronoun that points back
        "I'd like to join Aircall. I'd love to help build it.",
        "Aircall makes a cloud phone and dialer for sales and support teams. I'd like to join "
        "Aircall.",
        "I'm applying for the Director of Sales Enablement role at Aircall. At Haulmark I led 9 "
        "Account Executives.",
    ],
)
def test_a_next_sentence_that_points_nowhere_passes(text: str) -> None:
    assert _check(text, AIRCALL_JD, AIRCALL) == []


# second review of 2026-10-15: each of these passed with no violation at all
# when only the next sentence, and only a pronoun beside a first person, was
# checked. Any sentence of the text that may point back at the product in a
# claim about the candidate or the candidate's employers withdraws the excuse.
@pytest.mark.parametrize(
    "text",
    [
        # a product noun, not a pronoun
        "I'd like to join Aircall. I used the product daily at Haulmark.",
        "I'd like to join Aircall. I used the dialer daily at Haulmark.",
        "I'm excited about what Aircall is building. That product was my team's daily dialer at "
        "Haulmark.",
        "I'd like to join Aircall. Every rep I managed at Haulmark used the platform daily.",
        "I'd like to join Aircall. My Haulmark reps lived in that dialer.",
        "I'd like to join Aircall. That product ran our Haulmark floor.",
        "I'd like to join Aircall. My team knows this product well.",
        "I'd like to join Aircall. I know the product well.",
        "I'd like to join Aircall. I know your product well.",
        "I'd like to join Aircall. Every rep I managed used an AI dialer.",
        "I'd like to join Aircall. I rolled out the software to 9 AEs.",
        "I'd like to join Aircall. Our tools at Haulmark included it.",
        "I'd like to join Aircall. My reps were power users.",
        # a pronoun with the candidate's employer, not a first person
        "I'd like to join Aircall. It ran the Haulmark outbound floor.",
        "I'd like to join Aircall. Haulmark ran its outbound floor on it.",
        "I'd like to join Aircall. Haulmark ran its floor on it.",
        "I'd like to join Aircall. Haulmark's floor dialed with them.",
        "I'm applying for the role at Aircall. The Haulmark floor ran on it.",
        "I'd like to join Aircall. The Haulmark floor dialed on it every day.",
        "I'm applying for the role at Aircall. Haulmark reps lived in the product.",
        "I'd like to join Aircall. Haulmark was one of your customers.",
        # …a fact only a cited bullet states, the candidate's name, or "there"
        "I'd like to join Aircall. It ran a floor of 9 Account Executives.",
        "I'd like to join Aircall. Test Candidate lived in it for years.",
        "I'd like to join Aircall. The team there lived in it every day.",
        # the pointer past the next sentence, or before the mention
        "I'd like to join Aircall. Thanks. I used it daily at Haulmark.",
        "I'd like to join Aircall. At Haulmark I led a team of 9 Account Executives. We "
        "used it daily.",
        "I'd like to join Aircall. At Haulmark I led 9 Account Executives. That team lived in it.",
        "I used it daily at Haulmark. I'd like to join Aircall.",
        # in the mention's own sentence, or more than an aspiration
        "I'd like to join Aircall because I used it at Haulmark.",
        "I'd like to join Aircall. I'd love to keep using it.",
        # third review of 2026-10-15: a demonstrative with no noun after it,
        # or "the same thing" (each passed with no violation)
        "I'd like to join Aircall. My reps dialed on this every day.",
        "I'd like to join Aircall. I've dialed on this daily.",
        "I'd like to join Aircall. Every rep I managed dialed on that.",
        "I'd like to join Aircall. My floor ran on the same thing.",
        # …and its neighbors: a particle, an adverb, a contraction, a pronoun
        # subject before a verb, "the same" with no noun, a stand-in noun
        "I'd like to join Aircall. I rolled this out to 9 AEs.",
        "I'd like to join Aircall. I've seen that firsthand at Haulmark.",
        "I'd like to join Aircall. That's what my reps ran at Haulmark.",
        "I'd like to join Aircall. This was our backbone at Haulmark.",
        "I'd like to join Aircall. That ran our Haulmark floor.",
        "I'd like to join Aircall. That powered our floor at Haulmark.",
        "I'd like to join Aircall. These were my reps' daily drivers.",
        "I'd like to join Aircall. My team did the same at Haulmark.",
        "I'd like to join Aircall. My reps loved that one.",
    ],
)
def test_a_pointer_anywhere_handing_the_target_to_the_candidate_fails(text: str) -> None:
    violations = _check(text, AIRCALL_JD, AIRCALL)
    assert _flagged(violations, "unsourced_tool", "Aircall"), violations


# third review of 2026-10-15: a plain word of buying or holding the product,
# with no "your" and no adjective, passed with no violation ("I was an early
# customer." alone was caught). Any of them, bare or not, is a pointer.
@pytest.mark.parametrize(
    "text",
    [
        "I'd like to join Aircall. At Haulmark we were a customer.",
        "I'd like to join Aircall. I was a customer.",
        "I'd like to join Aircall. At Haulmark my reps were customers.",
        "I'd like to join Aircall. I bought seats for my whole Haulmark floor.",
        "I'd like to join Aircall. I championed the purchase.",
        # unchanged
        "I'd like to join Aircall. I was an early customer.",
        # the rest of the list
        "I'd like to join Aircall. At Haulmark we were a client for two years.",
        "I'd like to join Aircall. My reps were all users.",
        "I'd like to join Aircall. We held 9 licenses at Haulmark.",
        "I'd like to join Aircall. We subscribed at Haulmark.",
        "I'd like to join Aircall. I negotiated our contract.",
        "I'd like to join Aircall. I owned the renewal at Haulmark.",
        "I'd like to join Aircall. Haulmark was a subscriber.",
    ],
)
def test_a_word_of_buying_hands_the_target_to_the_candidate(text: str) -> None:
    violations = _check(text, AIRCALL_JD, AIRCALL)
    assert _flagged(violations, "unsourced_tool", "Aircall"), violations
    detail = next(v["detail"] for v in violations if v["kind"] == "unsourced_tool")
    assert "pointing back" in detail


@pytest.mark.parametrize(
    "text",
    [
        # a pointer in a sentence that only aspires, or about no one's history
        "I'd like to join Aircall and help it grow.",
        "I'd like to join Aircall. I look forward to discussing it.",
        "I'd like to join Aircall. I'd welcome the chance to talk about it.",
        "I'd like to join Aircall. I'd love to grow with the team there.",
        "I'd like to join Aircall. Hiring at that speed outruns training. I want to fix it.",
        "I'd like to join Aircall. Enablement sticks when reps use it every day.",
        "I'd like to join Aircall. There's a lot to build.",
        # a product word that modifies another noun
        "I'd like to join Aircall. I'd love to talk with the product team.",
        # "IT" the department is no pronoun
        "I'd like to join Aircall. At Haulmark I led IT rollouts for 9 Account Executives.",
        # the target's people, a first person that only aspires
        "I'm applying for the Director of Sales Enablement role at Aircall. I want to help your "
        "reps ramp faster.",
        # a pointer with no first person, employer or cited fact
        "I'm applying for the Director of Sales Enablement role at Aircall. Hiring that fast "
        "wears on a sales floor before anyone sees it.",
        # a demonstrative before a noun, or a "that" before a subject
        "I'd like to join Aircall. I remember that day at Haulmark.",
        "I'd like to join Aircall. I made sure that every rep at Haulmark ramped.",
        "I'd like to join Aircall. At Haulmark I learned that reps need coaching.",
        # a word of buying in a hyphen compound, or in a name the corpus
        # writes (b_050's "Customer Success" and "Client Services")
        "I'd like to join Aircall. At Haulmark I led customer-facing teams.",
        "I'd like to join Aircall. At Haulmark I sold subscription-based plans alongside "
        "Customer Success.",
        "I'd like to join Aircall. At Haulmark I partnered with Client Services.",
        # a word of buying with no first person, employer or cited fact
        "I'd like to join Aircall. Customers are the point of enablement.",
    ],
)
def test_a_pointer_outside_a_claim_about_the_candidate_passes(text: str) -> None:
    violations = _check(text, AIRCALL_JD, AIRCALL)
    assert not any(v["kind"] == "unsourced_tool" for v in violations), violations


def test_the_letters_sentences_pass_together() -> None:
    # a letter built from sentences of the kinds that failed before the fix (a
    # company fact, the job sought, a team held, a reason, an aspiration), as
    # one text: no sentence points back at the product in a claim about the
    # candidate
    letter = (
        "Aircall grew revenue 80% last year. The Director of Sales Enablement role at Aircall "
        "caught my eye. At Haulmark I led a team of 9 Account Executives. That matters because "
        "a new rep learns more from a recorded call than from a slide deck. I'd welcome the "
        "chance to build the enablement function at Aircall."
    )
    assert _check(letter, AIRCALL_JD, AIRCALL) == []


def test_the_handoff_message_names_the_pointing_sentence() -> None:
    violations = _check(
        "I'd like to join Aircall. Thanks. Haulmark reps lived in the product.", AIRCALL_JD, AIRCALL
    )
    tool = [v for v in violations if v["kind"] == "unsourced_tool"]
    assert len(tool) == 1, violations
    assert "pointing back" in tool[0]["detail"]
    assert "Haulmark reps lived in the product." in tool[0]["detail"]
    assert "'the product'" in tool[0]["detail"]
    assert tool[0]["sentence"] == "I'd like to join Aircall. Haulmark reps lived in the product."


def test_a_pointer_withdraws_nothing_for_a_target_that_is_no_tool() -> None:
    # the excuse is the target-as-tool's only: a letter to Wonka that says
    # "it" in a claim about the candidate is judged as before
    violations = _check("I'd like to join Wonka. I used it daily at Haulmark.", AIRCALL_JD, WONKA)
    assert not any(v["kind"] == "unsourced_tool" for v in violations), violations


def _answer(text: str, question: str) -> list[dict[str, str]]:
    out = validate_claims(
        text,
        ["b_052"],
        CORPUS,
        jd_text=AIRCALL_JD,
        known_companies=KNOWN,
        estimate_style="confident",
        question=question,
        **AIRCALL,
    )
    return out.violations


@pytest.mark.parametrize(
    ("text", "question"),
    [
        # the question names the tool: any mention of it answers the question
        ("I'd like to join Aircall.", "Have you used Aircall before?"),
        (
            "Aircall makes a cloud phone and dialer for sales and support teams.",
            "Do you know Aircall?",
        ),
        # the question asks about tools, use or experience
        (
            "Aircall makes a cloud phone and dialer for sales and support teams.",
            "Which sales tools are you proficient in?",
        ),
        ("I'd like to join Aircall.", "What dialers have you worked with?"),
    ],
)
def test_a_drafted_answer_to_a_tool_question_gets_no_exception(text: str, question: str) -> None:
    violations = _answer(text, question)
    assert _flagged(violations, "unsourced_tool", "Aircall"), violations


def test_a_drafted_answer_to_another_question_names_the_company() -> None:
    assert _answer("I'd like to join Aircall.", "Why do you want to join our team?") == []


@pytest.mark.parametrize(
    "question",
    [
        "Why do you want to join our team?",
        "What's your outbound setup?",
        "What did your reps dial with?",
    ],
)
def test_a_drafted_answer_names_the_job_sought(question: str) -> None:
    text = "I'm applying for the Director of Sales Enablement role at Aircall."
    assert not _flagged(_answer(text, question), "unsourced_tool", "Aircall")


# third review of 2026-10-15: a sentence that opens with the target's name
# answers the question, whatever its words. Each passed with no violation when
# only a question with a word of tools, use or experience lost the exception.
@pytest.mark.parametrize(
    ("text", "question"),
    [
        ("Aircall, end to end.", "What's your outbound setup?"),
        ("Aircall is what modern floors run on.", "What did your last team run outbound on?"),
        ("Aircall is the dialer modern floors run on.", "What did your reps dial with?"),
        ("Aircall is how modern floors connect.", "How did your SDR team connect with prospects?"),
        ("Aircall is the best on the market.", "Have you been a customer?"),
        # a present reaction is no aspiration either
        ("Aircall's AI dialer caught my eye.", "What did your reps dial with?"),
        # accepted false rejection: a question about the company itself
        (
            "Aircall makes a cloud phone and dialer for sales and support teams.",
            "Why do you want to join our team?",
        ),
        # any non-empty question marks a drafted answer (callers pass
        # "<label>\n<question>")
        ("Aircall is the best on the market.", "\n"),
    ],
)
def test_a_drafted_answer_never_opens_with_the_target(text: str, question: str) -> None:
    violations = _answer(text, question)
    assert _flagged(violations, "unsourced_tool", "Aircall"), violations
    detail = next(v["detail"] for v in violations if v["kind"] == "unsourced_tool")
    # the advice leaves out the letter-only form
    assert "the role at Aircall" in detail and "opens with its name" not in detail


def test_the_same_sentence_in_a_letter_still_passes() -> None:
    # a letter keeps the third form: the target plainly its subject
    assert (
        _check(
            "Aircall makes a cloud phone and dialer for sales and support teams.",
            AIRCALL_JD,
            AIRCALL,
        )
        == []
    )


# ---------------------------------------------------------------------------
# (3) a rate hyphenated onto its figure keeps its period
# ---------------------------------------------------------------------------

PROGRAM = "At Haulmark, the weekend-pickup surcharge launched at {}."


@pytest.mark.parametrize(
    "figure",
    [
        # the letter's own form first
        "$12K-a-week",
        "$12K-per-week",
        "$12K/week",
        "$12K/wk",
        "$12K a week",
        "$12K per week",
        "~$12K-a-week",
    ],
)
def test_a_hyphenated_rate_keeps_its_period(figure: str) -> None:
    assert _check(PROGRAM.format(figure), PIPER_JD, PIPER, ("b_050",)) == []


@pytest.mark.parametrize(
    ("figure", "kind"),
    [
        # no period at all
        ("$12K", "dropped_period"),
        # a hyphen after a word is no figure's period
        ("$12K once-a-week", "dropped_period"),
        # the wrong period
        ("$12K-a-year", "changed_period"),
        ("$12K-per-month", "changed_period"),
        # a changed value
        ("$15K-a-week", "fabricated_number"),
        ("$2M-a-week", "fabricated_number"),
    ],
)
def test_a_rate_still_keeps_its_value_and_period(figure: str, kind: str) -> None:
    violations = _check(PROGRAM.format(figure), PIPER_JD, PIPER, ("b_050",))
    assert kind in _kinds(violations), violations


def test_hyphenated_period_parse() -> None:
    def p(text: str) -> list[tuple[str, str]]:
        return [(c.text, c.period) for c in numeric_claims(text)]

    assert p("launched at $12K-a-week") == [("$12K", "week")]
    assert p("$3M-per-year in pipeline") == [("$3M", "year")]
    assert p("20-a-day dials") == [("20", "day")]
    assert p("5%-a-month churn") == [("5%", "month")]
    assert p("$1.2MM-a-year") == [("$1.2MM", "year")]
    # one period across a move, when only one end writes it
    assert p("from $12K-a-week to $40K") == [("$12K", "week"), ("$40K", "week")]
    # a hyphen after a word is no figure's period, and states none
    assert p("$12K with a once-a-week review") == [("$12K", "")]
    assert text_periods("a once-a-month review, a one-a-day habit") == frozenset()
    assert text_periods("$12K-a-week") == frozenset({"week"})


# ---------------------------------------------------------------------------
# review 2026-10-12, round 3: target_tool_ok
# (blocker) the aspiration was read on the whole sentence, so any aspiring
# clause excused every mention of the target-as-tool in it, a tool-use
# claim included; now each mention must itself be the aspiration's object,
# no usage word may govern it, and the sentence may state none of the
# candidate's own facts. (major) a sentence opening with the target could
# credit the candidate's cited facts to the target's product.
# ---------------------------------------------------------------------------

SEISMIC = {"target_company": "Seismic", "job_title": "Director of Sales Enablement"}
HIGHSPOT = {"target_company": "Highspot", "job_title": "Director of Sales Enablement"}
CITED = ("b_052", "b_050")


@pytest.mark.parametrize(
    ("text", "job"),
    [
        # every one returned NO violations before the fix
        ("I'd like to join your team after using Aircall at Haulmark.", AIRCALL),
        ("I'd like to join Aircall after using Aircall at Haulmark.", AIRCALL),
        ("I'm applying for the role at Aircall after using Aircall at Haulmark.", AIRCALL),
        (
            "I'm applying for the role at Aircall after rolling Aircall out to 9 AEs at Haulmark.",
            AIRCALL,
        ),
        ("I hope to join Aircall after rolling Aircall out to 9 AEs.", AIRCALL),
        (
            "I'm applying for the role at Seismic after running Seismic sequences at Haulmark.",
            SEISMIC,
        ),
        (
            "I'm applying for the role at Highspot after running Highspot sequences at Haulmark.",
            HIGHSPOT,
        ),
        ("I'd love to join Aircall after three years on Aircall at Haulmark.", AIRCALL),
        # a usage word or a product word beside the one mention
        ("I'd love to join a team that lives in Aircall.", AIRCALL),
        ("I'd be excited to keep dialing with Aircall.", AIRCALL),
        ("I hope to bring my Aircall playbooks to your team.", AIRCALL),
        ("I'd like to join Aircall and run Aircall sequences.", AIRCALL),
        # an aspiration beside the candidate's own cited facts
        ("I'd like to join Aircall, where leading 9 AEs would carry over.", AIRCALL),
        ("I want to work at Aircall because Haulmark taught me enablement.", AIRCALL),
        # a mention the aspiration does not govern
        ("Aircall would be my next dialer, and I'd like to join you.", AIRCALL),
    ],
)
def test_an_aspiration_excuses_only_its_own_object(text: str, job: dict[str, str]) -> None:
    violations = _check(text, AIRCALL_JD, job, CITED)
    word = job["target_company"]
    assert _flagged(violations, "unsourced_tool", word), violations


@pytest.mark.parametrize(
    ("text", "job"),
    [
        # every one returned NO violations before the fix
        ("Aircall is how 9 AEs hit quota.", AIRCALL),
        ("Aircall is how the team hit $30–40K/week.", AIRCALL),
        ("Aircall powers the floor behind $30–40K/week in surcharge revenue.", AIRCALL),
        ("Aircall drives the weekend-pickup surcharge's $30–40K/week.", AIRCALL),
        ("Seismic is how 9 AEs hit quota.", SEISMIC),
        ("Highspot is how 9 AEs hit quota.", HIGHSPOT),
        # a number the posting does not state
        ("Aircall doubles connect rates for 40 reps.", AIRCALL),
        # past tenses spelled like the present (review 2026-10-12, minor)
        ("Aircall cut ramp time for new reps.", AIRCALL),
        ("Aircall put the floor on autopilot.", AIRCALL),
    ],
)
def test_a_sentence_about_the_target_never_takes_the_candidates_facts(
    text: str, job: dict[str, str]
) -> None:
    violations = _check(text, AIRCALL_JD, job, CITED)
    assert _flagged(violations, "unsourced_tool", job["target_company"]), violations


# review 2026-10-15, round 3: a sentence about the target that credits it with
# what the sentence before states. Each two-sentence text returned NO
# violations: employer_fact and the number checks read the target's sentence
# alone, and "why", "the reason", "the credit" point past it.
CANDIDATE_FACTS = (
    "At Haulmark I grew surcharge revenue from ~$12K/week to $30–40K/week.",
    "At Haulmark I led a team of 9 Account Executives.",
)
CREDIT_TAILS = (
    ("Aircall is a big part of why.", AIRCALL),
    ("Aircall is the reason.", AIRCALL),
    ("Aircall is why.", AIRCALL),
    ("Aircall deserves the credit.", AIRCALL),
    ("Aircall gets the credit.", AIRCALL),
    ("Aircall is the secret.", AIRCALL),
    ("Aircall is how.", AIRCALL),
    ("Aircall is a large part of the story.", AIRCALL),
    ("Aircall is the reason behind it.", AIRCALL),
    ("Aircall is who I have to thank.", AIRCALL),
    ("Aircall is the one I owe it to.", AIRCALL),
    ("Aircall is a big part of that.", AIRCALL),
    ("Aircall made it possible.", AIRCALL),
    ("Aircall made that happen.", AIRCALL),
    ("Seismic is a big part of why.", SEISMIC),
    ("Seismic is the reason.", SEISMIC),
    ("Highspot deserves much of the credit.", HIGHSPOT),
    ("Highspot is the secret.", HIGHSPOT),
    # review 2026-10-15, round 4: credit worded with no word on the list.
    # Each returned NO violations after either fact: _own_predicate took any
    # two content words with none of the listed credit words, and "those
    # gains" / "that kind of growth" point back at the fact
    ("Aircall is the source of those gains.", AIRCALL),
    ("Aircall powers that kind of growth.", AIRCALL),
    ("Aircall is the real catalyst.", AIRCALL),
    ("Aircall is the driving force.", AIRCALL),
    ("Aircall is the deciding factor.", AIRCALL),
    ("Aircall is the key ingredient.", AIRCALL),
    ("Aircall is the magic ingredient.", AIRCALL),
    ("Aircall is the missing piece.", AIRCALL),
    ("Aircall is the hidden engine.", AIRCALL),
    ("Aircall is the engine underneath.", AIRCALL),
    ("Aircall is the unsung hero.", AIRCALL),
    ("Aircall earns the applause.", AIRCALL),
    ("Aircall earns all the kudos.", AIRCALL),
    ("Aircall is the common thread.", AIRCALL),
    ("Aircall is the biggest lever.", AIRCALL),
    ("Seismic is the real catalyst.", SEISMIC),
    ("Highspot is the driving force.", HIGHSPOT),
    # …and with a word of credit not on the list, or a pointer with a noun
    # after it ("that kind of", "such", "like that"). "Aircall played a big
    # role" read as naming the job sought ("role" after the name:
    # _names_seat_sought)
    *(
        (sentence.replace("Aircall", name), job)
        for sentence in (
            "Aircall played a big role.",
            "Aircall plays a big role in growth like that.",
            "Aircall is a major factor.",
            "Aircall contributes to growth of that kind.",
            "Aircall fuels that kind of growth.",
            "Aircall fuels such growth.",
            "Aircall is the engine of that kind of growth.",
        )
        for name, job in (("Aircall", AIRCALL), ("Seismic", SEISMIC))
    ),
    # …and their neighbours: a job-sought noun after a verb or a determiner,
    # or with a word of credit after it
    ("Aircall created the opportunity.", AIRCALL),
    ("Aircall shaped the process.", AIRCALL),
    ("Aircall has a role.", AIRCALL),
    ("Aircall plays roles in growth like that.", AIRCALL),
    ("Aircall's role in growth like that is clear.", AIRCALL),
    ("Aircall's big role in that growth is clear.", AIRCALL),
    ("Aircall supports growth like this.", AIRCALL),
    ("Aircall helps here.", AIRCALL),
)


@pytest.mark.parametrize("fact", CANDIDATE_FACTS)
@pytest.mark.parametrize(("tail", "job"), CREDIT_TAILS)
def test_the_next_sentence_never_credits_the_target_with_the_candidates_facts(
    fact: str, tail: str, job: dict[str, str]
) -> None:
    violations = _check(f"{fact} {tail}", AIRCALL_JD, job, CITED)
    assert _flagged(violations, "unsourced_tool", job["target_company"]), violations
    # the target's own sentence, not the candidate's, is the one that fails
    assert any(v["kind"] == "unsourced_tool" and tail in v["sentence"] for v in violations)


@pytest.mark.parametrize(("tail", "job"), CREDIT_TAILS)
def test_a_word_of_credit_fails_with_no_sentence_before_it(tail: str, job: dict[str, str]) -> None:
    # fail closed: the sentence never says what it credits, so it fails alone
    violations = _check(tail, AIRCALL_JD, job, CITED)
    assert _flagged(violations, "unsourced_tool", job["target_company"]), violations


@pytest.mark.parametrize("fact", CANDIDATE_FACTS)
@pytest.mark.parametrize(
    "tail",
    [
        # the company applied to, after the candidate's facts
        "I'd like to join Aircall.",
        "I'm applying for the Director of Sales Enablement role at Aircall.",
        "Aircall grew revenue 80% last year.",
        "Aircall makes a cloud phone and dialer for sales and support teams.",
        "Aircall's AI dialer caught my eye.",
        "Aircall caught my eye.",
        "Aircall's mission resonates with me.",
        "I'd welcome the chance to build the enablement function at Aircall.",
        # round 4: a reaction and nothing else, the job sought, the posting's words
        "Aircall's AI dialer immediately caught my eye.",
        "Aircall's approach to enablement resonates with me.",
        "Aircall's Director of Sales Enablement role caught my eye.",
        "Aircall's open role caught my eye.",
        "Aircall's posting caught my eye.",
        "Aircall supports the revenue team.",
    ],
)
def test_the_target_named_as_the_company_after_the_candidates_facts_passes(
    fact: str, tail: str
) -> None:
    assert _check(f"{fact} {tail}", AIRCALL_JD, AIRCALL, CITED) == []


@pytest.mark.parametrize(
    "text",
    [
        # the real cases still pass, with both bullets cited
        "I'd like to join Aircall.",
        "I'm applying for the Director of Sales Enablement role at Aircall.",
        "Aircall grew revenue 80% last year.",
        "I saw the Aircall posting and wanted to write.",
        "I'd welcome the chance to build the enablement function at Aircall.",
        "I'm excited about what Aircall is building.",
        "Aircall makes a cloud phone and dialer for sales and support teams.",
        "Aircall's AI dialer caught my eye.",
        # and other ways to name the company as the one applied to
        "Aircall's dialer caught my eye.",  # the company's product: a possessive
        "I'd love to work at Aircall.",
        "I'm interested in Aircall.",
        "I hope to be part of Aircall as it grows.",
    ],
)
def test_the_target_named_as_the_company_still_passes(text: str) -> None:
    assert _check(text, AIRCALL_JD, AIRCALL, CITED) == []


def test_the_letter_still_passes_with_both_bullets_cited() -> None:
    letter = (
        "Aircall grew revenue 80% last year. The Director of Sales Enablement role at Aircall "
        "caught my eye. At Haulmark I led a team of 9 Account Executives. That matters because "
        "a new rep learns more from a recorded call than from a slide deck. I'd welcome the "
        "chance to build the enablement function at Aircall."
    )
    assert _check(letter, AIRCALL_JD, AIRCALL, CITED) == []


# ---------------------------------------------------------------------------
# review 2026-10-12, round 4: handoffs() and the aspiration
# (blocker) handoffs() excused every pointer in a sentence that aspired
# anywhere, so a phrase about the candidate beside the aspiration handed the
# target's product over: each of the first eight returned NO violations. A
# phrase whose head is a word of holding, or that has a pronoun as its
# preposition's object, now makes the sentence no aspiration (_described_claim,
# _aspires), and each other pointer in an aspiring sentence is judged on its
# own (_pointer_claims): a usage word governing it, or a place inside such a
# phrase that is not its head.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "job"),
    [
        # the reviewer's repros
        ("I'd love to join Aircall as one of its earliest customers.", AIRCALL),
        ("I'd love to join Aircall as one of its longtime users.", AIRCALL),
        ("I'd like to join Aircall, a power user of it since 2022.", AIRCALL),
        ("I'd love to join Aircall, its customer since 2022.", AIRCALL),
        ("I'd love to join Aircall, a longtime admin of the platform.", AIRCALL),
        ("I'd like to join Seismic, a power user of it since 2022.", SEISMIC),
        ("I'd like to join Highspot, a power user of it since 2022.", HIGHSPOT),
        # …and their neighbors: other heads, before the subject, after a dash,
        # a semicolon or a parenthesis, after "being", a customer turned
        # candidate, a fragment of its own
        ("I'd love to join Aircall as one of its very first customers.", AIRCALL),
        ("I'd love to join Aircall as its most loyal customer.", AIRCALL),
        ("I'd love to join Aircall as one of the platform's earliest users.", AIRCALL),
        ("I'd love to join Aircall, an early adopter.", AIRCALL),
        ("I'd love to join Aircall, its biggest fan.", AIRCALL),
        ("I'd love to join Aircall, a happy customer.", AIRCALL),
        ("A longtime customer, I'd love to join Aircall.", AIRCALL),
        ("Customer since 2022, I'd love to join Aircall.", AIRCALL),
        ("Long a customer, I'd love to join Aircall.", AIRCALL),
        ("I'd love to join Aircall — longtime customer here.", AIRCALL),
        ("I'd love to join Aircall (a customer since 2022).", AIRCALL),
        ("I'd love to join Aircall; customer since 2022.", AIRCALL),
        ("I'd love to join Aircall after being one of its customers.", AIRCALL),
        ("I'd love to join Aircall after being a customer.", AIRCALL),
        ("I'd love to join Aircall, a customer-turned-applicant.", AIRCALL),
        ("I'd love to go from customer to colleague at Aircall.", AIRCALL),
        ("I'd love to join Aircall. Customer since 2022.", AIRCALL),
        ("I'd love to join Aircall. Longtime customer, first-time applicant.", AIRCALL),
        # a pronoun or a product word that a usage word governs, or inside a
        # phrase about the subject without being its head
        ("I'd love to join Aircall after using it.", AIRCALL),
        ("I'd like to join Aircall after using it daily.", AIRCALL),
        ("I'd love to join Aircall after a long run with it.", AIRCALL),
        ("I'd love to join Aircall (on it since 2022).", AIRCALL),
        ("I'd love to join Aircall, fluent in the platform.", AIRCALL),
        ("I'd love to join Aircall as an advocate for the platform.", AIRCALL),
        ("I'd love to join Aircall as a platform expert.", AIRCALL),
    ],
)
def test_a_phrase_about_the_candidate_beside_an_aspiration_fails(
    text: str, job: dict[str, str]
) -> None:
    violations = _check(text, AIRCALL_JD, job, CITED)
    assert _flagged(violations, "unsourced_tool", job["target_company"]), violations


def test_the_same_claim_in_its_own_sentence_fails_as_before() -> None:
    violations = _check(
        "I'd love to join Aircall. I was one of its earliest customers.", AIRCALL_JD, AIRCALL, CITED
    )
    assert _flagged(violations, "unsourced_tool", "Aircall"), violations


def test_a_pointer_a_usage_word_governs_is_named_in_the_reason() -> None:
    violations = _check("I'd love to join Aircall after using it.", AIRCALL_JD, AIRCALL, CITED)
    detail = next(v["detail"] for v in violations if v["kind"] == "unsourced_tool")
    assert "pointing back" in detail and "'it'" in detail, detail


@pytest.mark.parametrize(
    ("text", "phrase"),
    [
        # a target that is no tool: the same phrase makes the sentence no
        # aspiration, so "join Wonka" is a seat claimed (it passed before)
        ("I'd love to join Wonka as one of its earliest customers.", "as one of its earliest"),
        ("I'd love to join Wonka, its customer since 2022.", "its customer since 2022"),
        ("I'd love to join Wonka, a power user of it since 2022.", "a power user of it"),
        ("I'd love to join Wonka after being one of its customers.", "being one of its"),
        ("A longtime customer, I'd love to join Wonka.", "A longtime customer"),
        ("I'd love to go from customer to colleague at Wonka.", "from customer to colleague"),
    ],
)
def test_a_target_that_is_no_tool_is_never_held_beside_an_aspiration(
    text: str, phrase: str
) -> None:
    violations = _check(text, AIRCALL_JD, WONKA, CITED)
    detail = next((v["detail"] for v in violations if v["kind"] == "fabricated_employer"), "")
    assert phrase in detail, violations


@pytest.mark.parametrize(
    ("text", "job"),
    [
        # the real cases, and the target or the seat sought named in an
        # aspiration: no phrase about the candidate, no usage word
        ("I'd like to join Aircall.", AIRCALL),
        ("I'd like to join Aircall and help it grow.", AIRCALL),
        ("I hope to be part of Aircall as it grows.", AIRCALL),
        ("I'd love to join Aircall as it scales.", AIRCALL),
        ("I'd love to join Aircall while it scales.", AIRCALL),
        ("I'd love to join Aircall as its next Director of Sales Enablement.", AIRCALL),
        ("I'd love to join Aircall as one of its first enablement hires.", AIRCALL),
        ("I'd like to join Aircall and help scale its revenue team.", AIRCALL),
        ("I'd like to join Aircall and help its customers ramp.", AIRCALL),
        ("I'd love to join Aircall, where I could help it grow.", AIRCALL),
        ("I'd like to join Aircall as soon as possible.", AIRCALL),
        # an appositive that names the target, or its own head the product
        ("I'd love to join Aircall, a team building the dialer every floor needs.", AIRCALL),
        ("I'd love to join Aircall, the dialer modern floors run on.", AIRCALL),
        ("I'd love to join Aircall as its platform matures.", AIRCALL),
        # a pointer in another sentence that only aspires, or names no one
        ("I'd like to join Aircall. I'd love to be part of it.", AIRCALL),
        ("I'd like to join Aircall. One of my goals is to help it grow.", AIRCALL),
        ("I'd like to join Aircall. Happy customers renew.", AIRCALL),
        # a target that is no tool
        ("I'd like to join Wonka.", WONKA),
        ("I'd love to join Wonka as its next Partner Sales Manager.", WONKA),
        ("I'd love to join Wonka as part of its wholesale team.", WONKA),
        ("I'd love to join Wonka and help its customers grow.", WONKA),
        ("I'd love to join Wonka as it expands into wholesale.", WONKA),
        ("I'd love to join Wonka and serve its wholesale customers.", WONKA),
    ],
)
def test_an_aspiration_with_no_phrase_about_the_candidate_still_passes(
    text: str, job: dict[str, str]
) -> None:
    assert _check(text, AIRCALL_JD, job, CITED) == []


@pytest.mark.parametrize("fact", CANDIDATE_FACTS)
@pytest.mark.parametrize(
    "tail",
    [
        # round 4: outside the posting (jd_sources), a sentence about the
        # target uses only the posting's own words or a reaction: one word of
        # its own fails closed, whatever it says
        "Aircall supports the revenue team brilliantly.",
        "Aircall supports the revenue team every quarter.",
        "Aircall is the revenue team engine.",
        "Aircall's dialer caught my eye, and it shows.",
    ],
)
def test_a_sentence_about_the_target_off_the_posting_fails_closed(fact: str, tail: str) -> None:
    violations = _check(f"{fact} {tail}", AIRCALL_JD, AIRCALL, CITED)
    assert _flagged(violations, "unsourced_tool", "Aircall"), violations


# ---------------------------------------------------------------------------
# review 2026-10-20: the job sought credited with the candidate's facts
# (blocker) _seat_credits_target read the clause only when the target's name
# opened it, so a determiner before the name ("The Aircall process…") or a noun
# of the job sought before "at Aircall" ("The role at Aircall…") excused the
# mention as naming the job sought: each returned NO violations, after either
# fact or alone. The clause after the name is now read however it opens, a
# job sought that is its clause's subject may say nothing happened and state
# no number, and "the Aircall process"/"the Aircall search" read as the tool's.
# ---------------------------------------------------------------------------

JOB_SOUGHT_CREDITED = (
    # the reviewer's repros
    ("The Aircall process is the reason.", AIRCALL),
    ("The Aircall process is why.", AIRCALL),
    ("The Aircall process deserves the credit.", AIRCALL),
    ("The Aircall process made it possible.", AIRCALL),
    ("The Aircall process is the driving force.", AIRCALL),
    ("The Aircall interview process is a big part of why.", AIRCALL),
    ("The Aircall posting is a big part of why.", AIRCALL),
    ("The Aircall role is the reason.", AIRCALL),
    ("The Aircall search is the reason.", AIRCALL),
    ("The Aircall process grew the weekend-pickup surcharge to $30–40K/week.", AIRCALL),
    ("The Seismic process is the reason.", SEISMIC),
    ("The Highspot process deserves the credit.", HIGHSPOT),
    # …and their neighbours: the job sought before "at <target>", other
    # determiners, the target's people, a past tense or a number with no word
    # of credit, a word of credit after the job sought as an object, a
    # relative clause, an "it" after an opening prepositional phrase
    ("The role at Aircall is the reason.", AIRCALL),
    ("The interview process at Aircall is a big part of why.", AIRCALL),
    ("The posting at Aircall deserves the credit.", AIRCALL),
    ("The process at Aircall made it possible.", AIRCALL),
    ("The search at Aircall is the reason.", AIRCALL),
    ("The team at Aircall deserves the credit.", AIRCALL),
    ("This Aircall role is the reason.", AIRCALL),
    ("That Aircall posting deserves the credit.", AIRCALL),
    ("Your Aircall role is the reason.", AIRCALL),
    ("The new Aircall role is the reason.", AIRCALL),
    ("The Aircall role made it possible.", AIRCALL),
    ("The Aircall role is the driving force.", AIRCALL),
    ("The Aircall opening drove that growth.", AIRCALL),
    ("The Aircall role grew the weekend-pickup surcharge to $30–40K/week.", AIRCALL),
    ("The role at Aircall grew the weekend-pickup surcharge to $30–40K/week.", AIRCALL),
    ("The Aircall role powers the weekend-pickup surcharge's $30–40K/week.", AIRCALL),
    (
        "I'm applying for the role at Aircall because it drove the weekend-pickup surcharge to "
        "$30–40K/week.",
        AIRCALL,
    ),
    (
        "I'm applying for the Aircall role, which grew the weekend-pickup surcharge to "
        "$30–40K/week.",
        AIRCALL,
    ),
    ("The Seismic role is the reason.", SEISMIC),
    ("The role at Highspot deserves the credit.", HIGHSPOT),
)


@pytest.mark.parametrize("fact", ("", *CANDIDATE_FACTS))
@pytest.mark.parametrize(("tail", "job"), JOB_SOUGHT_CREDITED)
def test_the_job_sought_is_never_credited_with_the_candidates_facts(
    fact: str, tail: str, job: dict[str, str]
) -> None:
    violations = _check(f"{fact} {tail}".strip(), AIRCALL_JD, job, CITED)
    assert _flagged(violations, "unsourced_tool", job["target_company"]), violations
    assert any(v["kind"] == "unsourced_tool" and tail in v["sentence"] for v in violations)


@pytest.mark.parametrize("fact", ("", *CANDIDATE_FACTS))
@pytest.mark.parametrize(
    "tail",
    [
        # the job sought named as such: a reaction, an object of seeking, a
        # present description with no word of credit, a relative clause that
        # only reacts, an opening prepositional phrase
        "The Aircall posting caught my eye.",
        "The role at Aircall caught my eye.",
        "The Director of Sales Enablement role at Aircall caught my eye.",
        "I saw the Aircall posting and wanted to write.",
        "I saw the Aircall posting, which caught my eye.",
        "I'm excited about the Aircall role.",
        "The Aircall role is a natural next step.",
        "I'm applying for the Director of Sales Enablement role at Aircall.",
        "For the Director of Sales Enablement role at Aircall, the first project would be a "
        "library of recorded calls.",
    ],
)
def test_the_job_sought_named_as_such_still_passes(fact: str, tail: str) -> None:
    assert _check(f"{fact} {tail}".strip(), AIRCALL_JD, AIRCALL, CITED) == []


def test_an_it_after_the_job_sought_is_an_accepted_false_rejection() -> None:
    # fail closed (review 2026-10-20): "it" after the job sought may be the
    # target ("…because it drove…"), so "it caught my eye" fails too. The
    # same reaction passes as "The Aircall posting caught my eye." or "…, which
    # caught my eye."
    violations = _check(
        "I saw the Aircall posting and it caught my eye.", AIRCALL_JD, AIRCALL, CITED
    )
    assert _flagged(violations, "unsourced_tool", "Aircall"), violations
