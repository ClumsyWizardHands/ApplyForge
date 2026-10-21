"""Employer replies recorded from the person's own mailbox
(track/responses.py, MCP email_targets / match_email /
record_email_response / finish_email_check).

Matching runs on hand-written messages in tests/fixtures/email/ (a
Greenhouse rejection, an interview invite with a scheduling link, a Hireflix
video interview, an Ashby acknowledgement, a vague note from a company with
two applications, a newsletter). Recording is idempotent by message id;
the stage rules (acknowledgement, ghost revival, offer protection,
later-dated wins) hold; no email body is stored. No LLM is called and
nothing touches a network or a real mailbox."""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from jobkit import llm
from jobkit.pipeline import digest
from jobkit.track import responses
from tests.conftest import FIXTURES
from tests.helpers.applications import submitted

NOW = datetime.datetime(2026, 10, 21, 12, 0, tzinfo=datetime.UTC)
MESSAGES: dict[str, Any] = json.loads((FIXTURES / "email" / "messages.json").read_text())


@pytest.fixture(autouse=True)
def _fixed_clock_no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(responses, "_now", lambda: NOW)

    def refuse(*a: Any, **k: Any) -> Any:
        raise AssertionError("email tracking must not call an LLM")

    monkeypatch.setattr(llm, "call", refuse)
    monkeypatch.setattr(llm, "call_json", refuse)


def _app(
    conn: sqlite3.Connection,
    ext: str,
    company: tuple[int, str],
    title: str,
    submitted_on: str,
    *,
    stage: str = "submitted",
    board: tuple[int, str, str] | None = None,
    apply_url: str = "https://example.com/apply",
    domain: str | None = None,
) -> int:
    _, aid = submitted(
        conn,
        ext,
        days=1,
        stage=stage,
        title=title,
        company=company,
        apply_url=apply_url,
        board=board,
    )
    conn.execute("UPDATE applications SET submitted_at=? WHERE id=?", (submitted_on, aid))
    if domain:
        conn.execute("UPDATE companies SET domain=? WHERE id=?", (domain, company[0]))
    conn.commit()
    return aid


@pytest.fixture()
def apps(conn: sqlite3.Connection) -> dict[str, int]:
    north = (2, "Northwind Health")
    return {
        "clinical": _app(
            conn,
            "nw-1",
            north,
            "Clinical Operations Manager",
            "2026-09-30 15:00:00",
            board=(1, "greenhouse", "northwindhealth"),
            apply_url="https://job-boards.greenhouse.io/northwindhealth/jobs/1",
            domain="northwindhealth.example",
        ),
        "access": _app(
            conn,
            "nw-2",
            north,
            "Patient Access Supervisor",
            "2026-10-01",
            board=(1, "greenhouse", "northwindhealth"),
            apply_url="https://job-boards.greenhouse.io/northwindhealth/jobs/2",
        ),
        "support": _app(
            conn,
            "br-1",
            (3, "Brightline Robotics"),
            "Support Engineer",
            "2026-10-05",
            board=(2, "lever", "brightline"),
            apply_url="https://jobs.lever.co/brightline/abc",
            domain="brightlinerobotics.example",
        ),
        "designer": _app(
            conn,
            "jl-1",
            (4, "Juniper Learning"),
            "Instructional Designer",
            "2026-10-01",
            apply_url="https://jobs.ashbyhq.com/juniper/123",  # an aggregator row: no board
        ),
        "closed": _app(
            conn, "gx-1", (5, "Globex"), "Account Executive", "2026-09-01", stage="rejected"
        ),
    }


def _match(conn: sqlite3.Connection, key: str) -> responses.EmailMatch:
    m = MESSAGES[key]
    return responses.match_email(conn, m["sender"], m["subject"], m["snippet"], m["received_at"])


def _record(conn: sqlite3.Connection, aid: int, key: str, **over: Any) -> responses.Recorded:
    m = {**MESSAGES[key], **over}
    return responses.record_response(
        conn,
        aid,
        m["outcome"],
        m["received_at"],
        m["message_id"],
        m["link"],
        m.get("summary", ""),
    )


