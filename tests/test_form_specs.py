"""M2b: form-spec parsing, answer-bank matching, friction, and the MANDATORY
submit-guard test — a static (AST) scan of src/jobkit/autofill/ for anything
that could target a submit/apply/send control (CLAUDE.md hard rule 1). All offline."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from jobkit.autofill import browser
from jobkit.autofill.answer_bank import BankMatcher, never_draft
from jobkit.autofill.form_spec import _friction, _gh_form, _lever_form
from tests.conftest import FIXTURES, REPO_ROOT

TEST_BANK: dict[str, Any] = {
    "match_threshold": 0.82,
    "screening": [
        {
            "id": "work_auth",
            "aliases": ["Are you legally authorized to work in the United States?"],
            "answer": "Yes",
        },
        {
            "id": "sponsorship",
            "aliases": ["Will you now or in the future require sponsorship?"],
            "answer": "No",
        },
    ],
    "scaffolds": [
        {"id": "why_company", "aliases": ["Why do you want to work here?"]},
    ],
    "eeo": {
        "gender": "Male",
        "veteran_status": "I am not a protected veteran",
        "disability_status": None,
    },
}


def _client_returning(payload: Any, text: str | None = None) -> MagicMock:
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    resp.text = text or ""
    client.get.return_value = resp
    client.post.return_value = resp
    return client


def test_greenhouse_form_parse() -> None:
    payload = json.loads((FIXTURES / "gh_questions.json").read_text())
    fields = _gh_form(_client_returning(payload), "tok", "123")
    assert fields and len(fields) >= 10
    names = [f["identity"] for f in fields]
    assert "first_name" in names and "email" in names
    eeo = [f for f in fields if f["compliance_type"] == "eeo"]
    non_eeo = [f for f in fields if not f["compliance_type"]]
    assert non_eeo, "non-EEO questions present"
    # demographic questions, when present, must be separated
    if payload.get("demographic_questions"):
        assert eeo, "demographic questions must be flagged eeo"


def test_lever_form_parse() -> None:
    html = (FIXTURES / "lever_apply.html").read_text()
    fields = _lever_form(_client_returning(None, text=html), "tok", "id")
    assert fields and len(fields) >= 5
    assert any(f["compliance_type"] == "eeo" for f in fields) or not re.search(r"eeo\[", html), (
        "eeo[...] fields must be compliance-flagged when present"
    )
    assert all("identity" in f and "label" in f for f in fields)


def test_bank_matching_aliases_and_guards() -> None:
    m = BankMatcher(bank=TEST_BANK, use_embeddings=False)
    # alias hit
    assert m.match("Are you legally authorized to work in the United States?")["entry_id"] == (
        "work_auth"
    )
    # identity fields
    assert m.match("First Name")["kind"] == "identity"
    assert m.match("Resume/CV")["kind"] == "attach"
    # EEO with stored choice
    v = m.match("Gender", compliance_type="eeo")
    assert v["kind"] == "eeo" and v["answer"] == "Male"
    # EEO WITHOUT stored choice -> blank + flag, never guessed (hard rule 4)
    v = m.match("Disability Status", compliance_type="eeo")
    assert v["kind"] == "blank_flag"
    # EEO detected even without compliance flag
    assert m.match("Are you Hispanic or Latino?")["kind"] in ("blank_flag", "eeo")
    # salary history never answered (hard rule 5)
    assert m.match("What is your current salary?")["kind"] == "blank_flag"
    # unknown question -> novel
    assert m.match("Describe your favorite deployment war story")["kind"] == "novel"


def test_never_draft_rules() -> None:
    assert never_draft("What was your compensation at your last role?") == "compensation-related"
    assert never_draft("Do you hold a security clearance?") == "credential"
    assert never_draft("What is your gender identity?") == "demographic"
    assert never_draft("Why do you want this job?") is None


def test_friction_math() -> None:
    w = {"field_count": 1, "required_count": 2, "novel_question_count": 5, "has_assessment": 40}
    assert _friction(10, 5, 2, False, w) == 30
    assert _friction(10, 5, 2, True, w) == 70
    assert _friction(100, 50, 20, True, w) == 100  # capped


# ---- MANDATORY submit guard (CLAUDE.md hard rule 1) ----------------------------
#
# Every module under src/jobkit/autofill/ (the only package allowed to drive a
# browser; see test_browser_automation_confined_to_autofill) is parsed, and
# everything outside docstrings is checked for:
#   * any string literal mentioning "submit": selectors, ids like #submit_app, JS;
#   * a Playwright locator/action whose selector, text or name mentions
#     submit|apply|send: get_by_text("Apply").click(), page.click(sel),
#     locator(...).filter(has_text="Send"), get_by_role("button", name="Submit");
#   * a text selector naming apply/send anywhere ("text=Apply", ":has-text('Send')");
#   * the Enter key in any spelling: press("Enter"), "Control+Enter", a newline
#     typed via press_sequentially/type, a JS key 'Enter' (implicit form submit);
#   * .submit() / .requestSubmit() calls, in Python or inside JS strings;
#   * JS that clicks or synthesizes click/submit/key events inside evaluate
#     strings, unless that exact string is in _ALLOWED_JS with a reason.
# Paths resolve from this file, never from the cwd, so the scan cannot pass
# vacuously by finding zero files. Allowlisting is a reviewed edit to this file.

AUTOFILL_DIR = REPO_ROOT / "src" / "jobkit" / "autofill"

_TARGET_TEXT = re.compile(r"submit|apply|send", re.I)
# Playwright calls that find an element, or act on one by selector/text/name
_SELECTOR_CALLS = frozenset(
    {
        "locator",
        "get_by_text",
        "get_by_role",
        "get_by_label",
        "get_by_title",
        "get_by_alt_text",
        "get_by_placeholder",
        "get_by_test_id",
        "filter",
        "and_",
        "or_",
        "frame_locator",
        "query_selector",
        "query_selector_all",
        "wait_for_selector",
        "eval_on_selector",
        "eval_on_selector_all",
        "click",
        "dblclick",
        "tap",
        "check",
        "uncheck",
        "set_checked",
        "hover",
        "focus",
        "press",
        "dispatch_event",
        "drag_and_drop",
        "drag_to",
    }
)
_KEY_CALLS = frozenset({"press", "press_sequentially", "type", "insert_text", "down", "up"})
_SUBMIT_METHODS = frozenset({"submit", "requestSubmit", "request_submit"})
_ENTER_KEY = re.compile(r"(?:^|\+)(?:Numpad)?Enter$")
_JS_ENTER = re.compile(r"""['"`](?:Numpad)?Enter['"`]|\bkeyCode\s*[:=]+\s*13\b""")
_JS_CLICK = re.compile(r"\.click\s*\(")
_JS_SYNTH_EVENT = re.compile(
    r"""new\s+\w*Event\s*\(\s*['"`](?:click|submit|keydown|keypress|keyup|"""
    r"""mousedown|mouseup|pointerdown|pointerup)""",
    re.I,
)
_TEXT_SELECTOR = re.compile(
    r"""(?:has-text|:text(?:-is|-matches)?|\btext\s*=)\s*\(?\s*['"]?[^'")]*?(?:apply|send)""",
    re.I,
)
# a form submit called from inside a JS string; checked on EVERY string, the
# allowlisted ones included
_JS_SUBMIT_CALL = re.compile(r"\b(?:requestSubmit|submit)\s*\(", re.I)

