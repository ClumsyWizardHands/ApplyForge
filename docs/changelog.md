# What's new

ApplyForge changes often. This page lists the changes someone already using it
might notice, newest first. To update, see [Updating](setup.md#updating).

## 2026-10-23: fewer false holds

The first automatic runs held back good jobs and letters for the wrong
reasons. This round fixes that without loosening the fact check.

- **A letter that fails gets more drafts, each told exactly what failed.**
  Letters failed the fact check on everyday words such as "talk", "part",
  "point" and "work", read as claims of experience. The fact check was not
  loosened: `src/jobkit/validate/` is unchanged. A letter now gets up to 5
  drafts instead of 3 (`tailoring.cover_attempts` in `config/filters.yaml`).
  Every draft gets the full fact check, the claim judge and the authenticity
  review. Each new draft is written as a new letter and is told every problem
  of every earlier draft (its kind, the sentence and the rule in plain words),
  so it cannot bring back a problem an earlier draft had fixed. It is told not
  to write those sentences again. The letter prompt also now warns against
  what failed most:
  writing about the company you apply to as if you worked there ("our
  customers"), retelling a bullet in stronger words than it uses, using the
  posting's own names about you, and a close that promises something ("I'll
  bring sharp questions") or asks a question ("When can we talk?"). jobkit never deletes or edits sentences of a letter
  to make it pass. Jobs whose letter failed before this update are tailored
  once more on the next run, because they had only 3 drafts. See
  [When a letter fails](validators.md#when-a-letter-fails).
- **Every job is scored.** Each run scores every job that passes your
  filters; the fixed cap of 100 a stage is gone. To set a limit, add
  `triage.max_jobs_per_stage: N` to `config/filters.yaml` (setup asks).
  `jobkit triage --limit N` still works for one run.
- **Only deal-breakers hold a job back.** The new rule condition
  `blocking_flags: none` holds back a job only for a doubt about remote work or
  the location, or for pay that looks unrealistic. A missing pay range, a wide
  one, several roles in one posting or an unclear base-vs-OTE basis no longer
  does. A low ceiling (`underpriced`) still holds a job back unless you add it
  to `non_blocking_flags`. Two more conditions: `location_known: true` (a job whose location is
  unclear waits for you) and `employer_posting: true` (a feed job with no
  employer form waits for you, marked "no employer form found: apply on the
  employer's site (found via <link>)", the link being the feed's page, with
  tracking parameters such as `utm_source` removed).
  The flag lists are `auto_approve` in `config/filters.yaml`. See
  [Your bar](how-it-works.md#your-bar-automatic-approval).
- **A place in the job title wins.** "Account Manager - Denver, CO" filed
  under Boston, or "Chief of Staff | Colombia" filed under Cambridge, MA, is
  filtered by the place in the title. A region in the title ("LATAM",
  "EMEA") is often the territory covered, so it wins only when the feed names
  no place: "Channel Account Executive, LATAM" in Miami, FL stays a Miami job.
  See [Locations](how-it-works.md#locations).
- **Repost sites are kept out.** Sites such as Jobgether that repost other
  companies' jobs ("Our partner is looking for…") are recognised from a
  built-in list and from their wording, and the digest names each one it kept
  out. Their jobs still waiting in the queue are taken out too, and a job from
  one is never approved automatically. Discovery and board lookup also skip
  the companies on your blocklist.
  See [Repost sites](how-it-works.md#repost-sites).
- **An expired Claude login says so.** When the Claude login expires, the run
  stops calling Claude at once, and the digest, `jobkit schedule status` and
  the daily summary say "Claude login expired: run claude, then /login".
  `jobkit doctor` checks the login with one tiny call, and tells you to run
  `claude update` when Claude Code is out of date.

**What changes in an existing setup.** Scoring has no cap now, so a run after
a long gap may make more model calls than before. The rest is opt-in: a
`config/autoapprove.yaml` you wrote is never changed, so to use the new policy,
give your clears-your-bar rule `sanity_checked: true, blocking_flags: none,
location_known: true, employer_posting: true` in place of
`sanity_flags: none` (see `config/autoapprove.example.yaml`). Title locations,
repost sites and the login check apply to everyone. The 5 letter drafts apply to everyone; add
`tailoring: {cover_attempts: 3}` to `config/filters.yaml` to keep 3.

## 2026-10-22: automatic by default

ApplyForge now runs your search for you: every day it finds and scores jobs,
approves the ones that clear your bar, writes and fills the applications and
opens them in a browser window. You review and click submit.

- **Your bar.** Setup asks whether jobs that clear your bar are approved
  without asking you, and at what fit score (75 suggested); on your yes it
  writes one rule in `config/autoapprove.yaml`. A `config/autoapprove.yaml`
  you wrote yourself is never replaced. There is no daily limit unless you set
  `max_auto_approve_per_day`. You can still approve or reject any job
  yourself. See [Your bar](how-it-works.md#your-bar-automatic-approval).
- **Forms open after the daily run** when `staging.open_after_daily_run: true`
  is in `config/filters.yaml`. `jobkit schedule status` ends with whether it
  is on and how many applications are ready.
- **Job-alert emails and LinkedIn messages.** The email check reads them too.
  Each job in an alert is looked up on the employer's own Greenhouse, Lever or
  Ashby board and, when it's there, scored the same day (`jobkit same-day`);
  recruiter messages show up in your summary, by company and role, never by
  the sender's name. New tools: `record_job_alerts`,
  `record_linkedin_message`. See [Email replies](email-responses.md#job-alert-emails).
- **Web search for alert jobs.** When a job from your alert emails isn't on
  a board ApplyForge reads, Claude searches the web for it. Setup asks (yes
  recommended, no limit unless you name one). The employer's Greenhouse,
  Lever or Ashby posting is added and scored the same day; a posting on
  Workday or a similar site is listed in your email-check summary and digest
  with its direct link ("apply on the employer's site: <link>"), and no job
  is added for it. LinkedIn pages are never opened, nothing is signed in to,
  and Claude stops at any CAPTCHA
  ([LinkedIn](why-no-linkedin.md#web-search-for-alert-jobs)). Tools:
  `alert_jobs_to_search`, `record_search_result`.
- The banner reads "Your job search, automated. All you do is click submit."

**What changes in an existing setup.** The email check, when it runs, now
includes job alerts and LinkedIn messages. Everything else is a key your files
don't have yet, and without it ApplyForge behaves as before: no rule is added
to your `autoapprove.yaml`, forms are not opened after the run, and nothing is
searched. To turn them on, copy the keys you want from the templates
(`config/autoapprove.example.yaml`, `config/filters.example.yaml`). `jobkit
doctor` reports automatic approval and form opening.

## 2026-10-22: a roadmap and a plainer README

- The README has a banner, a diagram of the daily loop, a sample morning and a
  [roadmap](roadmap.md).
- Issue forms for pain points and ideas.
  [Contributing](contributing.md#pain-points-and-ideas) says how they're used.
- `jobkit schedule` off macOS now says the LaunchAgent doesn't apply and points
  at cron or a systemd timer on Linux, and at a Claude app scheduled task or
  Task Scheduler on Windows.
- The `jobkit` command no longer stops at startup on Windows over a Unix-only
  file lock.
- `jobkit add-url` on a site it doesn't read says so in plainer words. Nothing
  else about it changed.

## 2026-10-21: any field, email replies, follow-up notes removed

ApplyForge now works for any job titles, records employer replies from your
email (read-only), and no longer drafts follow-up notes. You press submit, as
before.

### Scoring for fields other than sales

If you set up ApplyForge before it supported other fields, your
`config/filters.yaml` has no `prompt_set` line, and scoring still uses the
original sales examples. [How scoring works](how-it-works.md#scoring) explains
the switch. After switching, run `uv run jobkit rescore` (or ask Claude to
rescore) so the jobs already in your queue are scored again.

### Close old Claude sessions first

A Claude session left open during this update keeps the old tools, which still
expect follow-up notes. Its `rescore` fails with an error, and its
`mark_submitted` may fail with "no such table: followups" and record nothing.
Restart Claude and mark it again.

### Your database

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

### Other changes you may notice

On an existing search, including a sales one whose config has none of the new
settings:

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
