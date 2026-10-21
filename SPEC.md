# JOBKIT — Build Specification

**Version:** 1.5 **Target builder:** Claude Opus in Claude Code **Changes from 1.4** (2026-10-21, §15): the person presses every submit, and the opt-in auto-submit amendment is withdrawn (§8) · follow-up note drafting removed; a plain no-reply rule marks applications ghosted (§7 stage 12) · employer replies read from the person's own mailbox, read-only (§7 stage 12) · `prompt_set` and per-track `pivot`, `min_score` and `emphasis` for searches outside sales (§5 stage 6) · `comp_sanity` thresholds moved into config (§5 stage 5) · location-aware Adzuna and USAJOBS searches (§2.2) **Changes in 1.4, from 1.3:** weekly tailoring cap DELETED — `volume_mode` is the only throughput throttle, default `high` · dedup key canonicalized, `location_bucket` defined · one-knob-one-owner config precedence (§1.7) · `on_remote_conflict` defaults to `flag` · embeddings pinned to local `fastembed` · commute data source specified · queue numbering pinned per session · `get_digest` tool · onboarding now populates `answer_bank.yaml` (2.7) and seeds standing rules (2.8) · M0b runs pre-MCP as a plain Claude Code conversation · Workday routed to manual path · `banned_phrases.yaml` declared authoritative **Companion doc:** `ONBOARDING.md` (milestone M0b)

---

## 0. What this is

A local-first pipeline that finds relevant job postings from open ATS APIs, sanity-checks them on remote policy and compensation, generates a tailored resume and cover letter, pre-fills the application form to the last click, and tracks everything through to outcome.

**Architecture:**

```
        ┌─ BACKGROUND (cron/launchd — runs without you) ────────────┐
        │  poll ATS APIs → normalize → dedupe → deterministic       │
        │  filter → extract requirements → remote check →           │
        │  comp check → score → write to SQLite                     │
        └───────────────────────────────────────────────────────────┘
                                  ↓
                          data/jobkit.db
                                  ↓
        ┌─ MCP SERVER (your cockpit, via Claude Desktop/Code) ──────┐
        │  "what's in the queue"  →  numbered list                  │
        │  "yes to 1, 3, 7"       →  tailor + render + stage form   │
        │  "open them"            →  browser tabs, pre-filled       │
        │  YOU CLICK SUBMIT                                         │
        │  "logged 1,3,7"         →  tracked, replies recorded      │
        └───────────────────────────────────────────────────────────┘
```

**The person presses submit.** Everything before that is automated. §8 gives the two reasons: a bot check on an employer's form is the employer asking for a person to submit, and no ATS offers applicants a submit API.

---

## 1. Design principles

1. **Deterministic filters before LLM filters.** Kill 90% of volume for free before spending a token.  
2. **The LLM selects; it does not invent.** Resume bullets come from a pre-approved corpus. Every claim traces to a `bullet_id`. Fail closed.  
3. **SQLite is the source of truth.** Sheets is a mirror. Everything reproducible from the DB.  
4. **Background work is background.** Polling, extraction, and scoring happen on a schedule with no human present. The human only ever sees a decision queue.  
5. **Approval is one keystroke.** Reviewing should take little effort. See §6.  
6. **Idempotent everything.** Re-runs create no duplicates and cost near-zero on cache hits.  
7. **One knob, one owner.** Every threshold lives in exactly one config location. Evaluation order is fixed: deterministic filters → sanity checks → standing rules, and the rejection log records which layer killed a job. If two configs appear to control the same behavior, that is a spec bug — raise it, do not reconcile silently.

---

## 2. Data sources

### 2.1 Open ATS board endpoints (read — public, unauthenticated)

| ATS | Endpoint shape |
| :---- | :---- |
| Greenhouse | `GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` |
| Lever | `GET https://api.lever.co/v0/postings/{company}?mode=json` |
| Ashby | `POST https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true` |
| Workable | `GET https://apply.workable.com/api/v1/widget/accounts/{sub}?details=true` |
| SmartRecruiters | `GET https://api.smartrecruiters.com/v1/companies/{company}/postings` |
| Recruitee | `GET https://{company}.recruitee.com/api/offers/` |
| Rippling | `GET https://api.rippling.com/platform/api/ats/v1/board/{token}/jobs` |

**Status (v1.5):** Greenhouse, Lever and Ashby are built. Workable and SmartRecruiters were verified live on 2026-08-18 and are not built (next on the roadmap); Rippling was partly verified and Recruitee could not be verified (`docs/endpoints_verified.md`). Employers on Workday, iCIMS, Taleo, SuccessFactors and similar systems are recognised from a pasted link but not read: their job APIs are undocumented or need an employer account (hard rules 9 and 11).

**M0 TASK:** verify every one of these live before building on it. Record actual shapes and date in `docs/endpoints_verified.md`. Rippling especially is newer and less stable.

Caveats worth knowing: SmartRecruiters' public postings feed is **tier-dependent** — customers on lower plans have it disabled. Lever customers can disable their public endpoint or use vanity URLs. Neither ATS publishes a list of which customers are public, so discovery failures are normal, not bugs.

### 2.2 Aggregator and board APIs (NEW in v1.2 — widen the net)

Per-company ATS endpoints return one employer per call. These return many, and cost nothing:

| Source | Endpoint | Scope | Cost |
| :---- | :---- | :---- | :---- |
| USAJOBS | `https://data.usajobs.gov/api/search` | US federal, all agencies | Free, unlimited. Requires a free API key + `User-Agent` email header |
| Adzuna | `https://api.adzuna.com/v1/api/jobs/{country}/search/{page}` | Broad aggregate, 50+ countries, **has salary data** | Free tier ~1,000 calls/month. Needs `app_id` + `app_key` |
| Arbeitnow | `https://www.arbeitnow.com/api/job-board-api` | ATS-sourced, remote flag, `visa_sponsorship` param | Free, no key |
| Remotive | `https://remotive.com/api/remote-jobs` | Remote tech | Free, no key |
| RemoteOK | `https://remoteok.com/api` | Remote tech | Free, no key |
| Himalayas | `https://himalayas.app/jobs/api` | Remote | Free |

**Amended in v1.5:** Adzuna and USAJOBS take optional locations from `feeds.adzuna.locations` (`where`, `distance_km`; at most 3 places a run, each searched with every phrase, up to 10 phrases) and `feeds.usajobs.locations` (`name`, `radius_miles`; at most 5), and Adzuna's country from `feeds.adzuna.country` (default `us`). Both use only their documented search parameters. A feed whose key is missing is skipped before any request and logged with the missing variable names.

**Dead ends — do not build on these.** The Indeed Publisher API was retired in 2023. LinkedIn job data is gated behind partner-only Talent Solutions. Any guide recommending either is out of date.

**Dedup is now load-bearing.** With aggregators in the mix, the same posting arrives from three sources. Cluster on `(normalized_company, normalized_title, location_bucket)` with `jd_hash` as a tiebreak — `location_bucket` is metro-level normalization (a metro slug like `nyc-metro`, or `remote-us` / `remote-global`), and this tuple is the canonical dedup key everywhere in this spec, §13 included. Always **prefer the direct ATS record** — it has the canonical apply URL and the form spec. Aggregator rows exist to *discover* companies you'd never have put on your target list; once discovered, run board discovery (§2.3) and switch to the direct feed.

### 2.3 Form-spec adapters (NEW — the highest-value addition in v1.2)

You cannot submit through an API (§8). But every major ATS lets you **read the application form structure** ahead of time, through a path that requires no employer credentials:

