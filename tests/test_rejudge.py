"""`jobkit rejudge` / MCP rejudge (pipeline/rejudge.py, claim-judge review
2026-10-10): letters and drafted answers stored before the claim judge existed
were never judged, and staging still fills them. The re-check runs the rules
and then the judge on them, and moves whatever fails to the fail-closed state
a new draft that failed is left in. Offline: the judge is a fake."""

from __future__ import annotations

import copy
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from jobkit import background, llm
from jobkit import db as q
from jobkit.autofill import form_spec
from jobkit.pipeline import rejudge, tailor
from jobkit.validate import claim_judge
from tests.conftest import SpawnRecorder
from tests.test_claims import (
    CORPUS,
    FABRIKAM_JD,
    GOOD_CITED,
    GOOD_LETTER,
    JUDGE_PASS,
    LUMON_LETTER,
    SELECTION,
    FakeLLM,
    flagging_judge,
    judged_text,
)

BLIND = "I also led the sales development team."  # the rules cannot see it
CAPTURED_AT = "2026-09-03 17:00:03"  # before the claim judge existed

# one job's stored AI drafts (claim-judge review 2026-10-10), and one the human gave
Q_MANAGER = "Do you have experience as a people manager of a team of high performing seniors?"
Q_THIRD_PARTY = "Have you ever been hired through Cyberdyne as a third party?"
Q_ACK = "Please acknowledge our privacy notice"
Q_MINE = "Why do you want to work here?"
FIELDS: list[dict[str, Any]] = [
    {"identity": "q_mgr", "label": Q_MANAGER, "type": "multi_value_single_select",
     "required": True, "options": ["Yes", "No"], "compliance_type": None,
     "match": {"kind": "novel"}},
    {"identity": "q_third_party", "label": Q_THIRD_PARTY, "type": "multi_value_single_select",
     "required": True, "options": ["Yes", "No"], "compliance_type": None,
     "match": {"kind": "novel"}},
    {"identity": "q_ack", "label": Q_ACK, "type": "multi_value_single_select",
     "required": True, "options": ["Acknowledge/Confirm"], "compliance_type": None,
     "match": {"kind": "novel"}},
    {"identity": "q_mine", "label": Q_MINE, "type": "textarea", "required": False,
     "options": [], "compliance_type": None, "match": {"kind": "novel"}},
]  # fmt: skip
DRAFTED = {
    "q_mgr": "Yes",
    "q_third_party": "No.",
    "q_ack": "Acknowledged.",
    "q_mine": "Because I said so, in my own words.",
}


def _question_of(prompt: str) -> str:
    return prompt.split("\nFORM QUESTION (", 1)[1].split("\nCITED SOURCES", 1)[0]


def answer_judge(*failing_questions: str) -> Callable[[str], dict[str, Any]]:
    """A fake judge for drafted answers: fails an answer to any of
    `failing_questions`, passes the rest; letters go to flagging_judge(BLIND)."""
    letters = flagging_judge(BLIND)

    def reply(prompt: str) -> dict[str, Any]:
        if "TEXT (cover letter)" in prompt:
            return letters(prompt)
        hit = [qn for qn in failing_questions if qn in _question_of(prompt)]
        if not hit:
            return copy.deepcopy(JUDGE_PASS)
        text = judged_text(prompt).strip()
        return {
            "unsupported": [
                {"sentence": text, "claim": f"answers {hit[0]!r} with {text!r}",
                 "why": "no source states it"}
            ],
            "verdict": "fail",
        }  # fmt: skip

    return reply


def _fake(monkeypatch: pytest.MonkeyPatch, judge: Any = JUDGE_PASS) -> FakeLLM:
    fake = FakeLLM({"claim_judge": [judge]})
    monkeypatch.setattr(llm, "call_json", fake.call_json)
    monkeypatch.setattr(llm, "forget", fake.forget)
    return fake


def _no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*a: Any, **kw: Any) -> Any:
        raise AssertionError("a dry run must make no LLM call")

    monkeypatch.setattr(llm, "call_json", refuse)


