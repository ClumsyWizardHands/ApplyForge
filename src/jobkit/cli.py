"""CLI — exists for cron and debugging. The MCP server (M3) is the primary interface."""

from __future__ import annotations

import asyncio
import sqlite3

import typer

from jobkit import db as q
from jobkit import paths
from jobkit import schedule as launchd
from jobkit.autofill.browser import stage_jobs
from jobkit.autofill.form_spec import capture_form_specs
from jobkit.config import ConfigMissing
from jobkit.discovery import board_resolver, linkedin_guest
from jobkit.discovery.ingest import ingest_url
from jobkit.discovery.registry import resolve_discovery_queue
from jobkit.discovery.url_resolver import resolve as resolve_url
from jobkit.onboarding import cli as onboarding_cli
from jobkit.onboarding.calibrate import calibrate as run_calibration
from jobkit.pipeline import self_review
from jobkit.pipeline.dedup import merge_feed_dupes
from jobkit.pipeline.digest import freshness_banner, write_digest
from jobkit.pipeline.feeds import run_feeds
from jobkit.pipeline.filter import refilter as run_refilter
from jobkit.pipeline.filter import run_filters, unsnooze
from jobkit.pipeline.maintenance import purge_empty_jd_cache
from jobkit.pipeline.nightly import NightlyBusy, nightly_stages, run_nightly
from jobkit.pipeline.poll import poll as poll_boards
from jobkit.pipeline.runner import Stage, run_stages
from jobkit.pipeline.tailor import figures as figures_check
from jobkit.pipeline.tailor import tailor_approved, tailor_job
from jobkit.pipeline.triage import (
    run_comp_check,
    run_extract,
    run_queue,
    run_remote_check,
    run_score,
)
from jobkit.track import responses
from jobkit.track.applications import close_ghosted

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def _entry() -> None:
    """jobkit: discover, sanity-check, tailor, stage. You click submit.

    Every command runs from the project root (JOBKIT_HOME, else this checkout)
    with its .env loaded, whatever directory it was started from."""
    paths.enter_home()


def _db() -> sqlite3.Connection:
    """The jobkit DB with its schema current. A missing DB is an error that
    names `jobkit init`; it is never created here (audit 2026-10-08 §3 item 2)."""
    try:
        conn = q.connect()
    except q.DatabaseMissing as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    q.init_db(conn)
    return conn


@app.command()
def doctor() -> None:
    """Check this checkout is ready to run (read-only): Python/uv, Playwright
    Chromium, the LLM backend, checkout location, config and data files, the
    database and the MCP registration (warnings: contact email, commute times
    for allowed places). Prints a fix for each problem; exits 1 when one needs
    fixing."""
    from jobkit.doctor import run_checks

    report = run_checks()
    typer.echo(report.render())
    if not report.ok:
        raise typer.Exit(1)


@app.command()
def init() -> None:
    """Create the SQLite database and schema (the only command that creates it)."""
    conn = q.connect(create=True)
    q.init_db(conn)
    typer.echo(f"initialized {q.DB_PATH}")


@app.command()
def poll() -> None:
    """Poll all configured boards once (idempotent)."""
    conn = _db()
    stats = asyncio.run(poll_boards(conn))
    typer.echo(
        f"boards={stats['boards']} ok={stats['ok']} 304={stats['not_modified']} "
        f"failed={stats['failed']} jobs_seen={stats['jobs_seen']} new={stats['jobs_new']}"
    )


@app.command()
def feeds() -> None:
    """Run all enabled aggregator feeds once (cross-source deduped)."""
    conn = _db()
    results = asyncio.run(run_feeds(conn))
    for source, s in results.items():
        typer.echo(
            f"{source}: seen={s.get('seen', 0)} merged={s.get('merged', 0)} "
            f"new={s.get('new', 0)} rejected={s.get('rejected', 0)}"
        )