| ATS | Form spec source | Notes |
| :---- | :---- | :---- |
| Greenhouse | `questions` array via per-job detail: `GET /v1/boards/{token}/jobs/{job_id}?questions=true` | Field name, type, required, options. **Corrected at M0** (2026-08-18): questions are NOT in the `?content=true` list payload — one extra request per surviving job. See `docs/endpoints_verified.md` |
| Lever | Retrieve-posting-application-questions endpoint | Required and custom fields per posting |
| Ashby | Application form definition | Each field carries a `path` property used as its identifier |
| SmartRecruiters | `GET /postings/{uuid}/configuration` | Screening questions, **diversity questions flagged with `complianceType: DIVERSITY`**, and the customer's privacy policy |
| Workable | Widget account details | Partial; fall back to DOM scrape |
| Others | DOM scrape at stage time | The universal fallback |

This runs **overnight, in the background, before any browser opens.** Three things it buys you:

1. **Application friction score.** A 4-field Lever form and a 22-field Workday gauntlet are not the same cost. Store `field_count`, `required_count`, `novel_question_count`, and `has_assessment` on `form_specs`, and surface friction in the review queue. Sort cheap-and-strong to the top.  
2. **Novel questions drafted while you sleep.** Match every field against the Answer Bank during the nightly run. Anything unmatched gets drafted then, not at review time. By morning, most applications have zero open questions.  
3. **Per-platform correctness.** SmartRecruiters requires diversity questions be separated from non-diversity ones and the privacy policy displayed; Ashby identifies fields by `path` rather than DOM selector; Greenhouse uses `question[]` naming. Encode these once per adapter instead of discovering them by breakage.

**Adapter contract addition:**

```py
def form_spec(self, job: NormalizedJob) -> FormSpec | None: ...   # None → DOM scrape at stage time
def field_identity(self, field: RawField) -> str: ...             # how THIS ATS names a field
def compliance_fields(self, spec: FormSpec) -> list[str]: ...     # EEO/diversity — never auto-filled without an explicit bank entry
```

### 2.4 Board token discovery

Three strategies in `src/discovery/`, in confidence order: (1) manual — parse token from a careers URL the user pastes; (2) careers-page crawl — regex ATS URLs out of `{domain}/careers` and `/jobs`, including iframe and script srcs; (3) slug probe — derive candidates from the company name, probe each ATS, rate-limited and cached. Store `confidence` and `verified_at`. Three consecutive 404s → `status = 'stale'`, surfaced in digest.

**New in v1.2:** aggregator results feed discovery. When Adzuna or Arbeitnow surfaces a company not in `targets.yaml` whose posting scores above threshold, queue it for board discovery automatically. This is how the target list grows without you maintaining it by hand.

**Note, 2026-10-12: built so far.** Strategy (1) is `jobkit add-url`; strategy (3), the slug probe, is `src/jobkit/discovery/board_resolver.py` (`jobkit resolve-boards`, MCP `resolve_boards`, nightly stage `resolve-boards` after discovery and before filter): Greenhouse/Lever/Ashby list APIs, the name and its domain label first, then the careers/-careers/jobs/hq/inc/io/ai/app/co/the variants; one request at a time (≥ 1 s apart, ≤ 1000 per run); each company probed at most once per `board_resolution.probe_cache_days` (a `board_probe` event; a matched board is stored with `confidence = 'probe'`). It runs for each aggregator job that passes the filter rather than after scoring, so triage scores the employer's JD; and it matches the posting itself, by the same title AND an agreeing location (whole-word place names whose named state or country must fit, stricter than the §2.2 dedup bucket; a bare "Remote" beside a place takes that place's scope) — never by title alone; none or several agreeing postings flag the job `board-match-ambiguous` for the human. The probe runs only for a company with no board known to jobkit, named by the apply URL or in the registry (for a company with one, it could only reach other companies' boards). A probe-found board is a board under the company's *name*, so its posting is picked only when the board is confirmed to be the company's — Greenhouse reports the same company name, or a domain of the company (its company row's domain, a company name that is a domain, an apply-URL host that spells the company name; never a shared host such as notion.site, github.com or a payroll suite) appears on the board; Lever and Ashby report no company name — and is otherwise flagged `board-match-ambiguous` with no boards row added until the human picks. **2026-10-15:** a registry board, and a board jobkit holds that the user did not list in `config/targets.yaml` (added by the registry, discovery, `add-url` or an earlier match), needs the same confirmation (the registry is keyed by normalized name only); only user-listed boards and the board the apply URL names are trusted. An unconfirmed match is flagged `board-match-unconfirmed`. A US location given only as the country with no remote signal agrees only with another such location, never with Remote-US or a metro. Strategy (2), the careers-page crawl, is not built: fetching an employer's careers HTML is not a documented public board endpoint (hard rule 11's wording), so it stays out until that rule changes.

### 2.5 Out of scope

LinkedIn, Indeed, Glassdoor direct. Gated APIs, adversarial anti-bot, retired publisher programs. Not worth the account risk.

**Note, 2026-10-12: one LinkedIn exception.** CLAUDE.md hard rule 11 now allows LinkedIn's logged-out ("guest") job search for discovery only (`src/jobkit/discovery/linkedin_guest.py`, `jobkit linkedin-discover`, MCP `linkedin_discover`; off unless `feeds.linkedin_guest.enabled`). It is never logged in, sends no cookies, uses an honest User-Agent, makes at most 6 searches per local day, and stops for the day on HTTP 429/999/403/401, any redirect, a CAPTCHA or challenge page at any status (Cloudflare's included) or a search page with no job card that is not the empty no-results page. It runs one at a time (a lock held for the whole run) and reads the day's block again before every request. It stores no LinkedIn job rows: a card enters jobkit directly only when its "apply on company site" link resolves to the employer's own Greenhouse/Lever/Ashby board (the add-url path), and otherwise only the company name goes to the discovery queue. Verified live the same day: the guest search returns job cards to a plain HTTP client, but the logged-out posting page no longer carries the employer's apply link, so in practice each card's company is queued and the discovery stage finds its board by name through the registry (§2.4); the run itself never adds or polls a board on a name match. The mode is still against LinkedIn's User Agreement. Indeed and Glassdoor stay out of scope. Details: `docs/why-no-linkedin.md`.

**Third-party auto-apply services** are also out of scope. Several vendors accept a job URL plus a candidate profile and file the application on your behalf. They drive the same submit click this system leaves to the person, which employer bot checks are there to stop (§8), and they require handing a third party your identity documents and full work history.

---

## 3. Data model (SQLite)

Tables from v1.0: `companies`, `boards`, `jobs`, `job_requirements`, `scores`, `applications`, `contacts` and `events`, plus the ones below. *(v1.5: the v1.0 `followups` table is gone. `db.init_db` archives each of its rows into `events` as `followups_archived`, after writing a SQLite backup to `data/jobkit.pre-followups.db`, then drops it.)*