def _stage(conn: sqlite3.Connection, aid: int) -> str:
    return str(conn.execute("SELECT stage FROM applications WHERE id=?", (aid,)).fetchone()[0])


# ---- matching ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "expected", "reason"),
    [
        ("greenhouse_rejection", "clinical", "sent through greenhouse"),
        ("scheduling_link_interview", "support", "sent from the company's domain"),
        ("hireflix_video_interview", "designer", "sent through hireflix for the employer"),
        ("ashby_acknowledgement", "designer", "sent through ashby"),
    ],
)
def test_each_message_finds_its_application(
    conn: sqlite3.Connection, apps: dict[str, int], key: str, expected: str, reason: str
) -> None:
    match = _match(conn, key)
    assert match.best is not None, match.render()
    assert match.best.application_id == apps[expected]
    assert any(r.startswith(reason) for r in match.best.reasons), match.best.reasons
    assert f"Clear match: application {apps[expected]}." in match.render()


def test_the_title_separates_two_applications_at_one_company(
    conn: sqlite3.Connection, apps: dict[str, int]
) -> None:
    match = _match(conn, "greenhouse_rejection")
    ids = [c.application_id for c in match.candidates]
    assert ids[:2] == [apps["clinical"], apps["access"]]
    assert "the job title" in match.candidates[0].reasons
    assert "the job title" not in match.candidates[1].reasons


def test_a_vague_note_from_a_company_with_two_applications_is_not_a_clear_match(
    conn: sqlite3.Connection, apps: dict[str, int]
) -> None:
    match = _match(conn, "generic_same_company")
    assert {c.application_id for c in match.candidates[:2]} == {apps["clinical"], apps["access"]}
    assert match.best is None
    assert "No clear match: ask the person" in match.render()


def test_an_unrelated_message_matches_nothing(
    conn: sqlite3.Connection, apps: dict[str, int]
) -> None:
    match = _match(conn, "newsletter")
    assert match.candidates == [] and match.best is None
    assert "No submitted application matches this message" in match.render()


def test_the_ats_alone_matches_nothing(conn: sqlite3.Connection, apps: dict[str, int]) -> None:
    match = responses.match_email(
        conn, "no-reply@greenhouse-mail.io", "An update on your application", "Hello"
    )
    assert match.candidates == []


def test_the_company_name_in_the_sender_domain_counts(
    conn: sqlite3.Connection, apps: dict[str, int]
) -> None:
    conn.execute("UPDATE companies SET domain=NULL WHERE id=3")
    match = responses.match_email(
        conn, "recruiting@mail.brightlinerobotics.example", "Your Support Engineer application"
    )
    assert match.best is not None and match.best.application_id == apps["support"]
    assert "the sender's domain names the company" in match.best.reasons


def test_an_email_older_than_the_submission_ranks_lower(
    conn: sqlite3.Connection, apps: dict[str, int]
) -> None:
    m = MESSAGES["scheduling_link_interview"]
    match = responses.match_email(conn, m["sender"], m["subject"], m["snippet"], "2026-09-01")
    (cand,) = match.candidates
    assert "received before this application was submitted" in cand.reasons
    assert cand.score == _match(conn, "scheduling_link_interview").candidates[0].score - 30


# ---- what to look for ----------------------------------------------------------------


def test_email_targets_lists_open_applications_and_a_gmail_search(
    conn: sqlite3.Connection, apps: dict[str, int]
) -> None:
    t = responses.email_targets(conn)
    assert [x.application_id for x in t.targets] == [
        apps["clinical"],
        apps["access"],
        apps["designer"],
        apps["support"],
    ]  # by submission time; the rejected one is done
    by_id = {x.application_id: x for x in t.targets}
    assert by_id[apps["designer"]].ats == "ashby"  # from the apply link
    assert by_id[apps["designer"]].apply_host == "jobs.ashbyhq.com"
    assert by_id[apps["clinical"]].ats == "greenhouse"
    assert t.last_checked_through is None
    assert t.search_after == datetime.date(2026, 9, 30)
    assert t.query.startswith("after:2026/09/30 {from:greenhouse.io from:greenhouse-mail.io ")
    for part in (
        "from:lever.co",
        "from:ashbyhq.com",
        "from:myworkday.com",
        "from:icims.com",
        "from:smartrecruiters.com",
        "from:workable.com",
        "from:hireflix.com",
        '"Northwind Health"',
        '"Brightline Robotics"',
        '"Juniper Learning"',
    ):
        assert part in t.query, part
    assert t.query.count('"Northwind Health"') == 1 and "Globex" not in t.query
    text = t.render()
    assert "4 submitted application(s) to look for" in text and "not been checked" in text
    assert "nothing is sent" in text
    # the scheduled run must not be told to open whole messages
    assert "Read each message" not in text and "sender, subject, date and snippet" in text


