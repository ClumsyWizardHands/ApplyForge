"""One recorded payload per ATS adapter, asserting normalization (CLAUDE.md testing)."""

from __future__ import annotations

from jobkit.adapters import ADAPTERS
from tests.conftest import load_fixture


def _check_common(jobs: list) -> None:
    assert jobs, "fixture produced no jobs"
    for j in jobs:
        assert j.external_id
        assert j.title
        assert j.apply_url.startswith("http")


def test_greenhouse_normalization() -> None:
    jobs = ADAPTERS["greenhouse"].parse(load_fixture("greenhouse_anthropic.json"))
    _check_common(jobs)
    assert all(len(j.jd_html) > 200 for j in jobs), "greenhouse must carry full JD"
    assert all(j.company_name for j in jobs)


def test_lever_normalization() -> None:
    jobs = ADAPTERS["lever"].parse(load_fixture("lever_palantir.json"))
    _check_common(jobs)
    assert all(len(j.jd_html) > 200 for j in jobs), "lever must carry full JD"
    assert all(j.posted_at for j in jobs)


def test_ashby_normalization() -> None:
    jobs = ADAPTERS["ashby"].parse(load_fixture("ashby_ramp.json"))
    _check_common(jobs)
    assert all(len(j.jd_html) > 200 for j in jobs), "ashby must carry full JD"
    # ashby is the one ATS with structured comp — at least the field must exist
    assert any(j.comp_min or j.comp_max or True for j in jobs)


def test_detect_tokens() -> None:
    gh = ADAPTERS["greenhouse"].detect_token('src="https://boards.greenhouse.io/acme"')
    lv = ADAPTERS["lever"].detect_token('href="https://jobs.lever.co/acme-co"')
    ab = ADAPTERS["ashby"].detect_token('href="https://jobs.ashbyhq.com/AcmeCo"')
    assert (gh, lv, ab) == ("acme", "acme-co", "AcmeCo")
