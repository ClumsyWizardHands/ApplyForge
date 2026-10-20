"""Stage 11 — Playwright staging (M5). Headed, persistent profile, human-paced.

THE ONE HARD RULE: this module never targets a submit/apply/send control.
Staging fills, highlights, screenshots, and STOPS at the review screen. The
human clicks. A static test scans this file to keep it that way, and a
runtime guard (refuse_send_control) probes every element before a fill click.

Abort conditions (answer_bank.yaml): CAPTCHA/Turnstile challenge shown, SSN/
DOB/government-ID or payment fields, embedded assessments, required field with
no bank match and no safe draft, or no application form on the page (closed
posting, wrong tab) → status needs_human with the reason. Never guessed at,
never dropped: a batch longer than MAX_TABS runs in successive batches.

Review 2026-10-12: the resume and cover letter go up
first, as copies named First_Last_Resume.pdf / First_Last_Cover_Letter.pdf,
and staging waits (bounded) for an ATS resume parser to finish before it
fills; every filled field is read back at the end and refilled once if the
page changed it; and a CAPTCHA check follows every click the filler makes
and the end of the fill, so a challenge the fill provoked stops the run."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
import unicodedata
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from playwright.sync_api import BrowserContext, Frame, Locator, Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from jobkit import db, paths
from jobkit.autofill.answer_bank import (
    BankMatcher,
    comp_expectation_block_for,
    human_answer_barred,
    load_bank,
    never_draft,
    screening_entry,
)
from jobkit.autofill.options import (
    choose_location,
    choose_option,
    location_queries,
    mentions_decline,
    parse_location,
    search_text,
)
from jobkit.log import log
from jobkit.pipeline import letter_gate

PROFILE_DIR = paths.HOME / "data" / "browser_profile"  # project root (paths.py)
SHOT_DIR = paths.HOME / "out" / "screenshots"
# the recruiter-named copies of resume.pdf / cover.pdf that staging uploads,
# one folder per job. Kept (not a throwaway temp dir): the browser reads an
# uploaded file again when the human sends the form, possibly hours later.
UPLOAD_DIR = paths.HOME / "out" / "uploads"
SLOW_MO_MS = 120
MAX_TABS = 8  # SPEC §7.1: hard cap on CONCURRENT tabs; longer lists run in batches
FORM_WAIT_MS = 15000  # how long a form may take to render (SPA, embedded iframe)
# after an upload: the longest wait for an ATS resume parser to finish
# rewriting the form, the quiet spell that counts as finished once it changed
# something, and the wait that counts as "it parses nothing" when it did not
PARSER_WAIT_MS = 8000
PARSER_QUIET_MS = 1500
PARSER_MIN_WAIT_MS = 3000
READBACK_SETTLE_MS = 500  # after the refills, before the second read-back
# the halt reason (prefix) when a CAPTCHA appears after a click the filler made
CAPTCHA_PROVOKED = "captcha-provoked-by-fill"
# the unfilled reason of a field the page changed after it was filled, twice
OVERWRITTEN = "overwritten-after-fill"

_CAPTCHA = re.compile(r"captcha|turnstile|hcaptcha|recaptcha|cf-challenge", re.I)
_HARD_STOP_FIELD = re.compile(
    r"social security|\bssn\b|date of birth|\bdob\b|passport|driver.s license|"
    r"government.issued|bank account|routing number|credit card|payment",
    re.I,
)
_ASSESSMENT = re.compile(r"assessment|timed test|coding challenge|hackerrank|codility", re.I)

# provenance colors (SPEC §7.1): bank plain/green, AI-drafted amber, needs-human red
_OUTLINE_JS = """
(args) => {
  const [selector, color] = args;
  const el = document.querySelector(selector);
  if (el) { el.style.outline = '3px solid ' + color; el.style.outlineOffset = '2px'; }
}
"""


def _identity_value(entry: str, bank: dict[str, Any]) -> str | None:
    ident = bank.get("identity", {})
    mapping = {
        "name": f"{ident.get('first_name', '')} {ident.get('last_name', '')}".strip(),
        "first_name": ident.get("first_name"),
        "last_name": ident.get("last_name"),
        "email": ident.get("email"),
        "phone": ident.get("phone"),
        "linkedin": ident.get("linkedin"),
        # each link field gets its own key or stays blank + flagged — never
        # another field's value (LinkedIn typed into GitHub/Portfolio, audit 2026-10-08)
        "github": ident.get("github"),
        "portfolio": ident.get("portfolio"),
        "portfolio_or_github": ident.get("portfolio") or ident.get("github"),
        "twitter": ident.get("twitter"),
        "city": ident.get("city"),
        "state": ident.get("state"),
        "country": ident.get("country"),
        "postal_code": ident.get("postal_code"),  # unset -> unfilled, never guessed
        "address": f"{ident.get('city', '')}, {ident.get('state', '')}".strip(", "),
    }
    v = mapping.get(entry)
    return str(v) if v else None


# apply URLs come from the DB (ultimately from the internet). Staging only ever
# navigates to known ATS hosts or the target company's own domain — a poisoned
# listing must not be able to point the autofiller at an arbitrary form.
_ATS_HOSTS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "workable.com",
    "recruitee.com",
    "rippling.com",
)


def apply_url_allowed(url: str, company_domain: str | None) -> bool:
    try:
        u = urlparse(url)
    except ValueError:
        return False
    if u.scheme != "https" or not u.hostname:
        return False
    host = u.hostname.lower()
    allowed = list(_ATS_HOSTS)
    if company_domain:
        d = company_domain.lower()
        allowed.append(d.removeprefix("www."))
    return any(host == d or host.endswith("." + d) for d in allowed)


_ASHBY_POSTING = re.compile(
    r"^/([^/]+)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?$", re.I
)


def normalize_apply_url(url: str) -> str:
    """Point the browser at the form itself. An Ashby posting URL without
    /application opens the Overview tab, which has no fields."""
    try:
        u = urlparse(url)
    except ValueError:
        return url
    if (u.hostname or "").lower() == "jobs.ashbyhq.com":
        m = _ASHBY_POSTING.match(u.path)
        if m:
            return urlunparse(u._replace(path=f"/{m.group(1)}/{m.group(2)}/application"))
    return url


def _screening_value(entry_id: str, bank: dict[str, Any]) -> str | None:
    for s in bank.get("screening", []):
        if s["id"] == entry_id:
            if s.get("action") == "leave_blank_and_flag":
                return None
            a = s.get("answer")
            return str(a) if a is not None else None
    return None


def _value_for(
    match: dict[str, Any], bank: dict[str, Any], drafted: dict[str, str], identity: str
) -> tuple[str | None, str]:
    """Returns (value, source) — source per SPEC §7.1 provenance."""
    kind = match.get("kind")
    if kind == "blank_flag":
        return None, "unfilled"  # flagged for the human: no bank value, no draft
    if kind == "human":  # answered through MCP answer_novel (staging_match)
        return (drafted[identity], "human_typed") if identity in drafted else (None, "unfilled")
    if kind == "identity":
        return _identity_value(str(match.get("entry_id")), bank), "answer_bank"
    if kind == "screening":
        return _screening_value(str(match.get("entry_id")), bank), "answer_bank"
    if kind == "eeo":
        return str(match.get("answer")), "answer_bank"
    if identity in drafted:
        return drafted[identity], "ai_drafted"
    return None, "unfilled"


def staging_match(
    matcher: BankMatcher,
    field: dict[str, Any],
    comp_block: str | None,
    human_answered: Collection[str] = (),
) -> dict[str, Any]:
    """Re-match a captured field against the CURRENT bank and matcher.

    Verdicts stored at capture go stale (audit 2026-10-08): a full name kept
    landing in 'Preferred First Name', an email in a former-employee question,
    and bank fixes never reached queued jobs. So staging matches again, with the
    ATS field id and this job's salary-expectation gate. A field the fresh
    matcher leaves to drafting stays blank if capture had flagged it, or if it
    is a kind of question that is never drafted (never_draft).

    human_answered: identities the human answered through MCP answer_novel
    (db.HUMAN_ANSWERED_FIELDS). Such a field gets a 'human' verdict and is
    filled with the human's answer: never_draft, a capture-time flag and the
    salary-expectation gate exist to keep the AI from answering, and a human
    answer is not a draft. EEO fields (hard rule 4) and compensation history
    (hard rule 5) stay with the matcher whoever answered, and a file upload
    stays an upload."""
    identity, label = str(field.get("identity") or ""), str(field.get("label") or "")
    stored = field.get("match") or {}
    fresh = matcher.match(
        label or identity,
        field.get("compliance_type"),
        identity=identity or None,
        comp_expectation_block=comp_block,
    )
    if (
        identity
        and identity in human_answered
        and fresh.get("kind") != "attach"
        and not human_answer_barred(label or identity, field.get("compliance_type"))
    ):
        return {"kind": "human", "source": "human_typed"}
    if fresh.get("kind") in ("novel", "scaffold"):
        if stored.get("kind") == "blank_flag":
            return dict(stored)
        blocked = never_draft(label or identity)
        if blocked:
            return {"kind": "blank_flag", "reason": f"never-draft:{blocked}"}
    return fresh


class ControlAmbiguous(LookupError):
    """The label leads to the controls of more than one question: a wrapper
    shared by several questions, or a label that several controls answer
    to. Nothing is filled; the field goes to the human as
    'control-ambiguous:<why>' (review 2026-10-12: the Race/Ethnicity answer
    went into Location)."""


# label words of the URL fields whose control is found by a partial name or
# placeholder (Lever: urls[LinkedIn profile]); any other label never is
_URLISH = frozenset({"linkedin", "github", "twitter", "portfolio", "website", "other", "x"})


def _find_control(scope: Frame, identity: str, label: str) -> Locator | None:
    """Layered locator strategy: name attr → id → aria label → role-by-name →
    the label's own control in the page (question_control). Never guesses
    selectors from thin air (SPEC stage 11.3). `scope` is the frame that
    holds the form (the page, or an embedded iframe).

    A label several controls answer to, of more than one question, raises
    ControlAmbiguous rather than taking the first (exact=False matches
    "Location" inside "Preferred work location"). A lone loose match is
    taken only when the exact lookup returns it too, or its own label is the
    label but for markers, punctuation, whitespace and case ("Location: *"):
    "Location" never lands in a box labelled "Are you open to relocation?"
    (_one_question). Nor do
    the later lookups hand that refused control back (review 2026-10-20: the
    page lookup found it from a "Location" text beside a box whose only
    label is its aria-label): ControlAmbiguous('label-is-another-questions')."""
    for sel in (f'[name="{identity}"]', f'[id="{identity}"]'):
        loc = scope.locator(sel)
        if loc.count() == 1:
            return loc
    if not label:
        return None
    refused: list[Locator] = []
    hit = _one_question(
        scope.get_by_label(label, exact=False),
        lambda: scope.get_by_label(label, exact=True),
        refused,
        label=label,
    )
    if hit is not None:
        return hit
    for role in ("textbox", "combobox", "radiogroup", "checkbox"):
        hit = _one_question(
            scope.get_by_role(role, name=label, exact=False),  # type: ignore[arg-type,unused-ignore]
            lambda role=role: scope.get_by_role(role, name=label, exact=True),  # type: ignore[arg-type,misc,unused-ignore]
            refused,
            label=label,
        )
        if hit is not None:
            return hit
    # radio groups share a name across several inputs — return the group anchor
    # (the field's own identity, like the name/id lookups above)
    radios = scope.locator(f'input[type="radio"][name="{identity}"]')
    if radios.count() >= 2:
        return radios.first
    return _not_refused(_found_after_labels(scope, identity, label), refused, label)


def _found_after_labels(scope: Frame, identity: str, label: str) -> Locator | None:
    """_find_control's lookups after the label and identity ones: a URL
    field by partial name, the label's own control in the page."""
    # URL fields only: match by partial name/placeholder (Lever: urls[LinkedIn profile])
    key = label.split()[0] if label else identity
    if key.strip(":*").lower() in _URLISH:
        for sel in (f'input[name*="{key}"]', f'input[placeholder*="{key}"]'):
            loc = scope.locator(sel)
            if loc.count() == 1:
                return loc
    # the label's own control in the page: label-for, aria-labelledby, or the
    # nearest element above the label text that holds one question's controls
    return _control_in_container(scope, label.replace('"', ""))


def _not_refused(found: Locator | None, refused: Sequence[Locator], label: str) -> Locator | None:
    """`found`, unless it is (or may be: it cannot be told) a control a label
    lookup refused as another question's (_one_question): then
    ControlAmbiguous('label-is-another-questions'), never that control."""
    if found is None:
        return None
    for other in refused:
        if _same_element(found, other) is not False:
            log("stage.control_ambiguous", label=label[:120], reason="label-is-another-questions")
            raise ControlAmbiguous("label-is-another-questions")
    return found


# what each matched element is, for telling one question's radio or checkbox
# group from the controls of several questions. Read-only.
_CONTROL_KINDS_JS = """
(els) => els.map((e) => [
  e.tagName.toLowerCase(),
  (e.getAttribute('type') || '').toLowerCase(),
  (e.getAttribute('role') || '').toLowerCase(),
  e.getAttribute('name') || '',
])
"""


