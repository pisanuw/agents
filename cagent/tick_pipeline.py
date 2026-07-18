"""One heartbeat tick (full research tick from Session 5 on):
poll inbound -> assemble context -> claude decision (tools off, schema) -> execute
bounded actions -> journal + per-tick audit dir. Continuity lives in files.
"""
from __future__ import annotations

import json
import os

from cagent import (
    clock, commands, config, control, daymarker, gmail, guardrails, persona, reflect, supervise,
)
from cagent.cognition import backoff, context, execute, invoke, meter, parse

LAST_TICK = config.REPO_ROOT / "var" / "last_tick.json"
# LAST_DIGEST, JOURNAL and the per-tick audit dir are per-persona and MUST be computed at call time
# (inside _maybe_digest()/_finish()/run()) to respect whichever CAGENT_PERSONA is active when the
# function runs. A module-level `TICKS = config.state_root()/"ticks"` froze to the flat legacy
# state/ticks/ because cli.cmd_run_tick imports this module before `_persona_flag` sets the env var,
# so `run-tick --persona X` wrote X's audit dir to the wrong namespace (and watchdog.prune_ticks,
# which only walks state/personas/*/ticks, then never pruned it). LAST_TICK lives in var/ by design
# (global, not per-persona).
def _ticks_dir():
    return config.state_root() / "ticks"


ACTION_SCHEMA = config.REPO_ROOT / "cagent" / "cognition" / "action_schema.json"


def _best_effort(label: str, log, fn, warn: bool = False):
    """Run one NON-CRITICAL tick stage, swallowing + logging any failure so a single stage can never
    abort the tick (continuity is per-tick; the next tick retries). Returns fn()'s value, or None if
    it raised. Replaces the ~10 copy-pasted `try: ... except Exception: log('... continuing')` blocks
    with one wrapper + one named step each -- which also makes each stage independently testable.
    warn=True logs the failure at WARNING (not INFO): use it for SECURITY-relevant stages (e.g. the
    token-exposure tripwire) so a silently broken guard is visible in the logs, not buried at INFO."""
    try:
        return fn()
    except Exception as e:
        (log.warning if warn else log.info)("%s failed (continuing): %s", label, e)
        return None


# --- named best-effort stages (each is one step of run(); see the numbered call order below) ------

def _poll_own_inbox(log) -> None:
    """Legacy single-persona path only: pull IMAP directly (multi-persona ticks consume the
    dispatcher's pre-routed inbox instead)."""
    new = gmail.poll_imap(commit=True)
    if new:
        log.info("pulled %d new inbound", len(new))


def _flag_token_burn(dropped, cfg, log) -> None:
    """If a non-owner message carried the COMMAND_TOKEN, burn it (refuse email commands until rotated)."""
    if commands.note_token_exposure(dropped, cfg, log):
        log.info("COMMAND_TOKEN burned (seen in non-owner mail); email commands now refused")


def _release_approved(cfg, log) -> None:
    """Release owner-APPROVED drafts whose send was deferred. Runs before the approval-request
    retries so already-blessed content gets first claim on the scarce send cap."""
    if cfg.MODE != "SUPERVISED":
        return
    released = supervise.retry_approved(cfg, log)
    if released:
        log.info("released %d deferred-approved draft(s): %s", len(released), [r["token"] for r in released])


def _retry_approval_requests(cfg, log) -> None:
    """Re-send approval requests that were never delivered, then remind (3/7/14d) and expire staged
    drafts the owner never acted on. SUPERVISED only (pending drafts exist only in that mode)."""
    if cfg.MODE != "SUPERVISED":
        return
    redel = supervise.retry_undelivered(cfg, log)
    if redel:
        log.info("re-sent %d undelivered approval request(s): %s", len(redel), [r["token"] for r in redel])
    rem = supervise.remind_and_expire_approvals(cfg, log)
    if rem["reminded"] or rem["expired"]:
        log.info("approval lifecycle: reminded=%s expired=%s", rem["reminded"], rem["expired"])


def _retry_command_acks(cfg, log) -> None:
    """Re-send command acks refused on an earlier tick (transient cap/QUIET/stop). Any mode."""
    acked = commands.retry_acks(cfg, log)
    if acked:
        log.info("re-sent %d queued command ack(s)", len(acked))


def _answer_status_request(cfg, log) -> None:
    """Answer a one-shot owner !STATUS with a live snapshot; clear the flag only once the reply
    was actually sent (M11), so a refused send retries next tick instead of dropping the request."""
    if not commands.status_requested():
        return
    res = supervise.send_status(cfg, log)
    if res.get("sent"):
        commands.clear_status_request()
    log.info("status request answered: %s", res)


def _apply_control_directives(cfg, log) -> None:
    """Apply git-control-plane directives the dispatcher queued for this persona (before context, so
    a fresh goal/instruction influences this very tick)."""
    drained = control.drain(cfg, log)
    if drained:
        log.info("control directives applied: %s", [d["type"] for d in drained])


