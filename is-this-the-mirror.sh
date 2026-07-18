#!/bin/bash
# Decide whether THIS clone is a mirror host (pull-only) or the LIVE host (runs the
# dispatcher, produces `cagent tick:` commits). Mirrors may git pull freely; the live
# host must NOT be pulled under, since that races the dispatcher's own git ops.
#
# Two signals, cheapest first:
#   1. Marker file -- an explicit `this-is-the-mirror.md` at the repo root declares a
#      mirror. Deterministic and offline; the operator's stated intent wins.
#   2. Git divergence -- if no marker, fetch and compare local `main` against origin.
#      A mirror is strictly BEHIND origin, and the commits it is missing are `cagent tick:`
#      commits it did not author (the live host produces them). If local `main` is behind
#      by one or more tick commits, this is a mirror. Anything else (ahead, in sync, or
#      behind only by non-tick commits) is treated as NOT a mirror -- fail safe, because
#      wrongly pulling on the live host is the dangerous outcome.
#
# Exit status: 0 = this IS a mirror, 1 = this is NOT a mirror.
# With -q/--quiet, print nothing (status only) for use in `if` guards.
# With --no-fetch, skip the network fetch and compare against cached remote refs.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
MARKER="$REPO_ROOT/this-is-the-mirror.md"

quiet=0
fetch=1
for arg in "$@"; do
    case "$arg" in
        -q|--quiet) quiet=1 ;;
        --no-fetch) fetch=0 ;;
    esac
done

say() { [ "$quiet" -eq 1 ] || echo "$@"; }

# 1. Explicit marker wins.
if [ -f "$MARKER" ]; then
    say "mirror: yes (found $MARKER)"
    exit 0
fi

# 2. Git divergence against origin/main.
cd "$REPO_ROOT"
if [ "$fetch" -eq 1 ]; then
    if ! git fetch --quiet origin main 2>/dev/null; then
        say "mirror: no (no marker; git fetch failed, cannot compare to origin)"
        exit 1
    fi
fi

# `git rev-list --left-right --count main...origin/main` prints "<left>\t<right>":
#   left  = commits on main not in origin  (how far local is AHEAD)
#   right = commits on origin not in main  (how far local is BEHIND)
if ! counts="$(git rev-list --left-right --count main...origin/main 2>/dev/null)"; then
    say "mirror: no (no marker; origin/main not found)"
    exit 1
fi
ahead="$(printf '%s' "$counts" | awk '{print $1}')"
behind="$(printf '%s' "$counts" | awk '{print $2}')"

if [ "${behind:-0}" -gt 0 ] && [ "${ahead:-0}" -eq 0 ]; then
    # Behind only: confirm the missing commits are `cagent tick:` commits from the live host.
    tick_count="$(git log --oneline --grep='cagent tick:' main..origin/main 2>/dev/null | wc -l | tr -d ' ')"
    if [ "${tick_count:-0}" -gt 0 ]; then
        say "mirror: yes (behind origin/main by $behind commit(s), $tick_count cagent-tick)"
        exit 0
    fi
    say "mirror: no (behind origin/main by $behind, but no cagent-tick commits)"
    exit 1
fi

say "mirror: no (no marker; local main not behind origin -- ahead=$ahead behind=$behind)"
exit 1
