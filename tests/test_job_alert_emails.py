"""Job-alert emails and LinkedIn message notifications in the email check
(track/job_alerts.py, MCP record_job_alerts / record_linkedin_message, the
alert search in email_targets, the digest sections).

Runs on hand-written messages in tests/fixtures/email/job_alerts.json (a
LinkedIn job alert listing five jobs, a LinkedIn InMail, a LinkedIn message,
an Indeed alert; fictional companies and people) in the shape Claude takes
from them. The title rules are the LinkedIn guest card rules; a kept job at
a company jobkit polls needs nothing; any other kept job's company goes to
the discovery queue and the board resolver looks for the same job on the
employer's own board. No LinkedIn URL or job id is stored anywhere. No LLM
is called and nothing touches a network or a real mailbox."""

from __future__ import annotations

import asyncio
import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from jobkit import llm
from jobkit.discovery import board_resolver as br
from jobkit.discovery import registry
from jobkit.normalize import normalize_company
from jobkit.pipeline import digest
from jobkit.track import job_alerts, responses
from tests import test_board_resolver as tbr
from tests.conftest import FIXTURES
from tests.helpers.applications import submitted

NOW = datetime.datetime(2026, 10, 21, 12, 0, tzinfo=datetime.UTC)
EMAILS: dict[str, Any] = json.loads((FIXTURES / "email" / "job_alerts.json").read_text())
ALERT = EMAILS["linkedin_job_alert"]
INDEED = EMAILS["indeed_alert"]
INMAIL = EMAILS["linkedin_inmail"]
MESSAGE = EMAILS["linkedin_message"]


@pytest.fixture(autouse=True)
def _fixed_clock_no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(responses, "_now", lambda: NOW)

    def refuse(*a: Any, **k: Any) -> Any:
        raise AssertionError("job-alert recording must not call an LLM")

    monkeypatch.setattr(llm, "call", refuse)
    monkeypatch.setattr(llm, "call_json", refuse)


def _polled_board(conn: sqlite3.Connection, name: str, ats: str, token: str) -> None:
    """A company jobkit already polls: an active board of a supported ATS."""
    cid = conn.execute(
        "INSERT INTO companies (name, normalized_name) VALUES (?, ?) RETURNING id",
        (name, normalize_company(name)),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO boards (company_id, ats, token, status) VALUES (?, ?, ?, 'active')",
        (cid, ats, token),
    )
    conn.commit()


def _alert(conn: sqlite3.Connection, msg: dict[str, Any]) -> job_alerts.AlertsRecorded:
    return job_alerts.record_job_alerts(
        conn, msg["extracted"], msg["message_id"], msg["received_at"], msg["source"]
    )


def _message(conn: sqlite3.Connection, msg: dict[str, Any]) -> job_alerts.MessageRecorded:
    x = msg["extracted"]
    return job_alerts.record_linkedin_message(
        conn,
        x["company"],
        x["summary"],
        msg["received_at"],
        msg["message_id"],
        msg["link"],
    )


