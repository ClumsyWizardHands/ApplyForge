"""Answer-bank matcher regressions (audit 2026-10-08 §2 item 4), built from
real form labels captured in form_specs. Offline, alias-only (no embeddings).

The failure being guarded: unanchored identity regexes ran before the
screening aliases, so any question that MENTIONED email/country/location got
the identity value typed in and outlined green as trusted — the candidate's own email
into one form's former-employee field and another's referrer field, a country
into the visa-sponsorship question, the home town into "open to relocation?"."""

from __future__ import annotations

from typing import Any

import pytest

from jobkit.autofill.answer_bank import (
    NO_JOB_CONTEXT,
    BankMatcher,
    comp_expectation_block,
    human_answer_barred,
    is_field_like,
    never_draft,
)
from jobkit.autofill.browser import _identity_value, _value_for, staging_match
from jobkit.config import Filters

# aliases as in data/answer_bank.example.yaml; no personal values
BANK: dict[str, Any] = {
    "match_threshold": 0.82,
    "identity": {
        "first_name": "Pat",
        "last_name": "Doe",
        "email": "pat@example.com",
        "phone": "+1 555 0100",
        "city": "Bristol",
        "state": "PA",
        "country": "United States",
        "linkedin": "https://linkedin.com/in/pat",
    },
    "screening": [
        {
            "id": "work_auth",
            "aliases": [
                "Are you legally authorized to work in the United States?",
                "Do you have the right to work in the country of employment?",
                "Work authorization status",
            ],
            "answer": "Yes",
        },
        {
            "id": "sponsorship",
            "aliases": [
                "Will you now or in the future require sponsorship?",
                "Do you require visa sponsorship?",
                "Sponsorship required",
            ],
            "answer": "No",
        },
        {
            "id": "comp_expectation",
            "aliases": [
                "What are your salary expectations?",
                "Desired compensation",
                "Expected base salary",
            ],
            "strategy": "match_posted_range_upper_third",
            "floor": 205000,
            "answer": "$205,000",
        },
        {
            "id": "salary_history",
            "aliases": ["Current salary", "Salary history", "What do you currently earn?"],
            "answer": "",
            "action": "leave_blank_and_flag",
        },
        {"id": "relocation", "aliases": ["Are you willing to relocate?", "Open to relocation"]},
        {
            "id": "previously_employed",
            "aliases": ["Have you previously worked for", "Are you a former employee?"],
            "answer": "No",
        },
        {"id": "referral_source", "aliases": ["How did you hear about us?"]},
    ],
    "scaffolds": [{"id": "why_company", "aliases": ["Why do you want to work here?"]}],
    "eeo": {"gender": None},
}

FORMER_EMPLOYEE_EMAIL = (
    "If you were previously employed by Cyberdyne, please share the email you used for "
    "signing in. If not, please put N/A"
)
REFERRAL_EMAIL_LABEL = (
    "Has someone at Wonka or Tyrell referred you for this role? If so, please include "
    "their email here"
)
VISA_SPONSORSHIP = (
    "Will you now or will you in the future require employment visa sponsorship to work in "
    "the country in which the job you're applying for is located?"
)
RELOCATION = "Are you open to relocation for this role? "
OTE_EXPECTATIONS = "Please state your OTE expectations"


def _m(bank: dict[str, Any] | None = None) -> BankMatcher:
    return BankMatcher(bank=bank or BANK, use_embeddings=False)


# ---- the five labels the audit named ----------------------------------------


def test_remote_former_employee_email_is_never_identity() -> None:
    v = _m().match(FORMER_EMPLOYEE_EMAIL, identity="question_31708769003")
    assert v["kind"] != "identity", v
    assert v.get("entry_id") != "email"
    # and it is never drafted either: an email is a fact the corpus never holds
    assert never_draft(FORMER_EMPLOYEE_EMAIL) == "contact-detail"


def test_referrer_email_is_never_identity() -> None:
    m = _m()
    for label in (REFERRAL_EMAIL_LABEL, "Referrer email (if referred)", "Referrer's email"):
        v = m.match(label, identity="0aa284e0-cef0-4f39-bcd7-c80305ef81a6")
        assert v["kind"] != "identity", (label, v)
    assert never_draft(REFERRAL_EMAIL_LABEL) == "contact-detail"
    assert never_draft("Who referred you to this role?") == "referral"


