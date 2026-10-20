"""The claim judge applies the user's estimate_style, like the rule checks
(2026-10-10: the judge rejected a confident '15%' for b_016's '~15%')."""

from __future__ import annotations

import pytest

from jobkit.validate import claim_judge


def test_confident_style_tells_the_judge_not_to_flag_a_dropped_hedge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("jobkit.validate.style.configured_estimate_style", lambda: "confident")
    rule = claim_judge.figures_rule()
    assert "do NOT flag a dropped '~'" in rule
    assert "changed value" in rule and "range collapsed" in rule


def test_hedged_style_keeps_estimates_approximate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jobkit.validate.style.configured_estimate_style", lambda: "hedged")
    assert "must stay approximate" in claim_judge.figures_rule()


def test_the_rule_reaches_the_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jobkit.validate.style.configured_estimate_style", lambda: "confident")
    corpus = {"bullets": [], "roles": [], "education": []}
    _pf, prompt = claim_judge.render_prompt("Hello.", [], corpus, target_company="Acme")
    assert "estimate_style: confident" in prompt
    assert "{figures}" not in prompt
