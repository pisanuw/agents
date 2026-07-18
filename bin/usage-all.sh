#!/usr/bin/env bash
# Token + cost usage across ALL personas, rolled up from each persona's committed tick journal.
# Read-only: no claude call, no network, so it is correct on the mirror. Shows the per-kind
# breakdown (cognition / gate-check / research / reflection) by default.
# Extra args pass through to `cagentctl usage`, e.g.:
#   bin/usage-all.sh                 # all personas, all time, with by-kind split
#   bin/usage-all.sh --days 7        # last 7 days
#   bin/usage-all.sh --persona scout  # one persona
#   bin/usage-all.sh --json          # machine-readable
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$ROOT/bin/cagentctl" usage --by-kind "$@"
