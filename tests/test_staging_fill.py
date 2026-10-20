"""Form-fill cases: what the matcher routes where, and what the filler
clicks, with fake Playwright objects. Offline, no browser.

1. A state dropdown never picks a neighbor that shares the answer's first
   word (North Carolina for North Dakota), and every option is read, not only
   the first 30 (North Dakota is option 35 of an alphabetical list).
2. Country-of-residence dropdowns are filled (the United States is far past
   option 30).
3. EEO: the test bank answers every EEO question "I don't wish to answer".
   The form's own decline option is chosen, "mark all that apply" groups get
   only the decline box, and transgender / sexual-orientation questions have
   their own keys.
4. Ashby Location autocompletes were left unfilled.
5. Legal acknowledgements fill only from the bank's legal_acknowledgement.
6. An outbound-sequences question was never-draft:contact-detail because it
   mentions "email" as a channel.

Review 2026-10-10 of those fixes: an arbitration or AI-policy TOPIC took the
acknowledgement entry ("Arbitration experience" -> "Yes"), the LGBTQIA+
community question took eeo.transgender, and the narrowed contact-detail check
let "What email should we use to contact you?" through to the drafter."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from jobkit.autofill import answer_bank, browser
from jobkit.autofill.answer_bank import (
    BankMatcher,
    approved_acknowledgement,
    legal_acknowledgement,
    never_draft,
    residence_question,
)
from jobkit.autofill.browser import Choice, ClickRefused, choice_for, staging_match
from tests.test_option_matching import COUNTRIES, STATES
from tests.test_staging_browser import FakeFrame, FakePage

DECLINE = "I don't wish to answer"
ACK_PREFER = ["I agree", "I Agree", "Yes", "I understand", "Acknowledge", "I acknowledge"]
OUTBOUND = (
    "Have you independently managed full outbound prospecting sequences (calls, email, and "
    "social) in a professional role?"
)

# no personal values: a made-up candidate in a town whose name many states share
BANK: dict[str, Any] = {
    "match_threshold": 0.82,
    "identity": {
        "first_name": "Pat",
        "last_name": "Doe",
        "email": "pat@example.com",
        "city": "Bristol",
        "state": "PA",
        "country": "United States",
    },
    "screening": [
        {
            "id": "sponsorship",
            "aliases": ["Do you require visa sponsorship?"],
            "answer": "No",
        },
        {
            "id": "country_of_residence",
            "aliases": ["What is your current country of residence?"],
            "answer": "United States",
        },
        {
            "id": "state_of_residence",
            "aliases": ["What is your state of residence?"],
            "answer": "Pennsylvania",
        },
        {
            "id": "working_location",
            "aliases": ["Please enter your current working location"],
            "answer": "Bristol, PA",
        },
        {
            "id": "outbound_sequences",
            "aliases": ["Have you independently managed full outbound prospecting sequences"],
            "answer": "Yes",
        },
        {
            "id": "legal_acknowledgement",
            "aliases": ["Please read the arbitration agreement"],
            "answer": "Yes",
            "select_preference": ACK_PREFER,
            # the auto-agree allowlist: exact labels the owner approved
            "approved_labels": [
                "Please read the arbitration agreement below",
                "Agreement to Arbitrate",
                "AI Policy for Application",
                "AI Policy for Interviewers",
                "I understand and agree that Soylent may check additional references "
                "of its own choosing to verify my work history.",
                "Do you consent to us contacting your references?",
            ],
        },
    ],
    "eeo": {
        k: DECLINE
        for k in (
            "gender",
            "race_ethnicity",
            "hispanic_latino",
            "veteran_status",
            "sexual_orientation",
            "transgender",
            "disability_status",
        )
    },
}


def _without(*ids: str) -> dict[str, Any]:
    return {**BANK, "screening": [s for s in BANK["screening"] if s["id"] not in ids]}


def _m(bank: dict[str, Any] | None = None) -> BankMatcher:
    return BankMatcher(bank=bank or BANK, use_embeddings=False)


def _verdict(v: dict[str, Any]) -> tuple[str, str]:
    return str(v.get("kind")), str(v.get("entry_id") or v.get("reason") or "")


# ---- matcher: EEO keys -------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "key"),
    [
        ("Do you identify as transgender?", "transgender"),
        ("How would you describe your sexual orientation? (mark all that apply)",
         "sexual_orientation"),
        ("Sexual Orientation", "sexual_orientation"),
        ("How would you describe your gender identity? (mark all that apply)", "gender"),
        ("How would you describe your racial/ethnic background? (mark all that apply)",
         "race_ethnicity"),
        ("Are you a veteran or active member of the United States Armed Forces?",
         "veteran_status"),
        ("Are you Hispanic/Latino?", "hispanic_latino"),
    ],
)  # fmt: skip
def test_eeo_labels_map_to_their_own_keys(label: str, key: str) -> None:
    v = _m().match(label, "eeo")
    assert (v["kind"], v["entry_id"], v["answer"]) == ("eeo", key, DECLINE)


def test_transgender_is_not_the_gender_answer() -> None:
    """Review 2026-10-09: "transgender" contains "gender", so the gender
    answer was offered to the transgender question."""
    only_gender = {**BANK, "eeo": {"gender": "Man"}}
    v = _m(only_gender).match("Do you identify as transgender?", "eeo")
    assert v == {"kind": "blank_flag", "reason": "eeo-no-stored-choice"}


# a form label as written (the "LBGTQIA+" is the form's own spelling)
LBGTQIA_LABEL = (
    "Do you identify as part of the Lesbian, Bisexual, Gay, Transgender, Queer, Intersex, and "
    "Asexual (LBGTQIA+) community?"
)


@pytest.mark.parametrize(
    "label",
    [
        "Pronouns",
        "What are your pronouns?",
        "Do you identify as LGBTQ+?",
        LBGTQIA_LABEL,
        LBGTQIA_LABEL.replace("LBGTQIA+", "LGBTQIA+"),
        "Are you part of the LBGTQIA+ community?",
        "Is your gender identity the same as the sex you were assigned at birth?",
        "Do you identify as transgender or non-binary?",
    ],
)
def test_pronouns_and_unkeyed_demographics_stay_unanswered(label: str) -> None:
    assert _m().match(label) == {"kind": "blank_flag", "reason": "eeo-no-stored-choice"}
    assert never_draft(label) == "demographic"


def test_the_lgbtqia_community_question_never_takes_the_transgender_answer() -> None:
    """Review 2026-10-10: _eeo_key checked the substring "transgender" first,
    so a form's LGBTQIA+ community question was answered from eeo.transgender
    — an entry that does not cover it (hard rule 4)."""
    bank = {**BANK, "eeo": {"transgender": "No", "sexual_orientation": "Heterosexual"}}
    for label in (LBGTQIA_LABEL, LBGTQIA_LABEL.replace("LBGTQIA+", "LGBTQIA+")):
        assert _m(bank).match(label, "eeo") == {
            "kind": "blank_flag",
            "reason": "eeo-no-stored-choice",
        }
        f = {"identity": "q_lgbtq", "label": label, "compliance_type": "eeo", "match": {}}
        assert staging_match(_m(bank), f, None)["kind"] == "blank_flag"
    # the question that IS about being transgender still takes the entry
    v = _m(bank).match("Do you identify as transgender?", "eeo")
    assert (v["entry_id"], v["answer"]) == ("transgender", "No")


@pytest.mark.parametrize(
    "label",
    [
        "How would you describe your gender identity? (e.g. woman, man, transgender, non-binary)",
        "Gender identity (e.g. woman, man, transgender, non-binary)",
        "What is your gender identity, such as woman, man, transgender or non-binary?",
    ],
)
def test_a_gender_label_listing_transgender_as_an_example_asks_for_gender(label: str) -> None:
    bank = {**BANK, "eeo": {"gender": "Man", "transgender": "No"}}
    v = _m(bank).match(label, "eeo")
    assert (v["kind"], v["entry_id"], v["answer"]) == ("eeo", "gender", "Man")


@pytest.mark.parametrize(
    "label",
    [
        "Transgender",
        "Transgender status",
        "Are you transgender?",
        "Do you consider yourself to be transgender?",
        "Do you identify as transgender? (Yes / No / Decline)",
    ],
)
def test_transgender_questions_reach_the_transgender_key(label: str) -> None:
    v = _m().match(label, "eeo")
    assert (v["kind"], v["entry_id"]) == ("eeo", "transgender")


def test_eeo_answers_come_only_from_the_eeo_block() -> None:
    """Hard rule 4: no eeo block, no answer — whatever the screening entries say."""
    no_eeo = {**BANK, "eeo": {}}
    for label in ("Do you identify as transgender?", "Sexual Orientation", "Gender"):
        assert _m(no_eeo).match(label, "eeo")["kind"] == "blank_flag"
    # "embrace" contains "race": never handed the race answer
    v = _m().match("How do you embrace change?")
    assert v.get("answer") is None


# ---- matcher: residence questions ------------------------------------------------------

COUNTRY_LABELS = [
    "What is your current country of residence?",
    "Please choose the country in which you are located.",
    "Which country do you currently live in?",
    "In what country are you based?",
    "Country you are located in",
    "Country of Residence",
    "What country do you reside in?",
]


@pytest.mark.parametrize("label", COUNTRY_LABELS)
def test_country_of_residence_questions_reach_the_entry(label: str) -> None:
    assert _verdict(_m().match(label, identity="question_1")) == (
        "screening",
        "country_of_residence",
    )


@pytest.mark.parametrize(
    "label",
    [
        "What is your state of residence?",
        "Which state do you live in?",
        "What state are you currently located in?",
        "U.S. State of Residence",
    ],
)
def test_state_of_residence_questions_reach_the_entry(label: str) -> None:
    assert _verdict(_m().match(label, identity="question_1")) == (
        "screening",
        "state_of_residence",
    )


@pytest.mark.parametrize(
    "label",
    [
        "Will you now or will you in the future require employment visa sponsorship to work in "
        "the country in which the job you're applying for is located?",
        "Are you legally eligible to work in the country where you are located?",
        "Are you willing to relocate to the country where the office is located?",
        "Please state where you live",
        "Which state are you licensed in?",
    ],
)
def test_questions_that_mention_a_country_are_not_residence(label: str) -> None:
    assert residence_question(label) is None
    assert "residence" not in str(_m().match(label).get("entry_id"))


def test_residence_without_a_bank_entry_is_never_identity() -> None:
    bare = _without("country_of_residence", "state_of_residence")
    for label in ("Please choose the country in which you are located.",
                  "Which state do you live in?"):  # fmt: skip
        assert _m(bare).match(label, identity="q")["kind"] not in ("identity", "screening")
    # a plain field name is still the identity field
    assert _verdict(_m(bare).match("Country")) == ("identity", "country")


# ---- matcher: legal acknowledgements ---------------------------------------------------

COVERED_ACKS = [
    "Please read the arbitration agreement below",
    "Agreement to Arbitrate",
    "AI Policy for Application",
    "AI Policy for Interviewers",
    "I understand and agree that Soylent may check additional references "
    "of its own choosing to verify my work history.\xa0",
    "Do you consent to us contacting your references?",
]


@pytest.mark.parametrize("label", COVERED_ACKS)
def test_covered_acknowledgements_fill_from_the_entry(label: str) -> None:
    assert legal_acknowledgement(label) == "covered"
    assert _verdict(_m().match(label, identity="q")) == ("screening", "legal_acknowledgement")


@pytest.mark.parametrize("label", COVERED_ACKS)
def test_without_the_entry_acknowledgements_stay_blank(label: str) -> None:
    v = _m(_without("legal_acknowledgement")).match(label, identity="q")
    assert v == {"kind": "blank_flag", "reason": "legal-acknowledgement-needs-human:covered"}
    assert never_draft(label) == "legal-acknowledgement"


@pytest.mark.parametrize(
    "label",
    [
        "Privacy notice",
        "By submitting my application, I acknowledge that I have read and understand Globex’s "
        "Job Applicant Privacy Notice",
        "If we decide you are good potential fit for the role, do you consent to us using this "
        "tool during the interviewing process?",
        "I agree to the Terms and Conditions",
    ],
)
def test_other_acknowledgements_go_to_the_human_even_with_the_entry(label: str) -> None:
    """The user's entry covers arbitration, AI-use policies and reference
    checks. A privacy notice or a recording consent is still the human's."""
    assert _m().match(label, identity="q") == {
        "kind": "blank_flag",
        "reason": "legal-acknowledgement-needs-human:other",
    }