def _one_group(loc: Locator) -> bool:
    """Every element `loc` matches is one radio or checkbox group (inputs
    sharing a name): one question's options, not several questions."""
    try:
        kinds = loc.evaluate_all(_CONTROL_KINDS_JS)
    except Exception:  # noqa: BLE001 — cannot tell: not one group
        return False
    if not isinstance(kinds, list) or len(kinds) < 2:
        return False
    names = set()
    for k in kinds:
        if not (isinstance(k, list) and len(k) == 4):
            return False
        tag, typ, role, name = (str(x) for x in k)
        if not name or not (typ in _CHOICE_TYPES or role in _CHOICE_TYPES):
            return False
        names.add(name)
    return len(names) == 1


# whether two locators' elements are one DOM element. Read-only.
_SAME_ELEMENT_JS = "(el, other) => el === other"


def _same_element(a: Locator, b: Locator) -> bool | None:
    """True when `a` and `b` resolve to the same DOM element, False when they
    do not, None when it cannot be told. Each caller fails closed its own
    way: a match is taken only on True, a refused control is refused unless
    False."""
    handle = None
    try:
        handle = b.element_handle(timeout=2000)
        same = a.evaluate(_SAME_ELEMENT_JS, handle)
    except Exception:  # noqa: BLE001 — cannot tell
        return None
    finally:
        if handle is not None:
            with contextlib.suppress(Exception):  # the handle is gone either way
                handle.dispose()
    return same if isinstance(same, bool) else None


# The element's own label texts, in Playwright's get_by_label order: the
# elements its aria-labelledby names (each, and joined), else its
# aria-label, else its <label>s; with none of those, its title or else its
# placeholder (the accessible-name fallbacks get_by_role reads). A label's
# text leaves out the controls inside it (a wrapped <select>'s options).
# Read-only.
_OWN_NAMES_JS = """
(el) => {
  const inner = new Set(['SELECT', 'OPTION', 'OPTGROUP', 'TEXTAREA', 'INPUT', 'BUTTON',
                         'SCRIPT', 'STYLE', 'TEMPLATE']);
  const text = (node) => {
    let out = '';
    for (const c of Array.from(node.childNodes || [])) {
      if (c.nodeType === 3) out += c.nodeValue || '';
      else if (c.nodeType === 1 && !inner.has(String(c.tagName || '').toUpperCase())) {
        out += ' ' + text(c) + ' ';
      }
    }
    return out;
  };
  const ids = (el.getAttribute('aria-labelledby') || '').split(/\\s+/).filter(Boolean);
  const refs = ids.map((id) => el.ownerDocument.getElementById(id)).filter(Boolean);
  if (refs.length) return refs.map(text).concat([refs.map(text).join(' ')]);
  const aria = el.getAttribute('aria-label') || '';
  if (aria.trim()) return [aria];
  const labels = Array.from(el.labels || []).map(text);
  if (labels.length) return labels;
  const title = el.getAttribute('title') || '';
  return [title.trim() ? title : el.getAttribute('placeholder') || ''];
}
"""
# required/optional markers in brackets, and what is not a letter or digit
_BRACKET_MARKER = re.compile(r"\((?:optional|required)\)")
_NOT_ALNUM = re.compile(r"[\W_]+")


def _label_key(text: str) -> str:
    """A label as _one_question compares an element's own name with it:
    _squash (whitespace, '*'/'✱' markers and case dropped), then a bracketed
    '(required)'/'(optional)' marker and punctuation dropped too. 'Location:
    *', 'Location (required)' and 'location' are 'Location'; 'Are you open to
    relocation?' and 'Country of citizenship' are other texts."""
    return _NOT_ALNUM.sub("", _BRACKET_MARKER.sub("", _squash(text)))


def _named_as(loc: Locator, label: str) -> bool | None:
    """True when one of the element's own label texts (_OWN_NAMES_JS) is
    `label` once both are reduced by _label_key, False when none is, None
    when they cannot be read (or the label reduces to nothing)."""
    want = _label_key(label)
    if not want:
        return None
    try:
        names = loc.evaluate(_OWN_NAMES_JS, timeout=2000)
    except Exception:  # noqa: BLE001 — cannot tell
        return None
    if not isinstance(names, list) or not all(isinstance(x, str) for x in names):
        return None
    return any(_label_key(x) == want for x in names)


def _one_question(
    loose: Locator,
    exact: Callable[[], Locator],
    refused: list[Locator] | None = None,
    *,
    label: str = "",
) -> Locator | None:
    """The control a label lookup found, when it is one question's: one radio
    or checkbox group, or one element the exact-text lookup also returns.
    exact=False matches a case-insensitive substring of the accessible name,
    so a lone loose match may be another question's control whose label
    holds this one ("Location" inside "Are you open to relocation?",
    "Country" inside "Country of citizenship"; review 2026-10-15, round 3):
    it is taken only when the exact lookup returns that same element (or
    one group), or, when the exact lookup returns nothing, when the
    element's own label text IS `label` but for markers, punctuation,
    whitespace and case (_named_as: 'Location: *', 'Location (required)',
    'First Name*', 'location'; review 2026-10-20, round 3: Playwright's
    exact match is case-sensitive and covers the whole name, so it missed
    these). Else None, and the caller goes on to the label's own control in
    the page (question_control). Several elements: the exact-text lookup
    decides; still several (of more than one question) raises
    ControlAmbiguous. None when nothing matched. A lone loose match not
    taken (its own label is another text, or cannot be read) is added to
    `refused`, for _find_control to keep it refused."""
    n = loose.count()
    if n == 0:
        return None
    if n > 1 and _one_group(loose):
        return loose.first
    tight = exact()
    m = tight.count()
    if n == 1:
        if m == 1 and _same_element(loose.first, tight.first) is True:
            return tight.first
        if m > 1 and _one_group(tight):
            return tight.first
        if m == 0 and label and _named_as(loose.first, label) is True:
            return loose.first  # its own label, with markers, punctuation or case
        if refused is not None:
            refused.append(loose.first)
        return None  # another question's label holds this one: not this control
    if m == 1 or (m > 1 and _one_group(tight)):
        return tight.first
    raise ControlAmbiguous("label-matches-several-controls")


# The page as a tree for question_control, read-only: every element under
# <body> (scripts, styles, svg and iframes skipped) with its parent, tag, the
# attributes that tie a label to a control, its text and child elements in
# document order (`parts`: a string, or a child's index), and `ctl`: its
# index among the frame's fillable controls (`els`, Playwright's order for
# _FILLABLE), or -1. `hid`: 1 when the element is not rendered (no layout
# box: display:none, or inside such an element); `ce`: its contenteditable.
_FORM_TREE_JS = """
(els) => {
  const pos = new Map(els.map((e, i) => [e, i]));
  const skip = new Set(['script', 'style', 'noscript', 'template', 'svg', 'head', 'iframe']);
  const out = [];
  const visit = (el, parent, depth) => {
    const tag = String(el.tagName || '').toLowerCase();
    if (skip.has(tag) || out.length >= 30000 || depth > 200) return -1;
    const i = out.length;
    const a = (k) => (el.getAttribute ? el.getAttribute(k) || '' : '');
    const node = {
      p: parent, tag, id: a('id'), for: a('for'), lb: a('aria-labelledby'), name: a('name'),
      type: a('type').toLowerCase(), role: a('role').toLowerCase(),
      ce: a('contenteditable').toLowerCase(),
      hid: el.getClientRects && el.getClientRects().length === 0
        && getComputedStyle(el).display !== 'contents' ? 1 : 0,
      ctl: pos.has(el) ? pos.get(el) : -1, parts: [],
    };
    out.push(node);
    for (const c of Array.from(el.childNodes || [])) {
      if (c.nodeType === 3) {
        const t = c.nodeValue || '';
        if (t.trim()) node.parts.push(t.slice(0, 4000));
      } else if (c.nodeType === 1) {
        const j = visit(c, i, depth + 1);
        if (j >= 0) node.parts.push(j);
      }
    }
    return i;
  };
  if (document.body) visit(document.body, -1, 0);
  return out;
}
"""
# the element question_control chose, to check it is still the one at that
# index when the locator is used. Read-only.
_CONTROL_PRINT_JS = """
(el) => [el.tagName.toLowerCase(), el.getAttribute('id') || '', el.getAttribute('name') || '',
         (el.getAttribute('type') || '').toLowerCase()]
"""
_FILLABLE = "input:not([type=hidden]), select, textarea, [role=combobox]"
_CHOICE_TYPES = frozenset({"radio", "checkbox"})
_SQUASH = re.compile(r"[\s*✱]+")
_TEXT_CAP = 20000  # characters of an element's text kept; holders are short
_LABEL_REACH = 3  # how far above the label text a <label> or an id may sit
# an element (not one of _FILLABLE) a question can be answered with: the
# asked question's own widget may be this one, not the fillable control
# beside it (review 2026-10-15: Yes/No <button>s, a role=button dropdown, an
# "Attach" button beside another question's text box)
_WIDGET_TAGS = frozenset({"button"})
_WIDGET_ROLES = frozenset(
    {
        "button",
        "textbox",
        "searchbox",
        "listbox",
        "switch",
        "slider",
        "spinbutton",
        "radiogroup",
        "radio",
        "checkbox",
        "menu",
    }
)
# text beside a control that is not another question's: required/optional
# markers (after _squash); text with fewer than two letters (a counter, "*")
_MARKER_TEXT = re.compile(r"\(?(?:optional|required)\)?")
_LETTER = re.compile(r"[^\W\d_]")
_WORD_CHAR = re.compile(r"[^\W_]")


def _squash(text: str) -> str:
    """Text compared without whitespace, required-markers or case: DOM text
    splits a label across elements ("Race/<b>Ethnicity</b> *")."""
    return _SQUASH.sub("", unicodedata.normalize("NFKC", text).replace("\xa0", " ")).casefold()