def test_visa_sponsorship_question_is_never_a_country() -> None:
    v = _m().match(VISA_SPONSORSHIP, identity="question_15527759008")
    assert v["kind"] != "identity", v
    # screening runs first: a bank alias for it wins over the word "country"
    bank = {**BANK, "screening": [*BANK["screening"]]}
    bank["screening"][1] = {
        **bank["screening"][1],
        "aliases": [*bank["screening"][1]["aliases"], "require employment visa sponsorship"],
    }
    v = _m(bank).match(VISA_SPONSORSHIP)
    assert (v["kind"], v["entry_id"]) == ("screening", "sponsorship")


def test_open_to_relocation_is_the_relocation_answer_not_an_address() -> None:
    v = _m().match(RELOCATION, identity="question_15515720008")
    assert (v["kind"], v["entry_id"]) == ("screening", "relocation")
    no_alias = {**BANK, "screening": [s for s in BANK["screening"] if s["id"] != "relocation"]}
    v = _m(no_alias).match("Are you open to relocation?")
    assert v["kind"] == "novel", "'relocation' contains 'location' — never identity.address"


def test_ote_expectations_go_to_comp_expectation_or_the_human() -> None:
    m = _m()
    # no job context: blank + flag (the default fails closed)
    v = m.match(OTE_EXPECTATIONS)
    assert v == {"kind": "blank_flag", "reason": NO_JOB_CONTEXT}
    # a track the bank figure does not fit: blank + flag with the reason
    v = m.match(OTE_EXPECTATIONS, comp_expectation_block="comp-expectation-needs-human:x")
    assert v == {"kind": "blank_flag", "reason": "comp-expectation-needs-human:x"}
    # a track it fits: the bank's comp_expectation entry
    v = m.match(OTE_EXPECTATIONS, comp_expectation_block=None)
    assert (v["kind"], v["entry_id"]) == ("screening", "comp_expectation")
    # never handed to the novel-question drafter
    assert never_draft(OTE_EXPECTATIONS) == "compensation-related"


# ---- the other probes from the audit, and more real labels -----------------

QUESTION_LABELS = [
    "Please state why you are interested in this role",
    "Is this your first sales role in the last 5 years?",
    "How did you hear about this job? (LinkedIn, Indeed, referral)",
    "Do you have experience selling mobile solutions?",
    "Are you willing to work from our NYC office location?",
    "Describe your first and last quota attainment",
    "What is your current country of residence?",
    "Please choose the country in which you are located.",
    "What is the zip code of your primary residence?",
    "What is your state of residence?",
    "Will you now or in the future require sponsorship for a visa to remain in your current "
    "location?",
    "Work eligibility is extremely important. Are you legally eligible to work in the country "
    "where you’re planning to work?",
    "What is the address from which you plan on working? If you would need to relocate, please "
    'type "relocating".',
    "This role is hybrid and based at the office named in the posting.",
    "Are you based in San Francisco, Austin or New York City?",
    FORMER_EMPLOYEE_EMAIL,
    REFERRAL_EMAIL_LABEL,
    VISA_SPONSORSHIP,
    RELOCATION,
]


@pytest.mark.parametrize("label", QUESTION_LABELS)
def test_question_shaped_labels_never_match_identity(label: str) -> None:
    assert not is_field_like(label)
    v = _m().match(label, identity="question_1")
    assert v["kind"] not in ("identity", "attach"), (label, v)


