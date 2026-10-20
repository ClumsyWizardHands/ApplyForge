"""Review 2026-10-12 (fill quality): the container lookup could still put one
question's answer into another question's box. _QUESTION_CONTROLS_JS keyed
unnamed controls alike ('group' / ''), so a shared wrapper whose class held
"field" (<div class="form-fields">) counted as ONE question and `.first`
handed back whichever control came first: the Race/Ethnicity answer went
into Location, the privacy-notice tick into the SMS-consent box.

Now the page is read as a tree (_FORM_TREE_JS) and question_control decides
in Python: a <label for>, aria-labelledby, or the nearest element above the
label text holding a control, which must hold ONE question's controls; a
wrapper holding two questions' controls is refused (ControlAmbiguous), never
narrowed to its first control.

Offline, no browser: tree_of() builds the same tree from HTML with
BeautifulSoup, the way _FORM_TREE_JS builds it in the page (every element
under <body>, scripts/styles/svg/iframes skipped, `parts` in document order,
`ctl` = the index among the elements matching _FILLABLE in document
order)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from bs4 import BeautifulSoup, NavigableString, Tag

from jobkit.autofill import browser
from jobkit.autofill.browser import ControlAmbiguous, question_control

_SKIP = {"script", "style", "noscript", "template", "svg", "head", "iframe"}


def _fillable(el: Tag) -> bool:
    if el.name == "input":
        return str(el.get("type") or "").lower() != "hidden"
    return el.name in ("select", "textarea") or str(el.get("role") or "") == "combobox"


def tree_of(html: str) -> list[dict[str, Any]]:
    """_FORM_TREE_JS's tree, built from HTML."""
    soup = BeautifulSoup(f"<html><body>{html}</body></html>", "html.parser")
    body = soup.body
    assert body is not None
    order = [e for e in body.find_all(True) if _fillable(e)]
    pos = {id(e): i for i, e in enumerate(order)}
    out: list[dict[str, Any]] = []

    def attr(el: Tag, k: str) -> str:
        v = el.get(k)
        return " ".join(v) if isinstance(v, list) else str(v or "")

    def visit(el: Tag, parent: int, hidden: bool) -> int:
        if el.name in _SKIP:
            return -1
        style = attr(el, "style").replace(" ", "").lower()
        hidden = hidden or el.has_attr("hidden") or "display:none" in style
        i = len(out)
        node: dict[str, Any] = {
            "p": parent,
            "tag": el.name,
            "id": attr(el, "id"),
            "for": attr(el, "for"),
            "lb": attr(el, "aria-labelledby"),
            "name": attr(el, "name"),
            "type": attr(el, "type").lower(),
            "role": attr(el, "role").lower(),
            "ce": attr(el, "contenteditable").lower(),
            "hid": 1 if hidden else 0,  # no layout box (display:none, or inside one)
            "ctl": pos.get(id(el), -1),
            "parts": [],
        }
        out.append(node)
        for c in el.children:
            if isinstance(c, Tag):
                j = visit(c, i, hidden)
                if j >= 0:
                    node["parts"].append(j)
            elif isinstance(c, NavigableString) and str(c).strip():
                node["parts"].append(str(c))
        return i

    visit(body, -1, False)
    return json.loads(json.dumps(out))  # what crosses from the page: plain JSON


def control_names(html: str) -> list[str]:
    """The fillable controls in _FILLABLE order, by name or id or tag."""
    soup = BeautifulSoup(f"<html><body>{html}</body></html>", "html.parser")
    assert soup.body is not None
    return [
        str(e.get("name") or e.get("id") or e.get("data-q") or e.name)
        for e in soup.body.find_all(True)
        if _fillable(e)
    ]


def pick(html: str, label: str) -> tuple[str | None, str]:
    index, why = question_control(tree_of(html), label)
    return (control_names(html)[index] if index is not None else None), why


# ---- the reviewer's two repros ----------------------------------------------------

LOCATION_THEN_RACE = """
<form>
  <div class="form-fields">
    <div><span>Location</span><input type="text" data-q="location"></div>
    <div><span>Race/Ethnicity (optional)</span><input type="text" data-q="race"></div>
  </div>
</form>
"""
SMS_THEN_PRIVACY = """
<div class="application-fields">
  <div><span>I consent to receive marketing SMS messages</span>
       <input type="checkbox" data-q="sms"></div>
  <div><span>I acknowledge the candidate privacy notice</span>
       <input type="checkbox" data-q="privacy"></div>
</div>
"""


def test_race_goes_into_its_own_box_never_location() -> None:
    """Before: _control_in_container(frame, 'Race/Ethnicity (optional)')
    returned the Location input (the shared 'form-fields' wrapper counted as
    one question with two unnamed controls)."""
    assert pick(LOCATION_THEN_RACE, "Race/Ethnicity (optional)") == ("race", "")
    assert pick(LOCATION_THEN_RACE, "Location") == ("location", "")


def test_the_privacy_notice_tick_goes_into_its_own_box_never_the_sms_one() -> None:
    assert pick(SMS_THEN_PRIVACY, "I acknowledge the candidate privacy notice") == (
        "privacy",
        "",
    )
    assert pick(SMS_THEN_PRIVACY, "I consent to receive marketing SMS messages") == ("sms", "")


