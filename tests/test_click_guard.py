"""Runtime half of hard rule 1: every fill click in autofill/browser.py is
preceded by an in-page probe of the element (refuse_send_control), and a
control that could send the application is never clicked. Fake Playwright
objects only — offline, no browser. The static half is in test_form_specs.py.

The last section is structural: an AST check that EVERY click in autofill/,
including one added later, sits behind refuse_send_control on the same
element. The fake-object tests above only cover the click paths that exist."""

from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from jobkit.autofill import answer_bank, browser
from jobkit.autofill.browser import ClickRefused, refuse_send_control
from tests.conftest import REPO_ROOT
from tests.test_form_specs import (
    _ALLOWED_JS,
    _JS_CLICK,
    _JS_SYNTH_EVENT,
    AUTOFILL_DIR,
    _docstring_ids,
    _python_files,
)
from tests.test_staging_browser import FakeFrame, FakePage

# ---- fakes ---------------------------------------------------------------------


class Ctl:
    """One element. `guard` is what the in-page probe would report for it
    ('' = safe). Records clicks and checks."""

    def __init__(
        self,
        guard: str = "",
        tag: str = "input",
        attrs: dict[str, str] | None = None,
        text: str = "",
        raises: bool = False,
    ) -> None:
        self.guard, self.tag, self.attrs, self.text, self.raises = (
            guard,
            tag,
            attrs or {},
            text,
            raises,
        )
        self.clicks = 0
        self.checks = 0
        self.probed = 0

    def evaluate(self, js: str, arg: Any = None) -> Any:
        if js == browser._CLICK_GUARD_JS:
            self.probed += 1
            if self.raises:
                raise RuntimeError("Element is not attached to the DOM")
            return self.guard
        if "tagName" in js:
            return self.tag
        if "labels" in js or "aria-label" in js:
            return self.text
        return None

    def get_attribute(self, name: str) -> str | None:
        return self.attrs.get(name)

    def click(self, **kw: Any) -> None:
        self.clicks += 1

    def check(self, **kw: Any) -> None:
        self.checks += 1

    def is_checked(self) -> bool:
        return self.checks > 0

    def inner_text(self, **kw: Any) -> str:
        return self.text


class Many:
    """A locator over several elements."""

    def __init__(self, els: list[Ctl]) -> None:
        self.els = els

    def count(self) -> int:
        return len(self.els)

    def nth(self, i: int) -> Ctl:
        return self.els[i]

    def all_inner_texts(self) -> list[str]:
        return [e.text for e in self.els]

    @property
    def first(self) -> Ctl:
        return self.els[0]

    def get_by_role(self, role: str, **kw: Any) -> Many:
        return self


class Scope:
    """The frame holding the form: get_by_role('option') lists `options`;
    locator(...) returns `container` (Ashby's labeled yes/no field)."""

    def __init__(self, options: list[Ctl] | None = None, container: Any = None) -> None:
        self.options, self.container = options or [], container

    def get_by_role(self, role: str, **kw: Any) -> Many:
        return Many(self.options)

    def locator(self, sel: str) -> Any:
        return self.container if self.container is not None else Many([])

    def wait_for_timeout(self, ms: float) -> None:
        return None


class Keyboard:
    def press(self, key: str) -> None:
        assert key == "Escape"


class Page:
    keyboard = Keyboard()


class Container:
    """Ashby field container: get_by_role('button', name=...) -> the button.
    It is the one innermost container holding the label and a button."""

    def __init__(self, button: Ctl) -> None:
        self.button = button

    def count(self) -> int:
        return 1

    def evaluate_all(self, js: str, arg: Any = None) -> list[int]:
        return [0]

    def nth(self, i: int) -> Container:
        return self

    @property
    def first(self) -> Container:
        return self

    def get_by_role(self, role: str, **kw: Any) -> Many:
        return Many([self.button] if role == "button" else [])


def _guard_log(home: Path) -> list[dict[str, Any]]:
    lines = (home / "logs" / "jobkit.jsonl").read_text().splitlines()
    return [e for e in map(json.loads, lines) if e["event"] == "stage.click_guard_refused"]


# ---- the guard itself ------------------------------------------------------------