@pytest.mark.parametrize(
    "label",
    [
        "Have you ever been party to an arbitration proceeding with a former employer, and if "
        "so, what was the outcome?",
        "Describe your experience with AI sales tools and our product policies",
        "Can you provide references?",
    ],
)
def test_a_topic_alone_is_not_an_acknowledgement(label: str) -> None:
    assert legal_acknowledgement(label) is None


# review 2026-10-10: each of these was "covered" and filled "Yes" from the entry
NOT_ACKS = [
    "Arbitration experience",
    "Do you have experience with contract review including arbitration clauses?",
    "Familiarity with AI policy frameworks",
    "Have you worked with arbitration agreements in prior roles?",
    "How many years of experience do you have with AI policy compliance?",
]


@pytest.mark.parametrize("label", NOT_ACKS)
def test_a_question_about_the_candidate_never_takes_the_acknowledgement_entry(
    label: str,
) -> None:
    """Offline repro from the review: a bank holding only legal_acknowledgement
    ("Yes"), no embeddings. The entry must not answer a skill question."""
    only_ack = {
        "screening": [{"id": "legal_acknowledgement", "aliases": [], "answer": "Yes"}],
        "eeo": {},
    }
    assert legal_acknowledgement(label) != "covered"
    v = _m(only_ack).match(label, identity="q")
    assert v.get("entry_id") != "legal_acknowledgement"
    f = {"identity": "q", "label": label, "options": ["Yes", "No"], "match": {}}
    fresh = staging_match(_m(only_ack), f, None)
    assert fresh.get("entry_id") != "legal_acknowledgement"
    assert not choice_for(fresh, only_ack, "Boolean").acknowledgement


@pytest.mark.parametrize(
    "label",
    [
        "Arbitration",  # a bare topic: the human's, not the drafter's
        "Have you read the arbitration agreement?",
        "Do you agree that arbitration is fair?",
        "Review the arbitration clauses in this contract",
        "If I agree to arbitration, what happens?",
        "May we contact your references?",
        "Do you consent to a background check and reference checks?",
    ],
)
def test_unshaped_acknowledgement_topics_go_to_the_human(label: str) -> None:
    assert legal_acknowledgement(label) == "other"
    assert _m().match(label, identity="q") == {
        "kind": "blank_flag",
        "reason": "legal-acknowledgement-needs-human:other",
    }
    assert never_draft(label) == "legal-acknowledgement"


