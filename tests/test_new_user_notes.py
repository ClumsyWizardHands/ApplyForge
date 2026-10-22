"""What a new user reads stays true to the project's decisions.

* Submit: the person always presses submit. There is no submit module and no
  `auto_submit` setting, CLAUDE.md hard rule 1 says the browser never clicks
  submit, and the setup and daily skills give the consent reason when someone
  asks jobkit to submit for them.
* The README is a product page: the banner with the owner's tagline, the
  automatic daily flow (find, score, approve what clears your bar, write, open
  the filled form, you click submit, replies and job alerts from your email),
  who ApplyForge suits and what it does on each kind of job system today, why
  you click submit (with the author's prediction, in the author's words),
  email replies, the roadmap, and the support note, in the author's words.
* A daily limit on automatic approval is optional: none by default, and no
  page presents one as part of the default flow.
* It reads as an evolving project, stated plainly: tested on the author's Mac,
  pain points and ideas welcome through the issue forms, and the hedges the
  author asked to drop stay gone. The principles are stated firmly, here and
  on the roadmap.
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
ISSUE_FORMS = REPO_ROOT / ".github" / "ISSUE_TEMPLATE"
ASSETS = REPO_ROOT / "assets"

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
    "roadmap.md",
    "changelog.md",
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

# The owner's tagline (2026-10-22), and the one it replaced
TAGLINE = "Your job search, automated. All you do is click submit."
OLD_TAGLINE = "run from claude. you press submit"

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
# Hedges the author asked to drop (2026-10-22): the platform line says what it
# was tested on, and a plain limit isn't an apology. (Setup's "about an hour"
# is plain information, not a hedge, and stays.)
HEDGES = (
    "windows is untested",
    "only as good as your resume",
    "the risk you take on",
    "close every open claude session",
    "experimental parser",
    "currently broken",
    "not supported yet",
    "planned, not built",
)

# The principles, stated on the roadmap and in the README's roadmap section
PRINCIPLE_WORDS = (
    "click submit",
    "never solves or gets around",
    "facts you confirmed",
    "read-only",
    "stay on your computer",
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
    # a product page, not a manual: detail belongs in docs/. The ceiling leaves
    # room for the README to grow with the project without becoming a manual.
    assert 120 <= len(lines) <= 400, len(lines)
    headings = [line for line in lines if line.startswith("## ")]
    for heading in (
        "## What it does",
        "## Who it suits",
        "## Get started",
        "## Why you click submit",
        "## Replies in your email (optional)",
        "## Where your data is kept",
        "## LinkedIn",
        "## Costs",
        "## Roadmap",
        "## Support",
    ):
        assert heading in headings, heading
    # no build history in the product page
    flat = _flat(README.read_text())
    assert not re.search(r"review 20\d\d-\d\d-\d\d|\bm0b\b|\bm[1-7] —", flat)


def test_readme_names_the_command_once_as_jobkit() -> None:
    first = _flat(README.read_text().split("\n## ", 1)[0])
    assert 'alt="applyforge' in first  # the banner carries the name
    assert "the command and the python package are called `jobkit`" in first
    assert "evolving project" in first
    assert "i built applyforge for my own job search" in first


def test_readme_banner_has_both_themes_and_stays_self_contained() -> None:
    text = README.read_text()
    for theme in ("light", "dark"):
        name = f"assets/banner-{theme}.svg"
        assert name in text, name
        svg = (REPO_ROOT / name).read_text()
        assert svg.startswith("<svg") and "<title" in svg and "<desc" in svg
        # no script, no external fetch: it renders the same everywhere
        assert "<script" not in svg and "href=" not in svg and "@import" not in svg
    # only the two banners ship (a Finder .DS_Store is gitignored)
    shipped = sorted(p.name for p in ASSETS.iterdir() if not p.name.startswith("."))
    assert shipped == ["banner-dark.svg", "banner-light.svg"]


def test_the_banner_carries_the_owners_tagline() -> None:
    for theme in ("light", "dark"):
        svg = (ASSETS / f"banner-{theme}.svg").read_text()
        assert f">{TAGLINE}</text>" in svg, theme
        assert TAGLINE.lower() in _flat(svg.split("<desc", 1)[1].split("</desc>", 1)[0])
    assert f'alt="ApplyForge: {TAGLINE[0].lower()}{TAGLINE[1:]}"' in README.read_text()
    pages = [*USER_PAGES, SPEC, ONBOARDING, *SKILL_FILES, *ASSETS.glob("*.svg")]
    assert [_rel(p) for p in pages if OLD_TAGLINE in _flat(p.read_text())] == []


def test_readme_describes_the_automatic_day_in_order() -> None:
    day = _section(README.read_text(), "## A normal day")
    flat = _flat(day)
    steps = (
        "it runs every day on its own",
        "it finds and scores jobs",
        "it approves the ones that clear the bar you set in setup",
        "it writes the resume and letter",
        "it opens each filled application",
        "you review as much as you like and click submit",
        "replies are tracked from your email",
        "your linkedin job alerts and messages are checked too",
    )
    at = [flat.find(s) for s in steps]
    assert -1 not in at and at == sorted(at), dict(zip(steps, at, strict=True))
    assert "limit" not in flat  # an optional daily limit is no part of the day
    # the email steps say they need a connected email
    assert flat.count("if you connected it") + flat.count("if you connected your email") == 2
    assert "you can still approve or reject anything yourself" in flat
    # the alert flow: looked up on a board ApplyForge can apply through, never a
    # LinkedIn row; web search for alert jobs is not part of the day
    assert "employer's own greenhouse, lever or ashby board" in flat
    assert "is scored the same day when it's there" in flat
    assert "scored the same day and approved if it clears your bar" in flat
    assert "web search" not in flat
    assert "lands in your queue" not in flat
    # the diagram: the only human step is reviewing and clicking submit
    mermaid = day[day.index("```mermaid") : day.index("```", day.index("```mermaid") + 3)]
    assert re.findall(r"^\s*class (\S+) you$", mermaid, re.M) == ["G"]
    assert 'G(["You review<br/>and click submit"])' in mermaid
    assert "Clears your bar<br/>approved automatically" in mermaid
    assert "You approve" not in mermaid


def test_a_daily_limit_is_never_part_of_the_default_flow() -> None:
    text = README.read_text()
    assert "max_auto_approve_per_day" not in text
    flat = _flat(text)
    # the README never mentions one: the optional limit lives in docs/setup.md
    # and docs/how-it-works.md
    assert "daily limit" not in flat
    assert "limit" not in _flat(_section(text, "## What it does"))
    # the template ships it commented out; the docs say none by default
    template = (REPO_ROOT / "config" / "autoapprove.example.yaml").read_text()
    assert "\n# max_auto_approve_per_day:" in template
    assert "\nmax_auto_approve_per_day:" not in template
    how = _flat((DOCS / "how-it-works.md").read_text())
    assert "no daily limit by default" in how


def test_readme_says_where_it_was_tested() -> None:
    start = _flat(_section(README.read_text(), "## Get started"))
    assert "on my mac" in start
    assert "linux" in start and "windows" in start
    assert "you need a mac or linux" not in start


def test_readme_says_what_it_does_on_each_job_system() -> None:
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
    assert "workable and smartrecruiters" in suits  # on the roadmap
    assert "(#roadmap)" in suits


def test_older_configs_are_told_how_to_switch_scoring() -> None:
    # an install from before any-field scoring has no prompt_set line and keeps
    # the sales examples; the changelog says how to switch
    flat = _flat((DOCS / "changelog.md").read_text())
    assert "prompt_set" in flat and "jobkit rescore" in flat


def test_roadmap_states_the_principles_and_the_openai_work() -> None:
    page = _flat((DOCS / "roadmap.md").read_text())
    section = _flat(_section(README.read_text(), "## Roadmap"))
    for word in PRINCIPLE_WORDS:
        assert word in page, word
        assert word in section, word
    # LinkedIn stays logged out ("logged" alone would pass "logged in")
    assert re.search(r"logged[- ]out", page) and re.search(r"logged[- ]out", section)
    assert "evolving" in page and "time and resources" in page
    for item in ("codex", "dots", "agents.md", "workable", "smartrecruiters", "linkedin"):
        assert item in page, item
    assert "(docs/roadmap.md)" in section
    for status in ("`planned`", "`exploring`", "`idea`"):
        assert status in page, status
    assert "## sources" in page  # the OpenAI facts carry their sources
    # LinkedIn job-alert emails were built (2026-10-22)
    assert "`done` read the linkedin job-alert emails" in page
    assert "job-alert emails and messages, read-only (done)" in section


def test_issue_forms_invite_pain_points_and_ideas() -> None:
    forms = {p.name: p.read_text() for p in ISSUE_FORMS.glob("*.yml")}
    assert set(forms) == {"pain-point.yml", "idea.yml", "config.yml"}
    assert "blank_issues_enabled: true" in forms["config.yml"]
    assert "ko-fi.com/clumsywizardhands" in forms["config.yml"]
    assert "leave out your resume" in _flat(forms["pain-point.yml"])
    # doctor prints the contact email: both pages say to remove it before pasting
    assert "email address" in _flat(forms["pain-point.yml"])
    assert "remove the email" in _flat(forms["pain-point.yml"])
    contributing = _flat((DOCS / "contributing.md").read_text())
    assert "email address" in contributing and "remove the email" in contributing
    assert "you still click submit" in _flat(forms["idea.yml"])
    assert "## pain points and ideas" in _flat((DOCS / "contributing.md").read_text())


@pytest.mark.parametrize(
    "page",
    [*USER_PAGES, ONBOARDING, *SKILL_FILES, *sorted((REPO_ROOT / "config").glob("*.example.yaml"))],
    ids=_rel,
)
def test_dropped_hedges_stay_gone(page: Path) -> None:
    flat = _flat(page.read_text())
    assert [h for h in HEDGES if h in flat] == []


def test_why_you_click_submit_gives_consent_the_key_fact_and_the_owners_guess() -> None:
    text = _section(README.read_text(), "## Why you click submit")
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
