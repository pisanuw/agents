#!/usr/bin/env bash
# launchd entrypoint for the watchdog (health check + maintenance). Independent of the tick.
. "$(dirname "$0")/_launchd_env.sh"
exec "$PY" -m cagent.watchdog "$@"