def question_control(nodes: Sequence[Mapping[str, Any]], label: str) -> tuple[int | None, str]:
    """The one control a question's label belongs to in the page tree
    (_FORM_TREE_JS): (its index among the fillable controls, ''), (None, '')
    when the label text or its control is not found, or (None, why) when the
    label leads to the controls of more than one question.

    Where the label text is: the innermost elements whose text holds the
    whole label (an element whose text IS the label is preferred over one
    that only contains it; never inside a <select>, an <option> or a
    control). With none whose text is the label, one that only contains it
    counts only when the label stands in it as whole words and it has no
    more letters than the label, markers aside: "Location" is not found in
    "Are you open to relocation?", nor "Country" in "Country of
    citizenship" (review 2026-10-20). From each, in order:
    1. a <label> at or just above it: the control its `for` names, or the
       controls inside it;
    2. the controls whose aria-labelledby names it (or an element between it
       and the nearest element holding a control);
    3. the nearest element above it that holds any control: the question's
       own wrapper, but only when it holds nothing else of another question:
       no rendered text with letters outside the label text, the controls
       and their own labels, and no other widget (a button, a role=button
       dropdown, an editable div). Otherwise the control may be a sibling
       question's while the asked question's own widget is one the fill
       cannot use (review 2026-10-15: Yes/No buttons beside an unlabelled
       LinkedIn box): 'container-holds-several-questions'.
    The controls found must be ONE question's: one control, or one radio or
    checkbox group (a shared name, or one fieldset / radiogroup / group).
    A wrapper holding controls of two questions (two unnamed boxes side by
    side, the reviewer's "form-fields" div) is never narrowed to its first
    control: why = 'container-holds-several-questions'. By (3), a single
    control with its own label that is another text is another question's:
    'control-labelled-by-another-question'. Label text found in several
    places that lead to different controls: 'label-matches-several-controls'."""
    n = len(nodes)
    if not n:
        return None, ""
    par = [-1] * n
    for i, nd in enumerate(nodes):
        p = nd.get("p")
        par[i] = p if isinstance(p, int) and not isinstance(p, bool) and 0 <= p < i else -1
    kids: list[list[int]] = [[] for _ in range(n)]
    for i in range(n):
        if par[i] >= 0:
            kids[par[i]].append(i)
    tag = [str(nd.get("tag") or "") for nd in nodes]
    hid = [bool(nd.get("hid")) for nd in nodes]
    ctl = [
        c if isinstance(c := nd.get("ctl"), int) and not isinstance(c, bool) else -1 for nd in nodes
    ]

    def attr(i: int, k: str) -> str:
        return str(nodes[i].get(k) or "")

    full = [""] * n
    end = list(range(n))  # last index of each subtree (preorder: contiguous)
    for i in range(n - 1, -1, -1):
        buf: list[str] = []
        for x in nodes[i].get("parts") or []:
            if isinstance(x, str):
                buf.append(_squash(x))
            elif isinstance(x, int) and not isinstance(x, bool) and i < x < n and par[x] == i:
                buf.append(full[x])
        full[i] = "".join(buf)[:_TEXT_CAP]
        for k in kids[i]:
            end[i] = max(end[i], end[k])
    by_id: dict[str, int] = {}
    for i in range(n):
        by_id.setdefault(attr(i, "id"), i)
    by_id.pop("", None)
    controls = [i for i in range(n) if ctl[i] >= 0]

    def ancestors(i: int) -> list[int]:
        out: list[int] = []
        a = par[i]
        while a >= 0:
            out.append(a)
            a = par[a]
        return out

    def is_choice(i: int) -> bool:
        return attr(i, "type") in _CHOICE_TYPES or attr(i, "role") in _CHOICE_TYPES

    def outer_controls(a: int) -> list[int]:
        """Controls in a's subtree, a control inside another one aside."""
        found = [j for j in range(a, end[a] + 1) if ctl[j] >= 0]
        return [j for j in found if not any(ctl[x] >= 0 for x in ancestors(j) if x >= a)]

    def groups(cs: list[int], top: int) -> int:
        keys: set[tuple[str, int | str]] = set()
        for j in cs:
            if is_choice(j) and attr(j, "name"):
                keys.add(("name", attr(j, "name")))
                continue
            box = next(
                (
                    x
                    for x in ancestors(j)
                    if x >= top
                    and (tag[x] == "fieldset" or attr(x, "role") in ("radiogroup", "group"))
                ),
                None,
            )
            keys.add(("box", box) if is_choice(j) and box is not None else ("one", j))
        return len(keys)

    def own_label_text(j: int) -> list[str]:
        """The texts of the labels a control has of its own."""
        texts = [
            full[i]
            for i in range(n)
            if tag[i] == "label" and attr(j, "id") and attr(i, "for") == attr(j, "id")
        ]
        texts += [full[a] for a in ancestors(j) if tag[a] == "label"]
        texts += [full[by_id[x]] for x in attr(j, "lb").split() if x in by_id]
        return [t for t in texts if t] if attr(j, "id") or texts else []

    def labels_of(j: int, top: int) -> list[int]:
        """The elements under `top` that label control j: a <label for> its
        id, a <label> around it, an element its aria-labelledby names."""
        out = [
            i
            for i in range(top, end[top] + 1)
            if tag[i] == "label" and attr(j, "id") and attr(i, "for") == attr(j, "id")
        ]
        out += [x for x in ancestors(j) if x > top and tag[x] == "label"]
        out += [
            by_id[x] for x in attr(j, "lb").split() if x in by_id and top < by_id[x] <= end[top]
        ]
        return out

    def shown_text(x: int, skip: set[int]) -> str:
        """x's rendered text in document order, the `skip` subtrees left out."""
        if x in skip or hid[x]:
            return ""
        buf: list[str] = []
        for part in nodes[x].get("parts") or []:
            if isinstance(part, str):
                buf.append(_squash(part))
                continue
            child = part if isinstance(part, int) and not isinstance(part, bool) else -1
            if x < child < n and par[child] == x:
                buf.append(shown_text(child, skip))
        return "".join(buf)

    def holds_another_question(a: int, cs: list[int], h: int, key: str) -> bool:
        """Does container `a`, besides the label text at h and the controls
        cs (with their own labels), hold anything of another question:
        rendered text with letters in it, or a widget that is not one of the
        fillable controls (a button, a role=button dropdown, an editable
        div)? Then the control may be another question's, and the asked
        question's own widget one the fill cannot use (review 2026-10-15:
        Yes/No buttons beside a LinkedIn box handed back the LinkedIn box)."""
        skip: set[int] = set()
        for j in cs:
            skip.add(j)
            skip.update(labels_of(j, a))
        h_holds_control = any(h <= j <= end[h] for j in cs)
        if not h_holds_control:
            skip.add(h)
        for x in range(a + 1, end[a] + 1):
            if hid[x] or ctl[x] >= 0 or any(s <= x <= end[s] for s in skip):
                continue
            if any(x <= j <= end[x] for j in cs):
                continue  # a wrapper of the controls themselves (a radiogroup)
            ce = attr(x, "ce")
            if (
                tag[x] in _WIDGET_TAGS
                or attr(x, "role") in _WIDGET_ROLES
                or ce not in ("", "false")
            ):
                return True
        text = shown_text(a, skip)
        if h_holds_control:
            text = text.replace(key, "", 1)
        return len(_LETTER.findall(_MARKER_TEXT.sub("", text))) >= 2

    def one_question(cs: list[int], top: int) -> tuple[list[int], str]:
        if not cs:
            return [], ""
        if groups(cs, top) != 1:
            return [], "container-holds-several-questions"
        return [cs[0]], ""

    def resolve(h: int, key: str) -> tuple[list[int], str]:
        up = ancestors(h)
        # 1. a <label> at or just above the text
        for a in [h, *up[:_LABEL_REACH]]:
            if tag[a] != "label":
                continue
            target = attr(a, "for")
            if target and target in by_id:
                t = by_id[target]
                return one_question(outer_controls(t), t)
            inside = outer_controls(a)
            if inside:
                return one_question(inside, a)
            break
        # 2. aria-labelledby naming the text, or an element around it that
        # holds no control yet
        ids: set[str] = set()
        for a in [h, *up]:
            if outer_controls(a):
                break
            if attr(a, "id"):
                ids.add(attr(a, "id"))
        if ids:
            named = [j for j in controls if set(attr(j, "lb").split()) & ids]
            if named:
                return one_question(named, 0)
        # 3. the nearest element above the text that holds a control
        for a in [h, *up]:
            cs = outer_controls(a)
            if not cs:
                continue
            got, why = one_question(cs, a)
            if got and len(cs) == 1:
                own = own_label_text(got[0])
                if (
                    own
                    and not any(key in t for t in own)
                    and not any(tag[x] == "label" for x in [h, *up[: up.index(a)]] if x != a)
                ):
                    return [], "control-labelled-by-another-question"
            if got and holds_another_question(a, cs, h, key):
                return [], "container-holds-several-questions"
            return got, why
        return [], ""

    def inside_control(i: int) -> bool:
        return (
            ctl[i] >= 0
            or any(ctl[a] >= 0 or tag[a] in ("select", "option", "textarea") for a in ancestors(i))
            or tag[i] in ("select", "option", "textarea")
        )

    def words_text(i: int) -> str:
        """i's own text in document order, each element's apart (a space
        between), controls and their options left out: its words as read."""
        buf: list[str] = []
        for part in nodes[i].get("parts") or []:
            if isinstance(part, str):
                buf.append(part)
                continue
            child = part if isinstance(part, int) and not isinstance(part, bool) else -1
            if i < child < n and par[child] == i and not inside_control(child):
                buf.append(words_text(child))
        return " ".join(buf)

    def lines_up(h: int, key: str, whole_key: bool) -> bool:
        """Does `key` stand in h's text as whole words: no letter or digit
        just before it, nor just after it when it is the whole label ("location"
        inside "relocation" does not)? Text that cannot be lined up does not."""
        raw = unicodedata.normalize("NFKC", words_text(h)).replace("\xa0", " ")
        squashed: list[str] = []
        at: list[int] = []
        for idx, ch in enumerate(raw):
            for c in _squash(ch):
                squashed.append(c)
                at.append(idx)
        text = "".join(squashed)
        pos = text.find(key)
        while pos >= 0:
            first, last = at[pos], at[pos + len(key) - 1]
            before = raw[first - 1] if first > 0 else ""
            after = raw[last + 1] if last + 1 < len(raw) else ""
            if not _WORD_CHAR.match(before) and not (whole_key and _WORD_CHAR.match(after)):
                return True
            pos = text.find(key, pos + 1)
        return False

    def letters(text: str) -> int:
        return len(_LETTER.findall(_MARKER_TEXT.sub("", text)))

    whole = _squash(label)
    for key in dict.fromkeys([whole, whole[:40]]):
        if len(key) < 2:
            continue
        holders = [
            i
            for i in range(n)
            if key in full[i] and not any(key in full[k] for k in kids[i]) and not inside_control(i)
        ]
        exact = [i for i in holders if full[i] == key]
        # No element's text IS the label: one that only holds it may be
        # another question's ("Location" inside "Are you open to
        # relocation?", "Country" inside "Country of citizenship"; review
        # 2026-10-20). It is the label's own only when the label stands in
        # it as whole words and it has no more letters than the label
        # (required/optional markers and its controls' text aside);
        # otherwise nothing is found and the field goes to the human.
        holders = exact or [
            h
            for h in holders
            if lines_up(h, key, key == whole) and letters(_squash(words_text(h))) <= letters(whole)
        ]
        if not holders:
            continue
        found: list[int] = []
        for h in holders:
            got, why = resolve(h, key)
            if why:
                return None, why
            found += [ctl[j] for j in got if ctl[j] not in found]
        if len(found) > 1:
            return None, "label-matches-several-controls"
        return (found[0], "") if found else (None, "")
    return None, ""


def _control_in_container(scope: Frame, label: str) -> Locator | None:
    """The control the label belongs to in the page (question_control over
    _FORM_TREE_JS), checked to be the same element when it is used. A label
    leading to several questions' controls raises ControlAmbiguous; one the
    page will not let us read, or whose control is not found, finds nothing:
    the field goes to the human instead of into the wrong box."""
    if not label.strip():
        return None
    ctls = scope.locator(_FILLABLE)
    try:
        tree = ctls.evaluate_all(_FORM_TREE_JS)
    except Exception:  # noqa: BLE001 — a page we cannot read finds nothing; never a guess
        return None
    if not isinstance(tree, list):
        return None
    index, why = question_control(tree, label)
    if why:
        log("stage.control_ambiguous", label=label[:120], reason=why)
        raise ControlAmbiguous(why)
    if index is None:
        return None
    want = next(
        (
            [str(nd.get(k) or "") for k in ("tag", "id", "name", "type")]
            for nd in tree
            if isinstance(nd, dict) and nd.get("ctl") == index
        ),
        None,
    )
    ctl = ctls.nth(index)
    try:
        got = ctl.evaluate(_CONTROL_PRINT_JS)
    except Exception:  # noqa: BLE001 — gone or changed: nothing found
        return None
    return ctl if want is not None and got == want else None


# Of the containers holding a label's text, the indices of the innermost ones
# that hold a control matching `need` (no other such container inside them).
# Every ancestor of a field matches the text too, and document order puts the
# outermost first: a whole-form wrapper whose class says "field" handed back
# its first input, another question's (2026-10-12). Used by the
# Yes/No pair, which then checks the pair in-page (_YES_NO_PAIR_JS).
_INNERMOST_JS = """
(els, need) => {
  const has = els.map((e) => !!e.querySelector(need));
  return els
    .map((e, i) => (has[i] && !els.some((o, j) => has[j] && o !== e && e.contains(o)) ? i : -1))
    .filter((i) => i >= 0);
}
"""


def _innermost(cont: Locator, need: str) -> Locator | None:
    """The ONE innermost element of `cont` holding a `need` control; None
    when there is none, or several (two questions share the label text)."""
    if cont.count() < 1:
        return None
    inner = cont.evaluate_all(_INNERMOST_JS, need)
    if not isinstance(inner, list) or len(inner) != 1:
        return None
    return cont.nth(int(inner[0]))


# ---- runtime half of hard rule 1: fill clicks never land on a send control ----
#
# The static scan (tests/test_form_specs.py) proves no selector in this module
# names a submit/apply/send control. It cannot prove what a selector built
# from a captured label resolves to at run time: _find_control's container and
# role fallbacks (WP0 review flagged them as a theoretical path) could in
# principle land on the form's own button. So every click and check below is
# preceded by an in-page probe of the element actually about to be clicked,
# and refused when it could send the form. tests/test_click_guard.py checks
# the AST: a click in autofill/ without refuse_send_control(<same element>)
# earlier in the same function fails the build.


class ClickRefused(RuntimeError):
    """A fill click aimed at a control that could send the application."""


# Returns '' when the element is safe to click, else why not. A <button> with
# no type attribute inside a form IS a send control (its default type sends).
# The name check reads the element's own text only (aria-label, title, value,
# visible text), not its question label: "Select all that apply" labels many
# ordinary dropdowns. Allowlisted verbatim in tests/test_form_specs.py.
_CLICK_GUARD_JS = """
(el) => {
  const typeOf = (n) => {
    if (!n || !n.tagName) return '';
    const attr = (n.getAttribute('type') || '').toLowerCase();
    if (n.tagName.toLowerCase() === 'button') return attr || (n.form ? 'submit' : 'button');
    return attr;
  };
  const own = typeOf(el);
  if (own === 'submit' || own === 'image') return 'submit-type';
  const host = el.closest('button, input[type="submit"], input[type="image"]');
  if (host && host !== el) {
    const t = typeOf(host);
    if (t === 'submit' || t === 'image') return 'inside-submit-control';
  }
  const own_text = [el.getAttribute('aria-label'), el.getAttribute('title'),
                    el.getAttribute('value'), el.innerText || ''].join(' ');
  if (/submit|apply|send/i.test(own_text)) return 'submit-like-name';
  return '';
}
"""


def refuse_send_control(ctl: Locator, what: str) -> None:
    """Raise ClickRefused unless `ctl` is safe for a fill click: not a send-type
    control, not inside one, and no submit/apply/send in its own name. An
    element that cannot be inspected is refused too (fail closed). Logged as
    stage.click_guard_refused; the field goes to the human as fill-failed."""
    try:
        why = str(ctl.evaluate(_CLICK_GUARD_JS) or "")
    except Exception as e:  # noqa: BLE001 — an element we cannot inspect is never clicked
        why = f"uninspectable: {str(e)[:80]}"
    if why:
        log("stage.click_guard_refused", control=what, reason=why)
        raise ClickRefused(f"fill click refused on {what}: {why}")


@dataclass
class FillNote:
    """What one fill did, for the read-back and the CAPTCHA re-check: how the
    answer went in ('text', 'select', 'radio', 'checkbox', 'listed' for a
    custom dropdown, 'location', 'button' for a yes/no pair; '' when nothing
    went in), the element that now holds it, the text expected there, and
    whether a click or check was made on the page, which a CAPTCHA can
    answer (Lever shows hCaptcha after scripted checkbox and radio clicks).

    `clicked` is set BEFORE the click (clicking()), never after it returns:
    Playwright's check() clicks first and then raises "Clicking the checkbox
    did not change its state" when a challenge swallowed the click, and a
    flag set after the call would skip the CAPTCHA check for exactly that
    click (review 2026-10-12)."""

    how: str = ""
    target: Locator | None = None
    expect: str = ""
    clicked: bool = False
    click_kind: str = ""

    def took(self, how: str, target: Locator, expect: str) -> None:
        self.how, self.target, self.expect = how, target, expect

    def clicking(self, kind: str) -> None:
        """Called just before a click, check or in-page label click."""
        self.clicked = True
        self.click_kind = kind

    def kind(self) -> str:
        """What the fill did, for a provoked CAPTCHA's report."""
        return self.how or self.click_kind or "click"


