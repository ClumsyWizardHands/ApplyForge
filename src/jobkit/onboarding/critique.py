"""Resume critique (ONBOARDING.md 1.4): blunt, specific, prioritized.

A SEPARATE output, written to data/resume_critique.md. Nothing in it is ever
copied into the corpus draft (hard rule 6): a suggestion becomes corpus text
only when the human says the words in the confirmation step.

Deterministic checks on the parsed resume, no LLM: bullets with no outcome or
number, scope-ambiguous openers, verb monotony, stale weighting, roles with no
bullets, ATS-hostile formatting seen during extraction, missing contact
details and a thin corpus. Each finding carries the question to ask, so the
interview (ONBOARDING.md 1.5) can recover the missing numbers."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from jobkit.onboarding.resume_parse import Extraction, ParsedResume, ParsedRole, heading_kind

THIN_CORPUS_BULLETS = 30
_NUMBER = re.compile(r"\d")
_WEAK_OPENERS = re.compile(
    r"^(?:responsible for|worked on|working on|helped|helping|assisted|assisting|involved in"
    r"|participated in|supported|supporting|contributed to|tasked with|duties included"
    r"|in charge of|exposure to|familiar with)\b",
    re.I,
)
_WARNING_TEXT = {
    "table": (
        "Text sits in a table. Many applicant tracking systems read tables out of order "
        "or skip them. Put the content in plain paragraphs and bullets."
    ),
    "header_footer": (
        "The page header or footer carries content (often name and contact details). "
        "ATS parsers commonly skip headers and footers. Move it into the body."
    ),
}


@dataclass(frozen=True)
class Finding:
    priority: int  # lower is more important
    title: str
    detail: list[str] = field(default_factory=list)


def _first_word(text: str) -> str:
    m = re.match(r"[A-Za-z][A-Za-z'-]*", text)
    return m.group(0).lower() if m else ""


def findings(resume: ParsedResume, extraction: Extraction) -> list[Finding]:
    out: list[Finding] = []
    bullets = [(r, b) for r in resume.roles for b in r.bullets]
    total = len(bullets)

    problem = extraction.problem()
    if problem:
        out.append(
            Finding(
                0,
                "The resume text did not extract cleanly",
                [
                    f"In the extracted text, {problem}. An ATS will likely garble it the "
                    "same way: export a text-based PDF or a DOCX (not a scan or an image)."
                ],
            )
        )
    for w in extraction.warnings:
        if w in _WARNING_TEXT:
            out.append(Finding(1, "Formatting that breaks ATS parsing", [_WARNING_TEXT[w]]))
    if "experience" not in {heading_kind(h) for h in resume.headings}:
        out.append(
            Finding(
                1,
                "No standard Experience heading",
                [
                    "ATS parsers find roles by the section heading. Use a plain heading such as "
                    "'Experience' or 'Work Experience'."
                ],
            )
        )

    no_number = [(r, b) for r, b in bullets if not _NUMBER.search(b)]
    if no_number:
        detail = [
            f"{len(no_number)} of {total} bullets state no number or outcome. For each: what "
            "changed because of it? Roughly how many, over what period, compared to what before?"
        ]
        detail += [f'"{b}"' for _, b in no_number[:12]]
        if len(no_number) > 12:
            detail.append(f"... and {len(no_number) - 12} more")
        out.append(Finding(2, "Bullets with no outcome", detail))

    weak = [b for _, b in bullets if _WEAK_OPENERS.match(b)]
    if weak:
        out.append(
            Finding(
                3,
                "Scope is ambiguous",
                [
                    "These say you were near the work, not what you owned. Did you own it, "
                    "lead it, or contribute one part? Say which, and what came of it."
                ]
                + [f'"{b}"' for b in weak[:8]],
            )
        )

    firsts = Counter(_first_word(b) for _, b in bullets if _first_word(b))
    repeated = [(w, n) for w, n in firsts.most_common() if n >= 3 and n / max(total, 1) >= 0.2]
    if repeated:
        out.append(
            Finding(
                4,
                "Verb monotony",
                [
                    ", ".join(f"{n} bullets start with '{w.capitalize()}'" for w, n in repeated)
                    + ". Vary the verbs so each line reads as a different kind of work."
                ],
            )
        )

    if len(resume.roles) >= 2:
        newest = resume.roles[0]
        heavier = [
            r for r in resume.roles[1:] if len(r.bullets) > len(newest.bullets) and r.bullets
        ]
        if heavier:
            out.append(
                Finding(
                    4,
                    "Stale weighting",
                    [
                        f"The first role listed ({_role_name(newest)}) has "
                        f"{len(newest.bullets)} bullets; "
                        + "; ".join(f"{_role_name(r)} has {len(r.bullets)}" for r in heavier)
                        + ". A reader weighs your most recent work most."
                    ],
                )
            )

    empty = [r for r in resume.roles if not r.bullets]
    if empty:
        out.append(
            Finding(
                5,
                "Roles with no bullets",
                [f"{_role_name(r)}: nothing says what you did there." for r in empty],
            )
        )

    long_ones = [b for _, b in bullets if len(b.split()) > 40]
    if long_ones:
        out.append(
            Finding(
                6,
                "Bullets too long to scan",
                ["Over 40 words; a reader skims these:"] + [f'"{b}"' for b in long_ones[:5]],
            )
        )

    missing = [
        label
        for label, value in (
            ("email", resume.email),
            ("phone", resume.phone),
            ("LinkedIn URL", resume.links.get("linkedin")),
        )
        if not value
    ]
    if missing:
        out.append(
            Finding(6, "Contact details not found", [f"No {', '.join(missing)} at the top."])
        )

    with_number = total - len(no_number)
    out.append(
        Finding(
            7,
            "Evidence for every number",
            [
                f"{with_number} bullets state a figure. In the confirmation step you will say, "
                "for each, whether you can point at a source (verified) or it is your honest "
                "approximation (estimate), and where it comes from. A number you cannot source "
                "either gets a source or gets cut."
            ],
        )
    )
    if total < THIN_CORPUS_BULLETS:
        out.append(
            Finding(
                2,
                "Thin corpus",
                [
                    f"{total} bullets. Under {THIN_CORPUS_BULLETS}, tailored resumes come out thin "
                    "whatever the tailoring does. The interview will ask for accomplishments "
                    "the resume leaves out; expect to add 8-12."
                ],
            )
        )
    return sorted(out, key=lambda f: f.priority)


def _role_name(role: ParsedRole) -> str:
    return f"{role.title or '?'} at {role.company or '?'}"


def render(resume: ParsedResume, extraction: Extraction, source_name: str) -> str:
    items = findings(resume, extraction)
    lines = [
        f"# Resume critique — {source_name}",
        "",
        "Suggestions only. Nothing on this page is in your corpus draft",
        "(data/career_corpus.draft.yaml): the draft holds your resume's own words.",
        "A change here reaches the corpus only when you say it in the confirmation step.",
        "",
        "## Top five",
        "",
    ]
    for i, f in enumerate(items[:5], 1):
        lines += _render_finding(i, f)
    if items[5:]:
        lines += ["## The rest", ""]
        for i, f in enumerate(items[5:], 6):
            lines += _render_finding(i, f)
    return "\n".join(lines).rstrip() + "\n"


def _render_finding(n: int, f: Finding) -> list[str]:
    out = [f"{n}. **{f.title}**"]
    out += [f"   - {d}" for d in f.detail]
    return out + [""]
