"""Option choice (autofill/options.py) — form-fill regressions.
Offline, pure functions, real-looking option lists.

A rule that took any option whose first word matched ("north") would answer
"North Dakota" with "North Carolina" on "What is your state of residence?". An
answer lands on an exact or known-equivalent option, a decline lands on
the form's decline option, and anything else either matches safely or is left
blank and flagged. A near miss is never filled."""

from __future__ import annotations

import pytest

from jobkit.autofill.options import (
    US_STATES,
    canonical_place,
    choose_location,
    choose_option,
    is_decline,
    location_queries,
    mentions_decline,
    parse_location,
    pick_option,
    search_text,
)

# Greenhouse order: the 50 states and DC, alphabetical
STATES = sorted(name for code, name in US_STATES.items() if code != "PR")

# a Greenhouse-style country list (ISO names), as Lumon renders it
COUNTRIES = [
    "Afghanistan", "Åland Islands", "Albania", "Algeria", "American Samoa", "Andorra", "Angola",
    "Anguilla", "Antarctica", "Antigua and Barbuda", "Argentina", "Armenia", "Aruba",
    "Australia", "Austria", "Azerbaijan", "Bahamas", "Bahrain", "Bangladesh", "Barbados",
    "Belarus", "Belgium", "Belize", "Benin", "Bermuda", "Bhutan", "Bolivia",
    "Bosnia and Herzegovina", "Botswana", "Brazil", "British Indian Ocean Territory",
    "Brunei Darussalam", "Bulgaria", "Burkina Faso", "Burundi", "Cambodia", "Cameroon",
    "Canada", "Cape Verde", "Cayman Islands", "Central African Republic", "Chad", "Chile",
    "China", "Colombia", "Comoros", "Congo", "Costa Rica", "Croatia", "Cuba", "Cyprus",
    "Czech Republic", "Denmark", "Djibouti", "Dominica", "Dominican Republic", "Ecuador",
    "Egypt", "El Salvador", "Estonia", "Ethiopia", "Fiji", "Finland", "France", "Gabon",
    "Gambia", "Georgia", "Germany", "Ghana", "Greece", "Grenada", "Guam", "Guatemala",
    "Guinea", "Guinea-Bissau", "Guyana", "Haiti", "Honduras", "Hong Kong", "Hungary",
    "Iceland", "India", "Indonesia", "Iran", "Iraq", "Ireland", "Isle of Man", "Israel",
    "Italy", "Jamaica", "Japan", "Jersey", "Jordan", "Kazakhstan", "Kenya", "Korea, Republic of",
    "Kuwait", "Latvia", "Lebanon", "Liechtenstein", "Lithuania", "Luxembourg", "Malaysia",
    "Malta", "Mexico", "Moldova", "Monaco", "Mongolia", "Montenegro", "Morocco", "Nepal",
    "Netherlands", "New Caledonia", "New Zealand", "Nicaragua", "Nigeria", "North Macedonia",
    "Norway", "Oman", "Pakistan", "Panama", "Paraguay", "Peru", "Philippines", "Poland",
    "Portugal", "Puerto Rico", "Qatar", "Romania", "Russian Federation", "Rwanda",
    "Saudi Arabia", "Senegal", "Serbia", "Singapore", "Slovakia", "Slovenia", "South Africa",
    "Spain", "Sri Lanka", "Sweden", "Switzerland", "Taiwan", "Thailand", "Tunisia", "Turkey",
    "Uganda", "Ukraine", "United Arab Emirates", "United Kingdom", "United States",
    "United States Minor Outlying Islands", "Uruguay", "Uzbekistan", "Venezuela", "Viet Nam",
    "Virgin Islands, British", "Virgin Islands, U.S.", "Zambia", "Zimbabwe",
]  # fmt: skip


# ---- states ------------------------------------------------------------------------


def test_north_dakota_is_north_dakota_not_north_carolina() -> None:
    """North Dakota sits right after North Carolina in an alphabetical list."""
    assert STATES.index("North Dakota") == STATES.index("North Carolina") + 1
    assert pick_option("North Dakota", STATES) == "North Dakota"
    assert pick_option("ND", STATES) == "North Dakota"  # identity.state is a code


