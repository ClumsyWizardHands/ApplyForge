"""MANDATORY adversarial provenance test (CLAUDE.md): a model output containing
a plausible but unsourced metric MUST be rejected. Plus render smoke tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jobkit.render.pdf import page_count, render_pdf
from jobkit.validate.provenance import numeric_claims, validate_tailored

CORPUS = {
    "bullets": [
        {
            "id": "b_043",
            "role_id": "r1",
            "metric_confidence": "verified",
            "text_long": "Grew the average reseller partner's annual bookings from ~$20K to "
            "$75K+ by training partner sellers across the Northeast.",
            "text_short": "Grew average partner bookings ~$20K → $75K+ a year.",
            "metric": "~$20K → $75K+",
        },
        {
            "id": "b_053",
            "role_id": "r1",
            "metric_confidence": "estimate",
            "text_long": "Cut time to first dispatch by ~40%.",
            "text_short": "Time to first dispatch -~40%.",
            "metric": "~40%",
        },
        {
            "id": "b_047",
            "role_id": "r1",
            "metric_confidence": "verified",
            "text_long": "Closed ~$5.6M in annual bookings with the Northeast team in 2025, "
            "~18% of it partner-sourced, at an average first-year contract of ~$20K.",
            "text_short": "Closed ~$5.6M in 2025 bookings (~18% partner-sourced); ~$20K "
            "average first-year contract.",
            "metric": "~$5.6M annual bookings; ~18% partner-sourced; ~$20K average contract",
        },
    ],
    "constraints": {"never_claim_titles": ["CTO"], "never_claim_tools": ["Kubernetes"]},
}


def test_adversarial_fabricated_metric_rejected() -> None:
    # plausible but unsourced: "47%" appears nowhere in the source bullet
    out = validate_tailored(
        [{"bullet_id": "b_043", "rendered": "Grew partner bookings 47% from ~$20K to $75K+."}],
        CORPUS,
    )
    assert not out.ok
    assert any(v["kind"] == "fabricated_number" and "47" in v["detail"] for v in out.violations)


def test_unknown_bullet_id_rejected() -> None:
    out = validate_tailored([{"bullet_id": "b_999", "rendered": "Anything."}], CORPUS)
    assert not out.ok and out.violations[0]["kind"] == "unknown_bullet_id"


@pytest.mark.parametrize("style", ["hedged", "confident"])
def test_estimate_must_stay_hedged(style: str) -> None:
    # config/banned_phrases.yaml drift.estimate_style: "hedged" is the rule
    # before 2026-10-08; "confident" lets an estimate drop its "~"
    flat = validate_tailored(
        [{"bullet_id": "b_053", "rendered": "Cut time to first dispatch by 40%."}],
        CORPUS,
        estimate_style=style,
    )
    if style == "hedged":
        assert not flat.ok
        assert any(v["kind"] == "unhedged_estimate" for v in flat.violations)
    else:
        assert flat.ok, flat.violations
    hedged = validate_tailored(
        [{"bullet_id": "b_053", "rendered": "Cut time to first dispatch by ~40%."}],
        CORPUS,
        estimate_style=style,
    )
    assert hedged.ok


def test_never_claim_and_unsourced_noun() -> None:
    out = validate_tailored(
        [
            {
                "bullet_id": "b_043",
                "rendered": "As CTO, grew bookings ~$20K to $75K+ using Kubernetes and Datadog.",
            }
        ],
        CORPUS,
    )
    kinds = {v["kind"] for v in out.violations}
    assert {"never_claim_title", "never_claim_tool", "unsourced_proper_noun"} <= kinds


def test_clean_selection_passes() -> None:
    out = validate_tailored(
        [
            {
                "bullet_id": "b_043",
                "rendered": "Grew average partner bookings ~$20K → $75K+ a year.",
            },
            {"bullet_id": "b_053", "rendered": "Time to first dispatch -~40%."},
        ],
        CORPUS,
    )
    assert out.ok, out.violations


def test_resume_renders_one_page(tmp_path: Path) -> None:
    data = {
        "identity": {
            "name": "Jordan Rivera",
            "display_location": "Bristol, PA",
            "phone": "+1 555 555 0100",
            "email": "jordan.rivera@example.com",
            "linkedin": "linkedin.com/in/jordan-rivera-example",
        },
        "positioning": "Revenue operations lead who carried a quota first.",
        "skills": ["Revenue Operations", "Channel Partnerships"],
        "roles": [
            {
                "company": "Haulmark",
                "title": "Regional Sales Manager",
                "dates": "Mar 2024 – Present",
                "bullets": [
                    "Grew average partner bookings ~$20K → $75K+ a year.",
                    "Time to first dispatch -~40%.",
                ],
            }
        ],
        "education": [
            {"school": "Lakeside State University", "degree": "B.S.", "years": "2008 – 2012"}
        ],
    }
    pdf = render_pdf("resume.typ", data, tmp_path / "resume.pdf")
    assert pdf.exists() and pdf.stat().st_size > 1000
    assert page_count(pdf) == 1


# ---- audit 2026-10-08 §2 item 3: inputs that used to pass as ok=True ----


@pytest.mark.parametrize(
    ("rendered", "needle"),
    [
        # 20 was "found" in ~$20K because numbers were compared as bare digits
        ("Grew partner bookings 20% to $75K+.", "20%"),
        # a rank's bare digit was "found" in a figure; rank and multiplier
        # claims were never checked
        ("Grew partner bookings ~$20K to $75K+ as the #1 rep, doubling pipeline.", "#1"),
        ("Grew partner bookings ~$20K to $75K+ as the #1 rep, doubling pipeline.", "doubling"),
        # number words were never claims
        ("Grew partner bookings ~$20K to $75K+ with a team of twelve enterprise AEs.", "twelve"),
        ("Grew partner bookings ~$20K to $75K+, tenfold.", "tenfold"),
        # audit review wp2: plural, approximate and fractional quantities
        ("Grew partner bookings ~$20K to $75K+, closing millions in revenue.", "millions"),
        ("Grew partner bookings ~$20K to $75K+ across hundreds of deals.", "hundreds"),
        ("Grew partner bookings ~$20K to $75K+, closing dozens of deals a month.", "dozens"),
        ("Grew partner bookings ~$20K to $75K+ on tens of millions in pipeline.", "tens of"),
        ("Grew partner bookings ~$20K to $75K+ over a decade.", "a decade"),
        ("Grew partner bookings ~$20K to $75K+ in a year and a half.", "a year and a half"),
        ("Grew partner bookings ~$20K to $75K+ in half a year.", "half a year"),
    ],
)
def test_adversarial_numbers_with_units(rendered: str, needle: str) -> None:
    out = validate_tailored([{"bullet_id": "b_043", "rendered": rendered}], CORPUS)
    assert not out.ok
    assert any(v["kind"] == "fabricated_number" and needle in v["detail"] for v in out.violations)


def test_hedge_must_sit_next_to_its_number() -> None:
    # estimate_style "hedged": "over" elsewhere in the sentence used to count
    # as hedging 40%
    out = validate_tailored(
        [{"bullet_id": "b_053", "rendered": "Cut time to first dispatch by 40%, over target."}],
        CORPUS,
        estimate_style="hedged",
    )
    assert not out.ok and any(v["kind"] == "unhedged_estimate" for v in out.violations)
    # a hedge right next to the number, or a range, still passes
    for ok in ("Cut time to first dispatch by ~40%, over target.", "Time to first dispatch -~40%."):
        res = validate_tailored(
            [{"bullet_id": "b_053", "rendered": ok}], CORPUS, estimate_style="hedged"
        )
        assert res.ok, ok
    # "confident": the plain 40% is the source's figure; "over target" after
    # it is not a bound on it
    plain = validate_tailored(
        [{"bullet_id": "b_053", "rendered": "Cut time to first dispatch by 40%, over target."}],
        CORPUS,
        estimate_style="confident",
    )
    assert plain.ok, plain.violations


TITLED = {
    "bullets": [
        {
            "id": "b_052",
            "role_id": "r1",
            "metric_confidence": "verified",
            "text_long": "Led up to 9 AEs, reporting to the general manager's office in "
            "Boston; ran the weekly pipeline review across the region.",
            "text_short": "Led up to 9 AEs; ran the weekly pipeline review.",
            "metric": "up to 9 AEs",
        },
        {
            "id": "b_058",
            "role_id": "r2",
            "metric_confidence": "verified",
            "text_long": "Closed 124% and then 131% of annual quota on mid-market payroll "
            "deals; Mid-Market Account Executive from 2018 to 2021.",
            "text_short": "124%, then 131% of annual quota on mid-market deals; Mid-Market AE.",
            "metric": "124% and 131% of annual quota",
        },
        {
            "id": "b_066",
            "role_id": "r3",
            "metric_confidence": "none",
            "text_long": "Sold tents, packs, and climbing harnesses to walk-in customers.",
            "text_short": "Sold tent/pack/climbing harness fittings to walk-ins — owned sale and "
            "fitting.",
            "metric": None,
        },
    ],
    "constraints": {
        "never_claim_titles": ["VP", "SVP", "CRO", "General Manager"],
        "never_claim_phrases": ["treasury management sales"],
    },
    "skill_index": {"Salesforce": "hands-on", "Channel Partnerships": "b_052"},
}


def test_short_titles_checked_against_never_claim() -> None:
    for rendered in ("Promoted to VP in 2022.", "Promoted to SVP in 2022."):
        out = validate_tailored([{"bullet_id": "b_058", "rendered": rendered}], TITLED)
        kinds = {v["kind"] for v in out.violations}
        assert {"never_claim_title", "unsourced_proper_noun"} <= kinds, (rendered, kinds)


def test_another_persons_title_and_substrings_do_not_trip() -> None:
    # the corpus's own words: "the general manager's office", and "across" contains "cro"
    long = TITLED["bullets"][0]["text_long"]
    assert validate_tailored([{"bullet_id": "b_052", "rendered": long}], TITLED).ok
    # …but a title the source never mentions is not sourced just by being someone else's
    out = validate_tailored(
        [{"bullet_id": "b_052", "rendered": "Led up to 9 AEs under our VP of Sales."}],
        TITLED,
    )
    assert {v["kind"] for v in out.violations} >= {"unsourced_title_mention"}


def test_slash_joined_short_text_passes_word_for_word() -> None:
    short = TITLED["bullets"][2]["text_short"]
    out = validate_tailored([{"bullet_id": "b_066", "rendered": short}], TITLED)
    assert out.ok, out.violations


def test_never_claim_phrase_rejected() -> None:
    out = validate_tailored(
        [{"bullet_id": "b_052", "rendered": "Led up to 9 AEs in treasury management sales."}],
        TITLED,
    )
    assert any(v["kind"] == "never_claim_phrase" for v in out.violations)


def test_skills_line_must_come_from_skill_index() -> None:
    ok = validate_tailored([], TITLED, skills_line=["salesforce", "Channel Partnerships"])
    assert ok.ok, ok.violations
    bad = validate_tailored([], TITLED, skills_line=["Salesforce", "Treasury management sales"])
    kinds = {v["kind"] for v in bad.violations}
    assert {"unindexed_skill", "never_claim_phrase"} <= kinds


# ---- audit review wp2 ----


@pytest.mark.parametrize("style", ["hedged", "confident"])
def test_verified_bullet_keeps_its_source_hedges(style: str) -> None:
    def check(rendered: str) -> Any:
        return validate_tailored(
            [{"bullet_id": "b_047", "rendered": rendered}], CORPUS, estimate_style=style
        )

    flat = CORPUS["bullets"][2]["text_short"].replace("~", "")
    if style == "hedged":
        # "~$5.6M" is hedged everywhere in b_047 (verified); the resume may not drop it
        out = check("$5.6M in annual bookings.")
        assert not out.ok
        assert any(v["kind"] == "dropped_hedge" and "$5.6M" in v["detail"] for v in out.violations)
        # every hedge stripped from the short text is caught, not just the first
        out = check(flat)
        (v,) = [v for v in out.violations if v["kind"] == "dropped_hedge"]
        assert all(n in v["detail"] for n in ("$5.6M", "18%", "$20K")), v
    else:
        # "confident": "~" may go, on any metric_confidence (ranges, "+" and
        # "up to" may not: tests/test_estimate_style.py)
        for ok in ("$5.6M in annual bookings.", flat):
            assert check(ok).ok, (ok, check(ok).violations)
    # the same claims hedged, or a number the source also states flat, still pass
    for ok in (
        "~$5.6M in annual bookings.",
        "Closed ~$5.6M in 2025 bookings; ~$20K average first-year contract.",
        "Closed roughly $5.6M in annual bookings (about 18% partner-sourced).",
    ):
        assert check(ok).ok, ok
    # verbatim source text always passes
    for k in ("text_long", "text_short"):
        assert check(CORPUS["bullets"][2][k]).ok, k


@pytest.mark.parametrize(
    "rendered",
    [
        "In my General Manager seat, led up to 9 AEs; ran the weekly pipeline review.",
        "My General Manager role: led up to 9 AEs; ran the weekly pipeline review.",
        "Took the General Manager seat; led up to 9 AEs, running the weekly pipeline review.",
        "Covered the General Manager role; led up to 9 AEs.",
    ],
)
def test_possessive_or_seat_verb_title_is_own(rendered: str) -> None:
    # b_052 mentions "the general manager's office" — sourced as someone else's
    # title, never as the candidate's
    out = validate_tailored([{"bullet_id": "b_052", "rendered": rendered}], TITLED)
    assert any(v["kind"] == "never_claim_title" for v in out.violations), out.violations


def test_quantity_words_parse() -> None:
    def keys(text: str) -> list[tuple[str, float]]:
        return [c.key for c in numeric_claims(text)]

    assert keys("millions") == [("vague", 1e6)]
    assert keys("tens of millions") == [("vague", 1e7)]
    assert keys("hundreds of thousands") == [("vague", 1e5)]
    assert keys("multi-million dollar deals") == [("vague", 1e6)]
    assert keys("a decade") == [("decade", 1.0)]
    assert keys("two decades") == [("decade", 2.0)]
    assert keys("half a decade") == [("decade", 0.5)]
    assert keys("a year and a half") == [("plain", 1.5)]
    assert keys("two and a half years") == [("plain", 2.5)]
    assert keys("cut churn in half") == [("multiplier", 0.5)]
    assert keys("halved churn") == [("multiplier", 0.5)]
    assert keys("two-thirds of revenue") == [("plain", round(2 / 3, 6))]
    # hedges carry over: "over a decade" is hedged
    assert numeric_claims("over a decade")[0].hedged
    # not quantities
    for text in ("the second half of 2023", "a half-hearted pitch", "a third rep", "test scores"):
        assert [c for c in numeric_claims(text) if c.kind != "year"] == [], text


# ---- second review of wp2: own-title claims that passed as "someone else's" ----
# b_052's source says "the general manager's office", so the title counts as
# sourced. Before the fix, each of these passed validate_tailored with ok=True;
# the pre-audit substring check rejected every one.


@pytest.mark.parametrize(
    "rendered",
    [
        "Owned the General Manager role; led up to 9 AEs.",
        "Ran the General Manager function; led up to 9 AEs.",
        "Led up to 9 AEs, effectively the General Manager.",
        "Led up to 9 AEs, serving in the General Manager capacity.",
        "the general manager's duties were mine",
        "Led up to 9 AEs; the General Manager role was mine.",
        "Led up to 9 AEs; ran the weekly pipeline review in the General Manager role.",
        "Led up to 9 AEs; did the general manager's job.",
        "Led up to 9 AEs; effectively the General Manager.",
        "Led up to 9 AEs; the org's General Manager.",
        # another person's title, paraphrased away from the source's own words
        "Led up to 9 AEs; ran the weekly pipeline review from the general manager's chair.",
        "Led up to 9 AEs; ran the weekly pipeline review for the General Manager.",
    ],
)
def test_sourced_title_rendered_as_own_is_rejected(rendered: str) -> None:
    out = validate_tailored([{"bullet_id": "b_052", "rendered": rendered}], TITLED)
    assert not out.ok
    assert any(v["kind"] == "never_claim_title" for v in out.violations), out.violations


def test_sourced_title_keeps_the_sources_own_words() -> None:
    # the source's phrase survives in a shorter rendering…
    for ok in (
        "Led up to 9 AEs, reporting to the general manager's office in Boston.",
        "Reporting to the general manager's office in Boston, led up to 9 AEs.",
    ):
        out = validate_tailored([{"bullet_id": "b_052", "rendered": ok}], TITLED)
        assert out.ok, (ok, out.violations)
