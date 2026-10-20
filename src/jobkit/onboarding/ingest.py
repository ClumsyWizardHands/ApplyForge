"""Phase 1 — resume ingest (ONBOARDING.md 1.1, 1.2, 1.4). MCP: ingest_resume.

Reads a resume (PDF, DOCX, HTML, plain text, or pasted text), writes:

  data/setup/resume_source.txt     the extracted text
  data/career_corpus.draft.yaml    the corpus DRAFT
  data/resume_critique.md          the critique (a separate file)

Hard rule 6: the draft contains only text from the resume. Each text field
(identity, roles, bullets, education, skills, the summary offered as a
positioning line) is a verbatim piece of the extracted text, and
`unverbatim()` checks that before anything is written: a draft with a single
string that is not in the source fails loudly and nothing is written.
`metric_confidence` and `evidence` are null in the draft — the human sets them
(corpus_confirm.confirm_bullets); nothing defaults them. Tags are suggested
only from the resume's own skills list, and `metric` holds only figures the
bullet itself states.

A garbled file extraction (low word score, or unreadable characters) is
refused with a request to paste the text: never a silent guess at a bad
parse. Pasted text is not word-scored (a resume dense with company and tool
names scores low without being garbled); it is refused only when too short
or full of unreadable characters, with a message that does not send the
person back to pasting the same text.

Each line of a resume summary (each bullet, or each paragraph) is its own
positioning candidate: lines are never joined across bullet markers or
other lines, so every candidate is a piece of the resume as written."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobkit.onboarding import critique
from jobkit.onboarding.files import (
    GENERATED_MARK,
    SetupError,
    SetupFiles,
    backup,
    dump_yaml,
    load_yaml,
    setup_files,
    today,
    write_text_atomic,
)
from jobkit.onboarding.resume_parse import (
    Extraction,
    ParsedResume,
    ResumeReadError,
    extract_text,
    extraction_from_paste,
    parse_resume,
)

DRAFT_VERSION = 1

# Draft keys whose values are bookkeeping or the human's own answers, not
# resume text: the verbatim check skips them.
META_KEYS = frozenset(
    {
        "draft_version",
        "source",
        "id",
        "role_id",
        "start",
        "end",
        "current",
        "confirmed",
        "notes",
        "status",
        "tags",
        "metric_confidence",
        "evidence",
        "archetype",
        "from_resume",
        "display_location",
        "home_city",
        "skill_index",
        "constraints",
        "voice_samples",
        "stories",
        "original_text",
    }
)

DRAFT_HEADER = f"""{GENERATED_MARK} — corpus DRAFT, not your corpus yet.
#
# Every text field below is copied from your resume (data/setup/resume_source.txt):
# nothing rewritten, improved or inferred (CLAUDE.md hard rule 6). Suggestions
# are in data/resume_critique.md, never here.
#
# metric_confidence and evidence are null: they are YOURS to set, bullet by
# bullet, in the confirmation step (MCP corpus_draft_status / confirm_bullets,
# CLI `jobkit setup draft` / `jobkit setup confirm`). Company, title and dates
# of each role are a best reading of the resume header: confirm or correct
# them. This draft becomes data/career_corpus.yaml only through
# promote_corpus, which refuses while anything is unconfirmed.
"""

_UNIT = (
    r"days?|weeks?|months?|years?|hours?|people|accounts?|customers?|clients?|reps?|users?"
    r"|deals?|tickets?|hires?|engineers?|countries|markets?|states?|cities|stores?"
    r"|locations?|partners?|projects?|products?|features?|points?|leads?|meetings?"
    r"|members?|employees?|students?|patients?|sites?|teams?|languages?"
)
_FIGURE = re.compile(
    r"(?<![A-Za-z0-9.])[~≈]?[$€£¥]?\d[\d,]*(?:\.\d+)?"
    r"(?:\s?(?:%|percent\b|[kKmMbB](?![A-Za-z])|x(?![A-Za-z])|million\b|billion\b|thousand\b))?"
    rf"\+?(?:\s(?:{_UNIT})\b)?"
)
_YEAR = re.compile(r"(?:19|20)\d{2}")


def metric_of(text: str) -> str | None:
    """The figures the bullet states, verbatim, joined with "; " — or None.
    A bare year is not a figure."""
    out: list[str] = []
    for m in _FIGURE.finditer(text):
        fig = m.group(0).strip()
        if _YEAR.fullmatch(fig):
            continue
        out.append(fig)
    return "; ".join(out) if out else None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:30]


def tag_slug(skill: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", skill.lower()).strip("-")


def suggest_tags(text: str, skills: list[str]) -> list[str]:
    """Skills from the resume's own skills list that the bullet names."""
    tags: list[str] = []
    for s in skills:
        if re.search(rf"(?<!\w){re.escape(s)}(?!\w)", text, re.I):
            slug = tag_slug(s)
            if slug and slug not in tags:
                tags.append(slug)
    return tags


