"""`jobkit doctor` checks for targeting: prompt set, the no-reply rule, role
tracks, standing-rule title classes and feed keys. Each new check is a warning
or information, never a failure, so a working setup stays "Ready."; the sales
test profile (a config written before prompt_set) gets no prompt_set warning."""

from __future__ import annotations

from pathlib import Path

import yaml

from jobkit import doctor
from tests.conftest import FIXTURES

PERSONAS = FIXTURES / "personas"


def _env(root: Path, environ: dict[str, str] | None = None) -> doctor.Env:
    return doctor.Env(
        root=root.resolve(),
        home=root / "home",
        platform="darwin",
        environ=environ or {},
        which=lambda name: None,
        python=(3, 12),
        site_packages=None,
    )


def _filters(root: Path) -> dict[str, object]:
    raw = yaml.safe_load((root / "config" / "filters.yaml").read_text())
    assert isinstance(raw, dict)
    return raw


def _write(root: Path, raw: object, name: str = "filters.yaml") -> None:
    (root / "config" / name).write_text(yaml.safe_dump(raw))


def _persona(root: Path, name: str) -> None:
    (root / "config" / "filters.yaml").write_text((PERSONAS / f"{name}.yaml").read_text())


# ---- prompt set ----


def test_sales_search_without_prompt_set_is_not_warned(jobkit_home: Path) -> None:
    check = doctor.check_prompt_set(_env(jobkit_home))
    assert check is not None and check.ok
    assert "sales" in check.detail


def test_non_sales_search_without_prompt_set_is_warned(jobkit_home: Path) -> None:
    raw = yaml.safe_load((PERSONAS / "engineer.yaml").read_text())
    del raw["prompt_set"]
    _write(jobkit_home, raw)
    check = doctor.check_prompt_set(_env(jobkit_home))
    assert check is not None and not check.ok and not check.required
    assert "prompt_set: general" in check.fix


def test_explicit_prompt_set_is_reported(jobkit_home: Path) -> None:
    _persona(jobkit_home, "rn_columbus")
    check = doctor.check_prompt_set(_env(jobkit_home))
    assert check == doctor.Check("prompt set", True, "prompt_set: general")


def test_missing_or_broken_filters_skip_the_new_checks(jobkit_home: Path) -> None:
    (jobkit_home / "config" / "filters.yaml").write_text("prompt_set: nursing\n")
    env = _env(jobkit_home)
    assert doctor.check_prompt_set(env) is None
    assert doctor.check_ghost_after(env) is None
    assert doctor.check_tracks(env) is None
    assert doctor.check_title_classes(env) is None
    assert doctor.check_feeds(env) == []
    (jobkit_home / "config" / "filters.yaml").unlink()
    assert doctor.check_prompt_set(env) is None


# ---- the no-reply rule ----


def test_legacy_followups_block_is_honoured_as_information(jobkit_home: Path) -> None:
    check = doctor.check_ghost_after(_env(jobkit_home))
    assert check is not None and check.ok
    assert "after 30 days" in check.detail and "followups block" in check.detail


def test_tracking_ghost_days_reported(jobkit_home: Path) -> None:
    _persona(jobkit_home, "engineer")
    check = doctor.check_ghost_after(_env(jobkit_home))
    assert check is not None and check.ok
    assert "after 21 days" in check.detail and "tracking.ghost_after_days" in check.detail


# ---- role tracks ----


def test_placeholder_track_is_warned(jobkit_home: Path) -> None:
    raw = _filters(jobkit_home)
    raw["filters"]["tracks"] = [  # type: ignore[index]
        {"id": "my_role", "title_regex": r"(?i)\bREPLACE ME\b"}
    ]
    raw["filters"]["comp"].pop("track_floors")  # type: ignore[index]
    _write(jobkit_home, raw)
    check = doctor.check_tracks(_env(jobkit_home))
    assert check is not None and not check.ok and not check.required
    assert "my_role" in check.detail and "set up jobkit" in check.fix