def _job(conn: sqlite3.Connection, company: str, ext: str, status: str = "approved") -> int:
    row = conn.execute("SELECT id FROM companies WHERE name = ?", (company,)).fetchone()
    cid = (
        row[0]
        if row
        else conn.execute(
            "INSERT INTO companies (name, normalized_name) VALUES (?, ?)",
            (company, company.lower()),
        ).lastrowid
    )
    jid = conn.execute(
        """INSERT INTO jobs (company_id, external_id, title, normalized_title, company_name,
           normalized_company, location_bucket, cluster_key, jd_html, jd_hash, apply_url, status)
           VALUES (?, ?, 'Director of Revenue Operations', 'director of revenue operations', ?, ?,
                   'remote-us', ?, ?, ?,
                   'https://x', ?)""",
        (cid, ext, company, company.lower(), ext, f"<p>{FABRIKAM_JD}</p>", f"h-{ext}", status),
    ).lastrowid
    assert jid is not None
    return int(jid)


def _letter(
    conn: sqlite3.Connection,
    home: Path,
    jid: int,
    letter: str,
    cited: list[str],
    *,
    judge_version: str | None = None,
    stage: str = "tailored",
) -> Path:
    """A stored, sendable letter as tailoring wrote it before the judge."""
    out = home / "out" / f"fabrikam-payments-director-of-revenue-operations-{jid}-2026-09-03"
    out.mkdir(parents=True)
    (out / "cover.pdf").write_text("%PDF stand-in")
    (out / "cover.txt").write_text(letter)
    (out / "resume.pdf").write_text("%PDF stand-in")
    prov: dict[str, Any] = {
        "job_id": jid,
        "bullets": [{"bullet_id": s["bullet_id"]} for s in SELECTION["selected"]],
        "cover_bullets_used": cited,
    }
    if judge_version is not None:
        prov["claim_judge_prompt"] = judge_version
        prov["cover_fact_check"] = {"ok": True, "violations": []}
    (out / "provenance.json").write_text(json.dumps(prov))
    conn.execute(
        """INSERT INTO applications (job_id, stage, resume_path, cover_path, provenance_path,
           updated_at) VALUES (?, ?, ?, ?, ?, '2026-09-03 17:49:29')""",
        (jid, stage, str(out / "resume.pdf"), str(out / "cover.pdf"), str(out / "provenance.json")),
    )
    conn.commit()
    return out


def _form_spec(
    conn: sqlite3.Connection,
    jid: int,
    *,
    fields: list[dict[str, Any]] = FIELDS,
    drafted: dict[str, str] = DRAFTED,
    human: tuple[str, ...] = ("q_mine",),
) -> None:
    conn.execute(
        """INSERT INTO form_specs (job_id, source, fields, field_count, required_count,
           novel_questions, novel_question_count, drafted_answers, has_assessment,
           friction_score, captured_at)
           VALUES (?, 'api_questions', ?, ?, 3, '[]', 0, ?, 0, 10, ?)""",
        (jid, json.dumps(fields), len(fields), json.dumps(drafted), CAPTURED_AT),
    )
    for ident in human:  # answered by the human through MCP answer_novel, after capture
        conn.execute(
            "INSERT INTO events (job_id, kind, detail) VALUES (?, 'novel_answered', ?)",
            (jid, ident),
        )
    conn.commit()


@pytest.fixture()
def corpus(jobkit_home: Path) -> Path:
    path = jobkit_home / "data" / "career_corpus.yaml"
    path.write_text(yaml.safe_dump(CORPUS))
    return path


def _app(conn: sqlite3.Connection, jid: int) -> tuple[str, str | None]:
    row = conn.execute(
        "SELECT stage, cover_path FROM applications WHERE job_id = ? ORDER BY id DESC", (jid,)
    ).fetchone()
    return str(row[0]), row[1]


def _events(conn: sqlite3.Connection, jid: int, kind: str) -> list[dict[str, Any]]:
    return [
        json.loads(r[0])
        for r in conn.execute(
            "SELECT detail FROM events WHERE job_id = ? AND kind = ? ORDER BY id", (jid, kind)
        )
    ]