@pytest.mark.parametrize("code", sorted(c for c in US_STATES if c != "PR"))
def test_every_state_picks_itself_by_name_and_code(code: str) -> None:
    name = US_STATES[code]
    assert pick_option(name, STATES) == name
    assert pick_option(code, STATES) == name
    assert pick_option(name.upper(), STATES) == name


@pytest.mark.parametrize("missing", ["New Jersey", "New York", "North Carolina", "Virginia"])
def test_a_state_not_in_the_list_is_left_blank(missing: str) -> None:
    """Never the neighbor that shares a word: New Hampshire, New Mexico, North
    Dakota, West Virginia."""
    without = [s for s in STATES if s != missing]
    assert pick_option(missing, without) is None
    neighbors = ["New Hampshire", "New Mexico", "New York", "North Dakota", "West Virginia",
                 "South Carolina", "Jersey"]  # fmt: skip
    assert pick_option(missing, [n for n in neighbors if n != missing]) is None


@pytest.mark.parametrize(
    ("options", "want"),
    [
        ([f"{n} ({c})" for c, n in US_STATES.items()], "Pennsylvania (PA)"),
        ([f"{c} - {n}" for c, n in US_STATES.items()], "PA - Pennsylvania"),
        (sorted(US_STATES), "PA"),
        ([f"{n}, USA" for n in STATES], "Pennsylvania, USA"),
    ],
    ids=["name (code)", "code - name", "codes only", "name, USA"],
)
def test_state_lists_in_other_formats(options: list[str], want: str) -> None:
    assert pick_option("Pennsylvania", options) == want
    assert pick_option("PA", options) == want


def test_lowercase_state_codes_are_words_not_states() -> None:
    assert canonical_place("or") is None and canonical_place("in") is None
    assert canonical_place("OR") == "state:OR"
    assert pick_option("Oregon", ["in", "or", "me", "Oregon"]) == "Oregon"


# ---- countries --------------------------------------------------------------------


@pytest.mark.parametrize(
    "us",
    ["United States", "United States of America", "USA", "US", "U.S.", "U.S.A.",
     "United States (US)", "United States of America (USA)"],
)  # fmt: skip
def test_united_states_in_any_spelling(us: str) -> None:
    options = [c for c in COUNTRIES if c != "United States"] + [us]
    options.sort()
    for want in ("United States", "United States of America", "USA", "US"):
        assert pick_option(want, options) == us, (want, us)


def test_united_states_never_lands_on_a_territory_or_the_uk() -> None:
    no_us = [c for c in COUNTRIES if c != "United States"]
    assert pick_option("United States", no_us) is None
    assert pick_option("USA", no_us) is None
    # the exact entry wins over its longer neighbors
    assert pick_option("United States", COUNTRIES) == "United States"


def test_search_text_types_the_full_name() -> None:
    assert search_text("PA") == "Pennsylvania"
    assert search_text("USA") == "United States"
    assert search_text("Company careers page") == "Company careers page"


# ---- EEO: the stored answer is a decline ---------------------------------------------

DECLINE = "I don't wish to answer"

EEO_OPTION_SETS = [
    # Greenhouse gender / race
    (["Male", "Female", "Decline To Self Identify"], "Decline To Self Identify"),
    (
        ["Hispanic or Latino", "White (Not Hispanic or Latino)", "Asian (Not Hispanic or Latino)",
         "Two or More Races (Not Hispanic or Latino)", "Decline To Self Identify"],
        "Decline To Self Identify",
    ),
    # VEVRAA veteran status
    (
        ["I am a veteran", "I identify as one or more of the classifications of protected veteran",
         "I am not a protected veteran", "I don't wish to answer"],
        "I don't wish to answer",
    ),
    # CC-305 disability
    (
        ["Yes, I have a disability (or previously had a disability)",
         "No, I do not have a disability and have not had one in the past",
         "I do not want to answer"],
        "I do not want to answer",
    ),
    # Contoso-style "mark all that apply"
    (["Man", "Woman", "Non-binary", "I prefer to self-describe", "I prefer not to say"],
     "I prefer not to say"),
    (["Yes", "No", "I choose not to disclose"], "I choose not to disclose"),
    (["Heterosexual", "Gay", "Lesbian", "Bisexual", "Prefer not to answer"],
     "Prefer not to answer"),
    (["Yes", "No", "Decline to state"], "Decline to state"),
    (["Yes", "No", "I'd rather not say"], "I'd rather not say"),
    (["Yes", "No", "I don’t wish to answer"], "I don’t wish to answer"),
]  # fmt: skip