def _maybe_reflect(cfg, log) -> None:
    """Reflection / goal-evolution on cadence (or on request)."""
    due, why = reflect.should_reflect(cfg)
    if due:
        log.info("reflection due (%s)", why)
        reflect.run(cfg, log)


def run(cfg, log) -> int:
    log.info("tick start persona=%s mode=%s", cfg.persona or "-", cfg.MODE)
    tick_id = clock.now().strftime("%Y%m%dT%H%M%S")
    tickdir = _ticks_dir() / tick_id
    tickdir.mkdir(parents=True, exist_ok=True)
    meter.reset()   # start this tick's token accounting; drained in _finish (any exit path)

    # 1. inbound. Multi-persona: the dispatcher ingests + routes (one reader) and this tick only
    # consumes its own pre-routed inbox. Legacy single-persona: the tick polls IMAP itself.
    if not os.environ.get("CAGENT_PERSONA"):
        _best_effort("imap poll", log, lambda: _poll_own_inbox(log))
    inbound_all = gmail.pending_inbound()

    # 1b. guardrails: owner-allowlist + RFC 3834 anti-loop (deterministic, before cognition)
    inbound, dropped = guardrails.filter_inbound(inbound_all, cfg)
    if dropped:
        log.info("dropped %d inbound: %s", len(dropped), [r for _, r in dropped])

    # 1b-bis. token-burn tripwire (non-owner mail carrying the COMMAND_TOKEN -> burn it). warn=True:
    # a silently broken security tripwire must surface at WARNING, not hide at INFO.
    _best_effort("token-exposure check", log, lambda: _flag_token_burn(dropped, cfg, log), warn=True)

    # 1c. email commands (exact-regex SUBJECT only, owner-only, token-authenticated, escalate-only)
    applied = commands.parse_and_apply(inbound, cfg, log)
    if applied:
        log.info("email commands applied: %s", applied)
        # optional "applied/refused" reply
        _best_effort("command ack", log, lambda: commands.acknowledge(applied, inbound, cfg, log))

    # 1d. SUPERVISED approvals: owner replies APPROVE/REJECT <token> release/discard drafts
    approvals = commands.handle_approvals(inbound, cfg, log)
    if approvals:
        log.info("approvals: %s", approvals)

    # 1d-bis. release owner-APPROVED drafts whose send was deferred (before the request retries so
    # already-blessed content gets first claim on the scarce send cap).
    _best_effort("approved-draft release", log, lambda: _release_approved(cfg, log))
    # 1d-ter. retry undelivered approval requests + remind/expire staged drafts.
    _best_effort("approval-request retry", log, lambda: _retry_approval_requests(cfg, log))
    # 1d-quater. retry command acks refused on an earlier tick (any mode).
    _best_effort("command-ack retry", log, lambda: _retry_command_acks(cfg, log))
    # 1d-quinquies. on-demand status: answer a one-shot owner !STATUS with a live snapshot.
    _best_effort("status reply", log, lambda: _answer_status_request(cfg, log))

    # 1d-sexies. Mark command/approval mail processed NOW, before cognition. Steps 1c/1d already
    # fired their deterministic side effects (goal/feedback creation, acks, draft release); if the
    # claude call below is rate-limited it returns WITHOUT marking inbound (so content mail is retried
    # after backoff), which would otherwise re-apply these next tick -- duplicate goals, duplicate
    # acks. Marking only the consumed subset keeps genuine content mail eligible for a cognition reply.
    consumed = commands.consumed_messages(inbound, cfg)
    _best_effort("mark command mail processed", log, lambda: gmail.mark_processed(consumed))
    # The consumed subset is ALSO withheld from the cognition context below: steps 1c/1d already
    # fully handled these deterministically (a REJECT deletes its draft, an APPROVE releases it), so
    # cognition has no reason to re-read them -- and doing so leaks the outcome. A reason-less
    # `REJECT <tok>` writes no memory note (delete-and-nothing-more, by design), yet the raw REJECT
    # email, left in `inbound`, would surface in the NEW MAIL section and tell the persona its draft
    # was rejected -- prompting a "what did I miss?" reply the silent reject was meant to avoid. A
    # *reasoned* reject still reaches the persona through its intended channel (the memory note).
    # Exclude by a STABLE key (uid + message-id), not object identity: a stage that copies/mutates a
    # message dict would otherwise defeat `m not in consumed` and re-surface a handled command (P2-2).
    _consumed_keys = {(m.get("uid"), m.get("message_id")) for m in consumed}
    content_inbound = [m for m in inbound if (m.get("uid"), m.get("message_id")) not in _consumed_keys]

    # 1e. git control plane (Phase 6): apply directives the dispatcher queued for this persona
    # BEFORE building context, so a fresh goal/instruction influences this very tick.
    _best_effort("control drain", log, lambda: _apply_control_directives(cfg, log))

    # 2. context (only owner CONTENT mail, wrapped as untrusted; command/approval mail excluded)
    ctx = context.build(cfg, content_inbound)
    (tickdir / "context.txt").write_text(ctx)

    # 3. cognition (tools off, structured decision)
    env = invoke.run_claude(ctx, append_system_prompt=persona.load_system_prompt(),
                            tools="", schema_path=str(ACTION_SCHEMA), label="tick")
    r = parse.parse(env)
    (tickdir / "status.txt").write_text(r.status)

    if r.rate_limited:
        backoff.record_failure(r.status, r.http)
        log.info("rate/auth limited (%s); backing off, no work this tick", r.status)
        _finish(cfg, ok=False, summary=r.status, tickdir=tickdir, log=log,
                journal={"kind": "tick", "ok": False, "status": r.status})
        return 0

    backoff.record_success()

    if r.status != "OK" or not isinstance(r.structured, dict):
        log.info("no structured decision (%s); tick is a no-op", r.status)
        gmail.mark_processed(inbound_all)  # don't loop on the same mail
        _finish(cfg, ok=False, summary=r.status, tickdir=tickdir, log=log,
                journal={"kind": "tick", "ok": False, "status": r.status})
        return 0

    decision = r.structured
    (tickdir / "decision.json").write_text(json.dumps(decision, indent=2))

    # Mark inbound processed BEFORE executing actions (M2). A crash mid-execute -- e.g. after a
    # send_email's SMTP send but before this mark -- would otherwise re-feed the same owner mail to
    # the next tick and reply again. Marking first makes the file-level dedup at-most-once, matching
    # the IMAP cursor's advance-before-cognition (a crash drops one reply rather than duplicating it).
    # The rate-limit (returns above, unmarked -> retried) and no-decision (marks above) paths are unchanged.
    gmail.mark_processed(inbound_all)

    # 4. execute bounded actions
    results = execute.apply(decision, cfg, log)
    (tickdir / "actions.json").write_text(json.dumps(results, indent=2))

    # reflection / goal-evolution on cadence (or on request)
    _best_effort("reflection", log, lambda: _maybe_reflect(cfg, log))

    # daily digest (once/day)
    _best_effort("digest", log, lambda: _maybe_digest(cfg, log))

    summary = (decision.get("summary") or "").strip()[:200]
    action_types = [a.get("type") for a in decision.get("actions", [])]
    # check_tripwire runs inside _finish (log= passed) so it fires on every exit path (M6).
    _finish(cfg, ok=True, summary=summary, tickdir=tickdir, log=log, journal={
        "kind": "tick", "mode": cfg.MODE, "ok": True, "summary": summary,
        "mood": decision.get("mood", ""), "actions": action_types,
        "results": results, "tick_id": tick_id})
    # After the journal is written (so it includes THIS tick), alarm if drafts keep failing the gate
    # -- a silent send-stall that is otherwise invisible outside the journal.
    _best_effort("gate-stall check", log, lambda: supervise.check_gate_stall(cfg, log))
    log.info("tick ok: %s | actions=%s", summary[:80], action_types)
    return 0