FIELD_LABELS: list[tuple[str, str | None, str]] = [
    ("First Name", "first_name", "identity.first_name"),
    ("Preferred First Name", "7c3610a2-745f-4bd7-927a-d8e97e80472c", "identity.first_name"),
    ("Last Name", "last_name", "identity.last_name"),
    ("Preferred Last Name", "question_64097448", "identity.last_name"),
    ("Name", "_systemfield_name", "identity.name"),  # Ashby: was "novel" (audit)
    ("Name", None, "identity.name"),
    ("Full name ✱", "name", "identity.name"),
    ("Legal First and Last Name", "_systemfield_name", "identity.name"),
    ("Name (first and last)", "q1", "identity.name"),
    ("Email", "_systemfield_email", "identity.email"),
    ("Email ✱", "email", "identity.email"),
    ("Phone Number", "00fe0976-a813-426e-92fb-df070b433e5f", "identity.phone"),
    ("LinkedIn Profile URL", "8214030a", "identity.linkedin"),
    ("Link to your LinkedIn profile", "c4f15582", "identity.linkedin"),
    ("Linkedin Profile\xa0", "b105e23c", "identity.linkedin"),
    ("LinkedIn URL ✱", "urls[LinkedIn]", "identity.linkedin"),
    ("GitHub URL", "urls[GitHub]", "identity.github"),
    ("GitHub Profile", "78a43fa2", "identity.github"),
    ("Github", "question_55120101004", "identity.github"),
    ("Link to your Github profile", "e4a64e8d", "identity.github"),
    ("Portfolio / Github URL", "1230e70e", "identity.portfolio_or_github"),
    ("Personal Website/Portfolio", "f4883bf8", "identity.portfolio"),
    ("Website", "question_17060082008", "identity.portfolio"),
    ("Portfolio URL", "urls[Portfolio]", "identity.portfolio"),
    ("Twitter URL", "urls[Twitter]", "identity.twitter"),
    ("Other website", "urls[Other]", "blank_flag.other-url-needs-human"),
    ("Other website", "a-uuid", "blank_flag.other-url-needs-human"),
    ("Resume/CV ✱", "resume", "attach.resume"),
    ("Resume", "_systemfield_resume", "attach.resume"),
    ("Cover Letter", "cover_letter", "attach.cover_letter"),
    ("Location", "_systemfield_location", "identity.address"),
    ("Location", "0a131ea7", "identity.address"),
    ("Current location ✱", "location", "identity.address"),
    ("City", "q", "identity.city"),
    ("State", "q", "identity.state"),
    ("Country", "q", "identity.country"),
    ("ZIP / Postal Code", "q", "identity.postal_code"),
]


@pytest.mark.parametrize(("label", "identity", "want"), FIELD_LABELS)
def test_field_labels_and_system_ids_map_to_identity(
    label: str, identity: str | None, want: str
) -> None:
    v = _m().match(label, identity=identity)
    got = f"{v['kind']}.{v.get('entry_id') or v.get('reason')}"
    assert got == want, (label, identity, v)


# ---- parentheticals (review 2026-10-08) --------------------------------------
#
# is_field_like checked "?" and the word count only AFTER every parenthetical
# was dropped, so a qualifier in parentheses was never seen and the label got
# the trusted identity fill: the candidate's email or name typed into a referrer or
# former-employee field.

QUALIFIED_LABELS = [
    "Location (would you relocate?)",
    "Email (the one you used when previously employed here)",
    "Name (of the employee who referred you)",
    "Email (if referred, the referrer's)",
    "Phone (referrer)",
    "Email (if referred (optional))",
    "Name (referrer)",
    "LinkedIn (of your referrer)",
]


@pytest.mark.parametrize("label", QUALIFIED_LABELS)
def test_a_qualifier_in_parentheses_is_never_identity(label: str) -> None:
    for ident in ("question_1", None):
        v = _m().match(label, identity=ident)
        assert v["kind"] != "identity", (label, v)


def test_a_question_mark_in_parentheses_is_a_question() -> None:
    assert not is_field_like("Location (would you relocate?)")
    assert not is_field_like("Email (do you have one?)")


def test_qualified_contact_fields_are_never_drafted() -> None:
    """Not identity, and not for the drafter either: a referrer's or a former
    employee's contact detail is a fact the corpus never holds."""
    assert never_draft("Email (if referred, the referrer's)") == "contact-detail"
    assert never_draft("Email (the one you used when previously employed here)") == (
        "contact-detail"
    )
    assert never_draft("Phone (referrer)") == "referral"
    assert never_draft("Name (of the employee who referred you)") == "referral"
    f = {"identity": "q", "label": "Phone (referrer)", "match": {"kind": "novel"}}
    assert staging_match(_m(), f, None) == {"kind": "blank_flag", "reason": "never-draft:referral"}