def _events(conn: sqlite3.Connection, kind: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT job_id, detail FROM events WHERE kind = ? ORDER BY id", (kind,))
    return [{"job_id": r["job_id"], **json.loads(r["detail"])} for r in rows]


def _counts(conn: sqlite3.Connection) -> tuple[int, ...]:
    return tuple(
        int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        for t in ("events", "discovery_queue", "rejections", "jobs", "companies", "boards")
    )


def _dump(conn: sqlite3.Connection) -> str:
    """Every value in every table, as one string."""
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    parts: list[str] = []
    for t in tables:
        for row in conn.execute(f"SELECT * FROM {t}"):  # noqa: S608 - table names from sqlite_master
            parts.extend(str(v) for v in tuple(row) if v is not None)
    return "\n".join(parts)


# ---- a job-alert email -------------------------------------------------------------


def test_a_linkedin_alert_goes_through_the_title_rules_and_becomes_cards(
    conn: sqlite3.Connection,
) -> None:
    _polled_board(conn, "Quillfeather Labs", "greenhouse", "quillfeather")
    rec = _alert(conn, ALERT)
    assert not rec.already
    assert (rec.kept, rec.queued_cards, rec.already_in_db, rec.dropped) == (3, 3, 0, 2)
    assert (rec.at_polled_board, rec.companies_queued, rec.repeated) == (1, 2, 0)
    text = rec.render()
    assert text.startswith(
        "recorded LinkedIn job alert of 2026-10-20: kept 3 (3 for board lookup, 0 already in "
        "jobkit), dropped 2 by the title rules; 2 company name(s) new to the discovery queue"
    )
    assert (
        "- Quillfeather Labs — Senior Account Executive (Remote - US): board lookup matches it "
        "on the company's board (greenhouse/quillfeather), already polled"
    ) in text
    assert "finish_email_check starts it" in text
    # the company names only, with the email source and no URL; the company
    # whose board jobkit polls is not queued again
    queued = conn.execute(
        "SELECT company_name, source, first_seen_url FROM discovery_queue ORDER BY id"
    ).fetchall()
    assert [tuple(r) for r in queued] == [
        ("Larkspur Analytics", "linkedin_email", None),
        ("Cobaltline Health", "linkedin_email", None),
    ]
    # the engineer exclusion and the paused SDR track, logged like a feed rejection
    rejected = [tuple(r) for r in conn.execute("SELECT external_ref, reason FROM rejections")]
    assert rejected[0][0] == f"linkedin_email/{ALERT['message_id']}#3"
    assert rejected[0][1].startswith("exclude_title:")
    assert rejected[1] == (f"linkedin_email/{ALERT['message_id']}#4", "track_paused:sdr_bdr")
    items = {e["company"]: e for e in _events(conn, job_alerts.EV_ITEM)}
    assert items["Quillfeather Labs"]["outcome"] == "known_board:greenhouse/quillfeather"
    assert items["Quillfeather Labs"]["card"] == 1
    assert items["Larkspur Analytics"]["outcome"] == "queued_company:no_registry_board"
    assert items["Larkspur Analytics"]["company_site_link"] is True
    assert items["Larkspur Analytics"]["ref"] == f"{ALERT['message_id']}#1"
    assert items["Marigold Freight"]["outcome"] == "rejected:track_paused:sdr_bdr"
    assert items["Marigold Freight"]["card"] == 0
    assert all(e["id"].startswith("alert-") and e["job_id"] is None for e in items.values())
    # nothing becomes a job row here: the employer's posting is the only way in
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_an_alert_job_jobkit_already_holds_is_not_looked_up_again(
    conn: sqlite3.Connection,
) -> None:
    """The Quillfeather board was polled: its 'Senior Account Executive,
    Remote - US' posting is a job row. The alert's job is that posting: no
    card, no second job row."""
    boards = _email_boards()
    _poll(conn, boards, "quillfeather", "Quillfeather Labs")
    have = _ats_job(conn, "quillfeather", QUILL_SENIOR_AE)
    rec = _alert(conn, ALERT)
    assert (rec.kept, rec.queued_cards, rec.already_in_db, rec.dropped) == (3, 2, 1, 2)
    assert (
        f"- Quillfeather Labs — Senior Account Executive (Remote - US): already in jobkit as job "
        f"{have} (the employer's own posting)"
    ) in rec.render()
    item = next(e for e in _events(conn, job_alerts.EV_ITEM) if e["company"] == "Quillfeather Labs")
    assert item["outcome"] == f"in_db:{have}" and item["card"] == 0 and item["job_id"] == have
    jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    br_items = br._card_items(conn, br.load_filters().board_resolution)
    assert "Quillfeather Labs" not in {it.company for it in br_items}
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == jobs


def test_an_alert_email_is_recorded_once(conn: sqlite3.Connection) -> None:
    first = _alert(conn, ALERT)
    before = _counts(conn)
    again = _alert(conn, ALERT)
    assert again.already and _counts(conn) == before
    assert again.counts() == first.counts()
    assert again.render().startswith("already recorded: this message is a LinkedIn job alert")


def test_another_board_s_alert_queues_under_its_own_source(conn: sqlite3.Connection) -> None:
    _alert(conn, ALERT)
    rec = _alert(conn, INDEED)
    # Harborview queued new; Larkspur's company is already queued; the store
    # manager is excluded
    assert (rec.kept, rec.queued_cards, rec.companies_queued, rec.dropped) == (2, 2, 1, 1)
    assert "Indeed job alert of 2026-10-21" in rec.render()
    sources = dict(
        tuple(r) for r in conn.execute("SELECT company_name, source FROM discovery_queue")
    )
    assert sources["Harborview Outfitters"] == "job_alert_email"
    assert sources["Larkspur Analytics"] == "linkedin_email"  # first seen wins
    larkspur = [e for e in _events(conn, job_alerts.EV_ITEM) if e["source"] == "indeed"][1]
    assert larkspur["outcome"] == "company_already_queued:no_registry_board"
    assert larkspur["queue_source"] == "job_alert_email"


def test_the_same_job_twice_in_one_email_counts_once(conn: sqlite3.Connection) -> None:
    jobs = [ALERT["extracted"][0], dict(ALERT["extracted"][0])]
    rec = job_alerts.record_job_alerts(conn, jobs, "m-dup", "2026-10-20", "linkedin")
    assert (rec.kept, rec.repeated) == (1, 1)
    assert len(_events(conn, job_alerts.EV_ITEM)) == 1


def test_items_may_come_as_json_text(conn: sqlite3.Connection) -> None:
    rec = job_alerts.record_job_alerts(
        conn, json.dumps(INDEED["extracted"]), "m-json", INDEED["received_at"], "Indeed"
    )
    assert rec.source == "indeed" and rec.kept == 2


@pytest.mark.parametrize(
    ("items", "message"),
    [
        ([{"company": "Larkspur Analytics", "title": "AE", "url": "https://x.example"}], "url"),
        ([{"company": "Larkspur Analytics", "title": "AE", "job_id": "4300000101"}], "job_id"),
        (
            [{"company": "Larkspur Analytics", "title": "https://www.linkedin.com/jobs/view/1"}],
            "holds a link",
        ),
        ([{"company": "Larkspur Analytics", "title": "AE 4300000101"}], "long number"),
        ([{"company": "lnkd.in/abc", "title": "AE"}], "holds a link"),
        ([{"company": "", "title": "AE"}], "company is required"),
        ([{"company": "Larkspur Analytics", "title": "AE\nApply now"}], "one line"),
        ([{"company": "X", "title": "AE", "company_site_link": "yes"}], "true, false"),
        ("not json", "JSON list"),
        ({"company": "X"}, "must be a list"),
    ],
)
def test_a_link_a_job_id_or_a_bad_item_is_refused_with_nothing_written(
    conn: sqlite3.Connection, items: Any, message: str
) -> None:
    before = _counts(conn)
    with pytest.raises(ValueError, match=message):
        job_alerts.record_job_alerts(conn, items, "m-bad", "2026-10-20", "linkedin")
    assert _counts(conn) == before


def test_bad_message_id_date_or_source_writes_nothing(conn: sqlite3.Connection) -> None:
    before = _counts(conn)
    for mid, when, source in (
        ("", "2026-10-20", "linkedin"),
        ("m", "2026-12-30", "linkedin"),
        ("m", "2026-10-20", "https://www.linkedin.com"),
        ("m", "2026-10-20", "4300000102"),
        ("m", "2026-10-20", "job4300000102"),
    ):
        with pytest.raises(ValueError):
            job_alerts.record_job_alerts(conn, ALERT["extracted"], mid, when, source)
    assert _counts(conn) == before


# ---- the employer's own board ------------------------------------------------------

EMAIL_BOARDS = FIXTURES / "email" / "boards"
QUILL_SENIOR_AE = "7303000101"
LARKSPUR_MM_NEW_YORK = "7301000101"


def _email_boards() -> tbr.Boards:
    """The recorded board fixtures plus the fictional Greenhouse boards of
    three companies in the alert (tests/fixtures/email/boards): Larkspur's
    'Account Executive, Mid-Market' in New York and in Chicago, Cobaltline's
    'Revenue Operations Manager' twice in Chicago (ambiguous), Quillfeather's
    'Senior Account Executive, Remote - US'."""
    return tbr.Boards(
        boards={
            ("greenhouse", "larkspuranalytics"): json.loads(
                (EMAIL_BOARDS / "greenhouse_larkspuranalytics.json").read_text()
            ),
            ("greenhouse", "cobaltlinehealth"): json.loads(
                (EMAIL_BOARDS / "greenhouse_cobaltlinehealth.json").read_text()
            ),
            ("greenhouse", "quillfeather"): json.loads(
                (EMAIL_BOARDS / "greenhouse_quillfeather.json").read_text()
            ),
        }
    )


def _poll(conn: sqlite3.Connection, boards: tbr.Boards, token: str, company: str) -> None:
    from jobkit.discovery import ingest

    polled = asyncio.run(ingest.poll_board(conn, "greenhouse", token, company, boards.transport))
    assert polled.ok
    conn.commit()


def _ats_job(conn: sqlite3.Connection, token: str, external_id: str) -> int:
    row = tbr.ats_row(conn, "greenhouse", token, external_id)
    assert row is not None
    return int(row["id"])


def _resolve(conn: sqlite3.Connection, boards: tbr.Boards) -> br.Report:
    return tbr.run(conn, boards, br.load_filters())


def test_alert_jobs_reach_the_board_resolver_as_cards(conn: sqlite3.Connection) -> None:
    _polled_board(conn, "Quillfeather Labs", "greenhouse", "quillfeather")
    _alert(conn, ALERT)
    items = br._card_items(conn, br.load_filters().board_resolution)
    assert [(it.company, it.title, it.location, it.source) for it in items] == [
        ("Larkspur Analytics", "Account Executive, Mid-Market", "New York, NY", "linkedin_email"),
        ("Quillfeather Labs", "Senior Account Executive", "Remote - US", "linkedin_email"),
        ("Cobaltline Health", "Revenue Operations Manager", "Chicago, IL", "linkedin_email"),
    ]
    assert all(it.kind == "card" and it.li_id.startswith("alert-") for it in items)
    assert items[0].ref() == f"job-alert email card {items[0].li_id}"


def test_alert_cards_resolve_to_the_employers_posting_by_title_and_location(
    conn: sqlite3.Connection, jobkit_home: Path
) -> None:
    """End to end, offline, on the recorded fixtures: the LinkedIn alert's
    Larkspur job (New York) is found on Larkspur's Greenhouse board, which
    has the same title in New York and in Chicago: the New York posting is
    the one. Cobaltline's board has two Chicago postings of the title: the
    card is flagged, nothing picked. Quillfeather's polled board has the
    job. The digest says where each was found."""
    _polled_board(conn, "Quillfeather Labs", "greenhouse", "quillfeather")
    _alert(conn, ALERT)
    boards = _email_boards()
    rep = _resolve(conn, boards)
    assert rep.cards == 3 and rep.ingested == 2 and rep.ambiguous == 1, rep.render()
    assert not any("linkedin" in str(r.url) for r in boards.requests)
    ny = _ats_job(conn, "larkspuranalytics", LARKSPUR_MM_NEW_YORK)
    quill = _ats_job(conn, "quillfeather", QUILL_SENIOR_AE)
    outcomes = {e["company"]: e for e in _events(conn, "board_resolve.card")}
    assert outcomes["Larkspur Analytics"]["outcome"] == "ingested"
    assert outcomes["Larkspur Analytics"]["job_id"] == ny
    assert outcomes["Quillfeather Labs"]["job_id"] == quill
    assert outcomes["Cobaltline Health"]["outcome"] == "ambiguous"
    # exactly one job row is the Larkspur card's posting
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE company_name = 'Larkspur Analytics' AND title = ? "
            "AND location_raw = 'New York, NY'",
            ("Account Executive, Mid-Market",),
        ).fetchone()[0]
        == 1
    )
    (flag,) = br.flags(conn)
    assert flag.source == "linkedin_email" and flag.job_id is None
    assert flag.line().startswith(
        f"job-alert email card {flag.li_id}: Cobaltline Health — Revenue Operations Manager "
        "(Chicago, IL) [board-match-ambiguous]"
    )
    text = digest.write_digest(conn, "2026-10-21").read_text()
    assert (
        "- Larkspur Analytics — Account Executive, Mid-Market (New York, NY), LinkedIn alert "
        f"2026-10-20: found on the employer's own board (greenhouse/larkspuranalytics): job {ny}"
    ) in text
    assert (
        "- Quillfeather Labs — Senior Account Executive (Remote - US), LinkedIn alert "
        f"2026-10-20: found on the employer's own board (greenhouse/quillfeather): job {quill}"
    ) in text
    assert (
        "- Cobaltline Health — Revenue Operations Manager (Chicago, IL), LinkedIn alert "
        "2026-10-20: flagged: more than one posting could be it"
    ) in text
    # the only job rows are the employer's own postings; a second run adds nothing
    assert not conn.execute(
        "SELECT id FROM jobs WHERE board_id IS NULL OR apply_url LIKE '%linkedin%'"
    ).fetchall()
    jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert _resolve(conn, boards).cards == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == jobs
    # the same job in a later alert is already in jobkit
    later = job_alerts.record_job_alerts(
        conn, [ALERT["extracted"][0]], "m-later", "2026-10-22", "linkedin"
    )
    assert (later.queued_cards, later.already_in_db) == (0, 1)


