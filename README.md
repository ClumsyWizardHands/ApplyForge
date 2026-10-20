# ApplyForge

ApplyForge is a local-first job application pipeline. It finds relevant
postings from open ATS APIs, sanity-checks remote policy and comp, tailors a
resume and cover letter from a pre-approved corpus, pre-fills the application
form to the last click, and tracks everything to outcome.

The Python package and the command it installs are still called `jobkit`
(`uv run jobkit ...`, MCP server `jobkit`), so the rest of this README calls
the program jobkit.

**The one hard rule: the system never presses submit.** See `SPEC.md` §8.

## Get your own jobkit

jobkit runs one person's job search on their own computer. The repository
holds the program. Your resume facts, answers and preferences are written by
a setup interview and stay on your machine.

### What you need

- **macOS or Linux** (Windows is untested). The checkout goes somewhere
  ordinary such as `~/code/jobkit`, not under Desktop, Documents, Downloads or
  iCloud Drive.
- **Access to the repository.** If it is private, your GitHub account needs an
  invitation, and git or the GitHub CLI (`gh auth login`) must be signed in on
  this computer.
- **uv**, the Python tool jobkit runs with: `brew install uv`, or
  `curl -LsSf https://astral.sh/uv/install.sh | sh`. It installs Python 3.12
  for jobkit.
- **The Claude app** (desktop, with its Code tab and scheduled tasks) or
  **Claude Code** in a terminal. The daily run is a scheduled task in the
  Claude app; without the app, a macOS LaunchAgent or a cron line runs it.
- **A Claude subscription or an API key.** jobkit's scoring and tailoring
  call Claude through the `claude` command (Claude Code, signed in to your
  subscription), or through `ANTHROPIC_API_KEY` in `.env` if you set one.

### Tuned for sales roles

jobkit searches for any job titles you choose, but its fit score (0 to 100)
is calibrated for sales roles. The scoring prompt (`prompts/score_fit.md`)
teaches the scale with worked examples from one invented sales career (an
account manager turned mid-market account executive). For other kinds of work the score still checks each
job's must-haves against the experience you confirmed, but its numbers are
less reliable: decide on a job's listed gaps and rationale, not on the score
alone, and think twice before accepting a standing rule that approves or
rejects jobs by score.

### Start

In Claude Code, say:

> Clone https://github.com/ClumsyWizardHands/ApplyForge and set it up

Claude clones the repository (somewhere ordinary such as `~/code/jobkit`; see
above) and follows the `jobkit-setup` skill that ships in it
(`.claude/skills/jobkit-setup/SKILL.md`). If it does not pick the skill up,
start a new session with the checkout as its folder and say **"set up
jobkit"**. The interview asks one question at a time and takes about an hour:

1. check the machine and install what is missing (each install on your yes)
2. read your resume (PDF, Word, HTML, text, or pasted)
3. confirm your experience bullet by bullet: keep, edit in your own words, or
   drop, and say how solid each number is and where it could be shown
4. what you want: titles, places, remote or not, pay floor, what to avoid
5. companies to watch
6. the answers application forms ask for (work authorization, notice period
   and so on; the demographic questions are optional)
7. a first run
8. the daily run, as a scheduled task in the Claude app

Stop whenever you like; "continue jobkit setup" picks up where you left off.

### Every day

**What runs on its own.** At the time you chose, the scheduled task runs
`jobkit nightly` in your checkout. It checks the job boards of your target
companies and a few job feeds, drops what your filters rule out, scores the
rest against your experience, tailors a resume and cover letter for each job
you approved, fills those jobs' application forms in a browser window that
closes when done, and drafts the follow-ups that are due. Claude then writes
a short summary in the task's session. The task runs while the Claude app is
open; a run missed while it is closed happens at the next launch. The
unattended run never approves, answers, sends or submits anything.

**What you always do.** Open the jobkit folder in Claude and say **"what's in
the queue"** (the `jobkit-daily` skill). You:

- approve or reject jobs by number; only approved jobs are tailored and filled
- check the tailored resume and cover letter
- review each filled form and click submit yourself (jobkit stops one click
  short, and never solves a CAPTCHA)
- answer the questions jobkit has for you: facts it needs and may not invent
- send follow-ups yourself; jobkit only drafts them

### Your data stays yours

Everything about you lives in your checkout and is gitignored: `data/` (your
career corpus, answer bank, database, interview answers, browser profile),
`config/*.yaml` (preferences, filters, companies, standing rules), `.env` and
`.mcp.json`. The repository tracks only the templates (`*.example.yaml`,
`.env.example`, `.mcp.example.json`), so `git pull` never overwrites your
files and git never commits them. What leaves your computer: requests to
public job boards and feeds, the application forms you approved, and what
Claude reads: the interview, and the postings and parts of your corpus that
jobkit sends it to score and tailor.

### Updating

```bash
cd ~/code/jobkit && git pull && uv sync && uv run jobkit doctor
```

Or ask Claude to run it. The skills, and the scheduled task (its prompt only
points at the skill file in your checkout), update with the code. Start a new
Claude session afterwards so the jobkit tools reload.

### Where things live

| where | what |
|---|---|
| `~/code/jobkit` | your checkout: the program, plus your files below |
| `data/career_corpus.yaml` | the facts you confirmed; every resume, letter and answer draws only on these |
| `data/answer_bank.yaml` | your answers to the questions application forms ask |
| `data/jobkit.db` | every job, decision and application |
| `config/*.yaml` | preferences and filters, target companies, standing rules, writing style, commute times |
| `out/digest/<date>.md` | the daily digest the summary is made from |
| `out/<company>-<title>-<id>-<date>/` | one job's tailored resume and cover letter, with their checks |
| `logs/` | run logs (`jobkit.jsonl`) |
| `.claude/skills/` | the setup, daily and scheduled-run routines Claude follows |
| the Claude app, Scheduled | the "jobkit daily run" task |

`uv run jobkit setup-status` shows which setup steps are done, and
`uv run jobkit doctor` checks the machine and your files, with the fix for
each problem. The rest of this README is the technical reference.

## How it works (60-second version)

```
BACKGROUND (`jobkit nightly`, launchd at 02:00 — runs without you)
  poll 30 ATS boards + 5 aggregator feeds (+ LinkedIn logged-out search, if enabled)
    → dedupe (canonical key; direct ATS record always wins)
    → deterministic filters   (~98% killed free, every rejection logged)
    → LLM triage (Haiku):     extract requirements · catch remote-policy lies
                              · judge comp/OTE realism · score fit 0-100
    → standing rules           auto-approve/reject the obvious (your standing rules)
    → form capture             every application form fetched, matched against
                              the answer bank, novel questions pre-drafted
    → auto-tailor approved     resume+cover from YOUR corpus only
    → stage approved forms     filled in a headed browser, closed when done;
                              never submitted
    → digest                   out/digest/{date}.md

FOREGROUND (you, ~2 min/application)
  "what's in the queue" → approve by number → forms staged in a headed
  browser, fields color-coded by provenance → YOU click submit →
  "mark submitted" → follow-ups draft themselves at +7/+14/+30 days
```

