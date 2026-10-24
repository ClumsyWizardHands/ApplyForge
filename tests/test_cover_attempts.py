"""Cover-letter drafts (2026-10-23): a letter is fixed at the source, never
repaired in code.

- filters.yaml `tailoring.cover_attempts` sets how many drafts a letter gets
  in all (absent: 5), and every draft runs the whole unchanged check chain
  (the rules, the claim judge, the authenticity passes);
- each regeneration is a new letter, told every violation of every earlier
  draft of the run (kind, the sentence, the rule in plain words), fact-check
  and authenticity failures in separate sections, grouped by draft
  (pipeline/cover_feedback.py);
- the attempt key carries the draft count, so a job that failed under three
  drafts is tailored once more when five are allowed;
- the repair by deletion is gone.

Every LLM call is faked; the candidate, employers and posting are the
invented ones of tests/test_claims.py. Offline."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from jobkit import db as q
from jobkit.config import DEFAULT_COVER_ATTEMPTS, Filters, load_filters
from jobkit.pipeline import cover_feedback, tailor
from tests.test_claims import GOOD_CITED, GOOD_LETTER, flagging_judge, judged_text
from tests.test_targeting import GOOD_SEL, _sel, env  # noqa: F401  (env is a fixture)

REPO = Path(__file__).resolve().parents[1]
TALK = "When can we talk this week?"
# every failing draft ends on a scheduling question the rules flag (soft)
FLAGGED = f"{GOOD_LETTER} {TALK}"
FLAGGED_DRAFT = {"letter": FLAGGED, "bullets_used": GOOD_CITED}
GOOD_DRAFT = {"letter": GOOD_LETTER, "bullets_used": GOOD_CITED}


def _set_attempts(home: Path, value: object) -> None:
    path = home / "config" / "filters.yaml"
    raw = yaml.safe_load(path.read_text()) or {}
    if value is None:
        raw.pop("tailoring", None)
    else:
        raw["tailoring"] = {"cover_attempts": value}
    path.write_text(yaml.safe_dump(raw, sort_keys=False))


def _stage(conn: sqlite3.Connection, jid: int) -> str:
    row = conn.execute(
        "SELECT stage FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1", (jid,)
    ).fetchone()
    return str(row[0])


def _failure_key(conn: sqlite3.Connection, jid: int) -> str:
    ev = conn.execute(q.LAST_COVER_FAILURE_FOR_JOB, {"job_id": jid}).fetchone()
    assert ev is not None
    return str(json.loads(ev["detail"])["attempt"])


# ---- the knob ----


def test_cover_attempts_defaults_to_five() -> None:
    assert DEFAULT_COVER_ATTEMPTS == 5
    assert Filters(raw={}).cover_attempts == 5
    assert Filters(raw={"tailoring": None}).cover_attempts == 5
    assert Filters(raw={"tailoring": {"cover_attempts": None}}).cover_attempts == 5
    assert Filters(raw={"tailoring": {"cover_attempts": 2}}).cover_attempts == 2


@pytest.mark.parametrize("bad", [0, -1, "5", 2.5, True])
def test_a_bad_cover_attempts_fails_at_load(bad: object) -> None:
    with pytest.raises(ValueError, match="tailoring.cover_attempts"):
        Filters(raw={"tailoring": {"cover_attempts": bad}})


def test_the_example_configs_carry_the_default() -> None:
    for name in ("filters.example.yaml", "filters.sales.example.yaml"):
        raw = yaml.safe_load((REPO / "config" / name).read_text())
        assert raw["tailoring"]["cover_attempts"] == 5, name


def test_intake_copies_the_tailoring_block_from_the_template() -> None:
    from jobkit.onboarding import intake

    blocks = intake._template_blocks((REPO / "config" / "filters.example.yaml").read_text())
    block = blocks["tailoring"]
    assert "cover_attempts: 5" in block and "Absent = 5" in block


# ---- the setting is honoured, every draft fully checked ----


@pytest.mark.parametrize(("attempts", "failing"), [(4, 3), (2, 1), (1, 0)])
def test_a_letter_ships_on_the_draft_that_passes(
    conn: sqlite3.Connection,
    env: Callable[..., Any],  # noqa: F811
    jobkit_home: Path,
    attempts: int,
    failing: int,
) -> None:
    _set_attempts(jobkit_home, attempts)
    fake, jid, _ = env([_sel(*GOOD_SEL)], letters=[FLAGGED_DRAFT] * failing + [GOOD_DRAFT])
    paths = tailor.tailor_job(conn, jid)
    assert len(fake.prompts["cover"]) == failing + 1
    # the drafts the rules failed never reached the judge; the passing one did,
    # and the critic read it
    assert [judged_text(p).strip() for p in fake.prompts["claim_judge"]] == [GOOD_LETTER]
    assert any(GOOD_LETTER in p for p in fake.prompts["critic"])
    out = Path(paths["provenance"]).parent
    assert (out / "cover.txt").read_text() == GOOD_LETTER
    assert not (out / "cover_rejected.md").exists()
    assert _stage(conn, jid) == "tailored"
    prov = json.loads(Path(paths["provenance"]).read_text())
    assert "cover_repair" not in prov


@pytest.mark.parametrize("attempts", [1, 2, 3, 6])
def test_a_letter_failing_every_draft_is_rejected_after_the_set_count(
    conn: sqlite3.Connection,
    env: Callable[..., Any],  # noqa: F811
    jobkit_home: Path,
    attempts: int,
) -> None:
    _set_attempts(jobkit_home, attempts)
    fake, jid, _ = env([_sel(*GOOD_SEL)], letters=[FLAGGED_DRAFT])
    paths = tailor.tailor_job(conn, jid)
    assert len(fake.prompts["cover"]) == attempts
    out = Path(paths["provenance"]).parent
    rejected = (out / "cover_rejected.md").read_text()
    assert TALK in rejected and "Repair" not in rejected and "repair" not in rejected
    assert not (out / "cover.txt").exists()
    assert _stage(conn, jid) == "approved"
    assert f":ca{attempts}:" in _failure_key(conn, jid)


def test_absent_means_five_drafts(
    conn: sqlite3.Connection,
    env: Callable[..., Any],  # noqa: F811
    jobkit_home: Path,
) -> None:
    _set_attempts(jobkit_home, None)
    assert "tailoring" not in load_filters().raw
    fake, jid, _ = env([_sel(*GOOD_SEL)], letters=[FLAGGED_DRAFT])
    tailor.tailor_job(conn, jid)
    assert len(fake.prompts["cover"]) == 5
    assert ":ca5:" in _failure_key(conn, jid)


def test_every_draft_goes_through_the_claim_judge(
    conn: sqlite3.Connection,
    env: Callable[..., Any],  # noqa: F811
    jobkit_home: Path,
) -> None:
    # the rules pass these drafts; the judge fails the first two on a
    # sentence, and the third (without it) passes
    _set_attempts(jobkit_home, 5)
    claim = "Those were hard numbers to earn."
    judged = {"letter": f"{GOOD_LETTER} {claim}", "bullets_used": GOOD_CITED}
    fake, jid, _ = env([_sel(*GOOD_SEL)], letters=[judged, judged, GOOD_DRAFT])
    fake.replies["claim_judge"] = [flagging_judge(claim)]
    paths = tailor.tailor_job(conn, jid)
    assert len(fake.prompts["cover"]) == 3
    assert len(fake.prompts["claim_judge"]) == 3
    assert Path(paths["cover"]).exists()
    # the judge's violation is fed back like any rule's
    second = fake.prompts["cover"][1]
    assert "claim_judge_unsupported" in second
    assert json.dumps(claim) in second
    assert cover_feedback.RULES["claim_judge_unsupported"] in second


def test_every_draft_goes_through_the_authenticity_passes(
    conn: sqlite3.Connection,
    env: Callable[..., Any],  # noqa: F811
    jobkit_home: Path,
) -> None:
    _set_attempts(jobkit_home, 3)
    fake, jid, _ = env([_sel(*GOOD_SEL)], letters=[GOOD_DRAFT])

    def critic(prompt: str) -> dict[str, Any]:
        # fails every letter draft; passes the resume selection
        if "usually outruns the forecast first" in prompt:
            return {"verdict": "fail", "notes": "reads like a template", "tells": []}
        return {"verdict": "pass", "notes": "", "tells": []}

    fake.replies["critic"] = [critic]
    paths = tailor.tailor_job(conn, jid)
    assert len(fake.prompts["cover"]) == 3
    assert len(fake.prompts["critic"]) >= 3
    out = Path(paths["provenance"]).parent
    assert "after 3 drafts" in (out / "cover_critique.md").read_text()
    second = fake.prompts["cover"][1]
    assert "Draft 1 FAILED AUTHENTICITY REVIEW" in second
    assert "reads like a template" in second
    assert cover_feedback.RULES["critic"] in second
    # a draft that passed the fact check: its citations (ids only) go forward
    assert f"It cited {', '.join(GOOD_CITED)}, and those citations passed" in second
    assert GOOD_LETTER not in second  # only what failed is passed on
    # the same note from drafts 1 and 2 is listed once, with both drafts
    third = fake.prompts["cover"][2]
    assert third.count("reads like a template") == 1
    assert "failed in drafts: 1, 2" in third
    assert "Draft 2 FAILED AUTHENTICITY REVIEW (0 new), and again: item 1 above." in third


# ---- what a regeneration is told ----


def test_the_regeneration_carries_the_exact_violations(
    conn: sqlite3.Connection,
    env: Callable[..., Any],  # noqa: F811
    jobkit_home: Path,
) -> None:
    _set_attempts(jobkit_home, 2)
    fake, jid, _ = env([_sel(*GOOD_SEL)], letters=[FLAGGED_DRAFT, GOOD_DRAFT])
    tailor.tailor_job(conn, jid)
    first, second = fake.prompts["cover"]
    assert "YOUR EARLIER DRAFTS FAILED" not in first
    assert "YOUR EARLIER DRAFTS FAILED (1 draft so far)" in second
    assert "Draft 1 FAILED THE FACT CHECK (1):" in second
    # kind, sentence and the rule in plain words, and the checker's own detail
    assert "1. unsourced_experience_claim" in second
    assert f"sentence: {json.dumps(TALK)}" in second
    assert f"rule: {cover_feedback.RULES['unsourced_experience_claim']}" in second
    assert "detail: " in second and "talk" in second.split("detail: ", 1)[1]
    # a new letter without the failed sentences; the draft itself is not passed
    # on (a sentence no check flagged may be a claim the checks cannot see),
    # and neither are a fact-check failure's citations
    assert cover_feedback.NEW_LETTER in second
    assert FLAGGED not in second and "It cited" not in second
    assert "name no company other than Fabrikam Payments" in second


def test_feedback_is_cumulative_across_drafts(
    conn: sqlite3.Connection,
    env: Callable[..., Any],  # noqa: F811
    jobkit_home: Path,
) -> None:
    # draft 1 fails the rules (kind A), draft 2 the claim judge (kind B): the
    # third prompt carries both, so a fresh prompt cannot bring back draft 1's
    _set_attempts(jobkit_home, 5)
    claim = "Those were hard numbers to earn."
    judged = {"letter": f"{GOOD_LETTER} {claim}", "bullets_used": GOOD_CITED}
    fake, jid, _ = env([_sel(*GOOD_SEL)], letters=[FLAGGED_DRAFT, judged, GOOD_DRAFT])
    fake.replies["claim_judge"] = [flagging_judge(claim)]
    paths = tailor.tailor_job(conn, jid)
    assert Path(paths["cover"]).exists()
    third = fake.prompts["cover"][2]
    assert "YOUR EARLIER DRAFTS FAILED (2 drafts so far)" in third
    assert "Draft 1 FAILED THE FACT CHECK (1):" in third
    assert "Draft 2 FAILED THE FACT CHECK (1):" in third
    assert "unsourced_experience_claim" in third and json.dumps(TALK) in third
    assert "claim_judge_unsupported" in third and json.dumps(claim) in third
    assert third.index(json.dumps(TALK)) < third.index(json.dumps(claim))


def test_an_authenticity_failure_keeps_the_fact_check_list(
    conn: sqlite3.Connection,
    env: Callable[..., Any],  # noqa: F811
    jobkit_home: Path,
) -> None:
    # draft 1 fails the fact check, draft 2 passes it and fails the critic:
    # draft 3 is still told draft 1's fact violation, in its own section
    _set_attempts(jobkit_home, 5)
    fake, jid, _ = env([_sel(*GOOD_SEL)], letters=[FLAGGED_DRAFT, GOOD_DRAFT, GOOD_DRAFT])
    letters_read = {"n": 0}

    def critic(prompt: str) -> dict[str, Any]:
        if "usually outruns the forecast first" in prompt:
            letters_read["n"] += 1
            if letters_read["n"] == 1:
                return {"verdict": "fail", "notes": "reads like a template", "tells": []}
        return {"verdict": "pass", "notes": "", "tells": []}

    fake.replies["critic"] = [critic]
    paths = tailor.tailor_job(conn, jid)
    assert len(fake.prompts["cover"]) == 3
    assert Path(paths["cover"]).exists()
    third = fake.prompts["cover"][2]
    facts = third.index("FACT CHECK FAILURES")
    authenticity = third.index("AUTHENTICITY FAILURES")
    assert facts < third.index(json.dumps(TALK)) < authenticity
    assert authenticity < third.index("reads like a template")
    assert "Draft 1 FAILED THE FACT CHECK" in third
    assert "Draft 2 FAILED AUTHENTICITY REVIEW" in third
    assert GOOD_LETTER not in third


def test_feedback_lists_every_violation_with_its_rule() -> None:
    violations = [
        {
            "kind": "fabricated_employer",
            "detail": "Acme is the company applied to",
            "sentence": "S1.",
        },
        {"kind": "missing_citations", "detail": "no bullet cited"},
        {"kind": "a_kind_with_no_entry", "detail": "d", "sentence": "S3."},
    ]
    history = cover_feedback.DraftHistory("Acme")
    assert history.render() == ""
    history.add(1, cover_feedback.FACT_CHECK, violations, cited=["b_1"])
    text = history.render()
    assert "Draft 1 FAILED THE FACT CHECK (3):" in text
    assert "1. fabricated_employer" in text and 'sentence: "S1."' in text
    assert cover_feedback.RULES["fabricated_employer"] in text
    assert "2. missing_citations" in text and "(not tied to one sentence)" in text
    assert "3. a_kind_with_no_entry" in text and cover_feedback.GENERIC_RULE in text
    assert "b_1" not in text  # a fact-check failure's citations are not carried
    assert "AUTHENTICITY FAILURES" not in text
    assert text.rstrip().endswith("the candidate's own employers.")


def test_feedback_lists_a_repeated_violation_once() -> None:
    a = {"kind": "fabricated_number", "detail": "12: in no cited bullet", "sentence": "S1."}
    b = {"kind": "unsourced_tool", "detail": "Zapflow", "sentence": "S2."}
    history = cover_feedback.DraftHistory("Acme")
    history.add(1, cover_feedback.FACT_CHECK, [a])
    history.add(2, cover_feedback.FACT_CHECK, [b, a])
    history.add(3, cover_feedback.FACT_CHECK, [a])
    text = history.render()
    assert text.count('"S1."') == 1 and text.count('"S2."') == 1
    assert "Draft 1 FAILED THE FACT CHECK (1):" in text
    assert "Draft 2 FAILED THE FACT CHECK (1 new), and again: item 1 above:" in text
    assert "Draft 3 FAILED THE FACT CHECK (0 new), and again: item 1 above." in text
    assert "failed in drafts: 1, 2, 3" in text
    assert "YOUR EARLIER DRAFTS FAILED (3 drafts so far)" in text


def test_feedback_marks_a_sentence_the_checker_cut() -> None:
    long = ("I built the whole outbound motion from nothing " * 5)[: cover_feedback.SENTENCE_CUT]
    history = cover_feedback.DraftHistory("Acme")
    history.add(
        1,
        cover_feedback.FACT_CHECK,
        [{"kind": "k", "sentence": long}, {"kind": "j", "sentence": "Short."}],
    )
    text = history.render()
    assert f"sentence (its first {cover_feedback.SENTENCE_CUT} characters; it may go on): " in text
    assert f"{json.dumps(long.strip())} …" in text
    assert 'sentence: "Short."' in text


def test_every_kind_seen_in_rejected_letters_has_a_plain_rule() -> None:
    for kind in (
        "unsourced_experience_claim",
        "unsourced_proper_noun",
        "fabricated_employer",
        "fabricated_number",
        "claim_judge_unsupported",
        "unsourced_tool",
        "cross_role_attribution",
        "dropped_period",
        "unsourced_title_mention",
        "never_claim_title",
        "added_qualifier",
        "claimed_job_title",
    ):
        assert cover_feedback.rule_for(kind) != cover_feedback.GENERIC_RULE, kind


def test_no_rule_steers_the_writer_to_lowercase_a_name() -> None:
    # the proper-noun check keys on capitals: "account associates" in
    # lowercase keeps the posting's claim and passes it, so the feedback must
    # never suggest lowercasing (Hard Rule 8)
    for kind, rule in [*cover_feedback.RULES.items(), ("generic", cover_feedback.GENERIC_RULE)]:
        assert "lowercase" not in rule.lower(), kind
        assert "lower case" not in rule.lower(), kind
    assert "any capitalization" in cover_feedback.RULES["unsourced_proper_noun"]


def test_the_number_rule_names_the_company_fact_exception() -> None:
    rule = cover_feedback.RULES["fabricated_number"]
    assert "company-fact sentence" in rule and "in order" in rule


# ---- the attempt key: a job that failed under three drafts is retried once ----


def test_a_failure_under_fewer_drafts_is_retailored_once(
    conn: sqlite3.Connection,
    env: Callable[..., Any],  # noqa: F811
    jobkit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_attempts(jobkit_home, 3)
    fake, jid, _ = env([_sel(*GOOD_SEL)], letters=[FLAGGED_DRAFT])
    tailor.tailor_job(conn, jid)
    assert len(fake.prompts["cover"]) == 3
    key = _failure_key(conn, jid)
    assert ":ca3:" in key

    called: list[int] = []
    monkeypatch.setattr(tailor, "tailor_job", lambda c, job_id: called.append(job_id) or {})
    # same settings: parked
    assert tailor.tailor_approved(conn) == 0 and called == []
    # more drafts allowed: retried
    _set_attempts(jobkit_home, 5)
    assert tailor.tailor_approved(conn) == 1 and called == [jid]


def test_a_failure_recorded_before_the_setting_is_retailored_once(
    conn: sqlite3.Connection,
    env: Callable[..., Any],  # noqa: F811
    jobkit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_tailor_job = tailor.tailor_job
    _set_attempts(jobkit_home, 3)
    fake, jid, _ = env([_sel(*GOOD_SEL)], letters=[FLAGGED_DRAFT])
    real_tailor_job(conn, jid)
    ev = conn.execute(
        "SELECT id, detail FROM events WHERE job_id = ? AND kind = 'cover_provenance_failed' "
        "ORDER BY id DESC LIMIT 1",
        (jid,),
    ).fetchone()
    detail = json.loads(ev["detail"])
    # the key every failure carried before tailoring.cover_attempts existed
    legacy = detail["attempt"].replace(":ca3:", ":")
    conn.execute(
        "UPDATE events SET detail = ? WHERE id = ?",
        (json.dumps({**detail, "attempt": legacy}), ev["id"]),
    )
    conn.commit()
    called: list[int] = []
    monkeypatch.setattr(tailor, "tailor_job", lambda c, job_id: called.append(job_id) or {})
    # under three drafts it still stands
    assert tailor.tailor_approved(conn) == 0 and called == []
    # under the default five it is retried
    _set_attempts(jobkit_home, None)
    assert tailor.tailor_approved(conn) == 1 and called == [jid]
    # that run fails again and records ca5: parked from then on
    monkeypatch.setattr(tailor, "tailor_job", real_tailor_job)
    real_tailor_job(conn, jid)
    assert ":ca5:" in _failure_key(conn, jid)
    monkeypatch.setattr(tailor, "tailor_job", lambda c, job_id: called.append(job_id) or {})
    assert tailor.tailor_approved(conn) == 0 and called == [jid]


# ---- the repair by deletion is gone ----


def test_the_repair_module_is_gone() -> None:
    name = "letter" + "_repair"
    assert importlib.util.find_spec(f"jobkit.pipeline.{name}") is None
    assert not hasattr(tailor, name)
    roots = [REPO / "src", REPO / "tests", REPO / "prompts", REPO / "docs", REPO / "config"]
    hits = [
        str(p)
        for root in roots
        for p in root.rglob("*")
        if p.suffix in {".py", ".md", ".yaml", ".yml"} and name in p.read_text(errors="ignore")
    ]
    assert hits == [], hits