def build_draft(
    parsed: ParsedResume, extraction: Extraction, source_name: str, sha256: str
) -> dict[str, Any]:
    roles: list[dict[str, Any]] = []
    used: set[str] = set()
    for i, r in enumerate(parsed.roles, 1):
        base = "role_" + (_slug(r.company or r.title or "") or str(i))
        rid, n = base, 2
        while rid in used:
            rid, n = f"{base}_{n}", n + 1
        used.add(rid)
        roles.append(
            {
                "id": rid,
                "header_lines": list(r.header_lines),
                "company": r.company,
                "title": r.title,
                "location": r.location,
                "dates": r.dates,
                "start": r.start,
                "end": r.end,
                "current": r.current,
                "confirmed": False,
                "notes": list(r.notes),
            }
        )
    bullets: list[dict[str, Any]] = []
    for parsed_role, role in zip(parsed.roles, roles, strict=True):
        for text in parsed_role.bullets:
            bullets.append(
                {
                    "id": f"b_{len(bullets) + 1:03d}",
                    "role_id": role["id"],
                    "text_long": text,
                    "text_short": text,
                    "tags": suggest_tags(text, parsed.skills),
                    "metric": metric_of(text),
                    "metric_confidence": None,
                    "evidence": None,
                    "status": "pending",
                }
            )
    # one entry per summary bullet or paragraph, each as written: the human
    # confirms the ones that position them and drops the rest
    positioning: list[dict[str, Any]] = [
        {
            "id": "pos_resume_summary" if n == 1 else f"pos_resume_summary_{n}",
            "archetype": None,
            "text": text,
            "from_resume": True,
            "confirmed": False,
        }
        for n, text in enumerate(parsed.summary, 1)
    ]
    return {
        "draft_version": DRAFT_VERSION,
        "source": {
            "file": source_name,
            "format": extraction.format,
            "method": extraction.method,
            "sha256": sha256,
            "word_score": round(extraction.word_score, 2),
            "text_file": "data/setup/resume_source.txt",
            "ingested": today(),
            "parse_notes": list(parsed.notes),
            "format_warnings": list(extraction.warnings),
        },
        "identity": {
            "name": parsed.name,
            "email": parsed.email,
            "phone": parsed.phone,
            "location_on_resume": parsed.location,
            # positioning choice and real home: asked, never taken from the resume
            "display_location": None,
            "home_city": None,
            "links": {
                "linkedin": parsed.links.get("linkedin", ""),
                "github": parsed.links.get("github", ""),
                "site": parsed.links.get("site", ""),
            },
            "confirmed": False,
        },
        "positioning": positioning,
        "roles": roles,
        "bullets": bullets,
        "education": [
            {
                "school": e.school,
                "degree": e.degree,
                "years": e.years,
                "lines": list(e.lines),
                "confirmed": False,
            }
            for e in parsed.education
        ],
        "skills_on_resume": list(parsed.skills),
        "other_sections": {k: list(v) for k, v in parsed.other_sections.items()},
        "skill_index": {},
        "constraints": {
            "never_claim_titles": [],
            "never_claim_tools": [],
            "never_claim_phrases": [],
        },
        "voice_samples": [],
        "stories": [],
    }


# ---------------------------------------------------------------------------
# the verbatim guarantee (hard rule 6)
# ---------------------------------------------------------------------------


def normalize_for_match(text: str) -> str:
    """Whitespace and line-wrap insensitive form used for the verbatim check."""
    s = unicodedata.normalize("NFKC", text)
    s = re.sub(r"-\s+", "-", s)
    return re.sub(r"\s+", " ", s).strip()


def draft_text_items(node: Any, path: str = "") -> list[tuple[str, str]]:
    """(path, text) for every resume-text string in the draft: META_KEYS are
    skipped, a `metric` is split into its figures."""
    out: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k in META_KEYS:
                continue
            sub = f"{path}.{k}" if path else str(k)
            if k == "metric" and isinstance(v, str):
                out += [(sub, part) for part in v.split("; ") if part.strip()]
            else:
                out += draft_text_items(v, sub)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += draft_text_items(v, f"{path}[{i}]")
    elif isinstance(node, str) and node.strip():
        out.append((path, node))
    return out


def unverbatim(draft: dict[str, Any], source_text: str) -> list[tuple[str, str]]:
    """Every (path, text) in the draft that is NOT in the source text. Empty
    for a draft that holds only resume text."""
    src = normalize_for_match(source_text)
    return [(p, t) for p, t in draft_text_items(draft) if normalize_for_match(t) not in src]


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


