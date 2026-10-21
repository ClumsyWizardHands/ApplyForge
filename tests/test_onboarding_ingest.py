"""Onboarding phase 1 — resume ingest (ONBOARDING.md 1.1-1.4).

The mandatory onboarding test (CLAUDE.md "Testing"): given a fixture resume,
the generated corpus draft contains no sentence absent from the source text.
Hard rule 6: no rewriting, no inferred metrics, metric_confidence and
evidence never defaulted, the critique kept out of the draft."""

from __future__ import annotations

import re
import unicodedata
import zipfile
import zlib
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import pytest
import yaml

from jobkit.onboarding import ingest, resume_parse
from jobkit.onboarding.files import GENERATED_MARK, SetupError
from jobkit.onboarding.ingest import ingest_resume, metric_of, unverbatim
from jobkit.onboarding.resume_parse import ParsedResume, ParsedRole
from tests.conftest import FIXTURES

RESUMES = FIXTURES / "resumes"
# A new user's resume: a second fictional person (Morgan Ellis, customer
# success), deliberately not the templates' example user, Jordan Rivera, so a
# value the draft takes from the template instead of the resume shows up.
TXT = RESUMES / "sample_resume.txt"
HTML = RESUMES / "sample_resume.html"

# draft keys that hold bookkeeping or the human's later answers, not resume text
BOOKKEEPING = {
    "draft_version",
    "source",
    "id",
    "role_id",
    "status",
    "start",
    "end",
    "current",
    "confirmed",
    "notes",
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
}


def _norm(text: str) -> str:
    s = unicodedata.normalize("NFKC", text)
    s = re.sub(r"-\s+", "-", s)
    return re.sub(r"\s+", " ", s).strip()


