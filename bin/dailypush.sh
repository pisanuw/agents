#!/usr/bin/env bash
# launchd entrypoint for the once-daily commit + push. Separate from the heartbeat;
# the cognition tick never runs git.
. "$(dirname "$0")/_launchd_env.sh"
exec "$PY" -m cagent.gitpush "$@"