def test_a_polled_board_card_never_makes_a_second_job(conn: sqlite3.Connection) -> None:
    """The alert names a job at a company whose board jobkit polls, before
    the poll has the posting; the poll then runs; the resolver matches the
    card to the posting the poll stored: no second row."""
    _polled_board(conn, "Quillfeather Labs", "greenhouse", "quillfeather")
    _alert(conn, ALERT)
    boards = _email_boards()
    _poll(conn, boards, "quillfeather", "Quillfeather Labs")
    quill = _ats_job(conn, "quillfeather", QUILL_SENIOR_AE)
    _resolve(conn, boards)
    (card,) = [
        e for e in _events(conn, "board_resolve.card") if e["company"] == "Quillfeather Labs"
    ]
    assert card["outcome"] == "ingested" and card["job_id"] == quill
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE title = 'Senior Account Executive'"
        ).fetchone()[0]
        == 1
    )


def test_an_alert_job_at_a_registry_company_enters_only_as_its_posting(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, jobkit_home: Path
) -> None:
    """The Soylent board (tests/fixtures/board_resolver) through the
    registry: New York is ingested; the Chicago one has no agreeing posting
    and is flagged."""
    real = registry.lookup
    monkeypatch.setattr(
        registry,
        "lookup",
        lambda name: tbr.FICTIONAL_REGISTRY.get(normalize_company(name)) or real(name),
    )
    jobs = [
        {"company": "Soylent.ai", "title": "Sales Director", "location": "New York, NY"},
        {"company": "Soylent.ai", "title": "Sales Director", "location": "Chicago, IL"},
    ]
    rec = job_alerts.record_job_alerts(conn, jobs, "m-soylent", "2026-10-20", "linkedin")
    assert (rec.kept, rec.queued_cards, rec.companies_queued) == (2, 2, 1)
    rep = tbr.run(conn, tbr.Boards())
    assert rep.cards == 2 and rep.ingested == 1 and rep.ambiguous == 1, rep.render()
    assert tbr.ats_row(conn, "ashby", "soylent", tbr.SOYLENT_NEW_YORK) is not None


