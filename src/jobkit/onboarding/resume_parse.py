"""Resume text extraction and structure (ONBOARDING.md 1.1).

Two steps, both deterministic (no LLM: transcription is not authorship, and
the MCP request path carries no LLM call outside tailoring and novel answers):

1. extract_text(path) — the resume's text. PDF (pypdf when installed, else
   poppler's `pdftotext` when on PATH, else a small built-in reader for
   simple text PDFs), DOCX (the document XML, stdlib only), HTML
   (BeautifulSoup) or plain text. A file's extraction gets a word score: the
   share of its words that are common English words. A garbled parse
   (custom font encodings, scanned images) scores low, and ingest then asks
   for the text to be pasted instead. Never silently guess at a garbled parse.
   Pasted text (and a plain-text file) is the person's own text, not a
   parse: it is not refused on the word score (names and tool names are not
   common English words, and a data engineer's resume can score under the
   bar), only when it is too short or holds unreadable characters
   (`garble_share`).

2. parse_resume(text) — sections, contact block, roles with their bullets,
   education and skills. Every string it returns is a substring of the text
   it was given (whitespace aside): a role header is split into company /
   title / location / dates by position and wording, never rewritten. The
   split is a best guess the human confirms; nothing here invents a name,
   a date or a number.

SPEC §10 lists no PDF or DOCX library. DOCX is a zip of XML and needs none.
For PDF, pypdf is used when someone has installed it (it is optional, not a
dependency), else `pdftotext` (poppler) when it is on PATH."""

from __future__ import annotations

import importlib
import re
import shutil
import subprocess
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from jobkit.validate.common_words import is_common_word

# An extraction (PDF, DOCX, HTML) under MIN_WORD_SCORE is treated as
# unreliable and the text is asked for. Pasted text and plain-text files are
# read as written, with nothing to garble: they are not word-scored.
MIN_WORD_SCORE = 0.5
UNSCORED_FORMATS = frozenset({"pasted", "text"})
# any text under this many characters is not a resume
MIN_CHARS = 200
# any text over this share of unreadable characters (U+FFFD, control,
# private-use, unassigned) is garbled, pasted text included: a PDF viewer
# copying a custom-encoded font yields them. A resume's symbol-font bullets
# stay far under it.
MAX_GARBLE_SHARE = 0.05

TEXT_SUFFIXES = (".txt", ".text", ".md")
HTML_SUFFIXES = (".html", ".htm")
UNSUPPORTED = (".doc", ".pages", ".rtf", ".odt", ".png", ".jpg", ".jpeg", ".heic")


class ResumeReadError(ValueError):
    """The resume could not be read reliably. The message asks for the text
    (or a different format) rather than guessing."""


@dataclass
class Extraction:
    text: str
    format: str  # pdf | docx | html | text | pasted
    method: str  # pypdf | pdftotext | builtin-pdf | docx-xml | html | plain | pasted
    word_score: float
    warnings: list[str] = field(default_factory=list)  # ATS-formatting observations

    def problem(self) -> str | None:
        """Why this text is not reliable enough to draft from, or None.

        The word score judges only an extraction (PDF, DOCX, HTML). Pasted
        text and a plain-text file are the person's text as written, so
        names and tool names (Snowflake, dbt, PySpark) are not evidence of a
        garbled parse there; they are refused only when too short or when
        they hold unreadable characters."""
        n = len(self.text.strip())
        if n < MIN_CHARS:
            return f"only {n} characters of text (a resume has at least {MIN_CHARS})"
        garble = garble_share(self.text)
        if garble > MAX_GARBLE_SHARE:
            return (
                f"{garble:.0%} of its characters are unreadable (replacement, control or "
                "private-use characters)"
            )
        if self.format not in UNSCORED_FORMATS and self.word_score < MIN_WORD_SCORE:
            return f"only {self.word_score:.0%} of its words are recognizable English words"
        return None

    @property
    def confident(self) -> bool:
        return self.problem() is None


# ---------------------------------------------------------------------------
# text clean-up shared by every format
# ---------------------------------------------------------------------------

# \uf0b7 \uf0a7 \uf0d8: Symbol / Wingdings bullets as a PDF viewer copies them
_BULLET_ONLY = re.compile(r"^\s*[•●▪◦‣∙·*\-–—►▸✓➢o\uf0b7\uf0a7\uf0d8]\s*$")


def clean_text(text: str) -> str:
    """Normalize line endings, compatibility characters (ligatures,
    non-breaking spaces) and trailing spaces, drop runs of blank lines, and
    join a line that is only a bullet marker to the line after it. The
    result is what the draft is checked against."""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x0c", "\n")
    text = text.replace("­", "")  # soft hyphen
    lines = [ln.rstrip().replace("\t", "  ") for ln in text.split("\n")]
    out: list[str] = []
    pending_marker = ""
    for ln in lines:
        if _BULLET_ONLY.match(ln):
            pending_marker = ln.strip() + " "
            continue
        if pending_marker and ln.strip():
            ln = pending_marker + ln.strip()
            pending_marker = ""
        if not ln.strip() and out and not out[-1].strip():
            continue
        out.append(ln)
    return "\n".join(out).strip("\n") + "\n"