@pytest.mark.parametrize(("options", "want"), EEO_OPTION_SETS)
def test_a_stored_decline_picks_the_forms_decline_option(options: list[str], want: str) -> None:
    assert pick_option(DECLINE, options) == want
    assert pick_option("Decline to self-identify", options) == want


def test_a_decline_with_no_decline_option_is_left_blank() -> None:
    assert pick_option(DECLINE, ["Male", "Female", "Non-binary"]) is None
    assert pick_option(DECLINE, ["Yes", "No"]) is None


@pytest.mark.parametrize(
    "answer",
    [
        "No, I do not have a disability and have not had one in the past",
        "I am not a protected veteran",
        "No, I do not want to relocate",
        "No, I do not require sponsorship",
        "I prefer to self-describe",
        "No",
    ],
)
def test_real_answers_are_not_declines(answer: str) -> None:
    assert not is_decline(answer)


# Review 2026-10-10: an option that states a status and declines only its
# detail matched the decline wording anywhere and was taken first in list
# order — a protected-veteran or has-a-disability answer for a stored decline.
VETERAN_STATUS_THEN_DECLINE = (
    "I am a protected veteran, but I choose not to self-identify the classifications to "
    "which I belong"
)
MIXED_OPTIONS = [
    VETERAN_STATUS_THEN_DECLINE,
    "I am a protected veteran but I choose not to self-identify the classification(s) to "
    "which I belong",
    "Yes, I have a disability, but I prefer not to say which",
    "Yes, I am Hispanic or Latino, but I prefer not to answer further",
    "No, I don't wish to answer",
    "Prefer not to say, but I am a veteran",
    "Decline to self-identify; however, I identify as LGBTQ+",
]


@pytest.mark.parametrize("option", MIXED_OPTIONS)
def test_an_option_that_states_a_status_is_not_a_decline(option: str) -> None:
    assert not is_decline(option)
    assert mentions_decline(option)  # still kept out of yes/no, acknowledgement and fuzzy


@pytest.mark.parametrize(
    ("options", "want"),
    [
        (
            [VETERAN_STATUS_THEN_DECLINE, "I am not a protected veteran",
             "I do not wish to self-identify"],
            "I do not wish to self-identify",
        ),
        (
            ["Yes, I have a disability, but I prefer not to say which",
             "No, I do not have a disability", "I do not want to answer"],
            "I do not want to answer",
        ),
        # the pure decline listed before the mixed one, and after it
        (
            ["I don't wish to answer", VETERAN_STATUS_THEN_DECLINE,
             "I am not a protected veteran"],
            "I don't wish to answer",
        ),
    ],
)  # fmt: skip
def test_a_stored_decline_skips_options_that_state_a_status(options: list[str], want: str) -> None:
    assert pick_option(DECLINE, options) == want
    assert pick_option("Decline to self-identify", options) == want


@pytest.mark.parametrize(
    "options",
    [
        [VETERAN_STATUS_THEN_DECLINE, "I am not a protected veteran"],
        [
            "I identify as one or more of the classifications of protected veteran",
            "I am a protected veteran but I choose not to self-identify the classification(s) "
            "to which I belong",
            "I am not a protected veteran",
        ],
        [
            "Yes, I have a disability, but I prefer not to say which",
            "No, I do not have a disability",
        ],
    ],
)
def test_a_stored_decline_with_only_mixed_options_is_left_blank(options: list[str]) -> None:
    assert choose_option(DECLINE, options) is None
    assert choose_option("Decline to self-identify", options) is None


