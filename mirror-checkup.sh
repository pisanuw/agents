#!/bin/bash
# Mirror dashboard: pull latest state then show activity summaries.
# set -e so a failed/conflicted pull surfaces as an error instead of being
# swallowed by the subsequent `clear`, which previously rendered stale dashboards silently.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

# Mirror/live-host detection lives in is-this-the-mirror.sh (marker file, then git
# divergence vs origin/main). Only pull when we are confident this is a mirror; pulling
# on the live host races the dispatcher's own git ops.
if "$REPO_ROOT/is-this-the-mirror.sh"; then
    echo "=== mirror-checkup.sh: this is a mirror host ==="
    echo "=== git pull ==="
    git -C "$REPO_ROOT" pull
    echo "=== pull complete ==="
else
    echo "=== mirror-checkup.sh: this is NOT a mirror host -- skipping git pull ==="
fi


clear
cd "$REPO_ROOT"   # dashboards below invoke ./bin/... by relative path; run them from the repo root
./bin/recent-all.sh
./bin/sent-all.sh
./bin/gate-blocks.sh
./bin/usage-all.sh
./bin/goals-all.sh
./bin/readiness.sh