def word_score(text: str) -> float:
    """Share of the words (3+ letters) that are common English words, email
    addresses and web links left out. A plain-English resume scores well
    above MIN_WORD_SCORE; a garbled PDF scores near 0. A resume dense with
    names and tool names can score under it, which is why pasted text is
    not refused on this score (Extraction.problem)."""
    words = re.findall(r"[A-Za-z]{3,}", URL.sub(" ", EMAIL.sub(" ", text)))
    if not words:
        return 0.0
    known = sum(1 for w in words if is_common_word(w))
    return known / len(words)


_GARBLE_CATEGORIES = frozenset({"Cc", "Co", "Cn", "Cs"})


def garble_share(text: str) -> float:
    """Share of the non-space characters that are unreadable: U+FFFD
    (replacement), control, private-use, surrogate or unassigned code points.
    Proper nouns and tool names do not raise it; a custom-encoded font copied
    out of a PDF viewer does."""
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    bad = sum(1 for c in chars if c == "\ufffd" or unicodedata.category(c) in _GARBLE_CATEGORIES)
    return bad / len(chars)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_BLOCK_TAGS = (
    "p",
    "div",
    "li",
    "ul",
    "ol",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "tr",
    "td",
    "th",
    "section",
    "article",
    "header",
    "footer",
    "table",
    "blockquote",
    "pre",
    "dd",
    "dt",
    "dl",
    "address",
)


def html_to_text(html: str) -> tuple[str, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "head", "template"]):
        t.decompose()
    warnings: list[str] = []
    if soup.find("table"):
        warnings.append("table")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert(0, "• ")
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in soup.get_text().split("\n")]
    return "\n".join(lines), warnings


# ---------------------------------------------------------------------------
# DOCX (stdlib: a zip of XML)
# ---------------------------------------------------------------------------

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_paragraph(p: ET.Element) -> str:
    parts: list[str] = []
    for node in p.iter():
        if node.tag == f"{_W}t":
            parts.append(node.text or "")
        elif node.tag == f"{_W}tab":
            parts.append("\t")
        elif node.tag in (f"{_W}br", f"{_W}cr"):
            parts.append("\n")
    text = "".join(parts)
    is_list = p.find(f"{_W}pPr/{_W}numPr") is not None
    style = p.find(f"{_W}pPr/{_W}pStyle")
    if style is not None and "list" in (style.get(f"{_W}val") or "").lower():
        is_list = True
    return f"• {text.strip()}" if is_list and text.strip() else text


def _docx_lines(root: ET.Element) -> tuple[list[str], bool]:
    """Paragraph texts in document order, and whether any sat in a table."""
    body = root.find(f"{_W}body")
    container = body if body is not None else root
    lines: list[str] = []
    tables = False
    for el in container:
        if el.tag == f"{_W}p":
            lines.append(_docx_paragraph(el))
        elif el.tag == f"{_W}tbl":
            tables = True
            lines += [_docx_paragraph(p) for p in el.iter(f"{_W}p")]
        elif el.tag == f"{_W}sdt":
            lines += [_docx_paragraph(p) for p in el.iter(f"{_W}p")]
    return lines, tables


def docx_to_text(path: Path) -> tuple[str, list[str]]:
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise ResumeReadError(
            f"{path.name} is not a readable .docx. Save it again as DOCX or PDF, or paste the "
            "resume text."
        ) from e
    warnings: list[str] = []
    with z:
        names = z.namelist()
        if "word/document.xml" not in names:
            raise ResumeReadError(f"{path.name} has no document body. Paste the resume text.")
        try:
            root = ET.fromstring(z.read("word/document.xml"))
            extra = [
                ET.fromstring(z.read(n))
                for n in sorted(names)
                if re.fullmatch(r"word/(?:header|footer)\d*\.xml", n)
            ]
        except ET.ParseError as e:
            raise ResumeReadError(f"{path.name} does not parse ({e}). Paste the text.") from e
    lines, tables = _docx_lines(root)
    if tables:
        warnings.append("table")
    head: list[str] = []
    for part in extra:
        texts = [t for t in (_docx_paragraph(p) for p in part.iter(f"{_W}p")) if t.strip()]
        head += texts
    if head:
        warnings.append("header_footer")
        lines = head + [""] + lines
    return "\n".join(lines), warnings


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def _load_pypdf() -> Any | None:
    """pypdf when installed (optional; SPEC §10 does not list it)."""
    try:
        return importlib.import_module("pypdf")
    except ImportError:
        return None


def _pdftotext() -> str | None:
    return shutil.which("pdftotext")