```sql
CREATE TABLE sanity (
  job_id INTEGER PRIMARY KEY REFERENCES jobs(id),

  -- remote reality
  remote_stated TEXT,          -- what the JD claims: remote|hybrid|onsite|unclear
  remote_actual TEXT,          -- what the JD's fine print implies
  remote_conflict INTEGER,     -- 1 if stated != actual
  geo_restriction TEXT,        -- "US only" | "CA residents" | "EU timezones" | null
  office_days_required INTEGER,
  commute_minutes INTEGER,     -- null if remote

  -- comp reality
  comp_published INTEGER,
  comp_required_by_law INTEGER,-- posting location is a disclosure state but no range = signal
  range_width_pct REAL,        -- (max-min)/min; >0.6 is a dodge
  demand_score INTEGER,        -- 0-100: how much the job asks for
  comp_percentile INTEGER,     -- posted comp vs benchmark for title+level+geo
  value_verdict TEXT,          -- fair | underpriced | overpriced | unknown
  value_rationale TEXT,

  flags TEXT,                  -- JSON array of human-readable warnings
  checked_at TEXT, prompt_version TEXT
);

CREATE TABLE form_specs (      -- cached application form structure, fetched overnight
  job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
  source TEXT,                 -- api_questions | dom_scrape
  fields TEXT,                 -- JSON: [{identity, label, type, required, options, compliance_type}]
  field_count INTEGER,
  required_count INTEGER,
  novel_questions TEXT,        -- JSON: questions with no Answer Bank match
  novel_question_count INTEGER,
  drafted_answers TEXT,        -- JSON: pre-drafted overnight, keyed by field identity
  has_assessment INTEGER,      -- embedded test/assessment detected
  friction_score INTEGER,      -- 0-100; low = cheap to apply
  captured_at TEXT
);

CREATE TABLE sources (         -- provenance when the same job arrives from several feeds
  job_id INTEGER REFERENCES jobs(id),
  source TEXT,                 -- greenhouse | adzuna | usajobs | arbeitnow | ...
  source_url TEXT,
  is_canonical INTEGER,        -- 1 for the direct ATS record; aggregators are never canonical
  seen_at TEXT,
  UNIQUE(job_id, source)
);

CREATE TABLE autofill_runs (
  id INTEGER PRIMARY KEY,
  job_id INTEGER REFERENCES jobs(id),
  status TEXT,                 -- staged | needs_human | submitted_by_human | abandoned | failed
  abort_reason TEXT,           -- set when status = needs_human
  filled_fields TEXT,          -- JSON: [{identity, value, source}] where source is
                               -- answer_bank | ai_drafted | human_typed | carried_forward
  unfilled_fields TEXT,        -- JSON: what needs the human
  screenshot_path TEXT,
  created_at TEXT
);

CREATE TABLE authenticity_runs (
  id INTEGER PRIMARY KEY,
  job_id INTEGER REFERENCES jobs(id),
  artifact TEXT,               -- resume | cover_letter
  verdict TEXT,                -- pass | soft_flag | fail
  lexical_flags TEXT,          -- JSON
  structural_flags TEXT,       -- JSON
  drift_flags TEXT,            -- JSON: [{bullet_id, similarity, rendered, source}]
  critic_notes TEXT,
  regeneration_count INTEGER,
  checked_at TEXT, prompt_version TEXT
);

CREATE TABLE decisions (       -- feeds the calibration loop (ONBOARDING.md phase 3)
  id INTEGER PRIMARY KEY,
  job_id INTEGER REFERENCES jobs(id),
  decision TEXT,               -- approve | reject | snooze
  reason TEXT,
  score_at_decision INTEGER,
  friction_at_decision INTEGER,
  auto INTEGER,                -- 1 if a standing rule decided it
  decided_at TEXT
);
```

---

## 4. Career corpus + Answer Bank

### 4.1 `data/career_corpus.yaml`

See `career_corpus.example.yaml`. Identity, roles, tagged bullets with `metric_confidence` and `evidence`, STAR stories, hard constraints. **If it isn't in this file, it cannot appear on a generated resume.** The provenance validator enforces it.

**Location is two fields, not one.** `display_location` is what the resume header says — a positioning choice the tailor may vary per job (`location_strategy` in `targets.yaml`). `home_coordinates` is where the human actually is, used only for commute math and never rendered. Application *form* fields — address, city, state — always take the real address from `answer_bank.yaml`, because those feed background checks and payroll rather than first impressions. Never let a positioning choice leak into a form field.

### 4.2 `data/answer_bank.yaml` (NEW — this is what makes submission fast)

The ~30 questions that appear on nearly every application, answered once — populated during onboarding (ONBOARDING.md 2.7), never inherited from the example file. See `answer_bank.example.yaml`.

Covers: work authorization, sponsorship, notice period, comp expectation (with a formula, not a fixed number), start date, relocation willingness, references, how-did-you-hear, salary history refusal, EEO/veteran/disability selections, and reusable scaffolds for the recurring free-text prompts ("why this company", "biggest impact", "why leaving").

**Matching:** questions are matched to bank entries by embedding similarity + a hand-maintained alias list. Anything below the match threshold is a **novel question** — Claude drafts an answer from the corpus + JD, and *only these* surface for real attention. Every novel question answered gets offered for promotion into the bank, so the bank converges and the review surface shrinks over time.

**EEO fields are never auto-selected without an explicit stored choice.** If the bank has no entry, leave blank and flag.

---

## 5. Pipeline (all background)

**1 · poll** — concurrent fetch with `If-None-Match`, raw JSON cached to `data/raw/{ats}/{token}/{date}.json`, upsert, mark vanished postings `closed_at`. Polite UA with contact address, exponential backoff on 429/5xx, concurrency 4.

**2 · filter** (free) — title regex include/exclude, location, comp floor, posting age, blocklist. Every rejection logged with reason.
*Amended 2026-10-08 (retarget):* role tracks are defined once in `filters.tracks` (ordered, first match wins); the comp floor is per track (`filters.comp.track_floors`, default `filters.comp.floor`), and an OTE-basis track also accepts `comp_max >= floor × ote_base_share` because many postings publish base only. That allowance applies to a base (or unlabelled) figure only: when a posting publishes both, comp is the range of the same kind as the title's floor (the OTE range for an OTE-basis track, the base range otherwise), and a posted figure that is itself OTE is compared with the full floor (review 2026-10-08). The standing-rules title class reads the same track classifier; it has no AE definition of its own. Postings whose location is only country-wide US, blank or "Distributed" go to the remote check (stage 4) instead of being rejected; a location naming a US city or state outside the accepted metros is still a location rejection. `jobkit refilter` re-runs this stage on filter-rejected open jobs after a config change, and never touches a job with a `decisions` row.

**3 · extract** (Haiku) — JD HTML → clean markdown → structured `must_have` / `nice_to_have` / `implied` / `disqualifier`. Cached on `jd_hash + prompt_version`. Strip EEO boilerplate before hashing or every JD looks 40% identical.

**4 · remote reality check** (Haiku) — *NEW.* JDs lie about remote. Extract stated policy, then hunt the fine print for contradictions: "remote" alongside "3 days in office," "remote-friendly" (means onsite with exceptions) vs "remote-first," geographic restrictions ("remote — US only," "must reside in CA"), timezone requirements, quarterly-onsite clauses. Set `remote_conflict` when stated ≠ actual.

*Company-level cross-check:* if 8 of a company's 10 open postings say hybrid-Chicago and one says remote, the remote one is probably a mistake or a bait. Flag it.

For hybrid roles, compute commute minutes from `home_coordinates` (the real location, never `display_location`). Over `max_commute_minutes` → reject regardless of fit. Transit time, not driving distance, wherever transit shapes the commute — in a metro with commuter rail or a subway, some crossings are far faster or slower than the mileage suggests. Data source, in this order: (1) `config/commute_table.yaml`, a small hand-maintained table of transit minutes from home to each destination bucket the user names (one bucket per neighborhood or town they would commute to; for a Chicago search, say, the Loop, Evanston, Oak Park, …) — with one fixed origin this covers nearly every real case; (2) haversine distance × a metro speed factor as the fallback for anything unlisted; (3) Google Routes API **only if** `GOOGLE_ROUTES_KEY` exists in `.env` — optional, never required, never a build dependency.

**5 · comp sanity check** (Haiku) — *NEW.* Two independent questions:

*(a) Is the pay acceptable to me?* Range vs `comp_floor`. Straightforward.

