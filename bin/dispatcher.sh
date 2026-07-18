#!/usr/bin/env bash
# launchd entrypoint for the round-robin persona dispatcher (Phase 3). In the multi-persona
# layout this REPLACES heartbeat.sh: instead of running one fixed tick, each fire spawns one
# enabled persona's tick (cycling through them). Mode is per-persona config, so this sets no
# AGENT_MODE. launchd gives a minimal environment, so set a full PATH (claude lives in
# ~/.local/bin) and run via the repo venv python.
. "$(dirname "$0")/_launchd_env.sh"
exec "$PY" -m cagent.dispatcher "$@"