# ---- LinkedIn messages -------------------------------------------------------------


def test_an_inmail_is_one_line_and_linked_to_the_application(conn: sqlite3.Connection) -> None:
    jid, aid = submitted(
        conn, "lark-1", days=3, company=(7, "Larkspur Analytics"), title="Account Executive"
    )
    rec = _message(conn, INMAIL)
    assert (rec.job_id, rec.application_id) == (jid, aid)
    text = rec.render()
    assert text.startswith("recorded the LinkedIn message (Larkspur Analytics), 2026-10-20: ")
    assert (
        f"about application {aid} (Larkspur Analytics — Account Executive, stage submitted)"
    ) in text
    assert "Nothing was sent" in text
    (ev,) = _events(conn, job_alerts.EV_MESSAGE)
    assert ev["job_id"] == jid and ev["application_id"] == aid
    assert ev["summary"] == INMAIL["extracted"]["summary"]
    assert ev["link"] == INMAIL["link"]
    # what is stored: their company, one line, when, the ids and the link;
    # never the sender's name
    assert set(ev) == {
        "job_id",
        "message_id",
        "received_at",
        "company",
        "summary",
        "link",
        "application_id",
        "stage",
        "job_company",
        "job_title",
        "job_status",
    }
    # the stage is the person's: a message never moves it
    stage = conn.execute("SELECT stage FROM applications WHERE id=?", (aid,)).fetchone()[0]
    assert stage == "submitted"