# ---- choosing among options (autofill/options.py decides, this module acts) ----

MAX_OPTIONS = 500  # a country list is ~250; a cap of 30 hides 21 of 51 states
LOCATION_WAIT_MS = 4000  # how long a location autocomplete may take to suggest


@dataclass(frozen=True)
class Choice:
    """How to choose among a field's options: the bank entry's
    select_preference, whether the entry is the user's legal_acknowledgement,
    and whether the field is a location autocomplete (Ashby Location)."""

    prefer: tuple[str, ...] = ()
    acknowledgement: bool = False
    location: bool = False


_NO_CHOICE = Choice()
_LOCATION_ENTRIES = frozenset({("identity", "address"), ("screening", "working_location")})


def choice_for(match: Mapping[str, Any], bank: dict[str, Any], ftype: str) -> Choice:
    kind, entry = str(match.get("kind") or ""), str(match.get("entry_id") or "")
    prefer: tuple[str, ...] = ()
    if kind == "screening":
        stored = screening_entry(bank, entry) or {}
        prefer = tuple(str(p) for p in (stored.get("select_preference") or []))
    return Choice(
        prefer=prefer,
        acknowledgement=(kind, entry) == ("screening", "legal_acknowledgement"),
        location=ftype.strip().lower() == "location" or (kind, entry) in _LOCATION_ENTRIES,
    )


def _choose(
    value: str, texts: list[str], choice: Choice, control: str, quiet: bool = False
) -> int | None:
    """choose_option, logged when no option safely carries the answer (the
    field is then left blank and flagged as fill-failed). quiet: a retry
    follows, so a miss here is not yet the field's outcome."""
    i = choose_option(value, texts, prefer=choice.prefer, acknowledgement=choice.acknowledgement)
    if i is None and not quiet:
        log(
            "stage.no_safe_option",
            control=control,
            want=value[:60],
            options=[t[:40] for t in texts[:15]],
            option_count=len(texts),
        )
    return i


# the text a radio or checkbox is known by: its <label>, aria-label, its own
# text (role=checkbox divs), a wrapping label, aria-labelledby, its parent's
# text, and only then its value (often an id, or "on")
_OPTION_LABEL_JS = """
(el) => {
  const byId = (id) => {
    const t = id ? document.getElementById(id) : null;
    return t ? t.innerText : '';
  };
  return (el.labels && el.labels[0] ? el.labels[0].innerText : '')
    || el.getAttribute('aria-label')
    || (el.tagName.toLowerCase() === 'input' ? '' : (el.innerText || ''))
    || (el.closest('label') ? el.closest('label').innerText : '')
    || byId(el.getAttribute('aria-labelledby'))
    || (el.parentElement ? el.parentElement.innerText : '')
    || el.value || '';
}
"""
# status rows a listbox shows while it searches; not options
_LIST_STATUS = re.compile(
    r"^(?:loading|searching|no (?:results|options|matches)|type to search|start typing)", re.I
)


def _listed_options(scope: Frame, ctl: Locator) -> Locator:
    """The open listbox's options: the one this control names in
    aria-controls/aria-owns when it does, else every option in the frame."""
    lid = ctl.get_attribute("aria-controls") or ctl.get_attribute("aria-owns") or ""
    if lid and not any(c in lid for c in '"\\\n '):
        box = scope.locator(f'[id="{lid}"]')
        if box.count() == 1:
            return box.get_by_role("option")
    return scope.get_by_role("option")


def _option_texts(opts: Locator) -> list[str]:
    return [str(t).strip() for t in opts.all_inner_texts()[:MAX_OPTIONS]]


def _pick_listed_option(
    scope: Frame,
    ctl: Locator,
    value: str,
    choice: Choice,
    quiet: bool = False,
    note: FillNote | None = None,
) -> bool:
    """Choose among the open dropdown's options and click the one chosen,
    after checking it still shows the text that was matched."""
    opts_loc = _listed_options(scope, ctl)
    texts = _option_texts(opts_loc)
    i = _choose(value, texts, choice, "dropdown", quiet)
    if i is None:
        return False
    option = opts_loc.nth(i)
    if option.inner_text().strip() != texts[i]:
        return False  # the list changed under us: never click a different option
    refuse_send_control(option, "option")
    if note is not None:
        note.clicking("option")
    option.click()
    if note is not None:
        note.took("listed", ctl, texts[i])
    return True


def _autocomplete(ctl: Locator, role: str) -> bool:
    """A control that offers suggestions as you type."""
    if role == "combobox" or ctl.get_attribute("aria-autocomplete"):
        return True
    return (ctl.get_attribute("aria-haspopup") or "").lower() in ("listbox", "true")