def test_a_finished_check_moves_the_search_window(
    conn: sqlite3.Connection, apps: dict[str, int]
) -> None:
    _record(conn, apps["support"], "scheduling_link_interview")
    check = responses.finish_email_check(conn, "2026-10-18T08:00:00Z")
    assert check.checked_through == "2026-10-18 08:00:00" and check.recorded_since == 1
    t = responses.email_targets(conn)
    assert t.last_checked_through == "2026-10-18 08:00:00"
    assert t.search_after == datetime.date(2026, 10, 17)  # a day of overlap
    assert t.query.startswith("after:2026/10/17 ")
    again = responses.finish_email_check(conn)
    assert again.checked_through == "2026-10-21 12:00:00"
    assert again.previous == "2026-10-18 08:00:00" and again.recorded_since == 0
    with pytest.raises(ValueError, match="in the future"):
        responses.finish_email_check(conn, "2026-12-01")


def test_no_targets_means_no_search(conn: sqlite3.Connection) -> None:
    t = responses.email_targets(conn)
    assert t.targets == [] and t.query == ""
    assert "nothing to look for" in t.render()


# ---- recording -----------------------------------------------------------------------


def test_a_rejection_moves_the_stage_and_stores_only_the_allowed_fields(
    conn: sqlite3.Connection, apps: dict[str, int]
) -> None:
    rec = _record(conn, apps["clinical"], "greenhouse_rejection", summary="Rejected after review.")
    assert (rec.stage_before, rec.stage_after) == ("submitted", "rejected")
    assert rec.received_at == "2026-10-13 18:05:11"  # the Date header, in UTC
    (row,) = conn.execute("SELECT job_id, detail FROM events WHERE kind='email_response'")
    detail = json.loads(row["detail"])
    assert detail == {
        "application_id": apps["clinical"],
        "outcome": "rejected",
        "received_at": "2026-10-13 18:05:11",
        "message_id": "18f2a6c4d1e0b001",
        "link": "https://mail.google.com/mail/u/0/#all/18f2a6c4d1e0b001",
        "summary": "Rejected after review.",
        "stage_before": "submitted",
        "stage_after": "rejected",
    }
    assert _stage(conn, apps["clinical"]) == "rejected"


def test_the_same_message_is_recorded_once(conn: sqlite3.Connection, apps: dict[str, int]) -> None:
    first = _record(conn, apps["support"], "scheduling_link_interview")
    assert first.stage_after == "interview" and not first.already
    dump = [tuple(r) for r in conn.execute("SELECT * FROM events ORDER BY id")]
    again = _record(conn, apps["support"], "scheduling_link_interview", outcome="offer")
    assert again.already and again.outcome == "interview"
    assert again.render().startswith("already recorded")
    assert [tuple(r) for r in conn.execute("SELECT * FROM events ORDER BY id")] == dump
    assert _stage(conn, apps["support"]) == "interview"
    other = _record(conn, apps["designer"], "scheduling_link_interview")
    assert other.already and f"not {apps['designer']}" in other.render()


def test_an_acknowledgement_never_changes_the_stage(
    conn: sqlite3.Connection, apps: dict[str, int]
) -> None:
    rec = _record(conn, apps["designer"], "ashby_acknowledgement")
    assert rec.stage_before == rec.stage_after == "submitted"
    assert "an acknowledgement does not change the stage" in rec.render()
    conn.execute("UPDATE applications SET stage='ghosted' WHERE id=?", (apps["designer"],))
    late_ack = _record(
        conn,
        apps["designer"],
        "ashby_acknowledgement",
        message_id="ack-2",
        received_at="2026-10-20",
    )
    assert late_ack.stage_after == "ghosted"


