# Roadmap

ApplyForge is an evolving project. I built it for my own job search, and I keep
changing it as I learn what works and what gets in the way. This page lists
what I'd like to add next, as my time and resources allow. Nothing here has a
date, and the order changes when people tell me what they need.

Each item has a status:

- `planned`: I intend to build it.
- `exploring`: looking into whether it works and fits.
- `idea`: worth doing, not started.
- `done`: built; [what's new](changelog.md) has the details.

Want something that isn't here, or hit a wall? [Open an
issue](https://github.com/ClumsyWizardHands/ApplyForge/issues). Pain points
people report move things up the list, and fixes go in as they're ready.
[What's new](changelog.md) lists what has landed.

## What stays the same

Every item below works within these:

- You click submit. An employer's bot check is the employer asking for a
  person.
- ApplyForge never solves or gets around a CAPTCHA.
- Everything it writes comes from facts you confirmed.
- Email is read-only.
- Nothing on LinkedIn is logged in: discovery runs logged-out searches, and
  job alerts and messages are read from your own email, never from the
  LinkedIn site.
- Your files and job database stay on your computer.

## More places to find jobs

- `planned` Paste a job description from any site (Workday, iCIMS, Taleo,
  SuccessFactors, a PDF, an email) and have it scored, written for and tracked
  like any other job.
- `planned` Track an application you sent somewhere ApplyForge doesn't read, so
  email replies match it and it can be marked ghosted.
- `planned` Read Workable job boards.
- `planned` Read SmartRecruiters job boards.
- `idea` Read Workday, iCIMS and Taleo job lists through public feeds those
  systems document, and fill their forms in the window you can see. Reading
  anything beyond a documented feed needs a change to the project's scraping
  rule.
- `idea` Read Recruitee job boards, once I can check one against a live board.
- `idea` When a feed sends only a snippet and its link points to a Greenhouse,
  Lever or Ashby posting, read the full posting from that board.
- `idea` Crawl an employer's own careers page when it has no job board. This
  needs a change to the project's scraping rule: only pages the employer
  publishes for job seekers, and no getting around bot checks.
- `idea` Job searches outside the United States: other countries' locations,
  currencies and job feeds.

## Better results in other fields

- `planned` Teach the fact checker the vocabulary of nursing, teaching,
  healthcare, the trades and public-sector work (titles, licenses, caseloads,
  patient ratios), with tests that try to slip an unsupported claim past it in
  each field.
- `planned` Read hourly pay ranges on Greenhouse, Lever and Ashby postings
  (today only yearly ranges are read), convert them to yearly (x 2080) and
  compare them with your pay floor.
- `idea` Hourly pay from Adzuna, USAJOBS and the remote-job feeds.
- `idea` More worked scoring examples per field, so a career change is judged
  against the field you're moving into.

## Setup and daily use

- `done` Your bar: setup asks for the fit score at which jobs are approved
  without asking you, and the daily run opens the filled forms for your
  review. A daily limit only if you want one.
- `planned` Propose more standing rules from your setup answers (a tier,
  remote, pay), for you to accept one by one.
- `idea` Reply drafts you edit and send yourself: for an interview request or
  a recruiter's message, a draft from your confirmed facts that you copy into
  your own email. ApplyForge never sends anything.
- `idea` Saved sign-ins for sites like Workday, with your permission for
  each site; ApplyForge never handles passwords. Never used to get past a
  CAPTCHA or bot check.
- `idea` Add your stories (situation, action, result) during the setup
  interview, instead of by hand.
- `idea` A Google Sheets copy of your pipeline, written from the database
  (the database stays the one source of truth).

## Works with more AI tools

Today you use ApplyForge through Claude. OpenAI's Codex reads the same kinds of
pieces (project instructions, skills, a tools server). The skills need Codex
wording where they name Claude features, and scoring and writing need an
OpenAI backend, which is the bigger piece.

- `planned` An `AGENTS.md` beside `CLAUDE.md`, so Codex follows the same
  project rules.
- `planned` Codex versions of the setup, daily and scheduled-run skills in
  `.agents/skills/`, with a test that keeps their steps in line with
  `.claude/skills/`.
- `planned` Steps for adding the jobkit tools to Codex (`codex mcp add`, or
  `~/.codex/config.toml`). The Codex CLI, the IDE extension and the ChatGPT
  desktop app share that setting.
- `planned` Scoring and writing through OpenAI with an OpenAI API key, using a
  small model for scoring and a stronger one for writing and fact checks. It
  ships only once the fact-check and authenticity tests pass on it. What the
  model reads (postings, parts of your experience, your queue) goes to OpenAI,
  the same way it goes to Claude today.
- `exploring` The same through `codex exec`, which uses the sign-in you saved
  in the Codex CLI (your ChatGPT account, if that's how you signed in). OpenAI
  recommends an API key for automation, so this is for people who'd rather use
  their plan's limits. It counts against those limits.
- `exploring` The daily run as a scheduled task in the ChatGPT desktop app,
  using a Codex version of the scheduled-run skill. It has to run in your own
  checkout, not a fresh worktree, because your data lives there. It needs
  network access for the job boards (an allow rule for
  `uv run jobkit nightly`) and, without Claude, the OpenAI scoring and writing
  item above.
- `exploring` Email replies through ChatGPT's Gmail or Outlook Email plugin.
  The plugins can also draft and archive; ApplyForge only reads (sender,
  subject, date, snippet).
- `exploring` dots, OpenAI's always-on personal agents. Ask your dot what's in
  the queue, or get the daily summary in ChatGPT, Slack or Teams. A dot works
  on your computer through a local Codex task, so this builds on the Codex
  items above and needs your computer on with the ChatGPT app open.
  - The dot would get the read-only jobkit tools (`get_queue`, `get_digest`,
    `get_stats`, `schedule_status`, `background_status`) and, at most, approve
    and reject. Staging and opening forms stay something you ask Claude or
    Codex for at your computer, and you click submit there.
  - The dot skill would tell it never to open an apply link or click submit.
    ApplyForge can't enforce a skill on an agent it doesn't run, so part of
    this item is a way for the jobkit tools to refuse the form tools when a
    dot is asking.
  - Your files stay on your computer, and what the dot reads goes to OpenAI,
    the same way what Claude reads goes to Anthropic today. The dot skill
    would use the jobkit tools and not open the files in `data/` directly.
    The README's [list of what leaves your
    computer](../README.md#where-your-data-is-kept) gets a matching line when
    this ships.
- `idea` One installable plugin with the skills and the jobkit tools, for Codex
  and the ChatGPT desktop app.
- `idea` Setup notes for other assistants that can use MCP tools.

## LinkedIn, within the principles

The logged-out discovery searches stay as they are: nothing logged in, and
nothing that hides from bot detection. Ideas for doing more without touching
your account:

- `done` Read the LinkedIn job-alert emails and message notifications you
  already get (read-only). Each job is looked up on the employer's own board
  and scored the same day; messages show up in your summary.
- `done` Web search for alert jobs board lookup can't find. Setup asks
  (yes recommended). The employer's Greenhouse, Lever or Ashby posting enters
  and is scored the same day; a job on Workday or a similar site is listed
  with its direct link so you can apply there. LinkedIn pages are never
  opened.
- `idea` Who you know at a company: load the Connections file from your own
  LinkedIn data export and keep it on your computer. It holds other people's
  details, so only the names at a company you're looking at are shown, when
  you ask.
- `idea` Paste the text or company name of a LinkedIn job you found yourself,
  and ApplyForge looks for it on the employer's board or treats it as a pasted
  description. It reads only what you paste and never opens the linkedin.com
  link.
- `idea` Use an official LinkedIn API for job seekers, if one appears.

[LinkedIn](why-no-linkedin.md#what-could-come-next) has more on these.

## Email beyond Gmail

- `exploring` Outlook and other mailboxes through your assistant's email
  connector. The email check already searches by sender and company when a
  connector doesn't take Gmail's search syntax; it needs real use to find the
  rough edges.

## Computers

ApplyForge is tested on my Mac, and the automated tests run on macOS and Linux
for every change. If you try it on Windows, tell me what breaks.

- `planned` `jobkit schedule` on Linux, with a systemd timer (today you add a
  cron line or a timer yourself).
- `planned` Run the tests on Windows too, and fix what they turn up. The
  `jobkit` command now starts there; the rest needs real use.
- `idea` `jobkit schedule` on Windows, with Task Scheduler.

## Under the hood

- `planned` Record one live Adzuna and one live USAJOBS response as test
  fixtures (today their fixtures are hand-built in the documented shapes).

## Sources

The OpenAI items above are based on OpenAI's documentation as of 2026-10-22.
Its Codex pages moved from developers.openai.com to learn.chatgpt.com, so check
them again before building.

- [AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md):
  how Codex finds project instructions. It doesn't read `CLAUDE.md` by default.
- [Skills](https://learn.chatgpt.com/docs/build-skills): skills live in
  `.agents/skills/` and need a `name` and `description`.
- [MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli): `codex mcp add`
  and `[mcp_servers]` in `~/.codex/config.toml`, shared by the CLI, the IDE
  extension and the desktop app.
- [Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode):
  `codex exec`, its sandbox, JSON output and sign-in.
- [Scheduled tasks](https://learn.chatgpt.com/docs/automations?surface=app):
  the desktop app's scheduled tasks, local project or worktree.
- [The desktop app](https://learn.chatgpt.com/docs/app): Codex as a mode of the
  ChatGPT desktop app on macOS, Windows and Linux.
- [Skills and plugins](https://learn.chatgpt.com/docs/skills-and-plugins) and
  [building plugins](https://developers.openai.com/plugins/build/plugins).
- [Manage your inbox](https://learn.chatgpt.com/use-cases/manage-your-inbox):
  the Gmail and Outlook Email plugins.
- [dots](https://learn.chatgpt.com/docs/dots) and [computers and
  apps](https://learn.chatgpt.com/docs/dots/computers-and-apps): what a dot is,
  where you reach it, and its access to the files, code and apps on one
  connected computer. dots were announced at OpenAI's DevDay on 2026-09-29 and
  are rolling out to ChatGPT Pro (for adults outside the EEA, UK and
  Switzerland), Business Premium, and Enterprise when an admin turns them on.
- LinkedIn's [Download your account
  data](https://www.linkedin.com/help/linkedin/answer/a1339364/downloading-your-account-data):
  what the Connections file holds.