def _maybe_digest(cfg, log) -> None:
    """Send one digest per day (SUPERVISED/LIVE only; DRY_RUN stages it).
    Marker is per-persona (M10) and written only when the digest was actually sent (M9),
    so a refused send retries on the next tick rather than silently skipping the day."""
    last_digest = config.state_root() / "last_digest"
    if daymarker.done_today(last_digest):
        return
    res = supervise.send_digest(cfg, log)
    if res.get("sent"):
        daymarker.mark(last_digest)
    log.info("daily digest: %s", res)


def _finish(cfg, ok: bool, summary: str, journal: dict, tickdir=None, log=None) -> None:
    # Drain the per-tick token meter (every claude sub-call: cognition + gate-check + research +
    # reflection). Fold a compact summary into the journal line so it reaches the mirror on push,
    # and dump the full per-call breakdown into the tick's audit dir. cost_notional now equals the
    # SUMMED cost across all sub-calls (was previously only the main cognition call, undercounting).
    usage_summary, usage_rows = meter.drain()
    journal["usage"] = usage_summary
    journal.setdefault("cost_notional", usage_summary["cost_usd"])
    if tickdir is not None:
        (tickdir / "usage.json").write_text(
            json.dumps({"summary": usage_summary, "calls": usage_rows}, indent=2))
    # Compute JOURNAL at call time so multi-persona ticks each write to their own journal (M10).
    journal_path = config.state_root() / "journal.jsonl"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with open(journal_path, "a") as f:
        f.write(json.dumps({"ts": clock.iso(), **journal}) + "\n")
    LAST_TICK.parent.mkdir(parents=True, exist_ok=True)
    LAST_TICK.write_text(json.dumps({"ts": clock.iso(), "mode": cfg.MODE, "ok": ok, "summary": summary}, indent=2))
    # LIVE auto-downgrade tripwire runs on EVERY tick exit (not just success) so repeated failures
    # on the rate-limited and no-decision paths also trigger the downgrade (M6).
    if log is not None:
        try:
            supervise.check_tripwire(cfg, log)
        except Exception as e:
            log.info("tripwire check failed (continuing): %s", e)   # log is non-None inside this guard