@pytest.mark.parametrize(
    "html",
    [
        # two questions' unnamed controls in one wrapper, no per-question element
        '<div class="form-fields"><span>Location</span><input type="text">'
        '<span>Race/Ethnicity (optional)</span><input type="text"></div>',
        '<div class="field"><span>I acknowledge the candidate privacy notice</span>'
        '<input type="checkbox"><span>I consent to SMS</span><input type="checkbox"></div>',
        # a label beside a box holding two questions' controls
        '<div><p>Race/Ethnicity (optional)</p><div><input type="text" name="a">'
        '<select name="b"><option>x</option></select></div></div>',
    ],
    ids=["two-text-boxes", "two-unnamed-checkboxes", "text-and-select"],
)
def test_a_wrapper_holding_two_questions_is_refused(html: str) -> None:
    label = "I acknowledge the candidate privacy notice" if "privacy" in html else "Race/Ethnicity"
    assert pick(html, label) == (None, "container-holds-several-questions")


# ---- label-for, aria-labelledby, one question's group --------------------------


def test_label_for_names_the_control_wherever_it_sits() -> None:
    html = """
    <div class="form-fields">
      <label for="loc">Location</label><label for="race">Race/Ethnicity</label>
      <input id="loc" type="text"><input id="race" type="text">
    </div>"""
    assert pick(html, "Race/Ethnicity") == ("race", "")
    assert pick(html, "Location") == ("loc", "")


def test_aria_labelledby_names_the_control() -> None:
    html = """
    <div class="fields">
      <span id="q1">Location</span><span id="q2">Race/Ethnicity</span>
      <div role="combobox" aria-labelledby="q2" data-q="race-box"></div>
      <input type="text" aria-labelledby="q1" data-q="loc">
    </div>"""
    assert pick(html, "Race/Ethnicity") == ("race-box", "")
    assert pick(html, "Location") == ("loc", "")


def test_a_label_wrapping_its_checkbox() -> None:
    html = """
    <div class="form-fields">
      <label><input type="checkbox" data-q="sms"> I consent to SMS</label>
      <label><input type="checkbox" data-q="privacy"> I acknowledge the privacy notice</label>
    </div>"""
    assert pick(html, "I acknowledge the privacy notice") == ("privacy", "")


def test_one_questions_radio_group_or_checkbox_group_is_one_question() -> None:
    radios = """
    <div class="form-fields">
      <div><span>Will you require sponsorship?</span>
        <label><input type="radio" name="spons" value="y">Yes</label>
        <label><input type="radio" name="spons" value="n">No</label></div>
      <div><span>Location</span><input type="text" data-q="loc"></div>
    </div>"""
    assert pick(radios, "Will you require sponsorship?") == ("spons", "")
    unnamed = """
    <fieldset><legend>Race/Ethnicity (select all that apply)</legend>
      <label><input type="checkbox">Asian</label>
      <label><input type="checkbox">I don't wish to answer</label>
    </fieldset>"""
    index, why = question_control(tree_of(unnamed), "Race/Ethnicity (select all that apply)")
    assert (index, why) == (0, "")


def test_a_combobox_input_inside_a_combobox_is_one_control() -> None:
    html = """
    <div class="field"><label>Gender</label>
      <div role="combobox" data-q="outer"><input type="text" data-q="inner"></div></div>
    <div class="field"><label>Veteran Status</label><input type="text" data-q="vet"></div>"""
    assert pick(html, "Gender") == ("outer", "")


def test_a_control_labelled_by_another_text_is_another_questions() -> None:
    html = """
    <div><span>Location</span>
      <label for="city">Preferred office</label><input id="city" type="text"></div>"""
    assert pick(html, "Location") == (None, "control-labelled-by-another-question")


def test_label_text_leading_to_two_controls_is_refused() -> None:
    html = """
    <div><span>Phone</span><input type="text" data-q="a"></div>
    <div><span>Phone</span><input type="text" data-q="b"></div>"""
    assert pick(html, "Phone") == (None, "label-matches-several-controls")


def test_an_element_whose_text_is_the_label_wins_over_one_that_mentions_it() -> None:
    html = """
    <section><h3>Location: New York (hybrid)</h3><p>About the role.</p></section>
    <div class="form-fields">
      <div><span>Location</span><input type="text" data-q="loc"></div>
      <div><span>LinkedIn</span><input type="text" data-q="li"></div>
    </div>"""
    assert pick(html, "Location") == ("loc", "")


def test_a_label_split_across_elements_and_marked_required_is_found() -> None:
    html = """
    <div><label>Race/<b>Ethnicity</b> (optional) <span class="req">*</span></label>
      <div><input type="text" data-q="race"></div></div>
    <div><label>Location</label><div><input type="text" data-q="loc"></div></div>"""
    assert pick(html, "Race/Ethnicity (optional)*") == ("race", "")


@pytest.mark.parametrize(
    "html",
    [
        "",
        "<div><span>Location</span></div>",  # no control anywhere
        "<select><option>Race/Ethnicity</option></select>",  # only an option's text
    ],
)
def test_nothing_found(html: str) -> None:
    assert question_control(tree_of(html), "Race/Ethnicity")[0] is None


# ---- _control_in_container: the tree from the page, the element checked ---------


def own_names(html: str) -> list[list[str]]:
    """_OWN_NAMES_JS's answer for each fillable control (in _FILLABLE order),
    built from HTML: the elements its aria-labelledby names (each, and
    joined), else its aria-label, else its <label>s (by `for`, or wrapping
    it), else its title or placeholder; a label's text without the controls
    inside it."""
    soup = BeautifulSoup(f"<html><body>{html}</body></html>", "html.parser")
    body = soup.body
    assert body is not None
    inner = {"select", "option", "optgroup", "textarea", "input", "button"} | _SKIP

    def text(node: Tag) -> str:
        out = ""
        for c in node.children:
            if isinstance(c, Tag):
                out += f" {text(c)} " if c.name not in inner else ""
            elif isinstance(c, NavigableString):
                out += str(c)
        return out

    out: list[list[str]] = []
    for el in (e for e in body.find_all(True) if _fillable(e)):
        ids = str(el.get("aria-labelledby") or "").split()
        refs = [r for r in (body.find(id=i) for i in ids) if isinstance(r, Tag)]
        aria = str(el.get("aria-label") or "")
        labels: list[Tag] = []
        if el.name in ("input", "select", "textarea"):
            if el.get("id"):
                labels += [lb for lb in body.find_all("label") if lb.get("for") == el.get("id")]
            labels += [p for p in el.parents if p.name == "label" and p not in labels]
        title = str(el.get("title") or "")
        if refs:
            out.append([text(r) for r in refs] + [" ".join(text(r) for r in refs)])
        elif aria.strip():
            out.append([aria])
        elif labels:
            out.append([text(lb) for lb in labels])
        else:
            out.append([title if title.strip() else str(el.get("placeholder") or "")])
    return out