# review 2026-10-10: each was covered by its read-the-policy sentence and
# answered "Yes" from the entry, though the question it asks is something else
READ_THEN_ASK = [
    "Please review our AI policy. Will you use AI assistance during your interviews?",
    "Please read the arbitration agreement below. Are you currently a party to any lawsuit or "
    "arbitration against a former employer?",
    "Please read the AI policy above and let us know if you used AI to prepare this application",
    "Please read our AI policy below. Would you be interested in joining our AI tooling beta?",
    "Do you agree to arbitrate disputes, and are you a party to any pending arbitration?",
    "Please read the AI policy and confirm whether you used AI on your application",
    # second review 2026-10-10: a question needs no "?" and no are/will/would
    # ("do you", "can you"), may follow a ":" or ";", or may be an instruction
    # to select or list something. Each of these was covered and got "Yes".
    "Do you agree to arbitrate claims, and do you currently have any pending claims against a "
    "former employer?",
    "Please read our AI policy below. Do you currently use AI tools at work",
    "Please read our AI policy below. Do you plan to use AI in interviews",
    "Please read the AI policy above: Do you use AI at work",
    "Please review our AI policy; do you use Copilot",
    "Please read the arbitration agreement below. Can you confirm you have no pending claims "
    "against prior employers",
    "Please review our AI policy and select which AI tools you used",
]
# second review 2026-10-10: the candidate confirms or certifies a FACT about
# themselves, not agreement to the policy; the entry's "Yes" attested to it
ATTESTS_A_FACT = [
    "Please review our AI policy and confirm that you did not use AI to complete this application.",
    "Please read our AI policy. Please confirm you did not use AI tools to write your answers.",
    "I acknowledge the AI policy and certify that I did not use AI to prepare my application",
    "Please read the arbitration agreement below and confirm you are not currently party to any "
    "lawsuit against a former employer.",
    "By checking this box, I agree to the arbitration agreement and confirm I have never been "
    "terminated for cause.",
]
# a user's legal_acknowledgement entry as the bank holds it: its aliases,
# its "Yes" and its select_preference (no personal values in it)
USER_ACK_ENTRY: dict[str, Any] = {
    "id": "legal_acknowledgement",
    "aliases": [
        "Please read the arbitration agreement",
        "Agreement to Arbitrate",
        "AI Policy for Application",
        "I understand and agree that",
        "may check additional references",
    ],
    "answer": "Yes",
    "select_preference": ACK_PREFER,
    "type": "boolean",
    "approved_labels": [
        "AI Policy for Application",
        "Please read the arbitration agreement below",
        "Agreement to Arbitrate",
        "I understand and agree that Soylent may check additional references "
        "of its own choosing to verify my work history.",
    ],
}


@pytest.mark.parametrize("label", READ_THEN_ASK + ATTESTS_A_FACT)
def test_a_label_that_asks_beyond_consent_goes_to_the_human(label: str) -> None:
    assert legal_acknowledgement(label) == "other"
    assert _m().match(label, identity="q") == {
        "kind": "blank_flag",
        "reason": "legal-acknowledgement-needs-human:other",
    }
    # however it reaches the entry, by alias or embedding, it is not agreed to
    alias_bank = {
        "screening": [{"id": "legal_acknowledgement", "aliases": [label[:30]], "answer": "Yes"}],
        "eeo": {},
    }
    assert _m(alias_bank).match(label, identity="q") == {
        "kind": "blank_flag",
        "reason": "legal-acknowledgement-needs-human:other",
    }
    assert never_draft(label) == "legal-acknowledgement"


@pytest.mark.parametrize("label", READ_THEN_ASK[6:] + ATTESTS_A_FACT)
def test_second_review_labels_never_reach_a_yes(label: str) -> None:
    """The review's offline repros: a bank holding only the entry, and one
    holding the user's entry with its aliases and select_preference. Each
    label stays blank and flagged at staging, so pick_option never sees it
    with acknowledgement=True and "Yes" is never chosen."""
    other = {"kind": "blank_flag", "reason": "legal-acknowledgement-needs-human:other"}
    only_ack = {"screening": [{"id": "legal_acknowledgement", "answer": "Yes"}], "eeo": {}}
    owner = {"screening": [USER_ACK_ENTRY], "eeo": {}}
    for bank in (only_ack, owner):
        assert _m(bank).match(label, identity="q") == other
        stale = {"kind": "screening", "entry_id": "legal_acknowledgement"}
        f = {"identity": "q", "label": label, "options": ["Yes", "No"], "match": stale}
        fresh = staging_match(_m(bank), f, None)
        assert fresh == other
        assert not choice_for(fresh, bank, "Boolean").acknowledgement


# third review 2026-10-10: the checks above look for question cues, so a
# fact written as a fragment, or a statement whose "I" is left out, stayed
# covered, and the bank's entry ("Yes") stated the fact or opted out. Each
# was checked offline against a full answer bank (BankMatcher without
# embeddings returned legal_acknowledgement for all five).
FRAGMENT_FACTS = [
    "Please read the arbitration agreement. Currently party to a lawsuit against an employer",
    "Please review our AI policy. AI used in this application (yes/no)",
    "Please read the arbitration agreement below. Opt out of arbitration",
    "I acknowledge the AI policy and have not used AI",
    "Please read the arbitration agreement below. Number of lawsuits filed against former "
    "employers",
]
# the same failure in other words: a line break for a full stop, the "I" left
# out with no negation, opt-out / waiver / withdrawal wording, a negation
FRAGMENT_FACTS_MORE = [
    "Please read the arbitration agreement below\nCurrently party to a lawsuit",
    "Please review our AI policy\nAI tools used",
    "I acknowledge the AI policy and have used AI to prepare this application",
    "I agree to the arbitration agreement and did not sign a prior one",
    "Please read the arbitration agreement below. I wish to opt out",
    "I agree to opt out of the arbitration agreement",
    "Please read the arbitration agreement below. I waive my right to opt-out",
    "I withdraw my consent to the AI policy",
    "I understand the arbitration agreement does not apply to me",
    "Agreement to Arbitrate: lawsuits pending",
    "AI Policy for Application - AI used",
    # a run-on fragment (Lever joins label parts with a space), or one after
    # a sentence that names the document
    "Please read the arbitration agreement below currently party to a lawsuit",
    "Please read the AI policy. AI policy - AI used",
    "Please read our AI policy. Our AI policy and AI used in this application",
]


@pytest.mark.parametrize("label", FRAGMENT_FACTS + FRAGMENT_FACTS_MORE)
def test_a_fragment_beside_the_acknowledgement_goes_to_the_human(label: str) -> None:
    other = {"kind": "blank_flag", "reason": "legal-acknowledgement-needs-human:other"}
    assert legal_acknowledgement(label) == "other"
    assert never_draft(label) == "legal-acknowledgement"
    only_ack = {"screening": [{"id": "legal_acknowledgement", "answer": "Yes"}], "eeo": {}}
    owner = {"screening": [USER_ACK_ENTRY], "eeo": {}}
    for bank in (BANK, only_ack, owner):
        assert _m(bank).match(label, identity="q") == other
        stale = {"kind": "screening", "entry_id": "legal_acknowledgement"}
        f = {"identity": "q", "label": label, "options": ["Yes", "No"], "match": stale}
        fresh = staging_match(_m(bank), f, None)
        assert fresh == other
        assert not choice_for(fresh, bank, "Boolean").acknowledgement


@pytest.mark.parametrize(
    "label",
    [
        # every sentence acknowledges, or only describes the document
        "AI Policy for Application. Our AI policy explains how we use AI in hiring.",
        "Agreement to Arbitrate\nPlease read the arbitration agreement below.",
        "Please read the arbitration agreement below. The agreement is linked above.",
        # a URL or an abbreviation is not a sentence break
        "Please read our AI policy at https://example.com/careers/ai-policy.",
        "I agree that Acme Inc. may contact my references",
        "Agreement to Arbitrate (required)",
        "Agreement to Arbitrate\n",
        "Agreement to Arbitrate - please read the agreement below",
        "Please read our AI policy - https://example.com/ai",
        "Please read the arbitration agreement below carefully before applying.",
    ],
)
def test_acknowledgement_sentences_stay_covered(label: str) -> None:
    """The classifier still reads these as covered acknowledgements, but since
    2026-10-10 the automatic "Yes" needs the exact label on the user's
    approved_labels allowlist: a variant goes to the human."""
    assert legal_acknowledgement(label) == "covered"
    owner = {"screening": [USER_ACK_ENTRY], "eeo": {}}
    v = _m(owner).match(label, identity="q")
    if approved_acknowledgement(owner, label):
        assert _verdict(v) == ("screening", "legal_acknowledgement")
    else:
        assert v == {
            "kind": "blank_flag",
            "reason": "legal-acknowledgement-needs-human:not-an-approved-label",
        }


# acknowledgement-shaped, but what it asks is about the candidate
@pytest.mark.parametrize(
    "label",
    [
        "Please read our AI policy. Did you use AI to write this application?",
        "Please read the arbitration agreement below. Have you ever been party to an arbitration?",
    ],
)
def test_a_shaped_question_about_the_candidate_goes_to_the_human(label: str) -> None:
    assert legal_acknowledgement(label) == "other"
    assert _m().match(label, identity="q") == {
        "kind": "blank_flag",
        "reason": "legal-acknowledgement-needs-human:other",
    }
    assert never_draft(label) == "legal-acknowledgement"


