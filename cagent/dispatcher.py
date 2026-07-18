"""Round-robin persona dispatcher (Phase 3). One launchd timer runs this; each fire spawns ONE
enabled persona's tick as a subprocess with CAGENT_PERSONA set, advancing a persisted cursor so
the personas take turns. Quota-frugal by design: total `claude` usage stays near single-agent
levels because only one persona thinks per heartbeat.

The dispatcher's own git + mailbox I/O (control.pull + ingest) runs UNDER the single-flight lock so
it can't interleave with a concurrent daily push's `git add -A`/commit; the lock is released before
the spawned `cagent.tick` (a separate process that takes the lock itself) and before the auto-push
(which also takes the lock). So a manual `run-tick`, the standalone dailypush, and the dispatcher
all serialize correctly. Mode is per-persona (each persona's config.toml [agent].mode), so the
dispatcher does NOT set AGENT_MODE: a DRAFT persona stays DRY_RUN even while an enabled one runs LIVE.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

from cagent import config, control, gitpush, gmail, locking, logging_setup

CURSOR = config.REPO_ROOT / "var" / "dispatch_cursor.json"


def _tick_timeout_s() -> int:
    """Hard cap on a single persona's tick subprocess, DERIVED from the per-claude-call budget.

    One tick can chain several `claude` calls (cognition + gate-check + revise + research +
    reflection), each bounded by config `tick_timeout_s`. The subprocess cap must exceed their SUM:
    a fixed 900s (from when per-call was 300) is smaller than two 500s calls, so a legitimately slow
    tick would be SIGKILLed mid-flight -- and because invoke.py starts `claude` in its OWN session
    (so its per-call timeout can clean it up), killing the python tick from here does NOT reach that
    grandchild `claude`, orphaning it to burn subscription quota. Sizing the cap above the realistic
    worst case lets invoke.py's own per-call timeout be the thing that bounds a genuine hang."""
    try:
        per_call = int(config.load().tick_timeout_s)
    except Exception:
        per_call = 500
    return per_call * 6 + 120   # ~6 chained calls + overhead; invoke.py bounds each call itself


def _read_index() -> int:
    try:
        return int(json.loads(CURSOR.read_text()).get("i", 0))
    except Exception:
        return 0


def _write_index(i: int) -> None:
    CURSOR.parent.mkdir(parents=True, exist_ok=True)
    CURSOR.write_text(json.dumps({"i": i}) + "\n")


def select(enabled: list[str], idx: int) -> str:
    """The persona for this fire: round-robin over the enabled list."""
    return enabled[idx % len(enabled)]


def _pull_and_ingest(enabled: list[str], log) -> None:
    """One cycle's git control-plane pull + mailbox ingest. Called under the single-flight lock (see
    main). Each step is independently best-effort: a pull/parse/network failure never blocks the tick.
    Phase 6: pull the repo, then route control/inbox/ directives to their target persona (pause/resume
    act now; goal/instruction/note are queued for the persona's own tick). Phase 4: ingest the shared
    mailbox once, then each own-account persona's separate mailbox, routing mail into per-persona
    inboxes."""
    try:
        pulled = control.pull(log)   # False = off-branch/no-network (already logged); inbox still drains
        applied = control.process_inbox(enabled, config.default_persona(), log)
        if applied:
            note = "" if pulled else " (git pull was a no-op this cycle; these were already local)"
            log.info("dispatcher: applied %d control directive(s)%s: %s", len(applied), note, applied)
    except Exception as e:
        log.info("dispatcher: control cycle failed (continuing): %s", e)
    try:
        routed = gmail.ingest(commit=True)
        if routed:
            log.info("dispatcher: ingested %d mail -> %s", len(routed), [r["persona"] for r in routed])
    except Exception as e:
        log.info("dispatcher: ingest failed (continuing): %s", e)
    try:
        own = gmail.ingest_own_accounts(commit=True)
        if own:
            log.info("dispatcher: ingested %d mail from own-account personas -> %s",
                     len(own), [r["persona"] for r in own])
    except Exception as e:
        log.info("dispatcher: own-account ingest failed (continuing): %s", e)


