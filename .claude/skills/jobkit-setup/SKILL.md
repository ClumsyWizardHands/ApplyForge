---
name: jobkit-setup
description: The guided jobkit setup interview for a new user, start to finish. Checks the machine, reads the person's resume, confirms every corpus entry with them (they set each metric's confidence and evidence), asks their job preferences, target companies and application answers one question at a time, runs the first pass, and schedules the daily run as a Claude app scheduled task (macOS LaunchAgent as fallback). Use when the person says "set up jobkit", "onboard me", "get me started", "continue jobkit setup", or on a first run (no data/career_corpus.yaml, or `jobkit setup-status` / `jobkit doctor` reports missing setup).
---

<!-- Maintainers: name jobkit MCP tools as calls, e.g. setup_status(), and CLI
commands in code spans, e.g. `uv run jobkit doctor`. tests/test_claude_skills.py
checks that each one exists. -->

# jobkit setup interview

jobkit finds job postings, checks them, tailors a resume and cover letter
from facts the person confirmed, and fills application forms up to the last
click. The person reviews and clicks submit. This interview builds their
files. It takes about an hour and can stop and resume at any point: every
answer is saved as it is given.

## How to talk (this matters more than speed)

The interview is long, so keep the load on the person low. Make every turn
easy to answer:

- **One question per message.** Short. Say what you need and why in one line.
- **Show progress** at the top of each message: `Step 3 of 8 · bullets 6-10 of 24`.
- **Offer choices** when there are a few: `verified / estimate / none`.
- **Accept short replies** ("keep · verified · Q3 board deck") and
  "skip", "back", "pause". On "pause": say what is saved and that "continue
  jobkit setup" picks up here.
- **No walls of text.** Never paste a whole file. Summarize, then ask.
- After each step: one line on what was saved, then the next step's first
  question.

## Rules that never bend

These protect the person. A tool refusal is the rule working: relay the
reason in plain words and ask again. Never work around one.

1. **The corpus draft holds only the resume's own text.** Never rewrite,
   improve, shorten or add to it. Improvements go in the critique
   (`data/resume_critique.md`), never into the draft. When the person wants
   different wording, ask them to type the sentence as they want it, and use
   exactly that. Never offer a rewrite of your own.
2. **`metric_confidence` and `evidence` come from the person, for every
   bullet.** Ask; never default, guess or suggest a value. If the person
   says one answer covers a whole batch, you may apply it to that batch. Never
   propose that yourself.
3. **Screening answers are asked, never assumed.** Nothing comes from the
   example files or the resume without the person saying it. Work
   authorization and sponsorship are legal statements: ask them exactly.
4. **EEO questions are optional.** Each takes the person's explicit choice,
   "I don't wish to answer", or nothing (the field then stays blank and is
   flagged on every form). Never pick one for them.
5. **Salary history is never answered.** jobkit leaves it blank and flags it.
6. **Approve before tailoring.** jobkit tailors only jobs the person approves
   (or standing rules they accepted one at a time; there are none at first).
7. **The person submits.** jobkit fills forms up to the last click and stops.
   It never clicks submit, and never solves a CAPTCHA or bot check. It has
   no submission step to turn on: there is no `src/jobkit/autofill/submit.py`
   and no `auto_submit` setting. If they ask for one, say so. Do not build,
   enable or configure automatic submission: that is a change to Hard Rule 1
   in `CLAUDE.md`, which only the owner of the jobkit GitHub repository makes.
8. **Facts only from the corpus.** A resume, letter or form answer may only
   state what the person confirmed. A question the corpus cannot answer is
   flagged for them, never invented.