@app.command(name="linkedin-discover")
def linkedin_discover(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="today's plan and budget only; no request to LinkedIn, no writes"
    ),
) -> None:
    """LinkedIn's logged-out ("guest") job search, discovery only (hard rule 11
    exception, 2026-10-12; off unless feeds.linkedin_guest.enabled). A few
    searches a day with an honest User-Agent, no login, no cookies; stops for
    the day on any block or challenge. A card becomes a job only when its
    posting page carries an employer apply link on a supported ATS (the add-url
    path); otherwise only its company name goes to the discovery queue. No
    LinkedIn job row is ever stored. One run at a time: while another run (this
    command, or the nightly / run-all linkedin-guest stage) holds the run lock,
    it sends nothing and reports busy. Exits 1 when a request failed (not on a
    block, not when busy)."""
    rep = linkedin_guest.run(_db(), dry_run=dry_run)
    typer.echo(rep.render())
    if rep.status == "error":
        raise typer.Exit(1)


@app.command(name="resolve-boards")
def resolve_boards(
    job_id: int = typer.Option(0, "--job-id", help="only this aggregator job (any status)"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="read the board APIs, write nothing (no ingest, merge or flag)"
    ),
    pick: str = typer.Option(
        "",
        "--pick",
        help="with --job-id: your pick among its flagged postings (posting id or URL), or any "
        "Greenhouse/Lever/Ashby posting URL",
    ),
) -> None:
    """Match aggregator jobs (and LinkedIn cards whose company was queued) to
    the employer's own Greenhouse/Lever/Ashby posting: the company's boards
    in jobkit, the registry, then, only for a company with no board there, a
    slug probe (acme, acme-careers, ...; cached per company for
    board_resolution.probe_cache_days). A posting is picked only when its
    title is the same AND its location agrees, and a board found by name (a
    registry, discovered or probe-found board; only config/targets.yaml
    boards and the apply-URL board are trusted) must be confirmed as the
    company's (Greenhouse's same company name, or a link to the company's
    domain on the board); one picked is ingested and the feed row merged
    into it (the ATS row wins). None or several agreeing: nothing is picked
    and the job is flagged board-match-ambiguous; an unconfirmed board:
    board-match-unconfirmed (digest, MCP get_queue / resolve_boards); `--pick`
    takes your choice. Also the nightly stage resolve-boards. Exits 1 when a
    request failed or a pick did not merge."""
    conn = _db()
    if pick:
        if not job_id:
            typer.echo("--pick needs --job-id (the aggregator job the posting is for)", err=True)
            raise typer.Exit(1)
        merged, text = board_resolver.pick(conn, job_id, pick)
        typer.echo(text)
        if not merged:
            raise typer.Exit(1)
        return
    rep = board_resolver.resolve(conn, job_ids=[job_id] if job_id else None, dry_run=dry_run)
    typer.echo(rep.render())
    if rep.errors:
        raise typer.Exit(1)


@app.command()
def filter() -> None:
    """Deterministic filter pass over status=new jobs (free, logged)."""
    conn = _db()
    s = run_filters(conn)
    typer.echo(f"examined={s['examined']} passed={s['passed']} rejected={s['rejected']}")


@app.command()
def refilter(
    dry_run: bool = typer.Option(False, "--dry-run", help="count only; change nothing"),
) -> None:
    """Re-run the filter on open jobs it rejected for title, paused track, comp
    or location (after a config change, e.g. a track set back to active: true).
    Resets them to new, logs a `refiltered` event per job, then runs the
    filter pass."""
    conn = _db()
    typer.echo(run_refilter(conn, dry_run=dry_run).render())


@app.command()
def triage(limit: int = typer.Option(25, help="max jobs per LLM stage this run")) -> None:
    """LLM triage: extract -> remote check -> comp sanity -> score -> queue."""
    conn = _db()
    typer.echo(f"extract: {run_extract(conn, limit)}")
    typer.echo(f"remote:  {run_remote_check(conn, limit)}")
    typer.echo(f"comp:    {run_comp_check(conn, limit)}")
    typer.echo(f"score:   {run_score(conn, limit)}")
    typer.echo(f"queued:  {run_queue(conn)}")


@app.command(name="form-specs")
def form_specs(limit: int = typer.Option(50, help="max forms to capture this run")) -> None:
    """Capture application form structures for queued jobs; match + draft overnight."""
    conn = _db()
    s = capture_form_specs(conn, limit)
    typer.echo(
        f"captured={s['captured']} dom_fallback={s['dom_fallback']} "
        f"novel={s['novel_total']} drafted={s['drafted']} "
        f"fact_check_failed={s['fact_check_failed']} posting_gone={s['posting_gone']}"
    )


