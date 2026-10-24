# How it works

This page describes what ApplyForge (the `jobkit` command) does with each job,
from finding it to recording the reply. Commands are listed in
[Commands and tools](commands.md); the checks on what it writes are in
[Fact checks](validators.md); form filling is in [Form filling](form-filling.md).

## Overview

```
DAILY RUN (`jobkit nightly`, from the Claude app's scheduled task, or a
           macOS LaunchAgent as the fallback; runs without you)
  read employer boards (Greenhouse, Lever, Ashby) and job feeds
    (+ logged-out LinkedIn search for company names, if you turned it on)
    → dedupe              one row per posting; the employer's own record wins
    → filters             title, place, pay floor, age, blocklist, repost
                          sites; free, every rejection logged with its reason
    → triage (small model) extract requirements, check the remote claim,
                          check the pay, score fit 0-100; every job that
                          passed the filters, unless you set a limit
    → form capture        each application form read, matched against your
                          answer bank, new questions drafted (its friction
                          feeds the rules)
    → standing rules      approve the jobs that clear your bar (and any other
                          rules you set)
    → tailor approved     resume and letter from your confirmed facts only
    → ghost-close         no reply after 30 days: stage 'ghosted'
    → fill approved forms in a visible browser, closed when done; not submitted
    → digest              out/digest/<date>.md
  then, in the scheduled task (Claude):
    → email check         replies, job-alert emails and LinkedIn messages,
                          read-only, if you connected your email; alert jobs
                          found on the employer's own board are scored,
                          approved if they clear your bar, written and filled
    → open the forms      the filled applications open in a browser window,
                          when staging.open_after_daily_run is on
    → summary             what is ready for you, replies, alert finds,
                          messages, the queue below your bar

YOU
  review each open application as much as you like and click submit →
  "I submitted Acme" → approve or reject anything below your bar by number
  ("what's in the queue", "approve 2") → answer the occasional question
```

Every number in a resume or letter must match a fact you confirmed, or the
draft is rejected. A reworded bullet that drifts from your wording is put back.
Before each click, the form filler checks the element and refuses anything that
would send the form, and a test fails if a click skips that check.

## Where jobs come from

- **Employer job boards.** The public Greenhouse, Lever and Ashby boards of the
  companies in `config/targets.yaml`, read every night. A board answers one
  request with every open job, with the full description, so these are the
  most complete source, and the only one whose forms ApplyForge fills.
- **Job feeds.** Adzuna (all industries; needs free keys), USAJOBS (US federal;
  needs a free key), Arbeitnow, Remotive, RemoteOK, Himalayas and Consider (VC
  portfolio boards). A feed row is never the canonical record: when the same
  posting also arrives from the employer's own board, the board's record wins,
  because it has the real apply link and form.
  - **Local search.** For hybrid or on-site work, Adzuna can search near your
    places (`feeds.adzuna.locations: [{where: "Columbus, OH", distance_km:
    40}]`) and USAJOBS too (`feeds.usajobs.locations: [{name: "Columbus,
    Ohio", radius_miles: 25}]`). Each Adzuna phrase is searched once per place,
    up to 10 phrases and 3 places a run; USAJOBS takes up to 5 places. The
    setup interview writes these from your answers.
  - `feeds.adzuna.country` picks Adzuna's country (default `us`); a value that
    isn't a two-letter code is logged and `us` is used.
  - A feed whose keys are missing is skipped before any request, and the log
    names the missing variables (`feeds.skipped_no_key`). A keyed feed that
    returns nothing logs `feeds.empty`.
