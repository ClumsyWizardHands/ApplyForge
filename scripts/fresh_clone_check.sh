#!/usr/bin/env bash
# Fresh-clone check: does what a new user gets from `git clone` install, pass
# its tests and report missing setup cleanly, with nobody's personal files in it?
#
# Clones this repository into a temporary folder over a file:// URL (the clone
# needs no network), then, in the clone:
#   1. no personal file: no config/*.yaml but the *.example.yaml templates,
#      nothing in data/ but the templates, no .env, .mcp.json, database, logs/,
#      out/, .claude/ file outside .claude/skills/, or local working note in
#      docs/ (docs/local/, the persona note and the dated audit, residual-issues
#      and proposed corpus diff files .gitignore names); and the templates and the jobkit
#      skills that setup needs are there
#   2. `uv sync --locked` (also proves uv.lock matches pyproject.toml)
#   3. `uv run pytest -q` (tests run in their offline sandbox; no personal
#      config; tests/test_setup_config.py also scans every file in the clone
#      for the original owner's identifiers)
#   4. `uv run jobkit doctor`: must report the missing setup and exit 1, not crash
#   5. `uv run jobkit setup-status`: must list the setup steps, the resume first
#   6. doctor and setup-status left the clone as they found it (read-only)
# It also lists personal paths still held by this repository's git history, as
# a warning: every collaborator's clone receives them (publish a fresh snapshot
# of the tree instead of this history).
#
# Usage: scripts/fresh_clone_check.sh [--working-tree] [--keep]
#   (default)       clone HEAD of the current branch: what is committed
#   --working-tree  clone a snapshot of the working tree as the next commit
#                   would hold it (tracked files plus untracked files that are
#                   not gitignored), to check before committing
#   --keep          keep the temporary folder (kept anyway when a step fails)
#
# Read-only for this checkout: it never commits, pushes or changes a file here.
# `uv sync` uses the network only for packages missing from uv's cache.
# Exit status: 0 when every step passes, 1 when one fails, 2 on a usage error.

set -uo pipefail # not -e: each step's status is checked and reported

MODE=head
KEEP=0
for arg in "$@"; do
  case "$arg" in
    --working-tree) MODE=working-tree ;;
    --keep) KEEP=1 ;;
    -h | --help)
      awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
      exit 0
      ;;
    *)
      echo "unknown option: $arg (see --help)" >&2
      exit 2
      ;;
  esac
done

ROOT="$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel 2>/dev/null)" || {
  echo "$0 is not inside a jobkit checkout" >&2
  exit 2
}

# The clone must resolve itself, not this checkout or another environment.
unset JOBKIT_HOME VIRTUAL_ENV UV_PROJECT_ENVIRONMENT

BASE="${TMPDIR:-/tmp}"
TMP="$(mktemp -d "${BASE%/}/jobkit-fresh-clone.XXXXXX")" || exit 2
CLONE="$TMP/jobkit"
FAILED=0

cleanup() {
  if [ "$KEEP" = 1 ] || [ "$FAILED" != 0 ]; then
    echo "temporary folder kept: $TMP"
  elif [ -n "$TMP" ] && [ -d "$TMP" ]; then
    rm -rf "$TMP"
  fi
}
trap cleanup EXIT

step() { printf '\n== %s\n' "$*"; }
pass() { printf '  ok    %s\n' "$*"; }
warn() { printf '  warn  %s\n' "$*"; }
fail() {
  printf '  FAIL  %s\n' "$*"
  FAILED=1
}
indent() { sed 's/^/        /'; }
finish() {
  echo
  if [ "$FAILED" = 0 ]; then
    echo "fresh-clone check passed ($MODE, $(git -C "$CLONE" rev-parse --short HEAD))"
    exit 0
  fi
  echo "fresh-clone check FAILED ($MODE)"
  exit 1
}