@pytest.mark.parametrize("why", ["submit-type", "inside-submit-control", "submit-like-name"])
def test_guard_refuses_and_logs_send_controls(why: str, jobkit_home: Path) -> None:
    ctl = Ctl(guard=why)
    with pytest.raises(ClickRefused, match=why):
        refuse_send_control(ctl, "dropdown")  # type: ignore[arg-type]
    logged = _guard_log(jobkit_home)
    assert logged and logged[-1]["reason"] == why and logged[-1]["control"] == "dropdown"


def test_guard_allows_an_ordinary_control() -> None:
    ctl = Ctl(guard="")
    refuse_send_control(ctl, "option")  # type: ignore[arg-type]
    assert ctl.probed == 1


def test_guard_fails_closed_on_an_element_it_cannot_inspect() -> None:
    with pytest.raises(ClickRefused, match="uninspectable"):
        refuse_send_control(Ctl(raises=True), "checkbox")  # type: ignore[arg-type]


# ---- every click path in _fill_control / _click_boolean_pair is guarded ----------


def test_dropdown_that_is_a_send_control_is_never_opened() -> None:
    ctl = Ctl(guard="submit-like-name", tag="div", attrs={"role": "combobox"})
    with pytest.raises(ClickRefused):
        browser._fill_control(Page(), Scope(), ctl, "LinkedIn", "select", [])  # type: ignore[arg-type]
    assert ctl.clicks == 0


def test_option_that_is_a_send_control_is_never_picked() -> None:
    ctl = Ctl(tag="div", attrs={"role": "combobox"})
    bad = Ctl(guard="submit-type", text="LinkedIn")
    with pytest.raises(ClickRefused):
        browser._fill_control(Page(), Scope([bad]), ctl, "LinkedIn", "select", [])  # type: ignore[arg-type]
    assert ctl.clicks == 1 and bad.clicks == 0  # the dropdown opened; the option was refused


def test_safe_dropdown_and_option_are_clicked_once_each() -> None:
    ctl = Ctl(tag="div", attrs={"role": "combobox"})
    opt = Ctl(text="LinkedIn")
    ok = browser._fill_control(Page(), Scope([opt]), ctl, "LinkedIn", "select", [])  # type: ignore[arg-type]
    assert ok and (ctl.clicks, opt.clicks) == (1, 1) and (ctl.probed, opt.probed) == (1, 1)


def test_checkbox_inside_a_send_control_is_never_checked() -> None:
    ctl = Ctl(guard="inside-submit-control", attrs={"type": "checkbox"})
    with pytest.raises(ClickRefused):
        browser._fill_control(Page(), Scope(), ctl, "Yes", "checkbox", [])  # type: ignore[arg-type]
    assert ctl.checks == 0


def test_radio_group_answer_that_is_a_send_control_is_never_checked() -> None:
    radio = Ctl(guard="submit-like-name", attrs={"aria-label": "Yes"})
    group = Ctl(tag="fieldset")
    group_many = Many([radio])
    group.get_by_role = lambda role, **kw: group_many  # type: ignore[attr-defined]
    with pytest.raises(ClickRefused):
        browser._fill_control(Page(), Scope(), group, "Yes", "radio", [])  # type: ignore[arg-type]
    assert radio.checks == 0


def test_yes_no_button_that_would_send_the_form_is_never_clicked() -> None:
    # a <button> with no type inside the form submits it: the probe says submit-type
    btn = Ctl(guard="submit-type", tag="button")
    with pytest.raises(ClickRefused):
        browser._click_boolean_pair(Scope(container=Container(btn)), "Authorized?", "Yes")  # type: ignore[arg-type]
    assert btn.clicks == 0
    ok_btn = Ctl(tag="button")
    assert browser._click_boolean_pair(Scope(container=Container(ok_btn)), "Authorized?", "Yes")  # type: ignore[arg-type]
    assert ok_btn.clicks == 1


@pytest.mark.parametrize(
    ("value", "clicked"),
    [
        ("Yes", True),
        ("yes, I am", True),
        ("No", True),
        ("No, I do not require sponsorship", True),
        # not a yes/no word: never mapped onto the pair (they were No, No, Yes)
        ("None of the above", False),
        ("Not sure", False),
        ("10 years", False),
        ("Nope", False),
    ],
)
def test_yes_no_pair_takes_only_a_whole_yes_or_no(value: str, clicked: bool) -> None:
    btn = Ctl(tag="button")
    got = browser._click_boolean_pair(Scope(container=Container(btn)), "Authorized?", value)  # type: ignore[arg-type]
    assert got is clicked and btn.clicks == int(clicked)