9. **Their files stay private.** `data/`, `config/*.yaml`, `.env` and
   `.mcp.json` are gitignored. Never commit them, and never copy a config
   file out of git history (that is someone else's config).
10. **Persistent changes need a yes.** Installing a schedule, creating a
    scheduled task or changing settings happens only on the person's explicit
    yes to that exact change.

## Tools: MCP first, CLI when it is not connected

Each step below names the jobkit MCP tool and its CLI twin; both run the same
code and the same checks. The MCP tools appear only after `.mcp.json` exists
and a new session starts in this folder. Until then use the CLI from the
checkout (call its absolute path REPO; `git rev-parse --show-toplevel`).

For CLI steps that take structured input (bullet decisions, answers,
companies), write the JSON to a file under `data/setup/tmp/` (gitignored) and
pass the path. That avoids shell quoting problems with apostrophes.

## Step 0 — Where are we?

Run setup_status() (CLI: `uv run jobkit setup-status`). It lists the steps
done and the next action. Resume at the first step not done. Tell the person
in one line: "Picking up at step N: <name>."

## Step 1 of 8 — Check the machine

Run `uv run jobkit doctor` (MCP: doctor()). On a fresh clone it fails on the
config and data files: that is expected, this interview writes them. Fix
only the machine items now, one at a time, asking before each:

- **uv missing**: show the person the install command doctor prints and let
  them run it (it downloads and runs an installer). Then a new terminal.
- **Python not 3.12**: `uv python install 3.12`, then `uv sync`.
- **Chromium missing**: `uv run playwright install chromium` (a download of
  about 150 MB; say so first). jobkit fills forms in a visible browser.
- **LLM backend**: either the `claude` CLI on PATH (Claude Code, logged in;
  runs on their Claude subscription) or `ANTHROPIC_API_KEY` in `.env`. Never
  type a key yourself: the person pastes it into `.env`.
- **Checkout location**: not under iCloud Drive, ~/Desktop, ~/Documents or
  ~/Downloads (macOS hides files there and launchd cannot read them).
  Doctor prints the move command; ask before running it.
- **Database**: `uv run jobkit init`.
- **MCP registration**: write `.mcp.json` from `.mcp.example.json` with
  `--directory` set to REPO (doctor prints the exact JSON), or run
  `claude mcp add --scope project jobkit -- uv run --directory REPO jobkit-mcp`.
  Tell the person the jobkit tools appear in the next session in this
  folder (approve the "jobkit" server when asked). Carry on with the CLI now.
- **Contact email** (a warning): offer to put `JOBKIT_CONTACT_EMAIL=` in `.env`
  for the polite job-board User-Agent. Optional.

Re-run doctor until only config/data items (and warnings) remain.

## Step 2 of 8 — The resume

Ask: "Where is your resume? A PDF, Word (.docx), HTML or text file path
works, or paste the text here."

- File: ingest_resume(path="<path>") (CLI: `uv run jobkit setup ingest "<path>"`).
- Pasted: ingest_resume(text="<the pasted text>") (CLI: save it to
  `data/setup/tmp/pasted_resume.txt`, then
  `uv run jobkit setup ingest --text-file data/setup/tmp/pasted_resume.txt`).
- Refused because the file did not extract cleanly: ask them to paste the
  text. Never guess at a garbled parse.
- A draft already holding confirmations is kept; only start over
  (ingest_resume(replace_draft=true), CLI `--replace-draft`) on their yes.

Then show the critique's top five items from `data/resume_critique.md`, one
line each. They are suggestions for the person's own resume, not changes to
the draft.

## Step 3 of 8 — Confirm the experience (the corpus)

Every later resume, letter and form answer draws only on what is confirmed
here. Run corpus_draft_status(batch_size=5) (CLI:
`uv run jobkit setup draft --batch 5`) to see what is left.

**3a. Identity, roles, education, summary lines**, via
update_corpus_draft(changes) (CLI: `uv run jobkit setup update-draft <file>`).
One question at a time:

- Name, email, phone as they should print. Confirm what the resume shows.
- **Display location**: "Which city should your resume header show?" It is a
  positioning choice and can differ from where they live. Always ask.
- **Home city**: where they actually live. Always ask.
- Each role: company and title exactly as they should print, start and end
  month. If the end is missing or only a year, ask "Is this your current
  job?" Never assume current.
- Each school and degree: keep, fix or drop.
- Each summary line from the resume: keep or drop.

**3b. Bullets, five at a time**, via confirm_bullets(decisions) (CLI:
`uv run jobkit setup confirm <file>`). One bullet per message:

```
Step 3 of 8 · bullet 7 of 24 · Sales Manager, Acme
"Grew pipeline 40% in two quarters."
Keep, edit or drop?  Then: how solid is the number?
  verified = you can point at a source · estimate = you'd say "roughly" · none = no figure
And the evidence, one line: where could you show it? (a dashboard, a doc, a person)
```

- `edit`: their own sentence, exactly as typed, as `text_long`.
- A bullet that states a number cannot be `none` and needs evidence.
- A bullet with no outcome: ask once, "Was there a number? Roughly how much,
  over what period?" If they give one, the bullet changes only by an edit
  in their words. "skip" is fine.
- After five, send the batch. It is all or nothing: on a refusal, relay the
  reason, fix just that bullet with the person, send again.

**3c. Voice samples (optional)**: "Paste 2 or 3 things you wrote yourself:
an email, a doc intro. It keeps cover letters sounding like you." Store them
as written via update_corpus_draft(changes) (`voice_samples`).

**3d. Promote**: promote_corpus() (CLI: `uv run jobkit setup promote`). It
reports bullet count and verified share. Under 30 bullets or 40% verified is
a thin corpus: say so plainly, and that the daily questions grow it over
time.

## Step 4 of 8 — What they want (preferences)

Ask against their corpus ("Your last two roles managed teams. Keep managing,
or go back to individual work?"), one question at a time. Keep their exact
words for each `said` field.

1. Direction: keep the current path, or pivot? Toward which titles? Each
   group of titles is a track.
2. Titles they would take, and titles they would refuse.
3. Seniority floor: any / no internships / experienced / senior.
4. Remote only, hybrid, or onsite is fine? Which cities? Max office days a
   week and max commute; minutes from home to each city.
5. Would they relocate? Where?
6. **Comp floor**: the number below which they would say no. Always ask the
   default floor (titles outside every track); per-track floors are
   optional. For sales roles: is it base or OTE?
7. Industries they want, and industries they refuse.
8. Companies they would never work for.
9. Writing style for estimated numbers: `hedged` (estimates keep "about") or
   `confident` (stated plainly, exactly as written).

Then run_intake(answers) (CLI: `uv run jobkit setup intake <file>`). The tool
description lists every field. On a refusal (for example a title their
floor would reject), ask the conflicting question again. Never edit a config
file the interview did not write without their yes
(replace_existing=true, CLI `--replace-existing`; the old copy is backed up).

If none of their tracks is a sales role, tell them in one line: jobkit's fit
score is calibrated for sales roles, so for their kind of work they should
decide on each job's gaps and rationale, not on the score alone (README,
"Tuned for sales roles").

## Step 5 of 8 — Companies to watch

Ask: "Name companies you'd love to work at." (tier 1), then "Others worth
watching?" (tier 2). You may suggest 20 to 40 more from their sector, stage,
size and places; they approve the list.

Run seed_targets(companies) (CLI:
`uv run jobkit setup targets "Name 1" "Name 2" --tier 1`, or
`uv run jobkit setup targets --file <file>` for a list with tiers). It only
uses the public job-board endpoints. Report: boards found, unverified, not
found. For each not found, ask for a careers page or job-board link and retry
with `{name, url}`.

## Step 6 of 8 — Application answers (the answer bank)

Run answer_bank_questions() (CLI: `uv run jobkit setup questions`) and ask
each question in its order, one per message. Save every few answers with
build_answer_bank(answers) (CLI: `uv run jobkit setup answers <file>`); answers
can come over several calls.

- Identity: you may offer what they told you in step 3a and ask "Same for
  applications?" The city here is the real one, not the display location.
- Work authorization and sponsorship: ask exactly as written.
- Comp expectation: give a number, their own wording, or leave it blank
  every time. A number below their default floor is refused.
- EEO block: say first that every one is optional. Offer three choices:
  their answer, "I don't wish to answer", or skip (blank and flagged).
- Legal acknowledgements: off unless they opt in; then only labels they
  approve word for word.

## Step 7 of 8 — First run

Run `uv run jobkit doctor` again: everything but warnings must pass. Then
start the first pass in the background (20 to 60 minutes; it uses their
Claude subscription or API key for scoring):

```bash
mkdir -p logs && uv run jobkit nightly > logs/first-run.log 2>&1
```

(MCP: run_nightly().) Do step 8 while it runs. When it finishes, show the
queue: get_queue() if the MCP tools are connected, else the "Review queue"
section of `out/digest/<today>.md`. Do not approve anything: explain that
they approve by number ("approve 1, 3"), and that tailoring and form filling
happen only for jobs they approve.

## Step 8 of 8 — Daily runs

The daily run is `jobkit nightly`: new postings, scoring, tailoring approved
jobs, filling approved forms (never submitting), follow-up drafts, digest.

Ask: "What time each day should jobkit run? Pick a time your computer is on
and the Claude app is open, for example 7:00." Mention that a browser window
may open and close by itself during the run when approved forms are filled.

**A. Claude app scheduled task (preferred).** If the
create_scheduled_task() tool is available:

1. list_scheduled_tasks(): if a jobkit task exists, offer to update it
   (update_scheduled_task()) instead of adding a second one.
2. On their yes, create_scheduled_task(taskId="jobkit-daily-run",
   title="jobkit daily run", description="Run jobkit nightly and summarize the digest",
   cronExpression="0 7 * * *", prompt=...) with their time in the cron
   expression (local time) and this prompt, REPO replaced by the absolute
   path:

   ```
   Daily jobkit run. The jobkit checkout is REPO. Read REPO/.claude/skills/jobkit-scheduled-run/SKILL.md and follow it exactly, with REPO as the checkout path.
   ```

   The prompt stays this short on purpose: the routine lives in the repo, so
   updates arrive with the code.
3. Tell them: it runs while the Claude app is open; if the app is closed at
   that time, it runs at the next launch.
4. Once the first run (step 7) has finished, offer a test run:
   run_scheduled_task(taskId="jobkit-daily-run"). It sees that run and only
   sends the summary. If it asks permission to run
   `uv run --directory REPO jobkit ...` commands or to read the skill file,
   they choose "always allow", so later runs do not stop and wait.

If that tool is not available (for example in a terminal session), give them
the prompt above and the time, and ask them to create the task in the Claude
app (Scheduled, then New task). Do not use cloud routines: they cannot reach
this computer's checkout.

**B. macOS LaunchAgent (fallback, or as well).** It runs `jobkit nightly`
even when the Claude app is closed, at `schedule.nightly_at` in
`config/filters.yaml`; the Mac must be awake. Show the dry run first:
`uv run jobkit schedule install --dry-run` (MCP: schedule_install()). Install
only on their explicit yes: `uv run jobkit schedule install` (MCP:
schedule_install(confirm=true)). `uv run jobkit schedule status` shows it
afterwards. If both A and B are set up, the Claude task sees that the night's
run already finished and only sends the summary.

**C. Linux**: no LaunchAgent. Use A, or give them a cron line that runs
`uv run --directory REPO jobkit nightly` for them to add themselves.

## Done

Run setup_status() once more and show the result. Then, in three lines:

- What runs daily, and when.
- Each day: open this folder in Claude and say "what's in the queue". The
  jobkit-daily skill takes it from there.
- Start a new session in this folder now so the jobkit tools load (approve
  the "jobkit" server when asked).
