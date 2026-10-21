# ApplyForge

ApplyForge finds job postings that match the roles you want, scores each one
against your experience, writes a resume and cover letter for each job you
approve, and fills in the application form. You read the filled form and press
submit yourself.

It runs on your own computer, and you use it by talking to Claude. The command
and the Python package are called `jobkit`, so that is the name you will see in
commands, file paths and the Claude tools.

## What it does

- **Finds jobs.** It reads the public job boards of employers you name on
  Greenhouse, Lever and Ashby, plus job feeds: Adzuna (all industries),
  USAJOBS (US federal) and several remote-job boards. If you turn it on, it
  also runs a few logged-out LinkedIn searches a day to find company names.
- **Filters them.** By title, place, commute, pay floor and remote policy. It
  logs the reason for every job it drops.
- **Scores the rest.** A fit score from 0 to 100 against the experience you
  confirmed, with the gaps listed. It also checks whether the remote claim is
  accurate and the pay is realistic.
- **Writes the application.** A one-page resume and a cover letter for each job
  you approve, built only from facts you confirmed, and checked against those
  facts before you see them.
- **Fills the form.** It opens the application in a browser window you can see,
  fills what it can from your saved answers, and marks what it left for you.
- **Keeps track.** What you applied to, what came back, and which applications
  have had no reply for 30 days. This covers jobs ApplyForge found or that you
  pasted from Greenhouse, Lever or Ashby.

## Who it suits

ApplyForge works for any job titles you choose: software, nursing, teaching,
sales, operations, and so on. During setup you describe the roles you want in
your own words, and it builds your search from your answers. If you are
changing fields, say so; those jobs are then scored as a career change, with a
lower bar to reach your queue if you want one.

How well it works for you depends mostly on where your employers post jobs:

- **Strongest for employers on Greenhouse, Lever or Ashby.** These are mostly
  tech companies and startups. For them ApplyForge reads each posting
  directly, scores it, writes the application and fills the form.
- **Weaker for hospitals, schools, government and large employers.** Most of
  them use Workday, iCIMS, Taleo or SuccessFactors, which ApplyForge can't read
  or fill. For those jobs:
  - paste the job link to Claude. ApplyForge names the system when it
    recognizes the link, and you apply on the employer's site. It doesn't add
    the job, so that application isn't tracked, matched to email replies or
    marked ghosted;
  - turn on Adzuna, which covers every industry and can search near your town
    (it needs a free key);
  - use USAJOBS for US federal jobs (also a free key);
  - turn on LinkedIn discovery. It helps only when the employer also posts on
    Greenhouse, Lever or Ashby.

  Feeds such as Adzuna often send only a snippet of a posting. A job with too
  little description to score is not scored; the digest lists it with its
  link so you can open it.

Planned next: a way to paste a job description from any site so that it is
scored, tailored and tracked like the rest, and readers for Workable and
SmartRecruiters job boards.

Other things to know:

- It is built for job searches in the United States.
- Pay floors are yearly. If you are paid by the hour, setup converts your rate.
- The results are only as good as your resume. A resume with few details
  gives thin results, and setup tells you when yours is thin.