def test_a_reply_revives_a_ghosted_application(
    conn: sqlite3.Connection, apps: dict[str, int]
) -> None:
    conn.execute("UPDATE applications SET stage='ghosted' WHERE id=?", (apps["designer"],))
    conn.commit()
    rec = _record(conn, apps["designer"], "hireflix_video_interview")
    assert (rec.stage_before, rec.stage_after) == ("ghosted", "interview")
    assert "it had been marked ghosted (no reply); now it has one" in rec.render()


def test_an_offer_is_never_changed_automatically(
    conn: sqlite3.Connection, apps: dict[str, int]
) -> None:
    conn.execute("UPDATE applications SET stage='offer' WHERE id=?", (apps["support"],))
    conn.commit()
    rec = _record(conn, apps["support"], "scheduling_link_interview", outcome="rejected")
    assert rec.stage_after == "offer" and "never changed automatically" in rec.render()
    assert _stage(conn, apps["support"]) == "offer"
    # the message is still on record
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='email_response'").fetchone()[0]


def test_the_later_dated_response_decides_the_stage(
    conn: sqlite3.Connection, apps: dict[str, int]
) -> None:
    aid = apps["support"]
    interview = _record(conn, aid, "scheduling_link_interview")  # 14 Oct
    assert interview.stage_after == "interview"
    older = _record(
        conn,
        aid,
        "scheduling_link_interview",
        outcome="rejected",
        message_id="old",
        received_at="2026-10-08",
    )
    assert older.stage_after == "interview" and "a later response (2026-10-14)" in older.render()
    newer = _record(
        conn,
        aid,
        "scheduling_link_interview",
        outcome="rejected",
        message_id="new",
        received_at="2026-10-19",
    )
    assert (newer.stage_before, newer.stage_after) == ("interview", "rejected")
    # a later interview invite (another round, a second look) wins over the rejection
    later = _record(
        conn, aid, "scheduling_link_interview", message_id="newer", received_at="2026-10-20"
    )
    assert later.stage_after == "interview"


def test_a_reply_never_undoes_a_stronger_outcome(
    conn: sqlite3.Connection, apps: dict[str, int]
) -> None:
    aid = apps["support"]
    _record(conn, aid, "scheduling_link_interview")
    rec = _record(
        conn,
        aid,
        "scheduling_link_interview",
        outcome="recruiter_question",
        message_id="q",
        received_at="2026-10-16",
    )
    assert rec.stage_after == "interview" and "does not undo stage 'interview'" in rec.render()
    assess = _record(
        conn,
        apps["designer"],
        "hireflix_video_interview",
        outcome="assessment",
        message_id="a1",
    )
    assert assess.stage_after == "replied"


def test_a_summary_with_a_line_break_is_refused(
    conn: sqlite3.Connection, apps: dict[str, int]
) -> None:
    """The summary is one line: a pasted email body (line breaks in it) is
    refused with nothing written."""
    body = (
        "Hi Jordan,\n\nThank you for applying. Unfortunately we will not be moving forward "
        "at this time.\n\nBest,\nThe Team"
    )
    for summary in (body, "Rejected.\nThanks", "Rejected.\r\nThanks"):
        with pytest.raises(ValueError, match="summary must be one line"):
            _record(conn, apps["clinical"], "greenhouse_rejection", summary=summary)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert _stage(conn, apps["clinical"]) == "submitted"
    # surrounding whitespace, a trailing newline included, is not a line break
    rec = _record(conn, apps["clinical"], "greenhouse_rejection", summary="  Rejected.\n")
    detail = json.loads(
        conn.execute("SELECT detail FROM events WHERE kind='email_response'").fetchone()[0]
    )
    assert detail["summary"] == "Rejected." and rec.outcome == "rejected"