# ---- staging survives a refusal: the field goes to the human ---------------------


@pytest.fixture()
def boolean_job(conn: sqlite3.Connection, jobkit_home: Path) -> Any:
    (jobkit_home / "data" / "answer_bank.yaml").write_text(
        yaml.safe_dump(
            {
                "identity": {"first_name": "Pat", "email": "pat@example.com"},
                "screening": [
                    {
                        "id": "work_auth",
                        "aliases": ["Are you legally authorized to work in the United States?"],
                        "answer": "Yes",
                    }
                ],
            }
        )
    )
    answer_bank.load_bank.cache_clear()
    conn.execute(
        "INSERT INTO companies (id, name, normalized_name, domain) "
        "VALUES (1, 'Initech', 'initech', 'initech.example')"
    )
    jid = conn.execute(
        """INSERT INTO jobs (company_id, external_id, title, normalized_title, company_name,
           normalized_company, location_bucket, cluster_key, jd_html, jd_hash, apply_url, status)
           VALUES (1, 'a1', 'Account Executive', 'ae', 'Initech', 'initech', 'remote-us', 'k',
                   'jd', 'h', 'https://jobs.ashbyhq.com/initech/x/application', 'approved')"""
    ).lastrowid
    fields = [
        {
            "identity": "q_auth",
            "label": "Are you legally authorized to work in the United States?",
            "type": "Boolean",
            "required": True,
            "match": {},
        }
    ]
    conn.execute(
        "INSERT INTO form_specs (job_id, source, fields, drafted_answers) "
        "VALUES (?, 'api_questions', ?, '{}')",
        (jid, json.dumps(fields)),
    )
    conn.commit()
    yield conn, jid
    answer_bank.load_bank.cache_clear()