def test_among_pure_declines_the_exact_wording_then_the_shortest_wins() -> None:
    options = [
        "I choose not to disclose my status at this time",
        "Prefer not to say",
        "Decline to self-identify",
    ]
    assert pick_option(DECLINE, options) == "Prefer not to say"
    assert pick_option("Decline to self-identify", options) == "Decline to self-identify"


def test_a_mixed_option_is_never_a_yes_no_or_fuzzy_answer() -> None:
    mixed = ["Yes, I have a disability, but I prefer not to say which", "No"]
    assert pick_option("Yes", mixed) is None
    assert pick_option("I am a protected veteran", [VETERAN_STATUS_THEN_DECLINE]) is None


def test_a_stored_answer_that_states_a_status_takes_only_its_own_option() -> None:
    options = [VETERAN_STATUS_THEN_DECLINE, "I am not a protected veteran", DECLINE]
    assert pick_option(VETERAN_STATUS_THEN_DECLINE, options) == VETERAN_STATUS_THEN_DECLINE
    assert pick_option(VETERAN_STATUS_THEN_DECLINE, options[1:]) is None


# ---- yes/no, preferences, safe fuzzy -------------------------------------------------


def test_yes_no_answers_pick_the_matching_word() -> None:
    # third review 2026-10-10: a bare "No" no longer takes an option that
    # says more than "No" (see test_a_bare_yes_no_never_takes_a_qualified_option);
    # select_preference is how the owner names one
    assert pick_option("No", ["Yes", "No, I do not want to relocate"]) is None
    assert pick_option(
        "No", ["Yes", "No, I do not want to relocate"], prefer=["No, I do not want to relocate"]
    ) == ("No, I do not want to relocate")
    assert pick_option("No", ["Yes", "No, I do not"]) == "No, I do not"
    assert pick_option("Yes", ["Yes, I can", "No"]) == "Yes, I can"
    sponsorship = [
        "No",
        "Yes, Netherlands Highly Skilled Migrant Visa",
        "Yes, USMCA Professional (TN) Visa (USA)",
    ]
    assert pick_option("No", sponsorship) == "No"
    assert pick_option("Yes", ["None of the above", "Not sure"]) is None
    assert pick_option("No", ["None of the above", "Not sure"]) is None
    # a decline option is never a "No"
    assert pick_option("No", ["Yes", "No answer"]) is None


# review 2026-10-10: a bare "No" took the first option starting with "No"
APPLIED_RECENTLY_OPTIONS = [
    "Yes",
    "No, this is my first time applying to this role",
    "No, I applied to this role more than 6 months ago",
]
SPONSORSHIP_TWO_NOS = [
    "No, but I will require sponsorship in the future",
    "No, I do not require sponsorship",
    "Yes",
]


def test_an_ambiguous_yes_no_is_left_blank() -> None:
    assert choose_option("No", APPLIED_RECENTLY_OPTIONS) is None
    assert choose_option("No", SPONSORSHIP_TWO_NOS) is None
    # the other word is still unambiguous on the same lists
    assert pick_option("Yes", APPLIED_RECENTLY_OPTIONS) == "Yes"
    assert pick_option("Yes", SPONSORSHIP_TWO_NOS) == "Yes"
    # a longer stored answer that only starts with the word is no better
    assert pick_option("No, I have not applied before", APPLIED_RECENTLY_OPTIONS) is None
    # two "Yes, ..." variants: the same rule
    assert pick_option("Yes", ["No", "Yes, H-1B transfer", "Yes, TN visa"]) is None


def test_an_ambiguous_yes_no_takes_only_the_option_select_preference_names() -> None:
    prefer = ["No, I do not require sponsorship"]
    assert pick_option("No", SPONSORSHIP_TWO_NOS, prefer=prefer) == (
        "No, I do not require sponsorship"
    )
    # a preference naming none of the options does not break the tie
    assert pick_option("No", SPONSORSHIP_TWO_NOS, prefer=["No"]) is None
    assert pick_option("No", APPLIED_RECENTLY_OPTIONS, prefer=["Not applicable"]) is None


def test_the_bare_word_breaks_a_yes_no_tie() -> None:
    options = ["No, but I will require sponsorship in the future", "No", "Yes"]
    assert pick_option("No", options) == "No"
    assert pick_option("No, I do not", ["No.", "No, but later", "Yes"]) == "No."
    # two bare words (a duplicated option) are still ambiguous
    assert pick_option("No, I do not", ["No", "no", "Yes"]) is None


