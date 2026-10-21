# Email replies

Employers answer applications by email: an automatic receipt, a rejection, an
invitation to an assessment or an interview, an offer, or a recruiter's
question. If you have connected your email to Claude (for example with the
Gmail connector in the Claude app), Claude can find those emails and record
what they say against the right application. This is optional. Without a
connector, tell Claude what you heard back ("Beta rejected me", "Acme wants an
interview") and it records that with the `record_outcome` tool.

## What it can and can't do

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
  review queue, empty when nothing was recorded.
- `get_stats` and the digest's funnel count replies, and "no reply after N
  days" for ghosted applications.