- If you set up ApplyForge before it supported other fields, your
  `config/filters.yaml` has no `prompt_set` line, and scoring still uses the
  original sales examples. [How scoring works](docs/how-it-works.md#scoring)
  explains the switch. After switching, run `uv run jobkit rescore` (or ask
  Claude to rescore) so the jobs already in your queue are scored again.

## Get started

You need a Mac or Linux computer (Windows is untested), the Claude app or Claude
Code, and a Claude subscription or an Anthropic API key. Setup installs the rest.

In Claude Code, say:

> Clone https://github.com/ClumsyWizardHands/ApplyForge into ~/code/jobkit and set it up

Use an ordinary folder like that one. Avoid Desktop, Documents, Downloads and
iCloud Drive: macOS keeps scheduled jobs out of those folders. Claude then
runs the setup interview, which is included in the repository. If it doesn't
start, open a new Claude session in that folder and say "set up jobkit".

The interview asks one question at a time and takes about an hour:

1. Check your computer and install what is missing, each install on your yes.
2. Read your resume (PDF, Word, HTML, plain text, or pasted).
3. Go through your experience line by line. You keep, edit or drop each line,
   and say how solid each number is and where it could be shown.
4. Ask what you want: the kinds of role, places, remote or not, pay (yearly or
   hourly), what to avoid, and whether this is a change of field.
5. Ask which employers to watch, or note that you will rely on the job feeds.
6. Ask the questions application forms always ask: work authorization, notice
   period and so on. The demographic questions are optional.
7. Run a first search.
8. Set up the daily run as a scheduled task in the Claude app.

You can stop at any point; say "continue jobkit setup" to pick up where you
left off. [Setup](docs/setup.md) covers doing each step by hand.

## A normal day

At the time you chose, the daily run checks the boards and feeds, drops what
your filters rule out, and scores the rest. For each job you approved, it
writes a resume and letter and fills the application form in a browser window,
which closes when it is done. It marks applications with no reply after 30 days
as ghosted. Claude then writes a short summary. The scheduled task runs while
the Claude app is open; a run missed while the app is closed happens when you
next open it.

Then open the jobkit folder in Claude and say what you want:

- "what's in the queue": new jobs, numbered, with score, pay, remote check and
  gaps
- "approve 1, 4" or "reject 2": only approved jobs get a resume, a letter and a
  filled form
- "open my applications": the filled forms open for you to check and submit
- "I submitted Acme": logs the application
- "I heard back from Beta, they want an interview": records the reply
- "questions for me": a few questions a day about facts that would help with
  open jobs. They come from Claude's review of the applications it writes and
  from gaps that recur across the queue

## What it does and what you do

| ApplyForge | You |
|---|---|
| finds and scores jobs | approve or reject each one |
| writes the resume and letter from your confirmed facts | read them |
| fills the application form | fill anything left blank, check it, press submit |
| asks for facts it doesn't have | answer in your own words, and confirm before anything is saved |
| stops at a CAPTCHA, an ID or payment field, or an assessment | handle those yourself |
| records replies you report, or finds them in your email if you connect it | decide what to do next |

## Why you press submit

ApplyForge fills in each application and stops. You read it and press submit.
It leaves submit to you on purpose.

Many employers put a CAPTCHA or another bot check on their application forms.
That check is the employer asking for a person to send the application. Some
of these checks are invisible, so software can't reliably tell which forms
have one. The way to respect all of them is for a person to press submit
every time. When a form shows a challenge, ApplyForge
stops and leaves that form for you. It doesn't try to solve or get around one.

There is also no way for an applicant's software to submit through the job
systems themselves. Greenhouse, Lever, Ashby and SmartRecruiters accept
applications through their APIs only with a key issued to the employer.
Submitting for you would mean pressing the employer's button in a browser,
which is what those checks are there to stop.

My guess at where this goes: as more applications come from bots, more employers will put checks on their forms and screen out what looks mass-produced. An application you read and sent yourself should hold up better whichever way that goes. If employers ever offer an approved way for software to apply on your behalf, that's consent, and ApplyForge could use it.

## Replies in your email (optional)

If you have connected your email to Claude (for example with the Gmail
connector), Claude can look through your inbox for replies to your
applications and record them: an automatic receipt, a rejection, an
assessment, an interview invitation, an offer, or a recruiter's question.
Claude only reads your mail; it doesn't send, delete, label or move anything,
and ApplyForge itself never connects to your mailbox. For each reply it stores
the outcome, the date, the message's id and link, and a one-line summary in
Claude's words. The text of the email is not stored. The scheduled daily run
checks too when `tracking.email_check` is true in `config/filters.yaml` (the
setup interview sets it). Without a connector, tell Claude what you heard
back and it records that. [Email replies](docs/email-responses.md) has the
details.

## Where your data is kept

Your resume facts, form answers, preferences and job database are stored in
your checkout, in files that git ignores. `git pull` leaves them alone, and they are
not committed. The repository holds only the program and the templates.

What leaves your computer:

- requests to public job boards and job feeds. Each request names a contact
  email address (`JOBKIT_CONTACT_EMAIL` in `.env`, or else the email from your
  application answers), as those services ask automated clients to do;
- what you type into application forms, and the files you attach. Some forms
  read an attached resume as soon as it is uploaded, before you submit;
- what Claude reads: the setup interview, and the postings and parts of your
  experience that ApplyForge sends it to score jobs and write applications;
- with LinkedIn discovery on, a few logged-out searches a day, which also
  include that contact email address;
- if you connect your email, the sender, subject, date and preview of emails
  about your applications, which Claude reads to record replies. When you ask
  Claude to check your email, it may open a whole message if the preview
  isn't enough. The scheduled daily run reads only the sender, subject, date
  and preview.

## LinkedIn

LinkedIn has no job API for applicants, and its terms forbid automated access.
ApplyForge reads employers' own job boards instead. An optional mode, off by
default, runs a few logged-out LinkedIn searches a day to find company names,
then looks for those jobs on each company's Greenhouse, Lever or Ashby board.
[LinkedIn](docs/why-no-linkedin.md) explains what it does and the risk you take
on if you turn it on.

## Costs

- **Claude.** ApplyForge calls Claude to score jobs (a small, inexpensive
  model) and to write and check applications (a larger model). With a Claude
  subscription, these calls go through Claude Code and count toward your
  plan's usage. With an API key in `.env`, you pay per call. Writing and
  checking one application costs much more than scoring one job; the design
  estimate is roughly $0.15 to $0.40 of API usage per tailored application.
- **Job feeds.** Adzuna and USAJOBS need free keys. The other feeds and the
  company boards need none.

## Updating

Close every open Claude session in the jobkit folder before you update, then
run:

```bash
cd ~/code/jobkit && git pull && uv sync && uv run jobkit doctor
```

Then start a new Claude session so the jobkit tools reload. If you are
updating from a version that drafted follow-up notes, read
[Updating](docs/setup.md#updating) first.

## More detail

- [Setup](docs/setup.md): installing by hand, `jobkit doctor`, your files,
  keys, the schedule and the Claude tools registration
- [How it works](docs/how-it-works.md): filters, role tracks, scoring,
  tailoring, questions, the nightly run and tracking
- [Fact checks](docs/validators.md): how every resume, letter and answer is
  checked against your confirmed facts
- [Form filling](docs/form-filling.md): what gets filled, what is left for you,
  and when filling stops
- [Commands and tools](docs/commands.md): every `jobkit` command and Claude
  tool
- [Email replies](docs/email-responses.md)
- [LinkedIn](docs/why-no-linkedin.md)
- [Contributing](docs/contributing.md): tests, the build spec and the project
  rules

## Support

Your support means a lot: https://ko-fi.com/clumsywizardhands

And to those of you coming to this page experiencing similar struggles to mine, may you find your way through the dark.

Live long and prosper, friends.
