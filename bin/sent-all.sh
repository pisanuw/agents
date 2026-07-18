#!/usr/bin/env bash
# All outbound mail across EVERY persona (incl. draft-approval requests), newest first, each line
# tagged with its persona. Companion to bin/recent-all.sh (ticks) and bin/status-all.sh. Thin
# wrapper over `cagentctl sent` with no --persona (which merges all personas); the logic + tests
# live in cagent/cli.py:cmd_sent.
#   bin/sent-all.sh            # newest 25 across all personas + drafts awaiting approval
#   bin/sent-all.sh 50         # newest 50
#   bin/sent-all.sh all        # everything
#   bin/sent-all.sh --persona scout   # narrow to one persona
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$ROOT/bin/cagentctl" sent "$@"
