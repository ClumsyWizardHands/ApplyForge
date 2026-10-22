"""`jobkit setup-status` (CLI) / MCP `setup_status`: which onboarding steps are
done, in order, with the next action; and the `jobkit setup ...` commands and
MCP tools that drive the interview."""

from __future__ import annotations

import asyncio
import json
import plistlib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobkit import cli, mcp_server
from jobkit import db as q
from jobkit import schedule as launchd
from jobkit.onboarding import mcp_tools
from jobkit.onboarding.answer_bank_builder import build_answer_bank
from jobkit.onboarding.ingest import ingest_resume
from jobkit.onboarding.status import setup_status
from tests.conftest import FIXTURES

TXT = FIXTURES / "resumes" / "sample_resume.txt"
PERSONAL = ("filters", "targets", "autoapprove", "banned_phrases", "commute_table")


@pytest.fixture()
def fresh(jobkit_home: Path) -> Path:
    for name in PERSONAL:
        (jobkit_home / "config" / f"{name}.yaml").unlink()
    return jobkit_home


def _states(root: Path | None = None) -> dict[str, str]:
    return {s.name.split(" (")[0]: s.state for s in setup_status(root=root).steps}


def test_fresh_clone_starts_at_the_resume(fresh: Path) -> None:
    status = setup_status()
    assert status.root == fresh.resolve()
    assert [s.name.split(" (")[0] for s in status.steps] == [
        "resume",
        "career corpus",
        "preferences",
        "target companies",
        "answer bank",
        "database",
        "MCP registration",
        "daily runs",
    ]
    assert all(s.state == "todo" for s in status.steps[:7])
    assert status.steps[7].state == "check"
    assert status.next_step is not None and status.next_step.name == "resume"
    text = status.render()
    assert "Next: resume" in text and "jobkit setup ingest" in text


def test_progress_moves_the_next_step(fresh: Path) -> None:
    ingest_resume(TXT)
    states = _states()
    assert states["resume"] == "done" and states["career corpus"] == "partial"
    nxt = setup_status().next_step
    assert nxt is not None and nxt.name == "career corpus"
    assert "8 of 8 bullets still to confirm" in nxt.detail


def test_test_profile_counts_as_configured(jobkit_home: Path) -> None:
    states = _states()
    assert states["preferences"] == "done"
    assert states["target companies"] == "done"


def _prefs(root: Path | None = None) -> str:
    step = next(s for s in setup_status(root=root).steps if s.name.startswith("preferences"))
    return f"{step.state}: {step.detail}"


def test_preferences_say_how_automatic_the_daily_run_is(jobkit_home: Path) -> None:
    # the test profile (like the owner's config): two rules, no limit, forms
    # not opened after the run
    assert _prefs() == (
        "done: filters and companion config load; automatic approval: 2 rules; "
        "forms open after the daily run: off"
    )
    auto = jobkit_home / "config" / "autoapprove.yaml"
    auto.write_text(auto.read_text() + "\nmax_auto_approve_per_day: 5\n")
    filters = jobkit_home / "config" / "filters.yaml"
    filters.write_text(filters.read_text() + "\nstaging:\n  open_after_daily_run: true\n")
    assert _prefs().endswith("2 rules, at most 5 a day; forms open after the daily run: on")
    auto.write_text('version: "1.0"\nauto_approve: []\nauto_reject: []\n')
    assert "automatic approval: off" in _prefs()


def test_a_bad_daily_limit_leaves_preferences_partial(jobkit_home: Path) -> None:
    auto = jobkit_home / "config" / "autoapprove.yaml"
    auto.write_text(auto.read_text() + "\nmax_auto_approve_per_day: 0\n")
    out = _prefs()
    assert out.startswith("partial:") and "max_auto_approve_per_day" in out


def test_answer_bank_partial_then_done(fresh: Path) -> None:
    build_answer_bank({"identity.first_name": "Morgan"})
    step = next(s for s in setup_status().steps if s.name.startswith("answer bank"))
    assert step.state == "partial" and "screening.work_auth" in step.detail


def test_hand_written_corpus_without_confidence_is_partial(fresh: Path) -> None:
    (fresh / "data" / "career_corpus.yaml").write_text(
        "bullets:\n- {id: b_1, role_id: r, text_long: x, metric_confidence: null}\n"
    )
    step = next(s for s in setup_status().steps if s.name == "career corpus")
    assert step.state == "partial" and "no metric_confidence" in step.detail


def test_database_and_daily_runs(fresh: Path) -> None:
    q.connect(create=True).close()
    assert _states()["database"] == "done"
    plist = launchd.plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps({"Label": launchd.LABEL, "WorkingDirectory": "/elsewhere"}))
    step = setup_status().steps[-1]
    assert step.state == "check" and "another checkout" in step.detail
    plist.write_bytes(plistlib.dumps({"Label": launchd.LABEL, "WorkingDirectory": str(fresh)}))
    assert setup_status().steps[-1].state == "done"


