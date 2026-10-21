# Commands and tools

You normally use ApplyForge by talking to Claude, which calls the jobkit tools
for you. The same work is available as `jobkit` commands, which the scheduled
run and debugging use. Run them from your checkout with `uv run jobkit …`.

## Setup and checks

```bash
uv run jobkit doctor          # setup check: tools, browser, model backend, config, data, database, Claude tools (read-only)
uv run jobkit setup-status    # which setup steps are done, in order, and the next action
uv run jobkit init            # create the SQLite database (the only command that does)
uv run jobkit setup ingest <resume> [--text-file f] [--replace-draft]  # resume → corpus draft (resume text only) + critique
uv run jobkit setup draft [--batch N]                   # the draft, N items at a time
uv run jobkit setup confirm|update-draft                # confirm or correct draft items
uv run jobkit setup promote [--replace-existing]        # write the confirmed corpus
uv run jobkit setup intake <answers.yaml> [--replace-existing]  # interview answers → config/filters.yaml + commute table
uv run jobkit setup targets <name>... [--tier N] [--file f] [--no-probe] [--skip-not-found]  # find and verify public boards → config/targets.yaml
uv run jobkit setup targets --feed-only "<your words>"  # no companies to watch: rely on the feeds
uv run jobkit setup questions                           # the screening questions, with what is set
uv run jobkit setup answers <answers.yaml> [--replace-existing]  # write data/answer_bank.yaml
uv run jobkit setup status                              # same as setup-status
```

## Finding and triaging jobs

```bash
uv run jobkit poll            # poll all configured boards once (repeat-safe, ETag-cached)
uv run jobkit feeds           # job feeds (deduplicated against the employer boards)
uv run jobkit add-url <url> [--no-score]  # a pasted job link; Greenhouse, Lever and Ashby jobs are polled, queued and scored
uv run jobkit score-jobs <id>...  # extract and score these jobs now (add_job_url runs it in the background)
uv run jobkit linkedin-discover [--dry-run]            # LinkedIn logged-out search, discovery only (off by default)
uv run jobkit resolve-boards [--job-id N] [--dry-run]  # aggregator and LinkedIn jobs → the employer's own posting
uv run jobkit resolve-boards --job-id N --pick <posting id|URL>  # your pick for a flagged job
uv run jobkit filter          # deterministic filter pass (free, logged)
uv run jobkit refilter [--dry-run]  # re-filter rejected open jobs after a config change
uv run jobkit triage [--limit N]    # extract → remote check → pay check → score → queue
uv run jobkit form-specs [--limit N]  # read application forms, match the answer bank, draft new answers; close dead postings
uv run jobkit rescore [--job-ids 12,15] [--dry-run]  # re-score jobs whose score is stale
```

## Applications

```bash
uv run jobkit tailor [--job-id N]   # resume and letter for approved jobs (all, or one)
uv run jobkit stage [--job-id N | --job-ids 3,4] [--keep-open]  # fill approved forms in a visible browser; stops before submit
uv run jobkit open-queue      # open filled forms for your review and submit
uv run jobkit review <job_id> # latest authenticity verdicts for a job's resume and letter
uv run jobkit self-review <job_id> [--full]  # what the self-review changed, and its questions for you
uv run jobkit figures [--bullet-id b_061 [--rendered "..."]]  # how numbers render under your figure style
uv run jobkit rejudge [--job-ids 12,15] [--dry-run]  # claim-judge stored letters and answers
uv run jobkit ghost-close [--dry-run]  # mark submitted applications with no reply after tracking.ghost_after_days (tool ghost_close)
```

## Questions for you

```bash
uv run jobkit questions [--all]   # facts only you know that would close gaps in open jobs (3 a day by default)
uv run jobkit answer <key> "<text>" [--evidence ".." --metric-confidence verified|estimate|none --role-id .. --kind skill|bullet]
uv run jobkit answer <key> --decline  # decline a question (no text needed)
uv run jobkit undecline <key>     # take a decline back: the question is open again
uv run jobkit confirm-proposal <id> [--evidence ".." --metric-confidence .. --role-id ..]  # writes the corpus
```