def test_no_email_body_is_stored(conn: sqlite3.Connection, apps: dict[str, int]) -> None:
    body = (
        "Hi Jordan, thank you for applying. Unfortunately we will not be moving forward "
        "at this time. We appreciate the time you invested and encourage you to apply "
        "again for future openings that match your background. Best, The Team"
    )
    rec = _record(conn, apps["clinical"], "greenhouse_rejection", summary=body)
    detail = json.loads(
        conn.execute("SELECT detail FROM events WHERE kind='email_response'").fetchone()[0]
    )
    assert len(detail["summary"]) <= responses.SUMMARY_MAX
    assert "\n" not in detail["summary"] and detail["summary"].endswith("…")
    dumped = "\n".join(str(v) for r in conn.execute("SELECT * FROM events") for v in tuple(r))
    assert "future openings" not in dumped and "Best," not in dumped
    assert MESSAGES["greenhouse_rejection"]["snippet"][:60] not in dumped
    assert rec.outcome == "rejected"


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"outcome": "ghosted"}, "outcome must be one of"),
        ({"message_id": ""}, "message_id is required"),
        ({"message_id": "two words"}, "one token"),
        ({"link": "javascript:alert(1)"}, "http"),
        ({"link": "https://x.test/" + "a" * 600}, "at most 500"),
        ({"received_at": "last Tuesday"}, "not a date"),
        ({"received_at": "2026-11-30"}, "in the future"),
    ],
)
def test_bad_input_writes_nothing(
    conn: sqlite3.Connection, apps: dict[str, int], over: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _record(conn, apps["clinical"], "greenhouse_rejection", **over)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert _stage(conn, apps["clinical"]) == "submitted"


def test_an_application_must_exist_and_be_submitted(
    conn: sqlite3.Connection, apps: dict[str, int]
) -> None:
    with pytest.raises(ValueError, match="no application with id 999"):
        _record(conn, 999, "greenhouse_rejection")
    conn.execute("UPDATE applications SET submitted_at=NULL WHERE id=?", (apps["access"],))
    with pytest.raises(ValueError, match="mark_submitted"):
        _record(conn, apps["access"], "greenhouse_rejection")


def test_outcome_spellings_are_normalized(conn: sqlite3.Connection, apps: dict[str, int]) -> None:
    rec = _record(conn, apps["access"], "generic_same_company", outcome="Recruiter Question")
    assert rec.outcome == "recruiter_question" and rec.stage_after == "replied"


# ---- the digest, the MCP tools and the CLI ------------------------------------------


def test_the_digest_lists_replies_since_the_last_digest(
    conn: sqlite3.Connection, apps: dict[str, int], jobkit_home: Path
) -> None:
    _record(conn, apps["support"], "scheduling_link_interview", summary="30 minute call.")
    text = digest.write_digest(conn, "2026-10-21").read_text()
    section = "## Replies recorded since the last digest (1)\n"
    assert section in text
    line = text[text.index(section) + len(section) :].splitlines()[0]
    assert line == (
        f"- Brightline Robotics — Support Engineer (job {_job(conn, apps['support'])}): "
        "interview, received 2026-10-14, stage submitted -> interview: 30 minute call."
    )
    assert text.index("## Review queue") < text.index(section) < text.index("## Funnel")
    # the next day's digest starts after this one was written
    future = datetime.datetime.now().timestamp() + 3600
    os.utime(digest.DIGEST_DIR / "2026-10-21.md", (future, future))
    assert "Replies recorded" not in digest.write_digest(conn, "2026-10-22").read_text()


def _job(conn: sqlite3.Connection, aid: int) -> int:
    return int(conn.execute("SELECT job_id FROM applications WHERE id=?", (aid,)).fetchone()[0])


def test_mcp_tools_are_read_only_wrappers(
    conn: sqlite3.Connection, apps: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import mcp_server

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    for name in ("email_targets", "match_email", "record_email_response", "finish_email_check"):
        doc = getattr(mcp_server, name).__doc__ or ""
        assert "sent" in doc, name  # each says nothing is sent
    assert "4 submitted application(s)" in mcp_server.email_targets()
    m = MESSAGES["hireflix_video_interview"]
    out = mcp_server.match_email(m["sender"], m["subject"], m["snippet"], m["received_at"])
    assert f"Clear match: application {apps['designer']}." in out
    assert mcp_server.match_email("x@y.z", received_at="soon").startswith("no match run:")
    rec = mcp_server.record_email_response(
        apps["designer"], "interview", m["received_at"], m["message_id"], m["link"], "Video."
    )
    assert rec.startswith(f"recorded interview (2026-10-15) for application {apps['designer']}")
    assert mcp_server.record_email_response(
        apps["designer"], "maybe", m["received_at"], "z"
    ).startswith("not recorded: outcome must be one of")
    assert "mailbox checked through" in mcp_server.finish_email_check()
    assert mcp_server.finish_email_check("tomorrow-ish").startswith("not recorded:")


def test_the_scheduled_check_is_off_without_the_key(
    conn: sqlite3.Connection,
    apps: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    jobkit_home: Path,
) -> None:
    """D9: the sales sandbox config has no tracking block, so the unattended
    run's email step stays off; asking Claude (scheduled=False) still works.
    tracking.email_check: true turns the scheduled step on."""
    import yaml

    from jobkit import mcp_server
    from jobkit.config import Filters, load_filters

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    assert "tracking" not in load_filters().raw and load_filters().email_check is False
    off = mcp_server.email_targets(scheduled=True)
    assert off.startswith("Email not checked: tracking.email_check is off")
    assert "submitted application(s)" not in off
    assert "4 submitted application(s)" in mcp_server.email_targets()
    path = jobkit_home / "config" / "filters.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["tracking"] = {"email_check": True}
    path.write_text(yaml.safe_dump(raw))
    assert "4 submitted application(s)" in mcp_server.email_targets(scheduled=True)
    raw["tracking"] = {"email_check": "yes"}
    path.write_text(yaml.safe_dump(raw))
    assert mcp_server.email_targets(scheduled=True).startswith("Email not checked: tracking")
    with pytest.raises(ValueError, match="true or false"):
        _ = Filters(raw={"tracking": {"email_check": 1}}).email_check
    # the template turns it on for new users
    template = yaml.safe_load((jobkit_home / "config" / "filters.example.yaml").read_text())
    assert Filters(raw=template).email_check is True


def test_the_scheduled_check_without_a_filters_file_says_so(
    conn: sqlite3.Connection,
    apps: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    jobkit_home: Path,
) -> None:
    """With no config/filters.yaml the unattended run gets the 'Email not
    checked' line naming the setup interview, not an exception."""
    from jobkit import mcp_server

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    (jobkit_home / "config" / "filters.yaml").unlink()
    off = mcp_server.email_targets(scheduled=True)
    assert off.startswith("Email not checked: config/filters.yaml not found")
    assert "setup interview" in off and "submitted application(s)" not in off


def test_cli_email_commands(
    conn: sqlite3.Connection, apps: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import cli

    monkeypatch.setattr(cli, "_db", lambda: conn)
    run = CliRunner().invoke
    res = run(cli.app, ["email", "targets"])
    assert res.exit_code == 0 and "Suggested Gmail search" in res.output
    m = MESSAGES["ashby_acknowledgement"]
    res = run(cli.app, ["email", "match", "--sender", m["sender"], "--subject", m["subject"]])
    assert res.exit_code == 0 and f"Clear match: application {apps['designer']}" in res.output
    args = ["email", "record", "--application-id", str(apps["designer"]), "--outcome"]
    res = run(
        cli.app,
        [*args, "acknowledged", "--received-at", m["received_at"], "--message-id", m["message_id"]],
    )
    assert res.exit_code == 0 and "stage submitted -> submitted" in res.output
    res = run(cli.app, [*args, "nope", "--received-at", "2026-10-02", "--message-id", "q"])
    assert res.exit_code == 1 and "not recorded" in res.output
    res = run(cli.app, ["email", "done", "--checked-through", "2026-10-20"])
    assert res.exit_code == 0 and "mailbox checked through 2026-10-20 00:00:00" in res.output