@app.command()
def tailor(job_id: int = typer.Option(0, help="specific job id; 0 = all approved")) -> None:
    """Tailor resumes + cover letters for approved jobs (provenance-gated).
    An approved aggregator job is first matched to the employer's own posting
    (resolve-boards; MCP approve starts this command), so the ATS row is the
    one tailored and staged."""
    conn = _db()
    pre, mapped = board_resolver.before_tailoring(conn, job_id or None)
    if pre is not None:
        typer.echo(pre.render())
    if job_id and mapped and mapped != job_id:
        typer.echo(f"job {job_id} was merged into job {mapped}: tailoring job {mapped}")
        job_id = mapped
    if job_id:
        artifacts = tailor_job(conn, job_id)
        for k, v in artifacts.items():
            typer.echo(f"{k}: {v}")
    else:
        typer.echo(f"tailored {tailor_approved(conn)} applications")


@app.command()
def figures(
    bullet_id: str = typer.Option("", "--bullet-id", help="check one bullet (e.g. b_021)"),
    rendered: str = typer.Option("", help="a rendering of --bullet-id to check"),
) -> None:
    """How numbers render (config/banned_phrases.yaml drift.estimate_style),
    and which corpus bullets pass provenance word for word under it. No LLM."""
    typer.echo(figures_check(bullet_id, rendered))


@app.command()
def review(job_id: int) -> None:
    """Latest authenticity verdicts for a job's artifacts."""
    conn = _db()
    for r in conn.execute(
        """SELECT artifact, verdict, lexical_flags, structural_flags, drift_flags, critic_notes
           FROM authenticity_runs WHERE job_id=? ORDER BY id DESC LIMIT 4""",
        (job_id,),
    ):
        typer.echo(f"{r['artifact']}: {r['verdict']}")
        for k in ("lexical_flags", "structural_flags", "drift_flags"):
            if r[k] and r[k] != "[]":
                typer.echo(f"  {k}: {r[k][:200]}")
        if r["critic_notes"]:
            typer.echo(f"  critic: {r['critic_notes'][:200]}")
    for f in conn.execute(q.PROVENANCE_EVENTS_FOR_JOB, {"job_id": job_id}):
        typer.echo(f"{f['kind']} [{f['at']}]: REJECTED — {f['detail'][:400]}")


@app.command(name="self-review")
def self_review_cmd(
    job_id: int,
    full: bool = typer.Option(False, help="also print the job's self_review.md"),
) -> None:
    """What the self-review asked itself about a job's drafted artifacts, its
    verdicts, what changed (or was discarded), and the questions it raised for
    you. Read-only; the review itself runs inside tailor and form-specs."""
    from jobkit.pipeline import tailor as tailor_mod

    typer.echo(self_review.summary(_db(), job_id, tailor_mod.OUT_DIR, full=full))


@app.command(name="questions")
def questions_cmd(
    show_all: bool = typer.Option(
        False, "--all", help="every open question, ranked (not counted against the daily cap)"
    ),
) -> None:
    """Questions for you: facts the corpus lacks that would close gaps in open
    queued/approved jobs (self-review questions + recurring scorer gaps),
    deduped and ranked by jobs helped. At most questions.max_per_day a day
    (config/filters.yaml). Then the proposals waiting for your confirmation.
    No LLM call."""
    from jobkit.pipeline import questions

    typer.echo(questions.render(_db(), all_=show_all))


