# ONBOARDING — resume review, intake interview, and config generation

**Companion to SPEC.md.** Covers milestone M0b. Read SPEC.md §4 (career corpus) first.

---

## Why this is its own phase

Every downstream stage reads from four files: `career_corpus.yaml`, `targets.yaml`, `filters.yaml`, and `answer_bank.yaml`. Hand-writing them is possible but slow, and the corpus in particular is where quality lives — a thin corpus produces thin resumes no matter how good the tailoring is.

Onboarding is a conversation that produces all four. Budget about an hour. It runs **once at setup** and **again as calibration** every 50 reviewed jobs.

**How it runs.** The person says "set up jobkit" in a Claude session whose folder is their checkout, and Claude follows the `jobkit-setup` project skill (`.claude/skills/jobkit-setup/SKILL.md`, which ships with the repo; "continue jobkit setup" resumes it). The work is done by the onboarding modules in `src/jobkit/onboarding/`. The skill reaches them through the jobkit MCP tools listed at the end of this file (`onboarding/mcp_tools.py`), or, until the MCP server is registered for the checkout, through their CLI twins (`jobkit setup ...`, `onboarding/cli.py`). Both run the same code and the same checks. `jobkit setup-status` (MCP `setup_status`) shows which steps are done and the next one.

**Where the output goes.** Everything onboarding writes is the person's own and stays on their machine: `data/career_corpus.yaml`, `data/answer_bank.yaml`, the draft, critique and interview answers in `data/` (`data/setup/`), and `config/filters.yaml`, `commute_table.yaml`, `targets.yaml`, `autoapprove.yaml` and `banned_phrases.yaml`. All of it is gitignored. The repository ships only the `*.example.yaml` templates; the data templates describe a fictional person, and no answer is ever taken from them (hard rule 7 below). Never restore a config file from git history; older commits hold another person's configuration.

**The ordering is not arbitrary.** Resume first, interview second. Interviewing someone about what job they want, cold, produces vague answers. Interviewing them *against their own resume* produces specific ones:

> Bad: "What kind of roles are you targeting?" Good: "Your last two roles were a store manager and then a support lead. Do you want to manage a team again, or move into the customer success work you did on the side? Those pull toward different postings and I'd filter differently."

---

## Phase 1 — Resume ingest and review

**MCP tool:** `ingest_resume(path)`

### 1.1 Parse

Accept PDF, DOCX, or plain text. Extract to structured intermediate: contact block, roles (company, title, dates, location), bullets under each role, education, skills section, any summary/objective.

Use a layout-aware parse — two-column resumes and text in tables are the common failure. If extraction confidence is low, say so and ask the user to paste the text instead. **Never silently guess at a garbled parse.** Pasted text (and a plain-text file) is the user's own text, not a parse: never refuse it on a dictionary-word measure, because company, product and tool names are not dictionary words and a technical resume scores low without being garbled. Refuse it only when it is too short or full of unreadable characters, and then ask for the text from the original document, not for the same paste again.

### 1.2 Propose a corpus draft