@pytest.mark.parametrize(
    "label",
    [
        "AI Policy for Application",
        "Agreement to Arbitrate",
        "I understand and agree that Soylent may check additional references "
        "of its own choosing to verify my work history.\xa0",
        "Do you consent to us contacting your references?",
        "Would you agree to arbitrate any employment disputes?",
        "Please read the arbitration agreement below. Do you agree to its terms?",
    ],
)
def test_consent_questions_stay_covered(label: str) -> None:
    assert legal_acknowledgement(label) == "covered"


@pytest.mark.parametrize(
    "label",
    [
        "Arbitration Agreement",
        "Mutual Arbitration Agreement ✱",
        "AI Use Policy",
        "Do you agree to arbitrate any disputes?",
        "I have read and agree to the Arbitration Agreement",
        "By submitting this application, I acknowledge that I have read the AI Policy",
        "Please acknowledge the AI policy",
        "I understand and agree that you may contact my references",
        # confirming or certifying having read / agreeing is still agreement
        "Please confirm you have read and agree to the AI policy",
        "I certify that I have read and understand the arbitration agreement",
        "Please confirm receipt of the arbitration agreement",
        # the employer confirms, not the candidate
        "I agree that you may contact my references to confirm my employment",
        "Do you consent to us contacting your references, including former managers?",
        "Please read the AI policy and select 'I agree'",
        "Please read our AI policy, which explains how we use AI in hiring",
    ],
)
def test_acknowledgement_shapes_are_covered(label: str) -> None:
    assert legal_acknowledgement(label) == "covered"


def test_a_broad_alias_never_carries_the_entry_past_its_kinds() -> None:
    """A user's bank may hold the alias "I understand and agree that" for a
    reference-check label like Soylent's. Alias (and embedding) hits pass the same
    shape check, so it cannot agree to a privacy notice."""
    bank = {
        **BANK,
        "screening": [
            {
                "id": "legal_acknowledgement",
                "aliases": ["I understand and agree that", "arbitration"],
                "answer": "Yes",
                "approved_labels": USER_ACK_ENTRY["approved_labels"],
            }
        ],
    }
    privacy = (
        "I understand and agree that my personal data will be processed per the Privacy Notice"
    )
    assert _m(bank).match(privacy, identity="q") == {
        "kind": "blank_flag",
        "reason": "legal-acknowledgement-needs-human:other",
    }
    assert _m(bank).match("Arbitration experience", identity="q") == {
        "kind": "blank_flag",
        "reason": "legal-acknowledgement-needs-human:not-an-acknowledgement",
    }
    soylent = COVERED_ACKS[4]
    assert _verdict(_m(bank).match(soylent, identity="q")) == (
        "screening",
        "legal_acknowledgement",
    )


# ---- matcher: contact-detail means asking for contact details --------------------------


def test_outbound_sequences_question_is_not_a_contact_detail() -> None:
    assert never_draft(OUTBOUND) is None
    assert _verdict(_m().match(OUTBOUND, identity="question_61200045002")) == (
        "screening",
        "outbound_sequences",
    )
    # the verdict captured earlier is replaced at staging
    f = {
        "identity": "question_61200045002",
        "label": OUTBOUND,
        "match": {"kind": "blank_flag", "reason": "never-draft:contact-detail"},
    }
    assert _verdict(staging_match(_m(), f, None)) == ("screening", "outbound_sequences")
    # with no bank entry it is an ordinary question for the drafter
    assert _m(_without("outbound_sequences")).match(OUTBOUND)["kind"] == "novel"


@pytest.mark.parametrize(
    "label",
    [
        "How many email sequences did you run per week?",
        "Which channels did you prospect through (phone, email, LinkedIn)?",
        "Have you managed the email channel for a sales team?",
        "Do you have experience with cold email outreach?",
    ],
)
def test_email_as_a_channel_is_not_a_contact_detail(label: str) -> None:
    assert never_draft(label) != "contact-detail"


@pytest.mark.parametrize(
    "label",
    [
        "Former employee email",
        "Email address",
        "Referrer email (if referred)",
        "What is the email address of the person who referred you?",
        "If you were previously employed by Cyberdyne, please share the email you used for "
        "signing in.",
        "Has someone referred you? If so, please include their email here",
        "Alternate email",
        "Email for scheduling",
        "Please provide your contact information",
        # review 2026-10-10: the narrowed check let these through to the drafter
        "What email should we use to contact you?",
        "Which email should we use to contact you?",
        "Which email did you use when you applied before?",
        "What email did you use when you applied previously?",
        "What email can we reach you at?",
        "What is a good email for you?",
        "Preferred email for scheduling",
        "What's your Calendly or email for scheduling?",
        # channel words do not hide an ask
        "What's your phone, email, LinkedIn?",
        "What is the best phone or email to reach you?",
        "What's your sales email?",
        "E-mail",
    ],
)
def test_asking_for_an_email_is_still_a_contact_detail(label: str) -> None:
    assert never_draft(label) in ("contact-detail", "referral")


# review 2026-10-10: a list of contact channels is a contact field, not "email as
# a sales channel" — these reached the drafter as novel questions
CONTACT_CHANNEL_FIELDS = [
    "Email / Phone",
    "Phone/Email",
    "Phone or email",
    "E-mail/Phone number",
    "Phone, email",
    "LinkedIn or email",
    "Email or LinkedIn*",
    "Phone or email?",
    "Have you got a phone or email we can use?",
    "Do you have a phone or email we can use?",
    "Cold email experience",  # field-like: the human answers (a false rejection is fine)
]


@pytest.mark.parametrize("label", CONTACT_CHANNEL_FIELDS)
def test_a_list_of_contact_channels_is_a_contact_detail(label: str) -> None:
    assert never_draft(label) == "contact-detail"
    f = {"identity": "q_contact", "label": label, "match": {}}
    assert staging_match(_m(), f, None) == {
        "kind": "blank_flag",
        "reason": "never-draft:contact-detail",
    }


@pytest.mark.parametrize(
    "label",
    [
        OUTBOUND,
        "How many emails did you send per day as an SDR?",
        "What email marketing tools have you used?",
        "Describe your experience with email outreach",
    ],
)
def test_a_question_about_sales_work_by_email_is_not_a_contact_detail(label: str) -> None:
    assert never_draft(label) is None


@pytest.mark.parametrize(
    "label",
    [
        "What email should we use to contact you?",
        "Which email did you use when you applied before?",
        "Preferred email for scheduling",
        "What's your Calendly or email for scheduling?",
    ],
)
def test_an_email_ask_left_to_drafting_is_blanked_at_staging(label: str) -> None:
    """At staging (and capture) a field the matcher leaves to the drafter is
    checked by never_draft: these must come back blank and flagged."""
    f = {"identity": "q_mail", "label": label, "match": {}}
    assert staging_match(_m(), f, None) == {
        "kind": "blank_flag",
        "reason": "never-draft:contact-detail",
    }


# ---- choice hints ----------------------------------------------------------------------


def test_choice_for_carries_preference_acknowledgement_and_location() -> None:
    ack = choice_for({"kind": "screening", "entry_id": "legal_acknowledgement"}, BANK, "Boolean")
    assert ack == Choice(prefer=tuple(ACK_PREFER), acknowledgement=True)
    assert choice_for({"kind": "identity", "entry_id": "address"}, BANK, "String").location
    assert choice_for({"kind": "screening", "entry_id": "working_location"}, BANK, "").location
    assert choice_for({"kind": "novel"}, BANK, "Location").location
    assert choice_for({"kind": "eeo", "entry_id": "gender"}, BANK, "ValueSelect") == Choice()


# ---- fake Playwright ---------------------------------------------------------------------


