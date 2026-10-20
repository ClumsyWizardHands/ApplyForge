"""Staging browser logic with fake Playwright objects — offline, no browser.

Covers audit 2026-10-08 §2.4 / §3.6: the invisible-reCAPTCHA badge must not
abort staging (all 8 Greenhouse runs died on it) while real challenges still
do; a page with no form halts as posting-not-found instead of recording zero
fills; the form is found inside an embedded iframe; jobs past MAX_TABS run in
later batches instead of vanishing; keep_open finishes when the tabs are
closed (macOS never fires the context close event); a held profile lock
defers the batch with an actionable reason."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from jobkit.autofill import answer_bank, browser
from jobkit.autofill.browser import (
    CAPTCHA_WIDGETS,
    captcha_challenge,
    detect_abort,
    is_captcha_challenge,
    locate_form,
    normalize_apply_url,
    profile_lock_holder,
)

# ---- fakes ---------------------------------------------------------------------


class FakeEl:
    def __init__(self, visible: bool, box: dict[str, float] | None, probe: dict[str, Any]):
        self.visible, self.box, self.probe = visible, box, probe

    def is_visible(self) -> bool:
        return self.visible

    def bounding_box(self) -> dict[str, float] | None:
        return self.box

    def evaluate(self, js: str) -> dict[str, Any]:
        return self.probe


class FakeLocator:
    def __init__(self, els: list[Any], text: str = "") -> None:
        self.els, self.text = els, text

    def count(self) -> int:
        return len(self.els)

    def nth(self, i: int) -> Any:
        return self.els[i]

    def inner_text(self, timeout: float | None = None) -> str:
        return self.text


class FakeFrame:
    """widgets: CAPTCHA widget name → elements. ids: field ids present in the
    DOM. labels: labels get_by_label can find. body: the page text."""

    def __init__(
        self,
        url: str = "https://job-boards.greenhouse.io/acme/jobs/1",
        widgets: dict[str, list[FakeEl]] | None = None,
        ids: tuple[str, ...] = (),
        labels: tuple[str, ...] = (),
        body: str = "",
    ) -> None:
        self.url, self.widgets, self.ids = url, widgets or {}, ids
        self.labels, self.body = labels, body
        self._by_sel = dict((sel, name) for name, sel in CAPTCHA_WIDGETS)

    def locator(self, sel: str) -> FakeLocator:
        if sel in self._by_sel:
            return FakeLocator(self.widgets.get(self._by_sel[sel], []))
        if sel == "body":
            return FakeLocator([], text=self.body)
        hit = any(f'[name="{i}"]' in sel or f'[id="{i}"]' in sel for i in self.ids)
        return FakeLocator([object()] if hit else [])

    def get_by_label(self, label: str, exact: bool = False) -> FakeLocator:
        return FakeLocator([object()] if label in self.labels else [])

    def evaluate(self, js: str, arg: Any = None) -> None:
        return None


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class FakePage:
    def __init__(self, frames: list[FakeFrame], status: int = 200) -> None:
        self.frames, self.status = frames, status
        self.url = frames[0].url if frames else "about:blank"
        self.visited: list[str] = []
        self.main_frame = frames[0] if frames else None

    def goto(self, url: str, **kw: Any) -> FakeResponse:
        self.visited.append(url)
        return FakeResponse(self.status)

    def wait_for_timeout(self, ms: float) -> None:
        return None

    def screenshot(self, **kw: Any) -> None:
        return None


BADGE_BOX = {"x": 1024.0, "y": 835.0, "width": 256.0, "height": 60.0}
INVISIBLE_ANCHOR = "https://www.google.com/recaptcha/api2/anchor?ar=1&k=K&hl=en&size=invisible&cb=x"


def _badge_frame(**extra: list[FakeEl]) -> FakeFrame:
    widgets: dict[str, list[FakeEl]] = {
        "recaptcha-iframe": [
            # the collapsed invisible-reCAPTCHA badge, fixed bottom-right
            FakeEl(True, BADGE_BOX, {"src": INVISIBLE_ANCHOR, "inBadge": True}),
            # the challenge frame, hidden until a challenge fires
            FakeEl(False, None, {"src": "https://www.google.com/recaptcha/api2/bframe?k=K"}),
        ],
        # the explicit-render container holding the badge
        "recaptcha-widget": [FakeEl(True, BADGE_BOX, {"hasBadge": True})],
    }
    for k, v in extra.items():
        widgets[k] = widgets.get(k, []) + v
    return FakeFrame(widgets=widgets)


# ---- CAPTCHA -------------------------------------------------------------------


def test_invisible_recaptcha_badge_is_not_a_challenge() -> None:
    page = FakePage([_badge_frame()])
    assert captcha_challenge(page) is None
    assert detect_abort(page, ["First Name", "Email"]) is None


def test_offscreen_challenge_frame_is_not_a_challenge() -> None:
    parked = FakeEl(
        True,
        {"x": 0.0, "y": -10000.0, "width": 400.0, "height": 580.0},
        {"src": "https://www.google.com/recaptcha/api2/bframe?k=K"},
    )
    assert captcha_challenge(FakePage([_badge_frame(**{"recaptcha-iframe": [parked]})])) is None
    box = {"x": 0, "y": 0, "width": 300, "height": 80}
    assert not is_captcha_challenge({"src": "https://hcaptcha.com/x#frame=checkbox-invisible"}, box)
    assert not is_captcha_challenge({"size": "invisible"}, box)


@pytest.mark.parametrize(
    ("widget", "el", "where"),
    [
        (
            "recaptcha-iframe",
            FakeEl(
                True,
                {"x": 40.0, "y": 900.0, "width": 304.0, "height": 78.0},
                {"src": "https://www.google.com/recaptcha/api2/anchor?k=K&size=normal"},
            ),
            0,
        ),
        (
            "recaptcha-iframe",  # the challenge frame once a challenge is actually shown
            FakeEl(
                True,
                {"x": 300.0, "y": 200.0, "width": 400.0, "height": 580.0},
                {"src": "https://www.google.com/recaptcha/api2/bframe?k=K"},
            ),
            0,
        ),
        (
            "hcaptcha-iframe",
            FakeEl(
                True,
                {"x": 10.0, "y": 10.0, "width": 300.0, "height": 150.0},
                {"src": "https://newassets.hcaptcha.com/captcha/v1/x#frame=challenge"},
            ),
            1,  # inside an embedded iframe
        ),
        (
            "turnstile-widget",
            FakeEl(True, {"x": 10.0, "y": 10.0, "width": 300.0, "height": 65.0}, {}),
            0,
        ),
    ],
)
def test_visible_challenges_still_abort_and_log_the_selector(
    widget: str, el: FakeEl, where: int, jobkit_home: Path
) -> None:
    frames = [_badge_frame(), FakeFrame(url="https://boards.greenhouse.io/embed/job_app?x=1")]
    frames[where].widgets.setdefault(widget, []).append(el)
    page = FakePage(frames)
    hit = captcha_challenge(page)
    assert hit is not None and hit.startswith(widget), hit
    assert detect_abort(page, [], job_id=7) == "captcha-or-turnstile-visible"
    log_lines = (jobkit_home / "logs" / "jobkit.jsonl").read_text().splitlines()
    events = [json.loads(line) for line in log_lines]
    fired = [e for e in events if e["event"] == "stage.captcha_detected"]
    assert fired and fired[-1]["job_id"] == 7 and fired[-1]["widget"].startswith(widget)


# ---- is there a form? ------------------------------------------------------------


def test_ashby_overview_url_is_pointed_at_the_application_tab() -> None:
    base = "https://jobs.ashbyhq.com/fabrikam-payments/a1b2c3d4-1234-4abc-8def-0123456789ab"
    assert normalize_apply_url(base) == base + "/application"
    assert normalize_apply_url(base + "/") == base + "/application"
    assert normalize_apply_url(base + "/application") == base + "/application"
    assert normalize_apply_url(base + "?src=x") == base + "/application?src=x"
    gh = "https://boards.greenhouse.io/acme/jobs/1"
    assert normalize_apply_url(gh) == gh


def test_form_found_inside_an_embedded_iframe() -> None:
    fields = [{"identity": "first_name", "label": "First Name"}, {"identity": "email"}]
    careers = FakeFrame(url="https://www.acme.com/company/careers", body="Join us")
    embed = FakeFrame(url="https://boards.greenhouse.io/embed/job_app?for=acme", ids=("email",))
    page = FakePage([careers, embed])
    assert locate_form(page, fields, timeout_ms=1000) is embed
    # falls back to labels when the ids differ from the DOM
    by_label = FakeFrame(labels=("First Name", "Last Name"))
    fields = [{"identity": "x1", "label": "First Name"}, {"identity": "x2", "label": "Last Name"}]
    assert locate_form(FakePage([by_label]), fields, timeout_ms=0) is by_label
    assert locate_form(FakePage([careers]), fields, timeout_ms=1000) is None


@pytest.fixture()
def staged_job(
    conn: sqlite3.Connection, jobkit_home: Path
) -> Iterator[tuple[sqlite3.Connection, int]]:
    (jobkit_home / "data" / "answer_bank.yaml").write_text(
        yaml.safe_dump({"identity": {"first_name": "Pat", "email": "pat@example.com"}})
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
    assert jid is not None
    fields = [
        {"identity": "_systemfield_name", "label": "Name", "required": True, "match": {}},
        {"identity": "_systemfield_email", "label": "Email", "required": True, "match": {}},
    ]
    conn.execute(
        "INSERT INTO form_specs (job_id, source, fields, drafted_answers) "
        "VALUES (?, 'api_questions', ?, '{}')",
        (jid, json.dumps(fields)),
    )
    conn.commit()
    yield conn, jid
    answer_bank.load_bank.cache_clear()


@pytest.mark.parametrize(
    ("frame", "status", "signal"),
    [
        (FakeFrame(url="https://jobs.ashbyhq.com/initech/x", body="Page not found"), 200,
         "page-says:Page not found"),
        (FakeFrame(url="https://jobs.ashbyhq.com/initech/x", body=""), 404, "http-404"),
        (FakeFrame(url="https://jobs.ashbyhq.com/initech/x", body="Overview"), 200,
         "no-captured-field-rendered"),
    ],
)  # fmt: skip
def test_no_form_halts_as_posting_not_found(
    staged_job: tuple[sqlite3.Connection, int], frame: FakeFrame, status: int, signal: str
) -> None:
    conn, jid = staged_job
    page = FakePage([frame], status=status)
    r = browser.stage_job(conn, page, jid)
    assert r["status"] == "needs_human" and r["reason"] == "posting-not-found", r
    assert r["detail"]["signal"] == signal
    run = conn.execute(
        "SELECT status, abort_reason, filled_fields FROM autofill_runs WHERE job_id=?", (jid,)
    ).fetchone()
    assert tuple(run) == ("needs_human", "posting-not-found", "[]")


def test_redirect_off_the_allowlist_halts(staged_job: tuple[sqlite3.Connection, int]) -> None:
    conn, jid = staged_job
    elsewhere = FakeFrame(url="https://forms.evil.example/x", ids=("_systemfield_email",))
    r = browser.stage_job(conn, FakePage([elsewhere]), jid)
    assert r["reason"] == "form-host-not-allowlisted:forms.evil.example"


# ---- human answers (MCP answer_novel) reach the form ---------------------------


def _pin(conn: sqlite3.Connection, jid: int, monkeypatch: pytest.MonkeyPatch) -> Any:
    from jobkit import mcp_server

    conn.executescript(mcp_server.PINS_SCHEMA)
    conn.execute("INSERT INTO queue_pins (n, job_id) VALUES (1, ?)", (jid,))
    conn.commit()
    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    return mcp_server


def test_answers_given_through_answer_novel_are_filled_at_staging(
    staged_job: tuple[sqlite3.Connection, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 2026-10-08: answer_novel stores the human's answer in
    drafted_answers next to the AI drafts, and staging's re-match blanked it
    wherever never_draft or the salary-expectation gate fired: a referrer email
    the human typed was dropped with no message. The human's answer is filled
    as human_typed; an AI draft for the same kind of field is still withheld,
    and the run says so. Comp history and EEO stay blank whoever answered."""
    conn, jid = staged_job
    fields = [
        {"identity": "_systemfield_name", "label": "Name", "required": True, "match": {}},
        {"identity": "q9", "label": "Referrer email (if referred)", "match": {"kind": "novel"}},
        {"identity": "q10", "label": "Desired compensation", "match": {"kind": "novel"}},
        {"identity": "q11", "label": "Former employee email", "match": {"kind": "novel"}},
        {"identity": "q12", "label": "Why Initech?", "match": {"kind": "novel"}},
        {"identity": "q13", "label": "What is your current base salary?", "match": {}},
        {"identity": "q14", "label": "Gender", "compliance_type": "eeo", "match": {}},
    ]
    ai_drafts = {"q11": "pat@example.com", "q12": "Draft about Initech."}
    conn.execute(
        "UPDATE form_specs SET fields=?, drafted_answers=?, captured_at=? WHERE job_id=?",
        (json.dumps(fields), json.dumps(ai_drafts), "2000-01-01 00:00:00", jid),
    )
    # an answer recorded before the current capture was replaced by it
    conn.execute(
        "INSERT INTO events (job_id, kind, detail, at) "
        "VALUES (?, 'novel_answered', 'q11', '1999-12-31 00:00:00')",
        (jid,),
    )
    mcp = _pin(conn, jid, monkeypatch)

    assert "human_typed" in mcp.answer_novel(1, "q9", "jane@acme.com")
    assert "human_typed" in mcp.answer_novel(1, "q10", "$140,000 OTE")
    # refused up front: hard rules 4 and 5 hold whoever answers
    assert "compensation history" in mcp.answer_novel(1, "q13", "$150,000")
    assert "EEO" in mcp.answer_novel(1, "q14", "Male")
    assert "answer not recorded" in mcp.answer_novel(1, "q99", "x")
    out = mcp.get_novel_questions(1)
    mine = out.split("answered by you")[1]
    assert "jane@acme.com" in mine and "$140,000 OTE" in mine and "pat@example.com" not in mine
    # a comp-history answer stored before answer_novel refused them
    stored = json.loads(
        conn.execute("SELECT drafted_answers FROM form_specs WHERE job_id=?", (jid,)).fetchone()[0]
    )
    assert "q13" not in stored and "q14" not in stored
    stored["q13"] = "$150,000"
    conn.execute(
        "UPDATE form_specs SET drafted_answers=? WHERE job_id=?", (json.dumps(stored), jid)
    )
    conn.execute(
        "INSERT INTO events (job_id, kind, detail) VALUES (?, 'novel_answered', 'q13')", (jid,)
    )
    conn.commit()

    typed: list[str] = []

    def fake_fill(page: Any, scope: Any, ctl: Any, value: str, *rest: Any) -> bool:
        typed.append(value)
        return True

    monkeypatch.setattr(browser, "_fill_control", fake_fill)
    form = FakeFrame(
        url="https://jobs.ashbyhq.com/initech/x/application",
        ids=tuple(str(f["identity"]) for f in fields),
    )
    matcher = answer_bank.BankMatcher(answer_bank.load_bank(), use_embeddings=False)
    r = browser.stage_job(conn, FakePage([form]), jid, matcher=matcher)
    assert r["status"] == "staged", r
    run = conn.execute(
        "SELECT filled_fields, unfilled_fields FROM autofill_runs WHERE job_id=?", (jid,)
    ).fetchone()
    filled = {f["identity"]: (f["value"], f["source"]) for f in json.loads(run[0])}
    unfilled = {u["identity"]: u for u in json.loads(run[1])}
    assert filled["q9"] == ("jane@acme.com", "human_typed")
    assert filled["q10"] == ("$140,000 OTE", "human_typed")
    assert filled["q12"] == ("Draft about Initech.", "ai_drafted")
    assert filled["_systemfield_name"] == ("Pat", "answer_bank")
    # the AI's email draft stays off the form, and the run records that
    assert unfilled["q11"] == {
        "identity": "q11",
        "reason": "never-draft:contact-detail",
        "withheld": "ai_drafted",
    }
    assert unfilled["q13"] == {
        "identity": "q13",
        "reason": "salary-history-never-answer",
        "withheld": "human_typed",
    }
    assert unfilled["q14"] == {"identity": "q14", "reason": "eeo-no-stored-choice"}
    assert "pat@example.com" not in typed and "$150,000" not in typed and "Male" not in typed