HARMLESS_PAREN_LABELS: list[tuple[str, str]] = [
    ("Name (first and last)", "identity.name"),
    ("Full Name (First, Last)", "identity.name"),
    ("Email (optional)", "identity.email"),
    ("Email (required)", "identity.email"),
    ("Email (e.g. name@example.com)", "identity.email"),
    ("Phone (with country code)", "identity.phone"),
    ("Location (City, State)", "identity.address"),
    ("Location (City / State / Country)", "identity.address"),
    ("LinkedIn Profile (https://linkedin.com/in/...)", "identity.linkedin"),
    ("LinkedIn URL (e.g. linkedin.com/in/yourname)", "identity.linkedin"),
    ("Website (if any)", "identity.portfolio"),
    ("Resume (PDF)", "attach.resume"),
]


@pytest.mark.parametrize(("label", "want"), HARMLESS_PAREN_LABELS)
def test_format_hints_and_required_markers_keep_the_field(label: str, want: str) -> None:
    v = _m().match(label, identity="q1")
    got = f"{v['kind']}.{v.get('entry_id') or v.get('reason') or ''}"
    assert got == want, (label, v)


def test_one_word_field_is_not_stolen_by_a_long_alias() -> None:
    # screening runs first now: "Country" must not hit the work_auth alias
    # "Do you have the right to work in the country of employment?"
    v = _m().match("Country")
    assert (v["kind"], v["entry_id"]) == ("identity", "country")


def test_url_fields_never_receive_the_linkedin_url() -> None:
    """LinkedIn was typed into GitHub/Portfolio/Other fields on 7 submitted
    applications (browser.py:58 fallback). Unset link keys stay blank."""
    m = _m()
    for label, ident in (
        ("GitHub URL", "urls[GitHub]"),
        ("Portfolio URL", "urls[Portfolio]"),
        ("Other website", "urls[Other]"),
        ("Portfolio / Github URL", "x"),
        ("Personal Website/Portfolio", "x"),
        ("Link to your Github profile", "x"),
        ("Website", "x"),
        ("Twitter", "x"),
    ):
        v = m.match(label, identity=ident)
        value, _ = _value_for(v, BANK, {}, ident)
        assert value is None, (label, v, value)
    assert _identity_value("portfolio", BANK) is None
    assert _identity_value("github", BANK) is None
    with_links = {
        **BANK,
        "identity": {**BANK["identity"], "github": "https://github.com/pat"},
    }
    assert _identity_value("github", with_links) == "https://github.com/pat"
    assert _identity_value("portfolio_or_github", with_links) == "https://github.com/pat"
    assert _identity_value("portfolio", with_links) is None


def test_comp_history_never_answered() -> None:
    m = _m()
    for label in (
        "What is your current salary?",
        "What is your current base salary?",
        "What is your current OTE?",
        "Most recent compensation (base + bonus)",
        "How much do you currently earn?",
    ):
        v = m.match(label, comp_expectation_block=None)
        assert v == {"kind": "blank_flag", "reason": "salary-history-never-answer"}, label
    # "learned" is not "earn": a referral-source question is not compensation
    assert never_draft("Where have you learned about Initech? Select all that apply.") is None


# ---- comp expectation: the bank's one figure only where it fits ------------

FILTERS = Filters(
    raw={
        "filters": {
            "tracks": [
                {
                    "id": "sdr_bdr_manager",
                    "title_regex": r"(?i)\b(sdr|bdr|sales development)\b.{0,25}\b(manager|lead)\b",
                },
                {"id": "sales_exec", "title_regex": r"(?i)\b(director|head|vp)\b.{0,30}\bsales\b"},
                {"id": "sdr_bdr", "title_regex": r"(?i)\b(sdr|bdr|sales development)\b"},
                {"id": "account_executive", "title_regex": r"(?i)\baccount executive\b"},
            ],
            "comp": {
                "floor": 165000,
                "track_floors": {
                    "sdr_bdr": {"floor": 75000, "basis": "ote"},
                    "sdr_bdr_manager": {"floor": 110000, "basis": "base"},
                    "account_executive": {"floor": 140000, "basis": "ote"},
                    "sales_exec": {"floor": 190000, "basis": "base"},
                },
                "ote_base_share": 0.6,
            },
        }
    }
)