def test_a_message_links_a_queued_job_and_is_recorded_once(conn: sqlite3.Connection) -> None:
    from tests.test_filter import _insert

    conn.execute(
        "INSERT INTO companies (id, name, normalized_name) VALUES (9, 'Larkspur Analytics', "
        "'larkspur analytics')"
    )
    jid = _insert(
        conn, external_id="q1", company_id=9, company_name="Larkspur Analytics", title="AE"
    )
    conn.execute("UPDATE jobs SET status='queued' WHERE id=?", (jid,))
    conn.commit()
    rec = _message(conn, INMAIL)
    assert rec.job_id == jid and rec.application_id is None
    assert f"about job {jid} (Larkspur Analytics — AE, queued)" in rec.render()
    before = _counts(conn)
    again = _message(conn, INMAIL)
    assert again.already and _counts(conn) == before
    assert again.render().startswith("already recorded: the LinkedIn message (Larkspur Analytics)")


def test_a_message_with_no_company_links_nothing(conn: sqlite3.Connection) -> None:
    rec = _message(conn, MESSAGE)
    assert rec.job_id is None and rec.company == ""
    assert "about" not in rec.render()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"summary": "Hi Jordan,\nI lead recruiting at Larkspur."}, "one line"),
        ({"summary": ""}, "summary is required"),
        ({"summary": "See https://www.linkedin.com/messaging/thread/1"}, "holds a link"),
        ({"link": "https://www.linkedin.com/messaging/thread/2-abc/"}, "not LinkedIn"),
        ({"link": "javascript:alert(1)"}, "http"),
        (
            {
                "link": "https://nam12.safelinks.protection.outlook.com/?url=https%3A%2F%2F"
                "www.linkedin.com%2Fmessaging%2Fthread%2F2-abc%2F"
            },
            "carries a LinkedIn link",
        ),
        (
            {"link": "https://www.google.com/url?q=https://www.linkedin.com/jobs/view/3912345678"},
            "carries a LinkedIn link",
        ),
        (
            {
                "link": "https://mail.google.com/mail/u/0/?view=x&u=https://www.linkedin.com/"
                "jobs/view/3912345678"
            },
            "carries a LinkedIn link",
        ),
        ({"company": "www.linkedin.com/company/someone"}, "holds a link"),
        (
            {"summary": "A recruiter at Acme asks about job 4300000101"},
            "holds a long number",
        ),
        ({"message_id": "two words"}, "one token"),
        ({"message_id": "https://www.linkedin.com/messaging/thread/2-abc"}, "not a link"),
        ({"message_id": "linkedin.com/jobs/view/3912345678"}, "not a link"),
        ({"message_id": "jobs/view/3912345678"}, "not a link"),
    ],
)
def test_a_bad_message_is_refused_with_nothing_written(
    conn: sqlite3.Connection, change: dict[str, str], message: str
) -> None:
    args = {
        **INMAIL["extracted"],
        "received_at": INMAIL["received_at"],
        "message_id": INMAIL["message_id"],
        "link": INMAIL["link"],
        **change,
    }
    before = _counts(conn)
    with pytest.raises(ValueError, match=message):
        job_alerts.record_linkedin_message(conn, **args)
    assert _counts(conn) == before


