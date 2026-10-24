---
name: jobkit-setup
description: The guided ApplyForge (jobkit) setup interview for a new user, start to finish, for any kind of job. Checks the machine, reads the person's resume, confirms every corpus entry and resume skill with them (they set each metric's confidence and evidence), asks their job preferences (titles, career change, pay, places) and their bar for automatic approval, sets up where postings come from (employer boards or the job feeds), asks their application answers one question at a time, runs the first pass, schedules the daily run as a Claude app scheduled task (macOS LaunchAgent as fallback), and optionally connects the email they apply from (replies, job-alert emails and LinkedIn messages, read-only). Use when the person says "set up jobkit", "set up ApplyForge", "onboard me", "get me started", "continue jobkit setup", or on a first run (no data/career_corpus.yaml, or `jobkit setup-status` / `jobkit doctor` reports missing setup).
---

<!-- Maintainers: name jobkit MCP tools as calls, e.g. setup_status(), and CLI
commands in code spans, e.g. `uv run jobkit doctor`. tests/test_claude_skills.py
checks that each one exists. -->

# ApplyForge setup interview

ApplyForge runs the person's job search every day: it finds job postings,
checks and scores them, approves the ones that clear the bar the person sets
here, tailors a resume and cover letter from facts the person confirmed, and
fills each application form up to the last click and opens it for review. The
person reviews each form and clicks submit. The command, the
package and the MCP server are all called `jobkit`. This interview builds the
person's files. It takes about an hour, goes one question at a time, and can
stop and resume at any point: every answer is saved as it is given.

It works for any job titles the person chooses: nursing, teaching,
engineering, sales, operations and so on.

## How to talk

The interview is long, so keep the load on the person low. Make every turn
easy to answer:

- **One question per message.** Short. Say what you need and why in one line.
- **Show progress** at the top of each message: `Step 3 of 8 · bullets 6-10 of 24`.
- **Offer choices** when there are a few: `verified / estimate / none`.
- **Accept short replies** ("keep · verified · Q3 board deck") and
  "skip", "back", "pause". On "pause": say what is saved and that "continue
  jobkit setup" picks up here.
- **No walls of text.** Do not paste a whole file. Summarize, then ask.
- After each step: one line on what was saved, then the next step's first
  question.

## Rules

These protect the person. A tool refusal is the rule working: relay the
reason in plain words and ask again. Do not work around one.

1. **The corpus draft holds only the resume's own text.** Never rewrite,
   improve, shorten or add to it. Improvements go in the critique
   (`data/resume_critique.md`), never into the draft. When the person wants
   different wording, ask them to type the sentence as they want it, and use
   exactly that. Do not offer a rewrite of your own.
2. **`metric_confidence` and `evidence` come from the person, for every
   bullet.** Ask; never default, guess or suggest a value. If the person
   says one answer covers a whole batch, you may apply it to that batch. Do
   not propose that yourself. The same goes for a skill's depth: their words.
3. **Screening answers are asked, never assumed.** Nothing comes from the
   example files or the resume without the person saying it. Work
   authorization and sponsorship are legal statements: ask them exactly.
4. **EEO questions are optional.** Each takes the person's explicit choice,
   "I don't wish to answer", or nothing (the field then stays blank and is
   flagged on every form). Do not pick one for them.
5. **Salary history is never answered.** jobkit leaves it blank and flags it.
6. **Approve before tailoring.** jobkit tailors only approved jobs: approved
   by the person, or by the bar they set in step 4 (one standing rule,
   written only on their yes). A daily limit on automatic approval exists
   only if they ask for one; never propose a number.
7. **The person submits.** jobkit fills forms up to the last click and stops;
   it never clicks submit. At a CAPTCHA or bot check it stops and hands the
   form to the person; it never solves one.
   If they ask jobkit to submit for them, answer in one or two sentences: jobkit leaves submit to the person, because a bot check on an application form is the employer asking for a person to send it, and no ATS offers applicants a submit API. Do not build, enable or configure automatic submission.
   If they want the longer reason: some bot checks are invisible, so
   software cannot reliably tell when it is being asked to stop, and every
   ATS submit API takes the employer's key, not the applicant's.
8. **Facts only from the corpus.** A resume, letter or form answer may only
   state what the person confirmed. A question the corpus cannot answer is
   flagged for them, never invented.