@pytest.mark.parametrize(
    ("title", "track"),
    [
        ("Sales Development Representative", "sdr_bdr"),
        ("SDR Manager, Enterprise", "sdr_bdr_manager"),
        ("Enterprise Account Executive", "account_executive"),
    ],
)
def test_low_floor_tracks_never_get_the_bank_figure(title: str, track: str) -> None:
    block = comp_expectation_block(title, None, BANK, FILTERS)
    assert block is not None and f"track-{track}" in block
    v = _m().match("What are your salary expectations?", comp_expectation_block=block)
    assert v["kind"] == "blank_flag"
    # and at staging time, whatever the stored verdict said
    stale = {"identity": "q", "label": OTE_EXPECTATIONS, "match": {"kind": "novel"}}
    fresh = staging_match(_m(), stale, block)
    assert fresh["kind"] == "blank_flag"
    assert _value_for(fresh, BANK, {"q": "$205,000"}, "q")[0] is None


def test_exec_and_default_tracks_may_use_the_bank_entry() -> None:
    assert comp_expectation_block("VP of Sales", None, BANK, FILTERS) is None
    assert comp_expectation_block("Deal Desk Manager", None, BANK, FILTERS) is None
    # but never above a posted ceiling, and never without config
    assert "posted-max" in str(comp_expectation_block("VP of Sales", 160000, BANK, FILTERS))
    assert comp_expectation_block("VP of Sales", None, BANK, None) is not None


# ---- staging re-match --------------------------------------------------------


def test_staging_rematches_stale_verdicts() -> None:
    m = _m()
    # frozen at capture: full name into "Preferred First Name"
    f = {"identity": "7c36", "label": "Preferred First Name", "match": {"kind": "identity",
         "entry_id": "name"}}  # fmt: skip
    assert staging_match(m, f, None)["entry_id"] == "first_name"
    # frozen at capture: Ashby's "Name" left novel
    f = {"identity": "_systemfield_name", "label": "Name", "match": {"kind": "novel"}}
    assert staging_match(m, f, None)["entry_id"] == "name"
    # frozen at capture: the candidate's email into a former-employee question
    f = {
        "identity": "question_31708769003",
        "label": FORMER_EMPLOYEE_EMAIL,
        "match": {"kind": "identity", "entry_id": "email"},
    }
    v = staging_match(m, f, None)
    assert v == {"kind": "blank_flag", "reason": "never-draft:contact-detail"}
    # a capture-time never-draft flag stands
    f = {
        "identity": "q",
        "label": "Tell us something unusual",
        "match": {"kind": "blank_flag", "reason": "never-draft:credential"},
    }
    assert staging_match(m, f, None)["kind"] == "blank_flag"
    # an EEO field is still answered only from the eeo block
    f = {"identity": "g", "label": "Gender", "compliance_type": "eeo", "match": {"kind": "novel"}}
    assert staging_match(m, f, None) == {"kind": "blank_flag", "reason": "eeo-no-stored-choice"}


# ---- human answers (MCP answer_novel) at staging (review 2026-10-08) ----------
#
# answer_novel stores the human's answer in form_specs.drafted_answers, next to
# the AI drafts. Re-matching at staging blanked it whenever never_draft or the
# salary-expectation gate fired, so a referrer email or a salary expectation
# the human typed was dropped. A human answer is not a draft: only the EEO and
# comp-history rules (hard rules 4 and 5) still keep it off the form.

REFERRER_EMAIL = {
    "identity": "q9",
    "label": "Referrer email (if referred)",
    "match": {"kind": "novel"},
}


def test_human_answer_fills_a_field_the_ai_may_never_draft() -> None:
    m = _m()
    drafted = {"q9": "jane@acme.com"}
    # the same value as an AI draft stays off the form
    v = staging_match(m, REFERRER_EMAIL, None)
    assert v == {"kind": "blank_flag", "reason": "never-draft:contact-detail"}
    assert _value_for(v, BANK, drafted, "q9") == (None, "unfilled")
    # the human's own answer goes on, marked as theirs
    v = staging_match(m, REFERRER_EMAIL, None, {"q9"})
    assert v == {"kind": "human", "source": "human_typed"}
    assert _value_for(v, BANK, drafted, "q9") == ("jane@acme.com", "human_typed")
    # a never-draft flag stored at capture does not block the human either
    flagged = {**REFERRER_EMAIL, "match": {"kind": "blank_flag", "reason": "never-draft:referral"}}
    assert staging_match(m, flagged, None, {"q9"})["kind"] == "human"
    # the referrer question, which an AI must never answer
    f = {"identity": "q_ref", "label": REFERRAL_EMAIL_LABEL, "match": {"kind": "novel"}}
    assert staging_match(m, f, None)["kind"] == "blank_flag"
    assert staging_match(m, f, None, {"q_ref"})["kind"] == "human"
    # an answer to another field is no answer for this one
    assert staging_match(m, REFERRER_EMAIL, None, {"q8"})["kind"] == "blank_flag"
    # a human answer with nothing stored is no value, never a guess
    v = staging_match(m, REFERRER_EMAIL, None, {"q9"})
    assert _value_for(v, BANK, {}, "q9") == (None, "unfilled")