class El:
    """One element. `guard` is what the in-page click guard reports for it."""

    def __init__(
        self,
        text: str = "",
        tag: str = "div",
        attrs: dict[str, str] | None = None,
        guard: str = "",
    ) -> None:
        self.text, self.tag, self.attrs, self.guard = text, tag, attrs or {}, guard
        self.clicks = self.checks = self.probed = 0
        self.checked = False
        self.value = ""
        self.selected: str | None = None
        self.on_click: Callable[[El], None] | None = None
        self.on_type: Callable[[El], None] | None = None
        self.group: Many | None = None  # role=group / fieldset children

    def evaluate(self, js: str, arg: Any = None) -> Any:
        if js == browser._CLICK_GUARD_JS:
            self.probed += 1
            return self.guard
        if "closest('label')" in js and ".click()" in js:  # the allowlisted label click
            self.clicks += 1
            self.checked = not self.checked
            return None
        if js == browser._OPTION_LABEL_JS or "labels" in js:
            return self.text
        if "tagName" in js:
            return self.tag
        return None

    def get_attribute(self, name: str) -> str | None:
        return self.attrs.get(name)

    def click(self, **kw: Any) -> None:
        self.clicks += 1
        if self.on_click:
            self.on_click(self)

    def check(self, **kw: Any) -> None:
        self.checks += 1
        self.checked = True

    def is_checked(self) -> bool:
        return self.checked

    def inner_text(self, **kw: Any) -> str:
        return self.text

    def fill(self, value: str, **kw: Any) -> None:
        self.value = value
        if self.on_type:
            self.on_type(self)

    def press_sequentially(self, text: str, **kw: Any) -> None:
        assert "\n" not in text
        self.value += text
        if self.on_type:
            self.on_type(self)

    def select_option(self, label: str) -> None:
        self.selected = label

    def locator(self, sel: str) -> Many:
        if sel == "option":
            return Many([El(t) for t in self.attrs.get("_options", "").split("|") if t])
        if sel.startswith("xpath=ancestor::") and self.group is not None:
            return Many([El(tag="fieldset")], group=self.group)
        return Many([])

    def get_by_role(self, role: str, **kw: Any) -> Many:
        return self.group if self.group is not None else Many([])


class Many:
    def __init__(self, els: list[El], group: Many | None = None) -> None:
        self.els, self._group = els, group

    def count(self) -> int:
        return len(self.els)

    def nth(self, i: int) -> El:
        return self.els[i]

    @property
    def first(self) -> El:
        return self.els[0]

    def all_inner_texts(self) -> list[str]:
        return [e.text for e in self.els]

    def get_by_role(self, role: str, **kw: Any) -> Many:
        return self._group or Many([])


class Keyboard:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    def press(self, key: str) -> None:
        assert key == "Escape"
        self.pressed.append(key)


class Form(FakeFrame):
    """The frame holding the form. The open dropdown's options are what
    get_by_role('option') returns; checkbox groups are found by selector."""

    def __init__(self, ids: tuple[str, ...] = ()) -> None:
        super().__init__(url="https://jobs.ashbyhq.com/acme/x/application", ids=ids)
        self.listbox: Callable[[], list[El]] = list
        self.groups: dict[str, Many] = {}

    def get_by_role(self, role: str, **kw: Any) -> Many:
        return Many(self.listbox() if role == "option" else [])

    def locator(self, sel: str) -> Any:
        if sel in self.groups:
            return self.groups[sel]
        return super().locator(sel)

    def wait_for_timeout(self, ms: float) -> None:
        return None

    def dropdown(self, options: list[str], *, typed_filter: bool = False, shown: int = 0) -> El:
        """A react-select style combobox. shown>0: only that many options render
        until something is typed (a virtualized list); typing filters."""
        ctl = El(tag="input", attrs={"role": "combobox"})
        opts = [El(t) for t in options]

        def listing() -> list[El]:
            if typed_filter and ctl.value:
                return [o for o in opts if ctl.value.lower() in o.text.lower()]
            return opts[:shown] if shown else opts

        ctl.on_click = lambda _el: setattr(self, "listbox", listing)
        ctl.on_type = lambda _el: setattr(self, "listbox", listing)
        ctl.options = opts  # type: ignore[attr-defined]
        return ctl


class Tab:
    def __init__(self) -> None:
        self.keyboard = Keyboard()


def _clicked(ctl: El) -> list[str]:
    return [o.text for o in ctl.options if o.clicks]  # type: ignore[attr-defined]


def _fill(form: Form, ctl: El, value: str, choice: Choice | None = None, **kw: Any) -> bool:
    return browser._fill_control(Tab(), form, ctl, value, "", [], choice, **kw)  # type: ignore[arg-type]


# ---- 1/2: dropdowns read every option, then type to filter --------------------------------


def test_state_dropdown_picks_a_state_past_option_30() -> None:
    form = Form()
    ctl = form.dropdown(STATES)
    assert STATES.index("North Dakota") >= 30
    assert _fill(form, ctl, "North Dakota")
    assert _clicked(ctl) == ["North Dakota"]
    picked = ctl.options[STATES.index("North Dakota")]  # type: ignore[attr-defined]
    assert picked.probed == 1 and ctl.probed == 1  # both clicks went through the guard


def test_state_dropdown_never_falls_back_to_another_north_state() -> None:
    form, tab = Form(), Tab()
    ctl = form.dropdown([s for s in STATES if s != "North Dakota"], typed_filter=True)
    assert not browser._fill_control(tab, form, ctl, "North Dakota", "", [])  # type: ignore[arg-type]
    assert _clicked(ctl) == [] and tab.keyboard.pressed == ["Escape"]
    assert ctl.value == "", "nothing left half-typed in the box"


def test_native_select_reads_the_live_options_not_the_captured_20() -> None:
    form = Form()
    sel = El(tag="select", attrs={"_options": "|".join(["Select...", *STATES])})
    captured = STATES[:20]  # form_spec capture keeps 20 options
    assert browser._fill_control(Tab(), form, sel, "North Dakota", "", captured)  # type: ignore[arg-type]
    assert sel.selected == "North Dakota"


@pytest.mark.parametrize("us", ["United States", "United States of America", "USA"])
def test_country_dropdown_finds_the_united_states(us: str) -> None:
    countries = sorted([c for c in COUNTRIES if c != "United States"] + [us])
    form = Form()
    ctl = form.dropdown(countries)
    assert _fill(form, ctl, "United States")
    assert _clicked(ctl) == [us]


def test_virtualized_country_list_is_filtered_by_typing() -> None:
    """Only the first 30 countries render until something is typed."""
    form = Form()
    ctl = form.dropdown(COUNTRIES, typed_filter=True, shown=30)
    assert _fill(form, ctl, "United States")
    assert ctl.value == "United States"
    assert _clicked(ctl) == ["United States"]  # never the Minor Outlying Islands


class Rerendered(El):
    """An option whose text changed between reading the list and the click."""

    def inner_text(self, **kw: Any) -> str:
        return "North Carolina"


def test_an_option_list_that_changes_before_the_click_is_not_clicked() -> None:
    form = Form()
    ctl = form.dropdown(STATES)
    moved = Rerendered("North Dakota")
    ctl.options[STATES.index("North Dakota")] = moved  # type: ignore[attr-defined]
    assert not _fill(form, ctl, "North Dakota")
    assert moved.clicks == 0 and moved.probed == 0
    assert _clicked(ctl) == []


# ---- 3: EEO declines --------------------------------------------------------------------


def test_eeo_dropdown_picks_the_forms_decline_option() -> None:
    form = Form()
    ctl = form.dropdown(["Yes", "No", "Decline to self-identify"])
    assert _fill(form, ctl, DECLINE)
    assert _clicked(ctl) == ["Decline to self-identify"]
    assert ctl.value == "", "a decline is never typed into the filter"


def test_mark_all_that_apply_ticks_only_the_decline_box() -> None:
    form = Form()
    labels = ["Man", "Woman", "Non-binary", "I prefer to self-describe", "I prefer not to say"]
    boxes = [El(t, tag="input", attrs={"type": "checkbox", "name": "gender[]"}) for t in labels]
    form.groups['input[type="checkbox"][name="gender[]"]'] = Many(boxes)
    assert _fill(form, boxes[0], DECLINE)
    assert [b.text for b in boxes if b.checked] == ["I prefer not to say"]
    assert all(b.probed == (1 if b.text == "I prefer not to say" else 0) for b in boxes)


