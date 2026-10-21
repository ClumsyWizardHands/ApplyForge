"""What a new user reads stays true to the project's decisions.

* Submit: the person always presses submit. There is no submit module and no
  `auto_submit` setting, CLAUDE.md hard rule 1 says the browser never clicks
  submit, and the setup and daily skills give the consent reason when someone
  asks jobkit to submit for them.
* The README is a short product page: who ApplyForge suits and where its
  discovery is weaker, why you press submit (with the author's prediction, in
  the author's words), email replies, and the support note, in the author's words.
* The reference material lives under docs/, and every relative link between
  these pages resolves.
* Wording that the decisions retired ("calibrated for sales roles", "never
  presses submit", "technical fact", follow-up drafts) is gone from every page
  a user or a user's Claude reads.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT

README = REPO_ROOT / "README.md"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
SPEC = REPO_ROOT / "SPEC.md"
ONBOARDING = REPO_ROOT / "ONBOARDING.md"
DOCS = REPO_ROOT / "docs"
SKILLS = REPO_ROOT / ".claude" / "skills"
SUBMIT_MODULE = REPO_ROOT / "src" / "jobkit" / "autofill" / "submit.py"
FUNDING = REPO_ROOT / ".github" / "FUNDING.yml"

# docs/ pages that ship (local working notes are gitignored: docs/local/ and
# the dated audit names, see .gitignore)
_LOCAL_NOTE = re.compile(r"^(audit-|residual-issues-)|-proposed-|^persona-mapping\.md$")
DOC_PAGES = sorted(p for p in DOCS.glob("*.md") if not _LOCAL_NOTE.search(p.name))
USER_PAGES = [README, *DOC_PAGES]
SKILL_FILES = sorted(SKILLS.glob("*/SKILL.md"))

NEW_DOCS = (
    "setup.md",
    "how-it-works.md",
    "validators.md",
    "form-filling.md",
    "commands.md",
    "email-responses.md",
    "why-no-linkedin.md",
    "contributing.md",
)

# the author's prediction, word for word
PREDICTION = (
    "My guess at where this goes: as more applications come from bots, more employers "
    "will put checks on their forms and screen out what looks mass-produced. An "
    "application you read and sent yourself should hold up better whichever way that "
    "goes. If employers ever offer an approved way for software to apply on your "
    "behalf, that's consent, and ApplyForge could use it."
)

# the author's support note, word for word
SUPPORT = (
    "Your support means a lot: https://ko-fi.com/clumsywizardhands\n"
    "\n"
    "And to those of you coming to this page experiencing similar struggles to mine, "
    "may you find your way through the dark.\n"
    "\n"
    "Live long and prosper, friends."
)

# The answer the setup and daily skills give when asked to submit
CONSENT = (
    "jobkit leaves submit to the person, because a bot check on an application form "
    "is the employer asking for a person to send it, and no ATS offers applicants a "
    "submit API. Do not build, enable or configure automatic submission."
)

RETIRED = (
    "calibrated for sales roles",
    "tuned for sales roles",
    "never presses submit",
    "technical fact",
    "technically forced",
    "follow-up draft",
)
# removed with follow-up drafting; SPEC.md's amendment names them as removed
RETIRED_TOOLS = ("get_followups", "run_followups", "mark_followup_sent", "jobkit followups")


def _flat(text: str) -> str:
    return " ".join(text.lower().split())


def _section(text: str, heading: str) -> str:
    """From the `heading` line to the next heading of the same level."""
    level = heading.split(" ", 1)[0]
    start = text.index(f"\n{heading}\n")
    end = text.find(f"\n{level} ", start + len(heading) + 2)
    return text[start : end if end != -1 else len(text)]


def _rel(p: Path) -> str:
    return str(p.relative_to(REPO_ROOT))


# ---- submit: the person presses it ----


def test_there_is_no_submit_module_and_no_setting() -> None:
    assert not SUBMIT_MODULE.exists()
    shipped = [
        *(
            p
            for p in (REPO_ROOT / "src").rglob("*.py")
            if not any(part.startswith(".") for part in p.relative_to(REPO_ROOT).parts)
        ),
        *(REPO_ROOT / "config").glob("*.example.yaml"),
        *(REPO_ROOT / "data").glob("*.example.yaml"),
        CLAUDE_MD,
        SPEC,
        ONBOARDING,
        *USER_PAGES,
        *SKILL_FILES,
    ]
    hits = {
        _rel(p): word
        for p in shipped
        if p.is_file()
        for word in ("auto_submit", "submit.py")
        if word in p.read_text()
    }
    assert hits == {}


def test_hard_rule_1_is_the_consent_rule() -> None:
    text = CLAUDE_MD.read_text()
    rule = text[text.index("1. **") : text.index("2. **Never solve a CAPTCHA")]
    assert rule.startswith("1. **The browser never clicks submit.**")
    flat = _flat(rule)
    assert "(spec.md §8)" in flat  # the history lives in SPEC.md §8 and §15
    assert "2026-10" not in flat  # no dated dev-log note in a file every session loads
    assert "the employer asking for a person to submit" in flat
    assert "employer's key" in flat
    assert "no file is exempt" in flat


def test_claude_md_states_that_email_is_read_only() -> None:
    flat = _flat(CLAUDE_MD.read_text())
    assert "**email is read-only.**" in flat
    assert "email connector" in flat
    assert "never the body" in flat


@pytest.mark.parametrize("name", ["jobkit-setup", "jobkit-daily"])
def test_skills_give_the_consent_reason(name: str) -> None:
    assert _flat(CONSENT) in _flat((SKILLS / name / "SKILL.md").read_text())


def test_spec_version_is_the_one_claude_md_names() -> None:
    m = re.search(r"\*\*Version:\*\* (\d+\.\d+)", SPEC.read_text())
    assert m, "SPEC.md has no version line"
    assert f"`SPEC.md` (v{m.group(1)})" in CLAUDE_MD.read_text()
    assert m.group(1) == "1.5"


def test_spec_section_8_is_consent_plus_the_employer_key_fact() -> None:
    s8 = _flat(_section(SPEC.read_text(), "## 8. Why the person presses submit"))
    assert "consent" in s8
    assert "issued to the hiring company" in s8
    for ats in ("greenhouse", "lever", "ashby", "smartrecruiters"):
        assert ats in s8, ats


# ---- the README ----


def test_readme_is_a_short_product_page() -> None:
    lines = README.read_text().splitlines()
    assert 120 <= len(lines) <= 260, len(lines)
    headings = [line for line in lines if line.startswith("## ")]
    for heading in (
        "## What it does",
        "## Who it suits",
        "## Get started",
        "## Why you press submit",
        "## Replies in your email (optional)",
        "## Where your data is kept",
        "## LinkedIn",
        "## Costs",
        "## Support",
    ):
        assert heading in headings, heading
    # no build history in the product page
    flat = _flat(README.read_text())
    assert not re.search(r"review 20\d\d-\d\d-\d\d|\bm0b\b|\bm[1-7] —", flat)


def test_readme_names_the_command_once_as_jobkit() -> None:
    first = _flat(README.read_text().split("\n## ", 1)[0])
    assert first.startswith("# applyforge")
    assert "the command and the python package are called `jobkit`" in first


def test_readme_says_where_discovery_is_strong_and_weak() -> None:
    suits = _flat(_section(README.read_text(), "## Who it suits"))
    assert "any job titles" in suits
    for system in ("greenhouse", "lever", "ashby"):
        assert system in suits, system
    for system in ("workday", "icims", "taleo", "successfactors"):
        assert system in suits, system
    for audience in ("hospitals", "schools", "government"):
        assert audience in suits, audience
    for fallback in ("paste the job link", "adzuna", "usajobs", "linkedin"):
        assert fallback in suits, fallback
    assert "workable and smartrecruiters" in suits  # the roadmap
    assert "prompt_set" in suits  # older configs keep the sales scoring


def test_why_you_press_submit_gives_consent_the_key_fact_and_the_owners_guess() -> None:
    text = _section(README.read_text(), "## Why you press submit")
    flat = _flat(text)
    assert "the employer asking for a person to send the application" in flat
    assert "invisible" in flat
    assert "only with a key issued to the employer" in flat
    assert PREDICTION in text


def test_support_note_is_the_owners_words() -> None:
    support = _section(README.read_text(), "## Support")
    assert support.strip() == "## Support\n\n" + SUPPORT
    assert FUNDING.read_text() == "ko_fi: clumsywizardhands\n"


def test_readme_points_at_the_email_and_linkedin_pages() -> None:
    text = README.read_text()
    assert "(docs/email-responses.md)" in text
    assert "(docs/why-no-linkedin.md)" in text


# ---- the docs ----


@pytest.mark.parametrize("name", NEW_DOCS)
def test_reference_page_exists(name: str) -> None:
    text = (DOCS / name).read_text()
    assert text.startswith("# ")


def test_known_wrongful_holds_moved_with_the_filters() -> None:
    text = (DOCS / "how-it-works.md").read_text()
    assert "**Known wrongful holds**" in text


def test_form_filling_names_only_the_systems_it_fills() -> None:
    which = _flat(_section((DOCS / "form-filling.md").read_text(), "## Which forms"))
    assert "greenhouse, lever and ashby" in which
    assert "dom-scrapeable" not in _flat((DOCS / "form-filling.md").read_text())


def test_email_page_is_read_only() -> None:
    flat = _flat((DOCS / "email-responses.md").read_text())
    assert "never connects to your mailbox" in flat
    assert "body of the email is not stored" in flat
    for tool in ("email_targets", "match_email", "record_email_response", "finish_email_check"):
        assert tool in flat, tool


def test_commands_page_lists_every_claude_tool() -> None:
    import asyncio

    from jobkit import mcp_server

    tools = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert len(tools) > 40
    text = (DOCS / "commands.md").read_text()
    missing = sorted(t for t in tools if f"`{t}(" not in text)
    assert missing == []


def test_commands_page_lists_every_cli_command() -> None:
    import typer

    from jobkit import cli

    root = typer.main.get_command(cli.app)
    names = sorted(root.commands)  # type: ignore[attr-defined]
    text = (DOCS / "commands.md").read_text()
    missing = [n for n in names if f"jobkit {n}" not in text]
    assert missing == []


# ---- links and retired wording ----

_LINK = re.compile(r"\]\(([^)\s]+)\)")


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading."""
    text = heading.lstrip("#").strip().lower().replace("`", "")
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    out: set[str] = set()
    in_code = False
    for line in path.read_text().splitlines():
        if line.startswith("```"):
            in_code = not in_code
        elif not in_code and line.startswith("#"):
            out.add(_slug(line))
    return out