class El:
    """One control in the page. `names`: what _OWN_NAMES_JS reads of it
    (None: the probe fails, as on a detached element)."""

    def __init__(self, print_: list[str], names: list[str] | None = None) -> None:
        self.print = print_
        self.names = names

    def evaluate(self, js: str, arg: Any = None, timeout: float | None = None) -> Any:
        if js == browser._SAME_ELEMENT_JS:  # _same_element (review 2026-10-20)
            return arg is self
        if js == browser._OWN_NAMES_JS:  # _named_as (review 2026-10-20, round 3)
            if self.names is None:
                raise RuntimeError("element detached")
            return self.names
        assert js == browser._CONTROL_PRINT_JS
        return self.print

    def element_handle(self, timeout: float | None = None) -> El:
        return self

    def dispose(self) -> None:
        pass


class Controls:
    def __init__(self, tree: list[dict[str, Any]] | Exception, els: list[El]) -> None:
        self.tree, self.els = tree, els

    def evaluate_all(self, js: str, arg: Any = None) -> Any:
        assert js == browser._FORM_TREE_JS
        if isinstance(self.tree, Exception):
            raise self.tree
        return self.tree

    def nth(self, i: int) -> El:
        return self.els[i]


class Frame:
    def __init__(self, controls: Controls) -> None:
        self.controls = controls

    def locator(self, sel: str) -> Controls:
        assert sel == browser._FILLABLE
        return self.controls


def _frame(html: str, tamper: bool = False) -> tuple[Frame, list[El]]:
    tree = tree_of(html)
    prints = [
        [str(n[k]) for k in ("tag", "id", "name", "type")]
        for n in sorted((n for n in tree if n["ctl"] >= 0), key=lambda n: int(n["ctl"]))
    ]
    names = own_names(html)
    if tamper:  # the page changed between the two reads
        prints, names = list(reversed(prints)), list(reversed(names))
    els = [El(p, n) for p, n in zip(prints, names, strict=True)]
    return Frame(Controls(tree, els)), els


def test_control_in_container_returns_the_questions_own_control() -> None:
    frame, els = _frame(LOCATION_THEN_RACE)
    assert browser._control_in_container(frame, "Race/Ethnicity (optional)") is els[1]  # type: ignore[arg-type]


def test_control_in_container_refuses_a_shared_wrapper(jobkit_home: Path) -> None:
    frame, _ = _frame(
        '<div class="form-fields"><span>Location</span><input type="text">'
        '<span>Race/Ethnicity (optional)</span><input type="text"></div>'
    )
    with pytest.raises(ControlAmbiguous, match="container-holds-several-questions"):
        browser._control_in_container(frame, "Race/Ethnicity (optional)")  # type: ignore[arg-type]
    logged = [
        json.loads(line)
        for line in (jobkit_home / "logs" / "jobkit.jsonl").read_text().splitlines()
        if '"stage.control_ambiguous"' in line
    ]
    assert logged and logged[0]["reason"] == "container-holds-several-questions"


def test_control_in_container_finds_nothing_when_the_page_changed_or_is_unreadable() -> None:
    named = LOCATION_THEN_RACE.replace('data-q="location"', 'name="loc"').replace(
        'data-q="race"', 'name="race"'
    )
    frame, els = _frame(named)
    assert browser._control_in_container(frame, "Race/Ethnicity (optional)") is els[1]  # type: ignore[arg-type]
    frame, _ = _frame(named, tamper=True)  # index 1 is now another element
    assert browser._control_in_container(frame, "Race/Ethnicity (optional)") is None  # type: ignore[arg-type]
    broken = Frame(Controls(RuntimeError("frame detached"), []))
    assert browser._control_in_container(broken, "Race/Ethnicity (optional)") is None  # type: ignore[arg-type]


# ---- the label lookups: several matches of more than one question ---------------


class Many:
    def __init__(self, kinds: list[list[str]]) -> None:
        self.kinds = kinds
        self.first = object()

    def count(self) -> int:
        return len(self.kinds)

    def evaluate_all(self, js: str, arg: Any = None) -> Any:
        assert js == browser._CONTROL_KINDS_JS
        return self.kinds


TEXT = ["input", "text", "", ""]


class Node:
    """One element in the page, and a locator to it: what _same_element
    asks of each (element_handle, then evaluate with the other's handle)."""

    def __init__(self, name: str, kind: list[str] | None = None) -> None:
        self.name, self.kind = name, kind or TEXT

    def element_handle(self, timeout: float | None = None) -> Node:
        return self

    def evaluate(self, js: str, arg: Any = None, timeout: float | None = None) -> Any:
        if js == browser._OWN_NAMES_JS:  # its own label is its accessible name
            return [self.name]
        assert js == browser._SAME_ELEMENT_JS
        return arg is self

    def dispose(self) -> None:
        pass

    def __repr__(self) -> str:
        return f"Node({self.name!r})"