def test_answer_novel_without_a_form_spec_records_nothing(
    staged_job: tuple[sqlite3.Connection, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, jid = staged_job
    conn.execute("DELETE FROM form_specs WHERE job_id=?", (jid,))
    mcp = _pin(conn, jid, monkeypatch)
    assert "answer not recorded" in mcp.answer_novel(1, "q9", "jane@acme.com")
    n = conn.execute("SELECT COUNT(*) FROM events WHERE kind='novel_answered'").fetchone()[0]
    assert n == 0


# ---- batches, review wait, profile lock --------------------------------------------


class FakeTab:
    def __init__(self, ctx: FakeCtx, url: str = "about:blank") -> None:
        self.ctx, self.url, self.closed = ctx, url, False
        self.timeouts_before_close = 0

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.ctx.pages.remove(self)

    def wait_for_event(self, event: str, timeout: float | None = None) -> None:
        assert event == "close"
        if self.timeouts_before_close > 0:  # the human is still reading
            self.timeouts_before_close -= 1
            raise browser.PlaywrightTimeoutError("Timeout 5000ms exceeded")
        self.close()  # the human closes the tab


class FakeCtx:
    def __init__(self, launches: list[FakeCtx]) -> None:
        self.pages: list[FakeTab] = []
        self.pages.append(FakeTab(self))  # persistent contexts open with a blank tab
        self.closed = False
        self.max_open = 0
        launches.append(self)

    def new_page(self) -> FakeTab:
        t = FakeTab(self, "https://x")
        self.pages.append(t)
        self.max_open = max(self.max_open, len(self.pages))
        return t

    def close(self) -> None:
        self.closed = True


class FakePW:
    def __init__(self, launches: list[FakeCtx], fail_on: int | None) -> None:
        self.launches, self.fail_on = launches, fail_on
        self.chromium = self

    def launch_persistent_context(self, *a: Any, **kw: Any) -> FakeCtx:
        assert kw.get("headless") is False, "never headless"
        if self.fail_on is not None and len(self.launches) == self.fail_on:
            raise RuntimeError("profile is already in use")
        return FakeCtx(self.launches)


@pytest.fixture()
def fake_browser(monkeypatch: pytest.MonkeyPatch) -> Any:
    state: dict[str, Any] = {"launches": [], "staged": [], "fail_on": None}

    @contextlib.contextmanager
    def fake_sync_playwright() -> Iterator[FakePW]:
        yield FakePW(state["launches"], state["fail_on"])

    def fake_stage_job(conn: Any, page: Any, jid: int, *a: Any) -> dict[str, Any]:
        state["staged"].append((jid, page.ctx))
        return {"job_id": jid, "status": "staged"}

    monkeypatch.setattr(browser, "sync_playwright", fake_sync_playwright)
    monkeypatch.setattr(browser, "stage_job", fake_stage_job)
    return state


def test_jobs_past_max_tabs_run_in_later_batches(fake_browser: Any) -> None:
    ids = list(range(1, 20))  # 19 jobs, MAX_TABS = 8
    results = browser.stage_jobs(None, ids)
    assert [r["job_id"] for r in results] == ids, "every job gets a result, in order"
    assert all(r["status"] == "staged" for r in results)
    launches = fake_browser["launches"]
    assert len(launches) == 3 and all(c.closed for c in launches)
    per_ctx = [sum(1 for _, c in fake_browser["staged"] if c is ctx) for ctx in launches]
    assert per_ctx == [8, 8, 3]
    assert all(c.max_open <= browser.MAX_TABS for c in launches)


def test_keep_open_waits_for_tabs_not_the_context(fake_browser: Any) -> None:
    ids = list(range(1, 11))
    results = browser.stage_jobs(None, ids, keep_open=True)
    assert len(results) == 10
    first, second = fake_browser["launches"]
    # all 8 tabs of batch 1 were open together for review, the blank tab was closed
    assert first.max_open == 8 and first.pages == [] and first.closed
    assert second.max_open == 2 and second.pages == [] and second.closed


def test_review_wait_survives_slow_review_and_browser_quit() -> None:
    launches: list[FakeCtx] = []
    ctx = FakeCtx(launches)
    tab = ctx.new_page()
    tab.timeouts_before_close = 3
    browser._wait_for_review(ctx)
    assert ctx.pages == []

    class QuitTab(FakeTab):
        def wait_for_event(self, event: str, timeout: float | None = None) -> None:
            raise RuntimeError("Target page, context or browser has been closed")

    ctx2 = FakeCtx(launches)
    ctx2.pages = [QuitTab(ctx2)]
    browser._wait_for_review(ctx2)  # returns, does not hang


def test_failed_launch_defers_the_rest_explicitly(fake_browser: Any, jobkit_home: Path) -> None:
    fake_browser["fail_on"] = 1  # second batch cannot launch
    results = browser.stage_jobs(None, list(range(1, 13)))
    assert [r["job_id"] for r in results] == list(range(1, 13))
    assert [r["status"] for r in results] == ["staged"] * 8 + ["deferred"] * 4
    assert results[-1]["reason"].startswith("browser-launch-failed")
    log = (jobkit_home / "logs" / "jobkit.jsonl").read_text()
    assert '"stage.deferred"' in log and '"stage.batched"' in log


def test_held_profile_lock_defers_with_an_actionable_reason(
    fake_browser: Any, jobkit_home: Path
) -> None:
    profile = browser.PROFILE_DIR
    profile.mkdir(parents=True, exist_ok=True)
    os.symlink(f"somehost-{os.getpid()}", profile / "SingletonLock")
    assert profile_lock_holder(profile) == os.getpid()
    results = browser.stage_jobs(None, [5, 6])
    assert [r["status"] for r in results] == ["deferred", "deferred"]
    assert f"kill {os.getpid()}" in results[0]["reason"]
    assert fake_browser["launches"] == [], "never launched against a held profile"


def test_carry_forward_stays_in_the_batch_and_is_off_unattended(
    fake_browser: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []

    def stage_job(conn: Any, page: Any, jid: int, batch_answers: Any = None, *a: Any) -> Any:
        seen.append(batch_answers)
        return {"job_id": jid, "status": "staged"}

    monkeypatch.setattr(browser, "stage_job", stage_job)
    browser.stage_jobs(None, [1, 2])
    assert seen == [{}, {}] and seen[0] is seen[1]  # one carry-forward map per batch
    seen.clear()
    browser.stage_jobs(None, [1, 2], carry_forward=False)  # the nightly run (SPEC §7.1)
    assert seen == [None, None]


def test_stale_profile_lock_is_ignored(tmp_path: Path) -> None:
    os.symlink("somehost-999999", tmp_path / "SingletonLock")  # no such pid
    assert profile_lock_holder(tmp_path) is None
    assert profile_lock_holder(tmp_path / "missing") is None
