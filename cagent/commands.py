"""Email-command channel. Commands are parsed by EXACT regex from the SUBJECT line of
owner mail ONLY, never inferred by the LLM from a body. They are ESCALATE-ONLY: every command
either tightens restriction (pause / stop-sending / throttle / quiet / no-research) or is
read-only (help / ping / status / goals). Anything that would RELAX restriction or raise
autonomy (resume, un-throttle, raise caps, mode promotion) is intentionally NOT an email
command -- it requires local cagentctl or the git control plane -- so a spoofed or injected
message can never un-pause or promote autonomy. Each command must carry the shared COMMAND_TOKEN.

Per-persona targeting: an optional `[name]` after the verb retargets a command at another enabled
persona (e.g. `!PAUSE [scout] <token>`) regardless of how the message threaded. Immediate flags
write that persona's own state directly; steering (GOAL/FOCUS) is queued to that persona's tick.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import timedelta

from cagent import (
    atomicio, clock, config, control, deferred, gmail, goals as goals_mod, guardrails, memory,
)

# Per-(running-)persona flags resolved at call time so the CLI and tests can change CAGENT_PERSONA
# (or REPO_ROOT) after import and see the correct path. Cross-persona targeting recomputes an
# explicit path instead (see _crosspath); tests monkeypatch these functions for the common
# current-persona path. var/STOP is the legacy single-persona global kill switch.
STOP = config.REPO_ROOT / "var" / "STOP"


def _status_request():        # noqa: F821  (Path imported by callers)
    return config.state_root() / "status_request.flag"


def _quiet_until():
    return config.state_root() / "quiet_until.json"


def _throttle():
    return config.state_root() / "throttle.json"


def _no_research():
    return config.state_root() / "no_research.flag"


def _token_burned():
    return config.state_root() / "token_burned.flag"


def _burn_notice_pending():
    # Armed when a token-burn owner alert is owed; cleared once the alert is actually delivered, so a
    # send refused by cap/quiet/stop is retried on a later tick instead of being lost forever (the burn
    # flag itself is durable and independent -- commands stay refused regardless of notice delivery).
    return config.state_root() / "token_burn_notice_pending"


def _send_burn_notice(cfg, log, sender="unknown", reason="unknown") -> bool:
    """Send the 'command token burned' owner alert (cap-exempt kind='alert'), clearing the pending
    marker on success. Returns True iff delivered. Never logs the token value."""
    try:
        gmail.send(subject="command token burned",
                   body_md=(f"A non-owner (from {sender}) message [dropped as {reason}] was seen "
                            "carrying the COMMAND_TOKEN, so it is now treated as compromised. All "
                            "email commands are refused until you rotate COMMAND_TOKEN in "
                            "~/.config/cagent/.env and remove token_burned.flag locally."),
                   kind="alert")
        _burn_notice_pending().unlink(missing_ok=True)
        return True
    except gmail.SendRefused as e:
        log.info("token-burn notice refused (will retry next tick): %s", e)
        return False


# Command acks whose send was refused (global cap / QUIET / stop) are staged here and retried each
# tick, exactly like undelivered approval requests, so a confirmation is never silently lost.
def _pending_acks():
    return config.state_root() / "pending_acks"


# The command table is the SINGLE SOURCE for email commands: each row carries its menu line, the
# verb(s) it answers to, and its handler. VERBS (the parser's allow-set), HELP_LINES (the !HELP menu
# + the send footer), and the dispatch map are all DERIVED from it below, so a verb can never be
# handled-but-undocumented or documented-but-unhandled -- the drift that let a verb in VERBS silently
# hit the "unhandled command" branch. Handlers have heterogeneous signatures, so each row adapts its
# handler to one uniform callable over a _Ctx bundle (lazy: the referenced functions are defined
# further down and resolved at call time).

@dataclass(frozen=True)
class _Ctx:
    """Everything a command handler might need, so the table can call every handler uniformly."""
    arg: str
    target: str
    cross: bool
    m: dict
    cfg: object
    log: object


@dataclass(frozen=True)
class _Cmd:
    label: str            # menu line WITH arg hint, e.g. "!GOAL <text>"
    desc: str             # one-line description shown in !HELP and the send footer
    verbs: tuple          # verb token(s) this row answers to (HELP/COMMANDS share one row)
    run: object           # (_Ctx) -> dict


_COMMAND_TABLE = [
    _Cmd("!HELP / !COMMANDS", "reply with this command list and the enabled personas",
         ("HELP", "COMMANDS"), lambda c: _reply(_help_text(c.cfg), "email commands", c.m, c.cfg, kind="help")),
    _Cmd("!PING", "reply 'alive @ <time>' so you can confirm the heartbeat runs", ("PING",),
         lambda c: _reply(f"alive @ {clock.iso()}\npersona: {_current_persona() or '(single-persona)'}\n"
                          f"mode: {c.cfg.MODE}", "ping", c.m, c.cfg, kind="ping")),
    _Cmd("!STATUS", "reply with a live operational snapshot", ("STATUS",),
         lambda c: _write_flag(_status_request_path(c.target))),
    _Cmd("!GOALS", "reply with the current goals and their ids", ("GOALS",),
         lambda c: _reply(_goals_text(c.target), "goals", c.m, c.cfg, kind="goals")),
    _Cmd("!GOAL <text>", "add or extend a goal", ("GOAL",),
         lambda c: _cmd_goal(c.arg, c.target, c.cross)),
    _Cmd("!DROP-GOAL <id>", "archive a goal (e.g. !DROP-GOAL G3)", ("DROP-GOAL",),
         lambda c: _cmd_drop_goal(c.arg, c.cross)),
    _Cmd("!FOCUS <topic>", "bias the next ticks toward a topic", ("FOCUS",),
         lambda c: _cmd_focus(c.arg, c.target)),
    _Cmd("!FEEDBACK <text>", "record owner feedback into memory for later ticks", ("FEEDBACK",),
         lambda c: _cmd_feedback(c.arg, c.cross)),
    _Cmd("!PAUSE", "pause this persona (resume is local/git only)", ("PAUSE",),
         lambda c: _cmd_pause(c.target)),
    _Cmd("!PAUSE-ALL", "pause every enabled persona at once", ("PAUSE-ALL",),
         lambda c: _cmd_pause_all()),
    _Cmd("!STOP-SENDING", "halt all outbound mail (clear locally)", ("STOP-SENDING",),
         lambda c: _write_flag(_stop_sending_path(c.target))),
    _Cmd("!QUIET <hours>", "mute outbound for N hours, then auto-clears", ("QUIET",),
         lambda c: _cmd_quiet(c.arg, c.target)),
    _Cmd("!THROTTLE <n>", "lower today's send cap to n (raising stays local)", ("THROTTLE",),
         lambda c: _cmd_throttle(c.arg, c.target)),
    _Cmd("!NO-RESEARCH", "disable web research until cleared locally", ("NO-RESEARCH",),
         lambda c: _write_flag(_no_research_path(c.target))),
]

HELP_LINES = [(c.label, c.desc) for c in _COMMAND_TABLE]
VERBS = frozenset(v for c in _COMMAND_TABLE for v in c.verbs)
_HANDLERS = {v: c.run for c in _COMMAND_TABLE for v in c.verbs}

# Verb (letters + hyphen), optional [persona], optional ':' and the rest as the argument.
CMD_RE = re.compile(r"^\s*!([A-Za-z-]+)\s*(?:\[([a-z0-9_-]+)\])?\s*:?\s*(.*)$", re.IGNORECASE)
# Per-draft approval verbs (token is the authenticator). EDIT/REJECT may carry ': <body|reason>'.
APPROVE_RE = re.compile(r"^\s*(APPROVE|REJECT|EDIT|HOLD)\s+([0-9a-fA-F]{6,12})\b\s*(?::\s*(.*))?\s*$",
                        re.IGNORECASE)


def status_requested() -> bool:
    return _status_request().exists()


def clear_status_request() -> None:
    _status_request().unlink(missing_ok=True)


# --------------------------- targeting + path helpers --------------------------- #

def _current_persona() -> str:
    return os.environ.get("CAGENT_PERSONA", "").strip()


def _persona_root(persona: str):
    """Explicit per-persona state root. Delegates to the single validated Facade so a persona name
    is regex-checked and the state/personas/<name>/ layout has exactly one owner (config.state_root).
    Only ever called with a named (non-empty, cross-persona) target."""
    return config.state_root(persona)


def _crosspath(target: str, fn, filename: str):
    """The TARGET persona's flag path. For the running persona call fn() (so tests can monkeypatch
    it); for a different persona recompute an explicit path so one persona can tighten another
    without leaving this tick."""
    if target and target != _current_persona():
        return _persona_root(target) / filename
    return fn()


def _stop_sending_path(target):
    if target and target != _current_persona():
        return _persona_root(target) / "stop_sending.flag"
    return config.state_root() / "stop_sending.flag"


def _status_request_path(target): return _crosspath(target, _status_request, "status_request.flag")
def _quiet_path(target): return _crosspath(target, _quiet_until, "quiet_until.json")
def _throttle_path(target): return _crosspath(target, _throttle, "throttle.json")
def _no_research_path(target): return _crosspath(target, _no_research, "no_research.flag")


def _first_int(arg: str):
    m = re.search(r"-?\d+", arg or "")
    return int(m.group()) if m else None


def _strip_token(arg: str, token: str) -> str:
    """Remove the COMMAND_TOKEN (it rides in the subject) from the free-text argument so it never
    leaks into a stored goal/focus/feedback, then collapse whitespace. Sealed records carry the
    redaction placeholder instead of the token; strip that too."""
    if token and token in arg:
        arg = arg.replace(token, " ")
    arg = (arg or "").replace(gmail.TOKEN_REDACTED, " ")
    return re.sub(r"\s+", " ", arg).strip()


# --------------------------- SUPERVISED approval replies --------------------------- #

def handle_approvals(messages: list[dict], cfg, log) -> list[dict]:
    """Owner replies APPROVE/REJECT/EDIT/HOLD <token> to release, discard, replace-and-send, or
    defer a SUPERVISED draft. The per-draft token is the authenticator (plus owner sender)."""
    from cagent import supervise
    out = []
    for m in messages:
        mo = APPROVE_RE.match(m.get("subject", "") or "")
        if not mo:
            continue
        verb = mo.group(1).upper()
        token = mo.group(2).lower()
        rest = (mo.group(3) or "").strip()
        if verb == "APPROVE":
            out.append({"approve": token, **supervise.approve(token, cfg, log)})
        elif verb == "EDIT":
            if not rest:
                out.append({"edit": token, "approved": False, "reason": "EDIT needs a body after ':'"})
            else:
                out.append({"edit": token, **supervise.approve(token, cfg, log, override_body=rest)})
        elif verb == "HOLD":
            out.append({"hold": token, **supervise.hold(token)})
        else:  # REJECT, optionally with a reason fed back into memory
            out.append({"reject": token, **supervise.reject(token, reason=rest)})
        log.info("approval action: %s %s", verb, token)
    return out


# --------------------------- command parsing + dispatch --------------------------- #

def consumed_messages(messages: list[dict], cfg=None) -> list[dict]:
    """The inbound messages that parse_and_apply()/handle_approvals() FULLY handle this tick: those
    carrying a recognized command verb, or an APPROVE/REJECT/EDIT/HOLD <token> subject. Their side
    effects (goal/feedback creation, command acks, draft release/discard) fire deterministically
    BEFORE cognition, so the tick marks them processed immediately. Otherwise a rate-limited cognition
    exit -- which deliberately leaves inbound unmarked so genuine content mail is retried after backoff
    -- would re-feed these next tick and re-apply them: duplicate goals, duplicate acks. Classification
    is by subject only and has no side effects (cfg is accepted for signature symmetry, unused)."""
    out = []
    for m in messages:
        subj = m.get("subject", "") or ""
        cmo = CMD_RE.match(subj)
        is_cmd = bool(cmo) and cmo.group(1).upper() in VERBS
        if is_cmd or APPROVE_RE.match(subj) is not None:
            out.append(m)
    return out


def parse_and_apply(messages: list[dict], cfg, log) -> list[dict]:
    """messages must already be owner-filtered. Returns one result dict per recognized command.
    Does NOT send acknowledgements (the tick calls acknowledge() so unit tests stay send-free)."""
    applied = []
    for m in messages:
        subj = m.get("subject", "") or ""
        mo = CMD_RE.match(subj)
        if not mo:
            continue
        verb = mo.group(1).upper()
        if verb not in VERBS:
            continue                                   # unknown/escalate-only verb -> silently ignored
        tag = (mo.group(2) or "").strip().lower()
        arg = _strip_token((mo.group(3) or "").strip(), cfg.command_token)

        # Authenticate: the shared token must appear in the subject. No token configured -> refuse
        # ALL email commands (owner uses cagentctl locally instead). Sealed records (ingest verified
        # then redacted the token before first write) carry cmd_token_ok as the auth witness.
        token_in_subj = bool(cfg.command_token) and (cfg.command_token in subj
                                                     or m.get("cmd_token_ok") is True)
        if not token_in_subj:
            applied.append({"cmd": verb, "refused": "missing/invalid COMMAND_TOKEN"})
            log.info("email command %s refused: token check failed", verb)
            continue
        # Burned-token tripwire: once the token was seen in non-owner mail, refuse everything until
        # the owner rotates it locally (escalate-only: a burn can only tighten).
        if _token_burned().exists():
            applied.append({"cmd": verb, "refused": "COMMAND_TOKEN burned; rotate it and clear "
                                                    "token_burned.flag locally"})
            continue
        # Resolve an optional [persona] target.
        if tag and tag not in config.enabled_personas():
            applied.append({"cmd": verb, "refused": f"unknown persona [{tag}]"})
            continue
        target = tag or _current_persona()
        cross = bool(tag) and target != _current_persona()
        # Defense in depth: the [persona] bracket is validated just above, but the fallback target is
        # _current_persona() -- the raw CAGENT_PERSONA env var, which is validated NOWHERE. A tick run
        # with a bogus persona (a manual run-tick typo, a stale env) must never let a flag-writing
        # command (!PAUSE, !STOP-SENDING, !QUIET, ...) materialize an orphan state/.STOP flag for a
        # persona that does not exist -- it would silently pause nothing (the 2026-07-03 alpha.STOP
        # incident). Empty target (legacy single-persona) still maps to the global var/STOP below.
        if target and target not in config.enabled_personas():
            applied.append({"cmd": verb, "refused": f"unknown persona [{target}]"})
            log.info("email command %s refused: current persona %r is not enabled", verb, target)
            continue

        res = _dispatch(verb, arg, target, cross, m, cfg, log)
        res["cmd"] = verb
        if "scope" not in res and target:
            res["scope"] = target
        applied.append(res)
        if res.get("ok"):
            log.info("applied email command: %s%s", verb, f" [{target}]" if cross else "")
    return applied


def _dispatch(verb, arg, target, cross, m, cfg, log) -> dict:
    run = _HANDLERS.get(verb)
    if run is None:                                        # unreachable: VERBS is derived from the table
        return {"refused": f"unhandled command: {verb}"}
    return run(_Ctx(arg=arg, target=target, cross=cross, m=m, cfg=cfg, log=log))


def _write_flag(path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(clock.iso() + "\n")
    return {"ok": True}


def _cmd_pause(target) -> dict:
    # Chokepoint guard: this is the one function that writes a per-persona var/persona/<name>.STOP.
    # `target` arrives pre-validated from parse_and_apply, but re-check here so the writer stays honest
    # for any future caller -- an unknown persona name must never leave an orphan .STOP that pauses
    # nothing (the 2026-07-03 alpha.STOP incident). Empty target -> the global var/STOP kill switch.
    if target and target not in config.enabled_personas():
        return {"refused": f"unknown persona [{target}]", "scope": target}
    path = control.stop_path(target) if target else STOP
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("paused by email command at " + clock.iso() + "\n")
    return {"ok": True, "scope": target or "global"}


def _cmd_pause_all() -> dict:
    paused = []
    for name in config.enabled_personas():
        sp = control.stop_path(name)
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text("paused by email !PAUSE-ALL at " + clock.iso() + "\n")
        paused.append(name)
    if not paused:                                     # legacy single-persona -> global kill switch
        STOP.parent.mkdir(parents=True, exist_ok=True)
        STOP.write_text("paused by email !PAUSE-ALL at " + clock.iso() + "\n")
        paused = ["global"]
    return {"ok": True, "scope": "all", "paused": paused}


def _cmd_quiet(arg, target) -> dict:
    h = _first_int(arg)
    if h is None or h <= 0:
        return {"refused": "!QUIET needs a positive number of hours"}
    h = min(h, 168)                                    # clamp to a week
    until = (clock.now() + timedelta(hours=h)).isoformat()
    p = _quiet_path(target)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"until": until, "set": clock.iso()}) + "\n")
    return {"ok": True, "hours": h, "until": until}


def _cmd_throttle(arg, target) -> dict:
    n = _first_int(arg)
    if n is None or n < 0:
        return {"refused": "!THROTTLE needs a non-negative integer cap"}
    p = _throttle_path(target)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"date": clock.today(), "cap": n}) + "\n")
    return {"ok": True, "cap": n}


def _cmd_goal(arg, target, cross) -> dict:
    if not arg:
        return {"refused": "empty goal text"}
    if cross:                                          # apply in the target persona's own tick
        control.enqueue(target, {"type": "goal", "text": arg})
        return {"ok": True, "queued": True, "title": arg[:60]}
    goals_mod.upsert({"title": arg[:120], "description": arg},
                     rationale="owner steering via email command")
    return {"ok": True, "title": arg[:60]}


def _cmd_focus(arg, target) -> dict:
    if not arg:
        return {"refused": "empty focus topic"}
    # Queue as a steering instruction. For the current persona, control.drain (later this tick)
    # surfaces it in context immediately; for another persona it applies on that persona's tick.
    control.enqueue(target, {"type": "instruction", "text": f"Focus bias: {arg}"})
    return {"ok": True, "queued": True, "topic": arg[:60]}


def _cmd_drop_goal(arg, cross) -> dict:
    gid = (arg.split() or [""])[0].upper()
    if not gid:
        return {"refused": "!DROP-GOAL needs a goal id, e.g. !DROP-GOAL G3"}
    if cross:
        return {"refused": "cross-persona !DROP-GOAL not supported; send it within that persona's thread"}
    if gid not in {g.get("id") for g in goals_mod.load()}:
        return {"refused": f"no active goal {gid}"}
    goals_mod.retire(gid, rationale="owner dropped via email command")
    return {"ok": True, "dropped": gid}


def _cmd_feedback(arg, cross) -> dict:
    if not arg:
        return {"refused": "empty feedback"}
    if cross:
        return {"refused": "cross-persona !FEEDBACK not supported; send it within that persona's thread"}
    path = memory.write_note(f"Owner feedback: {arg[:50]}", arg, tags=["feedback", "owner"], kind="feedback")
    return {"ok": True, "note": path}


# --------------------------- read-only replies + acks --------------------------- #

def _reply(body: str, label: str, m: dict, cfg, kind: str) -> dict:
    """Send a read-only reply through the normal send gate (staged in DRY_RUN, counts against the
    cap, refused while STOP-SENDING/QUIET/over-cap), threaded onto the triggering message."""
    try:
        r = gmail.send(subject=label, body_md=body, kind=kind, in_reply_to=m.get("message_id") or None)
        return {"ok": True, "replied": kind, "dry_run": getattr(r, "dry_run", None), "_self_replied": True}
    except gmail.SendRefused as e:
        return {"refused": f"reply not sent: {e}", "_self_replied": True}


def _help_text(cfg) -> str:
    cur = _current_persona() or "(single-persona)"
    enabled = ", ".join(config.enabled_personas()) or "(none)"
    out = [f"cagent email commands (persona: {cur}).", "",
           "Every command needs the COMMAND_TOKEN somewhere in the subject, e.g. '!PING <token>'.",
           "Add [name] after the verb to target another persona where supported "
           "(e.g. '!PAUSE [scout] <token>').", ""]
    out += [f"  {c:<20} {d}" for c, d in HELP_LINES]
    out += ["",
            "Escalate-only: email may tighten but never relax. Resume, un-throttle, raising caps,",
            "and mode changes are local cagentctl / git-control only.",
            "", f"Enabled personas: {enabled}.",
            "SUPERVISED approvals: subject 'APPROVE|REJECT|EDIT|HOLD <token>' (REJECT/EDIT take ': text')."]
    return "\n".join(out)


def _goals_text(target: str) -> str:
    glist = goals_mod.load() if (not target or target == _current_persona()) else _read_goals(target)
    actives = [g for g in glist if g.get("status") == "active"]
    lines = [f"Goals for {target or '(single-persona)'} ({clock.today()}):", ""]
    lines += [f"  [{g.get('id')}] {g.get('title', '')}" for g in actives] or ["  (no active goals)"]
    other = [g for g in glist if g.get("status") != "active"]
    if other:
        lines += ["", "Inactive:"] + [f"  [{g.get('id')}] {g.get('title', '')} -- {g.get('status')}"
                                       for g in other]
    lines += ["", "Reply '!DROP-GOAL <id> <token>' to archive one."]
    return "\n".join(lines)


def _read_goals(persona: str) -> list[dict]:
    p = _persona_root(persona) / "goals.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return []
    return []


def acknowledge(applied: list[dict], messages: list[dict], cfg, log) -> dict | None:
    """Cross-cutting nicety: one consolidated 'applied: !X / refused: !Y' confirmation per tick,
    for the action commands (read-only commands already replied themselves). Best-effort and
    config-gated ([commands].acknowledge); never forces a send (respects cap/stop/quiet)."""
    if not getattr(cfg, "ack_commands", True):
        return None
    notes = [a for a in applied if not a.get("_self_replied")]
    if not notes:
        return None
    lines = []
    for a in notes:
        if a.get("ok"):
            extra = next((str(a[k]) for k in ("scope", "title", "topic", "dropped", "cap", "hours")
                          if a.get(k) is not None), "")
            lines.append(f"applied: !{a.get('cmd')}" + (f" ({extra})" if extra else ""))
        else:
            lines.append(f"refused: !{a.get('cmd')} -- {a.get('refused', '')}")
    irt = next((m.get("message_id") for m in messages if m.get("message_id")), None)
    try:
        r = _send_ack(lines, irt)
        return {"acked": len(lines), "dry_run": getattr(r, "dry_run", None)}
    except gmail.SendRefused as e:
        # The refusal reason (global cap / QUIET / STOP-SENDING) is transient, so stage the ack and
        # retry it on a later tick rather than dropping the confirmation. Mirrors the approval-request
        # retry (supervise.retry_undelivered); the send gate keeps the retry cap-bounded, so no spam.
        _persist_ack(lines, irt)
        log.info("command ack not sent (queued for retry): %s", e)
        return {"acked": 0, "refused": str(e), "queued": True}


def _send_ack(lines: list[str], irt: str | None):
    """Single chokepoint for the ack send, shared by acknowledge() and retry_acks()."""
    return gmail.send(subject="command ack", body_md="\n".join(lines), kind="ack", in_reply_to=irt)


def _persist_ack(lines: list[str], irt: str | None) -> None:
    """Stage a refused ack for retry. Keyed by content so re-staging the identical ack is idempotent
    (one file per distinct confirmation), never the COMMAND_TOKEN (lines carry no secret)."""
    pa = _pending_acks()
    pa.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(((irt or "") + "\n" + "\n".join(lines)).encode()).hexdigest()[:12]
    (pa / f"{key}.json").write_text(
        json.dumps({"lines": lines, "in_reply_to": irt, "created": clock.iso()}, indent=2))


def retry_acks(cfg, log) -> list[dict]:
    """Re-send command acks whose send was refused on an earlier tick (global cap exhausted, QUIET,
    stop). Cap-bounded by the send gate itself: a still-refused retry leaves the ack queued for a
    later tick, so this never spams. Parallels supervise.retry_undelivered for approval requests.
    Returns the acks delivered this call."""
    if not getattr(cfg, "ack_commands", True):
        return []

    def attempt(d):
        _send_ack(d.get("lines", []), d.get("in_reply_to"))
        return {"acked": len(d.get("lines", [])), "in_reply_to": d.get("in_reply_to")}
    return deferred.drain(_pending_acks(), attempt, log)


# --------------------------- token-exposure tripwire --------------------------- #

def note_token_exposure(dropped: list, cfg, log) -> bool:
    """If a genuinely NON-owner message carried the COMMAND_TOKEN, the token is compromised: burn it
    (refuse all email commands until rotated locally) and alert the owner once. `dropped` is
    guardrails' reject list [(msg, reason), ...]. Returns True on a new burn. Never logs the token value.

    Ownership is RE-CHECKED per message here, not inferred from `dropped` membership. `dropped` mixes
    not-owner rejects with anti-loop rejects (own-footer-echo, bulk, auto-submitted), and crucially an
    OWNER's *reply* to one of our emails is dropped as own-footer-echo -- it quotes our disclosure
    footer -- while still carrying the COMMAND_TOKEN in its subject. Treating that as a leak would burn
    a persona's token from the owner's own commands. So only a message whose From is NOT an
    owner counts as exposure; owner mail dropped for any reason is skipped.

    Detection uses the FULL set of persona tokens (not just this persona's), scanned across every
    field, so a non-owner leaking ANOTHER persona's token -- or a token in a header -- is caught too
    (P1-7). For a sealed record the raw token is already redacted, so token_seen (set at ingest for any
    known token in any field) is the witness."""
    tokens = set(gmail._all_command_tokens())
    if cfg.command_token:
        tokens.add(cfg.command_token)          # always include the running persona's own token
    if not tokens:
        return False
    if _token_burned().exists():
        # Already burned. Retry the owner alert if a prior tick could not deliver it, so a token
        # compromise is never silently unreported. (The burn flag keeps commands refused meanwhile.)
        if _burn_notice_pending().exists():
            _send_burn_notice(cfg, log)
        return False
    for item in dropped:
        msg, reason = (item if isinstance(item, (tuple, list)) else (item, ""))
        if guardrails.is_owner(msg.get("from", ""), cfg):
            continue                                       # owner mail (e.g. a footer-echoed reply) is not a leak
        blob = " ".join(v for v in msg.values() if isinstance(v, str))   # subject, body AND headers
        if msg.get("token_seen") is True or any(t in blob for t in tokens):
            tb = _token_burned()
            tb.parent.mkdir(parents=True, exist_ok=True)
            tb.write_text(clock.iso() + "\n")
            atomicio.write_text(_burn_notice_pending(), clock.iso())   # arm; cleared once delivered
            # Name the sender (and why it was dropped) in the alert: the From address tells the owner a
            # real leak from a stranger apart from "that was me from an unregistered address" (fix via
            # OWNER_EMAIL), and the drop reason shows which guard caught it. addr_of matches the exact
            # comparison is_owner() used, so it shows the value that actually failed the allowlist.
            sender = guardrails.addr_of(msg.get("from", "")) or "unknown"
            log.info("COMMAND_TOKEN exposure detected in non-owner mail; token burned")
            _send_burn_notice(cfg, log, sender=sender, reason=reason or "unknown")
            return True
    return False
