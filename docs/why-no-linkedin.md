# LinkedIn

ApplyForge (the `jobkit` command) reads jobs from employers' own job boards. If
you turn it on, it also runs a few logged-out LinkedIn searches a day to find
company names, then looks for those jobs on each company's own board. This
page covers what LinkedIn allows, what ApplyForge does, and what could come
next.

## What LinkedIn allows

- **No job API for applicants.** LinkedIn's job data is available through its
  Talent Solutions partner programs, which are for employers, recruiters and
  their hiring systems. There is no public API for searching or applying as a
  candidate.
- **Its terms forbid automated access**, logged in or not, and LinkedIn detects
  it. It answers a client it refuses with HTTP 999 or 429, or redirects it to a
  sign-in wall or a security check.
- **A logged-out job search exists.** LinkedIn serves a guest job search to
  anyone as plain HTML: `GET
  https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=…&location=…`
  returns up to 10 job cards (LinkedIn job id, title, company, location, date
  posted), and `GET …/jobs-guest/jobs/api/jobPosting/<id>` returns one posting.
  Both answered an ordinary HTTP client with an honest User-Agent and no
  cookies when checked in October 2026.
- **Applying needs an account.** On the logged-out posting page, the Apply
  button opens a "Join or sign in" box, and the employer's apply link is not in
  the page. Easy Apply happens inside LinkedIn, needs an account, and has no
  applicant-side API.
- **A LinkedIn job link doesn't say where to apply.** A link like
  `linkedin.com/jobs/view/1234567890` names LinkedIn's copy of the posting, not
  the employer's hiring system.

## What ApplyForge doesn't do

ApplyForge never logs in to LinkedIn and never drives a browser there.
Logged-in automation is what LinkedIn suspends accounts for, and your profile,
network and recruiter messages matter too much to put at stake in the middle of
a search. It never gets around bot detection or solves a CAPTCHA. A job board
is faster anyway: one request returns every open role at a company, in
structured data.

## Discovery mode (off by default)

`jobkit linkedin-discover [--dry-run]` (Claude tool
`linkedin_discover(confirm=false)`) and a nightly stage right after the feeds
use the logged-out search, for discovery only. It is the one exception to the
project's no-scraping rule (CLAUDE.md hard rule 11). It is off until you set
`feeds.linkedin_guest.enabled: true` in `config/filters.yaml`. While it is off,
the nightly `linkedin-guest` stage does nothing and
`linkedin_discover(confirm=true)` refuses to start.

It only helps for employers that also post on Greenhouse, Lever or Ashby,
because those are the boards ApplyForge can look the job up on.

### What it does

1. **A few searches a day.** Up to 4 `keywords` in your `locations`, posted in
   the last `posted_within_days` (7). The setup interview writes the keywords
   from your track titles (the first four) and the locations from your places,
   plus "United States, remote" when you accept remote work. Each location has
   a `workplace`: remote, hybrid, onsite or any. If the `keywords` key is
   missing, jobkit falls back to built-in sales titles, and `jobkit doctor`
   warns when that would happen on a search with no sales track.
2. **Each card is filtered.** Cards seen in the last 14 days are skipped.
   Titles go through the filter's blocklist, exclude, include and paused-track
   rules; rejections are logged and recorded as `linkedin_guest/<id>`.
3. **Known boards are skipped.** If ApplyForge already polls the company's board,
   the card is skipped: the board already gives ApplyForge that job.
4. **Otherwise only the company name is kept.** It goes to the discovery queue
   (source `linkedin_guest`, no URL).