- **Your job-alert emails**, if you connected your email: each job in a
  LinkedIn or job-site alert is looked up on the employer's own Greenhouse,
  Lever or Ashby board and enters as that posting, scored the same day. See
  [Email replies](email-responses.md#job-alert-emails).
- **LinkedIn discovery** (off by default): a few logged-out searches a day for
  company names. See [LinkedIn](why-no-linkedin.md).
- **Links you paste.** `jobkit add-url <link>` (tool `add_job_url`). A
  Greenhouse, Lever or Ashby link polls that board at once and queues the job,
  then extracts and scores it (in the background from Claude), so it shows in
  the queue like any other.
  A link on another system (Workday, iCIMS, Taleo, Oracle, SuccessFactors,
  UKG, ADP, NEOGOV, USAJOBS, SchoolSpring and others) is recognised by its
  host and answered with the system's name and a note to apply on the
  employer's site. Nothing is fetched from those systems, and no job is added,
  so that application isn't tracked, matched to email replies or marked
  ghosted.

## Filters

### Role tracks

`config/filters.yaml` defines the kinds of role you want once, as an ordered
list under `filters.tracks`; the first match wins, so list narrower or more
senior tracks first. Each track has an `id` and a `title_regex`, which is also
an include pattern. `include_titles` holds only patterns outside every track.
`config.Filters.track_for(title)` is the classifier the rest of the code uses.

Optional per-track settings:

- `active: true|false` (default true): pause a track (below).
- `pivot: true`: you are moving into this kind of role from other work. Under
  `prompt_set: general`, the score treats the jobs as a career change (see
  [Scoring](#scoring)).
- `min_score`: this track's queue gate, in place of
  `gates.min_score_to_surface`. A career change usually needs a lower one.
- `emphasis`: one line, in your words, about what should lead your resume for
  this kind of role. Tailoring uses it under `prompt_set: general`; scoring
  doesn't.

The standing-rules `title_class` condition is read from the same classifier
and matches a track id. For the `account_executive` track, a founding or first
AE title is not `account_executive`. A `title_class` naming no track never
matches; the rules engine logs it and `jobkit doctor` warns.

### Pausing a track

A title whose first matching track is paused is a filter-layer rejection,
`track_paused:<id>`, after the title include and exclude checks and before
location and pay. A later track or a non-track `include_titles` pattern never
releases it. Pausing changes nothing else: the paused track still classifies
its titles, and its pay floor, standing-rule class, scoring and tailoring
emphasis stay defined. Feeds still read its postings so the filter can hold
them.

To resume a track, set `active: true` and run `uv run jobkit refilter` (tool
`refilter(confirm=true)`): every open posting held as `track_paused:<id>` goes
back through the filter. A job with a decision of yours stays rejected.
`refilter` lists the paused tracks, and the `tune_filters` tool shows how many
open postings each one holds.

**Example: pausing an entry-level track.** The sales test profile
(`tests/fixtures/config/filters.yaml`, like `config/filters.sales.example.yaml`)
lists `sdr_bdr_manager`, `sales_exec`, `sdr_bdr` and `account_executive`, in
that order, and pauses `sdr_bdr`. Because `sdr_bdr_manager` comes first and
stays active, "SDR Manager" and "Manager, Sales Development" still pass.

**Known wrongful holds** while `sdr_bdr` is paused, an accepted tradeoff: its
regex claims any "Business Development" title that no leadership track takes
first, so roles an include pattern targets can be held as
`track_paused:sdr_bdr`. In the test profile they include
"Business Development Executive",
"Account Executive, Business Development",
"Revenue Operations, Business Development",
"Deal Desk Analyst, Business Development" and
"Sales Compensation Analyst, Business Development". Senior business
development titles ("VP Business Development", "Regional Vice President,
Business Development", "Chief Business Development Officer") match no
leadership track, so they fall to `sdr_bdr` and are held too. The fix is a
`sales_exec` regex change; it is proposed in `tests/test_track_pause.py`, whose
matching tests are strict xfails until it is applied. To see a held job, look
for `track_paused:sdr_bdr` in the rejection log.

### Pay floors

Pay floors are per track (`filters.comp.track_floors`, keyed by track id);
titles that match no track use `filters.comp.floor`. Floors are yearly.

- A track with `basis: ote` has an on-target-earnings floor for commission
  roles. Many such postings publish base pay only, so a posting passes when its
  `comp_max` reaches the floor or `floor × comp.ote_base_share`. That allowance
  is for a base figure: a posted figure that is itself OTE is compared with the
  full floor (the rejection reason ends `:posted-ote`), and the standing-rule
  condition `comp_min_gte` never matches an OTE minimum.
- Floors apply only to USD (or unlabelled) ranges, and never to unpublished
  pay. Adzuna's `salary_min` request parameter is the lowest floor
  (`Filters.lowest_comp_floor`).
- Greenhouse and Lever postings include pay: Lever's `salaryRange` (per year
  only) and the Greenhouse pay-transparency block in the posting (annual USD
  ranges only). With both base and OTE blocks, the range of the same kind as
  the title's floor is used (`Filters.comp_floor_for(title).basis`); among
  ranges of one kind (geographic tiers), the highest ceiling. This is chosen
  at poll time, so a change to a track's basis takes effect on the board's next
  poll. The schema has no basis column; `greenhouse.comp_basis` reads it back
  from the stored description.

### Locations

No place is built in. The filter accepts the location buckets of
`filters.locations.allowed` and `remote_policy.relocation_cities`
(`Filters.accepted_location_buckets`; each place is bucketed by the same
`normalize.location_bucket` that buckets postings, so "Brooklyn" names
`nyc-metro` and "Bristol" names `other-bristol`), plus remote postings while
`filters.locations.accept_remote` is true (the default).

- `remote-intl` is rejected even when the board flags the job remote.
- A location that is only country-wide US ("United States", "USA - West",
  "US / Canada"), blank, or "Distributed" goes to the remote check instead of
  being rejected. A location naming a US city or state outside your places
  ("Tulsa, Oklahoma") is a `location:` rejection.
- The commute cap skips relocation cities (`Filters.relocation_buckets`), and
  `config/commute_table.yaml` is keyed by location bucket.
- A geo restriction passes when it names an entry of
  `remote_policy.acceptable_geo_restrictions`, one of your places or relocation
  cities, or a US state they name (its name, or its postal code unless the code
  is an English word such as OR or IN). Geo restrictions are matched as whole
  words.
- In the remote check, a hybrid answer on a country-wide location gets a
  `hybrid-office-unknown` flag and an unclear one a `remote-policy-unclear`
  flag. A hybrid office in a named city with no commute-table entry (and not a
  relocation city) is rejected as `sanity:commute:unknown`.
- **A place in the job title wins over the feed's location**
  (`normalize.title_location`, `filter.title_location_override`). Feeds often
  file a job under the company's head office. A title such as "Account
  Manager - Denver, CO" with the feed location "Boston, MA", or "Chief of
  Staff | Colombia" with "Cambridge, MA", is filtered by the place in the
  title. A country outside the US in the title is treated as that country
  (with a remote word, as `remote-intl`), unless the feed itself names a
  place outside the US. A US place in the title wins only when the feed names
  a different specific place: the same metro, a shared state or the same city
  agree, and "United States" or "Remote - US" in the feed is never replaced
  by a city. A title naming several places, or both US and non-US places,
  changes nothing. A region in the title ("LATAM", "EMEA", "Central EMEA")
  often names the territory a seller covers, not where the job is, so it
  wins only when the feed names no place (blank, "Remote", "Hybrid",
  "Distributed"): "Channel Account Executive, LATAM" filed under "Miami, FL -
  Remote" stays a US remote job. A city abroad whose name a US city shares
  ("Melbourne", "Paris", "Athens", "Dublin") counts only with its country
  ("Melbourne, Australia"). A rejection decided this way ends in `:title`
  (`location:denver-metro:title`), and each override is logged as a
  `filter.title_location` event with both locations. The stored location is
  not changed, so duplicate detection is unaffected. `refilter` leaves a
  rejection alone when the only change is the added `:title`.

### Repost sites

Some sites repost other companies' jobs under their own name ("Our partner is
looking for…", usually with no pay range). One such site can file thousands
of jobs on its own Lever board. Their postings have no employer form and hide
who is hiring, so ApplyForge keeps them out:

- **The built-in list** (`registries/reposters.yml`): Jobgether, Lensa,
  Jobright.ai, TieTalent, Virtual Vocations, JobLeads, Jobs via Dice, Jobs via
  eFinancialCareers and Jobs for Humanity, by name and, where known, by board.
  A listed board is never fetched, and a job filed under a listed name is
  rejected by the filter as `reposter:<name>`.
- **The wording check** (`discovery/reposters.py`): a board where most
  postings (more than half, from 3 postings up; both settings are in the same
  file) say the partner or client is the one hiring, or that the job is listed
  on someone's behalf, is treated the same way. "We work with our partners"
  or "we serve our clients" doesn't count. A board you listed in
  `config/targets.yaml` is exempt from the wording check, not from the list.
- **Where it applies:** polling, `add-url`, discovery and board lookup. Such a
  board is not added, or is set stale; its open jobs nobody has decided yet
  (including scored jobs waiting in the queue) are rejected with the reason
  `reposter:<name>`; a job on a stale or listed board is never approved
  automatically (`employer_posting` is false for it); and the digest has a
  "Repost sites kept out" section naming each one. `jobkit add-url` on a
  repost link says "not added" and why.
- Discovery and board lookup also skip the companies in
  `filters.blocklist_companies`, before any request.

### After a config change: `refilter`

The filter only reads new jobs, so `refilter` (tool `refilter`, a dry run until
`confirm=true`) re-checks open jobs that are still rejected and whose latest
rejection is a filter-layer title, paused-track, pay-floor or location reason.
It recomputes their location bucket, resets the ones whose verdict changes to
`new` (one `refiltered` event each, with the previous reason), and runs the
filter. Blocklist and stale rejections, later-layer rejections, closed jobs and
any job with a decision of yours are left alone. A second run changes nothing.
Postings are dated, so after a long gap most come back as `stale`; a fresh
`poll` matters more.

### Missing descriptions

Jobs with under 500 characters of description are refused before any model
call: triage rejects them and tailoring raises. Both log a rejection with
`layer=sanity`, `reason=jd_missing`. Job feeds are where this happens most:
Adzuna's search results carry only a snippet of each posting. A feed job set
aside this way stays rejected unless the employer's own Greenhouse, Lever or
Ashby posting is found for it. The digest counts the feed jobs set aside since
the last digest ("Feed jobs not scored"), and `get_stats` counts them all.
USAJOBS jobs carry the summary, duties, qualifications, requirements and
education from the search result, which is usually enough to score.

## Pay and remote checks

After the filters, a small model reads each description:

- **Remote check.** The stated policy against the fine print ("remote" beside
  "3 days in office", "must reside in CA"), with the commute rules above.
- **Pay check** (`comp_sanity` in `config/filters.yaml`). It flags:
  - a published range wider than `max_range_width_pct` (default 0.6) as
    `range-dodge`;
  - no range where pay-transparency law requires one
    (`flag_missing_range_when_required`);
  - `underpriced` when the description asks for a lot and the published ceiling
    is under `underpriced_below` (default 160000; the setup interview sets about
    1.25 times your lowest floor);
  - `ote-basis-unclear` when a ceiling at or above `ote_unclear_from` (default
    200000) doesn't say whether it is base or OTE, and `ote-unrealistic` from the
    model's reading. With `ote_check: ote_tracks`, both OTE flags apply only to
    titles whose pay floor has basis `ote`; with `always` (the default) they
    apply to every title.

## Scoring

A small model scores each job 0 to 100 against your experience, with a short
rationale and a list of gaps. The score is must-have coverage and how likely an
interview is. Tenure is computed from your role dates. Overqualification is
listed as the last gap (`overqualified: …`) instead of scoring a role near
zero.

**Prompt sets.** `prompt_set` at the top of `config/filters.yaml` picks the
requirement-extraction, scoring and resume-selection prompts:

- `general`: `prompts/extract_requirements_general.md`,
  `prompts/score_fit_general.md` and `prompts/select_bullets_general.md`. The
  extraction and scoring examples come from several
  fields (software, nursing, teaching, sales), and the resume selection uses
  each track's `emphasis`. The setup interview writes this for any search that
  isn't all sales titles, and for a sales search with a track marked as a
  change of field (`pivot`) or given an `emphasis`, since only these prompts
  read those two settings.
- `sales`: `prompts/extract_requirements.md`, `prompts/score_fit.md` and
  `prompts/select_bullets.md`, calibrated
  on sales roles, with resume ordering presets for SDR/BDR, AE, sales manager
  and sales executive tracks.
- Absent: `sales`. A config written before prompt sets existed keeps its
  scores and caches exactly as they were. To switch, add `prompt_set: general`,
  then run `uv run jobkit rescore` so queued jobs are scored again. `jobkit
  doctor` warns when the line is missing and no track looks like sales, and
  when a track sets `pivot` or `emphasis` under the sales prompts. Any other
  value fails at load.

Some prompts both sets share use sales examples to show the model a rule:
`cover_letter.md` and `novel_question.md` ("5–7 deals a month") and
`authenticity_critic.md` ("$8M in pipeline"). The examples say nothing about
you.

**The profile the scorer sees.** Every corpus role with its title, company,
start and end month and years in role, with that role's bullets under it;
career span; education; `skill_index`; and the never-claim lists. Under
`sales` it adds total years in sales-titled roles (a title a track places, or
one containing "sales"). Under `general` the line is "years in roles on the
target tracks", counting only titles a track places, with the track shown for
each. Years are computed as of the current month, which is printed in the
profile, so a corpus edit or a new month misses the score cache.

**Changing fields.** Under `general`, a job on a track with `pivot: true` is
scored as a career change: the prompt is told which track, and the switch is
listed as a gap after the unmet must-haves and before any overqualified gap.
Give such a track a lower `min_score` so its jobs reach your queue.

**The queue gate.** A job reaches the queue when its score is at least its
track's `min_score`, or `gates.min_score_to_surface` (default 65) when the
track has none. Jobs below are rejected as `score_below_surface:`.

**When scores are redone.** Every score records the corpus hash and the
prompt's version, and a general-set score also records the prompt's name (a
`score_fit_general` event; a score with no name is a `score_fit` score).
`rescore` treats a score as stale when the corpus changed or `prompt_set` was
switched, and for the general prompt also when its version changed. Neither
the nightly run nor `triage` re-scores a job that already has a score, so
after switching `prompt_set` run `uv run jobkit rescore` (or ask Claude to
rescore) to score the jobs already in your queue again.
`rescore` also puts back in the queue, without a model call, a job rejected
only by the score gate when its track sets `min_score`, it was rejected under
a different gate, its score meets the track's gate, and the filters still pass
it (a `regated` event each).

## Your bar: automatic approval

`config/autoapprove.yaml` holds standing rules, applied after the filters and
the sanity checks (`rules` stage). The setup interview asks for your bar and,
on your yes, writes one rule:

```yaml
auto_approve:
  - when: { score_gte: 75, sanity_checked: true, blocking_flags: none,
            location_known: true, employer_posting: true }
    reason: "clears-your-bar"
```

Every queued job with a fit score at or above the number is approved as if you
had approved it, with a decision marked automatic, when all of these hold:

- **Both sanity checks finished** (`sanity_checked: true`). A job whose remote
  or pay check did not finish waits in your queue until it does.
- **No deal-breaker flag** (`blocking_flags: none`). Only a real deal-breaker
  holds a job back: a doubt about remote work or the location
  (`remote-conflict`, `remote-policy-unclear`, `hybrid-office-unknown`, `geo`,
  `office-days`, `onsite`) or pay the posting can't back up
  (`ote-unrealistic`). A missing pay range (`no-range-where-required`), a wide
  one (`range-dodge`), several roles in one posting (`role-sprawl`) and an
  unclear base-vs-OTE basis (`ote-basis-unclear`) are shown in your queue but
  don't hold the job back. A low ceiling (`underpriced`) is in neither list,
  so it holds the job back until you add it to `non_blocking_flags`. The
  two lists are `auto_approve.blocking_flags` and
  `auto_approve.non_blocking_flags` in `config/filters.yaml`; a list you set
  replaces its default, a flag named in both is a load error, and a flag in
  neither list blocks, so a new kind of flag is never waved through unseen.
  The older condition `sanity_flags: none` still means no flag at all.
- **The location is clear** (`location_known: true`): the remote check ran
  and neither what the posting states nor what its fine print says is
  unclear.
- **It is the employer's own posting** (`employer_posting: true`). A job-feed
  row that board lookup could not match to the employer's Greenhouse, Lever
  or Ashby posting has no form to fill. It waits in your queue, and its line
  in `get_queue` and the digest ends "no employer form found: apply on the
  employer's site (found via <link>)"; the link is the feed's page for the
  job (tracking parameters such as `utm_source` removed), where you can find
  the employer. A job on a board that is no longer polled, or on a repost
  site, counts as no employer posting too.
  You can still approve it yourself.
- These last two hold for every rule in the file, even one that does not
  name them: no rule approves a job whose location is unclear or that is
  not the employer's own posting. Rules that reject still apply to it.

When a job clears the bar it is approved: the same run tailors it and
fills its form, and, when `staging.open_after_daily_run` is on, the scheduled
task opens it for you. Jobs below the bar stay in your queue, and
you can approve or reject any job yourself at any time. Change the number to
move your bar, or delete the rule to approve everything yourself.

- A rule file you wrote yourself is never replaced by setup. To use this
  policy in it, give your clears-your-bar rule the three conditions above in
  place of `sanity_flags: none`.
- **No daily limit by default.** `max_auto_approve_per_day: N` in
  `autoapprove.yaml` sets one, only if you want it. With a limit, the
  best-scoring jobs are approved first, and the rest stay in your queue (you
  can approve any of them now), each with an `auto_approve_waiting` event, and
  the next run tries them again. The digest then has an "Automatic approval"
  section with the day's count. Without the key nothing changes: no sorting,
  no section. A value that isn't a whole number of 1 or more stops the rules
  stage with a message naming the key.
- **Rejecting rules** (`auto_reject`) work the same way and are never limited.
- `jobkit doctor` reports the rule count and the limit, and fails on a bad
  value.

**Opening the forms.** `staging.open_after_daily_run: true` in
`config/filters.yaml` (setup asks; yes is the default) makes the scheduled
task end by opening the filled applications in a browser window. The last
line of `jobkit schedule status` (tool `schedule_status`) says whether it is
on and how many are ready. Absent, the forms wait until you say "open my
applications".

## Tailoring

For each approved job the strong model selects bullets from your corpus, and
writes a cover letter. Every bullet is your corpus text, and everything passes
the [fact checks](validators.md) before you see it.

- **Selection by track.** Tailoring passes the job's track (`track_for`, or
  `other`) to the selection prompt. Under `sales`, the prompt sets what leads
  for each sales track. Under `general`, it is told the track's `emphasis` in
  your words; when the emphasis names something no corpus bullet shows, it is
  ignored. Emphasis and order only. The track and the prompt are written to
  `provenance.json`.
- **Cover letters.** Paragraph 3 closes on a concrete, cited result the role
  needs, then asks for the conversation. A gap is mentioned only when it is one
  of the job's extracted must-haves, framed as how you cover it. The model gets
  the must-haves and the scorer's gaps, minus any `overqualified:` gap.
- **Repeated phrases** (`config/banned_phrases.yaml`).
  `cover_letter.hard_reject_patterns` fail known tics, each named for what it
  does: a contrast construction, a gap announcement, a candour preface, a
  title-never-held confession and an apologetic gap close. `cross_letter`
  compares each new letter's 5-word phrases with the newest 50 earlier letters
  (`out/*/cover.txt`): a phrase already used in 2 of them fails the letter
  (`on_repeat: fail`), and the regeneration prompt names it. A retold fact is
  not a repeat: phrases made only of corpus words, this job's description words
  and the employer's name are exempt; words are compared by stem; numbers and
  hedges never count; a phrase that retells a cited bullet may add as many
  narrative words as it contains of that bullet's words and figures; and the
  plain ask ("I'd like to talk about") is not phrasing.
- **No dropped figures.** A bullet variant that drops a number its other
  variant contains is not used: the tailor swaps it, restores a rendered bullet
  that trimmed one of its own numbers, and the one-page shrink only shortens
  bullets whose short variant keeps every number.
- **One page.** `render/pdf.page_count` reads the PDF's page tree (`/Count`) or
  its page objects, whatever the spacing and inside compressed object streams.
  A PDF in which no page is found raises, and tailoring then asks Typst itself
  (`render/pdf.rendered_pages`: the same template compiled to SVG, one image per
  page).
- **No empty roles.** The current role and every role that ended within
  `resume.recent_role_years` (default 5) get at least one bullet; when the
  model leaves one empty, the role's first corpus bullet that passes the fact
  check is added. Older roles with no bullet are folded into one "Earlier
  experience" line. `provenance.json` records `required_roles`,
  `selection_adjustments` and `earlier_experience`.
- **Folders** are `out/<company>-<title>-<job_id>-<date>/`, so duplicate
  postings don't overwrite each other.
- **Header location** comes from `location_strategy` in `filters.yaml`: the
  first rule whose `when` matches (`job_state`, `job_city_in`, `remote`), else
  `default`, else the corpus `identity.display_location`.
- **Letter drafts.** A letter gets up to `tailoring.cover_attempts` drafts
  (`config/filters.yaml`, default 5). Every draft gets the full fact check,
  the claim judge and the authenticity review. Each new draft is a new letter,
  told what failed in every draft before it (the kind, the sentence and the
  rule in plain words), and not to write those sentences again. See
  [When a letter fails](validators.md#when-a-letter-fails).
- **A letter that fails stays failed until something changes.** A cover letter
  that fails the fact check, or fails the critic on every draft, leaves its
  job at `approved`. The failure event records the attempt key: description
  hash, corpus hash and prompt versions (the selection prompt in use, the
  self-review prompts, `self_review.max_revision_rounds` and the claim judge
  prompt included) and the number of drafts allowed. `jobkit tailor` and
  `run-all` skip that job, logging `tailor.skipped_cover_failed`, until one of
  those changes, so a corpus edit or a `prompt_set` switch retries every
  parked job once. Raising `tailoring.cover_attempts` also retries it once
  (lowering it does not); a failure recorded before the setting existed
  counts as three drafts, so the default of 5 retries those jobs once. To
  retry one job by hand, use the `retailor(n)` tool or `jobkit tailor
  --job-id N`. `retailor` refuses an application that is already filled or
  further along.

## Questions for you

ApplyForge asks you for facts that aren't in your corpus and would help with
open jobs, and adjusts what they affect once you confirm them
(`src/jobkit/pipeline/questions.py`, `src/jobkit/pipeline/rescore.py`). No
model call is made to build, show or record a question.

### Where questions come from

Two sources, over open jobs that are queued or approved:

- the self-review's questions (`question_open` events; see
  [Self-review](validators.md#self-review));
- gaps the scorer names. Gap text is free prose, so it is reduced to topics
  from a fixed list: experience types (`exp-healthcare`, `exp-public-sector`,
  `exp-sdr-bdr-team-management`…), and, under `prompt_set: sales`, sales tools
  ("Clari proficiency" becomes `tool-clari`) and methods (`method-meddpicc`).
  Under `general`, gap text never turns into a tool or method question, and the
  experience questions are worded for any field ("Have you worked in
  healthcare (providers, payers, health tech)?" instead of "Have you sold into
  healthcare?"). A self-review question that names a tool is still asked.

An `overqualified:` gap is never a question, and neither is a gap about tenure
or degree, because your corpus dates already state those. A gap with no known
topic is not asked.

### What is not asked

A topic your corpus already answers is dropped. A tool or method counts as
answered only when a `skill_index` key is that tool ("Clari", or an alias such
as "SFDC" for Salesforce; not "Clari dashboards") or a never-claim list names
it. A bullet or role that mentions it in passing ("the team used Clari")
doesn't answer it. An experience topic counts as answered when its name matches
the skill index (keys and values), a bullet (text, tags, metric), a role,
education, a story or a never-claim list. A key you answered or declined isn't
asked again (`jobkit undecline` reopens a declined one).

### Ranking and the daily limit

- A self-review key that is only a topic plus generic words
  (`clari_experience`) merges into that topic. Any other key is normalised
  (generic words dropped, words sorted), so two spellings of one question are
  one question. Questions are ranked by the number of open jobs they would
  help, then by those jobs' summed scores.
- `questions.max_per_day` in `config/filters.yaml` (default 3; 0 asks
  nothing). The digest's "Questions for you" section, `jobkit questions`, the
  `get_questions` tool and the end of `get_queue` show the same day's
  questions, each with why it matters and which jobs it would help. A question
  you answer or decline that day still uses its slot. `jobkit questions --all`
  (`get_questions(show_all=true)`) lists every open one without the cap. Days
  are counted with `question_asked` events.

### Answering

`jobkit answer <key> "<text>"` or the `answer_question(key, answer, …)` tool.

- **A plain no is a decline.** The whole answer is judged, not only how it
  starts. An answer that is only a no ("no", "n/a", "never used it", "I
  haven't", "not that I recall", "No, never worked in healthcare") is a
  `question_declined` event, as is `--decline` (tool `decline=true`), which
  needs no text. The question isn't asked again, nothing is added to the
  corpus, and the gap stays a gap. `jobkit undecline <key>` (tool
  `undecline_question(key)`) takes a decline back.
- **An answer that may be a no or a fact is refused**, and nothing is
  recorded: one that opens with a negation but isn't a plain no ("None of my
  patients were readmitted", "No, but I used HubSpot daily"), and one that
  denies the thing asked about ("Used Gong daily; never used Clari", "limited
  exposure to Clari"). Answer just "no" (or use `--decline`), or give the fact
  without opening on a negation. Only a threshold with a figure may follow
  "yes" ("Yes, never below 115% of quota" writes "Never below 115% of quota").
  The text written drops the "yes" and is checked again, so any other opening
  negation is refused. A hyphenated word is one word ("No-code" is not a no).
  "Definitely not", "Absolutely not", "Negative" and "Clari? No" are plain noes.
- **A tool or method answer is one plain sentence of your own use**, because it
  becomes a `skill_index` name a resume may list. The whole answer must have
  this form: `[Clari:] [yes,] I used Clari` (or "I've used", "I have used", "I
  use", "I ran"; "it" may stand for the tool), followed only by how often
  ("daily", "twice a week"), how long ("for 2 years"), where ("at <company>",
  only a company of your corpus roles) and for what ("for forecasting", "in
  weekly reviews"). For example: "I used Clari weekly at Haulmark for two
  years". The tool must be named as the tool: "Clarity", "Clari-like",
  "Clari's", and lowercase "outreach" or "clay" for tools named with everyday
  words, are not it. Anything else is refused with what failed, and nothing is
  recorded: "Yes, daily at Haulmark", "Clari? Yes, I used it", "My manager used
  Clari", "Yes, I tried it", "Yes, if hired", "Only HubSpot and Salesforce".
  This keeps a tool you haven't used off your resume. A tool answer filed with
  `--kind bullet` is held to the same rule, and `--kind skill` is refused for
  anything but a tool or method question. Confirming applies the rule again.
- **A tool or method is added only through its own question.** An answer to any
  other question that names a tool or method from the topic list is refused,
  whoever used it, because a bullet naming a tool would let a letter claim it.
  The reply names the tool's own question (`tool-clari`). Tools named with
  everyday words count only when written as the tool ("Outreach", not "cold
  outreach"). Confirming applies this rule again.
- **Any other answer becomes a proposal** in `data/corpus_proposals.yaml`
  (gitignored), with `corpus_proposal` and `question_answered` events. The
  corpus itself isn't touched. A tool or method answer proposes a `skill_index`
  item named after the tool; any other answer proposes a bullet whose
  `text_long` and `text_short` are your answer as written, with a leading "yes"
  (or "yeah", "yep", "yup") dropped. Nothing is rewritten or improved.
  `--kind skill|bullet` overrides the default. The reply shows the exact entry
  that confirming would write. Answering the same key again replaces a pending
  proposal. A bare "yes" with no fact is refused, and so is an answer whose text,
  with the "yes" dropped, is or may be a no.

### Confirming

`jobkit confirm-proposal <id>` or the `confirm_proposal(id, …)` tool writes the
entry into `data/career_corpus.yaml`.

- It is refused until you set `evidence` (where the fact can be shown from) and
  `metric_confidence` (`verified`, `estimate` or `none`), and, for a bullet, the
  `role_id` it belongs to. You can set them when answering or when confirming;
  they are never defaulted.
- It is also refused when the text names something your corpus `constraints`
  say never to claim; when your answer, or the proposal's text, reads as a no or
  may be one (decline it instead); when a bullet proposal names a tool or
  method; when a skill proposal isn't for a tool or method question or names
  anything but that question's tool; and when the text is empty. These rules
  apply to the exact text the entry would write, because the proposals file is
  plain YAML that can be edited. The one excused text is your answer less a
  leading "yes" when it opens with a negation and denies nothing.
- Every confirm reply, and the "Awaiting your confirmation" list, shows that
  exact text in full, with the skill name for a skill item and your whole
  answer when it differs.
- The entry is inserted as text at the end of its block, so the file's comments
  and layout stay as they are. The result is parsed again and must equal the old
  corpus plus exactly the new entry, or nothing is written. The previous file is
  kept as `data/career_corpus.yaml.bak`. A skill item's value is your text plus
  "(confirmed DATE; evidence: …; metric_confidence: …)". A bullet gets the next
  `b_NNN` id, a topic tag, the figures in your text as `metric`, and your
  `metric_confidence` and `evidence`.
- Confirming records a `proposal_confirmed` event and starts `jobkit rescore
  --job-ids …` in the background for the jobs the question listed.

### `rescore`

`jobkit rescore [--job-ids 12,15] [--dry-run]`; the `rescore(job_ids,
confirm=false)` tool is a dry run, and with `confirm=true` it runs in the
background.

- By default it re-scores open jobs whose score is stale (above), or that have
  no `score_profile`. It covers jobs that are queued, filtered, approved, or
  rejected only by the score gate (newest rejection `score_below_surface:` and
  no decision of yours or of a standing rule). With `--job-ids` it re-scores
  those jobs whatever their state, applied jobs included.
- Afterwards it applies the queue gate and the standing rules again. A queued or
  filtered job below its gate is rejected with the usual reason, and a
  score-gate rejection at or above it is queued again. Approved and applied
  jobs keep their status; their new score is reported.
- A job rejected by the filter, by the sanity layer, by a standing rule or by
  you is never revived. A score-gate rejection is revived only if the filters
  still pass it under the current config; one that fails them now (too old, a
  blocklisted company, a title or pay the config no longer accepts) is not
  re-scored, and the real run records that filter rejection with its reason.
- Each result is a `rescored` event (old and new score, status before and
  after). The `rescore()` tool lists the latest results.

### A file that doesn't parse

If `data/career_corpus.yaml`, `data/corpus_proposals.yaml` or
`questions.max_per_day` can't be read, the digest's "Questions for you"
section, the end of `get_queue` and `get_questions` show one "Questions
unavailable: …" line naming the file (logged as `questions.unavailable`); the
rest of the digest and the queue render as usual. `answer` and
`confirm-proposal` refuse with the same reason and write nothing.

## Tracking applications

- **Logging a submission.** "I submitted Acme" calls the
  `mark_submitted(ids="", job_ids="", submitted_on="")` tool. `ids` are pinned
  queue numbers; `job_ids` are raw job ids, so a job applied to by hand or no
  longer in the queue can be marked. `submitted_on` (`YYYY-MM-DD`) back-dates
  the submission. A job already marked submitted is left unchanged.
- **One application row per submitted job** (`track/applications.py`).
  `mark_submitted` always leaves exactly one application row at `submitted`:
  several rows on a job are folded into one (the first submission, else the
  newest; an `applications_consolidated` event records what was removed), and a
  missing row is created. Re-tailoring a submitted job never sets its row back
  to `tailored` (a `retailored_after_submit` event says it ran), and a
  standing-rule approval never adds a second row. A job submitted on an
  aggregator row that is later merged into the employer's posting keeps its
  application there.
- **Recording a reply.** The `record_outcome(job_id, outcome, note)` tool, with
  outcome `reply`, `rejection`, `interview` or `offer`, writes an `outcome`
  event with the note and moves the application's stage (`replied`,
  `rejected`, `interview`, `offer`; a later reply never undoes a stronger
  outcome). The job must be marked submitted first. It follows a merged
  aggregator id to the employer's posting.
- **Replies from your email.** If you connect your email to Claude, Claude can
  find and record replies itself. See [Email replies](email-responses.md).
- **No reply.** Each night, the `ghost-close` stage marks an application
  `ghosted` when it has been at stage `submitted` for at least
  `tracking.ghost_after_days` (default 30) and has no recorded outcome and no
  email reply other than an automatic receipt. Each one gets an
  `application_ghosted` event. No model call is made. If
  `tracking.ghost_after_days` isn't set, a legacy `followups:` entry of kind
  `close_as_ghosted` supplies the days. `jobkit ghost-close --dry-run` lists
  what would change. A later reply moves a ghosted application on.
- **Repairs.** `jobkit repair-applications [--dry-run]` (tool
  `repair_applications(confirm=false)`, a dry run until `confirm=true`) fixes
  rows left inconsistent by older versions: a `marked_submitted` event with no
  submitted row, several rows on one job, a submitted row whose stage went
  back, a submitted job still listed as approved, an application left on a
  merged aggregator row. A job `applied` with no record of the day is listed for
  you, never guessed. Each fix writes an `application_repaired` event.
- **Stats.** `get_stats` and the digest's funnel show seen, filtered, queued,
  approved, submitted and replied, and "no reply after N days".

## Running the pipeline

- **Freshness banner.** The digest, `jobkit stats`, `get_queue` and `get_stats`
  open with "last successful poll N days ago" (from `boards.last_polled_at`; a
  304 counts as success). Past `schedule.stale_after_hours` (default 36) it is a
  `⚠ STALE DATA` warning, as it is when no poll ever succeeded. The next line is
  the last successful nightly run (`⚠ STALE NIGHTLY` past the same threshold;
  "No nightly run on record" when there has never been one), then a nightly run
  that started and hasn't finished (still running, or `⚠` when its process is
  gone). When the last run had failed stages, a final line names them.
- **`run-all`.** Each stage (poll, feeds, linkedin-guest, discovery,
  resolve-boards, filter, extract, remote, comp, score, unsnooze, queue,
  form-specs, rules, form-specs-approved, tailor, ghost-close, digest) runs
  isolated: an exception is caught, that stage's uncommitted writes are rolled
  back, and the rest still run, so the digest is always written. A stage that
  saw every one of its live model calls fail counts as failed
  (`llm_all_failed`, with the last failure's message; answers from the cache
  don't hide a dead backend), and a stage whose calls failed because the
  Claude login expired gets `llm_backend_down` (below). A
  `run_all.start` event (run id, mode, pid, stage names) opens the run; each
  stage writes a `run_all.stage` log line and event (run id, mode, status,
  seconds, model calls answered and failed, error); `run_all.done` (run id,
  mode, failed stages) closes it. `mode` is `run-all` or `nightly`. The command
  exits 1 when any stage failed.
- **No scoring cap.** Every job that passes the filters is extracted, checked
  and scored in the run. To cap each model stage, set
  `triage.max_jobs_per_stage: N` in `config/filters.yaml` (the setup
  interview asks; no limit is the default); the rest wait for the next run.
  `jobkit triage --limit N` overrides it for one run. Form capture keeps its
  own limit of 50 a run.
- **`same-day`.** `jobkit same-day [--resolve-boards] [<id>...]` scores jobs
  from job-alert emails and web search without waiting for the night. With
  `--resolve-boards` it runs board lookup first. Then the given jobs, plus the
  alert jobs board lookup added in the last two days that have no score yet,
  go through the nightly stages filter, extract, remote, comp, score and
  queue, limited to those jobs; form capture when one of them was queued; and
  the standing rules, so a job that clears your bar is approved the same day.
  Each stage runs isolated, a `same_day.done` event records the outcome per
  job, the last line of its output is a one-line summary, and it exits 1 when a
  stage failed. Tailoring and form filling follow in the scheduled task or the
  next nightly run. The Claude tools never start it while a nightly run is in
  progress; that run picks the jobs up.
- **Background launches.** Tools that do long work start
  `python -m jobkit.cli …` with the server's own interpreter and source, in
  the project root: `approve`, `retailor`, `stage_forms`, `restage`,
  `open_queue`, `run_nightly`, `add_job_url` (its scoring),
  `linkedin_discover`, `resolve_boards`, `rescore` and `rejudge` (with
  `confirm=true`), `confirm_proposal` (its rescore), and `finish_email_check`
  and `record_search_result` (`jobkit same-day`, for jobs from job-alert
  emails). Each
  launch's output goes to `logs/spawn/<timestamp>-<command>.log` and is recorded
  as a `spawn` event and a `mcp.spawn` log line; the reply names the pid and the
  log. `background_status(limit=8)` shows each recent launch as running,
  finished, or failed with its exit code (recorded once as a `spawn_exit`
  event), plus the last lines of its output.
- **The employer's record wins** (hard rule 10). When an employer board is
  polled, an open aggregator row whose apply link names the same posting (board,
  token and job id, or a Greenhouse `gh_jid` link) is merged into the board's
  row: its sources move over as non-canonical, the feed row is closed (not
  deleted), and `merged_into` / `merged_from` events record the pair. Your
  application or decision on the feed row moves to the board's row, which takes
  the feed row's approved, applied, rejected or snoozed status, but only when
  the board's row has no application or decision of its own. When both rows
  have an application or decision, nothing is merged and a `merge_held` event lists the pair for
  you. `jobkit merge-feed-dupes [--dry-run]` (tool
  `merge_feed_dupes(confirm=false)`) applies the same merge once to rows already
  stored.

## Finding the employer's own posting

`jobkit resolve-boards [--job-id N] [--dry-run]` (tool
`resolve_boards(confirm=false)`, and the nightly stage `resolve-boards` after
`discovery` and before `filter`) matches each aggregator job (Adzuna,
Himalayas, …) to the employer's own Greenhouse, Lever or Ashby posting, so the
employer's description is the one scored and the employer's form the one
filled. An aggregator row is never canonical. Code:
`discovery/board_resolver.py`.

- **Which jobs:** open aggregator rows that passed the filter (or, while still
  `new`, would pass it), approved ones first; and LinkedIn cards from the last
  14 days whose company went to the discovery queue (only the company, title and
  location from the card; no LinkedIn row or URL is stored); and the jobs from
  your job-alert emails, the same way (labelled "job-alert email card"). Approving runs it
  again: the `approve` tool starts `jobkit tailor`, which first resolves approved
  aggregator jobs. After such a merge the queue number you approved keeps
  working: `get_job`, `get_artifacts`, `retailor`, `review_artifacts`,
  `restage`, `stage_forms` and `mark_submitted` follow the merged row to the
  employer's posting, which now has your application.
- **Apply links on other systems.** A job whose apply link is on Workday
  (`myworkdayjobs.com`), SmartRecruiters, iCIMS (`careers-*.icims.com`),
  Workable, Recruitee or Rippling is left as it is. A job whose apply link is on
  any other system ApplyForge recognises (Taleo, Jobvite, BambooHR and so on) is
  still matched by company name, because companies change systems.
- **Finding the board:** the boards ApplyForge already has, the board the apply link
  names, and the board registry under the name or its domain label
  ("Soylent.ai" → `soylent`). Two are trusted to be the company's: a board you
  listed in `config/targets.yaml` (polled first when not polled in the last
  day), and the board the apply link names, for the job whose own apply link
  names it. Any other board, including a registry board or one added by
  discovery, `add-url` or an earlier match, must be confirmed (below), because
  the registry is keyed by normalised name only. Only when none of those finds a
  board does it probe the public list APIs: the name (spaces dropped,
  hyphenated, underscored) and its domain label, then, if those find nothing,
  with the suffixes and prefixes careers, -careers, jobs, -jobs, hq, -hq, inc,
  io, ai, app, co, the (`cyberdyne-careers`). A 404 means no such board. A
  company whose own board ApplyForge knows is never probed: a job missing from that
  board is "no matching title". A known board that can't be polled is retried
  next run. When the apply link already names the posting, the `add-url` path
  is used and nothing is probed.
- **Confirming a board found by name.** Another company can own a token, so a
  posting on such a board is picked only when the board confirms the company:
  Greenhouse reports the board's company name and it is the same name ("Tyrell"
  = "Tyrell"; "Ember" is not "Ember Grill"), or the board links to a domain of
  the company (a posting or apply link, a link with a scheme or "www.", an email
  address; a domain written as a word in a description doesn't count): the
  company row's domain, the company name when it is a domain ("Soylent.ai"), or
  the apply link's host when its name spells the company's (`ember.ai` or
  `getember.com` for Ember, not `emberfleet.com`). A shared host never counts:
  notion.site, github.com and github.io, google.com and forms.gle, airtable.com,
  typeform.com, tally.so, gem.com, payroll and HR suites such as ADP,
  Paylocity, UltiPro, Oracle Cloud and Taleo, and every aggregator, job site
  and ATS host (`SHARED_HOSTS` and `JOB_SITE_LABELS` in `board_resolver.py`).
  Lever and Ashby report no company name, so for them only a domain confirms.
  Unconfirmed, the job is flagged **board-match-unconfirmed** with its
  candidates: nothing is ingested, no board is added, and your approval stays
  where it is until you pick. A Greenhouse board whose company name doesn't fit
  at all ("Tyrellium Corp" for Tyrell) is ignored.
- **Picking the posting:** the same title (case, punctuation and "&"/"and"
  aside) and a location that agrees: the same metro, the same city in the same
  state or country, the same state, or both US-wide remote ("Remote - US", or
  "United States" with the remote flag). The US named as a country with no
  remote signal agrees only with another such location. "Americas", "North
  America" and "AMER" are not the US, and a blank or work-mode-only location
  ("Hybrid") agrees with nothing. Places are read as whole words, and a state or
  country the text names must fit the city: "Redmond, Washington" is not
  Washington DC, "Cambridge, UK" is not Boston, "London, Ontario" is not
  London. A city named with its state agrees only with the same city and state.
  A bare "Remote" beside a place takes that place's scope ("London / Remote" is
  not US-wide).
- **One agreeing posting:** it is ingested (`discovery/ingest.py`: the board is
  added if new and polled now) and the feed row is merged into it with the
  merge above. A board row that took your approval is extracted and scored at
  once. A title alone never picks: when none or several postings agree, or the
  one that agrees is on an unconfirmed board, the job is flagged
  **board-match-ambiguous** (or **board-match-unconfirmed**). The digest,
  `get_queue`, `get_job` and `resolve_boards()` list each flag with its
  candidates; pick one with `jobkit resolve-boards --job-id N --pick <posting id
  or URL>` (tool `resolve_boards(job_id=N, pick=...)`). A flag clears when a
  later run finds the title gone. A LinkedIn card's flag is listed for 7 days;
  add the right posting with `add_job_url(url)`.
- **Requests and the cache** (`board_resolution` in `config/filters.yaml`): only
  the public board list APIs and a poll of a matched board, with ApplyForge's honest
  User-Agent, one at a time, `delay_seconds` (1, the floor) apart, at most
  `max_requests_per_run` (300; ceiling 1000). A run commits what it has written
  before each request, so it never holds the database's write lock while it
  waits. HTTP 429 stops the run's probing; other failures leave the company for
  the next run. A company is probed at most once per `probe_cache_days` (14):
  the result is a `board_probe` event; within that window only the boards it
  found are read again (and still need confirming). A confirmed board whose
  posting matched becomes a board row (confidence `probe`) polled every night;
  a probe that found no board marks the company `not_found` in
  `discovery_queue`. `--dry-run` reads the board APIs and writes nothing;
  `resolve_boards()` without `confirm=true` reads only the database.
  `enabled: false` turns it off everywhere except `--pick`.
- **Skipped before any request:** a company on
  `filters.blocklist_companies`, and a repost site (by name, or an apply link
  to one), each with a line in the report; a board whose postings read like
  reposts is dropped and set stale ([Repost sites](#repost-sites)).
- **No match, no automatic approval.** A feed job left unmatched stays in
  your queue marked "no employer form found: apply on the employer's site
  (found via <link>)", and the clears-your-bar rule (`employer_posting: true`) never approves it. You can
  approve it yourself.
- A known company domain contributes its label as a board slug. Crawling
  careers pages directly is on the [roadmap](roadmap.md#more-places-to-find-jobs).

`jobkit add-url` uses the same `discovery/ingest.py` path. It polls only the
board the link names, and doesn't reset an existing company's tier when the
link names a board ApplyForge already knows. A board that can't be fetched makes it
exit 1.

## Models

All model calls go through `src/jobkit/llm.py`, the only place models are
chosen. Extract, remote, comp and score use `claude-haiku-5-5`. Resume
selection, cover letters, drafted form answers, the self-review and the claim
judge use `claude-opus-5-5`. The authenticity critic uses `claude-sonnet-5-5`.

- **With `ANTHROPIC_API_KEY` set:** the anthropic SDK is used, with full model
  ids and `max_tokens=8000`.
- **Otherwise:** the `claude` command (Claude Code, signed in) is used with the
  tier aliases `haiku`, `opus` and `sonnet`. Each call runs isolated, so it
  doesn't pick up your CLAUDE.md, MCP servers, skills or tools: it runs in a
  fresh temporary directory with `--strict-mcp-config` and an empty
  `--mcp-config`, `--tools ""`, `--safe-mode`, `--no-session-persistence` and
  a short `--system-prompt`. The `claude` binary must be on `PATH`, including
  under launchd.

**When the Claude login expires.** The `claude` command reports a lost login
("Failed to authenticate", "OAuth session expired", "Not logged in") on its
output, which `llm.py` reads and logs (`llm.cli_error`) with the CLI's own
words. The call is not retried, nothing is cached, and every later call in the
run fails at once without starting `claude` (for 15 minutes, so a long-lived
tools server recovers after you log in; each run starts fresh). The stage
status is `llm_backend_down` with the error "Claude login expired: run
claude, then /login", and the same line opens the digest, the `get_queue` and
`get_stats` banner and `jobkit schedule status`, and leads the scheduled
run's summary. Run `claude` in a terminal, type `/login`, and the next run
scores what was missed. A used-up usage limit stops the run the same way, and
a rejected `ANTHROPIC_API_KEY` says so. A model the CLI doesn't know stops
that model's calls with "run claude update"; a call answered by an older
model than asked for is logged once per stage as `llm.model_mismatch`.

`jobkit doctor` checks the login with one tiny `haiku` call (60-second
timeout, never cached), and fails with the fix when the login expired, the
call timed out or the model is unknown. When the CLI answers with an older
model or warns that it doesn't recognise the model, doctor adds a warning
telling you to run `claude update`. With `ANTHROPIC_API_KEY` set, or no
`claude` on `PATH`, no call is made.

Both backends send the same system prompt, from `prompts/system.md`. The cache
key is a sha256 of the stage, the resolved model id, the caller's key, the
system prompt and the full rendered prompt, so a corpus edit, a prompt edit or a
model change always misses the cache. A truncated, refused or error reply is
never cached.

## The nightly run

`jobkit nightly` (tool `run_nightly`) is the one unattended run a day: every
run-all stage up to ghost-close, then `stage` (form filling), then `digest`
last so it reports the filling.

- **What it fills.** Every approved job whose newest application is `tailored`,
  not submitted, still open, and not filled since it was last tailored, so a
  form filled on an earlier night isn't reopened every night. All of them run
  in one process through one visible browser at a time (at most 8 tabs per
  batch); each batch's browser is closed when its forms are filled, which frees
  the browser profile for the morning. No answer is carried from one job to
  another, since nobody is in the session.
- **Outcomes.** A job left `needs_human` (a CAPTCHA, a required question) is a
  normal outcome. A job `deferred` (another browser holds the profile) or
  `failed` fails the stage, with the reasons. At the end of the scheduled
  task (when `staging.open_after_daily_run` is on), or when you ask,
  `open_queue` reopens the filled forms for you.
- **A job whose filling keeps failing is capped.** A `failed` job (the page
  raised: a dead apply link, a page that never loads) writes no run, so the next
  night tries it again. Each failure is a `nightly.stage_failed` event. The
  second failure since the job was last tailored lists the job for you instead
  of failing the stage ("Needs you: job 7 Acme: staging failed 2 times, not
  retried: …"), and later nights don't open it, so one broken posting can't
  fail every nightly run. It is listed with the error until you submit it, it is
  rejected, closed or re-tailored (the count starts over), or a filling run you
  start (`jobkit stage --job-id <id>`, tool `stage_forms`) gets a result for it.
  `deferred` is temporary and never capped.
- **Stops reach you** (hard rule 2: detect the challenge, stop, and leave the job for the person). The stage
  summary names every `needs_human` job with its company and reason ("Needs you:
  job 7 Acme: captcha-or-turnstile-visible"). A job whose latest filling run
  stopped before filling anything (a CAPTCHA or bot check, a government-ID or
  payment field, an embedded assessment, no form on the page, no form spec, an
  apply link off the allowlist), or whose nightly filling failed twice
  (`staging-failed:<error>`), can't be reopened by `open_queue`. It is listed
  with its reason, screenshot and apply link in the digest's Staging section, in
  a `⚠` line under the `get_queue` banner, and in `jobkit schedule status`,
  until you submit it, it is rejected or closed, or a later run gets past the
  stop. The Staging section also counts the filled forms waiting in
  `open-queue`.
- Two stops that can be temporary are retried on a later night:
  `no-form-spec-captured` once a form spec with fields exists, and
  `posting-not-found` (a slow page) once more, twice in all per tailoring. A
  CAPTCHA, a government-ID or payment field, an assessment and an apply link off
  the allowlist are never retried automatically.
- There is no submit stage. Filled forms wait for you.
- The run is recorded with mode `nightly`. It exits 1 when any stage failed, and
  refuses to start (exit 1) while another nightly run is still running. The
  `run_nightly` tool starts it in the background, refuses while one is running,
  and warns when a browser holds the profile.

Scheduling the run is covered in [Setup](setup.md#the-daily-run).