# Personal and machine-local paths (CLAUDE.md hard rule 13, .gitignore), less
# the templates and project skills that ship. Reads paths on stdin.
PERSONAL_RE='^\.env$|^\.mcp\.json$|^data/|^config/[^/]+\.ya?ml$|\.db(-wal|-shm|-journal)?$|^(logs|out)/|^\.claude/|^docs/local/|^docs/(audit|residual-issues)-[^/]*$|^docs/[^/]*-proposed-[^/]*\.diff$|^docs/persona-mapping\.md$'
SHIPPED_RE='^data/[^/]+\.example\.yaml$|^config/[^/]+\.example\.yaml$|^\.claude/skills/'
personal_paths() { grep -E "$PERSONAL_RE" | grep -Ev "$SHIPPED_RE" || true; }

# What setup needs from a clone: the templates the interview starts from and
# the skills that run it.
REQUIRED=".env.example .mcp.example.json uv.lock
config/filters.example.yaml config/targets.example.yaml
config/autoapprove.example.yaml config/banned_phrases.example.yaml
config/commute_table.example.yaml
data/career_corpus.example.yaml data/answer_bank.example.yaml
.claude/skills/jobkit-setup/SKILL.md .claude/skills/jobkit-daily/SKILL.md
.claude/skills/jobkit-scheduled-run/SKILL.md"

echo "jobkit fresh-clone check: $ROOT ($MODE)"

# ---------------------------------------------------------------- clone
step "clone"
SRC="$ROOT"
BRANCH="$(git -C "$ROOT" symbolic-ref -q --short HEAD || true)"
if [ "$MODE" = working-tree ]; then
  # A throwaway repository in the temporary folder, holding one commit of what
  # the next commit here would hold: index entries still on disk, plus
  # untracked files that are not ignored. Only $SNAP is ever written; this
  # checkout is only read (tests/test_fresh_clone_check.py holds that).
  SNAP="$TMP/snapshot"
  git init -q "$SNAP" || {
    fail "git init of the snapshot"
    finish
  }
  (
    cd "$ROOT" || exit 1
    git ls-files -z --cached --others --exclude-standard |
      while IFS= read -r -d '' f; do
        if [ -e "$f" ] || [ -L "$f" ]; then printf '%s\0' "$f"; fi
      done |
      tar --null -T - -cf -
  ) | tar -xf - -C "$SNAP" || {
    fail "copying the working tree into the snapshot"
    finish
  }
  git -C "$SNAP" add -A -f &&
    git -C "$SNAP" -c core.hooksPath=/dev/null -c commit.gpgsign=false \
      -c user.name="fresh clone check" -c user.email="fresh-clone-check@localhost" \
      commit -q --no-verify -m "working-tree snapshot" || {
    fail "committing the snapshot"
    finish
  }
  SRC="$SNAP"
  BRANCH=""
fi
if [ -n "$BRANCH" ]; then
  git clone -q --branch "$BRANCH" "file://$SRC" "$CLONE"
else
  git clone -q "file://$SRC" "$CLONE" &&
    if [ "$MODE" = head ]; then git -C "$CLONE" checkout -q --detach "$(git -C "$ROOT" rev-parse HEAD)"; fi
fi || {
  fail "git clone file://$SRC"
  finish
}
pass "cloned ${BRANCH:-$MODE} at $(git -C "$CLONE" rev-parse --short HEAD) into $CLONE"

# ---------------------------------------------------------------- 1. personal files
step "1. no personal files, setup templates present"
found="$(git -C "$CLONE" ls-files --cached --others | personal_paths)"
if [ -n "$found" ]; then
  fail "the clone holds personal or machine-local files:"
  echo "$found" | indent
else
  pass "no personal file in the clone ($(git -C "$CLONE" ls-files | wc -l | tr -d ' ') files)"
fi
missing=""
for f in $REQUIRED; do
  [ -f "$CLONE/$f" ] || missing="$missing $f"
done
if [ -n "$missing" ]; then
  fail "missing from the clone:$missing"
else
  pass "templates and skills present ($(echo "$REQUIRED" | wc -w | tr -d ' ') files)"
fi

# ---------------------------------------------------------------- history (warning)
history="$(git -C "$ROOT" log --branches --remotes --tags --name-only --format= 2>/dev/null |
  sort -u | personal_paths)"