def _await_suggestions(scope: Frame, ctl: Locator) -> tuple[Locator, list[str]]:
    """The autocomplete's suggestions once they arrive (status rows such as
    "Loading..." do not count), or an empty list after LOCATION_WAIT_MS."""
    opts_loc = _listed_options(scope, ctl)
    for _ in range(max(1, LOCATION_WAIT_MS // 400)):
        scope.wait_for_timeout(400)
        opts_loc = _listed_options(scope, ctl)
        texts = _option_texts(opts_loc)
        if any(t and not _LIST_STATUS.match(t) for t in texts):
            return opts_loc, texts
    return opts_loc, []


def _fill_location(
    page: Page,
    scope: Frame,
    ctl: Locator,
    value: str,
    place: tuple[str | None, str],
    note: FillNote | None = None,
) -> bool:
    """A location autocomplete (Ashby's Location field).
    Type the bank's place ("Bristol, PA", then "Bristol, Pennsylvania"), then
    the state name, and take the suggestion for the same city in the same
    state, or, for the state query, the state itself. A suggestion naming
    another state, or another city, is never taken: no match leaves the box
    empty and the field to the human."""
    city, state = place
    refuse_send_control(ctl, "location")
    if note is not None:
        note.clicking("location")
    ctl.click()
    for query, state_only_ok in location_queries(city, state, value):
        ctl.fill("")
        ctl.press_sequentially(query, delay=40)
        opts_loc, texts = _await_suggestions(scope, ctl)
        i = choose_location(city, state, texts, state_only_ok=state_only_ok)
        if i is None:
            continue
        option = opts_loc.nth(i)
        if option.inner_text().strip() != texts[i]:
            continue  # suggestions changed under us
        refuse_send_control(option, "location option")
        option.click()
        if note is not None:
            note.took("location", ctl, texts[i])
        return True
    log("stage.no_safe_option", control="location", want=value[:60])
    ctl.fill("")
    page.keyboard.press("Escape")
    return False


def _checkbox_group(scope: Frame, ctl: Locator) -> Locator | None:
    """The other boxes of a "mark all that apply" group: same name, or the
    same enclosing fieldset / role=group."""
    name = ctl.get_attribute("name") or ""
    if name and not any(c in name for c in '"\\\n'):
        group = scope.locator(f'input[type="checkbox"][name="{name}"]')
        if group.count() >= 2:
            return group
    box = ctl.locator("xpath=ancestor::*[self::fieldset or @role='group'][1]")
    if box.count() == 1:
        group = box.get_by_role("checkbox")
        if group.count() >= 2:
            return group
    return None


def _check_one_box(
    scope: Frame, boxes: Locator, value: str, choice: Choice, note: FillNote | None = None
) -> bool:
    """Tick the ONE box that carries the answer (an EEO decline in a "mark
    all that apply" group) and nothing else. Boxes whose labels cannot be
    told apart are never ticked."""
    texts = [
        str(boxes.nth(i).evaluate(_OPTION_LABEL_JS) or "").strip() for i in range(boxes.count())
    ]
    if len(set(texts)) != len(texts):
        log("stage.no_safe_option", control="checkbox-group", reason="labels-not-distinct")
        return False
    i = _choose(value, texts, choice, "checkbox-group")
    if i is None:
        return False
    box = boxes.nth(i)
    refuse_send_control(box, "checkbox")
    if not box.is_checked():
        if note is not None:
            note.clicking("checkbox")
        # styled checkboxes hide the input behind a span — click its label in-DOM
        box.evaluate("el => { const l = el.closest('label'); (l || el).click(); }")
        scope.wait_for_timeout(200)
        if not box.is_checked():
            box.check(force=True)
    if note is not None:
        note.took("checkbox", box, texts[i])
    return True


def _fill_control(
    page: Page,
    scope: Frame,
    ctl: Locator,
    value: str,
    ftype: str,
    options: list[str],
    choice: Choice | None = None,
    note: FillNote | None = None,
) -> bool:
    """Put `value` into the control, by its kind. `note` (FillNote) records
    what went in where, for the read-back, and any click made."""
    choice = choice or _NO_CHOICE
    note = FillNote() if note is None else note
    tag = ctl.evaluate("el => el.tagName.toLowerCase()")
    role = (ctl.get_attribute("role") or "").lower()
    itype = (ctl.get_attribute("type") or "").lower() if tag == "input" else ""
    if tag == "select":
        # the live <option>s first: a captured list is cut at 20 (a long state list)
        live = [str(o).strip() for o in ctl.locator("option").all_inner_texts()]
        opts = live or [str(o).strip() for o in options]
        i = _choose(value, opts, choice, "select")
        if i is None:
            return False
        ctl.select_option(label=opts[i])
        note.took("select", ctl, opts[i])
        return True
    if choice.location and tag == "input" and _autocomplete(ctl, role):
        place = parse_location(value)
        if place is not None:
            return _fill_location(page, scope, ctl, value, place, note)
    if role in ("radiogroup", "group") or tag == "fieldset":
        radios = ctl.get_by_role("radio")
        if radios.count() == 0:
            boxes = ctl.get_by_role("checkbox")
            return boxes.count() > 0 and _check_one_box(scope, boxes, value, choice, note)
        labels = [
            str(
                radios.nth(i).get_attribute("aria-label")
                or radios.nth(i).evaluate("el => el.labels?.[0]?.innerText || ''")
            ).strip()
            for i in range(radios.count())
        ]
        if len(set(labels)) != len(labels):
            return False  # radios that cannot be told apart
        i = _choose(value, labels, choice, "radio")
        if i is None:
            return False
        radio = radios.nth(i)
        refuse_send_control(radio, "radio")
        note.clicking("radio")
        radio.check()
        note.took("radio", radio, labels[i])
        return True
    if role == "combobox" or (tag not in ("input", "textarea") and role in ("button", "listbox")):
        # custom dropdown: open it and choose among ALL its options
        refuse_send_control(ctl, "dropdown")
        note.clicking("dropdown")
        ctl.click()
        scope.wait_for_timeout(400)
        filterable = tag == "input" and not choice.acknowledgement and not mentions_decline(value)
        if _pick_listed_option(scope, ctl, value, choice, quiet=filterable, note=note):
            return True
        if filterable:
            # a long or virtualized list: type to filter, then choose again
            ctl.fill(search_text(value))
            scope.wait_for_timeout(500)
            if _pick_listed_option(scope, ctl, value, choice, note=note):
                return True
            ctl.fill("")
        page.keyboard.press("Escape")
        return False
    if itype == "radio":
        # pick the radio in this name-group whose label matches the value
        name = ctl.get_attribute("name") or ""
        group = scope.locator(f'input[type="radio"][name="{name}"]') if name else ctl
        texts = []
        for i in range(group.count()):
            texts.append(
                str(
                    group.nth(i).evaluate(
                        "el => (el.labels && el.labels[0] ? el.labels[0].innerText : '')"
                        " || el.getAttribute('aria-label') || el.value"
                        " || (el.closest('label') ? el.closest('label').innerText : '')"
                        " || (el.parentElement ? el.parentElement.innerText : '')"
                    )
                ).strip()
            )
        if len(set(texts)) != len(texts):
            return False  # radios that cannot be told apart
        picked = _choose(value, texts, choice, "radio")
        if picked is not None:
            target = group.nth(picked)
            refuse_send_control(target, "radio")
            note.clicking("radio")
            # styled radios hide the input behind a span — click the label in-DOM
            target.evaluate("el => { const l = el.closest('label'); (l || el).click(); }")
            scope.wait_for_timeout(200)
            if not target.is_checked():
                target.check(force=True)
            note.took("radio", target, texts[picked])
            return True
        return False
    if itype == "checkbox" or role == "checkbox":
        if value.lower().strip() in ("yes", "true", "1"):
            # one box (a consent or acknowledgement): ticked for yes
            refuse_send_control(ctl, "checkbox")
            note.clicking("checkbox")
            ctl.check()
            note.took("checkbox", ctl, "checked")
            return True
        # any other answer names one box of a "mark all that apply" group
        grouped = _checkbox_group(scope, ctl)
        return grouped is not None and _check_one_box(scope, grouped, value, choice, note)
    if itype == "file":
        return False  # files handled separately with real paths
    if choice_shaped(ftype, options):
        # a yes/no or pick-one answer never goes into a box the user types
        # in: that box is another question's, or the wrong widget (review
        # 2026-10-15: "Yes" for a Yes/No-button question went into the
        # LinkedIn box beside it, and the read-back confirmed it). A boolean
        # goes on to the Yes/No pair; anything else to the human.
        log("stage.choice_not_typed", ftype=ftype[:40], tag=tag, type=itype, options=len(options))
        return False
    ctl.fill(value)
    note.took("text", ctl, value)
    return True


# a field type that is answered by choosing, never by typing: Greenhouse
# multi_value_*_select, Ashby Boolean / ValueSelect / MultiValueSelect, a
# DOM-scraped select, radio or checkbox
_CHOICE_FTYPE = re.compile(r"bool|select|radio|checkbox|choice|dropdown|yes.?no")


def choice_shaped(ftype: str, options: Sequence[str]) -> bool:
    """The field is answered by choosing (its type says so, or it lists
    options), so its answer is never typed into a text box."""
    return bool(_CHOICE_FTYPE.search(ftype.lower())) or any(str(o).strip() for o in options)


# a whole yes/no word at the start of the answer: "No, I do not" is No;
# "None of the above", "Not sure" and "10 years" are neither (they used to be
# read as No, No and Yes by a plain prefix test)
_BOOL_YES = re.compile(r"^(?:yes|true)\b|^1$")
_BOOL_NO = re.compile(r"^(?:no|false)\b(?!['’])|^0$")


_YES_NO_CONTROLS = 'button, [role=button], [role=radio], input[type="radio"]'
# Run on the ONE control named the wanted answer: '' when it and the ONE
# control named the other answer are a single question's pair, else why not.
# Up from the control to the nearest element also holding the other answer
# (the pair), then up to the nearest element holding the label text (the
# question). The question must hold no other Yes/No, nothing between the
# pair and the label may show another question's words, and the question
# may hold no other shown control. A whole-form wrapper whose class says
# "field" is otherwise the innermost match, and its one "No" may belong to
# another question (review 2026-10-12). Read-only.
_YES_NO_PAIR_JS = """
(el, [short, want, other]) => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const pairSel = 'button, [role=button], [role=radio], input[type="radio"]';
  const fieldSel = 'input:not([type=hidden]), select, textarea, [role=combobox], '
    + '[role=checkbox], [role=radio], [role=textbox], [role=listbox]';
  const hidden = (e) => !e.getClientRects().length
    || getComputedStyle(e).visibility === 'hidden' || !!e.closest('[aria-hidden="true"]');
  const shown = (e) => {
    const r = e.getBoundingClientRect();
    if (hidden(e) || r.width <= 1 || r.height <= 1) return false;
    for (let a = e; a; a = a.parentElement) {
      if (getComputedStyle(a).opacity === '0') return false;
    }
    return true;
  };
  const nameOf = (e) => norm(e.getAttribute('aria-label')
    || (e.labels && e.labels.length ? e.labels[0].innerText : '')
    || e.innerText || e.textContent);
  const named = (root, n) => Array.from(root.querySelectorAll(pairSel))
    .filter((e) => !hidden(e) && nameOf(e) === n);
  if (nameOf(el) !== want) return 'name-differs';
  let pair = el.parentElement;
  while (pair && !named(pair, other).length) pair = pair.parentElement;
  if (!pair) return 'no-' + other + '-beside-it';
  const label = norm(short);
  let top = null;
  let question = pair;
  while (question && !norm(question.textContent).includes(label)) {
    top = question;
    question = question.parentElement;
  }
  if (!question) return 'label-not-found';
  const mine = named(question, want);
  const theirs = named(question, other);
  if (mine.length !== 1 || theirs.length !== 1) return 'several-yes-no-pairs';
  const own = (n) => [mine[0], theirs[0]].some(
    (c) => c.contains(n) || Array.from(c.labels || []).some((l) => l.contains(n)));
  if (top) {
    const walk = document.createTreeWalker(top, NodeFilter.SHOW_TEXT);
    for (let n = walk.nextNode(); n; n = walk.nextNode()) {
      if (!/[\\p{L}\\p{N}]/u.test(n.nodeValue || '') || own(n)) continue;
      if (n.parentElement && shown(n.parentElement)) return 'another-question-beside-it';
    }
  }
  const strays = Array.from(question.querySelectorAll(fieldSel))
    .filter((e) => !pair.contains(e) && shown(e));
  return strays.length ? 'other-controls-beside-it' : '';
}
"""


def _click_boolean_pair(scope: Frame, label: str, value: str, note: FillNote | None = None) -> bool:
    """Ashby renders Boolean questions as Yes/No button pairs inside a labeled
    container. Click the one matching the value, in the innermost container
    that holds the label and a button, and only when that container holds
    exactly one control named the answer and it belongs to this question's
    pair (_YES_NO_PAIR_JS): an outer wrapper holds every question's pair, and
    its one "No" may be another question's. Anything less clear clicks
    nothing (logged as stage.yes_no_unclear) and the field goes to the human."""
    vl = value.lower().strip()
    if _BOOL_YES.match(vl):
        want, other = "Yes", "No"
    elif _BOOL_NO.match(vl):
        want, other = "No", "Yes"
    else:
        return False  # never map an ambiguous answer onto a yes/no control
    short = label[:40].replace('"', "").replace("\xa0", " ")
    box = _innermost(
        scope.locator(f'div[class*="field"]:has-text("{short}"), fieldset:has-text("{short}")'),
        _YES_NO_CONTROLS,
    )
    if box is None:
        return False
    found = 0
    for role in ("button", "radio"):
        mine = box.get_by_role(role, name=want, exact=True)  # type: ignore[arg-type,unused-ignore]
        found = mine.count()
        if found:
            break
    if found != 1:
        if found:
            log("stage.yes_no_unclear", label=label[:80], want=want, reason=f"{found}-{want}")
        return False
    target = mine.first
    why = str(target.evaluate(_YES_NO_PAIR_JS, [short, want.lower(), other.lower()]) or "")
    if why:
        log("stage.yes_no_unclear", label=label[:80], want=want, reason=why)
        return False
    refuse_send_control(target, "yes-no button")
    if note is not None:
        note.clicking("button")
    target.click()
    if note is not None:
        note.took("button", target, want)
    return True


def scan_abort_html(html: str) -> str | None:
    if _CAPTCHA.search(html):
        return "captcha-or-turnstile-detected"
    if _HARD_STOP_FIELD.search(html):
        return "requests-government-id-or-payment"
    if _ASSESSMENT.search(html):
        return "embedded-assessment-detected"
    return None


# ---- CAPTCHA: detect a shown challenge, abort, hand to the human (hard rule 2) ----

# Elements that can host a CAPTCHA widget; the name is what gets logged.
CAPTCHA_WIDGETS: tuple[tuple[str, str], ...] = (
    ("recaptcha-iframe", 'iframe[src*="recaptcha"]'),
    ("hcaptcha-iframe", 'iframe[src*="hcaptcha"]'),
    ("turnstile-iframe", 'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]'),
    ("recaptcha-widget", ".g-recaptcha"),
    ("hcaptcha-widget", ".h-captcha"),
    ("turnstile-widget", ".cf-turnstile"),
)
_CHALLENGE_MIN_PX = 20
# what is_captcha_challenge needs to know about a widget element, read in-page
_CAPTCHA_PROBE_JS = """
(el) => ({
  src: el.getAttribute('src') || '',
  size: el.getAttribute('data-size') || '',
  inBadge: !!el.closest('.grecaptcha-badge'),
  hasBadge: !!el.querySelector('.grecaptcha-badge'),
})
"""


def is_captcha_challenge(probe: Mapping[str, Any], box: Mapping[str, Any] | None) -> bool:
    """True only for a challenge a human must solve before the form can go.

    Not a challenge: the invisible-reCAPTCHA badge (.grecaptcha-badge, about
    256x60, fixed bottom-right; it once aborted every Greenhouse run),
    its size=invisible anchor iframe, hCaptcha's invisible checkbox frame, a
    data-size="invisible" widget, and challenge frames parked offscreen until a
    challenge fires. Those act only when the human clicks, and the human
    handles whatever they show. A visible reCAPTCHA checkbox or challenge frame,
    an hCaptcha frame or a Turnstile widget is a challenge."""
    if box is None:
        return False
    x, y = float(box["x"]), float(box["y"])
    w, h = float(box["width"]), float(box["height"])
    if h <= _CHALLENGE_MIN_PX or w <= _CHALLENGE_MIN_PX:
        return False
    if x + w <= 0 or y + h <= 0:
        return False  # parked offscreen (reCAPTCHA keeps its bframe at top:-10000px)
    if probe.get("inBadge") or probe.get("hasBadge"):
        return False
    src = str(probe.get("src") or "").lower()
    if "size=invisible" in src or "checkbox-invisible" in src:
        return False
    return str(probe.get("size") or "").lower() != "invisible"


def captcha_challenge(page: Page) -> str | None:
    """The CAPTCHA challenge actually shown, in any frame, described for the
    log ('<widget> <selector> src=…') — or None. Only reads; never interacts."""
    for frame in page.frames:
        for name, sel in CAPTCHA_WIDGETS:
            try:
                widgets = frame.locator(sel)
                count = min(widgets.count(), 5)
            except Exception:  # noqa: BLE001 — a detached frame holds no widget
                continue
            for i in range(count):
                w = widgets.nth(i)
                try:
                    if not w.is_visible():
                        continue
                    box = w.bounding_box()
                    probe = w.evaluate(_CAPTCHA_PROBE_JS)
                except Exception:  # noqa: BLE001 — an unqueryable widget is not a visible one
                    continue
                if is_captcha_challenge(dict(probe or {}), box):
                    src = str(dict(probe or {}).get("src") or "")[:120]
                    size = f"{box['width']:.0f}x{box['height']:.0f}" if box else "?"
                    return f"{name} [{sel}] {size} src={src or '-'} frame={frame.url[:80]}"
    return None


def captcha_visible(page: Page) -> bool:
    """A CAPTCHA blocks staging only when a challenge widget is actually shown."""
    return captcha_challenge(page) is not None


def detect_abort(page: Page, field_labels: list[str], job_id: int | None = None) -> str | None:
    challenge = captcha_challenge(page)
    if challenge:
        log("stage.captcha_detected", job_id=job_id, widget=challenge)
        return "captcha-or-turnstile-visible"
    # hard stops scan FORM LABELS, not the whole page — the JD text above the
    # form legitimately contains words like "assessment" or "payment"
    label_blob = " | ".join(field_labels)
    if _HARD_STOP_FIELD.search(label_blob):
        return "requests-government-id-or-payment"
    if _ASSESSMENT.search(label_blob):
        return "embedded-assessment-detected"
    return None


# ---- is there a form to fill? ----

_POSTING_GONE = re.compile(
    r"page not found|job not found|posting not found|couldn.t find|could not find"
    r"|no longer (?:open|available|accepting|active|posted)"
    r"|(?:job|position|posting|role|opening) (?:has been |is )?(?:closed|filled|removed|expired)"
    r"|does not exist|\b404\b",
    re.I,
)


def _form_selector(fields: list[dict[str, Any]]) -> str | None:
    """A CSS selector matching any captured field's control by its ATS id."""
    parts: list[str] = []
    for f in fields:
        ident = str(f.get("identity") or "")
        if not ident or len(ident) > 80 or any(c in ident for c in '"\\\n'):
            continue
        parts.append(f'[name="{ident}"], [id="{ident}"]')
        if len(parts) >= 12:
            break
    return ", ".join(parts) or None


def _count(loc: Locator) -> int:
    try:
        return loc.count()
    except Exception:  # noqa: BLE001 — a frame that navigated away holds nothing
        return 0


def _frame_by_labels(page: Page, labels: list[str]) -> Frame | None:
    need = min(2, len(labels))
    if not need:
        return None
    for fr in page.frames:
        hits = sum(1 for lab in labels if _count(fr.get_by_label(lab, exact=False)) > 0)
        if hits >= need:
            return fr
    return None


def locate_form(
    page: Page, fields: list[dict[str, Any]], timeout_ms: int = FORM_WAIT_MS
) -> Frame | None:
    """The frame holding the application form: the page itself, or an iframe
    (e.g. an employer careers page that embeds its Greenhouse board after a
    redirect). Waits up to timeout_ms for any captured field to render, then
    tries the captured labels. None: no form to fill."""
    sel = _form_selector(fields)
    labels = [str(f.get("label") or "") for f in fields]
    labels = [lab for lab in labels if 0 < len(lab) <= 60][:6]
    steps = max(1, timeout_ms // 500)
    for step in range(steps + 1):
        if sel:
            for fr in page.frames:
                if _count(fr.locator(sel)) > 0:
                    return fr
        if not sel or step == steps:
            hit = _frame_by_labels(page, labels)
            if hit is not None:
                return hit
        if step < steps:
            page.wait_for_timeout(500)
    return None


def posting_gone(page: Page, http_status: int | None) -> str | None:
    """Why the page has no form: an HTTP error, or a closed/not-found notice."""
    if http_status is not None and http_status >= 400:
        return f"http-{http_status}"
    for fr in page.frames:
        try:
            text = fr.locator("body").inner_text(timeout=2000)
        except Exception:  # noqa: BLE001 — a frame without a readable body says nothing
            continue
        m = _POSTING_GONE.search(text[:20000])
        if m:
            return f"page-says:{m.group(0)[:40]}"
    return None


# ---- uploads: named copies, the right file input, then the parser ----------------

_UPLOAD_KINDS = {"resume": "Resume", "cover_letter": "Cover_Letter"}


def _name_part(text: Any) -> str:
    """One name, safe in a file name: accents dropped (José → Jose), any run
    of characters other than ASCII letters, digits and hyphens made one '_'."""
    ascii_text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9-]+", "_", ascii_text).strip("_-")


def upload_name(bank: Mapping[str, Any], entry: str, suffix: str) -> str | None:
    """First_Last_Resume.pdf / First_Last_Cover_Letter.pdf from the answer
    bank's identity first_name and last_name; None when either is missing."""
    ident = bank.get("identity") or {}
    first, last = _name_part(ident.get("first_name")), _name_part(ident.get("last_name"))
    if not first or not last or entry not in _UPLOAD_KINDS:
        return None
    ext = suffix.lower() if re.fullmatch(r"\.[A-Za-z0-9]{1,5}", suffix or "") else ""
    return f"{first}_{last}_{_UPLOAD_KINDS[entry]}{ext}"


def upload_copies(
    job_id: int, bank: Mapping[str, Any], sources: Mapping[str, Any]
) -> dict[str, Path]:
    """entry ('resume', 'cover_letter') -> the file to upload. Each existing
    source is copied to UPLOAD_DIR/job-<id>/ under its recruiter name, so the
    reviewer sees First_Last_Resume.pdf, not resume.pdf; the original is only
    read. With no usable name in the bank, or a copy that fails, the original
    itself is uploaded (logged). A missing source is left out."""
    out: dict[str, Path] = {}
    for entry, src in sources.items():
        if not src or not Path(str(src)).is_file():
            continue
        original = Path(str(src))
        name = upload_name(bank, entry, original.suffix)
        if name is None:
            log("stage.upload_name_unavailable", job_id=job_id, entry=entry)
            out[entry] = original
            continue
        target = UPLOAD_DIR / f"job-{job_id}" / name
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, target)
        except OSError as e:
            log("stage.upload_copy_failed", job_id=job_id, entry=entry, error=str(e)[:120])
            out[entry] = original
            continue
        out[entry] = target
    return out


_FILE_INPUT = 'input[type="file"]'
MAX_FILE_INPUTS = 12
# what a file input is known by, most specific first: its own <label>s,
# aria-label, aria-labelledby, name and id, then the text of up to 4
# enclosing elements that hold no other file input
_FILE_INPUT_TEXTS_JS = """
(el) => {
  const byId = (id) => {
    const t = id ? document.getElementById(id) : null;
    return t ? t.innerText : '';
  };
  const out = [
    Array.from(el.labels || []).map((l) => l.innerText).join(' '),
    el.getAttribute('aria-label') || '',
    byId(el.getAttribute('aria-labelledby')),
    el.getAttribute('name') || '',
    el.id || '',
  ];
  let box = el.parentElement;
  for (let i = 0; i < 4 && box; i += 1) {
    if (box.querySelectorAll('input[type="file"]').length > 1) break;
    out.push(box.innerText || '');
    box = box.parentElement;
  }
  return out;
}
"""
_RESUME_WORD = re.compile(r"(?<![a-z])(?:resume|résumé|curriculum|cv(?![a-z]))", re.I)
_COVER_WORD = re.compile(r"(?<![a-z])cover(?:[\s_-]*letter|(?![a-z]))", re.I)


def file_input_kind(texts: Sequence[str]) -> str | None:
    """'resume', 'cover_letter' or None for one file input, from what it is
    known by (_FILE_INPUT_TEXTS_JS order): the first text that names exactly
    one of the two decides. A text naming both ("Resume or cover letter")
    decides None, and so does nothing naming either: never a guess."""
    for t in texts:
        resume, cover = bool(_RESUME_WORD.search(t)), bool(_COVER_WORD.search(t))
        if resume and cover:
            return None
        if resume:
            return "resume"
        if cover:
            return "cover_letter"
    return None


def find_file_input(scope: Frame, identity: str, entry: str) -> tuple[Locator | None, str]:
    """The <input type="file"> an attach field uploads into, and how it was
    found; or None and why not. Only file inputs count: a hidden input or a
    wrapper sharing the field's name never does.

    1. the ATS's field id on a file input (Greenhouse's resume, Lever's
       resume, Ashby's _systemfield_resume);
    2. else the one file input on the form known as a resume (or a cover
       letter): an employer-hosted copy of the form names its own inputs and
       labels them its own way (2026-10-12: "Resume", not the API's
       "Resume/CV" or id "resume"). Two inputs claiming the same kind
       find nothing."""
    if identity and not any(c in identity for c in '"\\\n'):
        for attr in ("name", "id"):
            loc = scope.locator(f'{_FILE_INPUT}[{attr}="{identity}"]')
            if _count(loc) == 1:
                return loc, f"file-input-{attr}"
    inputs = scope.locator(_FILE_INPUT)
    n = min(_count(inputs), MAX_FILE_INPUTS)
    hits: list[int] = []
    for i in range(n):
        try:
            raw = inputs.nth(i).evaluate(_FILE_INPUT_TEXTS_JS)
        except Exception:  # noqa: BLE001 — an input we cannot read is not claimed
            continue
        texts = [str(t) for t in raw] if isinstance(raw, list) else [str(raw or "")]
        if file_input_kind(texts) == entry:
            hits.append(i)
    if len(hits) == 1:
        return inputs.nth(hits[0]), "file-input-label"
    if hits:
        return None, f"{len(hits)}-file-inputs-claim-{entry}"
    return None, (f"no-file-input-for-{entry}" if n else "no-file-input")


def _text_alternative(f: Mapping[str, Any]) -> bool:
    """Greenhouse's "or paste it" textarea beside a file upload (resume_text,
    cover_letter_text): satisfied by the upload, never typed into."""
    return str(f.get("type") or "").lower() == "textarea" or str(f.get("identity") or "").endswith(
        "_text"
    )


def _attach_files(
    form: Frame,
    job: Mapping[str, Any],
    bank: Mapping[str, Any],
    plan: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
    filled: list[dict[str, str]],
    unfilled: list[dict[str, str]],
    job_id: int,
) -> set[str]:
    """Upload the resume and cover letter, as recruiter-named copies, into
    their file inputs (find_file_input). Returns the entries uploaded. Every
    failure is logged (stage.attach_failed) and recorded with its cause in
    the unfilled entry's detail: no file, no file input, two inputs claiming
    it, the upload refused. A paste-instead textarea is satisfied by its
    file's upload (stage.attach_alternative_satisfied); without one it is
    left to the human."""
    if not any(match.get("kind") == "attach" for _, match in plan):
        return set()
    copies = upload_copies(
        job_id, bank, {"resume": job["resume_path"], "cover_letter": job["cover_path"]}
    )
    attached: set[str] = set()
    alternatives: list[tuple[str, str]] = []
    for f, match in plan:
        if match.get("kind") != "attach":
            continue
        identity = str(f.get("identity", ""))
        entry = "resume" if match.get("entry_id") == "resume" else "cover_letter"
        if _text_alternative(f):
            alternatives.append((identity, entry))
            continue
        path = copies.get(entry)
        if path is None:
            why = "no-file"
        else:
            ctl, why = find_file_input(form, identity, entry)
            if ctl is not None:
                try:
                    ctl.set_input_files(str(path))
                except Exception as e:  # noqa: BLE001 — recorded below, never silent
                    why = f"upload-failed:{str(e)[:100]}"
                else:
                    filled.append(
                        {"identity": identity, "value": path.name, "source": "answer_bank"}
                    )
                    attached.add(entry)
                    log(
                        "stage.attached",
                        job_id=job_id,
                        identity=identity,
                        entry=entry,
                        found_by=why,
                        file=path.name,
                    )
                    continue
        log("stage.attach_failed", job_id=job_id, identity=identity, entry=entry, why=why)
        unfilled.append({"identity": identity, "reason": "attachment", "detail": why})
    for identity, entry in alternatives:
        if entry in attached:
            log("stage.attach_alternative_satisfied", job_id=job_id, identity=identity, entry=entry)
            continue
        unfilled.append(
            {"identity": identity, "reason": "attachment", "detail": f"{entry}-not-uploaded"}
        )
    return attached


# every fillable value on the form, to see when a resume parser stops changing it
_FORM_VALUES_JS = """
() => Array.from(document.querySelectorAll('input, textarea, select'))
  .filter((e) => e.type !== 'file' && e.type !== 'hidden')
  .map((e) => ((e.type === 'checkbox' || e.type === 'radio') ? String(e.checked) : (e.value || '')))
  .join('|')
"""
_PARSER_STEP_MS = 500


def await_parser(page: Page, form: Frame, job_id: int | None = None) -> dict[str, Any]:
    """After an upload, wait for the ATS's resume parser to finish rewriting
    the form (Lever and others prefill name, email, phone from the file), so
    the fill that follows is not overwritten. Finished: the form's values
    changed and then held still for PARSER_QUIET_MS, or did not change at all
    for PARSER_MIN_WAIT_MS. Never longer than PARSER_WAIT_MS; a form whose
    values cannot be read gets one step. Logged as stage.parser_wait."""

    def snapshot() -> str | None:
        try:
            v = form.evaluate(_FORM_VALUES_JS)
        except Exception:  # noqa: BLE001 — unreadable: stop waiting, fill as before
            return None
        return v if isinstance(v, str) else None

    last = snapshot()
    waited = quiet = 0
    changed = settled = False
    while waited < PARSER_WAIT_MS:
        page.wait_for_timeout(_PARSER_STEP_MS)
        waited += _PARSER_STEP_MS
        now = snapshot()
        if now is None or last is None:
            break
        if now != last:
            changed, last, quiet = True, now, 0
            continue
        quiet += _PARSER_STEP_MS
        if (changed and quiet >= PARSER_QUIET_MS) or (not changed and waited >= PARSER_MIN_WAIT_MS):
            settled = True
            break
    out: dict[str, Any] = {"waited_ms": waited, "changed": changed, "settled": settled}
    log("stage.parser_wait", job_id=job_id, **out)
    return out


# ---- read-back: is every filled value still there? --------------------------------

# a native <select>'s chosen option
_SELECTED_TEXT_JS = "el => (el.selectedIndex >= 0 ? el.options[el.selectedIndex].text : '')"
# What a custom dropdown or location box SHOWS as its value, never the text
# around it (review 2026-10-12: the old probe read the innerText of enclosing
# elements, the question's label among them, so "No" passed on a cleared
# widget because the label said "now"). The widget is the control plus the
# elements above it that hold no other control and no <label>/<legend>; read
# from it: the input's own value, the element that shows the chosen option
# (react-select's single-value and the like), a button-style control's own
# text, and a placeholder shown in place of a value. Read-only.
_SHOWN_VALUE_JS = """
(el) => {
  const fillable = 'input:not([type=hidden]), select, textarea, [role=combobox]';
  let widget = el;
  for (let i = 0; i < 6 && widget.parentElement; i += 1) {
    const up = widget.parentElement;
    const tag = up.tagName.toLowerCase();
    if (['label', 'legend', 'fieldset', 'form', 'body'].includes(tag)) break;
    if (up.querySelector('label, legend')) break;
    const others = Array.from(up.querySelectorAll(fillable))
      .filter((c) => c !== el && !el.contains(c) && !c.contains(el));
    if (others.length) break;
    widget = up;
  }
  const texts = (sel) => Array.from(widget.querySelectorAll(sel))
    .filter((n) => n !== el && !n.contains(el))
    .map((n) => n.innerText || n.textContent || '')
    .filter((t) => t.trim());
  const tag = el.tagName.toLowerCase();
  const typed = tag === 'input' || tag === 'textarea';
  return {
    value: typed ? (el.value || '') : '',
    shown: texts('[class*="single-value"], [class*="singleValue"], [class*="selected-value"], '
      + '[class*="selectedValue"], [class*="multi-value__label"], [class*="multiValue__label"]'),
    own: typed ? '' : (el.innerText || el.textContent || ''),
    placeholder: texts('[class*="placeholder"]'),
  };
}
"""
_PRESSED_JS = "el => el.getAttribute('aria-pressed') || el.getAttribute('aria-checked') || ''"
_PHONEISH = re.compile(r"^\+?[\d\s\-().]{7,}$")
_FORMATTING = re.compile(r"[\s\-().]+")


def _text_key(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip().casefold()


def _url_key(s: str) -> str | None:
    if " " in s or "." not in s:
        return None
    return re.sub(r"^https?://", "", s).removeprefix("www.").rstrip("/")


def same_text(want: str, got: str) -> bool:
    """Whether a text box still holds what was typed, allowing only the
    reformatting ATS widgets do on their own: case and spacing; phone
    punctuation, and a country code added or dropped ("+1 555 555 0100" →
    "+15555550100" or "(555) 555-0100"); a URL's scheme, www. and trailing
    slash. Another value, an emptied box or a cut-off answer differs."""
    w, g = _text_key(want), _text_key(got)
    if w == g:
        return True
    if not w or not g:
        return False
    if _PHONEISH.match(w) and _PHONEISH.match(g):
        dw, dg = re.sub(r"\D", "", w), re.sub(r"\D", "", g)
        return (
            min(len(dw), len(dg)) >= 7
            and abs(len(dw) - len(dg)) <= 3
            and (dw.endswith(dg) or dg.endswith(dw))
        )
    if _FORMATTING.sub("", w) == _FORMATTING.sub("", g):
        return True
    uw = _url_key(w)
    return uw is not None and uw == _url_key(g)


def read_back(note: FillNote) -> tuple[bool | None, str]:
    """Whether the control the fill wrote still holds the answer: True,
    False, or None when it cannot be told (a widget we cannot read, or one
    that shows nothing at all), and what it holds, for the log. Only a
    reading that shows something else is a mismatch."""
    t = note.target
    if t is None or not note.how:
        return None, ""
    try:
        if note.how == "text":
            got = str(t.input_value())
            return same_text(note.expect, got), got
        if note.how == "select":
            sel = t.evaluate(_SELECTED_TEXT_JS)
            if not isinstance(sel, str):
                return None, ""
            return _text_key(sel) == _text_key(note.expect), sel
        if note.how in ("radio", "checkbox"):
            on = bool(t.is_checked())
            return on, ("checked" if on else "not checked")
        if note.how in ("listed", "location"):
            return _read_shown_value(t.evaluate(_SHOWN_VALUE_JS), note)
        if note.how == "button":
            state = t.evaluate(_PRESSED_JS)
            if state not in ("true", "false"):
                return None, ""
            return state == "true", f"aria={state}"
    except Exception:  # noqa: BLE001 — unreadable is unverified, never a mismatch
        return None, ""
    return None, ""


def _lines(text: Any) -> list[str]:
    return [k for k in (_text_key(x) for x in str(text or "").splitlines()) if k]


def _read_shown_value(shown: Any, note: FillNote) -> tuple[bool | None, str]:
    """read_back for a custom dropdown or location box, from _SHOWN_VALUE_JS:
    True only when a value the widget shows (its input's value, its
    chosen-option element, a button-style control's own text) IS the answer,
    a whole line of it, never a substring; False when it shows another value
    or a placeholder in place of one; None (unverified) when it shows
    nothing that can be read as its value."""
    if not isinstance(shown, Mapping):
        return None, ""
    values = _lines(shown.get("value"))
    for text in shown.get("shown") or []:
        values += _lines(text)
    values += _lines(shown.get("own"))
    wants = {_text_key(note.expect)}
    if note.how == "location":  # the box may keep only the city
        wants.add(_text_key(note.expect.split(",")[0]))
    wants.discard("")
    if values:
        return any(v in wants for v in values), values[0]
    for text in shown.get("placeholder") or []:
        for line in _lines(text):
            return False, line  # a placeholder where the answer should be
    return None, ""


@dataclass
class _Filled:
    """One field the fill put a value into, and how to put it in again."""

    identity: str
    label: str
    value: str
    ftype: str
    options: list[Any]
    choice: Choice
    ctl: Locator | None  # None: filled through the yes/no pair
    note: FillNote


@dataclass
class ReadBack:
    """The read-back of one run. mismatches: fields the first reading found
    changed; fixed: those the one refill put right; overwritten: still changed
    after it (identity, wanted, found); unverified: fields no reading could
    check. provoked: (identity, label, how, widget) when a refill click
    brought up a CAPTCHA."""

    checked: int = 0
    unverified: int = 0
    mismatches: list[str] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)
    overwritten: list[dict[str, str]] = field(default_factory=list)
    provoked: tuple[str, str, str, str] | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "unverified": self.unverified,
            "mismatches": len(self.mismatches),
            "fixed_by_refill": len(self.fixed),
            "overwritten_after_fill": len(self.overwritten),
            "changed": self.mismatches,
            "overwritten": [o["identity"] for o in self.overwritten],
        }


