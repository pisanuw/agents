#!/usr/bin/env bash
# Cross-persona graduation snapshot. Thin wrapper over `cagentctl readiness` (cmd_readiness in
# cagent/cli.py), kept for discoverability alongside status-all.sh / recent-all.sh. The table
# reads each persona's committed state/personas/<p>/ directly, so it is correct on a mirror clone
# (unlike `cagentctl scorecard`). Optional: --persona <name> to show one.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$ROOT/bin/cagentctl" readiness "$@"
