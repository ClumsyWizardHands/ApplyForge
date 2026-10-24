"""The LLM claim-support judge (validate/claim_judge.py, package 4): what it
is shown, how its reply is read (anything but a well-formed "pass" fails
closed), its llm.py cache, and the cover attempt key. Offline: the judge's
model is a fake; the letter- and answer-level gate tests live beside the
rule-based ones in tests/test_claims.py."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from jobkit import db as q
from jobkit import llm
from jobkit.pipeline import tailor
from jobkit.validate import claim_judge
from jobkit.validate.provenance import ProvenanceResult
from tests.test_claims import (
    CORPUS,
    GOOD_CITED,
    GOOD_LETTER,
    JUDGE_PASS,
    FakeLLM,
    _selection,
    judged_text,
    tailor_env,  # noqa: F401 — fixture
)

PASSED = ProvenanceResult(ok=True)


def _gate(conn: sqlite3.Connection, text: str = GOOD_LETTER, **kw: Any) -> ProvenanceResult:
    kw.setdefault("target_company", "Fabrikam Payments")
    kw.setdefault("job_title", "Director of Revenue Operations")
    return claim_judge.gate(conn, PASSED, text, GOOD_CITED, CORPUS, **kw)


# ---- what the judge is shown ----


def test_the_judge_sees_cited_sources_roles_skills_corpus_and_target() -> None:
    _pf, prompt = claim_judge.render_prompt(
        GOOD_LETTER,
        GOOD_CITED,
        CORPUS,
        target_company="Fabrikam Payments",
        job_title="Director of Revenue Operations",
    )
    cited = prompt.split("\nCITED SOURCES", 1)[1].split("\nROLES", 1)[0]
    # the cited bullets, each with employer, title, dates and metric
    assert "b_047 | Haulmark · Regional Sales Manager · Mar 2024 – Present" in cited
    assert "metric: ~$3.2M → ~$5.6M annual bookings; 41 new accounts" in cited
    assert "b_049 | ledgerline · Head of Revenue Operations · Nov 2021 – Mar 2024" in cited
    assert "b_057" not in cited and "b_058" not in cited  # uncited bullets are not sources
    roles = prompt.split("\nROLES", 1)[1].split("\nEDUCATION", 1)[0]
    assert "Keyward Security · Inside Sales Representative · Aug 2016" in roles
    assert "Lakeside State University" in prompt.split("\nEDUCATION:", 1)[1]
    skills = prompt.split("\nSKILL INDEX (", 1)[1].split("\nFULL CORPUS", 1)[0]
    assert "- MEDDIC: taught it to the AE team" in skills
    # the full corpus, uncited bullets included, for attribution
    corpus = prompt.split("\nFULL CORPUS (", 1)[1].split("\nTEXT (", 1)[0]
    assert "b_057 | ledgerline" in corpus
    assert "b_058 | Marlow Payroll · Mid-Market Account Executive" in corpus
    assert "TARGET: the Director of Revenue Operations role at Fabrikam Payments" in prompt
    assert "(none: this is not a form answer)" in prompt
    assert judged_text(prompt).strip() == GOOD_LETTER


def test_a_drafted_answer_is_judged_with_its_question() -> None:
    _pf, prompt = claim_judge.render_prompt(
        "Yes.",
        [],
        CORPUS,
        target_company="Hooli",
        job_title="Senior Account Executive",
        artifact_kind="drafted answer to an application-form question",
        question="Have you managed a team of 10+?",
    )
    assert "Have you managed a team of 10+?" in prompt.split("\nFORM QUESTION (", 1)[1]
    assert "(none: the text cites no source)" in prompt
    assert "TEXT (drafted answer to an application-form question):\nYes." in prompt


def _squash(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize(
    "question",
    [
        "Have you ever been terminated or asked to resign?",
        "Have you ever been convicted of a felony?",
        "Have you ever been placed on a PIP?",
    ],
)
def test_the_judge_is_never_told_a_plain_denial_is_not_a_claim(question: str) -> None:
    """Claim-judge review 2026-10-10: version 1.0 exempted "a plain denial
    ('No.', ..., 'I don't hold that license') that nothing below contradicts",
    so "No." to a termination or conviction question passed the judge as well
    as the rules. Only the target-tenure denial keeps a source: the role list."""
    pf, prompt = claim_judge.render_prompt(
        "No.",
        [],
        CORPUS,
        target_company="Fabrikam Payments",
        job_title="Director of Revenue Operations",
        artifact_kind="drafted answer to an application-form question",
        question=question,
    )
    assert pf.version == claim_judge.version() == "1.7"
    flat = _squash(prompt)
    # the general exemption is gone, wording and grounds both
    assert "plain denial" not in flat.lower()
    assert "cannot overstate" not in flat.lower()
    assert "I don't hold that license" not in flat
    not_a_claim = flat.split("WHAT IS NOT A CLAIM", 1)[1].split("WHAT SUPPORTS A CLAIM", 1)[0]
    assert "denial" not in not_a_claim.lower() and '"No."' not in not_a_claim
    # a denial is a claim, and the question it answers is read with it
    claim = flat.split("WHAT A CLAIM IS.", 1)[1].split("WHAT IS NOT A CLAIM", 1)[0]
    assert "A denial is a claim about the candidate's past or status." in claim
    assert question in _squash(prompt.split("\nFORM QUESTION (", 1)[1].split("\nCITED", 1)[0])
    # the one supported denial is narrow: no role at a company the role list lacks
    denials = _squash(prompt.split("\nDENIALS.", 1)[1].split("\n\n", 1)[0])
    assert denials.startswith(
        "ROLES supports ONE kind of denial: that the candidate has not worked at, or been "
        "employed by, a company ROLES does not list"
    )
    assert "Treat every other denial as unsupported" in denials
    for topic in (
        "terminations",
        "performance plans",
        "discipline",
        "convictions",
        "non-compete",
        "restrictive agreements",
        "sponsorship",
        "work authorization",
        "licenses",
    ):
        assert topic in denials, topic
    assert judged_text(prompt).strip() == "No."


def test_the_text_is_rendered_last_so_it_fills_no_other_placeholder() -> None:
    text = "I bring {corpus} and {skills}."
    _pf, prompt = claim_judge.render_prompt(text, [], CORPUS, target_company="Fabrikam Payments")
    assert judged_text(prompt).strip() == text  # literal, not expanded


# ---- how a reply is read: only a well-formed "pass" passes ----


def test_a_well_formed_pass_passes() -> None:
    assert claim_judge.parse_verdict({"unsupported": [], "verdict": "pass"}) == []
    assert claim_judge.parse_verdict({"unsupported": [], "verdict": " PASS "}) == []


@pytest.mark.parametrize(
    ("reply", "kind"),
    [
        ({"unsupported": [{"sentence": "S.", "claim": "c", "why": "w"}], "verdict": "fail"}, "u"),
        # a "pass" that lists a claim is inconsistent: the claim wins
        ({"unsupported": [{"sentence": "S.", "claim": "c", "why": "w"}], "verdict": "pass"}, "u"),
        ({"unsupported": [], "verdict": "fail"}, "u"),  # a fail that names nothing
        ({"unsupported": ["a bare string entry"], "verdict": "pass"}, "u"),
        ({"unsupported": [{}], "verdict": "pass"}, "u"),
        ({"verdict": "pass"}, "x"),
        ({"unsupported": None, "verdict": "pass"}, "x"),
        ({"unsupported": "nothing", "verdict": "pass"}, "x"),
        ({"unsupported": [], "verdict": "ok"}, "x"),
        ({"unsupported": [], "verdict": True}, "x"),
        ({"unsupported": []}, "x"),
        (["pass"], "x"),
        ("pass", "x"),
    ],
)
def test_anything_but_a_clean_pass_fails_closed(reply: Any, kind: str) -> None:
    violations = claim_judge.parse_verdict(reply)
    assert violations
    want = claim_judge.UNSUPPORTED if kind == "u" else claim_judge.UNPARSEABLE
    assert {v["kind"] for v in violations} == {want}
    assert all(set(v) == {"kind", "detail", "sentence"} for v in violations)


def test_an_unsupported_claim_keeps_its_sentence_and_reason() -> None:
    (v,) = claim_judge.parse_verdict(
        {
            "unsupported": [
                {
                    "sentence": "I also led the sales development team.",
                    "claim": "led an SDR team",
                    "why": "no cited bullet mentions a sales development team",
                }
            ],
            "verdict": "fail",
        }
    )
    assert v == {
        "kind": "claim_judge_unsupported",
        "detail": "led an SDR team — no cited bullet mentions a sales development team",
        "sentence": "I also led the sales development team.",
    }


# ---- the real llm.call_json: cached by llm.py keys, a bad reply never cached ----


@pytest.fixture()
def backend(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The CLI backend, faked: replies[stage] is the raw text the model returns."""
    state: dict[str, Any] = {"calls": [], "reply": json.dumps(JUDGE_PASS)}

    def fake(stage: str, prompt: str) -> tuple[str, dict[str, Any]]:
        state["calls"].append((stage, prompt))
        return str(state["reply"]), {}

    monkeypatch.setattr(llm, "_call_claude_cli", fake)
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    return state


def _cached(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT model FROM llm_cache WHERE stage='claim_judge'")]


def test_a_verdict_is_cached_under_the_full_prompt(
    conn: sqlite3.Connection, backend: dict[str, Any]
) -> None:
    assert _gate(conn).ok
    assert _gate(conn).ok  # served from llm_cache
    assert len(backend["calls"]) == 1 and backend["calls"][0][0] == "claim_judge"
    assert _cached(conn) == ["claude-opus-5-5"]  # strong tier
    # a different text is a different prompt: a new call
    assert _gate(conn, GOOD_LETTER + " Thanks.").ok
    assert len(backend["calls"]) == 2


def test_a_reply_with_no_json_fails_closed_and_is_never_cached(
    conn: sqlite3.Connection, backend: dict[str, Any]
) -> None:
    backend["reply"] = "Every claim here looks supported to me."
    result = _gate(conn)
    assert not result.ok
    assert [v["kind"] for v in result.violations] == ["claim_judge_unparseable"]
    assert len(backend["calls"]) == 2  # llm.call_json's one fresh retry
    assert _cached(conn) == []
    # the next run asks again rather than replaying the bad reply
    backend["reply"] = json.dumps(JUDGE_PASS)
    assert _gate(conn).ok and len(backend["calls"]) == 3


def test_a_json_reply_that_is_not_a_verdict_is_dropped_from_the_cache(
    conn: sqlite3.Connection, backend: dict[str, Any]
) -> None:
    backend["reply"] = json.dumps({"verdict": "looks good"})
    assert [v["kind"] for v in _gate(conn).violations] == ["claim_judge_unparseable"]
    assert _cached(conn) == []


def test_a_fail_verdict_is_cached_like_any_answer(
    conn: sqlite3.Connection, backend: dict[str, Any]
) -> None:
    backend["reply"] = json.dumps(
        {"unsupported": [{"sentence": "x", "claim": "y", "why": "z"}], "verdict": "fail"}
    )
    assert not _gate(conn).ok and not _gate(conn).ok
    assert len(backend["calls"]) == 1 and len(_cached(conn)) == 1


def test_a_judge_call_that_fails_outright_raises(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(stage: str, prompt: str) -> tuple[str, dict[str, Any]]:
        raise RuntimeError("claude -p failed: network")

    monkeypatch.setattr(llm, "_call_claude_cli", down)
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    with pytest.raises(RuntimeError, match="llm call failed"):
        _gate(conn)


def test_tailor_ships_nothing_when_the_judge_cannot_be_reached(
    conn: sqlite3.Connection,
    tailor_env: Callable[..., tuple[FakeLLM, int]],  # noqa: F811
) -> None:
    fake, jid = tailor_env(
        [{"letter": GOOD_LETTER, "bullets_used": GOOD_CITED}],
        [_selection(["Salesforce", "MEDDIC"])],
    )

    def down(prompt: str) -> dict[str, Any]:
        raise RuntimeError("llm call failed after 3 attempts: network")

    fake.replies["claim_judge"] = [down]
    with pytest.raises(RuntimeError, match="llm call failed"):
        tailor.tailor_job(conn, jid)
    outs = list(tailor.OUT_DIR.glob("*/"))
    assert outs and not any((o / "cover.txt").exists() or (o / "cover.pdf").exists() for o in outs)
    app = conn.execute("SELECT stage, cover_path FROM applications WHERE job_id=?", (jid,))
    assert tuple(app.fetchone()) == ("approved", None)
    # no failure event: nothing parks the job, so the next run tries again
    assert conn.execute(q.LAST_COVER_FAILURE_FOR_JOB, {"job_id": jid}).fetchone() is None


# ---- the cover attempt key carries the judge's version ----


def _record_failure(
    conn: sqlite3.Connection, job_id: int, attempt: str, kind: str = "cover_provenance_failed"
) -> None:
    if conn.execute("SELECT 1 FROM companies WHERE id = 1").fetchone() is None:
        conn.execute("INSERT INTO companies (id, name, normalized_name) VALUES (1, 'Acme', 'acme')")
    conn.execute(
        """INSERT INTO jobs (id, company_id, external_id, title, normalized_title,
           company_name, normalized_company, location_bucket, cluster_key, jd_html,
           jd_hash, apply_url, status)
           VALUES (?, 1, ?, 'AE', 'ae', 'Acme', 'acme', 'remote-us', ?, '<p>JD</p>', 'h',
                   'https://x', 'approved')""",
        (job_id, f"e{job_id}", f"k{job_id}"),
    )
    conn.execute(
        q.RECORD_EVENT,
        {
            "job_id": job_id,
            "kind": kind,
            "detail": json.dumps({"attempt": attempt}),
        },
    )


def test_attempt_key_names_the_judge_version() -> None:
    legacy = tailor._attempt_key("h", "c", "1.4", "1.5", "confident", "sr")
    judged = tailor._attempt_key("h", "c", "1.4", "1.5", "confident", "sr", judge="1.0")
    counted = tailor._attempt_key(
        "h", "c", "1.4", "1.5", "confident", "sr", judge="1.0", attempts=5
    )
    assert legacy == "h:c:1.4:1.5:sr:confident"
    assert judged == "h:c:1.4:1.5:sr:cj1.0:confident"
    assert counted == "h:c:1.4:1.5:sr:cj1.0:ca5:confident"


CURRENT = {
    "chash": "c",
    "style": "confident",
    "review": "sr",
    "tailor": "1.4",
    "cover": "1.5",
    "judge": "2.0",
    "attempts": "3",
}
INPUTS = ("h", "c", "1.4", "1.5", "confident", "sr")


@pytest.mark.parametrize("kind", ["cover_provenance_failed", "cover_authenticity_failed"])
def test_a_failure_before_the_judge_existed_still_parks_the_job(
    conn: sqlite3.Connection, kind: str
) -> None:
    # recorded before the judge (and under the three drafts every run wrote
    # then): the judge only adds rejections, so it stands under three drafts
    _record_failure(conn, 1, tailor._attempt_key(*INPUTS), kind)
    assert tailor._unchanged_since_cover_failure(conn, 1, "h", dict(CURRENT))
    # recorded under this judge version, with or without a draft count: parked
    _record_failure(conn, 2, tailor._attempt_key(*INPUTS, judge="2.0"), kind)
    assert tailor._unchanged_since_cover_failure(conn, 2, "h", dict(CURRENT))
    _record_failure(conn, 5, tailor._attempt_key(*INPUTS, judge="2.0", attempts=3), kind)
    assert tailor._unchanged_since_cover_failure(conn, 5, "h", dict(CURRENT))
    # recorded under an older judge version: retried
    _record_failure(conn, 3, tailor._attempt_key(*INPUTS, judge="1.0", attempts=3), kind)
    assert not tailor._unchanged_since_cover_failure(conn, 3, "h", dict(CURRENT))
    # any other input changed: retried, as before
    _record_failure(conn, 4, tailor._attempt_key("h", "OLD", *INPUTS[2:], judge="2.0"), kind)
    assert not tailor._unchanged_since_cover_failure(conn, 4, "h", dict(CURRENT))


@pytest.mark.parametrize("kind", ["cover_provenance_failed", "cover_authenticity_failed"])
def test_a_failure_is_retried_when_more_drafts_are_allowed(
    conn: sqlite3.Connection, kind: str
) -> None:
    five = {**CURRENT, "attempts": "5"}
    # recorded before tailoring.cover_attempts existed (three drafts), with
    # or without a judge version: retried under five
    _record_failure(conn, 1, tailor._attempt_key(*INPUTS, judge="2.0"), kind)
    assert not tailor._unchanged_since_cover_failure(conn, 1, "h", dict(five))
    _record_failure(conn, 2, tailor._attempt_key(*INPUTS), kind)
    assert not tailor._unchanged_since_cover_failure(conn, 2, "h", dict(five))
    _record_failure(conn, 3, tailor._attempt_key(*INPUTS, judge="2.0", attempts=3), kind)
    assert not tailor._unchanged_since_cover_failure(conn, 3, "h", dict(five))
    # recorded under five: parked (the retry happens once)
    _record_failure(conn, 4, tailor._attempt_key(*INPUTS, judge="2.0", attempts=5), kind)
    assert tailor._unchanged_since_cover_failure(conn, 4, "h", dict(five))
    # fewer drafts than already failed retry nothing
    _record_failure(conn, 6, tailor._attempt_key(*INPUTS, judge="2.0", attempts=8), kind)
    assert tailor._unchanged_since_cover_failure(conn, 6, "h", dict(five))


def test_the_judge_prompt_is_versioned_and_strong_tier() -> None:
    pf = llm.PromptFile(claim_judge.PROMPT_NAME)
    assert pf.stage == claim_judge.STAGE == "claim_judge"
    assert pf.version == claim_judge.version()
    assert llm.resolve_model(claim_judge.STAGE) == llm.resolve_model("cover")
    # every placeholder the module fills is in the prompt, and nothing else is left
    _pf, prompt = claim_judge.render_prompt("T", [], CORPUS, target_company="X", job_title="Y")
    for name in ("artifact_kind", "company", "title", "question", "cited", "roles"):
        assert "{" + name + "}" in pf.body and "{" + name + "}" not in prompt
    for name in ("education", "skills", "corpus", "text"):
        assert "{" + name + "}" in pf.body and "{" + name + "}" not in prompt
    assert Path(llm.PROMPTS_DIR / "claim_judge.md").exists()