if [ -n "$history" ]; then
  n="$(echo "$history" | wc -l | tr -d ' ')"
  commits="$(echo "$history" | tr '\n' '\0' |
    xargs -0 git -C "$ROOT" log --branches --remotes --tags --format=%h -- 2>/dev/null |
    sort -u | wc -l | tr -d ' ')"
  warn "this repository's history still holds $n personal path(s) in $commits commit(s); every clone"
  warn "receives them (publish a fresh snapshot of the tree instead of this history):"
  echo "$history" | head -8 | indent
  [ "$n" -le 8 ] || echo "        ... and $((n - 8)) more"
else
  pass "no personal path anywhere in this repository's history"
fi

if ! command -v uv >/dev/null 2>&1; then
  fail "uv is not on PATH (https://docs.astral.sh/uv/getting-started/installation/)"
  finish
fi

# ---------------------------------------------------------------- 2. uv sync
step "2. uv sync --locked"
(cd "$CLONE" && uv sync --locked) >"$TMP/uv-sync.log" 2>&1
rc=$?
if [ "$rc" = 0 ]; then
  pass "$(grep -E '^(Resolved|Installed|Audited) ' "$TMP/uv-sync.log" | paste -sd ';' - | sed 's/;/; /g')"
else
  fail "uv sync exited $rc"
  tail -20 "$TMP/uv-sync.log" | indent
  finish
fi

# ---------------------------------------------------------------- 3. pytest
step "3. uv run pytest -q"
(cd "$CLONE" && uv run pytest -q) >"$TMP/pytest.log" 2>&1
rc=$?
if [ "$rc" = 0 ]; then
  pass "$(tail -1 "$TMP/pytest.log")"
else
  fail "pytest exited $rc"
  tail -40 "$TMP/pytest.log" | indent
fi

# Snapshot of the clone's untracked and ignored paths before the read-only
# commands, so step 6 can tell what they added.
untracked() { git -C "$CLONE" status --porcelain --ignored | sort; }
before="$(untracked)"

# ---------------------------------------------------------------- 4. doctor
step "4. uv run jobkit doctor (expected: missing setup, exit 1)"
(cd "$CLONE" && uv run jobkit doctor) >"$TMP/doctor.log" 2>&1
rc=$?
indent <"$TMP/doctor.log"
if grep -q 'Traceback' "$TMP/doctor.log"; then
  fail "doctor crashed (traceback above)"
elif [ "$rc" != 1 ]; then
  fail "doctor exited $rc; a clone with no setup must exit 1"
elif ! grep -q 'problem(s) to fix' "$TMP/doctor.log"; then
  fail "doctor did not report the problems to fix"
else
  missing=""
  for f in $(echo "$REQUIRED" | tr ' ' '\n' | grep '^config/' | sed 's/\.example//'); do
    grep -q "^\[FAIL\].*$f" "$TMP/doctor.log" || missing="$missing $f"
  done
  for f in data/career_corpus.yaml data/answer_bank.yaml; do
    grep -q "^\[FAIL\].*${f#data/}" "$TMP/doctor.log" || missing="$missing $f"
  done
  if [ -n "$missing" ]; then
    fail "doctor did not name these missing files as problems:$missing"
  else
    pass "doctor reported the missing setup (exit 1, no traceback)"
  fi
fi

# ---------------------------------------------------------------- 5. setup-status
step "5. uv run jobkit setup-status"
(cd "$CLONE" && uv run jobkit setup-status) >"$TMP/setup-status.log" 2>&1
rc=$?
indent <"$TMP/setup-status.log"
if grep -q 'Traceback' "$TMP/setup-status.log"; then
  fail "setup-status crashed (traceback above)"
elif [ "$rc" != 0 ]; then
  fail "setup-status exited $rc"
elif ! grep -Eq '^1\. \[ *todo *\] resume' "$TMP/setup-status.log"; then
  fail "setup-status did not put the resume first, as todo"
elif ! grep -q '^Next: resume' "$TMP/setup-status.log"; then
  fail "setup-status did not name the resume as the next step"
else
  pass "setup-status lists the steps; next: the resume"
fi

# ---------------------------------------------------------------- 6. read-only
step "6. doctor and setup-status wrote nothing"
added="$(comm -13 <(echo "$before") <(untracked))"
if [ -n "$added" ]; then
  fail "the clone gained paths:"
  echo "$added" | indent
else
  pass "no new file in the clone (no database, config, data or logs)"
fi

finish