@app.command(name="answer")
def answer_cmd(
    key: str,
    text: str = typer.Argument("", help="your answer, in your words (optional with --decline)"),
    evidence: str = typer.Option("", help="where this is defensible from (you set it)"),
    metric_confidence: str = typer.Option(
        "", "--metric-confidence", help="verified | estimate | none (you set it)"
    ),
    role_id: str = typer.Option("", "--role-id", help="the corpus role a bullet belongs to"),
    kind: str = typer.Option("", help="skill | bullet (default: skill for a tool or method)"),
    decline: bool = typer.Option(False, "--decline", help="decline the question explicitly"),
) -> None:
    """Answer question KEY. An answer that is only a "no" ("no", "never used
    it", "I have never worked in healthcare"), or --decline, is recorded as a decline:
    not asked again, the gap stays a gap (`jobkit undecline KEY` takes it
    back). An answer that may be a no or a fact ("Never below 95% on
    patient-satisfaction surveys")
    is refused, and so is a tool or method answer that is not one plain
    sentence of your own use: "[yes,] I used <tool>" (or I've used, I have
    used, I use, I ran; "it" for the tool), then only how often, how long,
    at which of your corpus companies and for what ("I used Clari weekly at
    Haulmark for two years"). "Yes, daily", "My manager used Clari" and
    "Clari? Only heard of it" are refused. Only a tool or method question
    can add a skill_index item, and an answer to any other question that
    names a tool or method ("Yes, I trained 40 new hires; the team used Clari")
    is refused: answer the tool's own question instead.
    Any other answer becomes a corpus PROPOSAL in
    data/corpus_proposals.yaml, in your words. Nothing reaches
    data/career_corpus.yaml until `jobkit confirm-proposal <id>`."""
    from jobkit.pipeline import questions

    res = questions.answer(
        _db(),
        key,
        text,
        evidence=evidence,
        metric_confidence=metric_confidence,
        role_id=role_id,
        kind=kind,
        decline=decline,
    )
    typer.echo(res.text)
    if not res.ok:
        raise typer.Exit(1)


@app.command(name="undecline")
def undecline_cmd(key: str) -> None:
    """Take back your decline of question KEY: it is open again and can be
    answered. Nothing is written to the corpus."""
    from jobkit.pipeline import questions

    res = questions.undecline(_db(), key)
    typer.echo(res.text)
    if not res.ok:
        raise typer.Exit(1)


@app.command(name="confirm-proposal")
def confirm_proposal_cmd(
    proposal_id: int,
    evidence: str = typer.Option("", help="set or replace the proposal's evidence"),
    metric_confidence: str = typer.Option(
        "", "--metric-confidence", help="verified | estimate | none"
    ),
    role_id: str = typer.Option("", "--role-id", help="the corpus role a bullet belongs to"),
) -> None:
    """Write proposal ID into data/career_corpus.yaml (a skill_index item or a
    bullet), with the evidence and metric_confidence you set (never
    defaulted), then re-score the jobs its question listed in the background."""
    from jobkit.pipeline import questions

    res = questions.confirm(
        _db(),
        proposal_id,
        evidence=evidence,
        metric_confidence=metric_confidence,
        role_id=role_id,
    )
    typer.echo(res.text)
    if not res.ok:
        raise typer.Exit(1)


@app.command(name="rescore")
def rescore_cmd(
    job_ids: str = typer.Option("", "--job-ids", help="comma-separated job ids; default: stale"),
    dry_run: bool = typer.Option(False, "--dry-run", help="list only; no LLM call"),
) -> None:
    """Re-score open jobs scored against an older corpus (queued, filtered,
    rejected only by the score gate, approved), or the jobs given; re-apply the
    queue gate and the standing rules. Approved and applied jobs keep their
    status (their new score is reported). A score-gate rejection the current
    filters reject stays rejected (a filter rejection is recorded)."""
    from jobkit.pipeline.rescore import rescore

    try:
        ids = [int(x) for x in job_ids.replace(",", " ").split()]
    except ValueError:
        typer.echo(f"--job-ids must be numbers like '12,15', got {job_ids!r}", err=True)
        raise typer.Exit(1) from None
    typer.echo(rescore(_db(), ids or None, dry_run=dry_run).render())


@app.command(name="rejudge")
def rejudge_cmd(
    job_ids: str = typer.Option(
        "", "--job-ids", help="comma-separated job ids; default: every job not submitted"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="list only; no LLM call"),
) -> None:
    """Check stored cover letters and drafted answers the claim judge never
    read (written before it, or under an older judge prompt): the rule-based
    fact check, then the judge. A failed letter goes to cover_rejected.md and
    its application back to approved; a failed answer back to your open
    questions. Answers you gave are never judged. Exits 1 when the judge could
    not be reached for some text (run again)."""
    from jobkit.pipeline.rejudge import rejudge

    try:
        ids = [int(x) for x in job_ids.replace(",", " ").split()]
    except ValueError:
        typer.echo(f"--job-ids must be numbers like '12,15', got {job_ids!r}", err=True)
        raise typer.Exit(1) from None
    report = rejudge(_db(), ids or None, dry_run=dry_run)
    typer.echo(report.render())
    if report.unjudged:
        raise typer.Exit(1)


