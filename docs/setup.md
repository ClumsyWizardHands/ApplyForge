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

- **A computer you can run Claude Code on.** I built and tested ApplyForge on
  my Mac, and CI runs the tests on macOS and Linux. Windows reports are welcome: [open an
  issue](https://github.com/ClumsyWizardHands/ApplyForge/issues) with whatever
  trips you up.
- **A plain folder for the checkout.** Tip from experience: use something like
  `~/code/jobkit`. On macOS, Desktop, Documents, Downloads and iCloud Drive are
  off-limits to scheduled jobs, and iCloud sets the hidden flag on files, which
  makes Python skip the virtualenv's `.pth` file so `import jobkit` fails.
  `jobkit doctor` catches this and prints the command to move the checkout.
- **Access to the repository.** While it's private, ask for an invite, then
  sign in with git or `gh auth login`.
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
| `jobkit-setup` | "set up jobkit", "continue jobkit setup" | the setup interview (your bar included), then the first run and the daily schedule |
| `jobkit-daily` | "what's in the queue", "check my email" | the applications waiting for your review, approve or reject the rest by number, check the tailored files, open the filled forms, log submissions, check your email for replies, job alerts and LinkedIn messages, answer jobkit's questions |
| `jobkit-scheduled-run` | (the scheduled task runs it) | starts `jobkit nightly` unless a nightly run finished in the last 12 hours and waits for it; if `tracking.email_check` is on and an email connector is available, checks your email (read-only; records only clear reply matches) for replies, job alerts and LinkedIn messages, and gets the alert jobs your bar approved written for and filled; opens the filled forms when `staging.open_after_daily_run` is on; then writes one summary: what failed, what is ready for you, replies, alert finds, messages, the queue below your bar and the questions for you |

The scheduled task's prompt is one line that points at
`.claude/skills/jobkit-scheduled-run/SKILL.md` in your checkout, so the routine
updates with the code. The task runs while the Claude app is open; a run missed
while it is closed happens at the next launch. The unattended run doesn't
approve jobs itself (your bar does), answer questions or submit anything. Use a scheduled task in
the app, not a cloud routine: cloud routines run on another machine and can't
see your checkout. `tests/test_claude_skills.py` checks
that every `jobkit` command and Claude tool the skills name exists, with the
options they use, and that the scheduled run only starts the nightly run,
reads its results, records clear email replies, job alerts and messages,
writes and fills the jobs your bar approved, and opens the forms only when you
turned that on.

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
- an LLM backend: `ANTHROPIC_API_KEY`, or the `claude` command on PATH. With
  the `claude` command, one tiny call (60-second timeout) checks the login:
  when it expired, the fix is to run `claude` and type `/login`;
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
- LinkedIn discovery turned on with no keywords and no sales track;
- a `claude` command that answers with an older model than jobkit asks for,
  or warns that it doesn't recognise the model: run `claude update`.

It prints the no-reply rule it will use (the number of days, and where that
number came from) as information. It exits 1 while anything needs fixing;
warnings don't fail it. The fix for a missing config file is the setup
interview or its template, never a copy from git history.

## Your files

All of these are gitignored. The setup interview writes them; each has a
commented template next to it, so you can also copy and edit by hand.

| file | what it holds | template |
|---|---|---|
| `config/filters.yaml` | prompt set, role tracks, title include/exclude, places, pay floors, remote policy, schedule, no-reply rule, email check, opening the forms after the daily run, web search for alert jobs, feeds | `config/filters.example.yaml` (any field); `config/filters.sales.example.yaml` (a worked sales search) |
| `config/targets.yaml` | companies polled directly, with tiers and job boards | `config/targets.example.yaml` |
| `config/autoapprove.yaml` | your bar (the clears-your-bar rule, written on your yes in setup), any other standing rules, and an optional daily limit | `config/autoapprove.example.yaml` (the template has the clears-your-bar rule on: after copying it, delete that rule if you want to approve every job yourself) |
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
| 3. preferences | `run_intake(answers)` | `jobkit setup intake <answers.yaml>` | `config/filters.yaml`, `config/commute_table.yaml`; `autoapprove.yaml` (your bar, when you said yes) and `banned_phrases.yaml` when missing |
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
- **Your bar** (`auto_approve`). On a yes it writes one rule in
  `config/autoapprove.yaml`: approve every job scoring your number or more
  (75 suggested), unless a deal-breaker holds it back: a doubt about remote
  or location, pay that looks unrealistic, a location left unclear, or a feed
  job with no employer form. A daily limit
  (`max_auto_approve_per_day`) is written only when you give a number; by
  default there is none. On a no, no rule is written and you approve every
  job yourself. A rule file you wrote yourself is kept, and the output names
  the rule to add. See [Your bar](how-it-works.md#your-bar-automatic-approval).
- **Opening the forms** (`open_forms`). `staging.open_after_daily_run`, true
  unless you say no: the scheduled run ends by opening the filled
  applications for your review.
- **Web search for alert jobs** (`alert_search`). "When a job from your
  alert emails isn't on a board I can read, should I search the web for
  it?" Yes is recommended; a job on Workday or a similar site is then listed
  with its direct link. A limit on searches per email check is written only
  when you give one. See
  [LinkedIn](why-no-linkedin.md#web-search-for-alert-jobs).
- **A scoring limit** (`scoring_limit`). Every job that passes your filters
  is scored in each run. Only when you give a number is
  `triage.max_jobs_per_stage` written; the rest then wait for the next run.
- **Cover letter drafts** (`tailoring.cover_attempts`, default 5). Copied
  from the template with no question: how many drafts a cover letter gets
  before it is set aside for you. Change the number in `config/filters.yaml`.

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

`config/autoapprove.yaml` starts with your bar when you said yes to it, and
no other rules. You add more as patterns show up, and `calibrate` proposes
some. STAR stories go into the corpus by hand for now.

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
  open, at the time you chose. After `jobkit nightly` it checks your email
  (replies, job-alert emails, LinkedIn messages) when `tracking.email_check`
  is on and an email connector is there, gets the alert jobs found on the
  employer's own board scored, written for and filled, opens the filled forms
  when `staging.open_after_daily_run` is on, and sends you a summary. The
  first time it runs `jobkit tailor`, `jobkit stage` or `jobkit open-queue`,
  the Claude app may ask; choose "always allow".
- **A macOS LaunchAgent**, `com.jobkit.nightly` in `~/Library/LaunchAgents`,
  managed by `jobkit schedule install|uninstall|status` (tools
  `schedule_install`, `schedule_uninstall`, `schedule_status`).
- **Linux, Windows and other systems:** the Claude app's scheduled task works
  the same way. Or run `jobkit nightly` once a day, from the project root,
  with cron or a systemd user timer (Linux) or Task Scheduler (Windows). The
  `schedule` commands set up the macOS agent only.

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
  `com.jobkit.followups` agents, then prints the agent. Tip:
  launchd doesn't wake the Mac. A run missed while it sleeps happens when it
  wakes; one missed while it's shut down is skipped.
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

ApplyForge changes often. Close any open Claude sessions in the jobkit folder,
then run:

```bash
cd ~/code/jobkit && git pull && uv sync && uv run jobkit doctor
```

Then start a new Claude session so the jobkit tools load the new code. A
session left open keeps the old tools, which can fail against the updated
database and record nothing.

[What's new](changelog.md) lists each change you might notice, and what an
update does to your files when it needs to.