def _refill(page: Page, form: Frame, d: _Filled, note: FillNote) -> None:
    """Put the field's value in once more, the way it went in first."""
    if d.ctl is None:
        _click_boolean_pair(form, d.label, d.value, note)
    else:
        _fill_control(page, form, d.ctl, d.value, d.ftype, d.options, d.choice, note)


def read_back_all(
    page: Page, form: Frame, done: Sequence[_Filled], job_id: int | None = None
) -> ReadBack:
    """Read back every filled field. A field showing something else is
    filled once more (a click doing so is followed by the CAPTCHA check) and
    read again after READBACK_SETTLE_MS; one still changed is 'overwritten'.
    Logged per field (stage.readback_mismatch, stage.overwritten_after_fill)
    and as a whole (stage.readback)."""
    rb = ReadBack()
    changed: list[tuple[_Filled, str]] = []
    for d in done:
        ok, got = read_back(d.note)
        if ok is None:
            rb.unverified += 1
            continue
        rb.checked += 1
        if not ok:
            changed.append((d, got))
            rb.mismatches.append(d.identity)
            log(
                "stage.readback_mismatch",
                job_id=job_id,
                identity=d.identity,
                how=d.note.how,
                wanted=d.note.expect[:80],
                found=got[:80],
            )
    for d, _ in changed:
        note = FillNote()
        raised = False
        try:
            _refill(page, form, d, note)
        except Exception as e:  # noqa: BLE001 — a refused or failed refill leaves it changed
            raised = True
            log("stage.refill_error", job_id=job_id, identity=d.identity, error=str(e)[:120])
        # a refill that raised may have clicked first (check() does)
        if note.clicked or raised:
            challenge = captcha_challenge(page)
            if challenge:
                rb.provoked = (d.identity, d.label, note.kind(), challenge)
                return rb
        if note.how:
            d.note = note
    if changed:
        page.wait_for_timeout(READBACK_SETTLE_MS)
    for d, first in changed:
        ok, got = read_back(d.note)
        if ok:
            rb.fixed.append(d.identity)
            continue
        found = (got or first)[:80]
        rb.overwritten.append({"identity": d.identity, "wanted": d.value[:80], "found": found})
        log(
            "stage.overwritten_after_fill",
            job_id=job_id,
            identity=d.identity,
            wanted=d.value[:80],
            found=found,
        )
    log("stage.readback", job_id=job_id, **rb.summary())
    return rb


