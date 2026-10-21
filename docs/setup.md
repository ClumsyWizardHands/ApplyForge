# Setup

ApplyForge runs one person's job search on their own computer. The repository
holds the program. Your resume facts, answers and preferences are written by a
setup interview and stay on your machine. The command, package and Claude tools
are named `jobkit`.

The short way is to let Claude do everything: in Claude Code, say "Clone
https://github.com/ClumsyWizardHands/ApplyForge into ~/code/jobkit and set it up"
(see the [README](../README.md#get-started)). This page covers what that does,
and how to do each part by hand.

## What you need

- **macOS or Linux.** Windows is untested. Put the checkout somewhere ordinary
  such as `~/code/jobkit`, not under Desktop, Documents, Downloads or iCloud
  Drive. macOS stops scheduled jobs from reading those folders, and iCloud
  sets the hidden flag on files, which makes Python skip the virtualenv's
  `.pth` file so `import jobkit` fails.
- **Access to the repository.** If it is private, your GitHub account needs an
  invitation, and git or the GitHub CLI (`gh auth login`) must be signed in.
- **uv**, the Python tool ApplyForge runs with: `brew install uv`, or
  `curl -LsSf https://astral.sh/uv/install.sh | sh`. It installs Python 3.12.
- **The Claude app** (desktop, with its Code tab and scheduled tasks) or
  **Claude Code** in a terminal. The daily run is a scheduled task in the
  Claude app; without the app, a macOS LaunchAgent or a cron line runs it.
- **A Claude subscription or an API key.** Scoring and tailoring call Claude
  through the `claude` command (Claude Code, signed in to your subscription),
  or through `ANTHROPIC_API_KEY` in `.env` if you set one.

## Where files are stored

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
`uv run jobkit doctor` checks the machine and your files, with the fix for each
problem.

## Project skills

Claude loads these from `.claude/skills/` when its folder is your checkout.

| skill | say | what it does |
|---|---|---|
| `jobkit-setup` | "set up jobkit", "continue jobkit setup" | the setup interview, then the first run and the daily schedule |
| `jobkit-daily` | "what's in the queue" | approve or reject by number, check the tailored files, open the filled forms, log submissions and employer responses, answer jobkit's questions |
| `jobkit-scheduled-run` | (the scheduled task runs it) | starts `jobkit nightly` unless a nightly run finished in the last 12 hours, waits for it, checks your email for replies if `tracking.email_check` is on and an email connector is available (read-only; records only clear matches), then writes one summary from the digest: what failed, what waits on you, the queue and the questions for you |

The scheduled task's prompt is one line that points at
`.claude/skills/jobkit-scheduled-run/SKILL.md` in your checkout, so the routine
updates with the code. The task runs while the Claude app is open; a run missed
while it is closed happens at the next launch. The unattended run doesn't
approve jobs, answer questions or submit anything. Cloud routines can't reach
your checkout, so they can't run jobkit. `tests/test_claude_skills.py` checks
that every `jobkit` command and Claude tool the skills name exists, with the
options they use, and that the scheduled run only starts the nightly run,
reads its results and records clear email replies.

## Installing by hand

```bash
git clone <this repo> ~/code/jobkit   # not under ~/Desktop, ~/Documents or iCloud Drive
cd ~/code/jobkit
uv sync                               # Python 3.12 and dependencies
uv run playwright install chromium    # the visible browser that fills forms
uv run jobkit init                    # creates data/jobkit.db; the only command that does
cp .mcp.example.json .mcp.json        # then set --directory to this checkout (below)
```

Then open the checkout in Claude (Claude Code or the Claude app's Code tab) and
say "set up jobkit". The interview reads your resume, builds your career
corpus from its text only (you confirm every entry, and set each figure's
confidence and evidence yourself), asks the answer-bank questions, and writes
your config. Finally:

```bash
uv run jobkit doctor                  # every check, with the exact fix for each problem
```

## `jobkit doctor`

`jobkit doctor` (Claude tool `doctor`) is read-only. It checks:

- Python 3.12 and `uv`;
- Playwright's Chromium (the revision the installed Playwright needs);
- an LLM backend: `ANTHROPIC_API_KEY`, or the `claude` command on PATH;
- that the checkout is not in an iCloud-synced folder, and that no virtualenv
  `.pth` file is hidden;
- every personal config file below, present and loading;
- the corpus, the answer bank and the database;
- the `.mcp.json` registration.

It also warns, without failing, about:

- a missing contact address for the User-Agent sent to job boards;
- a place in `filters.locations.allowed` with no commute time. Without one, a
  hybrid posting there is rejected as `sanity:commute:unknown`;
- a `filters.yaml` with no `prompt_set` and no track that looks like sales (see
  [scoring](how-it-works.md#scoring));
- the template's placeholder track (`my_role`, `REPLACE ME`) still in place;
- a standing rule whose `title_class` names no track;
- Adzuna or USAJOBS turned on without their keys in the environment;
- LinkedIn discovery turned on with no keywords and no sales track.

It prints the no-reply rule it will use (the number of days, and where that
number came from) as information. It exits 1 while anything needs fixing;
warnings don't fail it. The fix for a missing config file is the setup
interview or its template, never a copy from git history.

## Your files

All of these are gitignored. The setup interview writes them; each has a
commented template next to it, so you can also copy and edit by hand.

| file | what it holds | template |
|---|---|---|
| `config/filters.yaml` | prompt set, role tracks, title include/exclude, places, pay floors, remote policy, schedule, no-reply rule, feeds | `config/filters.example.yaml` (any field); `config/filters.sales.example.yaml` (a worked sales search) |
| `config/targets.yaml` | companies polled directly, with tiers and job boards | `config/targets.example.yaml` |
| `config/autoapprove.yaml` | standing approve and reject rules (none approve at first) | `config/autoapprove.example.yaml` |
| `config/banned_phrases.yaml` | writing-style lists and `drift.estimate_style` | `config/banned_phrases.example.yaml` |
| `config/commute_table.yaml` | minutes from home per location bucket | `config/commute_table.example.yaml` |
| `data/career_corpus.yaml` | every fact a resume, letter or answer may use | `data/career_corpus.example.yaml` |
| `data/answer_bank.yaml` | the questions every application form asks | `data/answer_bank.example.yaml` |
| `.env` | optional keys (`ANTHROPIC_API_KEY`, Adzuna, USAJOBS) and `JOBKIT_CONTACT_EMAIL` | `.env.example` |
| `.mcp.json` | the Claude tools registration for this checkout | `.mcp.example.json` |

A command that needs a missing config file stops with one line naming both
fixes, for example: `config/filters.yaml not found — run the jobkit setup
interview (say "set up jobkit" in Claude) or copy config/filters.example.yaml`.
The data templates describe a fictional person, Jordan Rivera. Don't copy an
answer from them.

## The setup interview, step by step

Each step is a Claude tool (what Claude calls when you say "set up jobkit") and
a `jobkit setup` command. `jobkit setup-status` (tool `setup_status`,
read-only) lists the steps in order, which are done, and the next action.

| step | Claude tools | CLI | writes |
|---|---|---|---|
| 1. read the resume | `ingest_resume(path, text)` | `jobkit setup ingest <file> [--text-file f]` | `data/career_corpus.draft.yaml`, `data/resume_critique.md`, `data/setup/resume_source.txt` |
| 2. confirm the corpus | `corpus_draft_status`, `confirm_bullets`, `update_corpus_draft`, `promote_corpus` | `jobkit setup draft`, `confirm`, `update-draft`, `promote` | `data/career_corpus.yaml` |
| 3. preferences | `run_intake(answers)` | `jobkit setup intake <answers.yaml>` | `config/filters.yaml`, `config/commute_table.yaml`; `autoapprove.yaml` (no rules) and `banned_phrases.yaml` when missing |
| 4. target companies | `seed_targets(companies, feed_only_said)` | `jobkit setup targets <name>... [--file f]`, or `--feed-only "<your words>"` | `config/targets.yaml` (new entries appended) |
| 5. answer bank | `answer_bank_questions`, `build_answer_bank(answers)` | `jobkit setup questions`, `jobkit setup answers <answers.yaml>` | `data/answer_bank.yaml` |

Structured input (bullet decisions, interview answers, companies) is JSON for
the Claude tools, and a YAML or JSON file or inline JSON for the CLI. The tool
descriptions list every field. What each step does and checks:

### Reading the resume

- **Formats:** PDF, DOCX, HTML, plain text, or the text pasted. PDF text comes
  from pypdf when it is installed, else poppler's `pdftotext` when it is on
  PATH, else a small built-in reader for simple text PDFs. Neither library is a
  dependency; DOCX needs none.
- A PDF, DOCX or HTML file that doesn't extract cleanly (under 50% recognisable
  English words, or more than 5% unreadable characters) is refused with a
  request to paste the text instead. Pasted text and plain-text files aren't
  word-scored, so a resume full of company and tool names (Snowflake, dbt,
  PySpark) gets through. They are refused only when under 200 characters or
  full of unreadable characters (text copied out of a PDF viewer from a custom
  font), and then the message asks for the text from the original document.
- **Section headings from any field.** A short heading ending in "experience",
  "history" or "rotations" opens the job history ("Clinical Experience",
  "Teaching Experience"), unless an earlier word makes it a job title or a
  sentence ("Head of Customer Experience"). Lines under a licences and
  certifications heading, or a projects or portfolio heading, are kept apart
  from the jobs. During the confirmation step Claude offers each line, and a
  line you keep is added to your skills in the resume's wording, which is how
  scoring and tailoring see a licence or a portfolio piece. When no job
  with bullets is found, the error lists the headings it saw, so Claude can ask
  which one holds your jobs.

### Confirming your experience

- **The draft holds only text from your resume.** Every string in it is checked
  against the extracted text before anything is written, and a draft with one
  string the resume doesn't contain is refused. Company, title and dates of each
  role are a best reading of its header, which you confirm or correct; a year
  with no month stays unset until you give the month. A role is confirmed only
  with an end month or your explicit `current: true`. A role whose resume dates
  end in a year alone, or give one date, is never saved as current, because that
  would print "Present" on every resume.
- Each bullet or paragraph of the resume's summary is offered as its own
  positioning line, as written; keep the ones that describe you and drop the
  rest. `metric` holds only figures the bullet states, and tags come only from
  the resume's own skills list. Suggestions go to the critique, not into the
  draft.
- **Skills.** Each skill your resume lists is offered on its own. You keep it,
  with a few words of your own on how deep it goes, or drop it. Only skills you
  keep reach the corpus's `skill_index`; a kept skill with no description is
  refused (`update_corpus_draft` key `resume_skills`).
- **`metric_confidence` and `evidence` are yours.** They are empty in the
  draft. `confirm_bullets` refuses a kept bullet without a confidence, a bullet
  with a figure but no evidence, and `none` for a bullet that states a figure.
  A figure is any number the bullet's final `text_long` or `text_short` states
  (read again on every decision and at promotion, so an edit that adds a number
  is caught) or its `metric` holds. `metric` can't be cleared while the wording
  still states one, and an edit without `metric` takes the figures its new
  wording states. A list of decisions is applied all or nothing.
- `promote_corpus` refuses while any bullet, role, education entry,
  positioning line or the identity block is unconfirmed. The display location
  (the city the resume and letter header show) and your home city are asked,
  never read off the resume. It then reports the bullet count, how many of the
  bullets that state a figure are verified, and the top tags. It says the
  corpus is thin when it has under 30 bullets, or when under 40% of the bullets
  that state a figure are verified.

### Preferences

- **`run_intake`** quotes your answer above every setting it decides, writes a
  config only if it loads with the pipeline's own loader, and refuses answers
  that contradict each other (a title you named that your seniority floor or an
  exclusion would reject).
- **Role tracks.** For each kind of role you name, it writes a track: the job
  titles, the words every title has and the words that narrow it, and titles
  that must and must not match (checked before writing). A space between words
  also matches a hyphen, a slash or nothing, and "senior" and "sr." are the same
  word. A track can be marked as a change of field (`pivot`), with its own queue
  gate (`min_score`) and a line in your words about what should lead your resume
  (`emphasis`). A track named "AE", "Account Executive", "SDR", "BDR" or "SDR
  Manager" gets the id the sales prompts use (`account_executive`, `sdr_bdr`,
  `sdr_bdr_manager`); another sales track with no sales emphasis is named in
  the notes.
- **Prompt set.** It writes `prompt_set: sales` when every track is a sales
  title and no track is marked as a change of field or given an emphasis, and
  `prompt_set: general` otherwise (see [scoring](how-it-works.md#scoring)).
- **Pay.** It refuses answers with no default pay floor (`comp.default_floor`,
  the floor for titles outside your tracks): the answer bank's one
  salary-expectation figure is measured against it. A track whose own floor is
  under the default is named in the output, because its jobs get that field
  left blank and flagged too. Hourly pay is converted to a yearly floor (rate
  times hours a week times 52), with the arithmetic in a comment.
- **Places.** Your home places, the state they are in, and nearby towns within
  your commute (each with minutes) become allowed places and commute-table
  entries. With a state, each place is written with it ("Columbus, OH") and
  `match_state: true` is set, so a posting in Columbus, GA or Dublin, Ireland
  is rejected, and a remote job limited to residents of your state passes. A
  nearby town that the location table puts in another metro is left out and
  named in the notes. A home place that the table files under a metro by its
  spelling (Mansfield, Ohio holds "sf") is named in the notes too. For a remote-only
  search, the state you give is added to the acceptable geo restrictions.
- **Feeds.** Adzuna keywords come from your track titles, with your places for
  hybrid or on-site work. If you also take remote work, Adzuna runs one search
  with no place as well (`search_nationwide: true`), so remote jobs from
  anywhere come back. USAJOBS is turned on only if you want federal jobs.
  LinkedIn discovery keywords are the first four of your titles, and it stays
  off. Remote-only boards are turned off if you don't accept remote work.
- ApplyForge has no industry filter, because postings carry no industry. Wanted and
  refused industries are recorded as comments and used to choose target
  companies; companies you refuse go in `blocklist_companies`.

### Target companies

- **`seed_targets`** finds each company's Greenhouse, Lever or Ashby board from
  a pasted board URL, the bundled registry (`registries/`), or a few tokens
  made from the name and domain, and verifies it on the public board endpoint.
  Nothing else is fetched. A Lever board that answers with no postings is kept
  but marked unverified. A company with no board found is recorded without one
  (its tier still applies to postings the feeds find there) and reported with
  what was tried.
- **Feeds only.** If the employers you want don't use those systems, you can
  skip naming companies. Your reason, in your words, is recorded as
  `feed_only_said` in `config/targets.yaml`, and setup counts the step as done
  with a warning that postings will come from the feeds.

### The answer bank

- **`build_answer_bank`** takes only the template's structure, never a value
  from it. EEO keys take only your explicit choice ("I don't wish to answer" is
  one); a key you skip stays blank and flagged on every form. Salary history is
  never answered. Legal acknowledgements are off unless you opt in, and then
  only labels you approve word for word (`approved_labels`, empty at first) are
  ticked. Answers can be given over several sittings.
- **Hourly pay.** A salary expectation can be hourly. Then the answer is your
  own wording (or left blank), and no yearly floor is set from it, so an hourly
  answer doesn't block your own salary field.

### Your existing files

A file the interview didn't write (your own `filters.yaml`, corpus or answer
bank) is replaced only if you ask (`replace_existing=true`,
`--replace-existing`), and the old copy goes to `data/setup/backup/` first.
What you said is kept in `data/setup/*.answers.yaml`. All of `data/` and
`config/*.yaml` is gitignored.

Planned, not built: proposing the first standing rules from your answers
(`seed_rules`); `config/autoapprove.yaml` starts with none. STAR stories are
added to the corpus by hand.

## Keys and contact address (`.env`)

Secrets go in `.env` (see `.env.example`). The CLI and the Claude tools server
load it at start; a variable already set in the environment wins.

- `ANTHROPIC_API_KEY`: optional. Without it, model calls go through the
  `claude` command and your subscription.
- `ADZUNA_APP_ID` and `ADZUNA_APP_KEY`: free keys from Adzuna's developer site.
  Without them the Adzuna feed is skipped, and the run log says which variable
  is missing.
- `USAJOBS_API_KEY` and `USAJOBS_USER_AGENT_EMAIL`: a free key from USAJOBS,
  and the email address you registered with.
- `JOBKIT_CONTACT_EMAIL`: the contact address in the User-Agent sent to job
  boards. Without it, `identity.email` from your answer bank is used (a template
  placeholder such as `@example.com` is skipped), else no address.

## Locations

Places come only from your config; none are built in.

- A town outside the built-in metro table buckets as `other-<town>` (a "Bristol,
  PA" posting is `other-bristol`). Every town in `filters.locations.allowed`
  that buckets this way needs a commute time under `buckets:` in
  `config/commute_table.yaml` (`other-bristol: 25`). Without one, the remote
  check rejects every hybrid posting there as `sanity:commute:unknown`.
  `jobkit doctor` warns about each allowed place the table doesn't cover and
  names the key to add.
- Regional geo restrictions ("Delaware Valley", "Mid-Atlantic") pass only when
  you list them: add each spelling you accept to
  `remote_policy.acceptable_geo_restrictions`.
- A state in `filters.locations.allowed` (such as `"Pennsylvania"`) is a place
  the filter accepts. A posting located only in that state passes the filter
  and goes to the remote check, which rejects it as `sanity:onsite` or
  `sanity:commute:unknown` unless the posting turns out to be remote. `jobkit
  doctor` lists it with the towns above, as a warning. Leave it without a
  commute time to keep hybrid postings there rejected.

## Project root

Every data path (`data/jobkit.db`, `logs/`, `out/`, `data/raw/`, the browser
profile, the corpus and answer bank) is resolved from one root: `JOBKIT_HOME` if
set, otherwise the checkout the package was loaded from. The CLI and the Claude
tools server change into that root at start, so `config/`, `prompts/` and
`templates/` resolve whatever directory they were launched from. A missing
database is an error that names `jobkit init`; no other command creates one.

## Registering the Claude tools

The tools are provided by an MCP server registered in `.mcp.json`, which is gitignored
because it holds an absolute path. The repository includes `.mcp.example.json`;
copy it and point `--directory` at your checkout:

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

`uv run` (without `--no-sync`) keeps the editable install in step, so source
edits reach the server and everything it launches after a restart.
`tests/test_operations.py` checks the example on every run, and checks your
local `.mcp.json` (that `--directory` exists) only when the file is present.
[Commands and tools](commands.md) lists every tool.

## The daily run

The setup interview offers two ways to run `jobkit nightly` once a day. Each is
created only on your yes.

- **A scheduled task in the Claude app** (the default). Its prompt points at
  the `jobkit-scheduled-run` skill in your checkout. It runs while the app is
  open, at the time you chose.
- **A macOS LaunchAgent**, `com.jobkit.nightly` in `~/Library/LaunchAgents`,
  managed by `jobkit schedule install|uninstall|status` (tools
  `schedule_install`, `schedule_uninstall`, `schedule_status`). On other
  systems these commands say scheduling isn't supported; run `jobkit nightly`
  from cron or a systemd timer yourself.

About the LaunchAgent:

- It runs `<uv> run --directory <project root> jobkit nightly` at
  `schedule.nightly_at` in `config/filters.yaml` (quoted `"HH:MM"` local time,
  default `"02:00"`). `uv` and `claude` are found with `which` at install time.
  It sets the working directory to the project root, PATH to their directories
  plus `/usr/bin:/bin:/usr/sbin:/sbin`, `JOBKIT_HOME`, and
  `PYTHONPATH=<root>/src`. Output goes to `logs/nightly.out.log` and
  `logs/nightly.err.log`. To change the time, edit `nightly_at` and install
  again; `status` flags a mismatch.
- `install` refuses a checkout under `~/Desktop`, `~/Documents`, `~/Downloads`
  or iCloud Drive, including data, config or logs linked there. It replaces an
  installed agent and removes the old `com.jobkit.morning` and
  `com.jobkit.followups` agents, then prints the agent and a warning. The Mac
  must be awake at that time; launchd doesn't wake it. A run missed while it
  sleeps happens at the next wake; one missed while it is shut down is skipped.
  `--dry-run` prints the plist and installs nothing.
- `status` is read-only. It shows whether the agent is installed and loaded
  (launchd state, runs, last exit code), the latest nightly run stage by stage
  with its exit, the last successful one, and the jobs stopped in form filling
  that wait on you (reason, screenshot, apply URL).
- The `schedule_install` and `schedule_uninstall` tools are dry runs until
  called with `confirm=true`.
- `scripts/install_schedule.sh` only runs
  `uv run --directory <repo> jobkit schedule install`.

## Updating

Close every open Claude session in the jobkit folder before you run `git pull`,
then:

```bash
cd ~/code/jobkit && git pull && uv sync && uv run jobkit doctor
```

Then start a new Claude session so the jobkit tools reload. A session left open
during the update keeps running the old tools against the new code and the
new database, and any of its tools can misbehave, not only the follow-up ones.
For example, its `rescore` fails with an error, and its `mark_submitted` still
reports follow-up reminders that are never drafted, or fails with "no such
table: followups" and records nothing; restart Claude and mark it again. Each
of its calls also re-creates an empty `followups` table that the next new
command drops again.

If your database has the `followups` table from an older version, the first
jobkit command that opens the database does this once. That is any command
other than `jobkit doctor`, or the first Claude tool call:

1. saves a copy of the whole database as `data/jobkit.pre-followups.db` (if that
   name is taken, `-2`, `-3` and so on; an existing copy is never overwritten);
2. stores every row of the table, unsent drafts included, as a
   `followups_archived` event;
3. drops the table.

If the copy can't be written, nothing changes. Rows found later (an old open
session can add them) get a copy of their own under the next free name, up to
five copies; after that they are kept as events with no copy. An empty
`followups` table found later is dropped with no copy, since it holds
nothing. While one jobkit process makes the copy, others wait for it (up to
two minutes) instead of failing. A `followups:` block left in your
`config/filters.yaml` is now read only for its `close_as_ghosted` days, when
`tracking.ghost_after_days` is not set.

The no-reply rule now covers every application still at `submitted` with no
reply on record after that many days, including ones the old follow-up
schedule had no reminder for (submissions from before follow-ups existed, or a
reminder you skipped). So the first nightly run after the update may mark
several applications as ghosted at once. To see which ones first, run `uv run
jobkit ghost-close --dry-run`, or ask Claude to run it (Claude's
`ghost_close` tool with `confirm` false does the same).

Other changes you may notice on an existing search, including a sales one
whose config has none of the new settings:

- A job you paste with `jobkit add-url` (or `add_job_url`) is now extracted
  and scored, which is two Claude calls per link. Before, it was queued with
  no score.
- `jobkit add-url` on a Taleo, Jobvite, USAJOBS or other recognised but
  unsupported link now names the system and exits 0, where it used to exit 1
  with "could not recognize". The note for unsupported systems is worded
  differently, and LinkedIn discovery records such a card by the system's name
  (for example `offsite:taleo`) instead of `offsite:unrecognized`.
- The digest has no follow-ups section. Its funnel ends with "no reply after
  30 days" when any application has been marked ghosted. A "Replies recorded
  since the last digest" section appears when email replies were recorded,
  and a "Feed jobs not scored" line when feed jobs came with too short a
  description to score.
- A keyed feed (Adzuna, USAJOBS) that returns no rows is recorded as `empty`
  (log `feeds.empty`), where it used to be recorded as `skipped_no_key`, so
  the run summary on a quiet day reads differently.
- Standing rules can name any track id as `title_class`. A rule naming
  `account_executive`, `sdr_bdr` or `sdr_bdr_manager` decides as before.
- An Ashby posting that gives an hourly, weekly or monthly range is no longer
  read as a yearly one: its pay is treated as unpublished. Before, a $45-60
  an hour posting was read as $60 a year and rejected by your pay floor; now
  it passes the filter like any posting without a published yearly range, and
  is scored. The next poll rewrites the pay of such postings already stored.
- `jobkit add-url` on a link whose site it can't recognize (many hospitals and
  schools use their own careers domain) now says so and exits 0, where it
  used to exit 1.
- The no-reply rule covers every application at `submitted` with no reply on
  record, and skips one whose stage you set back. The old rule marked any
  application with a pending close reminder. It counts days by your local
  date; ghost rows the old code made counted by the UTC date.
- `uv run jobkit run-all` and the nightly run name the stage `ghost-close`
  where they said `followups`.
- To have the scheduled daily run check your email for replies, add
  `email_check: true` under `tracking:` in `config/filters.yaml`. Without it
  the scheduled run skips email; asking Claude to check your email works
  either way.
- USAJOBS stores the posting's summary alone unless
  `feeds.usajobs.full_description: true` is set (the setup interview writes
  it). With it, duties, qualifications and education are stored too, so
  federal jobs are long enough to score; each one is scored again once.