# Exact JS strings that may click, each with the reason it cannot reach a submit.
_ALLOWED_JS = {
    # browser._fill_control, radio branch: `el` is an input[type=radio] from the
    # field's own name-group, already matched to the answer; the click lands on
    # its wrapping <label> because styled radios hide the input.
    "el => { const l = el.closest('label'); (l || el).click(); }",
}

# Read-only probes that must NAME submit to detect it: the runtime click guard
# (browser.refuse_send_control). Exempt only from the "mentions submit" rule;
# the click, synthetic-event, Enter, text-selector and submit-call rules still
# apply to them. Pinned by hash: changing the probe is a reviewed edit here.
_GUARD_JS_SHA256 = "9a5ad605556d038ea8e47b4199da0ab0e6407074730af01cc59b3ef6a4d25e02"
_ALLOWED_GUARD_JS = {browser._CLICK_GUARD_JS}


def _python_files(root: Path) -> list[Path]:
    """*.py under root, skipping hidden dirs (stray .claude/worktrees copies)."""
    return sorted(
        p
        for p in root.rglob("*.py")
        if not any(
            part.startswith(".") or part == "__pycache__" for part in p.relative_to(root).parts
        )
    )


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                ids.add(id(first.value))
    return ids


def _strings_in(node: ast.AST) -> list[str]:
    return [
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def submit_guard_violations(source: str, name: str = "<source>") -> list[str]:
    tree = ast.parse(source, filename=name)
    docstrings = _docstring_ids(tree)
    found: list[str] = []

    def flag(node: ast.AST, why: str) -> None:
        found.append(f"{name}:{getattr(node, 'lineno', '?')}: {why}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            s = node.value
            if "submit" in s.lower() and s not in _ALLOWED_GUARD_JS:
                flag(node, f"string mentions submit: {s!r}")
            if _JS_SUBMIT_CALL.search(s):
                flag(node, f"JS submit call: {s!r}")
            if _ENTER_KEY.search(s) or _JS_ENTER.search(s):
                flag(node, f"Enter key (implicit form submit): {s!r}")
            if _TEXT_SELECTOR.search(s):
                flag(node, f"text selector targets apply/send: {s!r}")
            if s not in _ALLOWED_JS and (_JS_CLICK.search(s) or _JS_SYNTH_EVENT.search(s)):
                flag(node, f"JS click / synthetic event not in _ALLOWED_JS: {s!r}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            method = node.func.attr
            texts = [
                s for a in [*node.args, *(k.value for k in node.keywords)] for s in _strings_in(a)
            ]
            if method in _SUBMIT_METHODS:
                flag(node, f".{method}() submits a form")
            if method in _SELECTOR_CALLS:
                for s in texts:
                    if _TARGET_TEXT.search(s):
                        flag(node, f".{method}() targets {s!r}")
            if method in _KEY_CALLS:
                for s in texts:
                    if "\n" in s or "\r" in s:
                        flag(node, f".{method}() types a newline (implicit form submit)")
    return found


def test_submit_guard_static_scan() -> None:
    """MANDATORY (CLAUDE.md rule 1): no code path in autofill/ may target a
    submit/apply/send control."""
    files = _python_files(AUTOFILL_DIR)
    assert files, f"submit guard scanned 0 files under {AUTOFILL_DIR}: it would pass vacuously"
    assert "browser.py" in {p.name for p in files}, f"browser.py missing from {files}"
    violations = [
        v
        for py in files
        for v in submit_guard_violations(py.read_text(), str(py.relative_to(REPO_ROOT)))
    ]
    assert not violations, "submit-like targeting in autofill/ (hard rule 1):\n" + "\n".join(
        violations
    )


def test_submit_guard_allowlist_is_live() -> None:
    """A stale _ALLOWED_JS entry is a pre-approved hole: every entry must still
    exist verbatim in autofill/."""
    corpus = "".join(p.read_text() for p in _python_files(AUTOFILL_DIR))
    stale = [js for js in _ALLOWED_JS if js not in corpus]
    assert not stale, f"remove stale _ALLOWED_JS entries: {stale}"


def test_click_guard_probe_is_pinned_and_read_only() -> None:
    """The runtime guard's probe is the one string in autofill/ allowed to
    name submit. It may only read the element: no click, no synthetic event,
    no Enter, no form submit. Any edit to it fails here until the hash below
    is updated in review."""
    import hashlib

    js = browser._CLICK_GUARD_JS
    assert hashlib.sha256(js.encode()).hexdigest() == _GUARD_JS_SHA256, (
        "browser._CLICK_GUARD_JS changed: review it, then update _GUARD_JS_SHA256"
    )
    assert not _JS_CLICK.search(js) and not _JS_SYNTH_EVENT.search(js)
    assert not _JS_SUBMIT_CALL.search(js) and not _JS_ENTER.search(js)
    assert not re.search(r"dispatchEvent|\.focus\s*\(|\.blur\s*\(", js)
    for code in ("submit-type", "inside-submit-control", "submit-like-name"):
        assert f"'{code}'" in js
    # the exemption covers this exact string only
    assert submit_guard_violations(f"X = {js[:-1]!r}") != []


@pytest.mark.parametrize(
    "src",
    [
        'page.evaluate("f => f.requestSubmit ()")',
        'JS = "() => document.forms[0].submit()"\npage.evaluate(JS)',
    ],
    ids=["spaced requestSubmit", "submit via constant"],
)
def test_submit_guard_catches_js_submit_calls(src: str) -> None:
    assert any("JS submit call" in v for v in submit_guard_violations(src)), src


_BYPASSES = {
    "get_by_text click": 'page.get_by_text("Submit application").click()',
    "get_by_text apply": 'page.get_by_text("Apply for this job").click()',
    "get_by_role name": 'page.get_by_role("button", name="Send").click()',
    "stored locator": 'btn = page.get_by_text("Submit")\nbtn.click()',
    "id selector": "page.locator('#submit_app').click()",
    "type=submit via constant": 'SEL = "input[type=submit]"\npage.locator(SEL).click()',
    "page.click selector": "page.click('button.apply-now')",
    "has-text selector": "page.locator(\"button:has-text('Apply')\").click()",
    "text= selector via constant": 'SEL = "text=Send application"\npage.locator(SEL).click()',
    "filter has_text": 'page.locator("button").filter(has_text="Apply").first.click()',
    "press Enter": 'ctl.press("Enter")',
    "keyboard Enter": 'page.keyboard.press("Enter")',
    "chord Enter": 'page.keyboard.press("Control+Enter")',
    "page.press selector Enter": 'page.press("#email", "NumpadEnter")',
    "newline typed": 'ctl.press_sequentially("done\\n")',
    "python form.submit": "form.submit()",
    "requestSubmit in JS": 'page.evaluate("document.forms[0].requestSubmit()")',
    "form.submit in JS": 'page.evaluate("f => f.submit()")',
    "JS click in evaluate": "page.evaluate(\"document.querySelector('form button').click()\")",
    "JS click via constant": (
        "JS = \"el => el.form.querySelector('button').click()\"\nctl.evaluate(JS)"
    ),
    "JS synthetic submit": "page.evaluate(\"f => f.dispatchEvent(new Event('submit'))\")",
    "JS Enter keydown": (
        "page.evaluate(\"el => el.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter'}))\")"
    ),
    "dispatch_event submit": 'ctl.dispatch_event("submit")',
}


@pytest.mark.parametrize("src", list(_BYPASSES.values()), ids=list(_BYPASSES))
def test_submit_guard_catches_known_bypasses(src: str) -> None:
    assert submit_guard_violations(src), f"submit guard missed: {src}"


_BENIGN = {
    "escape key": 'page.keyboard.press("Escape")',
    "yes/no pair": 'cont.first.get_by_role("button", name=want, exact=True).first.click()',
    "fill value": "ctl.fill(value)",
    "fill multiline": 'ctl.fill("line one\\nline two")',
    "name selector": "page.locator(f'[name=\"{identity}\"]').click()",
    "option pick": 'page.get_by_role("option").nth(0).click()',
    "allowlisted label click": (
        "target.evaluate(\"el => { const l = el.closest('label'); (l || el).click(); }\")"
    ),
    "docstring": 'def f():\n    """Never clicks submit, never calls form.submit()."""\n',
    "apply url navigation": 'page.goto(job["apply_url"], wait_until="domcontentloaded")',
}


@pytest.mark.parametrize("src", list(_BENIGN.values()), ids=list(_BENIGN))
def test_submit_guard_allows_fill_patterns(src: str) -> None:
    assert submit_guard_violations(src) == []


def test_browser_automation_confined_to_autofill() -> None:
    """The scan above only covers autofill/, so nothing else may drive a browser."""
    drivers = []
    for py in _python_files(REPO_ROOT / "src" / "jobkit"):
        for node in ast.walk(ast.parse(py.read_text())):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            if any(m == "playwright" or m.startswith("playwright.") for m in mods):
                drivers.append(py)
    assert drivers, "expected autofill/browser.py to import playwright"
    outside = [str(p.relative_to(REPO_ROOT)) for p in drivers if AUTOFILL_DIR not in p.parents]
    assert not outside, f"browser automation outside autofill/ escapes the submit guard: {outside}"