## Email replies

These record what Claude read in your own mailbox. They are for debugging; the
Claude tools below are the normal path. Nothing is sent. Do not paste a
sender, subject, snippet or summary copied from an email into `email match`
or `email record`: an email's text inside a shell command can run commands.

```bash
uv run jobkit email targets   # applications to look for, the last check, and a suggested Gmail search
uv run jobkit email match --sender "..." [--subject ..] [--snippet ..] [--received-at ..]  # which application one email belongs to
uv run jobkit email record --application-id N --outcome rejected --received-at .. --message-id .. [--link ..] [--summary ..]
uv run jobkit email done [--checked-through ..]  # record that the mailbox was read through a time
```

## Runs and reports

```bash
uv run jobkit run-all         # the full background pipeline; exits 1 if a stage failed
uv run jobkit nightly         # run-all + fill approved forms + digest (the scheduled entry point)
uv run jobkit digest          # write out/digest/<today>.md
uv run jobkit stats           # freshness banner + row counts
uv run jobkit calibrate       # decision-log analysis → numbered proposals (nothing applied)
uv run jobkit schedule install [--dry-run]    # macOS: LaunchAgent running `jobkit nightly`
uv run jobkit schedule status     # installed? loaded? last nightly run; jobs stopped in form filling
uv run jobkit schedule uninstall [--dry-run]  # unload and delete the LaunchAgent
```

## Maintenance

Repairs for databases written by older versions. Each is safe to run twice.

```bash
uv run jobkit merge-feed-dupes [--dry-run]     # merge feed rows into the employer's posting row
uv run jobkit repair-applications [--dry-run]  # one submitted application row per submitted job
uv run jobkit purge-empty-jd-cache [--dry-run] # clear cached results made from empty job descriptions
```

`purge-empty-jd-cache` (tool `purge_empty_jd_cache`, a dry run until
`confirm=true`) deletes cached model results whose key contains the
empty-string hash `e3b0c442`, deletes the scores of jobs with under 500
characters of description, and moves those still `queued` back to `filtered`.
Approved and applied jobs keep their status, and nothing in `out/` is touched.

## Claude tools

