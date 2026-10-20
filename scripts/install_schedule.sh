#!/bin/bash
# Retired 2026-10-08. The schedule is one LaunchAgent, com.jobkit.nightly,
# managed by the CLI (and MCP schedule_install / schedule_uninstall /
# schedule_status):
#
#   uv run jobkit schedule install [--dry-run]   # nightly run at schedule.nightly_at
#   uv run jobkit schedule status
#   uv run jobkit schedule uninstall
#
# This wrapper only forwards to `jobkit schedule install` (it also removes the
# com.jobkit.morning / com.jobkit.followups agents this script used to
# install). It no longer sets UV_NO_EDITABLE or passes --no-sync: those ran a
# frozen install that ignored source edits.

set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
exec uv run --directory "$REPO" jobkit schedule install "$@"