class Found:
    """What a label or role lookup matched."""

    def __init__(self, nodes: list[Node]) -> None:
        self.nodes = nodes
        self.first = nodes[0] if nodes else Node("<none>")

    def count(self) -> int:
        return len(self.nodes)

    def evaluate_all(self, js: str, arg: Any = None) -> Any:
        assert js == browser._CONTROL_KINDS_JS
        return [n.kind for n in self.nodes]


def _matches(name: str, text: str, exact: bool) -> bool:
    """Playwright's text match for get_by_label / get_by_role(name=):
    exact=True is the whole name, case-sensitive; exact=False a
    case-insensitive substring (whitespace squeezed either way)."""
    name, text = " ".join(name.split()), " ".join(text.split())
    return name == text if exact else text.casefold() in name.casefold()


class Page:
    """A frame whose labelled controls are `named` (accessible name, role,
    element) and whose page tree is `frame`'s (question_control)."""

    def __init__(self, named: list[tuple[str, str, Node]], frame: Frame) -> None:
        self.named, self.frame = named, frame

    def locator(self, sel: str) -> Any:
        if sel == browser._FILLABLE:
            return self.frame.controls
        return Found([])  # no name / id / radio / partial-name match

    def get_by_label(self, label: str, exact: bool = False) -> Found:
        return Found([n for name, _role, n in self.named if _matches(name, label, exact)])

    def get_by_role(self, role: str, name: str = "", exact: bool = False) -> Found:
        return Found([n for nm, r, n in self.named if r == role and _matches(nm, name, exact)])


def test_one_question_takes_one_match_or_one_radio_group() -> None:
    box = Node("box")
    one = Found([box])
    assert browser._one_question(one, lambda: Found([box])) is box  # type: ignore[arg-type,return-value]
    # a lone loose match the exact lookup does not return is not taken
    # (review 2026-10-15, round 3; see test_a_lone_loose_match_…)
    assert browser._one_question(one, lambda: Found([])) is None  # type: ignore[arg-type,return-value]
    radios = Many([["input", "radio", "", "spons"], ["input", "radio", "", "spons"]])
    assert browser._one_question(radios, lambda: Many([])) is radios.first  # type: ignore[arg-type,return-value]
    assert browser._one_question(Many([]), lambda: Many([TEXT])) is None  # type: ignore[arg-type,return-value]


def test_several_matches_fall_back_to_the_exact_label_then_refuse() -> None:
    """get_by_label("Location", exact=False) also matches "Preferred work
    location": before, `.first` took whichever came first."""
    exact = Many([TEXT])
    assert browser._one_question(Many([TEXT, TEXT]), lambda: exact) is exact.first  # type: ignore[arg-type,return-value]
    with pytest.raises(ControlAmbiguous, match="label-matches-several-controls"):
        browser._one_question(Many([TEXT, TEXT]), lambda: Many([TEXT, TEXT]))  # type: ignore[arg-type,return-value]
    with pytest.raises(ControlAmbiguous):
        browser._one_question(Many([TEXT, TEXT]), lambda: Many([]))  # type: ignore[arg-type,return-value]
    two_groups = Many([["input", "radio", "", "a"], ["input", "radio", "", "b"]])
    with pytest.raises(ControlAmbiguous):
        browser._one_question(two_groups, lambda: Many([]))  # type: ignore[arg-type,return-value]


# ---- review 2026-10-15, round 3: a lone loose match of another question --------
#
# get_by_label(label, exact=False) and get_by_role(..., exact=False) match a
# case-insensitive substring of the accessible name, and _one_question took a
# lone loose match without asking the exact lookup. When the asked question's
# own control has no accessible name (its text sits in a div) and one other
# control's label holds the label text, the answer went into that control:
# 'Location' inside 'Are you open to relocation?', 'Country' inside 'Country
# of citizenship'. Now a lone loose match is taken only when the exact lookup
# returns that same element; otherwise the label's own control in the page
# (question_control) decides.

LOCATION_BESIDE_RELOCATION = (
    '<div class="q"><div>Location</div><div role="combobox" id="loc"></div></div>'
    '<div class="q"><label for="reloc">Are you open to relocation?</label>'
    '<input type="text" id="reloc"></div>'
)
COUNTRY_BESIDE_CITIZENSHIP = (
    '<div class="q"><div>Country</div><select id="country"><option>United States</option>'
    "</select></div>"
    '<div class="q"><label for="cit">Country of citizenship</label>'
    '<select id="cit"><option>United States</option></select></div>'
)


@pytest.mark.parametrize(
    ("html", "label", "other_name", "other_role"),
    [
        (LOCATION_BESIDE_RELOCATION, "Location", "Are you open to relocation?", "textbox"),
        (COUNTRY_BESIDE_CITIZENSHIP, "Country", "Country of citizenship", "combobox"),
    ],
    ids=["location-vs-relocation", "country-vs-citizenship"],
)
def test_a_lone_loose_match_of_another_question_is_not_taken(
    html: str, label: str, other_name: str, other_role: str
) -> None:
    frame, els = _frame(html)
    other = Node(other_name)
    page = Page([(other_name, other_role, other)], frame)
    # the loose lookups find only the other question's control
    assert page.get_by_label(label).first is other
    assert page.get_by_label(label, exact=True).count() == 0
    got = browser._find_control(page, "q_1", label)  # type: ignore[arg-type]
    assert got is not other
    # …and the label's own control in the page is the one found
    assert got is els[0]


def test_a_lone_loose_match_of_another_question_is_never_filled_by_any_lookup() -> None:
    """With no page tree to decide from, nothing is found: blank and flag."""
    other = Node("Are you open to relocation?")
    page = Page(
        [("Are you open to relocation?", "textbox", other)],
        Frame(Controls(RuntimeError("frame detached"), [])),
    )
    assert browser._find_control(page, "q_1", "Location") is None  # type: ignore[arg-type]


