"""Same-day scoring (pipeline/same_day.py, `jobkit same-day`): the jobs found
from job-alert emails go through the nightly run's own stages, limited to
those jobs, in a background launch, so one that clears the person's bar is
approved the same day.

The LLM boundary is mocked (tests/test_triage.py's canned answers over the
real llm.call), the board reads go to a MockTransport, and every background
launch is mocked: nothing touches a network or starts a process."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from jobkit import background, llm, rules
from jobkit.autofill import form_spec
from jobkit.pipeline import filter as filter_mod
from jobkit.pipeline import nightly, same_day, triage
from jobkit.track import job_alerts
from tests import test_board_resolver as tbr
from tests.test_filter import FILTERS, _insert
from tests.test_job_alert_emails import ALERT, _email_boards
from tests.test_triage import CANNED, REAL_JD

RULE = 'version: "1.0"\nauto_approve:\n  - when: { score_gte: 75 }\n    reason: "clears-your-bar"\n'


@pytest.fixture()
def llm_calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """The real llm.call over a fake backend (one entry per call), the test
    filters, a one-rule autoapprove.yaml, and no form-spec capture (it reads
    the corpus and the application form)."""
    calls: list[str] = []

    def fake_backend(stage: str, prompt: str) -> tuple[str, dict[str, Any]]:
        calls.append(stage)
        return json.dumps(CANNED[stage]), {}

    monkeypatch.setattr(llm, "_call_claude_cli", fake_backend)
    monkeypatch.setattr(triage, "_profile_summary", lambda: "EXPERIENCE: test profile")
    for mod in (triage, filter_mod, rules):
        monkeypatch.setattr(mod, "load_filters", lambda: FILTERS)
    path = tmp_path / "autoapprove.yaml"
    path.write_text(RULE)
    monkeypatch.setattr(rules, "RULES_PATH", str(path))
    monkeypatch.setattr(form_spec, "capture_form_specs", lambda c, limit=None: {"captured": 0})
    return calls


def _new_job(conn: sqlite3.Connection, external_id: str) -> int:
    return _insert(conn, external_id=external_id, jd_html=REAL_JD, jd_hash=external_id)


def _status(conn: sqlite3.Connection, job_id: int) -> str:
    return str(conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()[0])


def test_only_the_given_jobs_go_through_the_stages_and_one_is_approved(
    conn: sqlite3.Connection, llm_calls: list[str]
) -> None:
    found = _new_job(conn, "found-by-search")
    other = _new_job(conn, "polled-with-the-board")
    lines: list[str] = []
    rep = same_day.run(conn, [found], echo=lines.append)
    assert rep.failed == []
    # one job: extract, remote, comp and score, once each
    assert sorted(llm_calls) == ["comp", "extract", "remote", "score"]
    assert _status(conn, found) == "approved"
    assert _status(conn, other) == "new"  # left for the nightly run
    auto = conn.execute(
        "SELECT decision, auto, reason FROM decisions WHERE job_id = ?", (found,)
    ).fetchone()
    assert tuple(auto) == ("approve", 1, "clears-your-bar")
    assert lines[-2] == (
        f"job {found} Acme — Director of Sales: approved automatically (fit 82); tailoring and "
        "form filling come next"
    )
    assert lines[-1] == "same-day: 1 job(s): 1 approved"
    done = conn.execute("SELECT detail FROM events WHERE kind = 'same_day.done'").fetchone()
    assert json.loads(done[0]) == {
        "job_ids": [found],
        "failed": [],
        "jobs": {str(found): "approved"},
    }


def test_a_job_below_the_rules_stays_in_the_review_queue(
    conn: sqlite3.Connection, llm_calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(CANNED, "score", {**CANNED["score"], "score": 72})
    jid = _new_job(conn, "j1")
    rep = same_day.run(conn, [jid])
    assert _status(conn, jid) == "queued"
    assert rep.summary() == "same-day: 1 job(s): 1 in your review queue"


def test_a_job_whose_remote_check_failed_is_never_approved(
    conn: sqlite3.Connection,
    llm_calls: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The remote check's LLM call fails, so no sanity row is written. The
    job is scored, but the queue gate leaves it filtered for the next run:
    a rule with `sanity_flags: none` never sees a flag list no check wrote."""
    (tmp_path / "autoapprove.yaml").write_text(
        'version: "1.0"\nauto_approve:\n  - when: { score_gte: 75, sanity_flags: none }\n'
        '    reason: "clears-your-bar"\n'
    )

    def fake_backend(stage: str, prompt: str) -> tuple[str, dict[str, Any]]:
        llm_calls.append(stage)
        if stage == "remote":
            raise RuntimeError("model unavailable")
        return json.dumps(CANNED[stage]), {}

    monkeypatch.setattr(llm, "_call_claude_cli", fake_backend)
    jid = _new_job(conn, "remote-fails")
    same_day.run(conn, [jid])
    assert conn.execute("SELECT COUNT(*) FROM sanity WHERE job_id = ?", (jid,)).fetchone()[0] == 0
    assert _status(conn, jid) == "filtered"
    assert not conn.execute("SELECT 1 FROM decisions WHERE job_id = ?", (jid,)).fetchone()
    # the next run checks it again; once the checks finish it passes the gate
    monkeypatch.setattr(
        llm, "_call_claude_cli", lambda stage, prompt: (json.dumps(CANNED[stage]), {})
    )
    same_day.run(conn, [jid])
    assert conn.execute("SELECT COUNT(*) FROM sanity WHERE job_id = ?", (jid,)).fetchone()[0] == 1
    assert _status(conn, jid) in ("queued", "approved")