def test_status_never_raises_on_broken_files(fresh: Path) -> None:
    (fresh / "data" / "career_corpus.draft.yaml").write_text("bullets: [unclosed\n")
    (fresh / "config" / "filters.yaml").write_text("filters: {tracks: [{id: x}]}\n")
    states = _states()
    assert states["career corpus"] == "partial"
    assert states["preferences"] == "todo"


# ---------------------------------------------------------------------------
# CLI and MCP surfaces
# ---------------------------------------------------------------------------


def test_cli_setup_status_and_flow(fresh: Path) -> None:
    runner = CliRunner()
    res = runner.invoke(cli.app, ["setup-status"])
    assert res.exit_code == 0, res.output
    assert "Next: resume" in res.output
    res = runner.invoke(cli.app, ["setup", "ingest", str(TXT)])
    assert res.exit_code == 0, res.output
    assert "8 bullets" in res.output
    res = runner.invoke(cli.app, ["setup", "draft", "--batch", "2"])
    assert res.exit_code == 0 and "Next 2 bullets" in res.output
    bad = json.dumps([{"id": "b_001", "action": "keep", "metric_confidence": "verified"}])
    res = runner.invoke(cli.app, ["setup", "confirm", bad])
    assert res.exit_code == 1 and "evidence is required" in res.output
    good = json.dumps([{"id": "b_004", "action": "drop"}])
    res = runner.invoke(cli.app, ["setup", "confirm", good])
    assert res.exit_code == 0 and "applied: 1 dropped" in res.output
    res = runner.invoke(cli.app, ["setup", "promote"])
    assert res.exit_code == 1 and "not promoted" in res.output
    res = runner.invoke(cli.app, ["setup", "questions"])
    assert res.exit_code == 0 and "screening.sponsorship" in res.output
    res = runner.invoke(cli.app, ["setup", "status"])
    assert res.exit_code == 0 and "career corpus" in res.output


def test_cli_reads_answer_files(fresh: Path, tmp_path: Path) -> None:
    answers = tmp_path / "answers.yaml"
    answers.write_text("identity.first_name: Morgan\nscreening.sponsorship: 'no'\n")
    res = CliRunner().invoke(cli.app, ["setup", "answers", str(answers)])
    assert res.exit_code == 0, res.output
    assert "saved 2 answer(s)" in res.output


def test_every_setup_tool_is_on_the_mcp_server() -> None:
    tools = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    expected = {fn.__name__ for fn in mcp_tools.TOOLS}
    assert expected <= tools
    assert expected == {
        "setup_status",
        "ingest_resume",
        "corpus_draft_status",
        "confirm_bullets",
        "update_corpus_draft",
        "promote_corpus",
        "answer_bank_questions",
        "build_answer_bank",
        "run_intake",
        "seed_targets",
    }


def test_mcp_setup_status_and_ingest(fresh: Path) -> None:
    assert "Next: resume" in mcp_tools.setup_status()
    out = mcp_tools.ingest_resume(path=str(TXT))
    assert "Draft: data/career_corpus.draft.yaml" in out
    assert "Next 5 bullets" in mcp_tools.corpus_draft_status()


def test_feed_only_targets_count_as_done_with_a_warning(fresh: Path) -> None:
    from jobkit.onboarding.targets import seed_targets

    seed_targets([], feed_only_said="my employers use Workday and iCIMS")
    status = setup_status()
    step = next(s for s in status.steps if s.name.startswith("target companies"))
    assert step.state == "done"
    assert "feeds only" in step.detail
    assert step.warning == "0 polled boards; postings come from the feeds"
    assert "! 0 polled boards; postings come from the feeds" in status.render()


def test_targets_without_boards_or_feed_only_stay_partial(fresh: Path) -> None:
    (fresh / "config" / "targets.yaml").write_text(
        'companies:\n  - { name: "City Hospital", tier: 1 }\n'
    )
    step = next(s for s in setup_status().steps if s.name.startswith("target companies"))
    assert step.state == "partial" and "feed_only_said" in step.next_action


def test_cli_and_mcp_feed_only(fresh: Path) -> None:
    res = CliRunner().invoke(
        cli.app, ["setup", "targets", "--feed-only", "the district uses Frontline"]
    )
    assert res.exit_code == 0, res.output
    assert "discovery comes from the feeds" in res.output
    assert _states()["target companies"] == "done"
    out = mcp_tools.seed_targets(feed_only_said="feeds only, still")
    assert "feeds only, still" in out