def test_a_long_summary_is_cut_to_one_short_line(conn: sqlite3.Connection) -> None:
    rec = job_alerts.record_linkedin_message(conn, "", "word " * 80, "2026-10-20", "m-long")
    assert len(rec.summary) == responses.SUMMARY_MAX and rec.summary.endswith("…")


# ---- nothing from LinkedIn is stored -----------------------------------------------


def test_no_linkedin_url_or_job_id_is_stored_anywhere(conn: sqlite3.Connection) -> None:
    """After recording every fixture email, the board resolver's run and
    the email check, nothing in any table holds a LinkedIn or job-board URL,
    a LinkedIn job id, or a sentence of an email body."""
    _polled_board(conn, "Quillfeather Labs", "greenhouse", "quillfeather")
    submitted(conn, "lark-1", days=3, company=(7, "Larkspur Analytics"))
    _alert(conn, ALERT)
    _alert(conn, INDEED)
    _message(conn, INMAIL)
    _message(conn, MESSAGE)
    rep = _resolve(conn, _email_boards())
    assert rep.ingested >= 2, rep.render()
    responses.finish_email_check(conn, "2026-10-21T10:00:00Z")
    stored = _dump(conn).lower()
    for needle in ("linkedin.com", "lnkd.in", "indeed.com", "jk=", "trackingid", "/jobs/view/"):
        assert needle not in stored, needle
    for li_id in ALERT["linkedin_job_ids"]:
        assert li_id not in stored, li_id
    # no sender's name either: the summary says who wrote by role
    for name in ("morgan vale", "riley ashgrove"):
        assert name not in stored, name
    # and no email body: the bodies' own sentences are not stored
    for msg in (INMAIL, MESSAGE):
        for line in msg["body_text"].splitlines():
            if len(line) > 40:
                assert line.lower() not in stored, line


@pytest.mark.parametrize(
    "message_id",
    [
        "https://www.linkedin.com/jobs/view/3912345678",
        "www.linkedin.com/jobs/view/3912345678",
        "lnkd.in/e3912345678",
        "jobs/view/3912345678",
        "4300000101",
        "job-4300000101",
    ],
)
def test_a_linkedin_link_as_message_id_is_refused_with_nothing_written(
    conn: sqlite3.Connection, message_id: str
) -> None:
    before = _dump(conn)
    item = [{"company": "Acme Widgets", "title": "Account Executive", "location": "Remote"}]
    with pytest.raises(ValueError, match="not a link"):
        job_alerts.record_job_alerts(conn, item, message_id, "2026-10-21T10:00:00Z", "linkedin")
    with pytest.raises(ValueError, match="not a link"):
        job_alerts.record_linkedin_message(
            conn, "Acme Widgets", "A recruiter asks for a call.", "2026-10-21", message_id
        )
    assert _dump(conn) == before


def test_a_message_id_may_be_a_gmail_id_or_an_rfc_message_id(conn: sqlite3.Connection) -> None:
    for mid in ("18f2a6c4d1e0c1ff", "1923456789abcdef", "<abc.123@mail.linkedin.com>"):
        assert job_alerts.clean_message_id(mid) == mid


# ---- the email check ---------------------------------------------------------------


def test_email_targets_suggests_the_alert_search_and_what_to_take(
    conn: sqlite3.Connection,
) -> None:
    t = responses.email_targets(conn)
    assert t.targets == [] and t.query == ""  # no application to look for
    assert t.alert_after == datetime.date(2026, 10, 14)  # a week back on the first check
    assert t.alert_query.startswith("after:2026/10/14 {from:jobalerts-noreply@linkedin.com ")
    for sender in (
        "from:jobs-noreply@linkedin.com",
        "from:messages-noreply@linkedin.com",
        "from:inmail-hit-reply@linkedin.com",
        "from:indeed.com",
        "from:glassdoor.com",
        "from:ziprecruiter.com",
        "from:wellfound.com",
    ):
        assert sender in t.alert_query, sender
    text = t.render()
    assert "nothing to look for" in text  # about applications only
    assert f"Job alerts and LinkedIn messages: suggested Gmail search: {t.alert_query}" in text
    flat = " ".join(text.split())
    for part in (
        "each job's company, title and location",
        "'apply on company site' link",
        "Do not open or follow any link in it",
        "record_job_alerts once per email",
        "the sender's company if shown, and one line in your words about who wrote (by role, "
        "never by name)",
        "record_linkedin_message. Never reply.",
    ):
        assert part in flat, part


