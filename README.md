<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/banner-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="assets/banner-light.svg">
    <img alt="ApplyForge: your job search, run from Claude. You press submit." src="assets/banner-light.svg" width="100%">
  </picture>
</p>

<p align="center">
  <a href="https://github.com/ClumsyWizardHands/ApplyForge/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/ClumsyWizardHands/ApplyForge/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Runs in Claude Code" src="https://img.shields.io/badge/runs_in-Claude_Code-d97757?logo=claude&logoColor=white">
  <img alt="Python 3.12" src="https://img.shields.io/badge/python-3.12-3776ab?logo=python&logoColor=white">
  <img alt="Tested on my Mac" src="https://img.shields.io/badge/tested_on-my_Mac-555555?logo=apple&logoColor=white">
  <br>
  <img alt="Your data stays local" src="https://img.shields.io/badge/data-stays_local-2da44e">
  <img alt="Status: evolving" src="https://img.shields.io/badge/status-evolving-c2410c">
  <a href="https://ko-fi.com/clumsywizardhands"><img alt="Support on Ko-fi" src="https://img.shields.io/badge/Ko--fi-support_the_project-ff5e5b?logo=kofi&logoColor=white"></a>
</p>

I built ApplyForge for my own job search. It finds job postings that match the
roles you want, scores each one against your experience, writes a resume and
cover letter for each job you approve, and fills in the application form. You
read the filled form and press submit yourself.

It runs on your own computer, and you use it by talking to Claude. The command
and the Python package are called `jobkit`, so that is the name you will see in
commands, file paths and the Claude tools.