def _strings(node: Any, path: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k in BOOKKEEPING:
                continue
            if k == "metric" and isinstance(v, str):
                out += [(f"{path}.metric", p) for p in v.split("; ")]
            else:
                out += _strings(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += _strings(v, f"{path}[{i}]")
    elif isinstance(node, str) and node.strip():
        out.append((path, node))
    return out


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]


def _draft(home: Path) -> dict[str, Any]:
    data = yaml.safe_load((home / "data" / "career_corpus.draft.yaml").read_text())
    assert isinstance(data, dict)
    return data


def _assert_only_resume_text(draft: dict[str, Any], source: str) -> None:
    src = _norm(source)
    missing = [
        (path, sentence)
        for path, text in _strings(draft)
        for sentence in _sentences(text)
        if _norm(sentence) not in src
    ]
    assert missing == [], missing


# ---------------------------------------------------------------------------
# the mandatory onboarding test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", [TXT, HTML], ids=["txt", "html"])
def test_draft_contains_no_sentence_absent_from_the_resume(
    jobkit_home: Path, fixture: Path
) -> None:
    ingest_resume(fixture)
    draft = _draft(jobkit_home)
    source = (jobkit_home / "data" / "setup" / "resume_source.txt").read_text()
    _assert_only_resume_text(draft, source)
    # and the source is the resume itself, not something else
    raw = re.sub(r"<[^>]+>", " ", fixture.read_text())
    for b in draft["bullets"]:
        assert _norm(b["text_long"]) in _norm(raw)
    assert len(draft["bullets"]) == 8 and len(draft["roles"]) == 2


def test_confidence_and_evidence_are_never_defaulted(jobkit_home: Path) -> None:
    ingest_resume(TXT)
    draft = _draft(jobkit_home)
    for b in draft["bullets"]:
        assert b["metric_confidence"] is None
        assert b["evidence"] is None
        assert b["status"] == "pending"
    ident = draft["identity"]
    # positioning choices are asked, never read off the resume
    assert ident["display_location"] is None and ident["home_city"] is None
    assert ident["location_on_resume"] == "Denver, CO"
    assert not ident["confirmed"]
    assert all(not r["confirmed"] for r in draft["roles"])


def test_metrics_are_only_figures_the_bullet_states(jobkit_home: Path) -> None:
    ingest_resume(TXT)
    by_text = {b["text_long"]: b for b in _draft(jobkit_home)["bullets"]}
    assert by_text["Trained 9 new support hires."]["metric"] == "9"
    assert (
        by_text["Resolved 1,200+ tickets a year at a 96% satisfaction score."]["metric"]
        == "1,200+ tickets; 96%"
    )
    assert by_text["Led the renewal forecast for the Denver office."]["metric"] is None
    assert metric_of("Rebuilt the program in 2019 for the West team.") is None
    assert metric_of("Grew pipeline ~$2.3M and cut churn 4.5%.") == "~$2.3M; 4.5%"


def test_tags_come_only_from_the_resume_skills(jobkit_home: Path) -> None:
    ingest_resume(TXT)
    draft = _draft(jobkit_home)
    skills = {re.sub(r"[^a-z0-9]+", "-", s.lower()) for s in draft["skills_on_resume"]}
    for b in draft["bullets"]:
        assert set(b["tags"]) <= skills
    assert "onboarding" in next(b for b in draft["bullets"] if b["id"] == "b_002")["tags"]


def test_critique_is_a_separate_file(jobkit_home: Path) -> None:
    result = ingest_resume(TXT)
    critique = (jobkit_home / "data" / "resume_critique.md").read_text()
    draft_text = (jobkit_home / "data" / "career_corpus.draft.yaml").read_text()
    assert "Bullets with no outcome" in critique
    assert "Responsible for quarterly business reviews" in critique  # quoted, flagged
    assert "Scope is ambiguous" in critique and "Thin corpus" in critique
    for heading in ("Bullets with no outcome", "Scope is ambiguous", "Thin corpus", "Top five"):
        assert heading not in draft_text
    assert draft_text.startswith(GENERATED_MARK)
    assert "Bullets with no outcome" in result.render()


def test_roles_are_read_verbatim_with_dates_parsed(jobkit_home: Path) -> None:
    ingest_resume(TXT)
    roles = _draft(jobkit_home)["roles"]
    first, second = roles
    assert (first["company"], first["title"], first["location"]) == (
        "Fernbrook Software",
        "Customer Success Manager",
        "Denver, CO",
    )
    assert (first["start"], first["end"], first["current"]) == ("2021-06", None, True)
    assert (second["company"], second["title"]) == (
        "Harbor Health Systems",
        "Senior Support Specialist",
    )
    assert (second["start"], second["end"]) == ("2017-08", "2021-05")


def test_a_year_alone_is_never_given_a_month(jobkit_home: Path) -> None:
    text = TXT.read_text().replace("Aug 2017 – May 2021", "2017 – 2021")
    ingest_resume(text=text)
    role = _draft(jobkit_home)["roles"][1]
    assert role["dates"] == "2017 – 2021"
    assert role["start"] is None and role["end"] is None
    assert any("year only" in n for n in role["notes"])


# ---------------------------------------------------------------------------
# adversarial: an invented sentence never reaches the draft
# ---------------------------------------------------------------------------


def test_invented_text_is_refused_and_nothing_written(
    jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = resume_parse.parse_resume

    def embellish(text: str) -> ParsedResume:
        parsed = real(text)
        parsed.roles[0].bullets.append("Grew net revenue retention to 135% in two quarters.")
        return parsed

    monkeypatch.setattr(ingest, "parse_resume", embellish)
    with pytest.raises(SetupError, match="not found in the resume"):
        ingest_resume(TXT)
    assert not (jobkit_home / "data" / "career_corpus.draft.yaml").exists()


def test_a_rewritten_bullet_is_refused(jobkit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = resume_parse.parse_resume

    def improve(text: str) -> ParsedResume:
        parsed = real(text)
        role: ParsedRole = parsed.roles[1]
        role.bullets[1] = "Spearheaded the knowledge base migration to a new help center."
        return parsed

    monkeypatch.setattr(ingest, "parse_resume", improve)
    with pytest.raises(SetupError):
        ingest_resume(TXT)


def test_unverbatim_finds_an_invented_metric() -> None:
    source = TXT.read_text()
    draft = {"bullets": [{"text_long": "Trained 9 new support hires.", "metric": "9; 40%"}]}
    assert unverbatim(draft, source) == [("bullets[0].metric", "40%")]


# ---------------------------------------------------------------------------
# formats
# ---------------------------------------------------------------------------

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _p(text: str, bullet: bool = False) -> str:
    num = '<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
    runs = "<w:r><w:tab/></w:r>".join(
        f'<w:r><w:t xml:space="preserve">{escape(part)}</w:t></w:r>' for part in text.split("\t")
    )
    return f"<w:p>{num if bullet else ''}{runs}</w:p>"


def build_docx(path: Path, lines: list[tuple[str, bool]], header: str = "") -> Path:
    body = "".join(_p(t, b) for t, b in lines)
    doc = f'<?xml version="1.0"?><w:document xmlns:w="{_W}"><w:body>{body}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", doc)
        if header:
            z.writestr("word/header1.xml", f'<w:hdr xmlns:w="{_W}">{_p(header)}</w:hdr>')
    return path


def _docx_lines() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    for line in TXT.read_text().splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("• "):
            out.append((s[2:], True))
        elif out and out[-1][1] and s[0].islower():
            out[-1] = (out[-1][0] + " " + s, True)
        else:
            out.append((s, False))
    return out


def test_docx_resume(jobkit_home: Path, tmp_path: Path) -> None:
    path = build_docx(tmp_path / "resume.docx", _docx_lines())
    result = ingest_resume(path)
    draft = _draft(jobkit_home)
    assert result.extraction.format == "docx"
    assert len(draft["bullets"]) == 8
    assert draft["identity"]["name"] == "Morgan Ellis"
    _assert_only_resume_text(draft, (jobkit_home / "data/setup/resume_source.txt").read_text())


def test_docx_header_content_is_flagged(jobkit_home: Path, tmp_path: Path) -> None:
    lines = [(t, b) for t, b in _docx_lines() if not t.startswith("Morgan Ellis")]
    path = build_docx(tmp_path / "resume.docx", lines, header="Morgan Ellis")
    ingest_resume(path)
    critique = (jobkit_home / "data" / "resume_critique.md").read_text()
    assert "header or footer" in critique
    assert _draft(jobkit_home)["identity"]["name"] == "Morgan Ellis"


def _pdf(lines: list[bytes]) -> bytes:
    """A minimal PDF (Helvetica, one Flate-compressed content stream)."""
    ops = [b"BT /F1 10 Tf 72 760 Td 12 TL"]
    for ln in lines:
        esc = ln.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
        ops.append(b"(" + esc + b") Tj T*")
    ops.append(b"ET")
    content = zlib.compress(b"\n".join(ops))
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(content)
        + content
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


@pytest.fixture()
def builtin_pdf_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the stdlib PDF reader: no pypdf, no pdftotext (and no spawn)."""
    monkeypatch.setattr(resume_parse, "_load_pypdf", lambda: None)
    monkeypatch.setattr(resume_parse, "_pdftotext", lambda: None)


def test_pdf_resume_with_the_builtin_reader(
    jobkit_home: Path, tmp_path: Path, builtin_pdf_only: None
) -> None:
    lines = [ln.encode("cp1252") for ln in TXT.read_text().splitlines() if ln.strip()]
    path = tmp_path / "resume.pdf"
    path.write_bytes(_pdf(lines))
    result = ingest_resume(path)
    assert result.extraction.method == "builtin-pdf"
    draft = _draft(jobkit_home)
    assert len(draft["roles"]) == 2 and len(draft["bullets"]) == 8
    _assert_only_resume_text(draft, (jobkit_home / "data/setup/resume_source.txt").read_text())


def test_garbled_pdf_is_refused_with_a_request_to_paste(
    jobkit_home: Path, tmp_path: Path, builtin_pdf_only: None
) -> None:
    # glyph ids from a custom-encoded font come out as noise
    noise = [bytes((65 + (i * 7 + j * 13) % 26) for j in range(40)) for i in range(40)]
    path = tmp_path / "resume.pdf"
    path.write_bytes(_pdf(noise))
    with pytest.raises(SetupError, match="paste the resume's text"):
        ingest_resume(path)
    assert not (jobkit_home / "data" / "career_corpus.draft.yaml").exists()
    # the fix: the text, pasted
    ingest_resume(text=TXT.read_text())
    assert _draft(jobkit_home)["source"]["format"] == "pasted"


def test_unsupported_format_names_the_fix(jobkit_home: Path, tmp_path: Path) -> None:
    doc = tmp_path / "resume.doc"
    doc.write_bytes(b"\xd0\xcf\x11\xe0")
    with pytest.raises(SetupError, match="PDF or DOCX"):
        ingest_resume(doc)


def test_no_roles_found_is_refused(jobkit_home: Path) -> None:
    with pytest.raises(SetupError, match="no roles with bullets"):
        ingest_resume(text="Morgan Ellis\n" + "Just a long paragraph of words about me. " * 20)


# ---------------------------------------------------------------------------
# re-ingest
# ---------------------------------------------------------------------------


def test_reingest_never_discards_confirmations(jobkit_home: Path) -> None:
    from jobkit.onboarding.corpus_confirm import confirm_bullets

    ingest_resume(TXT)
    ingest_resume(TXT)  # nothing confirmed yet: a fresh read is fine
    confirm_bullets([{"id": "b_004", "action": "drop"}])
    with pytest.raises(SetupError, match="already holds your confirmations"):
        ingest_resume(TXT)
    ingest_resume(TXT, replace_draft=True)
    assert (jobkit_home / "data" / "setup" / "backup" / "career_corpus.draft.yaml.prev").is_file()
    assert all(b["status"] == "pending" for b in _draft(jobkit_home)["bullets"])


def test_existing_corpus_is_untouched_by_ingest(jobkit_home: Path) -> None:
    corpus = jobkit_home / "data" / "career_corpus.yaml"
    corpus.write_text("corpus_version: '1.0'\nbullets: []\n")
    result = ingest_resume(TXT)
    assert corpus.read_text() == "corpus_version: '1.0'\nbullets: []\n"
    assert any("replace_existing" in n for n in result.notes)


def test_a_resume_title_line_is_not_the_name() -> None:
    parsed = resume_parse.parse_resume("RESUME\n" + TXT.read_text())
    assert parsed.name == "Morgan Ellis"


# ---------------------------------------------------------------------------
# pasted text is the fallback: a clean resume full of tool names gets through
# ---------------------------------------------------------------------------

DATA_ENGINEER = RESUMES / "data_engineer_resume.txt"


def test_a_jargon_heavy_pasted_resume_is_accepted(jobkit_home: Path) -> None:
    """Company, product and tool names (Snowflake, dbt, PySpark) are not
    common English words: this clean resume scores under the bar a file's
    extraction must meet. Pasting is the fix a refused file is sent to, so
    pasted text is not refused on that score."""
    text = DATA_ENGINEER.read_text()
    assert resume_parse.word_score(text) < resume_parse.MIN_WORD_SCORE  # the case under test
    result = ingest_resume(text=text)
    draft = _draft(jobkit_home)
    assert result.extraction.format == "pasted"
    assert len(draft["roles"]) == 2 and len(draft["bullets"]) == 6
    assert draft["roles"][0]["company"] == "Copperline Analytics"
    _assert_only_resume_text(draft, (jobkit_home / "data/setup/resume_source.txt").read_text())
    assert result.render().startswith("Read pasted text.")
    # a plain-text file is read as written too: nothing to garble
    (jobkit_home / "resume.txt").write_text(text)
    assert ingest_resume("resume.txt").extraction.format == "text"


def test_a_jargon_heavy_pdf_is_refused_and_pasting_its_text_works(
    jobkit_home: Path, tmp_path: Path, builtin_pdf_only: None
) -> None:
    text = DATA_ENGINEER.read_text()
    path = tmp_path / "resume.pdf"
    path.write_bytes(_pdf([ln.encode("cp1252") for ln in text.splitlines() if ln.strip()]))
    with pytest.raises(SetupError, match="paste the resume's text") as exc:
        ingest_resume(path)
    assert "pasted text is not scored against English words" in str(exc.value)
    assert not (jobkit_home / "data" / "career_corpus.draft.yaml").exists()
    ingest_resume(text=text)  # the fix the message names works
    assert _draft(jobkit_home)["source"]["format"] == "pasted"


def test_garbled_or_short_pasted_text_is_refused_without_a_circular_fix(
    jobkit_home: Path,
) -> None:
    # what a PDF viewer copies out of a custom-encoded font: private-use glyphs
    def word(i: int, j: int) -> str:
        return "".join(chr(0xE000 + (i * 31 + j * 7 + k) % 400) for k in range(6))

    garbled = "\n".join(" ".join(word(i, j) for j in range(6)) for i in range(20))
    with pytest.raises(SetupError, match="pasted text cannot be used") as exc:
        ingest_resume(text=garbled)
    message = str(exc.value)
    assert "unreadable" in message and "original document" in message
    assert "paste the resume's text" not in message  # never back to the same step
    with pytest.raises(SetupError, match="only 22 characters"):
        ingest_resume(text="Morgan Ellis\nResume v2")
    assert not (jobkit_home / "data" / "career_corpus.draft.yaml").exists()
    assert resume_parse.garble_share(TXT.read_text()) == 0.0


def test_symbol_font_bullets_are_bullet_markers(jobkit_home: Path) -> None:
    """A PDF viewer copies Symbol-font bullets as private-use characters:
    they mark bullets (never part of the text) and are not garble."""
    text = TXT.read_text().replace("• ", "\uf0b7 ")
    ingest_resume(text=text)
    draft = _draft(jobkit_home)
    assert len(draft["bullets"]) == 8
    assert all("\uf0b7" not in b["text_long"] for b in draft["bullets"])


# ---------------------------------------------------------------------------
# summaries: each bullet or paragraph is its own verbatim positioning line
# ---------------------------------------------------------------------------

SUMMARY = (
    "SUMMARY\n"
    "Customer success leader who builds onboarding programs for mid-market software customers.\n"
)


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        (
            "SUMMARY\n"
            "• Customer success leader for B2B software teams.\n"
            "• Known for calm escalations and clean renewals.\n",
            [
                "Customer success leader for B2B software teams.",
                "Known for calm escalations and clean renewals.",
            ],
        ),
        (
            "SUMMARY OF QUALIFICATIONS\n"
            "- Eight years in customer success and support.\n"
            "- Built onboarding programs for mid-market software\n"
            "  customers and their implementation teams.\n",
            [
                "Eight years in customer success and support.",
                "Built onboarding programs for mid-market software customers and their "
                "implementation teams.",
            ],
        ),
        (
            "Summary\n"
            "Customer success leader who builds onboarding programs\n"
            "for mid-market software customers.\n",
            [
                "Customer success leader who builds onboarding programs for mid-market "
                "software customers."
            ],
        ),
    ],
    ids=["bulleted-summary", "summary-of-qualifications", "wrapped-paragraph"],
)
def test_summary_lines_are_positioning_candidates_as_written(
    jobkit_home: Path, block: str, expected: list[str]
) -> None:
    text = TXT.read_text()
    assert SUMMARY in text
    ingest_resume(text=text.replace(SUMMARY, block))
    draft = _draft(jobkit_home)
    assert [p["text"] for p in draft["positioning"]] == expected
    assert draft["positioning"][0]["id"] == "pos_resume_summary"
    assert all(p["from_resume"] and not p["confirmed"] for p in draft["positioning"])
    assert len(draft["bullets"]) == 8
    _assert_only_resume_text(draft, (jobkit_home / "data/setup/resume_source.txt").read_text())


def test_summary_lines_apart_in_the_resume_are_never_joined(jobkit_home: Path) -> None:
    """Two summary lines above the first heading with a contact line between
    them: two candidates, each a piece of the resume, never one string the
    resume does not contain."""
    from jobkit.onboarding.corpus_confirm import update_draft

    text = TXT.read_text()
    contact = (
        "Denver, CO | morgan.ellis@example.com | (555) 010-0142 | "
        "linkedin.com/in/morgan-ellis-example\n\n" + SUMMARY
    )
    assert contact in text
    first = (
        "Customer success leader who builds onboarding programs for mid-market software customers."
    )
    second = "Known for calm escalations and clean renewals across every account."
    between = "Denver, CO | morgan.ellis@example.com | (555) 010-0142"
    ingest_resume(text=text.replace(contact, f"{first}\n{between}\n{second}\n\n"))
    draft = _draft(jobkit_home)
    assert [p["text"] for p in draft["positioning"]] == [first, second]
    assert [p["id"] for p in draft["positioning"]] == ["pos_resume_summary", "pos_resume_summary_2"]
    assert draft["identity"]["email"] == "morgan.ellis@example.com"
    _assert_only_resume_text(draft, (jobkit_home / "data/setup/resume_source.txt").read_text())
    # the person keeps the line that positions them and drops the other
    update_draft(
        {
            "positioning": [
                {"id": "pos_resume_summary", "archetype": "customer success leadership"},
                {"id": "pos_resume_summary_2", "drop": True},
            ]
        }
    )
    kept = _draft(jobkit_home)["positioning"]
    assert [(p["id"], p["confirmed"]) for p in kept] == [("pos_resume_summary", True)]


def test_summary_without_headings_never_takes_a_role_header() -> None:
    """No section headings at all: the paragraph above the first dated role is
    the summary; the role's own header lines are not."""
    text = (
        "Morgan Ellis\n"
        "morgan.ellis@example.com | (555) 010-0142\n"
        "Customer success leader who builds onboarding programs for mid-market software\n"
        "customers across the Mountain West.\n"
        "\n"
        "Customer Success Manager at Fernbrook Software\n"
        "Jun 2021 – Present\n"
        "• Managed a book of 48 mid-market accounts worth $3.2M in annual recurring revenue.\n"
    )
    parsed = resume_parse.parse_resume(text)
    assert parsed.summary == [
        "Customer success leader who builds onboarding programs for mid-market software "
        "customers across the Mountain West."
    ]
    assert parsed.roles[0].company == "Fernbrook Software"


# ---------------------------------------------------------------------------
# resumes from other fields (fictional personas)
# ---------------------------------------------------------------------------

PERSONAS = FIXTURES / "onboarding_personas"


@pytest.mark.parametrize("name", ["rn", "teacher", "engineer"])
def test_persona_drafts_contain_no_sentence_absent_from_the_resume(
    jobkit_home: Path, name: str
) -> None:
    """The mandatory onboarding test, on resumes with field-specific headings."""
    ingest_resume(PERSONAS / f"{name}.txt")
    draft = _draft(jobkit_home)
    source = (jobkit_home / "data" / "setup" / "resume_source.txt").read_text()
    _assert_only_resume_text(draft, source)
    assert draft["roles"] and all(b["metric_confidence"] is None for b in draft["bullets"])


@pytest.mark.parametrize(
    "name, heading, first_title",
    [
        ("rn", "CLINICAL EXPERIENCE", "Staff Nurse"),
        ("teacher", "TEACHING EXPERIENCE", "Science Teacher"),
        ("engineer", "EXPERIENCE", "Senior Software Engineer"),
    ],
)
def test_field_specific_experience_headings_hold_the_roles(
    name: str, heading: str, first_title: str
) -> None:
    parsed = resume_parse.parse_resume((PERSONAS / f"{name}.txt").read_text())
    assert heading in parsed.headings
    assert len(parsed.roles) == 2 and all(r.bullets for r in parsed.roles)
    assert parsed.roles[0].title == first_title


def test_licenses_and_projects_are_kept_in_other_sections() -> None:
    rn = resume_parse.parse_resume((PERSONAS / "rn.txt").read_text())
    licenses = rn.other_sections["LICENSES & CERTIFICATIONS"]
    assert licenses[0] == "Registered Nurse, Ohio Board of Nursing, active"
    # the clinical roles never land in the licenses section
    assert not any("Riverbend" in line for line in licenses)
    teacher = resume_parse.parse_resume((PERSONAS / "teacher.txt").read_text())
    assert teacher.other_sections["PORTFOLIO"] == [
        "Water Cycle eLearning module, built in Articulate Rise",
        "Lab safety scenario, built in Articulate Storyline",
    ]
    assert teacher.summary == ["Middle school science teacher moving into instructional design."]


@pytest.mark.parametrize(
    "line, kind",
    [
        ("Clinical Experience", "experience"),
        ("NURSING EXPERIENCE", "experience"),
        ("Clinical Rotations", "experience"),
        ("Teaching History", "experience"),
        ("Volunteer Experience", "other"),  # the exact name wins
        ("Licensure", "credentials"),
        ("Licenses & Certifications", "credentials"),
        ("Portfolio", "projects"),
        ("Freelance Projects", "projects"),
        ("Head of Customer Experience", None),  # a job title, not a heading
        ("I have experience", None),
    ],
)
def test_heading_kinds(line: str, kind: str | None) -> None:
    assert resume_parse.heading_kind(line) == kind


def test_field_headings_are_not_called_non_standard() -> None:
    from jobkit.onboarding import critique
    from jobkit.onboarding.resume_parse import extraction_from_paste

    text = (PERSONAS / "rn.txt").read_text()
    titles = [
        f.title
        for f in critique.findings(resume_parse.parse_resume(text), extraction_from_paste(text))
    ]
    assert "No standard Experience heading" not in titles


def test_no_roles_names_the_headings_it_saw(jobkit_home: Path) -> None:
    text = (
        "Morgan Ellis\nSUMMARY\n"
        + "A long paragraph of words about me. " * 10
        + "\nCAREER STORY\nWorked at several places doing several things.\n"
    )
    with pytest.raises(SetupError, match=r"headings found: SUMMARY\. Ask which one holds the jobs"):
        ingest_resume(text=text)
