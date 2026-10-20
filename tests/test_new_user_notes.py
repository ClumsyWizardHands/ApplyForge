"""Interim notes for new users, kept in place until the repository owner decides.

Two things only the repository owner decides before anyone is invited:

* prompts/score_fit.md's worked anchors are one invented sales career. While
  they are, "Get your own jobkit" and step 4 of the setup skill tell a new user
  that the fit score is calibrated for sales roles.
* CLAUDE.md Hard Rule 1 carries an amendment naming
  src/jobkit/autofill/submit.py and an `auto_submit` setting, and neither
  ships. While submit.py is absent, the setup and daily skills tell a user's
  Claude that jobkit has no submission step and not to build, enable or
  configure one, and no shipped code or template mentions `auto_submit`.

These tests do not make either decision. Each requires its note only while the
condition it warns about holds, so the repository owner's change is not blocked by them.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import REPO_ROOT

SKILLS = REPO_ROOT / ".claude" / "skills"
SCORE_PROMPT = REPO_ROOT / "prompts" / "score_fit.md"
SUBMIT_MODULE = REPO_ROOT / "src" / "jobkit" / "autofill" / "submit.py"

SALES_NOTE = "calibrated for sales roles"
NO_SUBMIT_NOTES = (
    "no submission step to turn on",
    "there is no `src/jobkit/autofill/submit.py` and no `auto_submit` setting",
    "do not build, enable or configure automatic submission",
    "only the owner of the jobkit github repository makes",
)


def _flat(text: str) -> str:
    return " ".join(text.lower().split())


def _section(text: str, heading: str) -> str:
    """From the `heading` line to the next heading of the same level."""
    level = heading.split(" ", 1)[0]
    start = text.index(f"\n{heading}\n")
    end = text.find(f"\n{level} ", start + len(heading) + 2)
    return text[start : end if end != -1 else len(text)]


def _one_career_anchors(prompt_text: str) -> bool:
    """True while the scoring anchors are the single sales career shape."""
    flat = _flat(prompt_text)
    return "scored against one profile shape" in flat and "account executive" in flat


def test_the_anchor_detector_is_not_vacuous() -> None:
    assert _one_career_anchors(
        "WORKED ANCHORS — five role examples, each scored against one profile shape: "
        "about 2.5 yrs as a mid-market account executive"
    )
    assert not _one_career_anchors("WORKED ANCHORS — one per role track, each its own profile.")


def test_sales_tuned_scoring_is_disclosed_while_the_anchors_are_one_sales_career() -> None:
    if not _one_career_anchors(SCORE_PROMPT.read_text()):
        return  # the owner replaced the anchors; the notes may go
    readme = (REPO_ROOT / "README.md").read_text()
    get_your_own = _flat(_section(readme, "## Get your own jobkit"))
    assert "### tuned for sales roles" in get_your_own
    assert SALES_NOTE in get_your_own
    assert "prompts/score_fit.md" in get_your_own
    setup = (SKILLS / "jobkit-setup" / "SKILL.md").read_text()
    step_4 = _flat(_section(setup, "## Step 4 of 8 — What they want (preferences)"))
    assert SALES_NOTE in step_4
    assert '"tuned for sales roles"' in step_4  # points at the README section


def _shipped_text(paths: list[Path]) -> dict[str, str]:
    return {str(p.relative_to(REPO_ROOT)): p.read_text() for p in paths if p.is_file()}


def test_skills_refuse_to_build_auto_submit_while_it_does_not_ship() -> None:
    if SUBMIT_MODULE.exists():
        return  # the owner shipped it; the skill lines change with it
    for name in ("jobkit-setup", "jobkit-daily"):
        text = _flat((SKILLS / name / "SKILL.md").read_text())
        assert [n for n in NO_SUBMIT_NOTES if n not in text] == [], name
    # what the skills state stays true: no shipped code or template has the setting
    shipped = _shipped_text(
        [
            *(REPO_ROOT / "src").rglob("*.py"),
            *(REPO_ROOT / "config").glob("*.example.yaml"),
            *(REPO_ROOT / "data").glob("*.example.yaml"),
        ]
    )
    assert shipped, "no shipped files found"
    assert [path for path, text in shipped.items() if "auto_submit" in text] == []