@pytest.mark.parametrize("page", USER_PAGES, ids=_rel)
def test_relative_links_resolve(page: Path) -> None:
    broken = []
    for target in _LINK.findall(page.read_text()):
        if re.match(r"[a-z]+:", target):
            continue  # https:, mailto:
        file_part, _, anchor = target.partition("#")
        dest = (page.parent / file_part).resolve() if file_part else page
        if not dest.is_file() or (anchor and anchor not in _anchors(dest)):
            broken.append(target)
    assert broken == []


def test_the_slug_matches_github() -> None:
    assert _slug("## Finding the employer's own posting") == "finding-the-employers-own-posting"
    assert _slug("### Questions for you") == "questions-for-you"
    assert _slug("## Replies in your email (optional)") == "replies-in-your-email-optional"


def test_retired_wording_is_gone() -> None:
    pages = [*USER_PAGES, SPEC, ONBOARDING, CLAUDE_MD, *SKILL_FILES]
    hits = {_rel(p): phrase for p in pages for phrase in RETIRED if phrase in _flat(p.read_text())}
    assert hits == {}


def test_removed_follow_up_tools_are_not_offered() -> None:
    pages = [*USER_PAGES, ONBOARDING, CLAUDE_MD, *SKILL_FILES]
    hits = {_rel(p): name for p in pages for name in RETIRED_TOOLS if name in _flat(p.read_text())}
    assert hits == {}


def test_the_clone_line_is_the_same_everywhere() -> None:
    """The README and the setup page give one repository URL to clone."""
    urls = set()
    for page in ("README.md", "docs/setup.md"):
        text = (REPO_ROOT / page).read_text()
        urls |= set(re.findall(r"https://github\.com/ClumsyWizardHands/[\w.-]+", text))
    assert urls == {"https://github.com/ClumsyWizardHands/ApplyForge"}