def test_the_queue_gate_waits_for_the_comp_check_only_when_asked(
    conn: sqlite3.Connection,
) -> None:
    """run_queue(require_sanity=True) leaves a job with no comp check
    filtered; without it (the golden tests' direct calls) nothing changes."""
    jid = _new_job(conn, "no-comp")
    conn.execute("UPDATE jobs SET status = 'filtered' WHERE id = ?", (jid,))
    conn.execute("INSERT INTO scores (job_id, fit_score) VALUES (?, 90)", (jid,))
    conn.execute("INSERT INTO sanity (job_id, flags) VALUES (?, '[]')", (jid,))
    conn.commit()
    assert triage.run_queue(conn, FILTERS, require_sanity=True) == 0
    assert _status(conn, jid) == "filtered"
    conn.execute("UPDATE sanity SET demand_score = 3 WHERE job_id = ?", (jid,))
    conn.commit()
    assert triage.run_queue(conn, FILTERS, require_sanity=True) == 1
    assert _status(conn, jid) == "queued"


def test_triage_and_the_nightly_queue_stage_keep_todays_behaviour(
    conn: sqlite3.Connection,
    llm_calls: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the same-day run holds a job whose checks did not finish. The
    run-all/nightly 'queue' stage and `jobkit triage` queue it as before, and
    an owner-shaped rule (score 85+, no sanity flag) then approves it."""
    from jobkit import cli

    path = tmp_path / "owner.yaml"
    path.write_text(
        'version: "1.0"\nauto_approve:\n'
        '  - when: { score_gte: 85, sanity_flags: none }\n    reason: "R1"\n'
        'auto_reject:\n  - when: { score_lt: 75 }\n    reason: "R5"\n'
    )
    monkeypatch.setattr(rules, "RULES_PATH", str(path))
    no_sanity = _new_job(conn, "no-sanity")
    no_demand = _new_job(conn, "no-demand")
    for jid in (no_sanity, no_demand):
        conn.execute("UPDATE jobs SET status = 'filtered' WHERE id = ?", (jid,))
        conn.execute("INSERT INTO scores (job_id, fit_score) VALUES (?, 90)", (jid,))
    conn.execute("INSERT INTO sanity (job_id, flags) VALUES (?, '[]')", (no_demand,))
    conn.commit()
    stages = dict(cli._run_all_stages())
    stages["queue"](conn)
    assert _status(conn, no_sanity) == "queued" and _status(conn, no_demand) == "queued"
    stats = rules.run_standing_rules(conn)
    assert stats["auto_approved"] == 2
    assert _status(conn, no_sanity) == "approved" and _status(conn, no_demand) == "approved"
    # `jobkit triage` queues it too
    later = _new_job(conn, "triage-no-sanity")
    conn.execute("UPDATE jobs SET status = 'filtered' WHERE id = ?", (later,))
    conn.execute("INSERT INTO scores (job_id, fit_score) VALUES (?, 90)", (later,))
    conn.commit()
    assert "triage" not in {name for name, _ in cli._run_all_stages()}
    assert triage.run_queue(conn) == 1 and _status(conn, later) == "queued"
    src = Path(cli.__file__).read_text()
    assert "require_sanity" not in src


def test_the_interview_rule_never_approves_a_job_whose_checks_did_not_finish(
    conn: sqlite3.Connection,
    llm_calls: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The nightly/run-all queue stage still queues a scored job with no
    sanity row (its remote check failed) or no demand_score (its comp check
    failed). The interview's clears-your-bar rule leaves both queued; the
    same job with both checks finished is approved."""
    from jobkit import cli
    from jobkit.onboarding import intake

    path = tmp_path / "interview.yaml"
    path.write_text(intake.render_autoapprove(intake.AutoApproveAnswer(enabled=True)))
    monkeypatch.setattr(rules, "RULES_PATH", str(path))
    no_sanity = _new_job(conn, "no-sanity")
    no_demand = _new_job(conn, "no-demand")
    checked = _new_job(conn, "checked")
    for jid in (no_sanity, no_demand, checked):
        conn.execute("UPDATE jobs SET status = 'filtered' WHERE id = ?", (jid,))
        conn.execute("INSERT INTO scores (job_id, fit_score) VALUES (?, 90)", (jid,))
    conn.execute("INSERT INTO sanity (job_id, flags) VALUES (?, '[]')", (no_demand,))
    conn.execute("INSERT INTO sanity (job_id, flags, demand_score) VALUES (?, '[]', 3)", (checked,))
    conn.commit()
    dict(cli._run_all_stages())["queue"](conn)
    stats = rules.run_standing_rules(conn)
    assert stats == {"evaluated": 3, "auto_approved": 1, "auto_rejected": 0}
    assert _status(conn, no_sanity) == "queued" and _status(conn, no_demand) == "queued"
    assert _status(conn, checked) == "approved"
    assert not conn.execute(
        "SELECT 1 FROM decisions WHERE job_id IN (?, ?)", (no_sanity, no_demand)
    ).fetchone()


def test_a_filtered_out_job_is_rejected_without_an_llm_call(
    conn: sqlite3.Connection, llm_calls: list[str]
) -> None:
    jid = _insert(conn, external_id="intern", title="Sales Intern", jd_html=REAL_JD)
    rep = same_day.run(conn, [jid])
    assert llm_calls == [] and _status(conn, jid) == "rejected"
    assert rep.jobs[0].line().startswith(f"job {jid} Acme — Sales Intern: rejected (filter: ")


def test_alert_card_jobs_are_picked_up_after_board_lookup(
    conn: sqlite3.Connection, jobkit_home: Path
) -> None:
    """`same-day --resolve-boards`: board lookup ingests the alert job's
    posting (polling the whole board); only that posting goes on to scoring,
    the board's other new postings wait for the nightly run."""
    job_alerts.record_job_alerts(
        conn, ALERT["extracted"], ALERT["message_id"], ALERT["received_at"], "linkedin"
    )
    boards = _email_boards()
    seen: list[list[int]] = []

    def stage_list(ids: Any) -> list[tuple[str, Any]]:
        seen.append(list(ids))
        return [("score", lambda c: "scored")]

    rep = same_day.run(
        conn,
        resolve_boards=True,
        resolver=lambda c: tbr.run(c, boards),
        stage_list=stage_list,
    )
    ny = tbr.ats_row(conn, "greenhouse", "larkspuranalytics", "7301000101")
    assert ny is not None
    assert seen == [[int(ny["id"])]]
    assert rep.resolve_lines and "LinkedIn card(s)" in rep.resolve_lines[0]
    chicago = tbr.ats_row(conn, "greenhouse", "larkspuranalytics", "7301000102")
    assert chicago is not None and chicago["status"] == "new"
    # a scored job is not picked up again
    conn.execute(
        "INSERT INTO scores (job_id, fit_score, rationale) VALUES (?, 80, 'x')", (ny["id"],)
    )
    conn.commit()
    assert same_day.alert_card_jobs(conn) == []


def test_a_failing_stage_does_not_stop_the_rest(conn: sqlite3.Connection) -> None:
    ran: list[str] = []

    def boom(c: sqlite3.Connection) -> object:
        raise RuntimeError("model unavailable")

    def stage_list(ids: Any) -> list[tuple[str, Any]]:
        return [("extract", boom), ("rules", lambda c: ran.append("rules") or "ok")]

    jid = _new_job(conn, "j1")
    rep = same_day.run(conn, [jid], stage_list=stage_list)
    assert rep.failed == ["extract"] and ran == ["rules"]
    assert rep.stage_lines[0] == "extract: FAILED — RuntimeError: model unavailable"
    assert rep.summary().endswith("; failed stage(s): extract")


def test_nothing_to_score_says_so(conn: sqlite3.Connection) -> None:
    rep = same_day.run(conn, [])
    assert rep.render() == (
        "same-day: no job from job-alert emails or web search waits for scoring"
    )


def test_the_stage_functions_without_ids_are_unchanged(
    conn: sqlite3.Connection, llm_calls: list[str]
) -> None:
    """The nightly run calls each stage without job ids: every job, as before."""
    a, b = _new_job(conn, "a"), _new_job(conn, "b")
    assert filter_mod.run_filters(conn)["examined"] == 2
    assert triage.run_extract(conn) == 2
    assert triage.run_extract(conn, job_ids=[]) == 0
    assert {_status(conn, a), _status(conn, b)} == {"filtered"}


# ---- starting it in the background ----------------------------------------------------


@pytest.fixture()
def launches(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    out: list[tuple[str, ...]] = []

    def spawn(c: sqlite3.Connection, *args: str, job_id: int | None = None) -> background.Launch:
        out.append(args)
        return background.Launch(7, 4242, tuple(args), Path("logs/spawn/x.log"))

    monkeypatch.setattr(background, "spawn", spawn)
    return out


def test_start_launches_same_day_in_the_background(
    conn: sqlite3.Connection, launches: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    out = same_day.start(conn, [12])
    assert launches == [("same-day", "12")]
    assert out.startswith("same-day scoring: started `jobkit same-day 12` as launch #7")
    assert same_day.start(conn, resolve_boards=True).startswith("same-day scoring: started")
    assert launches[-1] == ("same-day", "--resolve-boards")
    # a board lookup already running: not a second one
    busy = background.SpawnStatus(
        3, ("same-day", "--resolve-boards"), 1, "2026-10-21 11:00:00", Path("x"), "running", None
    )
    monkeypatch.setattr(background, "statuses", lambda c, limit=8: [busy])
    assert same_day.board_lookup_running(conn) is busy
    assert "already running (launch #3)" in same_day.start(conn, resolve_boards=True)
    assert len(launches) == 2
    # a search result's job still starts: a different job
    same_day.start(conn, [13])
    assert launches[-1] == ("same-day", "13")
    # the nightly run in progress does the same stages
    monkeypatch.setattr(nightly, "in_progress", lambda c: object.__new__(_Run))
    assert "the nightly run in progress" in same_day.start(conn, [14])
    assert len(launches) == 3


class _Run:
    run = "20261021T020000Z"


def test_only_a_search_job_launch_does_not_count_as_board_lookup(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = background.SpawnStatus(
        4, ("same-day", "12"), 1, "2026-10-21 11:00:00", Path("x"), "running", None
    )
    monkeypatch.setattr(background, "statuses", lambda c, limit=8: [other])
    assert same_day.board_lookup_running(conn) is None


def test_cli_same_day(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    from jobkit import cli

    monkeypatch.setattr(cli, "_db", lambda: conn)
    monkeypatch.setattr(same_day, "stages", lambda ids: [("score", lambda c: "ok")])
    jid = _new_job(conn, "j1")
    res = CliRunner().invoke(cli.app, ["same-day", str(jid)])
    assert res.exit_code == 0, res.output
    assert res.output.splitlines()[0] == "score: ok"
    assert res.output.splitlines()[-1] == "same-day: 1 job(s): 1 not scored"

    def boom(c: sqlite3.Connection) -> object:
        raise RuntimeError("x")

    monkeypatch.setattr(same_day, "stages", lambda ids: [("score", boom)])
    assert CliRunner().invoke(cli.app, ["same-day", str(jid)]).exit_code == 1


def test_a_search_job_launch_leaves_alert_card_jobs_to_board_lookup(
    conn: sqlite3.Connection, jobkit_home: Path
) -> None:
    """Two launches never score the same job: `same-day <id>` (a search
    result) takes only that job, `same-day --resolve-boards` the cards."""
    job_alerts.record_job_alerts(
        conn, ALERT["extracted"], ALERT["message_id"], ALERT["received_at"], "linkedin"
    )
    tbr.run(conn, _email_boards())
    assert same_day.alert_card_jobs(conn)
    seen: list[list[int]] = []

    def stage_list(ids: Any) -> list[tuple[str, Any]]:
        seen.append(list(ids))
        return []

    other = _new_job(conn, "found-by-search")
    same_day.run(conn, [other], stage_list=stage_list)
    assert seen == [[other]]
    same_day.run(conn, stage_list=stage_list)
    assert seen[-1] == same_day.alert_card_jobs(conn)
