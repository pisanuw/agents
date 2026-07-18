"""Heartbeat entrypoint: `python -m cagent.tick`. Order of guards matters:
persona validation -> kill switch -> backoff gate -> single-flight lock -> the tick pipeline.
The expected-skip guards (STOP, per-persona pause, backoff, lock) exit cleanly (0) so launchd
never sees a crash for a normal skip. Persona validation is different: a bogus CAGENT_PERSONA is
an operator error (a typo / stale env), not a skip, so it exits NON-zero to fail the run loudly.
"""
from __future__ import annotations

import os
import sys

from cagent import config, control, locking, logging_setup, tick_pipeline
from cagent.cognition import backoff

STOP = config.REPO_ROOT / "var" / "STOP"


def main() -> int:
    log = logging_setup.setup()
    cfg = config.load()

    # Stop bogus manual runs AT THE SOURCE. CAGENT_PERSONA is just an env var: `cagentctl run-tick
    # --persona X` validates it, but a directly-set value (`CAGENT_PERSONA=alpha python -m
    # cagent.tick`, or `CAGENT_PERSONA=alpha cagentctl run-tick` with no flag) bypasses that check.
    # If it names a persona with no personas/<name>/ directory, refuse before touching ANY state --
    # otherwise the tick namespaces itself into an empty state/personas/<name>/ and can strand orphan
    # flags there (the 2026-07-03 alpha.STOP incident). known_personas() (dirs, enabled OR draft),
    # not enabled_personas(), so testing a draft via run-tick still works; empty (legacy, no
    # personas/ dir) can't validate and does not block. Non-zero exit: an operator error, not a skip.
    persona = os.environ.get("CAGENT_PERSONA", "").strip()
    known = config.known_personas()
    if persona and known and persona not in known:
        log.error("REFUSING tick: CAGENT_PERSONA=%r has no personas/%s/ directory (known: %s); "
                  "misconfigured or manual run -- no cognition, no state touched", persona, persona,
                  ", ".join(known))
        return 2

    if STOP.exists():
        log.info("STOPPED (var/STOP present); no cognition this tick")
        return 0

    # Per-persona kill switch: the dispatcher already skips a paused persona before spawning, but
    # a forced `run-tick` bypasses it. Honor the per-persona flag here too so email !PAUSE and the
    # git-control 'pause' directive both halt cognition for THIS persona, not just scheduling.
    if persona and control.is_paused(persona):
        log.info("STOPPED (var/persona/%s.STOP present); no cognition this tick", persona)
        return 0

    open_, reason = backoff.gate_open()
    if not open_:
        log.info("DEFERRED %s", reason)
        return 0

    try:
        with locking.single_flight():
            return tick_pipeline.run(cfg, log)
    except locking.LockHeld:
        log.info("lock held by another run; skipping this tick")
        return 0


if __name__ == "__main__":
    sys.exit(main())