# second review 2026-10-10: step 5 compared only the answer's first word, so
# an option whose own qualifier contradicts the answer was picked
ONSITE = "Yes — up to 2 days per week in Philadelphia or Bucks County."  # the bank's onsite_ok


@pytest.mark.parametrize(
    ("want", "options"),
    [
        (ONSITE, ["Yes, 5 days per week", "No"]),
        (ONSITE, ["Yes, I can relocate to San Francisco", "No"]),
        ("No, I do not require sponsorship",
         ["Yes", "No, but I will require sponsorship in the future"]),
        (ONSITE, ["Yes, in New Jersey", "No"]),
    ],
)  # fmt: skip
def test_a_qualified_yes_no_never_takes_an_option_that_contradicts_it(
    want: str, options: list[str]
) -> None:
    assert pick_option(want, options) is None


@pytest.mark.parametrize(
    ("want", "options", "expected"),
    [
        # the bare word, or a remainder the answer already says
        (ONSITE, ["Yes", "No"], "Yes"),
        (ONSITE, ["Yes, up to 2 days per week", "No"], "Yes, up to 2 days per week"),
        (ONSITE, ["Yes, Bucks County", "No"], "Yes, Bucks County"),
        ("No, I do not require sponsorship", ["Yes", "No"], "No"),
        ("No, I do not require sponsorship", ["Yes, I require sponsorship", "No, I do not"],
         "No, I do not"),
        # words that only echo the question
        ("Yes, I am authorized to work in the US", ["Yes, I can", "No"], "Yes, I can"),
    ],
)  # fmt: skip
def test_a_qualified_yes_no_takes_an_option_it_backs(
    want: str, options: list[str], expected: str
) -> None:
    assert pick_option(want, options) == expected


# third review 2026-10-10: the qualified check ran one way only, so an
# option that DROPS the answer's negation was the single candidate. Each of
# these picked the option stating the opposite of the bank's answer.
@pytest.mark.parametrize(
    ("want", "options"),
    [
        ("No, I will not require sponsorship", ["Yes", "No, I will require sponsorship"]),
        ("No, I will not require sponsorship now or in the future",
         ["Yes", "No, I will require sponsorship in the future", "No, I do not need sponsorship"]),
        ("Yes, I am authorized and do not need sponsorship",
         ["Yes, I am authorized and need sponsorship", "No"]),
        # the same words in another order say something else
        ("No, I will not require sponsorship now",
         ["Yes", "No, I will require sponsorship, not now"]),
        # an option that ADDS a negation the answer does not hold
        ("Yes, I am authorized and need sponsorship",
         ["Yes, I am authorized and do not need sponsorship", "No"]),
    ],
)  # fmt: skip
def test_a_qualified_yes_no_never_takes_an_option_that_drops_its_negation(
    want: str, options: list[str]
) -> None:
    assert choose_option(want, options) is None


def test_a_qualified_yes_no_still_takes_an_option_with_the_same_negation() -> None:
    want = "No, I will not require sponsorship now or in the future"
    options = ["Yes", "No, I will not require sponsorship"]
    assert pick_option(want, options) == "No, I will not require sponsorship"


# third review 2026-10-10: a bare answer took the only option starting with
# its word, whatever that option went on to state
@pytest.mark.parametrize(
    ("want", "options"),
    [
        ("No", ["Yes", "No, but I will require sponsorship in the future"]),
        ("Yes", ["Yes, with sponsorship", "No"]),
        ("Yes", ["Yes, I used AI", "No, I did not use AI"]),
        ("Yes", ["Yes, I do not", "No"]),
        ("No", ["Yes", "No, this is my first time applying to this role"]),
        ("No", ["Yes", "No, I do not require sponsorship to work in the country"]),
    ],
)
def test_a_bare_yes_no_never_takes_a_qualified_option(want: str, options: list[str]) -> None:
    assert choose_option(want, options) is None
    # the owner can still name it in select_preference
    named = next(o for o in options if o.lower().startswith(want.lower() + ","))
    assert pick_option(want, options, prefer=[named]) == named


