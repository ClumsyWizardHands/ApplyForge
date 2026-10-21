# CLAUDE.md — working instructions for this repo

## Read first

`SPEC.md` (v1.5) is the contract, with `ONBOARDING.md` covering milestone M0b. Read it fully before writing code. If something is wrong or unbuildable, say so and propose a change — do not silently deviate.

## Using jobkit (not building it)

When the person wants to use jobkit rather than change its code, follow the project skills in `.claude/skills/` (they ship with the repo): `jobkit-setup` (first run, or "set up jobkit"), `jobkit-daily` ("what's in the queue") and `jobkit-scheduled-run` (what the daily scheduled task runs). The build order below is for developing jobkit; the hard rules apply to both.

## Personal files are local

Each person's data stays on their machine. `data/` (except `data/*.example.yaml`), `config/*.yaml` (except `config/*.example.yaml`), `.env` and `.mcp.json` are gitignored, and the setup interview writes them on each machine. Treat every one of them as hard rule 13 treats the corpus and answer bank: never commit one, never copy one out of git history (older commits hold the original owner's config, not a template), and never read a person's own copy in a test. Working notes made from a person's data (an audit, a review dump, a proposed corpus diff) go in `docs/local/`, which is gitignored; every other file under `docs/` ships. Commit by naming paths, not with `git add -A`. `tests/conftest.py` runs every test in a sandbox holding the templates plus the test profile in `tests/fixtures/config/`. Before committing a change to what ships (a template, a skill, `.gitignore`, the tracked file list), run `scripts/fresh_clone_check.sh --working-tree`: it clones the result and proves a new user's clone installs, passes the tests, and reports missing setup without a personal file present.

## Build order

**M0 → M0b → M1 → M1b → M2 → M2b → M3 → M4 → M4b → M5 → M6.** Do not start a milestone until the previous one's acceptance criteria pass. Report acceptance explicitly at each boundary and stop for review. M0 through M6 are built (`docs/contributing.md`); new work follows the same acceptance discipline.

M0 is not ceremony. The ATS endpoint shapes in SPEC.md §2.1 come from prior knowledge and may have drifted. Verify against live boards and correct the spec before building on them.

M0b (onboarding) is built. The modules in `src/jobkit/onboarding/` do the work; `onboarding/mcp_tools.py` wraps them as MCP tools and `onboarding/cli.py` as `jobkit setup ...` commands, which run the same code and checks; the `jobkit-setup` skill drives them. A change to a setup step keeps all three in step: `tests/test_claude_skills.py` checks that every tool and command a skill names exists.

## Hard rules

1. **The browser never clicks submit.** The person presses submit on every application. A bot check on an employer's form is the employer asking for a person to submit, invisible checks can't be detected reliably, and every ATS submit API takes the employer's key, not the applicant's (SPEC.md §8). Fill code never targets a submit, apply or send control: the static scan of `src/jobkit/autofill/` (no file is exempt) and the runtime click guard enforce it. Do not build, enable or configure automatic submission.  
2. **Never solve a CAPTCHA or bot challenge.** Detect, abort, hand to the human.  
3. **Never bypass the provenance validator.** A failing tailored resume must fail loudly, not get a relaxed check.  
4. **Never auto-select an EEO/demographic field** without an explicit entry in the Answer Bank. Absent an entry: leave blank and flag.  
5. **Never answer a compensation-history question.** Leave blank, flag. Often illegal to ask.  
6. **During onboarding, the corpus draft contains only text from the resume.** No rewriting, no improving, no inferred metrics. Suggested improvements go in the critique, never into the draft. `metric_confidence` and `evidence` are set by the human and never defaulted — every fabrication guard downstream depends on them.  
7. **Calibration proposes; the human disposes.** No config change is applied without explicit per-item acceptance.  
8. **Never generate an answer asserting a fact with no corpus support.** Flag as a novel question instead. This is the highest fabrication-risk surface in the system.  
9. **Do not search for a candidate-side submission API.** SPEC.md §8 documents, per platform, that none exists — every ATS submit endpoint authenticates with the *employer's* credential. Adding adapters widens discovery and form pre-fill only. If you think you've found an exception, stop and raise it rather than building on it.  
10. **Aggregator rows are never canonical.** When the same posting arrives from both a direct ATS feed and an aggregator, the ATS record wins — it carries the real apply URL and the form spec.  
11. **No scraping of gated sites.** Only the documented public board endpoints, plus Playwright on application forms the user is legitimately applying through. **One opt-in exception (off unless the user turns on `feeds.linkedin_guest` in `config/filters.yaml`): LinkedIn's logged-out ("guest") job search, for discovery only** — never logged in, at most 6 searches a day, an honest User-Agent, no cookies, no proxies or disguised browsers, and it stops for the day on any block or challenge. It stores no LinkedIn job rows: for each match it takes the company name, looks up that company's own Greenhouse/Lever/Ashby board, and a posting enters jobkit only when the same job title is found there (the ATS record wins, rule 10); otherwise only the company name goes to the discovery queue. Indeed/Glassdoor stay out (no use of keys not issued to the user).
12. **Never hit live APIs in tests.** Record fixtures. Tests pass offline.  
13. **Never commit** `data/career_corpus.yaml`, `data/answer_bank.yaml`, `.env`, `data/jobkit.db`, `data/raw/`, or `data/browser_profile/`. Verify `.gitignore` before the first commit.