def test_checkbox_group_in_a_fieldset_without_shared_names() -> None:
    form = Form()
    labels = ["Heterosexual", "Gay", "Bisexual", "I don't wish to answer"]
    boxes = [El(t, tag="input", attrs={"type": "checkbox"}) for t in labels]
    group = Many(boxes)
    for b in boxes:
        b.group = group
    assert _fill(form, boxes[0], DECLINE)
    assert [b.text for b in boxes if b.checked] == ["I don't wish to answer"]


def test_checkbox_group_with_indistinct_labels_is_never_ticked() -> None:
    form = Form()
    same = "Man Woman I prefer not to say"  # every box reads its parent's whole text
    boxes = [El(same, tag="input", attrs={"type": "checkbox", "name": "g"}) for _ in range(3)]
    form.groups['input[type="checkbox"][name="g"]'] = Many(boxes)
    assert not _fill(form, boxes[0], DECLINE)
    assert not any(b.checked or b.clicks for b in boxes)


def test_decline_box_that_would_send_the_form_is_refused() -> None:
    form = Form()
    boxes = [
        El("Man", tag="input", attrs={"type": "checkbox", "name": "g"}),
        El("Decline", tag="input", attrs={"type": "checkbox", "name": "g"}, guard="submit-type"),
    ]
    form.groups['input[type="checkbox"][name="g"]'] = Many(boxes)
    with pytest.raises(ClickRefused):
        _fill(form, boxes[0], DECLINE)
    assert not any(b.checked or b.clicks for b in boxes)


def test_radio_fieldset_picks_the_decline_radio() -> None:
    form = Form()
    radios = [
        El(t, tag="input", attrs={"aria-label": t})
        for t in ("I am not a protected veteran", "I don't wish to answer")
    ]
    fieldset = El(tag="fieldset")
    fieldset.group = Many(radios)
    assert _fill(form, fieldset, DECLINE)
    assert [r.checks for r in radios] == [0, 1]


# review 2026-10-10: an option that states a status and declines only its detail
# is not a decline, on every control that chooses an option
VETERAN_MIXED = (
    "I am a protected veteran, but I choose not to self-identify the classifications to "
    "which I belong"
)
VETERAN_OPTIONS = [VETERAN_MIXED, "I am not a protected veteran", "I do not wish to self-identify"]


def test_veteran_radios_skip_the_status_then_decline_option() -> None:
    form = Form()
    radios = [El(t, tag="input", attrs={"aria-label": t}) for t in VETERAN_OPTIONS]
    fieldset = El(tag="fieldset")
    fieldset.group = Many(radios)
    assert _fill(form, fieldset, DECLINE)
    assert [r.checks for r in radios] == [0, 0, 1]


def test_veteran_radios_with_only_status_options_are_left_blank() -> None:
    form = Form()
    radios = [El(t, tag="input", attrs={"aria-label": t}) for t in VETERAN_OPTIONS[:2]]
    fieldset = El(tag="fieldset")
    fieldset.group = Many(radios)
    assert not _fill(form, fieldset, DECLINE)
    assert not any(r.checks or r.clicks for r in radios)


def test_veteran_select_and_dropdown_skip_the_status_then_decline_option() -> None:
    form = Form()
    sel = El(tag="select", attrs={"_options": "|".join(VETERAN_OPTIONS)})
    assert _fill(form, sel, DECLINE) and sel.selected == "I do not wish to self-identify"
    ctl = form.dropdown(VETERAN_OPTIONS)
    assert _fill(form, ctl, DECLINE)
    assert _clicked(ctl) == ["I do not wish to self-identify"]


def test_disability_checkbox_group_ticks_only_the_pure_decline() -> None:
    form = Form()
    labels = [
        "Yes, I have a disability, but I prefer not to say which",
        "No, I do not have a disability",
        "I do not want to answer",
    ]
    boxes = [El(t, tag="input", attrs={"type": "checkbox", "name": "dis[]"}) for t in labels]
    form.groups['input[type="checkbox"][name="dis[]"]'] = Many(boxes)
    assert _fill(form, boxes[0], DECLINE)
    assert [b.text for b in boxes if b.checked] == ["I do not want to answer"]


# ---- 5: acknowledgements --------------------------------------------------------------------

ARBITRATE = "I understand and agree to the terms of the Agreement to Arbitrate set forth above."
ACK = Choice(prefer=tuple(ACK_PREFER), acknowledgement=True)


def test_acknowledgement_dropdown_and_select_take_the_affirmative_option() -> None:
    form = Form()
    ctl = form.dropdown([ARBITRATE])
    assert _fill(form, ctl, "Yes", ACK)
    assert _clicked(ctl) == [ARBITRATE] and ctl.value == ""
    sel = El(tag="select", attrs={"_options": f"I do not agree|{ARBITRATE}"})
    assert _fill(form, sel, "Yes", ACK) and sel.selected == ARBITRATE


def test_without_the_entry_a_yes_never_agrees_to_arbitration() -> None:
    form = Form()
    ctl = form.dropdown([ARBITRATE])
    assert not _fill(form, ctl, "Yes")  # no acknowledgement choice: no legal_acknowledgement
    assert _clicked(ctl) == []


def test_acknowledgement_checkbox_is_ticked_through_the_guard() -> None:
    box = El(tag="input", attrs={"type": "checkbox"})
    assert _fill(Form(), box, "Yes", ACK)
    assert box.checks == 1 and box.probed == 1
    refused = El(tag="input", attrs={"type": "checkbox"}, guard="submit-like-name")
    with pytest.raises(ClickRefused):
        _fill(Form(), refused, "Yes", ACK)
    assert refused.checks == 0


# ---- 4: location autocompletes --------------------------------------------------------------

SUGGESTIONS: dict[str, list[str]] = {
    "Bristol, PA": [
        "Bristol, Connecticut, United States",
        "Bristol, Tennessee, United States",
        "Bristol, Pennsylvania, United States",
    ],
    "Bristol, Pennsylvania": ["Bristol, Pennsylvania, United States"],
    "Pennsylvania": [
        "Pennsylvania, United States",
        "Pennsylvania Furnace, Pennsylvania, United States",
    ],
}


def _location(form: Form, suggest: Callable[[str], list[str]]) -> tuple[El, list[El]]:
    ctl = El(tag="input", attrs={"role": "combobox", "aria-autocomplete": "list"})
    made: list[El] = []

    def listing() -> list[El]:
        els = [El(t) for t in suggest(ctl.value)]
        made.extend(els)
        return els

    ctl.on_type = lambda _el: setattr(form, "listbox", listing)
    return ctl, made


LOC = Choice(location=True)


def test_ashby_location_takes_the_same_city_in_the_same_state() -> None:
    form = Form()
    ctl, made = _location(form, lambda q: SUGGESTIONS.get(q, []))
    assert _fill(form, ctl, "Bristol, PA", LOC)
    assert [e.text for e in made if e.clicks] == ["Bristol, Pennsylvania, United States"]
    assert ctl.probed == 1


def test_location_falls_back_to_the_state_never_another_state() -> None:
    """When no suggestion names the city in the right state, the picker takes
    the state itself, never another state's town."""
    only_others = {
        "Bristol, PA": ["Bristol, Connecticut, United States"],
        "Bristol, Pennsylvania": ["Bristol, Virginia, United States"],
        "Pennsylvania": [
            "Pennsylvania Furnace, Pennsylvania, United States",
            "Pennsylvania, United States",
        ],
    }
    form = Form()
    ctl, made = _location(form, lambda q: only_others.get(q, []))
    assert _fill(form, ctl, "Bristol, PA", LOC)
    assert [e.text for e in made if e.clicks] == ["Pennsylvania, United States"]


