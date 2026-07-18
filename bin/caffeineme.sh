#!/usr/bin/env bash
# caffeineme.sh <hours> — keep THIS Mac awake (no idle sleep) for <hours>, then auto-release.
#
# Uses macOS `caffeinate -i` (prevents system idle sleep). Works on battery and AC.
# Examples:
#   ./caffeineme.sh 10        # stay awake 10 hours
#   ./caffeineme.sh 24        # stay awake a full day
#   nohup ./caffeineme.sh 24 >/dev/null 2>&1 &   # ...and keep going after you close the terminal
#
# Caveats (macOS laptop):
#   * Prevents IDLE sleep only. Closing the lid on battery still sleeps the Mac.
#   * The battery drains while awake. Plug in for long windows.
#   * Ctrl-C (or closing a foreground terminal) releases it early.
set -uo pipefail

prog="$(basename "$0")"

usage() {
  echo "usage: $prog <hours>     e.g. $prog 10   or   $prog 24" >&2
  exit 2
}

[ $# -eq 1 ] || usage
hours="$1"

# Validate: a single positive number (integer or decimal), nothing else.
case "$hours" in
  ''|*[!0-9.]*|*.*.*) usage ;;          # empty, non-numeric, or more than one dot
esac
awk "BEGIN{exit !($hours > 0)}" || usage  # must be > 0

secs=$(awk "BEGIN{printf \"%d\", $hours * 3600}")
[ "$secs" -ge 1 ] || { echo "$prog: <hours> too small (rounds to 0 seconds)" >&2; exit 2; }

end=$(date -r "$(( $(date +%s) + secs ))" "+%a %b %e %H:%M:%S %Z")
echo "$prog: preventing idle sleep for ${hours}h (${secs}s)."
echo "$prog: awake until ${end}.  Stop early with Ctrl-C."
echo "$prog: note — closing the lid on battery still sleeps the Mac."

# Run caffeinate as a child so we can report on early stop and on normal expiry.
caffeinate -i -t "$secs" &
cpid=$!
trap 'echo; echo "$prog: released early."; kill "$cpid" 2>/dev/null; exit 0' INT TERM
wait "$cpid"
echo "$prog: ${hours}h window elapsed — the Mac may now idle-sleep normally."
