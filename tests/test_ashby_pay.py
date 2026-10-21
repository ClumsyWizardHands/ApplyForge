"""Ashby reports pay per interval ('1 YEAR', '1 HOUR', ...). The pay floors
are yearly, so only an annual range is stored; an hourly range is left
unpublished, as the Greenhouse and Lever adapters do."""

from __future__ import annotations

from jobkit.adapters import ADAPTERS
from jobkit.adapters.ats.ashby import _compensation
from tests.conftest import load_fixture


def test_hourly_range_is_left_unpublished() -> None:
    comp = {
        "summaryComponents": [
            {
                "compensationType": "Salary",
                "interval": "1 HOUR",
                "currencyCode": "USD",
                "minValue": 38,
                "maxValue": 52,
            }
        ]
    }
    assert _compensation(comp) == (None, None, None)


def test_annual_range_is_read_as_before() -> None:
    comp = {
        "summaryComponents": [
            {"compensationType": "EquityPercentage", "interval": "NONE", "minValue": 0.1},
            {
                "compensationType": "Salary",
                "interval": "1 YEAR",
                "currencyCode": "USD",
                "minValue": 150000,
                "maxValue": 190000,
            },
        ]
    }
    assert _compensation(comp) == (150000, 190000, "USD")
    # a component with no interval keeps the earlier annual reading
    del comp["summaryComponents"][1]["interval"]
    assert _compensation(comp) == (150000, 190000, "USD")


def test_hourly_fixture_normalizes() -> None:
    jobs = {j.title: j for j in ADAPTERS["ashby"].parse(load_fixture("ashby_hourly.json"))}
    rn = jobs["Registered Nurse, Virtual Care"]
    assert (rn.comp_min, rn.comp_max, rn.comp_currency) == (None, None, None)
    # an hourly component first, then the annual one: the annual one is used
    mgr = jobs["Nurse Manager"]
    assert (mgr.comp_min, mgr.comp_max, mgr.comp_currency) == (105000, 125000, "USD")


def test_recorded_ramp_board_keeps_its_annual_pay() -> None:
    jobs = ADAPTERS["ashby"].parse(load_fixture("ashby_ramp.json"))
    assert any(j.comp_max and j.comp_max > 100000 for j in jobs)


def test_owner_path_decision_hourly_ae_posting_is_unpublished_pay() -> None:
    """A deliberate change on the sales path (listed under Updating in
    docs/setup.md): before this round a $45-60 an hour AE posting was read as
    a $45-60 a year range and rejected by the comp floor. It is now stored as
    unpublished pay, so the filter passes it to the next stage, as it does
    any posting without a yearly range. Monthly and weekly ranges are the
    same."""
    from jobkit.config import load_filters
    from jobkit.pipeline.filter import reject_reason_now as _reject_reason
    from tests.test_onboarding_intake import _job

    filters = load_filters()  # the sales sandbox profile (no new keys)
    for interval in ("1 HOUR", "1 WEEK", "1 MONTH"):
        comp = {
            "summaryComponents": [
                {
                    "compensationType": "Salary",
                    "interval": interval,
                    "currencyCode": "USD",
                    "minValue": 45,
                    "maxValue": 60,
                }
            ]
        }
        lo, hi, cur = _compensation(comp)
        assert (lo, hi, cur) == (None, None, None), interval
        row = {**_job("Account Executive"), "comp_min": lo, "comp_max": hi, "comp_currency": cur}
        assert _reject_reason(row, filters) is None, interval
    # what the old annual reading did with the same posting
    old = {**_job("Account Executive"), "comp_min": 45, "comp_max": 60, "comp_currency": "USD"}
    assert str(_reject_reason(old, filters)).startswith("comp_below_floor:60<")
