#!/usr/bin/env bash
# `cagentctl status` for every ENABLED persona. There is no built-in --all flag;
# status takes a single --persona, so this just loops the enabled round-robin list.
# Extra args pass through to each call, e.g. bin/status-all.sh
# Note: status's last_tick is the shared var/last_tick.json (global, not per-persona);
# use bin/recent-all.sh for each persona's own latest activity.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CTL="$ROOT/bin/cagentctl"
# Capture + check first (pipefail makes this catch a failing cagentctl OR awk), so a failed listing
# surfaces loudly instead of the old `for p in $(...)` silently iterating nothing.
ENABLED="$("$CTL" personas | awk '/\[ENABLED\]/{print $1}')" \
  || { echo "status-all: 'cagentctl personas' failed" >&2; exit 1; }
[ -n "$ENABLED" ] || { echo "status-all: no enabled personas" >&2; exit 0; }
for p in $ENABLED; do                        # names are single [a-z0-9_-] tokens; word-split is safe
  printf '\n==================== %s ====================\n' "$p"
  "$CTL" status --persona "$p" "$@"
done
