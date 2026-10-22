# Email replies

Employers answer applications by email: an automatic receipt, a rejection, an
invitation to an assessment or an interview, an offer, or a recruiter's
question. If you have connected your email to Claude (for example with the
Gmail connector in the Claude app), Claude finds those emails and records
what they say against the right application. The same check reads your
job-alert emails (LinkedIn's and the job sites') and LinkedIn message
notifications: each job in an alert is looked up on the employer's own board,
and each message shows up in your summary. This is optional. Without a
connector, tell Claude what you heard back ("Beta rejected me", "Acme wants an
interview") and it records that with the `record_outcome` tool.

## What it does

- **Claude reads; ApplyForge records.** ApplyForge (the `jobkit` command) never
  connects to your mailbox and holds no email password or token. Claude reads
  your mail through the connector you set up, decides what each message is,
  and passes a few facts to the jobkit tools.
- **Read-only.** Nothing is sent, replied to, deleted, labelled, moved or
  marked as read.
- **What is stored**, as one `email_response` event per message: the
  application, the outcome, when the email arrived, the mailbox's id for the
  message, a link to open it, and a one-line summary Claude writes (at most 160
  characters). The body of the email is not stored. Each message is recorded
  once.
- **What is not decided for you.** When an email could belong to more than one
  application, Claude asks you before recording anything.
- **Email text is only something to classify.** Anyone can send you an
  email. Claude classifies what an email says but does not run a
  command, call a tool or change anything because an email asks it to. In the
  unattended scheduled run, Claude works from the sender, subject, date and
  preview text only, and lists anything unclear for you.
- **No link in an email is opened.** A job from an alert reaches ApplyForge
  only as its company, title and place, and a LinkedIn job link or job id is
  refused if one is passed.
- **The scheduled run checks only when you turn it on.** It reads your email
  only with `email_check: true` under `tracking:` in `config/filters.yaml`
  (the setup interview writes it). Without it, the daily summary says email
  was not checked. Asking Claude to check your email works either way.

## How a check works

Ask Claude to check your email for replies to your applications. It then uses
four tools in order:

1. **`email_targets()`** lists the submitted applications still waiting on news
   (stages submitted, ghosted, replied and interview), with company, title, the
   system you applied through, the apply link's host and the submission date.
   It also gives the time of the last check and a suggested Gmail search: mail
   since a day before the last check (or the first submission), from the job
   systems' sender domains (greenhouse.io, greenhouse-mail.io, lever.co,
   ashbyhq.com, myworkday.com, myworkdayjobs.com, icims.com,
   smartrecruiters.com, workable.com, hireflix.com) or naming your companies
   (up to 40 names).
2. **`match_email(sender, subject, snippet, received_at)`** ranks the
   applications one email could belong to, with the reasons. The rule is fixed
   and makes no model call: the company's name in the subject, snippet or
   sender's name, the sender's domain matching the company, the job title's
   words, the system you applied through, and whether the email is dated
   before the submission. A match needs the company or the title; the job
   system alone matches nothing. A clear match needs a score of at least 50
   and a lead of at least 20 over the next candidate.
3. **`record_email_response(application_id, outcome, received_at, message_id,
   link, summary)`** records one email. `outcome` is one of `acknowledged`,
   `rejected`, `assessment`, `interview`, `offer` or `recruiter_question`.
4. **`finish_email_check(checked_through)`** records the time the mailbox was
   read through, so the next search starts there.