def test_messages_since_the_last_check_are_listed_for_the_summary(
    conn: sqlite3.Connection,
) -> None:
    submitted(conn, "lark-1", days=3, company=(7, "Larkspur Analytics"), title="AE")
    _alert(conn, ALERT)
    _message(conn, INMAIL)
    _message(conn, MESSAGE)
    t = responses.email_targets(conn)
    assert t.alert_emails_recorded == 1 and len(t.linkedin_messages) == 2
    text = t.render()
    assert "Since the last check: 1 job-alert email(s) and 2 LinkedIn message(s) recorded." in text
    assert "LinkedIn messages to include in the summary:" in text
    assert (
        "- LinkedIn message (Larkspur Analytics), received 2026-10-20: Larkspur Analytics' head of "
        "recruiting asks for a 20 minute call about the Account Executive role. [application 1 "
        "(Larkspur Analytics — AE, stage submitted)]"
    ) in text
    check = responses.finish_email_check(conn, "2026-10-21T10:00:00Z")
    assert (check.alert_emails_since, check.linkedin_messages_since) == (1, 2)
    assert "0 response(s), 1 job-alert email(s) and 2 LinkedIn message(s) recorded" in (
        check.render()
    )
    after = responses.email_targets(conn)
    assert after.linkedin_messages == [] and after.alert_emails_recorded == 0
    assert after.alert_after == datetime.date(2026, 10, 20)  # a day of overlap
    assert "Since the last check" not in after.render()


# ---- the digest --------------------------------------------------------------------


def test_the_digest_lists_alert_jobs_and_linkedin_messages(
    conn: sqlite3.Connection, jobkit_home: Path
) -> None:
    _polled_board(conn, "Quillfeather Labs", "greenhouse", "quillfeather")
    _alert(conn, ALERT)
    _message(conn, MESSAGE)
    text = digest.write_digest(conn, "2026-10-21").read_text()
    head = "## Jobs from job-alert emails since the last digest (3 kept, from 1 email(s))\n"
    assert head in text
    block = text[text.index(head) + len(head) :].split("\n\n")[0].splitlines()
    assert block == [
        "- Larkspur Analytics — Account Executive, Mid-Market (New York, NY), LinkedIn alert "
        "2026-10-20: waiting for board lookup (the next resolve-boards run)",
        "- Quillfeather Labs — Senior Account Executive (Remote - US), LinkedIn alert "
        "2026-10-20: waiting for board lookup (the next resolve-boards run)",
        "- Cobaltline Health — Revenue Operations Manager (Chicago, IL), LinkedIn alert "
        "2026-10-20: waiting for board lookup (the next resolve-boards run)",
        "- 2 more dropped by the title rules",
    ]
    msgs = "## LinkedIn messages since the last digest (1)\n"
    assert msgs in text
    assert text[text.index(msgs) + len(msgs) :].splitlines()[0] == (
        "- LinkedIn message, received 2026-10-21: A new connection asks whether you are open to "
        "sales operations roles."
    )
    assert text.index("## Review queue") < text.index(head) < text.index("## Funnel")


def test_the_digest_says_nothing_without_alerts_or_messages(conn: sqlite3.Connection) -> None:
    assert job_alerts.digest_lines(conn, "2026-10-01 00:00:00") == []
    assert responses.digest_lines(conn, "2026-10-01 00:00:00") == []


# ---- the MCP tools, the gate and the CLI -------------------------------------------


