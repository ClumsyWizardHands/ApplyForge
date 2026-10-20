"""Typst PDF rendering via the typst package (local compile, no binary install)."""

from __future__ import annotations

import json
import re
import zlib
from pathlib import Path
from typing import Any

import typst


def render_pdf(template: str, data: dict[str, Any], out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_bytes = typst.compile(
        Path("templates") / template,
        sys_inputs={"data": json.dumps(data)},
    )
    assert isinstance(pdf_bytes, bytes)
    out_path.write_bytes(pdf_bytes)
    return out_path


def rendered_pages(template: str, data: dict[str, Any]) -> int:
    """How many pages typst lays the template out on, counted by typst itself:
    the same template and data compiled to SVG, one image per page."""
    pages = typst.compile(
        Path("templates") / template,
        format="svg",
        sys_inputs={"data": json.dumps(data)},
    )
    return len(pages) if isinstance(pages, list) else 1


# A dictionary with no dictionary inside it, and the page-tree entries in one.
# PDF writers spell names with or without a space ("/Type /Pages" or Typst's
# "/Type/Pages"), and may keep objects in compressed object streams.
_DICT = re.compile(rb"<<((?:(?!<<|>>).)*)>>", re.S)
_TYPE_PAGES = re.compile(rb"/Type\s*/Pages(?![A-Za-z0-9])")
_TYPE_PAGE = re.compile(rb"/Type\s*/Page(?![A-Za-z0-9])")
_COUNT = re.compile(rb"/Count\s+(\d+)")
_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)


def _bodies(raw: bytes) -> list[bytes]:
    """The PDF's bytes and every Flate stream inflated (object streams hold
    page objects in PDF 1.5+ files); a stream that will not inflate is
    skipped."""
    out = [raw]
    for m in _STREAM.finditer(raw):
        try:
            out.append(zlib.decompress(m.group(1)))
        except zlib.error:
            continue
    return out


def page_count(pdf_path: Path) -> int:
    """The number of pages in the PDF: the root page tree's /Count (the
    largest /Count of any /Type /Pages node), or, with no page tree found,
    the number of /Type /Page objects; looked for in the file and in its
    inflated streams, whatever the spacing.

    The old count looked for b"/Type /Page" with a space only. Typst writes
    "/Type/Page", so every resume counted as one page and the one-page rule
    never shrank a two-page resume (review 2026-10-12). A PDF in which no
    page can be found raises ValueError: an unreadable count never passes
    as one page."""
    tree = 0
    leaves = 0
    for body in _bodies(pdf_path.read_bytes()):
        for d in _DICT.finditer(body):
            text = d.group(1)
            if _TYPE_PAGES.search(text):
                for c in _COUNT.finditer(text):
                    tree = max(tree, int(c.group(1)))
            elif _TYPE_PAGE.search(text):
                leaves += 1
    pages = tree or leaves
    if pages < 1:
        raise ValueError(f"no page found in {pdf_path}")
    return pages