The jobkit tools server (`jobkit-mcp`, registered in `.mcp.json`; see
[Setup](setup.md#registering-the-claude-tools)) is the main interface. Tools
that change things in bulk are dry runs until called with `confirm=true`. Queue
numbers are pinned per `get_queue` render, so a re-sort between the list and
your reply can't misroute an approval.

**Queue and decisions**

| tool | does |
|---|---|
| `get_queue(min_score, limit, max_friction)` | numbered jobs with score, company, title, pay, remote verdict, friction, top gaps and flags; the digest header and questions for you |
| `get_job(n)` | full detail: description summary, requirements, gaps, pay analysis, latest form-filling run |
| `approve(ids)` | `"1,3,7"`, `"all"` or `">85"`; starts tailoring in the background |
| `reject(ids, reason)` | dismiss; the reason feeds filter tuning |
| `snooze(ids, days)` | back to the queue later |
| `add_job_url(url)` | a pasted job link (Greenhouse, Lever or Ashby jobs are polled and queued, then scored in the background; the job shows in the queue once scored) |

**Applications**

| tool | does |
|---|---|
| `get_artifacts(n)` | paths to the resume, letter, gap report and provenance |
| `review_artifacts(n)` | authenticity verdicts and flagged spans |
| `get_self_review(n, job_id, full)` | the self-review's outcome and questions |
| `retailor(n)` | tailor one job again |
| `stage_forms(ids, job_ids)` | fill approved forms in one browser process; stops before submit |
| `restage(n)` | fill one job's form again from its stored form spec |
| `open_queue()` | open the filled forms for your review |
| `get_novel_questions(n)` | form questions the answer bank didn't cover, with drafts |
| `answer_novel(n, field_identity, answer)` | your answer to one form question |
| `mark_submitted(ids, job_ids, submitted_on)` | log submissions (queue numbers or job ids; back-dating allowed) |
| `record_outcome(job_id, outcome, note)` | `reply`, `rejection`, `interview` or `offer` |

**Email replies** (read-only; see [Email replies](email-responses.md))

| tool | does |
|---|---|
| `email_targets(scheduled)` | submitted applications still waiting on news, the last check, and a suggested Gmail search; with `scheduled=true` (the scheduled run) nothing unless `tracking.email_check` is true |
| `match_email(sender, subject, snippet, received_at)` | the applications one email most likely belongs to, ranked, with reasons |
| `record_email_response(application_id, outcome, received_at, message_id, link, summary)` | record one email's outcome for one application |
| `finish_email_check(checked_through)` | record when the mailbox was checked through |

**Questions and corpus**

| tool | does |
|---|---|
| `get_questions(show_all)` | today's questions for you |
| `answer_question(key, answer, evidence, metric_confidence, role_id, kind, decline)` | answer or decline one |
| `undecline_question(key)` | reopen a declined question |
| `confirm_proposal(proposal_id, evidence, metric_confidence, role_id)` | write a proposal into your corpus |
| `rescore(job_ids, confirm=false)` | re-score stale jobs |
| `rejudge(job_ids, confirm=false)` | claim-judge stored letters and answers |
| `figures(bullet_id, rendered)` | how numbers render under your figure style |
| `corpus_health()` | bullet count, figure coverage, tags, gaps against recent postings |

**Discovery**

| tool | does |
|---|---|
| `linkedin_discover(confirm=false)` | LinkedIn logged-out discovery (off unless enabled) |
| `resolve_boards(job_id, confirm=false, pick)` | match aggregator jobs to the employer's posting, or record your pick |
| `refilter(confirm=false)` | re-filter after a config change |
| `tune_filters()` | rejection reasons, with suggested config edits |
| `merge_feed_dupes(confirm=false)` | merge stored feed rows into the employer's posting |

**Runs and status**

| tool | does |
|---|---|
| `run_nightly()` | start the nightly run in the background |
| `background_status(limit)` | recent background launches and their output |
| `get_stats(period)` | funnel and conversion rates, including no reply after N days, and the feed jobs not scored for a short description, with their links |
| `ghost_close(confirm=false)` | mark applications with no reply after N days as ghosted (lists them first) |
| `get_digest(date)` | the daily digest |
| `calibrate()` | numbered config proposals from your decisions |
| `doctor()` | the setup check |
| `schedule_status()`, `schedule_install(confirm=false)`, `schedule_uninstall(confirm=false)` | the macOS LaunchAgent |
| `repair_applications(confirm=false)` | repair application rows |
| `purge_empty_jd_cache(confirm=false)` | the cache repair above |

**Setup interview**

| tool | does |
|---|---|
| `setup_status()` | which setup steps are done, and the next one |
| `ingest_resume(path, text, replace_draft)` | read the resume; write the corpus draft and the critique |
| `corpus_draft_status(batch_size)` | what is left to confirm; the next bullets |
| `confirm_bullets(decisions)` | keep, edit, add the number or drop; sets `metric_confidence` and `evidence` |
| `update_corpus_draft(changes)` | identity, roles, education, positioning, skills, constraints, voice samples |
| `promote_corpus(replace_existing)` | write `career_corpus.yaml` once everything is confirmed |
| `run_intake(answers, replace_existing)` | write `filters.yaml` and the commute table from the interview |
| `seed_targets(companies, probe, include_not_found, feed_only_said)` | find and verify boards; or record that you rely on the feeds |
| `answer_bank_questions()` | the screening questions, with what is set |
| `build_answer_bank(answers, replace_existing)` | write `answer_bank.yaml` |