def pdf_to_text(path: Path) -> tuple[str, str]:
    """(text, method). Tries pypdf, then `pdftotext`, then the built-in reader."""
    pypdf = _load_pypdf()
    if pypdf is not None:
        try:
            reader = pypdf.PdfReader(str(path))
            return "\n".join((page.extract_text() or "") for page in reader.pages), "pypdf"
        except Exception:  # noqa: BLE001 — fall through to the next reader
            pass
    exe = _pdftotext()
    if exe:
        done = subprocess.run(
            [exe, "-enc", "UTF-8", str(path), "-"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if done.returncode == 0 and done.stdout.strip():
            return done.stdout, "pdftotext"
    return builtin_pdf_text(path.read_bytes()), "builtin-pdf"


_STREAM_START = re.compile(rb"stream\r?\n")
_DELIMS = b"()<>[]{}/%"
_WS = b" \t\r\n\x0c\x00"


def _pdf_streams(data: bytes) -> list[bytes]:
    """Decoded content of every stream that holds text-showing operators."""
    out: list[bytes] = []
    for m in _STREAM_START.finditer(data):
        start = m.end()
        end = data.find(b"endstream", start)
        if end < 0:
            continue
        header = data[max(0, m.start() - 600) : m.start()]
        cut = header.rfind(b" obj")
        if cut >= 0:
            header = header[cut:]
        if re.search(rb"/(?:DCTDecode|JPXDecode|CCITTFaxDecode|JBIG2Decode)\b", header):
            continue
        if re.search(rb"/Subtype\s*/Image\b|/Length1\b|/Length2\b|/Length3\b", header):
            continue
        raw = data[start:end].rstrip(b"\r\n")
        if b"/FlateDecode" in header:
            try:
                raw = zlib.decompressobj().decompress(raw)
            except zlib.error:
                continue
        if b"BT" in raw and (b"Tj" in raw or b"TJ" in raw or b"'" in raw):
            out.append(raw)
    return out


def _read_literal(cs: bytes, i: int) -> tuple[bytes, int]:
    """A PDF literal string starting at cs[i] == '('. Returns (bytes, next index)."""
    depth = 0
    out = bytearray()
    i += 1
    n = len(cs)
    escapes = {
        ord("n"): b"\n",
        ord("r"): b"\r",
        ord("t"): b"\t",
        ord("b"): b"\b",
        ord("f"): b"\f",
    }
    while i < n:
        c = cs[i]
        if c == ord("\\") and i + 1 < n:
            nxt = cs[i + 1]
            if nxt in escapes:
                out += escapes[nxt]
                i += 2
            elif nxt in b"01234567":
                j = i + 1
                while j < n and j < i + 4 and cs[j] in b"01234567":
                    j += 1
                out.append(int(cs[i + 1 : j], 8) & 0xFF)
                i = j
            elif nxt in b"\r\n":
                i += 2
                if nxt == ord("\r") and i < n and cs[i] == ord("\n"):
                    i += 1
            else:
                out.append(nxt)
                i += 2
            continue
        if c == ord("("):
            depth += 1
        elif c == ord(")"):
            if depth == 0:
                return bytes(out), i + 1
            depth -= 1
        out.append(c)
        i += 1
    return bytes(out), i


def _decode(s: bytes) -> str:
    return s.decode("cp1252", errors="replace")


def _content_text(cs: bytes) -> str:
    """Text shown by one content stream: Tj / TJ / ' / " strings, with a
    line break where the text position moves to a new line."""
    out: list[str] = []
    operands: list[tuple[str, Any]] = []
    i, n = 0, len(cs)
    last_y: float | None = None

    def newline() -> None:
        if out and not out[-1].endswith("\n"):
            out.append("\n")

    while i < n:
        c = cs[i]
        if c in _WS:
            i += 1
            continue
        if c == ord("%"):
            while i < n and cs[i] not in b"\r\n":
                i += 1
            continue
        if c == ord("("):
            s, i = _read_literal(cs, i)
            operands.append(("str", s))
            continue
        if c == ord("<"):
            if cs[i + 1 : i + 2] == b"<":
                i += 2
                continue
            j = cs.find(b">", i)
            if j < 0:
                break
            hexs = re.sub(rb"[^0-9A-Fa-f]", b"", cs[i + 1 : j])
            if len(hexs) % 2:
                hexs += b"0"
            operands.append(("str", bytes.fromhex(hexs.decode())))
            i = j + 1
            continue
        if c == ord(">"):
            i += 1
            continue
        if c == ord("["):
            operands.append(("[", None))
            i += 1
            continue
        if c == ord("]"):
            items: list[Any] = []
            while operands and operands[-1][0] != "[":
                items.append(operands.pop()[1])
            if operands:
                operands.pop()
            operands.append(("arr", list(reversed(items))))
            i += 1
            continue
        if c == ord("/"):
            j = i + 1
            while j < n and cs[j] not in _WS and cs[j] not in _DELIMS:
                j += 1
            operands.append(("name", cs[i + 1 : j]))
            i = j
            continue
        j = i
        while j < n and cs[j] not in _WS and cs[j] not in _DELIMS:
            j += 1
        if j == i:
            i += 1
            continue
        tok = cs[i:j]
        i = j
        try:
            operands.append(("num", float(tok)))
            continue
        except ValueError:
            pass
        op = tok.decode("latin-1")
        nums = [v for k, v in operands if k == "num"]
        if op == "Tj" and operands and operands[-1][0] == "str":
            out.append(_decode(operands[-1][1]))
        elif op in ("'", '"') and operands and operands[-1][0] == "str":
            newline()
            out.append(_decode(operands[-1][1]))
        elif op == "TJ" and operands and operands[-1][0] == "arr":
            for item in operands[-1][1]:
                if isinstance(item, bytes):
                    out.append(_decode(item))
                elif isinstance(item, float) and item < -200:
                    out.append(" ")
        elif op in ("Td", "TD") and len(nums) >= 2:
            if abs(nums[-1]) > 0.01:
                newline()
            elif nums[-2] > 0 and out and not out[-1].endswith((" ", "\n")):
                out.append(" ")
        elif op == "T*":
            newline()
        elif op == "Tm" and len(nums) >= 6:
            y = nums[-1]
            if last_y is not None and abs(y - last_y) > 0.01:
                newline()
            last_y = y
        elif op == "ET":
            newline()
        elif op == "BI":  # inline image: skip to EI
            k = cs.find(b"EI", i)
            i = n if k < 0 else k + 2
        operands.clear()
    return "".join(out)


def builtin_pdf_text(data: bytes) -> str:
    """Text of a simple PDF (standard fonts, Flate or no compression). Text
    in a CID / custom-encoded font comes out as noise; the word score then
    marks the extraction unreliable and the text is asked for instead."""
    if not data.startswith(b"%PDF"):
        raise ResumeReadError("not a PDF file. Paste the resume text instead.")
    return "\n".join(_content_text(s) for s in _pdf_streams(data))


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def extract_text(path: Path) -> Extraction:
    """Read a resume file. Raises ResumeReadError for a format that cannot
    be read; a readable but unreliable result comes back with confident ==
    False (ingest then refuses it and asks for the text)."""
    suffix = path.suffix.lower()
    if not path.is_file():
        raise ResumeReadError(f"no file at {path}")
    warnings: list[str] = []
    if suffix in TEXT_SUFFIXES:
        raw, fmt, method = path.read_text(encoding="utf-8", errors="replace"), "text", "plain"
    elif suffix in HTML_SUFFIXES:
        raw, warnings = html_to_text(path.read_text(encoding="utf-8", errors="replace"))
        fmt, method = "html", "html"
    elif suffix == ".docx":
        raw, warnings = docx_to_text(path)
        fmt, method = "docx", "docx-xml"
    elif suffix == ".pdf":
        raw, method = pdf_to_text(path)
        fmt = "pdf"
    elif suffix in UNSUPPORTED:
        raise ResumeReadError(
            f"{suffix} files cannot be read here. Save the resume as PDF or DOCX, or paste "
            "its text."
        )
    else:
        raise ResumeReadError(
            f"unknown resume format {suffix or '(none)'}: use PDF, DOCX, HTML or plain text, "
            "or paste the text."
        )
    text = clean_text(raw)
    return Extraction(
        text=text, format=fmt, method=method, word_score=word_score(text), warnings=warnings
    )


def extraction_from_paste(text: str) -> Extraction:
    t = clean_text(text)
    return Extraction(text=t, format="pasted", method="pasted", word_score=word_score(t))


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------

_HEADINGS: dict[str, tuple[str, ...]] = {
    "experience": (
        "experience",
        "work experience",
        "professional experience",
        "relevant experience",
        "employment",
        "employment history",
        "work history",
        "career history",
        "career experience",
        "professional background",
        "experience highlights",
    ),
    "education": (
        "education",
        "education & training",
        "education and training",
        "academic background",
        "education & certifications",
        "education and certifications",
    ),
    "skills": (
        "skills",
        "technical skills",
        "core skills",
        "key skills",
        "skills & tools",
        "skills and tools",
        "core competencies",
        "competencies",
        "areas of expertise",
        "expertise",
        "tools",
        "technologies",
        "tools & technologies",
        "tools and technologies",
        "skills & interests",
        "skills and interests",
        "skills & certifications",
        "skills and certifications",
    ),
    "summary": (
        "summary",
        "professional summary",
        "career summary",
        "profile",
        "professional profile",
        "about",
        "about me",
        "objective",
        "career objective",
        "overview",
        "summary of qualifications",
        "qualifications summary",
        "qualifications",
    ),
    # licenses and certifications: kept in other_sections under their heading
    "credentials": (
        "certifications",
        "certificates",
        "certification",
        "licenses",
        "license",
        "licensure",
        "credentials",
        "licenses & certifications",
        "licenses and certifications",
        "licensure & certifications",
        "licensure and certifications",
        "certifications & licenses",
        "certifications and licenses",
        "licenses & credentials",
        "licenses and credentials",
        "professional licenses",
    ),
    # projects and portfolio pieces: kept in other_sections under their heading
    "projects": (
        "projects",
        "selected projects",
        "portfolio",
        "selected work",
        "work samples",
    ),
    "other": (
        "awards",
        "honors",
        "awards & honors",
        "honors & awards",
        "awards and honors",
        "publications",
        "volunteer",
        "volunteer experience",
        "volunteering",
        "leadership",
        "activities",
        "interests",
        "languages",
        "affiliations",
        "memberships",
        "additional information",
        "additional",
        "references",
        "patents",
        "speaking",
        "training",
        "courses",
    ),
}
_HEADING_KIND = {name: kind for kind, names in _HEADINGS.items() for name in names}

_BULLET = re.compile(
    r"^\s*(?:[•●▪◦‣∙·*►▸✓➢\uf0b7\uf0a7\uf0d8]|[-–—](?=\s)|o(?=\s{1,3}[A-Z])|\d{1,2}[.)](?=\s))\s*"
)

_MON = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
)
_SEASON = r"(?:spring|summer|fall|autumn|winter)"
_ONE_DATE = (
    rf"(?:{_MON}\s*,?\s*(?:19|20)\d{{2}}|{_SEASON}\s+(?:19|20)\d{{2}}"
    r"|(?:0?[1-9]|1[0-2])\s*/\s*(?:19|20)\d{2}|(?:19|20)\d{2}\s*[-/.]\s*(?:0[1-9]|1[0-2])(?!\d)"
    r"|(?:19|20)\d{2})"
)
_PRESENT = r"(?:present|current|now|today|ongoing|date)"
DATE_RANGE = re.compile(
    rf"(?<![\w/])(?P<start>{_ONE_DATE})\s*(?:-|–|—|to|until|through|thru|~)\s*"
    rf"(?P<end>{_ONE_DATE}|{_PRESENT})(?![\w/])",
    re.I,
)
ONE_DATE = re.compile(rf"(?<![\w/]){_ONE_DATE}(?![\w/])", re.I)
_MONTHS = {
    m: i
    for i, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for m in names
}

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(
    r"(?<!\w)(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)|\d{2,4})[\s.-]?\d{3,4}[\s.-]?\d{3,4}(?!\w)"
)
URL = re.compile(
    r"(?<![@\w])(?:https?://)?(?:www\.)?(?:[\w-]+\.)+(?:com|io|dev|me|net|org|co|ai|app|xyz|page)"
    r"(?:/[\w/.%~-]*)?(?![\w@])",
    re.I,
)
LOCATION = re.compile(
    r"^(?:[A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){0,3},\s*"
    r"(?!(?:Inc|Llc|LLC|Ltd|Corp|Co|Plc|PLC|Llp|LLP|Group|Holdings|Company)\b)"
    r"(?:[A-Z]{2}|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)"
    r"(?:\s+\d{5})?|Remote(?:\s*\([^)]*\))?|Hybrid|Remote\s*[-–]\s*[A-Z][\w .]+)$"
)
_SPLIT = re.compile(r"\s+[|•·]\s+|\s*\|\s*|\s+[–—]\s+|\s+-\s+|\s{2,}")
_TITLE_WORD = re.compile(
    r"\b(?:manager|management|director|engineer|engineering|developer|executive|representative"
    r"|rep|analyst|designer|lead|leader|head|vp|svp|evp|avp|vice president|president|officer"
    r"|chief|specialist|consultant|associate|coordinator|intern|scientist|architect"
    r"|administrator|founder|co-founder|cofounder|owner|partner|advisor|adviser|assistant"
    r"|teacher|nurse|recruiter|strategist|producer|editor|writer|researcher|technician"
    r"|supervisor|principal|staff|senior|sr|junior|jr|ceo|cto|cfo|coo|cmo|cro|sdr|bdr|ae"
    r"|agent|attorney|counsel|paralegal|accountant|controller|planner|operator|instructor"
    r"|professor|fellow|trainee|apprentice|clerk|cashier|therapist|physician|pharmacist"
    r"|programmer|tester|generalist|ambassador|member|volunteer)\b",
    re.I,
)
_ABBREV_END = re.compile(r"\b(?:inc|co|corp|ltd|llc|llp|plc|jr|sr|st|dr|u\.s|n\.a)\.$", re.I)
_SCHOOL = re.compile(
    r"\b(?:university|college|school|institute|academy|polytechnic|conservatory|seminary"
    r"|universidad|université|universität|bootcamp)\b",
    re.I,
)
_DEGREE = re.compile(
    r"(?<!\w)(?:b\.?\s?a|b\.?\s?s|b\.?\s?sc|b\.?\s?eng|bfa|bba|m\.?\s?a|m\.?\s?s|m\.?\s?sc"
    r"|m\.?\s?eng|mfa|mba|m\.?b\.?a|ph\.?\s?d|j\.?d|m\.?d|ed\.?d|a\.?a|a\.?s)\.?(?!\w)"
    r"|\b(?:bachelor\w*|master\w*|doctor\w*|associate'?s? degree|associate of \w+|diploma"
    r"|certificate|certification|ged|high school)\b",
    re.I,
)


@dataclass
class ParsedRole:
    header_lines: list[str] = field(default_factory=list)
    company: str | None = None
    title: str | None = None
    location: str | None = None
    dates: str | None = None
    start: str | None = None
    end: str | None = None
    current: bool = False
    bullets: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class ParsedEducation:
    lines: list[str] = field(default_factory=list)
    school: str | None = None
    degree: str | None = None
    years: str | None = None


@dataclass
class ParsedResume:
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    links: dict[str, str] = field(default_factory=dict)
    # each bullet or paragraph of the summary, as written: one positioning
    # candidate apiece (lines are never joined across a bullet marker or a
    # line that is not part of the summary, so each is a piece of the text)
    summary: list[str] = field(default_factory=list)
    roles: list[ParsedRole] = field(default_factory=list)
    education: list[ParsedEducation] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    other_sections: dict[str, list[str]] = field(default_factory=dict)
    headings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# A heading of at most this many words that ends in one of these words opens
# that kind of section: "Clinical Experience", "Teaching Experience", "Clinical
# Rotations", "Freelance Projects". The exact names above win, so "Volunteer
# Experience" stays a section of its own.
_HEADING_TAIL_WORDS = 4
_HEADING_TAILS = {
    "experience": "experience",
    "history": "experience",
    "rotations": "experience",
    "projects": "projects",
}
# words that make a short line a job title or a sentence rather than a
# heading: "Head of Customer Experience", "I have experience"
_TITLE_LIKE = frozenset(
    {
        "of",
        "for",
        "to",
        "at",
        "head",
        "director",
        "manager",
        "vp",
        "vice",
        "president",
        "lead",
        "chief",
        "officer",
        "specialist",
        "coordinator",
        "analyst",
        "associate",
        "senior",
        "i",
        "my",
        "have",
        "has",
        "with",
        "years",
    }
)


def heading_kind(line: str) -> str | None:
    """The section a heading line opens, or None. "WORK EXPERIENCE",
    "Experience:", "E X P E R I E N C E" and "Clinical Experience" all open
    experience; "Licenses & Certifications" opens credentials and
    "Portfolio" opens projects (both kept in other_sections)."""
    s = line.strip().strip(":").strip()
    if not s or len(s) > 45:
        return None
    if re.fullmatch(r"(?:[A-Za-z&]\s){2,}[A-Za-z&]", s):
        s = s.replace(" ", "")
    key = re.sub(r"\s+", " ", re.sub(r"[^a-z& ]", "", s.lower())).strip()
    kind = _HEADING_KIND.get(key)
    if kind is None and " " not in key:
        # a letter-spaced heading lost its word gaps: "WORKEXPERIENCE"
        kind = {k.replace(" ", ""): v for k, v in _HEADING_KIND.items()}.get(key)
    if kind is None and key:
        words = key.split()
        # the whole line must be heading words: letters only before the
        # filter above, so a sentence that ends in "experience" never counts
        plain = re.fullmatch(r"[A-Za-z&/\- ]+", s) is not None
        if plain and len(words) <= _HEADING_TAIL_WORDS and not _TITLE_LIKE.intersection(words[:-1]):
            kind = _HEADING_TAILS.get(words[-1])
    return kind


def strip_bullet(line: str) -> tuple[str, bool]:
    """(text without its bullet marker, whether it had one)."""
    m = _BULLET.match(line)
    if m and m.end() < len(line):
        return line[m.end() :].strip(), True
    return line.strip(), False


def parse_month(text: str) -> tuple[str | None, str]:
    """One date as YYYY-MM, or None with why. A year alone or a season gives
    no month: the human is asked rather than a month being made up."""
    s = text.strip().lower().rstrip(".")
    m = re.fullmatch(r"([a-z]+)\.?\s*,?\s*((?:19|20)\d{2})", s)
    if m and m.group(1) in _MONTHS:
        return f"{m.group(2)}-{_MONTHS[m.group(1)]:02d}", ""
    m = re.fullmatch(r"(\d{1,2})\s*/\s*((?:19|20)\d{2})", s)
    if m and 1 <= int(m.group(1)) <= 12:
        return f"{m.group(2)}-{int(m.group(1)):02d}", ""
    m = re.fullmatch(r"((?:19|20)\d{2})\s*[-/.]\s*(\d{1,2})", s)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{m.group(1)}-{int(m.group(2)):02d}", ""
    if re.fullmatch(r"(?:19|20)\d{2}", s):
        return None, "year only"
    return None, "no month"


def _header_like(line: str) -> bool:
    s = line.strip()
    if not s or s[0].islower():
        return False
    if len(s.split()) > 10:
        return False
    return not (s.endswith((".", "!", "?", ";")) and not _ABBREV_END.search(s))


def _join(prev: str, nxt: str) -> str:
    if prev.endswith("-") and nxt[:1].islower():
        return prev + nxt
    return f"{prev} {nxt}"


def _split_parts(text: str) -> list[str]:
    parts = [p.strip(" ,;|") for p in _SPLIT.split(text)]
    return [p for p in parts if p]


def _role_from_header(role: ParsedRole) -> None:
    """Fill company / title / location / dates from the header lines, each a
    verbatim piece of them."""
    parts: list[str] = []
    for line in role.header_lines:
        m = DATE_RANGE.search(line)
        if m and role.dates is None:
            role.dates = m.group(0).strip()
            start, why = parse_month(m.group("start"))
            role.start = start
            if start is None:
                role.notes.append(f"start date '{m.group('start').strip()}' is a {why}: ask")
            end_raw = m.group("end").strip()
            if re.fullmatch(_PRESENT, end_raw, re.I):
                role.current = True
            else:
                end, why = parse_month(end_raw)
                role.end = end
                if end is None:
                    role.notes.append(f"end date '{end_raw}' is a {why}: ask")
            line = (line[: m.start()] + "  " + line[m.end() :]).strip()
        elif role.dates is None:
            one = ONE_DATE.search(line)
            if one and _split_parts(line) and len(line.strip()) - len(one.group(0)) < 4:
                role.dates = one.group(0).strip()
                role.notes.append(f"one date only ('{role.dates}'): ask for start and end")
                line = ""
        parts += _split_parts(line)
    candidates: list[str] = []
    for p in parts:
        if role.location is None and LOCATION.fullmatch(p):
            role.location = p
            continue
        # "Title, Company" / "Title at Company" / "Company, City, ST"
        loc = re.search(r",\s*([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){0,3},\s*[A-Z]{2})$", p)
        if role.location is None and loc:
            role.location = loc.group(1)
            p = p[: loc.start()].strip(" ,")
        pieces = re.split(r",\s+|\s+at\s+|\s+@\s+", p, maxsplit=1)
        if len(pieces) == 2 and bool(_TITLE_WORD.search(pieces[0])) != bool(
            _TITLE_WORD.search(pieces[1])
        ):
            candidates += [x.strip() for x in pieces if x.strip()]
        else:
            candidates.append(p)
    titles = [c for c in candidates if _TITLE_WORD.search(c)]
    others = [c for c in candidates if not _TITLE_WORD.search(c)]
    role.title = titles[0] if titles else None
    if others:
        role.company = others[0]
    elif len(titles) > 1:
        role.company = titles[1]
    if role.company is None:
        role.notes.append("company not found in the header: ask")
    if role.title is None:
        role.notes.append("title not found in the header: ask")
    if role.dates is None:
        role.notes.append("no dates in the header: ask")


def _parse_experience(lines: list[str], notes: list[str]) -> list[ParsedRole]:
    roles: list[ParsedRole] = []
    role: ParsedRole | None = None
    last_kind = ""  # "item" | "header" | ""
    for raw in lines:
        if not raw.strip():
            continue
        text, marked = strip_bullet(raw)
        if marked:
            if role is None:
                role = ParsedRole()
                roles.append(role)
            role.bullets.append(text)
            last_kind = "item"
            continue
        if DATE_RANGE.search(text) or (last_kind != "item" and _header_like(text)):
            if last_kind != "header" or role is None:
                role = ParsedRole()
                roles.append(role)
            role.header_lines.append(text)
            last_kind = "header"
            continue
        if role is not None and last_kind == "item" and role.bullets:
            prev = role.bullets[-1]
            if text[:1].islower() or (
                not prev.rstrip().endswith((".", "!", "?")) and not _header_like(text)
            ):
                role.bullets[-1] = _join(prev, text)
                continue
            if _header_like(text):
                role = ParsedRole(header_lines=[text])
                roles.append(role)
                last_kind = "header"
                continue
        if role is None:
            role = ParsedRole()
            roles.append(role)
        role.bullets.append(text)
        last_kind = "item"
    for r in roles:
        if r.header_lines:
            _role_from_header(r)
        else:
            r.notes.append("statements with no role header above them: which role are they?")
    if not roles:
        notes.append("no roles found in the experience section")
    return roles


def _parse_education(lines: list[str]) -> list[ParsedEducation]:
    entries: list[ParsedEducation] = []
    cur: ParsedEducation | None = None
    for raw in lines:
        if not raw.strip():
            continue
        text, marked = strip_bullet(raw)
        years = None
        m = DATE_RANGE.search(text) or ONE_DATE.search(text)
        rest = text
        if m:
            years = m.group(0).strip()
            rest = (text[: m.start()] + "  " + text[m.end() :]).strip()
        parts = _split_parts(rest)
        split: list[str] = []
        for p in parts:
            pieces = re.split(r",\s+", p, maxsplit=1)
            if len(pieces) == 2 and (
                (_SCHOOL.search(pieces[0]) and _DEGREE.search(pieces[1]))
                or (_DEGREE.search(pieces[0]) and _SCHOOL.search(pieces[1]))
            ):
                split += pieces
            else:
                split.append(p)
        school = next((p for p in split if _SCHOOL.search(p)), None)
        degree = next((p for p in split if p != school and _DEGREE.search(p)), None)
        new_entry = cur is None or (school and cur.school) or (degree and cur.degree and not school)
        if marked and cur is not None and not school and not degree:
            new_entry = False
        if new_entry:
            cur = ParsedEducation()
            entries.append(cur)
        assert cur is not None
        cur.lines.append(text)
        if school and not cur.school:
            cur.school = school
        if degree and not cur.degree:
            cur.degree = degree
        if years and not cur.years:
            cur.years = years
    return entries


def _parse_skills(lines: list[str]) -> list[str]:
    out: list[str] = []
    for raw in lines:
        text, _ = strip_bullet(raw)
        if not text:
            continue
        label = re.match(r"^[A-Z][\w &/-]{1,30}:\s*", text)
        if label:
            text = text[label.end() :]
        for item in re.split(r"\s*[,;|•·]\s*|\s{2,}", text):
            item = item.strip(" .")
            if item and len(item.split()) <= 6 and item not in out:
                out.append(item)
    return out


_DOC_TITLE = re.compile(r"(?i)^(?:r[ée]sum[ée]|cv|curriculum vitae)\b")


def _name_like(part: str) -> bool:
    return (
        not _DOC_TITLE.match(part)
        and "@" not in part
        and not re.search(r"\d", part)
        and 1 <= len(part.split()) <= 5
        and not URL.search(part)
        and not LOCATION.fullmatch(part)
        and heading_kind(part) is None
        and bool(re.search(r"[A-Za-z]", part))
    )


def _parse_contact(
    lines: list[str], resume: ParsedResume
) -> tuple[list[tuple[int, str]], list[str]]:
    """Contact details from the lines above the first heading. Returns
    (summary candidates, body: every line that is not a contact line).

    A summary candidate is a paragraph: a line of 8+ words that is not a
    contact detail, with the lines directly below it that continue it (no
    blank line, no contact line between). Each comes with the body index of
    its last line. Lines that are not adjacent in the resume are never
    joined, so every candidate is a piece of the text as written. Scanning
    stops at the first line with a date range: that is a role, not the
    contact block."""
    runs: list[tuple[int, str]] = []
    run_open = False
    body: list[str] = []
    in_body = False
    seen = 0
    for line in lines:
        s = line.strip()
        if not s:
            run_open = False
            continue
        seen += 1
        if in_body or DATE_RANGE.search(s):
            in_body = True
            run_open = False
            body.append(s)
            continue
        parts = _split_parts(s)
        contact = False
        if resume.name is None and seen <= 3 and parts and _name_like(parts[0]):
            resume.name = parts[0]
            parts = parts[1:]
            contact = True
        for p in parts:
            if m := EMAIL.search(p):
                # a second address is still a contact detail, never summary prose
                if resume.email is None:
                    resume.email = m.group(0)
                contact = True
            elif "@" not in p and (u := URL.search(p)):
                link = u.group(0)
                low = link.lower()
                key = "site"
                if "linkedin." in low:
                    key = "linkedin"
                elif "github." in low:
                    key = "github"
                resume.links.setdefault(key, link)
                contact = True
            elif resume.phone is None and (ph := PHONE.search(p)):
                resume.phone = ph.group(0).strip()
                contact = True
            elif resume.location is None and LOCATION.fullmatch(p):
                resume.location = p
                contact = True
        if contact:
            run_open = False
            continue
        continues = run_open and (
            len(s.split()) >= 8
            or s[:1].islower()
            or not runs[-1][1].rstrip().endswith((".", "!", "?"))
        )
        if continues:
            runs[-1] = (len(body), _join(runs[-1][1], s))
        elif len(s.split()) >= 8:
            runs.append((len(body), s))
            run_open = True
        else:
            run_open = False
        body.append(s)
    return runs, body


def _summary_candidates(lines: list[str]) -> list[str]:
    """A summary section's positioning candidates: each bullet (with the
    lines it wraps onto), or each paragraph of an unbulleted section. Lines
    are joined only where they are adjacent in the resume and no bullet
    marker starts the next one, so each candidate is a piece of the text."""
    out: list[str] = []
    cur: str | None = None
    for raw in lines:
        if not raw.strip():
            if cur:
                out.append(cur)
            cur = None
            continue
        text, marked = strip_bullet(raw)
        if marked or cur is None:
            if cur:
                out.append(cur)
            cur = text
        else:
            cur = _join(cur, text)
    if cur:
        out.append(cur)
    return out


def _experience_without_heading(body: list[str]) -> tuple[int, list[str]]:
    """No Experience heading: the roles start at the first line with a date
    range (and up to two header lines just above it). Returns (the body
    index where the roles start, experience lines)."""
    k = next((i for i, ln in enumerate(body) if DATE_RANGE.search(ln)), None)
    if k is None:  # no heading and no dated role: nothing reads as a role
        return len(body), []
    start = k
    while start > 0 and k - start < 2 and _header_like(body[start - 1]):
        start -= 1
    return start, body[start:]


def parse_resume(text: str) -> ParsedResume:
    """Sections, contact block, roles, education, skills. Every string in
    the result is taken from `text`."""
    resume = ParsedResume()
    sections: list[tuple[str, str, list[str]]] = []  # (kind, heading, lines)
    pre: list[str] = []
    for line in text.split("\n"):
        kind = heading_kind(line) if line.strip() else None
        if kind:
            sections.append((kind, line.strip().strip(":").strip(), []))
            resume.headings.append(line.strip())
            continue
        (sections[-1][2] if sections else pre).append(line)
    runs, body = _parse_contact(pre, resume)
    roles_start = len(body)
    exp_lines: list[str] = []
    for kind, heading, lines in sections:
        if kind == "experience":
            exp_lines += lines + [""]
        elif kind == "education":
            resume.education += _parse_education(lines)
        elif kind == "skills":
            resume.skills += [s for s in _parse_skills(lines) if s not in resume.skills]
        elif kind == "summary":
            if not resume.summary:  # the first summary section
                resume.summary = _summary_candidates(lines)
        else:
            kept = [strip_bullet(ln)[0] for ln in lines if ln.strip()]
            if kept:
                resume.other_sections.setdefault(heading, []).extend(kept)
    if not any(kind == "experience" for kind, _, _ in sections):
        resume.notes.append(
            "no Experience heading found: roles were read from the lines above the first "
            "other section"
        )
        roles_start, exp_lines = _experience_without_heading(body)
    if not resume.summary:
        # no summary section: the paragraphs above the roles, never a role header
        resume.summary = [text for last, text in runs if last < roles_start]
    resume.roles = _parse_experience(exp_lines, resume.notes)
    if not any(r.bullets for r in resume.roles):
        resume.notes.append(
            "headings found: "
            + ("; ".join(resume.headings) if resume.headings else "none")
            + ". Ask which one holds the jobs"
        )
    return resume