def test_real_tracks_pass(jobkit_home: Path) -> None:
    check = doctor.check_tracks(_env(jobkit_home))
    assert check is not None and check.ok and "account_executive" in check.detail


# ---- standing rules ----


def test_unknown_title_class_is_a_warning(jobkit_home: Path) -> None:
    _write(
        jobkit_home,
        {"auto_reject": [{"when": {"title_class": "registered_nurse"}, "reason": "x"}]},
        "autoapprove.yaml",
    )
    check = doctor.check_title_classes(_env(jobkit_home))
    assert check is not None and not check.ok and not check.required
    assert "registered_nurse" in check.detail and "account_executive" in check.fix
    report = doctor.run_checks(_env(jobkit_home))
    assert check in report.checks and check not in report.failures


def test_known_title_classes_pass(jobkit_home: Path) -> None:
    check = doctor.check_title_classes(_env(jobkit_home))
    assert check is not None and check.ok and "account_executive" in check.detail
    _persona(jobkit_home, "rn_columbus")
    _write(
        jobkit_home,
        {"auto_approve": [{"when": {"title_class": "registered_nurse"}, "reason": "x"}]},
        "autoapprove.yaml",
    )
    check = doctor.check_title_classes(_env(jobkit_home))
    assert check is not None and check.ok


# ---- feeds ----


def test_enabled_keyed_feed_without_keys_is_warned(jobkit_home: Path) -> None:
    checks = {c.name: c for c in doctor.check_feeds(_env(jobkit_home))}
    adzuna = checks["adzuna feed"]  # the sales profile turns adzuna on
    assert not adzuna.ok and not adzuna.required
    assert "ADZUNA_APP_ID and ADZUNA_APP_KEY" in adzuna.detail
    assert "usajobs feed" not in checks  # off in the profile
    keyed = _env(jobkit_home, {"ADZUNA_APP_ID": "a", "ADZUNA_APP_KEY": "b"})
    assert {c.name: c.ok for c in doctor.check_feeds(keyed)} == {"adzuna feed": True}


def test_usajobs_keys(jobkit_home: Path) -> None:
    _persona(jobkit_home, "rn_columbus")
    checks = {c.name: c for c in doctor.check_feeds(_env(jobkit_home, {"USAJOBS_API_KEY": "k"}))}
    assert not checks["usajobs feed"].ok
    assert "USAJOBS_USER_AGENT_EMAIL" in checks["usajobs feed"].detail


def test_linkedin_on_built_in_sales_keywords_is_warned_for_a_non_sales_search(
    jobkit_home: Path,
) -> None:
    raw = yaml.safe_load((PERSONAS / "engineer.yaml").read_text())
    raw["feeds"]["linkedin_guest"] = {"enabled": True}
    _write(jobkit_home, raw)
    checks = {c.name: c for c in doctor.check_feeds(_env(jobkit_home))}
    li = checks["linkedin discovery"]
    assert not li.ok and not li.required and "built-in sales keywords" in li.detail
    # a sales search on the built-in keywords is what they are for
    raw = yaml.safe_load((FIXTURES / "config" / "filters.yaml").read_text())
    raw["feeds"]["linkedin_guest"] = {"enabled": True}
    _write(jobkit_home, raw)
    names = {c.name for c in doctor.check_feeds(_env(jobkit_home))}
    assert "linkedin discovery" not in names


# ---- the sales profile stays Ready ----


def test_new_checks_never_fail_the_sales_profile(jobkit_home: Path) -> None:
    env = _env(jobkit_home)
    for step in (
        doctor.check_prompt_set,
        doctor.check_ghost_after,
        doctor.check_tracks,
        doctor.check_title_classes,
    ):
        check = step(env)
        assert check is not None and check.ok, step.__name__
    assert all(not c.required for c in doctor.check_feeds(env))