def _spec(conn: sqlite3.Connection, jid: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM form_specs WHERE job_id = ?", (jid,)).fetchone()
    assert row is not None
    return row


# ---- letters ----


def test_a_letter_the_judge_fails_is_moved_to_the_fail_closed_state(
    conn: sqlite3.Connection, jobkit_home: Path, corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jid = _job(conn, "Fabrikam Payments", "f1")
    out = _letter(conn, jobkit_home, jid, f"{GOOD_LETTER} {BLIND}", GOOD_CITED)
    fake = _fake(monkeypatch, flagging_judge(BLIND))

    report = rejudge.rejudge(conn)

    # the rules passed it (a blind spot), so the judge read it, with its citations
    (prompt,) = fake.prompts["claim_judge"]
    assert judged_text(prompt).strip() == f"{GOOD_LETTER} {BLIND}"
    assert "b_047 | Haulmark · Regional Sales Manager" in prompt.split("\nCITED SOURCES", 1)[1]
    # the state a letter that failed when drafted is left in (pipeline/tailor.py)
    assert _app(conn, jid) == ("approved", None)
    assert not (out / "cover.pdf").exists() and not (out / "cover.txt").exists()
    assert (out / "resume.pdf").exists()
    rejected = (out / "cover_rejected.md").read_text()
    assert "do not send" in rejected and "NOT FOR SENDING" in rejected
    assert "claim_judge_unsupported" in rejected and BLIND in rejected
    (ev,) = _events(conn, jid, "cover_provenance_failed")
    assert ev["rejudge"] is True and ev["attempt"] == "" and ev["judge"] == claim_judge.version()
    assert ev["violations"][0]["sentence"] == BLIND
    # no attempt key: the next tailor run re-tailors an approved job once, with the judge
    assert not tailor._unchanged_since_cover_failure(conn, jid, "h-f1", {})
    (done,) = _events(conn, jid, rejudge.EVENT)
    assert done["letter"] == str(out / "cover.pdf") and done["letter_ok"] is False
    text = report.render()
    assert "1 failed" in text and "FAILED" in text and "back at approved" in text
    # staging can no longer attach it: nothing is tailored
    assert rejudge.select(conn).jobs == {}


def test_a_letter_the_rules_fail_never_reaches_the_judge(
    conn: sqlite3.Connection, jobkit_home: Path, corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A letter written for another company (Lumon) and attached to a
    Fabrikam Payments job before the stricter rules. The rules fail it; the
    judge is not asked."""
    _job(conn, "Lumon", "g0", status="rejected")
    jid = _job(conn, "Fabrikam Payments", "f1")
    out = _letter(conn, jobkit_home, jid, LUMON_LETTER, ["b_047"])
    fake = _fake(monkeypatch)

    rejudge.rejudge(conn)

    assert "claim_judge" not in fake.prompts
    assert _app(conn, jid) == ("approved", None)
    assert "wrong_company" in (out / "cover_rejected.md").read_text()


def test_a_letter_that_passes_stays_and_is_not_checked_again(
    conn: sqlite3.Connection, jobkit_home: Path, corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jid = _job(conn, "Fabrikam Payments", "f1")
    out = _letter(conn, jobkit_home, jid, GOOD_LETTER, GOOD_CITED)
    fake = _fake(monkeypatch, flagging_judge(BLIND))

    report = rejudge.rejudge(conn)

    assert len(fake.prompts["claim_judge"]) == 1
    assert _app(conn, jid) == ("tailored", str(out / "cover.pdf"))
    assert (out / "cover.pdf").exists() and not (out / "cover_rejected.md").exists()
    assert "1 passed, 0 failed" in report.render()
    (done,) = _events(conn, jid, rejudge.EVENT)
    assert done["letter_ok"] is True
    # the next run skips it: an earlier rejudge passed it under this judge version
    again = rejudge.select(conn)
    assert again.jobs == {}
    assert (jid, f"letter passed an earlier rejudge (judge {claim_judge.version()})") in (
        again.skipped
    )


def test_a_letter_judged_when_drafted_is_skipped(
    conn: sqlite3.Connection, jobkit_home: Path, corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jid = _job(conn, "Fabrikam Payments", "f1")
    _letter(conn, jobkit_home, jid, GOOD_LETTER, GOOD_CITED, judge_version=claim_judge.version())
    old = _job(conn, "Fabrikam Payments", "f2")
    _letter(conn, jobkit_home, old, GOOD_LETTER, GOOD_CITED, judge_version="1.0")
    _no_llm(monkeypatch)
    sel = rejudge.select(conn)
    # a letter judged under an older judge prompt is checked again
    assert list(sel.jobs) == [old]
    assert (jid, f"letter judged when it was drafted (judge {claim_judge.version()})") in (
        sel.skipped
    )


def test_an_unreadable_letter_fails_closed_without_the_judge(
    conn: sqlite3.Connection, jobkit_home: Path, corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jid = _job(conn, "Fabrikam Payments", "f1")
    out = _letter(conn, jobkit_home, jid, GOOD_LETTER, GOOD_CITED)
    (out / "provenance.json").write_text("{not json")
    fake = _fake(monkeypatch)
    dry = rejudge.rejudge(conn, dry_run=True).render()
    assert "missing or unreadable" in dry and "fails closed" in dry

    rejudge.rejudge(conn)

    assert "claim_judge" not in fake.prompts
    assert _app(conn, jid) == ("approved", None)
    (ev,) = _events(conn, jid, "cover_provenance_failed")
    assert ev["violations"][0]["kind"] == rejudge.UNREADABLE


def test_a_letter_retailored_while_judged_is_left_alone(
    conn: sqlite3.Connection, jobkit_home: Path, corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jid = _job(conn, "Fabrikam Payments", "f1")
    out = _letter(conn, jobkit_home, jid, f"{GOOD_LETTER} {BLIND}", GOOD_CITED)
    judge = flagging_judge(BLIND)

    def retailor_meanwhile(prompt: str) -> dict[str, Any]:
        conn.execute("UPDATE applications SET cover_path = 'out/new/cover.pdf'")
        return judge(prompt)

    _fake(monkeypatch, retailor_meanwhile)
    report = rejudge.rejudge(conn)
    assert _app(conn, jid) == ("tailored", "out/new/cover.pdf")
    assert (out / "cover.pdf").exists() and not (out / "cover_rejected.md").exists()
    assert report.changed == [(jid, "letter")]
    assert _events(conn, jid, "cover_provenance_failed") == []


# ---- drafted answers ----


def test_a_drafted_answer_the_judge_fails_goes_back_to_the_human(
    conn: sqlite3.Connection, jobkit_home: Path, corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jid = _job(conn, "Cyberdyne", "r1")
    _form_spec(conn, jid)
    fake = _fake(monkeypatch, answer_judge(Q_MANAGER, Q_THIRD_PARTY))

    report = rejudge.rejudge(conn)

    # every AI draft was judged with its question; the human's answer was not
    prompts = fake.prompts["claim_judge"]
    assert len(prompts) == 3
    assert not any(Q_MINE in _question_of(p) for p in prompts)
    third_party = next(p for p in prompts if Q_THIRD_PARTY in _question_of(p))
    assert judged_text(third_party).strip() == "No."
    assert "(options: ['Yes', 'No'])" in _question_of(third_party)
    assert "(none: the text cites no source)" in third_party  # a stored answer keeps no citations

    spec = _spec(conn, jid)
    drafted = json.loads(spec["drafted_answers"])
    assert drafted == {"q_ack": "Acknowledged.", "q_mine": DRAFTED["q_mine"]}
    open_q = {o["identity"]: o for o in json.loads(spec["novel_questions"])}
    assert set(open_q) == {"q_mgr", "q_third_party"} and spec["novel_question_count"] == 2
    assert open_q["q_third_party"]["flag"] == "unsourced-claim"
    assert open_q["q_third_party"]["fact_check"][0]["kind"] == "claim_judge_unsupported"
    assert open_q["q_third_party"]["label"] == Q_THIRD_PARTY
    fields = {f["identity"]: f for f in json.loads(spec["fields"])}
    assert fields["q_ack"][form_spec.JUDGED_KEY] == claim_judge.version()
    assert form_spec.JUDGED_KEY not in fields["q_third_party"]
    assert form_spec.JUDGED_KEY not in fields["q_mine"]
    assert spec["captured_at"] == CAPTURED_AT  # human answers stay human
    assert spec["friction_score"] == min(100, 4 * 1 + 3 * 2 + 2 * 5)
    failed = _events(conn, jid, "novel_provenance_failed")
    assert {e["field"] for e in failed} == {"q_mgr", "q_third_party"}
    assert all(e["rejudge"] is True for e in failed)
    (done,) = _events(conn, jid, rejudge.EVENT)
    assert done["answers_ok"] == ["q_ack"] and sorted(done["answers_failed"]) == [
        "q_mgr",
        "q_third_party",
    ]
    text = report.render()
    assert "1 passed, 2 failed" in text and "get_novel_questions" in text
    assert f"job {jid}: 1 answer(s) you gave (never judged)" in text

    # the human sees them as open questions, with the reasons
    from jobkit import mcp_server

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    conn.executescript(mcp_server.PINS_SCHEMA)
    conn.execute("INSERT INTO queue_pins (n, job_id) VALUES (1, ?)", (jid,))
    shown = mcp_server.get_novel_questions(1)
    assert "q_third_party" in shown.split("drafted overnight:")[0] and "unsourced-claim" in shown
    assert '"q_third_party"' not in shown.split("drafted overnight:")[1]
    # and nothing is checked twice: the passed draft carries the judge version
    again = rejudge.select(conn)
    assert again.jobs == {}
    assert (jid, f"1 answer(s) judged when drafted (judge {claim_judge.version()})") in (
        again.skipped
    )


def test_an_answer_changed_while_judged_is_left_alone(
    conn: sqlite3.Connection, jobkit_home: Path, corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jid = _job(conn, "Cyberdyne", "r1")
    _form_spec(conn, jid, human=())
    judge = answer_judge(Q_THIRD_PARTY)

    def human_answers_meanwhile(prompt: str) -> dict[str, Any]:
        if Q_THIRD_PARTY in _question_of(prompt):  # the human answers it through answer_novel
            conn.execute(
                "INSERT INTO events (job_id, kind, detail) VALUES (?, 'novel_answered', ?)",
                (jid, "q_third_party"),
            )
        return judge(prompt)

    _fake(monkeypatch, human_answers_meanwhile)
    report = rejudge.rejudge(conn)
    assert json.loads(_spec(conn, jid)["drafted_answers"])["q_third_party"] == "No."
    assert (jid, "answer q_third_party") in report.changed
    assert _events(conn, jid, "novel_provenance_failed") == []


def test_capture_stamps_the_drafts_the_judge_passed(
    conn: sqlite3.Connection, jobkit_home: Path, corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn.execute("INSERT INTO companies (id, name, normalized_name) VALUES (1, 'Hooli', 'w')")
    conn.execute("INSERT INTO boards (id, company_id, ats, token) VALUES (1, 1, 'greenhouse', 'w')")
    jid = conn.execute(
        """INSERT INTO jobs (board_id, company_id, external_id, title, normalized_title,
           company_name, normalized_company, location_bucket, cluster_key, jd_html, jd_hash,
           apply_url, status)
           VALUES (1, 1, 'g1', 'Senior Account Executive', 'sae', 'Hooli', 'hooli',
                   'remote-us', 'k', '<p>JD</p>', 'h', 'https://x', 'queued')"""
    ).lastrowid
    assert jid is not None
    conn.commit()

    class StubMatcher:
        bank: dict[str, Any] = {"scaffolds": []}

        def match(self, label: str, compliance_type: str | None = None, **kw: Any) -> Any:
            return {"kind": "novel"}

    field = {
        "identity": "q_team",
        "label": "How large a team have you led?",
        "type": "textarea",
        "required": False,
        "options": [],
        "compliance_type": None,
    }
    monkeypatch.setattr(form_spec, "BankMatcher", StubMatcher)
    monkeypatch.setitem(form_spec.FETCHERS, "greenhouse", lambda c, t, e: [dict(field)])

    def call_json(c: sqlite3.Connection, stage: str, prompt: str, **kw: Any) -> dict[str, Any]:
        if stage == "claim_judge":
            return JUDGE_PASS
        return {"answer": "At Haulmark I led 9 AEs.", "bullets_used": ["b_052"]}

    monkeypatch.setattr(llm, "call_json", call_json)
    monkeypatch.setattr(llm, "forget", lambda *a: None)
    assert form_spec.capture_form_specs(conn)["drafted"] == 1
    (stored,) = json.loads(_spec(conn, jid)["fields"])
    assert stored[form_spec.JUDGED_KEY] == claim_judge.version()
    # …so the re-check leaves the draft the judge already passed with its citations
    sel = rejudge.select(conn)
    assert (
        sel.jobs == {}
        and (jid, f"1 answer(s) judged when drafted (judge {claim_judge.version()})") in sel.skipped
    )


# ---- scope, the unreachable judge, dry run, MCP + CLI ----


def test_submitted_and_applied_jobs_are_not_checked(
    conn: sqlite3.Connection, jobkit_home: Path, corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _job(conn, "Fabrikam Payments", "f1")
    _letter(conn, jobkit_home, sent, GOOD_LETTER, GOOD_CITED)
    conn.execute("UPDATE applications SET submitted_at = '2026-09-04' WHERE job_id = ?", (sent,))
    applied = _job(conn, "Cyberdyne", "r1", status="applied")
    _form_spec(conn, applied)
    closed = _job(conn, "Cyberdyne", "r2", status="closed")  # can still be staged by job id
    _form_spec(conn, closed)
    conn.commit()
    _no_llm(monkeypatch)
    sel = rejudge.select(conn)
    assert list(sel.jobs) == [closed]
    explicit = rejudge.select(conn, [sent, 99999])
    assert explicit.jobs == {}
    assert (sent, "already submitted: nothing left to stop") in explicit.skipped
    assert (99999, "no such job") in explicit.skipped


def test_an_unreachable_judge_changes_nothing_and_the_cli_exits_1(
    conn: sqlite3.Connection, jobkit_home: Path, corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jid = _job(conn, "Fabrikam Payments", "f1")
    out = _letter(conn, jobkit_home, jid, f"{GOOD_LETTER} {BLIND}", GOOD_CITED)
    _form_spec(conn, jid, human=())

    def down(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("llm call failed after 3 attempts: overloaded")

    monkeypatch.setattr(llm, "call_json", down)
    report = rejudge.rejudge(conn)
    assert _app(conn, jid) == ("tailored", str(out / "cover.pdf"))
    assert json.loads(_spec(conn, jid)["drafted_answers"]) == DRAFTED
    assert len(report.unjudged) == 5 and report.outcomes == []
    assert _events(conn, jid, rejudge.EVENT) == []
    assert "NOT CHECKED — the judge could not be reached; run again" in report.render()
    # the next run tries them again
    assert len(rejudge.select(conn).jobs[jid].answers) == 4

    from jobkit import cli

    monkeypatch.setattr(cli, "_db", lambda: conn)
    res = CliRunner().invoke(cli.app, ["rejudge"])
    assert res.exit_code == 1 and "NOT CHECKED" in res.output


def test_the_dry_run_lists_and_writes_nothing(
    conn: sqlite3.Connection, jobkit_home: Path, corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jid = _job(conn, "Cyberdyne", "r1")
    out = _letter(conn, jobkit_home, jid, f"{GOOD_LETTER} {BLIND}", GOOD_CITED)
    _form_spec(conn, jid)
    _no_llm(monkeypatch)
    before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    text = rejudge.rejudge(conn, dry_run=True).render()
    assert text.startswith("DRY RUN — no LLM call")
    assert "would check 1 cover letter(s) and 3 drafted answer(s) on 1 job(s)" in text
    listed, skipped = text.split("skipped:", 1)
    assert str(out / "cover.pdf") in listed and "q_third_party" in listed
    assert "q_mine" not in listed  # the human's own answer is never judged
    assert "1 answer(s) you gave (never judged)" in skipped
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
    assert _app(conn, jid)[0] == "tailored" and (out / "cover.pdf").exists()


def test_mcp_rejudge_is_a_dry_run_until_confirmed(
    conn: sqlite3.Connection,
    jobkit_home: Path,
    corpus: Path,
    spawned: SpawnRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jobkit import mcp_server

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    jid = _job(conn, "Fabrikam Payments", "f1")
    _letter(conn, jobkit_home, jid, GOOD_LETTER, GOOD_CITED)
    _no_llm(monkeypatch)
    dry = mcp_server.rejudge()
    assert dry.startswith("DRY RUN") and "rejudge(confirm=true)" in dry and spawned.calls == []
    started = mcp_server.rejudge(job_ids=str(jid), confirm=True)
    assert f"started `jobkit rejudge --job-ids {jid}`" in started
    assert spawned.calls == [background.cli_argv("rejudge", "--job-ids", str(jid))]
    assert mcp_server.rejudge(job_ids="x", confirm=True).startswith("nothing re-judged")
    assert _app(conn, jid)[0] == "tailored"


def test_get_artifacts_says_a_rejudged_letter_is_retailored_once(
    conn: sqlite3.Connection, jobkit_home: Path, corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import mcp_server

    jid = _job(conn, "Fabrikam Payments", "f1")
    _letter(conn, jobkit_home, jid, f"{GOOD_LETTER} {BLIND}", GOOD_CITED)
    _fake(monkeypatch, flagging_judge(BLIND))
    rejudge.rejudge(conn)
    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    conn.executescript(mcp_server.PINS_SCHEMA)
    conn.execute("INSERT INTO queue_pins (n, job_id) VALUES (1, ?)", (jid,))
    shown = mcp_server.get_artifacts(1)
    assert "REJECTED by the fact check" in shown and "re-check (rejudge)" in shown
    assert "re-tailors an approved job once" in shown
    assert "claim_judge_unsupported" in mcp_server.review_artifacts(1)


def test_cli_rejudge_dry_run_and_bad_ids(
    conn: sqlite3.Connection, jobkit_home: Path, corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import cli

    jid = _job(conn, "Fabrikam Payments", "f1")
    _letter(conn, jobkit_home, jid, GOOD_LETTER, GOOD_CITED)
    _no_llm(monkeypatch)
    monkeypatch.setattr(cli, "_db", lambda: conn)
    runner = CliRunner()
    res = runner.invoke(cli.app, ["rejudge", "--dry-run"])
    assert res.exit_code == 0 and "DRY RUN" in res.output and f"job {jid}" in res.output
    res = runner.invoke(cli.app, ["rejudge", "--job-ids", "x"])
    assert res.exit_code == 1
    assert _app(conn, jid)[0] == "tailored"


def test_passing_letter_cli_run(
    conn: sqlite3.Connection, jobkit_home: Path, corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import cli

    jid = _job(conn, "Fabrikam Payments", "f1")
    _letter(conn, jobkit_home, jid, GOOD_LETTER, GOOD_CITED)
    _fake(monkeypatch)
    monkeypatch.setattr(cli, "_db", lambda: conn)
    res = CliRunner().invoke(cli.app, ["rejudge", "--job-ids", str(jid)])
    assert res.exit_code == 0, res.output
    assert "1 passed, 0 failed" in res.output
    assert q.CLAIMS_REJUDGED_EVENT in {
        r[0] for r in conn.execute("SELECT kind FROM events WHERE job_id = ?", (jid,))
    }
