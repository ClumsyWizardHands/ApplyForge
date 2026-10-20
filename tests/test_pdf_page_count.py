"""render/pdf.py page_count (review 2026-10-12): it counted b"/Type /Page"
with a space, but Typst writes "/Type/Page" and "/Type/Pages", so every
Typst PDF counted as ONE page and tailoring's one-page rule never shrank a
two-page resume. The count now reads the page tree's /Count (or the page
objects), whatever the spacing, in the file and in its inflated object
streams, and an unreadable PDF raises instead of passing as one page.

Offline: the typst package compiles locally (no binary, no network)."""

from __future__ import annotations

import copy
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import typst

from jobkit.pipeline import tailor
from jobkit.render import pdf
from jobkit.render.pdf import page_count, render_pdf, rendered_pages
from tests.test_targeting import GOOD_SEL, _sel, env  # noqa: F401 — env is a fixture


def _typst_pdf(tmp_path: Path, pages: int) -> Path:
    src = tmp_path / f"doc{pages}.typ"
    src.write_text("\n#pagebreak()\n".join(f"Page {i + 1}" for i in range(pages)))
    out = tmp_path / f"doc{pages}.pdf"
    body = typst.compile(str(src))
    assert isinstance(body, bytes)
    out.write_bytes(body)
    return out


def _old_count(path: Path) -> int:
    """The count before the fix, verbatim."""
    raw = path.read_bytes()
    return max(1, raw.count(b"/Type /Page") - raw.count(b"/Type /Pages"))


@pytest.mark.parametrize("pages", [1, 2, 3])
def test_a_typst_pdf_counts_its_pages(tmp_path: Path, pages: int) -> None:
    doc = _typst_pdf(tmp_path, pages)
    assert page_count(doc) == pages
    assert _old_count(doc) == 1  # the bug: Typst's "/Type/Page" was never seen


RESUME = {
    "identity": {
        "name": "Pat Doe",
        "display_location": "New York, NY",
        "phone": "+1 555 555 5555",
        "email": "pat@example.com",
        "linkedin": "linkedin.com/in/x",
    },
    "positioning": "B2B sales leader.",
    "skills": ["Sales Leadership", "Revenue Growth"],
    "roles": [
        {
            "company": "Acme",
            "title": "Sales Manager",
            "dates": "Nov 2021 – Mar 2024",
            "bullets": [
                "Grew ~$3.2M → ~$5.6M in annual bookings.",
                "Time to first dispatch -~40%.",
            ],
        }
    ],
    "education": [
        {"school": "Lakeside State University", "degree": "B.S.", "years": "2008 – 2012"}
    ],
}


def _long_resume() -> dict[str, Any]:
    data = copy.deepcopy(RESUME)
    bullet = (
        "Led a team of up to 9 Account Executives across three segments, rebuilt the "
        "onboarding program and ran weekly call reviews with every new hire."
    )
    data["roles"] = [
        {**data["roles"][0], "company": f"Acme {i}", "bullets": [bullet] * 8} for i in range(6)
    ]
    return data


def test_a_two_page_resume_counts_two(tmp_path: Path) -> None:
    out = render_pdf("resume.typ", _long_resume(), tmp_path / "resume.pdf")
    n = page_count(out)
    assert n >= 2 and n == rendered_pages("resume.typ", _long_resume())
    assert _old_count(out) == 1
    one = render_pdf("resume.typ", RESUME, tmp_path / "short.pdf")
    assert page_count(one) == 1 == rendered_pages("resume.typ", RESUME)


def test_spacing_and_compressed_object_streams(tmp_path: Path) -> None:
    spaced = tmp_path / "spaced.pdf"
    spaced.write_bytes(
        b"%PDF-1.4\n1 0 obj\n<< /Type /Pages /Kids [2 0 R 3 0 R] /Count 2 >>\nendobj\n"
        b"2 0 obj\n<< /Type /Page /Parent 1 0 R >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 1 0 R >>\nendobj\n%%EOF\n"
    )
    assert page_count(spaced) == 2
    # PDF 1.5+: the page tree inside a Flate-compressed object stream
    objstm = zlib.compress(
        b"1 0 2 52 3 90 <</Type/Pages/Kids[2 0 R 3 0 R 4 0 R]/Count 3>> "
        b"<</Type/Page/Parent 1 0 R>> <</Type/Page/Parent 1 0 R>> <</Type/Page/Parent 1 0 R>>"
    )
    packed = tmp_path / "packed.pdf"
    packed.write_bytes(
        b"%PDF-1.7\n5 0 obj\n<</Type/ObjStm/N 4/First 12/Filter/FlateDecode/Length "
        + str(len(objstm)).encode()
        + b">>\nstream\n"
        + objstm
        + b"\nendstream\nendobj\n%%EOF\n"
    )
    assert page_count(packed) == 3


def test_a_pdf_with_no_page_found_never_passes_as_one_page(tmp_path: Path) -> None:
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"%PDF-1.7\nnothing here\n%%EOF\n")
    with pytest.raises(ValueError, match="no page found"):
        page_count(junk)


def test_tailoring_shrinks_a_two_page_resume(
    conn: Any,
    env: Callable[..., Any],  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one-page rule with the real count: the first render is two pages,
    so the selection is shrunk once and rendered again (one page)."""
    _, jid, _ = env([_sel(*GOOD_SEL)])
    two, one = _typst_pdf(tmp_path, 2).read_bytes(), _typst_pdf(tmp_path, 1).read_bytes()
    renders: list[str] = []

    def render(template: str, data: dict[str, Any], out: Path) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        first_resume = template == "resume.typ" and "resume.typ" not in renders
        out.write_bytes(two if first_resume else one)
        renders.append(template)
        return out

    shrinks: list[int] = []
    real_shrink = tailor._shrink

    def shrink(*a: Any, **k: Any) -> bool:
        shrinks.append(1)
        return real_shrink(*a, **k)

    monkeypatch.setattr(tailor, "render_pdf", render)
    monkeypatch.setattr(tailor, "page_count", pdf.page_count)  # the env stubs it to 1
    monkeypatch.setattr(tailor, "_shrink", shrink)
    paths = tailor.tailor_job(conn, jid)
    assert shrinks == [1]
    assert renders.count("resume.typ") == 2
    assert page_count(Path(paths["resume"])) == 1