def stage_job(
    conn: sqlite3.Connection,
    page: Page,
    job_id: int,
    batch_answers: dict[str, str] | None = None,
    matcher: BankMatcher | None = None,
) -> dict[str, Any]:
    """Fill one application form to the last click. Returns the run summary.

    Order (review 2026-10-12): the resume and cover letter go up
    first, as First_Last_Resume.pdf / First_Last_Cover_Letter.pdf copies,
    and the fill waits (at most PARSER_WAIT_MS) for an ATS resume parser to
    finish; then every other field is filled. A CAPTCHA check follows every
    click the filler makes (and every fill that raised, since check() clicks
    before it raises), comes once more before the read-back so no refill
    click lands on a challenge, and again at the end: a challenge it
    provoked stops the run as 'captcha-provoked-by-fill:<control>' with a
    screenshot, and nothing touches the challenge (hard rule 2). Last, every filled value
    is read back; one the page changed is filled once more, and one still
    changed leaves the filled list as 'overwritten-after-fill' and makes the
    run needs_human. The read-back is an event ('readback') keyed by the
    autofill_runs id."""
    bank = load_bank()
    job = conn.execute(
        """SELECT j.*, fs.fields, fs.drafted_answers, fs.source AS fs_source,
                  a.resume_path, a.cover_path, c.domain AS company_domain
           FROM jobs j LEFT JOIN form_specs fs ON fs.job_id = j.id
           LEFT JOIN applications a ON a.job_id = j.id
           LEFT JOIN companies c ON c.id = j.company_id
           WHERE j.id = ? ORDER BY a.id DESC LIMIT 1""",
        (job_id,),
    ).fetchone()
    if job is None:
        raise ValueError(f"no job {job_id}")
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    shot = SHOT_DIR / f"staged-{job_id}.png"

    def _halt(reason: str, screenshot: bool = False, **detail: Any) -> dict[str, Any]:
        if screenshot:
            with contextlib.suppress(Exception):
                page.screenshot(path=str(shot), full_page=True)
        conn.execute(
            """INSERT INTO autofill_runs (job_id, status, abort_reason, filled_fields,
               unfilled_fields, screenshot_path) VALUES (?, 'needs_human', ?, '[]', '[]', ?)""",
            (job_id, reason, str(shot) if screenshot else None),
        )
        conn.commit()
        log("stage.aborted", job_id=job_id, reason=reason, **detail)
        out: dict[str, Any] = {"job_id": job_id, "status": "needs_human", "reason": reason}
        if detail:
            out["detail"] = detail
        return out

    # fail closed BEFORE the browser touches anything
    # a cover letter the claim judge never read is not final: the job waits
    # for re-tailoring (pipeline/letter_gate.py), nothing is attached or filled
    unjudged = letter_gate.check_job(conn, job_id)
    if unjudged is not None:
        why, app = unjudged
        letter_gate.flag(conn, app, why)
        return _halt(letter_gate.REASON, why=why, cover=str(app["cover_path"]))
    if not apply_url_allowed(str(job["apply_url"] or ""), job["company_domain"]):
        return _halt(f"apply-url-not-allowlisted:{str(job['apply_url'])[:80]}")
    pre_fields = json.loads(job["fields"] or "[]")
    if not pre_fields:
        return _halt("no-form-spec-captured")  # empty dom_scrape stub must never fake success

    response = page.goto(
        normalize_apply_url(str(job["apply_url"])), wait_until="domcontentloaded", timeout=60000
    )
    page.wait_for_timeout(1000)

    # the form must actually be there: a closed posting, Ashby's Overview tab
    # or an unrendered embed used to record a run with zero fills
    form = locate_form(page, pre_fields)
    if form is None:
        abort = detect_abort(page, [], job_id)  # a challenge page hides the form
        if abort:
            return _halt(abort, screenshot=True)
        return _halt(
            "posting-not-found",
            screenshot=True,
            signal=posting_gone(page, response.status if response else None)
            or "no-captured-field-rendered",
            final_url=page.url[:200],
        )
    # redirects happen: the frame we type into must itself be allowlisted
    if not apply_url_allowed(form.url, job["company_domain"]):
        host = urlparse(form.url).hostname or form.url[:60]
        return _halt(f"form-host-not-allowlisted:{host}", screenshot=True, final_url=form.url[:200])

    abort = detect_abort(page, [str(f.get("label", "")) for f in pre_fields], job_id)
    if abort:
        return _halt(abort, screenshot=True)

    fields = pre_fields
    drafted = json.loads(job["drafted_answers"] or "{}")
    # drafted_answers holds AI drafts AND answers the human gave through MCP
    # answer_novel; the novel_answered events tell them apart
    human = {str(r[0]) for r in conn.execute(db.HUMAN_ANSWERED_FIELDS, {"job_id": job_id})}
    filled: list[dict[str, str]] = []
    unfilled: list[dict[str, str]] = []
    matcher = matcher or BankMatcher(bank)
    comp_block = comp_expectation_block_for(str(job["title"] or ""), job["comp_max"], bank)

    plan: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for f in fields:
        stored = f.get("match") or {}
        match = staging_match(matcher, f, comp_block, human)
        if (stored.get("kind"), stored.get("entry_id")) != (
            match.get("kind", "novel"),
            match.get("entry_id"),
        ):
            log(
                "stage.rematched",
                job_id=job_id,
                identity=str(f.get("identity", "")),
                was={k: stored.get(k) for k in ("kind", "entry_id", "reason")},
                now={k: match.get(k) for k in ("kind", "entry_id", "reason")},
            )
        plan.append((f, match))

    def _provoked(identity: str, label: str, how: str, widget: str) -> dict[str, Any]:
        # hard rule 2: the challenge is never touched; the run stops here,
        # names the control whose click brought it up, and goes to the human
        log(
            "stage.captcha_provoked",
            job_id=job_id,
            control=identity,
            label=label[:120],
            kind=how,
            widget=widget,
        )
        return _halt(
            f"{CAPTCHA_PROVOKED}:{identity[:80]}",
            screenshot=True,
            control=identity,
            label=label[:120],
            kind=how,
            widget=widget,
            filled_before=[x["identity"] for x in filled],
        )

    # 1. the resume and cover letter first: an ATS that parses the resume
    # rewrites name, email and phone, so the fill waits until it is done
    attached = _attach_files(form, job, bank, plan, filled, unfilled, job_id)
    if attached:
        await_parser(page, form, job_id)

    # 2. every other field; a CAPTCHA check after every click
    done: list[_Filled] = []
    for f, match in plan:
        identity, label = str(f.get("identity", "")), str(f.get("label", ""))
        kind = match.get("kind", "novel")
        if kind == "attach":
            continue  # uploaded above

        if kind == "blank_flag":
            entry = {"identity": identity, "reason": str(match.get("reason", "flagged"))}
            if identity in drafted:
                # a stored answer a gate keeps off the form is reported, never
                # dropped silently (review 2026-10-08)
                entry["withheld"] = "human_typed" if identity in human else "ai_drafted"
                log("stage.answer_withheld", job_id=job_id, **entry)
            unfilled.append(entry)
            continue

        value, source = _value_for(match, bank, drafted, identity)
        if value is None and batch_answers is not None and kind in ("novel", "scaffold"):
            # carry-forward WITHIN this batch only (SPEC §7.1): an answer given to
            # a near-identical question earlier in the session, marked as such
            key = re.sub(r"[^a-z ]", "", label.lower())[:60]
            if key in batch_answers:
                value, source = batch_answers[key], "carried_forward"
        if value is None:
            # a bank field with no stored value is left blank and flagged by
            # name — never filled from another field (LinkedIn into GitHub)
            reason = (
                f"not-in-answer-bank:{match.get('entry_id')}"
                if kind in ("identity", "screening")
                else "no-safe-answer"
            )
            unfilled.append({"identity": identity, "reason": reason})
            continue
        if _HARD_STOP_FIELD.search(label):
            unfilled.append({"identity": identity, "reason": "hard-stop-field"})
            continue
        try:
            ctl = _find_control(form, identity, label)
        except ControlAmbiguous as e:
            # the label leads to two questions' controls: never the first one
            unfilled.append({"identity": identity, "reason": f"control-ambiguous:{e}"})
            continue
        if ctl is None:
            unfilled.append({"identity": identity, "reason": "control-not-found"})
            continue
        ftype = str(f.get("type", ""))
        options = list(f.get("options") or [])
        choice = choice_for(match, bank, ftype)
        note = FillNote()
        raised = False
        try:
            ok = _fill_control(page, form, ctl, value, ftype, options, choice, note)
        except Exception as e:  # noqa: BLE001 — a stubborn control goes to the human
            log("stage.fill_error", job_id=job_id, identity=identity, error=str(e)[:120])
            ok, raised = False, True
        # a fill that raised may have clicked first: check() clicks, then
        # raises when a challenge swallowed the click. The check runs before
        # anything else is clicked, the yes/no pair below included.
        if note.clicked or raised:
            challenge = captcha_challenge(page)
            if challenge:
                return _provoked(identity, label, note.kind(), challenge)
        via_pair = False
        if not ok and ftype.lower() == "boolean":
            raised = False
            try:
                ok = via_pair = _click_boolean_pair(form, label, value, note)
            except ClickRefused:
                ok = False  # logged by the guard; the field goes to the human
            except Exception as e:  # noqa: BLE001 — a stubborn control goes to the human
                log("stage.fill_error", job_id=job_id, identity=identity, error=str(e)[:120])
                ok, raised = False, True
            if note.clicked or raised:
                challenge = captcha_challenge(page)
                if challenge:
                    return _provoked(identity, label, note.kind(), challenge)
        if ok:
            filled.append({"identity": identity, "value": value[:80], "source": source})
            done.append(
                _Filled(
                    identity=identity,
                    label=label,
                    value=value,
                    ftype=ftype,
                    options=options,
                    choice=choice,
                    ctl=None if via_pair else ctl,
                    note=note,
                )
            )
            if batch_answers is not None and source in (
                "ai_drafted",
                "human_typed",
                "carried_forward",
            ):
                batch_answers[re.sub(r"[^a-z ]", "", label.lower())[:60]] = value
            color = "#f5a623" if source == "ai_drafted" else "#3fb950"
            with contextlib.suppress(Exception):  # cosmetic only
                form.evaluate(_OUTLINE_JS, [f'[name="{identity}"]', color])
            page.wait_for_timeout(SLOW_MO_MS)
        else:
            unfilled.append({"identity": identity, "reason": "fill-failed"})

    def _challenge_after_fill() -> dict[str, Any] | None:
        # a challenge no single click was seen to bring up: named after the
        # last field filled
        challenge = captcha_challenge(page)
        if not challenge:
            return None
        last = done[-1] if done else None
        return _provoked(
            last.identity if last else "after-fill",
            last.label if last else "",
            "after-fill",
            challenge,
        )

    # 3. read every filled value back; refill once; still changed -> the human.
    # No refill click while a challenge is showing: one check first.
    halted = _challenge_after_fill()
    if halted is not None:
        return halted
    rb = read_back_all(page, form, done, job_id)
    if rb.provoked is not None:
        return _provoked(*rb.provoked)
    for o in rb.overwritten:
        filled = [x for x in filled if x["identity"] != o["identity"]]
        unfilled.append({"identity": o["identity"], "reason": OVERWRITTEN, **o})
        with contextlib.suppress(Exception):
            form.evaluate(_OUTLINE_JS, [f'[name="{o["identity"]}"]', "#f85149"])

    # 4. and once more after the whole fill, refills included
    halted = _challenge_after_fill()
    if halted is not None:
        return halted

    # required-but-unfilled → red highlight, and abort if any hard requirement is open
    required_open = [
        u
        for u in unfilled
        if any(f.get("identity") == u["identity"] and f.get("required") for f in fields)
    ]
    for u in required_open:
        with contextlib.suppress(Exception):
            form.evaluate(_OUTLINE_JS, [f'[name="{u["identity"]}"]', "#f85149"])

    page.screenshot(path=str(shot), full_page=True)
    # SPEC abort condition: a required field with no bank match and no safe
    # draft halts staging — fail closed as needs_human, never a fake "staged".
    # A value the page changed after the fill is a wrong value on the form,
    # required or not: the human looks at it.
    status = "needs_human" if required_open or rb.overwritten else "staged"
    reasons = []
    if required_open:
        reasons.append(f"required-fields-open:{len(required_open)}")
    if rb.overwritten:
        reasons.append(f"{OVERWRITTEN}:{len(rb.overwritten)}")
    fill_rate = len(filled) / max(1, len(filled) + len(unfilled))
    cur = conn.execute(
        """INSERT INTO autofill_runs (job_id, status, abort_reason, filled_fields,
           unfilled_fields, screenshot_path) VALUES (?, ?, ?, ?, ?, ?)""",
        (
            job_id,
            status,
            "; ".join(reasons) or None,
            json.dumps(filled),
            json.dumps(unfilled),
            str(shot),
        ),
    )
    readback = {"run_id": cur.lastrowid, **rb.summary()}
    conn.execute(
        "INSERT INTO events (job_id, kind, detail) VALUES (?, 'staged', ?)",
        (job_id, f"fill_rate={fill_rate:.0%} readback_mismatches={len(rb.mismatches)}"),
    )
    # the read-back of this run, by autofill_runs id (no schema change): what
    # a submit precondition reads (zero overwritten_after_fill)
    conn.execute(
        db.RECORD_EVENT,
        {"job_id": job_id, "kind": "readback", "detail": json.dumps(readback)},
    )
    conn.commit()
    log(
        "stage.done",
        job_id=job_id,
        filled=len(filled),
        unfilled=len(unfilled),
        fill_rate=round(fill_rate, 2),
        readback_mismatches=len(rb.mismatches),
        overwritten_after_fill=len(rb.overwritten),
        screenshot=str(shot),
    )
    return {
        "job_id": job_id,
        "status": status,
        "filled": len(filled),
        "unfilled": len(unfilled),
        "required_open": len(required_open),
        "fill_rate": fill_rate,
        "open_questions": [u["identity"] for u in unfilled],
        "readback_mismatches": len(rb.mismatches),
        "overwritten_after_fill": [o["identity"] for o in rb.overwritten],
        "readback": readback,
        "screenshot": str(shot),
    }