@app.command()
def stage(
    job_id: int = typer.Option(0, help="specific job; 0 = all tailored approved"),
    job_ids: str = typer.Option("", help="comma-separated batch (one browser, one profile)"),
    keep_open: bool = typer.Option(False, help="leave browser open for review"),
) -> None:
    """Fill approved application forms in a visible browser and stop before submit."""
    conn = _db()
    if job_ids:
        ids = [int(x) for x in job_ids.replace(",", " ").split()]
    elif job_id:
        ids = [job_id]
    else:
        ids = [
            r["id"]
            for r in conn.execute(
                """SELECT DISTINCT j.id FROM jobs j JOIN applications a ON a.job_id=j.id
               WHERE a.stage='tailored' AND a.resume_path IS NOT NULL"""
            )
        ]
    if not ids:
        typer.echo("nothing tailored to stage")
        raise typer.Exit()
    for r in stage_jobs(conn, ids, keep_open=keep_open):
        changed = r.get("overwritten_after_fill") or []
        typer.echo(
            f"job {r['job_id']}: {r['status']} "
            f"({r.get('filled', 0)} filled, {r.get('unfilled', 0)} open)"
            + (f" — {r.get('reason')}" if r.get("reason") else "")
            + (
                f" — read-back: {r['readback_mismatches']} changed after filling"
                + (f", still wrong after a refill: {', '.join(changed)}" if changed else "")
                if r.get("readback_mismatches")
                else ""
            )
        )


@app.command(name="open-queue")
def open_queue_cmd() -> None:
    """Open every application ready for you — latest staging run 'staged', or
    'needs_human' with fields filled — in tabs, staged first, then cheapest
    friction. You click."""
    conn = _db()
    rows = conn.execute(q.OPEN_QUEUE_JOBS).fetchall()
    if not rows:
        typer.echo("nothing staged or waiting on you (stage forms first: jobkit stage)")
        raise typer.Exit()
    for r in rows:
        typer.echo(f"opening job {r['job_id']}: {r['company_name']} — {r['title']} ({r['status']})")
    stage_jobs(conn, [int(r["job_id"]) for r in rows], keep_open=True)


def _run_all_stages() -> list[Stage]:
    """run-all's stages, in order. Each one runs isolated (pipeline/runner.py)."""
    from jobkit.rules import run_standing_rules

    return [
        ("poll", lambda c: asyncio.run(poll_boards(c))),
        ("feeds", lambda c: asyncio.run(run_feeds(c))),
        # off unless feeds.linkedin_guest.enabled; a block is an outcome, not a failure
        ("linkedin-guest", lambda c: linkedin_guest.run(c)),
        ("discovery", resolve_discovery_queue),
        # aggregator jobs / LinkedIn cards -> the employer's posting, before
        # filter and triage so the employer's JD is the one scored
        ("resolve-boards", board_resolver.run_stage),
        ("filter", run_filters),
        ("extract", lambda c: run_extract(c, 100)),
        ("remote", lambda c: run_remote_check(c, 100)),
        ("comp", lambda c: run_comp_check(c, 100)),
        ("score", lambda c: run_score(c, 100)),
        ("unsnooze", unsnooze),
        ("queue", run_queue),
        ("form-specs", lambda c: capture_form_specs(c, 50)),  # BEFORE rules: friction feeds them
        ("rules", run_standing_rules),
        ("form-specs-approved", lambda c: capture_form_specs(c, 50)),  # auto-approved stragglers
        ("tailor", tailor_approved),
        # no reply after tracking.ghost_after_days: stage 'ghosted' (no LLM)
        ("ghost-close", lambda c: close_ghosted(c)),
        ("digest", write_digest),
    ]


@app.command(name="run-all")
def run_all() -> None:
    """The full background pipeline: poll -> feeds -> LinkedIn guest discovery
    (when enabled) -> discovery -> resolve-boards -> filter -> triage ->
    form-specs -> standing rules -> tailor approved -> ghost-close -> digest.
    Cron entrypoint. Each stage runs isolated (a failing stage does not stop
    the rest) and is recorded as a run_all.stage event; exits 1 if any stage
    failed or saw every one of its LLM calls fail."""
    conn = _db()
    outcomes = run_stages(conn, _run_all_stages(), echo=typer.echo)
    failed = [o.stage for o in outcomes if not o.ok]
    if failed:
        typer.echo(f"run-all: {len(failed)} stage(s) failed: {', '.join(failed)}", err=True)
        raise typer.Exit(1)