Trust chain: every resume claim traces to a bullet you confirmed in
onboarding (`data/career_corpus.yaml`, gitignored). The provenance validator
hard-fails fabricated numbers; the 4-pass authenticity review reverts drifted
text to your own words; a static test guarantees no code path can ever click
submit (SPEC §8 explains why that's also technically forced).

## Documents

- `SPEC.md` — the contract (v1.4). Read first.
- `ONBOARDING.md` — milestone M0b: resume ingest, intake interview, config generation.
- `CLAUDE.md` — working instructions, build order, hard rules.
- `.claude/skills/` — the setup, daily and scheduled-run routines Claude follows when you use jobkit.
- `docs/endpoints_verified.md` — M0 ground truth: live-verified ATS endpoint shapes.
- Examples, fixtures and tests describe one fictional user, Jordan Rivera (`data/*.example.yaml` hold a simpler version of the same person). A few onboarding and form-fill tests use other made-up people on purpose (Morgan Ellis, Casey Reyes, Pat Doe), so that a value copied from the templates instead of from the new user's own input shows up.
- `docs/why-no-linkedin.md` — what LinkedIn allows, what jobkit does with it (discovery only), and why.

## Setup

jobkit is one person's job search: your corpus, your answers, your targets.
The repo holds the product; your own files stay on your machine (gitignored)
and are created by a guided setup interview.

**The short way: let Claude do all of it** ("Get your own jobkit" above).
Claude clones the repository and the `jobkit-setup` skill, which ships in
this repo, runs the eight-step interview: machine check and installs, your
resume, confirming your experience, what you want, companies to watch,
application answers, a first run, and the daily run. The daily run becomes a
scheduled task in the Claude app (the macOS LaunchAgent is the fallback).

**Project skills** (`.claude/skills/`, loaded by Claude in this folder):

| skill | say | what it does |
|---|---|---|
| `jobkit-setup` | "set up jobkit", "continue jobkit setup" | the setup interview below, then the first run and the daily schedule |
| `jobkit-daily` | "what's in the queue" | approve or reject by number, check the tailored files, open the filled forms, log submissions and employer responses, answer jobkit's questions, follow-ups |
| `jobkit-scheduled-run` | (the scheduled task runs it) | starts `jobkit nightly` unless a nightly run finished in the last 12 hours, waits for it, then sends one summary from the digest: what failed, what waits on you, the queue, questions for you, follow-ups due |

The Claude app task's prompt is one line that points at
`.claude/skills/jobkit-scheduled-run/SKILL.md` in your checkout, so the
routine updates with the code. The scheduled task runs while the Claude app
is open (a run missed while it is closed happens at the next launch). The
unattended run never approves, answers, sends or submits anything. Cloud
routines cannot reach your checkout, so they cannot run jobkit.
`tests/test_claude_skills.py` checks that every `jobkit` command and MCP tool
the skills name exists, with the options they use, and that the scheduled run
only starts the nightly run and reads its results.

The manual way:

```bash
git clone <this repo> ~/code/jobkit   # NOT under ~/Desktop, ~/Documents or iCloud Drive
cd ~/code/jobkit
uv sync                               # Python 3.12 + dependencies
uv run playwright install chromium    # the headed browser that stages forms
uv run jobkit init                    # creates data/jobkit.db — the only command that does
cp .mcp.example.json .mcp.json        # then set --directory to this checkout (see below)
```

Then open the checkout in Claude (Claude Code or the Claude app's Code tab) and
say **"set up jobkit"**: the setup interview reads your resume, builds your
career corpus from its text only (you confirm every entry, and set each
metric's confidence and evidence yourself), asks the answer-bank questions out
loud, and writes your config. Finally:

```bash
uv run jobkit doctor                  # every check, with the exact fix for each problem
```

`jobkit doctor` (also MCP `doctor`) is read-only. It checks Python 3.12 and
`uv`, Playwright's Chromium (the revision the installed Playwright needs), an
LLM backend (`ANTHROPIC_API_KEY`, or the `claude` CLI on PATH, which runs on a
Claude subscription), that the checkout is not under an iCloud-synced folder
(iCloud sets the macOS hidden flag; Python then skips the hidden `.pth` file
and `import jobkit` fails) and that no venv `.pth` file is hidden, every
personal config file below (present and loading), the corpus and answer bank,
the database, the `.mcp.json` registration, and two warnings: a contact
address for the polite User-Agent, and a commute time for every place in
`filters.locations.allowed` that is not a relocation city (without one, a
hybrid posting there is rejected as `sanity:commute:unknown`). It exits 1
while anything needs fixing; warnings do not fail it. A missing config file's
fix is the setup interview or its template, never a copy from git history.

**Your files (all gitignored).** The setup interview writes them; each has a
commented template next to it, so you can also copy and edit by hand:

| file | what it holds | template |
|---|---|---|
| `config/filters.yaml` | role tracks, title include/exclude, places, comp floors, remote policy, schedule, feeds | `config/filters.example.yaml` |
| `config/targets.yaml` | companies polled directly, with tiers and ATS boards | `config/targets.example.yaml` |
| `config/autoapprove.yaml` | standing approve/reject rules (start with none approving) | `config/autoapprove.example.yaml` |
| `config/banned_phrases.yaml` | authenticity review lists and `drift.estimate_style` | `config/banned_phrases.example.yaml` |
| `config/commute_table.yaml` | minutes from home per location bucket | `config/commute_table.example.yaml` |
| `data/career_corpus.yaml` | every fact a resume, letter or answer may use | `data/career_corpus.example.yaml` |
| `data/answer_bank.yaml` | the questions every application form asks | `data/answer_bank.example.yaml` |
| `.env` | optional keys (`ANTHROPIC_API_KEY`, Adzuna, USAJOBS), `JOBKIT_CONTACT_EMAIL` | `.env.example` |
| `.mcp.json` | the MCP registration for this checkout | `.mcp.example.json` |

A command that needs a missing config file stops with one line naming both
fixes: `config/filters.yaml not found — run the jobkit setup interview (say
"set up jobkit" in Claude) or copy config/filters.example.yaml`. The data
templates describe a fictional person; never inherit an answer from them.

**The setup interview, step by step.** Each step is an MCP tool (what Claude
calls when you say "set up jobkit") and a `jobkit setup` command.
`jobkit setup-status` (MCP `setup_status`, read-only) lists the steps in order,
which are done, and the next action.

| step | MCP tools | CLI | writes |
|---|---|---|---|
| 1. read the resume | `ingest_resume(path, text)` | `jobkit setup ingest <file> [--text-file f]` | `data/career_corpus.draft.yaml`, `data/resume_critique.md`, `data/setup/resume_source.txt` |
| 2. confirm the corpus | `corpus_draft_status`, `confirm_bullets`, `update_corpus_draft`, `promote_corpus` | `jobkit setup draft`, `confirm`, `update-draft`, `promote` | `data/career_corpus.yaml` |
| 3. preferences | `run_intake(answers)` | `jobkit setup intake <answers.yaml>` | `config/filters.yaml`, `config/commute_table.yaml`; `autoapprove.yaml` (no rules) and `banned_phrases.yaml` when missing |
| 4. target companies | `seed_targets(companies)` | `jobkit setup targets <name>... [--file f]` | `config/targets.yaml` (new entries appended) |
| 5. answer bank | `answer_bank_questions`, `build_answer_bank(answers)` | `jobkit setup questions`, `jobkit setup answers <answers.yaml>` | `data/answer_bank.yaml` |

Structured input (bullet decisions, interview answers, companies) is JSON for
the MCP tools, and a YAML/JSON file or inline JSON for the CLI. The tool
descriptions list every field. What each step guarantees:

- **The draft holds only text from your resume** (hard rule 6). Every string
  in it is checked against the extracted text before anything is written; a
  draft with one string the resume does not contain is refused. Company,
  title and dates of each role are a best reading of its header, which you
  confirm or correct; a year with no month stays unset until you give the
  month. A role is confirmed only with an end month or your explicit
  `current: true`: a role whose resume dates end in a year alone, or give
  one date, is never saved as current (that would print "Present" on every
  resume). Each bullet or paragraph of the resume's summary is offered as its
  own positioning line, as written; keep the ones that position you and drop
  the rest. `metric` holds only figures the bullet states, and tags come only
  from the resume's own skills list. Suggestions go to the critique, never
  into the draft.
- **`metric_confidence` and `evidence` are yours.** They are empty in the
  draft. `confirm_bullets` refuses a kept bullet without a confidence, a
  bullet with a figure but no evidence, and `none` for a bullet that states a
  figure. A figure is any number the bullet's final `text_long` or
  `text_short` states (read again from the wording on every decision and at
  promotion, so an edit that adds a number is caught) or its `metric` holds;
  `metric` cannot be cleared while the wording still states one, and an edit
  without `metric` takes the figures its new wording states. A decision list
  is applied all or nothing. `promote_corpus` refuses
  while any bullet, role, education entry, positioning line or the identity
  block is unconfirmed; the display location (the city the resume and letter
  header show; required, since every tailored header shows one) and home
  city are asked, never read off the resume. It then reports the
  bullet count, the verified share and the top tags, and says when the
  corpus is thin (under 30 bullets or 40% verified).
- **Resume formats:** PDF, DOCX, HTML, plain text, or the text pasted. PDF
  text comes from pypdf when it is installed, else poppler's `pdftotext` when
  it is on PATH, else a small built-in reader for simple text PDFs. Neither
  library is a dependency (SPEC §10 lists none); DOCX needs none. A PDF, DOCX
  or HTML file that does not extract cleanly (under 50% recognizable English
  words, or more than 5% unreadable characters) is refused with a request to
  paste the text instead. Pasted text and plain-text files are not
  word-scored, so a resume full of company and tool names (Snowflake, dbt,
  PySpark) gets through; they are refused only when under 200 characters or
  full of unreadable characters (text copied out of a PDF viewer from a
  custom font), and that message asks for the text from the original
  document, not for the same paste again.
- **`run_intake`** quotes your answer above every setting it decides, writes
  a config only if it loads with the pipeline's own loader, and refuses
  answers that contradict each other (a title you named that your seniority
  floor or an exclusion would reject). It also refuses answers with no
  default comp floor (`comp.default_floor`, the floor for titles outside your
  tracks): the answer bank's one salary-expectation figure is measured
  against it, and without it every job in your tracks would get that field
  left blank and flagged. A track whose own floor is under the default is
  named in the output, because its jobs get that field blank and flagged
  too. jobkit has no industry filter
  (postings carry no industry): wanted and refused industries are recorded as
  comments and used to choose target companies; companies you refuse go in
  `blocklist_companies`. Feed keyword searches use your track titles.
- **`seed_targets`** finds each company's Greenhouse, Lever or Ashby board from
  a pasted board URL, the bundled registry (`registries/`), or a few tokens
  made from the name and domain, and verifies it on the public board endpoint
  (SPEC §2.1). Nothing else is fetched. A Lever board that answers with no
  postings is kept but marked unverified. A company with no board found is
  recorded without one (its tier still applies to postings the feeds find
  there) and reported with what was tried.
- **`build_answer_bank`** takes only the template's structure, never a value
  from it. EEO keys take only your explicit choice ("I don't wish to answer"
  is one); a key you skip stays blank and flagged on every form. Salary
  history is never answered. Legal acknowledgements are off unless you opt
  in, and then only labels you approve word for word (`approved_labels`,
  empty at first) are ticked. Answers can be given over several sittings.
- **Your existing files are safe.** A file the interview did not write (your
  own `filters.yaml`, corpus or answer bank) is never replaced unless you ask
  (`replace_existing=true`, `--replace-existing`), and the old copy goes to
  `data/setup/backup/` first. What you said is kept in
  `data/setup/*.answers.yaml`. All of `data/` and `config/*.yaml` is
  gitignored.
- Not built yet: proposing the first standing rules from your answers
  (ONBOARDING.md 2.8, `seed_rules`); `config/autoapprove.yaml` starts with
  none. STAR stories (1.5) are added to the corpus by hand for now.

The polite User-Agent sent to job boards names `JOBKIT_CONTACT_EMAIL`, else
`identity.email` from your answer bank (a template placeholder such as
`@example.com` is skipped), else no address.

Secrets go in `.env` (see `.env.example`); the CLI and the MCP server load it
at start (python-dotenv). A variable already set in the environment wins over
`.env`.

**Locations.** Location logic reads your config, not a built-in list of places
(see "Role tracks, comp floors and `refilter`"):

- A town outside the built-in metro table buckets as `other-<town>` (a
  "Bristol, PA" posting is `other-bristol`). Every town in
  `filters.locations.allowed` that buckets this way needs a commute time under
  `buckets:` in `config/commute_table.yaml` (`other-bristol: 25`); without
  one, the remote check rejects every hybrid posting there as
  `sanity:commute:unknown`. `jobkit doctor` warns about each allowed place the
  table does not cover and names the exact key to add.
- Regional geo restrictions ("Delaware Valley", "Mid-Atlantic") pass only
  when you list them: add each spelling you accept to
  `remote_policy.acceptable_geo_restrictions` in `config/filters.yaml`.
- A state named in `filters.locations.allowed` (such as `"Pennsylvania"`) is a
  place the filter accepts: a posting located only in that state passes the
  filter and goes to the remote check, which rejects it as `sanity:onsite` or
  `sanity:commute:unknown` unless the posting turns out to be remote.
  `jobkit doctor` lists it with the towns above (a warning, not a failure);
  leave it without a commute time to keep hybrid postings there rejected.

**Project root.** Every data path (`data/jobkit.db`, `logs/`, `out/`,
`data/raw/`, the browser profile, the corpus and answer bank) is resolved from
one root: `JOBKIT_HOME` if set, otherwise the jobkit checkout the package was
loaded from. The CLI and MCP server also change into that root at start, so
`config/`, `prompts/` and `templates/` resolve whatever directory they were
launched from. A missing database is an error that names `jobkit init`; no
other command creates one (before, starting from the wrong directory silently
created an empty DB and every answer came back zero).

## Status

- [x] M0 — Ground truth (endpoint verification)
- [x] M0b — Onboarding (corpus · targets · answer bank · standing rules)
- [x] M1 — Ingest (ATS boards · ETag-idempotent)
- [x] M1b — Aggregator feeds (keyless feeds · cross-source dedup · auto-discovery)
- [x] M2 — Triage (filters → LLM triage → scored queue)
- [x] M2b — Form specs (API capture + fallback · answers pre-drafted · friction-scored)
- [x] M3 — MCP + review loop (pinned numbering · standing rules live)
- [x] M4 — Tailoring (provenance-gated · selection diversity · one-page Typst)
- [x] M4b — Authenticity review (4 passes · drift auto-revert · adversarial critic)
- [x] M5 — Submit queue (Playwright staging · never clicks submit)
- [x] M6 — Tracking + calibration (follow-up drafts · calibration proposals · launchd nightly schedule)
- [x] Discovery expansion (Getro/Consider/CoS-RSS/a16z feeds · freehire registry · URL resolver)
- [ ] M7 — Interview prep (stretch)

## Commands

```bash
uv run jobkit doctor  # setup check: tools, browser, LLM backend, config, data, DB, MCP (read-only)
uv run jobkit setup-status  # which setup-interview steps are done, in order, and the next action
uv run jobkit setup ingest <resume>  # resume → corpus draft (resume text only) + critique
uv run jobkit setup draft|confirm|update-draft|promote  # confirm the draft, then write the corpus
uv run jobkit setup intake <answers.yaml>  # interview answers → config/filters.yaml + companions
uv run jobkit setup targets <name>...      # find + verify public boards → config/targets.yaml
uv run jobkit setup questions|answers <answers.yaml>  # the answer bank, asked and written
uv run jobkit init    # create the SQLite schema
uv run jobkit poll    # poll all configured boards once (idempotent, ETag-cached)
uv run jobkit feeds   # aggregator + portfolio-board feeds (deduped vs direct boards)
uv run jobkit add-url <url>  # ingest any pasted job URL (email alerts, DMs, boards)
uv run jobkit linkedin-discover [--dry-run]  # LinkedIn logged-out search, discovery only (off by default)
uv run jobkit resolve-boards [--job-id N] [--dry-run]  # aggregator/LinkedIn jobs -> the employer's own posting
uv run jobkit resolve-boards --job-id N --pick <posting id|URL>  # your pick for a board-match-ambiguous/-unconfirmed job
uv run jobkit filter      # deterministic filter pass (free, logged)
uv run jobkit refilter [--dry-run]  # re-filter rejected open jobs after a config change
uv run jobkit triage      # LLM triage: extract → remote → comp → score → queue
uv run jobkit form-specs  # capture application forms, match bank, draft novel Qs; close dead postings
uv run jobkit digest      # write out/digest/{today}.md (freshness banner, staging, questions for you, follow-ups due)
uv run jobkit stats       # freshness banner + row counts
uv run jobkit run-all     # full background pipeline; exits 1 if a stage failed
uv run jobkit nightly     # run-all + stage approved forms + digest (the launchd entrypoint)
uv run jobkit schedule install [--dry-run]  # macOS: LaunchAgent running `jobkit nightly` at 02:00
uv run jobkit schedule status     # installed? loaded? last nightly run; jobs halted in staging
uv run jobkit schedule uninstall  # unload and delete the LaunchAgent
uv run jobkit followups   # draft due follow-ups; close ghosted at +30d; skip notes with no contact
uv run jobkit merge-feed-dupes [--dry-run]  # one-off: merge feed rows into their ATS row
uv run jobkit repair-applications [--dry-run]  # one submitted application row (with follow-ups) per submitted job
uv run jobkit calibrate   # decision-log analysis → numbered proposals
uv run jobkit stage       # Playwright staging (never clicks submit)
uv run jobkit open-queue  # open staged applications for review + your click
uv run jobkit purge-empty-jd-cache [--dry-run]  # one-off repair, see below
uv run jobkit figures [--bullet-id b_061 [--rendered "..."]]  # how numbers render, see below
uv run jobkit self-review <job_id> [--full]  # what the self-review changed, and its questions for you
uv run jobkit questions [--all]   # facts only you know that would close gaps in open jobs (≤3/day)
uv run jobkit answer <key> "<text>" [--evidence ".." --metric-confidence verified|estimate|none --role-id ..]
uv run jobkit answer <key> --decline  # decline a question explicitly (no text needed)
uv run jobkit undecline <key>     # take a decline back: the question is open again
uv run jobkit confirm-proposal <id> [--evidence ".." --metric-confidence .. --role-id ..]  # writes the corpus
uv run jobkit rescore [--job-ids 12,15] [--dry-run]  # re-score jobs scored against an older corpus
uv run jobkit rejudge [--job-ids 12,15] [--dry-run]  # claim-judge letters/answers stored before the judge
```

`purge-empty-jd-cache` (also MCP `purge_empty_jd_cache`, which is a dry run
until called with `confirm=true`) repairs the empty-JD cache collision from the
2026-10-08 audit: it deletes `llm_cache` rows whose key contains the empty-string
hash `e3b0c442`, deletes the scores of jobs with under 500 characters of JD
text, and moves those still `queued` back to `filtered`. Approved and applied
jobs keep their status, and nothing in `out/` is touched. It is safe to run
twice.

### Role tracks, comp floors and `refilter`

`config/filters.yaml` defines the role tracks once, as an ordered list under
`filters.tracks` (`sdr_bdr_manager`, `sales_exec`, `sdr_bdr`,
`account_executive`; first match wins, leadership before IC). Each track's
`title_regex` is also an include pattern; `include_titles` holds only the
non-track patterns (in the test profile: revenue and sales operations, deal
desk, channel and partner roles, sales compensation, enablement). `config.Filters.track_for(title)` is the classifier other code
uses. The standing-rules title class (`rules._title_class`) is read from it
and has no AE pattern of its own: an `account_executive`-track title ("Senior
Sales Executive", "Mid-Market AE") is `account_executive` unless it is a
founding/first AE, and an SDR/BDR title is never `account_executive`. The test
profile also excludes any title containing "engineer".

**Pausing a track.** Each track has `active: true|false` (default `true`) in
`filters.tracks`, its one owner. A title whose *first* matching track is
paused is a filter-layer rejection, `track_paused:<id>` (after the title
include/exclude checks, before location and comp). There is no exception:
neither a later *track* nor a non-track
`include_titles` pattern releases a title its first, paused track claims. The
per-track `hold_regex` that guarded a proposed release exception is gone, and
config load rejects it.
**Known wrongful holds** while `sdr_bdr` is paused (an accepted tradeoff):
its regex claims any "Business Development" title that no leadership track
takes first, so roles an include pattern targets can be held as
`track_paused:sdr_bdr`. In the test profile they include
"Business Development Executive",
"Account Executive, Business Development",
"Revenue Operations, Business Development",
"Deal Desk Analyst, Business Development" and
"Sales Compensation Analyst, Business Development". Senior BD titles are
held for the same reason
(see the known gap below). To see one, look for `track_paused:sdr_bdr` in the
rejection log; `refilter` after a config change re-examines them.
Pausing changes nothing else: the paused track still classifies its titles
(`track_for` returns it, so an "SDR (AE track)" title never becomes an AE),
and its comp floor, standing-rule class, scoring and tailoring emphasis stay
defined. Feeds still ingest its postings so the filter can hold them against a
job row.
**Example: pausing IC SDR/BDR.** If you no longer take individual-contributor
SDR/BDR roles, set `active: false` on `sdr_bdr` (the test profile does). Because
`sdr_bdr_manager` is listed first and stays active, "SDR Manager" and
"Manager, Sales Development" still pass. To resume IC
SDR/BDR: set `active: true` on `sdr_bdr` in `config/filters.yaml`, then run
`uv run jobkit refilter` (MCP `refilter(confirm=true)`); every open posting
held as `track_paused:sdr_bdr` goes back through the filter (a job with a
human decision stays rejected). `refilter` (CLI and MCP) lists the paused
tracks, and MCP `tune_filters` shows how many open postings each one holds.
Known gap: senior business-development titles ("VP Business Development",
"Regional Vice President, Business Development", "Chief Business Development
Officer") match no leadership track, so they fall to `sdr_bdr` and are held
while it is paused. The fix is a `sales_exec` regex change in
`config/filters.yaml`. It is proposed in `tests/test_track_pause.py` and has
not been applied; the matching tests are strict xfails until it is.

Comp floors are per track (`filters.comp.track_floors`, keyed by track id);
titles that match no track use `filters.comp.floor`. A track with
`basis: ote` has an OTE floor: because many postings publish base only, such a
posting passes when its `comp_max` reaches the floor or `floor ×
comp.ote_base_share`. That allowance is for a base figure: a posted figure that
is itself OTE is compared with the full floor (the rejection reason ends
`:posted-ote`), and the standing-rule condition `comp_min_gte` never matches an
OTE minimum. Floors apply only to USD (or unlabelled) ranges and never to
unpublished comp. Adzuna's `salary_min` is the lowest floor the filter accepts
(`Filters.lowest_comp_floor`).

Greenhouse and Lever postings now carry comp: Lever's `salaryRange`
(per-year only) and the Greenhouse pay-transparency block in the posting HTML
(annual USD ranges only). With base and OTE blocks, comp is the range of the
same kind as the title's floor (`Filters.comp_floor_for(title).basis`): the
OTE range for an OTE-floor track (`sdr_bdr`, `account_executive`), so a
standing rule on comp judges an AE on its OTE; the base range otherwise. Among ranges of one kind
(geographic tiers), the highest ceiling. The choice is made at poll time; a
change to a track's floor basis takes effect on the next poll of that board. The schema
has no basis column, so `greenhouse.comp_basis` reads it back from the stored
JD. Location: no place is hard-coded. The filter accepts the location buckets
of `filters.locations.allowed` and `remote_policy.relocation_cities`
(`Filters.accepted_location_buckets`: each place is bucketed by the same
`normalize.location_bucket` that buckets postings, so "Brooklyn" names
`nyc-metro` and "Bristol" names `other-bristol`), plus remote postings while
`filters.locations.accept_remote` is true (the default). `remote-intl` is
rejected even when the ATS flags the job remote;
locations that are only country-wide US ("United States", "USA - West",
"US / Canada"), blank, or "Distributed" pass to the LLM remote check instead of
being rejected. A location naming a US city or state outside the accepted
places ("Tulsa, Oklahoma", "San Mateo, CA United States") is still a
`location:` rejection. The commute cap skips relocation cities
(`Filters.relocation_buckets`), and `config/commute_table.yaml` is keyed by
location bucket (a table that only has `manhattan-midtown` still serves
`nyc-metro`). A geo restriction passes when it names an entry of
`remote_policy.acceptable_geo_restrictions`, one of your allowed places or
relocation cities, or a US state they name (its name, or its postal code unless
the code is an English word such as OR or IN). In the remote check, a hybrid answer on a country-wide
location gets a `hybrid-office-unknown` flag and an unclear one a
`remote-policy-unclear` flag; a hybrid office in a named city with no
commute-table entry (and not a relocation city) is rejected as
`sanity:commute:unknown`. Geo restrictions are matched as whole words.

`refilter` (also MCP `refilter`, a dry run until called with `confirm=true`)
is for after a config change, since the filter only reads `status='new'` jobs.
It takes open jobs that are still `rejected` and whose latest rejection is a
filter-layer title, paused-track (`track_paused:`), comp-floor or location
reason, recomputes their location
bucket, resets the ones whose verdict changes to `new` (one `refiltered` event
each, with the previous reason), and runs the filter pass. Blocklist and stale
rejections, later-layer rejections, closed jobs and jobs a human has moved on
are left alone, as is any job with a `decisions` row: a job refilter revived
and the human then rejected through `reject` stays rejected. A second run changes nothing. Postings are dated, so after a
long gap most of them come back as `stale`: a fresh `poll` matters more.

Jobs with under 500 characters of JD text are refused before any LLM call:
triage rejects them and tailoring raises. Both log a rejection with
`layer=sanity`, `reason=jd_missing`.

### Fact check on everything an employer reads

Resume bullets, the skills line, cover letters and AI-drafted answers to novel
form questions are all checked against the corpus before anything is used
(`src/jobkit/validate/provenance.py`, `src/jobkit/validate/claims.py`). Any
violation fails closed.

- **Numbers keep their units.** `20%` does not match `~$20K`, and `#1` does not
  match `$75K+`. Number words ("team of twelve"), rank claims ("#1 rep", "top
  rep") and multipliers ("doubled", "tenfold") count as numeric claims and need
  a source.
- **Numbers are said confidently, but exactly as the corpus states them.**
  One knob decides how an approximation mark is treated:
  `config/banned_phrases.yaml` → `drift.estimate_style` (it replaced
  `drift.enforce_hedging`). The shipped template sets `hedged` (the stricter
  rule, and what an absent key means); the setup interview asks which you
  want, and the test profile runs under `confident`:
  - `confident`: `~`, `about`, `approximately`, `roughly` and `around` may be
    dropped, even on an `estimate` metric (`~19%` may read `19%`; b_061's
    `$2.6M` and b_063's `140` render as written). Everything else must match the
    source: the same value and unit, no added precision (never `19.4%`), a
    range stays the whole range (`6–10`, never `10`), and `+`, `up to`,
    `under`, `over` stay where the source has them and are never added
    (`up to $1.8M/yr`, never `$1.8M a year`). A bound written after the number is
    the same thing: `$2.6M or more`, `$2.6M-plus`, `$2.6M and up` from a source
    that says `$2.6M` fail like `over $2.6M`. A peak is a qualifier of its own, before
    or after the number: `peaked at $110K` states b_050's `peaking at ~$110K`,
    while `up to $110K`, `topped out at $110K` and a bare `$110K` do not
    (2026-10-10). A peak never hides another qualifier: every bound phrase
    after the number is read, so `~$110K at peak or more`, `$110K peak, tops`
    and `over $110K at peak` fail as `added_qualifier`. Violations: `fabricated_number`, `collapsed_range`,
    `changed_range`, `dropped_qualifier`, `added_qualifier`.
  - `hedged` (the rule before 2026-10-08): every number of an `estimate`
    metric keeps a hedge right next to it (`40%, over target` is unhedged;
    `~40%`, `up to 9`, `40+` and ranges like `$60–75K` are hedged), and a
    number its source only states hedged keeps that hedge. Under `hedged`,
    b_061 and b_063 fail as written.

  In both styles **a rate keeps its period**. A figure's period is the one
  written next to it: `/day`, `/mo`, `a year`, `per month`, `each week`,
  `annually`, `monthly`, `run-rate`, `ARR`/`MRR`, `YoY`/`MoM`, hyphenated
  onto the figure included (`$22K-a-month`, `$1.8M-per-year`; a hyphen after a
  word, as in `once-a-month`, is no figure's period). `$1.8M/yr` never becomes
  `$1.8M/month` (`changed_period`), `$22K` never becomes `$22K/month`
  (`added_period`), and `5–7 clients/month` never becomes `5–7 clients`
  (`dropped_period`). A figure with no period of its own may take a period
  the same text states elsewhere, but never one it does not state. Durations
  are not periods (`in under a year`, `within a year`).

  `metric_confidence` in the corpus is the same in both. The knob is read by
  the resume check, the letter/answer check, the authenticity review (pass 3
  flags an inflated or reshaped figure in either style, and an unhedged
  estimate only under `hedged`) and the selection, cover-letter and
  novel-question prompts (`FIGURE STYLE:` in each). A missing key means
  `hedged`; an unknown value, or a leftover `enforce_hedging`, is an error.

  `jobkit figures` (MCP `figures()`) prints the style, what it allows, and
  which corpus bullets pass word for word under it. `--bullet-id b_041
  --rendered "Ran 10 trial kickoffs/week."` (MCP `figures("b_041", "...")`)
  checks one rendering. It makes no LLM call and opens no database.
- **Never-claim lists.** These are optional keys in `data/career_corpus.yaml`.
  An absent key means an empty list.

  ```yaml
  constraints:
    never_claim_titles: ["VP", "SVP", "CRO", "General Manager"]
    never_claim_phrases: ["treasury management sales"]
    never_claim_tools: []
  ```

  A title rendered as the candidate's own is a hard fail. Own is the default:
  "Promoted to VP", "As VP of Sales", "I was effectively the General Manager",
  "Owned the General Manager role", "the org's General Manager" and
  "the general manager's duties were mine" all fail. An acronym also covers its
  S/E/A forms (VP covers SVP). A title counts as another person's only with an
  explicit person signal: "the general manager's office", "reported to the VP",
  "our VP of Sales asked". Even then it needs a source:
  - in a resume bullet, the sourced title must keep the source's own words on
    each side ("reporting to the general manager's office"). A paraphrase is a hard fail.
  - in a letter or answer, a cited bullet (or, in a sentence about the target,
    the JD) must mention the title.

  When the job being applied for has a never-claim title ("VP of Sales"), the
  letter may name the job ("the VP of Sales role at Oscorp", "your VP of Sales
  role"). It may never claim the title: "I was the VP of Sales at Haulmark" and
  "As VP of Sales…" still fail. A sentence that names one of the candidate's
  employers, or states one of the candidate's facts, gets this excuse only when
  the title is tied to the target ("…prepared me for the VP of Sales role at
  Oscorp"). Titles match on word boundaries, so "CRO" no longer fires inside
  "across". A never-claim phrase anywhere is a hard fail.
- **Skills line.** It may contain only `skill_index` keys. Its final value is
  written to `provenance.json` (`skills_line`). When `skill_index` is empty,
  the skills line is left off the resume and `tailor.skills_omitted` is logged.
- **Cover letters.** The model sees only the selected bullets, each labeled
  `employer · title · dates`, and must return `bullets_used`. Every number and
  proper noun must appear in a cited bullet. A capital at the start of a
  sentence makes a proper noun only when the word is not a common English
  word (the one list, `src/jobkit/validate/common_words.txt`, and nothing
  else: a word the corpus or the JD also writes in lowercase, such as a
  domain or a handle, is not made common by it) or it is a known name: the
  target, a
  candidate employer or school, a `skill_index` key, a sales tool or method, a
  company in the DB, a name the JD or the corpus uses, or a word the letter
  itself capitalizes mid-sentence. A name the JD or the corpus uses is any
  word they capitalize mid-sentence, with two exceptions: a word after the
  first one in a title-cased run of common words ("Success" in "Customer
  Success"), and a capital they also write in lowercase that a lowercase
  "team"/"org"/"function"/"department" follows ("People teams"). So "Success
  shows up later." passes beside a JD with "Customer Success",
  while "Brightwave hired me…", "Oracle was one of my accounts…" (beside
  "Oracle Cloud Infrastructure") and "Slack ran my team…" (beside "in Slack
  and keep slack in the schedule") fail (2026-10-10). A standard contraction
  at the start of a sentence ("Let's", "Don't", "We'll", "Here's") is never a
  name, even when the JD capitalizes it after an emoji or a bullet ("✨ Let’s
  give businesses…"); "Brightwave's team hired me…" and "Oracle's team…"
  still fail (2026-10-15). A sentence about the target
  company may also use numbers and names from the JD, but only when the
  company is plainly its subject. The sentence opens with the company's name
  ("Fabrikam Payments doubled its payment volume in 2025"). A sentence that says
  nothing happened may instead open with "you"/"your" or "the role" ("Your
  Account Associates book 35 walkthroughs a week"), but "Your team doubled…" fails.
  The sentence contains no "I"/"my" and names none of the candidate's
  employers. It points back at or compares with nothing ("there", "then",
  "too", "the same", "like", "as the old team did", "that team", "they"),
  introduces no person and does not name the candidate ("a leader who…", "a
  closer booking…"), and promises nothing ("will", "would", "can"). So "Booking 35 walkthroughs a week with a team like your
  Account Associates was the job" does not borrow the JD's "35", and "At
  Haulmark I saw revenue double" does not borrow its "doubled". Reactions such
  as "caught my attention" do not count as "I"/"my"; "means a lot to me" and
  "exciting to me" do, since "…, which means a lot to me" can close a claim.
  Durations are numbers: a plan's window ("the first sixty days", "within 30
  days", "a 30-60-90 day plan") needs a cited source, in a letter and in an
  answer. Metrics need a source in every tense ("I'll grow your ticket
  volume 5x", "I'll book 35 walkthroughs a week"), even when the JD has the number.

  A first-person sentence never uses a JD word, not even to name the
  target's own people: "I'll coach your Account Associates" fails on
  "Associates" unless a cited bullet has it. An allowance for that was tried
  and removed, because its guard (a list of experience words) kept missing
  new phrasings: "I'll coach your Account Associates as their former peer" and
  "I'll still coach your Account Associates" claim a seat or an activity the
  corpus does not state. A sentence makes no claim about the candidate when
  it names none of the candidate's employers, makes no past-tense claim
  ("coached", "I've led", "previously", "fell"), and every "I"/"we" in it
  takes a future, modal or stance verb ("I'll", "I'd", "I can", "I think").
  "I'd" before a verb whose past participle is the same as its base form
  ("I'd run", "I'd become", "I'd set up") might mean "I had", so it counts as
  a claim. Every "my"/"our" in it is a plan ("my plan") or ends a short
  phrase ("around my meetings"); "me" after a preposition and
  "myself" count the same as "my". Such a sentence may name the job's title
  as the role sought (below), but it still needs a cited bullet for every
  number and every other proper noun.

  A first-person sentence that claims experience, a skill, credentials or a
  background may use only words of the bullets it cites (their employer and
  title included), or it fails as `unsourced_experience_claim`. Such a
  sentence has a form of "bring" or "I offer/contribute/lend", a word such
  as experience, expertise, background, knowledge, track record, skills,
  credentials, licensed, certified, degree or years, or "I am a/an/the". A
  modal does not make it a plan: "I would bring years of field service operations
  leadership", "I can bring the trade credentials", "I have deep
  expertise in equipment financing" and "I am a licensed electrician" fail
  unless a cited bullet has every word. "Bring" and its like need no source,
  but "years" and "expertise" do. "I'd bring the onboarding program that cut time to
  first dispatch by ~40%" passes with b_044 cited. The
  target's name supplies no word, so "…, and I'd bring that to Fabrikam" fails.
  The same rule applies to drafted answers (sixth review of the self-review,
  2026-10-09). A modal or future sentence that says what the candidate has
  is such a claim too, with no word of experience in it: a verb of having
  after "I'd", "I'll", "I can", "I want to" and the like ("I'd have the
  trade chops your team needs", "I'll draw on my supplier network", "I
  could lean on…"; "I'd have to learn…" is not one), or a "my"/"our" that is
  not a plan in a sentence with a modal ("My field service leadership would serve
  your team"). Such a sentence is never one that "makes no claim about the
  candidate" above, so the job's title in it is checked as a seat claimed.
  "I'd welcome the chance to talk" and "I'm excited about the role at Fabrikam
  Payments" still pass (review 2026-10-09). The second review of 2026-10-09
  added more markers of such a claim: knowing or understanding after a modal
  ("I'd know exactly how equipment financing works", "I would already know the
  billing side", "I'd come in knowing…"), needing no ramp ("on day one", "I'd
  hit the ground running", "Nobody would need to teach me…"), ease,
  readiness, credibility or fit ("…is second nature to me", "I'd be
  productive immediately", "Contractors would trust me instantly", "that's
  me"), an identity ("As a former electrician, I'd…", "…as a fellow
  tradesperson", "I'd be the electrician in the room"), and a past claim or an
  "I" that is not forward inside a clause with a modal ("I'd be the rare
  Director of Revenue Operations who has run a plumbing shop", "I'd thrive
  leading technicians, as I always have"). With no "I" at all, a hire the company would make counts
  too: "You'd be hiring a licensed electrician", "Fabrikam Payments would get a
  seasoned field service operations leader". "I'd love to understand your
  priorities", "As a first step, I'd…" and "As an Inside Sales Manager,
  I'd build pipeline" still pass. Fail closed: a past tense in a clause with
  a modal is a claim whatever its subject, so "I'd rather show the numbers
  than claim the team had been bigger" needs cited words too (an accepted
  false rejection). The third review of 2026-10-09 added possession brought
  along on arrival ("I'd walk in with the trade chops your team needs",
  "I'd show up with…", "armed with"), familiarity ("Equipment financing won't
  be new to me", "None of this would be new to me", "I've seen all of this
  before"), an ability ("I can navigate building-code compliance", "I could
  teach anyone trade licensing"; "I can help" is an offer and passes) and a
  skill verb after a modal ("I'd speak the technicians' language", "I'll
  train your reps…"). A claim that names nothing a cited bullet could source
  besides its own frame ("I can do this", "I'd bring that", "None of this
  would be new to me") fails whatever is cited. These markers are lists, so
  other wording of the same claim can still get through; the critic and
  your review of the staged form remain the check for that. One verb needs
  no cited word of its own (2026-10-10): a neutral change verb (climbed,
  grew, rose, increased, improved, went from) whose clause restates a move a
  cited bullet states. The clause's subject must be the move's own words in
  the bullet, all of them and in order (one leading verb such as "improving"
  or "lifting" may be left out), with no negation or modal before the verb,
  its figures the bullet's in the bullet's order, and nothing may follow
  them before the next punctuation mark but a "+" or a period ("a month").
  The sentence may name no year and no span of time other than a figure's
  own rate ("55–70 requests a week"). "…, and renewal rate climbed from 44%
  to 71%." passes on b_055 ("raising the renewal rate from ~44% to
  ~71%"), while "…climbed from 71% to 44%", "win rate climbed from 44% to
  71%", "rate climbed from 44% to 71%", "…climbed from 44% to 71% in a
  week", "…that week", "…before Q3", "within a week, renewal rate
  climbed…", "renewal rate never climbed…" and "I grew renewal
  rate from 44% to 71%" still fail, and so does "CAC climbed from 17% to
  26%" beside "cutting CAC by lifting win rate from ~17% to ~26%" (a move of
  win rate).

  The company applied to may be named as where the candidate wants to work
  ("I'd love to join Contoso", "I'm excited about the role at Contoso"), never
  as a past or current employer. Unless the corpus lists it as a role
  company, it fails as `fabricated_employer` (a hard fail) when named with a
  cue only a past or current seat has: "I previously worked at Contoso", "Yes,
  I was at Contoso before", "I've been with Contoso", "I have worked for
  Contoso", "I've been employed by Contoso", "I joined Contoso", "I left Contoso",
  "while at Contoso", "ex-Contoso", "a former Contoso AE", "formerly at
  Contoso", "an alum of Contoso", "a Contoso alum", "Contoso hired me", "I'd love
  to rejoin / return to / be back at Contoso". Other employment wording ("work
  at", "join", "at Contoso" in a first-person sentence, "my time at Contoso",
  "my Contoso team", "Contoso, where I…") passes only in a sentence that
  aspires and claims nothing: a forward word ("would", "I'd", "I'll",
  "want", "excited"), no past tense, every "I" forward, no "my" beyond a plan, and no
  word of tenure ("again", "back", "before", "still"). "At Contoso I ran the
  SDR team" and "I work at Contoso" fail; "At Contoso, I'd focus on pipeline"
  passes. "at Contoso" after the job sought or the company's own people ("I
  saw the role at Contoso", "…prepared me for the role at Contoso", "to meet
  the team at Contoso") passes unless the rest of its clause is a claim ("the
  role at Contoso that I held"). A client a cited bullet names stays a client
  ("accounts at Nakatomi", applying to Nakatomi). Fail closed: "I led 9
  AEs at Haulmark, and I'd love to join Contoso", "I'd love to build my career
  at Contoso", "The team at Contoso impressed me" and "No, I have never worked
  at Contoso" fail, accepted false rejections (a negation excuses nothing: "I
  never left Contoso" says you are still there), so you answer "Have you
  worked for us before?" yourself. A later sentence that points back without
  the name ("I worked there before") is not seen (review 2026-10-09).

  Second review of 2026-10-09: the check now fails closed. A list of cues
  kept missing new wording ("I worked at Haulmark, then Contoso", "I'd love to
  stay at Contoso", "Contoso gave me my first sales job", "I was Contoso's
  first AE", "I led Contoso's enterprise team", "I came from Contoso",
  "Current employer: Contoso", "I was on your sales team"). Now:
  - In a sentence with any "I", "me", "my", "we" or "our" (a present
    reaction such as "matters to me" or "caught my eye" aside), any mention
    of the company fails unless the sentence only aspires (as above, with
    "stay", "continue", "keep", "home" and "one more time" added to the words
    of tenure), names the job sought or the company's people ("I saw the
    Contoso posting"), compares ("a stack like Contoso", "a team Contoso's
    size"), or describes the company in the present ("Fabrikam is larger than any
    business I have worked in…", "Contoso is solving a problem I care about").
  - A seat cue fails in any sentence: right before the name, earlier in a
    list that runs on to it ("at both Haulmark and Contoso", "at places like
    Contoso"), right after it ("Contoso gave me…", "Contoso and I go way back"),
    a seat after "I was/I'm/I've been" ("I was an early Contoso hire", "I
    served as Contoso's Inside Sales Manager"), an employer label
    ("Current employer: Contoso", "Background: Contoso"), a résumé line
    ("Account Executive, Contoso", "Contoso | Account Executive", "Contoso,
    2019–2021") or a bare answer ("Yes, Contoso.").
  - "you"/"your" after a seat cue is the company: "I was on your sales
    team", "I'm one of your current reps", "I previously sold your
    platform", "I led your enterprise team", "Your company was my home". "I'd
    love to be on your team" passes. The job's own title tied to the company
    is a seat claimed when its clause makes a past claim: "I was your
    Inside Sales Manager" fails as `claimed_job_title` too.
  - Third review of 2026-10-09: "you"/"your" now fail closed like the
    company's name. In a sentence with any "I", "me", "my", "we" or "our",
    any "you"/"your" fails unless the sentence only aspires, or it is "thank
    you", the job sought ("I saw your posting", "I read your job
    description") or the company's people met or sought ("I enjoyed meeting
    your team", "For your reps, I'd…"). "I reported to your CEO", "I carried
    your quota", "I miss your sales floor", "Your sales playbook is the one I
    wrote" and "I have run your weekly revenue meeting" fail. "At Haulmark I
    built the onboarding program that cut time to first dispatch by ~40%, which is
    what your team needs" fails too (an
    accepted false rejection): write the comment as its own sentence ("Your
    team needs that."). An aspiration or a description of the company has no
    "my" beyond a plan ("Contoso is on my resume", "You'll see Contoso on my
    resume"), no "me" as a verb's object ("Contoso will remember me"), no
    re-contact ("I'd love to reconnect with Contoso") and no phrase that
    describes you as its customer or user ("I'd love to join Contoso as one
    of its earliest customers", "…, a power user of it since 2022", "A
    longtime customer, I'd…"; fourth review of 2026-10-15). "for", "to" or "into"
    name the job sought only after a word of wanting or applying: "I was
    promoted to the role at Contoso" and "I reported to the team at Contoso"
    fail, "I'm excited about the role at Contoso" passes. A present reaction
    with a word of tenure is an old one: "Contoso still matters to me" fails.
  - A lowercase spelling counts ("I previously worked at contoso",
    "monday.com", "hims & hers"). A name whose lowercase form is a word of
    the corpus or the job description ("pilot" for Pilot: "a small pilot")
    is judged in lowercase only after a seat cue.
  - A drafted answer is checked with its question. When the question asks
    whether you worked or work at the company ("Have you previously worked
    for Fabrikam Payments?", "Are you a current or former employee?", "Have you
    worked with us before?"), only a plain "No" (or "N/A") passes unless the
    corpus lists the company; "Yes." fails as `fabricated_employer` and the
    field stays open for you.
  Fail closed: "What drew me to Contoso is its mission", "I interviewed at
  Contoso last year" and "I'd love to stay in touch with Contoso" fail too
  (accepted false rejections).

  A sales tool or method from the questions' topic list (Clari, Highspot,
  HubSpot, MEDDPICC…) may be named in a letter or drafted answer only when a
  `skill_index` key is that tool; otherwise the sentence fails as
  `unsourced_tool`. A cited bullet that names the tool is not enough, because
  a bullet can say someone else used it: "At Haulmark I ran Clari forecast
  calls every week." citing "My manager ran Clari forecast calls at Haulmark"
  used to pass. The check does not look at who used the tool, so a sentence
  about the target ("Fabrikam Payments runs its forecast on Clari") fails too, an
  accepted false rejection; add the tool to `skill_index` (through its
  question, or by hand) if you did use it. One exception (2026-10-15): when
  the company applied to IS the tool (Aircall, Dialpad, Highspot, Seismic,
  Vidyard, Orum…), its name may appear as that company:
  the job sought ("I'm applying for the Director of Sales Enablement role at
  Aircall"), the object of an aspiration in its own clause ("I'd like to join
  Aircall", "work at Aircall", "excited about what Aircall is building") in a
  sentence that states none of your own facts, or a present-tense sentence
  about the company that opens with its name, has no I/my/me/we/our, and
  carries no number the posting does not state and no fact of your cited
  bullets ("Aircall makes a cloud phone and dialer…"; a past fact only in the posting's
  own words, "cut"/"put"/"set" read as past). Each mention is judged on its
  own: a usage word before it or a product word after it ("using Aircall",
  "rolling Aircall out", "three years on Aircall", "Seismic playbooks") makes it
  the tool whatever else the sentence says (review 2026-10-12: "I'd like to
  join your team after using Aircall at Haulmark" passed because the sentence
  aspired, and "Aircall is how 9 AEs hit quota" credited your Haulmark
  numbers to the product). Named as a tool the candidate used it still fails
  ("I used Aircall daily at Haulmark", "Haulmark ran its floor on Aircall", "Aircall
  was the team's dialer for years"). Every mention also fails when any
  sentence of the letter, before or after it, may point back at the product
  in a claim about you or your employers: a pronoun ("it", "its", "they",
  "their"), a demonstrative with no noun after it or "the same" ("dialed on
  this every day", "That's what we ran", "the same thing"), a product word
  ("the product", "that dialer", "the platform", "our tools") or a word of
  buying or holding it, bare or not ("a customer", "users", "seats", "the
  purchase", "our contract", "the renewal") beside a first person that does
  more than aspire, an employer's name, a fact a cited bullet states, or
  "the team there" ("I'd like to join Aircall. Thanks. I used it daily.",
  "…Haulmark reps lived in the product.", "…I was a customer.", "…My reps
  dialed on this every day."). A sentence that aspires no longer excuses its
  own pointers (fourth review of 2026-10-15): a phrase that describes you as
  the company's customer or user makes it no aspiration ("I'd love to join
  Aircall as one of its earliest customers", "I'd like to join Aircall, a power
  user of it since 2022", "…— longtime customer here", "from customer to
  colleague"), and a pronoun or product word that a usage word governs, or
  that sits inside an "as" phrase or appositive without being its head,
  still points back ("I'd love to join Aircall after using it", "…as an
  advocate for the platform"). "I'd like to join Aircall and help it grow",
  "…as it grows", "…as its next Director of Sales Enablement" and "Aircall, the
  dialer modern floors run on" still pass; "I'd love to build on it" and
  "…grow with it" fail (accepted false rejections). This fails closed: "At
  Haulmark I grew the program and its revenue", "At Haulmark I sold to our
  customers" and "I learned that in enablement, adoption is everything" in
  a letter to Aircall are rejected too, and the letter is regenerated with
  each pointing
  sentence and its word in the reason. A hyphen compound ("customer-facing")
  and a capitalized name your corpus writes ("Customer Success") are not
  words of buying. A drafted answer to a question that names the tool or
  asks about tools, use or experience gets no exception; any other drafted
  answer may name the target only as the job sought or in a sentence that
  only aspires, never in a sentence that opens with its name ("What did
  your reps dial with?" "Aircall is what modern floors run on." fails).

  The fact check still cannot see a claim that has no number, no proper noun
  and none of those words: "I also led the sales development team", or "I
  ran the merchant team for ~18 months", whose words all come from its cited
  bullets though no bullet says it. Holding every first-person sentence to
  its cited bullets' words would also reject honest sentences ("I coach
  deals on MEDDIC"). The authenticity critic (for a letter) and you remain
  the check for those.

  These rules reject some honest sentences on purpose ("For your Account
  Associates, that means notes on every recorded call, not only the calls
  that fit around my meetings", "Your team doubled its payment volume"). A false rejection
  costs a regeneration, or at worst a parked letter the human can fix. A false
  acceptance would send an unsourced claim.

  In a sentence that makes a claim about the candidate, the job's own title
  is never claimed as held. "I was the Inside Sales Manager at Haulmark"
  and "As an Inside Sales Manager, I coach reps daily" fail as
  `claimed_job_title`. The title may still name the role being sought: "I
  want to be an Inside Sales Manager", "I'm excited to apply as an
  Inside Sales Manager", "As an Inside Sales Manager candidate, I…",
  "As an Inside Sales Manager, I'd…", "As your Inside Sales Manager,
  I'd…" and "the Inside Sales Manager role at Contoso" all pass. So does
  an honest disclaimer that negates the seat itself: "I haven't held the
  Inside Sales Manager title, but…", "I have not been an Inside Sales
  Manager before". "I haven't just held the … title" and "I wasn't
  officially the …" are still claims.

  Each fact must belong to the employer named next to it, and a sentence that
  mixes facts from two employers must name both. A sentence that refers back to the one before ("It came from…",
  "The revenue came from…", "…came from them") is checked together with it. One
  employer's result may never be credited to another employer's work ("The
  ~$5.6M in Haulmark bookings came from usage-based pricing I launched at
  ledgerline"), even when both are named. One phrase used only in one
  employer's bullets ("usage-based pricing")
  counts as that employer's fact. Naming a company that is not the
  target, one of the candidate's employers, or mentioned in the JD or the
  corpus is a hard fail (`wrong_company`). The letter is regenerated up to
  twice with the violations fed back. If it still fails:
  - no `cover.pdf` or `cover.txt` is written, and `cover_rejected.md` holds
    the violations and the last draft, marked NOT FOR SENDING;
  - the application stays at `approved` with no `cover_path`, so it is never
    staged;
  - a `cover_provenance_failed` event is recorded. MCP `review_artifacts` and
    `get_artifacts`, and `jobkit review <job_id>`, show it.
- **No re-tailoring loop.** A cover letter that fails, by the fact check or by
  authenticity after two regenerations, leaves its job at `approved`. Its
  failure event records the attempt key: JD hash, corpus hash and prompt
  versions (the self-review prompts, `self_review.max_revision_rounds` and
  the claim judge prompt included). `jobkit tailor` and `jobkit run-all` skip such a job, logging
  `tailor.skipped_cover_failed`, until one of those inputs changes. A corpus
  edit therefore retries every parked job once. A stored letter that
  `jobkit rejudge` failed has no attempt key, so an approved job is
  re-tailored once (see the claim judge, below). To retry one job by hand,
  for example after editing the corpus, call MCP `retailor(n)` or run
  `jobkit tailor --job-id N`. `retailor` refuses an application that is
  already staged or further along.
- **Novel answers.** The same check runs against the labeled profile ids the
  answer cites. In an answer, a window such as "within 30 days" usually
  describes the candidate's availability, notice or relocation. Like every
  number, it needs a cited source. Only the human or the Answer Bank may state
  those facts (hard rule 8). A draft that fails twice is not stored. The question stays
  open with `flag: unsourced-claim` and a `fact_check` list, which MCP
  `get_novel_questions` shows. A `novel_provenance_failed` event is recorded,
  and `jobkit form-specs` prints `fact_check_failed=N`.
- **Authenticity critic.** A missing, empty or unparseable critic verdict counts
  as `fail`, never `pass`. On resumes, em-dash and semicolon density flags that
  come from the corpus's own punctuation are marked `corpus-inherited`. Those
  flags still show, but they no longer stop the critic from running. The
  critic can only make the verdict worse.

### Claim judge: a second check on letters and answers

The rule-based fact check reads numbers, names, employers and a list of
experience phrases. It cannot read meaning, so a claim in plain words gets
past it: "I also led the sales development team.", "I know trade licensing
inside and out.", "Fabrikam Payments was a great place to work." (that one says
you worked there). An LLM claim judge reads every cover letter and every
drafted novel answer written from now on that the rule-based check passes
(`src/jobkit/validate/claim_judge.py`, prompt `prompts/claim_judge.md`, stage
`claim_judge` on `claude-opus-5-5`). Letters and answers stored before the
judge existed were never judged, and staging still fills them until
`jobkit rejudge` checks them (below).

- **What it reads.** The text; the bullets it cites, each with employer,
  title, dates and metric; every role; education; the skill index with its
  notes; the full labeled corpus; the target company and title; and, for an
  answer, the form question.
- **What counts as a claim.** Anything about your past, your skills or
  experience, your employers, titles, results, credentials or relationships,
  including a claim that is only implied. These are not claims: what you want
  to do in the role, opinions about the target company, facts about the
  target, and greetings and thanks.
- **A denial is a claim** (judge prompt 1.1). "No." to "Have you ever been
  terminated or asked to resign?", "Have you ever been convicted of a
  felony?" or "Have you ever been placed on a PIP?" states a fact about your
  past that the corpus never records, and so does "N/A" or "I have not". The
  only denial with a source is "I have not worked at" a company your role
  list does not include (a plain "No." to "Have you previously worked for
  us?"). The judge reports any other denial, about terminations, discipline,
  convictions, non-competes, sponsorship or work status, or licenses, and the
  question stays open for you. Version 1.0 exempted every plain denial.
- **What supports a claim.** A cited bullet, the role list, education, or a
  skill index entry that states it. A fact stated only in a bullet the text
  did not cite counts as unsupported, because the writer must cite what it
  uses. That can reject an honest sentence; it costs one redraft.
- **It can only reject.** It runs only after the rule-based check passes, so
  it never sees, and can never rescue, a draft the rules rejected. A `fail`
  verdict, a `pass` that still lists a claim, and a reply that is not the
  expected JSON all fail the draft.
- **A judge failure is a fact-check failure.** A letter is regenerated up to
  twice with the judge's reasons fed back. If it still fails,
  `cover_rejected.md` lists them as `claim_judge_unsupported` (or
  `claim_judge_unparseable`) violations with the sentence each is in, the
  application stays at `approved`, and a `cover_provenance_failed` event is
  recorded. A drafted answer gets one regeneration. If it still fails, the
  question stays open with `flag: unsourced-claim` and the judge's reasons in
  its `fact_check` list.
- **If the judge cannot be reached** (every retry failed), nothing is written
  as final and no failure is recorded. The letter's job stays at `approved`
  and the next `jobkit tailor` or `jobkit run-all` tries again. A drafted
  answer stays open for you.
- **Cost and caching.** One strong-model call per draft that passes the rules,
  cached in `llm_cache` like every other call. The cache key covers the full
  prompt, so the same draft is judged once, and a changed draft, corpus or
  prompt version is judged again. A reply that does not parse is never
  cached. `provenance.json` records the judge's prompt version
  (`claim_judge_prompt`). The cover attempt key includes it, so a job parked
  by the judge is retried when the judge prompt changes. A job parked before
  the judge existed stays parked, because the judge only adds rejections.
- **No switch.** It is a fabrication guard (hard rules 3 and 8), so it
  cannot be turned off. It runs inside `jobkit tailor` (MCP `retailor`),
  `jobkit run-all` and `jobkit form-specs`, and on stored texts in
  `jobkit rejudge`. Its findings show where fact-check failures already
  show: `jobkit review <job_id>`, MCP `review_artifacts`, `get_artifacts`
  and `get_novel_questions`.
- **Letters and answers stored before the judge: `jobkit rejudge`.** A
  letter tailored, or an answer drafted, before the judge existed (or under
  an older judge prompt) was never judged. Run `jobkit rejudge --dry-run`
  (MCP `rejudge()`) to list them, with no LLM call, then `jobkit rejudge`
  (MCP `rejudge(confirm=true)`, which runs in the background).
  `--job-ids 12,15` (MCP `rejudge(job_ids="12, 15")`) checks only those
  jobs. Each stored text gets the check a new draft gets: the rule-based
  fact check against the current corpus, then the judge.
  - **What it checks.** Every job not submitted and not applied to,
    rejected and closed postings included, because `jobkit stage` and MCP
    `stage_forms(job_ids=…)` can still stage them. For a letter, the
    application at `tailored` with its `cover.txt`, and the citations and
    resume bullets in its `provenance.json`. For answers, every stored
    answer in the form spec except the ones you gave through
    `answer_novel`, which are never judged. A stored answer keeps no
    citations, so it is checked against none: the role list, education and
    skill index can still support it, but a fact that only a bullet states
    fails it. That can reject an honest answer, and the question comes back
    to you. A letter missing its `cover.txt` or `provenance.json` cannot be
    checked and fails closed.
  - **What happens to a failure.** A letter gets the state a letter that
    failed when drafted is left in: `cover.pdf` and `cover.txt` are removed,
    `cover_rejected.md` holds the reasons and the letter marked NOT FOR
    SENDING, the application goes back to `approved` with no letter, so it
    is never staged, and a `cover_provenance_failed` event is recorded. That
    event has no attempt key, so the next `jobkit tailor`, `run-all` or
    nightly run re-tailors an approved job once with the judge in the loop.
    If that letter fails too, the job is parked as usual. An answer is
    removed from the drafted answers and goes back to your open questions
    with `flag: unsourced-claim` and the reasons (MCP
    `get_novel_questions`), with a `novel_provenance_failed` event.
  - **What it skips.** A letter whose `provenance.json` names the current
    judge prompt (judged when tailored), an answer whose form field records
    the current judge version (form capture now stamps every draft the judge
    passed, as `claim_judge` in the form spec's fields), and anything an
    earlier `rejudge` passed under the current version. A text that changed
    while the judge was reading it (re-tailored, recaptured, or answered by
    you) is left alone.
  - **If the judge cannot be reached**, that text is left as it was and
    unjudged, the report lists it under NOT CHECKED, nothing is recorded,
    and the command exits 1. Run it again.
  - **Never staged until judged.** Whether or not `rejudge` has run, a
    tailored letter with no record that the judge passed it (its
    `provenance.json` names no `claim_judge_prompt`, its fact check did not
    pass, or the file is missing or not the letter's own) is not final:
    `jobkit stage`, MCP `stage_forms`/`restage`/`open_queue` halt the job
    as `cover-letter-needs-retailoring` before the browser touches
    anything, and the nightly run skips it and names it under "Needs you".
    It is marked once with a `cover_needs_retailor` event and listed in the
    digest's Staging section, MCP `get_queue` and `schedule status` until it
    is re-tailored with the judge (below) or a `rejudge` passes that very
    letter (`src/jobkit/pipeline/letter_gate.py`).
  - **By hand instead.** `retailor(n)` or `jobkit tailor --job-id N`
    re-tailors one letter with the judge. For one job's answers, run
    `jobkit rejudge --job-ids N`, or answer the question yourself with
    `answer_novel`.
  - Each job checked gets a `claims_rejudged` event (judge version, the
    letter and whether it passed, the answers that passed and failed). The
    judge's calls are cached on the full prompt, so checking an unchanged
    text again costs nothing.

### Scoring and tailoring for the target roles

- **Scoring profile.** The fit scorer sees every corpus role with its title,
  company, start and end month and years in role, with that role's bullets
  under it; total years in sales-titled roles (a title the role-track
  classifier places, or one containing "sales"); career span; education;
  `skill_index`; and the never-claim lists. Years are computed as of the
  current month, which is printed in the profile, so a corpus edit (or a new
  month) changes the rendered prompt and misses the score cache.
- **`prompts/score_fit.md` 1.5.** Five worked anchors (mid-market AE,
  SMB account manager, senior mid-market AE, sales manager, VP Sales), each
  scored against one invented example profile (an account manager turned
  mid-market AE). The score is must-have coverage and interview likelihood,
  tenure is computed from the role dates, and overqualification is listed as
  the last gap (`overqualified: …`) instead of scoring a role near zero. Jobs
  scored under an earlier version keep their scores: `triage` only scores
  unscored jobs.
- **Cover letters (`prompts/cover_letter.md` 1.4).** Paragraph 3 closes on a
  concrete, cited win the role needs, then asks for the conversation. A gap
  is mentioned only when it is one of the job's extracted must-haves, framed
  as how the candidate covers it. The model gets the must-haves and the
  scorer's gaps minus any `overqualified:` gap.
- **Repeated-phrase guard** (`config/banned_phrases.yaml`, version 1.3).
  `cover_letter.hard_reject_patterns` hard-fails known tics, each named for
  what it does rather than for one wording: a contrast construction, a gap
  announcement, a candor preface, a title-never-held confession and an
  apologetic gap close.
  `cross_letter` compares each new letter's 5-word phrases with the newest 50
  earlier letters (`out/*/cover.txt`): a phrase already used in 2 of them fails
  the letter (`on_repeat: fail`) and the regeneration prompt names it. A
  retold fact is not a repeat: phrases made only of corpus words, this job's
  JD words and the target's name are exempt; words are compared by stem
  ("delivered" retells "delivering"); numbers and hedges never count
  ("roughly 18%" retells "~18%"); a phrase that retells one of the letter's
  cited bullets may add as many narrative words as it carries of that
  bullet's words and figures ("delivering 131% of annual quota"); and the
  plain ask ("I'd like to talk about") is not phrasing. In a leave-one-out
  check over a batch of generated letters (each with its corpus, JD and
  `bullets_used`), no retold fact was flagged.
- **Track-aware selection.** Tailoring passes the job's role track
  (`Filters.track_for`, or `other`) to `prompts/select_bullets.md` 1.3, which
  sets what leads for `sdr_bdr`, `sdr_bdr_manager`, `account_executive` and
  `sales_exec`. Emphasis and order only; every bullet is still corpus text
  checked by provenance. The track is written to `provenance.json`.
- **No dropped metrics.** A bullet variant that drops a number its other
  variant carries is never used: the tailor swaps it, restores a rendered
  bullet that trimmed away one of its own numbers, and the one-page shrink
  only shortens bullets whose short variant keeps every number.
- **One page, counted right.** `render/pdf.page_count` reads the PDF's page
  tree (`/Count`) or its page objects, whatever the spacing and inside
  compressed object streams. The old count looked for `/Type /Page` with a
  space; Typst writes `/Type/Page`, so every resume counted as one page and
  the one-page rule never shrank a two-page resume (review 2026-10-12). A PDF
  in which no page
  is found raises, and tailoring then asks Typst itself
  (`render/pdf.rendered_pages`: the same template and data compiled to SVG,
  one image per page).
- **No empty roles.** The current role and every role that ended within
  `resume.recent_role_years` (filters.yaml, default 5) get at least one
  bullet; when the model leaves one empty, the role's first corpus bullet
  that passes provenance is added. Older roles with no bullet are folded into
  one "Earlier experience" line. `provenance.json` records `required_roles`,
  `selection_adjustments` and `earlier_experience`.
- **Artifact folders** are `out/<company>-<title>-<job_id>-<date>/`, so
  duplicate postings no longer overwrite each other.
- **Header location** comes only from `location_strategy` in filters.yaml:
  the first rule whose `when` matches (`job_state`, `job_city_in`, `remote`),
  else `default`, else the corpus `identity.display_location`.

### Self-review: the system checks its own drafts

Each drafted artifact is reviewed by the model before a human sees it. That
covers the resume bullet selection, the cover letter and each drafted novel
form answer.

- **What the review sees** (`prompts/self_review.md`, stage `self_review`,
  `claude-opus-5-5`): the job's must-have, nice-to-have and implied
  requirements; the draft; the full labeled corpus (bullet ids, employer,
  title, dates, metrics, `metric_confidence`, positioning variants,
  `skill_index`, never-claim lists); the gap report; the role track; and any
  violations the draft failed. A cover letter's review is told it may cite
  only the bullets on the resume.
- **What it asks itself.** Which must-haves are unaddressed although the
  corpus supports them? Which strong corpus facts are unused? Is the opening
  specific to this company and role? Is anything apologetic, generic,
  repetitive or weak? Does anything over-claim? Is there anything to change
  to give the best answer? It returns `verdict` (`good` or `revise`), a
  `changes` list (what, why, bullet ids) and `questions_for_human`.
- **Only a passing resume selection is ever revised.** A resume bullet
  selection that passed its checks and got `revise` is rewritten once by the
  tailor stage, with the change list appended to its prompt
  (`prompts/self_review_revision.md`). Before the writer sees the list, each
  change whose `what` uses a word that is in none of the corpus, the passing
  selection and the job's title is dropped (`validate/revision.py`). The
  company's name supplies no word unless the corpus has it, and words of
  tense and tenure ("was", "were", "been", "had", "did", "before", "once",
  "ever", "ago", "previously", "formerly", "currently") need a source like
  any other word, so "Say I was at Fabrikam Payments before" is dropped (review
  2026-10-09; "Say the General Manager was who I reported to" is dropped too,
  an accepted false rejection). Editing words such as "add", "lead with" and
  "cut" do not count. "Mention managing SDRs" never reaches the writer. The
  report lists dropped changes, and when every change is dropped nothing is
  revised. A revision may add, drop, reorder or swap corpus bullets and change
  the skills line, but it may not reword a bullet: every bullet it renders
  must be its named variant's corpus text, word for word, or the revision is
  discarded (`revision_reworded`). This is needed because provenance checks
  only a bullet's numbers, proper nouns and never-claim terms. A revision that
  kept b_052's text and added "I also managed the sales development team."
  passed it and shipped (sixth review of the self-review, 2026-10-09). A
  first draft may still trim a bullet lightly. The revision then runs the
  selection's full check chain again (provenance, then authenticity). Its
  authenticity verdict may not be worse than the selection it replaces, and
  its drift pass may flag no new word. A revision that fails anything is
  discarded and the original ships. A fact the job needs and the corpus lacks
  belongs in the questions for you.
- **A cover letter or drafted answer is never revised.** Its review is
  reported only: the changes appear in the report as "suggested (not
  applied)", and the letter or answer that passed the fact check (and, for a
  letter, the critic) is what ships. Rewritten prose cannot be checked
  against the corpus simply. An earlier version allowed a revision when each
  new or changed sentence used only words from its cited bullets, and that
  let fabrications through: moving "I also led 9 AEs there" after the
  ledgerline sentence re-attributed the team to ledgerline, and "I ran the merchant
  team for ~18 months." used only words its bullets contain (fifth review of the
  self-review, 2026-10-09).
- **A draft that failed its checks** (for example, a letter that failed the fact check) has its
  violations passed to the review, and that review is reported only. The
  regeneration carries the same check feedback as before the self-review
  existed and nothing from the review, inside the existing cap (resume 2
  attempts, letter 3, answer 2). It is checked as before: provenance and
  authenticity for the resume, the fact check and the critic for a letter,
  the fact check for an answer. An earlier version sent the review's changes
  along, screened against words that included the failed draft's own; a
  change could then endorse a sentence the fact check cannot see ("keep the
  line about managing the inside sales team"). Nothing else changes from
  before: a letter that fails the fact check is rejected (one that fails
  only the critic is rendered with its critique for you), a resume fails
  loudly, and an answer's field stays open for you with the reason.
- **One knob:** `self_review.max_revision_rounds` in `config/filters.yaml`
  (default 1) caps the revise rounds of a passing resume selection per run,
  and the reviews per artifact at max(1, knob). A failing draft's review
  counts as one. At 0 the review still runs and reports, but nothing is
  revised.
- **Cost guard.** Each review goes through `llm.call_json`. Its cache key is
  namespaced by artifact, JD hash, corpus hash (profile hash for answers) and
  prompt versions, and llm.py also hashes the full rendered prompt into it.
  Tailoring the same job again therefore reviews nothing twice, and an
  accepted revision is cached too. A discarded revision, or a reply without a
  valid verdict, is dropped from the cache. A review that fails or returns no
  valid verdict changes nothing: the checked draft ships and
  `self_review.failed` is logged.
- **Records.** `out/<job folder>/self_review.md` lists, per artifact, what the
  model asked itself, its verdict, the changes it asked for and why, the
  bullets added or dropped, why a revision was discarded, and the questions
  for you. It is rendered from `self_review.json` in the same folder, so form
  capture and tailoring each add their artifacts to one report. Each review
  is an events row (`kind 'self_review'`, detail: artifact, round, verdict,
  outcome `no_change|accepted|discarded|not_revised`, `original_failed` when
  the reviewed draft had failed its checks, changes kept and dropped by the
  screen, bullets added and dropped, why a revision was discarded).
  `provenance.json` carries the outcomes as well. When a discarded resume
  revision's authenticity run was recorded, the shipped selection's run is
  recorded again, so the newest `authenticity_runs` row is always the
  artifact that ships.
- **Questions for you.** Questions ask only for facts that are absent from
  the corpus and would close a real gap. Questions about compensation history
  or demographics are dropped (hard rules 4 and 5). Each question is stored
  once per job and key as an event (`kind 'question_open'`, detail: `key`,
  `question`, `why`, `fact_needed`, `job_id`, `artifact`). They are put in
  front of you, merged with the scorer's recurring gaps, as described in the
  next section.
- **Read it:** MCP `get_self_review(n | job_id, full)` or
  `jobkit self-review <job_id> [--full]`. Both show the latest outcome per
  artifact, the questions raised, the report path, and with `full` the
  report itself. Neither makes an LLM call.

### Questions for you, corpus proposals and `rescore`

The system asks you only for facts it cannot know, and then adjusts
everything they affect (`src/jobkit/pipeline/questions.py`,
`src/jobkit/pipeline/rescore.py`). No LLM call is made to build, show or
record a question.

- **Where questions come from.** Two sources, over open jobs that are queued
  or approved:
  - the self-review's questions (`question_open` events, see above);
  - gaps the fit scorer names in `scores.gaps`. Gap text is free prose, so it
    is reduced to topics from a fixed vocabulary: sales tools
    ("Clari proficiency (required tool not documented)" becomes `tool-clari`),
    sales methodologies (`method-meddpicc`) and experience types
    (`exp-healthcare`, `exp-sdr-bdr-team-management`, `exp-public-sector`…).
    An `overqualified:` gap is never a question. Neither is an experience gap
    about tenure or degree ("3+ years closing", "limited enterprise
    exposure"), because the corpus dates already state those facts. A gap with
    no known topic is not asked.
- **What is never asked.** A topic the corpus already answers is dropped. A
  tool or method counts as answered only when a `skill_index` key is that
  tool ("Clari" or an alias such as "SFDC" for Salesforce; not "Clari
  dashboards") or a never-claim list names it. A bullet, role or
  `skill_index` value that mentions it in passing ("the team used Clari")
  does not answer it. An experience topic counts as answered when its name
  matches the skill index (keys and values), a bullet (text, tags, metric),
  a role, education, a story or a never-claim list. A key you answered or
  declined is not asked again (`jobkit undecline` reopens a declined one).
- **Dedupe and ranking.** A self-review key that is only a topic plus generic
  words (`clari_experience`) merges into that topic. Any other key is
  normalized (generic words dropped, words sorted), so `ledgerline_expansion_revenue_split`
  and `expansion_revenue_split_at_ledgerline` are one question. Questions are ranked by
  the number of open jobs they would help, then by those jobs' summed fit
  scores.
- **At most N a day.** One knob: `questions.max_per_day` in
  `config/filters.yaml` (default 3; 0 asks nothing). The digest's "Questions
  for you" section, `jobkit questions`, MCP `get_questions` and the end of MCP
  `get_queue` show the same day's questions. Each shows the question, why it
  matters and which jobs it would help. A question you answer or decline that
  day still uses its slot, so answering one does not reveal another until the
  next day. `jobkit questions --all` (`get_questions(show_all=true)`) lists
  every open one without using the cap. Days are counted with
  `question_asked` events.
- **Answering.** `jobkit answer <key> "<text>"` or MCP
  `answer_question(key, answer, …)`.
  - The whole answer is judged, never only how it starts. An answer that is
    only a no ("no", "nope", "n/a", "never used it", "I haven't", "I have
    never used Clari", "I've not used it", "unfortunately not", "not that I
    recall", "No, never sold into healthcare") is a `question_declined`
    event. So is `--decline` (MCP `decline=true`), which needs no text. The
    question is not asked again, nothing is added to the corpus, and the gap
    stays a gap. `jobkit undecline <key>` (MCP `undecline_question(key)`)
    takes a decline back: the question is open again and can be answered.
  - An answer that may be a no or a fact is refused and nothing is recorded:
    one that opens with a negation but is not a plain no ("Never below 115%
    of quota at Haulmark", "None of my AEs churned", "No, but I used HubSpot
    daily"), and one that denies the thing asked about ("Used Gong daily;
    never used Clari", "limited exposure to Clari"). Answer just "no" (or use
    `--decline`), or give the fact so that it does not open with a negation.
    Only a threshold with a figure may, after "yes" ("Yes, never below 115%
    of quota at Haulmark" writes "Never below 115% of quota at Haulmark"). The
    text written drops the "yes" and is checked again, so any other opening
    negation is refused: "Yes, not in healthcare." would write "Not in
    healthcare." and "Yes, no healthcare at all." "No healthcare at all."
    (review 2026-10-09, third pass). A hyphenated word
    is one word: "No-code" is not a no, so "No-code workflows at ledgerline" is
    a fact (to a tool question it is still refused: see below). "Definitely
    not", "Absolutely not", "Negative" and "Clari? No" are plain noes.
  - **A tool or method answer must be one plain sentence of your own use**,
    because it becomes a `skill_index` name a tailored resume may list,
    whatever the value says. The whole answer must have this form:
    `[Clari:] [yes,] I used Clari` (or "I've used", "I have used", "I use",
    "I ran"; "it" may stand for the tool), followed by nothing but how often
    ("daily", "weekly", "twice a week", "every day"), how long ("for 2
    years", "for 18 months", "for the past two years"), where ("at
    <company>", only a company of your own corpus roles) and for what ("for
    forecasting", "on every deal", "in pipeline reviews"), e.g. "I used
    Clari weekly at Haulmark for two years". The tool must be named as the
    tool: "Clarity", "Clari-like", "Clari's" and (for tools named with
    everyday words) lowercase "outreach" or "clay" are not it. Anything else
    is refused, the reply says what failed, and nothing is recorded: "Yes,
    daily at Haulmark" (a bare "yes" or "daily" no longer opens an answer),
    "Clari? Yes, I used it" (no "?"), "Yes, my AE", "My manager used Clari",
    "Yes, my manager's Clari dashboards", "Yes, I tried it", "Yes, if hired",
    "Clari? Only heard of it", "Only HubSpot and Salesforce" and even "Yes,
    never below 115% of quota". A refusal costs one re-answer; a wrong
    proposal can put a tool you never used on a resume. A tool answer filed
    with `--kind bullet` is held to the same rule, and `--kind skill` is
    refused for anything but a tool or method question (an experience such
    as "healthcare" is never a skill item). Confirm re-applies the rule, so
    a proposal recorded under an older, looser rule is refused there.
  - **A tool or method is added only through its own question.** An answer
    to any other question (an experience, a self-review fact) that names a
    tool or method from the topic list is refused, whoever used it: "Yes,
    my manager ran Clari forecast calls at Haulmark" and "Yes, sold to 12
    lab couriers at Haulmark; the team used Clari for the forecast" both are. A
    bullet naming a tool would let a letter claim the tool as yours. The
    reply names the tool's own question (`tool-clari`); answer the first
    question again without the tool, or decline it. Tools named with
    everyday words count only when written as the tool ("Outreach", not
    "cold outreach"). Confirm re-applies this rule to a bullet proposal's
    answer and text.
  - Any other answer becomes a **proposal** in `data/corpus_proposals.yaml`
    (gitignored, written by jobkit), with `corpus_proposal` and
    `question_answered` events. The corpus itself is not touched. A tool or
    method answer proposes a `skill_index` item named after the tool; any other
    answer proposes a bullet whose `text_long` and `text_short` are your answer
    as written, with a leading "yes" (or "yeah", "yep", "yup") dropped. Nothing is rewritten or improved
    (hard rule 6). `--kind skill|bullet` overrides the default. The reply shows
    the exact entry that confirming would write and, for a skill item, the
    skill name next to the exact text it writes and your whole answer,
    verbatim. Answering the same key again replaces a pending proposal. A
    bare "yes" with no fact is refused, and so is an answer whose text, with
    the "yes" dropped, is a no or may be one: "Yes, no." would write "No."
- **Confirming is the human gate.** `jobkit confirm-proposal <id>` or MCP
  `confirm_proposal(id, …)` writes the entry into `data/career_corpus.yaml`.
  It is refused until you have set `evidence` (where the fact is defensible
  from) and `metric_confidence` (`verified`, `estimate` or `none`), and, for a
  bullet, the `role_id` it belongs to. You can set them when answering or
  when confirming; they are never defaulted (hard rules 6 and 8). It is also
  refused when the text names something your corpus `constraints` say never
  to claim, when your answer reads as a no or may be one under the rules
  above (a proposal recorded before they existed): a no never enters the
  corpus, so decline it instead, when a bullet proposal names a tool or
  method, and when a skill proposal is not for a tool or method question or
  names anything but that question's tool. The same rules apply to the exact
  text the entry would write (the proposal's `text`), not only to your
  answer: the proposals file is plain YAML, and a text edited to "I have
  never used Clari" beside the answer "Yes, I used Clari weekly at Haulmark"
  used to be written (review 2026-10-09). An empty text is refused too. The
  one excused text is your answer less a leading "yes" when it opens with a
  negation and denies nothing ("Yes, never below 115% of quota" writes
  "Never below 115% of quota"). Every confirm reply (and the "Awaiting your
  confirmation" list) shows that exact text, whole and never shortened, with
  the skill name for a skill item and your whole answer, verbatim, when it
  differs from the text. The entry is inserted as text at the end of its block, so the
  file's comments and layout stay as they are. The result is parsed again and
  must equal the old corpus plus exactly the new entry, or nothing is written.
  The previous file is kept as `data/career_corpus.yaml.bak`. A skill item's
  value is your text plus "(confirmed DATE; evidence: …; metric_confidence:
  …)". A bullet gets the next `b_NNN` id, a topic tag, the figures in your
  text as `metric`, and your `metric_confidence` and `evidence`. Confirmation
  records a `proposal_confirmed` event and starts `jobkit rescore --job-ids …`
  in the background for the jobs the question listed.
- **`rescore`.** `jobkit rescore [--job-ids 12,15] [--dry-run]`; MCP
  `rescore(job_ids, confirm=false)` is a dry run, and with `confirm=true` it
  runs `jobkit rescore` in the background. Every score now records the corpus
  hash it was made against (a `score_profile` event per score). By default
  rescore re-scores open jobs whose newest `score_profile` names another
  corpus hash, or that have none. The statuses it covers are queued, filtered,
  rejected only by the score gate (newest rejection `score_below_surface:` and
  no human or standing-rule decision), and approved. With `--job-ids` it
  re-scores those jobs whatever their hash, applied jobs included. Afterwards
  it re-applies the queue gate (`gates.min_score_to_surface`) and the standing
  rules. A queued or filtered job below the gate is rejected with the usual
  `score_below_surface` reason, and a score-gate rejection at or above it is
  queued again. **Approved and applied jobs never change status**; their new
  score is reported. A job rejected by the filter, by the sanity layer, by a
  standing rule or by you is never revived. A score-gate rejection is revived
  only if the deterministic filters still pass it under the current
  `config/filters.yaml` (evaluation order: filters, then sanity, then
  standing rules). One that fails them now (a posting older than
  `filters.max_age_days`, a blocklisted company, a title or comp the config no
  longer accepts) is not re-scored and stays rejected; the real run records
  that filter-layer rejection with its reason, so no later rescore picks it
  up. The dry run lists these jobs. Each result is a `rescored` event (old and
  new score, status before and after). MCP `rescore()` lists the latest
  results.
- **A file that does not parse.** If `data/career_corpus.yaml`,
  `data/corpus_proposals.yaml` or `questions.max_per_day` cannot be read, the
  digest's "Questions for you" section, the end of `get_queue` and
  `get_questions` show one "Questions unavailable: …" line naming the file
  (logged as `questions.unavailable`); the rest of the digest and the queue,
  staging halts included, render as usual. `answer` and `confirm-proposal`
  refuse with the same reason and write nothing.

### Staging: what gets filled, and when it stops

`jobkit stage` (MCP `stage_forms`, `restage`) and `jobkit open-queue` (MCP
`open_queue`) fill forms in a headed browser and never click submit.

- **Fields are matched again at staging time**, against the current answer
  bank, so a bank edit reaches jobs already in the queue. A changed verdict is
  logged as `stage.rematched`.
- **Identity values go only into identity fields.** Name, email, phone, links
  and address fill a field only when its label is a short field name (at most
  4 words, no "?", matched as a whole label) or the ATS's own field id
  (`_systemfield_name`, `first_name`, `urls[GitHub]`, …). A question that
  mentions "email", "country" or "location" goes to the screening answers or
  to you. Screening aliases are checked before identity.
- **Dropdowns, radios and checkboxes** (`src/jobkit/autofill/options.py`).
  Every option is read, not only the first 30 (in an alphabetical state
  list, North Dakota is option 35). A long list that
  renders only part of itself is filtered by typing the answer, never with
  Enter. The answer must land on one of these, in order: the form's decline
  option, for a decline answer; an exact match; a known equivalent ("PA" =
  "Pennsylvania" = "Pennsylvania (PA)"; "United States" = "United States of
  America" = "USA" = "US" = "U.S."); the bank entry's `select_preference`;
  the option starting with the same Yes/No, only when exactly one does or
  exactly one is that bare word (a bare "No" among "No, this is my first time
  applying to this role" and "No, I applied to this role more than 6 months
  ago", or among "No, but I will require sponsorship in the future" and "No,
  I do not require sponsorship", is left blank unless `select_preference`
  names the option); or a safe partial match. A
  partial match is refused when the extra words are a negation, a conflicting
  number or a different state, or when two options fit equally. A state or
  country answer never gets a partial match, so North Dakota never becomes
  North Carolina, and New Jersey never becomes New York or Jersey. With no safe option the field stays blank as
  `fill-failed`, and `stage.no_safe_option` logs the answer and the options it
  saw.
- **Where you live.** Country- and state-of-residence questions, however they
  are worded ("Please choose the country in which you are located", "Which
  state do you live in?"), use the bank's `country_of_residence` and
  `state_of_residence` screening entries. A sponsorship, eligibility or
  relocation question that mentions "the country where the job is located" is
  not a residence question.
- **Location pickers.** An autocomplete location field (Ashby's Location,
  a form's "working location" picker) gets the bank's place typed in: "Bristol, PA",
  then "Bristol, Pennsylvania", then the state name. Staging takes the
  suggestion for the same city in the same state. Failing that, once the
  state name is typed, it takes the state itself. A suggestion that names
  another state, two states or another city is never taken. If nothing fits,
  the box is cleared and left to you. A plain text location box is simply
  typed into.
- **EEO / voluntary self-identification** is filled only from the `eeo` block
  of `data/answer_bank.yaml`. Its keys are `gender`, `race_ethnicity`,
  `hispanic_latino`, `veteran_status`, `disability_status`,
  `sexual_orientation` and `transgender`; pronouns are never filled, and
  neither is a question with no key of its own: membership of the LGBTQIA+
  community, or the sex assigned at birth. `transgender` answers only a
  question about being transgender ("Do you identify as transgender?"); a
  gender question that lists it as an example ("Gender identity (e.g. woman,
  man, transgender, non-binary)") takes `gender`. When the
  stored answer declines ("I don't wish to answer"), the form's own decline
  option is chosen, whatever its wording ("Decline to self-identify", "I do
  not want to answer", "Prefer not to say", "I choose not to disclose").
  Only an option that is purely a decline counts: one that states a status
  and declines only its detail ("I am a protected veteran, but I choose not
  to self-identify the classifications to which I belong", "Yes, I have a
  disability, but I prefer not to say which") is never chosen. Among pure
  decline options the exact wording wins, then the shortest.
  In a "mark all that apply" checkbox group, only that box is ticked. With no
  pure decline option, or no `eeo` entry, the field stays blank and flagged.
- **Legal acknowledgements.** Arbitration agreements, AI-use policies and
  consent to contact or check references are filled from the bank's
  `legal_acknowledgement` entry, and only when that entry exists and the
  label is worded as an acknowledgement: your own statement ("I understand
  and agree …", "I have read …"), an instruction ("Please read the
  arbitration agreement below"), a consent question ("Do you consent to us
  contacting your references?") or a known title ("Agreement to Arbitrate",
  "AI Policy for Application"). A question about you ("Arbitration
  experience", "Familiarity with AI policy frameworks") never takes the entry,
  and neither does a label that tells you to read the policy and then asks
  something other than agreement or consent ("Please review our AI policy.
  Will you use AI assistance during your interviews?", "… let us know if you
  used AI to prepare this application"): it goes to you as
  `legal-acknowledgement-needs-human:other`.
  Staging picks the one affirmative option ("I understand and agree to …",
  "I will read …", or a `select_preference` entry) and ticks a single consent
  checkbox. Without the entry these fields are left blank as
  `legal-acknowledgement-needs-human:covered`. Any other acknowledgement or
  consent (a privacy notice, consent to recording an interview, terms and
  conditions, "Have you read the arbitration agreement?") goes to you as
  `legal-acknowledgement-needs-human:other`. The entry's `aliases` cannot
  widen it: an alias or embedding hit on a label outside those kinds is left
  blank and flagged too. The AI never drafts an answer to one.
- **Contact details** (`never-draft:contact-detail`): any question that
  mentions email is treated as asking for an address (yours, a referrer's, a
  former employee's) and is never drafted. The one exception is a question
  about doing sales work: it asks about your experience or activity ("Have
  you …", "How many …", "experience") in sales-work words ("prospecting",
  "sequences", "outreach", "campaigns", …), every mention of email is a
  sales channel, in a list of channels ("outbound sequences (calls, email,
  and social)") or before a channel word ("email sequences", "cold email
  outreach"), and the question does not also ask how to reach someone
  ("What's your phone, email, LinkedIn?" stays blocked). Those are answered
  from the screening bank or drafted as usual. A field name that lists
  contact channels ("Email / Phone", "Phone or email", "LinkedIn or email")
  is always a contact detail.
- **Your record and status** are never drafted, because any answer, a plain
  "No." included, states a fact the corpus does not hold. A question the
  bank does not answer is left blank and flagged as
  `never-draft:employment-record` (terminated, fired, let go, asked to
  resign, a PIP or performance improvement plan, disciplinary action),
  `never-draft:criminal-record` (convictions, felonies, misdemeanors, a
  criminal record, pending charges), `never-draft:restrictive-covenant`
  (non-compete, non-solicitation, restrictive covenants) or
  `never-draft:work-status` (visa sponsorship, work authorization,
  citizenship, green card, H-1B). `never_draft` does not catch sales words
  ("forecasting discipline", "sold with conviction"), but see the
  sponsorship rule below for "executive sponsor" and Visa. The bank's own
  `sponsorship`, `non_compete` and `work_auth` entries still answer the
  wordings they match, and an answer you give through `answer_novel` is
  filled. Those two entries answer with opposite polarity, so neither
  answers the other's question, nor a label that asks both ("authorized to
  work … and will you require sponsorship?"), unless one of its aliases is
  the label word for word: the field is left blank as
  `work-status-needs-human:<why>`. On 2026-10-12 a Greenhouse form's "Do you
  now, or will you ever, require employment sponsorship …?" took `work_auth`'s "Yes" by
  embedding similarity; add that wording to `sponsorship`'s aliases to have
  it filled. A match by embedding similarity (not by an alias) is used only
  when the label is recognised as asking that entry's own question: "need
  U.S. sponsorship", "require a visa", "need a work visa" or "sponsor an
  immigration case" for `sponsorship`; "authorized/eligible to work",
  "legally able to work", "can you legally work", "right to work", or "able
  to work" / "can you work" followed by a country ("in the U.S.", "in the
  country where this job is located") for `work_auth`. "Able to work in our
  NYC office", "in US time zones" and "can you work weekends" are not
  work-status questions. A label that asks the sponsorship question in
  reverse ("Can you work in the US without visa sponsorship?", "…with no
  need for sponsorship?") is answered by neither entry: `sponsorship`'s "No"
  would say you cannot work without it. It is left blank as
  `work-status-needs-human:asks-without-sponsorship` (or
  `…asks-authorization-and-sponsorship` when it also asks about working
  there), unless an alias is the label word for word ("without executive
  sponsorship" is sales vocabulary and not caught). Any other wording ("Are you a U.S. citizen?", "Visa status")
  is left blank as `work-status-needs-human:embedding-not-an-authorization-question`
  or `…embedding-not-a-sponsorship-question`. No other bank entry answers a
  sponsorship or authorization question ("Are you at least 18 and will you
  require sponsorship?" does not take `age_18`'s "Yes"), and none answers a
  work-status question by embedding ("Are you a U.S. citizen?" does not take
  `based_in_us`'s "Yes"): `work-status-needs-human:work-status-question-took-<entry>`.
  Add the exact wording as an alias to have such a field filled. Staging
  applies the same rule, so a "No." drafted before this rule existed is not
  filled.

  **Sponsorship rule (2026-10-15; a sponsorship question had been staged
  with `work_auth`'s "Yes").** Any label that mentions
  sponsor/sponsorship, a visa, an H-1B or immigration is answered only by the
  bank's `sponsorship` entry, and only when the label is recognised as asking
  whether you need sponsorship (`work_status_refusal` in
  `autofill/answer_bank.py`), or by a bank entry one of whose aliases IS the
  label word for word. Anything else (another entry's alias inside the label,
  an embedding match, an identity or EEO value, the drafter) leaves it blank,
  flagged `sponsorship-needs-human:<why>`. `work_auth` never answers a label
  asking about sponsorship or a visa, and `sponsorship` never answers one
  asking whether you are authorized to work, not even through an alias that
  is the label word for word (`…:sponsorship-question-took-work_auth`,
  `…:authorization-question-took-sponsorship`). This catches sales
  vocabulary and the company Visa too ("Do your deals require executive
  sponsorship?", "Why Visa?"): those go to you rather than to the drafter. A
  blank work-status field you answer through `answer_novel` is filled.
  Staging re-matches every stored field, so a form spec captured before this
  rule (such as a stored `work_auth` verdict on a sponsorship question) is
  blanked at staging too.
  **Review 2026-10-15.** A label asking whether you *require* work
  authorization or a work permit ("Do you require work authorization to be
  employed in the United States?", "Will you require a work permit …?", "Is
  a work permit required …?") asks the sponsorship question in other words:
  `work_auth` never answers it (`…:requires-authorization-question-took-work_auth`),
  and `sponsorship` answers it only by an alias that is the label word for
  word, or when the label also asks about sponsorship
  (`…:asks-whether-work-authorization-is-required`). A label with a negation
  within three words of its work-status words ("Can you confirm you will not
  require sponsorship?", "…you won't need visa sponsorship?", "We do not
  sponsor visas. Will you …?") is answered by neither entry unless an alias
  is the label word for word (`…:negated-work-status-question`), and a match
  by embedding similarity does not answer a work-status label with a
  negation anywhere in it (`…:embedding-negated-work-status-question`). With
  the real embedder those four wordings had matched the wrong entry at
  0.877–0.934.
- **Links.** GitHub, portfolio and Twitter fields use their own
  `identity.github`, `identity.portfolio` and `identity.twitter` keys in
  `data/answer_bank.yaml`. An unset key leaves the field blank and flagged
  (`not-in-answer-bank:github`); the LinkedIn URL is never used as a
  substitute. "Other website" is always left to you.
- **Salary expectations.** The bank's one `comp_expectation` figure is never
  used for a job whose role track has a floor below the default
  `filters.comp.floor` (often an SDR or AE track), or when the posting's own
  maximum is below it. Those
  fields are left blank and flagged. Salary-history questions are always left
  blank and flagged.
- **Your answers to open questions.** An answer you give through MCP
  `answer_novel(n, field_identity, answer)` is filled at staging and marked
  `human_typed`, including on fields the AI never drafts (a referrer's email,
  a salary expectation for a low-floor track) and over a bank value for that
  field. `answer_novel` refuses EEO/demographic fields (only the `eeo` block
  in `data/answer_bank.yaml` fills those) and compensation-history questions
  (never answered), and it names the form's fields when the identity is
  unknown. `get_novel_questions` lists your answers separately from the AI
  drafts. Staging tells the two apart by the `novel_answered` event, so an
  answer written straight into `form_specs.drafted_answers` is treated as an
  AI draft. When a rule keeps a stored answer off the form, the run's
  unfilled entry says so (`"withheld": "ai_drafted"` or `"human_typed"`) and
  the log records `stage.answer_withheld`.
- **Fill clicks never land on a send control.** Besides the static scan of
  `src/jobkit/autofill/`, every click or check the filler makes (dropdowns,
  options, radios, checkboxes, location suggestions, Ashby Yes/No buttons)
  is preceded by an in-page probe of that element. It is refused when it is
  a submit/image control, sits inside one, is a `<button>` with no type inside the form
  (which would send it), or has submit/apply/send in its own text. A refusal
  is logged as `stage.click_guard_refused` and the field goes to you as
  `fill-failed`; nothing is clicked. A test (`tests/test_click_guard.py`)
  reads the code and fails if any click, check or JS click in
  `src/jobkit/autofill/` is not preceded by this probe on the same element
  in the same function, so a click added later without it cannot ship.
- **CAPTCHA.** Staging stops only for a challenge that is actually shown: a
  visible reCAPTCHA checkbox or challenge frame, hCaptcha, or Turnstile. The
  invisible-reCAPTCHA badge in the corner does not stop it. The widget that
  triggered a stop is logged as `stage.captcha_detected`. Nothing ever
  interacts with a challenge.
- **A CAPTCHA the fill brings up.** Some forms show a challenge only after a
  scripted click (Lever shows hCaptcha after checkbox and radio clicks). So
  the check runs again after every click the filler makes (checkbox, radio,
  Yes/No button, dropdown, option, location suggestion), after any fill that
  failed with an error (Playwright's check() clicks first and then fails
  with "Clicking the checkbox did not change its state" when a challenge
  took the click, so that click is checked too, before any Yes/No fallback
  click), once before the read-back so no refill click lands on a
  challenge, and once more when the fill is done. If a challenge appears, staging stops at once with
  `captcha-provoked-by-fill:<field>` (the field whose click brought it up, or
  the last field filled when it appeared at the end), takes a screenshot,
  logs `stage.captcha_provoked` with the field's label and the widget, and
  never touches the challenge (hard rule 2). The job is listed with the other
  halts in the digest, `get_queue` and `schedule status`, and the nightly run
  does not retry it.
- **Uploads go first, named for the recruiter.** The resume and cover letter
  are uploaded before anything is typed, as copies named
  `First_Last_Resume.pdf` and `First_Last_Cover_Letter.pdf` from the answer
  bank's `identity.first_name` and `last_name` (accents dropped, other
  characters made `_`). The copies live in `out/uploads/job-<id>/`, because
  the browser reads an uploaded file again when you send the form; the
  tailored `resume.pdf` and `cover.pdf` are never changed. With no first or
  last name in the bank, the original file is uploaded and
  `stage.upload_name_unavailable` is logged. A file goes only into a real
  file input: the one with the ATS's field id (`resume`,
  `_systemfield_resume`), else the one input on the form known as a resume
  (or a cover letter) by its own label, name or id, then by its field's text.
  An employer-hosted copy of a Greenhouse form names and labels its inputs
  its own way (2026-10-12: "Resume", not "Resume/CV"). Two inputs
  claiming the same file get nothing. Every failure is logged as
  `stage.attach_failed` and recorded in the run's unfilled entry with its
  cause (`no-file`, `no-file-input-for-resume`,
  `2-file-inputs-claim-resume`, `upload-failed:…`). Greenhouse's "or paste
  it" box (`resume_text`, `cover_letter_text`) is never typed into; it
  counts as answered once its file is uploaded.
- **Waiting for the resume parser.** Some ATSs read the uploaded resume and
  write their own reading into name, email and phone. After an upload,
  staging waits until the form's values stop changing (1.5 s with no change
  after a change, or 3 s with none at all), at most 8 s, then fills. Logged
  as `stage.parser_wait`.
- **Read-back.** After the fill, every text, select, radio, checkbox,
  dropdown, location and Yes/No value is read back from the page and
  compared with what was put in. Reformatting the page does on its own does
  not count: case and spacing, phone punctuation and country code, a URL's
  `https://`, `www.` and trailing slash. A field showing something else is
  filled once more; if it still differs it leaves the filled list as
  `overwritten-after-fill` (with what was filled and what the form shows),
  is outlined red, and the run is `needs_human` whether or not the field is
  required. A widget that cannot be read, or shows nothing at all, is
  counted as unverified, not as a mismatch. A custom dropdown or location
  box is read from what the widget itself shows as its value (its input's
  value, the element showing the chosen option, a button-style control's own
  text), never from the text around it, and the value must BE the answer (a
  whole line of it), not contain it: the old read-back took the question's
  label as the value, so a cleared "No" passed because the label said "now"
  (review 2026-10-12). A placeholder shown in place of a value is a mismatch;
  a widget that shows no value at all is unverified. Each run writes a `readback`
  event (the `autofill_runs` id, fields checked, changed, put right by the
  refill, still changed, unverified); `jobkit stage` prints the count, and
  MCP `get_job` shows the latest run and any field the form changed.
- **The right box.** When a field has no id or name staging can use, its
  control is found from its label in the page (`question_control` in
  `autofill/browser.py`, over a read-only tree of the page): the control a
  `<label for>` names or a `<label>` wraps, the control whose
  `aria-labelledby` names the label, or else the nearest element above the
  label text that holds a control, which must hold ONE question's controls
  (one control, or one radio or checkbox group). A wrapper holding two
  questions' controls is never narrowed to its first one, a control with its
  own label of another text is another question's, and a label found in two
  places leading to two controls is ambiguous: the field is left blank as
  `control-ambiguous:<why>` (log `stage.control_ambiguous`). The nearest
  element holding a control must also hold nothing else of another question:
  no shown text with letters outside the label text, the control and its own
  labels, and no other widget (a button, a `role=button` dropdown, an
  editable box). Otherwise the control may be a sibling question's while the
  asked question's own widget is one staging cannot fill (review 2026-10-15:
  Yes/No buttons beside an unlabelled LinkedIn box handed back the LinkedIn
  box): `control-ambiguous:container-holds-several-questions`. And a yes/no
  or pick-one answer (a boolean or select field, or one with options) is
  never typed into a text box (log `stage.choice_not_typed`): a boolean goes
  on to the Yes/No buttons, anything else is left blank. An outer wrapper
  used to win: on one employer's form the Race/Ethnicity answer was typed into
  Location, and with unnamed controls in a shared "form-fields" wrapper it
  still could (review 2026-10-12). A label lookup that matches several
  controls of more than one question ("Location" inside "Preferred work
  location") is decided by the exact label, or refused the same way.
  The Ashby Yes/No buttons are looked up the same way, and a Yes/No answer
  is clicked only when that container holds exactly one button (or radio)
  with the answer's name, and an in-page check finds it and the opposite
  answer form one question's pair: the nearest element holding both must
  lead to the label without passing another question's text, the question
  may hold no other Yes/No pair and no other shown field. A whole-form
  wrapper whose class says "field" holding several questions' buttons used
  to get its first "No" clicked for another question. Anything less clear
  clicks nothing, logs `stage.yes_no_unclear` with the reason, and the
  field goes to you as `fill-failed`.
- **No form, no run.** Staging waits up to 15 seconds for the form, including
  a form inside an embedded iframe (a Greenhouse board inside an employer's
  careers page). If none appears, the job stops as `posting-not-found`, with
  the signal (`http-404`, `page-says:…`, `no-captured-field-rendered`) and the
  final URL in the log. An Ashby posting URL is sent to its `/application`
  tab.
- **Dead postings are closed before tailoring.** `jobkit form-specs` (and the
  nightly `form-specs` stages, MCP `run_nightly`) fetches each queued or
  approved job's form from its ATS. When the ATS says that posting does not
  exist (Greenhouse's per-job API answers 404 or 410; Lever's apply page
  fails and its postings API answers 404 or `{"ok":false,"error":"Document
  not found"}`; Ashby's GraphQL answers `jobPosting: null` AND the board's
  public job list, `api.ashbyhq.com/posting-api/job-board/<token>`, answers
  without the posting), the job is closed
  (status `closed`, `closed_at`, event `posting_gone` with the signal, log
  `form_spec.posting_gone`) and gets no form spec, so it is never tailored
  or staged. Any other failure (5xx, 403, a timeout, a page that will not
  parse) keeps the old behaviour: a `dom_scrape` placeholder, and the job
  goes on. Ashby's form endpoint is one GraphQL POST shared by every
  posting, so its HTTP status (404 and 410 included) never closes a job: it
  would say the endpoint moved, not that the posting is gone (review
  2026-10-12). `jobkit form-specs` prints `posting_gone=N`; MCP `get_job` says
  why a job closed. Greenhouse's `location_questions` (a required Location
  box some employers add) are captured too, without their hidden
  latitude/longitude fields.
- **More than 8 jobs.** At most 8 tabs are open at once. A longer list runs in
  batches of 8. With `--keep-open` (and `open-queue`), the next batch opens
  after you close every tab of the current one. Closing the tabs ends the
  wait, so the browser releases its profile; Cmd+Q is no longer needed. A job
  that cannot be staged is returned as `deferred` with a reason, for example
  when another browser still holds the profile (the reason names the pid to
  quit).

### Operations: freshness, run-all, follow-ups, background runs

- **Freshness banner.** The digest, `jobkit stats` and MCP `get_queue` and
  `get_stats` open with "last successful poll N days ago" (from
  `boards.last_polled_at`; a 304 counts as success). Past
  `schedule.stale_after_hours` in `config/filters.yaml` (36) it is a
  `⚠ STALE DATA` warning, as it is when no poll ever succeeded. The next line
  is the last successful nightly run (`⚠ STALE NIGHTLY` past the same
  threshold; "No nightly run on record" when there has never been one), then
  a nightly run that started and has not finished (still running, or `⚠` when
  its process is gone). When the last run (run-all or nightly) had failed
  stages, a final line names them.
- **`run-all`.** Each stage (poll, feeds, linkedin-guest, discovery, resolve-boards, filter, extract, remote,
  comp, score, unsnooze, queue, form-specs, rules, form-specs-approved,
  tailor, followups, digest) runs isolated: an exception is caught, that
  stage's uncommitted writes are rolled back, and the rest still run, so the
  digest is always written. A stage that returned but saw every one of its
  LLM calls fail counts as failed (`llm_all_failed`). A `run_all.start`
  event (run id, mode, pid, stage names) opens the run; each stage writes a
  `run_all.stage` log line and a `run_all.stage` event (run id, mode, status,
  seconds, LLM calls answered/failed, error); a `run_all.done` event (run id,
  mode, failed stages) closes it. `mode` is `run-all` or `nightly`. The
  command exits 1 when any stage failed. `followups` is now a run-all stage
  too.
- **Follow-ups.** The schedule is `followups:` in `config/filters.yaml`
  (+7d `status_check`, +14d `hiring_manager_note` with
  `only_if_contact_known`, +30d `close_as_ghosted`). A hiring-manager note is
  scheduled only when a `contacts` row exists for the company; a pending one
  whose company has none is marked `skipped` by `jobkit followups` instead of
  being drafted. One failed draft no longer stops the others.
  - The digest's "Follow-ups due" section (after the review queue) lists
    every pending follow-up due today or earlier with its id, kind, due
    date, company and job, and whether its draft is ready, so the
    scheduled run's summary reports them. Sending stays yours.
  - MCP `mark_submitted(ids="", job_ids="", submitted_on="")`: `job_ids` are
    raw job ids, so a job applied to by hand or no longer in the queue can be
    marked (pinned numbers in `ids` still work). `submitted_on`
    (`YYYY-MM-DD`) back-dates the submission and its follow-ups. A job
    already marked submitted is left unchanged.
  - MCP `get_followups` lists due follow-ups with their drafts. When any is
    undrafted (or a ghost-close is due) it starts `jobkit followups` in the
    background and says so; call it again for the drafts. MCP
    `run_followups` starts that run directly. Sending is always manual; MCP
    `mark_followup_sent("12, 13")` takes sent ones off the list.
  - MCP `record_outcome(job_id, outcome, note)` with outcome `reply`,
    `rejection`, `interview` or `offer` writes an `outcome` event with the
    note, moves the application stage (`replied`, `rejected`, `interview`,
    `offer`; a later reply never undoes a stronger outcome) and skips the
    application's pending follow-ups. The job must be marked submitted first.
- **Background launches.** MCP `approve`, `retailor`, `stage_forms`,
  `restage`, `open_queue`, `get_followups`/`run_followups` and `run_nightly`
  start `python -m jobkit.cli …` with the server's own interpreter and source
  (not `uv run --no-sync`), in the project root. Each launch's output goes to
  `logs/spawn/<timestamp>-<command>.log` and is recorded as a `spawn` event
  (args, pid, log) and a `mcp.spawn` log line; the tool's reply names the pid
  and the log. MCP `background_status(limit=8)` shows each recent launch as
  running, finished ok, or FAILED with its exit code (recorded once as a
  `spawn_exit` event), plus the last lines of its output.
- **Staging is one process.** `stage_forms(ids="", job_ids="")` runs all the
  jobs in one `jobkit stage --job-ids … --keep-open` process (at most 8 tabs at
  a time; see Staging above). The browser profile admits one process, so
  `stage_forms`, `restage` and `open_queue` refuse to start while a staging
  launch is running or a browser holds the profile, and say which pid.
- **`open-queue`** (CLI and MCP) opens jobs whose latest staging run is
  `staged`, or `needs_human` with at least one field filled — fully staged
  first, then cheapest friction — skipping jobs already submitted and runs
  that halted before filling anything. MCP `open_queue` lists the jobs it
  opens, or says there is nothing to open.
- **ATS rows win over feed rows** (hard rule 10). When an ATS board is polled,
  an open aggregator row whose apply URL names the same posting (ATS, board
  token and job id, or a Greenhouse `gh_jid` link) is merged into the ATS row:
  its sources move over as non-canonical, the feed row is closed (not
  deleted), and `merged_into` / `merged_from` events record the pair. A
  human's application or decision on the feed row moves to the ATS row, which
  takes the feed row's approved / applied / rejected / snoozed status, but
  only when the ATS row has no application or decision of its own. When both
  rows carry state, nothing is merged and a `merge_held` event lists the pair
  for you. `jobkit merge-feed-dupes [--dry-run]` (MCP
  `merge_feed_dupes(confirm=false)`, a dry run until `confirm=true`) applies
  the same merge once to the rows already stored.
- **One application row per submitted job** (`track/applications.py`).
  A job marked submitted on an aggregator row could look unsubmitted
  (2026-10-15): the next nightly poll merged the row into the employer's
  posting, where its application row (stage `submitted`), follow-ups and
  decision now are, so a LEFT JOIN from the aggregator row's id found no
  application. Nothing was lost.
  `mark_submitted` now always leaves exactly one application row at
  `submitted` with its follow-ups: several rows on a job are folded into one
  (the first submission, else the newest; follow-ups and artifact paths
  kept, an `applications_consolidated` event records what was removed), a
  missing row is created, and an already-submitted row is left as it was
  (follow-ups added only if it has none). Re-tailoring a submitted job never
  sets its row back to `tailored` (a `retailored_after_submit` event says it
  ran), a standing-rule auto-approve never adds a second row, and
  `record_outcome` follows a merged aggregator id to the employer's posting.
  `jobkit repair-applications [--dry-run]` (MCP
  `repair_applications(confirm=false)`, a dry run until `confirm=true`) fixes
  rows left inconsistent before: a `marked_submitted` event with no submitted
  row (recorded on that day, with follow-ups), several rows on one job, a
  submitted row whose stage went back, a submitted row with no follow-ups, a
  submitted job still listed as approved, an application left on a merged
  aggregator row. A job `applied` with no record of the day is listed for
  you, never invented. Each fix writes an `application_repaired` event.

### LinkedIn guest discovery (off by default)

`jobkit linkedin-discover [--dry-run]` (MCP `linkedin_discover(confirm=false)`)
uses LinkedIn's logged-out job search to find postings, for discovery only.
It is the one exception to hard rule 11 (CLAUDE.md, 2026-10-12), and it is
still against LinkedIn's User Agreement. The full account, including what was
verified live, is `docs/why-no-linkedin.md`.

- **Off until you turn it on:** `feeds.linkedin_guest.enabled: true` in
  `config/filters.yaml`. While it is off, the nightly stage `linkedin-guest`
  does nothing and `linkedin_discover(confirm=true)` refuses to start.
- **One run at a time:** a run holds an exclusive lock on
  `data/jobkit.db.linkedin_guest.lock` (next to the database) for its whole
  length. A second run started meanwhile (`jobkit linkedin-discover` during
  the nightly's `linkedin-guest` stage, say) sends nothing and reports
  `busy`. `linkedin_discover(confirm=true)` also refuses while a nightly or a
  `run-all` is in progress, since both include the stage. The 3–6 s delay
  holds across runs: a run's first request waits out what is left of it since
  the last request of any run.
- **What it sends:** plain HTTP GETs to
  `www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search` (no browser,
  never logged in, no cookies, no proxies), with the User-Agent
  `jobkit/0.1 (personal job-search tool; logged-out job discovery, a few
  requests a day; contact: …)`. The contact is `JOBKIT_CONTACT_EMAIL` (`.env`),
  else the answer bank's `identity.email`; with neither, nothing is sent.
  LinkedIn sees that address, so set `JOBKIT_CONTACT_EMAIL` to something other
  than your LinkedIn login if you want them kept apart.
- **Limits (filters.yaml, one owner):** up to 4 `keywords` × `locations`
  (`workplace`: remote, hybrid, onsite or any), `posted_within_days` (7), at
  most `max_search_requests_per_day` (6) searches per local calendar day,
  `max_detail_requests_per_day` posting pages (0: the logged-out posting page no
  longer contains the employer's apply link), and `min_delay_seconds` (3,
  randomized up to 6) between requests. 6, 40 and 3 are hard limits in
  `config.py`: a config value past them fails at load. Requests are counted
  as `linkedin_guest.request` events when sent, so the caps apply across runs.
  With more keyword × location pairs than searches, the pairs rotate by day.
- **Stops on any block:** HTTP 429, 999, 403 or 401, any redirect (authwall,
  login, checkpoint), a CAPTCHA or challenge page at any status (including
  Cloudflare's "Just a moment..." check, usually HTTP 503), or a search
  page with no job card that is not the empty no-results page ends the run,
  records a
  `linkedin_guest.blocked` event, and LinkedIn is skipped until the next local
  calendar day. The day's block is read again before every request, so a
  block recorded by any run stops the others too. A block is not a stage
  failure; the run never retries around it and never solves a CAPTCHA. A
  network error or 5xx ends the run without closing the day
  (`jobkit linkedin-discover` exits 1).
- **What happens to each card:** cards seen in the last 14 days are skipped; titles
  go through the filter's blocklist, exclude, include and paused-track rules
  (rejections are logged and recorded as `linkedin_guest/<id>`); a card whose
  company's board jobkit already polls is skipped. Every other card's company
  name goes to `discovery_queue` (source `linkedin_guest`, no URL). The
  `discovery` stage, which runs right after, looks the name up in the board
  registry and adds the company's Greenhouse, Lever or Ashby board; the next
  poll reads it, and the employer's postings go through filter and triage as
  usual. The `resolve-boards` stage that follows also looks for the card's own
  posting (same title and an agreeing location) on the company's board, slug
  probe included, and ingests it when exactly one fits (see "Aggregator and
  LinkedIn jobs → the employer's own board"). The LinkedIn run itself never adds or polls a board on a company-name
  match (hard rule 11's exception: only an apply link that resolves to the
  employer's ATS brings a posting in). That direct path exists for when a
  posting page carries the apply link (`max_detail_requests_per_day` > 0 and
  LinkedIn showing the link to logged-out visitors again): the board is then
  polled at once through the `add-url` path (`discovery/ingest.py`), and the
  card counts as "new via ATS". No job row, source or queue entry ever holds a
  LinkedIn URL.
- **What you see:** `--dry-run` (and `linkedin_discover()` without
  `confirm=true`) prints today's plan, budget used, block state and whether a
  run is in progress, without contacting LinkedIn. Each run prints, and records as a `linkedin_guest.run`
  event, what each card became; `linkedin_guest.seen` events keep the
  per-card outcome (with the ATS job id when one matched). The digest's
  "LinkedIn guest discovery" line sums the day: searched, new via ATS,
  companies queued, blocked.

### Aggregator and LinkedIn jobs → the employer's own board

`jobkit resolve-boards [--job-id N] [--dry-run]` (MCP `resolve_boards(confirm=false)`;
the nightly / run-all stage `resolve-boards`, after `discovery` and before
`filter`) matches each aggregator job (Adzuna, Himalayas, ...) to the
employer's own Greenhouse, Lever or Ashby posting, so the employer's JD is the
one triaged and the employer's form the one staged. Adzuna's apply page blocks
automated browsers, and an aggregator row is never canonical (hard rule 10).
It replaces looking each one up by hand, which goes wrong when a board holds
the same title in two cities or uses a token that is not the company's name.
`discovery/board_resolver.py`.

- **Which jobs:** open aggregator rows that passed the filter (or, while
  still `new`, would pass it), approved ones first; and LinkedIn cards from the
  last 14 days whose company went to the discovery queue (only the company,
  title and location from the card; no LinkedIn row or URL is ever stored).
  At approve time again: MCP `approve` starts `jobkit tailor`, which first
  resolves approved aggregator jobs (and `tailor --job-id N` tailors the ATS row
  when job N was merged into it, then or earlier). After such a merge the queue
  number you approved keeps working: `get_job`, `get_artifacts`, `retailor`,
  `review_artifacts`, `restage`, `stage_forms` and `mark_submitted` follow the
  merged aggregator row (queue number or job id) to the employer's posting,
  which carries your application now.
- **Finding the board, per company:** the boards jobkit already has, the
  board the apply URL names, and the board registry under the name or its
  domain label ("Soylent.ai" -> `soylent`). Only two are trusted to be the
  company's: a board you listed in `config/targets.yaml` (read from the DB,
  polled first when not polled in the last day) and the board the apply URL
  names, the latter only for the job whose own apply URL names it: for any
  other job of the same company it must be confirmed like a probe hit (review
  2026-10-15: one row's Greenhouse URL for `lumonhealth`, "Lumon Health Inc",
  merged another "Lumon" row there). A registry board, or a board jobkit holds that the registry,
  discovery, `add-url` or an earlier match added, is read from its list API
  and must be confirmed like a probe hit (below): the registry is keyed by
  normalized name only, so "Initrode" reached Initrode Genomics' `initrode83`
  board and moved an approval there (review 2026-10-12). Only when none of those finds a board at all, a slug probe of the
  public list APIs: the name (spaces dropped, hyphenated, underscored) and its
  domain label, and only if those find nothing, with the suffixes and prefixes
  careers, -careers, jobs, -jobs, hq, -hq, inc, io, ai, app, co, the
  (`cyberdyne-careers`). A 404 means no such board. A company whose own board
  jobkit knows is never probed: a job missing from that board (closed, or
  retitled by the aggregator) is "no matching title", because the probe could
  only reach other companies' boards (`tyrellhq`, `thetyrell` for Tyrell); a known board
  that cannot be polled is an error retried next run. When the apply URL
  already names the posting (a Greenhouse/Lever/Ashby link), the `add-url` path
  is used and no probe is made.
- **A board found by name (probe, registry, or held without being in
  `targets.yaml`) is only a board under the company's name**, and
  another company can own that token. Its posting is picked only when
  something on the board confirms the company: Greenhouse reports the board's
  company name and it is the same name ("Tyrell" = "Tyrell"; "Ember" is not "Ember
  Grill"), or the board links to a domain of the company (a posting or apply
  URL, a link with a scheme or "www.", an e-mail address; a domain written
  as a word in a description, "we live in Salesforce and Hyperion.io", is not
  evidence): the company row's domain, the company
  name when it is a domain ("Soylent.ai"), or the apply URL's host when its
  name spells the company's (`ember.ai` or `getember.com` for Ember, not
  `emberfleet.com`). A shared host never counts, because another company's
  board can link its own page there: notion.site, github.com and github.io,
  google.com and forms.gle, airtable.com, typeform.com, tally.so, gem.com,
  payroll and HR suites such as ADP, Paylocity, UltiPro, Oracle Cloud and
  Taleo, and every aggregator, job site and ATS host (`SHARED_HOSTS` and
  `JOB_SITE_LABELS` in `board_resolver.py`). Lever
  and Ashby report no company name, so for them only a domain confirms, and a
  description that merely names the company does not (another "Ember" names
  itself too). Unconfirmed, the posting is flagged **board-match-unconfirmed**
  with its candidates instead: nothing is ingested, no boards row is added and
  your approval stays where it is until you pick. A Greenhouse board whose own
  company name does not fit at all ("Tyrellium Corp" for Tyrell) is ignored.
- **Picking the posting:** the same title (case, punctuation and "&"/"and"
  aside; never the normalized title, which drops qualifiers) AND a location
  that agrees: the same metro, the same city in the same state or country,
  the same state, or both US-wide remote ("Remote - US", or "United States"
  with the remote flag). The US named as a country with no remote signal
  (Adzuna's bare "US" with remote unknown) agrees only with another such
  location, never with "Remote - US" or a metro: with the same title in New
  York and Remote-US it used to pick the remote posting (review 2026-10-12).
  "Americas", "North America" and "AMER" are not the US, and a blank or
  work-mode-only location ("Hybrid") agrees with nothing. Places
  are read as whole words, not with the dedup key's substring table, and a
  state or country the text names must fit the city: "Redmond, Washington" is
  not Washington DC, "Cambridge, UK" is not Boston, "London, Ontario" is not
  London, "Mansfield, TX" is not San Francisco, and "Washington" or
  "Cambridge" with no state is neither metro. A city named with its state
  agrees only with the same city and state ("Round Rock" alone and "Round
  Rock, TX" do not agree: flagged). A bare "Remote" beside a place takes
  that place's scope: "London / Remote" and "Berlin | Remote" are not
  US-wide; bare "Remote" on its own still is. Exactly one such posting: it is ingested
  (`discovery/ingest.py`: the board is added if new and polled now) and the
  feed row is merged into it with the existing merge (sources move over, the
  feed row is closed, your approval or snooze moves to the ATS row unless that
  row has decisions of its own, in which case the pair is held for you). An
  ATS row that took your approval is extracted and scored at once. A board
  poll that merges the feed row itself (a `gh_jid` apply URL naming a posting
  on that board) counts as the same merge. A title alone never picks: when the
  title matches but none or several postings agree on location, or the one
  that agrees is on an unconfirmed board, nothing is picked and the job
  is flagged **board-match-ambiguous** (or **board-match-unconfirmed**). The
  digest, `get_queue`, `get_job` and
  `resolve_boards()` list each flag with its candidate postings; pick one with
  `jobkit resolve-boards --job-id N --pick <posting id or URL>` (MCP
  `resolve_boards(job_id=N, pick=...)`). A flag clears when a later run finds
  the title gone. A LinkedIn card's flag is listed for 7 days; add the right
  posting with `add_job_url(url)`.
- **Requests and the cache (`board_resolution` in `config/filters.yaml`):**
  only the public board list APIs and ingest's poll of a matched board, with
  jobkit's honest User-Agent, one at a time, `delay_seconds` (1, the floor)
  apart, at most `max_requests_per_run` (300; ceiling 1000) per run. A run
  commits what it has written before each request, so it never holds the
  database's write lock while it waits (MCP `get_queue` and `approve` and a
  background `tailor` write to the same database). HTTP 429
  stops the run's probing; other failures leave the company for the next run.
  A company is slug-probed at most once per `probe_cache_days` (14): the
  result is a `board_probe` event; within the window only the boards it found
  are read again (and still need confirming). A confirmed board whose posting
  matched becomes a boards row (confidence `probe`) polled every night; a
  probe that found no board marks the company `not_found` in
  `discovery_queue`. `--dry-run` reads the board
  APIs and writes nothing; `resolve_boards()` without `confirm=true` reads only
  the DB. `enabled: false` turns it off everywhere (except `--pick`).
- **Not done:** the careers-page crawl of SPEC §2.4 does not exist in jobkit; a
  known company domain only contributes its label as a slug.

`jobkit add-url` uses the same `discovery/ingest.py` path. It now polls only
the board the URL names (it used to run the full poll), and it no longer resets
an existing company's tier to 3 when the URL names a board jobkit already
knows. A board that cannot be fetched makes it exit 1.

### LLM backend

All model calls go through `src/jobkit/llm.py`, which is the only place models
are chosen. Extract, remote, comp and score use `claude-haiku-5-5`. Tailor,
cover letters, follow-ups, novel answers and the self-review of those drafts
use `claude-opus-5-5`, and so does the claim judge that checks every letter
and drafted answer. The authenticity critic uses `claude-sonnet-5-5`.

- **With `ANTHROPIC_API_KEY` set:** the anthropic SDK is used, with full model
  ids and `max_tokens=8000`.
- **Otherwise:** the `claude` CLI (Claude Code, logged in) is used with the tier
  aliases `haiku`, `opus` and `sonnet`. Each call runs isolated, so it does not
  pick up your CLAUDE.md, MCP servers, skills or tools: it runs in a fresh temp
  directory with `--strict-mcp-config` and an empty `--mcp-config`,
  `--tools ""`, `--safe-mode`, `--no-session-persistence` and a short
  `--system-prompt`. The `claude` binary must be on `PATH`, including under
  launchd.

Both backends send the same system prompt, read from `prompts/system.md`
(versioned in frontmatter like every other prompt).

The LLM cache key is a sha256 of the stage, the resolved model id, the
caller's key, the system prompt and the full rendered prompt. A corpus edit, a
prompt edit or a model change therefore always misses the cache. A truncated,
refused or error reply is never cached.

### Nightly run and schedule

- **`jobkit nightly`** (MCP `run_nightly`) is the one unattended run a night:
  every run-all stage (poll, feeds, discovery, filter, extract, remote, comp,
  score, unsnooze, queue, form-specs, rules, form-specs-approved, tailor,
  followups), then `stage`, then `digest` last so it reports the staging.
  - `stage` stages every approved job whose newest application is
    `tailored` (resume written), not submitted, still open, and not staged
    since it was last tailored, so a form staged on an earlier night is not
    reopened every night. All of them run in this one process through one
    headed browser at a time (at most 8 tabs per batch); each batch's browser
    is closed when its forms are filled, which releases the profile lock for
    the morning. No answer is carried forward from one job to another, since
    nobody is in the session. A job left `needs_human` (CAPTCHA, a required
    question) is a normal outcome. A job `deferred` (another browser holds
    the profile) or `failed` fails the stage, with the reasons. In the
    morning, `open_queue` reopens the staged forms for your review.
  - **A job whose staging keeps failing is capped.** A `failed` job (the
    page raised: a dead apply URL, `net::ERR_NAME_NOT_RESOLVED`, a page that
    never loads) writes no staging run, so the next night tries it again.
    Each failure is recorded as a `nightly.stage_failed` event. The failure
    that makes 2 since the job was last tailored hands the job to you
    instead of failing the stage ("Needs you: job 7 Acme: staging failed 2
    times, not retried: …"), and later nights stop opening a tab on it, so
    one broken posting cannot fail every nightly run and freeze "last
    successful nightly run". It is listed with the error like a halt (below)
    until you submit it, it is rejected, closed or re-tailored (the count
    starts over), or a staging run you start (`jobkit stage --job-id <id>`,
    MCP `stage_forms`) gets a result for it. `deferred` is transient and
    never capped.
  - **Halts reach you** (hard rule 2: detect, abort, hand to the human).
    Nobody watches this stage, so the stage summary names every
    `needs_human` job with its company and reason ("Needs you: job 7 Acme:
    captcha-or-turnstile-visible"). A job whose latest staging run (nightly
    or yours) stopped before filling anything (a CAPTCHA or bot check, a
    government-ID or payment field, an embedded assessment, no form on the
    page, no form spec, an apply URL off the allowlist), or whose nightly
    staging failed twice (`staging-failed:<error>`), cannot be reopened
    by `open_queue`. It is listed with its reason, screenshot and apply URL
    in the digest's Staging section, in a `⚠` line under the banner of MCP
    `get_queue`, and in `jobkit schedule status` (MCP `schedule_status`),
    until you submit it (MCP `mark_submitted`), it is rejected or closed, or
    a later staging run gets past the halt. The digest's Staging section
    also counts the filled forms waiting in `open-queue`.
  - Two halts that can be transient are retried on a later night:
    `no-form-spec-captured` once a form spec with fields exists, and
    `posting-not-found` (a slow page) once more, twice in all per tailoring.
    A CAPTCHA, a government-ID or payment field, an assessment and an apply
    URL off the allowlist are never retried automatically.
  - **Nothing is ever submitted.** There is no submit stage (hard rule 1,
    SPEC §8). You click submit.
  - The run is recorded with mode `nightly` (events above). It exits 1 when
    any stage failed, and refuses to start (exit 1) while another nightly run
    is still running. MCP `run_nightly` starts it in the background, refuses
    while one is running, and warns when a browser holds the staging profile.
- **`jobkit schedule install|uninstall|status`** (MCP `schedule_install`,
  `schedule_uninstall`, `schedule_status`) manage one LaunchAgent,
  `com.jobkit.nightly`, in `~/Library/LaunchAgents`. macOS only: elsewhere
  each command says scheduling is not supported yet (run `jobkit nightly`
  from cron or a systemd timer yourself).
  - The agent runs `<uv> run --directory <project root> jobkit nightly` at
    `schedule.nightly_at` (`config/filters.yaml`, quoted `"HH:MM"` local
    time, default `"02:00"`). `uv` and `claude` are resolved with `which` at
    install time. It sets WorkingDirectory to the project root and PATH to
    their directories plus `/usr/bin:/bin:/usr/sbin:/sbin`, plus
    `JOBKIT_HOME` and `PYTHONPATH=<root>/src`. Output goes to
    `logs/nightly.out.log` and `logs/nightly.err.log`. Change `nightly_at`,
    then install again; `status` flags a mismatch.
  - `install` refuses a checkout under `~/Desktop`, `~/Documents`,
    `~/Downloads` or iCloud Drive, including data, config or logs symlinked
    there: macOS stops launchd jobs from reading those folders. It replaces
    an installed agent and removes the old `com.jobkit.morning` and
    `com.jobkit.followups` agents, then prints the agent and a warning. The
    Mac must be awake at that time: launchd does not wake it. A run missed
    while it sleeps happens at the next wake; one missed while it is shut
    down is skipped. `--dry-run` prints the plist and installs nothing.
  - `status` is read-only. It shows whether the agent is installed and
    loaded (launchd state, runs, last exit code), the latest nightly run
    stage by stage with its exit, the last successful one, and the jobs
    halted in staging that wait on you (reason, screenshot, apply URL).
  - MCP `schedule_install` and `schedule_uninstall` are dry runs until called
    with `confirm=true`. Install only on your explicit yes.
  - `scripts/install_schedule.sh` is retired. It now only runs
    `uv run --directory <repo> jobkit schedule install`, without the old
    `UV_NO_EDITABLE=1` and `--no-sync`, which ran a frozen install that
    ignored source edits.

### Sheets, fixtures and tests

Sheets mirror: not wired (optional per SPEC §10; needs gspread credentials —
SQLite remains the single source of truth either way).

Fixtures for offline tests: `uv run python scripts/record_fixtures.py` (manual,
when ATS shapes change). Tests never hit live APIs.

Tests: `uv run pytest -q` (works from any directory, and on a fresh clone with
no personal config). `tests/conftest.py` runs every test in a throwaway sandbox
under `tmp_path`. Its `config/` holds the shipped templates
(`config/*.example.yaml`) plus the test profile (`tests/fixtures/config/*.yaml`),
never your own `config/*.yaml`. It redirects the DB, log,
`out/`, personal-YAML paths and the LaunchAgents directory into the sandbox
(the schedule tests also fake `launchctl`), and it refuses to open
`data/jobkit.db`, to spawn a process, or to open a network connection. A test
that needs to check a spawn (for example `approve` starting `jobkit tailor`)
requests the `spawned` fixture, which records the command without running it.
A new path constant in `src/` that writes data or reads personal data must be
added to `SANDBOXED_PATHS` in `tests/conftest.py`.

Fresh-clone check: `scripts/fresh_clone_check.sh` clones this checkout's HEAD
into a temporary folder over a `file://` URL and proves what a new user gets:
no personal file in the clone (only the `*.example.yaml` templates in
`config/` and `data/`, no `.env`, `.mcp.json` or database, nothing in
`.claude/` but the skills, no local working note in `docs/`) and the
templates and skills present; then
`uv sync --locked`, `uv run pytest -q`, `uv run jobkit doctor` (must report the
missing setup and exit 1, not crash), `uv run jobkit setup-status` (must put
the resume first), and that those two wrote nothing. It also warns about
personal paths still in git history. `--working-tree` checks the next commit
instead (tracked plus untracked, not-ignored files) and `--keep` keeps the
folder. It never changes this checkout; `uv sync` goes to the network only for
packages missing from uv's cache. Exit 0 means a new user's clone works.

Local working notes (an audit, a review dump, a proposed corpus diff: anything
made from one person's data) go in `docs/local/`, which is gitignored, as are
the dated `docs/audit-*`, `docs/residual-issues-*` and
`docs/*-proposed-*.diff` notes written before that folder existed. Every other
file under `docs/` ships. `tests/test_setup_config.py` scans every file a
commit would hold (tracked files plus untracked ones `.gitignore` lets
through: code, tests, fixtures, registries, skills and docs alike) for a
maintainer's own identifiers. The markers live only in two gitignored files:
`docs/local/owner-markers.txt`, one regular expression per line, and
`docs/local/owner-marker-digests.txt`, keyed digests of a marker's words with
a key that stays local too. Neither ships, and without them those scans skip.
The common-word list (`src/jobkit/validate/common_words.txt`) is exempt: it
is ordinary English. Examples use the fictional user Jordan Rivera. In a checkout that holds other untracked files, commit by
naming paths (`git add <path>...`), not `git add -A`.

## MCP server (primary interface)

Registered in `.mcp.json`, which is gitignored and machine-local because it
holds an absolute path. The repo ships `.mcp.example.json`; copy it and point
`--directory` at your own checkout:

```bash
cp .mcp.example.json .mcp.json   # then edit the --directory path
```

```json
{
  "mcpServers": {
    "jobkit": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/your/jobkit/checkout", "jobkit-mcp"]
    }
  }
}
```

`uv run` (without `--no-sync`) keeps the editable install in sync, so source
edits reach the server and everything it launches after a restart.
`tests/test_operations.py` checks the example on every run, and checks your
local `.mcp.json` (that `--directory` exists) only when the file is present.

A fresh Claude Code session in this repo exposes jobkit tools: `get_queue`,
`get_job(n)`, `approve("1,3,7"|"all"|">85")`, `reject`, `snooze`,
`get_novel_questions`, `answer_novel`,
`mark_submitted(ids, job_ids, submitted_on)`, `record_outcome(job_id, outcome, note)`,
`get_followups`, `run_followups`, `mark_followup_sent`, `get_stats`,
`run_nightly`, `doctor`, `schedule_status`, `schedule_install(confirm=false)`,
`schedule_uninstall(confirm=false)`,
`get_digest`, `calibrate`, `corpus_health`, `tune_filters`,
`refilter(confirm=false)`, `purge_empty_jd_cache(confirm=false)`,
`figures(bullet_id, rendered)`,
`merge_feed_dupes(confirm=false)`, `repair_applications(confirm=false)`,
`get_artifacts(n)`, `review_artifacts(n)`,
`get_self_review(n, job_id, full)`, `get_questions(show_all)`,
`answer_question(key, answer, evidence, metric_confidence, role_id, kind, decline)`,
`undecline_question(key)`,
`confirm_proposal(proposal_id, evidence, metric_confidence, role_id)`,
`rescore(job_ids, confirm=false)`, `rejudge(job_ids, confirm=false)`, `retailor(n)`,
`stage_forms(ids, job_ids)`, `restage(n)`, `open_queue`,
`background_status`, `add_job_url(url)`, `linkedin_discover(confirm=false)`,
`resolve_boards(job_id, confirm=false, pick)`.
Queue numbering is pinned per render.

The setup interview (see Setup) adds `setup_status`,
`ingest_resume(path, text, replace_draft)`, `corpus_draft_status(batch_size)`,
`confirm_bullets(decisions)`, `update_corpus_draft(changes)`,
`promote_corpus(replace_existing)`, `answer_bank_questions`,
`build_answer_bank(answers, replace_existing)`,
`run_intake(answers, replace_existing)` and
`seed_targets(companies, probe, include_not_found)`.

## Manual fallback: Claude in Chrome

Workday-hosted applications and portals with their own login flows route to
the manual path: use Claude in Chrome for a conversational assist on a single
application. Playwright staging is for batches on Greenhouse / Lever / Ashby /
SmartRecruiters and DOM-scrapeable forms.