*(b) Is the job worth what it pays?* This is the one you asked for. Compute a **demand score** from: count of must-haves, years of experience required, management scope, on-call expectations, travel percentage, unusual credential requirements, and breadth (a "full-stack + data + DevOps + ML" posting is three jobs). Compare against posted comp for that title, level, and geography. Emit `value_verdict`:

- `underpriced` — high demand, low comp. Example flag: *"9 must-haves, 8+ YOE, people management, on-call — posted at $115–130k. This is a $170k+ scope."*  
- `overpriced` — worth noting; often means undisclosed difficulty (turnaround role, distressed company, brutal hours).  
- `fair` — proceed.

*Range-width dodge:* `(max − min) / min > 0.6` means the range is meaningless. Flag it — several states penalize ranges this wide, so it also signals a sloppy employer.

*Missing range where legally required:* As of 2026, California, Colorado, Hawaii, Illinois, Maryland, Massachusetts, Minnesota, New Jersey, New York, Vermont, and Washington (plus DC) require a good-faith pay range in the posting, and remote postings performable from those states generally fall under their law. A remote posting with no range is therefore either non-compliant or deliberately opaque. Flag as `comp_required_by_law = 1, comp_published = 0` — it's a real signal about the employer.

*Amended in v1.5:* the thresholds are config, not constants. `comp_sanity` in `filters.yaml` holds `max_range_width_pct` (0.6), `flag_missing_range_when_required` (true), `underpriced_below` (160000), `ote_unclear_from` (200000) and `ote_check` (`always`, or `ote_tracks` to apply both OTE flags only to titles whose floor basis is `ote`). The defaults equal the old constants.

Maintain `config/comp_benchmarks.yaml` — your own table of title × level × geo → expected range. Seed it manually; refine as you see real ranges. Do not scrape Levels.fyi.

**6 · score** (Haiku) — 0–100 fit, two-sentence rationale, explicit `gaps` array. Rubric in `prompts/score_fit.md` must contain three anchored worked examples (a 90, a 60, a 30) or scores bunch at 70–85 and become useless. Re-calibrate against your own labels after 50 jobs.