def staging_lines(conn: sqlite3.Connection, job_id: int) -> list[str]:
    """For MCP get_job: the posting-gone check that closed the job, if any,
    and the latest staging run with its read-back (fields the page changed
    after filling, which the human must look at). Read-only."""
    lines: list[str] = []
    gone = conn.execute(db.POSTING_GONE_EVENT, {"job_id": job_id}).fetchone()
    if gone is not None:
        try:
            signal = str(json.loads(gone["detail"] or "{}").get("signal") or "?")
        except ValueError:
            signal = "?"
        lines.append(
            f"closed {gone['at']} UTC: the ATS says the posting is gone ({signal}); "
            "it is not tailored or staged"
        )
    run = conn.execute(db.LATEST_STAGING_RUN, {"job_id": job_id}).fetchone()
    if run is None:
        return lines
    line = f"staging {run['created_at']} UTC: {run['status']}"
    if run["abort_reason"]:
        line += f" ({run['abort_reason']})"
    try:
        rb = json.loads(run["readback"]) if run["readback"] else None
        unfilled = json.loads(run["unfilled_fields"] or "[]")
    except ValueError:
        rb, unfilled = None, []
    if rb:
        line += (
            f"; read-back: {rb.get('checked', 0)} checked, {rb.get('mismatches', 0)} changed "
            f"after filling, {rb.get('fixed_by_refill', 0)} put right by a refill"
        )
    lines.append(line)
    for u in unfilled:
        if isinstance(u, dict) and u.get("reason") == OVERWRITTEN:
            lines.append(
                f"⚠ {u.get('identity')}: {OVERWRITTEN} (filled {u.get('wanted')!r}, the form "
                f"shows {u.get('found')!r}); check it before you send the form"
            )
    return lines


# ---- batches, the review wait, and the profile lock ----


def profile_lock_holder(profile_dir: Path) -> int | None:
    """PID of a live Chromium holding this profile, or None. Chromium's
    SingletonLock is a symlink to '<host>-<pid>'; a dead pid is a stale lock
    Chromium clears itself. Only checks liveness (signal 0), never signals."""
    try:
        target = os.readlink(profile_dir / "SingletonLock")
    except OSError:
        return None
    m = re.search(r"-(\d+)$", target)
    if not m:
        return None
    pid = int(m.group(1))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    except OSError:
        return None
    return pid


def _deferred(job_ids: list[int], reason: str) -> list[dict[str, Any]]:
    """Jobs that could not be staged this run, returned and logged — never dropped."""
    if job_ids:
        log("stage.deferred", job_ids=job_ids, reason=reason)
    return [{"job_id": j, "status": "deferred", "reason": reason} for j in job_ids]


def _wait_for_review(ctx: BrowserContext) -> None:
    """Block until the human has closed every tab, or quit the browser.

    Waiting for the context's own close event hangs on macOS: closing the last
    window with the red button leaves Chromium running, the event never fires,
    and the profile stays locked until Cmd+Q. So watch the pages instead; the
    caller then closes the context, which releases the profile lock."""
    while True:
        try:
            open_pages = [p for p in ctx.pages if not p.is_closed()]
        except Exception:  # noqa: BLE001 — the context is already gone
            return
        if not open_pages:
            return
        try:
            open_pages[0].wait_for_event("close", timeout=5000)
        except PlaywrightTimeoutError:
            continue  # still open: re-check them all (another tab may have closed first)
        except Exception:  # noqa: BLE001 — browser quit (Cmd+Q) or the context was torn down
            return


def _stage_batch(
    conn: sqlite3.Connection,
    ctx: BrowserContext,
    batch: list[int],
    matcher: BankMatcher | None,
    keep_open: bool,
    carry_forward: bool = True,
) -> tuple[list[dict[str, Any]], bool]:
    """Stage one batch, one tab per job. Returns (results, browser_closed):
    browser_closed means the human closed the browser mid-batch, so the rest
    of this batch (and later batches) are deferred, not staged."""
    results: list[dict[str, Any]] = []
    stray = list(ctx.pages)  # the blank tab a persistent context opens with
    # carry-forward never crosses batches, and only runs in a session the
    # human is in (SPEC §7.1): None turns it off for the unattended nightly run
    batch_answers: dict[str, str] | None = {} if carry_forward else None
    for i, jid in enumerate(batch):
        try:
            page = ctx.new_page()
        except Exception as e:  # noqa: BLE001 — the window was closed under us
            results.extend(_deferred(batch[i:], f"browser-closed: {str(e)[:80]}"))
            return results, True
        for p in stray:  # closed so "no tabs left" means the human is done
            with contextlib.suppress(Exception):
                p.close()
        stray = []
        try:
            results.append(stage_job(conn, page, jid, batch_answers, matcher))
        except Exception as e:  # noqa: BLE001 — isolate per-job failures
            log("stage.job_failed", job_id=jid, error=str(e)[:200])
            results.append({"job_id": jid, "status": "failed", "reason": str(e)[:120]})
        if not keep_open:
            with contextlib.suppress(Exception):
                page.close()
    return results, False


def stage_jobs(
    conn: sqlite3.Connection,
    job_ids: list[int],
    keep_open: bool = False,
    carry_forward: bool = True,
) -> list[dict[str, Any]]:
    """Stage job_ids in a headed persistent-profile browser, at most MAX_TABS
    tabs at a time (SPEC §7.1). A longer list runs in successive batches, each
    its own browser session on the one profile; with keep_open the next batch
    opens only after the human has closed every tab of this one. Every id gets
    a result: a job that could not be staged comes back as 'deferred' with the
    reason (profile locked, browser closed mid-batch), and is logged.
    carry_forward=False (the unattended nightly run) never reuses one job's
    answer on another job's form."""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    batches = [job_ids[i : i + MAX_TABS] for i in range(0, len(job_ids), MAX_TABS)]
    if len(batches) > 1:
        log("stage.batched", total=len(job_ids), batches=len(batches), max_tabs=MAX_TABS)
    try:
        matcher: BankMatcher | None = BankMatcher(load_bank())
    except Exception as e:  # noqa: BLE001 — each job then fails on its own, visibly
        log("stage.matcher_unavailable", error=str(e)[:120])
        matcher = None
    results: list[dict[str, Any]] = []
    with sync_playwright() as pw:
        for n, batch in enumerate(batches):
            remaining = [j for b in batches[n:] for j in b]
            holder = profile_lock_holder(PROFILE_DIR)
            if holder is not None:
                results.extend(
                    _deferred(
                        remaining,
                        f"profile-locked-by-pid-{holder}: quit that browser (Cmd+Q) "
                        f"or kill {holder}, then stage again",
                    )
                )
                break
            try:
                ctx = pw.chromium.launch_persistent_context(
                    str(PROFILE_DIR),
                    headless=False,
                    slow_mo=SLOW_MO_MS,
                    viewport={"width": 1280, "height": 900},
                )
            except Exception as e:  # noqa: BLE001 — report, never drop
                results.extend(_deferred(remaining, f"browser-launch-failed: {str(e)[:100]}"))
                break
            try:
                staged, browser_closed = _stage_batch(
                    conn, ctx, batch, matcher, keep_open, carry_forward
                )
                results.extend(staged)
                if browser_closed:
                    results.extend(
                        _deferred(
                            [j for b in batches[n + 1 :] for j in b], "browser-closed-mid-batch"
                        )
                    )
                    break
                if keep_open:
                    log("stage.review_wait", batch=n + 1, of=len(batches), job_ids=batch)
                    _wait_for_review(ctx)
            finally:
                with contextlib.suppress(Exception):
                    ctx.close()
    return results