@app.command()
def nightly() -> None:
    """The one unattended run a night (launchd entrypoint: `jobkit schedule
    install`). run-all's stages (poll -> feeds -> LinkedIn guest discovery
    (when enabled) -> discovery -> resolve-boards -> filter ->
    triage -> form-specs -> standing rules -> tailor -> ghost-close), then
    stage every approved, tailored job not staged since it was tailored (one
    browser process, closed when done, so the profile lock is released), then
    the digest. Never submits: staged forms wait for you (open-queue). Each
    stage runs isolated and is recorded (mode nightly); exits 1 if any stage
    failed, or if another nightly run is still running."""
    conn = _db()
    try:
        outcomes = run_nightly(conn, nightly_stages(_run_all_stages()), echo=typer.echo)
    except NightlyBusy as e:
        typer.echo(f"nightly: {e}", err=True)
        raise typer.Exit(1) from e
    failed = [o.stage for o in outcomes if not o.ok]
    if failed:
        typer.echo(f"nightly: {len(failed)} stage(s) failed: {', '.join(failed)}", err=True)
        raise typer.Exit(1)
    typer.echo(f"nightly: all {len(outcomes)} stages ok")


schedule_app = typer.Typer(
    no_args_is_help=True,
    help="The nightly run under macOS launchd (com.jobkit.nightly). Other OSes: not "
    "supported yet; run `jobkit nightly` from cron or a systemd timer.",
)
app.add_typer(schedule_app, name="schedule")


@schedule_app.command("install")
def schedule_install(
    dry_run: bool = typer.Option(False, "--dry-run", help="print the plist; install nothing"),
) -> None:
    """Install (or replace) the LaunchAgent that runs `jobkit nightly` at
    schedule.nightly_at (config/filters.yaml, default 02:00). Refuses a
    checkout under ~/Desktop, ~/Documents, ~/Downloads or iCloud Drive."""
    try:
        typer.echo(launchd.install(dry_run=dry_run))
    except launchd.ScheduleError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e


@schedule_app.command("uninstall")
def schedule_uninstall(
    dry_run: bool = typer.Option(False, "--dry-run", help="list what would be removed"),
) -> None:
    """Unload and delete the nightly LaunchAgent (and the old agents
    scripts/install_schedule.sh installed)."""
    try:
        typer.echo(launchd.uninstall(dry_run=dry_run))
    except launchd.ScheduleError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e


@schedule_app.command("status")
def schedule_status() -> None:
    """Installed? Loaded in launchd (state, runs, last exit code)? The last
    nightly run stage by stage, the last successful one, and the jobs a
    staging run stopped on (CAPTCHA, ID/payment field, no form) that wait on
    you, with reason, screenshot and apply URL. Read-only."""
    try:
        conn: sqlite3.Connection | None = q.connect()
    except q.DatabaseMissing:
        conn = None
    text, supported = launchd.status(conn)
    typer.echo(text)
    if not supported:
        raise typer.Exit(1)


@app.command(name="add-url")
def add_url(
    url: str,
    score: bool = typer.Option(
        True,
        "--score/--no-score",
        help="extract and score the queued job now (--no-score prints `needs scoring: [id]` "
        "instead; MCP add_job_url then scores it in the background)",
    ),
) -> None:
    """Ingest a pasted job URL (from anywhere — an email alert, a friend, a
    board). Resolves the employer's ATS from the URL shape; a Greenhouse,
    Lever or Ashby board is polled now, the job is queued and then extracted
    and scored, so it shows in the queue like any other."""
    conn = _db()
    if resolve_url(url) is None:
        # many hospitals, schools and agencies link from their own careers
        # domain: say what to do, and exit 0 (nothing failed)
        typer.echo(
            "jobkit can't read this site, so nothing was added. Apply on the employer's site."
        )
        raise typer.Exit()
    res = asyncio.run(ingest_url(conn, url))
    hit = res.resolved
    assert hit is not None
    typer.echo(
        f"recognized: {hit.ats} board={hit.token or '?'} job={hit.external_id or '?'}"
        + (f" ({hit.note})" if hit.note else "")
    )
    if res.poll is None:
        if hit.supported:  # the system is readable but the link names no board
            typer.echo("no board name in that link, so nothing was added")
        raise typer.Exit()
    typer.echo(f"polled board: {res.poll.describe()}")
    if res.status == "poll_failed":
        typer.echo("the board could not be fetched (see logs/jobkit.jsonl poll.board_failed)")
        raise typer.Exit(1)
    if hit.external_id:
        row = res.job
        if row:
            conn.execute(q.QUEUE_PASTED_JOB, {"job_id": row["id"]})
            conn.commit()
            typer.echo(f"queued: [{row['id']}] {row['company_name']} — {row['title']}")
            _score_pasted(conn, int(row["id"]), now=score)
        else:
            typer.echo(
                "board polled but that job id wasn't in the public feed (may be closed or unlisted)"
            )