def test_the_exact_label_of_one_control_is_still_found_by_label() -> None:
    box = Node("Location")
    frame, _ = _frame(LOCATION_BESIDE_RELOCATION)
    page = Page(
        [("Location", "combobox", box), ("Are you open to relocation?", "textbox", Node("r"))],
        frame,
    )
    assert browser._find_control(page, "q_1", "Location") is box  # type: ignore[arg-type]


def test_a_lone_loose_match_that_is_not_the_exact_match_is_not_taken() -> None:
    # the exact lookup returns another element (the page changed between the
    # reads), or the two cannot be compared: fail closed
    a, b = Node("a"), Node("b")
    assert browser._one_question(Found([a]), lambda: Found([b])) is None  # type: ignore[arg-type,return-value]

    class Broken(Node):
        def element_handle(self, timeout: float | None = None) -> Node:
            raise RuntimeError("element detached")

    c = Broken("c")
    assert browser._one_question(Found([c]), lambda: Found([c])) is None  # type: ignore[arg-type,return-value]


def test_the_partial_name_lookup_is_for_url_fields_only() -> None:
    class Loc:
        def __init__(self, n: int) -> None:
            self.n = n
            self.first = self

        def count(self) -> int:
            return self.n

    asked: list[str] = []

    class Scope:
        def locator(self, sel: str) -> Loc:
            asked.append(sel)
            return Loc(1 if sel.startswith("input[name*=") else 0)

        def get_by_label(self, label: str, exact: bool = False) -> Loc:
            return Loc(0)

        def get_by_role(self, role: str, **kw: Any) -> Loc:
            return Loc(0)

    got = browser._find_control(Scope(), "q_1", "LinkedIn Profile")  # type: ignore[arg-type]
    assert got is not None and 'input[name*="LinkedIn"]' in asked
    asked.clear()
    # any other label goes on to the page-tree lookup (unreadable here: None)
    assert browser._find_control(Scope(), "q_2", "Race/Ethnicity") is None  # type: ignore[arg-type]
    assert not any("name*=" in a for a in asked) and browser._FILLABLE in asked


def test_the_new_probes_are_read_only() -> None:
    for js in (
        browser._FORM_TREE_JS,
        browser._CONTROL_KINDS_JS,
        browser._CONTROL_PRINT_JS,
        browser._SAME_ELEMENT_JS,
        browser._OWN_NAMES_JS,
    ):
        assert ".click(" not in js and "dispatchEvent" not in js and "Enter" not in js
        assert "setAttribute" not in js and ".value =" not in js


# ---- review 2026-10-15: a sibling question's lone control ------------------------
#
# Step 3 (the nearest element above the label text that holds a control)
# counted controls, not questions. When the asked question's own widget is
# not fillable (Yes/No <button>s, a role=button dropdown, an "Attach"
# button) and a shared wrapper also holds a sibling question with one
# unlabelled control, the sibling's control came back: question_control
# returned (0, '') for all three DOMs below, and stage_job typed "Yes" into
# the LinkedIn box. Now the wrapper must hold nothing else of another
# question: no rendered text with letters outside the label text, the
# controls and their own labels, and no other widget.

YES_NO_BUTTONS_BESIDE_LINKEDIN = (
    '<div class="fields"><div><div>Are you authorized to work in the US?</div>'
    '<button type="button">Yes</button><button type="button">No</button></div>'
    '<div><div>LinkedIn</div><input type="text"></div></div>'
)
DROPDOWN_BESIDE_LOCATION = (
    '<div class="fields"><div><div>Will you now or in the future require sponsorship?</div>'
    '<div role="button" aria-haspopup="listbox">Select...</div></div>'
    '<div><input type="text" placeholder="Location"></div></div>'
)
ATTACH_BESIDE_PHONE = (
    '<div class="fields"><div><div>Cover letter</div><button type="button">Attach</button></div>'
    '<div><div>Phone</div><input type="text"></div></div>'
)


@pytest.mark.parametrize(
    ("html", "label"),
    [
        (YES_NO_BUTTONS_BESIDE_LINKEDIN, "Are you authorized to work in the US?"),
        (DROPDOWN_BESIDE_LOCATION, "Will you now or in the future require sponsorship?"),
        (ATTACH_BESIDE_PHONE, "Cover letter"),
        # an icon-only widget (no text) is still another widget
        (
            '<div class="fields"><div><div>Are you authorized to work in the US?</div>'
            '<button type="button" aria-label="Yes"></button></div>'
            '<div><input type="text" placeholder="LinkedIn"></div></div>',
            "Are you authorized to work in the US?",
        ),
        # an editable div (a rich-text answer box) beside another question's box
        (
            '<div class="fields"><div><div>Why do you want to work here?</div>'
            '<div contenteditable="true"></div></div>'
            '<div><input type="text" placeholder="Phone"></div></div>',
            "Why do you want to work here?",
        ),
    ],
    ids=["yes-no-buttons", "role-button-dropdown", "attach-button", "icon-button", "editable"],
)
def test_a_sibling_questions_lone_control_is_refused(html: str, label: str) -> None:
    assert question_control(tree_of(html), label) == (None, "container-holds-several-questions")


def test_the_sibling_questions_own_label_still_finds_its_box() -> None:
    """Refusing the asked question does not stop the sibling's own lookup."""
    assert question_control(tree_of(YES_NO_BUTTONS_BESIDE_LINKEDIN), "LinkedIn") == (0, "")
    assert question_control(tree_of(ATTACH_BESIDE_PHONE), "Phone") == (0, "")