*Amended in v1.5 (prompt sets):* `prompt_set` at the top of `filters.yaml` picks the scoring and bullet-selection prompts. `sales` (and an absent key) uses `score_fit.md` and `select_bullets.md` unchanged, so an existing config's results do not move. `general` uses `score_fit_general.md` (worked bands from several fields, plus a career-change band) and `select_bullets_general.md` (each track's `emphasis`, in the person's words, in place of the sales presets). Any other value fails at load. Tracks gain optional `pivot` (score the track's jobs as a career change: the switch is a gap after the unmet must-haves and before any overqualified gap), `min_score` (the track's queue gate in place of `gates.min_score_to_surface`) and `emphasis` (one line, at most 200 characters; used in selection, never in scoring). Every score records the corpus hash and prompt version, and a `score_fit_general` score also records the prompt name. Rescore treats a new corpus hash or a switch of prompt set as stale, and for `score_fit_general` a new version too; a `score_fit.md` version bump alone leaves sales scores as they are, as before prompt sets. The setup interview writes `general` unless every track is a sales title and no track sets `pivot` or `emphasis`. Golden tests pin the sales path byte for byte.

**7 · form spec** (adapter + Haiku) — *NEW in v1.2.* For every job that survived scoring, fetch the application form structure via the ATS adapter (§2.3). Match each field against the Answer Bank. Draft answers for unmatched fields using the corpus + JD, subject to the `never_draft_if` rules. Compute `friction_score` from field count, required count, novel-question count, and assessment presence.

This is the last background stage, and it runs while you sleep. By morning most applications have zero open questions and a known cost to file.

**8 · queue** — everything above `min_score_to_surface` with no blocking sanity flag lands in the review queue, sorted by score with `friction_score` shown alongside. A 91-fit job behind a 22-field form and an 87-fit job behind a 4-field form are not the same decision.

Stages 9–11 (tailor, cover letter, form staging) run **only after approval**. See §6 and §7.

*Amended 2026-10-08 (nightly run):* the background pipeline runs once a night, unattended, as `jobkit nightly` (MCP `run_nightly`): every stage above, standing rules, tailoring of approved jobs and the no-reply rule (stage 12; v1.5), then stage 11 for approved jobs whose application is tailored and not staged since, then the digest (stage 13) last. Staging runs in one process, one headed browser at a time, closed when its forms are filled so the profile lock is free in the morning, and with no carry-forward between jobs (§7.1: nobody is in the session). It never submits (§8); the staged forms wait for `open_queue`. Each stage runs isolated, the run is recorded (`run_all.*` events with mode `nightly`), the digest shows the last successful nightly run, and the command exits non-zero when a stage failed. The schedule is a scheduled task in the Claude app, whose prompt points at `.claude/skills/jobkit-scheduled-run/SKILL.md` (v1.5). The fallback is one macOS LaunchAgent, `com.jobkit.nightly`, managed by `jobkit schedule install|uninstall|status` (MCP `schedule_*`) at `schedule.nightly_at` in `filters.yaml` (default `"02:00"`). On Linux, cron runs `jobkit nightly`. The LaunchAgent replaces `scripts/install_schedule.sh` and the unread per-stage times in `schedule:`.

---

## 6. The review loop

### 6.1 MCP server

`src/jobkit/mcp_server.py` — stdio MCP server, registered in Claude Desktop / Claude Code config. This is the primary interface. The CLI exists for cron and debugging.

**Tools exposed:**

| Tool | Does |
| :---- | :---- |
| `get_queue(min_score, limit, max_friction)` | Numbered pending jobs with score, company, title, comp, remote verdict, **friction score**, top 3 gaps, sanity flags. Numbering pinned per session — see below |
| `get_job(n)` | Full detail — JD summary, requirements, gap report, comp analysis |
| `approve(ids)` | Accepts `[1,3,7]` or `"all"` or `">85"`. Triggers tailor + render + form staging |
| `reject(ids, reason)` | Dismisses; reason feeds filter tuning |
| `snooze(ids, days)` | Back to queue later |
| `get_artifacts(n)` | Paths to resume PDF, cover letter, gap report, provenance |
| `stage_forms(ids)` | Playwright: open, autofill, screenshot, leave on review screen |
| `open_queue()` | Launch every staged application as a tab, cheapest-friction first; return manifest |
| `restage(id)` | Re-run staging from cached form spec after a dropped session |
| `get_novel_questions(n)` | Only the questions the Answer Bank couldn't cover |
| `answer_novel(n, answers)` | Records answers; offers promotion into the bank |
| `mark_submitted(ids)` | Logs application |
| `record_outcome(job_id, outcome)` | Records a reply, rejection, interview or offer |
| `email_targets()` · `match_email(...)` · `record_email_response(...)` · `finish_email_check()` | Replies from the person's own mailbox, read through their email connector (v1.5, §7 stage 12) |
| `get_stats(period)` | Funnel + conversion rates |
| `get_digest(date?)` | The daily digest — overnight auto-approvals, sanity flags, reposts, replies recorded. Newest digest header is also prepended to `get_queue` output |
| `tune_filters()` | Rejection-reason analysis with suggested config edits |
| `ingest_resume(path)` | Parse resume, propose corpus draft, return critique (ONBOARDING.md §1) |
| `confirm_bullets(batch, decisions)` | Human confirmation loop; sets `metric_confidence` + `evidence` |
| `run_intake()` | Branching interview; writes `targets.yaml` + `filters.yaml` (ONBOARDING.md §2) |
| `seed_targets()` | Propose companies from interview answers, run board discovery |
| `calibrate()` | Decision-log analysis, numbered config proposals (ONBOARDING.md §3) |
| `corpus_health()` | Bullet count, metric coverage, tag distribution, gaps vs recent postings |
| `build_answer_bank()` | Screening-question walk; writes `answer_bank.yaml` (ONBOARDING.md 2.7) |
| `seed_rules()` | Planned: proposes initial standing rules for per-rule acceptance (ONBOARDING.md 2.8). Not built; `autoapprove.yaml` starts with no rules |
| `review_artifacts(id)` | Authenticity verdict + flagged spans for resume and cover letter |

**Queue numbering is pinned.** `get_queue` captures a number → `job_id` map at render time; `approve` / `reject` / `snooze` resolve against that pinned map, never against the queue's current order. A re-sort between render and reply cannot misroute an approval. New render, new map.

**What using it feels like:**

```
You:    what's in the queue
Claude: 12 jobs. Top 5:
        1. Acme — Staff Engineer — $190–240k — remote ✓ — 91 — gaps: Kubernetes
        2. Beta — Solutions Architect — $175–210k — hybrid 2d Chicago (38min) — 87 — clean
        3. Gamma — Sr Engineer — $120–140k — "remote" ⚠ conflict: 3d/wk in office — 84
        ...
        ⚠ Gamma's remote claim contradicts its own fine print.
        ⚠ Delta (#7) is underpriced: 9 must-haves + management at $115k.

You:    yes to 1 and 2, reject 3, everything else above 80
Claude: [tailors 6 · provenance clean · forms staged · 2 novel questions on #5]
```

That's the whole interaction. One message in, one message back.

### 6.2 Standing rules

`config/autoapprove.yaml` lets the queue pre-decide the obvious cases so only genuine judgment calls surface:

```
auto_approve:
  - when: { tier: 1, score_gte: 85, sanity_flags: none }
  - when: { score_gte: 92, remote: true, comp_min_gte: 200000 }
auto_reject:
  # Never duplicate a sanity-layer control here (remote conflict, commute,
  # underpriced belong to remote_policy / comp_sanity). One knob, one owner.
  - when: { tier: 3, score_lt: 75 }
```

Auto-approved jobs still get tailored and staged — they just don't ask you first. They appear in the digest as "done, FYI." Auto-rejects are logged and reviewable weekly, never silently dropped.

Planned: onboarding seeds the first rules (ONBOARDING.md 2.8), proposed from your interview answers and accepted one rule at a time. Today `autoapprove.yaml` starts with none. Calibration then grows the set from observed behavior. Within two weeks the queue should be short and every item on it should be genuinely interesting.

---

## 7. Tailoring and submission

**9 · tailor** (strong model) — select 8–12 bullets from corpus against requirements under a one-page budget; order by relevance within chronological roles; pick the positioning variant matching the role archetype; emit `tailored_resume.json` with `bullet_id` provenance.

**Provenance validator** (`src/validate/provenance.py`) — every rendered bullet maps to a `bullet_id`; every number appears in that bullet's `metric` or `text_long`, with its unit and, for a rate, its period ("$2M/yr" is never "$2M/month"), and is written the way `drift.estimate_style` requires (stage 10b, pass 3: under `confident` only an approximation mark may differ from the source; under `hedged` an estimate keeps its hedge); every named tool or company appears in the corpus. Violation → hard fail, no PDF, log the diff. **Never relax this check to make a build pass.**

Render via Typst → PDF. Emit `gap_report.md` — the unmet must-haves. Aggregated across 50 postings, this tells you which single skill is actually gating your target roles.

**10 · cover letter** — three paragraphs, ~250 words. P1: the role plus one concrete non-generic fact about the company from the JD. P2: two corpus bullets as narrative, with numbers. P3: honest bridge on the largest gap, direct close.

**10b · authenticity review** (`src/validate/authenticity.py`) — *NEW in v1.3.* Runs on **both** the resume and the cover letter, after generation, before render. Three passes, cheapest first.

**Pass 1 — Lexical (free, deterministic).** Reject on a banned-phrase hit.

*Cover letter:* "excited to apply," "passionate about," "fast-paced environment," "align with your mission," "leverage my skills," "proven track record," "wear many hats," "hit the ground running," "perfect fit," "I believe I would be," "as you can see from my resume."

*Resume:* "spearheaded," "orchestrated," "championed," "utilized," "leveraged" (as a verb), "results-driven," "detail-oriented," "team player," "dynamic professional," "cross-functional synergies," "best-in-class," "robust," "seamless," "holistic," "actionable insights," "instrumental in," "played a key role in."

`config/banned_phrases.yaml` is **authoritative**; the phrase lists above are illustrative. Where prose and YAML disagree, the YAML governs. Maintain it there so the list grows without a code change.

**Pass 2 — Structural (free, deterministic).** These are what actually give away machine authorship, and none of them are phrase-level:

- **Bullet length uniformity.** Human resumes have ragged bullets. If the standard deviation of bullet character length is under 15% of the mean, it reads as generated. Flag.  
- **Verb-class monotony.** No more than two bullets opening with verbs from the same semantic class (lead/manage/direct/oversee are one class, not four).  
- **Perfect parallelism.** Every bullet in identical grammatical shape is a tell.  
- **Em-dash and semicolon density** above a per-100-words threshold.  
- **Sentence-length variance** in the cover letter. Uniform ~18-word sentences read as generated; humans write a four-word sentence and then a thirty-word one.  
- **Adjective stacking** — three or more adjectives before a noun.  
- **Company-swap test** (cover letter): if any sentence would be equally true with a different company name substituted, it's filler. Flag per sentence.

**Pass 3 — Drift check (the important one).** Your corpus bullets are text *you* wrote. The tailor is supposed to select and lightly reframe, not rewrite. So diff each rendered bullet against its corpus source:

- Cosine similarity below `drift_threshold` (default 0.75) → the model rewrote rather than reframed. Flag with a side-by-side and default to the corpus original.  
- Any content word in the rendered bullet absent from the source → flag. This overlaps the provenance validator but catches softer drift: adjectives and framing the model added that were never yours.  
- Preserve the figure. Governed by one knob, `drift.estimate_style` in `config/banned_phrases.yaml` (2026-10-08; it replaced `enforce_hedging`). Under `hedged`, a bullet whose `metric_confidence` is `estimate` must not render with a flat assertion: "Roughly 40%" may not become "40%." Under `confident` (the setting since 2026-10-08), an approximation mark ("~", "about", "approximately", "roughly", "around") may be dropped even on an estimate, but the figure must otherwise match its source exactly: same value and unit, no added precision, a range stays the range, and "+", "up to", "under", "over" stay where the source has them and are never added (before or after the number: "$2.6M or more" and "$2.6M-plus" are added bounds too). In both styles an inflated value, added precision, a reshaped figure or a rate moved to another period ("5–7 clients/month" → "5–7 clients/week", "$2.6M annually" → "$2.6M/month") is flagged. The provenance validator and the claims validator apply the same knob.

**Pass 4 — Adversarial critic (strong model, only if passes 1–3 are clean).** Prompt in `prompts/authenticity_critic.md`:

> You screen 200 applications a week and have become good at spotting machine-written ones. Read this and name the three specific things that tip you off. Quote the exact text. If nothing does, say so — do not manufacture criticism.

Returns `verdict: pass | soft_flag | fail` with cited spans. `fail` triggers regeneration with the critique fed back in. Two attempts, then surface to the human with both drafts and the critique.

**On AI-detection tooling:** the goal here is writing that reads as yours, not evading a classifier. Detector scores are unreliable in both directions and optimizing against them produces worse writing. The drift check is the real defense: text that came from your corpus, lightly reframed and verifiably traceable, reads as human because it *is*.

Record every run in an `authenticity_runs` table — verdict, flags, regeneration count. If the same flag fires across many jobs, the prompt needs fixing, not the output.

**11 · stage_form** (Playwright, headed)

1. Launch a persistent browser profile (`data/browser_profile/`) so any logins persist.  
2. Navigate to the application URL.  
3. Load the cached `form_specs` row fetched overnight (§2.3). Only DOM-scrape if the adapter returned `None` or the cached spec is stale. Use the adapter's `field_identity()` to map spec fields to DOM elements — never guess selectors from labels.  
4. Fill every field with an Answer Bank match above threshold. Attach the tailored resume and cover letter PDFs.  
5. Leave novel questions blank and **visually highlight them** (inject a CSS outline).  
6. Screenshot, write `autofill_runs` row with status `staged`.  
7. **Stop before submit.** The person presses it (§8). Do not solve a CAPTCHA.

**Note, 2026-10-12.** Step 4 now runs in this order: the resume and cover letter are attached first, as copies named `First_Last_Resume.pdf` / `First_Last_Cover_Letter.pdf` from the Answer Bank identity (kept in `out/uploads/job-<id>/`; the tailored originals are never changed), into the file input the ATS field id names or else the one input labelled as that file; staging then waits, at most 8 s, for an ATS resume parser to stop rewriting the form; then the other fields are filled. After the fill every filled value is read back; a value the page changed is filled once more, and one still changed is recorded as `overwritten-after-fill`, makes the run `needs_human`, and is counted in a `readback` event keyed by the `autofill_runs` id (no schema change). The CAPTCHA check of step 7 also runs after every click the filler makes and once after the fill; a challenge found then stops the run as `captcha-provoked-by-fill:<field>` with a screenshot, and is never touched. Form-spec capture (§2.3) closes a job whose per-job ATS fetch is a definite not-found (404/410, Lever's or Ashby's not-found body): status `closed`, event `posting_gone`, never tailored or staged; any other fetch failure keeps the DOM-scrape placeholder.

Batch mode is the point. `open_queue()` launches every staged application as a tab in one window, ordered cheapest-friction first, and returns a manifest: which are complete and need only a glance, which have open questions, which hit an abort condition.

### 7.1 The submit queue

A first-class object, not a side effect of staging. `submit_queue` holds everything tailored and staged but not yet filed.

**Per-field provenance.** Every filled field records `source`: `answer_bank` · `ai_drafted` · `human_typed` · `carried_forward`. The browser renders these as colored outlines — bank fields plain, AI-drafted fields amber, empty-required fields red. You see at a glance what you're vouching for. Provenance persists to `autofill_runs` so a bad answer is traceable to where it came from.

**Answering in place.** For an open question you can either type into the form directly, or ask Claude in chat — `answer_novel(job_id, field, text)` writes it into the live DOM. Whichever you use, the answer is captured, and after two uses of a near-identical question it's offered for promotion into the Answer Bank. The queue gets cheaper every week it runs.

**Carry-forward.** Within one batch, an answer given to a repeated question ("Why are you looking to leave?") is offered for the remaining tabs, marked `carried_forward` so you know it wasn't written fresh. Never silently reused across batches — only within a session you're actively in.

**Abort conditions** (from `answer_bank.yaml`) halt staging for that job and mark it `needs_human`: CAPTCHA or Turnstile present, SSN / DOB / government ID requested, payment details requested, an embedded assessment or timed test, or a required field with no bank match and no safe draft. The job stays in the queue with the reason attached; it is never dropped and never guessed at.

**Session hygiene.** One persistent profile, human-paced interaction (`slow_mo`, randomized inter-field delays), a hard cap on concurrent tabs (default 8), and no headless mode ever. Staging runs at a human pace.

**Recovery.** Forms expire, sessions drop, selectors drift. Every staged run screenshots on completion and on failure. `restage(job_id)` re-runs from the cached form spec and refills from `autofill_runs`, so a dropped session costs seconds rather than a redo.

### 7.2 Playwright vs Claude in Chrome

Playwright is the engine: programmatic, headed, persistent profile, screenshots, deterministic replay, and it can hold eight tabs open without supervision. Claude in Chrome is the right tool for the one-off — a Workday form the adapter can't parse, a company portal with its own login flow, anything where you want a conversational assist on a single application rather than a batch. Build Playwright; keep Chrome as the documented manual fallback (`docs/form-filling.md`). Workday-hosted applications route to the manual path by default and take an automatic friction penalty — they typically sit behind per-company account creation, which staging must never perform, and their multi-page DOM defeats the scrape fallback often enough that pretending otherwise wastes the batch.

**12 · track** — on `mark_submitted`, stage → `submitted`. *(Amended in v1.5.)* No follow-up notes are drafted. A plain status rule, run each night as the `ghost-close` stage with no LLM call, marks an application `ghosted` when it has been at `submitted` for `tracking.ghost_after_days` (default 30; a legacy `followups:` entry of kind `close_as_ghosted` supplies the days when the key is absent) with no recorded outcome and no email reply other than an automatic receipt. Replies are recorded by `record_outcome`, or from the person's own mailbox: Claude reads it through the email connector the person set up, and the `email_targets`, `match_email`, `record_email_response` and `finish_email_check` tools record what it finds. jobkit never connects to a mailbox, and nothing there is sent or changed. Each reply is one `email_response` event holding the outcome (acknowledged, rejected, assessment, interview, offer or recruiter_question), the date, the message id, a link and a one-line summary; the body is never stored. Matching is a fixed rule (company name, company domain, the ATS applied through, title words), and an unclear match goes to the person.

**13 · digest** — daily markdown written to `out/digest/{date}.md` and exposed via the `get_digest` MCP tool (the newest digest header is also prepended to `get_queue` output, so overnight activity is visible without asking): new jobs above threshold, auto-approved FYIs, sanity flags, reposts, stale boards, replies recorded since the last digest, and the funnel (seen → filtered → queued → approved → submitted → replied) with conversion rates at each step. The funnel is how you learn whether targeting or the resume is the bottleneck.

---

## 8. Why the person presses submit

There are two reasons. One is a choice about consent; the other is how the ATS platforms work. *(Amended in v1.5, owner decision 2026-10-21: the opt-in auto-submit amendment of 2026-10-09 is withdrawn, and nothing in jobkit submits an application.)*

**Consent.** Many employers put a CAPTCHA or another bot check on their application forms. A bot check is the employer asking for a person to send the application, and jobkit respects it. Some checks are invisible, so software can't reliably tell which forms have one. The person therefore presses submit on every application, and when a form shows a challenge, staging stops and hands it to the person (hard rule 2).

**No applicant-side submit API.** Every ATS that accepts applications through an API does so only with a credential issued to the hiring company. Verified individually:

| ATS | Submit endpoint | Auth required | Whose credential |
| :---- | :---- | :---- | :---- |
| Greenhouse | `POST /v1/boards/{token}/jobs/{id}` | HTTP Basic, API key | Employer's, from their API Credentials page |
| Lever | Apply-to-a-posting | API key | Employer's, generated by a Super Admin in integration settings |
| Ashby | `applicationForm.submit` | `candidatesWrite` permission | Employer's API key |
| SmartRecruiters | `POST /postings/{uuid}/candidates` | `X-SmartToken` header | Employer's, minted in their admin or partner OAuth |
| Workable / Recruitee / Rippling | Same pattern | Account-scoped | Employer's |

Each of these submit endpoints takes a key issued to the hiring company, so an applicant has no API to submit through. More ATS adapters add jobs to find and forms to fill; they add no way to submit (hard rule 9). Submitting for the person would mean pressing the employer's button in a browser, which is what the bot checks are there to stop.

If employers ever offer an approved way for software to apply on a person's behalf, that is consent, and jobkit could use it.

What is left for the person on a filled form:

1. **Free-text custom questions** vary per job; a wrong answer can't be taken back.  
2. **EEO / veteran / disability** sections are legally the applicant's own responses.  
3. **Work authorization and sponsorship**: a wrong answer is misrepresentation.  
4. **Turnstile / hCaptcha** guards many forms. jobkit does not solve them.  
5. **An application can't be withdrawn.** A wrong resume at a tier-1 company uses up that company.

The Answer Bank answers 1–3, so most forms need only a review and the click.

---

## 9. Volume

The system supports high throughput. Configure it in `config/filters.yaml`:

```
volume_mode: high        # focused | balanced | high
# focused:  ≤5/day,  full tailoring, manual review of every one
# balanced: ≤15/day, full tailoring, standing rules handle the obvious
# high:     ≤40/day, full tailoring on tier 1–2, template+swap for tier 3
#
# volume_mode is the ONLY throughput throttle. There is NO weekly cap —
# v1.3's max_tailored_per_week is deleted. Do not reintroduce it.
```

**Cost:** roughly $0.15–0.40 per fully tailored application in tokens. At 40/day that's $6–16/day. Real but not prohibitive.

**Decision, recorded:** the dial starts at `high` as a design choice: the default is a wide-net search, and every artifact passes the same gates at any volume. A user searching more narrowly can set `balanced` or `focused`. The known cost, stated once: response rate scales sub-linearly with volume, and ATS dedup plus recruiter pattern-recognition punish obvious mass application. The guardrail is the funnel, not a throttle — authenticity review and the provenance validator gate every artifact at every volume, and quality is never bought back by capping throughput. If reply rate drops below 8% over any rolling 100 submissions, `calibrate()` proposes dropping to `balanced` and names which funnel stage is leaking. Until the stats say otherwise, run wide.

---

## 10. Stack

Python 3.12 · `uv` · Typer (CLI) · **MCP Python SDK (`mcp`)** · `httpx` async · SQLite raw SQL, no ORM · `markdownify` + `bs4` · `anthropic` SDK (Haiku for extract/remote/comp/score, strong model for tailor/cover) · `fastembed` (local embeddings for answer-bank matching and the drift check — no API key, keeps the pipeline local-first) · **Playwright (headed, persistent profile)** · Typst for PDF · YAML + Pydantic config · `gspread` optional mirror · pytest with recorded fixtures.

Secrets in `.env`. `data/` gitignored except `*.example.*`. `config/*.yaml` are one person's choices and gitignored too (2026-10-12, so anyone can run their own jobkit from the repo); each ships as a commented `config/*.example.yaml` template, and the setup interview writes the real files.

---

## 11. Repo layout

```
jobkit/
├── CLAUDE.md · SPEC.md · pyproject.toml
├── config/                       # *.yaml gitignored (personal); *.example.yaml templates tracked
│   ├── targets.yaml · filters.yaml
│   ├── autoapprove.yaml          # standing rules — seeded in onboarding 2.8, grown by calibration
│   ├── banned_phrases.yaml       # authenticity pass 1, editable without a code change
│   ├── comp_benchmarks.yaml      # title × level × geo → expected range
│   └── commute_table.yaml        # transit minutes home → destination bucket (§5 stage 4)
├── data/
│   ├── career_corpus.yaml        # gitignored
│   ├── answer_bank.yaml          # gitignored
│   ├── jobkit.db · raw/ · browser_profile/
│   └── *.example.yaml
├── prompts/                      # each versioned in frontmatter
│   ├── extract_requirements.md · remote_check.md · comp_sanity.md
│   ├── authenticity_critic.md · resume_critique.md · intake_interview.md
│   ├── score_fit.md · select_bullets.md · cover_letter.md
│   ├── score_fit_general.md · select_bullets_general.md   # prompt_set: general (v1.5)
│   └── novel_question.md
├── templates/resume.typ · cover_letter.typ
├── out/{company}-{role}-{date}/  # resume.pdf, cover.pdf, gap_report.md, provenance.json
├── src/jobkit/
│   ├── mcp_server.py             # PRIMARY INTERFACE
│   ├── cli.py · db.py · models.py · llm.py · schedule.py  # schedule: launchd nightly agent
│   ├── adapters/            # ats/ (board+form) and feeds/ (aggregators)
│   ├── discovery/
│   ├── pipeline/                 # filter, extract, remote, comp, score, tailor, cover
│   ├── autofill/                 # browser.py, form_spec.py, answer_bank.py
│   ├── validate/                 # provenance.py, authenticity.py
│   ├── onboarding/               # resume_parse.py, ingest.py, critique.py, corpus_confirm.py,
│   │                             # intake.py, targets.py, answer_bank_builder.py, status.py,
│   │                             # calibrate.py; mcp_tools.py + cli.py (`jobkit setup ...`)
│   ├── render/ · track/
├── scripts/verify_endpoints.py · install_schedule.sh  # retired: runs `jobkit schedule install`
└── tests/
```

**Adapter contract** — adding an ATS is one file:

```py
class BoardAdapter(Protocol):
    ats_name: str
    def board_url(self, token: str) -> str: ...
    async def fetch(self, token: str, etag: str | None) -> FetchResult: ...
    def parse(self, payload: Any) -> list[NormalizedJob]: ...
    def detect_token(self, html: str) -> str | None: ...
    def form_spec(self, job: NormalizedJob) -> FormSpec | None: ...   # None → DOM scrape at stage time
    def field_identity(self, field: RawField) -> str: ...             # how THIS ATS names a field
    def compliance_fields(self, spec: FormSpec) -> list[str]: ...     # EEO/diversity — never auto-filled without an explicit bank entry
```

**Aggregator adapters** implement a narrower protocol — they discover, they don't stage:

```py
class FeedAdapter(Protocol):
    source_name: str
    requires_key: bool
    async def search(self, query: SearchSpec) -> list[NormalizedJob]: ...
    # No form_spec. Aggregator rows are never canonical; once a company is
    # discovered this way, run board discovery and switch to the direct feed.
```

---

## 12. Milestones

**M0 — Ground truth (½ day).** `verify_endpoints.py` green for Greenhouse, Lever, Ashby. `docs/endpoints_verified.md` written. *Accept:* all three return parseable JD text for a known posting; Greenhouse `questions` array captured.

**M0b — Onboarding (2 days).** Resume parse, corpus draft, bullet confirmation loop, resume critique, voice samples, intake interview, config generation, Answer Bank population, standing-rule seeding, target seeding. Runs as a plain Claude Code conversation calling `src/jobkit/onboarding/` directly — the MCP wrappers for these functions land in M3; M0b precedes the MCP server by design. Full detail in `ONBOARDING.md`. *Accept:* from a real resume PDF and one conversation, the system produces a `career_corpus.yaml` containing **only** text the human confirmed, with `metric_confidence` and `evidence` set on every bullet by the human — plus populated `targets.yaml`, `filters.yaml`, and `answer_bank.yaml` — every screening answer confirmed by the human, never inherited from the example file — and a human-accepted seed set of standing rules in `autoapprove.yaml`, each setting commented with the answer that produced it.

**M1 — Ingest (1 day).** Schema, three adapters, `poll`, raw caching, dedupe, `closed_at`. *Accept:* polling 20 companies twice creates zero duplicates and full JD text.

**M1b — Aggregator feeds (1 day).** USAJOBS, Adzuna, Arbeitnow, Remotive, RemoteOK behind `FeedAdapter`. Cross-source dedup. Auto-queue newly discovered companies for board discovery. *Accept:* a job posted by a company on both a direct ATS feed and an aggregator produces exactly one row, with the ATS record marked canonical and both recorded in `sources`.

**M2 — Triage (1.5 days).** Filters, `extract`, **remote check**, **comp sanity**, `score`, `digest`. *Accept:* on 200 real jobs, filters log every rejection reason; remote-conflict detection catches a hand-built set of 10 deceptive postings with ≥8 hits; comp check flags a known-underpriced posting; score rank-correlates ≥0.7 with your hand labels. **[AMENDED at M2 acceptance, 2026-08-18, owner decision: hand-label correlation waived — replaced by calibration over the real decisions log (ONBOARDING.md Phase 3), which measures the same thing against actual approve/reject behavior every 50 decisions.]**

**M2b — Form specs (1 day).** Form-spec adapters for Greenhouse, Lever, Ashby, SmartRecruiters, with DOM-scrape fallback. Overnight Answer Bank matching and novel-question drafting. Friction scoring. *Accept:* for 20 real postings across all four ATSs, the captured form spec matches the live form's field count within ±1, compliance-flagged fields are correctly identified, and SmartRecruiters' `DIVERSITY` questions are separated from the rest.

**M3 — MCP + review loop (1 day).** MCP server, all queue tools, MCP wrappers for the M0b onboarding functions, standing rules engine. *Accept:* from a fresh Claude Desktop session you can see the queue, approve by number, and get artifact paths back — with no CLI use at all.

**M4 — Tailoring (2 days).** Bullet selection, provenance validator, Typst render. (The corpus already exists from M0b.) *Accept:* (a) an adversarial fixture attempting to induce a fabricated metric is caught by the validator; (b) three resumes for genuinely different roles differ in bullet selection by ≥40%; (c) one page, clean render.

**M4b — Authenticity review (1 day).** Four-pass reviewer over both artifacts: lexical, structural, drift, adversarial critic. *Accept:* (a) a deliberately slop-laden cover letter fails pass 1; (b) a set of bullets with artificially uniform length fails pass 2; (c) a tailored bullet rewritten beyond the drift threshold is caught and reverted to its corpus original; (d) under `drift.estimate_style: hedged`, an `estimate`-confidence metric rendered as a flat assertion is flagged — under `confident`, it is not, but an inflated, more precise or reshaped figure (a range cut to its top end, a dropped "up to") is; (e) genuinely good output passes all four without manufactured criticism.

**M5 — Submit queue (2.5 days).** Answer Bank, Playwright staging, per-field provenance rendering, novel-question flow, carry-forward, abort conditions, batch `open_queue`, `restage` recovery. *Accept:* on live Greenhouse, Lever, and Ashby forms, ≥85% of fields fill correctly, every filled field carries a correct `source`, EEO fields respect stored choices or stay blank, abort conditions halt cleanly with a reason, novel questions are highlighted, and **the browser never clicks submit** — assert with a test that fails if any submit selector is ever targeted.

**M6 — Tracking + calibration (1.5 days).** Applications, events, drafted follow-up notes (removed in v1.5; see §7 stage 12), Sheets mirror, funnel stats, launchd/cron install script, and the `calibrate()` loop over the decisions log. *Accept:* after 50 seeded decisions, calibration surfaces at least one real filter pattern, one standing-rule candidate, and the top corpus gap — as numbered proposals that require explicit acceptance.

**M7 — Interview prep (stretch).** `prep(job_id)` — matching STAR stories, likely questions per requirement, questions to ask, known contacts.

---

## 13. Things that will go wrong

- **Board tokens rot** when companies migrate ATS. Surface stale boards; don't fail silently.  
- **JD HTML is filthy.** Strip EEO/benefits boilerplate before hashing.  
- **Scores bunch** without anchored rubric examples.  
- **Duplicate postings** — same role in 5 locations. Cluster on the canonical key from §2.2: `(normalized_company, normalized_title, location_bucket)`, `jd_hash` tiebreak.  
- **Form selectors drift.** Greenhouse and Lever redesign periodically. Keep adapters thin and screenshot every staged run so failures are diagnosable after the fact.  
- **Corpus drift** invalidates old outputs. Store `corpus_hash` in `provenance.json`.  
- **Auto-submit.** Out of scope; see §8.

---

## 14. Success criteria

Measure these:

- **Response rate** (replies ÷ submitted). Target 15%+. Below 8%, the funnel tells you whether it's targeting or the resume.  
- **Time from posting appearing to submitted.** Target under 48 hours. Early applications get read; week-three applications get skimmed.  
- **Human minutes per application.** Target under 2, including both glances.  
- **No fabricated claims.** One invented figure that comes up in an interview costs more than the system saves in a year.


---

## 15. Amendments in v1.5 (2026-10-21)

Owner decisions of 2026-10-21. Each changes the sections named; the text there carries the detail.

1. **Submit (§0, §7 stage 11, §8, §13).** The person presses submit on every application. The 2026-10-09 amendment to CLAUDE.md hard rule 1, which planned an opt-in unattended submit step, is withdrawn: there is no submit module and no setting that enables one, and the static scan of `src/jobkit/autofill/` exempts no file. The reason given to users is consent: a bot check on an employer's form is the employer asking for a person to submit, invisible checks can't be detected reliably, and every ATS submit API takes the employer's key, not the applicant's.
2. **Follow-ups (§3, §6.1, §7 stages 12 and 13).** Follow-up note drafting is removed: the drafting stage, its prompt, its MCP tools (`get_followups`, `run_followups`, `mark_followup_sent`), the `jobkit followups` command and the digest section. The `followups` table is archived into `events` (`followups_archived`, one per row, drafts included) after a SQLite backup to `data/jobkit.pre-followups.db` (never overwriting an existing backup: a later run that finds rows writes `-2`, `-3` ..., up to five copies), then dropped; the migration is idempotent and logged. An empty table (an old open session re-creates one on every call) is dropped with no backup. The 30-day no-reply bookkeeping stays as the `ghost-close` status rule, `tracking.ghost_after_days`.
3. **Email replies (§6.1, §7 stage 12; CLAUDE.md architecture invariants).** Employer replies can be recorded from the person's own mailbox, read only through the email connector the person set up in Claude. jobkit never connects to a mailbox, sends or changes nothing there, and stores only the outcome, the date, a link and a one-line summary. The unattended scheduled run checks email only with `tracking.email_check: true` (the setup interview writes it); asking Claude to check works either way.
4. **Any field (§2.1, §2.2, §5 stages 5 and 6).** `prompt_set: sales | general` (absent means `sales`), per-track `pivot`, `min_score` and `emphasis`, the `comp_sanity` thresholds as config, `tracking.ghost_after_days`, Adzuna and USAJOBS locations (with `feeds.adzuna.search_nationwide` for someone near a place who also takes remote work), `filters.locations.match_state` (a place written "City, ST" also checks the posting's state), `feeds.usajobs.full_description` (duties, qualifications and education stored with the summary), and a general requirement-extraction prompt under `prompt_set: general`. With places configured, Adzuna's location adds the state it reports and a USAJOBS "Multiple Locations" posting takes its site in a searched place. Deliberate changes on the sales path, listed under Updating in docs/setup.md: an Ashby range paid by the hour, week or month is stored as unpublished pay, and `add-url` exits 0 with a note on a link it can't use. Every new behaviour hangs off a key an existing config lacks, so with the key absent the code runs as before; golden tests on the sales test profile prove it.
5. **Coverage (§2.1).** Discovery is strongest for employers on Greenhouse, Lever and Ashby. Links on other systems are recognised and named, not read. Next on the roadmap: a paste-the-description path for jobs on any site, and Workable and SmartRecruiters board adapters.
6. **Schedule (§5, nightly amendment).** The setup interview schedules the nightly run as a scheduled task in the Claude app; the macOS LaunchAgent is the fallback.
