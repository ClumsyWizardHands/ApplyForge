# Why no LinkedIn?

People expect a job-search tool to start with LinkedIn. jobkit doesn't, on purpose. This page explains what we found and what jobkit does instead.

**Update 2026-10-12.** Finding 2 below was wrong about what is technically possible, and is corrected. LinkedIn has a logged-out ("guest") job search that answers a plain HTTP request: no browser, no login. jobkit now has a discovery-only mode that uses it, off by default ([Discovery-only mode](#discovery-only-mode-2026-10-12) below). It is still against LinkedIn's User Agreement; CLAUDE.md hard rule 11 allows it only as an opt-in for low-volume, logged-out discovery, which each user decides on for their own checkout.

## What we found

**1. LinkedIn has no job-data API for job seekers.**
LinkedIn's job data is only available through its Talent Solutions partner programs, which are for employers, recruiters and the hiring systems they use. There is no public API for searching jobs or applying as a candidate. (SPEC.md §2.2 lists this as a dead end, next to the retired Indeed Publisher API.)

**2. LinkedIn has strict no-bot rules, and it enforces them. (Corrected 2026-10-12.)**
LinkedIn's User Agreement prohibits bots, scrapers and any other automated access, logged in or not, and LinkedIn actively detects them.

The earlier version of this page said the only way to search LinkedIn without an API was to drive a browser window logged in as you. That was wrong. LinkedIn serves a logged-out ("guest") job search to anyone, as plain HTML fragments: `GET https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=…&location=…` returns up to 10 job cards (LinkedIn job id, title, company, location, date posted), and `GET …/jobs-guest/jobs/api/jobPosting/<id>` returns one posting. We verified both live on 2026-10-12 with an ordinary HTTP client, an honest User-Agent naming jobkit, and no cookies or login: every request was answered with HTTP 200. So the limit on logged-out search is LinkedIn's rules, not a technical barrier.

What is still true:
- **Applying needs a login.** On the logged-out posting page, the Apply button of a posting that applies on the employer's site now opens a "Join or sign in" box. The employer's apply link is not in the page (older guest pages included it; on 2026-10-12, none of the four postings we checked did). Easy Apply happens inside LinkedIn and needs an account too.
- **Logged-in automation risks your account.** Driving a browser logged in as you is what LinkedIn bans accounts for: your profile, your network and your recruiter messages, lost in the middle of a job search. jobkit never does it.
- **LinkedIn blocks automated guests.** A client it refuses gets HTTP 999 or 429, or is redirected to a sign-in wall ("authwall") or a security check. jobkit stops when it gets any of these (see below).

**3. Browser automation is slow and expensive compared to job boards.**
Driving a browser means an AI looks at the page, decides where to click, waits for it to load, and repeats, for every search and every posting. Each step costs AI tokens and seconds. A company job board answers one request with every open role in clean, structured data: a whole company in under a second, for free. Getting the same postings through a LinkedIn browser window would cost far more and take far longer, for jobs jobkit can already read at the source.

**4. The tools that do it break jobkit's own safety rules.**
We reviewed the open-source LinkedIn "auto-appliers" (docs/research-2026-09-02.md). They work by:
- hiding the automation from bot detection (for example, "undetected" browser drivers),
- storing your LinkedIn password in plain text, and
- in some cases, paying a service to solve CAPTCHAs.

jobkit never evades bot detection and never solves CAPTCHAs (hard rules 2 and 11). The best-known project, AIHawk, deleted its application code in 2026.

**5. "Easy Apply" has no applicant-side submit API either.**
Like every applicant tracking system jobkit supports, LinkedIn's Easy Apply only accepts submissions through LinkedIn's own interface. The only programmatic channels belong to employers (hard rule 9).

**6. A LinkedIn job link doesn't tell jobkit where to apply.**
A link like `linkedin.com/jobs/view/1234567890` names LinkedIn's copy of the posting, not the employer's hiring system. Visiting it logged out doesn't help either: since at least 2026-10-12 the logged-out page no longer contains the employer's apply link (finding 2). The discovery-only mode therefore finds the employer's board by company name instead.

## Discovery-only mode (2026-10-12)

CLAUDE.md hard rule 11 ("no scraping of gated sites") has one exception: LinkedIn's logged-out job search, for discovery only. `jobkit linkedin-discover` (MCP `linkedin_discover`) and a nightly stage right after the feeds use it. It is **off** until you set `feeds.linkedin_guest.enabled: true` in `config/filters.yaml`.

What it does:
1. A few logged-out searches a day: up to 4 keywords (default: account executive, sales manager, sales director, head of sales) in your locations (default: United States remote), posted in the last 7 days.
2. Each card's title goes through jobkit's own title rules (exclude and include lists, paused tracks; anything with "engineer" is out). Cards seen in the last 14 days are skipped.
3. If jobkit already polls the company's board, the card is skipped: jobkit already gets that job from the board.
4. Otherwise only the company name goes to the discovery queue (source `linkedin_guest`).
5. The discovery stage, which runs right after, looks the name up in jobkit's board registry (about 15,000 public Greenhouse, Lever and Ashby boards). If it finds the company's board, it adds it, and the next poll reads it. The employer's own postings become jobs, with the real application forms, and go through the normal filter, scoring and tailoring. This takes about one night longer than polling the board at once, but rule 11's exception lets a LinkedIn posting into jobkit directly only through its "apply on company site" link, and the logged-out pages no longer show that link. A company name alone only goes to the queue.

jobkit never stores a LinkedIn job row, LinkedIn job link or LinkedIn copy of a posting. A LinkedIn find enters jobkit only as the employer's ATS record (hard rule 10).

Limits, all in `feeds.linkedin_guest` (config/filters.yaml):
- **Never logged in.** Plain HTTP requests (no browser), no cookies kept or sent, no proxies, no disguised browser identity.
- **An honest User-Agent:** `jobkit/0.1 (personal job-search tool; logged-out job discovery, a few requests a day; contact: …)`. The contact is `JOBKIT_CONTACT_EMAIL` from `.env`, or else your answer-bank email. LinkedIn sees this address with every request, so if you want it kept apart from your LinkedIn login, set `JOBKIT_CONTACT_EMAIL` to a different address. With no contact set, jobkit makes no requests.
- **A few requests a day:** at most 6 searches per local calendar day (`max_search_requests_per_day`, default 6). Posting pages are off (`max_detail_requests_per_day: 0`) because they no longer carry the apply link. Requests are 3 to 6 seconds apart (`min_delay_seconds`, randomized up to twice that). Every request is counted as it is sent, so the caps apply across runs. These numbers are upper limits: a higher value in the config is refused, and raising one would mean changing hard rule 11, not the config.
- **It stops on any block.** HTTP 429, 999, 403 or 401, any redirect (to the sign-in wall, the login page or a security checkpoint), a CAPTCHA or challenge page with any status (LinkedIn's own, or the Cloudflare "Just a moment..." check, which usually comes back as HTTP 503), or a search page that has no job cards and is not the empty no-results page ends the run immediately. jobkit records a `linkedin_guest.blocked` event and skips LinkedIn until the next calendar day. Every run checks for that event again before each request, so no run keeps going after another one was blocked. It never retries around a block and never solves a CAPTCHA (hard rule 2).
- **One run at a time.** A run holds a lock for its whole length. A second run started meanwhile (for example `jobkit linkedin-discover` while the nightly run is in its LinkedIn stage) sends nothing and reports `busy`, so requests never go out from two runs at once, and the 3 to 6 seconds between requests also hold from one run to the next.

The digest gets a line for the day: searches made, new jobs found through the employer's board, companies queued, and whether LinkedIn blocked us.

**The terms-of-service caveat.** This mode is still against LinkedIn's User Agreement, which forbids automated access whether or not you are logged in. Turning it on means accepting that, for low-volume, logged-out discovery (CLAUDE.md rule 11). Because no request is logged in, your LinkedIn account is not involved, but LinkedIn can still block the address the requests come from.

**Indeed and Glassdoor stay out.** jobkit doesn't scrape them, logged in or out, and doesn't use API keys that weren't issued to you. Indeed's Publisher API was retired in 2023, and SPEC.md §2.2 still lists it as a dead end.

## What jobkit does instead

Most jobs on LinkedIn are copies of a posting that also lives on the employer's own careers page. jobkit goes to the source:

- **Company job boards.** jobkit reads the public Greenhouse, Lever and Ashby boards of the companies on your watch list every night. You get the same jobs, usually earlier, with the real application form, which LinkedIn doesn't give you.
- **Job feeds.** Adzuna and the remote-job feeds surface companies you didn't know to watch, and jobkit adds their boards when it finds them.
- **Links you paste.** See a job on LinkedIn? Click **"Apply on company website"** and give that link to jobkit (`jobkit add-url <link>`, or just paste it to Claude). If the link points at Greenhouse, Lever or Ashby, the job joins your normal pipeline: scored, tailored and pre-filled.
- **Planned:** reading your LinkedIn job-alert *emails* (not LinkedIn itself) and pulling out the company apply links automatically. This is the approved direction (docs/research-2026-09-02.md) but isn't built yet.

## What LinkedIn is still best for

Use LinkedIn by hand for what automation can't do well: messaging recruiters and hiring managers, asking for referrals, and keeping your profile current. Recruiters cross-check it, so your title there should match your resume. A referral from a real person still beats any number of cold applications.

## Roadmap: LinkedIn in a future version

LinkedIn is too large a part of how people find jobs to leave out for good. The discovery-only mode above is the first step. Later steps stay within the rules above (no automated logged-in browsing, no evading bot detection). Directions worth exploring:

- **Job-alert emails.** Read your LinkedIn job-alert emails and pull out the company apply links automatically (already approved, not yet built).
- **Your own LinkedIn data.** LinkedIn lets every member download their data, including connections. Importing that file would let jobkit spot people you already know at companies in your queue, and suggest referrals.
- **Outreach drafts.** jobkit drafts recruiter and hiring-manager messages from your fact file; you send them yourself on LinkedIn.
- **Assisted browsing.** While you browse LinkedIn yourself, Claude reads the posting you have open and adds the job to your pipeline. You drive; nothing runs unattended.
- **Official access.** If LinkedIn opens a job-seeker API or partner program, use it.

