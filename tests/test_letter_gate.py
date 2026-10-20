"""A cover letter the claim judge never read is not final (review 2026-10-10,
pipeline/letter_gate.py). A letter tailored before the judge existed, or with
no record that the judge read it, is never attached or staged: staging halts
on it before the browser touches anything, the nightly run skips it, it is
marked once with a `cover_needs_retailor` event, and the digest (with MCP
get_queue and schedule status, through pipeline/halts.py) tells the human to
re-tailor it. Re-tailoring (a letter drafted with the judge in the loop) or a
`jobkit rejudge` pass of that very letter releases it.

Offline: sandbox DB, fake pages, no LLM, no browser."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from jobkit import db as q
from jobkit.autofill import answer_bank, browser
from jobkit.pipeline import halts, letter_gate, nightly
from jobkit.pipeline.digest import write_digest
from tests.test_filter import _insert
from tests.test_staging_browser import FakeFrame, FakePage

JUDGE = "1.0"


@pytest.fixture(autouse=True)
def bank(jobkit_home: Path) -> Iterator[None]:
    """stage_job reads the answer bank first: a minimal fixture one."""
    (jobkit_home / "data" / "answer_bank.yaml").write_text(
        yaml.safe_dump({"identity": {"first_name": "Pat", "email": "pat@example.com"}})
    )
    answer_bank.load_bank.cache_clear()
    yield
    answer_bank.load_bank.cache_clear()


def _job(conn: sqlite3.Connection, ext: str, company: str = "Acme") -> int:
    jid = _insert(conn, external_id=ext, title="Account Executive", company_name=company)
    conn.execute("UPDATE jobs SET status='approved' WHERE id=?", (jid,))
    conn.commit()
    return jid


def _letter(
    conn: sqlite3.Connection,
    home: Path,
    jid: int,
    *,
    judge: str | None = None,
    ok: bool = True,
    prov: bool = True,
    prov_job: int | None = None,
    tailored_at: str = "datetime('now', '-1 hour')",
    folder: str = "",
) -> Path:
    """A tailored application with a letter, as tailoring wrote it: before the
    claim judge (judge=None) or with the judge in the loop."""
    out = home / "out" / (folder or f"acme-account-executive-{jid}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "cover.pdf").write_text("%PDF stand-in")
    (out / "cover.txt").write_text("Dear Acme, ...")
    (out / "resume.pdf").write_text("%PDF stand-in")
    if prov:
        record: dict[str, Any] = {"job_id": prov_job or jid, "cover_bullets_used": ["b_001"]}
        if judge is not None:
            record["claim_judge_prompt"] = judge
            record["cover_fact_check"] = {"ok": ok, "violations": []}
        (out / "provenance.json").write_text(json.dumps(record))
    conn.execute(
        "INSERT INTO applications (job_id, stage, resume_path, cover_path, provenance_path, "
        f"updated_at) VALUES (?, 'tailored', ?, ?, ?, {tailored_at})",
        (jid, str(out / "resume.pdf"), str(out / "cover.pdf"), str(out / "provenance.json")),
    )
    conn.commit()
    return out


def _app(conn: sqlite3.Connection, jid: int) -> sqlite3.Row:
    row = conn.execute(q.APPLICATION_LETTER_FOR_JOB, {"job_id": jid}).fetchone()
    assert row is not None
    return row  # type: ignore[no-any-return]


def _events(conn: sqlite3.Connection, jid: int) -> list[dict[str, Any]]:
    return [
        json.loads(r["detail"])
        for r in conn.execute(
            "SELECT detail FROM events WHERE job_id=? AND kind=? ORDER BY id",
            (jid, letter_gate.EVENT),
        )
    ]


# ---- what counts as judged ----------------------------------------------------------


def test_a_letter_tailored_before_the_judge_is_not_final(
    conn: sqlite3.Connection, jobkit_home: Path
) -> None:
    jid = _job(conn, "old")
    _letter(conn, jobkit_home, jid)
    assert letter_gate.why_not_final(conn, _app(conn, jid)) == (
        "tailored before the claim judge read cover letters"
    )


def test_a_letter_the_judge_passed_when_drafted_is_final(
    conn: sqlite3.Connection, jobkit_home: Path
) -> None:
    jid = _job(conn, "new")
    _letter(conn, jobkit_home, jid, judge=JUDGE)
    assert letter_gate.why_not_final(conn, _app(conn, jid)) is None
    assert letter_gate.check_job(conn, jid) is None


@pytest.mark.parametrize(
    ("kw", "why"),
    [
        ({"prov": False}, "is missing or unreadable"),
        ({"judge": JUDGE, "ok": False}, "its fact check did not pass"),
        ({"judge": JUDGE, "prov_job": 999}, "is not this letter's own record"),
    ],
)
def test_no_record_of_a_passing_judge_is_not_final(
    conn: sqlite3.Connection, jobkit_home: Path, kw: dict[str, Any], why: str
) -> None:
    jid = _job(conn, "x")
    _letter(conn, jobkit_home, jid, **kw)
    got = letter_gate.check_job(conn, jid)
    assert got is not None and why in got[0], got


def test_a_provenance_record_from_another_folder_is_not_the_letters(
    conn: sqlite3.Connection, jobkit_home: Path
) -> None:
    jid = _job(conn, "moved")
    out = _letter(conn, jobkit_home, jid)
    other = jobkit_home / "out" / "elsewhere"
    other.mkdir()
    (other / "provenance.json").write_text(
        json.dumps({"job_id": jid, "claim_judge_prompt": JUDGE, "cover_fact_check": {"ok": True}})
    )
    conn.execute(
        "UPDATE applications SET provenance_path=? WHERE job_id=?",
        (str(other / "provenance.json"), jid),
    )
    conn.commit()
    got = letter_gate.check_job(conn, jid)
    assert got is not None and "not this letter's own record" in got[0]
    assert (out / "cover.pdf").exists()  # the gate never touches the letter's files


def test_a_rejudge_pass_of_this_letter_since_it_was_tailored_releases_it(
    conn: sqlite3.Connection, jobkit_home: Path
) -> None:
    jid = _job(conn, "rj")
    out = _letter(conn, jobkit_home, jid)
    cover = str(out / "cover.pdf")

    def rejudged(letter: str, ok: int, at: str) -> None:
        detail = {"judge": JUDGE, "letter": letter, "letter_ok": ok}
        conn.execute(
            f"INSERT INTO events (job_id, kind, detail, at) VALUES (?, ?, ?, {at})",
            (jid, q.CLAIMS_REJUDGED_EVENT, json.dumps(detail)),
        )
        conn.commit()

    rejudged(cover, 0, "datetime('now')")  # failed: not a pass
    rejudged(cover, 1, "datetime('now', '-2 hours')")  # before it was tailored
    rejudged(str(out / "other.pdf"), 1, "datetime('now')")  # another letter
    assert letter_gate.check_job(conn, jid) is not None
    rejudged(cover, 1, "datetime('now')")
    assert letter_gate.check_job(conn, jid) is None


def test_no_letter_is_nothing_to_gate(conn: sqlite3.Connection) -> None:
    jid = _job(conn, "resume-only")
    conn.execute(
        "INSERT INTO applications (job_id, stage, resume_path) VALUES (?, 'tailored', 'r.pdf')",
        (jid,),
    )
    conn.commit()
    assert letter_gate.check_job(conn, jid) is None
    assert letter_gate.check_job(conn, 9999) is None  # no application at all


# ---- staging never attaches it --------------------------------------------------------


def test_staging_halts_before_the_browser_touches_anything(
    conn: sqlite3.Connection, jobkit_home: Path
) -> None:
    jid = _job(conn, "stage")
    _letter(conn, jobkit_home, jid)
    page = FakePage([FakeFrame(url="https://x.com", ids=("email",))])
    for _ in range(2):
        r = browser.stage_job(conn, page, jid)
        assert r["status"] == "needs_human" and r["reason"] == letter_gate.REASON, r
        assert r["detail"]["why"] == "tailored before the claim judge read cover letters"
    assert page.visited == [], "the browser never navigated"
    runs = conn.execute(
        "SELECT status, abort_reason, filled_fields FROM autofill_runs WHERE job_id=?", (jid,)
    ).fetchall()
    assert [tuple(r) for r in runs] == [("needs_human", letter_gate.REASON, "[]")] * 2
    # marked once, however often staging meets it
    (event,) = _events(conn, jid)
    assert event["cover"].endswith("cover.pdf") and "before the claim judge" in event["why"]


def test_staging_a_judged_letter_goes_on_to_the_form(
    conn: sqlite3.Connection, jobkit_home: Path
) -> None:
    jid = _job(conn, "judged")
    conn.execute(
        "UPDATE jobs SET apply_url='https://jobs.ashbyhq.com/acme/x/application' WHERE id=?",
        (jid,),
    )
    _letter(conn, jobkit_home, jid, judge=JUDGE)
    page = FakePage([FakeFrame(url="https://jobs.ashbyhq.com/acme/x/application")])
    r = browser.stage_job(conn, page, jid)
    # past the letter gate: the next check (no form spec captured) stops it
    assert r["reason"] == "no-form-spec-captured", r
    assert _events(conn, jid) == []


# ---- nightly skips it, the digest names it ------------------------------------------------


def _record_stage_jobs(calls: list[list[int]]) -> Any:
    def fake(
        c: sqlite3.Connection, ids: list[int], keep_open: bool = True, carry_forward: bool = True
    ) -> list[dict[str, Any]]:
        calls.append(list(ids))
        return [{"job_id": j, "status": "staged"} for j in ids]

    return fake


def test_nightly_skips_an_unjudged_letter_and_names_it(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _job(conn, "old", company="Umbrella")
    new = _job(conn, "new")
    _letter(conn, jobkit_home, old)
    _letter(conn, jobkit_home, new, judge=JUDGE)
    calls: list[list[int]] = []
    monkeypatch.setattr(nightly, "stage_jobs", _record_stage_jobs(calls))
    summary = nightly.stage_tailored(conn)
    assert calls == [[new]], "only the judged letter is staged"
    assert summary.startswith("1 job(s): 1 staged. Needs you: ")
    assert (
        f"job {old} Umbrella: {letter_gate.REASON} (tailored before the claim judge read cover "
        f"letters); not staged until re-tailored (MCP retailor, `jobkit tailor --job-id {old}`)"
    ) in summary
    assert len(_events(conn, old)) == 1 and _events(conn, new) == []

    # only the held job left: nothing staged, no browser, still named, marked once
    conn.execute("UPDATE jobs SET status='applied' WHERE id=?", (new,))
    conn.commit()
    calls.clear()
    again = nightly.stage_tailored(conn)
    assert calls == []
    assert again.startswith("nothing new to stage. Needs you: ") and f"job {old} Umbrella" in again
    assert len(_events(conn, old)) == 1


def test_the_digest_tells_the_human_to_retailor_and_forgets_it_once_judged(
    conn: sqlite3.Connection, jobkit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jid = _job(conn, "dig", company="Umbrella")
    out = _letter(conn, jobkit_home, jid)
    digest = write_digest(conn).read_text()
    assert "### ⚠ Stopped before filling anything: waiting on you (1)" in digest
    assert (
        f"- job {jid} · Umbrella — Account Executive: {letter_gate.REASON}:tailored before the "
        "claim judge read cover letters (its cover letter was never read by the claim judge"
    ) in digest
    assert "re-tailor it (MCP retailor, `jobkit tailor --job-id <id>`)" in digest
    write_digest(conn)
    assert len(_events(conn, jid)) == 1, "marked once across digests"
    # get_queue's line and schedule status read the same list
    line = halts.queue_line(conn)
    assert line is not None and f"job {jid} Umbrella ({letter_gate.REASON}:" in line
    assert f"job {jid} · Umbrella — Account Executive: {letter_gate.REASON}" in "\n".join(
        halts.report_lines(conn)
    )

    # re-tailored with the judge in the loop: released everywhere
    (out / "provenance.json").write_text(
        json.dumps({"job_id": jid, "claim_judge_prompt": JUDGE, "cover_fact_check": {"ok": True}})
    )
    conn.execute("UPDATE applications SET updated_at=datetime('now') WHERE job_id=?", (jid,))
    conn.commit()
    assert halts.waiting(conn) == []
    assert "## Staging" not in write_digest(conn).read_text()
    calls: list[list[int]] = []
    monkeypatch.setattr(nightly, "stage_jobs", _record_stage_jobs(calls))
    assert nightly.stage_tailored(conn) == "1 job(s): 1 staged"
    assert calls == [[jid]]


def test_a_letter_halt_is_listed_only_while_the_letter_is_unjudged(
    conn: sqlite3.Connection, jobkit_home: Path
) -> None:
    jid = _job(conn, "halted")
    out = _letter(conn, jobkit_home, jid)
    browser.stage_job(conn, FakePage([FakeFrame(url="https://x.com")]), jid)
    (h,) = halts.waiting(conn)
    assert h.job_id == jid and h.reason.startswith(f"{letter_gate.REASON}:")
    # judged now (a rejudge pass of this letter): the stale halt row waits on nothing
    conn.execute(
        "INSERT INTO events (job_id, kind, detail) VALUES (?, ?, ?)",
        (
            jid,
            q.CLAIMS_REJUDGED_EVENT,
            json.dumps({"judge": JUDGE, "letter": str(out / "cover.pdf"), "letter_ok": 1}),
        ),
    )
    conn.commit()
    assert halts.waiting(conn) == []


def test_a_form_staged_with_an_unjudged_letter_is_not_counted_ready(
    conn: sqlite3.Connection, jobkit_home: Path
) -> None:
    jid = _job(conn, "was-staged")
    _letter(conn, jobkit_home, jid)
    conn.execute(
        "INSERT INTO autofill_runs (job_id, status, filled_fields) VALUES (?, 'staged', ?)",
        (jid, json.dumps([{"identity": "email"}])),
    )
    conn.commit()
    lines = "\n".join(halts.digest_lines(conn))
    assert "waiting for your review" not in lines
    assert f"job {jid} · Acme — Account Executive: {letter_gate.REASON}" in lines


def test_closed_rejected_and_submitted_jobs_are_not_listed(
    conn: sqlite3.Connection, jobkit_home: Path
) -> None:
    ids = [_job(conn, f"j{i}") for i in range(3)]
    for jid in ids:
        _letter(conn, jobkit_home, jid)
    conn.execute("UPDATE jobs SET status='rejected' WHERE id=?", (ids[0],))
    conn.execute("UPDATE jobs SET closed_at=datetime('now') WHERE id=?", (ids[1],))
    conn.execute("UPDATE applications SET submitted_at=datetime('now') WHERE job_id=?", (ids[2],))
    conn.commit()
    assert letter_gate.pending(conn) == []
    # staging any of them by hand still never attaches the letter
    assert letter_gate.check_job(conn, ids[0]) is not None