@dataclass
class IngestResult:
    files: SetupFiles
    source_name: str
    extraction: Extraction
    draft: dict[str, Any]
    critique_top: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        ex = self.extraction
        bullets = self.draft["bullets"]
        with_metric = sum(1 for b in bullets if b.get("metric"))
        how = (
            ""
            if ex.format == "pasted"
            else f" ({ex.format} via {ex.method}; {ex.word_score:.0%} recognizable words)"
        )
        lines = [
            f"Read {self.source_name}{how}.",
            f"Draft: {self.files.rel(self.files.corpus_draft)} — {len(self.draft['roles'])} "
            f"roles, {len(bullets)} bullets ({with_metric} state a figure), "
            f"{len(self.draft['education'])} education entries, "
            f"{len(self.draft['skills_on_resume'])} skills. Every text field is copied from "
            "the resume; metric_confidence and evidence are unset.",
            f"Critique: {self.files.rel(self.files.critique)} (separate; not in the draft). "
            "Top items: " + "; ".join(self.critique_top),
        ]
        identity = self.draft["identity"]
        lines.append(
            f"Identity read: name={identity['name']!r} email={identity['email']!r} "
            f"phone={identity['phone']!r} location on resume={identity['location_on_resume']!r}"
        )
        lines.append("Roles (confirm company, title and dates as you write them):")
        for r in self.draft["roles"]:
            dates = r["dates"] or "no dates"
            extra = f"  [{'; '.join(r['notes'])}]" if r["notes"] else ""
            lines.append(
                f"  {r['id']}: {r['title'] or '?'} at {r['company'] or '?'} ({dates}){extra}"
            )
        lines += [f"Note: {n}" for n in self.notes]
        lines.append(
            "Next: confirm identity (display location and home city are asked, not read), "
            "roles and education, then walk the bullets five at a time "
            "(corpus_draft_status, confirm_bullets)."
        )
        return "\n".join(lines)


def _draft_has_human_work(draft: Any) -> bool:
    if not isinstance(draft, dict):
        return False
    if (draft.get("identity") or {}).get("confirmed"):
        return True
    if any((r or {}).get("confirmed") for r in draft.get("roles") or []):
        return True
    return any((b or {}).get("status") not in (None, "pending") for b in draft.get("bullets") or [])


def ingest_resume(
    path: str | Path | None = None,
    *,
    text: str | None = None,
    replace_draft: bool = False,
    root: Path | None = None,
) -> IngestResult:
    """Read the resume and write the draft, the critique and the source text.

    `text`: the resume pasted as text, used instead of a file (the fix when a
    file does not extract cleanly). `replace_draft`: start over even though
    the current draft already holds confirmations (it is backed up first)."""
    files = setup_files(root)
    if text is not None and text.strip():
        extraction = extraction_from_paste(text)
        source_name = "pasted text"
        raw_bytes = extraction.text.encode()
    elif path:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = (files.root / p).resolve()
        try:
            extraction = extract_text(p)
        except ResumeReadError as e:
            raise SetupError(str(e)) from e
        source_name = p.name
        raw_bytes = p.read_bytes()
    else:
        raise SetupError("give the resume file's path, or paste the resume text")

    problem = extraction.problem()
    if problem and extraction.format == "pasted":
        # the fallback itself: never send the person back to pasting the same text
        raise SetupError(
            f"the pasted text cannot be used: {problem}. Paste the whole resume as plain "
            "text, copied from the original document (Word, Google Docs, Pages) rather than "
            "from a PDF viewer, which can turn a custom font into unreadable characters; or "
            "give the resume file's path."
        )
    if problem:
        raise SetupError(
            f"{source_name} did not extract cleanly ({problem}; "
            f"{len(extraction.text.strip())} characters). Rather than guess at a garbled "
            "parse, paste the resume's text (ingest_resume with text=...): pasted text is not "
            "scored against English words, so company, product and tool names do not count "
            "against it. Or export it again as a text-based PDF or DOCX."
        )

    existing = load_yaml(files.corpus_draft) if files.corpus_draft.is_file() else None
    if _draft_has_human_work(existing) and not replace_draft:
        raise SetupError(
            f"{files.rel(files.corpus_draft)} already holds your confirmations. Re-reading the "
            "resume would start them over. Continue with corpus_draft_status, or ask again with "
            "replace_draft=true (the current draft is copied to data/setup/backup/ first)."
        )

    parsed = parse_resume(extraction.text)
    if not any(r.bullets for r in parsed.roles):
        raise SetupError(
            f"no roles with bullets were found in {source_name}"
            + (f" ({'; '.join(parsed.notes)})" if parsed.notes else "")
            + ". Check that the resume has an Experience section, or paste its text."
        )
    sha = hashlib.sha256(raw_bytes).hexdigest()
    draft = build_draft(parsed, extraction, source_name, sha)
    bad = unverbatim(draft, extraction.text)
    if bad:
        # never write a draft with text the resume does not contain (hard rule 6)
        shown = "; ".join(f"{p}={t!r}" for p, t in bad[:5])
        raise SetupError(f"draft text not found in the resume, nothing written: {shown}")

    if files.corpus_draft.is_file():
        backup(files, files.corpus_draft)
    write_text_atomic(files.resume_text, extraction.text)
    write_text_atomic(files.corpus_draft, DRAFT_HEADER + "\n" + dump_yaml(draft))
    write_text_atomic(files.critique, critique.render(parsed, extraction, source_name))

    notes = list(parsed.notes)
    if files.corpus.is_file():
        notes.append(
            f"{files.rel(files.corpus)} already exists. It is untouched; promoting this draft "
            "will need replace_existing=true (the current corpus is backed up first)."
        )
    top = [f.title for f in critique.findings(parsed, extraction)[:5]]
    return IngestResult(
        files=files,
        source_name=source_name,
        extraction=extraction,
        draft=draft,
        critique_top=top,
        notes=notes,
    )