Convert each extracted bullet into a corpus `bullets[]` entry: `id`, `role_id`, `text_long` (as written), `tags[]` (suggested only from the resume's own skills list), `metric` (the figures the bullet states, verbatim), `metric_confidence: null`, `evidence: null` (both unset until the human sets them in 1.3). The draft is `data/career_corpus.draft.yaml`; it becomes `data/career_corpus.yaml` only through `promote_corpus` (1.6). Every text field of the draft is checked against the extracted resume text before it is written, and a draft holding any other text is refused.

**Hard rule: the draft contains only text that appeared in the resume.** No rewriting, no "improving," no inferring a metric that isn't stated. The model's job here is transcription and tagging, not authorship. Anything it wants to change goes in the critique (1.4), not the draft.

### 1.3 Confirm each bullet with the human

Walk the bullets in batches of five. For each, the human does one of: **keep as-is** · **edit the wording** · **add the number** · **drop it**. Two fields must be set by the human and cannot be defaulted:

- `metric_confidence` — `verified` (you can point at the source) / `estimate` (directionally right, you'd say "roughly" out loud) / `none`  
- `evidence` — where this is defensible from. A dashboard, a doc, a person who'd confirm it. One line.

A bullet that states a figure cannot be `none` and needs `evidence`. "States a figure" means its final wording (`text_long` or `text_short`) contains a number, or its `metric` holds one. The wording is read again on every decision and at promotion, so an edit that adds a number, or a keep that clears `metric`, cannot get a figure past these checks.

This is the single highest-leverage 20 minutes in the whole system. Every downstream fabrication guard keys off these two fields. Under `drift.estimate_style: hedged` (config/banned_phrases.yaml), a bullet marked `estimate` gets hedged language in generated output and a bullet marked `verified` gets stated flatly. Under `confident` (the setting since 2026-10-08), both are stated plainly, but exactly as written: the same value and unit, ranges whole, and "+"/"up to" kept. `metric_confidence` is recorded the same way in both.

Also generate `text_short` for each kept bullet — a compressed variant for tight layouts. **Show both and get confirmation**; a bad compression is a fabrication risk. The draft starts `text_short` as the bullet's own resume text; a shorter variant goes into the corpus only as the human approved it, word for word (`confirm_bullets` `text_short`).

### 1.4 Critique the resume

Separate output, not folded into the corpus. Blunt and specific:

- **Bullets with no outcome.** "Managed the CRM migration" — managed it to what result? These are the weakest lines on any resume and there are usually six of them.  
- **Unquantified claims that could be quantified.** Flag with a prompt: what was the number?  
- **Verb monotony.** If eleven bullets start with "Led" or "Managed," say so.  
- **Claims with no evidence.** Anything the human couldn't source in 1.3 — either find the evidence or cut it.  
- **Scope ambiguity.** "Worked on the platform team" tells a reader nothing about whether you owned it or attended its standups.  
- **Stale weighting.** Six bullets on a 2016 role and two on the current one.  
- **Formatting problems that break ATS parsing** — text in tables, headers/footers carrying content, graphics, two-column layouts, non-standard section headings.

Deliver as a prioritized list, not a wall. Top five things to fix, then the rest.

### 1.5 Fill the gaps by interview

For every bullet the critique flagged as unquantified, ask directly: *"Roughly how many? Over what period? Compared to what before?"* Recover numbers the resume left out. This routinely adds 8–12 usable metrics that were never on the page, and it is the most valuable output of the entire onboarding.

Add STAR `stories[]` the same way, for the three or four accomplishments the human describes with most energy.

Finally, collect 2–3 short samples of the human's own unassisted prose — an email they're proud of, a doc intro, anything they actually wrote. Store as `voice_samples[]` in the corpus. The cover-letter prompt uses them as few-shot voice grounding, which attacks slop at generation time instead of leaving the authenticity review to catch it after the fact.

### 1.6 Write

Emit `data/career_corpus.yaml`. Report bullet count, metric coverage (% with `verified` confidence), and tag distribution. A corpus under 30 bullets or under 40% verified metrics will produce mediocre tailoring — say so plainly rather than declaring success.

---

## Phase 2 — Intake interview

**MCP tool:** `run_intake()`

Conversational and branching, not a form. Reads the corpus from Phase 1 and asks against it. Roughly 20 questions, most of them follow-ups.

### 2.1 Direction

- Continue the current trajectory, or pivot? If pivot, toward what, and what in the corpus supports it?  
- Which of your tagged skill clusters do you want the next role built around — and which do you want *less* of? (The second half is the one people skip and it's the more useful filter.)  
- IC or management? Both acceptable, or a real preference?  
- Title bands you'd accept. Title bands you'd refuse.

### 2.2 Constraints

- Comp floor — the number below which you'd decline regardless. Not the target, the floor. Always ask the default floor (titles outside every track), even when each track has its own: the answer bank's single salary-expectation figure is measured against it. `run_intake` refuses answers without one, and names any track whose floor is under it, because salary-expectation fields on that track's jobs are left blank and flagged.  
- Comp target — what a good outcome looks like.  
- Remote / hybrid / onsite. If hybrid: max days, max commute.  
- Geographic restrictions you'd accept or refuse.  
- **Display location.** Ask this explicitly — it is a positioning decision most people make by accident. What the resume header says need not be the town on your utility bill, and the right answer often varies by job. Confirm the real address separately, for forms. The corpus records one display city (`identity.display_location`, never empty: every tailored header shows one); per-job variation is `location_strategy` in `filters.yaml`.  
- Timeline. Urgent search, active-but-patient, or passive? This sets `volume_mode` and the friction thresholds.

### 2.3 Company shape

- Stage: pre-seed, Series A–B, growth, public, agency, nonprofit, government.  
- Size bands.  
- Sectors you want. **Sectors you'd refuse** — this is the highest-signal question in the interview and it should be asked directly.  
- Named dream companies. These become tier 1 immediately.  
- Named companies you'd never work for. These become the blocklist.

### 2.4 Dealbreakers

Ask explicitly rather than inferring: on-call rotation, travel percentage, security clearance requirements, equity-heavy comp structures, contract or 1099, unpaid take-home assessments over N hours, RTO mandates, industries with ethical objections.

Each answer becomes a `filters.yaml` entry with a rejection reason string, so the log tells you *why* something was killed.

### 2.5 Seed the target list

From sector + stage + size + geography, propose 30–60 companies. Run board discovery on all of them. Report which have discoverable boards and which don't. Ask the human to add anyone missing.

Aggregator feeds (SPEC.md §2.2) then grow the list on their own — this is only the seed.

### 2.6 Write

Emit `config/targets.yaml` and `config/filters.yaml`, fully commented with the interview answer that produced each setting. When a filter later rejects something surprising, the comment tells you which answer to revisit.

Then continue to 2.7 and 2.8 — onboarding is not done when the filters are written.

### 2.7 Populate the Answer Bank

Walk the ~30 recurring screening questions conversationally and write `data/answer_bank.yaml`. Ask each one out loud — never assume, never inherit from the example file: work authorization and sponsorship (a wrong answer is misrepresentation, not a typo), notice period, start date, comp expectation strategy and floor, relocation, onsite tolerance, non-compete status (ask directly — for anyone who has held a sales or leadership role a restrictive covenant is a live possibility), references, how-did-you-hear preference, and the EEO block, where "decline to self-identify" is an explicit, valid choice and an absent entry means the field stays blank and flagged. This is fifteen minutes that converts the submit queue from "fill out a form" to "glance and click," which is the entire point of the system.

### 2.8 Seed standing rules

Propose 3–6 `auto_approve` / `auto_reject` rules derived from the interview answers — e.g., *"tier 1, score ≥ 85, no sanity flags → auto-approve?"* The human accepts or rejects each rule individually. Accepted rules go live immediately: auto-approved jobs are tailored and staged without asking and appear in the digest as done-FYI. This makes the system automatic from day one; calibration (Phase 3) then refines the set from observed behavior rather than creating it from nothing. Never seed a rule that duplicates a sanity-layer control — remote conflict, commute, and underpriced belong to `remote_policy` and `comp_sanity`.

### 2.9 First run and the daily run

The `jobkit-setup` skill ends the interview by running `jobkit doctor` until only warnings remain, starting the first `jobkit nightly` run, and scheduling the daily one: a scheduled task in the Claude app whose prompt only points at `.claude/skills/jobkit-scheduled-run/SKILL.md` in the checkout (so the routine updates with the code), with the macOS LaunchAgent (`jobkit schedule install`) as the fallback. Each is created only on the person's explicit yes. Nothing is approved during setup: the person approves jobs by number afterwards ("what's in the queue", the `jobkit-daily` skill), and always clicks submit.

---

## Phase 3 — Calibration (recurring)

**MCP tool:** `calibrate()`. Runs after every 50 reviewed jobs, or on demand.

The intake interview captured what you *said* you wanted. Your approve/reject decisions record what you *actually* want. Calibration reconciles them.

Analyze the decision log for patterns and propose config changes — never apply them silently:

- **Consistent rejections the filters missed.** *"You've rejected all 7 postings that mentioned an on-call rotation. Add it as a filter?"*  
- **Score miscalibration.** If approvals cluster at 72–78 while the threshold sits at 75, the rubric is compressed. Propose re-anchoring with three of your own recent decisions as new worked examples.  
- **Dead target companies.** *"Nine companies have posted nothing in 90 days. Drop to tier 3 or remove?"*  
- **Standing rule candidates.** *"You've approved every tier-1 posting above 85 without hesitation, 14 times. Auto-approve that pattern?"* This is how `autoapprove.yaml` gets populated — from demonstrated behavior.  
- **Corpus gaps.** The aggregated gap reports across every scored job. *"Kubernetes appeared as a must-have in 31 of 84 postings and your corpus has nothing on it. That's the single skill gating your target roles."*  
- **Funnel diagnosis.** Where the drop-off is. High queue-to-approve rejection means the filters are too loose. High submit-to-reply silence means the resume or the targeting is wrong, and the gap reports tell you which.

Present as numbered proposals. Human accepts or rejects each. Accepted changes are written to config with a dated comment recording the calibration run that produced them.

---

## MCP tools added by this phase

| Tool | Does |
| :---- | :---- |
| `setup_status()` | Which setup steps are done, in order, and the next action (also `jobkit setup-status`) |
| `ingest_resume(path, text)` | Parse (or take pasted text), write the corpus draft and the critique (a separate file) |
| `corpus_draft_status(batch_size)` | What is left to confirm; the next five bullets |
| `confirm_bullets(decisions)` | Human confirmation loop: keep / edit / add the number / drop, sets `metric_confidence` + `evidence` |
| `update_corpus_draft(changes)` | Identity (display location asked, never read), roles, education, positioning, `skill_index`, constraints, voice samples |
| `promote_corpus()` | Writes `career_corpus.yaml` once everything is confirmed; reports corpus health honestly |
| `run_intake(answers)` | Claude conducts the branching interview; this writes `filters.yaml` (each setting commented with the answer) + `commute_table.yaml` |
| `seed_targets(companies)` | Board discovery (URL, registry, public-endpoint probe) for the companies named; appends `targets.yaml` |
| `calibrate()` | Decision-log analysis, numbered config proposals |
| `corpus_health()` | Bullet count, metric coverage, tag distribution, gaps vs recent postings |
| `answer_bank_questions()` | The screening questions in interview order, with what is set |
| `build_answer_bank(answers)` | Writes `answer_bank.yaml` from the human's answers (ONBOARDING.md 2.7) |
| `seed_rules()` | Proposes initial standing rules for per-rule acceptance (ONBOARDING.md 2.8). Not built yet: `autoapprove.yaml` starts with no rules |

---

## Hard rules for this phase

1. **The corpus draft contains only resume text.** No rewriting, no improving, no inferred metrics. Improvements go in the critique.  
2. **`metric_confidence` and `evidence` are set by the human, never defaulted.** Every fabrication guard downstream depends on them.  
3. **Never invent a company, role, date, or credential.** If the parse is ambiguous, ask. A role is current only when the resume says so ("Present") or the human answers that it is: a year-only or missing end date is asked, never left to read as current. Confirm the company's name as the human writes it, not as a registered entity name or a resume abbreviation — it will render on every generated document.  
4. **Never let `display_location` reach an application form field.** Forms take the real address from `answer_bank.yaml`.  
5. **Report corpus weakness honestly.** Under 30 bullets or under 40% verified metrics is a thin corpus. Say so rather than declaring onboarding complete.  
6. **Calibration proposes; the human disposes.** No config change is applied without explicit acceptance.  
7. **Screening answers are asked, never assumed.** Every `answer_bank.yaml` entry is set by the human in 2.7 — nothing inherited from the example file. A wrong screening answer is misrepresentation.