5. **The employer's board is found.** The discovery stage, which runs right
   after, looks the name up in the board registry (about 15,000 public
   Greenhouse, Lever and Ashby boards) and adds the company's board; the next
   poll reads it, and the employer's postings go through filter and scoring as
   usual. The `resolve-boards` stage that follows also looks for the card's own
   posting (same title and an agreeing location) on the company's board, and
   ingests it when exactly one fits (see [Finding the employer's own
   posting](how-it-works.md#finding-the-employers-own-posting)).

The LinkedIn run itself never adds or polls a board on a company-name match.
The only way a LinkedIn card brings a posting straight in is an "apply on
company site" link that resolves to the employer's board, and logged-out pages
don't show that link, so in practice step 4 applies. No job row, source or
queue entry ever holds a LinkedIn URL or a LinkedIn copy of a posting; a
LinkedIn find enters ApplyForge only as the employer's own record.

### Limits

All of these are in `feeds.linkedin_guest` in `config/filters.yaml`.

- **Never logged in.** Plain HTTP GETs to
  `www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search`: no browser,
  no cookies kept or sent, no proxies, no disguised browser identity.
- **An honest User-Agent:** `jobkit/0.1 (personal job-search tool; logged-out
  job discovery, a few requests a day; contact: …)`. The contact is
  `JOBKIT_CONTACT_EMAIL` from `.env`, else your answer bank's email; with
  neither, nothing is sent. LinkedIn sees that address with every request, so
  set `JOBKIT_CONTACT_EMAIL` to a different address if you want it kept apart
  from your LinkedIn login.
- **A few requests a day:** at most `max_search_requests_per_day` (6) searches
  per local calendar day, and `max_detail_requests_per_day` posting pages (0,
  because the logged-out page no longer carries the apply link). Requests are
  `min_delay_seconds` (3) to twice that apart, randomised. 6 and 40 are
  ceilings and 3 seconds is a floor: more searches or posting pages, or a
  shorter delay, fails at load. Requests are counted as
  `linkedin_guest.request` events when sent, so the caps apply across runs.
  With more keyword and location pairs than searches, the pairs rotate by day.
- **It stops on any block.** HTTP 429, 999, 403 or 401, any redirect (sign-in
  wall, login, checkpoint), a CAPTCHA or challenge page at any status
  (including Cloudflare's "Just a moment..." check, usually HTTP 503), or a
  search page with no job card that isn't the empty no-results page ends the
  run, records a `linkedin_guest.blocked` event, and skips LinkedIn until the
  next local calendar day. The day's block is read again before every request,
  so a block recorded by any run stops the others. A block isn't a stage
  failure; the run doesn't retry around it. A network error or 5xx ends the run
  without closing the day (`jobkit linkedin-discover` exits 1).
- **One run at a time.** A run holds an exclusive lock on
  `data/jobkit.db.linkedin_guest.lock` for its whole length. A second run
  started meanwhile sends nothing and reports `busy`.
  `linkedin_discover(confirm=true)` also refuses while a nightly or `run-all` is
  in progress. The delay between requests also holds from one run to the next.

### What you see

`--dry-run` (and `linkedin_discover()` without `confirm=true`) prints today's
plan, the budget used, the block state and whether a run is in progress,
without contacting LinkedIn. Each run prints, and records as a
`linkedin_guest.run` event, what each card became; `linkedin_guest.seen` events
keep each card's outcome. The digest's "LinkedIn guest discovery" line sums the
day: searches made, new jobs found on employer boards, companies queued, and
whether LinkedIn blocked the run.

### Your call

LinkedIn's User Agreement forbids automated access, logged in or not, so this
mode stays off until you turn it on. No request is logged in, so your LinkedIn
account is never involved. If LinkedIn blocks your address, that block applies
to your own logged-out browsing from that network too; ApplyForge stops for the
day and tries again tomorrow.

Indeed and Glassdoor are out of scope: ApplyForge doesn't scrape them, logged in or
out, and doesn't use API keys that weren't issued to you. Indeed's Publisher
API was retired in 2023.

## Using LinkedIn alongside ApplyForge

- **Paste the employer's link.** See a job on LinkedIn? Click "Apply on company
  website" and give that link to Claude (or `jobkit add-url <link>`). A
  Greenhouse, Lever or Ashby link joins your pipeline: scored, tailored and
  filled. A link on another system is named, and you apply on the employer's
  site.
- **Do the human parts by hand:** messaging recruiters and hiring managers,
  asking for referrals, and keeping your profile current. Recruiters cross-check
  it, so your title there should match your resume. A referral usually gets more
  attention than a cold application.

## What could come next

I'd like a better LinkedIn process, as long as it keeps to the same
principles: nothing logged in, and nothing that hides from bot detection.
Ideas so far, also on the
[roadmap](roadmap.md#linkedin-within-the-principles):

- **Job-alert emails.** Read the LinkedIn job-alert emails you already get
  (read-only) and look each job up on the employer's own board.
- **Your own LinkedIn data.** LinkedIn lets members download their data,
  including connections. Importing that file would let ApplyForge point out
  people you already know at companies in your queue. The file stays on your
  computer; only the names at a company you're looking at are shown, when you
  ask.
- **Paste a job you found yourself.** Paste the job's text or the company name
  from a LinkedIn posting you're reading, and ApplyForge looks for the job on
  the employer's own board. It reads only what you paste and never opens the
  linkedin.com link.
- **Official access.** If LinkedIn opens a job-seeker API or partner program,
  use it.