# printed by `add-url --no-score` for a queued job with no score yet; MCP
# add_job_url reads it and starts `jobkit score-jobs <id>` in the background
NEEDS_SCORING = "needs scoring: [{job_id}]"


def _score_pasted(conn: sqlite3.Connection, job_id: int, *, now: bool) -> None:
    """A pasted job skips the filter and triage stages (they read only status
    'filtered'), and the queue lists only scored jobs: extract and score it
    here, or say it still needs it."""
    if conn.execute("SELECT 1 FROM scores WHERE job_id = ?", (job_id,)).fetchone():
        return
    if not now:
        typer.echo(NEEDS_SCORING.format(job_id=job_id))
        return
    _echo_scores(conn, [job_id])


def _echo_scores(conn: sqlite3.Connection, job_ids: list[int]) -> None:
    from jobkit.pipeline.triage import extract_and_score

    scored = extract_and_score(conn, job_ids)
    for jid in job_ids:
        if jid in scored:
            typer.echo(f"scored: [{jid}] fit {scored[jid]}")
        else:
            typer.echo(
                f"not scored: [{jid}] (already scored, or its description is missing or too "
                "short; see logs/jobkit.jsonl)"
            )


@app.command(name="score-jobs")
def score_jobs(job_ids: list[int]) -> None:
    """Extract and score these jobs now, whatever their status. Used for a
    pasted job (MCP add_job_url starts it in the background). Jobs already
    scored are left as they are."""
    _echo_scores(_db(), job_ids)


@app.command(name="ghost-close")
def ghost_close(
    dry_run: bool = typer.Option(False, "--dry-run", help="list only; change nothing"),
) -> None:
    """Mark submitted applications with no reply after tracking.ghost_after_days
    (config/filters.yaml, default 30) as ghosted. The nightly run does this;
    --dry-run lists them first."""
    conn = _db()
    typer.echo(close_ghosted(conn, dry_run=dry_run).render())


email_app = typer.Typer(
    no_args_is_help=True,
    help="Replies in your own mailbox, recorded by hand (debugging; the MCP tools "
    "email_targets, match_email, record_email_response and finish_email_check are the "
    "normal path). Nothing is ever sent.",
)
app.add_typer(email_app, name="email")


@email_app.command("targets")
def email_targets_cmd() -> None:
    """Submitted applications to look for, the last check and a Gmail search."""
    typer.echo(responses.email_targets(_db()).render())


@email_app.command("match")
def email_match_cmd(
    sender: str = typer.Option(..., help="the From line"),
    subject: str = typer.Option("", help="the subject"),
    snippet: str = typer.Option("", help="a short snippet"),
    received_at: str = typer.Option("", "--received-at", help="when it arrived"),
) -> None:
    """Rank the applications one email could belong to."""
    try:
        typer.echo(responses.match_email(_db(), sender, subject, snippet, received_at).render())
    except ValueError as e:
        typer.echo(f"no match run: {e}", err=True)
        raise typer.Exit(1) from None