def test_human_salary_expectation_passes_the_comp_gate() -> None:
    block = comp_expectation_block("Enterprise Account Executive", None, BANK, FILTERS)
    assert block is not None
    f = {"identity": "q10", "label": "Desired compensation", "match": {"kind": "novel"}}
    # the bank's one figure is blocked for this track, and the AI never drafts it
    assert staging_match(_m(), f, block)["kind"] == "blank_flag"
    v = staging_match(_m(), f, block, {"q10"})
    assert _value_for(v, BANK, {"q10": "$140,000 OTE"}, "q10") == ("$140,000 OTE", "human_typed")
    # no comp_expectation entry in the bank at all
    bare = {**BANK, "screening": [s for s in BANK["screening"] if s["id"] != "comp_expectation"]}
    assert staging_match(_m(bare), f, None)["reason"] == "comp-expectation-not-in-bank"
    assert staging_match(_m(bare), f, None, {"q10"})["kind"] == "human"
    # the human's answer for this job outranks the bank's figure too
    assert staging_match(_m(), f, None)["entry_id"] == "comp_expectation"
    assert staging_match(_m(), f, None, {"q10"})["kind"] == "human"


@pytest.mark.parametrize(
    "label",
    [
        "What is your current base salary?",
        "Most recent compensation (base + bonus)",
        "How much do you currently earn?",
        "Salary history",
    ],
)
def test_human_answer_never_reaches_a_comp_history_field(label: str) -> None:
    """Hard rule 5 holds whoever answered."""
    f = {"identity": "h", "label": label, "match": {"kind": "novel"}}
    v = staging_match(_m(), f, None, {"h"})
    assert v == {"kind": "blank_flag", "reason": "salary-history-never-answer"}
    assert _value_for(v, BANK, {"h": "$140,000"}, "h")[0] is None


def test_human_answer_never_selects_an_eeo_field() -> None:
    """Hard rule 4: only the bank's eeo block selects an EEO answer."""
    m = _m()
    for f in (
        {"identity": "g", "label": "Gender", "compliance_type": "eeo", "match": {"kind": "novel"}},
        {"identity": "g", "label": "Gender identity", "match": {"kind": "novel"}},
        {"identity": "g", "label": "Are you a protected veteran?", "match": {"kind": "novel"}},
    ):
        v = staging_match(m, f, None, {"g"})
        assert v["kind"] == "blank_flag", f
        assert _value_for(v, BANK, {"g": "Male"}, "g")[0] is None
    with_choice = {**BANK, "eeo": {"gender": "Decline to self-identify"}}
    f = {"identity": "g", "label": "Gender", "compliance_type": "eeo", "match": {"kind": "novel"}}
    v = staging_match(_m(with_choice), f, None, {"g"})
    assert _value_for(v, with_choice, {"g": "Male"}, "g") == (
        "Decline to self-identify",
        "answer_bank",
    )


def test_human_answer_on_an_upload_field_keeps_the_upload() -> None:
    f = {"identity": "resume", "label": "Resume/CV", "match": {"kind": "attach"}}
    assert staging_match(_m(), f, None, {"resume"})["kind"] == "attach"


def test_human_answer_barred_matches_the_matcher() -> None:
    assert human_answer_barred("Gender", "eeo") == "eeo-answer-bank-only"
    assert human_answer_barred("Race / Ethnicity") == "eeo-answer-bank-only"
    assert human_answer_barred("What is your current OTE?") == "salary-history-never-answer"
    for label in ("Desired compensation", "Referrer email", OTE_EXPECTATIONS, "Why Wonka?"):
        assert human_answer_barred(label) is None, label