ApplyForge is an evolving project. I keep improving it as my time and
resources allow, and what people run into decides what I work on next. The
[roadmap](#roadmap) shows where it's headed. If something trips you up,
[open an issue](https://github.com/ClumsyWizardHands/ApplyForge/issues).

## What it does

<table>
  <tr>
    <td width="50%" valign="top">
      <h3>🔎 Finds jobs</h3>
      The public Greenhouse, Lever and Ashby boards of employers you name, plus Adzuna (every industry), USAJOBS (US federal) and remote-job feeds. Optional logged-out LinkedIn searches find more company names.
    </td>
    <td width="50%" valign="top">
      <h3>📊 Scores them</h3>
      Filters by title, place, commute, pay floor and remote policy, and logs why each job was dropped. Scores the rest 0 to 100 against your experience, lists the gaps, and checks the remote claim and the pay.
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <h3>✍️ Writes from your facts</h3>
      A one-page resume and a cover letter for each job you approve, built only from facts you confirmed and checked against them before you see anything.
    </td>
    <td width="50%" valign="top">
      <h3>📝 Fills the form</h3>
      In a browser window you can see, from your saved answers. It marks what it left for you and stops at a CAPTCHA.
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <h3>✅ You press submit</h3>
      You read each application and send it yourself.
    </td>
    <td width="50%" valign="top">
      <h3>📬 Keeps track</h3>
      What you applied to and what came back, from your email if you connect it (read-only) or from what you tell Claude. Thirty days of silence is marked ghosted.
    </td>
  </tr>
</table>

## Who it suits

ApplyForge works for any job titles you choose: software, nursing, teaching,
sales, operations, and so on. During setup you describe the roles you want in
your own words, and it builds your search from your answers. If you are
changing fields, say so; those jobs are then scored as a career change, with a
lower bar to reach your queue if you want one.

Where your employers post jobs decides how much ApplyForge does for each one:

- **Greenhouse, Lever and Ashby: the whole pipeline.** These are mostly tech
  companies and startups. ApplyForge reads each posting directly, scores it,
  writes the application, fills the form and tracks it.
- **Hospitals, schools, government and large employers: finding and
  scoring.** Most of them use Workday, iCIMS, Taleo or SuccessFactors.
  Reading Workday, iCIMS and Taleo through their documented feeds is an idea
  on the [roadmap](#roadmap). Today:
  - turn on Adzuna, which covers every industry and can search near your town
    (free key);
  - use USAJOBS for US federal jobs (also a free key);
  - turn on LinkedIn discovery, which finds employers that also post on
    Greenhouse, Lever or Ashby;
  - paste the job link to Claude. ApplyForge names the system when it
    recognizes the link, and you apply on the employer's site. Tracking those
    applications is on the roadmap.

  Feeds such as Adzuna often send only a snippet of a posting. A job with too
  little description to score goes in the digest with its link so you can
  open it.

On the roadmap: readers for Workable and SmartRecruiters job boards, and
pasting a job description from any site so it's scored, written for and tracked
like the rest.

A few more things:

- It's set up for US job searches today (pay floors, USAJOBS, the location
  table). Other countries are on the roadmap.
- Pay floors are yearly. If you are paid by the hour, setup converts your rate.
- The more detail your resume has, the better the applications. Setup tells
  you where yours is thin, and the daily questions fill gaps over time.

## Get started

Bring the Claude app or Claude Code, and a Claude subscription or an Anthropic
API key. Setup installs the rest. I built and tested ApplyForge on my Mac, and
the tests also run on Linux. On Windows, give it a go and
[tell me](https://github.com/ClumsyWizardHands/ApplyForge/issues) what trips
you up.

In Claude Code, say:

> Clone https://github.com/ClumsyWizardHands/ApplyForge into ~/code/jobkit and set it up

Tip from my own setup: keep the checkout in a plain folder like `~/code`.
macOS hides Desktop, Documents, Downloads and iCloud Drive from scheduled jobs,
so the daily run can't reach files there.

Claude then runs the setup interview that ships with the repo. To start it
yourself, open a Claude session in that folder and say "set up jobkit". It
asks one question at a time, takes about an hour, and saves each answer as you
go:

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

Stop whenever you like; say "continue jobkit setup" to pick up where you left
off. [Setup](docs/setup.md) covers doing each step by hand.

## A normal day

```mermaid
flowchart TB
    subgraph find["1 · Find"]
        direction LR
        A["Boards<br/>and feeds"] --> B["Filter<br/>title, place, pay"]
        B --> C["Score 0-100<br/>vs your experience"]
        C --> D(["You approve"])
    end
    subgraph apply["2 · Apply"]
        direction LR
        E["Resume + letter<br/>from your facts"] --> F["Form filled<br/>where you see it"]
        F --> G(["You review<br/>and submit"])
        G --> H["Replies tracked<br/>(email optional)"]
    end
    find --> apply
    classDef you fill:#c2410c,stroke:#9a3412,stroke-width:2px,color:#ffffff
    class D,G you
    style find fill:none,stroke:#8c959f,stroke-dasharray:5 5
    style apply fill:none,stroke:#8c959f,stroke-dasharray:5 5
```

At the time you chose, the daily run checks the boards and feeds, drops what
your filters rule out, and scores the rest. For each job you approved, it
writes a resume and letter and fills the application form in a browser window,
which closes when it is done. It marks applications with no reply after 30 days
as ghosted, and Claude writes a short summary. The scheduled task runs while
the Claude app is open; if the app was closed, the run happens when you next
open it.

Then open the jobkit folder in Claude and say what you want. Here is a morning
(the companies are made up):

```text
You:     what's in the queue

Claude:  Last successful poll 2 hours ago (2026-10-22 06:01 UTC).
         2 forms filled and waiting for your review.

         1. Northwind Health · Operations Manager · $95–118k · remote ✓ · 88
            gaps: Lean certification
         2. Haulmark Logistics · Customer Success Lead · $82–96k · onsite/hybrid · 84
            gaps: Salesforce admin
         3. Larkspur Labs · Program Manager · $105–130k · remote ✓ · 79
            gaps: grant reporting
         4. Cedar & Pine · Office Lead · $58–64k · remote ✓ · 66 · gaps: none
            ⚠ remote-conflict: "must live within 30 miles of our Denver office"

         2 questions for you today. Approve, reject or skip any?
         (e.g. approve 1, 4 · reject 2)

You:     approve 1, 3 · reject 4, not really remote

Claude:  Approved 1 and 3; their resumes and letters are being written now.
         Rejected 4 (not really remote).

You:     open my applications

Claude:  Opened 2 filled forms in the browser. Check each one, then press submit.
```

Other things to say: "I submitted Acme" logs the application, "I heard back
from Beta, they want an interview" records the reply, and "questions for me"
brings up a few questions about facts that would help with open jobs.

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

ApplyForge fills in each application and stops. You read it and press submit,
every time.

Many employers put a CAPTCHA or another bot check on their application forms.
That check is the employer asking for a person to send the application. Some
of these checks are invisible, so software can't reliably tell which forms
have one. The way to respect all of them is for a person to press submit
every time. When a form shows a challenge, ApplyForge stops and leaves it for
you. It never solves or gets around one.

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
Email is read-only: Claude reads, ApplyForge records, and nothing is sent,
deleted, labelled or moved. ApplyForge itself never connects to your mailbox.
For each reply it stores the outcome, the date, the message's id and link, and
a one-line summary in Claude's words, never the text of the email. The
scheduled daily run checks too when `tracking.email_check` is true in
`config/filters.yaml` (the setup interview sets it). Without a connector, tell
Claude what you heard back and it records that.
[Email replies](docs/email-responses.md) has the details.

## Where your data is kept

Your resume facts, form answers, preferences and job database stay on your
machine, in files git ignores. `git pull` leaves them alone and they are never
committed. The repository holds only the program and the templates.

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

ApplyForge reads employers' own job boards. An optional mode runs a few
logged-out LinkedIn searches a day to find company names, then finds those
jobs on each company's own board. It never logs in. LinkedIn's terms forbid
automated access, so the mode stays off until you turn it on.
[LinkedIn](docs/why-no-linkedin.md) covers how it works and what else I'm
looking at for LinkedIn.

## Costs

- **Claude.** ApplyForge calls Claude to score jobs (a small, inexpensive
  model) and to write and check applications (a larger model). With a Claude
  subscription, these calls go through Claude Code and count toward your
  plan's usage. With an API key in `.env`, you pay per call: expect roughly
  $0.15 to $0.40 per tailored application, and a small fraction of that to
  score a job.
- **Job feeds.** Adzuna and USAJOBS need free keys. The other feeds and the
  company boards need none.

## Roadmap

What I'd like to add, as my time and resources allow. Your pain points move
things up the list. The full list, with the status of each item, is in
[docs/roadmap.md](docs/roadmap.md).

| Theme | Ideas and plans |
|---|---|
| More places to find jobs | paste a job description from any site, and Workable and SmartRecruiters boards (planned); Workday, iCIMS and Taleo feeds, and job searches outside the US (ideas) |
| More AI tools | OpenAI's Codex: project rules, skills and the jobkit tools (planned); the daily run in the ChatGPT desktop app, and OpenAI's dots (exploring); other assistants that use MCP tools (idea) |
| Better results in other fields | the fact checker learns nursing, teaching, trades and public-sector wording, and hourly pay ranges (planned) |
| LinkedIn, within the principles | your LinkedIn job-alert emails, read-only (exploring); who you know at a company, from your own data export (idea) |
| More computers | `jobkit schedule` on Linux (planned); `jobkit schedule` and the tests on Windows (ideas) |

Some things stay fixed: you press submit, ApplyForge never solves or gets around
a CAPTCHA, everything it writes comes from facts you confirmed, email is
read-only, LinkedIn stays logged out, and your files and job database stay on
your computer.

## Updating

To update, close any open Claude sessions in the jobkit folder, then run:

```bash
cd ~/code/jobkit && git pull && uv sync && uv run jobkit doctor
```

Start a fresh session afterwards so the jobkit tools load the new code.
ApplyForge changes often; [what's new](docs/changelog.md) lists what changed.

## More detail

- [Setup](docs/setup.md): installing by hand, `jobkit doctor`, your files,
  keys, the schedule and the Claude tools registration
- [How it works](docs/how-it-works.md): filters, role tracks, scoring,
  tailoring, questions, the nightly run and tracking
- [Fact checks](docs/validators.md): how every resume, letter and answer is
  checked against your confirmed facts
- [Form filling](docs/form-filling.md): what gets filled, what is left for you,
  and when filling stops
- [Commands and tools](docs/commands.md): every `jobkit` command and Claude tool
- [Email replies](docs/email-responses.md) and [LinkedIn](docs/why-no-linkedin.md)
- [Roadmap](docs/roadmap.md) and [what's new](docs/changelog.md)
- [Contributing](docs/contributing.md): pain points, ideas, tests, the build
  spec and the project rules

## Support

Your support means a lot: https://ko-fi.com/clumsywizardhands

And to those of you coming to this page experiencing similar struggles to mine, may you find your way through the dark.

Live long and prosper, friends.