def test_location_with_no_safe_suggestion_is_left_empty() -> None:
    wrong = ["New Mexico, United States", "New York, United States",
             "Bristol, Connecticut, United States"]  # fmt: skip
    form, tab = Form(), Tab()
    ctl, made = _location(form, lambda q: wrong)
    ok = browser._fill_control(tab, form, ctl, "Bristol, PA", "Location", [], LOC)  # type: ignore[arg-type]
    assert not ok and not any(e.clicks for e in made)
    assert ctl.value == "" and tab.keyboard.pressed == ["Escape"]


def test_location_ignores_loading_rows() -> None:
    form = Form()
    calls = {"n": 0}

    def suggest(q: str) -> list[str]:
        calls["n"] += 1
        return ["Loading..."] if calls["n"] < 3 else SUGGESTIONS.get(q, [])

    ctl, made = _location(form, suggest)
    assert _fill(form, ctl, "Bristol, PA", LOC)
    assert [e.text for e in made if e.clicks] == ["Bristol, Pennsylvania, United States"]


def test_a_plain_location_text_box_is_typed_into() -> None:
    box = El(tag="input", attrs={"type": "text"})
    assert _fill(Form(), box, "Bristol, PA", LOC)
    assert box.value == "Bristol, PA" and box.clicks == 0


# ---- the whole form through stage_job --------------------------------------------------------


def _field(identity: str, label: str, ftype: str = "multi_value_single_select", **kw: Any) -> Any:
    return {"identity": identity, "label": label, "type": ftype, "required": True,
            "match": kw.pop("match", {}), **kw}  # fmt: skip


FIELDS = [
    _field("q_state", "What is your state of residence?", options=STATES[:20]),
    _field("q_country", "Please choose the country in which you are located."),
    _field("_systemfield_location", "Location", "Location"),
    _field("q_work_loc", "Please enter your current working location", "Location"),
    _field("q_arbitrate", "Agreement to Arbitrate", options=[ARBITRATE]),
    _field("q_read", "Please read the arbitration agreement below"),
    _field("q_outbound", OUTBOUND, options=["Yes", "No"],
           match={"kind": "blank_flag", "reason": "never-draft:contact-detail"}),
    _field("q_gender", "How would you describe your gender identity? (mark all that apply)",
           "input_text", compliance_type="eeo", required=False),
    _field("q_trans", "Do you identify as transgender?", "input_text", compliance_type="eeo",
           required=False),
    _field("q_orient", "How would you describe your sexual orientation? (mark all that apply)",
           "input_text", compliance_type="eeo", required=False),
]  # fmt: skip