@pytest.mark.parametrize(
    ("want", "options", "expected"),
    [
        ("No", ["Yes", "No, I do not"], "No, I do not"),
        ("No", ["Yes", "No, I have not"], "No, I have not"),
        ("No", ["Yes", "No, never"], "No, never"),
        ("Yes", ["Yes, I do", "No"], "Yes, I do"),
        ("Yes", ["Yes, that's correct", "No"], "Yes, that's correct"),
        ("No", ["No, but I will require sponsorship in the future", "No", "Yes"], "No"),
    ],
)
def test_a_bare_yes_no_takes_an_option_that_only_echoes_it(
    want: str, options: list[str], expected: str
) -> None:
    assert pick_option(want, options) == expected


def test_select_preference_is_tried_in_order() -> None:
    options = ["LinkedIn", "Indeed", "Company Website", "Referral", "Other"]
    prefer = ["Company Website", "Careers Page", "Other", "Job Board"]
    assert pick_option("Company careers page", options, prefer=prefer) == "Company Website"
    assert pick_option("Company careers page", ["LinkedIn", "Other"], prefer=prefer) == "Other"


@pytest.mark.parametrize(
    ("want", "options", "expected"),
    [
        # Lumon: the drafted "Moderate experience" names one option fully
        (
            "Moderate experience",
            ["No tech sales experience", "Some experience (fewer than 2 years)",
             "Moderate experience (2–4 years)", "Extensive experience (4+ years)"],
            "Moderate experience (2–4 years)",
        ),
        ("Company careers page", ["LinkedIn", "Careers Page", "Other"], "Careers Page"),
        # a relocation answer that contains "no" is not "No"
        (
            "Only for Chicago or Raleigh. Otherwise no: I would rather keep my "
            "current home and work remotely.",
            ["Yes", "No"],
            None,
        ),
        # a negation is a different answer
        ("I am a protected veteran", ["I am not a protected veteran", "Other"], None),
        ("Agree", ["Do not agree", "Undecided"], None),
        # two options fit equally well: the human picks
        ("Experience", ["Experience (sales)", "Experience (marketing)"], None),
        # a different state inside a longer option
        ("Springfield office", ["Springfield office, IL", "Remote"], None),
        # a creatable dropdown's "Create" row would invent an option
        ("Company careers page", ['Create "Company careers page"', "LinkedIn"], None),
    ],
)  # fmt: skip
def test_fuzzy_matching_only_when_safe(want: str, options: list[str], expected: str | None) -> None:
    assert pick_option(want, options) == expected


def test_choose_option_returns_the_index() -> None:
    texts = ["  Alabama ", "New Mexico", "New Jersey"]
    assert choose_option("New Jersey", texts) == 2
    assert choose_option("", texts) is None
    assert choose_option("New Jersey", []) is None


# ---- legal acknowledgements (the bank's legal_acknowledgement entry) -------------------

ACK_PREFER = ["I agree", "I Agree", "Yes", "I understand", "Acknowledge", "I acknowledge"]


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        # single-option selects
        (["I understand and agree to the terms of the Agreement to Arbitrate set forth above."],
         "I understand and agree to the terms of the Agreement to Arbitrate set forth above."),
        (["I will read the arbitration agreement below."],
         "I will read the arbitration agreement below."),
        (["Yes", "No"], "Yes"),
        (["I agree", "I do not agree"], "I agree"),
        (["I do not agree", "I acknowledge and agree"], "I acknowledge and agree"),
        (["Acknowledge/Confirm"], "Acknowledge/Confirm"),
        (["I do not agree"], None),
        (["I agree to the terms", "I accept the terms"], None),  # two affirmatives: the human
    ],
)  # fmt: skip
def test_acknowledgement_picks_the_one_affirmative_option(
    options: list[str], expected: str | None
) -> None:
    got = pick_option("Yes", options, prefer=ACK_PREFER, acknowledgement=True)
    assert got == expected