def main() -> int:
    log = logging_setup.setup()
    enabled = config.enabled_personas()
    if not enabled:
        log.info("dispatcher: no enabled personas; nothing to do")
        return 0

    # Branch guard: this working tree is shared with any interactive `git checkout`. If HEAD is off
    # the agent's branch, every git mutation this cycle (control.pull + the auto-push below) is a
    # no-op by design, so the persona's tick still runs but its state stays LOCAL until the tree is
    # switched back. Warn loudly once here; the pull/push guards do the actual enforcement.
    on_branch, branch = gitpush.on_expected_branch()
    if not on_branch:
        log.warning("dispatcher: working tree on %r, not %r -- git pull/commit/push DISABLED this "
                    "cycle; the tick runs but its state stays LOCAL until you `git switch %s`.",
                    branch or "(detached)", gitpush.EXPECTED_BRANCH, gitpush.EXPECTED_BRANCH)

    # The git control-plane pull + mailbox ingest run UNDER the single-flight lock so a concurrent
    # daily push (the standalone dailypush.sh job, or a manual one) can't `git add -A`/commit while
    # our `git pull --rebase` is rewriting the index/working tree. The lock is released here, BEFORE
    # the tick subprocess (which takes the lock itself) -- holding it across the subprocess would
    # deadlock it. If the lock is busy, skip this phase (the tick still runs; next cycle pulls).
    try:
        with locking.single_flight():
            _pull_and_ingest(enabled, log)
    except locking.LockHeld:
        log.info("dispatcher: lock held (a push/tick is running); skipping pull+ingest this cycle")
    except Exception as e:
        log.info("dispatcher: pull/ingest cycle failed (continuing): %s", e)

    idx = _read_index()
    persona = select(enabled, idx)
    _write_index((idx + 1) % len(enabled))

    if control.is_paused(persona):
        # Skip only this persona's TICK -- NOT the auto-push below. _pull_and_ingest above may have
        # routed inbound owner mail into committed state this cycle; returning here would leave it
        # unpushed, so remote monitoring goes blind exactly while personas are paused.
        log.info("dispatcher: persona %s paused (var/persona/%s.STOP); skipping its tick", persona, persona)
    else:
        log.info("dispatcher: running persona %s (slot %d/%d)",
                 persona, (idx % len(enabled)) + 1, len(enabled))
        env = dict(os.environ, CAGENT_PERSONA=persona)
        env.pop("AGENT_MODE", None)   # mode is per-persona config, not a global override
        timeout_s = _tick_timeout_s()
        try:
            r = subprocess.run([sys.executable, "-m", "cagent.tick"], env=env,
                               cwd=str(config.REPO_ROOT), timeout=timeout_s)
            log.info("dispatcher: persona %s tick exit=%s", persona, r.returncode)
        except subprocess.TimeoutExpired:
            log.info("dispatcher: persona %s tick exceeded %ss; killed", persona, timeout_s)

    # Auto-push after each cycle (even a paused one) so repo state is monitorable from remote in near
    # real time.
    # This runs at the dispatcher (orchestrator) level, NOT inside the cognition tick (the
    # constitution forbids the tick from touching git); the tick subprocess has exited, so the
    # single-flight lock is free. force=True bypasses the once-a-day + DRY_RUN guards but KEEPS
    # gitpush's in-process secret scan. Best-effort: a push failure never fails the cycle.
    try:
        res = gitpush.daily_push(config.load(), log, force=True, message=f"cagent tick: {persona}")
        log.info("dispatcher: auto-push %s", res)
    except Exception as e:
        log.info("dispatcher: auto-push failed (continuing): %s", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