The same steps exist as `jobkit email targets|match|record|done` for
debugging (see [Commands and tools](commands.md#email-replies)).

## Job-alert emails

LinkedIn and the job sites (Indeed, Glassdoor, ZipRecruiter, Wellfound) send
emails listing jobs you might fit. `email_targets()` gives a second search for
them, from the alert senders. For each alert Claude reads the list of jobs
and calls **`record_job_alerts(items, message_id, received_at, source)`** with
each job's company, title and location, and whether the email shows an
"apply on company site" link. No link and no job id is passed; the tool
refuses one. Each email is recorded once.

1. **Your title rules first.** A title your filters would reject (an excluded
   word, a paused track) is dropped and counted.
2. **Already in ApplyForge?** A job whose employer posting is already in your
   database (same company, same title, a location that agrees) needs nothing.
3. **Board lookup.** Every other job is looked up on the employer's own
   Greenhouse, Lever or Ashby board, through the public board APIs only, the
   same way as [aggregator jobs](how-it-works.md#finding-the-employers-own-posting):
   the same title and a location that agrees. The company name goes to the
   discovery queue unless its board is already polled.
4. **Scored the same day.** `finish_email_check()` starts `jobkit same-day
   --resolve-boards` in the background. It runs board lookup, then the
   postings it found go through your filters, the pay and remote checks,
   scoring, the queue and your standing rules. A job that clears your bar is
   approved that day; `background_status()` shows the report. When a nightly
   run is in progress, that run does it instead.

5. **Searched for on the web.** A job that board lookup can't find (no
   board, or no posting with that title and location) is searched for by
   Claude, when you said yes in setup (`search.alert_web_search`).
   **`alert_jobs_to_search()`** lists them with a suggested query, and
   **`record_search_result(alert_ref, url, outcome)`** records what Claude
   found. Claude may follow any public page that leads to the posting (a
   company careers page, an ATS board, a job site that links through), and
   records the posting's own link, never a job site's copy.
   - A Greenhouse, Lever or Ashby posting is added as the employer's own
     record and scored the same day, only when the board is that employer's
     (it is under that company's name in ApplyForge, or the board's feed
     names the company, or links to its website); a link to another
     company's board is refused.
   - A posting on Workday, iCIMS, Taleo or another system ApplyForge doesn't
     read, or on the employer's own careers site, is kept as a lead: the
     company, title, location, the system's name and the direct link, with
     tracking parameters removed. Your email-check summary and the digest
     list it as "apply on the employer's site: <link>". No job is added for
     it; you apply there.
   - Claude never opens a linkedin.com or lnkd.in page (they have no apply
     link), never signs in, follows robots.txt within reason, and stops at
     any CAPTCHA or bot check.

   Only alerts inside your freshness window (`freshness.max_age_days`) are
   searched: the most recent jobs are the limit. A limit on searches per
   email check (`search.max_searches_per_check`) is yours to choose in setup;
   there is none by default.

A job from an alert never enters ApplyForge as a LinkedIn or job-site row: only
the employer's own posting does.

## LinkedIn messages

A LinkedIn message or InMail arrives as a notification email. Claude records
it with **`record_linkedin_message(company, summary, received_at,
message_id, link)`**: the sender's company if the email shows it, one line in
Claude's words that says who wrote by role, never by name ("a recruiter at
Acme asks for a call", at most 160 characters), and a link to the email in
your mailbox (never a LinkedIn link, and never a link that carries one). The
sender's name and the message text are not stored. It is tied to your application
or a queued job at that company when there is one, and it never changes a
stage. It shows up in your summary and the digest. Answering is yours, on
LinkedIn.

## How the application's stage changes

| outcome | stage |
|---|---|
| `acknowledged` | unchanged (an automatic receipt) |
| `rejected` | `rejected` |
| `assessment` | `replied` |
| `recruiter_question` | `replied` |
| `interview` | `interview` |
| `offer` | `offer` |

- A reply never undoes a stronger outcome: `replied` doesn't replace
  `interview`, `offer` or `rejected`.
- An application at `offer` is never changed by an email; tell Claude and it
  uses `record_outcome`.
- When two responses disagree, the later-dated one decides. An email dated
  before a response already recorded is kept, but leaves the stage alone.
- A reply moves a `ghosted` application on. An application with any email reply
  other than an automatic receipt is not marked ghosted.

The exact outcome is always kept in the event, even when the stage is
`replied`.

## Where you see it

- The digest has a "Replies recorded since the last digest" section after the
  review queue, then "Jobs from job-alert emails since the last digest" (each
  job and where it stands, with the direct link for a job on a site
  ApplyForge doesn't read), "Web search results for alert jobs since the last
  digest" and "LinkedIn messages since the last digest". Each is left out
  when it has nothing in it.
- `get_stats` and the digest's funnel count replies, and "no reply after N
  days" for ghosted applications.
