"""MANDATORY adversarial tests for every employer-facing claim (audit
2026-10-08 §2 item 3): cover letters and drafted novel-question answers are
fact-checked against the labeled bullets they cite, and a failing one is never
written as final. Offline: every LLM call is mocked, nothing is rendered."""

from __future__ import annotations

import copy
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from jobkit import llm
from jobkit.autofill import form_spec
from jobkit.config import DEFAULT_COVER_ATTEMPTS
from jobkit.pipeline import tailor
from jobkit.validate import authenticity, claim_judge
from jobkit.validate.claims import (
    _no_candidate_claim,
    asks_target_tenure,
    drafting_profile,
    has_hard,
    labeled_sources_for_prompt,
    plain_no,
    validate_claims,
)
from jobkit.validate.provenance import ProvenanceResult

CORPUS: dict[str, Any] = {
    "identity": {
        "name": "Test Candidate",
        "phone": "+1 555 555 0100",
        "email": "candidate@example.com",
        "links": {"linkedin": "linkedin.com/in/example"},
    },
    "roles": [
        {
            "id": "role_haulmark",
            "company": "Haulmark",
            "title": "Regional Sales Manager",
            "start": "2024-03",
            "end": None,
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
    "skill_index": {
        "Salesforce": "hands-on",
        "Gong": "hands-on",
        "MEDDIC": "taught it to the AE team",
        "Channel Partnerships": "b_052",
    },
    "constraints": {
        "never_claim_titles": [
            "VP",
            "Vice President",
            "SVP",
            "CRO",
            "Chief Revenue Officer",
            "Head of Sales",
            "Director of Sales",
            "General Manager",
        ],
        "never_claim_phrases": ["treasury management sales"],
    },
    "positioning": [
        {
            "id": "pos_ops",
            "archetype": "revenue operations",
            "text": "Builds the deal desks and pricing a sales team runs on.",
        }
    ],
    "voice_samples": [{"id": "v_001", "text": "I'd rather walk you through the spreadsheet."}],
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
            "id": "b_049",
            "role_id": "role_ledgerline",
            "metric_confidence": "verified",
            "text_long": "Launched usage-based pricing for the SMB lending line, adding $2.4M in "
            "net-new ARR in its first year across 600+ merchant accounts.",
            "text_short": "Launched usage-based pricing for SMB lending: $2.4M net-new ARR in "
            "its first year (600+ merchant accounts).",
            "metric": "$2.4M net-new ARR; 600+ merchant accounts",
        },
        {
            "id": "b_046",
            "role_id": "role_ledgerline",
            "metric_confidence": "estimate",
            "text_long": "Moved commission tracking off spreadsheets into Salesforce and rebuilt "
            "the sales reports in Looker, cutting the month-end commission run from ~6 days to 2.",
            "text_short": "Commissions into Salesforce, reports into Looker; month-end commission "
            "run ~6 days → 2.",
            "metric": "month-end commission run ~6 days → 2",
        },
        {
            "id": "b_052",
            "role_id": "role_haulmark",
            "metric_confidence": "verified",
            "text_long": "Led a team of 9 account executives, reporting to the General Manager, "
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
            "text_short": "Northeast bookings ~$3.2M → ~$5.6M a year; 41 new carrier accounts; "
            "~18% partner-sourced by 2025.",
            "metric": "~$3.2M → ~$5.6M annual bookings; 41 new accounts; ~18% partner-sourced",
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
            "id": "b_058",
            "role_id": "role_marlow",
            "metric_confidence": "verified",
            "text_long": "Made President's Club in 2019 and 2020, closing 124% and then 131% of "
            "annual quota on mid-market payroll deals.",
            "text_short": "President's Club 2019 and 2020 (124%, then 131% of annual quota).",
            "metric": "President's Club 2019, 2020; 124% and 131% of annual quota",
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
        {
            "id": "b_057",
            "role_id": "role_ledgerline",
            "metric_confidence": "none",
            "text_long": "Wrote the territory-planning model the sales team uses each January.",
            "text_short": "Wrote the sales team's territory-planning model.",
            "metric": None,
        },
    ],
}

FABRIKAM_JD = (
    "Fabrikam Payments has more than 4,000 contractor shops on its platform, mostly "
    "independent HVAC and plumbing businesses that send invoices and take payments through "
    "it, and it doubled its payment volume in 2025. We are hiring a Director of Revenue "
    "Operations to report to the CFO. The director runs the weekly revenue meeting, owns the "
    "forecast and the systems behind it, and keeps the leadership dashboards current. We "
    "want someone who has sold, has managed people and likes building tools other people use "
    "every day. US remote."
)

# P1 restates the posting in its own words; the comment on that fact is a
# sentence of its own (fifth review of validator-refine: a sentence that
# borrows the JD's "doubled" says nothing the posting does not)
GOOD_LETTER = (
    "Fabrikam Payments doubled its payment volume in 2025. Growth like that usually outruns "
    "the forecast first.\n\n"
    "Since March 2024 I have been a Regional Sales Manager at Haulmark, where I grew the "
    "Northeast region from ~$3.2M to ~$5.6M in annual bookings. I also led 9 AEs there, "
    "reporting to the General Manager, and built the channel-partner program from 3 to 17 "
    "reseller partners in ~18 months. Earlier, as Head of Revenue Operations at ledgerline, I "
    "launched usage-based pricing for the SMB lending line, adding $2.4M in net-new ARR in its "
    "first year.\n\n"
    "I coach deals on MEDDIC and live in Salesforce. Contractors are a new market for me, and "
    "I would want to learn how they buy."
)
GOOD_CITED = ["b_047", "b_052", "b_049"]

# a letter that credits the ~$5.6M Haulmark bookings to the separate $2.4M of
# ledgerline pricing revenue (two employers' results run together)
CROSS_ROLE_LETTER = (
    "At Haulmark I grew annual bookings to ~$5.6M. Much of that came from usage-based "
    "pricing for merchant accounts and SMB lending."
)
# a letter drafted for one company (Lumon), checked against another's job
LUMON_LETTER = (
    "I'm writing about the Director of Revenue Operations opening at Lumon. At Haulmark I "
    "grew the Northeast region from ~$3.2M to ~$5.6M in annual bookings, and Lumon is where "
    "I'd like to do that next."
)
# a drafted answer that invents a VP of Sales: b_052 names only the
# General Manager, and as someone the candidate reported to
VP_MENTION_ANSWER = "Our VP of Sales asked me to build the channel-partner program."

KNOWN = ["Lumon", "Fabrikam Payments", "Tailspin", "Hooli", "Pilot", "Haulmark"]


def _check(text: str, cited: list[str], **kw: Any) -> list[dict[str, str]]:
    kw.setdefault("target_company", "Fabrikam Payments")
    kw.setdefault("job_title", "Director of Revenue Operations")
    kw.setdefault("jd_text", FABRIKAM_JD)
    kw.setdefault("known_companies", KNOWN)
    out = validate_claims(text, cited, CORPUS, **kw)
    assert out.ok == (not out.violations)
    return out.violations


def _kinds(violations: list[dict[str, str]]) -> set[str]:
    return {v["kind"] for v in violations}


# ---- genuinely good output passes, without manufactured criticism ----


def test_good_letter_passes() -> None:
    assert _check(GOOD_LETTER, GOOD_CITED) == []


def test_good_answers_pass() -> None:
    hooli = {"target_company": "Hooli", "job_title": "Senior Account Executive"}
    ok = (
        "Yes. At Haulmark I led 9 AEs and built the channel-partner program from 3 to 17 "
        "reseller partners in ~18 months."
    )
    assert _check(ok, ["b_052"], jd_text="", **hooli) == []
    assert _check("I have a B.S. from Lakeside State University.", ["edu_1"], **hooli) == []
    rank = (
        "At Marlow Payroll I made President's Club in 2019 and 2020, closing 131% of annual quota."
    )
    assert _check(rank, ["b_058"], **hooli) == []
    both = (
        "At Haulmark I led 9 AEs; at ledgerline I launched usage-based "
        "pricing that added $2.4M in net-new ARR."
    )
    assert _check(both, ["b_052", "b_049"], **hooli) == []


def test_company_fact_may_use_jd_numbers_but_a_candidate_claim_may_not() -> None:
    fact = "Fabrikam has more than 4,000 contractor shops on its platform."
    assert _check(fact, []) == []
    bad = _check("At Haulmark I grew the region to 4,000 shops.", ["b_047"])
    assert any(v["kind"] == "fabricated_number" and "4,000" in v["detail"] for v in bad)


# ---- the mandatory adversarial cases ----


def test_cross_role_attribution_rejected() -> None:
    for cited in (["b_047", "b_049"], ["b_047"]):  # citing the pricing bullet or hiding it
        kinds = _kinds(_check(CROSS_ROLE_LETTER, cited))
        assert "cross_role_attribution" in kinds, (cited, kinds)


def test_fact_attached_to_the_wrong_employer_rejected() -> None:
    v = _check("At Haulmark I launched usage-based pricing that added $2.4M in ARR.", ["b_049"])
    assert {"misattributed_claim", "uncited_employer"} <= _kinds(v)
    v = _check("At Haulmark I rebuilt the sales reports in Looker.", ["b_046", "b_052"])
    assert any(v_["kind"] == "misattributed_claim" and "Looker" in v_["detail"] for v_ in v)


def test_letter_naming_another_company_is_a_hard_fail() -> None:
    v = _check(LUMON_LETTER, ["b_047"])
    assert any(x["kind"] == "wrong_company" and "Lumon" in x["detail"] for x in v)
    # even with no companies table to compare against, the name is unsourced
    v = _check(LUMON_LETTER, ["b_047"], known_companies=[])
    assert any(x["kind"] == "unsourced_proper_noun" and "Lumon" in x["detail"] for x in v)
    # the right company, its JD's own names and the candidate's employers are fine
    assert _check(GOOD_LETTER, GOOD_CITED, known_companies=KNOWN) == []


def test_vp_of_sales_left_answer_rejected() -> None:
    v = _check(
        VP_MENTION_ANSWER,
        ["b_052"],
        target_company="Hooli",
        job_title="Senior Account Executive",
        jd_text="",
    )
    assert {"unsourced_proper_noun", "unsourced_title_mention"} <= _kinds(v)


@pytest.mark.parametrize(
    ("text", "cited", "needle"),
    [
        ("At Haulmark I grew annual bookings 320% to ~$5.6M.", ["b_047"], "320%"),
        ("At Haulmark I was the #1 rep, doubling pipeline.", ["b_047"], "#1"),
        ("At Haulmark I was the #1 rep, doubling pipeline.", ["b_047"], "doubling"),
        ("At Haulmark I led a team of twelve AEs.", ["b_052"], "twelve"),
    ],
)
def test_embellished_numbers_rejected(text: str, cited: list[str], needle: str) -> None:
    v = _check(text, cited)
    assert any(x["kind"] == "fabricated_number" and needle in x["detail"] for x in v), v


@pytest.mark.parametrize("style", ["hedged", "confident"])
def test_hedges_survive_into_letters(style: str) -> None:
    flat = _check(
        "At Haulmark I cut time to first dispatch by 40%, over target.",
        ["b_044"],
        estimate_style=style,
    )
    hedged = _check(
        "At Haulmark I cut time to first dispatch by ~40%.", ["b_044"], estimate_style=style
    )
    dropped = _check(
        "At Haulmark I grew annual bookings to $5.6M.", ["b_047"], estimate_style=style
    )
    assert hedged == []
    if style == "hedged":
        assert "unhedged_estimate" in _kinds(flat)
        assert "dropped_hedge" in _kinds(dropped)
    else:  # "~" may go; the figure is otherwise the bullet's
        assert flat == [] and dropped == []
    # b_047's ~$5.6M is annual bookings: a "$5.6M book" drops that, in either
    # style (review of render-confident)
    no_period = _check("At Haulmark I grew a $5.6M book.", ["b_047"], estimate_style=style)
    assert "dropped_period" in _kinds(no_period), no_period


def test_never_claim_titles_and_phrases_in_prose() -> None:
    own = _check("As VP of Sales at Haulmark I led 9 AEs.", ["b_052"])
    assert "never_claim_title" in _kinds(own)
    phrase = _check("At Haulmark I also ran treasury management sales.", ["b_052"])
    assert "never_claim_phrase" in _kinds(phrase)
    # another person's title, worded as the bullet words it, is fine
    assert _check("I reported to the General Manager at Haulmark.", ["b_052"]) == []


def test_prose_conventions_do_not_trip_the_check() -> None:
    hooli_jd = (
        "Hooli's customers include Fernbrook and Oakmont. You will work with a Global VP "
        "of Sales on enterprise deals from our Denver office."
    )
    hooli = {"target_company": "Hooli", "job_title": "Partner Manager", "jd_text": hooli_jd}
    # sentence-initial capitals, a reaction ("caught my attention") beside JD
    # facts, another person's title taken from the JD, and a hyphenated compound
    letter = (
        "The role caught my attention. Hooli's customers include Fernbrook and Oakmont. "
        "Separately, at Haulmark I led 9 AEs. The role works with a Global VP "
        "of Sales. My AI-assisted coaching work is the reason I'd like to talk."
    )
    assert _check(letter, ["b_052"], **hooli) == []
    # …but never in one sentence with them: a reaction is first person, and
    # "…, which matters to me" closes a claim as easily (fifth review of
    # validator-refine). It passed at aca26cc; an accepted false rejection.
    v = _check(
        "The role in Denver caught my attention: Hooli's customers include Fernbrook and Oakmont.",
        [],
        **hooli,
    )
    assert {x["detail"].split(" ")[0] for x in v} >= {"Denver", "Fernbrook", "Oakmont"}, v


def test_a_title_belongs_to_its_employer() -> None:
    # even when the job being applied for has the same title
    for title in ("Head of Revenue Operations", "Account Executive"):
        v = _check(
            "As Head of Revenue Operations at Haulmark, I led 9 AEs.", ["b_052"], job_title=title
        )
        assert any(x["kind"] == "misattributed_claim" and "Revenue" in x["detail"] for x in v), v


def test_citations_must_be_real_and_provided() -> None:
    v = _check("At Haulmark I led 9 AEs.", ["b_999", "b_047"], allowed_ids=["b_047"])
    assert "unknown_bullet_id" in _kinds(v)
    v = _check("At Haulmark I led 9 AEs.", ["b_052"], allowed_ids=["b_047"])
    assert "bullet_not_provided" in _kinds(v)
    assert "missing_citations" in _kinds(_check("At Haulmark I led 9 AEs.", []))


# ---- audit review wp2: inputs that still passed as ok=True ----

WITH_TOOLS = [*KNOWN, "Salesforce", "Gong", "Nakatomi"]


@pytest.mark.parametrize(
    ("text", "cited"),
    [
        # skill_index tools are companies too: named as an employer, not a tool
        ("I was an Account Executive at Salesforce.", ["b_058"]),
        ("I sold to Nakatomi at Salesforce.", ["b_041"]),
        ("I spent my early career at Salesforce.", []),
        ("I worked at Gong.", []),
        ("I was an AE at Marlow Payroll, Salesforce and Gong.", ["b_058"]),
        ("I'd love to join Salesforce.", []),
        # a corpus client named as where the candidate worked
        ("I was a buyer at Nakatomi.", ["b_041"]),
        ("My team at Nakatomi signed 12–15 new accounts a month.", ["b_041"]),
        # résumé-style answer with no "I"
        (
            "Account Executive at Salesforce, then Mid-Market Account Executive at Marlow Payroll.",
            ["b_058"],
        ),
    ],
)
def test_fabricated_employer_is_a_hard_fail(text: str, cited: list[str]) -> None:
    for known in (KNOWN, WITH_TOOLS):  # with or without the companies table
        out = validate_claims(
            text,
            cited,
            CORPUS,
            target_company="Hooli",
            job_title="Senior Account Executive",
            jd_text="",
            known_companies=known,
        )
        assert not out.ok and has_hard(out), (text, out.violations)
        assert any(v["kind"] == "fabricated_employer" for v in out.violations), out.violations


def test_tools_and_clients_are_still_fine() -> None:
    hooli = {"target_company": "Hooli", "job_title": "Senior Account Executive"}
    ok = [
        ("At Haulmark I lived in Salesforce and Gong every day.", ["b_052"]),
        (
            "At Haulmark, Salesforce and Gong were part of every coaching session I ran.",
            ["b_052"],
        ),
        ("I joined Gong calls with every rep at Haulmark.", ["b_052"]),
        (
            "At Keyward Security I signed accounts at Nakatomi, Ashgrove and Pemberton Mills.",
            ["b_041"],
        ),
        (
            "At Keyward Security I closed 12–15 new accounts a month with buyers at Nakatomi.",
            ["b_041"],
        ),
        ("At Haulmark and Keyward Security, I carried a number.", ["b_052", "b_041"]),
        ("I studied at Lakeside State University.", ["edu_1"]),
        ("I'd like to join the team at Hooli.", []),
    ]
    for text, cited in ok:
        v = _check(text, cited, jd_text="", known_companies=WITH_TOOLS, **hooli)
        assert v == [], (text, v)
    # "at Hooli" names the target, never a fabricated employer; what the
    # candidate would bring is a claim, and "discipline" is in no cited bullet
    # (sixth review of the self-review)
    bring = "I'd bring the same discipline to the team at Hooli."
    v = _check(bring, [], jd_text="", known_companies=WITH_TOOLS, **hooli)
    assert _kinds(v) == {"unsourced_experience_claim"}, v
    # "deals at <tool>" is not a client: a tool is never where deals happen
    v = _check("At Haulmark I closed deals at Salesforce.", ["b_052"], **hooli)
    assert "fabricated_employer" in _kinds(v)


@pytest.mark.parametrize(
    "text",
    [
        "In my General Manager seat at Haulmark, I led 9 AEs.",
        "My General Manager role at Haulmark meant I led 9 AEs.",
        "I took the General Manager seat at Haulmark and led 9 AEs.",
        "I filled the General Manager role at Haulmark, leading 9 AEs.",
        "I stepped up to the General Manager role at Haulmark and led 9 AEs.",
    ],
)
def test_possessive_and_seat_verbs_claim_the_title(text: str) -> None:
    # b_052 mentions "reporting to the General Manager", so the title is sourced
    # as someone else's — rendered as the candidate's own it is still a hard fail
    v = _check(text, ["b_052"], target_company="Hooli", job_title="Account Executive")
    assert "never_claim_title" in _kinds(v), v


def test_another_persons_title_after_my_or_our_is_not_a_claim() -> None:
    hooli = {"target_company": "Hooli", "job_title": "Account Executive", "jd_text": ""}
    for text in (
        "I reported to my General Manager at Haulmark and led 9 AEs.",
        "I worked alongside the General Manager at Haulmark, leading 9 AEs.",
    ):
        assert _check(text, ["b_052"], **hooli) == [], text
    # "our VP of Sales asked" is someone else — and unsourced
    v = _check(VP_MENTION_ANSWER, ["b_052"], **hooli)
    assert "unsourced_title_mention" in _kinds(v) and "never_claim_title" not in _kinds(v)


@pytest.mark.parametrize(
    ("text", "cited", "needle"),
    [
        ("I closed millions in revenue at Haulmark.", ["b_047"], "millions"),
        ("I closed hundreds of deals at Marlow Payroll.", ["b_058"], "hundreds"),
        ("I closed dozens of deals a month at Marlow Payroll.", ["b_058"], "dozens"),
        ("I managed tens of millions in pipeline.", [], "tens of millions"),
        ("I sold for over a decade.", [], "a decade"),
        ("Before this I spent a decade in sales.", [], "a decade"),
        ("I have two decades of sales experience.", [], "two decades"),
        (
            "At Marlow Payroll I made President's Club in a year and a half.",
            ["b_058"],
            "a year and a half",
        ),
        ("At Haulmark I cut time to first dispatch in half.", ["b_044"], "half"),
        ("At Keyward Security I worked multi-million dollar deals.", ["b_041"], "multi-million"),
    ],
)
def test_vague_and_fractional_quantities_are_claims(
    text: str, cited: list[str], needle: str
) -> None:
    v = _check(text, cited, target_company="Hooli", job_title="Account Executive", jd_text="")
    assert any(x["kind"] == "fabricated_number" and needle in x["detail"] for x in v), v
    if not cited:  # a first-person quantity is a candidate fact: it needs a source
        assert "missing_citations" in _kinds(v)


def test_prompts_get_labeled_sources_only() -> None:
    ctx = labeled_sources_for_prompt(CORPUS, ["b_047", "b_049"])
    assert "b_047 | Haulmark · Regional Sales Manager · Mar 2024 – Present" in ctx
    assert "b_049 | ledgerline · Head of Revenue Operations · Nov 2021 – Mar 2024" in ctx
    assert "b_052" not in ctx and "b_057" not in ctx
    profile = drafting_profile(CORPUS)
    assert "edu_1 | Education" in profile and "b_057 | ledgerline" in profile
    assert "never_claim" not in profile and "'VP'" in profile


# ---------------------------------------------------------------------------
# tailor_job: a letter that fails the fact check is never written as final
# ---------------------------------------------------------------------------

SELECTION = {
    "positioning_id": "pos_ops",
    "selected": [
        {"bullet_id": b, "variant": "short", "rendered": ""}
        for b in ("b_047", "b_052", "b_049", "b_044", "b_058", "b_041")
    ],
    "skills_line": ["salesforce", "MEDDIC"],
}


def _selection(skills: list[str]) -> dict[str, Any]:
    sel = copy.deepcopy(SELECTION)
    by_id = {b["id"]: b for b in CORPUS["bullets"]}
    for s in sel["selected"]:
        if s["bullet_id"] in ("b_047", "b_052"):  # ragged lengths, as a real resume
            s["variant"] = "long"
        s["rendered"] = by_id[s["bullet_id"]][f"text_{s['variant']}"]
    sel["skills_line"] = skills
    return sel


# the claim judge's reply to a text it finds fully supported (validate/claim_judge.py)
JUDGE_PASS: dict[str, Any] = {"unsupported": [], "verdict": "pass"}


class FakeLLM:
    """llm.call_json stand-in: replies per stage, records every prompt. A
    reply may be a function of the prompt. The claim judge passes everything
    unless a test gives it replies of its own ("claim_judge"); it still runs
    for real, on every text the rules pass."""

    def __init__(self, replies: dict[str, list[Any]]) -> None:
        self.replies: dict[str, list[Any]] = {"claim_judge": [JUDGE_PASS], **replies}
        self.prompts: dict[str, list[str]] = {}
        self.forgotten: list[str] = []

    def call_json(
        self, conn: sqlite3.Connection, stage: str, prompt: str, *, cache_key: str | None = None
    ) -> dict[str, Any]:
        self.prompts.setdefault(stage, []).append(prompt)
        queue = self.replies[stage]
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        out: dict[str, Any] = reply(prompt) if callable(reply) else copy.deepcopy(reply)
        return out

    def forget(self, conn: sqlite3.Connection, stage: str, prompt: str, key: str) -> None:
        self.forgotten.append(stage)


def judged_text(prompt: str) -> str:
    """The TEXT a claim-judge prompt asks about (prompts/claim_judge.md ends with it)."""
    return prompt.rsplit("\nTEXT (", 1)[1].split("\n", 1)[1]


def flagging_judge(*claims: str) -> Callable[[str], dict[str, Any]]:
    """A fake claim judge that reports each of `claims` the text under review
    contains, and passes a text that contains none of them."""

    def reply(prompt: str) -> dict[str, Any]:
        found = [c for c in claims if c in judged_text(prompt)]
        return {
            "unsupported": [
                {"sentence": c, "claim": c.rstrip("."), "why": "no cited source states it"}
                for c in found
            ],
            "verdict": "fail" if found else "pass",
        }

    return reply

    def forget(self, conn: sqlite3.Connection, stage: str, prompt: str, key: str) -> None:
        self.forgotten.append(stage)


@pytest.fixture()
def tailor_env(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., tuple[FakeLLM, int]]:
    def setup(
        letters: list[dict[str, Any]],
        selections: list[dict[str, Any]],
        corpus: dict[str, Any] = CORPUS,
    ) -> tuple[FakeLLM, int]:
        (jobkit_home / "data" / "career_corpus.yaml").write_text(yaml.safe_dump(corpus))
        conn.execute("INSERT INTO companies (name, normalized_name) VALUES ('Lumon', 'lumon')")
        cur = conn.execute(
            "INSERT INTO companies (name, normalized_name) VALUES ('Fabrikam Payments', 'fabrikam')"
        )
        jid = conn.execute(
            """INSERT INTO jobs (company_id, external_id, title, normalized_title,
               company_name, normalized_company, location_bucket, cluster_key, jd_html,
               jd_hash, apply_url, status)
               VALUES (?, 'f1', 'Director of Revenue Operations', 'director of revenue operations',
                       'Fabrikam Payments', 'fabrikam',
                       'remote-us', 'k', ?, 'h-fabrikam', 'https://x', 'approved')""",
            (cur.lastrowid, f"<p>{FABRIKAM_JD}</p>"),
        ).lastrowid
        assert jid is not None
        conn.execute("INSERT INTO applications (job_id, stage) VALUES (?, 'approved')", (jid,))
        conn.commit()
        fake = FakeLLM(
            {
                "tailor": selections,
                "cover": letters,
                "critic": [{"verdict": "pass", "notes": "", "tells": []}],
            }
        )
        monkeypatch.setattr(llm, "call_json", fake.call_json)
        monkeypatch.setattr(llm, "forget", fake.forget)
        monkeypatch.setattr(authenticity, "_default_embed", lambda t: [[1.0, 0.0] for _ in t])

        def fake_render(template: str, data: dict[str, Any], out: Path) -> Path:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(data))
            return out

        monkeypatch.setattr(tailor, "render_pdf", fake_render)
        monkeypatch.setattr(tailor, "page_count", lambda p: 1)
        return fake, jid

    return setup