def test_control_in_container_raises_for_a_sibling_questions_box() -> None:
    frame, _ = _frame(YES_NO_BUTTONS_BESIDE_LINKEDIN)
    with pytest.raises(ControlAmbiguous, match="container-holds-several-questions"):
        browser._control_in_container(frame, "Are you authorized to work in the US?")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "html",
    [
        # required/optional markers, a character counter
        '<div><div>Why us?</div><span>(optional)</span><textarea data-q="why"></textarea>'
        "<span>0/1000</span></div>",
        # text that is not rendered (a validation message waiting to show)
        '<div><div>Why us?</div><textarea data-q="why"></textarea>'
        '<div style="display: none">This field is <b>required</b> to continue</div>'
        '<div hidden><button type="button">Retry</button></div></div>',
        # the radios' own labels and the radiogroup around them
        '<div><div>Why us?</div><div role="radiogroup">'
        '<label><input type="radio" name="why" data-q="why">Pay</label>'
        '<label><input type="radio" name="why">Team</label></div></div>',
        # the radios' wrapping labels hold the label text's words too
        '<div><div>Why us?</div><label><input type="checkbox" name="why" data-q="why">'
        "Why us? I read the brief</label></div>",
    ],
    ids=["markers-and-counter", "hidden-text", "radiogroup", "own-wrapping-label"],
)
def test_what_belongs_to_the_question_itself_is_not_another_question(html: str) -> None:
    assert pick(html, "Why us?") == ("why", "")


def test_a_label_without_for_is_no_control_label() -> None:
    """own_label_text matched every <label> with no `for` to every control
    with no id ('' == ''), so a Lever textarea question was refused as
    labelled by another question."""
    html = """
    <ul>
      <li><label><div>Full name</div><input name="name" type="text"></label></li>
      <li><div><div class="text">Tell us about your enablement work</div>
        <textarea name="cards[x][field0]"></textarea></div></li>
    </ul>"""
    assert pick(html, "Tell us about your enablement work") == ("cards[x][field0]", "")


FIXTURES = Path(__file__).parent / "fixtures"


def test_every_question_on_the_recorded_lever_form_finds_its_own_control() -> None:
    """The recorded Lever apply page: each question's label leads to its own
    control (by name), or to nothing; never to another question's."""
    html = (FIXTURES / "lever_apply.html").read_text()  # the <form>, no <body>
    tree, names = tree_of(html), control_names(html)
    got: dict[str, str | None] = {}
    for q in BeautifulSoup(html, "html.parser").select("li.application-question"):
        el = q.select_one(".application-label")
        label = el.get_text(" ", strip=True) if el else ""
        own = {str(c.get("name") or c.get("id") or "") for c in q.select("input, select, textarea")}
        index, why = question_control(tree, label)
        got[label] = names[index] if index is not None else None
        assert index is None or names[index] in own, (label, names[index])
    assert got["Do you require sponsorship now or in the future? ✱"] == (
        "cards[001326c7-6b46-464c-a794-0c3336b493b5][field1]"
    )
    assert got[
        "Do you have experience supporting Enterprise Sales and SDR teams? If so, please "
        "describe your experience in 1–3 sentences. ✱"
    ] == ("cards[3891d943-3510-4701-9f4d-60c13bc85813][field0]")


# ---- and a choice answer is never typed into a text box ---------------------------


@pytest.mark.parametrize(
    ("ftype", "options"),
    [
        ("boolean", []),
        ("Boolean", []),
        ("multi_value_single_select", []),
        ("ValueSelect", []),
        ("MultiValueSelect", []),
        ("radio", []),
        ("checkbox", []),
        ("input_text", ["Yes", "No"]),
    ],
)
def test_a_choice_answer_is_never_typed_into_a_text_box(ftype: str, options: list[str]) -> None:
    from tests.test_staging_fill import El, Form, Tab

    for tag, attrs in (("input", {"type": "text"}), ("textarea", {})):
        box = El(tag=tag, attrs=attrs)
        assert not browser._fill_control(Tab(), Form(), box, "Yes", ftype, options)  # type: ignore[arg-type]
        assert box.value == ""
    assert browser.choice_shaped(ftype, options)


@pytest.mark.parametrize("ftype", ["input_text", "textarea", "String", "LongText", "text", ""])
def test_a_text_answer_is_still_typed(ftype: str) -> None:
    from tests.test_staging_fill import El, Form, Tab

    box = El(tag="input", attrs={"type": "text"})
    assert browser._fill_control(Tab(), Form(), box, "Yes", ftype, [])  # type: ignore[arg-type]
    assert box.value == "Yes"
    assert not browser.choice_shaped(ftype, [])


# ---- review 2026-10-20: the page lookup took another question's label ---------------
#
# _one_question refused the lone loose match, and _find_control fell through
# to the page lookup, where question_control took any element whose text
# HOLDS the label as the label's place when none IS it: with the asked
# question's own text absent from the page (a stale spec, other wording) or
# the only other text, 'Location' went into "Are you open to relocation?" and
# 'Country' into "Country of citizenship". An element that only holds the
# label now counts only when the label stands in it as whole words and it has
# no more letters than the label (markers aside), and _find_control never
# hands back a control a label lookup refused.

RELOCATION_ONLY = (
    '<div class="q"><label for="reloc">Are you open to relocation?</label>'
    '<input type="text" id="reloc"></div>'
)
CITIZENSHIP_ONLY = (
    '<div class="q"><label for="cit">Country of citizenship</label>'
    '<select id="cit"><option>United States</option></select></div>'
)
BASED_BESIDE_RELOCATION = (
    '<div class="q"><div>Where are you based?</div><input type="text" id="loc"></div>'
    '<div class="q"><label for="reloc">Are you open to relocation?</label>'
    '<input type="text" id="reloc"></div>'
)