def test_refused_click_leaves_the_field_to_the_human(
    boolean_job: tuple[sqlite3.Connection, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, jid = boolean_job

    def no_fill(*a: Any) -> bool:
        return False

    def refused(*a: Any) -> bool:
        raise ClickRefused("fill click refused on yes-no button: submit-type")

    monkeypatch.setattr(browser, "_fill_control", no_fill)
    monkeypatch.setattr(browser, "_click_boolean_pair", refused)
    form = FakeFrame(url="https://jobs.ashbyhq.com/initech/x/application", ids=("q_auth",))
    matcher = answer_bank.BankMatcher(answer_bank.load_bank(), use_embeddings=False)
    r = browser.stage_job(conn, FakePage([form]), jid, matcher=matcher)
    assert r["status"] == "needs_human" and r["required_open"] == 1, r
    run = conn.execute(
        "SELECT unfilled_fields FROM autofill_runs WHERE job_id=?", (jid,)
    ).fetchone()
    assert json.loads(run[0]) == [{"identity": "q_auth", "reason": "fill-failed"}]


# ---- structural: every click in autofill/ sits behind the guard ------------------
#
# The fakes above prove the click paths that exist today are guarded. They
# cannot see a NEW .click() or .check() added without refuse_send_control: the
# static scan in test_form_specs.py flags a click only when its selector text
# looks like submit/apply/send, and a label-built locator never does (WP0
# review). So this AST check covers every click in autofill/:
#   * a Locator method that clicks (_CLICK_METHODS);
#   * a call carrying JS that clicks. Only the _ALLOWED_JS strings may (the
#     static scan rejects the rest), and that JS must reach the page as a direct
#     call argument, inline or as a module-level constant, so the check can
#     follow it.
# Each must be dominated by `refuse_send_control(<same receiver>, ...)` as a
# plain statement, earlier in the same block or an enclosing block of the
# same function, with no name in the receiver rebound in between (a loop
# between them widens that window to the loop's end). A guard inside an
# if/try/with does not count: it may not have run. Page/Frame selector clicks
# and mouse coordinate clicks are refused outright, since the guard can only
# probe a Locator. Fails with file:line.

_GUARD_NAME = "refuse_send_control"
# dispatch_event can fire a synthetic click, so it is guarded like one
_CLICK_METHODS = frozenset(
    {"click", "dblclick", "tap", "check", "uncheck", "set_checked", "dispatch_event"}
)
# Locator.click/dblclick/tap/check/uncheck take keyword arguments only. A
# positional argument (or selector=, x=, y=) is a Page/Frame selector click or
# a mouse coordinate click: there is no element for the guard to probe.
_LOCATOR_ONLY = frozenset({"click", "dblclick", "tap", "check", "uncheck"})
_UNPROBEABLE_KW = frozenset({"selector", "x", "y"})
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.Module)
_LOOPS = (ast.For, ast.AsyncFor, ast.While)


def _clicking_js(s: str) -> bool:
    return s in _ALLOWED_JS or bool(_JS_CLICK.search(s) or _JS_SYNTH_EVENT.search(s))


def _callee(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else ""


def _is_guard(stmt: ast.stmt, receiver: ast.expr) -> bool:
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
        return False
    call = stmt.value
    return (
        _callee(call.func) == _GUARD_NAME
        and bool(call.args)
        and ast.dump(call.args[0]) == ast.dump(receiver)
    )


def _dominating_guard(
    call: ast.Call, receiver: ast.expr, parents: dict[ast.AST, ast.AST]
) -> tuple[ast.stmt | None, int, ast.AST]:
    """(the guard statement or None, the last line from which a rebinding of
    the receiver still reaches the call, the enclosing function scope)."""
    node: ast.AST = call
    reach = call.lineno
    while not isinstance(node, _SCOPES):
        parent = parents[node]
        for _, value in ast.iter_fields(parent):
            if not isinstance(value, list):
                continue
            idx = next((i for i, v in enumerate(value) if v is node), None)
            if idx is None:
                continue
            for prev in reversed(value[:idx]):
                if isinstance(prev, ast.stmt) and _is_guard(prev, receiver):
                    scope = parent
                    while not isinstance(scope, _SCOPES):
                        scope = parents[scope]
                    return prev, reach, scope
        if isinstance(parent, _LOOPS):
            reach = max(reach, parent.end_lineno or reach)
        node = parent
    return None, reach, node


def _module_constants(tree: ast.Module) -> tuple[dict[str, str], set[int]]:
    """Module-level NAME = "string" constants, and the ids of their value nodes."""
    consts: dict[str, str] = {}
    value_ids: set[int] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
        elif isinstance(stmt, ast.AnnAssign):
            targets = [stmt.target]
        else:
            continue
        value = stmt.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            value_ids.add(id(value))
            consts.update({t.id: value.value for t in targets if isinstance(t, ast.Name)})
    return consts, value_ids


def click_sites(source: str, name: str = "<source>") -> list[tuple[int, str, str | None]]:
    """Every click in `source`: (line, what, problem or None if guarded)."""
    tree = ast.parse(source, filename=name)
    parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
    docstrings = _docstring_ids(tree)
    consts, const_values = _module_constants(tree)
    clicking_consts = {k for k, v in consts.items() if _clicking_js(v)}
    direct_args = {
        id(a)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        for a in [*n.args, *(k.value for k in n.keywords)]
    }

    def carries_click_js(a: ast.expr) -> bool:
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            return _clicking_js(a.value)
        return isinstance(a, ast.Name) and a.id in clicking_consts

    sites: list[tuple[int, str, str | None]] = []

    def site(node: ast.AST, what: str, problem: str | None) -> None:
        sites.append((getattr(node, "lineno", 0), what, problem))

    for node in ast.walk(tree):
        # clicking JS the call check below cannot follow
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _clicking_js(node.value)
            and id(node) not in docstrings | direct_args | const_values
        ) or (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in clicking_consts
            and id(node) not in direct_args
        ):
            site(node, "clicking JS", "not passed straight to a call")
            continue
        # a click method taken as a value (click = ctl.click; partial(ctl.check))
        if isinstance(node, ast.Attribute) and node.attr in _CLICK_METHODS:
            parent = parents.get(node)
            if not (isinstance(parent, ast.Call) and parent.func is node):
                why = "click method used as a value: the guard cannot follow it"
                site(node, f".{node.attr}", why)
            continue
        if not isinstance(node, ast.Call):
            continue
        method = _callee(node.func)
        args = [*node.args, *(k.value for k in node.keywords)]
        if method == "getattr" and any(
            isinstance(a, ast.Constant) and a.value in _CLICK_METHODS for a in args
        ):
            site(node, "getattr", "click method looked up by name: the guard cannot follow it")
            continue
        js = any(carries_click_js(a) for a in args)
        if method not in _CLICK_METHODS and not js:
            continue
        what = f"JS click via .{method}()" if js else f".{method}()"
        if method in _LOCATOR_ONLY and (
            node.args or any(k.arg in _UNPROBEABLE_KW for k in node.keywords)
        ):
            site(node, what, "selector or coordinate click: no element for the guard to probe")
            continue
        if not isinstance(node.func, ast.Attribute):
            site(node, what, "no receiver for refuse_send_control to probe")
            continue
        receiver = node.func.value
        guard, reach, scope = _dominating_guard(node, receiver, parents)
        if guard is None:
            rcv = ast.unparse(receiver)
            site(node, what, f"no refuse_send_control({rcv}, ...) before it in the same function")
            continue
        names = {n.id for n in ast.walk(receiver) if isinstance(n, ast.Name)}
        after = guard.end_lineno or guard.lineno
        rebound = sorted(
            {
                n.id
                for n in ast.walk(scope)
                if isinstance(n, ast.Name)
                and isinstance(n.ctx, ast.Store | ast.Del)
                and n.id in names
                and after < n.lineno <= reach
            }
        )
        site(node, what, f"{', '.join(rebound)} rebound after the guard" if rebound else None)
    return sorted(sites, key=lambda t: t[0])


def unguarded_clicks(source: str, name: str = "<source>") -> list[str]:
    return [f"{name}:{ln}: {what}: {why}" for ln, what, why in click_sites(source, name) if why]


def test_every_click_in_autofill_is_behind_the_guard() -> None:
    """Hard rule 1, runtime half: no click in autofill/ can bypass
    refuse_send_control, whatever its selector says."""
    files = _python_files(AUTOFILL_DIR)
    assert "browser.py" in {p.name for p in files}, f"browser.py missing from {files}"
    assert browser.refuse_send_control.__name__ == _GUARD_NAME
    problems = [
        v for py in files for v in unguarded_clicks(py.read_text(), str(py.relative_to(REPO_ROOT)))
    ]
    assert not problems, "click in autofill/ not behind refuse_send_control:\n" + "\n".join(
        problems
    )
    # not vacuous: the radio, dropdown, option, styled-radio (JS + forced
    # check), checkbox and Ashby yes/no clicks are all found and guarded
    found = click_sites((AUTOFILL_DIR / "browser.py").read_text())
    assert len(found) >= 7, f"expected the 7 known click sites in browser.py, found {found}"


_ALLOWED_CLICK_JS = next(iter(_ALLOWED_JS))

_UNGUARDED = {
    "no guard": "def f(ctl):\n    ctl.click()",
    "guard on another element": ('def f(a, b):\n    refuse_send_control(a, "x")\n    b.click()'),
    "guard after the click": ('def f(ctl):\n    ctl.click()\n    refuse_send_control(ctl, "x")'),
    "guard in the other branch": (
        'def f(ctl, c):\n    if c:\n        refuse_send_control(ctl, "x")\n'
        "    else:\n        ctl.click()"
    ),
    "guard only inside an if": (
        'def f(ctl, c):\n    if c:\n        refuse_send_control(ctl, "x")\n    ctl.click()'
    ),
    "guard swallowed by try": (
        'def f(ctl):\n    try:\n        refuse_send_control(ctl, "x")\n'
        "    except ClickRefused:\n        pass\n    ctl.click()"
    ),
    "click in the except handler": (
        'def f(ctl):\n    try:\n        refuse_send_control(ctl, "x")\n'
        "    except Exception:\n        ctl.click()"
    ),
    "receiver rebound": (
        'def f(ctl, other):\n    refuse_send_control(ctl, "x")\n    ctl = other\n    ctl.click()'
    ),
    "receiver rebound later in a loop": (
        'def f(ctl, items):\n    refuse_send_control(ctl, "x")\n    for item in items:\n'
        "        ctl.click()\n        ctl = item"
    ),
    "loop variable": "def f(items):\n    for c in items:\n        c.click()",
    "comprehension": "def f(items):\n    [c.click() for c in items]",
    "guard in the outer function": (
        'def f(ctl):\n    refuse_send_control(ctl, "x")\n    def g():\n        ctl.click()\n    g()'
    ),
    "lambda": 'def f(ctl):\n    refuse_send_control(ctl, "x")\n    run(lambda: ctl.click())',
    "dblclick": "def f(ctl):\n    ctl.dblclick()",
    "tap": "def f(ctl):\n    ctl.tap()",
    "check": "def f(ctl):\n    ctl.check(force=True)",
    "uncheck": "def f(ctl):\n    ctl.uncheck()",
    "set_checked": "def f(ctl):\n    ctl.set_checked(True)",
    "synthetic click": 'def f(ctl):\n    ctl.dispatch_event("click")',
    "allowlisted JS click": f"def f(t):\n    t.evaluate({_ALLOWED_CLICK_JS!r})",
    "allowlisted JS via constant": f"JS = {_ALLOWED_CLICK_JS!r}\ndef f(t):\n    t.evaluate(JS)",
    "allowlisted JS via local": (
        f'def f(t):\n    js = {_ALLOWED_CLICK_JS!r}\n    refuse_send_control(t, "x")\n'
        "    t.evaluate(js)"
    ),
    "allowlisted JS as keyword": (
        f"def f(t):\n    t.evaluate(expression={_ALLOWED_CLICK_JS!r}, arg=None)"
    ),
    "JS click on the page": (
        "def f(page):\n    page.evaluate(\"document.querySelector('x').click()\")"
    ),
    "page selector click": (
        'def f(page):\n    refuse_send_control(page, "x")\n    page.click("#field")'
    ),
    "selector keyword": (
        'def f(frame):\n    refuse_send_control(frame, "x")\n    frame.check(selector="#agree")'
    ),
    "mouse coordinates": "def f(page):\n    page.mouse.click(10, 20)",
    "bare function click": "def f(click):\n    click()",
    "aliased bound method": (
        'def f(ctl):\n    click = ctl.click\n    refuse_send_control(ctl, "x")\n    click()'
    ),
    "partial": "def f(ctl):\n    run(functools.partial(ctl.check, force=True))",
    "getattr": 'def f(ctl):\n    getattr(ctl, "click")()',
}


@pytest.mark.parametrize("src", list(_UNGUARDED.values()), ids=list(_UNGUARDED))
def test_click_coverage_catches_unguarded_clicks(src: str) -> None:
    assert unguarded_clicks(src), f"unguarded click not caught:\n{src}"


_GUARDED = {
    "guard then click": 'def f(ctl):\n    refuse_send_control(ctl, "x")\n    ctl.click()',
    "guard in an enclosing block": (
        'def f(t):\n    refuse_send_control(t, "radio")\n    t.wait()\n'
        "    if not t.is_checked():\n        t.check(force=True)"
    ),
    "guard then allowlisted JS": (
        f'def f(t):\n    refuse_send_control(t, "radio")\n    t.evaluate({_ALLOWED_CLICK_JS!r})'
    ),
    "guard via module attribute": (
        'def f(btn):\n    browser.refuse_send_control(btn.first, "x")\n    btn.first.click()'
    ),
    "guard inside the loop": (
        'def f(items):\n    for c in items:\n        refuse_send_control(c, "x")\n        c.click()'
    ),
    "receiver bound before the guard": (
        "def f(cont):\n    btn = cont.a()\n    btn = cont.b()\n"
        '    refuse_send_control(btn.first, "x")\n    btn.first.click()'
    ),
    "not clicks": (
        "def f(page, ctl, v):\n    ctl.fill(v)\n    ctl.select_option(label=v)\n"
        '    page.keyboard.press("Escape")\n    ctl.set_input_files(v)'
    ),
}


@pytest.mark.parametrize("src", list(_GUARDED.values()), ids=list(_GUARDED))
def test_click_coverage_accepts_guarded_clicks(src: str) -> None:
    assert unguarded_clicks(src) == []
