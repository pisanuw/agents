#!/usr/bin/env bash
# launchd entrypoint. launchd gives a minimal environment, so set a full PATH
# (claude lives in ~/.local/bin) and run the tick via the repo venv python.
. "$(dirname "$0")/_launchd_env.sh"
exec "$PY" -m cagent.tick "$@"