## Architecture invariants

- **Background and foreground are separate.** Polling, extraction, sanity checks, and scoring run headless on a schedule with no human present. The MCP server reads the resulting DB and triggers post-approval actions. Do not put LLM calls in the MCP request path except tailoring and novel-question drafting.  
- **The MCP server is the primary interface.** The CLI exists for cron and debugging. Every user-facing capability must be reachable through an MCP tool. A CLI-only feature isn't done.  
- **SQLite is the only source of truth.** Sheets is a write-only mirror.  
- **Email is read-only.** Employer replies are read only through the person's own email connector in Claude; jobkit never connects to a mailbox, and nothing there is sent, deleted or changed. Only the outcome, the date, a link to the message and a one-line summary are stored, never the body (`src/jobkit/track/responses.py`).  
- **One knob, one owner.** Every threshold lives in exactly one config file. Evaluation order: deterministic filters → sanity checks → standing rules. The rejection log records which layer decided. If two configs appear to control the same behavior, the spec has a bug — raise it, do not reconcile silently.

## Conventions

- Python 3.12, `uv`, `ruff` for lint + format, `mypy --strict` on `src/`.  
- Raw SQL in `db.py` as named queries. No ORM.  
- Prompts live in `prompts/*.md` with a `version:` in YAML frontmatter. Never inline a prompt string in Python. Bump the version on any edit — caches key off it.  
- All LLM calls go through `src/jobkit/llm.py`, which handles retries, logs token counts, and enforces the model tier per stage (Haiku for extract/remote/comp/score; strong model for tailor/cover/novel-question).  
- Playwright runs **headed** with a persistent profile. Never headless — the human needs to see the form they are about to submit.  
- Structured logging to `logs/jobkit.jsonl`. Every filter rejection logs its reason; every autofill run logs filled and unfilled fields.

## Testing

- pytest with recorded HTTP fixtures under `tests/fixtures/`.  
- One recorded payload per ATS adapter, asserting normalization.  
- **Adversarial provenance test (mandatory):** a fixture whose model output contains a plausible but unsourced metric. Assert rejection.  
- **Submit-guard test (mandatory):** static scan of `src/jobkit/autofill/` for submit-like selectors. Assert none.  
- **Authenticity tests:** a slop-laden cover letter fails pass 1; artificially uniform bullet lengths fail pass 2; a bullet rewritten past the drift threshold is caught and reverted to its corpus original; an `estimate`-confidence metric rendered as a flat assertion is flagged; genuinely good output passes all four passes without manufactured criticism.  
- **Onboarding test:** given a fixture resume, assert the generated corpus draft contains no sentence absent from the source text.  
- Remote-conflict test: 10 hand-built deceptive postings, ≥8 detected.

## When you are uncertain

Ask before: changing the data model, adding a dependency not in SPEC.md §10, changing the human-gate structure, or touching anything in Hard Rules. Proceed without asking on: implementation details, module-internal organization, test coverage, error handling.

## Definition of done for a milestone

Acceptance criteria pass · tests green · `ruff` and `mypy` clean · README and `docs/commands.md` updated with new commands and MCP tools · a one-paragraph summary of what changed and what the next milestone unblocks.