@pytest.fixture()
def staged_form(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]]]:
    conn.execute(
        "INSERT INTO companies (id, name, normalized_name, domain) "
        "VALUES (1, 'Acme', 'acme', 'acme.com')"
    )
    jid = conn.execute(
        """INSERT INTO jobs (company_id, external_id, title, normalized_title, company_name,
           normalized_company, location_bucket, cluster_key, jd_html, jd_hash, apply_url, status)
           VALUES (1, 'a1', 'Account Executive', 'ae', 'Acme', 'acme', 'remote-us', 'k',
                   'jd', 'h', 'https://jobs.ashbyhq.com/acme/x/application', 'approved')"""
    ).lastrowid
    conn.execute(
        "INSERT INTO form_specs (job_id, source, fields, drafted_answers) "
        "VALUES (?, 'api_questions', ?, '{}')",
        (jid, json.dumps(FIELDS)),
    )
    conn.commit()

    def run(bank: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        (jobkit_home / "data" / "answer_bank.yaml").write_text(yaml.safe_dump(bank))
        answer_bank.load_bank.cache_clear()
        form = Form(ids=tuple(f["identity"] for f in FIELDS))
        controls: dict[str, El] = {
            "q_state": form.dropdown(STATES),
            "q_country": form.dropdown(COUNTRIES, typed_filter=True, shown=30),
            "q_arbitrate": form.dropdown([ARBITRATE]),
            "q_read": form.dropdown(["I will read the arbitration agreement below."]),
            "q_outbound": form.dropdown(["Yes", "No"]),
            "q_trans": form.dropdown(["Yes", "No", "Decline to self-identify"]),
        }
        for ident in ("_systemfield_location", "q_work_loc"):
            controls[ident], made = _location(form, lambda q: SUGGESTIONS.get(q, []))
            controls[ident].options = made  # type: ignore[attr-defined]
        for ident, labels in (
            ("q_gender", ["Man", "Woman", "Non-binary", "I don't wish to answer"]),
            ("q_orient", ["Heterosexual", "Gay", "Bisexual", "Prefer not to say"]),
        ):
            boxes = [El(t, tag="input", attrs={"type": "checkbox", "name": ident}) for t in labels]
            form.groups[f'input[type="checkbox"][name="{ident}"]'] = Many(boxes)
            controls[ident] = boxes[0]
            boxes[0].options = boxes  # type: ignore[attr-defined]
        monkeypatch.setattr(browser, "_find_control", lambda scope, ident, label: controls[ident])
        page = FakePage([form])
        page.keyboard = Keyboard()  # type: ignore[attr-defined]
        matcher = BankMatcher(answer_bank.load_bank(), use_embeddings=False)
        r = browser.stage_job(conn, page, int(jid or 0), matcher=matcher)  # type: ignore[arg-type]
        run_row = conn.execute(
            "SELECT filled_fields, unfilled_fields FROM autofill_runs WHERE job_id=? "
            "ORDER BY id DESC LIMIT 1",
            (jid,),
        ).fetchone()
        filled = {f["identity"]: f["value"] for f in json.loads(run_row[0])}
        unfilled = {u["identity"]: u["reason"] for u in json.loads(run_row[1])}
        picked = {
            ident: [o.text for o in getattr(ctl, "options", []) if o.clicks or o.checked]
            for ident, ctl in controls.items()
        }
        return {"result": r, "filled": filled, "unfilled": unfilled}, picked

    yield run
    answer_bank.load_bank.cache_clear()


def test_the_regression_fields_fill_correctly(staged_form: Any) -> None:
    out, picked = staged_form(BANK)
    assert out["unfilled"] == {}, out
    assert out["result"]["status"] == "staged"
    assert picked == {
        "q_state": ["Pennsylvania"],
        "q_country": ["United States"],
        "_systemfield_location": ["Bristol, Pennsylvania, United States"],
        "q_work_loc": ["Bristol, Pennsylvania, United States"],
        "q_arbitrate": [ARBITRATE],
        "q_read": ["I will read the arbitration agreement below."],
        "q_outbound": ["Yes"],
        "q_trans": ["Decline to self-identify"],
        "q_gender": ["I don't wish to answer"],
        "q_orient": ["Prefer not to say"],
    }
    assert out["filled"]["q_state"] == "Pennsylvania"
    assert out["filled"]["_systemfield_location"] == "Bristol, PA"


def test_without_the_entries_those_fields_stay_blank_and_flagged(staged_form: Any) -> None:
    bare = {
        **_without("legal_acknowledgement", "country_of_residence", "state_of_residence"),
        "eeo": {},
    }
    out, picked = staged_form(bare)
    assert out["unfilled"]["q_arbitrate"] == "legal-acknowledgement-needs-human:covered"
    assert out["unfilled"]["q_read"] == "legal-acknowledgement-needs-human:covered"
    for ident in ("q_gender", "q_trans", "q_orient"):
        assert out["unfilled"][ident] == "eeo-no-stored-choice"
    for ident in ("q_arbitrate", "q_read", "q_gender", "q_trans", "q_orient"):
        assert picked[ident] == [], ident
    assert out["result"]["status"] == "needs_human"


def _no_safe_option_events(home: Path) -> list[dict[str, Any]]:
    log = home / "logs" / "jobkit.jsonl"
    lines = log.read_text().splitlines() if log.exists() else []
    return [e for e in map(json.loads, lines) if e["event"] == "stage.no_safe_option"]


def test_a_miss_is_logged_once_and_only_when_the_field_stays_blank(jobkit_home: Path) -> None:
    form = Form()
    ctl = form.dropdown(COUNTRIES, typed_filter=True, shown=30)
    assert _fill(form, ctl, "United States")  # found after typing: no miss logged
    assert _no_safe_option_events(jobkit_home) == []
    ctl = form.dropdown([s for s in STATES if s != "North Dakota"], typed_filter=True)
    assert not _fill(form, ctl, "North Dakota")
    (event,) = _no_safe_option_events(jobkit_home)
    assert event["control"] == "dropdown" and event["want"] == "North Dakota"


# review 2026-10-10: a bare "No" among two "No, ..." options is ambiguous
APPLIED_BEFORE_OPTIONS = [
    "Yes",
    "No, this is my first time applying to this role",
    "No, I applied to this role more than 6 months ago",
]


def test_an_ambiguous_no_is_left_blank_and_logged_on_every_control(jobkit_home: Path) -> None:
    form = Form()
    sel = El(tag="select", attrs={"_options": "|".join(["Select...", *APPLIED_BEFORE_OPTIONS])})
    assert not _fill(form, sel, "No") and sel.selected is None
    ctl = form.dropdown(APPLIED_BEFORE_OPTIONS)
    assert not _fill(form, ctl, "No") and _clicked(ctl) == []
    radios = [El(t, tag="input", attrs={"aria-label": t}) for t in APPLIED_BEFORE_OPTIONS]
    fieldset = El(tag="fieldset")
    fieldset.group = Many(radios)
    assert not _fill(form, fieldset, "No")
    assert not any(r.checks or r.clicks for r in radios)
    events = _no_safe_option_events(jobkit_home)
    assert events and all(e["want"] == "No" for e in events)
    # select_preference naming one option is how the owner settles it
    pref = Choice(prefer=("No, this is my first time applying to this role",))
    sel = El(tag="select", attrs={"_options": "|".join(APPLIED_BEFORE_OPTIONS)})
    assert _fill(form, sel, "No", pref)
    assert sel.selected == "No, this is my first time applying to this role"


# second review 2026-10-10: the bank's onsite_ok answer took "Yes, 5 days per
# week" on its first word
ONSITE = "Yes — up to 2 days per week in Philadelphia or Bucks County."


def test_a_qualified_yes_is_left_blank_and_logged(jobkit_home: Path) -> None:
    form = Form()
    options = ["Yes, 5 days per week", "No"]
    sel = El(tag="select", attrs={"_options": "|".join(["Select...", *options])})
    assert not _fill(form, sel, ONSITE) and sel.selected is None
    ctl = form.dropdown(["Yes, I can relocate to San Francisco", "No"])
    assert not _fill(form, ctl, ONSITE) and _clicked(ctl) == []
    events = _no_safe_option_events(jobkit_home)
    assert len(events) == 2 and all(e["want"] == ONSITE[:60] for e in events)
    # the bare word still carries it
    ctl = form.dropdown(["Yes", "No"])
    assert _fill(form, ctl, ONSITE) and _clicked(ctl) == ["Yes"]


# ---- 2026-10-10: the auto-agree is an exact-label allowlist -----------------
# Classifying free-text acknowledgements kept admitting compound labels that
# commit the candidate to something else or state a fact. These came from the
# safety review and must never get the automatic "Yes".
COMPOUND_ACKS = [
    "Do you agree to the arbitration agreement and to relocate to New York?",
    "Would you agree to the arbitration agreement and start within two weeks?",
    "Will you agree to our AI policy and use AI in your interviews?",
    "Please read the arbitration agreement and agree to relocate.",
    "I consent to the AI policy and to a drug test.",
    "I agree to the AI policy and I wrote this application myself",
    "I acknowledge the AI policy and I used AI for this application.",
    "I understand and agree that the AI policy applies and that I used AI to prepare "
    "this application",
    "I authorize references to be contacted and I authorize a credit check",
    "Please read the arbitration agreement below and agree to relocate",
]


@pytest.mark.parametrize("label", COMPOUND_ACKS)
def test_compound_acknowledgements_never_auto_agree(label: str) -> None:
    v = _m({"screening": [USER_ACK_ENTRY], "eeo": {}}).match(label, identity="q")
    assert v.get("entry_id") != "legal_acknowledgement"
    assert v.get("kind") == "blank_flag"


@pytest.mark.parametrize("label", USER_ACK_ENTRY["approved_labels"])
def test_approved_labels_auto_agree_word_for_word(label: str) -> None:
    bank = {"screening": [USER_ACK_ENTRY], "eeo": {}}
    assert approved_acknowledgement(bank, label)
    assert approved_acknowledgement(bank, label.upper() + " *")  # case/markers only
    assert _verdict(_m(bank).match(label, identity="q")) == (
        "screening",
        "legal_acknowledgement",
    )


def test_no_allowlist_means_no_auto_agree() -> None:
    entry = {k: v for k, v in USER_ACK_ENTRY.items() if k != "approved_labels"}
    bank = {"screening": [entry], "eeo": {}}
    for label in USER_ACK_ENTRY["approved_labels"]:
        assert not approved_acknowledgement(bank, label)
        assert _m(bank).match(label, identity="q").get("kind") == "blank_flag"


# ---- 2026-10-10 review: no side doors around the allowlist -----------------
BUNDLED_ACKS = [
    "I agree to the arbitration agreement and confirm I am at least 18",
    "I acknowledge the AI policy and confirm I am 18 years or older",
    "By checking this box I agree to the arbitration agreement and certify I am at least "
    "18 years of age",
    "Agreement to Arbitrate - are you at least 18?",
    "Do you consent to a background check? By checking yes you also agree to binding arbitration.",
    "I agree to the Agreement to Arbitrate. Are you legally authorized to work in the "
    "United States?",
    "I consent to the privacy policy and confirm I am able to work onsite in person",
]
SIDE_BANK: dict[str, Any] = {
    "screening": [
        USER_ACK_ENTRY,
        {"id": "age_18", "aliases": ["at least 18", "18 years or older"], "answer": "Yes"},
        {
            "id": "background_check",
            "aliases": ["Do you consent to a background check?"],
            "answer": "Yes",
        },
        {
            "id": "work_auth",
            "aliases": ["Are you legally authorized to work in the United States?"],
            "answer": "Yes",
        },
        {"id": "onsite_ok", "aliases": ["work onsite", "in person"], "answer": "Yes"},
    ],
    "eeo": {},
}


@pytest.mark.parametrize("label", BUNDLED_ACKS)
def test_an_acknowledgement_never_borrows_another_entrys_yes(label: str) -> None:
    v = _m(SIDE_BANK).match(label, identity="q")
    assert v.get("kind") == "blank_flag", v


def test_an_entrys_own_exact_alias_still_answers() -> None:
    v = _m(SIDE_BANK).match("Do you consent to a background check?", identity="q")
    assert _verdict(v) == ("screening", "background_check")


@pytest.mark.parametrize(
    "label", ["Class action waiver", "Electronic signature", "Candidate consent"]
)
def test_unrecognised_agreement_labels_are_never_drafted(label: str) -> None:
    assert never_draft(label) == "legal-acknowledgement"


# ---- 2026-10-11: the exact allowlist is the single criterion ----------------
CERTIFY = (
    "I certify that the information provided in this application is true and complete "
    "to the best of my knowledge."
)


def test_an_approved_label_auto_agrees_even_if_the_classifier_says_other() -> None:
    entry = {**USER_ACK_ENTRY, "approved_labels": [*USER_ACK_ENTRY["approved_labels"], CERTIFY]}
    bank = {"screening": [entry], "eeo": {}}
    assert legal_acknowledgement(CERTIFY) == "other"
    assert _verdict(_m(bank).match(CERTIFY, identity="q")) == ("screening", "legal_acknowledgement")


@pytest.mark.parametrize(
    "label",
    [
        CERTIFY.replace("true and complete", "true"),
        CERTIFY + " I also confirm I have never been terminated.",
        "I certify that I have never been terminated.",
    ],
)
def test_a_near_miss_of_an_approved_label_goes_to_the_human(label: str) -> None:
    entry = {**USER_ACK_ENTRY, "approved_labels": [*USER_ACK_ENTRY["approved_labels"], CERTIFY]}
    v = _m({"screening": [entry], "eeo": {}}).match(label, identity="q")
    assert v.get("entry_id") != "legal_acknowledgement"