def test_tailor_rejects_lumon_letter_for_fabrikam(
    conn: sqlite3.Connection,
    tailor_env: Callable[..., tuple[FakeLLM, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lumon = {"letter": LUMON_LETTER, "bullets_used": ["b_047"]}
    fake, jid = tailor_env([lumon], [_selection(["Salesforce", "MEDDIC"])])
    paths = tailor.tailor_job(conn, jid)

    out = Path(paths["resume"]).parent
    assert paths["cover"] == ""
    assert not (out / "cover.pdf").exists() and not (out / "cover.txt").exists()
    rejected = (out / "cover_rejected.md").read_text()
    assert "wrong_company" in rejected and "NOT FOR SENDING" in rejected
    # every draft (tailoring.cover_attempts, absent: 5) was fact-checked; none
    # reached the critic; cache dropped once
    assert len(fake.prompts["cover"]) == DEFAULT_COVER_ATTEMPTS
    assert all("cover letter" not in p for p in fake.prompts.get("critic", []))
    # the resume critic DID run: the semicolons are the corpus's own (inherited)
    assert len(fake.prompts["critic"]) == 1 and "resume bullets" in fake.prompts["critic"][0]
    assert fake.forgotten.count("cover") == 1
    # the model saw only the selected bullets, labeled with employer/title/dates
    prompt = fake.prompts["cover"][0]
    assert "b_047 | Haulmark · Regional Sales Manager" in prompt and "b_057" not in prompt
    # the application does not advance and points at no letter
    app = conn.execute("SELECT stage, cover_path FROM applications WHERE job_id=?", (jid,))
    assert tuple(app.fetchone()) == ("approved", None)
    ev = conn.execute(
        "SELECT detail FROM events WHERE job_id=? AND kind='cover_provenance_failed'", (jid,)
    ).fetchone()
    assert ev is not None and "wrong_company" in ev[0]
    prov = json.loads(Path(paths["provenance"]).read_text())
    assert prov["cover_fact_check"]["ok"] is False
    # …and the human can see it through the MCP server
    from jobkit import mcp_server

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    conn.executescript(mcp_server.PINS_SCHEMA)
    conn.execute("INSERT INTO queue_pins (n, job_id) VALUES (1, ?)", (jid,))
    review = mcp_server.review_artifacts(1)
    assert "cover_provenance_failed" in review and "wrong_company" in review
    assert "REJECTED by the fact check" in mcp_server.get_artifacts(1)


def test_tailor_good_letter_is_final_and_skills_are_validated(
    conn: sqlite3.Connection, tailor_env: Callable[..., tuple[FakeLLM, int]]
) -> None:
    good = {"letter": GOOD_LETTER, "bullets_used": GOOD_CITED}
    # first selection invents a skill and uses a never-claim phrase → retried
    first = _selection(["Salesforce", "Treasury management sales"])
    fake, jid = tailor_env([good], [first, _selection(["salesforce", "MEDDIC"])])
    paths = tailor.tailor_job(conn, jid)

    assert len(fake.prompts["tailor"]) == 2 and fake.forgotten.count("tailor") == 1
    assert "unindexed_skill" in fake.prompts["tailor"][1]
    assert Path(paths["cover"]).exists() and "cover_rejected" not in paths
    prov = json.loads(Path(paths["provenance"]).read_text())
    assert prov["skills_line"] == ["Salesforce", "MEDDIC"]  # canonical skill_index spelling
    assert prov["cover_fact_check"] == {"ok": True, "violations": []}
    assert prov["cover_bullets_used"] == GOOD_CITED
    stage = conn.execute("SELECT stage FROM applications WHERE job_id=?", (jid,)).fetchone()[0]
    assert stage == "tailored"


def test_tailor_omits_skills_when_index_is_empty(
    conn: sqlite3.Connection, tailor_env: Callable[..., tuple[FakeLLM, int]], jobkit_home: Path
) -> None:
    corpus = {k: v for k, v in CORPUS.items() if k != "skill_index"}
    good = {"letter": GOOD_LETTER.replace(" I coach deals on MEDDIC and live in Salesforce.", "")}
    good["bullets_used"] = GOOD_CITED
    _fake, jid = tailor_env([good], [_selection(["Anything At All"])], corpus=corpus)
    paths = tailor.tailor_job(conn, jid)  # no crash
    prov = json.loads(Path(paths["provenance"]).read_text())
    assert prov["skills_line"] == []
    assert prov["skills_line_source"].startswith("omitted")
    log = (jobkit_home / "logs" / "jobkit.jsonl").read_text()
    assert "tailor.skills_omitted" in log


# ---------------------------------------------------------------------------
# novel answers: a failing draft is not stored; the human sees why
# ---------------------------------------------------------------------------


def test_novel_draft_failing_fact_check_is_not_returned(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLLM({"novel": [{"answer": VP_MENTION_ANSWER, "bullets_used": ["b_052"]}]})
    monkeypatch.setattr(llm, "call_json", fake.call_json)
    monkeypatch.setattr(llm, "forget", fake.forget)
    answer, reason, violations = form_spec.draft_novel_answer(
        conn,
        prompt=llm.PromptFile("novel_question"),
        corpus=CORPUS,
        profile=drafting_profile(CORPUS),
        job={"id": 1, "title": "Senior Account Executive", "company_name": "Hooli"},
        label="Tell us about a time you stepped up",
        question="Tell us about a time you stepped up",
    )
    assert answer is None and reason == "unsourced-claim"
    assert {"unsourced_proper_noun", "unsourced_title_mention"} <= _kinds(violations)
    assert len(fake.prompts["novel"]) == 2 and fake.forgotten == ["novel"]
    assert "YOUR PREVIOUS DRAFT FAILED THE FACT CHECK" in fake.prompts["novel"][1]
    assert "b_052 | Haulmark · Regional Sales Manager" in fake.prompts["novel"][0]
    # the configured estimate_style (config/banned_phrases.yaml) picks the figure rule
    assert "FIGURE STYLE: confident." in fake.prompts["novel"][0]


def test_capture_keeps_failed_draft_out_of_the_form(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (jobkit_home / "data" / "career_corpus.yaml").write_text(yaml.safe_dump(CORPUS))
    conn.execute("INSERT INTO companies (id, name, normalized_name) VALUES (1, 'Hooli', 'w')")
    conn.execute("INSERT INTO boards (id, company_id, ats, token) VALUES (1, 1, 'greenhouse', 'w')")
    jid = conn.execute(
        """INSERT INTO jobs (board_id, company_id, external_id, title, normalized_title,
           company_name, normalized_company, location_bucket, cluster_key, jd_html, jd_hash,
           apply_url, status)
           VALUES (1, 1, 'g1', 'Senior Account Executive', 'sae', 'Hooli', 'hooli',
                   'remote-us', 'k', '<p>JD</p>', 'h', 'https://x', 'queued')"""
    ).lastrowid
    conn.commit()

    class StubMatcher:
        bank: dict[str, Any] = {"scaffolds": []}

        def match(
            self, label: str, compliance_type: str | None = None, **kw: Any
        ) -> dict[str, Any]:
            return {"kind": "novel"}

    fields = [
        {
            "identity": "q_story",
            "label": "Tell us about a time you stepped up",
            "type": "textarea",
            "required": True,
            "options": [],
            "compliance_type": None,
        },
        {
            "identity": "q_team",
            "label": "How large a team have you led?",
            "type": "textarea",
            "required": False,
            "options": [],
            "compliance_type": None,
        },
    ]
    monkeypatch.setattr(form_spec, "BankMatcher", StubMatcher)
    monkeypatch.setitem(form_spec.FETCHERS, "greenhouse", lambda c, t, e: copy.deepcopy(fields))
    answers = {
        "Tell us about a time you stepped up": {
            "answer": VP_MENTION_ANSWER,
            "bullets_used": ["b_052"],
        },
        "How large a team have you led?": {
            "answer": "At Haulmark I led 9 AEs.",
            "bullets_used": ["b_052"],
        },
    }

    judged: list[str] = []

    def call_json(c: sqlite3.Connection, stage: str, prompt: str, **kw: Any) -> dict[str, Any]:
        if stage == "claim_judge":
            judged.append(prompt)
            return JUDGE_PASS
        return next(v for k, v in answers.items() if k in prompt.split("FORM QUESTION:")[-1])

    monkeypatch.setattr(llm, "call_json", call_json)
    monkeypatch.setattr(llm, "forget", lambda *a: None)

    stats = form_spec.capture_form_specs(conn)
    assert stats["drafted"] == 1 and stats["fact_check_failed"] == 1
    # the judge read only the answer the rules passed, with its question
    assert len(judged) == 1 and "How large a team have you led?" in judged[0]
    row = conn.execute(
        "SELECT drafted_answers, novel_questions FROM form_specs WHERE job_id=?", (jid,)
    ).fetchone()
    assert json.loads(row["drafted_answers"]) == {"q_team": "At Haulmark I led 9 AEs."}
    (open_q,) = json.loads(row["novel_questions"])
    assert open_q["identity"] == "q_story" and open_q["flag"] == "unsourced-claim"
    assert any(v["kind"] == "unsourced_title_mention" for v in open_q["fact_check"])
    kinds = [r[0] for r in conn.execute("SELECT kind FROM events WHERE job_id=?", (jid,))]
    assert kinds == ["novel_provenance_failed"]


# ---------------------------------------------------------------------------
# second review of wp2: inputs that still passed as ok=True
# ---------------------------------------------------------------------------

OSCORP_VP_JD = (
    "Oscorp is hiring a VP of Sales to build and lead the sales team. The VP of Sales "
    "reports to the CEO."
)


@pytest.mark.parametrize(
    ("job_title", "text"),
    [
        ("VP of Sales", "I was the VP of Sales at Haulmark, leading 9 AEs."),
        ("VP of Sales", "As VP of Sales at Haulmark, I led 9 AEs."),
        ("Head of Sales", "At Haulmark I served as Head of Sales, leading 9 AEs."),
        (
            "Head of Sales",
            "Before Haulmark promoted me to Head of Sales, I led 9 AEs.",
        ),
        # no employer named, but a candidate fact or an explicit claim cue
        ("VP of Sales", "VP of Sales for ~18 months; led 9 AEs."),
        ("VP of Sales", "I did the VP of Sales job for ~18 months."),
        ("VP of Sales", "I have operated at the VP of Sales level for years."),
        ("VP of Sales", "As your VP of Sales, I would build the team."),
    ],
)
def test_job_title_never_excuses_claiming_it(job_title: str, text: str) -> None:
    # the leadership roles this candidate applies for are exactly the
    # never-claim titles: naming the job must not make claiming it pass
    v = _check(text, ["b_052"], target_company="Oscorp", job_title=job_title, jd_text="")
    assert "never_claim_title" in _kinds(v), v


def test_job_title_may_still_name_the_job() -> None:
    vp = {"target_company": "Oscorp", "job_title": "VP of Sales", "jd_text": OSCORP_VP_JD}
    for text, cited in (
        ("I'm excited about the VP of Sales role at Oscorp.", []),
        ("The VP of Sales role at Oscorp caught my attention.", []),
        ("Oscorp is hiring a VP of Sales to build the team.", []),
        ("This VP of Sales role is the next step for me.", []),
        (
            "At Haulmark I led 9 AEs, which prepared me for the VP of Sales role at Oscorp.",
            ["b_052"],
        ),
        ("My time at Haulmark prepared me for your VP of Sales role.", ["b_052"]),
    ):
        assert _check(text, cited, **vp) == [], text
    # someone else's VP of Sales in the candidate's past still needs a source,
    # even when the job being applied for is VP of Sales
    v = _check(VP_MENTION_ANSWER, ["b_052"], **vp)
    assert "unsourced_title_mention" in _kinds(v), v
    v = _check("At Haulmark I reported to the VP of Sales.", ["b_052"], **vp)
    assert {"unsourced_title_mention", "unsourced_proper_noun"} <= _kinds(v), v


@pytest.mark.parametrize(
    "text",
    [
        "At Haulmark I owned the General Manager function, leading 9 AEs.",
        "At Haulmark I was effectively the General Manager.",
        "At Haulmark I was, in effect, the General Manager.",
        "At Haulmark, the General Manager role was mine.",
        "At Haulmark I ran the Northeast region in the General Manager role.",
        "At Haulmark I did the general manager's job.",
        "At Haulmark I was the org's General Manager.",
        "At Haulmark I was more of a General Manager than a manager.",
    ],
)
def test_own_title_phrasings_in_prose_rejected(text: str) -> None:
    v = _check(text, ["b_052"], target_company="Hooli", job_title="Account Executive")
    assert "never_claim_title" in _kinds(v), v


def test_another_persons_title_still_passes_in_prose() -> None:
    hooli = {"target_company": "Hooli", "job_title": "Account Executive", "jd_text": ""}
    for text in (
        "At Haulmark I reported to the General Manager and led 9 AEs.",
        "At Haulmark I worked closely with the General Manager.",
        "At Haulmark I took the channel-partner plan to the General Manager.",
        "At Haulmark, the General Manager at the time owned the forecast, so I built the "
        "channel-partner program from 3 to 17 reseller partners in ~18 months.",
    ):
        assert _check(text, ["b_052"], **hooli) == [], text


@pytest.mark.parametrize(
    "text",
    [
        # the second sentence refers back without one of the _ANAPHORA phrases
        "At Haulmark I grew annual bookings to ~$5.6M. It came from usage-based pricing I "
        "launched for merchant accounts.",
        "At Haulmark I grew annual bookings to ~$5.6M. The revenue came from usage-based "
        "pricing I launched for merchant accounts.",
        "At Haulmark I grew annual bookings to ~$5.6M. "
        "The engine behind it was usage-based pricing for merchant accounts.",
        # both employers named, one credited with the other's result
        "I launched usage-based pricing at ledgerline. The ~$5.6M in annual Haulmark "
        "bookings came from it.",
        "The ~$5.6M in annual Haulmark bookings came from usage-based pricing I launched at "
        "ledgerline.",
        # one sentence, no employer named
        "I grew annual bookings to ~$5.6M from usage-based pricing.",
    ],
)
def test_cross_role_attribution_variants_rejected(text: str) -> None:
    for cited in (["b_047", "b_049"], ["b_047"]):
        v = _check(text, cited)
        assert "cross_role_attribution" in _kinds(v), (cited, v)


def test_two_employers_kept_apart_still_pass() -> None:
    hooli = {"target_company": "Hooli", "job_title": "Account Executive", "jd_text": ""}
    for text, cited in (
        (
            "At Haulmark I grew annual bookings to ~$5.6M. At ledgerline I launched "
            "usage-based pricing that added $2.4M in net-new ARR.",
            ["b_047", "b_049"],
        ),
        (
            "At Haulmark I grew annual bookings to ~$5.6M. The usage-based pricing I "
            "launched earlier at ledgerline added $2.4M in net-new ARR.",
            ["b_047", "b_049"],
        ),
        (
            "At ledgerline I launched usage-based pricing that drove $2.4M in net-new ARR.",
            ["b_049"],
        ),
        (
            "At Haulmark I grew the Northeast region to ~$5.6M in annual "
            "bookings. It added 41 new carrier accounts.",
            ["b_047"],
        ),
        (
            "At Haulmark I cut time to first dispatch by ~40%. It came from the remote "
            "onboarding program I built there.",
            ["b_044"],
        ),
    ):
        assert _check(text, cited, **hooli) == [], text


@pytest.mark.parametrize(
    ("text", "cited"),
    [
        # MANDATORY: an observation verb does not turn a candidate claim into a
        # company fact — "doubled" and "4,000" are the Fabrikam JD's numbers
        ("At Haulmark I saw revenue double.", ["b_052"]),
        ("At Haulmark I saw the region grow to more than 4,000 shops.", ["b_052"]),
        ("At Haulmark I noticed bookings double.", ["b_047"]),
        ("I think the region doubled.", ["b_047"]),
        # not first person, but not about the target either
        ("The region doubled to more than 4,000 shops.", ["b_047"]),
        ("At Haulmark the region doubled.", ["b_047"]),
    ],
)
def test_jd_numbers_never_back_a_candidate_claim(text: str, cited: list[str]) -> None:
    v = _check(text, cited)
    assert any(x["kind"] == "fabricated_number" for x in v), v


def test_company_facts_from_the_jd_still_pass() -> None:
    # each restates the posting word for word after its opening (fifth review
    # of validator-refine)
    for text in (
        "Fabrikam Payments doubled its payment volume in 2025.",
        "Fabrikam has more than 4,000 contractor shops on its platform.",
    ):
        assert _check(text, []) == [], text
    for text in (
        "Contoso's Account Associates book 35 walkthroughs a week.",
        "Contoso expects 5x ticket volume within 120 days.",
        # a sentence that says nothing happened may open with "your"/"the role"
        "Your Account Associates book 35 walkthroughs a week.",
        "The role works with the Director of Sales on lost-deal reviews and playbooks.",
    ):
        assert _check(text, [], **CONTOSO) == [], text


# fourth review of validator-refine: the JD's numbers and names back only a
# sentence whose subject is plainly the target. Each of these passed at aca26cc
# and before this review; each credits a JD number to the candidate, or could.
JD_FACT_BORROWED = [
    # the reviewer's repros: a gerund subject, a past tense, a pointer back
    (
        "At Haulmark I led 9 AEs. Booking 35 walkthroughs a week with a team like your "
        "Account Associates was the job, which matters to me.",
        ["b_052"],
        "35",
    ),
    (
        "Booking 35 walkthroughs a week with a team like your Account Associates was the job.",
        ["b_052"],
        "35",
    ),
    (
        "Booking 35 walkthroughs a week with a team like your Account Associates was the job.",
        [],
        "35",
    ),
    (
        "At Haulmark I led 9 AEs and built the channel-partner program "
        "from 3 to 17 reseller partners in ~18 months. Growing "
        "ticket volume 5x within 120 days, as your team does, was the job there.",
        ["b_052"],
        "5x",
    ),
    (
        "At Haulmark I led 9 AEs and built the channel-partner program "
        "from 3 to 17 reseller partners in ~18 months. Growing "
        "ticket volume 5x within 120 days, as your team does, was the job there.",
        ["b_052"],
        "120",
    ),
    # the line-1599 case with each reaction, and with none: it fails on the
    # gerund opening, not only on "me"
    *(
        (
            "Booking 35 walkthroughs a week with a team like your Account Associates was the job"
            + tail,
            ["b_052"],
            "35",
        )
        for tail in (
            ", which means a lot to me.",
            ", which matters to me.",
            ", which is what caught my eye.",
            ", which speaks to me.",
            ", which resonates.",
            ".",
        )
    ),
    # a past tense needs the target's name in front, not "your"
    ("Your kind of team booked 35 walkthroughs a week.", [], "35"),
    ("Your Account Associates booked 35 walkthroughs a week.", [], "35"),
    # a gerund, a pronoun or an article in front
    ("Booking 35 walkthroughs a week is the job, as your posting says.", [], "35"),
    ("They book 35 walkthroughs a week, like your Account Associates.", [], "35"),
    ("A team like yours books 35 walkthroughs a week.", [], "35"),
    ("The daily job is booking 35 walkthroughs a week, as your Account Associates do.", [], "35"),
    # the target's name in front, then a pointer back or a comparison
    ("Contoso's 5x ticket volume within 120 days was the norm there.", [], "5x"),
    ("Contoso grows ticket volume 5x within 120 days, as the old team did.", [], "5x"),
    ("Contoso's Account Associates book 35 walkthroughs a week, like the team before.", [], "35"),
    ("Contoso's Account Associates book 35 walkthroughs a week, and they did too.", [], "35"),
    (
        "Contoso's Account Associates book 35 walkthroughs a week, the same pace as before.",
        [],
        "35",
    ),
    # …a person the sentence introduces, who may be the candidate
    ("Contoso gets a leader who books 35 walkthroughs a week.", [], "35"),
    ("Contoso gets a closer that books 35 walkthroughs a week.", [], "35"),
    ("Contoso gets a closer booking 35 walkthroughs a week.", [], "35"),
    ("Contoso gets a closer with 35 walkthroughs a week behind them.", [], "35"),
    ("Contoso gets Test Candidate, booking 35 walkthroughs a week.", [], "35"),
    ("You get a leader who books 35 walkthroughs a week.", [], "35"),
    # …or a promise: a metric needs a source in every tense, with or without "I"
    ("Contoso's ticket volume will grow 5x within 120 days.", [], "5x"),
    ("Your Account Associates would book 35 walkthroughs a week.", [], "35"),
    ("You'll get 35 walkthroughs a week from your Account Associates.", [], "35"),
]


@pytest.mark.parametrize(("text", "cited", "needle"), JD_FACT_BORROWED)
@pytest.mark.parametrize("style", ["hedged", "confident"])
def test_a_jd_number_backs_only_a_sentence_about_the_target(
    text: str, cited: list[str], needle: str, style: str
) -> None:
    v = _check(text, cited, estimate_style=style, **CONTOSO)
    assert any(x["kind"] == "fabricated_number" and needle in x["detail"] for x in v), v


def test_your_team_doubled_is_an_accepted_false_rejection() -> None:
    # it passed at aca26cc; a past tense now needs the target's name in front
    # ("Your kind of team booked…" is the same shape)
    v = _check("Your team doubled its payment volume to more than 4,000 shops.", [])
    assert any(x["kind"] == "fabricated_number" and "4,000" in x["detail"] for x in v), v
    for text in (
        "Fabrikam Payments has more than 4,000 contractor shops on its platform.",
        "Fabrikam Payments doubled its payment volume in 2025.",
    ):
        assert _check(text, []) == [], text


# fifth review of validator-refine: the fourth review's opening rule and its
# lists of pointers still let a sentence that opens with the target recast the
# JD's fact as the candidate's, in words no list named. Each of these passed
# before this review and at aca26cc, except the three marked "guard"; a
# sentence that borrows a JD number now restates the posting word for word,
# and has no I/my/me at all.
JD_FACT_RECAST = [
    # the target's name in front, then the candidate's past in other words
    ("Contoso's 5x ticket volume within 120 days was standard at the last job.", [], "5x"),
    ("Contoso's 5x ticket volume within 120 days was routine on the AE team.", [], "5x"),
    (
        "Contoso grows ticket volume 5x within 120 days, a number the Northeast team hit "
        "every quarter.",
        [],
        "5x",
    ),
    ("Contoso grows ticket volume 5x within 120 days, a number hit every quarter.", [], "5x"),
    ("Contoso's 35 walkthroughs a week was the job for years.", [], "35"),
    ("Contoso: 35 walkthroughs a week was the job for years.", [], "35"),
    # guard: the gerund after the colon
    ("Contoso caught my eye: booking 35 walkthroughs a week was the job for years.", [], "35"),
    (
        "Contoso books 35 walkthroughs a week, and booking that many was the job for years.",
        [],
        "35",
    ),
    ("Contoso books 35 walkthroughs a week; that was the job for years.", [], "35"),
    ("Contoso's Account Associates book 35 walkthroughs a week, an old habit.", [], "35"),
    (
        "Contoso's Account Associates book 35 walkthroughs a week, which used to be the job.",
        [],
        "35",
    ),
    ("Contoso's Account Associates book 35 walkthroughs a week, which is familiar.", [], "35"),
    ("Contoso's Account Associates book 35 walkthroughs a week — the old pace.", [], "35"),
    (
        "Contoso's Account Associates book 35 walkthroughs a week, "
        "a pace this writer kept for years.",
        [],
        "35",
    ),
    ("The role books 35 walkthroughs a week, an old habit.", [], "35"),
    ("Your Account Associates book 35 walkthroughs a week, an old habit.", [], "35"),
    # …after a sentence about the candidate
    (
        "At Haulmark I led 9 AEs. Contoso's 5x "
        "ticket volume within 120 days was the norm for the sales floor.",
        ["b_052"],
        "5x",
    ),
    # a reaction is first person: it keeps the JD's facts out of the sentence
    # (guards: both failed already, on "former" and "matters to me")
    ("Contoso's 5x ticket volume within 120 days caught my eye as a former peer.", [], "5x"),
    ("Your Account Associates book 35 walkthroughs a week, which matters to me.", [], "35"),
]


@pytest.mark.parametrize(("text", "cited", "needle"), JD_FACT_RECAST)
@pytest.mark.parametrize("style", ["hedged", "confident"])
def test_a_jd_number_is_never_recast_as_the_candidates(
    text: str, cited: list[str], needle: str, style: str
) -> None:
    v = _check(text, cited, estimate_style=style, **CONTOSO)
    assert any(x["kind"] == "fabricated_number" and needle in x["detail"] for x in v), v


@pytest.mark.parametrize(
    "text",
    [
        # a reaction, or a word of someone's past, beside a JD name
        "Your Account Associates caught my eye as a former peer.",
        "Your Account Associates matter to me as a longtime closer.",
        "Your Account Associates are familiar faces.",
        "Contoso's Account Associates are an old habit.",
    ],
)
def test_a_jd_name_is_never_recast_as_the_candidates(text: str) -> None:
    v = _check(text, [], **CONTOSO)
    assert any(x["kind"] == "unsourced_proper_noun" and "Associate" in x["detail"] for x in v), v


def test_a_postings_own_history_words_never_back_a_jd_number() -> None:
    # a posting's requirements have words of someone's history; a sentence
    # that restates them word for word still borrows no JD number
    # (_JD_HISTORY)
    jd = (
        CONTOSO_JD + " The ideal candidate has previous experience booking 35 walkthroughs a week. "
        "A proven track record of 5x ticket volume within 120 days is required. You used to "
        "book 35 walkthroughs a week."
    )
    kw = {**CONTOSO, "jd_text": jd}
    for text, needle in (
        ("Contoso's ideal candidate has previous experience booking 35 walkthroughs a week.", "35"),
        (
            "Contoso's proven track record of 5x ticket volume within 120 days is required.",
            "5x",
        ),
        ("Contoso used to book 35 walkthroughs a week.", "35"),
    ):
        v = _check(text, [], **kw)
        assert any(x["kind"] == "fabricated_number" and needle in x["detail"] for x in v), text
    # the posting's facts, in its words, still pass
    assert _check("Contoso expects 5x ticket volume within 120 days.", [], **kw) == []


# fifth review of validator-refine: a set of allowed words was not enough. A
# real posting has "was", "job", "for" and "years", and these sentences are
# made only of this posting's words; each would credit its numbers to the
# candidate. The sentence must restate one run of the posting, in its order.
# (appended to CONTOSO_JD, which is defined below)
RICH_JD_TAIL = (
    " About the job: this team was built in 2019 and has led the market for years. You have "
    "5+ years in sales, have led a team, and have managed reps who were top performers. The "
    "ideal candidate has booked walkthroughs at scale, was a top rep, and knows the job."
)


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("Contoso's 35 walkthroughs a week was the job for years.", "35"),
        ("Contoso's Account Associates book 35 walkthroughs a week, the job for years.", "35"),
        ("Contoso's 5x ticket volume within 120 days was a top rep's job for years.", "5x"),
        ("Contoso's ideal candidate has booked 35 walkthroughs a week for years.", "35"),
        ("Contoso's 5x ticket volume within 120 days has led the market for years.", "5x"),
        (
            "Contoso's Account Associates book 35 walkthroughs a "
            "week, and the ideal candidate was a top rep.",
            "35",
        ),
        ("Contoso's 35 walkthroughs a week, a top rep in sales for 5+ years.", "35"),
    ],
)
@pytest.mark.parametrize("style", ["hedged", "confident"])
def test_a_jd_number_is_never_recast_in_the_postings_own_words(
    text: str, needle: str, style: str
) -> None:
    v = _check(text, [], estimate_style=style, **{**CONTOSO, "jd_text": CONTOSO_JD + RICH_JD_TAIL})
    assert any(x["kind"] == "fabricated_number" and needle in x["detail"] for x in v), v


# Accepted false rejections (fifth review of validator-refine): each passed
# before. A company fact in other words or another order than the posting's,
# or beside a reaction, now needs a cited source for the JD's numbers and
# names; the letter restates the fact word for word and comments on it in the
# next sentence.
JD_FACT_PARAPHRASES = [
    ("Fabrikam grew to more than 4,000 shops last year.", "fabrikam", "4,000"),
    ("Fabrikam's payment volume doubled last year.", "fabrikam", "doubled"),
    (
        "Fabrikam Payments doubled its payment volume to more than 4,000 shops.",
        "fabrikam",
        "4,000",
    ),
    ("Fabrikam's team doubled its payment volume in 2025.", "fabrikam", "doubled"),
    ("Your team serves more than 4,000 shops.", "fabrikam", "4,000"),
    ("The role serves more than 4,000 shops.", "fabrikam", "4,000"),
    (
        "Fabrikam Payments doubled its payment volume in 2025, and growth like that usually "
        "outruns the forecast first.",
        "fabrikam",
        "doubled",
    ),
    ("Contoso's 5x ticket volume within 120 days caught my attention.", "contoso", "5x"),
    (
        "Contoso is hiring an Inside Sales Manager to grow Account Associates into future "
        "Account Executives.",
        "contoso",
        "Associates",
    ),
]


@pytest.mark.parametrize(("text", "fixture", "needle"), JD_FACT_PARAPHRASES)
def test_a_paraphrased_company_fact_is_an_accepted_false_rejection(
    text: str, fixture: str, needle: str
) -> None:
    kw: dict[str, Any] = dict(CONTOSO) if fixture == "contoso" else {}
    v = _check(text, [], **kw)
    kinds = {"fabricated_number", "unsourced_proper_noun"}
    assert any(x["kind"] in kinds and needle in x["detail"] for x in v), v


# ---------------------------------------------------------------------------
# a job whose cover failed is not re-tailored on every run (review of wp2)
# ---------------------------------------------------------------------------


def _llm_calls(fake: FakeLLM) -> int:
    return sum(len(p) for p in fake.prompts.values())


def test_failed_cover_is_not_retailored_every_run(
    conn: sqlite3.Connection,
    tailor_env: Callable[..., tuple[FakeLLM, int]],
    jobkit_home: Path,
) -> None:
    lumon = {"letter": LUMON_LETTER, "bullets_used": ["b_047"]}
    fake, jid = tailor_env([lumon], [_selection(["Salesforce", "MEDDIC"])])
    # the first background run tailors the job; its cover fails the fact check
    assert tailor.tailor_approved(conn) == 1
    calls = _llm_calls(fake)
    assert calls > 0
    ev = conn.execute(
        "SELECT detail FROM events WHERE job_id=? AND kind='cover_provenance_failed'", (jid,)
    ).fetchone()
    assert json.loads(ev[0])["attempt"].startswith("h-fabrikam:")
    # later runs with the same JD, corpus and prompts make no LLM call at all
    assert tailor.tailor_approved(conn) == 0
    assert tailor.tailor_approved(conn) == 0
    assert _llm_calls(fake) == calls
    n_events = conn.execute(
        "SELECT COUNT(*) FROM events WHERE job_id=? AND kind='cover_provenance_failed'", (jid,)
    ).fetchone()[0]
    assert n_events == 1
    log = (jobkit_home / "logs" / "jobkit.jsonl").read_text()
    assert "tailor.skipped_cover_failed" in log
    # a corpus edit is a new attempt: the job is retried once, then skipped again
    corpus = copy.deepcopy(CORPUS)
    corpus["voice_samples"].append({"id": "v_002", "text": "Short sentences. Real numbers."})
    (jobkit_home / "data" / "career_corpus.yaml").write_text(yaml.safe_dump(corpus))
    assert tailor.tailor_approved(conn) == 1
    assert _llm_calls(fake) > calls
    calls = _llm_calls(fake)
    assert tailor.tailor_approved(conn) == 0
    assert _llm_calls(fake) == calls


def test_retailor_tool_retries_one_job(
    conn: sqlite3.Connection,
    tailor_env: Callable[..., tuple[FakeLLM, int]],
    monkeypatch: pytest.MonkeyPatch,
    spawned: Any,
) -> None:
    from jobkit import mcp_server

    lumon = {"letter": LUMON_LETTER, "bullets_used": ["b_047"]}
    _fake, jid = tailor_env([lumon], [_selection(["Salesforce", "MEDDIC"])])
    tailor.tailor_job(conn, jid)
    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    conn.executescript(mcp_server.PINS_SCHEMA)
    conn.execute("INSERT INTO queue_pins (n, job_id) VALUES (1, ?)", (jid,))
    assert "retailor(1)" in mcp_server.get_artifacts(1)
    out = mcp_server.retailor(1)
    assert "re-tailoring item 1" in out
    assert spawned.calls == [mcp_server.background.cli_argv("tailor", "--job-id", str(jid))]
    # never once the application has moved past tailoring
    conn.execute("UPDATE applications SET stage='staged' WHERE job_id=?", (jid,))
    assert "not re-tailoring" in mcp_server.retailor(1)
    assert len(spawned.calls) == 1


def test_cover_failing_authenticity_twice_is_not_retailored_every_run(
    conn: sqlite3.Connection, tailor_env: Callable[..., tuple[FakeLLM, int]]
) -> None:
    good = {"letter": GOOD_LETTER, "bullets_used": GOOD_CITED}
    fake, jid = tailor_env([good], [_selection(["Salesforce", "MEDDIC"])])
    # resume critic passes; every cover critic verdict fails
    fake.replies["critic"] = [
        {"verdict": "pass", "notes": "", "tells": []},
        *[{"verdict": "fail", "notes": "generic", "tells": ["x"]}] * 4,
    ]
    assert tailor.tailor_approved(conn) == 1
    stage = conn.execute("SELECT stage FROM applications WHERE job_id=?", (jid,)).fetchone()[0]
    assert stage == "approved"
    ev = conn.execute(
        "SELECT detail FROM events WHERE job_id=? AND kind='cover_authenticity_failed'", (jid,)
    ).fetchone()
    assert ev is not None and json.loads(ev[0])["attempt"].startswith("h-fabrikam:")
    calls = _llm_calls(fake)
    assert tailor.tailor_approved(conn) == 0
    assert _llm_calls(fake) == calls


# ---------------------------------------------------------------------------
# forward-looking sentences about the target (review 2026-10-09).
# Two allowances were tried and removed (third and fourth reviews of
# validator-refine): a plan's window needs a cited source like any duration,
# and a first-person sentence borrows no JD word, not even for the target's
# own people ("I'll coach your Account Associates"). Their rejections stand as
# accepted false rejections; nothing is relaxed
# ---------------------------------------------------------------------------

# an invented posting, written for these tests
CONTOSO_JD = (
    "Contoso makes ticketing and box-office software for concert halls and minor-league ballparks, "
    "and it is hiring an Inside Sales Manager. Our Account Associates book 35 walkthroughs a week "
    "and we expect 5x ticket volume within 120 days. You will lead the Account Associates "
    "who answer inbound walkthrough requests and hire for the team as it grows. You will work with "
    "the Director of Sales on lost-deal reviews and playbooks, and track renewal rates in Metabase."
)
CONTOSO: dict[str, Any] = {
    "target_company": "Contoso",
    "job_title": "Inside Sales Manager",
    "jd_text": CONTOSO_JD,
}
# three sentences a letter for this posting might draft. None passes: the
# first and third borrow the JD's "Account Associate(s)" in a first-person
# sentence, and the second states a duration no source holds. All three are
# accepted false rejections.
CONTOSO_SENTENCES = [
    "For your Account Associates, that means notes on every recorded call, not only the calls "
    "that fit around my meetings.",
    "I'd spend my first sixty days listening to your call recordings.",
    "In the Inside Sales Manager role at Contoso, I'd make sure every Account Associate "
    "hears feedback on their own calls, not only on the calls I join.",
]


# Accepted false rejections (fourth review of validator-refine): each of these
# passed under allowance (a), which let a JD word name the target's own people
# in a forward-looking first-person sentence. Its guard was a list of
# experience words that new phrasings slipped past (see
# test_a_first_person_sentence_borrows_no_jd_word), so it was removed: a
# first-person sentence needs a cited source for every proper noun, as at
# aca26cc. "Your new Account Associates will get…" has no "I", but a promise is
# no sentence about the target (_JD_PROMISE).
FORMER_ALLOWANCE_A = [
    CONTOSO_SENTENCES[0],
    "I'll coach every Account Associate on your team.",
    "Your Account Associates will get coaching that scales, and I'll build it.",
    "Account Associates on your team deserve coaching that scales, and I'll build it.",
    "I can coach your Account Associates.",
    "Your new Account Associates will get coaching that scales.",
    "I'll know every Account Associate on your team by name.",
    "I'd build a coaching cadence for Contoso's Account Associates.",
    "I'd sit in on calls with Account Associates at Contoso.",
    "I'd sit in on calls with the Account Associates at Contoso.",
]


@pytest.mark.parametrize("text", FORMER_ALLOWANCE_A)
def test_allowance_a_is_removed(text: str) -> None:
    v = _check(text, [], **CONTOSO)
    assert any(x["kind"] == "unsourced_proper_noun" and "Associate" in x["detail"] for x in v), v


def test_the_contoso_sentence_fails_inside_a_letter_and_nothing_else_does() -> None:
    letter = (
        "Contoso is hiring an Inside Sales Manager. "
        "Contoso's Account Associates book 35 walkthroughs a week.\n\n"
        "At Haulmark I led 9 AEs and built the channel-partner program from 3 to 17 reseller "
        "partners in ~18 months. I also turned a small pilot into a remote onboarding "
        "program.\n\n" + CONTOSO_SENTENCES[0] + " Let's set up the conversation."
    )
    for style in ("hedged", "confident"):
        v = _check(letter, ["b_052", "b_044"], estimate_style=style, **CONTOSO)
        assert v, style
        assert all(
            x["kind"] == "unsourced_proper_noun" and x["sentence"] == CONTOSO_SENTENCES[0][:160]
            for x in v
        ), v
        # without it, the same letter passes: P1 opens with the target's name
        rest = letter.replace(CONTOSO_SENTENCES[0] + " ", "")
        assert _check(rest, ["b_052", "b_044"], estimate_style=style, **CONTOSO) == []


# Accepted false rejections (third review of validator-refine): a plan's
# window needs a cited source like any other number, in a letter and in an
# answer. The allowance that let these through also let through "I'll never
# forget hitting quota within the first 30 days", and could not be made safe
# simply, so it was removed.
PLAN_WINDOWS = [
    CONTOSO_SENTENCES[1],
    "I'd build a 30-60-90 day plan for the Inside Sales Manager role.",
    "By month three, every Account Associate on your team would run their own pipeline review.",
    "My plan for the first 90 days is to sit in on calls with your Account Associates.",
    "Within 30 days I'd know every Account Associate on your team by name.",
    "Over the next two quarters I'd rebuild your playbooks.",
    "I'd like to walk you through the first 90 days.",
    "I'll sit in on calls with your Account Associates, starting in the first 30 days.",
    "Within the first 30 days, I'll sit in on every call with your Account Associates.",
    "I'll set up weekly reviews for your Account Associates within the first 30 days.",
    "A 90-day plan is attached.",
    "Let's talk about the first 90 days.",
    "I'd love to talk about how I'd spend my first 60 days.",
    "I look forward to discussing the first 90 days.",
    "I'll rebuild your sales process within the first 90 days.",
    "I'll run weekly sales calls with your Account Associates within the first 30 days.",
    "Then within 30 days I'll start weekly pipeline reviews.",
    "By day 30 I'll know every Account Associate on your team by name.",
    "Your Account Associates will get weekly coaching within the first 30 days.",
    "I'll start by sitting in on calls in the first 30 days.",
    "I'll show you how I'd spend the first 60 days.",
    "I'll coach the reps you hire within the first 30 days.",
    "In my first 6 months I'd sit in on every call with your Account Associates.",
]


@pytest.mark.parametrize("text", PLAN_WINDOWS)
@pytest.mark.parametrize("style", ["hedged", "confident"])
def test_a_plan_window_needs_a_source(text: str, style: str) -> None:
    kinds = _kinds(_check(text, [], estimate_style=style, **CONTOSO))
    assert kinds & {"fabricated_number", "missing_citations"}, text


# …and so does a JD word in any first-person sentence, including one whose
# only first person is "means a lot to me"
@pytest.mark.parametrize(
    "text",
    [
        CONTOSO_SENTENCES[2],
        "I'll partner with your Director of Sales on every Account Associate's development.",
        "Working with your Account Associates means a lot to me.",
        "I'm looking forward to meeting your Account Associates.",
    ],
)
def test_an_untied_jd_word_is_rejected(text: str) -> None:
    v = _check(text, [], **CONTOSO)
    assert any(x["kind"] == "unsourced_proper_noun" and "Associate" in x["detail"] for x in v), v


@pytest.mark.parametrize(
    ("text", "cited", "kind", "needle"),
    [
        # a past claim: the duration and the JD noun stay forbidden
        (
            "In my first 90 days at Haulmark I doubled revenue.",
            ["b_047"],
            "fabricated_number",
            "90",
        ),
        (
            "In my first 90 days at Haulmark I doubled revenue.",
            ["b_047"],
            "fabricated_number",
            "doubled",
        ),
        ("In my first 90 days I hit quota.", [], "fabricated_number", "90"),
        ("In my first 90 days, the team hit quota.", [], "fabricated_number", "90"),
        (
            "Within 90 days at Haulmark I onboarded new carriers.",
            ["b_044"],
            "fabricated_number",
            "90",
        ),
        ("I ramped your kind of team in 90 days.", [], "fabricated_number", "90"),
        (
            "At Haulmark I coached our Account Associates.",
            ["b_052"],
            "unsourced_proper_noun",
            "Assoc",
        ),
        ("I've coached teams like your Account Associates.", [], "unsourced_proper_noun", "Assoc"),
        (
            "I coach Account Associates every day, which is what your team needs.",
            [],
            "unsourced_proper_noun",
            "Assoc",
        ),
        # the JD's title as the candidate's own, in any tense
        ("As an Account Associate I built pipeline.", [], "unsourced_proper_noun", "Associate"),
        (
            "As an Account Associate I'd bring that energy to your team.",
            [],
            "unsourced_proper_noun",
            "Associate",
        ),
        ("I'd be the Account Associate your team needs.", [], "unsourced_proper_noun", "Associate"),
        # a JD tool is never the candidate's skill, and is not the target's own here
        (
            "I'd bring deep Metabase expertise to your team.",
            [],
            "unsourced_proper_noun",
            "Metabase",
        ),
        (
            "I'll run Metabase reports for your Account Associates.",
            [],
            "unsourced_proper_noun",
            "Metabase",
        ),
        # metrics stay forbidden in every tense, JD numbers included
        ("I'll grow your ticket volume 5x.", [], "fabricated_number", "5x"),
        (
            "I'll grow your ticket volume fivefold in my first 90 days.",
            [],
            "fabricated_number",
            "five",
        ),
        ("I'll add $2M in pipeline for your team.", [], "fabricated_number", "$2M"),
        ("I'll lift your win rates 20% in 90 days.", [], "fabricated_number", "20%"),
        ("I'll book 35 walkthroughs a week within 90 days.", [], "fabricated_number", "35"),
        ("I'd hire 5 Account Associates in my first 90 days.", [], "fabricated_number", "5"),
        (
            "I'd give every Account Associate on your team 5 accounts.",
            [],
            "fabricated_number",
            "5",
        ),
        # a duration someone has is tenure, not a plan
        (
            "I'd bring 18 months of coaching to your Account Associates.",
            [],
            "fabricated_number",
            "18",
        ),
        ("I'll bring two decades of sales to your team.", [], "fabricated_number", "two decades"),
        ("In my first two years I'd double your team.", [], "fabricated_number", "two"),
        # review of validator-refine: a past outcome in a relative clause or behind
        # a tense-ambiguous verb ("cut", "put") or an irregular past ("fell")
        (
            "I'll bring my onboarding program, which cut new-rep ramp to 30 days.",
            [],
            "fabricated_number",
            "30",
        ),
        (
            "I'll bring the playbook that put new reps on quota in 45 days.",
            [],
            "fabricated_number",
            "45",
        ),
        (
            "I'll repeat the coaching cadence under which ramp fell to 30 days.",
            [],
            "fabricated_number",
            "30",
        ),
        (
            "I'll bring my ramp program, which cut onboarding to 30 days, to your team.",
            ["b_052"],
            "fabricated_number",
            "30",
        ),
        (
            "I'll bring the playbook that put new reps on quota within the first 45 days.",
            [],
            "fabricated_number",
            "45",
        ),
        # …a present claim about the candidate's own team ("my reps", "our team")
        ("My reps ramp in 30 days, and I'll do the same for yours.", [], "fabricated_number", "30"),
        (
            "My team closes deals in 14 days, and I'll bring that to your team.",
            [],
            "fabricated_number",
            "14",
        ),
        (
            "Our team coaches every Account Associate weekly, and I'll do the same for your team.",
            [],
            "unsourced_proper_noun",
            "Assoc",
        ),
        (
            "My team coaches every Account Associate weekly, the way your team will.",
            [],
            "unsourced_proper_noun",
            "Assoc",
        ),
        (
            "Every Account Associate on my team books meetings daily, "
            "and I'll bring that to yours.",
            [],
            "unsourced_proper_noun",
            "Assoc",
        ),
        ("I'll bring my Account Associates to your team.", [], "unsourced_proper_noun", "Assoc"),
        (
            "Each of my Account Associates would thrive on your team.",
            [],
            "unsourced_proper_noun",
            "Assoc",
        ),
        # …a JD tool, bound by "the", is still not the target's people
        (
            "I can run the Metabase integration your team needs.",
            [],
            "unsourced_proper_noun",
            "Metabase",
        ),
        (
            "I'll run the Metabase integration your team needs.",
            [],
            "unsourced_proper_noun",
            "Metabase",
        ),
        # …a habit, a track record or a stance that reports a fact
        ("I would typically ramp a new rep to quota in 45 days.", [], "fabricated_number", "45"),
        ("I would usually get new hires productive within 30 days.", [], "fabricated_number", "30"),
        ("I'd normally close an enterprise deal in 90 days.", [], "fabricated_number", "90"),
        ("My sales cycle would be 45 days.", [], "fabricated_number", "45"),
        ("I'd say my average deal closes in 60 days.", [], "fabricated_number", "60"),
        ("Reps on my team would hit quota within 60 days.", [], "fabricated_number", "60"),
        ("My deals would usually close within 30 days.", [], "fabricated_number", "30"),
        ("I'll keep ramping reps in 45 days.", [], "fabricated_number", "45"),
        (
            "I would typically ramp a new rep within the first 45 days.",
            [],
            "fabricated_number",
            "45",
        ),
        ("I'd say reps ramp within 30 days.", [], "fabricated_number", "30"),
        ("I'd hit quota within my first 30 days.", [], "fabricated_number", "30"),
        # …a duration as an outcome, or in a clause no future verb governs
        ("I'll ramp your new reps to quota in 45 days.", [], "fabricated_number", "45"),
        ("I can get your reps productive within 30 days.", [], "fabricated_number", "30"),
        ("New hires would be productive within 30 days.", [], "fabricated_number", "30"),
        (
            "New reps reach quota within 30 days with this playbook, and I'll bring it to you.",
            [],
            "fabricated_number",
            "30",
        ),
        # second review of validator-refine: a future verb in a relative or object
        # clause does not govern the duration's own verb (the comma-less form)
        *(
            (text, [], "fabricated_number", "30")
            for text in (
                "New reps reach quota within the first 30 days with the playbook I'll bring.",
                "Reps ramp within the first 30 days under the program I'll bring to your team.",
                "Reps get to quota within the first 30 days on the playbook I'll bring.",
                "Within the first 30 days reps reach quota with the playbook I'll bring.",
                "The playbook I'll bring gets reps to quota within the first 30 days.",
                "I'll show you how they ramp within the first 30 days.",
                "I'll show you how new reps ramp within the first 30 days.",
            )
        ),
        # …"me" after a preposition is the candidate, as "my" is: no JD noun
        # bound by "the", no unsourced duration for their team
        (
            "I'll keep coaching the Account Associates reporting to me, and your team too.",
            [],
            "unsourced_proper_noun",
            "Assoc",
        ),
        (
            "Your Account Associates will get the coaching "
            "the Account Associates under me get today.",
            [],
            "unsourced_proper_noun",
            "Assoc",
        ),
        (
            "I'll bring the weekly reviews the Account Associates under me run today to your team.",
            [],
            "unsourced_proper_noun",
            "Assoc",
        ),
        (
            "Your Account Associates will ramp within the first 30 days, as they do for me.",
            [],
            "fabricated_number",
            "30",
        ),
        (
            "I'll coach the Account Associates who report to me.",
            [],
            "unsourced_proper_noun",
            "Assoc",
        ),
        ("I'll onboard myself within the first 30 days.", [], "fabricated_number", "30"),
        # …"I'd" before a participle that is also the base form may be "I had"
        (
            "I'd run the whole partner program within my first 90 days, and I'll do it for "
            "your team.",
            [],
            "fabricated_number",
            "90",
        ),
        (
            "Within the first 90 days, I'd run the partner program.",
            [],
            "fabricated_number",
            "90",
        ),
        (
            "By my first 90 days I'd become the go-to coach for every new rep.",
            [],
            "fabricated_number",
            "90",
        ),
        (
            "I'd set up weekly reviews for the Account Associates, and I'll do it for your team.",
            [],
            "unsourced_proper_noun",
            "Assoc",
        ),
        # …the candidate's own availability, start date, notice or relocation is
        # never a plan's window, even in a cover letter
        ("I'll be available to start within 30 days.", [], "fabricated_number", "30"),
        ("I'll be able to start within the next two weeks.", [], "fabricated_number", "two"),
        ("I'd be ready to relocate within the first 30 days.", [], "fabricated_number", "30"),
        ("I'll start within 30 days.", [], "fabricated_number", "30"),
        ("I'd join your team within 30 days of an offer.", [], "fabricated_number", "30"),
    ],
)
def test_forward_allowances_never_cover_the_past_or_a_metric(
    text: str, cited: list[str], kind: str, needle: str
) -> None:
    v = _check(text, cited, **CONTOSO)
    assert any(x["kind"] == kind and needle in x["detail"] for x in v), v


def test_the_job_title_is_never_claimed_as_held() -> None:
    # in CORPUS, "Inside" is Keyward Security's title word: claimed at Haulmark
    # it is misattributed
    v = _check("I was the Inside Sales Manager at Haulmark.", ["b_052"], **CONTOSO)
    assert "misattributed_claim" in _kinds(v), v
    # with no such coincidence, the job's own title claimed as held still fails
    corpus = copy.deepcopy(CORPUS)
    for role in corpus["roles"]:
        role["title"] = role["title"].replace("Inside ", "")
    for b in corpus["bullets"]:
        for k in ("text_long", "text_short"):
            b[k] = b[k].replace("inside ", "")

    def check(text: str, cited: list[str]) -> list[dict[str, str]]:
        return validate_claims(text, cited, corpus, known_companies=KNOWN, **CONTOSO).violations

    for text, cited in (
        ("I was the Inside Sales Manager at Haulmark.", ["b_052"]),
        ("At Haulmark I was the Inside Sales Manager, leading 9 AEs.", ["b_052"]),
        ("I was the Inside Sales Manager for ~18 months.", ["b_052"]),
        ("As Inside Sales Manager at Haulmark, I led 9 AEs.", ["b_052"]),
        ("At Haulmark I did the job of an Inside Sales Manager.", ["b_052"]),
        ("As an Inside Sales Manager, I coach reps daily.", []),
        # a negation that does not deny the seat is still a claim (second
        # review of validator-refine)
        ("I haven't just held the Inside Sales Manager title at Haulmark.", ["b_052"]),
        ("I have not only been the Inside Sales Manager at Haulmark.", ["b_052"]),
        ("I wasn't officially the Inside Sales Manager at Haulmark.", ["b_052"]),
        ("I never stopped being the Inside Sales Manager at Haulmark.", ["b_052"]),
        (
            "Nobody would say I haven't held the Inside Sales Manager title at Haulmark.",
            ["b_052"],
        ),
    ):
        assert "claimed_job_title" in _kinds(check(text, cited)), text
    for text, cited in (
        # the role sought, in a sentence that claims nothing about the candidate
        # or in an aspirational frame (review of validator-refine)
        ("As an Inside Sales Manager, I'd build pipeline.", []),
        ("I'm excited to apply as an Inside Sales Manager.", []),
        ("As an Inside Sales Manager candidate, I'd build the team.", []),
        ("I want to be an Inside Sales Manager.", []),
        ("I'd thrive as an Inside Sales Manager here.", []),
        ("Becoming an Inside Sales Manager is the next step for me.", []),
        ("I've always wanted to be an Inside Sales Manager.", []),
        ("As your Inside Sales Manager, I'd build the team.", []),
        ("I'd be the Inside Sales Manager your team needs.", []),
        ("I'm applying for the Inside Sales Manager role.", []),
        (
            "At Haulmark I led 9 AEs, which prepared "
            "me for the Inside Sales Manager role at Contoso.",
            ["b_052"],
        ),
        ("As Regional Sales Manager at Haulmark, I led 9 AEs.", ["b_052"]),  # a title they held
        # an honest disclaimer — what "Have you worked as an Inside Sales
        # Manager?" calls for (second review of validator-refine)
        (
            "I haven't held the Inside Sales Manager title, but I led 9 AEs at Haulmark.",
            ["b_052"],
        ),
        ("I have not been an Inside Sales Manager before.", []),
        ("No, I haven't held the Inside Sales Manager title.", []),
        ("I've never held the title of Inside Sales Manager.", []),
        ("I have not worked as an Inside Sales Manager.", []),
        ("I'm not yet an Inside Sales Manager.", []),
        ("I was never an Inside Sales Manager.", []),
        ("I wasn't titled Inside Sales Manager.", []),
    ):
        assert check(text, cited) == [], text
    # the title is the role sought, but what the candidate brings is a claim,
    # and no cited bullet says "hands-on coaching" (sixth review of the
    # self-review)
    v = check("As an Inside Sales Manager candidate, I bring hands-on coaching.", [])
    assert _kinds(v) == {"unsourced_experience_claim"}, v


def test_never_claim_titles_from_the_jd_stay_guarded() -> None:
    # someone else's title, tied to the target in a forward sentence, is sourced
    # by the JD; claimed as the candidate's own it is still a hard fail
    out = validate_claims(
        "As your Director of Sales, I'd build the team.",
        [],
        CORPUS,
        known_companies=KNOWN,
        **CONTOSO,
    )
    assert "never_claim_title" in _kinds(out.violations) and has_hard(out)
    v = _check(
        "Our Director of Sales asked me to build the channel-partner program.",
        ["b_052"],
        **CONTOSO,
    )
    assert "unsourced_title_mention" in _kinds(v), v


# the questions form_spec.draft_novel_answer drafts with no allowed_ids ("What
# is your typical sales cycle?"): a habit or a track record is a fact, in any
# fixture (review of validator-refine)
TRACK_RECORD_ANSWERS = [
    "I would typically ramp a new rep to quota in 45 days.",
    "I would usually get new hires productive within 30 days.",
    "I'd normally close an enterprise deal in 90 days.",
    "My sales cycle would be 45 days.",
    "I'd say my average deal closes in 60 days.",
    "Reps on my team would hit quota within 60 days.",
    "My deals would usually close within 30 days.",
    "I'll keep ramping reps in 45 days.",
]


@pytest.mark.parametrize("text", TRACK_RECORD_ANSWERS)
@pytest.mark.parametrize("fixture", ["fabrikam", "contoso"])
def test_a_track_record_is_never_a_plan(text: str, fixture: str) -> None:
    kw: dict[str, Any] = dict(CONTOSO) if fixture == "contoso" else {}
    assert {"missing_citations", "fabricated_number"} <= _kinds(_check(text, [], **kw)), text


def test_a_past_outcome_in_a_relative_clause_fails_inside_a_letter() -> None:
    letter = (
        "Contoso is hiring an Inside Sales Manager to grow "
        "Account Associates into future Account Executives.\n\n"
        "At Haulmark I led 9 AEs and built the channel-partner "
        "program from 3 to 17 reseller partners in ~18 months.\n\n"
        "I'll bring my ramp program, which cut onboarding to 30 days, to your team."
    )
    v = _check(letter, ["b_052"], **CONTOSO)
    assert any(x["kind"] == "fabricated_number" and "30" in x["detail"] for x in v), v


@pytest.mark.parametrize(
    ("text", "term"),
    [
        ("I'd bring the MEDDPICC rigor your Account Associates need.", "MEDDPICC"),
        ("I'd run the Outreach cadences your team needs from day one.", "Outreach"),
        ("I'll lean on the HubSpot workflows I'd rebuild for your team.", "HubSpot"),
        ("I'd lean on the HubSpot workflows your team uses.", "HubSpot"),
        ("I'll rebuild the Lead Scoring your Account Associates use.", "Scoring"),
    ],
)
def test_a_jd_requirement_is_never_borrowed_as_a_skill(text: str, term: str) -> None:
    # JD-required tools and methods name no one at the target: allowance (a)
    # covers the target's people, roles and teams only (review of validator-refine)
    jd = CONTOSO_JD + " Experience with MEDDPICC, Outreach, HubSpot and Lead Scoring required."
    v = _check(text, [], **{**CONTOSO, "jd_text": jd})
    assert any(x["kind"] == "unsourced_proper_noun" and term in x["detail"] for x in v), v


# third review of validator-refine: each of these passed before the plan-window
# allowance was removed and allowance (a) narrowed (the fourth review removed
# (a) as well). This stand-in JD names a third company's team (Vandelay) and a
# third company whose name ends in a corpus word (Globex Payroll).
RIVALS_JD = CONTOSO_JD + " You will win accounts from the Vandelay Sales team and Globex Payroll."
RIVALS: dict[str, Any] = {**CONTOSO, "jd_text": RIVALS_JD}


@pytest.mark.parametrize(
    ("text", "cited", "kw", "kind", "needle"),
    [
        # experience reported about JD people tied only by "the"
        (
            "I'll bring the lessons of coaching the Account Associates to your team.",
            [],
            CONTOSO,
            "unsourced_proper_noun",
            "Associates",
        ),
        (
            "At Haulmark I led 9 AEs. I'll bring the lessons of coaching the Field "
            "Associates there to your team.",
            ["b_052"],
            CONTOSO,
            "unsourced_proper_noun",
            "Associates",
        ),
        # durations in a forward frame: memory, storytelling, a knack, a regret
        (
            "I'll never forget hitting quota within the first 30 days.",
            [],
            CONTOSO,
            "fabricated_number",
            "30",
        ),
        (
            "I'd love to tell you about ramping new reps within 30 days.",
            [],
            CONTOSO,
            "fabricated_number",
            "30",
        ),
        (
            "I'll bring a knack for ramping reps within 30 days.",
            [],
            CONTOSO,
            "fabricated_number",
            "30",
        ),
        (
            "I'll miss closing deals within the first two weeks.",
            [],
            CONTOSO,
            "fabricated_number",
            "two",
        ),
        # a third company's team named in the JD is never the target's own
        (
            "I'll win accounts from the Vandelay Sales team for your Account Associates.",
            [],
            RIVALS,
            "unsourced_proper_noun",
            "Vandelay",
        ),
        (
            "I'll help your Account Associates win accounts from the Vandelay Sales team.",
            [],
            RIVALS,
            "unsourced_proper_noun",
            "Vandelay",
        ),
        ("I'll help your Vandelay Sales team.", [], RIVALS, "unsourced_proper_noun", "Vandelay"),
        (
            "I'll help your team win accounts from Globex Payroll.",
            [],
            RIVALS,
            "unsourced_proper_noun",
            "Globex",
        ),
        # a past claim closed by "means a lot to me" borrows no JD number (it
        # fails on its gerund opening too: see JD_FACT_BORROWED)
        (
            "Booking 35 walkthroughs a week with a team like your Account Associates was the job, "
            "which means a lot to me.",
            ["b_052"],
            CONTOSO,
            "fabricated_number",
            "35",
        ),
        (
            "Running a team that booked 35 walkthroughs a week, "
            "the pace your Account Associates keep, "
            "was the work that means a lot to me.",
            ["b_052"],
            CONTOSO,
            "fabricated_number",
            "35",
        ),
        (
            "Your 5x ticket volume within 120 days is exciting to me.",
            [],
            CONTOSO,
            "fabricated_number",
            "5x",
        ),
    ],
)
def test_third_review_holes_are_closed(
    text: str, cited: list[str], kw: dict[str, Any], kind: str, needle: str
) -> None:
    v = _check(text, cited, **kw)
    assert any(x["kind"] == kind and needle in x["detail"] for x in v), v


# A first-person sentence borrows no JD word (fourth review of
# validator-refine). Allowance (a) let one name the target's people when tied
# by "your" and withdrew it on a list of experience words; the first group
# below are what that list caught. The second group are the reviewer's
# phrasings it missed: each passed under (a) and failed at aca26cc. A
# denylist keeps missing new phrasings, so (a) was removed and all of these
# fail on the JD word itself.
@pytest.mark.parametrize(
    "text",
    [
        # what (a)'s denylist missed: a prior seat, tenure, or an activity
        # that is already going on
        "I'll coach your Account Associates as their former peer.",
        "I'll coach your Account Associates like a manager who knows their job firsthand.",
        "I'll coach your Account Associates as a longtime closer.",
        "I'll coach your Account Associates as an ex-Account Executive.",
        "I'll continue to coach your Account Associates.",
        "I'll still coach your Account Associates.",
        # experience nouns
        "I'll bring the lessons of coaching to your Account Associates.",
        "I'll bring experience leading reps to your Account Associates.",
        "I'll bring years of coaching to your Account Associates.",
        "I'll bring a background in coaching to your Account Associates.",
        "I'll bring a track record with new reps to your Account Associates.",
        "I'd bring strong credentials to your Account Associates.",
        "I'd bring deep expertise to your Account Associates.",
        "I'll bring a knack for coaching to your Account Associates.",
        # memory and repetition
        "I'll never forget the reps who were like your Account Associates.",
        "I'll miss the reps, and I'll give your Account Associates the same energy.",
        "I'll coach your Account Associates again.",
        # a gerund with an object
        "I'll bring a love of managing reps to your Account Associates.",
        "Coaching new reps is what I'd bring to your Account Associates.",
        "I'll start leading your Account Associates in week one.",
        # a past tense, even inside a reaction
        "I'd coach your Account Associates the way your posting showed me.",
        "I'd coach your Account Associates as I coached reps.",
        # a tie that is not the target's: "the"/"all"/"these"/"those"/"every"/
        # "each" alone, a competitor's, or another company's
        "I'll coach the Account Associates.",
        "I'll coach all Account Associates.",
        "I'll coach these Account Associates.",
        "I'll coach those Account Associates for your team.",
        "I'll coach every Account Associate for your team.",
        "I'll coach each Account Associate for your team.",
        "I'll coach your competitors' Account Associates.",
        "I'll win deals from the Account Associates at Contoso's competitors for you.",
        "I'll win deals from Account Associates on your team's rival.",
        # the candidate's own
        "I'll coach your Account Associates and my Account Associates together.",
        "I'll coach your Account Associates like the Account Associates under me.",
        "I'll coach your Account Associates like the reps who report to us.",
    ],
)
def test_a_first_person_sentence_borrows_no_jd_word(text: str) -> None:
    v = _check(text, [], **CONTOSO)
    assert any(x["kind"] == "unsourced_proper_noun" and "Associate" in x["detail"] for x in v), v


def test_the_contoso_sentence_and_its_claim_twins_fail() -> None:
    for twin in (
        CONTOSO_SENTENCES[0],
        # the same sentence, reporting experience
        "For your Account Associates, that means the lost-deal reviews that worked for my team.",
        "For your Account Associates, that means reviewing calls the way I did at Haulmark.",
        "For your Account Associates, I'd bring years of weekly lost-deal reviews.",
        "For the Account Associates, that means notes on every recorded call, not only the "
        "calls that fit around my meetings.",
    ):
        assert _check(twin, [], **CONTOSO), twin


# second review of validator-refine: each of these stated an unsourced result,
# tenure or circumstance of the candidate's and passed with no citations. All
# failed at aca26cc with missing_citations and fabricated_number.
UNSOURCED_TIMELINES = [
    "New reps reach quota within the first 30 days with the playbook I'll bring.",
    "Reps ramp within the first 30 days under the program I'll bring to your team.",
    "Reps get to quota within the first 30 days on the playbook I'll bring.",
    "Your Account Associates will ramp within the first 30 days, as they do for me.",
    "I'd run the whole partner program within my first 90 days, and I'll do it for your team.",
    "Within the first 90 days, I'd run the partner program.",
    "By my first 90 days I'd become the go-to coach for every new rep.",
    "I'll be available to start within 30 days.",
    "I'll be able to start within the next two weeks.",
    "I'd be ready to relocate within the first 30 days.",
]


@pytest.mark.parametrize("text", UNSOURCED_TIMELINES)
def test_an_unsourced_timeline_needs_citations_in_a_letter(text: str) -> None:
    assert {"missing_citations", "fabricated_number"} <= _kinds(_check(text, [], **CONTOSO)), text


# a window in a drafted answer is usually the candidate's availability,
# notice or relocation, which only the human or the Answer Bank may state
# (Hard Rule 8); like any window, it needs a cited source
NOVEL_TIMELINES = [
    "I'll be available to start within 30 days.",
    "I'll be able to start within the next two weeks.",
    "I'd be ready to relocate within the first 30 days.",
    "I'd spend my first sixty days listening to your call recordings.",
]


@pytest.mark.parametrize("text", NOVEL_TIMELINES)
def test_a_novel_answer_gets_no_unsourced_window(text: str) -> None:
    v = _check(text, [], **CONTOSO)
    assert {"missing_citations", "fabricated_number"} <= _kinds(v), text


@pytest.mark.parametrize("text", NOVEL_TIMELINES)
def test_draft_novel_answer_never_returns_an_unsourced_timeline(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    fake = FakeLLM({"novel": [{"answer": text, "bullets_used": []}]})
    monkeypatch.setattr(llm, "call_json", fake.call_json)
    monkeypatch.setattr(llm, "forget", fake.forget)
    answer, reason, violations = form_spec.draft_novel_answer(
        conn,
        prompt=llm.PromptFile("novel_question"),
        corpus=CORPUS,
        profile=drafting_profile(CORPUS),
        job={
            "id": 1,
            "title": "Inside Sales Manager",
            "company_name": "Contoso",
            "jd_html": f"<p>{CONTOSO_JD}</p>",
        },
        label="When could you start?",
        question="When could you start?",
    )
    assert answer is None and reason == "unsourced-claim"
    assert "fabricated_number" in _kinds(violations)


def test_tailor_rejects_a_cover_letter_with_an_unsourced_plan_window(
    conn: sqlite3.Connection, tailor_env: Callable[..., tuple[FakeLLM, int]]
) -> None:
    # tailor.py checks a cover letter with no plan-window allowance (third
    # review of validator-refine): "my first sixty days" needs a source
    letter = GOOD_LETTER + " I'd spend my first sixty days listening to your call recordings."
    _fake, jid = tailor_env(
        [{"letter": letter, "bullets_used": GOOD_CITED}], [_selection(["Salesforce", "MEDDIC"])]
    )
    paths = tailor.tailor_job(conn, jid)
    assert paths["cover"] == "" and paths["cover_rejected"]
    prov = json.loads(Path(paths["provenance"]).read_text())
    assert prov["cover_fact_check"]["ok"] is False
    assert any(
        v["kind"] == "fabricated_number" and "sixty" in v["detail"]
        for v in prov["cover_fact_check"]["violations"]
    )


@pytest.mark.parametrize(
    ("sentence", "ok"),
    [
        # a first-person sentence borrows no JD word: allowance (a) is removed
        # (fourth review of validator-refine), so the cover is parked
        (" I'd build the leadership dashboards your Account Associates need.", False),
        # the same JD words in a sentence about the role that restates the
        # posting word for word (fifth review)
        (" The role partners with the Account Associates.", True),
        # …in other words than the posting's: an accepted false rejection
        (" Fabrikam's Account Associates need leadership dashboards.", False),
    ],
)
def test_tailor_checks_a_letter_naming_the_targets_own_people(
    conn: sqlite3.Connection,
    tailor_env: Callable[..., tuple[FakeLLM, int]],
    sentence: str,
    ok: bool,
) -> None:
    # through tailor.py, with a stand-in JD that names Fabrikam's
    # "Account Associates"
    letter = GOOD_LETTER + sentence
    _fake, jid = tailor_env(
        [{"letter": letter, "bullets_used": GOOD_CITED}], [_selection(["Salesforce", "MEDDIC"])]
    )
    conn.execute(
        "UPDATE jobs SET jd_html = ? WHERE id = ?",
        (f"<p>{FABRIKAM_JD} You will partner with the Account Associates.</p>", jid),
    )
    conn.commit()
    paths = tailor.tailor_job(conn, jid)
    prov = json.loads(Path(paths["provenance"]).read_text())
    if ok:
        assert prov["cover_fact_check"] == {"ok": True, "violations": []}, prov
        assert paths["cover"] and "cover_rejected" not in paths
    else:
        assert prov["cover_fact_check"]["ok"] is False
        assert [
            (v["kind"], v["detail"].split(" ")[0]) for v in prov["cover_fact_check"]["violations"]
        ] == [("unsourced_proper_noun", "Associates")], prov["cover_fact_check"]
        assert paths["cover"] == "" and paths["cover_rejected"]


# ---------------------------------------------------------------------------
# sixth review of the self-review (2026-10-09; Hard Rule 8): a first-person
# claim of experience, skill, credentials or background uses only the words of
# the bullets it cites (unsourced_experience_claim). Each of these passed every
# check, mid-letter, at the end of GOOD_LETTER and in a drafted answer, with
# every id allowed; tailor_job shipped the letter when the critic passed it.
# ---------------------------------------------------------------------------

EXPERIENCE_CLAIMS = [
    "I would bring years of field service operations leadership.",
    "I can bring the trade credentials.",
    "I'd bring deep experience in warranty claims management.",
    "I have deep expertise in equipment financing.",
    "I am a licensed electrician.",
    # the same claim in other frames
    "I'd love to bring years of field service leadership to Fabrikam.",
    "My background in equipment financing fits this role.",
    "I'm a former HVAC technician.",
    "I offer deep knowledge of warranty claims management.",
    "I bring a proven track record in construction sales.",
    "I have the skills to run your weekly revenue meeting.",
]
PARTNERS = (
    "At Haulmark I built the channel-partner program from 3 to 17 reseller partners in ~18 months."
)


@pytest.mark.parametrize("claim", EXPERIENCE_CLAIMS)
def test_an_experience_claim_uses_only_its_cited_bullets_words(claim: str) -> None:
    for text, cited in (
        (f"{GOOD_LETTER} {claim}", GOOD_CITED),
        (GOOD_LETTER.replace("Since March 2024", f"{claim} Since March 2024"), GOOD_CITED),
        (f"{PARTNERS} {claim}", ["b_052"]),
        (f"Yes. {claim}", ["b_052"]),
    ):
        assert "unsourced_experience_claim" in _kinds(_check(text, cited)), text


def test_an_experience_claim_in_its_cited_bullets_words_passes() -> None:
    assert _check(GOOD_LETTER, GOOD_CITED) == []
    assert _check(PARTNERS, ["b_052"]) == []
    for text, cited in (
        (
            "I'd bring the onboarding program that cut time to first dispatch by ~40%.",
            ["b_044"],
        ),
        ("I have a B.S. from Lakeside State University.", ["edu_1"]),
        ("I was the Head of Revenue Operations at ledgerline.", ["b_049"]),
        ("I'm excited to apply.", []),
    ):
        assert _check(text, cited) == [], text
    # an accepted false rejection: the target's name lends no word to a claim
    # (a target named "Field Service Partners" would source "I'm a field
    # service partner")
    v = _check(
        "At Haulmark I led a team of 9 account executives, and I'd bring that to Fabrikam.",
        ["b_052"],
    )
    # …and the target named in a sentence with a past claim fails closed
    # (second review 2026-10-09: "I led Contoso's enterprise team" passed), an
    # accepted false rejection like "…, and I'd love to join Contoso"
    claims = [
        (x["kind"], x["detail"].split(":")[0]) for x in v if x["kind"] != "fabricated_employer"
    ]
    assert claims == [("unsourced_experience_claim", "fabrikam")], v
    assert _kinds(v) == {"unsourced_experience_claim", "fabricated_employer"}, v


# What word coverage cannot see (the validate/claims.py docstring): a claim
# with no number, proper noun or experience marker, whose words all come from
# its cited bullets ("merchant" is b_049's, "~18 months" b_052's) or would be
# caught only by holding every first-person sentence to its cited bullets'
# words, which fails GOOD_LETTER's honest "I coach deals on MEDDIC"; a past
# implied by an opinion ("…was a great place to work"); a skill in plain words
# ("inside and out"); a relationship. These were strict xfails. The LLM claim
# judge (validate/claim_judge.py, package 4) is now the gate for them: it
# reads every letter and drafted answer the rules pass. The first test still
# pins the rules' blind spot (a rule that starts catching one turns it red,
# and the docstring and this list are updated with it); the rest show each
# claim failed by the judge, in a letter and in an answer, through the same
# fail-closed paths as a rule-based violation.
BLIND_SPOT = [
    "I ran the merchant team for ~18 months.",
    "I also led the sales development team.",
    "I owned the entire company revenue.",
    "Fabrikam Payments was a great place to work.",
    "I know trade licensing inside and out.",
    "My old team at Haulmark loved me.",
]


@pytest.mark.parametrize("claim", BLIND_SPOT)
def test_the_rules_alone_miss_a_blind_spot_claim(claim: str) -> None:
    assert _check(f"{GOOD_LETTER} {claim}", GOOD_CITED) == []
    assert _check(f"{PARTNERS} {claim}", ["b_052"]) == []


def _judge_gate(conn: sqlite3.Connection, text: str, cited: list[str]) -> ProvenanceResult:
    """The cover letter's full fact check: the rules, then the judge."""
    rules = validate_claims(
        text,
        cited,
        CORPUS,
        target_company="Fabrikam Payments",
        job_title="Director of Revenue Operations",
        jd_text=FABRIKAM_JD,
        known_companies=KNOWN,
    )
    return claim_judge.gate(
        conn,
        rules,
        text,
        cited,
        CORPUS,
        target_company="Fabrikam Payments",
        job_title="Director of Revenue Operations",
    )


@pytest.mark.parametrize("claim", BLIND_SPOT)
def test_the_claim_judge_fails_a_blind_spot_claim(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, claim: str
) -> None:
    fake = FakeLLM({"claim_judge": [flagging_judge(*BLIND_SPOT)]})
    monkeypatch.setattr(llm, "call_json", fake.call_json)
    monkeypatch.setattr(llm, "forget", fake.forget)
    result = _judge_gate(conn, f"{GOOD_LETTER} {claim}", GOOD_CITED)
    assert not result.ok
    assert result.violations == [
        {
            "kind": "claim_judge_unsupported",
            "detail": f"{claim.rstrip('.')} — no cited source states it",
            "sentence": claim,
        }
    ]
    # the same judge passes the honest letter: it is not failing everything
    assert _judge_gate(conn, GOOD_LETTER, GOOD_CITED).ok
    assert len(fake.prompts["claim_judge"]) == 2


@pytest.mark.parametrize("claim", BLIND_SPOT)
def test_tailor_never_ships_a_letter_the_judge_fails(
    conn: sqlite3.Connection, tailor_env: Callable[..., tuple[FakeLLM, int]], claim: str
) -> None:
    # every draft carries the claim; the rules and the critic pass all of them
    fake, jid = tailor_env(
        [{"letter": f"{GOOD_LETTER} {claim}", "bullets_used": GOOD_CITED}],
        [_selection(["Salesforce", "MEDDIC"])],
    )
    fake.replies["claim_judge"] = [flagging_judge(claim)]
    paths = tailor.tailor_job(conn, jid)
    out = Path(paths["resume"]).parent
    # exactly like a rule-based failure: nothing rendered, the draft kept for review
    assert paths["cover"] == "" and paths["cover_rejected"]
    assert not (out / "cover.txt").exists() and not (out / "cover.pdf").exists()
    rejected = (out / "cover_rejected.md").read_text()
    assert "claim_judge_unsupported" in rejected and "no cited source states it" in rejected
    assert f'in: "{claim}"' in rejected and "NOT FOR SENDING" in rejected
    prov = json.loads(Path(paths["provenance"]).read_text())
    assert prov["cover_fact_check"]["ok"] is False
    assert _kinds(prov["cover_fact_check"]["violations"]) == {"claim_judge_unsupported"}
    assert prov["claim_judge_prompt"] == claim_judge.version()
    # every draft judged; the regenerations carry the judge's reason
    n = DEFAULT_COVER_ATTEMPTS
    assert len(fake.prompts["cover"]) == n and len(fake.prompts["claim_judge"]) == n
    assert fake.forgotten.count("cover") == 1
    for regen in fake.prompts["cover"][1:]:
        assert "Draft 1 FAILED THE FACT CHECK" in regen
        assert "claim_judge_unsupported" in regen
    # the same violation in every draft is listed once, with the drafts it failed in
    last = fake.prompts["cover"][-1]
    assert last.count("1. claim_judge_unsupported") == 1
    assert f"failed in drafts: {', '.join(str(d) for d in range(1, n))}" in last
    # a letter that fails the judge never reaches the critic
    assert all("resume bullets" in p for p in fake.prompts["critic"])
    app = conn.execute("SELECT stage, cover_path FROM applications WHERE job_id=?", (jid,))
    assert tuple(app.fetchone()) == ("approved", None)
    ev = conn.execute(
        "SELECT detail FROM events WHERE job_id=? AND kind='cover_provenance_failed'", (jid,)
    ).fetchone()
    assert ev is not None and "claim_judge_unsupported" in ev[0]
    assert f":cj{claim_judge.version()}:" in json.loads(ev[0])["attempt"]


def test_tailor_ships_the_regeneration_the_judge_passes(
    conn: sqlite3.Connection, tailor_env: Callable[..., tuple[FakeLLM, int]]
) -> None:
    claim = "I also led the sales development team."
    fake, jid = tailor_env(
        [
            {"letter": f"{GOOD_LETTER} {claim}", "bullets_used": GOOD_CITED},
            {"letter": GOOD_LETTER, "bullets_used": GOOD_CITED},
        ],
        [_selection(["Salesforce", "MEDDIC"])],
    )
    fake.replies["claim_judge"] = [flagging_judge(claim)]
    paths = tailor.tailor_job(conn, jid)
    assert paths["cover"] and "cover_rejected" not in paths
    assert (Path(paths["resume"]).parent / "cover.txt").read_text() == GOOD_LETTER
    assert len(fake.prompts["cover"]) == 2 and len(fake.prompts["claim_judge"]) == 2
    assert claim in fake.prompts["cover"][1]  # the judge's finding is the feedback
    app = conn.execute("SELECT stage FROM applications WHERE job_id=?", (jid,)).fetchone()
    assert app[0] == "tailored"


@pytest.mark.parametrize("claim", BLIND_SPOT)
def test_draft_novel_answer_never_returns_an_answer_the_judge_fails(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, claim: str
) -> None:
    fake = FakeLLM(
        {
            "novel": [{"answer": f"{PARTNERS} {claim}", "bullets_used": ["b_052"]}],
            "claim_judge": [flagging_judge(claim)],
        }
    )
    monkeypatch.setattr(llm, "call_json", fake.call_json)
    monkeypatch.setattr(llm, "forget", fake.forget)
    question = "Tell us about a time you stepped up"
    answer, reason, violations = form_spec.draft_novel_answer(
        conn,
        prompt=llm.PromptFile("novel_question"),
        corpus=CORPUS,
        profile=drafting_profile(CORPUS),
        job={
            "id": 1,
            "title": "Director of Revenue Operations",
            "company_name": "Fabrikam Payments",
            "jd_html": f"<p>{FABRIKAM_JD}</p>",
        },
        label=question,
        question=question,
        known_companies=KNOWN,
    )
    assert answer is None and reason == "unsourced-claim"
    assert _kinds(violations) == {"claim_judge_unsupported"}
    assert violations[0]["sentence"] == claim
    # one regeneration, carrying the judge's finding as fact-check feedback
    assert len(fake.prompts["novel"]) == 2 and len(fake.prompts["claim_judge"]) == 2
    assert "YOUR PREVIOUS DRAFT FAILED THE FACT CHECK" in fake.prompts["novel"][1]
    assert "claim_judge_unsupported" in fake.prompts["novel"][1]
    # the judge read the answer with its question
    judge_input = fake.prompts["claim_judge"][0].split("\nFORM QUESTION (", 1)[1]
    assert question in judge_input.split("\nCITED SOURCES", 1)[0]
    assert fake.forgotten.count("novel") == 1


# ---- a judge "pass" can never rescue a rule-based failure ----


def test_a_judge_pass_cannot_rescue_a_rule_failure(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLLM({"claim_judge": [JUDGE_PASS]})
    monkeypatch.setattr(llm, "call_json", fake.call_json)
    monkeypatch.setattr(llm, "forget", fake.forget)
    for text, cited in (
        (LUMON_LETTER, ["b_047"]),
        (CROSS_ROLE_LETTER, ["b_047", "b_049"]),
        (VP_MENTION_ANSWER, ["b_052"]),
    ):
        rules = validate_claims(
            text, cited, CORPUS, target_company="Fabrikam Payments", known_companies=KNOWN
        )
        assert not rules.ok, text
        gated = claim_judge.gate(
            conn, rules, text, cited, CORPUS, target_company="Fabrikam Payments"
        )
        assert gated is rules and not gated.ok, text  # returned unchanged
    assert "claim_judge" not in fake.prompts  # the judge was never even asked


def test_tailor_judge_pass_cannot_rescue_a_rule_failure(
    conn: sqlite3.Connection, tailor_env: Callable[..., tuple[FakeLLM, int]]
) -> None:
    fake, jid = tailor_env(
        [{"letter": LUMON_LETTER, "bullets_used": ["b_047"]}],
        [_selection(["Salesforce", "MEDDIC"])],
    )
    fake.replies["claim_judge"] = [JUDGE_PASS]
    paths = tailor.tailor_job(conn, jid)
    assert paths["cover"] == "" and paths["cover_rejected"]
    rejected = (Path(paths["resume"]).parent / "cover_rejected.md").read_text()
    assert "wrong_company" in rejected and "claim_judge" not in rejected
    assert "claim_judge" not in fake.prompts
    assert len(fake.prompts["cover"]) == DEFAULT_COVER_ATTEMPTS


def test_draft_novel_answer_judge_pass_cannot_rescue_a_rule_failure(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLLM(
        {
            "novel": [{"answer": VP_MENTION_ANSWER, "bullets_used": ["b_052"]}],
            "claim_judge": [JUDGE_PASS],
        }
    )
    monkeypatch.setattr(llm, "call_json", fake.call_json)
    monkeypatch.setattr(llm, "forget", fake.forget)
    answer, reason, violations = form_spec.draft_novel_answer(
        conn,
        prompt=llm.PromptFile("novel_question"),
        corpus=CORPUS,
        profile=drafting_profile(CORPUS),
        job={"id": 1, "title": "Senior Account Executive", "company_name": "Hooli"},
        label="Tell us about a time you stepped up",
        question="Tell us about a time you stepped up",
        known_companies=KNOWN,
    )
    assert answer is None and reason == "unsourced-claim" and violations
    assert not any(v["kind"].startswith("claim_judge") for v in violations)
    assert "claim_judge" not in fake.prompts


# ---- a judge reply that is not a verdict fails closed ----

JUDGE_GARBAGE: list[Any] = [
    {"verdict": "maybe", "unsupported": []},
    {"verdict": "pass"},  # no "unsupported" list
    {"unsupported": [], "verdict": None},
    {"unsupported": "none", "verdict": "pass"},
    {"result": "looks fine"},
]


@pytest.mark.parametrize("reply", JUDGE_GARBAGE)
def test_tailor_unparseable_judge_output_fails_closed(
    conn: sqlite3.Connection, tailor_env: Callable[..., tuple[FakeLLM, int]], reply: Any
) -> None:
    fake, jid = tailor_env(
        [{"letter": GOOD_LETTER, "bullets_used": GOOD_CITED}],
        [_selection(["Salesforce", "MEDDIC"])],
    )
    fake.replies["claim_judge"] = [reply]
    paths = tailor.tailor_job(conn, jid)
    assert paths["cover"] == "" and paths["cover_rejected"]
    rejected = (Path(paths["resume"]).parent / "cover_rejected.md").read_text()
    assert "claim_judge_unparseable" in rejected and "failed closed" in rejected
    assert len(fake.prompts["claim_judge"]) == DEFAULT_COVER_ATTEMPTS
    # a bad reply is never served again
    assert fake.forgotten.count("claim_judge") == DEFAULT_COVER_ATTEMPTS
    app = conn.execute("SELECT stage FROM applications WHERE job_id=?", (jid,)).fetchone()
    assert app[0] == "approved"


def test_draft_novel_answer_unparseable_judge_output_fails_closed(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLLM({"novel": [{"answer": PARTNERS, "bullets_used": ["b_052"]}]})
    monkeypatch.setattr(llm, "forget", fake.forget)

    def call_json(c: sqlite3.Connection, stage: str, prompt: str, **kw: Any) -> dict[str, Any]:
        if stage == "claim_judge":  # what llm.call_json raises when no JSON comes back
            fake.prompts.setdefault(stage, []).append(prompt)
            raise ValueError("no JSON object in response: Looks supported to me.")
        return fake.call_json(c, stage, prompt, **kw)

    monkeypatch.setattr(llm, "call_json", call_json)
    answer, reason, violations = form_spec.draft_novel_answer(
        conn,
        prompt=llm.PromptFile("novel_question"),
        corpus=CORPUS,
        profile=drafting_profile(CORPUS),
        job={
            "id": 1,
            "title": "Director of Revenue Operations",
            "company_name": "Fabrikam Payments",
        },
        label="Tell us about a time you stepped up",
        question="Tell us about a time you stepped up",
        known_companies=KNOWN,
    )
    assert answer is None and reason == "unsourced-claim"
    assert _kinds(violations) == {"claim_judge_unparseable"}
    assert len(fake.prompts["claim_judge"]) == 2
    assert fake.forgotten.count("claim_judge") == 2


@pytest.mark.parametrize("claim", EXPERIENCE_CLAIMS[:5])
def test_tailor_never_ships_a_letter_with_an_experience_claim(
    conn: sqlite3.Connection, tailor_env: Callable[..., tuple[FakeLLM, int]], claim: str
) -> None:
    # every draft carries the claim; the critic passes all of them
    _fake, jid = tailor_env(
        [{"letter": f"{GOOD_LETTER} {claim}", "bullets_used": GOOD_CITED}],
        [_selection(["Salesforce", "MEDDIC"])],
    )
    paths = tailor.tailor_job(conn, jid)
    assert paths["cover"] == "" and paths["cover_rejected"]
    assert not (Path(paths["resume"]).parent / "cover.txt").exists()
    prov = json.loads(Path(paths["provenance"]).read_text())
    assert prov["cover_fact_check"]["ok"] is False
    assert "unsourced_experience_claim" in _kinds(prov["cover_fact_check"]["violations"])


@pytest.mark.parametrize(
    "text",
    [
        f"{PARTNERS} I would bring years of field service operations leadership.",
        "Yes. I can bring the trade credentials.",
        f"{PARTNERS} I have deep expertise in equipment financing.",
        f"{PARTNERS} I am a licensed electrician.",
    ],
)
def test_draft_novel_answer_never_returns_an_experience_claim(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    fake = FakeLLM({"novel": [{"answer": text, "bullets_used": ["b_052"]}]})
    monkeypatch.setattr(llm, "call_json", fake.call_json)
    monkeypatch.setattr(llm, "forget", fake.forget)
    answer, reason, violations = form_spec.draft_novel_answer(
        conn,
        prompt=llm.PromptFile("novel_question"),
        corpus=CORPUS,
        profile=drafting_profile(CORPUS),
        job={
            "id": 1,
            "title": "Director of Revenue Operations",
            "company_name": "Fabrikam Payments",
            "jd_html": f"<p>{FABRIKAM_JD}</p>",
        },
        label="Tell us about a time you stepped up",
        question="Tell us about a time you stepped up",
        known_companies=KNOWN,
    )
    assert answer is None and reason == "unsourced-claim"
    assert "unsourced_experience_claim" in _kinds(violations)
    # one regeneration, carrying the fact check's feedback
    assert len(fake.prompts["novel"]) == 2
    assert "YOUR PREVIOUS DRAFT FAILED THE FACT CHECK" in fake.prompts["novel"][1]


# ---- a sales tool or method is named only as a skill_index key (review
# 2026-10-09, skill-answers). The question loop writes bullets in the human's
# own words, third parties included ("the team used Clari"); a cited bullet
# naming a tool sourced a first-person claim of using it ----

TOOL_CORPUS: dict[str, Any] = copy.deepcopy(CORPUS)
TOOL_CORPUS["bullets"] += [
    {
        "id": "b_mgr",
        "role_id": "role_haulmark",
        "metric_confidence": "none",
        "evidence": "me",
        "text_long": "My manager ran Clari forecast calls at Haulmark",
        "text_short": "My manager ran Clari forecast calls at Haulmark",
    },
    {
        "id": "b_carriers",
        "role_id": "role_haulmark",
        "metric_confidence": "verified",
        "evidence": "me",
        "text_long": "Signed 12 carriers at Haulmark; the team used Clari for the forecast",
        "text_short": "Signed 12 carriers at Haulmark; the team used Clari for the forecast",
    },
]

# (text, cited): every one passed before; each names a tool no skill_index
# key is
UNSOURCED_TOOL_CLAIMS = [
    # the reviewer's repros
    ("At Haulmark I ran Clari forecast calls every week.", ["b_mgr"]),
    ("I used Clari for my forecast at Haulmark, signing 12 carriers.", ["b_carriers"]),
    # lowercase, possessive, someone else's sentence, the human's own wording
    ("At Haulmark I ran clari forecast calls.", ["b_mgr"]),
    ("Clari forecast calls were my weekly routine at Haulmark.", ["b_mgr"]),
    ("My manager ran Clari forecast calls at Haulmark, so I know the tool.", ["b_mgr"]),
    ("The team used Clari for the forecast at Haulmark.", ["b_carriers"]),
    # a resume bullet naming a tool sources it no more than an answer's does:
    # Looker is no skill_index key (b_046 says the candidate rebuilt reports in it)
    ("At ledgerline I rebuilt the sales reports in Looker.", ["b_046"]),
]


def _tool_check(
    text: str, cited: list[str], corpus: dict[str, Any] | None = None, **kw: Any
) -> ProvenanceResult:
    kw.setdefault("target_company", "Hooli")
    kw.setdefault("job_title", "Senior Account Executive")
    kw.setdefault("known_companies", KNOWN)
    return validate_claims(text, cited, TOOL_CORPUS if corpus is None else corpus, **kw)


@pytest.mark.parametrize(("text", "cited"), UNSOURCED_TOOL_CLAIMS)
def test_a_tool_on_a_cited_bullet_is_not_a_source(text: str, cited: list[str]) -> None:
    out = _tool_check(text, cited)
    assert not out.ok, text
    tool = [v for v in out.violations if v["kind"] == "unsourced_tool"]
    assert tool and "is not in the skill index" in tool[0]["detail"], out.violations
    assert not has_hard(out)  # a rewording: the letter is regenerated without it


def test_a_tool_the_skill_index_has_is_fine() -> None:
    """The remedy is the human's: a skill_index key (from the tool's own
    question, or added by hand). An alias names the same tool."""
    indexed = copy.deepcopy(TOOL_CORPUS)
    indexed["skill_index"]["Clari"] = "I used Clari weekly at Haulmark"
    out = _tool_check("At Haulmark I ran Clari forecast calls every week.", ["b_mgr"], indexed)
    assert out.ok, out.violations
    for text in (
        "At Haulmark I lived in Salesforce and Gong every day.",
        "I coach deals on MEDDIC at Haulmark.",
    ):
        out = _tool_check(text, ["b_052"])
        assert out.ok, (text, out.violations)
    # an alias is the indexed tool here (the proper-noun check still wants
    # its spelling in a source)
    out = _tool_check("At Haulmark I lived in SFDC every day.", ["b_052"])
    assert "unsourced_tool" not in _kinds(out.violations), out.violations
    # a key that only contains the name is not the tool
    partial = copy.deepcopy(TOOL_CORPUS)
    partial["skill_index"]["Clari dashboards (read-only)"] = "seen"
    out = _tool_check("At Haulmark I ran Clari forecast calls every week.", ["b_mgr"], partial)
    assert "unsourced_tool" in _kinds(out.violations)


@pytest.mark.parametrize(
    ("text", "kw"),
    [
        # the posting's own words about the target
        (
            "Fabrikam Payments runs its forecast on Clari.",
            {
                "target_company": "Fabrikam Payments",
                "jd_text": "Fabrikam Payments runs its forecast on Clari.",
            },
        ),
        # the target is the tool's vendor, named as a tool the candidate used
        (
            "At Haulmark I ran Clari forecast calls every week.",
            {"target_company": "Clari", "job_title": "Account Executive"},
        ),
    ],
)
def test_a_tool_outside_the_skill_index_is_an_accepted_false_rejection(
    text: str, kw: dict[str, str]
) -> None:
    """Who used the tool is never judged: a sentence about the target, or the
    target's own name used as a tool, fails all the same. A false rejection
    costs a sentence (or the letter, which the human then writes); a false
    acceptance puts a tool the candidate never used in a letter."""
    out = _tool_check(text, [], **kw)
    assert "unsourced_tool" in _kinds(out.violations), out.violations


def test_the_tools_vendor_named_as_the_company_applied_to_passes() -> None:
    """2026-10-15: the target that IS the tool may be named as the company
    applied to; it was an accepted false rejection before
    (tests/test_claims_false_positives_1015.py has the near-misses)."""
    out = _tool_check("I'd love to join Clari.", [], target_company="Clari", job_title="AE")
    assert "unsourced_tool" not in _kinds(out.violations), out.violations


def test_draft_novel_answer_never_claims_a_tool_from_a_third_party_bullet(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = "Yes. At Haulmark I ran Clari forecast calls every week."
    fake = FakeLLM({"novel": [{"answer": text, "bullets_used": ["b_mgr"]}]})
    monkeypatch.setattr(llm, "call_json", fake.call_json)
    monkeypatch.setattr(llm, "forget", fake.forget)
    answer, reason, violations = form_spec.draft_novel_answer(
        conn,
        prompt=llm.PromptFile("novel_question"),
        corpus=TOOL_CORPUS,
        profile=drafting_profile(TOOL_CORPUS),
        job={
            "id": 1,
            "title": "Account Executive",
            "company_name": "Hooli",
            "jd_html": "<p>Experience with Clari is a plus.</p>",
        },
        label="Which forecasting tools have you used?",
        question="Which forecasting tools have you used?",
        known_companies=KNOWN,
    )
    assert answer is None and reason == "unsourced-claim"
    assert "unsourced_tool" in _kinds(violations)


# ---------------------------------------------------------------------------
# review 2026-10-09 (Hard Rule 8): the target named as the candidate's
# employer. The fabricated-employer check excused the target like one of the
# candidate's own employers, so each of these passed. The target may be named
# only as where the candidate wants to work.
# ---------------------------------------------------------------------------

TARGET_AS_EMPLOYER = [
    # the review's repros
    "I previously worked at Contoso.",
    "Yes, I was at Contoso before.",
    "I've been with Contoso.",
    "I have worked for Contoso.",
    "I've been employed by Contoso.",
    # every cue the review names: joined, left, my time at, previously /
    # formerly at, alum / alumni, ex-
    "I joined Contoso after Keyward Security.",
    "I left Contoso for Haulmark.",
    "My time at Contoso taught me how to coach.",
    "Previously at Contoso.",
    "Formerly at Contoso.",
    "I'm a Contoso alum.",
    "Contoso alumni like me know the product.",
    "I'm an alum of Contoso.",
    "As an ex-Contoso rep, I know the product.",
    "I'm a former Contoso AE.",
    # the same claim in other words
    "I spent my early career at Contoso.",
    "I was on Contoso's sales team.",
    "I was part of Contoso's sales team.",
    "When I was with Contoso, I coached reps.",
    "While at Contoso, I coached reps.",
    "At Contoso I ran the SDR team.",
    "At Contoso, I was the top rep.",
    "I work at Contoso.",
    "I'm currently at Contoso.",
    "I am still at Contoso.",
    "I interned at Contoso.",
    "I sold for Contoso.",
    "I represented Contoso at trade shows.",
    "I worked with Contoso.",
    "Having worked at Contoso, I know the product.",
    "Working at Contoso taught me how to coach.",
    "Working at Contoso to grow the SDR team.",
    "Currently working at Contoso.",
    "I'm proud to have worked at Contoso.",
    "I loved my years at Contoso.",
    "My Contoso team coached every rep.",
    "I was a Contoso employee.",
    "Contoso was my first employer.",
    "Contoso is my employer.",
    "Contoso hired me.",
    "Contoso is where I started my career.",
    "Contoso, where I worked, is growing.",
    "I held the role at Contoso.",
    "I was on the team at Contoso.",
    "I'm excited about the role at Contoso, which I held before.",
    "Account Executive at Contoso, then Regional Sales Manager at Haulmark.",
    "Previously Account Executive at Contoso.",
    # an aspiration that says the candidate was there
    "I'd love to rejoin Contoso.",
    "I'd love to return to Contoso.",
    "I'd love to come back to Contoso.",
    "I'd love to be back at Contoso.",
    "I'd love to join Contoso again.",
    # second review 2026-10-09: each of these passed. A seat cue earlier in a
    # list that runs on to the name
    "I worked at Haulmark, then Contoso.",
    "I worked at Haulmark and later Contoso.",
    "I worked at Haulmark and also Contoso.",
    "I worked at Haulmark as well as Contoso.",
    "I worked at either Haulmark or Contoso.",
    "I've worked at both Haulmark and Contoso.",
    "I've worked at both Contoso and Haulmark.",
    "I've worked at companies like Haulmark and Contoso.",
    "I've worked at places like Contoso.",
    "Previously worked at Haulmark and Contoso.",
    "Prior experience includes Contoso.",
    # staying, continuing, coming home
    "I'd love to continue at Contoso.",
    "I'd love to stay at Contoso.",
    "I'd love to remain at Contoso.",
    "I'd love to keep working at Contoso.",
    "I'd love to continue working at Contoso.",
    "I'd love to come home to Contoso.",
    "I'd love to be a Contoso rep one more time.",
    # the target as the subject, the object or a modifier of the candidate's
    # history
    "Contoso gave me my first sales job.",
    "Contoso made me a manager.",
    "Contoso taught me enterprise sales.",
    "Contoso's reps were my peers.",
    "Contoso and I go way back.",
    "Ask anyone at Contoso about me.",
    "I led Contoso's enterprise team.",
    "I built Contoso's enterprise team.",
    "I managed the Contoso enterprise team.",
    "I helped Contoso grow its venue network.",
    "I came from Contoso.",
    "I came over from Contoso.",
    "I've sold Contoso to concert halls.",
    "At Contoso: I led the SDR team.",
    # a seat at the target
    "I was Contoso's first AE.",
    "I was an early Contoso AE.",
    "I was an early Contoso hire.",
    "I was Contoso's Inside Sales Manager.",
    "I served as Contoso's Inside Sales Manager.",
    # labels and résumé lines
    "My current employer: Contoso.",
    "Current employer: Contoso.",
    "Background: Contoso.",
    "Account Executive, Contoso",
    "Contoso | Account Executive",
    "Contoso, 2019–2021.",
    # a comparison that puts the candidate there
    "Contoso is better than anywhere else I've worked.",
    "Contoso is a better place than anywhere I've worked since.",
    "Contoso is bigger than when I joined.",
    "Contoso is where I started my career.",
    # third review 2026-10-09: each passed through the describes-target and
    # forward exemptions. A "my" on the candidate's history, "me" as an
    # object after the target or "your", and re-contact are history claims
    "Contoso is on my resume.",
    "You'll see Contoso on my resume.",
    "You will see Contoso on my resume.",
    "You'll spot Contoso on my resume.",
    "Contoso will remember me.",
    "Your team will remember me from Contoso.",
    "I'd love to reconnect with Contoso.",
    # …and the job sought or the target's people after a verb that puts the
    # candidate in the seat or under its people ("for", "to", "into" govern
    # the job sought only after a word of wanting or applying)
    "I was promoted to the role at Contoso.",
    "I moved into the role at Contoso.",
    "I stepped into the role at Contoso.",
    "I reported to the team at Contoso.",
    "I sold to the team at Contoso.",
    "Thanks for the role at Contoso; I loved it.",
    # a past "showed me" or "told me" is no reaction, and a word of tenure
    # makes a present reaction an old one
    "I'd love to meet the team at Contoso who showed me the ropes.",
    "Contoso still matters to me.",
]


@pytest.mark.parametrize("claim", TARGET_AS_EMPLOYER)
def test_the_target_is_never_named_as_the_candidates_employer(claim: str) -> None:
    for text, cited in (
        (claim, []),
        (f"At Haulmark I led 9 AEs. {claim}", ["b_052"]),
        (f"Yes. {claim}", ["b_052"]),
    ):
        out = validate_claims(text, cited, CORPUS, **CONTOSO)
        assert has_hard(out), (text, out.violations)
        hits = [v for v in out.violations if v["kind"] == "fabricated_employer"]
        assert hits and "Contoso is the company applied to" in hits[0]["detail"], out.violations


@pytest.mark.parametrize(
    "claim",
    [
        "I was at Fabrikam Payments before.",
        "Yes, I was at Fabrikam before.",
        "I've been with Fabrikam Payments.",
        # second review 2026-10-09
        "I led Fabrikam's sales team.",
        "I was at fabrikam payments before.",
        "I was on your sales team.",
    ],
)
def test_the_target_as_employer_fails_in_a_letter_and_an_answer(claim: str) -> None:
    for text, cited in (
        (f"{GOOD_LETTER} {claim}", GOOD_CITED),
        (GOOD_LETTER.replace("Since March 2024", f"{claim} Since March 2024"), GOOD_CITED),
        (f"{PARTNERS} {claim}", ["b_052"]),
    ):
        v = _check(text, cited)
        assert "fabricated_employer" in _kinds(v), (text, v)


# the target named as where the candidate wants to work, or as the job sought
TARGET_ASPIRED = [
    "I'd love to join Contoso.",
    "I'm excited to join Contoso.",
    "I hope to join Contoso.",
    "My goal is to join Contoso.",
    "I want to work at Contoso.",
    "I'd welcome the chance to work for Contoso.",
    "Joining Contoso would be a great next step.",
    "Working at Contoso would let me coach every day.",
    "I'd like to join the team at Contoso.",
    "I'd love to meet the team at Contoso.",
    "At Contoso, I'd focus on pipeline.",
    "I'm excited about the role at Contoso.",
    "I'm interested in the role at Contoso.",
    "I'm applying for the Inside Sales Manager role at Contoso.",
    "I saw the Inside Sales Manager role at Contoso last week.",
    "I was excited to see the role at Contoso.",
    "Thank you for considering me for the role at Contoso.",
    "This role at Contoso is the next step for me.",
    "Contoso is where I want to build the next chapter.",
    "Contoso is my top choice.",
    "I'd be a great Contoso rep.",
    "I look forward to hearing from Contoso.",
    # second review 2026-10-09: what still passes under the fail-closed rule
    "Contoso's mission matters to me.",
    "Contoso's posting caught my eye.",
    "Contoso is solving a problem I care about.",
    "Contoso's mission is one I share.",
    "I saw the Contoso posting last week.",
    "I haven't run enablement inside a stack like Contoso.",
    "I haven't run an org Contoso's size.",
    "I'd love to join contoso.",
    "I'm excited about the role at contoso.",
]


@pytest.mark.parametrize("text", TARGET_ASPIRED)
def test_the_target_may_be_named_as_where_the_candidate_wants_to_work(text: str) -> None:
    assert _check(text, [], **CONTOSO) == [], text
    v = _check(f"At Haulmark I led 9 AEs. {text}", ["b_052"], **CONTOSO)
    assert v == [], v
    # the job sought, in a sentence with a past claim
    ok = "At Haulmark I led 9 AEs, which prepared me for the role at Contoso."
    assert _check(ok, ["b_052"], **CONTOSO) == []


def test_target_as_employer_accepted_false_rejections() -> None:
    """Fail closed: a past tense anywhere in the sentence, or a "my" that is
    not a plan, keeps the target from passing as an aspiration, and a
    negation excuses no cue ("I never left Contoso" says the candidate is
    still there): the human answers "Have you worked for us before?"."""
    for text, cited in (
        ("I led 9 AEs at Haulmark, and I'd love to join Contoso.", ["b_052"]),
        ("I'd love to build my career at Contoso.", []),
        ("The team at Contoso impressed me.", []),
        ("No, I have never worked at Contoso.", []),
        ("I never left Contoso.", []),
        # second review 2026-10-09: any other mention of the target in a
        # first-person sentence that is not only an aspiration fails closed
        ("What drew me to Contoso is its mission.", []),
        ("I interviewed at Contoso last year.", []),
        ("I'd love to stay in touch with Contoso.", []),
        ("I was a fan of Contoso's reps.", []),
        # third review 2026-10-09: "you"/"your" fail closed in a first-person
        # sentence that does more than aspire, so a cited fact and a comment
        # on the company go in two sentences; a "my" beyond a plan is a claim
        # of history beside the target; a past act on the job sought is one
        (
            "At Haulmark I built the onboarding program that cut time to first "
            "dispatch by ~40%, which is what your team needs.",
            ["b_044"],
        ),
        ("Your posting tells me you need a builder.", []),
        ("Contoso would be the next step in my career.", []),
        ("I applied for the role at Contoso.", []),
    ):
        assert "fabricated_employer" in _kinds(_check(text, cited, **CONTOSO)), text
    # …and split in two, the same fact and comment pass
    split = (
        "At Haulmark I built the onboarding program "
        "that cut time to first dispatch by ~40%. Your team needs that."
    )
    assert _check(split, ["b_044"], **CONTOSO) == []


def test_the_target_may_be_named_as_an_employer_the_corpus_lists() -> None:
    """Applying back to one of the candidate's own employers, or to a client a
    cited bullet names, is no fabrication."""
    haulmark = {"target_company": "Haulmark", "job_title": "General Manager", "jd_text": ""}
    for text in (
        "I previously worked at Haulmark, where I led 9 AEs.",
        "I'd love to rejoin Haulmark.",
        "At Haulmark I led 9 AEs.",
    ):
        assert "fabricated_employer" not in _kinds(_check(text, ["b_052"], **haulmark)), text
    nakatomi = {"target_company": "Nakatomi", "job_title": "Account Executive", "jd_text": ""}
    text = "At Keyward Security I signed accounts at Nakatomi, Ashgrove and Pemberton Mills."
    assert _check(text, ["b_041"], **nakatomi) == []
    # …but a client is not where the candidate worked
    v = _check("I worked at Nakatomi.", ["b_041"], **nakatomi)
    assert "fabricated_employer" in _kinds(v), v
    # …and a skill_index tool a cited bullet names is never a client
    salesforce = {"target_company": "Salesforce", "job_title": "Account Executive", "jd_text": ""}
    v = _check("At ledgerline I ran accounts at Salesforce.", ["b_046"], **salesforce)
    assert "fabricated_employer" in _kinds(v), v


@pytest.mark.parametrize(
    "claim",
    [
        "I was at Fabrikam Payments before.",
        # third review 2026-10-09
        "I reported to your CEO.",
        "Fabrikam Payments is on my resume.",
        "I'd love to reconnect with Fabrikam Payments.",
    ],
)
def test_tailor_never_ships_a_letter_naming_the_target_as_employer(
    conn: sqlite3.Connection, tailor_env: Callable[..., tuple[FakeLLM, int]], claim: str
) -> None:
    _fake, jid = tailor_env(
        [{"letter": f"{GOOD_LETTER} {claim}", "bullets_used": GOOD_CITED}],
        [_selection(["Salesforce", "MEDDIC"])],
    )
    paths = tailor.tailor_job(conn, jid)
    assert paths["cover"] == "" and paths["cover_rejected"]
    prov = json.loads(Path(paths["provenance"]).read_text())
    assert "fabricated_employer" in _kinds(prov["cover_fact_check"]["violations"])


@pytest.mark.parametrize(
    "text",
    [
        "Yes, I was at Fabrikam Payments before.",
        f"{PARTNERS} I previously worked at Fabrikam Payments.",
        "Yes. I've been employed by Fabrikam Payments.",
    ],
)
def test_draft_novel_answer_never_names_the_target_as_employer(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    fake = FakeLLM({"novel": [{"answer": text, "bullets_used": ["b_052"]}]})
    monkeypatch.setattr(llm, "call_json", fake.call_json)
    monkeypatch.setattr(llm, "forget", fake.forget)
    answer, reason, violations = form_spec.draft_novel_answer(
        conn,
        prompt=llm.PromptFile("novel_question"),
        corpus=CORPUS,
        profile=drafting_profile(CORPUS),
        job={
            "id": 1,
            "title": "Director of Revenue Operations",
            "company_name": "Fabrikam Payments",
            "jd_html": f"<p>{FABRIKAM_JD}</p>",
        },
        label="Have you worked with us before?",
        question="Have you worked with us before?",
        known_companies=KNOWN,
    )
    assert answer is None
    assert "fabricated_employer" in _kinds(violations), violations


@pytest.mark.parametrize(
    "text",
    [
        f"{PARTNERS} I reported to your CEO.",
        f"{PARTNERS} I have run your weekly revenue meeting.",
        "Fabrikam Payments is on my resume.",
        "I'd love to reconnect with Fabrikam Payments.",
        "Yes. Your team will remember me from Haulmark.",
    ],
)
def test_draft_novel_answer_never_puts_the_candidate_at_the_target_in_other_words(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    """Third review 2026-10-09: "you"/"your", a "my" on the candidate's
    history and re-contact, asked a question that is not about tenure (so
    asks_target_tenure does not decide it)."""
    fake = FakeLLM({"novel": [{"answer": text, "bullets_used": ["b_052"]}]})
    monkeypatch.setattr(llm, "call_json", fake.call_json)
    monkeypatch.setattr(llm, "forget", fake.forget)
    answer, reason, violations = form_spec.draft_novel_answer(
        conn,
        prompt=llm.PromptFile("novel_question"),
        corpus=CORPUS,
        profile=drafting_profile(CORPUS),
        job={
            "id": 1,
            "title": "Director of Revenue Operations",
            "company_name": "Fabrikam Payments",
            "jd_html": f"<p>{FABRIKAM_JD}</p>",
        },
        label="Why Fabrikam Payments?",
        question="Why Fabrikam Payments?",
        known_companies=KNOWN,
    )
    assert answer is None and reason == "unsourced-claim"
    assert "fabricated_employer" in _kinds(violations), violations


# ---------------------------------------------------------------------------
# review 2026-10-09 (Hard Rule 8): a first-person modal or future sentence
# that asserts what the candidate has is a candidate claim, in a letter and in
# a drafted answer: its content words need the cited bullets, and it never
# counts as a sentence that claims nothing (so the job's own title is checked
# as a seat in it). Honest forward sentences still pass.
# ---------------------------------------------------------------------------

# second review 2026-10-09: knowledge, background, credentials or experience
# with none of the words above (the Fabrikam case is a novel answer)
NEW_MODAL_CLAIMS = [
    "As a former electrician, I'd understand your customers.",
    "As an electrician myself, I'd understand your customers.",
    "As someone who has run an HVAC business, I'd understand your customers.",
    "I'd be the rare Director of Revenue Operations who has run a plumbing shop.",
    "I'd come in knowing contractor billing cold.",
    "I would already know the billing side.",
    "I'll know the billing side cold.",
    "I'd know exactly how equipment financing works.",
    "I'd thrive leading technicians, as I always have.",
    "I could run your licensing function on day one.",
    "I'd hit the ground running on equipment financing.",
    "Nobody would need to teach me warranty claims management.",
    "I'd understand your customers as a fellow tradesperson.",
    # …and a credential with no first person: a hire the target would make
    "You'd be hiring a licensed electrician.",
    "Fabrikam Payments would get a seasoned field service operations leader.",
    "You'd get a seasoned field service operations leader.",
]

# third review 2026-10-09: possession, familiarity and skill with none of the
# listed verbs or nouns, each passing with zero citations in a letter and in a
# drafted answer (FABRIKAM_JD, cited [] and PARTNERS + b_052)
THIRD_REVIEW_MODAL_CLAIMS = [
    # the review's repros: possession brought along on arrival
    "I'd walk in with the trade chops your team needs.",
    "I'd show up with deep building-code compliance chops.",
    # …familiarity
    "Equipment financing won't be new to me.",
    "Licensing would not be new to me.",
    "None of this would be new to me.",
    "Contractor billing is nothing new for me.",
    "I've seen all of this before.",
    # …a skill
    "I can teach your team equipment financing.",
    "I could teach anyone trade licensing.",
    "I can navigate building-code compliance.",
    "I'd speak the technicians' language.",
    # the same claims in other words
    "I'd come armed with a contractor network.",
    "I'd walk in the door with a supplier network.",
    "Equipment financing isn't new territory for me.",
    "I'm not new to equipment financing.",
    "I've navigated equipment financing before.",
    "I can handle equipment financing.",
    "I could run equipment financing.",
    "I'll train your reps on equipment financing.",
    "I'd be happy to teach your team trade licensing.",
    # an unbounded claim: nothing a cited bullet could source, so it fails
    # whatever is cited ("None of this would be new to me" passed beside
    # b_047's "new-business")
    "I can do this.",
    "I could do that.",
    "I'd bring that.",
]

MODAL_EXPERIENCE_CLAIMS = [
    # the review's repros
    "I would bring years of field service operations leadership to Fabrikam Payments.",
    "I can bring the trade credentials your team needs.",
    "I'd bring deep experience in building-code compliance.",
    # possession in a modal frame without a word of experience
    "I'd have the trade chops your team needs.",
    "I will have the supplier contacts you need.",
    "We'd have the trade depth to scale this.",
    "I'll draw on my supplier network.",
    "I'd love to draw on my supplier network.",
    "I could lean on deep supplier relationships.",
    "I want to leverage my contractor network.",
    "My field service leadership would serve your team well.",
    *NEW_MODAL_CLAIMS,
    *THIRD_REVIEW_MODAL_CLAIMS,
]


@pytest.mark.parametrize("claim", MODAL_EXPERIENCE_CLAIMS)
def test_a_modal_claim_of_what_the_candidate_has_needs_its_cited_bullets(claim: str) -> None:
    assert not _no_candidate_claim(claim, set())
    for text, cited in (
        (claim, []),
        (f"{GOOD_LETTER} {claim}", GOOD_CITED),
        (GOOD_LETTER.replace("Since March 2024", f"{claim} Since March 2024"), GOOD_CITED),
        (f"{PARTNERS} {claim}", ["b_052"]),
        (f"Yes. {claim}", ["b_052"]),
    ):
        assert "unsourced_experience_claim" in _kinds(_check(text, cited)), text


@pytest.mark.parametrize("claim", MODAL_EXPERIENCE_CLAIMS[:3])
def test_draft_novel_answer_never_returns_a_modal_experience_claim(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, claim: str
) -> None:
    fake = FakeLLM({"novel": [{"answer": f"{PARTNERS} {claim}", "bullets_used": ["b_052"]}]})
    monkeypatch.setattr(llm, "call_json", fake.call_json)
    monkeypatch.setattr(llm, "forget", fake.forget)
    answer, reason, violations = form_spec.draft_novel_answer(
        conn,
        prompt=llm.PromptFile("novel_question"),
        corpus=CORPUS,
        profile=drafting_profile(CORPUS),
        job={
            "id": 1,
            "title": "Director of Revenue Operations",
            "company_name": "Fabrikam Payments",
            "jd_html": f"<p>{FABRIKAM_JD}</p>",
        },
        label="What would you bring to this role?",
        question="What would you bring to this role?",
        known_companies=KNOWN,
    )
    assert answer is None and reason == "unsourced-claim"
    assert "unsourced_experience_claim" in _kinds(violations)


def test_a_modal_experience_claim_is_checked_for_the_jobs_title_as_a_seat() -> None:
    """It is a claim about the candidate, so the job's own title in it is a
    seat claimed (claimed_job_title); naming the role sought still passes."""
    kw = {
        "target_company": "Fabrikam Payments",
        "job_title": "Contractor Success Lead",
        "jd_text": "",
    }
    v = _check("I would bring years as a Contractor Success Lead.", ["b_052"], **kw)
    assert {"claimed_job_title", "unsourced_experience_claim"} <= _kinds(v), v
    assert _check("I'd be the Contractor Success Lead your team needs.", [], **kw) == []
    assert _check("I want to be a Contractor Success Lead.", [], **kw) == []


@pytest.mark.parametrize("claim", NEW_MODAL_CLAIMS + THIRD_REVIEW_MODAL_CLAIMS[:11])
def test_draft_novel_answer_never_returns_a_claim_of_knowledge_or_background(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, claim: str
) -> None:
    for text in (claim, f"{PARTNERS} {claim}"):
        fake = FakeLLM({"novel": [{"answer": text, "bullets_used": ["b_052"]}]})
        monkeypatch.setattr(llm, "call_json", fake.call_json)
        monkeypatch.setattr(llm, "forget", fake.forget)
        answer, reason, violations = form_spec.draft_novel_answer(
            conn,
            prompt=llm.PromptFile("novel_question"),
            corpus=CORPUS,
            profile=drafting_profile(CORPUS),
            job={
                "id": 1,
                "title": "Director of Revenue Operations",
                "company_name": "Fabrikam Payments",
                "jd_html": f"<p>{FABRIKAM_JD}</p>",
            },
            label="Why are you a fit for Fabrikam Payments?",
            question="Why are you a fit for Fabrikam Payments?",
            known_companies=KNOWN,
        )
        assert answer is None and reason == "unsourced-claim", text
        assert "unsourced_experience_claim" in _kinds(violations), (text, violations)


@pytest.mark.parametrize(
    "text",
    [
        "I'd welcome the chance to talk.",
        "I'm excited about the role at Fabrikam Payments.",
        "I'd have to learn how contractors buy.",
        "I would want to learn how contractors buy.",
        "I'd love to talk about how I can help.",
        "My plan would be to start with the weekly revenue meeting.",
        # second review 2026-10-09: wanting to know is not knowing, and an
        # idiom after "as a" is no identity
        "I'd love to understand your priorities.",
        "I want to know more about the team.",
        "I'd like to know how you run the weekly revenue meeting.",
        "As a first step, I'd sit in on the weekly revenue meeting.",
        "As a result, I'd start with the weekly revenue meeting.",
        # third review 2026-10-09: a conversation is no skill, meeting is no
        # arrival with anything, an offer of help is no ability, and an
        # honest disclaimer is no familiarity
        "I'd love to speak with you about the role.",
        "I'd love to come in and meet with your team.",
        "I'd love to talk about how I can help.",
        "Construction is new to me, and I'd learn it fast.",
        "I haven't seen a contractor's billing before, and I'd learn it fast.",
    ],
)
def test_an_honest_forward_sentence_still_passes(text: str) -> None:
    assert _check(text, []) == [], text
    assert _check(f"{GOOD_LETTER} {text}", GOOD_CITED) == [], text


# ---------------------------------------------------------------------------
# second review 2026-10-09 (Hard Rule 8): the target as the candidate's
# employer, in lowercase, in the second person, and as the answer to a form
# question about working there. Each of these passed with no violations.
# ---------------------------------------------------------------------------

# (target, sentence): a lowercase spelling is the target too
LOWERCASE_TARGET_AS_EMPLOYER = [
    ("monday.com", "I previously worked at monday.com."),
    ("monday.com", "I was at monday.com before."),
    ("hims & hers", "I previously worked at hims & hers."),
    ("Contoso", "I previously worked at contoso."),
    ("Contoso", "Yes, I was at contoso before."),
    ("Contoso", "At contoso I ran the SDR team."),
    ("Pilot", "I previously worked at pilot."),
    ("Fabrikam Payments", "I was at fabrikam payments before."),
    ("Fabrikam Payments", "I previously worked at fabrikam."),
]


@pytest.mark.parametrize(("target", "claim"), LOWERCASE_TARGET_AS_EMPLOYER)
def test_the_target_in_lowercase_is_still_the_target(target: str, claim: str) -> None:
    kw = {"target_company": target, "job_title": "Account Executive", "jd_text": ""}
    for text, cited in ((claim, []), (f"{PARTNERS} {claim}", ["b_052"])):
        out = validate_claims(text, cited, CORPUS, known_companies=KNOWN, **kw)
        hits = [v for v in out.violations if v["kind"] == "fabricated_employer"]
        assert hits and f"{target} is the company applied to" in hits[0]["detail"], (
            text,
            out.violations,
        )


def test_a_lowercase_ordinary_word_is_judged_only_after_a_seat_cue() -> None:
    """The word "pilot" is in the corpus ("Turned a small pilot into…"): for Pilot it
    is the target only after a seat cue, so the corpus's own sentence passes."""
    pilot = {"target_company": "Pilot", "job_title": "Account Executive", "jd_text": ""}
    text = (
        "At Haulmark I turned a small pilot into a remote onboarding program "
        "that cut time to first dispatch by ~40%."
    )
    assert _check(text, ["b_044"], **pilot) == []
    assert _check("I'd start with a pilot for your reps.", [], **pilot) == []
    assert "fabricated_employer" in _kinds(_check("I previously worked at pilot.", [], **pilot))


# "you"/"your" after a seat cue stand for the target
SECOND_PERSON_SEATS = [
    "I was on your sales team.",
    "I was one of your AEs.",
    "I used to be one of your reps.",
    "I'm one of your current reps.",
    "I currently work on your sales team.",
    "I already work on your team.",
    "I previously sold your platform.",
    "I was your Inside Sales Manager.",
    "I've already been your Inside Sales Manager.",
    "I've worked with you before.",
    "Your company was my home.",
    "Your sales floor is where I learned to sell.",
    "I learned to sell on your floor.",
]


@pytest.mark.parametrize("claim", SECOND_PERSON_SEATS)
@pytest.mark.parametrize("fixture", ["fabrikam", "contoso"])
def test_the_target_in_the_second_person_is_never_the_candidates_employer(
    claim: str, fixture: str
) -> None:
    kw: dict[str, Any] = CONTOSO if fixture == "contoso" else {}
    for text, cited in ((claim, []), (f"{PARTNERS} {claim}", ["b_052"])):
        v = _check(text, cited, **kw)
        assert "fabricated_employer" in _kinds(v), (text, v)


@pytest.mark.parametrize(
    "text",
    [
        "I'd love to be on your team.",
        "I'd love to join your team.",
        "I look forward to working with you.",
        "I'd love to work with you.",
        "Thank you for your time.",
        "I'd love to sell your platform.",
        "I'd be your next Inside Sales Manager.",
        "As your Inside Sales Manager, I'd build the team.",
        # third review 2026-10-09: what a first-person sentence may still
        # name with "you"/"your": thanks, the job sought, the target's people
        "Thank you for considering me.",
        "I hope you'll consider me for the role.",
        "I saw your posting last week.",
        "I read your job description closely.",
        "I'm excited about your Inside Sales Manager role.",
        "For your reps, I'd build a coaching cadence.",
        "Your team needs a coach, and I'd love to be one.",
        "I'd love to speak with you about the role.",
    ],
)
def test_the_second_person_may_still_name_where_the_candidate_wants_to_be(text: str) -> None:
    assert _check(text, [], **CONTOSO) == [], text


# third review 2026-10-09 (Hard Rule 8): "you"/"your" fail closed as the
# target's name does. Each of these passed with no citation and no violation,
# alone and after PARTNERS: a past or present act on the target's own things,
# with no seat cue before "you"/"your" and no place noun after it. Each claims
# tenure at the target ("I reported to Contoso's CEO" already failed)
SECOND_PERSON_TENURE = [
    # the review's repros with CONTOSO
    "I reported to your CEO.",
    "I worked under your CEO.",
    "I carried your quota.",
    "I wrote your sales playbook.",
    "Your sales playbook is the one I wrote.",
    "I hired your current sales leaders.",
    "I used your internal CRM daily.",
    "I miss your sales floor.",
    # …and with Fabrikam Payments and FABRIKAM_JD
    "I used to run your weekly revenue meeting.",
    "I ran your weekly revenue meeting for a year.",
    "I have run your weekly revenue meeting.",
    "Your weekly revenue meeting is one I built.",
    "I set up your licensing process.",
    # the same claim in other words
    "I know your CEO well.",
    "Your CEO knows me.",
    "Your reps still call me.",
    "You may remember me.",
    "Your team would learn from me.",
    "I learned your job inside out.",
    "I saw your sales process up close.",
    "I was promoted to your Inside Sales Manager role.",
    "I reported to your team.",
    # "thank you" is excused only when the rest of its clause claims nothing
    "Thank you for promoting me.",
    "Thank you for hiring me.",
    # a word of tenure makes a present reaction an old one
    "Your sales floor still speaks to me.",
]


@pytest.mark.parametrize("claim", SECOND_PERSON_TENURE)
@pytest.mark.parametrize("fixture", ["fabrikam", "contoso"])
def test_you_and_your_fail_closed_in_a_first_person_sentence(claim: str, fixture: str) -> None:
    kw: dict[str, Any] = CONTOSO if fixture == "contoso" else {}
    target = "Contoso" if fixture == "contoso" else "Fabrikam Payments"
    for text, cited in ((claim, []), (f"{PARTNERS} {claim}", ["b_052"])):
        v = _check(text, cited, **kw)
        hits = [x for x in v if x["kind"] == "fabricated_employer"]
        assert hits and f"{target} is the company applied to" in hits[0]["detail"], (text, v)


def test_the_jobs_title_held_at_the_target_is_a_claimed_seat() -> None:
    """Tied to the target ("your …", "Contoso's …"), the job's title is the
    role sought only in a clause that claims nothing: "I was your Inside
    Sales Manager" claims the seat (claimed_job_title) as well as the
    employer (fabricated_employer)."""
    corpus = copy.deepcopy(CORPUS)  # no "Inside" in any title or bullet
    for role in corpus["roles"]:
        role["title"] = role["title"].replace("Inside ", "")
    for b in corpus["bullets"]:
        for k in ("text_long", "text_short"):
            b[k] = b[k].replace("inside ", "")
    for claim in (
        "I was your Inside Sales Manager.",
        "I've already been your Inside Sales Manager.",
        "I was Contoso's Inside Sales Manager.",
        "I served as Contoso's Inside Sales Manager.",
    ):
        for text, cited in ((claim, []), (f"{PARTNERS} {claim}", ["b_052"])):
            out = validate_claims(text, cited, corpus, known_companies=KNOWN, **CONTOSO)
            kinds = _kinds(out.violations)
            assert {"claimed_job_title", "fabricated_employer"} <= kinds, (text, out.violations)
    for text in (
        "As your Inside Sales Manager, I'd build the team.",
        "I'd be your next Inside Sales Manager.",
    ):
        out = validate_claims(text, [], corpus, known_companies=KNOWN, **CONTOSO)
        assert out.ok, (text, out.violations)


def test_accepted_false_rejections_of_a_claim_frame() -> None:
    """Fail closed: a past tense inside a modal clause is a claim whatever its
    subject, so an honest disclaimer needs its cited bullets' words too."""
    v = _check("I'd rather show the numbers than claim the team had been bigger.", [])
    assert "unsourced_experience_claim" in _kinds(v), v


# a form question about working at the target: the answer is checked with it
TENURE_QUESTIONS = [
    "Have you previously worked for Fabrikam Payments?",
    "Have you worked with us before?",
    "Are you a current or former employee?",
    "Have you ever been employed by Fabrikam Payments or any of its subsidiaries?",
    "Do you currently work at Fabrikam Payments?",
    "Previous Fabrikam employee?",
    "Have you worked here before?",
]
NOT_TENURE_QUESTIONS = [
    "Tell us about your work experience.",
    "Have you worked in a startup?",
    "When could you start working with us?",
    "Are you legally authorized to work for any employer in the US?",
    "Why do you want to work here?",
]


def test_a_question_about_working_at_the_target_is_recognized() -> None:
    for q in TENURE_QUESTIONS:
        assert asks_target_tenure(q, "Fabrikam Payments"), q
    for q in NOT_TENURE_QUESTIONS:
        assert not asks_target_tenure(q, "Fabrikam Payments"), q
    for a in ("No.", "No", "No, I have not.", "No, I haven't.", "N/A"):
        assert plain_no(a), a
    for a in ("Yes.", "Yes, I have.", "No, but I worked at Haulmark.", "Not yet."):
        assert not plain_no(a), a


@pytest.mark.parametrize("question", TENURE_QUESTIONS)
def test_only_a_plain_no_answers_a_question_about_working_at_the_target(question: str) -> None:
    for answer, cited in (
        ("Yes.", []),
        ("Yes, I have.", GOOD_CITED),
        (f"Yes. {PARTNERS}", ["b_052"]),
    ):
        v = _check(answer, cited, question=question)
        assert "fabricated_employer" in _kinds(v), (answer, v)
    assert _check("No.", [], question=question) == []
    assert _check("No, I haven't.", GOOD_CITED, question=question) == []
    # the question names nothing here: the same answer to another question passes
    assert _check("Yes.", [], question="Are you authorized to work in the US?") == []


def test_a_question_about_working_at_one_of_the_candidates_employers_is_answered() -> None:
    haulmark = {"target_company": "Haulmark", "job_title": "General Manager", "jd_text": ""}
    v = _check(
        "Yes. At Haulmark I led 9 AEs.",
        ["b_052"],
        question="Have you previously worked for Haulmark?",
        **haulmark,
    )
    assert v == [], v


@pytest.mark.parametrize("answer", ["Yes.", "Yes, I have."])
def test_draft_novel_answer_never_says_yes_to_having_worked_at_the_target(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    fake = FakeLLM({"novel": [{"answer": answer, "bullets_used": GOOD_CITED}]})
    monkeypatch.setattr(llm, "call_json", fake.call_json)
    monkeypatch.setattr(llm, "forget", fake.forget)
    drafted, reason, violations = form_spec.draft_novel_answer(
        conn,
        prompt=llm.PromptFile("novel_question"),
        corpus=CORPUS,
        profile=drafting_profile(CORPUS),
        job={
            "id": 1,
            "title": "Director of Revenue Operations",
            "company_name": "Fabrikam Payments",
            "jd_html": f"<p>{FABRIKAM_JD}</p>",
        },
        label="Have you previously worked for Fabrikam Payments?",
        question="Have you previously worked for Fabrikam Payments?",
        known_companies=KNOWN,
    )
    assert drafted is None and reason == "unsourced-claim"
    assert "fabricated_employer" in _kinds(violations), violations
    # one regeneration, carrying the fact check's feedback
    assert len(fake.prompts["novel"]) == 2


def test_draft_novel_answer_returns_a_plain_no_to_having_worked_at_the_target(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLLM({"novel": [{"answer": "No.", "bullets_used": []}]})
    monkeypatch.setattr(llm, "call_json", fake.call_json)
    monkeypatch.setattr(llm, "forget", fake.forget)
    drafted, reason, violations = form_spec.draft_novel_answer(
        conn,
        prompt=llm.PromptFile("novel_question"),
        corpus=CORPUS,
        profile=drafting_profile(CORPUS),
        job={
            "id": 1,
            "title": "Director of Revenue Operations",
            "company_name": "Fabrikam Payments",
            "jd_html": f"<p>{FABRIKAM_JD}</p>",
        },
        label="Are you a former employee?",
        question="Are you a former employee?",
        known_companies=KNOWN,
    )
    assert (drafted, reason, violations) == ("No.", None, [])