def test_mcp_tools_are_read_only_and_deterministic(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jobkit import mcp_server

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    for name in ("record_job_alerts", "record_linkedin_message"):
        assert name in tools
        doc = " ".join((getattr(mcp_server, name).__doc__ or "").split())
        assert "Read-only" in doc and "nothing is sent" in doc, name
    out = mcp_server.record_job_alerts(
        ALERT["extracted"], ALERT["message_id"], ALERT["received_at"]
    )
    assert out.startswith("recorded LinkedIn job alert of 2026-10-20: kept 3")
    out = mcp_server.record_job_alerts(
        json.dumps(INDEED["extracted"]), INDEED["message_id"], INDEED["received_at"], "indeed"
    )
    assert out.startswith("recorded Indeed job alert")
    bad = mcp_server.record_job_alerts(
        [{"company": "X", "title": "AE", "link": "https://x.example"}], "m-x", "2026-10-20"
    )
    assert bad.startswith("not recorded: job 1: unexpected field(s) link")
    x = INMAIL["extracted"]
    out = mcp_server.record_linkedin_message(
        x["company"], x["summary"], INMAIL["received_at"], INMAIL["message_id"]
    )
    assert out.startswith("recorded the LinkedIn message (Larkspur Analytics)")
    assert mcp_server.record_linkedin_message("", "two\nlines", "2026-10-20", "m").startswith(
        "not recorded: summary must be one line"
    )


def test_finish_email_check_starts_board_lookup_for_alert_jobs(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same day: the check that recorded alert jobs starts `jobkit same-day
    --resolve-boards` in the background (board lookup, then scoring; no LLM
    and no board request in the MCP call itself) and lists the jobs for the
    summary. A check with no alert job starts nothing; a board lookup or
    nightly run already going is left to do it."""
    from jobkit import background, mcp_server
    from jobkit.pipeline import same_day

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    launched: list[tuple[str, ...]] = []

    def spawn(c: sqlite3.Connection, *args: str, job_id: int | None = None) -> background.Launch:
        launched.append(args)
        return background.Launch(99, 4242, tuple(args), Path("logs/spawn/x.log"))

    monkeypatch.setattr(background, "spawn", spawn)
    monkeypatch.setattr(same_day, "board_lookup_running", lambda c: None)
    out = mcp_server.finish_email_check("2026-10-20T06:00:00Z")
    assert launched == [] and "board lookup" not in out
    mcp_server.record_job_alerts(ALERT["extracted"], ALERT["message_id"], ALERT["received_at"])
    out = mcp_server.finish_email_check("2026-10-21T10:00:00Z")
    assert launched == [("same-day", "--resolve-boards")]
    assert (
        "board lookup for 3 job(s) from job-alert emails: started `jobkit same-day "
        "--resolve-boards` as launch #99"
    ) in out
    assert "Jobs from job-alert emails in this check:" in out
    assert (
        "- Larkspur Analytics — Account Executive, Mid-Market (New York, NY), LinkedIn alert "
        "2026-10-20: waiting for board lookup (the next resolve-boards run)"
    ) in out
    # the next check has no new alert job: nothing started
    assert "board lookup" not in mcp_server.finish_email_check("2026-10-21T11:00:00Z")
    assert len(launched) == 1
    # a resolve-boards run already going does it
    busy = background.SpawnStatus(
        7, ("resolve-boards",), 1, "2026-10-21 11:00:00", Path("x.log"), "running", None
    )
    monkeypatch.setattr(same_day, "board_lookup_running", lambda c: busy)
    mcp_server.record_job_alerts(
        INDEED["extracted"], INDEED["message_id"], INDEED["received_at"], "indeed"
    )
    out = mcp_server.finish_email_check("2026-10-21T12:00:00Z")
    assert len(launched) == 1
    assert "`jobkit resolve-boards` is already running (launch #7)" in out


def test_recording_works_on_request_whatever_the_scheduled_gate_says(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tracking.email_check governs only the unattended run (email_targets
    with scheduled=true); asking Claude to check email always works."""
    from jobkit import mcp_server
    from jobkit.config import load_filters

    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    assert load_filters().email_check is False
    off = mcp_server.email_targets(scheduled=True)
    assert off.startswith("Email not checked") and "jobalerts-noreply" not in off
    assert "jobalerts-noreply@linkedin.com" in mcp_server.email_targets()
    out = mcp_server.record_job_alerts(
        ALERT["extracted"], ALERT["message_id"], ALERT["received_at"]
    )
    assert out.startswith("recorded")


def test_cli_alert_and_message_commands(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from jobkit import cli

    monkeypatch.setattr(cli, "_db", lambda: conn)
    run = CliRunner().invoke
    items = tmp_path / "jobs.json"
    items.write_text(json.dumps(ALERT["extracted"]))
    args = ["email", "alerts", "--items-file", str(items), "--message-id", ALERT["message_id"]]
    res = run(cli.app, [*args, "--received-at", "2026-10-20"])
    assert res.exit_code == 0 and "kept 3" in res.output, res.output
    res = run(cli.app, [*args, "--received-at", "2026-10-20"])
    assert res.exit_code == 0 and res.output.startswith("already recorded")
    res = run(
        cli.app,
        [
            *["email", "alerts", "--items-file", str(tmp_path / "missing.json")],
            *["--message-id", "m2", "--received-at", "2026-10-20"],
        ],
    )
    assert res.exit_code == 1 and "not recorded" in res.output
    msg = ["email", "linkedin-message", "--summary"]
    res = run(cli.app, [*msg, "Asks about sales ops roles.", "--received-at", "2026-10-21"])
    assert res.exit_code == 2  # --message-id is required
    res = run(
        cli.app,
        [*msg, "Asks about sales ops roles.", "--received-at", "2026-10-21", "--message-id", "m3"],
    )
    assert res.exit_code == 0 and "recorded the LinkedIn message, 2026-10-21" in res.output
    res = run(
        cli.app,
        [
            *msg,
            "x",
            *["--received-at", "2026-10-21", "--message-id", "m4"],
            *["--link", "https://www.linkedin.com/messaging/thread/1/"],
        ],
    )
    assert res.exit_code == 1 and "not LinkedIn" in res.output
