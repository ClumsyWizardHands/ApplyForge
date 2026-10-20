"""Sponsorship polarity (2026-10-15). A Greenhouse question, "Do you now, or
will you ever, require employment sponsorship to work in the country where
this job is located?", was staged with "Yes": the application said the
candidate needs sponsorship.

The path before the fix: capture_form_specs -> BankMatcher.match found no
alias, the embedder put the label at 0.839 to work_auth's alias "Are you
legally authorized to work in the United States?" (threshold 0.82), and
gated() returned
{"kind": "screening", "entry_id": "work_auth", "similarity": 0.839}. At
staging, staging_match re-matched with the same matcher and got the same
verdict, _value_for -> _screening_value("work_auth") gave "Yes", and the
Yes/No select took it.

The rule now: a label that mentions sponsorship, a visa or immigration is
answered only by the bank's sponsorship entry (when the label is recognised
as asking whether the candidate needs sponsorship) or by an entry whose own
alias IS the label; otherwise it is blank and flagged
sponsorship-needs-human. work_auth never answers a sponsorship question and
sponsorship never answers an authorization question, whatever the aliases.
Offline: a fake embedder stands in for fastembed."""

from __future__ import annotations

import copy
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from jobkit import llm
from jobkit.autofill import answer_bank, browser, form_spec
from jobkit.autofill.answer_bank import BankMatcher, mentions_sponsorship
from jobkit.autofill.browser import staging_match

SPONSOR_Q = (
    "Do you now, or will you ever, require employment sponsorship to work in the country "
    "where this job is located?"
)
AUTH_Q = "Are you legally authorized to work in the country where this job is located?"
AUTH_ALIAS = "Are you legally authorized to work in the United States?"
SPONSOR_ALIAS = "Will you now or in the future require sponsorship?"

# the bank before the fix (no alias for that wording); values are made up,
# the entry ids and polarity are the answer bank template's
BANK_AT_HEAD: dict[str, Any] = {
    "match_threshold": 0.82,
    "screening": [
        {"id": "work_auth", "aliases": [AUTH_ALIAS], "answer": "Yes", "type": "boolean"},
        {
            "id": "sponsorship",
            "aliases": [SPONSOR_ALIAS, "Do you require visa sponsorship?", "Sponsorship required"],
            "answer": "No",
            "type": "boolean",
        },
        {"id": "relocation", "aliases": ["Are you willing to relocate?"], "answer": "Yes"},
        {"id": "visa_type", "aliases": ["Which visa do you currently hold?"], "answer": "None"},
    ],
    "eeo": {"veteran_status": "I am not a protected veteran"},
}
# ... and with an alias for that wording added
BANK_NOW = copy.deepcopy(BANK_AT_HEAD)
BANK_NOW["screening"][1]["aliases"].append(
    "Do you now, or will you ever, require employment sponsorship"
)

# unit vectors [work_auth, sponsorship, other alias, unlisted label]; cosine =
# dot product, so an unlisted label is near no alias (novel)
VECTORS: dict[str, list[float]] = {
    AUTH_ALIAS: [1.0, 0.0, 0.0, 0.0],
    SPONSOR_ALIAS: [0.0, 1.0, 0.0, 0.0],
    "Do you require visa sponsorship?": [0.0, 1.0, 0.0, 0.0],
    "Sponsorship required": [0.0, 1.0, 0.0, 0.0],
    "Do you now, or will you ever, require employment sponsorship": [0.0, 1.0, 0.0, 0.0],
    "Are you willing to relocate?": [0.0, 0.0, 1.0, 0.0],
    "Which visa do you currently hold?": [0.0, 0.0, 1.0, 0.0],
    SPONSOR_Q: [0.839, 0.544, 0.0, 0.0],  # where fastembed put it: nearer work_auth
    AUTH_Q: [0.898, 0.44, 0.0, 0.0],
}


class FakeEmbedder:
    def embed(self, texts: list[str]) -> Iterator[list[float]]:
        for t in texts:
            yield VECTORS.get(t, [0.0, 0.0, 0.0, 1.0])


@pytest.fixture()
def fake_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(answer_bank, "_embedder", lambda: FakeEmbedder())


def _matcher(bank: dict[str, Any]) -> BankMatcher:
    return BankMatcher(bank=copy.deepcopy(bank), use_embeddings=True)


# the field as capture stored it before the fix
SPONSOR_FIELD: dict[str, Any] = {
    "identity": "question_51000002",
    "label": SPONSOR_Q,
    "type": "multi_value_single_select",
    "required": True,
    "options": ["Yes", "No"],
    "compliance_type": None,
    "match": {
        "kind": "screening",
        "entry_id": "work_auth",
        "source": "answer_bank",
        "similarity": 0.839,
    },
}


# ---- the path before the fix, end to end ------------------------------------