@pytest.mark.parametrize(
    ("html", "label"),
    [
        (RELOCATION_ONLY, "Location"),
        (CITIZENSHIP_ONLY, "Country"),
        (BASED_BESIDE_RELOCATION, "Location"),
        # a text that holds the label as whole words, with more letters: a
        # longer question, not this one
        (
            '<div class="q"><div>Preferred work location</div><input type="text" id="p"></div>',
            "Location",
        ),
        (
            '<div class="q"><span>Country of citizenship</span><select id="c"><option>US'
            "</option></select></div>",
            "Country",
        ),
    ],
    ids=[
        "relocation-only",
        "citizenship-only",
        "based-beside-relocation",
        "preferred-work-location",
        "citizenship-span",
    ],
)
def test_a_label_held_only_inside_another_questions_text_finds_nothing(
    html: str, label: str
) -> None:
    assert question_control(tree_of(html), label) == (None, "")


@pytest.mark.parametrize(
    ("html", "label", "other_name", "other_role"),
    [
        (RELOCATION_ONLY, "Location", "Are you open to relocation?", "textbox"),
        (CITIZENSHIP_ONLY, "Country", "Country of citizenship", "combobox"),
        (BASED_BESIDE_RELOCATION, "Location", "Are you open to relocation?", "textbox"),
    ],
    ids=["relocation-only", "citizenship-only", "based-beside-relocation"],
)
def test_find_control_never_returns_the_other_questions_control(
    html: str, label: str, other_name: str, other_role: str
) -> None:
    frame, els = _frame(html)
    other = els[-1]  # the other question's control, as both lookups see it
    page = Page([(other_name, other_role, other)], frame)  # type: ignore[list-item]
    assert page.get_by_label(label).first is other
    try:
        got = browser._find_control(page, "q_1", label)  # type: ignore[arg-type]
    except ControlAmbiguous:
        got = None
    assert got is None
    # …and with the loose match a distinct object (the page lookup decides)
    page = Page([(other_name, other_role, Node(other_name))], frame)
    assert browser._find_control(page, "q_1", label) is None  # type: ignore[arg-type]


def test_a_control_a_label_lookup_refused_is_never_handed_back() -> None:
    # the asked label's text beside a box whose only label is its aria-label:
    # the page lookup finds that box from the "Location" text, but the loose
    # label lookup saw it was "Are you open to relocation?"
    html = (
        '<div class="q"><div>Location</div>'
        '<input type="text" aria-label="Are you open to relocation?"></div>'
    )
    frame, els = _frame(html)
    assert question_control(tree_of(html), "Location") == (0, "")
    page = Page([("Are you open to relocation?", "textbox", els[0])], frame)  # type: ignore[list-item]
    with pytest.raises(ControlAmbiguous, match="label-is-another-questions"):
        browser._find_control(page, "q_1", "Location")  # type: ignore[arg-type]


def test_a_refused_control_that_cannot_be_compared_is_refused() -> None:
    class Detached(Node):
        def element_handle(self, timeout: float | None = None) -> Node:
            raise RuntimeError("element detached")

    frame, els = _frame(
        '<div class="q"><div>Location</div><input type="text" aria-label="Where to?"></div>'
    )
    page = Page([("Your location history", "textbox", Detached("h"))], frame)
    with pytest.raises(ControlAmbiguous, match="label-is-another-questions"):
        browser._find_control(page, "q_1", "Location")  # type: ignore[arg-type]
    # nothing refused: the page lookup's control is taken
    page = Page([], frame)
    assert browser._find_control(page, "q_1", "Location") is els[0]  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("html", "label", "want"),
    [
        # the label's own text with only markers, punctuation or its control's
        # options beside it is still the label's place
        ('<div><label for="l">Location (required)</label><input id="l"></div>', "Location", "l"),
        (
            '<div><label for="l">Location: <span>*</span></label><input id="l"></div>',
            "Location",
            "l",
        ),
        (
            '<div><label for="g">Gender<select id="g"><option>Female</option>'
            "<option>Male</option></select></label></div>",
            "Gender",
            "g",
        ),
        # a long label cut at 40 characters: the page's shorter wording
        (
            '<div><label for="w">Are you legally authorized to work in the United States?'
            '</label><input id="w"></div>',
            "Are you legally authorized to work in the United States of America?",
            "w",
        ),
    ],
)
def test_the_labels_own_text_with_markers_is_still_found(html: str, label: str, want: str) -> None:
    assert pick(html, label) == (want, "")


def test_a_longer_question_sharing_a_long_labels_first_words_is_not_found() -> None:
    html = (
        '<div><label for="w">Are you legally authorized to work in the United States without '
        "employer visa sponsorship now or at any time in the future?</label>"
        '<input id="w"></div>'
    )
    label = "Are you legally authorized to work in the United States?"
    assert question_control(tree_of(html), label) == (None, "")


# ---- review 2026-10-20, round 3: the label's own control, marked required --------
#
# _one_question took a lone loose match only when Playwright's exact lookup
# returned it too, and exact=True is case-sensitive over the whole name. A
# field whose own label reads 'Location: *', 'Location (required)', 'First
# Name*' or differs only in case missed the exact lookup, so its control
# went into `refused`; question_control then found that same control (it
# squashes markers and case) and _not_refused raised
# ControlAmbiguous('label-is-another-questions'): the field went to the
# human. Now a lone loose match is taken when the element's own label text
# is the label once markers, punctuation, whitespace and case are dropped
# (_named_as, _label_key). 'Are you open to relocation?' and 'Country of
# citizenship' are other texts and are still refused.

