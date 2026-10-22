# Contributing

ApplyForge is an evolving project, and the most useful thing you can send is
what got in your way. The command, package and Claude tools are named `jobkit`.

## Pain points and ideas

[Open an issue](https://github.com/ClumsyWizardHands/ApplyForge/issues/new/choose)
and pick a form:

- **Pain point or bug**: something broke, confused you, or took longer than it
  should. Say what you were doing, what happened, and your computer (macOS,
  Linux or Windows). Paste the output of `uv run jobkit doctor` if it's about
  setup. It shows your email address, folder paths and the places in your
  search, so remove the email line, trim the paths, and generalize any town
  names first. Do the same with error text: trim your home-folder
  name from paths and remove any email address. Leave out your resume, answers
  and anything else personal.
- **Idea**: something you'd like ApplyForge to do. The
  [roadmap](roadmap.md) lists what's already planned, and what people ask for
  moves up it.

Fixes land as they're ready; [What's new](changelog.md) lists them. The rest of
this page is for changing the code.

## Documents

- `CLAUDE.md`: working instructions for Claude as a developer of this repo,
  including the hard rules. Claude Code reads it from the repository root.
- `SPEC.md`: the build specification, with dated amendments.
- `ONBOARDING.md`: the setup interview's design (milestone M0b).
- `docs/endpoints_verified.md`: the job-board endpoint shapes checked against
  live boards.
- `docs/research-2026-09-02.md`: the review of LinkedIn auto-apply projects
  and other approaches.
- `.claude/skills/`: the setup, daily and scheduled-run routines Claude follows
  when someone uses ApplyForge.
- Examples, fixtures and tests describe fictional people. The main one is
  Jordan Rivera (`data/*.example.yaml` hold a simpler version of the same
  person). The scoring and setup tests add an engineer, a nurse in Columbus,
  Ohio, and a teacher moving into instructional design
  (`tests/fixtures/personas/`, `tests/fixtures/onboarding_personas/`). A few
  onboarding and form-filling tests use other made-up people on purpose (Morgan
  Ellis, Casey Reyes, Pat Doe), so that a value copied from the templates
  instead of from a new user's own input shows up.

## Milestones

M0 (endpoint verification) through M6 (tracking and calibration) are built,
along with discovery feeds, the board resolver, LinkedIn discovery, any-field
scoring and email reply tracking. M7, interview preparation, is a stretch goal.
New work follows the same discipline as the milestones: acceptance criteria,
green tests, clean `ruff` and `mypy --strict`, and docs updated with new
commands and tools.

## Tests

`uv run pytest -q` works from any directory, and on a fresh clone with no
personal config. Tests never contact live APIs; recorded fixtures live under
`tests/fixtures/`, and `uv run python scripts/record_fixtures.py` records new
ones by hand when a board's shape changes.

`tests/conftest.py` runs every test in a throwaway sandbox under `tmp_path`.
Its `config/` holds the shipped templates (`config/*.example.yaml`) plus the
test profile (`tests/fixtures/config/*.yaml`), never your own `config/*.yaml`.
It redirects the database, log, `out/`, personal-YAML paths and the
LaunchAgents directory into the sandbox (the schedule tests also fake
`launchctl`), and it refuses to open `data/jobkit.db`, to spawn a process, or
to open a network connection. A test that needs to check a spawn (for example
`approve` starting `jobkit tailor`) requests the `spawned` fixture, which
records the command without running it. A new path constant in `src/` that
writes data or reads personal data must be added to `SANDBOXED_PATHS` in
`tests/conftest.py`.

**Golden tests.** The sales test profile (`tests/fixtures/config/filters.yaml`)
stands in for an existing sales user's config, which has no `prompt_set`. Files
under `tests/fixtures/golden/` pin what that profile produces (score prompts,
profile summary, pay flags, queue gate, title classes, standing-rule verdicts,
selection prompts, gap questions, feed requests) byte for byte, so new
behaviour can't move an existing user's results. Regenerate them only on
purpose, with `JOBKIT_REGEN_GOLDEN=1`.

Lint and types: `uv run ruff check`, `uv run ruff format --check src tests`,
`uv run mypy --strict src`.

## Fresh-clone check

`scripts/fresh_clone_check.sh` clones this checkout's HEAD into a temporary
folder over a `file://` URL and proves what a new user gets: no personal file
in the clone (only the `*.example.yaml` templates in `config/` and `data/`, no
`.env`, `.mcp.json` or database, nothing in `.claude/` but the skills, no local
working note in `docs/`) and the templates and skills present; then `uv sync
--locked`, `uv run pytest -q`, `uv run jobkit doctor` (must report the missing
setup and exit 1, not crash), `uv run jobkit setup-status` (must put the resume
first), and that those two wrote nothing. It also warns about personal paths
still in git history. `--working-tree` checks the next commit instead (tracked
plus untracked, not-ignored files) and `--keep` keeps the folder. It never
changes this checkout; `uv sync` goes to the network only for packages missing
from uv's cache. Exit 0 means a new user's clone works.

## Local notes and privacy scans

Local working notes (an audit, a review dump, a proposed corpus diff: anything
made from one person's data) go in `docs/local/`, which is gitignored, as are
the dated `docs/audit-*`, `docs/residual-issues-*` and
`docs/*-proposed-*.diff` notes. Every other file under `docs/` is committed and published.

`tests/test_setup_config.py` scans every file a commit would hold (tracked
files plus untracked ones `.gitignore` lets through) for a maintainer's own
identifiers. The markers live only in two gitignored files:
`docs/local/owner-markers.txt`, one regular expression per line, and
`docs/local/owner-marker-digests.txt`, keyed digests of a marker's words with a
key that stays local too. Without them those scans skip, so run the suite in a
checkout that has them before merging. `JOBKIT_REQUIRE_OWNER_SCAN=1` makes a
missing file fail the scan instead of skipping it, and
`JOBKIT_OWNER_LOCAL_DIR=<path to docs/local>` points the scan at those files
from another checkout or worktree. The common-word list
(`src/jobkit/validate/common_words.txt`) is exempt: it is ordinary English. In
a checkout that holds other untracked files, commit by naming paths
(`git add <path>...`), not `git add -A`.

## Open items

The [roadmap](roadmap.md) has the full list. Two for contributors:

- Adzuna and USAJOBS are still to be checked against live responses in
  `docs/endpoints_verified.md`. Their test fixtures are hand-built in the
  documented shapes, so they prove the request shape and parsing. Record one
  live response from each (with and without locations) into
  `tests/fixtures/feeds/` and confirm the field names.
- Google Sheets mirror: not built yet (an idea on the roadmap). SQLite stays
  the only source of truth either way.