9. **Their files stay private.** `data/`, `config/*.yaml`, `.env` and
   `.mcp.json` are gitignored. Never commit them, and never copy a config
   file out of git history (that is someone else's config).
10. **Persistent changes need a yes.** Installing a schedule, creating a
    scheduled task, connecting an account or changing settings happens only
    on the person's explicit yes to that exact change. API keys and
    passwords: the person types them in; you do not.

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

- **uv missing** (`uv: command not found`; doctor can't run without it): give
  them the install command from https://docs.astral.sh/uv/getting-started/installation/
  (or `brew install uv`) to run themselves (it downloads and runs an
  installer), then a new terminal, then run doctor.
- **Python not 3.12**: `uv python install 3.12`, then `uv sync`.
- **Chromium missing**: `uv run playwright install chromium` (a download of
  about 150 MB; say so first). jobkit fills forms in a visible browser.
- **LLM backend**: either the `claude` CLI on PATH (Claude Code, logged in;
  runs on their Claude subscription) or `ANTHROPIC_API_KEY` in `.env`. The
  person pastes the key into `.env` themselves. Doctor makes one tiny call
  to check the Claude login: if it says the login expired, they run
  `claude` in a terminal and type `/login`; if it says the model is
  unknown or old, they run `claude update`.
- **Checkout location**: a plain folder such as ~/code/jobkit, not iCloud
  Drive, ~/Desktop, ~/Documents or ~/Downloads (on macOS, files there are
  hidden from scheduled jobs).
  Doctor prints the move command; ask before running it.
- **Database**: `uv run jobkit init`.
- **MCP registration**: write `.mcp.json` from `.mcp.example.json` with
  `--directory` set to REPO (doctor prints the exact JSON), or run
  `claude mcp add --scope project jobkit -- uv run --directory REPO jobkit-mcp`.
  Tell the person the jobkit tools appear in the next session in this
  folder (approve the "jobkit" server when asked). Carry on with the CLI now.
- **Contact email** (a warning): offer to put `JOBKIT_CONTACT_EMAIL=` in `.env`
  for the polite job-board User-Agent. Optional.
- **Job feed keys** (optional and free; this can wait until step 5): Adzuna
  lists jobs in every industry. Its key comes from developer.adzuna.com and
  goes in `.env` as `ADZUNA_APP_ID=` and `ADZUNA_APP_KEY=`. USAJOBS lists US
  federal jobs. Its key comes from developer.usajobs.gov and goes in `.env`
  as `USAJOBS_API_KEY=` and `USAJOBS_USER_AGENT_EMAIL=` (the email they
  registered with). The person pastes the keys in themselves.

Re-run doctor until only config/data items (and warnings) remain.

## Step 2 of 8 — The resume

Ask: "Where is your resume? A PDF, Word (.docx), HTML or text file path
works, or paste the text here."

- File: ingest_resume(path="<path>") (CLI: `uv run jobkit setup ingest "<path>"`).
- Pasted: ingest_resume(text="<the pasted text>") (CLI: save it to
  `data/setup/tmp/pasted_resume.txt`, then
  `uv run jobkit setup ingest --text-file data/setup/tmp/pasted_resume.txt`).
- Refused because the file did not extract cleanly: ask them to paste the
  text. Do not guess at a garbled parse.
- Refused because no jobs were found: the message lists the headings it saw
  ("headings found: ..."). Show them and ask which one holds the jobs. Then
  ask the person to paste the resume text with that one heading changed to
  "Experience" (their edit; nothing else changes), and ingest the pasted
  text.
- A draft already holding confirmations is kept; only start over
  (ingest_resume(replace_draft=true), CLI `--replace-draft`) on their yes.

Then show the critique's top five items from `data/resume_critique.md`, one
line each. They are suggestions for the person's own resume, not changes to
the draft.

## Step 3 of 8 — Confirm the experience (the corpus)

Every later resume, letter and form answer draws only on what is confirmed
here. Run corpus_draft_status(batch_size=5) (CLI:
`uv run jobkit setup draft --batch 5`) to see what is left.

**3a. Identity, roles, education, summary lines, skills**, via
update_corpus_draft(changes) (CLI: `uv run jobkit setup update-draft <file>`).
One question at a time:

- Name, email, phone as they should print. Confirm what the resume shows.
- **Display location**: "Which city should your resume header show?" It is a
  positioning choice and can differ from where they live. Always ask.
- **Home city**: where they actually live. Always ask.
- Each role: company and title exactly as they should print, start and end
  month. If the end is missing or only a year, ask "Is this your current
  job?" Do not assume current.
- Each school and degree: keep, fix or drop.
- Each summary line from the resume: keep or drop.
- **Each skill the resume lists**, one at a time (corpus_draft_status lists
  the ones not yet decided): "Your resume lists Epic. Should applications
  claim it? If yes, how well do you know it, in a few words?" Send
  `resume_skills: [{name, depth}]` with their words as the depth, or
  `{name, drop: true}`. This matters most for tools and systems: the fact
  check refuses a letter that names a tool the person has not confirmed,
  and this is the main place to confirm one.
- **Each licence, certification or project line** (corpus_draft_status lists
  them under the resume's own heading), one at a time, in the resume's words:
  "Your resume lists 'Registered Nurse, Ohio Board of Nursing, active'.
  Should applications claim it? Anything to add, in a few words?" Send
  `resume_skills: [{name: <the line as written>, depth: <their words>}]`, or
  `{name, drop: true}`. A kept line is what scoring sees: a licence left out
  here counts as missing on every job that asks for it.

**3b. Bullets, five at a time**, via confirm_bullets(decisions) (CLI:
`uv run jobkit setup confirm <file>`). One bullet per message, showing the
person's own bullet:

```
Step 3 of 8 · bullet 7 of 24 · <title>, <company>
"<the bullet exactly as the resume has it>"
Keep, edit or drop?  Then: how solid is the number?
  verified = you can point at a source · estimate = you'd say "roughly" · none = no figure
And the evidence, one line: where could you show it? (a report, a review, a person)
```

- `edit`: their own sentence, exactly as typed, as `text_long`.
- A bullet that states a number cannot be `none` and needs evidence.
- A bullet with no outcome: ask once, "Was there a number? Roughly how much,
  over what period?" (patients per shift, students per class, tickets a
  week, deals a quarter). If they give one, the bullet changes only by an
  edit in their words. "skip" is fine.
- After five, send the batch. It is all or nothing: on a refusal, relay the
  reason, fix just that bullet with the person, send again.

**3c. Voice samples (optional)**: "Paste 2 or 3 things you wrote yourself:
an email, a doc intro. It keeps cover letters sounding like you." Store them
as written via update_corpus_draft(changes) (`voice_samples`).

**3d. Promote**: promote_corpus() (CLI: `uv run jobkit setup promote`). It
reports the bullet count and how many of the bullets that state a figure are
verified. Under 30 bullets, or under 40% of those figures verified, is a thin
corpus: say so plainly, and that the daily questions grow it over time.

## Step 4 of 8 — What they want (preferences)

Ask against their corpus ("Your last two roles managed teams. Keep managing,
or go back to individual work?"), one question at a time. Keep their exact
words for each `said` field.

1. **Direction**: which kinds of role? Each kind is a track. For each track,
   ask "Is this a change of field?" If yes, set `pivot: true` and propose
   `min_score` 45 or 50, with the reason in one line: the score measures how
   well the corpus meets a job's must-haves, and a career changer's corpus
   rarely meets them on paper, so the usual cut-off of 65 would hide most of
   the jobs worth a look. They pick the number.
2. **Title words**: "What word does every title you want contain?"
   (Nurse, Engineer, Teacher) and "Which words narrow it?" (ICU, Backend,
   Instructional). These become `title_words`.
3. **Typical titles**: propose 10 to 15 real titles for the track (for
   example "Staff Nurse, ICU", "Clinical Nurse II", "Sr. Software Engineer").
   They keep or drop each one. Kept titles go in `should_match`, dropped ones
   in `should_not_match`; run_intake checks the track against both and says
   which one fails.
4. **Emphasis**: "For this kind of role, what should lead your resume?" One
   line in their words, saved as `emphasis` (for example "patient outcomes
   and charge nurse shifts first").
5. **Titles and schedules they would refuse**, in their words: night shift,
   PRN, contract, travel, commission-only. These go in `exclude_titles`.
6. Seniority floor: any / no internships / experienced / senior.
7. **Places**: remote only, hybrid, or on-site is fine? Which city, and which
   state? A state alone is not a place: if they say "anywhere in Ohio", ask
   which cities. Max office days a week and max commute; minutes from home to
   each place. Then propose nearby towns within their commute, one at a time:
   keep (with minutes) or drop. Kept towns go in `nearby`. A kept town in
   another state goes in with its state (`"Covington, KY": 15`); a bare town
   gets their home state.
8. Would they relocate? Where?
9. **Pay floor**: "Do you think of pay as a salary or an hourly rate?" For
   hourly, ask the rate and the hours a week (`unit: hourly`,
   `hours_per_week`); jobkit converts it to a yearly floor and shows the
   arithmetic in the config. Always ask the default floor (titles outside
   every track); per-track floors are optional. Only for sales titles: is
   the floor base pay, or base plus commission (OTE)?
10. Industries they want, and industries they refuse.
11. Companies they would never work for.
12. **Federal jobs**: "Would you consider US federal government jobs?" A yes
    turns on USAJOBS (`federal: {wanted: true, said}`); it needs the free
    key from step 1.
13. Writing style for estimated numbers: `hedged` (estimates keep "about") or
    `confident` (stated plainly, exactly as written).
14. **Their bar.** "Should jobs that clear your bar be approved without asking
    you? Each one gets its resume, letter and filled form, and you still
    review it and click submit. I suggest yes, at a fit score of 75." One
    line on what the score is: how well their confirmed experience meets the
    job's must-haves, 0 to 100. Then one line on what still waits for them
    in the queue: jobs below the bar; jobs with a deal-breaker warning (a
    doubt about remote or location, or pay that looks unrealistic); jobs
    whose remote or location status is unclear; and job feed postings with
    no form jobkit can fill (they apply on the employer's site). A missing
    pay range or several roles in one posting does not hold a job back.
    They pick the number. Send `auto_approve: {enabled: true, min_score: 75, said}`, or
    `{enabled: false, said}` on a no. The rule intake writes carries those
    checks (`blocking_flags: none`, `location_known: true`,
    `employer_posting: true`); the flag lists are in `config/filters.yaml`
    under `auto_approve`.
15. **A daily limit (optional).** Only on a yes to 14, ask once and plainly:
    "Do you want a cap on how many jobs are approved in a day? By default
    there is none." Do not suggest a number or recommend a cap. Only when
    they give a number, add `per_day` to `auto_approve`; otherwise leave it
    out.
16. **Opening the forms.** "When the daily run finishes, should the filled
    applications open in a browser window for you to review?" Yes is the
    default. Send `open_forms: {after_daily_run: true, said}` (or `false`).
17. **Searching for alert jobs.** "When a job from your alert emails isn't
    on a board I can read, should I search the web for it?" I suggest yes.
    One line on what it does: Claude follows public pages to the employer's
    own posting; a job on Workday or a similar site is listed with its
    direct link so they can apply there; LinkedIn pages are skipped and
    nothing is signed into. Send `alert_search: {enabled: true, said}`, or
    `{enabled: false, said}` on a no. With no objection, yes is the answer.
18. **A search limit (optional).** Only on a yes to 17, ask once: "Is there
    a limit on how many searches per email check you want (no limit is
    fine; recency already limits it)?" No limit is the default. Only when
    they give a number, add `per_check` to `alert_search`; otherwise leave
    it out.
19. **A scoring limit (optional).** Ask once: "Every job that passes your
    filters is scored in each run. Do you want a limit on how many jobs are
    scored per run? By default there is none." Do not suggest a number. Only
    when they give one, send `scoring_limit: {per_stage: <their number>, said}`;
    otherwise leave it out.

Then run_intake(answers) (CLI: `uv run jobkit setup intake <file>`). The tool
description lists every field. A yes to 14 writes one rule in
`config/autoapprove.yaml`; if that file holds rules the person wrote, it is
kept as it is, even with replace_existing, and the output names the rule to
add (or, on a no, the rule to delete). On a refusal (for example a kept title their
seniority floor would reject), ask the conflicting question again. Do not
edit a config file the interview did not write without their yes
(replace_existing=true, CLI `--replace-existing`; the old copy is backed up).

Tell them in one line how scoring is set: intake writes `prompt_set: sales`
when every track is a software sales title (account executive, SDR/BDR,
sales manager; not retail, insurance or device sales) and no track is marked
as a change of field or given an emphasis, and `prompt_set: general` (scoring examples from many
fields) otherwise.
Also say in one line that each cover letter gets up to 5 drafts, every one
fully checked and told every failure of every earlier draft of the run (its kind,
sentence and rule) (`tailoring.cover_attempts`
in `config/filters.yaml` changes the number). No question for it.

## Step 5 of 8 — Where postings come from

First say this in two or three lines, plainly:

- jobkit reads employer job boards directly only on Greenhouse, Lever and
  Ashby. Those are mostly tech companies and startups.
- Hospitals, school districts, government and most large employers use
  Workday, iCIMS, Taleo or SuccessFactors. jobkit finds those jobs through
  the feeds (Adzuna for any industry, USAJOBS for federal jobs). A pasted link
  on those systems gets the system's name, and the person applies on the
  employer's site; jobkit doesn't track that application yet. LinkedIn
  discovery helps when the employer also posts on Greenhouse, Lever or Ashby.
  Fuller support for the other systems is on the roadmap
  (docs/roadmap.md).

Then ask: "Name companies you'd love to work at." (tier 1), then "Others worth
watching?" (tier 2). You may suggest 20 to 40 more from their field, places
and size; they approve the list.

Run seed_targets(companies) (CLI:
`uv run jobkit setup targets "Name 1" "Name 2" --tier 1`, or
`uv run jobkit setup targets --file <file>` for a list with tiers). It only
uses the public job-board endpoints. Report: boards found, unverified, not
found. Ask once whether they have a careers or job-board link for the ones
not found, and retry those with `{name, url}`. Do not chase each one.

**If their employers are not on those boards** (or they name none): ask them
to say in their own words that they will rely on the feeds, and record it
with seed_targets(feed_only_said="<their words>") (CLI:
`uv run jobkit setup targets --feed-only "<their words>"`). No company is
needed, and the step counts as done. Then:

- Check that the Adzuna key (and the USAJOBS key, if they want federal jobs)
  is in `.env`. Without a key that feed is skipped, and doctor warns.
- A posting link found anywhere can be added with add_job_url(url). For a
  Greenhouse, Lever or Ashby link it queues the job and scores it in the
  background, and the job is in the queue once scored; for another system it
  names the system, and they apply on the employer's site.
- LinkedIn discovery is off by default. When on, it runs a few logged-out
  LinkedIn searches a day for their titles. It adds a job only when the
  employer also posts it on Greenhouse, Lever or Ashby; otherwise it notes
  the company name. Turning it on is `enabled: true` under
  `feeds.linkedin_guest` in `config/filters.yaml`, on their yes.

## Step 6 of 8 — Application answers (the answer bank)

Run answer_bank_questions() (CLI: `uv run jobkit setup questions`) and ask
each question in its order, one per message. Save every few answers with
build_answer_bank(answers) (CLI: `uv run jobkit setup answers <file>`); answers
can come over several calls.

- Identity: you may offer what they told you in step 3a and ask "Same for
  applications?" The city here is the real one, not the display location.
- Work authorization and sponsorship: ask exactly as written.
- Comp expectation: give a number, their own wording, or leave it blank
  every time. A number below their default floor is refused. For hourly pay,
  use their own wording (`policy: text`, `unit: hourly`) or leave it blank;
  the tool refuses a number or a floor for hourly pay.
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

(MCP: run_nightly().) Do step 8 while it runs. When it finishes, show what
it did: the jobs their bar approved (written for and filled; open_queue()
opens them for review), and the queue below their bar: get_queue() if the
MCP tools are connected, else the "Review queue" section of
`out/digest/<today>.md`. Do not approve anything yourself: explain that they
approve or reject the rest by number ("approve 1, 3 · reject 2"), and that
tailoring and form filling happen only for approved jobs.

## Step 8 of 8 — Daily runs

The daily run is `jobkit nightly`: new postings, scoring, approving the jobs
that clear their bar, tailoring approved jobs, filling approved forms (the
person submits), marking applications with no reply after 30 days as
ghosted, and the digest. The scheduled task then checks their email (D
below) and, if they said yes in step 4, opens the filled forms for review.

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

**C. Not on a Mac**: use A, or give them a cron line (or their system's
scheduler entry) that runs `uv run --directory REPO jobkit nightly`, for them
to add themselves.

**D. The email they apply from (optional).** Ask: "Want jobkit to watch
your email for replies from employers, job-alert emails and LinkedIn
messages? It reads the inbox you apply from through Claude's email
connector." On a yes, they connect it themselves in the Claude app's
connector settings (Gmail, or another email connector), for the account
they put on applications. Tell them what it does: the daily "check my email"
and the scheduled run search that mailbox for replies to the applications
they marked submitted, and record each one as an outcome (acknowledged,
rejected, assessment, interview, offer, or a recruiter's question) with a
one-line summary and a link to the message. Each job in a LinkedIn or job
site alert is looked up on the employer's own Greenhouse, Lever or Ashby
board, scored the same day and approved if it clears their bar; LinkedIn
links are never opened. A LinkedIn message shows up in their summary as who,
which company and one line. It only reads. It does not send, reply, forward,
delete, archive, label or mark anything read, and it keeps no email text
beyond that one line. Without it, they tell jobkit about replies
themselves.

## Done

Run setup_status() once more and show the result. Then, in three lines:

- What runs daily, and when.
- Each day: open this folder in Claude and say "what's in the queue". The
  jobkit-daily skill takes it from there.
- Start a new session in this folder now so the jobkit tools load (approve
  the "jobkit" server when asked).