def test_the_path_before_the_fix_gave_yes(
    fake_embedder: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduce it: with the work-status gate switched off (there was
    none), the matcher returns work_auth by embedding at 0.839, the verdict
    capture stored, and staging turns it into "Yes"."""
    monkeypatch.setattr(BankMatcher, "work_status_refusal", lambda self, label, verdict: None)
    m = _matcher(BANK_AT_HEAD)
    before = m.match(SPONSOR_Q, identity="question_51000002")
    assert before == SPONSOR_FIELD["match"]
    verdict = staging_match(m, copy.deepcopy(SPONSOR_FIELD), None)
    assert browser._value_for(verdict, BANK_AT_HEAD, {}, "question_51000002") == (
        "Yes",
        "answer_bank",
    )


def test_staging_never_fills_the_sponsorship_question_with_yes(fake_embedder: None) -> None:
    """Staging re-matches the stored field: the stored work_auth verdict is
    replaced by a blank flag, and nothing goes into the select."""
    verdict = staging_match(_matcher(BANK_AT_HEAD), copy.deepcopy(SPONSOR_FIELD), None)
    assert verdict == {
        "kind": "blank_flag",
        "reason": "sponsorship-needs-human:sponsorship-question-took-work_auth",
    }
    assert browser._value_for(verdict, BANK_AT_HEAD, {}, "question_51000002") == (
        None,
        "unfilled",
    )


def test_with_an_alias_for_it_the_question_takes_sponsorships_no(
    fake_embedder: None,
) -> None:
    verdict = staging_match(_matcher(BANK_NOW), copy.deepcopy(SPONSOR_FIELD), None)
    assert (verdict["kind"], verdict.get("entry_id")) == ("screening", "sponsorship")
    assert browser._value_for(verdict, BANK_NOW, {}, "question_51000002") == ("No", "answer_bank")


def test_the_authorization_question_still_takes_work_auth(fake_embedder: None) -> None:
    v = _matcher(BANK_AT_HEAD).match(AUTH_Q, identity="question_51000001")
    assert (v["kind"], v.get("entry_id")) == ("screening", "work_auth")


def test_the_human_may_still_answer_it(fake_embedder: None) -> None:
    verdict = staging_match(
        _matcher(BANK_AT_HEAD),
        copy.deepcopy(SPONSOR_FIELD),
        None,
        human_answered={"question_51000002"},
    )
    assert verdict == {"kind": "human", "source": "human_typed"}


def test_capture_stores_a_blank_flag_and_never_drafts_it(
    conn: sqlite3.Connection,
    jobkit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_embedder: None,
) -> None:
    """capture_form_specs, the path that stored work_auth: the
    stored verdict is now a blank flag, and the drafter is never asked."""
    (jobkit_home / "data" / "career_corpus.yaml").write_text(
        yaml.safe_dump({"experience": [], "skills": []})
    )
    conn.execute("INSERT INTO companies (id, name, normalized_name) VALUES (1, 'Wonka', 'wonka')")
    conn.execute(
        "INSERT INTO boards (id, company_id, ats, token) VALUES (1, 1, 'greenhouse', 'wonka')"
    )
    jid = conn.execute(
        """INSERT INTO jobs (board_id, company_id, external_id, title, normalized_title,
           company_name, normalized_company, location_bucket, cluster_key, jd_html, jd_hash,
           apply_url, status)
           VALUES (1, 1, '7000001', 'Partner Sales Manager', 'psm', 'Wonka', 'wonka',
                   'remote-us', 'k', '<p>JD</p>', 'h', 'https://x', 'queued')"""
    ).lastrowid
    conn.commit()
    fields = [
        {k: v for k, v in SPONSOR_FIELD.items() if k != "match"},
        {
            "identity": "question_51000001",
            "label": AUTH_Q,
            "type": "multi_value_single_select",
            "required": True,
            "options": ["Yes", "No"],
            "compliance_type": None,
        },
    ]
    monkeypatch.setattr(form_spec, "BankMatcher", lambda: _matcher(BANK_AT_HEAD))
    monkeypatch.setitem(form_spec.FETCHERS, "greenhouse", lambda c, t, e: copy.deepcopy(fields))
    asked: list[str] = []

    def call_json(c: sqlite3.Connection, stage: str, prompt: str, **kw: Any) -> dict[str, Any]:
        asked.append(stage)
        return {"answer": "Yes.", "bullets_used": []}

    monkeypatch.setattr(llm, "call_json", call_json)
    monkeypatch.setattr(llm, "forget", lambda *a: None)
    stats = form_spec.capture_form_specs(conn)
    assert stats["captured"] == 1 and stats["drafted"] == 0 and asked == []
    row = conn.execute("SELECT fields FROM form_specs WHERE job_id=?", (jid,)).fetchone()
    stored = {f["identity"]: f["match"] for f in json.loads(row["fields"])}
    assert stored["question_51000002"] == {
        "kind": "blank_flag",
        "reason": "sponsorship-needs-human:sponsorship-question-took-work_auth",
    }
    assert stored["question_51000001"]["entry_id"] == "work_auth"


# ---- any mention: only the sponsorship entry or an exact alias ---------------


@pytest.mark.parametrize(
    "label",
    [
        SPONSOR_Q,
        "Do you require visa sponsorship?",
        "Will you need an H-1B transfer?",
        "Is your immigration status pending?",
        "Do your deals require executive sponsorship?",
        "Why do you want to work at Visa?",
        "Have you sold event sponsorships?",
    ],
)
def test_mentions_sponsorship(label: str) -> None:
    assert mentions_sponsorship(label)


@pytest.mark.parametrize(
    "label", [AUTH_Q, "Are you willing to relocate?", "Why do you want to work here?"]
)
def test_does_not_mention_sponsorship(label: str) -> None:
    assert not mentions_sponsorship(label)


@pytest.mark.parametrize(
    ("label", "why"),
    [
        # nothing in the bank answers it: never handed to the drafter
        (
            "Do your deals require executive sponsorship?",
            "only-the-sponsorship-entry-answers-not-novel",
        ),
        ("Why do you want to work at Visa?", "only-the-sponsorship-entry-answers-not-novel"),
        ("Is your immigration case pending?", "only-the-sponsorship-entry-answers-not-novel"),
        # another entry's alias inside a label that mentions a visa
        (
            "Are you willing to relocate? We cannot support visa applications.",
            "only-the-sponsorship-entry-answers-not-relocation",
        ),
        # the sponsorship entry by an alias inside a label that does not ask
        # whether the candidate needs sponsorship (sales vocabulary)
        (
            "Executive sponsorship required for enterprise deals: how have you secured it?",
            "not-recognised-as-a-sponsorship-question",
        ),
        # an EEO value for a label that also asks about a visa
        ("Veteran or visa status", "only-the-sponsorship-entry-answers-not-eeo.veteran_status"),
    ],
)
def test_a_sponsorship_label_nothing_may_answer_is_blank_and_flagged(
    fake_embedder: None, label: str, why: str
) -> None:
    assert _matcher(BANK_AT_HEAD).match(label) == {
        "kind": "blank_flag",
        "reason": f"sponsorship-needs-human:{why}",
    }


def test_an_entry_whose_alias_is_the_label_word_for_word_answers(fake_embedder: None) -> None:
    v = _matcher(BANK_AT_HEAD).match("Which visa do you currently hold")
    assert (v["kind"], v.get("entry_id")) == ("screening", "visa_type")
    v = _matcher(BANK_AT_HEAD).match("Sponsorship required?")
    assert (v["kind"], v.get("entry_id")) == ("screening", "sponsorship")


@pytest.mark.parametrize(
    "label",
    [SPONSOR_ALIAS, "Do you require visa sponsorship?", "Will you need visa sponsorship?"],
)
def test_the_sponsorship_entry_answers_sponsorship_questions(
    fake_embedder: None, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    monkeypatch.setitem(VECTORS, label, [0.3, 0.954, 0.0, 0.0])
    v = _matcher(BANK_AT_HEAD).match(label)
    assert (v["kind"], v.get("entry_id")) == ("screening", "sponsorship")


def test_work_auth_never_answers_a_sponsorship_question_even_by_exact_alias(
    fake_embedder: None,
) -> None:
    bank = copy.deepcopy(BANK_AT_HEAD)
    bank["screening"][0]["aliases"].append(SPONSOR_Q)  # the user's mistake
    assert _matcher(bank).match(SPONSOR_Q) == {
        "kind": "blank_flag",
        "reason": "sponsorship-needs-human:sponsorship-question-took-work_auth",
    }


def test_sponsorship_never_answers_an_authorization_question_even_by_exact_alias(
    fake_embedder: None,
) -> None:
    bank = copy.deepcopy(BANK_AT_HEAD)
    bank["screening"][1]["aliases"].append(AUTH_ALIAS)  # the user's mistake
    bank["screening"][0]["aliases"] = ["Work authorization status"]
    assert _matcher(bank).match(AUTH_ALIAS) == {
        "kind": "blank_flag",
        "reason": "work-status-needs-human:authorization-question-took-sponsorship",
    }


def test_never_draft_still_blocks_a_stored_draft(fake_embedder: None) -> None:
    """A draft stored before the gate is withheld at staging: the matcher's
    blank flag outranks drafted_answers."""
    field = {
        "identity": "q",
        "label": "Is your immigration case pending?",
        "match": {"kind": "novel"},
    }
    verdict = staging_match(_matcher(BANK_AT_HEAD), field, None)
    assert verdict["kind"] == "blank_flag"
    assert browser._value_for(verdict, BANK_AT_HEAD, {"q": "No."}, "q") == (None, "unfilled")


# ---- review 2026-10-15: wrong-polarity wordings the gate let through ---------
#
# With the real bge-small embedder against a filled-in bank, the
# reviewer found four labels that took the wrong side's answer:
#   "Do you require work authorization ..."   -> work_auth 0.91  -> "Yes"
#   "Will you require a work permit ..."      -> work_auth 0.877 -> "Yes"
# ("Yes" says the candidate NEEDS a permit), and
#   "... you will not require sponsorship ..." -> sponsorship 0.877 -> "No"
#   "... you won't need visa sponsorship?"     -> sponsorship 0.934 -> "No"
# ("No" says the candidate WILL need sponsorship). work_status_refusal
# returned None for all four. Now: requiring work authorization or a permit
# is a needs-sponsorship question (never work_auth; sponsorship only by an
# alias that is the label word for word), and a negation near the
# work-status words sends the label to the human.

REQUIRES_AUTH = "Do you require work authorization to be employed in the United States?"
REQUIRES_PERMIT = "Will you require a work permit to work in the United States?"
NOT_REQUIRE = "Can you confirm that you will not require sponsorship now or in the future?"
WONT_NEED = "Is it correct that you won't need visa sponsorship?"


@pytest.mark.parametrize(
    ("label", "entry", "similarity", "why"),
    [
        (
            REQUIRES_AUTH,
            "work_auth",
            0.91,
            "work-status-needs-human:requires-authorization-question-took-work_auth",
        ),
        (
            REQUIRES_PERMIT,
            "work_auth",
            0.877,
            "work-status-needs-human:requires-authorization-question-took-work_auth",
        ),
        (NOT_REQUIRE, "sponsorship", 0.877, "sponsorship-needs-human:negated-work-status-question"),
        (WONT_NEED, "sponsorship", 0.934, "sponsorship-needs-human:negated-work-status-question"),
    ],
)
def test_the_reviewers_four_labels_are_refused(
    label: str, entry: str, similarity: float, why: str
) -> None:
    """The reviewer's offline repro, verbatim: before, None for all four."""
    m = BankMatcher(bank=copy.deepcopy(BANK_NOW), use_embeddings=False)
    verdict = {"kind": "screening", "entry_id": entry, "similarity": similarity}
    assert m.work_status_refusal(label, verdict) == why
    # and by alias, short of the label word for word
    assert m.work_status_refusal(label, {"kind": "screening", "entry_id": entry}) == why


@pytest.mark.parametrize(
    ("label", "vector", "why"),
    [
        (
            REQUIRES_AUTH,
            [0.91, 0.415, 0.0, 0.0],
            "work-status-needs-human:requires-authorization-question-took-work_auth",
        ),
        (
            REQUIRES_PERMIT,
            [0.877, 0.48, 0.0, 0.0],
            "work-status-needs-human:requires-authorization-question-took-work_auth",
        ),
        (
            NOT_REQUIRE,
            [0.48, 0.877, 0.0, 0.0],
            "sponsorship-needs-human:negated-work-status-question",
        ),
        (
            WONT_NEED,
            [0.357, 0.934, 0.0, 0.0],
            "sponsorship-needs-human:negated-work-status-question",
        ),
    ],
)
def test_the_reviewers_four_labels_end_to_end(
    fake_embedder: None,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    vector: list[float],
    why: str,
) -> None:
    """Where the real embedder put them: capture and staging leave them
    blank, and nothing reaches the form."""
    monkeypatch.setitem(VECTORS, label, vector)
    m = _matcher(BANK_NOW)
    assert m.match(label) == {"kind": "blank_flag", "reason": why}
    field = {"identity": "q1", "label": label, "match": {"kind": "screening"}}
    verdict = staging_match(m, field, None)
    assert verdict == {"kind": "blank_flag", "reason": why}
    assert browser._value_for(verdict, BANK_NOW, {}, "q1") == (None, "unfilled")


@pytest.mark.parametrize("label", [REQUIRES_AUTH, REQUIRES_PERMIT])
def test_requiring_a_permit_takes_sponsorship_only_word_for_word(
    fake_embedder: None, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    # near sponsorship by embedding: still blank
    monkeypatch.setitem(VECTORS, label, [0.3, 0.954, 0.0, 0.0])
    assert _matcher(BANK_NOW).match(label) == {
        "kind": "blank_flag",
        "reason": "work-status-needs-human:asks-whether-work-authorization-is-required",
    }
    # an alias that IS the label: the user's word, the sponsorship "No"
    bank = copy.deepcopy(BANK_NOW)
    bank["screening"][1]["aliases"].append(label)
    v = _matcher(bank).match(label)
    assert (v["kind"], v.get("entry_id")) == ("screening", "sponsorship")
    # work_auth never, even word for word
    bank = copy.deepcopy(BANK_NOW)
    bank["screening"][0]["aliases"].append(label)
    assert _matcher(bank).match(label) == {
        "kind": "blank_flag",
        "reason": "work-status-needs-human:requires-authorization-question-took-work_auth",
    }


@pytest.mark.parametrize("label", [NOT_REQUIRE, WONT_NEED])
def test_a_negated_sponsorship_label_takes_neither_side_unless_word_for_word(
    fake_embedder: None, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    monkeypatch.setitem(VECTORS, label, [0.84, 0.5426, 0.0, 0.0])  # near work_auth
    assert _matcher(BANK_NOW).match(label)["kind"] == "blank_flag"
    bank = copy.deepcopy(BANK_NOW)
    bank["screening"][1]["aliases"].append(label)  # the user's own word
    v = _matcher(bank).match(label)
    assert (v["kind"], v.get("entry_id")) == ("screening", "sponsorship")


def test_the_real_label_that_also_asks_about_a_work_permit_takes_sponsorship(
    fake_embedder: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A label seen on a real application form: it asks about sponsorship and,
    in the same polarity, about needing a work permit. Both say "Yes" for a
    candidate who needs one, so the sponsorship entry's "No" fits. Before,
    "work permit" read as an authorization question and it was blank.

    Review 2026-10-20: the note after its "?" is another sentence, which
    could as well be a second question or turn the answer around ("Write
    'Yes' if you are a U.S. citizen"), so by embedding it is now blank +
    flag (_asks_besides): an accepted false flag. The user's alias, word
    for word, still answers it."""
    label = (
        "Do you now or in the future require sponsorship for employment visa status? (Write "
        '"Yes" if you require any immigration or work permit/EAD application or renewal - '
        "sponsored by an employer, university or otherwise - either now or at some point in "
        "the future)"
    )
    monkeypatch.setitem(VECTORS, label, [0.3, 0.954, 0.0, 0.0])
    assert _matcher(BANK_NOW).match(label) == {
        "kind": "blank_flag",
        "reason": "sponsorship-needs-human:asks-a-second-question",
    }
    bank = copy.deepcopy(BANK_NOW)
    bank["screening"][1]["aliases"].append(label)  # the user's own word
    v = BankMatcher(bank=bank, use_embeddings=False).match(label)
    assert (v["kind"], v.get("entry_id")) == ("screening", "sponsorship"), v
    monkeypatch.setitem(VECTORS, label, [0.954, 0.3, 0.0, 0.0])
    assert _matcher(BANK_NOW).match(label) == {
        "kind": "blank_flag",
        "reason": "sponsorship-needs-human:sponsorship-question-took-work_auth",
    }


# ---- review 2026-10-15, round 3: a bare "visa sponsorship" mention -----------
#
# _ASKS_SPONSORSHIP's `visas? sponsor\w*` alternative matched the phrase in
# either polarity, so the sponsorship entry's "No" answered labels that ask
# the question the other way round with no negation word. The reviewer's
# offline repro: work_status_refusal(label, {"kind": "screening", "entry_id":
# "sponsorship", "similarity": 0.9}) returned None (allowed) for all four
# below. "No" to each says the candidate NEEDS sponsorship. Now a word that
# turns the question around (exempt, unnecessary, irrelevant, free of/from,
# not applicable, do not need) sends the label to neither side, and by
# embedding the sponsorship entry answers only the require/need wording or
# "sponsor you/your".

REVERSED_LABELS = [
    "Are you exempt from visa sponsorship requirements?",
    "Is visa sponsorship unnecessary in your case?",
    "Would you be able to start immediately, free of any visa sponsorship requirement?",
    "Are you a US citizen or permanent resident (i.e., visa sponsorship is irrelevant to you)?",
    # …and the rest of the vocabulary
    "Is visa sponsorship not applicable to you?",
    "Are you free from any need for visa sponsorship?",
    "Confirm you do not need visa sponsorship.",
    "Visa sponsorship: N/A for you?",
]


@pytest.mark.parametrize("label", REVERSED_LABELS)
@pytest.mark.parametrize("similarity", [0.9, None], ids=["embedding", "alias"])
def test_a_reversed_sponsorship_label_is_blank_and_flagged(
    label: str, similarity: float | None
) -> None:
    m = BankMatcher(bank=copy.deepcopy(BANK_NOW), use_embeddings=False)
    verdict: dict[str, Any] = {"kind": "screening", "entry_id": "sponsorship"}
    if similarity is not None:
        verdict["similarity"] = similarity
    why = m.work_status_refusal(label, verdict)
    assert why is not None and why.startswith("sponsorship-needs-human:"), why
    # and work_auth's "Yes" never answers it either
    assert m.work_status_refusal(label, {**verdict, "entry_id": "work_auth"}) is not None


@pytest.mark.parametrize("label", REVERSED_LABELS[:4])
def test_the_reviewers_reversed_labels_end_to_end(
    fake_embedder: None, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    """Placed nearest the sponsorship alias by the embedder: capture and
    staging leave the field blank, and nothing reaches the form."""
    monkeypatch.setitem(VECTORS, label, [0.3, 0.954, 0.0, 0.0])
    m = _matcher(BANK_NOW)
    want = {"kind": "blank_flag", "reason": "sponsorship-needs-human:reversed-work-status-question"}
    assert m.match(label) == want
    field = {"identity": "q1", "label": label, "match": {"kind": "screening"}}
    verdict = staging_match(m, field, None)
    assert verdict == want
    assert browser._value_for(verdict, BANK_NOW, {}, "q1") == (None, "unfilled")


@pytest.mark.parametrize(
    "label",
    [
        # a mention with no require/need and no reversing word: the embedder
        # cannot tell which way it asks
        "Visa sponsorship",
        "Visa sponsorship status",
        "Is visa sponsorship a non-issue for you?",
    ],
)
def test_by_embedding_a_bare_visa_sponsorship_mention_is_not_the_question(
    fake_embedder: None, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    monkeypatch.setitem(VECTORS, label, [0.3, 0.954, 0.0, 0.0])
    assert _matcher(BANK_NOW).match(label) == {
        "kind": "blank_flag",
        "reason": "sponsorship-needs-human:embedding-not-a-sponsorship-question",
    }


def test_a_reversed_label_answers_only_by_an_alias_that_is_the_label() -> None:
    label = REVERSED_LABELS[0]
    bank = copy.deepcopy(BANK_NOW)
    bank["screening"][1]["aliases"].append(label)  # the user's own word
    m = BankMatcher(bank=bank, use_embeddings=False)
    v = m.match(label)
    assert (v["kind"], v.get("entry_id")) == ("screening", "sponsorship")


@pytest.mark.parametrize(
    "label",
    [
        SPONSOR_Q,
        SPONSOR_ALIAS,
        "Will you now or in the future require visa sponsorship?",
        "Will you now or in the future require sponsorship for employment visa status (e.g., "
        "H-1B visa status)?",
        "Will you now or will you in the future require employment visa sponsorship to work in "
        "the country in which the job you're applying for is located?",
        "Do you now or will you in the future need sponsorship for employment visa status in the "
        "country in which you are applying?",
        "Will you now or in the future require Initech to sponsor an immigration case in order to "
        "employ you?",
        "Do you require visa sponsorship?",
    ],
)
def test_the_standard_wordings_still_answer_no_by_embedding(
    fake_embedder: None, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    """The require/need wordings (the one above, and labels seen on real
    application forms) still take the sponsorship entry's "No" when the
    embedder puts them nearest it."""
    monkeypatch.setitem(VECTORS, label, [0.3, 0.954, 0.0, 0.0])
    m = _matcher(BANK_AT_HEAD)
    v = m.match(label)
    assert (v["kind"], v.get("entry_id")) == ("screening", "sponsorship"), v
    assert "similarity" in v or label in BANK_AT_HEAD["screening"][1]["aliases"]
    assert browser._value_for(v, BANK_AT_HEAD, {}, "q1") == ("No", "answer_bank")


def test_that_wording_still_answers_no_by_the_users_alias(fake_embedder: None) -> None:
    v = _matcher(BANK_NOW).match(SPONSOR_Q)
    assert (v["kind"], v.get("entry_id")) == ("screening", "sponsorship")
    assert browser._value_for(v, BANK_NOW, {}, "q1") == ("No", "answer_bank")


# ---- review 2026-10-15, round 4: an allowlist of question shapes -------------
#
# _ASKS_NEEDS_SPONSORSHIP took require/need ANYWHERE before the sponsor word,
# and _REVERSED listed the words that turn the question around. The
# reviewer's offline repro: work_status_refusal(label, {"kind": "screening",
# "entry_id": "sponsorship", "similarity": 0.9}) returned None (the "No"
# allowed) for each label below; a candidate who needs no sponsorship answers
# each "Yes", so "No" says the opposite (the failure above). No list of
# reversing words can fail closed. By embedding, the sponsorship entry now
# answers only a label that IS the plain question: "you" the subject of
# require/need, only connector words between them.

ROUND_4_LABELS = [
    "Are you excused from needing visa sponsorship?",
    "Can you rule out needing visa sponsorship?",
    "Do you lack any need for visa sponsorship?",
    "Is your need for visa sponsorship nonexistent?",
    "Are you able to work here with zero need for visa sponsorship?",
    "Is it true that you require zero sponsorship?",
    "Are you relieved of any requirement for visa sponsorship?",
    "Have you been released from needing visa sponsorship?",
    "Can you work for us minus any need for sponsorship?",
    "Are you beyond needing sponsorship?",
    "Can you avoid needing visa sponsorship?",
    "Would you be able to forgo needing sponsorship?",
    "Is needing sponsorship off the table for you?",
    "Is the need to sponsor you nonexistent?",
    "Are you outside the group of candidates who require sponsorship?",
]
# …and neighbours that open like the question but are not it
ROUND_4_NEIGHBOURS = [
    "Do you require zero sponsorship?",
    "Will you need nothing but sponsorship?",
    "Will nobody need to sponsor you?",
    "Do you require sponsorship, or are you able to start now?",
    "Do you now, or will you ever, avoid needing sponsorship?",
]


@pytest.mark.parametrize("label", ROUND_4_LABELS + ROUND_4_NEIGHBOURS)
def test_the_round_4_labels_are_blank_and_flagged_by_embedding(label: str) -> None:
    """The reviewer's offline repro, verbatim: before, None for all 15."""
    m = BankMatcher(bank=copy.deepcopy(BANK_NOW), use_embeddings=False)
    verdict = {"kind": "screening", "entry_id": "sponsorship", "similarity": 0.9}
    why = m.work_status_refusal(label, verdict)
    assert why is not None and why.startswith("sponsorship-needs-human:"), why
    # and work_auth's "Yes" never answers it either
    assert m.work_status_refusal(label, {**verdict, "entry_id": "work_auth"}) is not None


@pytest.mark.parametrize("label", ROUND_4_LABELS)
def test_the_round_4_labels_end_to_end(
    fake_embedder: None, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    """Placed nearest the sponsorship alias, as the embedder very likely
    would (they share its vocabulary): capture and staging leave the field
    blank, and nothing reaches the form."""
    monkeypatch.setitem(VECTORS, label, [0.3, 0.954, 0.0, 0.0])
    m = _matcher(BANK_NOW)
    want = {
        "kind": "blank_flag",
        "reason": "sponsorship-needs-human:embedding-not-a-sponsorship-question",
    }
    assert m.match(label) == want
    field = {"identity": "q1", "label": label, "match": {"kind": "screening"}}
    verdict = staging_match(m, field, None)
    assert verdict == want
    assert browser._value_for(verdict, BANK_NOW, {}, "q1") == (None, "unfilled")


@pytest.mark.parametrize(
    "label",
    [
        # other wordings seen on real application forms that ask only
        # whether the candidate needs sponsorship
        "Will you now or in the future require Vandelay to commence (“sponsor”) an immigration "
        "case in order to employ you?",
        "Will you now or in the future require Globex to sponsor an immigration case in order to "
        "employ you?",
        "Will you now or in the future require sponsorship for a visa to remain in your current "
        "location?",
        "Will you now, or in the future,  require visa sponsorship for employment visa status "
        "(e.g. H-1B visa status)?",
        "WILL YOU NOW OR IN THE FUTURE REQUIRE SPONSORSHIP FOR EMPLOYMENT VISA STATUS (E.G., "
        "H-1B, TN, E-3, F-1 VISA STATUS)?",
        "Will you require Visa Sponsorship now, or in the future?",
        "Do you require sponsorship now or in the future? ✱",
        "Do you now, or will you in the future require Visa sponsorship?",
        "Will you require sponsorship if you join Umbrella?",
        # other shapes of the same question
        "Will you be requiring sponsorship?",
        "Would you need us to sponsor your visa?",
        "Will you need an H-1B transfer?",
        "Will we need to sponsor you?",
        "Will Acme need to sponsor you for a visa?",
    ],
)
def test_the_plain_question_still_answers_no_by_embedding(
    fake_embedder: None, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    monkeypatch.setitem(VECTORS, label, [0.3, 0.954, 0.0, 0.0])
    v = _matcher(BANK_AT_HEAD).match(label)
    assert (v["kind"], v.get("entry_id")) == ("screening", "sponsorship"), v
    assert browser._value_for(v, BANK_AT_HEAD, {}, "q1") == ("No", "answer_bank")


def test_a_round_4_label_answers_only_by_an_alias_that_is_the_label() -> None:
    label = ROUND_4_LABELS[1]
    bank = copy.deepcopy(BANK_NOW)
    bank["screening"][1]["aliases"].append(label)  # the user's own word
    v = BankMatcher(bank=bank, use_embeddings=False).match(label)
    assert (v["kind"], v.get("entry_id")) == ("screening", "sponsorship")


# ---- review 2026-10-20: a second question after the sponsorship question -----
#
# _asks_needs_sponsorship found a second question only as "…, or are you…"
# (_NEW_QUESTION), and the alias branch looked for none: "Do you require visa
# sponsorship? Are you a U.S. citizen or green card holder?" took the
# sponsorship entry's single "No" by embedding (work_status_refusal returned
# None) and by the alias "Do you require visa sponsorship?" inside it. A
# citizen answers the second question "Yes". Now any other sentence or
# question with words in it, before or after, leaves the field blank and
# flagged; only required/optional markers and a Yes/No hint may stand beside.

SECOND_QUESTION_LABELS = [
    # the reviewer's three
    "Do you require visa sponsorship? Are you a U.S. citizen or green card holder?",
    "Will you now or in the future require sponsorship? Are you currently a U.S. citizen?",
    "Do you require visa sponsorship? Is your current status permanent?",
    # …and their neighbours: a question before it, a sentence after it, a
    # semicolon, an exclamation
    "Are you a U.S. citizen? Do you require visa sponsorship?",
    "Do you require visa sponsorship? Please also state your citizenship.",
    "Do you require visa sponsorship; if so, which visa do you hold?",
    "Do you require visa sponsorship! Are you a citizen?",
]


@pytest.mark.parametrize("label", SECOND_QUESTION_LABELS)
def test_a_second_question_is_blank_and_flagged_by_embedding(label: str) -> None:
    m = BankMatcher(bank=copy.deepcopy(BANK_NOW), use_embeddings=False)
    verdict = {"kind": "screening", "entry_id": "sponsorship", "similarity": 0.9}
    why = m.work_status_refusal(label, verdict)
    assert why is not None and why.startswith("sponsorship-needs-human:"), why
    assert m.work_status_refusal(label, {**verdict, "entry_id": "work_auth"}) is not None


@pytest.mark.parametrize("label", SECOND_QUESTION_LABELS)
def test_a_second_question_is_blank_and_flagged_by_alias(label: str) -> None:
    m = BankMatcher(bank=copy.deepcopy(BANK_NOW), use_embeddings=False)
    verdict = {"kind": "screening", "entry_id": "sponsorship", "source": "answer_bank"}
    why = m.work_status_refusal(label, verdict)
    assert why is not None and why.startswith("sponsorship-needs-human:"), why
    v = m.match(label)
    assert v["kind"] == "blank_flag" and v["reason"].startswith("sponsorship-needs-human:"), v
    assert browser._value_for(v, BANK_NOW, {}, "q1") == (None, "unfilled")


@pytest.mark.parametrize("label", SECOND_QUESTION_LABELS[:3])
def test_a_second_question_end_to_end(
    fake_embedder: None, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    monkeypatch.setitem(VECTORS, label, [0.3, 0.954, 0.0, 0.0])
    m = _matcher(BANK_NOW)
    want = {"kind": "blank_flag", "reason": "sponsorship-needs-human:asks-a-second-question"}
    assert m.match(label) == want
    field = {"identity": "q1", "label": label, "match": {"kind": "screening"}}
    assert staging_match(m, field, None) == want


@pytest.mark.parametrize(
    "label",
    [
        # markers and a Yes/No hint beside the one question
        "Do you require visa sponsorship? (Yes/No)",
        "Do you require visa sponsorship? *",
        "Do you require visa sponsorship? (Required)",
        "Yes or no: will you now or in the future require sponsorship?",
        "Will you now or in the future require sponsorship for employment visa status (e.g., "
        "H-1B visa status)?",
    ],
)
def test_one_question_with_markers_still_answers_no(
    fake_embedder: None, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    monkeypatch.setitem(VECTORS, label, [0.3, 0.954, 0.0, 0.0])
    v = _matcher(BANK_AT_HEAD).match(label)
    assert (v["kind"], v.get("entry_id")) == ("screening", "sponsorship"), v
    assert browser._value_for(v, BANK_AT_HEAD, {}, "q1") == ("No", "answer_bank")


def test_a_second_question_answers_only_by_an_alias_that_is_the_label() -> None:
    label = SECOND_QUESTION_LABELS[0]
    bank = copy.deepcopy(BANK_NOW)
    bank["screening"][1]["aliases"].append(label)  # the user's own word
    v = BankMatcher(bank=bank, use_embeddings=False).match(label)
    assert (v["kind"], v.get("entry_id")) == ("screening", "sponsorship")


# ---- review 2026-10-20, round 3: what follows the sponsor or visa word -------
#
# _ASKS_NEEDS_SPONSORSHIP checked the question's shape up to the sponsor or
# visa word and took any words after it. The reviewer's offline repro: with
# the label placed nearest the sponsorship alias (VECTORS[label] = [0.3,
# 0.954, 0, 0]), _matcher(BANK_AT_HEAD).match(label) returned {kind:
# screening, entry_id: sponsorship, similarity: 0.954} and _value_for gave
# ("No", "answer_bank") for each of the five labels below, and
# work_status_refusal returned None. The "No" answers relocation assistance,
# another country, another person, travel. By embedding, only the endings on
# the allowlist (_SPONSOR_TAIL) may follow the sponsor or visa word now.

UNLISTED_ENDING_LABELS = [
    # the reviewer's five
    "Do you require visa sponsorship or relocation assistance?",
    "Do you require visa sponsorship to work in the UK?",
    "Would you require visa sponsorship to work in Canada?",
    "Will you need visa sponsorship for any family members?",
    "Will you need a visa to travel to China for this role?",
    # …and neighbours: an allowed piece first, then something else
    "Will you require sponsorship to work in the United States or Canada?",
    "Do you require visa sponsorship to work in the US for a different employer?",
    "Will you require sponsorship for this role or any other role?",
    "Do you require visa sponsorship for your spouse?",
    "Do you require visa sponsorship now or in the future (e.g., for your spouse)?",
    "Will you now or in the future require sponsorship and a relocation package?",
    "Will you require sponsorship if you join our London office?",
    "Do you need a visa to work in Germany?",
]
UNLISTED = "sponsorship-needs-human:embedding-unlisted-words-after-sponsorship"


@pytest.mark.parametrize("label", UNLISTED_ENDING_LABELS)
def test_an_unlisted_ending_is_blank_and_flagged_by_embedding(label: str) -> None:
    m = BankMatcher(bank=copy.deepcopy(BANK_AT_HEAD), use_embeddings=False)
    verdict = {"kind": "screening", "entry_id": "sponsorship", "similarity": 0.9}
    assert m.work_status_refusal(label, verdict) == UNLISTED
    # and work_auth's "Yes" never answers it either
    assert m.work_status_refusal(label, {**verdict, "entry_id": "work_auth"}) is not None


@pytest.mark.parametrize("label", UNLISTED_ENDING_LABELS[:5])
def test_the_reviewers_unlisted_endings_end_to_end(
    fake_embedder: None, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    """The reviewer's repro, verbatim: before, sponsorship at 0.954 and
    ("No", "answer_bank"). Capture and staging now leave it blank."""
    monkeypatch.setitem(VECTORS, label, [0.3, 0.954, 0.0, 0.0])
    m = _matcher(BANK_AT_HEAD)
    want = {"kind": "blank_flag", "reason": UNLISTED}
    assert m.match(label) == want
    field = {"identity": "q1", "label": label, "match": {"kind": "screening"}}
    verdict = staging_match(m, field, None)
    assert verdict == want
    assert browser._value_for(verdict, BANK_AT_HEAD, {}, "q1") == (None, "unfilled")


# Labels seen on real application forms (2026-10-20, company names replaced)
# that mention sponsor and that the sponsorship entry answered by embedding
# before this change: each still takes the "No". (Other real labels were
# already blank + flag for other reasons, e.g. they also ask about work
# authorization or add a second sentence; unchanged.)
LIVE_SPONSORSHIP_WORDINGS = [
    SPONSOR_Q,
    "Do you now or will you in the future need sponsorship for employment visa status in the "
    "country in which you are applying?",
    "Do you now, or will you in the future require Visa sponsorship?",
    "Do you require sponsorship now or in the future? ✱",
    "Do you require visa sponsorship? ",
    "WILL YOU NOW OR IN THE FUTURE REQUIRE SPONSORSHIP FOR EMPLOYMENT VISA STATUS (E.G., H-1B, "
    "TN, E-3, F-1 VISA STATUS)?\n",
    "Will you now or in the future require Globex to sponsor an immigration case in order to "
    "employ you?",
    "Will you now or in the future require Initech to sponsor an immigration case in order to "
    "employ you?",
    "Will you now or in the future require Vandelay to commence (“sponsor”) an immigration case "
    "in order to employ you? ",
    "Will you now or in the future require employer sponsorship for employment visa status "
    "(e.g., H-1B, O-1, TN)?",
    "Will you now or in the future require employment visa sponsorship to work in the country "
    "in which the job you're applying for is located?",
    "Will you now or in the future require sponsorship for a visa to remain in your current "
    "location?",
    "Will you now or in the future require sponsorship for employment visa status (e.g., H-1B "
    "visa status)?",
    "Will you now or in the future require sponsorship for employment visa status (e.g., H-1B, "
    "TN, etc.)?",
    "Will you now or in the future require sponsorship for employment visa status (i.e. H-1B "
    "visa status)?",
    "Will you now or in the future require sponsorship for employment visa status in the United "
    "States?",
    "Will you now or in the future require visa sponsorship to work in country in which the job "
    "you're applying for is located?",
    "Will you now or in the future require visa sponsorship to work in the United States?",
    "Will you now or in the future require visa sponsorship to work in the country where this "
    "position is located?",
    "Will you now or in the future require visa sponsorship?",
    "Will you now or will you in the future require employment visa sponsorship to work in the "
    "country in which the job you're applying for is located?\n",
    "Will you now or will you in the future require employment visa sponsorship to work in the "
    "country in which the job you're applying for is located? ",
    "Will you now or will you in the future require employment visa sponsorship?",
    "Will you now, or in the future,\xa0 require visa sponsorship for employment visa status "
    "(e.g. H-1B visa status)?",
    "Will you require Visa Sponsorship now, or in the future?",
    "Will you require sponsorship if you join Umbrella?",
]
# the endings the allowlist names, each after the plain question
ALLOWED_ENDINGS = [
    "Do you require visa sponsorship?",
    "Do you require visa sponsorship (e.g., H-1B)?",
    "Will you require visa sponsorship now or in the future?",
    "Will you require visa sponsorship for this role?",
    "Will you require visa sponsorship for this position?",
    "Will you require visa sponsorship for this job?",
    "Will you require visa sponsorship for employment?",
    "Will you require visa sponsorship to work in the US?",
    "Will you require visa sponsorship to work in the U.S.?",
    "Will you require visa sponsorship to work in the United States?",
    "Will you require visa sponsorship to work in the country where this job is located?",
    "Will you require visa sponsorship, now or in the future, for this role (e.g. H-1B, TN)?",
    "Do you require visa sponsorship (Yes/No)?",
]


@pytest.mark.parametrize("label", LIVE_SPONSORSHIP_WORDINGS + ALLOWED_ENDINGS)
def test_the_live_wordings_and_allowed_endings_still_answer_no_by_embedding(
    fake_embedder: None, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    for text in (label, label.strip()):  # the matcher embeds the label stripped
        monkeypatch.setitem(VECTORS, text, [0.3, 0.954, 0.0, 0.0])
    m = _matcher(BANK_AT_HEAD)
    v = m.match(label)
    assert (v["kind"], v.get("entry_id")) == ("screening", "sponsorship"), v
    # by embedding, unless the label is one of the entry's aliases word for word
    if label.strip() not in BANK_AT_HEAD["screening"][1]["aliases"]:
        assert v.get("similarity") == pytest.approx(0.954, abs=1e-3), v
    assert browser._value_for(v, BANK_AT_HEAD, {}, "q1") == ("No", "answer_bank")
    field = {"identity": "q1", "label": label, "match": {"kind": "screening"}}
    assert staging_match(m, field, None) == v


def test_an_unlisted_ending_answers_only_by_an_alias_that_is_the_label() -> None:
    label = UNLISTED_ENDING_LABELS[0]
    bank = copy.deepcopy(BANK_AT_HEAD)
    bank["screening"][1]["aliases"].append(label)  # the user's own word
    v = BankMatcher(bank=bank, use_embeddings=False).match(label)
    assert (v["kind"], v.get("entry_id")) == ("screening", "sponsorship")


# ---- review 2026-10-20, round 4: the words before the sponsor or visa word ----
#
# _plain_ending checked only the text after the END of the
# _ASKS_NEEDS_SPONSORSHIP match. That match takes up to 60 characters
# between require/need and the sponsor word, so with a visa word first and a
# sponsor word later everything between went unread, and any noun before the
# word did too. The reviewer's offline repro: with the label placed nearest
# the sponsorship alias (VECTORS[label] = [0.3, 0.954, 0, 0]),
# _matcher(BANK_AT_HEAD).match(label) took the sponsorship entry and
# _value_for gave ("No", "answer_bank") for each label below, and
# work_status_refusal(label, {kind: screening, entry_id: sponsorship,
# similarity: 0.9}) returned None. The "No" answers relocation, another
# country, another person, travel. The allowlist now runs from the verb:
# only an article, the kind of sponsorship or visa, or "<who> to (commence)"
# before the FIRST sponsor or visa word, then the tail allowlist.

# a visa word first, a sponsor word later: the words after the visa word
AFTER_THE_VISA_WORD_LABELS = [
    "Do you require a visa or relocation sponsorship?",
    "Do you require visa or relocation sponsorship?",
    "Will you need a visa to work in the UK or sponsorship?",
    "Will you need a visa to travel to China, or sponsorship?",
    "Will you need a visa to travel to China for this role, or sponsorship?",
    "Do you require a visa for your spouse or sponsorship?",
    # …and a neighbour
    "Will you need a work visa for your family?",
]
# a noun phrase before the sponsor or visa word
BEFORE_THE_WORD_LABELS = [
    "Do you require relocation assistance or visa sponsorship?",
    "Do you require UK visa sponsorship?",
    "Will you need a Canadian work visa?",
    "Will you need a China travel visa for this role?",
    "Do you require family visa sponsorship?",
    # …and neighbours
    "Do you require relocation or visa sponsorship?",
    "Will you need a student visa?",
    "Do you require your spouse to sponsor you?",
    "Do you require the Canadian government's sponsorship?",
]
BEFORE = "sponsorship-needs-human:embedding-unlisted-words-before-sponsorship"


def _round_4_reason(label: str) -> str:
    return BEFORE if label in BEFORE_THE_WORD_LABELS else UNLISTED


@pytest.mark.parametrize("label", AFTER_THE_VISA_WORD_LABELS + BEFORE_THE_WORD_LABELS)
def test_unlisted_words_before_the_word_are_blank_and_flagged_by_embedding(label: str) -> None:
    m = BankMatcher(bank=copy.deepcopy(BANK_AT_HEAD), use_embeddings=False)
    verdict = {"kind": "screening", "entry_id": "sponsorship", "similarity": 0.9}
    assert m.work_status_refusal(label, verdict) == _round_4_reason(label)
    # and work_auth's "Yes" never answers it either
    assert m.work_status_refusal(label, {**verdict, "entry_id": "work_auth"}) is not None


@pytest.mark.parametrize("label", AFTER_THE_VISA_WORD_LABELS + BEFORE_THE_WORD_LABELS)
def test_unlisted_words_before_the_word_end_to_end(
    fake_embedder: None, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    """The reviewer's repro, verbatim: before, the sponsorship entry by
    embedding and ("No", "answer_bank"). Capture and staging now leave it
    blank, and nothing reaches the form."""
    monkeypatch.setitem(VECTORS, label, [0.3, 0.954, 0.0, 0.0])
    m = _matcher(BANK_AT_HEAD)
    want = {"kind": "blank_flag", "reason": _round_4_reason(label)}
    assert m.match(label) == want
    field = {"identity": "q1", "label": label, "match": {"kind": "screening"}}
    verdict = staging_match(m, field, None)
    assert verdict == want
    assert browser._value_for(verdict, BANK_AT_HEAD, {}, "q1") == (None, "unfilled")


@pytest.mark.parametrize(
    "label",
    [
        # an article and the kind of sponsorship or visa
        "Do you require employer sponsorship?",
        "Will you require employment visa sponsorship?",
        "Will you require immigration sponsorship?",
        "Will you need a work visa?",
        "Will you need an employment visa?",
        "Do you require H-1B sponsorship?",
        "Do you require H-1B visa sponsorship to work in the United States?",
        "Do you require U.S. visa sponsorship?",
        # who sponsors
        "Will you require the company to sponsor you?",
        "Will you now or in the future require Acme to sponsor your visa?",
        "Would you need us to sponsor your visa?",
    ],
)
def test_allowlisted_words_before_the_word_still_answer_no_by_embedding(
    fake_embedder: None, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    monkeypatch.setitem(VECTORS, label, [0.3, 0.954, 0.0, 0.0])
    m = _matcher(BANK_AT_HEAD)
    v = m.match(label)
    assert (v["kind"], v.get("entry_id")) == ("screening", "sponsorship"), v
    assert v.get("similarity") == pytest.approx(0.954, abs=1e-3), v
    assert browser._value_for(v, BANK_AT_HEAD, {}, "q1") == ("No", "answer_bank")
    field = {"identity": "q1", "label": label, "match": {"kind": "screening"}}
    assert staging_match(m, field, None) == v


def test_unlisted_words_before_the_word_answer_only_by_an_alias_that_is_the_label() -> None:
    label = BEFORE_THE_WORD_LABELS[0]
    bank = copy.deepcopy(BANK_AT_HEAD)
    bank["screening"][1]["aliases"].append(label)  # the user's own word
    v = BankMatcher(bank=bank, use_embeddings=False).match(label)
    assert (v["kind"], v.get("entry_id")) == ("screening", "sponsorship")


# --- review 2026-10-20, round 5: an alias is matched as a substring, so the
# ending allowlist holds on the alias path too (the user's alias, BANK_NOW)

ALIAS_SUBSTRING_UNLISTED_ENDINGS = [
    "Do you now, or will you ever, require employment sponsorship to work in Canada?",
    "Do you now, or will you ever, require employment sponsorship or relocation assistance?",
    "Do you now, or will you ever, require employment sponsorship for any family members?",
]


@pytest.mark.parametrize("label", ALIAS_SUBSTRING_UNLISTED_ENDINGS)
def test_an_alias_inside_the_label_does_not_answer_an_unlisted_ending(label: str) -> None:
    m = BankMatcher(bank=BANK_NOW, use_embeddings=False)
    v = m.match(label)
    assert v["kind"] == "blank_flag", v
    assert "unlisted-words-after-sponsorship" in str(v.get("reason")), v
    field = {"identity": "q1", "label": label, "match": {"kind": "screening"}}
    assert staging_match(m, field, None)["kind"] == "blank_flag"


@pytest.mark.parametrize(
    "label",
    [
        "Do you now, or will you ever, require employment sponsorship?",
        "Do you now, or will you ever, require employment sponsorship to work in the "
        "country where this job is located?",
        "Do you now, or will you ever, require employment sponsorship for this role?",
    ],
)
def test_an_alias_inside_the_label_still_answers_an_allowlisted_ending(label: str) -> None:
    m = BankMatcher(bank=BANK_NOW, use_embeddings=False)
    v = m.match(label)
    assert (v["kind"], v.get("entry_id")) == ("screening", "sponsorship"), v
    assert browser._value_for(v, BANK_NOW, {}, "q1") == ("No", "answer_bank")