# third review 2026-10-10: step 5 returned the only option starting with
# "Yes", whatever it asserted, and step 6 took any affirmative option.
# Reached from covered labels such as "AI Policy for Application" or "Please
# read the arbitration agreement below".
@pytest.mark.parametrize(
    "options",
    [
        ["Yes, I opt out of arbitration", "No, I agree to arbitration"],
        ["Yes, I used AI", "No, I did not use AI"],
        ["Yes, I opt out of arbitration"],
        ["Yes, I used AI"],
        ["I acknowledge that I used AI", "I do not agree"],
        ["I agree to opt out of arbitration", "I do not agree"],
        ["I agree to waive the arbitration agreement"],
        ["Yes, I withdraw my consent", "No"],
        ["Yes, I have been party to a lawsuit", "No"],
        ["Yes, I agree to arbitration, but not for wage claims", "No"],
    ],
)
def test_an_acknowledgement_never_takes_an_option_that_states_something_else(
    options: list[str],
) -> None:
    assert choose_option("Yes", options, prefer=ACK_PREFER, acknowledgement=True) is None


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (["Yes, I agree", "No"], "Yes, I agree"),
        (["Yes, I have read and agree to the arbitration agreement", "No"],
         "Yes, I have read and agree to the arbitration agreement"),
        (["Yes, I agree to Tyrell's AI policy", "No"], "Yes, I agree to Tyrell's AI policy"),
        # the agreeing option, not the opt-out one beside it
        (["Yes, I agree", "Yes, I opt out of arbitration", "No"], "Yes, I agree"),
    ],
)  # fmt: skip
def test_an_acknowledgement_takes_an_option_that_only_agrees(
    options: list[str], expected: str
) -> None:
    assert pick_option("Yes", options, prefer=ACK_PREFER, acknowledgement=True) == expected


def test_an_acknowledgement_option_is_never_chosen_without_the_entry() -> None:
    """Without acknowledgement=True (no legal_acknowledgement entry behind the
    field) a "Yes" never turns into agreeing to an arbitration clause."""
    options = ["I understand and agree to the terms of the Agreement to Arbitrate set forth above."]
    assert pick_option("Yes", options) is None
    assert pick_option("Yes", ["I will read the arbitration agreement below."]) is None


# ---- location autocomplete --------------------------------------------------------------


def test_parse_location() -> None:
    assert parse_location("Bristol, PA") == ("Bristol", "PA")
    assert parse_location("Bristol, Pennsylvania, United States") == ("Bristol", "PA")
    assert parse_location("Pennsylvania") == (None, "PA")
    assert parse_location("Remote") is None
    assert parse_location("Bristol, PA, NY") is None  # two states are not one place


def test_location_queries_try_the_city_then_the_state() -> None:
    assert location_queries("Bristol", "PA", "Bristol, PA") == [
        ("Bristol, PA", False),
        ("Bristol, Pennsylvania", False),
        ("Pennsylvania", True),
    ]
    assert location_queries(None, "PA", "Pennsylvania") == [("Pennsylvania", True)]


@pytest.mark.parametrize(
    ("suggestions", "state_only_ok", "expected"),
    [
        (["Bristol, Rhode Island, United States", "Bristol, Indiana, United States",
          "Bristol, Pennsylvania, United States"], False, 2),
        (["Bristol, PA 19007, USA"], False, 0),
        (["Bristol, Bucks County, Pennsylvania, United States"], False, 0),
        # another city in the right state, a township of the same name: never
        (["Langhorne, PA, USA", "Bristol Township, PA, USA"], False, None),
        # the state itself, only once the state is what was typed
        (["Pennsylvania, USA"], False, None),
        (["Langhorne, PA, USA", "Pennsylvania, USA"], True, 1),
        # never another state, never a place naming two states
        (["New Jersey, USA", "New York, USA",
          "Bristol, Rhode Island, United States"], True, None),
        (["Bristol, NY, PA"], True, None),
        (["Bristol, Quebec, Canada"], True, None),
        ([], True, None),
    ],
)  # fmt: skip
def test_choose_location_never_picks_another_state(
    suggestions: list[str], state_only_ok: bool, expected: int | None
) -> None:
    got = choose_location("Bristol", "PA", suggestions, state_only_ok=state_only_ok)
    assert got == expected
