"""estimate_style — "say the numbers confidently" (decision of 2026-10-08).

config/banned_phrases.yaml `drift.estimate_style` is the one knob. Under
"confident" an approximation mark ("~", "about", "approximately") may go, even
on an estimate-confidence metric, but the figure must otherwise be the
corpus's: same value and unit, no added precision, ranges stay ranges, and
"+" / "up to" / "under" / "over" stay where the source has them. Under
"hedged" the pre-2026-10-08 rule holds (and its tests stay, parameterized by
the knob, in test_provenance.py, test_claims.py and test_authenticity.py).

b_061 and b_063 state their figures unhedged in their own source text, so
under "hedged" they failed provenance as written."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from jobkit import llm, mcp_server
from jobkit.pipeline import tailor
from jobkit.validate import style
from jobkit.validate.authenticity import drift_check, review_resume
from jobkit.validate.claims import validate_claims
from jobkit.validate.provenance import figures_report, numeric_claims, validate_tailored
from tests.conftest import REPO_ROOT, TEST_CONFIG

CORPUS: dict[str, Any] = {
    "identity": {"name": "Test Candidate"},
    "roles": [
        {
            "id": "role_haulmark",
            "company": "Haulmark",
            "title": "Regional Sales Manager",
            "start": "2024-03",
            "end": None,
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
        {
            "id": "role_ledgerline",
            "company": "ledgerline",
            "title": "Head of Revenue Operations",
            "start": "2021-11",
            "end": "2024-03",
        },
    ],
    "bullets": [
        {
            "id": "b_061",
            "role_id": "role_ledgerline",
            "metric_confidence": "estimate",
            "text_long": "Paid out $2.6M in sales commissions annually.",
            "text_short": "Paid out $2.6M in sales commissions annually.",
            "metric": "$2.6M in commissions paid per year",
        },
        {
            "id": "b_063",
            "role_id": "role_marlow",
            "metric_confidence": "estimate",
            "text_long": "Signed 5–7 new payroll clients a month at an average of 140 "
            "employees each, up to $1.8M a year in new revenue.",
            "text_short": "5–7 new payroll clients/month, 140 employees each on average; up to "
            "$1.8M/yr in new revenue.",
            "metric": "5–7 clients/month; 140 employees on average; up to $1.8M/yr",
        },
        {
            "id": "b_041",
            "role_id": "role_keyward",
            "metric_confidence": "estimate",
            "text_long": "Ran 6–10 trial kickoffs a week with small IT teams; 20+ of those "
            "teams moved to paid multi-site plans.",
            "text_short": "6–10 trial kickoffs/week with small IT teams; 20+ teams moved to paid "
            "plans.",
            "metric": "6–10 trial kickoffs/week; 20+ teams to paid plans",
        },
        {
            "id": "b_055",
            "role_id": "role_ledgerline",
            "metric_confidence": "verified",
            "text_long": "Stood up a deal desk that approves ~$350–400K of non-standard quotes "
            "a month; the average discount fell from ~19% to ~12%.",
            "text_short": "Deal desk approving ~$350–400K of quotes a month; average discount "
            "~19% → ~12%.",
            "metric": "~$350–400K of quotes/month; average discount ~19% → ~12%",
        },
        {
            "id": "b_053",
            "role_id": "role_ledgerline",
            "metric_confidence": "estimate",
            "text_long": "Answered 40+ pricing questions a day from sellers in the deal-desk "
            "channel.",
            "text_short": "Answered 40+ pricing questions a day from sellers.",
            "metric": "40+ pricing questions a day",
        },
        {
            "id": "b_044",
            "role_id": "role_haulmark",
            "metric_confidence": "estimate",
            "text_long": "Cut time to first dispatch for new carriers by ~40%, with setup "
            "calls kept under ~6 hours.",
            "text_short": "Time to first dispatch -~40%; setup calls under ~6 hours.",
            "metric": "time to first dispatch -~40%; setup calls under ~6 hours",
        },
        {
            "id": "b_052",
            "role_id": "role_haulmark",
            "metric_confidence": "verified",
            "text_long": "Ran a Northeast sales team of up to 9 reps selling route planning to "
            "regional freight carriers.",
            "text_short": "Ran up to 9 reps selling to regional carriers.",
            "metric": "up to 9 reps",
        },
    ],
    "constraints": {"never_claim_titles": ["VP"]},
}

BULLETS = {b["id"]: b for b in CORPUS["bullets"]}


def _check(bid: str, rendered: str, estimate_style: str | None) -> list[dict[str, str]]:
    out = validate_tailored(
        [{"bullet_id": bid, "rendered": rendered}], CORPUS, estimate_style=estimate_style
    )
    assert out.ok == (not out.violations)
    return out.violations


def _kinds(violations: list[dict[str, str]]) -> set[str]:
    return {v["kind"] for v in violations}


# ---------------------------------------------------------------------------
# the knob: one owner, read from config/banned_phrases.yaml
# ---------------------------------------------------------------------------


def test_test_profile_sets_confident_and_template_documents_the_knob() -> None:
    cfg = yaml.safe_load((TEST_CONFIG / "banned_phrases.yaml").read_text())
    assert cfg["drift"]["estimate_style"] == "confident"
    assert style.estimate_style_from(cfg) == "confident"
    # the shipped template sets a valid style (hedged: the stricter rule, and
    # what an absent key means); the setup interview asks which one you want
    tpl = yaml.safe_load((REPO_ROOT / "config" / "banned_phrases.example.yaml").read_text())
    assert style.estimate_style_from(tpl) == "hedged"
    for c in (cfg, tpl):
        # one knob: the retired enforce_hedging key is gone
        assert "enforce_hedging" not in c["drift"]
    # the sandbox copy is what every validator reads when no style is passed
    assert style.configured_estimate_style() == "confident"


def test_knob_values_fail_loudly() -> None:
    assert style.estimate_style_from({}) == "hedged"  # absent: the stricter rule
    assert style.estimate_style_from({"drift": {"estimate_style": "Confident"}}) == "confident"
    with pytest.raises(ValueError, match="estimate_style must be one of"):
        style.estimate_style_from({"drift": {"estimate_style": "bold"}})
    with pytest.raises(ValueError, match="enforce_hedging was replaced"):
        style.estimate_style_from({"drift": {"enforce_hedging": True}})
    with pytest.raises(ValueError):
        validate_tailored([], CORPUS, estimate_style="loud")


def _set_style(home: Path, value: str) -> None:
    path = home / "config" / "banned_phrases.yaml"
    path.write_text(
        path.read_text().replace("estimate_style: confident", f"estimate_style: {value}")
    )


def test_validators_default_to_the_configured_style(jobkit_home: Path) -> None:
    flat = "Paid out $2.6M in sales commissions annually."
    assert _check("b_061", flat, None) == []  # config says confident
    _set_style(jobkit_home, "hedged")
    assert style.configured_estimate_style() == "hedged"
    assert "unhedged_estimate" in _kinds(_check("b_061", flat, None))


# ---------------------------------------------------------------------------
# b_061 and b_063 as written
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bid", ["b_061", "b_063"])
@pytest.mark.parametrize("variant", ["text_long", "text_short"])
def test_estimates_as_written_pass_under_confident(bid: str, variant: str) -> None:
    text = BULLETS[bid][variant]
    assert _check(bid, text, "confident") == []
    # …and under "hedged" they fail on their own unhedged figure — the bug
    hedged = _check(bid, text, "hedged")
    assert _kinds(hedged) == {"unhedged_estimate"}, hedged


# ---------------------------------------------------------------------------
# what "confident" still refuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("estimate_style", ["confident", "hedged"])
@pytest.mark.parametrize(
    ("bid", "rendered", "confident_kind"),
    [
        # inflated value
        ("b_061", "Paid out $3M in sales commissions annually.", "fabricated_number"),
        # added precision
        ("b_061", "Paid out $2.63M in sales commissions annually.", "fabricated_number"),
        # a range turned into its top end
        (
            "b_041",
            "Ran 10 trial kickoffs a week with small IT teams; 20+ teams moved to paid plans.",
            "collapsed_range",
        ),
        # "up to" dropped
        (
            "b_063",
            "Signed 5–7 new payroll clients a month at an average of 140 employees each, "
            "$1.8M a year in new revenue.",
            "dropped_qualifier",
        ),
    ],
)
def test_required_failures(
    estimate_style: str, bid: str, rendered: str, confident_kind: str
) -> None:
    v = _check(bid, rendered, estimate_style)
    assert v, rendered
    if estimate_style == "confident":
        assert confident_kind in _kinds(v), v


@pytest.mark.parametrize(
    ("bid", "rendered", "kind"),
    [
        (
            "b_055",
            "Stood up a deal desk; average discount 19.4% → 12%.",
            "fabricated_number",
        ),
        (
            "b_053",
            "Answered 40 pricing questions a day from sellers.",
            "dropped_qualifier",
        ),
        (
            "b_041",
            "6–10 trial kickoffs/week with small IT teams; 20 teams moved to paid plans.",
            "dropped_qualifier",
        ),
        (
            "b_044",
            "Time to first dispatch -40%; setup calls 6 hours.",
            "dropped_qualifier",
        ),
        ("b_052", "Ran 9 reps selling to regional carriers.", "dropped_qualifier"),
        ("b_061", "Paid out over $2.6M in sales commissions annually.", "added_qualifier"),
        (
            "b_063",
            "Signed 7 new payroll clients a month at an average of 140 employees each, up "
            "to $1.8M a year in new revenue.",
            "collapsed_range",
        ),
        ("b_061", "Paid out $2.4–2.6M in sales commissions annually.", "changed_range"),
    ],
)
def test_confident_keeps_the_figure_exact(bid: str, rendered: str, kind: str) -> None:
    assert kind in _kinds(_check(bid, rendered, "confident"))


@pytest.mark.parametrize(
    ("bid", "rendered"),
    [
        # "~" dropped on a verified figure, and on an estimate
        (
            "b_055",
            "Deal desk approving $350–400K of quotes a month; average discount 19% → 12%.",
        ),
        (
            "b_044",
            "Time to first dispatch -40%; setup calls under 6 hours.",
        ),
        (
            "b_044",
            "Time to first dispatch -40%; setup calls <6 hours.",
        ),
        # "~" / "about" added is still the source's figure
        ("b_061", "Paid out about $2.6M in sales commissions annually."),
        ("b_061", "Paid out ~$2.6M in sales commissions annually."),
        # ranges, "+" and "up to" kept
        (
            "b_041",
            "6-10 trial kickoffs/week with small IT teams; 20+ teams moved to paid plans.",
        ),
        (
            "b_063",
            "5–7 new payroll clients/month at 140 employees each on average; up to $1.8M a "
            "year in new revenue.",
        ),
        ("b_053", "Answered 40+ pricing questions daily from sellers."),
        ("b_052", "Ran up to 9 reps selling to regional carriers."),
    ],
)
def test_confident_passes_plain_figures(bid: str, rendered: str) -> None:
    assert _check(bid, rendered, "confident") == []


def test_qualifiers_parse() -> None:
    def q(text: str) -> list[tuple[str, bool, list[str], str]]:
        return [(c.text, c.approx, sorted(c.bounds), c.figure) for c in numeric_claims(text)]

    assert q("up to ~$1.8M a year") == [("$1.8M", True, ["max"], "up to ~$1.8M")]
    assert q("quote errors <~6%") == [("6%", True, ["max"], "<~6%")]
    assert q("no more than 5 reps") == [("5", False, ["max"], "no more than 5")]
    assert q("just under 5%") == [("5%", False, ["near"], "just under 5%")]
    assert q("10+ users") == [("10", False, ["min"], "10+")]
    assert q(">100% of quota") == [("100%", False, ["min"], ">100%")]
    assert q("$3.2M -> $5.6M") == [("$3.2M", False, [], "$3.2M"), ("$5.6M", False, [], "$5.6M")]
    (lo, hi) = numeric_claims("~$350–400K/month")
    assert lo.range_keys == hi.range_keys == (("currency", 350000.0), ("currency", 400000.0))
    assert lo.figure == hi.figure == "~$350–400K" and lo.approx and not hi.approx
    # the hedged-mode flag is untouched
    assert [c.hedged for c in numeric_claims("~19%, 19%, 6–10, up to 9")] == [
        True,
        False,
        True,
        True,
        True,
    ]


# ---------------------------------------------------------------------------
# review of render-confident, finding 1: a rate keeps its period, in both
# styles. b_061 and b_063 render unhedged only under "confident", so before
# this a 12x inflation of their period passed as ok=True.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("estimate_style", ["confident", "hedged"])
@pytest.mark.parametrize(
    ("bid", "rendered", "kind"),
    [
        # the reviewer's reproductions
        (
            "b_063",
            "5–7 new payroll clients/month, 140 employees each on average; up to "
            "$1.8M/month in new revenue.",
            "changed_period",
        ),
        (
            "b_063",
            "5–7 new payroll clients/week, 140 employees each on average; up to $1.8M/yr.",
            "changed_period",
        ),
        ("b_061", "Paid out $2.6M/month in sales commissions annually.", "changed_period"),
        ("b_061", "Paid out $2.6M in sales commissions monthly.", "changed_period"),
        (
            "b_041",
            "Ran 6–10 trial kickoffs a month with small IT teams; 20+ teams moved to paid plans.",
            "changed_period",
        ),
        # every way of writing the period is read
        ("b_061", "Paid out $2.6M a month in sales commissions.", "changed_period"),
        ("b_061", "Paid out $2.6M in sales commissions per quarter.", "changed_period"),
        ("b_061", "Paid out $2.6M in sales commissions every week.", "changed_period"),
        ("b_055", "The deal desk approves $350–400K of quotes a year.", "changed_period"),
        # the period moved onto a figure the source states without one
        (
            "b_063",
            "5–7 new payroll clients/month, 140 employees/month each on average; up to $1.8M/yr.",
            "added_period",
        ),
        # the period dropped
        ("b_061", "Paid out $2.6M in sales commissions.", "dropped_period"),
        (
            "b_063",
            "5–7 new payroll clients, 140 employees each on average; up to $1.8M/yr.",
            "dropped_period",
        ),
        # a period too far from the figure to be tied to it still has to be
        # the source's: the text says "monthly", the source says annually
        (
            "b_061",
            "Paid out $2.6M in sales commissions to the whole sales team, every month.",
            "dropped_period",
        ),
    ],
)
def test_a_rate_keeps_its_period(estimate_style: str, bid: str, rendered: str, kind: str) -> None:
    v = _check(bid, rendered, estimate_style)
    assert kind in _kinds(v), v


@pytest.mark.parametrize("estimate_style", ["confident", "hedged"])
@pytest.mark.parametrize(
    ("bid", "rendered"),
    [
        ("b_061", "Paid out ~$2.6M in sales commissions per year."),
        ("b_061", "Paid out ~$2.6M a year in sales commissions."),
        ("b_061", "Paid out ~$2.6M in annual sales commissions."),
        ("b_061", "Paid out ~$2.6M in sales commissions each year."),
        # too far to tie to the figure, but the text states the source's period
        ("b_061", "Paid out ~$2.6M in sales commissions to the whole sales team every year."),
        (
            "b_063",
            "Signed ~5–7 new payroll clients a month at an average of ~140 employees each, "
            "up to ~$1.8M a year in new revenue.",
        ),
        (
            "b_041",
            "Ran 6–10 trial kickoffs a week with small IT teams; 20+ teams moved to paid plans.",
        ),
        ("b_053", "Answered 40+ seller pricing questions per day."),
        (
            "b_053",
            "Answered 40+ pricing questions from sellers daily in the deal-desk channel.",
        ),
        ("b_055", "The deal desk approves ~$350–400K of quotes a month."),
    ],
)
def test_the_same_period_in_other_words_passes(
    estimate_style: str, bid: str, rendered: str
) -> None:
    assert _check(bid, rendered, estimate_style) == []


def test_period_parse() -> None:
    def p(text: str) -> list[tuple[str, str]]:
        return [(c.text, c.period) for c in numeric_claims(text)]

    short = "5–7 new payroll clients/month, 140 employees each on average; up to $1.8M/yr."
    assert p(short) == [("5", "month"), ("7", "month"), ("140", ""), ("$1.8M", "year")]
    long = (
        "Signed 5–7 new payroll clients a month at an average of 140 employees each, up to "
        "$1.8M a year in new revenue."
    )
    assert p(long) == [("5", "month"), ("7", "month"), ("140", ""), ("$1.8M", "year")]
    assert p("Paid out $2.6M in sales commissions annually.") == [("$2.6M", "year")]
    assert p("used daily by 40+ people") == [("40", "day")]
    assert p("a ~$5.6M annual bookings run-rate") == [("$5.6M", "run_rate")]
    # one period across a move: "from ~$20K to $75K+", "~$20K → $75K+ monthly"
    assert p("growing monthly partner bookings from ~$20K to $75K+") == [
        ("$20K", "month"),
        ("$75K", "month"),
    ]
    assert p("~$20K → $75K+ monthly") == [("$20K", "month"), ("$75K", "month")]
    assert p("$1.3M ARR, $90K MRR, 30% YoY, 5% month-over-month") == [
        ("$1.3M", "year"),
        ("$90K", "month"),
        ("30%", "year"),
        ("5%", "month"),
    ]
    # durations, other clauses and other lines are not the figure's period
    assert p("Closed >120% of quota on a team of 14 reps; ramped in under a year.") == [
        ("120%", ""),
        ("14", ""),
    ]
    assert p("Ran the store walk monthly; a reseller tier adding $150–250K/year.") == [
        ("$150", "year"),
        ("250K", "year"),
    ]
    assert p("$2.6M in 2017\nannually") == [("$2.6M", ""), ("2017", "")]
    assert p("a year of selling $2.6M") == [("$2.6M", "")]


# ---------------------------------------------------------------------------
# review of render-confident, finding 2: a bound written after the number
# ("$2.6M or more", "$2.6M-plus", "$2.6M and up") is a bound like "over $2.6M"
# ---------------------------------------------------------------------------

TRAILING = [
    "Paid out $2.6M or more in sales commissions annually.",
    "Paid out $2.6M-plus in sales commissions annually.",
    "Paid out $2.6M plus in sales commissions annually.",
    "Paid out $2.6M and up in sales commissions annually.",
    "Paid out $2.6M, at least, in sales commissions annually.",
]


@pytest.mark.parametrize("rendered", TRAILING)
def test_trailing_bound_is_added_qualifier(rendered: str) -> None:
    assert "added_qualifier" in _kinds(_check("b_061", rendered, "confident"))
    # "hedged" already refused these on b_061 (an unhedged estimate)
    assert _check("b_061", rendered, "hedged")


@pytest.mark.parametrize(
    "text",
    [
        "At ledgerline I paid out $2.6M or more in sales commissions a year.",
        "At ledgerline I paid out $2.6M-plus in sales commissions a year.",
        "At ledgerline I paid out $2.6M and up in sales commissions a year.",
    ],
)
def test_trailing_bound_fails_in_letters(text: str) -> None:
    assert "added_qualifier" in _kinds(_letter(text, ["b_061"], "confident"))


def test_trailing_bound_kept_and_dropped() -> None:
    corpus = {
        **CORPUS,
        "bullets": [
            *CORPUS["bullets"],
            {
                "id": "b_070",
                "role_id": "role_keyward",
                "metric_confidence": "verified",
                "text_long": "Ran 10 or more product demos a week.",
                "text_short": "Ran 10 or more product demos a week.",
                "metric": "10 or more product demos a week",
            },
        ],
    }

    def check(rendered: str) -> set[str]:
        res = validate_tailored(
            [{"bullet_id": "b_070", "rendered": rendered}], corpus, estimate_style="confident"
        )
        return _kinds(res.violations)

    assert check("Ran 10 or more product demos a week.") == set()
    assert check("Ran 10+ product demos a week.") == set()  # the same bound
    assert "dropped_qualifier" in check("Ran 10 product demos a week.")
    # b_052's "up to 9" is a max bound: "9 reps or fewer" says the same
    assert _check("b_052", "Ran 9 reps or fewer, selling to regional carriers.", "confident") == []
    assert "added_qualifier" in _kinds(
        _check("b_052", "Ran up to 9 reps or more, selling to regional carriers.", "confident")
    )


def test_trailing_bounds_parse() -> None:
    def q(text: str) -> list[tuple[str, list[str], str]]:
        return [(c.text, sorted(c.bounds), c.figure) for c in numeric_claims(text)]

    assert q("$2.6M or more") == [("$2.6M", ["min"], "$2.6M or more")]
    assert q("$2.6M-plus in commissions") == [("$2.6M", ["min"], "$2.6M-plus")]
    assert q("$2.6M plus in commissions") == [("$2.6M", ["min"], "$2.6M plus")]
    assert q("$2.6M and up") == [("$2.6M", ["min"], "$2.6M and up")]
    assert q("$1.8M, tops.") == [("$1.8M", ["max"], "$1.8M, tops")]
    # words between: the bound counts, the figure stays the number
    assert q("9 AEs or fewer") == [("9", ["max"], "9")]
    assert q("5–7 deals a month or more")[1][:2] == ("7", ["min"])
    # not bounds on the number before them
    assert q("$75K plus 3 renewals") == [("$75K", [], "$75K"), ("3", [], "3")]
    assert q("$75K plus a bonus") == [("$75K", [], "$75K")]
    assert q("6 reps and up to 9 AEs") == [("6", [], "6"), ("9", ["max"], "up to 9")]
    assert q("grew 30% or more importantly kept churn flat") == [("30%", [], "30%")]
    assert q("hit $2.6M at least once") == [("$2.6M", [], "$2.6M")]
    assert q("led 9 AEs and up-leveled managers") == [("9", [], "9")]


# ---------------------------------------------------------------------------
# review of render-confident: a comparative word before the number ("commissions
# exceeding $2.6M", "topping $2.6M", "beyond $2.6M") is a bound like "over $2.6M".
# Before this, every one of these passed as ok=True on b_061 under confident.
# ---------------------------------------------------------------------------

COMPARATIVE = [
    # the reviewer's reproductions
    "Paid out sales commissions exceeding $2.6M annually.",
    "Paid out sales commissions topping $2.6M annually.",
    "Paid out sales commissions beyond $2.6M annually.",
    # the rest of the suggested vocabulary
    "Paid out sales commissions that exceeded $2.6M annually.",
    "Paid out sales commissions that tops $2.6M annually.",
    "Paid out sales commissions that topped $2.6M annually.",
    "Paid out sales commissions surpassing $2.6M annually.",
    "Paid out sales commissions that surpassed $2.6M annually.",
    "Paid out well past $2.6M in sales commissions annually.",
    "Paid out better than $2.6M in sales commissions annually.",
    "Paid out as much as $2.6M in sales commissions annually.",
    "Paid out sales commissions capped at $2.6M annually.",
    # other verbs and comparatives an LLM reaches for
    "Paid out sales commissions outpacing $2.6M annually.",
    "Paid out sales commissions crossing $2.6M annually.",
    "Paid out sales commissions clearing $2.6M annually.",
    "Paid out sales commissions that broke $2.6M annually.",
    "Paid out sales commissions beating $2.6M annually.",
    "Paid out greater than $2.6M in sales commissions annually.",
    "Paid out sales commissions approaching $2.6M annually.",
    "Paid out sales commissions not exceeding $2.6M annually.",
    # a determiner between the verb and the figure
    "Paid out sales commissions exceeding a $2.6M target annually.",
    "Paid out sales commissions crossing the $2.6M mark annually.",
    # fail closed: a comparative of unknown direction is still a bound
    "Paid out faster than $2.6M in sales commissions annually.",
]


@pytest.mark.parametrize("rendered", COMPARATIVE)
def test_comparative_bound_is_added_qualifier(rendered: str) -> None:
    assert "added_qualifier" in _kinds(_check("b_061", rendered, "confident"))
    # "hedged" already refused these on b_061 (an unhedged estimate)
    assert "unhedged_estimate" in _kinds(_check("b_061", rendered, "hedged"))


@pytest.mark.parametrize(
    "text",
    [
        "At ledgerline I paid out sales commissions exceeding $2.6M a year.",
        "At ledgerline I paid out sales commissions topping $2.6M a year.",
        "At ledgerline I paid out sales commissions beyond $2.6M a year.",
        "At ledgerline I paid out well past $2.6M in sales commissions a year.",
        "At ledgerline I paid out better than $2.6M in sales commissions a year.",
    ],
)
def test_comparative_bound_fails_in_letters(text: str) -> None:
    assert "added_qualifier" in _kinds(_letter(text, ["b_061"], "confident"))


def test_comparative_bound_kept_and_matched() -> None:
    corpus = {
        **CORPUS,
        "bullets": [
            *CORPUS["bullets"],
            {
                "id": "b_071",
                "role_id": "role_ledgerline",
                "metric_confidence": "estimate",
                "text_long": "Paid out commissions exceeding $2M a quarter.",
                "text_short": "Paid out commissions exceeding $2M a quarter.",
                "metric": "commissions exceeding $2M per quarter",
            },
        ],
    }

    def check(rendered: str) -> set[str]:
        res = validate_tailored(
            [{"bullet_id": "b_071", "rendered": rendered}], corpus, estimate_style="confident"
        )
        return _kinds(res.violations)

    # the source's own words, and the same "min" bound in other words
    assert check("Paid out commissions exceeding $2M a quarter.") == set()
    assert check("Paid out over $2M in commissions a quarter.") == set()
    assert check("Paid out $2M+ in commissions a quarter.") == set()
    # the bound dropped, or turned into a ceiling
    assert "dropped_qualifier" in check("Paid out $2M in commissions a quarter.")
    assert "dropped_qualifier" in check("Paid out up to $2M in commissions a quarter.")
    # b_052's "up to 9" is a "max" bound: "as many as 9" says the same
    assert (
        _check("b_052", "Ran as many as 9 reps, selling to regional carriers.", "confident") == []
    )
    assert "dropped_qualifier" in _kinds(
        _check("b_052", "Ran more than 9 reps, selling to regional carriers.", "confident")
    )


def test_comparative_bounds_parse() -> None:
    def q(text: str) -> list[tuple[str, list[str], str]]:
        return [(c.text, sorted(c.bounds), c.figure) for c in numeric_claims(text)]

    assert q("pipeline exceeding $2.6M") == [("$2.6M", ["min"], "exceeding $2.6M")]
    assert q("exceeded a $7M target") == [("$7M", ["min"], "exceeded a $7M")]
    assert q("crossing the $500K mark") == [("$500K", ["min"], "crossing the $500K")]
    assert q("not exceeding $2.6M") == [("$2.6M", ["max"], "not exceeding $2.6M")]
    assert q("as many as 9 AEs") == [("9", ["max"], "as many as 9")]
    assert q("no greater than 5%") == [("5%", ["max"], "no greater than 5%")]
    assert q("not lower than 5%") == [("5%", ["min"], "not lower than 5%")]
    assert q("nearing $2.6M") == [("$2.6M", ["near"], "nearing $2.6M")]
    assert q("faster than 3 days") == [("3", ["cmp"], "faster than 3")]
    # not bounds: time phrases, non-comparatives, ranks, plain verbs
    assert q("in the past 3 years") == [("3", [], "3")]
    assert q("over the past 12 months") == [("12", [], "12")]
    assert q("past 12 months") == [("12", [], "12")]
    assert q("my past 2 roles") == [("2", [], "2")]
    assert q("went past $2.6M") == [("$2.6M", ["min"], "past $2.6M")]
    assert q("other than 3 reps") == [("3", [], "3")]
    assert q("rather than 3 reps") == [("3", [], "3")]
    assert q("top 10% of reps") == [("10%", [], "10%")]
    assert q("generating $500K") == [("$500K", [], "$500K")]
    assert q("as well as 3 managers") == [("3", [], "3")]


# ---------------------------------------------------------------------------
# letters and drafted answers (validate/claims.py)
# ---------------------------------------------------------------------------


def _letter(text: str, cited: list[str], estimate_style: str) -> list[dict[str, str]]:
    out = validate_claims(
        text,
        cited,
        CORPUS,
        target_company="Fabrikam Payments",
        job_title="Account Executive",
        estimate_style=estimate_style,
    )
    return out.violations


def test_letters_say_the_numbers_confidently() -> None:
    ok = [
        ("At ledgerline I paid out $2.6M in sales commissions a year.", ["b_061"]),
        (
            "At Marlow Payroll I signed 5–7 new payroll clients a month at an average of 140 "
            "employees each, up to $1.8M a year in new revenue.",
            ["b_063"],
        ),
        (
            "At ledgerline I stood up a deal desk, and the average discount fell from 19% to 12%.",
            ["b_055"],
        ),
    ]
    for text, cited in ok:
        assert _letter(text, cited, "confident") == [], text
    assert "unhedged_estimate" in _kinds(_letter(*ok[0], "hedged"))
    bad = [
        (
            "At ledgerline I paid out $3M in sales commissions a year.",
            ["b_061"],
            "fabricated_number",
        ),
        (
            "At ledgerline I paid out $2.63M in sales commissions a year.",
            ["b_061"],
            "fabricated_number",
        ),
        (
            "At Keyward Security I ran 10 trial kickoffs a week.",
            ["b_041"],
            "collapsed_range",
        ),
        (
            "At Marlow Payroll I signed $1.8M a year in new revenue.",
            ["b_063"],
            "dropped_qualifier",
        ),
        ("At Haulmark I ran 9 reps.", ["b_052"], "dropped_qualifier"),
    ]
    for text, cited, kind in bad:
        assert kind in _kinds(_letter(text, cited, "confident")), text
        assert _letter(text, cited, "hedged"), text


@pytest.mark.parametrize("estimate_style", ["confident", "hedged"])
@pytest.mark.parametrize(
    ("text", "cited", "kind"),
    [
        (
            "At ledgerline I paid out $2.6M a month in sales commissions.",
            ["b_061"],
            "changed_period",
        ),
        (
            "At Marlow Payroll I signed 5–7 new payroll clients a week at an average of 140 "
            "employees each, up to $1.8M a year in new revenue.",
            ["b_063"],
            "changed_period",
        ),
        (
            "At Marlow Payroll I signed 5–7 new payroll clients a month at an average of 140 "
            "employees each, up to $1.8M a month in new revenue.",
            ["b_063"],
            "changed_period",
        ),
        (
            "At Keyward Security I ran 6–10 trial kickoffs a month.",
            ["b_041"],
            "changed_period",
        ),
        (
            "At ledgerline I paid out $2.6M in sales commissions.",
            ["b_061"],
            "dropped_period",
        ),
    ],
)
def test_letters_keep_the_period(
    estimate_style: str, text: str, cited: list[str], kind: str
) -> None:
    v = _letter(text, cited, estimate_style)
    assert kind in _kinds(v), v
    # a range's two ends are one figure: one violation, not two
    assert len([x for x in v if x["kind"] == kind]) == 1, v


# ---------------------------------------------------------------------------
# authenticity review, pass 3: no unhedged-estimate flag under "confident",
# but inflation and precision changes are still flagged
# ---------------------------------------------------------------------------


def _same(texts: list[str]) -> list[list[float]]:
    return [[1.0, 0.0] for _ in texts]


def _cfg(estimate_style: str) -> dict[str, Any]:
    return {"drift": {"similarity_threshold": 0.75, "estimate_style": estimate_style}}


def _figure_flags(rendered: str, bid: str, estimate_style: str) -> list[str]:
    sel = [{"bullet_id": bid, "variant": "short", "rendered": rendered}]
    flags, _ = drift_check(sel, CORPUS, _cfg(estimate_style), embed=_same)
    return [f for flag in flags for f in flag.get("figures", [])]


def test_authenticity_does_not_flag_confident_estimates() -> None:
    for bid in ("b_061", "b_063"):
        assert _figure_flags(BULLETS[bid]["text_short"], bid, "confident") == []
    # under "hedged" the same text is a flat estimate
    hedged = _figure_flags(BULLETS["b_061"]["text_short"], "b_061", "hedged")
    assert hedged and hedged[0].startswith("unhedged_estimate")


@pytest.mark.parametrize("estimate_style", ["confident", "hedged"])
@pytest.mark.parametrize(
    ("bid", "rendered", "kind"),
    [
        ("b_061", "Paid out $3M in sales commissions annually.", "fabricated_number"),
        ("b_061", "Paid out $2.63M in sales commissions annually.", "fabricated_number"),
        (
            "b_055",
            "Stood up a deal desk; average discount ~19.4% → ~12%.",
            "fabricated_number",
        ),
        (
            "b_041",
            "10 trial kickoffs/week with small IT teams; 20+ teams moved to paid plans.",
            "collapsed_range",
        ),
        (
            "b_063",
            "5–7 new payroll clients/month, 140 employees each on average; $1.8M/yr.",
            "dropped_qualifier",
        ),
        # review of render-confident: the period and a trailing bound
        (
            "b_063",
            "5–7 new payroll clients/month, 140 employees each on average; up to "
            "$1.8M/month in new revenue.",
            "changed_period",
        ),
        ("b_061", "Paid out $2.6M or more in sales commissions annually.", "added_qualifier"),
        # review of render-confident: a comparative word before the figure
        ("b_061", "Paid out sales commissions exceeding $2.6M annually.", "added_qualifier"),
        ("b_061", "Paid out sales commissions topping $2.6M annually.", "added_qualifier"),
    ],
)
def test_authenticity_flags_inflation_and_precision(
    estimate_style: str, bid: str, rendered: str, kind: str
) -> None:
    flags = _figure_flags(rendered, bid, estimate_style)
    assert flags, rendered
    if estimate_style == "confident" or kind in ("fabricated_number", "changed_period"):
        assert any(f.startswith(kind) for f in flags), flags


def test_reverted_bullet_is_left_to_the_provenance_recheck() -> None:
    # a bullet reverted for drift is its source's own words: no figure flag
    sel = [{"bullet_id": "b_061", "variant": "short", "rendered": "Paid out $3M in commissions."}]
    far = lambda texts: [[1.0, 0.0] if i % 2 == 0 else [0.0, 1.0] for i in range(len(texts))]  # noqa: E731
    flags, reverted = drift_check(sel, CORPUS, _cfg("confident"), embed=far)
    assert reverted == 1 and not any("figures" in f for f in flags)
    assert sel[0]["rendered"] == BULLETS["b_061"]["text_short"]


def test_review_resume_fails_an_inflated_figure(conn: sqlite3.Connection) -> None:
    from tests.test_filter import _insert

    jid = _insert(conn)
    conn.commit()
    good = [{"bullet_id": "b_061", "variant": "short", "rendered": BULLETS["b_061"]["text_short"]}]
    rep = review_resume(conn, jid, good, CORPUS, embed=_same, use_critic=False)
    assert rep.verdict == "pass" and rep.drift_flags == []
    bad = [{"bullet_id": "b_061", "variant": "short", "rendered": "Paid out $3M in commissions."}]
    rep = review_resume(conn, jid, bad, CORPUS, embed=_same, use_critic=False)
    assert rep.verdict == "fail"
    assert any("fabricated_number" in f for d in rep.drift_flags for f in d.get("figures", []))
    # review of render-confident: a 12x period change fails the review too
    # (before, it showed up only as a non-fatal new_words flag)
    month = "5–7 new payroll clients/month, 140 employees each on average; up to $1.8M/month."
    bad = [{"bullet_id": "b_063", "variant": "short", "rendered": month}]
    rep = review_resume(conn, jid, bad, CORPUS, embed=_same, use_critic=False)
    assert rep.verdict == "fail"
    assert any("changed_period" in f for d in rep.drift_flags for f in d.get("figures", []))


def test_critic_prompt_does_not_treat_a_plain_number_as_a_tell() -> None:
    p = llm.PromptFile("authenticity_critic")
    assert p.version == "1.3"
    assert 'neither a\nmissing "~" nor a present one is a tell' in p.body


# ---------------------------------------------------------------------------
# prompts: the rule follows the knob
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "version"),
    [("select_bullets", "1.8"), ("cover_letter", "1.16"), ("novel_question", "1.8")],
)
def test_prompts_carry_both_rules_keyed_by_the_knob(name: str, version: str) -> None:
    p = llm.PromptFile(name)
    assert p.version == version
    body = " ".join(p.body.split())
    assert "FIGURE STYLE: {estimate_style}." in body
    assert "- confident: " in body and "- hedged: " in body
    assert '"6–10", never "10"' in body
    # review of render-confident: the period of a rate and trailing bounds
    assert "In both styles a rate keeps its period" in body
    assert '"or more", "-plus"' in body
    rendered = p.render(estimate_style="confident")
    assert "{estimate_style}" not in rendered


def test_attempt_key_includes_the_style() -> None:
    a = tailor._attempt_key("h", "c", "1.4", "1.5", "confident")
    assert a.endswith(":confident") and a != tailor._attempt_key("h", "c", "1.4", "1.5", "hedged")


# ---------------------------------------------------------------------------
# CLI `jobkit figures` and MCP `figures`
# ---------------------------------------------------------------------------


def test_figures_report_lists_what_fails_as_written() -> None:
    confident = figures_report(CORPUS, "confident")
    assert confident.startswith("estimate_style: confident")
    assert "14/14 variants pass" in confident and "FAIL" not in confident
    hedged = figures_report(CORPUS, "hedged")
    assert "FAIL  b_061 long" in hedged and "FAIL  b_063 short" in hedged
    one = figures_report(
        CORPUS, "confident", bullet_id="b_041", rendered="Ran 10 trial kickoffs/week."
    )
    assert "FAIL  b_041 rendered" in one and "collapsed_range" in one
    assert "ok    b_061 long" in figures_report(CORPUS, "confident", bullet_id="b_061")
    assert "no bullet 'b_999'" in figures_report(CORPUS, "confident", bullet_id="b_999")


def test_figures_cli_and_mcp(jobkit_home: Path) -> None:
    from jobkit.cli import app

    (jobkit_home / "data" / "career_corpus.yaml").write_text(yaml.safe_dump(CORPUS))
    runner = CliRunner()
    res = runner.invoke(app, ["figures"])
    assert res.exit_code == 0, res.output
    assert "estimate_style: confident" in res.output
    assert "every bullet can be rendered word for word" in res.output
    res = runner.invoke(
        app,
        [
            "figures",
            "--bullet-id",
            "b_063",
            "--rendered",
            "Signed 5–7 new payroll clients a month at an average of 140 employees each, "
            "$1.8M a year in new revenue.",
        ],
    )
    assert res.exit_code == 0 and "dropped_qualifier" in res.output, res.output
    assert "every bullet can be rendered word for word" in mcp_server.figures()
    assert "ok    b_061 rendered" in mcp_server.figures(
        "b_061", "Paid out $2.6M in sales commissions annually."
    )
    _set_style(jobkit_home, "hedged")
    assert "FAIL  b_061 long" in mcp_server.figures()


def test_figures_without_a_corpus(jobkit_home: Path) -> None:
    assert mcp_server.figures().startswith("no corpus at")
