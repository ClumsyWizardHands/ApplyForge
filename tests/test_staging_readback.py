"""Review 2026-10-12, items 2, 4 and 5, and an employer-hosted form's resume
bug of the same day. Fake Playwright objects only: offline, no browser.

* An employer-hosted form: the Greenhouse posting is served on the employer's
  own careers site, in the employer's own form. Its file input is labelled "Resume", not the
  API's "Resume/CV", and is not the element named "resume". The attach step
  used the text-field lookup and swallowed every failure, so the resume was
  never uploaded and the run said only 'attachment'. The resume now goes into
  the one file input known as a resume, and every failure is logged with its
  cause. The same run typed the Race/Ethnicity answer into Location: the
  container lookup took the outermost wrapper.
* Item 2: uploads first, a bounded wait for the resume parser, then the fill;
  every filled value is read back, refilled once, and a value still changed
  is 'overwritten-after-fill' and goes to the human.
* Item 4: a CAPTCHA check after every click the filler makes and after the
  fill; a provoked challenge stops the run and is never touched.
* Item 5: uploads are First_Last_Resume.pdf copies; the originals are untouched."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from jobkit import db
from jobkit.autofill import answer_bank, browser
from jobkit.autofill.answer_bank import BankMatcher
from jobkit.autofill.browser import FillNote, file_input_kind, read_back, same_text
from jobkit.pipeline import halts
from tests.test_staging_browser import FakeEl, FakeFrame, FakePage

EMPLOYER_URL = "https://careers.wonka.example/jobs?gh_jid=7004321"
BANK: dict[str, Any] = {
    "identity": {
        "first_name": "Pat",
        "last_name": "Doe",
        "email": "pat@example.com",
        "phone": "+1 555 0100",
    },
    "screening": [
        {"id": "age_18", "aliases": ["Are you at least 18 years of age?"], "answer": "Yes"},
    ],
}
# a visible hCaptcha challenge frame
HCAPTCHA = FakeEl(
    True,
    {"x": 400.0, "y": 300.0, "width": 300.0, "height": 420.0},
    {"src": "https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html#frame=challenge"},
)


# ---- fakes ---------------------------------------------------------------------


class Ctl:
    """One form control. after_fill / after_check: what the page does next
    (a parser rewriting the value, a CAPTCHA appearing)."""

    def __init__(
        self,
        name: str,
        ops: list[tuple[str, ...]],
        tag: str = "input",
        attrs: dict[str, str] | None = None,
        texts: list[str] | None = None,
    ) -> None:
        self.name, self.ops, self.tag = name, ops, tag
        self.attrs = dict(attrs or {})
        self.texts = list(texts or [])
        self.value = ""
        self.checked = False
        self.files: list[str] = []
        self.after_fill: Callable[[Ctl], None] | None = None
        self.after_check: Callable[[Ctl], None] | None = None

    def evaluate(self, js: str, arg: Any = None) -> Any:
        if js == browser._CLICK_GUARD_JS:
            return ""
        if js == browser._FILE_INPUT_TEXTS_JS:
            return list(self.texts)
        if "tagName" in js:
            return self.tag
        return None

    def get_attribute(self, name: str) -> str | None:
        return self.attrs.get(name)

    def count(self) -> int:
        return 1

    @property
    def first(self) -> Ctl:
        return self

    def set_input_files(self, path: str) -> None:
        self.files.append(path)
        self.ops.append(("upload", self.name, Path(path).name))

    def fill(self, value: str) -> None:
        self.value = value
        self.ops.append(("fill", self.name, value))
        if self.after_fill:
            self.after_fill(self)

    def input_value(self) -> str:
        return self.value

    def check(self, **kw: Any) -> None:
        self.checked = True
        self.ops.append(("check", self.name))
        if self.after_check:
            self.after_check(self)

    def is_checked(self) -> bool:
        return self.checked


class Many:
    def __init__(self, els: list[Any]) -> None:
        self.els = els

    def count(self) -> int:
        return len(self.els)

    def nth(self, i: int) -> Any:
        return self.els[i]

    @property
    def first(self) -> Any:
        return self.els[0]

    def set_input_files(self, path: str) -> None:
        # a Locator acts on its one element (Playwright raises when several match)
        (el,) = self.els
        el.set_input_files(path)


class Form(FakeFrame):
    """The frame holding the form. file_inputs: selector -> file inputs (any
    other file-input selector finds none). values: what the form's values
    read as, one call per parser-wait snapshot."""

    def __init__(
        self,
        ids: tuple[str, ...],
        file_inputs: dict[str, list[Ctl]],
        values: Callable[[], str | None] | None = None,
    ) -> None:
        super().__init__(url=EMPLOYER_URL, ids=ids)
        self.file_inputs, self.values = file_inputs, values

    def locator(self, sel: str) -> Any:
        if sel in self.file_inputs:
            return Many(self.file_inputs[sel])
        if sel.startswith('input[type="file"]'):
            return Many([])
        return super().locator(sel)

    def evaluate(self, js: str, arg: Any = None) -> Any:
        if js == browser._FORM_VALUES_JS and self.values is not None:
            return self.values()
        return None


class Shots(FakePage):
    def __init__(self, frames: list[FakeFrame]) -> None:
        super().__init__(frames)
        self.shots: list[str] = []

    def screenshot(self, **kw: Any) -> None:
        self.shots.append(str(kw.get("path")))


def _field(identity: str, label: str, ftype: str = "input_text", required: bool = True) -> Any:
    return {"identity": identity, "label": label, "type": ftype, "required": required, "match": {}}


# the employer form's captured spec, the part these tests need
EMPLOYER_FIELDS = [
    _field("first_name", "First Name"),
    _field("email", "Email"),
    _field("phone", "Phone"),
    _field("resume", "Resume/CV", "input_file"),
    _field("resume_text", "Resume/CV", "textarea"),
]


def _events(home: Path, name: str) -> list[dict[str, Any]]:
    log = home / "logs" / "jobkit.jsonl"
    rows = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    return [r for r in rows if r["event"] == name]


@pytest.fixture()
def staging(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., dict[str, Any]]]:
    """Run stage_job on an employer-hosted job. Returns run(fields, controls, form)
    -> {result, filled, unfilled, run, job_id}."""
    (jobkit_home / "data" / "answer_bank.yaml").write_text(yaml.safe_dump(BANK))
    answer_bank.load_bank.cache_clear()
    resume = jobkit_home / "out" / "wonka-7001" / "resume.pdf"
    resume.parent.mkdir(parents=True)
    resume.write_bytes(b"%PDF-1.4 the tailored resume")
    conn.execute(
        "INSERT INTO companies (id, name, normalized_name, domain) "
        "VALUES (1, 'Wonka', 'wonka', 'wonka.example')"
    )
    jid = conn.execute(
        """INSERT INTO jobs (company_id, external_id, title, normalized_title, company_name,
           normalized_company, location_bucket, cluster_key, jd_html, jd_hash, apply_url, status)
           VALUES (1, '7004321', 'Partner Sales Manager, Wholesale', 'psm', 'Wonka',
                   'wonka', 'remote-us', 'k', 'jd', 'h', ?, 'approved')""",
        (EMPLOYER_URL,),
    ).lastrowid
    assert jid is not None
    conn.execute(
        "INSERT INTO applications (job_id, stage, resume_path) VALUES (?, 'tailored', ?)",
        (jid, str(resume)),
    )
    conn.commit()

    def run(fields: list[dict[str, Any]], controls: dict[str, Any], form: Form) -> dict[str, Any]:
        conn.execute("DELETE FROM form_specs WHERE job_id=?", (jid,))
        conn.execute(
            "INSERT INTO form_specs (job_id, source, fields, drafted_answers) "
            "VALUES (?, 'api_questions', ?, '{}')",
            (jid, json.dumps(fields)),
        )
        conn.commit()
        monkeypatch.setattr(browser, "_find_control", lambda scope, i, label: controls.get(i))
        page = Shots([form])
        matcher = BankMatcher(answer_bank.load_bank(), use_embeddings=False)
        r = browser.stage_job(conn, page, int(jid), matcher=matcher)  # type: ignore[arg-type]
        row = conn.execute(
            "SELECT * FROM autofill_runs WHERE job_id=? ORDER BY id DESC LIMIT 1", (jid,)
        ).fetchone()
        return {
            "result": r,
            "filled": {f["identity"]: f for f in json.loads(row["filled_fields"])},
            "unfilled": {u["identity"]: u for u in json.loads(row["unfilled_fields"])},
            "run": row,
            "job_id": int(jid),
            "page": page,
            "resume": resume,
        }

    yield run
    answer_bank.load_bank.cache_clear()


def _text_controls(ops: list[tuple[str, ...]]) -> dict[str, Ctl]:
    return {i: Ctl(i, ops, attrs={"type": "text"}) for i in ("first_name", "email", "phone")}


def _file_input(name: str, label: str, ops: list[tuple[str, ...]]) -> Ctl:
    # what _FILE_INPUT_TEXTS_JS reads on the employer's page: no <label for>, no
    # aria; a generated name and id; then the field's own text
    return Ctl(
        name,
        ops,
        attrs={"type": "file", "name": name},
        texts=[
            "",
            "",
            "",
            name,
            name,
            f"{label} (required)",
            f"{label} (required)\nAccepted formats: PDF, DOC",
        ],
    )


# ---- the employer-hosted form's resume bug -----------------------------------------


def test_the_resume_goes_into_the_file_input_labelled_resume(
    staging: Callable[..., dict[str, Any]], jobkit_home: Path
) -> None:
    ops: list[tuple[str, ...]] = []
    resume_in = _file_input("file-1", "Resume", ops)
    cover_in = _file_input("file-2", "Cover Letter", ops)
    # "resume" names some other element on the page (not a file input), as
    # the old name lookup would have found
    form = Form(
        ids=("first_name", "email", "phone", "resume"),
        file_inputs={'input[type="file"]': [resume_in, cover_in]},
    )
    out = staging(EMPLOYER_FIELDS, _text_controls(ops), form)

    copy = browser.UPLOAD_DIR / f"job-{out['job_id']}" / "Pat_Doe_Resume.pdf"
    assert resume_in.files == [str(copy)] and cover_in.files == []
    assert copy.read_bytes() == b"%PDF-1.4 the tailored resume"
    # the original is untouched, under its own name
    assert out["resume"].read_bytes() == b"%PDF-1.4 the tailored resume"
    assert out["filled"]["resume"]["value"] == "Pat_Doe_Resume.pdf"
    # the paste-it-instead textarea is satisfied by the upload, not open
    assert "resume" not in out["unfilled"] and "resume_text" not in out["unfilled"]
    assert out["result"]["status"] == "staged", out["result"]
    (attached,) = _events(jobkit_home, "stage.attached")
    assert attached["found_by"] == "file-input-label" and attached["identity"] == "resume"
    assert _events(jobkit_home, "stage.attach_alternative_satisfied")[0]["identity"] == (
        "resume_text"
    )
    # uploads first: nothing was typed before the resume went up
    assert ops[0] == ("upload", "file-1", "Pat_Doe_Resume.pdf")
    assert {o[0] for o in ops[1:]} == {"fill"}


def test_resume_found_by_its_ats_id_first(
    staging: Callable[..., dict[str, Any]], jobkit_home: Path
) -> None:
    """An employer-hosted Greenhouse form whose file input is id="resume"."""
    ops: list[tuple[str, ...]] = []
    by_id = Ctl("resume", ops, attrs={"type": "file", "id": "resume"})
    form = Form(
        ids=("first_name", "email", "phone", "resume"),
        file_inputs={'input[type="file"][id="resume"]': [by_id]},
    )
    out = staging(EMPLOYER_FIELDS, _text_controls(ops), form)
    assert len(by_id.files) == 1 and out["result"]["status"] == "staged"
    assert _events(jobkit_home, "stage.attached")[0]["found_by"] == "file-input-id"


def test_two_inputs_claiming_the_resume_get_nothing_and_say_why(
    staging: Callable[..., dict[str, Any]], jobkit_home: Path
) -> None:
    ops: list[tuple[str, ...]] = []
    a, b = _file_input("file-1", "Resume", ops), _file_input("file-9", "Resume", ops)
    form = Form(ids=("first_name",), file_inputs={'input[type="file"]': [a, b]})
    out = staging(EMPLOYER_FIELDS, _text_controls(ops), form)
    assert a.files == [] and b.files == []
    assert out["unfilled"]["resume"] == {
        "identity": "resume",
        "reason": "attachment",
        "detail": "2-file-inputs-claim-resume",
    }
    assert out["unfilled"]["resume_text"]["detail"] == "resume-not-uploaded"
    assert out["result"]["status"] == "needs_human"
    failed = _events(jobkit_home, "stage.attach_failed")
    assert failed and failed[0]["why"] == "2-file-inputs-claim-resume"


def test_an_upload_the_page_refuses_is_logged_not_swallowed(
    staging: Callable[..., dict[str, Any]], jobkit_home: Path
) -> None:
    ops: list[tuple[str, ...]] = []
    bad = _file_input("file-1", "Resume", ops)

    def refuse(path: str) -> None:
        raise RuntimeError("Error: Non-multiple file input can only accept single file")

    bad.set_input_files = refuse  # type: ignore[method-assign]
    form = Form(ids=("first_name",), file_inputs={'input[type="file"]': [bad]})
    out = staging(EMPLOYER_FIELDS, _text_controls(ops), form)
    assert out["unfilled"]["resume"]["detail"].startswith("upload-failed:Error: Non-multiple")
    assert _events(jobkit_home, "stage.attach_failed")[0]["why"].startswith("upload-failed")


@pytest.mark.parametrize(
    ("texts", "kind"),
    [
        (["Resume/CV"], "resume"),
        (["", "", "", "resume", "resume"], "resume"),
        (["", "", "", "resumeUpload"], "resume"),
        (["CV"], "resume"),
        (["Résumé"], "resume"),
        (["Cover Letter"], "cover_letter"),
        (["", "", "", "cover_letter"], "cover_letter"),
        (["coverLetter"], "cover_letter"),
        # the input's own label decides before its field's wider text
        (["Resume", "Resume & Cover Letter"], "resume"),
        # never a guess
        (["Resume or cover letter"], None),
        (["Portfolio", "Work samples"], None),
        (["Discover our team"], None),
        (["CVV"], None),
        ([], None),
    ],
)
def test_file_input_kind(texts: list[str], kind: str | None) -> None:
    assert file_input_kind(texts) == kind


# ---- item 5: recruiter-friendly names, originals untouched -------------------------


@pytest.mark.parametrize(
    ("first", "last", "entry", "suffix", "name"),
    [
        ("Pat", "Doe", "resume", ".pdf", "Pat_Doe_Resume.pdf"),
        ("Pat", "Doe", "cover_letter", ".pdf", "Pat_Doe_Cover_Letter.pdf"),
        ("José", "O'Neil-Smith", "resume", ".PDF", "Jose_O_Neil-Smith_Resume.pdf"),
        ("Mary Ann", "van der Berg", "resume", ".docx", "Mary_Ann_van_der_Berg_Resume.docx"),
        ("../../etc", "passwd", "resume", ".pdf", "etc_passwd_Resume.pdf"),
        ("Pat", "Doe", "resume", "", "Pat_Doe_Resume"),
        ("Pat", "", "resume", ".pdf", None),
        ("", "Doe", "resume", ".pdf", None),
        ("Pat", "Doe", "portfolio", ".pdf", None),
    ],
)
def test_upload_name(first: str, last: str, entry: str, suffix: str, name: str | None) -> None:
    bank = {"identity": {"first_name": first, "last_name": last}}
    assert browser.upload_name(bank, entry, suffix) == name


def test_upload_copies_leave_the_originals_alone(jobkit_home: Path) -> None:
    folder = jobkit_home / "out" / "acme-7"
    folder.mkdir(parents=True)
    resume, cover = folder / "resume.pdf", folder / "cover.pdf"
    resume.write_bytes(b"resume bytes")
    cover.write_bytes(b"cover bytes")
    before = sorted(p.name for p in folder.iterdir())
    bank = {"identity": {"first_name": "Pat", "last_name": "Doe"}}
    out = browser.upload_copies(7, bank, {"resume": str(resume), "cover_letter": str(cover)})
    assert out == {
        "resume": browser.UPLOAD_DIR / "job-7" / "Pat_Doe_Resume.pdf",
        "cover_letter": browser.UPLOAD_DIR / "job-7" / "Pat_Doe_Cover_Letter.pdf",
    }
    assert out["resume"].read_bytes() == b"resume bytes"
    assert out["cover_letter"].read_bytes() == b"cover bytes"
    assert sorted(p.name for p in folder.iterdir()) == before
    assert resume.read_bytes() == b"resume bytes" and cover.read_bytes() == b"cover bytes"
    # no name in the bank: the original goes up as before, and the log says so
    out = browser.upload_copies(7, {"identity": {"first_name": "Pat"}}, {"resume": str(resume)})
    assert out == {"resume": resume}
    assert _events(jobkit_home, "stage.upload_name_unavailable")[0]["entry"] == "resume"
    # a missing file is left out
    assert browser.upload_copies(7, bank, {"resume": None, "cover_letter": "nope.pdf"}) == {}


# ---- item 2: the resume parser, then the fill, then the read-back ------------------


def test_the_fill_waits_for_the_resume_parser_to_settle(
    staging: Callable[..., dict[str, Any]], jobkit_home: Path
) -> None:
    ops: list[tuple[str, ...]] = []
    snaps = iter(["||", "||", "Patricia|p@x.io|", "Patricia|p@x.io|5550100"])

    def values() -> str:
        ops.append(("snapshot",))
        return next(snaps, "Patricia|p@x.io|5550100")

    form = Form(
        ids=("first_name",),
        file_inputs={'input[type="file"]': [_file_input("file-1", "Resume", ops)]},
        values=values,
    )
    out = staging(EMPLOYER_FIELDS, _text_controls(ops), form)
    (wait,) = _events(jobkit_home, "stage.parser_wait")
    assert wait["changed"] is True and wait["settled"] is True
    assert wait["waited_ms"] <= browser.PARSER_WAIT_MS
    kinds = [o[0] for o in ops]
    last_snapshot = max(i for i, k in enumerate(kinds) if k == "snapshot")
    first_fill = kinds.index("fill")
    assert kinds[0] == "upload" and last_snapshot < first_fill
    assert out["result"]["status"] == "staged"


@pytest.mark.parametrize(
    ("snapshots", "waited", "changed", "settled"),
    [
        (lambda n: str(n), browser.PARSER_WAIT_MS, True, False),  # never settles: bounded
        (lambda n: "same", browser.PARSER_MIN_WAIT_MS, False, True),  # parses nothing
        (lambda n: None, 500, False, False),  # unreadable: one step
    ],
    ids=["never-settles", "no-parser", "unreadable"],
)
def test_parser_wait_is_bounded(
    snapshots: Callable[[int], str | None], waited: int, changed: bool, settled: bool
) -> None:
    calls = iter(range(10_000))
    form = Form(ids=(), file_inputs={}, values=lambda: snapshots(next(calls)))
    out = browser.await_parser(FakePage([form]), form, 1)  # type: ignore[arg-type]
    assert out == {"waited_ms": waited, "changed": changed, "settled": settled}


def test_a_value_the_page_keeps_changing_goes_to_the_human(
    staging: Callable[..., dict[str, Any]], conn: sqlite3.Connection, jobkit_home: Path
) -> None:
    ops: list[tuple[str, ...]] = []
    controls = _text_controls(ops)
    # the ATS's parser writes its own reading of the resume over the name
    controls["first_name"].after_fill = lambda c: setattr(c, "value", "Patricia")
    form = Form(
        ids=("first_name",),
        file_inputs={'input[type="file"]': [_file_input("file-1", "Resume", ops)]},
    )
    out = staging(EMPLOYER_FIELDS, controls, form)
    assert [o for o in ops if o[:2] == ("fill", "first_name")] == [
        ("fill", "first_name", "Pat"),
        ("fill", "first_name", "Pat"),  # refilled once, never more
    ]
    assert "first_name" not in out["filled"]
    assert out["unfilled"]["first_name"] == {
        "identity": "first_name",
        "reason": "overwritten-after-fill",
        "wanted": "Pat",
        "found": "Patricia",
    }
    r = out["result"]
    assert r["status"] == "needs_human" and r["overwritten_after_fill"] == ["first_name"]
    assert r["readback_mismatches"] == 1
    assert out["run"]["abort_reason"] == "required-fields-open:1; overwritten-after-fill:1"
    ev = conn.execute(
        "SELECT detail FROM events WHERE job_id=? AND kind='readback'", (out["job_id"],)
    ).fetchone()
    readback = json.loads(ev["detail"])
    assert readback["run_id"] == out["run"]["id"]
    assert readback["mismatches"] == 1 and readback["overwritten_after_fill"] == 1
    assert readback["checked"] == 3 and readback["overwritten"] == ["first_name"]
    staged = conn.execute(
        "SELECT detail FROM events WHERE job_id=? AND kind='staged'", (out["job_id"],)
    ).fetchone()
    assert staged["detail"].endswith("readback_mismatches=1")
    assert _events(jobkit_home, "stage.overwritten_after_fill")[0]["identity"] == "first_name"
    # MCP get_job shows it
    lines = browser.staging_lines(conn, out["job_id"])
    assert any("first_name: overwritten-after-fill" in x for x in lines), lines


def test_one_refill_that_holds_keeps_the_field_filled(
    staging: Callable[..., dict[str, Any]], jobkit_home: Path
) -> None:
    ops: list[tuple[str, ...]] = []
    controls = _text_controls(ops)
    once = iter([True])
    controls["email"].after_fill = lambda c: setattr(c, "value", "") if next(once, False) else None
    form = Form(
        ids=("first_name",),
        file_inputs={'input[type="file"]': [_file_input("file-1", "Resume", ops)]},
    )
    out = staging(EMPLOYER_FIELDS, controls, form)
    r = out["result"]
    assert r["status"] == "staged" and r["readback_mismatches"] == 1
    assert r["overwritten_after_fill"] == [] and r["readback"]["fixed_by_refill"] == 1
    assert out["filled"]["email"]["value"] == "pat@example.com"


def test_an_ats_reformatting_the_phone_is_not_a_mismatch(
    staging: Callable[..., dict[str, Any]],
) -> None:
    ops: list[tuple[str, ...]] = []
    controls = _text_controls(ops)
    controls["phone"].after_fill = lambda c: setattr(c, "value", "+15550100")
    form = Form(
        ids=("first_name",),
        file_inputs={'input[type="file"]': [_file_input("file-1", "Resume", ops)]},
    )
    out = staging(EMPLOYER_FIELDS, controls, form)
    assert out["result"]["readback_mismatches"] == 0 and out["result"]["status"] == "staged"


@pytest.mark.parametrize(
    ("want", "got", "same"),
    [
        ("+1 555 555 0100", "+15555550100", True),
        ("+1 555 555 0100", "(555) 555-0100", True),
        ("+1 555 0100", "+1 555 0199", False),
        ("https://linkedin.com/in/pat", "linkedin.com/in/pat/", True),
        ("https://linkedin.com/in/pat", "https://www.linkedin.com/in/pat", True),
        ("https://linkedin.com/in/pat", "https://linkedin.com/in/someone", False),
        ("Pat", "PAT ", True),
        ("Pat", "", False),
        ("Pat", "Patricia", False),
        ("A long answer about quota.", "A long answer about", False),  # cut off
        ("19007", "19007", True),
    ],
)
def test_same_text(want: str, got: str, same: bool) -> None:
    assert same_text(want, got) is same


class Probe:
    def __init__(self, js_result: Any = None, checked: bool = False, raises: bool = False):
        self.js_result, self.checked, self.raises = js_result, checked, raises

    def evaluate(self, js: str, arg: Any = None) -> Any:
        if self.raises:
            raise RuntimeError("detached")
        return self.js_result

    def is_checked(self) -> bool:
        return self.checked

    def input_value(self) -> str:
        if self.raises:
            raise RuntimeError("detached")
        return str(self.js_result or "")


@pytest.mark.parametrize(
    ("note", "result"),
    [
        (FillNote("select", Probe("Yes"), "Yes"), (True, "Yes")),  # type: ignore[arg-type]
        (FillNote("select", Probe("Select..."), "Yes"), (False, "Select...")),  # type: ignore[arg-type]
        (FillNote("radio", Probe(checked=False), "No"), (False, "not checked")),  # type: ignore[arg-type]
        (FillNote("checkbox", Probe(checked=True), "x"), (True, "checked")),  # type: ignore[arg-type]
        (FillNote("listed", Probe({"value": "", "shown": ["Yes"]}), "Yes"), (True, "yes")),  # type: ignore[arg-type]
        (
            FillNote("listed", Probe({"value": "", "placeholder": ["Select..."]}), "Yes"),
            (False, "select..."),
        ),  # type: ignore[arg-type]
        (FillNote("listed", Probe({"value": "", "shown": []}), "Yes"), (None, "")),  # type: ignore[arg-type]
        (
            FillNote(
                "location",
                Probe({"value": "Langhorne"}),
                "Langhorne, Pennsylvania, United States",
            ),  # type: ignore[arg-type]
            (True, "langhorne"),
        ),
        (FillNote("button", Probe("true"), "Yes"), (True, "aria=true")),  # type: ignore[arg-type]
        (FillNote("button", Probe(""), "Yes"), (None, "")),  # type: ignore[arg-type]
        (FillNote("text", Probe(raises=True), "x"), (None, "")),  # type: ignore[arg-type]
        (FillNote("select", Probe(raises=True), "x"), (None, "")),  # type: ignore[arg-type]
        (FillNote(), (None, "")),
    ],
)
def test_read_back(note: FillNote, result: tuple[bool | None, str]) -> None:
    assert read_back(note) == result


# Review 2026-10-12: _SHOWN_VALUE_JS read the innerText of up to 3 enclosing
# elements, the question's label among them, and read_back took any substring:
# expected "No", input '', boxes ['Select...', 'Will you now or in the future
# require sponsorship?* Select...'] read back as (True, ...) because "no" is in
# "now". The probe now returns only what the widget shows as its value, and a
# value must BE the answer (a whole line of it).
@pytest.mark.parametrize(
    ("how", "shown", "expect", "result"),
    [
        # the reviewer's cleared widget: nothing shown as a value -> unverified
        ("listed", {"value": "", "shown": [], "own": "", "placeholder": []}, "No", (None, "")),
        # the old probe's output shape is not read at all
        (
            "listed",
            ["", "Will you now or in the future require sponsorship?* Select..."],
            "No",
            (None, ""),
        ),
        # a placeholder where the answer should be: changed, refilled once
        ("listed", {"value": "", "placeholder": ["Select..."]}, "No", (False, "select...")),
        # another answer, or one that merely contains the expected text
        ("listed", {"value": "", "shown": ["Yes"]}, "No", (False, "yes")),
        ("listed", {"value": "", "shown": ["Not sure"]}, "No", (False, "not sure")),
        ("listed", {"value": "", "shown": ["I know"]}, "No", (False, "i know")),
        ("listed", {"value": "Now"}, "No", (False, "now")),
        # a button-style dropdown shows its value as its own text
        ("listed", {"value": "", "own": "No"}, "No", (True, "no")),
        ("listed", {"value": "", "own": "Select...\nNo"}, "No", (True, "select...")),
        ("listed", {"value": "", "own": "Select..."}, "No", (False, "select...")),
        # the input holds the chosen option itself
        ("listed", {"value": "No"}, "No", (True, "no")),
        # a location box: the whole suggestion or its city, nothing looser
        (
            "location",
            {"value": "Bristol, Pennsylvania, United States"},
            "Bristol, Pennsylvania, United States",
            (True, "bristol, pennsylvania, united states"),
        ),
        (
            "location",
            {"value": "Bristol"},
            "Bristol, Pennsylvania, United States",
            (True, "bristol"),
        ),
        (
            "location",
            {"value": "Bristol, CT"},
            "Bristol, Pennsylvania, United States",
            (False, "bristol, ct"),
        ),
        (
            "location",
            {"value": "", "shown": []},
            "Bristol, Pennsylvania, United States",
            (None, ""),
        ),
    ],
)
def test_read_back_compares_only_the_shown_value(
    how: str, shown: Any, expect: str, result: tuple[bool | None, str]
) -> None:
    assert read_back(FillNote(how, Probe(shown), expect)) == result  # type: ignore[arg-type]


def test_the_shown_value_probe_never_reads_the_label() -> None:
    js = browser._SHOWN_VALUE_JS
    # the widget stops below any <label>/<legend> and any other control, and
    # no enclosing element's whole text is collected
    assert "querySelector('label, legend')" in js and "others.length" in js
    assert "box.innerText" not in js


# ---- item 4: a CAPTCHA the fill provokes stops the run ---------------------------


AGE_FIELDS = [
    _field("first_name", "First Name"),
    _field("q_age", "Are you at least 18 years of age?", "multi_value_single_select"),
    _field("email", "Email"),
]


def test_a_captcha_after_a_checkbox_click_stops_the_run(
    staging: Callable[..., dict[str, Any]], conn: sqlite3.Connection, jobkit_home: Path
) -> None:
    ops: list[tuple[str, ...]] = []
    controls: dict[str, Any] = _text_controls(ops)
    form = Form(ids=("first_name",), file_inputs={})
    age = Ctl("q_age", ops, attrs={"type": "checkbox"})
    # Lever-style: an hCaptcha challenge appears after a scripted checkbox click
    age.after_check = lambda c: form.widgets.update({"hcaptcha-iframe": [HCAPTCHA]})
    controls["q_age"] = age
    out = staging(AGE_FIELDS, controls, form)

    r = out["result"]
    assert r["status"] == "needs_human" and r["reason"] == "captcha-provoked-by-fill:q_age"
    assert r["detail"]["control"] == "q_age" and r["detail"]["kind"] == "checkbox"
    assert r["detail"]["filled_before"] == ["first_name"]
    assert "hcaptcha-iframe" in r["detail"]["widget"]
    # it stopped at once: the field after the click was never filled
    assert ("fill", "email", "pat@example.com") not in ops
    assert out["page"].shots, "a screenshot of the challenge"
    run = out["run"]
    assert run["abort_reason"] == "captcha-provoked-by-fill:q_age"
    assert run["filled_fields"] == "[]" and run["screenshot_path"]
    (ev,) = _events(jobkit_home, "stage.captcha_provoked")
    assert ev["control"] == "q_age" and ev["label"].startswith("Are you at least 18")
    # the human sees it in the halts list (digest, get_queue, schedule status)
    (halt,) = halts.waiting(conn)
    assert halt.job_id == out["job_id"] and halt.reason == "captcha-provoked-by-fill:q_age"
    assert "CAPTCHA appeared after staging clicked" in halt.meaning()
    assert not halt.next_nightly_retries


def test_a_captcha_seen_after_the_fill_names_the_last_control(
    staging: Callable[..., dict[str, Any]],
) -> None:
    ops: list[tuple[str, ...]] = []
    controls = _text_controls(ops)
    form = Form(ids=("first_name",), file_inputs={})
    # no click brought it up: a challenge that shows up once typing is done
    controls["phone"].after_fill = lambda c: form.widgets.update({"hcaptcha-iframe": [HCAPTCHA]})
    fields = [
        _field("first_name", "First Name"),
        _field("email", "Email"),
        _field("phone", "Phone"),
    ]
    out = staging(fields, controls, form)
    r = out["result"]
    assert r["reason"] == "captcha-provoked-by-fill:phone" and r["detail"]["kind"] == "after-fill"
    # every field was filled first: the per-click check is not what caught it
    assert [o[1] for o in ops if o[0] == "fill"] == ["first_name", "email", "phone"]


def test_no_captcha_means_no_halt(staging: Callable[..., dict[str, Any]]) -> None:
    ops: list[tuple[str, ...]] = []
    controls: dict[str, Any] = _text_controls(ops)
    controls["q_age"] = Ctl("q_age", ops, attrs={"type": "checkbox"})
    out = staging(AGE_FIELDS, controls, Form(ids=("first_name",), file_inputs={}))
    assert out["result"]["status"] == "staged"
    assert ("check", "q_age") in ops and ("fill", "email", "pat@example.com") in ops


# Review 2026-10-12: Playwright's check() clicks and THEN raises "Clicking the
# checkbox did not change its state" when a challenge swallowed the click.
# The click flag used to be set only after check() returned, so the CAPTCHA
# check was skipped for exactly that click and the fill went on.
SWALLOWED = "Locator.check: Clicking the checkbox did not change its state"


def _swallowing_check(form: Form, after: int = 1) -> Callable[[Ctl], None]:
    """after_check: on the `after`-th check a challenge takes the click: the
    box stays unticked, hCaptcha shows, and check() raises. Before that the
    page unticks the box (so the read-back finds it changed)."""
    calls = iter(range(1, 100))

    def run(c: Ctl) -> None:
        c.checked = False
        if next(calls) >= after:
            form.widgets.update({"hcaptcha-iframe": [HCAPTCHA]})
            raise RuntimeError(SWALLOWED)

    return run


def test_a_check_that_raises_after_clicking_still_stops_the_run(
    staging: Callable[..., dict[str, Any]], jobkit_home: Path
) -> None:
    ops: list[tuple[str, ...]] = []
    controls: dict[str, Any] = _text_controls(ops)
    form = Form(ids=("first_name",), file_inputs={})
    age = Ctl("q_age", ops, attrs={"type": "checkbox"})
    age.after_check = _swallowing_check(form)
    controls["q_age"] = age
    out = staging(AGE_FIELDS, controls, form)
    r = out["result"]
    assert r["status"] == "needs_human" and r["reason"] == "captcha-provoked-by-fill:q_age"
    # named after the control whose click it took, though check() raised
    assert r["detail"]["control"] == "q_age" and r["detail"]["kind"] == "checkbox"
    assert ("fill", "email", "pat@example.com") not in ops
    assert _events(jobkit_home, "stage.fill_error")[0]["error"].startswith(SWALLOWED[:30])


def test_a_raising_check_is_followed_by_the_captcha_check_before_the_yes_no_pair(
    staging: Callable[..., dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    ops: list[tuple[str, ...]] = []
    controls: dict[str, Any] = _text_controls(ops)
    form = Form(ids=("first_name",), file_inputs={})
    age = Ctl("q_age", ops, attrs={"type": "checkbox"})
    age.after_check = _swallowing_check(form)
    controls["q_age"] = age
    pair_clicks: list[str] = []
    monkeypatch.setattr(
        browser, "_click_boolean_pair", lambda *a, **k: bool(pair_clicks.append(a[1]))
    )
    fields = [
        _field("first_name", "First Name"),
        _field("q_age", "Are you at least 18 years of age?", "boolean"),
        _field("email", "Email"),
    ]
    out = staging(fields, controls, form)
    assert out["result"]["reason"] == "captcha-provoked-by-fill:q_age"
    assert pair_clicks == [], "no Yes/No click while a challenge is showing"


def test_a_refill_check_that_raises_after_clicking_stops_the_run(
    staging: Callable[..., dict[str, Any]],
) -> None:
    ops: list[tuple[str, ...]] = []
    controls: dict[str, Any] = _text_controls(ops)
    form = Form(ids=("first_name",), file_inputs={})
    age = Ctl("q_age", ops, attrs={"type": "checkbox"})
    # the page unticks the box after the fill; the refill's check is swallowed
    age.after_check = _swallowing_check(form, after=2)
    controls["q_age"] = age
    out = staging(AGE_FIELDS, controls, form)
    r = out["result"]
    assert r["reason"] == "captcha-provoked-by-fill:q_age" and r["detail"]["kind"] == "checkbox"
    assert [o for o in ops if o[0] == "check"] == [("check", "q_age"), ("check", "q_age")]


def test_a_challenge_showing_before_the_read_back_stops_every_refill(
    staging: Callable[..., dict[str, Any]],
) -> None:
    ops: list[tuple[str, ...]] = []
    controls: dict[str, Any] = _text_controls(ops)
    form = Form(ids=("first_name",), file_inputs={})
    age = Ctl("q_age", ops, attrs={"type": "checkbox"})
    # the page unticks the box (a refill would click it again), and a
    # challenge shows once typing is done: no click was seen to bring it up
    age.after_check = lambda c: setattr(c, "checked", False)
    controls["q_age"] = age
    controls["email"].after_fill = lambda c: form.widgets.update({"hcaptcha-iframe": [HCAPTCHA]})
    out = staging(AGE_FIELDS, controls, form)
    r = out["result"]
    assert r["reason"] == "captcha-provoked-by-fill:email" and r["detail"]["kind"] == "after-fill"
    assert [o for o in ops if o[0] == "check"] == [("check", "q_age")], "no refill click"


def test_the_click_flag_is_set_before_the_click() -> None:
    class Raising:
        def evaluate(self, js: str, arg: Any = None) -> Any:
            return "" if js == browser._CLICK_GUARD_JS else "input"

        def get_attribute(self, name: str) -> str | None:
            return "checkbox" if name == "type" else None

        def check(self, **kw: Any) -> None:
            raise RuntimeError(SWALLOWED)

    note = FillNote()
    with pytest.raises(RuntimeError, match="did not change its state"):
        browser._fill_control(Page(), FakeFrame(), Raising(), "Yes", "checkbox", [], None, note)  # type: ignore[arg-type]
    assert note.clicked and note.kind() == "checkbox" and not note.how


class Page:
    def wait_for_timeout(self, ms: float) -> None:
        return None


# ---- the container lookup (Race into Location) -----------------------------------
# tests/test_question_control.py: question_control over the page tree.


class Box:
    """One matched container for the Yes/No pair: its controls."""

    def __init__(self, controls: list[Any]) -> None:
        self.controls = controls

    def get_by_role(self, role: str, name: str = "", exact: bool = False) -> Many:
        return Many([c for c in self.controls if c.text == name and role == "button"])


class Matches:
    """The containers a :has-text selector matched, outermost first, and the
    innermost indices _INNERMOST_JS would return."""

    def __init__(self, boxes: list[Box], inner: list[int] | Exception) -> None:
        self.boxes, self.inner = boxes, inner
        self.need: str | None = None

    def count(self) -> int:
        return len(self.boxes)

    def evaluate_all(self, js: str, need: str) -> list[int]:
        assert js == browser._INNERMOST_JS
        self.need = need
        if isinstance(self.inner, Exception):
            raise self.inner
        return self.inner

    def nth(self, i: int) -> Box:
        return self.boxes[i]

    @property
    def first(self) -> Box:
        return self.boxes[0]


class Scope:
    def __init__(self, matches: Matches) -> None:
        self.matches = matches

    def locator(self, sel: str) -> Matches:
        return self.matches


def test_an_ambiguous_control_is_flagged_and_nothing_is_typed(
    staging: Callable[..., dict[str, Any]],
) -> None:
    """A label whose page lookup finds two questions' controls (a shared
    wrapper) leaves the field blank as control-ambiguous, and the run goes
    on: nothing is typed into either box."""
    ops: list[tuple[str, ...]] = []
    controls: dict[str, Any] = _text_controls(ops)

    class Refusing(dict[str, Any]):
        def get(self, key: str, default: Any = None) -> Any:
            if key == "email":
                raise browser.ControlAmbiguous("container-holds-several-questions")
            return super().get(key, default)

    form = Form(ids=("first_name",), file_inputs={})
    out = staging(AGE_FIELDS, Refusing(controls), form)
    assert out["unfilled"]["email"]["reason"] == (
        "control-ambiguous:container-holds-several-questions"
    )
    assert not any(o[1] == "email" for o in ops if o[0] == "fill")
    assert "first_name" in out["filled"]


class Button:
    """A Yes/No button. pair: what _YES_NO_PAIR_JS reports for it in the
    page ('' = it and the other answer are one question's pair)."""

    def __init__(self, text: str, pair: str = "") -> None:
        self.text, self.pair = text, pair
        self.clicks = 0
        self.pair_args: list[Any] = []

    def evaluate(self, js: str, arg: Any = None) -> Any:
        if js == browser._YES_NO_PAIR_JS:
            self.pair_args.append(arg)
            return self.pair
        assert js == browser._CLICK_GUARD_JS
        return ""

    def click(self, **kw: Any) -> None:
        self.clicks += 1


def test_yes_no_pair_clicks_in_the_innermost_field() -> None:
    other_no, our_no = Button("No"), Button("No")
    wrapper = Box([other_no, our_no])  # the whole form: another question's No first
    field = Box([our_no])
    matches = Matches([wrapper, field], [1])
    note = FillNote()
    assert browser._click_boolean_pair(Scope(matches), "Will you require sponsorship?", "No", note)  # type: ignore[arg-type]
    assert (other_no.clicks, our_no.clicks) == (0, 1)
    assert note.clicked and note.how == "button" and note.expect == "No"
    assert matches.need == browser._YES_NO_CONTROLS
    # the one-question probe ran on the button about to be clicked
    assert our_no.pair_args == [["Will you require sponsorship?", "no", "yes"]]


def test_yes_no_pair_never_clicks_in_a_wrapper_holding_two_questions(jobkit_home: Path) -> None:
    """Review 2026-10-12: <div class="application-fields"> holds two
    <div class="question"> blocks, each with Yes/No. The question divs do not
    match the field selector, so the wrapper is the innermost match, and its
    first "No" was the travel question's: 'Are you currently employed by a
    competitor?' clicked q1n."""
    q1_yes, q1_no, q2_yes, q2_no = Button("Yes"), Button("No"), Button("Yes"), Button("No")
    wrapper = Box([q1_yes, q1_no, q2_yes, q2_no])
    matches = Matches([wrapper], [0])
    note = FillNote()
    label = "Are you currently employed by a competitor?"
    assert not browser._click_boolean_pair(Scope(matches), label, "No", note)  # type: ignore[arg-type]
    assert [b.clicks for b in (q1_yes, q1_no, q2_yes, q2_no)] == [0, 0, 0, 0]
    assert not note.clicked and not note.how
    (ev,) = _events(jobkit_home, "stage.yes_no_unclear")
    assert ev["reason"] == "2-No" and ev["want"] == "No"


@pytest.mark.parametrize(
    "why",
    [
        "another-question-beside-it",  # the one "No" in the wrapper is another question's
        "other-controls-beside-it",  # our question is a checkbox beside another's pair
        "several-yes-no-pairs",
        "label-not-found",
        "no-yes-beside-it",
    ],
)
def test_yes_no_pair_never_clicks_a_button_the_page_says_is_not_ours(
    why: str, jobkit_home: Path
) -> None:
    # one "No" in the container, but the in-page probe finds it belongs to
    # another question (or cannot tell): nothing is clicked
    no = Button("No", pair=why)
    matches = Matches([Box([Button("Yes"), no])], [0])
    note = FillNote()
    assert not browser._click_boolean_pair(Scope(matches), "Do you have a non-compete?", "No", note)  # type: ignore[arg-type]
    assert no.clicks == 0 and not note.clicked
    (ev,) = _events(jobkit_home, "stage.yes_no_unclear")
    assert ev["reason"] == why


def test_the_yes_no_probe_is_read_only_and_pinned_to_one_question() -> None:
    js = browser._YES_NO_PAIR_JS
    assert ".click(" not in js and "dispatchEvent" not in js and "Enter" not in js
    for why in (
        "several-yes-no-pairs",
        "another-question-beside-it",
        "other-controls-beside-it",
        "label-not-found",
    ):
        assert why in js


# ---- the static and runtime submit guards still see every click ------------------


def test_new_code_adds_no_unguarded_click_and_no_submit_selector() -> None:
    from tests.test_click_guard import unguarded_clicks
    from tests.test_form_specs import AUTOFILL_DIR, submit_guard_violations

    src = (AUTOFILL_DIR / "browser.py").read_text()
    assert unguarded_clicks(src) == []
    assert submit_guard_violations(src) == []
    for js in (
        browser._FILE_INPUT_TEXTS_JS,
        browser._FORM_VALUES_JS,
        browser._SHOWN_VALUE_JS,
        browser._SELECTED_TEXT_JS,
        browser._PRESSED_JS,
        browser._INNERMOST_JS,
        browser._FORM_TREE_JS,
        browser._CONTROL_KINDS_JS,
        browser._CONTROL_PRINT_JS,
        browser._YES_NO_PAIR_JS,
    ):  # read-only probes: no click, no event, no key
        assert ".click(" not in js and "dispatchEvent" not in js and "Enter" not in js


def test_staging_lines_without_a_run_are_empty(conn: sqlite3.Connection) -> None:
    assert browser.staging_lines(conn, 999) == []
    assert conn.execute(db.LATEST_STAGING_RUN, {"job_id": 999}).fetchone() is None


def test_the_nightly_summary_names_values_the_form_changed(conn: sqlite3.Connection) -> None:
    from jobkit.pipeline import nightly

    conn.execute("INSERT INTO companies (id, name, normalized_name) VALUES (1, 'Acme', 'acme')")
    jid = conn.execute(
        """INSERT INTO jobs (company_id, external_id, title, normalized_title, company_name,
           normalized_company, location_bucket, cluster_key, jd_html, jd_hash, apply_url, status)
           VALUES (1, 'x', 'AE', 'ae', 'Acme', 'acme', 'remote-us', 'k', 'jd', 'h', 'u',
                   'approved')"""
    ).lastrowid
    result = {
        "job_id": jid,
        "status": "needs_human",
        "required_open": 0,
        "overwritten_after_fill": ["first_name"],
    }
    assert nightly._needs_you(conn, [result]) == [
        f"job {jid} Acme: 0 required question(s) open, 1 value(s) the form changed after "
        "filling (first_name), open_queue reopens it"
    ]
