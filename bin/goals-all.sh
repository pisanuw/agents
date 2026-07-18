#!/usr/bin/env bash
# Active quests (goals) across ALL personas, one block per persona. Thin wrapper over
# `cagentctl goals` (which defaults to sweeping every enabled persona), matching the
# *-all.sh dashboard family. Reads each persona's committed goals.json directly, so it
# is correct on a mirror too. Optional --persona <name> to show just one.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
exec "$ROOT/bin/cagentctl" goals "$@"