@email_app.command("record")
def email_record_cmd(
    application_id: int = typer.Option(..., "--application-id"),
    outcome: str = typer.Option(..., help=", ".join(responses.OUTCOMES)),
    received_at: str = typer.Option(..., "--received-at", help="when it arrived"),
    message_id: str = typer.Option(..., "--message-id", help="the mailbox's id for it"),
    link: str = typer.Option("", help="a link to the message"),
    summary: str = typer.Option("", help="one line in your words (160 characters)"),
) -> None:
    """Record one email's outcome for one application. The body is never stored."""
    try:
        rec = responses.record_response(
            _db(), application_id, outcome, received_at, message_id, link, summary
        )
    except ValueError as e:
        typer.echo(f"not recorded: {e}", err=True)
        raise typer.Exit(1) from None
    typer.echo(rec.render())


@email_app.command("done")
def email_done_cmd(
    checked_through: str = typer.Option(
        "", "--checked-through", help="ISO date-time the mailbox was read through; default now"
    ),
) -> None:
    """Record that the mailbox was checked through a time."""
    try:
        typer.echo(responses.finish_email_check(_db(), checked_through).render())
    except ValueError as e:
        typer.echo(f"not recorded: {e}", err=True)
        raise typer.Exit(1) from None


@app.command()
def calibrate() -> None:
    """Decision-log analysis -> numbered proposals. Nothing applied automatically."""
    conn = _db()
    typer.echo(run_calibration(conn).render())


@app.command()
def digest() -> None:
    """Write out/digest/{today}.md."""
    conn = _db()
    typer.echo(str(write_digest(conn)))


@app.command(name="purge-empty-jd-cache")
def purge_empty_jd_cache_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="count only; change nothing"),
) -> None:
    """One-off repair: delete LLM cache rows keyed on the empty-JD hash (e3b0c442)
    and reset scores of jobs whose JD is under 500 characters."""
    conn = _db()
    typer.echo(purge_empty_jd_cache(conn, dry_run=dry_run).render())


@app.command(name="merge-feed-dupes")
def merge_feed_dupes_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="count only; change nothing"),
) -> None:
    """One-off repair: merge each open aggregator-feed row whose apply URL names
    an ATS posting into that posting's ATS row (ATS canonical, hard rule 10).
    Pairs where both rows carry an application or decision are listed, not merged."""
    conn = _db()
    typer.echo(merge_feed_dupes(conn, dry_run=dry_run).render())


@app.command(name="repair-applications")
def repair_applications_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="list only; change nothing"),
) -> None:
    """Make every submitted job hold exactly one application row at a
    submitted stage (track/applications.py): a job marked submitted with no
    application row saying so, several rows on one job, a submitted row whose
    stage went back, an application left on a merged aggregator row. A job 'applied' with no
    record of the day is listed, never guessed. MCP: repair_applications."""
    from jobkit.track.applications import repair

    conn = _db()
    typer.echo(repair(conn, dry_run=dry_run).render())


@app.command()
def stats() -> None:
    """Row counts and freshness."""
    conn = _db()
    typer.echo(freshness_banner(conn))
    for label, sql in [
        ("companies", "SELECT COUNT(*) c FROM companies"),
        ("boards", "SELECT COUNT(*) c FROM boards"),
        ("boards_stale", "SELECT COUNT(*) c FROM boards WHERE status='stale'"),
        ("jobs_open", "SELECT COUNT(*) c FROM jobs WHERE closed_at IS NULL"),
        ("jobs_closed", "SELECT COUNT(*) c FROM jobs WHERE closed_at IS NOT NULL"),
        ("jobs_with_jd", "SELECT COUNT(*) c FROM jobs WHERE length(jd_html) > 200"),
    ]:
        typer.echo(f"{label}: {conn.execute(sql).fetchone()['c']}")


# The setup interview's steps (ONBOARDING.md): `jobkit setup ingest|draft|
# confirm|update-draft|promote|questions|answers|intake|targets|status`.
app.add_typer(onboarding_cli.setup_app, name="setup")


@app.command(name="setup-status")
def setup_status() -> None:
    """Which setup steps are done (resume, corpus, preferences, targets,
    answer bank, database, MCP, daily runs), in order, and the next action.
    Read-only; `jobkit doctor` checks the machine side."""
    onboarding_cli.show_status()


def main() -> None:
    """The `jobkit` console script: the Typer app, with a missing personal
    config file reported as one line naming the setup interview and the
    template to copy (config.ConfigMissing), not as a traceback."""
    try:
        app()
    except ConfigMissing as e:
        typer.echo(f"jobkit: {e}", err=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
