"""2026-10-12: "Do you now, or will you ever, require employment
sponsorship to work in the country where this job is located?" matched the
work_auth entry by embedding similarity (0.839, threshold 0.82) and was filled
"Yes": the application said the candidate needs sponsorship. Work
authorization and sponsorship answer with opposite polarity, so neither
entry may answer the other's question, nor a label asking both, unless an
alias is the label word for word. A blank and a flag beat a wrong pick.

Review 2026-10-12: the guard missed wordings. "U.S." ended the clause before
"sponsorship", "need a work visa" and "legally able to work" were not
recognised, so "Do you now or in the future need U.S. sponsorship?" placed
near work_auth (0.84) still took its "Yes". Now dotted abbreviations are
undotted, the windows stop only at a question mark, a verdict reached by
embedding must be recognised as asking that entry's own side, and no other
entry answers a sponsorship or authorization question.
Offline: a fake embedder stands in for fastembed."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from jobkit.autofill import answer_bank
from jobkit.autofill.answer_bank import BankMatcher, _work_status_crossed, never_draft

AUTH = "Are you legally authorized to work in the United States?"
SPONSOR = "Will you now or in the future require sponsorship?"
BOTH_ALIAS = "Are you authorized to work here without needing sponsorship?"
BASED = "Are you based in the United States?"
AGE = "at least 18"
BANK: dict[str, Any] = {
    "match_threshold": 0.82,
    "screening": [
        {"id": "work_auth", "aliases": [AUTH, BOTH_ALIAS], "answer": "Yes"},
        {"id": "sponsorship", "aliases": [SPONSOR], "answer": "No"},
        {"id": "based_in_us", "aliases": [BASED], "answer": "Yes"},
        {"id": "age_18", "aliases": [AGE], "answer": "Yes"},
    ],
}
SPONSOR_Q = (
    "Do you now, or will you ever, require employment sponsorship to work in the country "
    "where this job is located?"
)
AUTH_Q = "Are you legally authorized to work in the country where this job is located?"
PLAIN_SPONSOR = "Will you now or in the future require visa sponsorship?"
WITHOUT = "Are you eligible to work in the United States without sponsorship?"
TWO_PART = "Are you authorized to work in the US, and will you require sponsorship?"

# the review's wordings that the guard did not recognise (2026-10-12)
NEED_US = "Do you now or in the future need U.S. sponsorship?"
GOVERNMENT = (
    "Will you now or in the future require the U.S. government's sponsorship of an employment visa?"
)
WORK_VISA = "Will you need a work visa to work in the United States?"
NEED_VISA = "Do you require a visa to work in the U.S.?"
LEGALLY_ABLE = "Are you legally able to work in the U.S.?"
CAN_LEGALLY = "Can you legally work in the United States?"
# work-status wordings neither side's patterns claim
CITIZEN = "Are you a U.S. citizen?"
VISA_STATUS = "Visa status"

# unit vectors: [work_auth, sponsorship, based_in_us, BOTH_ALIAS, anything else];
# cosine = dot product, and an unlisted label is near nothing (novel)
VECTORS: dict[str, list[float]] = {
    AUTH: [1.0, 0.0, 0.0, 0.0, 0.0],
    BOTH_ALIAS: [0.0, 0.0, 0.0, 1.0, 0.0],
    SPONSOR: [0.0, 1.0, 0.0, 0.0, 0.0],
    BASED: [0.0, 0.0, 1.0, 0.0, 0.0],
    AGE: [0.0, 0.0, 0.0, 0.0, 0.0],  # near nothing: answers by alias only
    SPONSOR_Q: [0.839, 0.544, 0.0, 0.0, 0.0],  # nearer work_auth, as fastembed put it
    AUTH_Q: [0.898, 0.44, 0.0, 0.0, 0.0],
    PLAIN_SPONSOR: [0.2, 0.98, 0.0, 0.0, 0.0],
    WITHOUT: [0.3, 0.954, 0.0, 0.0, 0.0],
    TWO_PART: [0.9, 0.436, 0.0, 0.0, 0.0],
}
NEAR_WORK_AUTH = [0.84, 0.5426, 0.0, 0.0, 0.0]  # the review's repro: 0.84 to work_auth
NEAR_SPONSORSHIP = [0.5426, 0.84, 0.0, 0.0, 0.0]
NEAR_BASED_IN_US = [0.0, 0.0, 0.84, 0.0, 0.5426]  # fastembed put CITIZEN at 0.836


class FakeEmbedder:
    def embed(self, texts: list[str]) -> Iterator[list[float]]:
        for t in texts:
            yield VECTORS.get(t, [0.0, 0.0, 0.0, 0.0, 1.0])


@pytest.fixture()
def matcher(monkeypatch: pytest.MonkeyPatch) -> BankMatcher:
    monkeypatch.setattr(answer_bank, "_embedder", lambda: FakeEmbedder())
    return BankMatcher(bank=BANK, use_embeddings=True)


def _place(monkeypatch: pytest.MonkeyPatch, label: str, vector: list[float]) -> None:
    monkeypatch.setitem(VECTORS, label, vector)


def test_the_sponsorship_question_never_takes_the_work_auth_yes(matcher: BankMatcher) -> None:
    v = matcher.match(SPONSOR_Q, identity="question_51000002")
    assert v == {
        "kind": "blank_flag",
        "reason": "sponsorship-needs-human:sponsorship-question-took-work_auth",
    }


@pytest.mark.parametrize(
    ("label", "entry"),
    [
        (AUTH_Q, "work_auth"),
        (PLAIN_SPONSOR, "sponsorship"),
        (AUTH, "work_auth"),
        (SPONSOR, "sponsorship"),
    ],
)
def test_each_side_still_answers_its_own_question(
    matcher: BankMatcher, label: str, entry: str
) -> None:
    v = matcher.match(label)
    assert (v["kind"], v.get("entry_id")) == ("screening", entry), v


@pytest.mark.parametrize(
    ("label", "why"),
    [
        (WITHOUT, "authorization-question-took-sponsorship"),
        (TWO_PART, "asks-authorization-and-sponsorship"),
        # an alias inside a longer label that also asks the other question
        (f"{SPONSOR} Are you authorized to work in the US?", "asks-authorization-and-sponsorship"),
    ],
)
def test_crossed_or_two_part_labels_go_to_the_human(
    matcher: BankMatcher, label: str, why: str
) -> None:
    assert matcher.match(label) == {
        "kind": "blank_flag",
        "reason": f"sponsorship-needs-human:{why}",
    }


def test_work_auth_never_answers_a_sponsorship_label_even_word_for_word(
    matcher: BankMatcher,
) -> None:
    """2026-10-15: the work_auth entry never answers a label that asks about
    needing sponsorship, not even through an alias that is the label word for
    word (BOTH_ALIAS is one of work_auth's aliases here). Before, the alias
    let work_auth's "Yes" through."""
    assert matcher.match(BOTH_ALIAS) == {
        "kind": "blank_flag",
        "reason": "sponsorship-needs-human:sponsorship-question-took-work_auth",
    }


@pytest.mark.parametrize("label", [NEED_US, GOVERNMENT, WORK_VISA, NEED_VISA])
def test_review_sponsorship_wordings_never_take_the_work_auth_yes(
    matcher: BankMatcher, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    """The review's repro: the label sits at 0.84 to work_auth. Before the fix
    NEED_US returned work_auth (similarity 0.84) and filled "Yes"; with the
    real fastembed model WORK_VISA (0.842) and NEED_VISA (0.851) did."""
    _place(monkeypatch, label, NEAR_WORK_AUTH)
    assert matcher.match(label, identity="question_1") == {
        "kind": "blank_flag",
        "reason": "sponsorship-needs-human:sponsorship-question-took-work_auth",
    }


@pytest.mark.parametrize("label", [NEED_US, GOVERNMENT, WORK_VISA, NEED_VISA])
def test_review_sponsorship_wordings_still_reach_sponsorship(
    matcher: BankMatcher, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    _place(monkeypatch, label, NEAR_SPONSORSHIP)
    v = matcher.match(label)
    assert (v["kind"], v.get("entry_id")) == ("screening", "sponsorship"), v


@pytest.mark.parametrize("label", [LEGALLY_ABLE, CAN_LEGALLY])
def test_legally_able_to_work_never_takes_the_sponsorship_no(
    matcher: BankMatcher, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    _place(monkeypatch, label, NEAR_SPONSORSHIP)
    assert matcher.match(label) == {
        "kind": "blank_flag",
        "reason": "work-status-needs-human:authorization-question-took-sponsorship",
    }
    _place(monkeypatch, label, NEAR_WORK_AUTH)
    v = matcher.match(label)
    assert (v["kind"], v.get("entry_id")) == ("screening", "work_auth"), v


@pytest.mark.parametrize(
    ("label", "vector", "why"),
    [
        # by embedding, an entry answers only a label that asks its own side
        (
            CITIZEN,
            NEAR_WORK_AUTH,
            "work-status-needs-human:embedding-not-an-authorization-question",
        ),
        (
            VISA_STATUS,
            NEAR_SPONSORSHIP,
            "sponsorship-needs-human:embedding-not-a-sponsorship-question",
        ),
        # and no other entry answers a work-status question by embedding
        (
            CITIZEN,
            NEAR_BASED_IN_US,
            "work-status-needs-human:work-status-question-took-based_in_us",
        ),
        (
            NEED_US,
            NEAR_BASED_IN_US,
            "sponsorship-needs-human:work-status-question-took-based_in_us",
        ),
    ],
)
def test_unrecognised_or_other_entry_embedding_verdicts_go_to_the_human(
    matcher: BankMatcher,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    vector: list[float],
    why: str,
) -> None:
    _place(monkeypatch, label, vector)
    assert matcher.match(label) == {"kind": "blank_flag", "reason": why}


def test_another_entrys_alias_never_answers_a_sponsorship_question(matcher: BankMatcher) -> None:
    # age_18's alias "at least 18" is inside the label: its "Yes" would say
    # the candidate requires sponsorship
    assert matcher.match("Are you at least 18 and will you require sponsorship?") == {
        "kind": "blank_flag",
        "reason": "sponsorship-needs-human:work-status-question-took-age_18",
    }
    v = matcher.match("Are you at least 18 years of age?")
    assert (v["kind"], v.get("entry_id")) == ("screening", "age_18")
    v = matcher.match(BASED)
    assert (v["kind"], v.get("entry_id")) == ("screening", "based_in_us")


def test_another_entrys_word_for_word_alias_still_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    label = "Are you 18 or older and authorized to work in the US?"
    bank = {"screening": [{"id": "age_18", "aliases": [label], "answer": "Yes"}]}
    monkeypatch.setattr(answer_bank, "_embedder", lambda: FakeEmbedder())
    v = BankMatcher(bank=bank, use_embeddings=True).match(label)
    assert (v["kind"], v.get("entry_id")) == ("screening", "age_18")


@pytest.mark.parametrize(
    ("label", "entry", "why"),
    [
        (SPONSOR_Q, "work_auth", "sponsorship-question-took-work_auth"),
        (SPONSOR_Q, "sponsorship", None),
        (AUTH_Q, "work_auth", None),
        (AUTH_Q, "sponsorship", "authorization-question-took-sponsorship"),
        (TWO_PART, "work_auth", "asks-authorization-and-sponsorship"),
        # the review's wordings: "U.S." no longer ends the clause, a visa
        # needed is a sponsorship question, "legally able" is authorization
        (NEED_US, "work_auth", "sponsorship-question-took-work_auth"),
        (NEED_US, "sponsorship", None),
        (GOVERNMENT, "work_auth", "sponsorship-question-took-work_auth"),
        (GOVERNMENT, "sponsorship", None),
        (WORK_VISA, "work_auth", "sponsorship-question-took-work_auth"),
        (NEED_VISA, "work_auth", "sponsorship-question-took-work_auth"),
        (
            "Do you need sponsorship to work in the U.S.A.?",
            "work_auth",
            "sponsorship-question-took-work_auth",
        ),
        (
            "Will you need to be sponsored for an H-1B?",
            "work_auth",
            "sponsorship-question-took-work_auth",
        ),
        (
            "Will you now or in the future require Initech to sponsor an immigration case in "
            "order to employ you?",
            "sponsorship",
            None,
        ),
        (LEGALLY_ABLE, "sponsorship", "authorization-question-took-sponsorship"),
        (LEGALLY_ABLE, "work_auth", None),
        (CAN_LEGALLY, "sponsorship", "authorization-question-took-sponsorship"),
        (
            "Do you have the legal right to work in the US?",
            "sponsorship",
            "authorization-question-took-sponsorship",
        ),
        # sales vocabulary is not a sponsorship question
        ("Do your deals require executive sponsorship?", "work_auth", None),
        ("Do your deals require executive sponsorship?", "relocation", None),
        # other entries: not on a sponsorship or authorization question
        (SPONSOR_Q, "relocation", "work-status-question-took-relocation"),
        (AUTH_Q, "age_18", "work-status-question-took-age_18"),
        ("Are you willing to relocate?", "relocation", None),
        ("Are you able to work in our NYC office 3 days a week?", "onsite_ok", None),
    ],
)
def test_work_status_crossed(label: str, entry: str, why: str | None) -> None:
    assert _work_status_crossed(answer_bank._norm_text(label), entry) == why


@pytest.mark.parametrize(
    ("label", "entry", "why"),
    [
        (AUTH, "work_auth", None),
        (PLAIN_SPONSOR, "sponsorship", None),
        (CITIZEN, "work_auth", "embedding-not-an-authorization-question"),
        (VISA_STATUS, "sponsorship", "embedding-not-a-sponsorship-question"),
        (CITIZEN, "based_in_us", "work-status-question-took-based_in_us"),
        ("Are you located in the United States?", "based_in_us", None),
    ],
)
def test_work_status_crossed_by_embedding(label: str, entry: str, why: str | None) -> None:
    low = answer_bank._norm_text(label)
    assert _work_status_crossed(low, entry, by_embedding=True) == why


@pytest.mark.parametrize("label", [SPONSOR_Q, NEED_US, GOVERNMENT, LEGALLY_ABLE, CAN_LEGALLY])
def test_the_blank_is_never_drafted_either(label: str) -> None:
    assert never_draft(label) == "work-status"


# ---- review 2026-10-12 (fill-quality): the question asked in reverse -----------
#
# "Can you work in the US without needing visa sponsorship?" matched the
# sponsorship entry by embedding (0.954) and filled its "No": the form said
# the candidate cannot work without sponsorship. "able to work" / "can you
# work" + a country now read as an authorization question, and a label that
# pairs "without" (or "no need for") with a visa or sponsorship goes to
# neither side unless an alias is the label word for word.

WITHOUT_VISA = "Can you work in the US without visa sponsorship?"
ABLE_WITHOUT = "Are you able to work in the U.S. without visa sponsorship?"
WITHOUT_NEEDING = "Can you work in the US without needing visa sponsorship?"
REVERSED = [WITHOUT_VISA, ABLE_WITHOUT, WITHOUT_NEEDING]
NEAR_SPONSORSHIP_954 = [0.3, 0.954, 0.0, 0.0, 0.0]  # where the review's repro put them


@pytest.mark.parametrize("label", REVERSED)
@pytest.mark.parametrize(
    "vector", [NEAR_SPONSORSHIP_954, NEAR_SPONSORSHIP, NEAR_WORK_AUTH], ids=["0.954", "s", "a"]
)
def test_reversed_sponsorship_question_takes_neither_side(
    matcher: BankMatcher, monkeypatch: pytest.MonkeyPatch, label: str, vector: list[float]
) -> None:
    _place(monkeypatch, label, vector)
    assert matcher.match(label) == {
        "kind": "blank_flag",
        "reason": "sponsorship-needs-human:asks-authorization-and-sponsorship",
    }


@pytest.mark.parametrize(
    ("label", "entry", "why"),
    [
        *[(lab, "sponsorship", "asks-authorization-and-sponsorship") for lab in REVERSED],
        *[(lab, "work_auth", "asks-authorization-and-sponsorship") for lab in REVERSED],
        *[(lab, "relocation", "work-status-question-took-relocation") for lab in REVERSED],
        # "without" + sponsorship with no authorization wording: still neither side
        (WITHOUT, "work_auth", "asks-without-sponsorship"),
        ("Can you start without visa sponsorship?", "sponsorship", "asks-without-sponsorship"),
        ("Is there no need for visa sponsorship?", "sponsorship", "asks-without-sponsorship"),
        (
            "Are you eligible for employment with no requirement for an H-1B?",
            "sponsorship",
            "asks-without-sponsorship",
        ),
        ("Can you start without visa sponsorship?", "age_18", "work-status-question-took-age_18"),
        # sales vocabulary, an office, a time zone: not work status
        ("Have you closed deals without executive sponsorship?", "relocation", None),
        ("Are you able to work in US time zones?", "onsite_ok", None),
        ("Can you work in the US Eastern time zone?", "onsite_ok", None),
        ("Can you work weekends?", "onsite_ok", None),
        # "able to work" / "can you work" + a country is an authorization question
        ("Are you able to work in the U.S.?", "work_auth", None),
        (
            "Are you able to work in the U.S.?",
            "sponsorship",
            "authorization-question-took-sponsorship",
        ),
        ("Can you work in the United States?", "work_auth", None),
        (
            "Can you work in the country where this job is located?",
            "onsite_ok",
            "work-status-question-took-onsite_ok",
        ),
    ],
)
def test_reversed_and_able_to_work_wordings(label: str, entry: str, why: str | None) -> None:
    low = answer_bank._norm_text(label)
    assert _work_status_crossed(low, entry) == why
    assert _work_status_crossed(low, entry, by_embedding=True) == why


def test_able_to_work_in_the_us_reaches_work_auth_by_embedding(
    matcher: BankMatcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    # before: not recognised as an authorization question, so blank by embedding
    label = "Are you able to work in the U.S.?"
    _place(monkeypatch, label, NEAR_WORK_AUTH)
    v = matcher.match(label)
    assert (v["kind"], v.get("entry_id")) == ("screening", "work_auth"), v


def test_a_reversed_label_never_takes_work_auth_even_word_for_word(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-10-15: work_auth never answers a sponsorship label, whatever its
    aliases say. The human answers it (MCP answer_novel)."""
    bank = {"screening": [{"id": "work_auth", "aliases": [WITHOUT_VISA], "answer": "Yes"}]}
    monkeypatch.setattr(answer_bank, "_embedder", lambda: FakeEmbedder())
    assert BankMatcher(bank=bank, use_embeddings=True).match(WITHOUT_VISA) == {
        "kind": "blank_flag",
        "reason": "sponsorship-needs-human:sponsorship-question-took-work_auth",
    }


@pytest.mark.parametrize("label", [*REVERSED, "Can you start without visa sponsorship?"])
def test_a_reversed_label_is_never_drafted(label: str) -> None:
    assert never_draft(label) == "work-status"


# ---- review 2026-10-15: requiring a permit, and negations ---------------------
#
# "Do you require work authorization ...?" and "Will you require a work
# permit ...?" read as authorization questions, so work_auth's "Yes" (the
# candidate needs a permit) went through; "you will not require sponsorship"
# and "you won't need visa sponsorship" read as sponsorship questions, so
# sponsorship's "No" (the candidate WILL need it) went through. Requiring
# work authorization or a permit is now a needs-sponsorship question, and a
# negation within three words of the work-status words, or anywhere for a
# verdict reached by embedding, sends the label to the human.


@pytest.mark.parametrize(
    ("label", "entry", "why"),
    [
        # requiring work authorization or a permit: never work_auth ...
        (
            "Do you require work authorization to be employed in the United States?",
            "work_auth",
            "requires-authorization-question-took-work_auth",
        ),
        (
            "Will you require a work permit to work in the United States?",
            "work_auth",
            "requires-authorization-question-took-work_auth",
        ),
        (
            "Will you require, now or in the future, employment authorization to work in the US?",
            "work_auth",
            "requires-authorization-question-took-work_auth",
        ),
        (
            "Do you need authorization to work in the U.S.?",
            "work_auth",
            "requires-authorization-question-took-work_auth",
        ),
        (
            "Is a work permit required for you to work in the US?",
            "work_auth",
            "requires-authorization-question-took-work_auth",
        ),
        # ... and the sponsorship entry only when the label asks about sponsorship too
        (
            "Do you require work authorization to be employed in the United States?",
            "sponsorship",
            "asks-whether-work-authorization-is-required",
        ),
        (
            "Will you require a work permit to work in the United States?",
            "sponsorship",
            "asks-whether-work-authorization-is-required",
        ),
        ("Do you require a work permit or visa sponsorship?", "sponsorship", None),
        (
            "Do you require a work permit or visa sponsorship?",
            "work_auth",
            "sponsorship-question-took-work_auth",
        ),
        # what else the label asks still counts
        (
            "Are you authorized to work in the US, or will you need a work permit?",
            "work_auth",
            "requires-authorization-question-took-work_auth",
        ),
        (
            "Are you authorized to work in the US, or will you need a work permit?",
            "sponsorship",
            "authorization-question-took-sponsorship",
        ),
        (
            "Do you need a work permit?",
            "relocation",
            "work-status-question-took-relocation",
        ),
        # not a requirement on the candidate: sponsorship is what is required
        # (this label, seen on a real form, still goes to the human: "work
        # authorization" reads as the other question too), or the requirement
        # is the role's and the question is authorization
        (
            "Will you now or in the future require visa sponsorship to maintain that work "
            "authorization?",
            "sponsorship",
            "asks-authorization-and-sponsorship",
        ),
        (
            "This role requires candidates to be legally authorized to work in the US. Are you "
            "legally authorized to work in the US?",
            "work_auth",
            None,
        ),
        ("Do you hold a valid work permit for the United States?", "work_auth", None),
        # the passive form stops at a comma or a conjunction: an either/or
        # label still asks both questions
        (
            "Do you have work authorization, or is it required that we sponsor you?",
            "sponsorship",
            "asks-authorization-and-sponsorship",
        ),
        ("Do you currently hold work authorization in the United States?", "work_auth", None),
        # a negation near the work-status words: neither side
        (
            "Can you confirm that you will not require sponsorship now or in the future?",
            "sponsorship",
            "negated-work-status-question",
        ),
        (
            "Can you confirm that you will not require sponsorship now or in the future?",
            "work_auth",
            "sponsorship-question-took-work_auth",
        ),
        (
            "Is it correct that you won't need visa sponsorship?",
            "sponsorship",
            "negated-work-status-question",
        ),
        (
            "Is it correct that you won’t need visa sponsorship?",
            "sponsorship",
            "negated-work-status-question",
        ),
        (
            "You will never require visa sponsorship, correct?",
            "sponsorship",
            "negated-work-status-question",
        ),
        ("Do you not need sponsorship?", "sponsorship", "negated-work-status-question"),
        ("You no longer need an H-1B, correct?", "sponsorship", "negated-work-status-question"),
        ("No visa sponsorship required, correct?", "sponsorship", "negated-work-status-question"),
        (
            "Are you not authorized to work in the United States?",
            "work_auth",
            "negated-work-status-question",
        ),
        (
            "Is there any reason you would not be legally able to work in the US?",
            "work_auth",
            "negated-work-status-question",
        ),
        (
            "Doesn't your status require sponsorship?",
            "sponsorship",
            "negated-work-status-question",
        ),
        # the employer's own statement counts too: a false flag (the human
        # answers), never a wrong answer
        (
            "We do not sponsor visas. Will you now or in the future require sponsorship?",
            "sponsorship",
            "negated-work-status-question",
        ),
        # "no" alone, or a negation far from the work-status words, is not one
        ("Are you legally authorized to work in the United States? (Yes/No)", "work_auth", None),
        ("Yes or no: will you now or in the future require sponsorship?", "sponsorship", None),
        (
            "Are you legally authorized to work in the United States? If not, please explain.",
            "work_auth",
            None,
        ),
        (
            "Do you now, or will you ever, require employment sponsorship to work in the country "
            "where this job is located?",
            "sponsorship",
            None,
        ),
    ],
)
def test_requires_a_permit_and_negations(label: str, entry: str, why: str | None) -> None:
    assert _work_status_crossed(answer_bank._norm_text(label), entry) == why


@pytest.mark.parametrize(
    ("label", "entry"),
    [
        (
            "Are you legally authorized to work in the United States? If not, please explain.",
            "work_auth",
        ),
        ("Are you authorized to work in the US? We do not offer relocation.", "work_auth"),
    ],
)
def test_by_embedding_any_negation_goes_to_the_human(label: str, entry: str) -> None:
    """The embedder does not read polarity, so an embedding-only verdict
    does not answer a work-status label with a negation anywhere; an alias
    inside the label still does (a negation far from the work-status words)."""
    low = answer_bank._norm_text(label)
    assert _work_status_crossed(low, entry) is None
    assert (
        _work_status_crossed(low, entry, by_embedding=True)
        == "embedding-negated-work-status-question"
    )


def test_an_alias_inside_a_label_with_a_far_negation_still_answers(matcher: BankMatcher) -> None:
    label = f"{AUTH} If not, please explain."
    v = matcher.match(label)
    assert (v["kind"], v.get("entry_id")) == ("screening", "work_auth"), v


def test_a_sponsorship_label_with_another_sentence_goes_to_the_human(
    matcher: BankMatcher,
) -> None:
    """Review 2026-10-20: the sponsorship entry's "No" never answers a label
    that says anything besides its question, by alias or by embedding (the
    sentence after it may be a second question: "…? Are you a U.S.
    citizen?"). This one needs no human (its "No" is right): an accepted
    false flag, answered once the user adds the label as an alias word for
    word. Before, the alias inside it answered."""
    label = f"{SPONSOR} If yes, we cannot proceed."
    low = answer_bank._norm_text(label)
    assert _work_status_crossed(low, "sponsorship") == "asks-a-second-question"
    by_embedding = _work_status_crossed(low, "sponsorship", by_embedding=True)
    assert by_embedding == "asks-a-second-question"
    assert matcher.match(label) == {
        "kind": "blank_flag",
        "reason": "sponsorship-needs-human:asks-a-second-question",
    }


@pytest.mark.parametrize(
    "label",
    [
        "Do you require work authorization to be employed in the United States?",
        "Will you require a work permit to work in the United States?",
        "Is it correct that you won't need visa sponsorship?",
    ],
)
def test_the_new_wordings_are_never_drafted(label: str) -> None:
    assert never_draft(label) == "work-status"