OWN_LABEL_MARKED = [
    # (the page, the control's accessible name, the stored label)
    (
        '<div class="q"><label for="loc">Location: <span>*</span></label>'
        '<input type="text" id="loc"></div>',
        "Location: *",
        "Location",
    ),
    (
        '<div class="q"><label for="loc">Location <span>*</span></label>'
        '<input type="text" id="loc"></div>',
        "Location *",
        "Location",
    ),
    (
        '<div class="q"><label for="loc">Location (required)</label>'
        '<input type="text" id="loc"></div>',
        "Location (required)",
        "Location",
    ),
    (
        '<div class="q"><label for="fn">First Name*</label><input type="text" id="fn"></div>',
        "First Name*",
        "First Name",
    ),
    # a case difference
    (
        '<div class="q"><label for="loc">Location</label><input type="text" id="loc"></div>',
        "Location",
        "location",
    ),
    (
        '<div class="q"><label for="fn">FIRST NAME</label><input type="text" id="fn"></div>',
        "FIRST NAME",
        "First name",
    ),
]
OWN_LABEL_IDS = [
    "location-colon-star",
    "location-star",
    "location-required",
    "first-name-star",
    "case",
    "upper-case",
]


@pytest.mark.parametrize(("html", "name", "label"), OWN_LABEL_MARKED, ids=OWN_LABEL_IDS)
def test_find_control_takes_the_labels_own_control_with_markers_or_case(
    html: str, name: str, label: str
) -> None:
    frame, els = _frame(html)
    page = Page([(name, "textbox", els[0])], frame)  # type: ignore[list-item]
    # the loose lookup finds the field's own control; the exact one does not
    assert page.get_by_label(label).first is els[0]
    assert page.get_by_label(label, exact=True).count() == 0
    assert browser._find_control(page, "q_1", label) is els[0]  # type: ignore[arg-type]


@pytest.mark.parametrize(("html", "name", "label"), OWN_LABEL_MARKED, ids=OWN_LABEL_IDS)
def test_before_the_own_label_check_these_fields_went_to_the_human(
    html: str, name: str, label: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's repro: with no own-label check, the loose match is
    refused and the page lookup's same control raises."""
    monkeypatch.setattr(browser, "_named_as", lambda loc, label: None)
    frame, els = _frame(html)
    page = Page([(name, "textbox", els[0])], frame)  # type: ignore[list-item]
    with pytest.raises(ControlAmbiguous, match="label-is-another-questions"):
        browser._find_control(page, "q_1", label)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("html", "label", "other_name", "other_role"),
    [
        (LOCATION_BESIDE_RELOCATION, "Location", "Are you open to relocation?", "textbox"),
        (COUNTRY_BESIDE_CITIZENSHIP, "Country", "Country of citizenship", "combobox"),
        (RELOCATION_ONLY, "Location", "Are you open to relocation?", "textbox"),
        (CITIZENSHIP_ONLY, "Country", "Country of citizenship", "combobox"),
    ],
    ids=[
        "location-vs-relocation",
        "country-vs-citizenship",
        "relocation-only",
        "citizenship-only",
    ],
)
def test_find_control_still_refuses_a_loose_match_whose_own_label_is_another_text(
    html: str, label: str, other_name: str, other_role: str
) -> None:
    frame, els = _frame(html)
    other = els[-1]  # the other question's control: its own label is other_name
    page = Page([(other_name, other_role, other)], frame)  # type: ignore[list-item]
    assert browser._named_as(other, label) is False  # type: ignore[arg-type]
    refused: list[Any] = []
    got = browser._one_question(
        page.get_by_label(label),  # type: ignore[arg-type]
        lambda: page.get_by_label(label, exact=True),  # type: ignore[arg-type,return-value]
        refused,
        label=label,
    )
    assert got is None and refused == [other]
    try:
        found = browser._find_control(page, "q_1", label)  # type: ignore[arg-type]
    except ControlAmbiguous:
        found = None
    assert found is not other
    # beside its own text the label's own control is found; alone, nothing
    assert found is (els[0] if len(els) > 1 else None)


def test_a_loose_match_whose_own_label_cannot_be_read_is_refused() -> None:
    """Fail closed: the probe fails, so the loose match is not confirmed and
    stays refused; the page lookup's same control raises."""
    frame, els = _frame(OWN_LABEL_MARKED[1][0])
    els[0].names = None
    page = Page([("Location *", "textbox", els[0])], frame)  # type: ignore[list-item]
    assert browser._named_as(els[0], "Location") is None  # type: ignore[arg-type]
    with pytest.raises(ControlAmbiguous, match="label-is-another-questions"):
        browser._find_control(page, "q_1", "Location")  # type: ignore[arg-type]


def test_a_loose_match_the_exact_lookup_contradicts_is_not_taken_by_its_name() -> None:
    # the exact lookup returns another element (the page changed between the
    # reads): its own name is not asked, the loose match is refused
    a, b = Node("Location *"), Node("Location")
    refused: list[Any] = []
    got = browser._one_question(
        Found([a]),  # type: ignore[arg-type]
        lambda: Found([b]),  # type: ignore[arg-type,return-value]
        refused,
        label="Location",
    )
    assert got is None and refused == [a]


@pytest.mark.parametrize(
    ("name", "label", "same"),
    [
        ("Location: *", "Location", True),
        ("Location (required)", "Location", True),
        ("Location (Optional)", "location", True),
        ("First Name*", "First Name", True),
        ("first  name ✱", "First Name", True),
        ("U.S. citizen?", "US citizen", True),
        ("Are you open to relocation?", "Location", False),
        ("Country of citizenship", "Country", False),
        ("Location required", "Location", False),  # a bare word is not a marker
        ("Preferred location", "Location", False),
    ],
)
def test_label_key(name: str, label: str, same: bool) -> None:
    assert (browser._label_key(name) == browser._label_key(label)) is same
