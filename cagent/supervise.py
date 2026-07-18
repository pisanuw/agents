"""SUPERVISED mode + autonomy rails.

SUPERVISED: the agent never auto-sends a finding to the owner. Instead it stages the draft
and emails an APPROVAL REQUEST (to the +staging tag, which lands in the owner's inbox). The
owner replies `APPROVE <token>` or `REJECT <token>`; the next tick releases or discards it.

Also here: the daily digest, the graduation scorecard, and the LIVE auto-downgrade tripwire
(repeated failures/guardrail trips -> drop to SUPERVISED and tell the owner why).
"""
from __future__ import annotations

import html
import json
import secrets
from datetime import datetime
from urllib.parse import quote

from cagent import atomicio, clock, config, daymarker, deferred, gmail

# Per-persona state paths resolved at CALL time, not frozen at import. The tick subprocess has
# CAGENT_PERSONA set before import either way, but a CLI command that selects a persona at RUNTIME
# (`--persona`, via _persona_flag) must read THAT persona's namespace, not whatever was current when
# this module first imported (the flat legacy state/). Tests override by monkeypatching the resolver,
# e.g. monkeypatch.setattr(supervise, "_pending", lambda: tmp).
def _pending():
    return config.state_root() / "emails" / "pending"


def _journal():
    return config.state_root() / "journal.jsonl"


def _scorecard_path():
    return config.state_root() / "soft_launch_report.md"


def _mode_override():
    return config.state_root() / "mode_override"


def _goals():
    return config.state_root() / "goals.json"


# --------------------------- approval flow --------------------------- #

def _token(subject: str, body: str) -> str:
    # This token is the SOLE authenticator that releases a SUPERVISED draft (APPROVE/EDIT by mail),
    # so it must be unguessable. A sha256 of clock.iso()+subject+body was derivable from public
    # inputs (send time + the draft the owner just received). Use a CSPRNG instead. 8 hex chars = 32
    # bits, matching the old length and staying inside APPROVE_RE's [0-9a-fA-F]{6,12} bound; subject
    # and body are no longer inputs (kept as params for the stable call signature).
    return secrets.token_hex(4)


def stage_draft(subject: str, body: str, kind: str) -> str:
    pend = _pending()
    pend.mkdir(parents=True, exist_ok=True)
    tok = _token(subject, body)
    atomicio.write_text(pend / f"{tok}.json", json.dumps(
        {"token": tok, "subject": subject, "body": body, "kind": kind,
         "created": clock.iso(), "approved": False, "request_sent": False}, indent=2))
    return tok


def list_pending() -> list[dict]:
    pend = _pending()
    if not pend.exists():
        return []
    out = []
    for p in sorted(pend.glob("*.json")):
        try:
            out.append(json.loads(p.read_text()))
        except json.JSONDecodeError:
            continue
    return out


def backlog_depth() -> int:
    """Count of LIVE (non-held) drafts queued in this persona's pending/ — both AWAITING the owner's
    decision and APPROVED_UNSENT (approved, waiting on a send slot). This is the outbound backlog the
    tick applies backpressure against: when it is deep, the persona has already proposed more than it
    can deliver under the send caps, so drafting/researching still more only grows an unsendable queue.
    Held (!HOLD) drafts are the owner's deliberate parking and do not count toward the pressure."""
    return sum(1 for d in list_pending() if not d.get("held"))


def get(token: str) -> dict | None:
    p = _pending() / f"{token}.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return None
    return None


# Canonical draft states. The `held` flag (owner !HOLD) is ORTHOGONAL to these -- it hides a draft
# from the digest without moving it in this lifecycle -- so callers test d.get("held") separately.
AWAITING = "awaiting"                # request delivered (or unknown), waiting on the owner's decision
UNREQUESTED = "unrequested"          # the approval REQUEST itself was never delivered (request_sent False)
APPROVED_UNSENT = "approved_unsent"  # owner said APPROVE but the send was deferred (cap/quiet/stop)


def draft_status(d: dict) -> str:
    """The approval-lifecycle state of a staged draft, derived from its flags in the ONE place that
    interprets them -- so callers stop re-deriving 'approved-but-unsent' six different ways and stop
    misreading the `approved` flag (which means approved-but-NOT-yet-sent, not delivered):
      - APPROVED_UNSENT: owner approved, send deferred; retry_approved releases it on the next slot;
      - UNREQUESTED:     the approval request never reached the owner; retry_undelivered re-sends it;
      - AWAITING:        request delivered (or legacy/unknown), waiting on the owner's decision."""
    if d.get("approved"):
        return APPROVED_UNSENT
    if d.get("request_sent") is False:
        return UNREQUESTED
    return AWAITING


def _mark_request_sent(token: str, sent: bool) -> None:
    """Record on the staged draft whether its approval-REQUEST email was actually delivered. A
    request refused at send time (e.g. the global cap was exhausted) leaves request_sent=False so
    retry_undelivered re-sends it later, instead of the draft sitting silently with the owner
    never notified."""
    p = _pending() / f"{token}.json"
    if not p.exists():
        return
    try:
        d = json.loads(p.read_text())
    except json.JSONDecodeError:
        return
    d["request_sent"] = sent
    atomicio.write_text(p, json.dumps(d, indent=2))


def _approval_links(cfg, token: str) -> tuple[str, str]:
    """(approve, reject) one-tap mailto: links for a token -- a NEW message with the exact anchored
    subject the matcher needs, sidestepping the plain-'Reply' trap. Shared by the per-draft request
    and the consolidated backlog email."""
    addr = gmail.reply_address(cfg)
    return (f"mailto:{addr}?subject={quote(f'APPROVE {token}')}",
            f"mailto:{addr}?subject={quote(f'REJECT {token}')}")


def request_approval(token: str, subject: str, body: str, cfg, log) -> bool:
    """Email the owner the draft + token so they can APPROVE/REJECT. This send is the supervision
    channel itself, so it goes out normally (to the +staging tag). Returns True if the request was
    delivered (or staged in DRY_RUN), False if the send was refused, and records that on the draft
    so an undelivered request can be retried on a later tick."""
    # One-tap approve/reject: a mailto link opens a NEW message with the exact subject the
    # matcher needs (APPROVE_RE is anchored). This sidesteps the plain-'Reply' trap, where the
    # subject becomes 'Re: [cagent] APPROVE …' and is NOT recognized — and it keeps approve and
    # reject distinct, which a plain reply (inheriting the request's subject) cannot.
    approve_link, reject_link = _approval_links(cfg, token)
    req_body = (
        "A dispatch is ready for your approval.\n\n"
        f"  APPROVE  ->  {approve_link}\n"
        f"  REJECT   ->  {reject_link}\n\n"
        f"Tap a link (it opens a message with the right subject pre-filled), or compose a NEW "
        f"message whose SUBJECT is exactly 'APPROVE {token}' or 'REJECT {token}'. A plain 'Reply' "
        f"(subject 'Re: …') is not recognized — the subject must start with the verb.\n\n"
        f"--- proposed subject ---\n{subject}\n\n--- proposed body ---\n{body}\n"
    )
    # HTML twin so Gmail renders the mailto links as one-tap buttons WITH the subject pre-filled
    # (plain-text auto-linkers drop the ?subject query, so only the bare address is clickable).
    # The hrefs carry a single query param (no '&'), so they are safe to embed unescaped.
    req_html = (
        "<p>A dispatch is ready for your approval.</p>"
        '<p style="font-size:1.15em">'
        f'<a href="{approve_link}">&#9989; APPROVE</a>'
        "&nbsp;&nbsp;&nbsp;|&nbsp;&nbsp;&nbsp;"
        f'<a href="{reject_link}">&#10060; REJECT</a></p>'
        "<p>Tapping a link opens a new message with the right subject pre-filled. Or compose a NEW "
        f"message whose subject is exactly <code>APPROVE {token}</code> or <code>REJECT {token}</code> "
        "(a plain Reply, subject &lsquo;Re: &hellip;&rsquo;, is not recognized).</p>"
        f"<p><b>Proposed subject:</b><br>{html.escape(subject)}</p>"
        "<p><b>Proposed body:</b></p>"
        '<pre style="white-space:pre-wrap;border-left:3px solid #ccc;padding:0 0 0 10px">'
        f"{html.escape(body)}</pre>"
    )
    try:
        gmail.send(subject=f"[cagent] DRAFT REQUEST {token}: {subject[:60]}", body_md=req_body,
                   kind="approval", html_body=req_html)
        log.info("approval requested for draft %s", token)
        _mark_request_sent(token, True)
        return True
    except gmail.SendRefused as e:
        log.info("approval-request send refused (will retry): %s", e)
        _mark_request_sent(token, False)
        return False


def approve(token: str, cfg, log, override_body: str | None = None) -> dict:
    """Release a staged draft. `override_body` (the EDIT command) replaces the body with the
    owner's own text before sending: the owner is then the author, so it is trusted (the
    gate-check exists to catch the AGENT's claims, not the owner's edits); gmail.send still
    enforces the AI-disclosure footer and the per-draft token still authenticates."""
    d = get(token)
    if not d:
        return {"approved": False, "reason": "unknown token"}
    body = override_body if override_body is not None else d["body"]
    try:
        r = gmail.send(subject=d["subject"], body_md=body, kind=d.get("kind", "finding"))
        (_pending() / f"{token}.json").unlink(missing_ok=True)
        log.info("released approved draft %s (edited=%s dry_run=%s)",
                 token, override_body is not None, r.dry_run)
        return {"approved": True, "sent": r.ok, "dry_run": r.dry_run, "to": r.to,
                "edited": override_body is not None}
    except gmail.SendRefused as e:
        # The owner said YES; only a send slot is missing (cap/quiet/stop). Record the approval on
        # the draft so it leaves the "awaiting your approval" queue and enters "approved, waiting to
        # send" -- retry_approved() then releases it the moment headroom exists. Without this the
        # approval intent was silently lost (the triggering APPROVE email is already marked
        # processed, so it never re-fires) and the draft sat forever looking un-acted-upon.
        d["approved"] = True
        d["approved_at"] = clock.iso()
        d["body"] = body                                   # persist the EDIT override, if any
        d["edited"] = override_body is not None
        d["send_deferred_reason"] = str(e)
        atomicio.write_text(_pending() / f"{token}.json", json.dumps(d, indent=2))
        log.info("approved draft %s queued for send (deferred: %s)", token, e)
        return {"approved": True, "sent": False, "queued": True, "reason": str(e)}


def retry_approved(cfg, log) -> list[dict]:
    """Release owner-APPROVED drafts whose send was deferred (cap/quiet/stop when the approval
    landed). Runs every tick, BEFORE approval-request retries, so content the owner already blessed
    gets first claim on the scarce send cap ahead of asking about new drafts. Cap-bounded by the
    send gate itself: a still-refused draft stays queued for a later tick, so this never spams. When
    a draft finally goes out, the owner receives the content itself -- that IS the confirmation, so
    no extra send is spent. Returns the drafts released this call."""
    def attempt(d):
        tok = d.get("token")
        if not tok or draft_status(d) != APPROVED_UNSENT:
            return None                                    # not an approved-deferred draft -> skip
        r = gmail.send(subject=d["subject"], body_md=d["body"], kind=d.get("kind", "finding"))
        log.info("released deferred-approved draft %s (dry_run=%s)", tok, r.dry_run)
        return {"token": tok, "subject": (d.get("subject") or "")[:60], "dry_run": r.dry_run}
    return deferred.drain(_pending(), attempt, log)


def hold(token: str) -> dict:
    """Defer a draft: keep it staged but mark it held so the daily digest stops re-listing it."""
    d = get(token)
    if not d:
        return {"held": False, "reason": "unknown token"}
    d["held"] = True
    atomicio.write_text(_pending() / f"{token}.json", json.dumps(d, indent=2))
    return {"held": True, "token": token}


def reject(token: str, reason: str = "") -> dict:
    """Discard a staged draft. A `reason` (REJECT <token>: <reason>) is fed back into memory so
    the persona learns why a draft was turned down."""
    p = _pending() / f"{token}.json"
    d = get(token)
    existed = p.exists()
    p.unlink(missing_ok=True)
    if reason and d:
        from cagent import memory
        memory.write_note(f"Rejected draft: {d.get('subject', '')[:50]}",
                          f"The owner rejected a staged draft.\nReason: {reason}\n\n"
                          f"Subject was: {d.get('subject', '')}",
                          tags=["feedback", "rejection"], kind="feedback")
    return {"rejected": existed, "token": token, "reason": reason or None}


def _request_was_delivered(token: str) -> bool:
    """Backfill helper for drafts staged before request_sent was tracked: True if a delivered
    (non-dry-run) approval-request row for this token exists in the per-persona or shared ledger,
    so the many already-notified drafts are not re-sent on the first retry pass."""
    for ledger in (config.state_root() / "send_ledger.jsonl",
                   config.shared_root() / "send_ledger.jsonl"):
        if not ledger.exists():
            continue
        for line in ledger.read_text().splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("kind") == "approval" and not r.get("dry_run") and token in (r.get("subject") or ""):
                return True
    return False


def retry_undelivered(cfg, log) -> list[dict]:
    """Re-send the approval request for any staged draft whose request was never delivered (refused
    when the send gate was over cap / stopped). Cap-bounded by the gate itself: a still-over-cap
    retry is refused again and stays queued for a later tick, so this never spams. Drafts staged
    before request_sent existed are backfilled from the ledger once (so already-notified drafts are
    not re-sent). Returns the drafts whose request was delivered this call."""
    out = []
    for d in list_pending():
        tok = d.get("token")
        if not tok or draft_status(d) == APPROVED_UNSENT:  # approved -> awaiting a send slot, not the owner
            continue
        if "request_sent" not in d:                        # legacy draft: infer delivery from the ledger
            delivered = _request_was_delivered(tok)
            _mark_request_sent(tok, delivered)
            d["request_sent"] = delivered
        if d.get("request_sent"):
            continue
        if request_approval(tok, d.get("subject", ""), d.get("body", ""), cfg, log):
            out.append({"token": tok, "subject": (d.get("subject") or "")[:60]})
    return out


# --------------------------- daily digest --------------------------- #

def _today_journal() -> list[dict]:
    return [e for e in atomicio.read_jsonl(_journal())
            if str(e.get("ts", "")).startswith(clock.today()) and e.get("kind") == "tick"]


def build_digest() -> tuple[str, str]:
    ticks = _today_journal()   # already filtered to today's kind=="tick" entries
    actions: dict[str, int] = {}
    for e in ticks:
        for a in e.get("actions", []) or []:
            actions[a] = actions.get(a, 0) + 1
    goals = []
    goals_path = _goals()
    if goals_path.exists():
        try:
            goals = json.loads(goals_path.read_text())
        except json.JSONDecodeError:
            goals = []
    live = [d for d in list_pending() if not d.get("held")]     # held drafts (!HOLD) stop re-prompting
    awaiting = [d for d in live if draft_status(d) != APPROVED_UNSENT]        # need the owner's decision
    approved_unsent = [d for d in live if draft_status(d) == APPROVED_UNSENT]  # yes; only a send slot is missing
    lines = [f"Daily dispatch, {clock.today()}.", "",
             f"Ticks today: {len(ticks)}. Actions: " + (", ".join(f"{k}×{v}" for k, v in actions.items()) or "none") + ".",
             "", "Active quests:"]
    lines += [f"  - [{g['id']}] {g['title']}" for g in goals if g.get("status") == "active"] or ["  (none)"]
    if awaiting:
        lines += ["", f"Drafts awaiting your approval ({len(awaiting)}):"]
        lines += [f"  - {d['token']}: {d['subject']}"
                  + ("   (approval request not yet delivered)" if draft_status(d) == UNREQUESTED else "")
                  for d in awaiting]
    if approved_unsent:
        lines += ["", f"Approved, waiting to send ({len(approved_unsent)}) -- no action needed; "
                  "they go out as send capacity frees up:"]
        lines += [f"  - {d['token']}: {d['subject']}" for d in approved_unsent]
    return f"[cagent] daily digest {clock.today()}", "\n".join(lines)


def send_digest(cfg, log) -> dict:
    subject, body = build_digest()
    try:
        r = gmail.send(subject=subject, body_md=body, kind="digest")
        return {"sent": r.ok, "dry_run": r.dry_run}
    except gmail.SendRefused as e:
        return {"sent": False, "reason": str(e)}


# --------------------------- on-demand status (!STATUS) --------------------------- #

def build_status(cfg) -> tuple[str, str]:
    """Live operational snapshot, sent when the owner emails !STATUS. Prepends current operating
    state (mode, kill switches, backoff, last tick) onto the daily-digest rollup. Best-effort:
    each piece degrades to a placeholder rather than raising, so a status reply always composes."""
    from cagent import control
    from cagent.cognition import backoff
    who = cfg.persona or "(single-persona)"
    paused = (config.REPO_ROOT / "var" / "STOP").exists() or (
        bool(cfg.persona) and control.is_paused(cfg.persona))
    sending_stopped = (config.state_root() / "stop_sending.flag").exists()
    try:
        gate_open, reason = backoff.gate_open()
    except Exception:
        gate_open, reason = True, ""
    last_path = config.REPO_ROOT / "var" / "last_tick.json"
    last = " ".join(last_path.read_text().split()) if last_path.exists() else "(none yet)"
    head = [
        f"Status for {who}, {clock.iso()}.", "",
        f"Mode:         {cfg.MODE}",
        f"Paused:       {'YES (skips ticks until resumed locally)' if paused else 'no'}",
        f"Sending:      {'STOPPED via !STOP-SENDING' if sending_stopped else 'enabled'}",
        f"Backoff gate: {'open' if gate_open else 'DEFERRED (' + reason + ')'}",
        f"Last tick:    {last}",
        "",
    ]
    _, digest_body = build_digest()
    return f"[cagent] status {clock.today()}", "\n".join(head) + digest_body


def send_status(cfg, log) -> dict:
    """Answer an owner !STATUS request. Goes through the normal send gate (DRY_RUN stages it,
    SUPERVISED/LIVE send it: a status reply is never gated for approval, same as the digest)."""
    subject, body = build_status(cfg)
    try:
        r = gmail.send(subject=subject, body_md=body, kind="status")
        return {"sent": r.ok, "dry_run": r.dry_run}
    except gmail.SendRefused as e:
        return {"sent": False, "reason": str(e)}


# --------------------------- scorecard --------------------------- #

def persona_stats(state_dir) -> dict:
    """Aggregate a persona's committed journal + pending dir into the counts the scorecard AND the
    `cagentctl readiness` table both report: ticks / ok / fail / days / gate-blocked / refused /
    pending / last_ts. Reads committed state directly (so it is correct on a mirror); `state_dir` is
    a persona's state root. One home for this counting (was duplicated in cli.cmd_readiness)."""
    rows = atomicio.read_jsonl(state_dir / "journal.jsonl")
    ticks = [e for e in rows if e.get("kind") == "tick"]
    ok = sum(1 for e in ticks if e.get("ok"))
    days = sorted({str(e.get("ts", ""))[:10] for e in ticks if e.get("ts")})
    blocked = refused = 0
    for e in ticks:
        for r in e.get("results", []) or []:
            if r.get("blocked_by_gate"):
                blocked += 1
            if r.get("refused"):
                refused += 1
    pend_dir = state_dir / "emails" / "pending"
    pending = approved_unsent = 0
    for p in (pend_dir.glob("*.json") if pend_dir.exists() else []):
        try:
            is_approved = draft_status(json.loads(p.read_text())) == APPROVED_UNSENT
        except (json.JSONDecodeError, OSError):
            is_approved = False
        if is_approved:
            approved_unsent += 1                            # owner said yes; only a send slot is missing
        else:
            pending += 1                                    # still awaiting the owner's decision
    return {"ticks": len(ticks), "ok": ok, "fail": len(ticks) - ok, "days": days,
            "blocked": blocked, "refused": refused, "pending": pending,
            "approved_unsent": approved_unsent,
            "last_ts": max((e.get("ts") for e in ticks if e.get("ts")), default="")}


def scorecard() -> str:
    # persona_stats + state_root resolve at CALL time, so `cagentctl scorecard --persona X` (which
    # sets CAGENT_PERSONA after this module imported) reads X's namespace instead of the flat legacy
    # state/ (the old "all zeros on a mirror" bug).
    state = config.state_root()
    persona = state.name if state.parent.name == "personas" else "(legacy flat state/)"
    s = persona_stats(state)
    days = s["days"]
    md = [
        f"# cagent soft-launch scorecard ({clock.today()})", "",
        f"- Persona: {persona}",
        f"- Days with ticks: {len(days)} ({days[0] if days else '-'} .. {days[-1] if days else '-'})",
        f"- Ticks: {s['ticks']} (ok: {s['ok']})",
        f"- Gate-blocked drafts: {s['blocked']}",
        f"- Sends refused (cap/allowlist/stop): {s['refused']}",
        f"- Drafts currently pending approval: {s['pending']}", "",
        "## Graduation criteria (>=14 days, ~300 ticks, zero unsafe egress)",
        f"- [{'x' if len(days) >= 14 else ' '}] >= 14 days observed",
        f"- [{'x' if s['ticks'] >= 300 else ' '}] ~300 ticks",
        "- [ ] zero unsafe egress attempts that bypassed a guard (review manually)",
        "- [ ] goal evolution stayed coherent (review manually)",
        "- [ ] daily push succeeded every day (review manually)", "",
        "Graduation to LIVE is a manual owner decision; this is evidence, not an auto-gate.",
    ]
    text = "\n".join(md)
    _scorecard_path().write_text(text + "\n")
    return text


# --------------------------- auto-downgrade tripwire --------------------------- #

def _downgrade_notice_pending():
    return config.state_root() / "mode_downgrade_notice_pending"


def _try_downgrade_notice(cfg, log, fails=None) -> bool:
    """Send the LIVE->SUPERVISED notice, clearing the pending marker on success. kind='alert' is
    cap-exempt so a full send cap can't swallow it. Returns True iff delivered."""
    n = f"{fails} consecutive unsuccessful ticks" if fails else "repeated unsuccessful ticks"
    try:
        gmail.send(subject="[cagent] auto-downgraded to SUPERVISED",
                   body_md=(f"I detected {n} and have downgraded myself from LIVE to SUPERVISED out "
                            "of caution. I will draft for your approval until you clear "
                            "state/mode_override and restore LIVE."), kind="alert")
        _downgrade_notice_pending().unlink(missing_ok=True)
        return True
    except gmail.SendRefused as e:
        log.info("downgrade notice refused (will retry next tick): %s", e)
        return False


def check_tripwire(cfg, log, fail_threshold: int = 5) -> dict:
    """If the last N ticks show repeated failure/guardrail trips while LIVE, drop to SUPERVISED
    (write an override config respects) and tell the owner why. The owner notice is retried on a
    later tick if the send is refused -- the downgrade is durable but must never leave the owner
    silently unaware they lost LIVE."""
    if cfg.MODE != "LIVE":
        # Already downgraded: keep retrying an undelivered notice so it is never permanently lost.
        if _downgrade_notice_pending().exists():
            _try_downgrade_notice(cfg, log)
        return {"tripped": False, "reason": "not LIVE"}
    recent = _today_journal()[-fail_threshold:]
    fails = sum(1 for e in recent if not e.get("ok"))
    if len(recent) >= fail_threshold and fails >= fail_threshold:
        atomicio.write_text(_mode_override(), "SUPERVISED")
        atomicio.write_text(_downgrade_notice_pending(), clock.iso())  # arm; cleared once delivered
        _try_downgrade_notice(cfg, log, fails=fails)
        log.info("AUTO-DOWNGRADE: LIVE -> SUPERVISED (%d/%d fails)", fails, len(recent))
        return {"tripped": True, "fails": fails}
    return {"tripped": False}


# --------------------------- gate-check stall alarm --------------------------- #

def _gate_stall_flag():
    return config.state_root() / "gate_stall_alert.json"


def gate_block_streak(state) -> tuple[int, str]:
    """Most recent run of send_email attempts blocked by the gate-check, newest tick first, stopping
    at the first send that actually went out (sent/supervised/staged). A long streak is a SILENT send
    stall -- the persona keeps drafting but every draft fails fact-check and nothing reaches the owner
    (invisible outside the journal; this is how a persona can go days without mail). Canonical home;
    cli.cmd_status delegates here."""
    rows = atomicio.read_jsonl(state / "journal.jsonl")
    streak, last_reason = 0, ""
    for e in reversed(rows):
        sends = [r for r in (e.get("results") or [])
                 if isinstance(r, dict) and r.get("type") == "send_email"]
        if not sends:
            continue
        if any(r.get("sent") or r.get("supervised") or r.get("dry_run") for r in sends):
            break  # a draft got out here; the streak ends
        for r in sends:
            if "blocked_by_gate" in r:
                streak += 1
                if not last_reason:
                    v = r["blocked_by_gate"] or {}
                    flags = [k for k in ("fabrication", "false_victory", "hidden_failure",
                                         "metaphor_leak", "safety") if v.get(k)]
                    last_reason = ",".join(flags) + (" (after revise)" if r.get("revised") else "")
    return streak, last_reason


def check_gate_stall(cfg, log, threshold: int = 3) -> dict:
    """Alarm on a silent send-stall: when >= threshold consecutive drafts are gate-blocked with
    nothing delivered, email the owner so the persona isn't dark for days unnoticed. At most one
    alert per day (a multi-day stall re-reminds daily); the dedupe clears the moment a send gets
    through. Best-effort: the alert goes through the normal send gate (it is NOT gate-checked -- it
    is a supervise send, not a model draft), so a closed cap/quiet just defers it."""
    state = config.state_root()
    streak, reason = gate_block_streak(state)
    flag = _gate_stall_flag()
    if streak < threshold:
        daymarker.clear(flag)                              # stall cleared -> reset the dedupe
        return {"stalled": False, "streak": streak}
    if daymarker.done_today(flag):
        return {"stalled": True, "alerted": False, "streak": streak}   # already alerted today
    who = cfg.persona or "this agent"
    body = (f"{who} has had {streak} consecutive draft(s) blocked by the fact-check gate with nothing "
            f"delivered (most recent reason: {reason or 'n/a'}). Its letters are not reaching you.\n\n"
            f"Inspect: cagentctl recent --persona {cfg.persona or ''} / cagentctl status --persona "
            f"{cfg.persona or ''}. A long streak often means the gate lacks the note a claim rests on, "
            "not that the draft is wrong.")
    try:
        r = gmail.send(subject=f"[stall] {who}: {streak} drafts blocked by gate-check",
                       body_md=body, kind="alert")
        daymarker.mark(flag, streak=streak)
        log.info("gate-stall alert sent for %s (streak=%d)", who, streak)
        return {"stalled": True, "alerted": True, "streak": streak, "dry_run": r.dry_run}
    except gmail.SendRefused as e:
        log.info("gate-stall alert refused (will retry next tick/day): %s", e)
        return {"stalled": True, "alerted": False, "streak": streak, "reason": str(e)}


# --------------------------- approval reminders + backlog --------------------------- #

REMINDER_DAYS = [3, 7, 14]   # re-send a pending draft's approval request at these ages, then discard
EXPIRE_GRACE_DAYS = 3        # after the final reminder, allow this many more days before discarding


def _age_days(created: str) -> float | None:
    try:
        return (clock.now() - datetime.fromisoformat(created)).total_seconds() / 86400
    except (TypeError, ValueError):
        return None


def send_approval_backlog(cfg, log) -> dict:
    """ONE consolidated email listing every pending (non-held) draft with its own one-tap APPROVE/
    REJECT links, so the owner can clear a whole persona's backlog in a single pass -- instead of a
    burst of per-draft requests (25 across the fleet, mostly duplicates, would flood + hit send caps).
    Counts as a single send. Returns {sent, count} / {skipped}."""
    pend = [d for d in list_pending()
            if not d.get("held") and draft_status(d) != APPROVED_UNSENT and d.get("token")]
    if not pend:
        return {"skipped": "no pending drafts"}
    who = cfg.persona or "this agent"
    lines = [f"{len(pend)} draft(s) from {who} are awaiting your approval. "
             "Tap APPROVE or REJECT under each (each opens a message with the right subject "
             "pre-filled), or reply with subject 'APPROVE <token>' / 'REJECT <token>'.", ""]
    items_html = []
    for d in pend:
        tok = d["token"]
        approve, reject = _approval_links(cfg, tok)
        age = _age_days(d.get("created", ""))
        agestr = f"{age:.0f}d old" if age is not None else "age?"
        lines += [f"[{tok}] {d.get('subject', '')[:70]}  ({agestr})",
                  f"  APPROVE -> {approve}",
                  f"  REJECT  -> {reject}", ""]
        items_html.append(
            f"<li style='margin-bottom:10px'><b>{html.escape(d.get('subject', '')[:90])}</b> "
            f"<span style='color:#888'>({agestr}, <code>{tok}</code>)</span><br>"
            f'<a href="{approve}">&#9989; APPROVE</a>&nbsp;&nbsp;|&nbsp;&nbsp;'
            f'<a href="{reject}">&#10060; REJECT</a></li>')
    body = "\n".join(lines)
    html_body = (f"<p>{len(pend)} draft(s) from {html.escape(who)} are awaiting your approval.</p>"
                 f"<ul>{''.join(items_html)}</ul>")
    try:
        r = gmail.send(subject=f"[cagent] {len(pend)} approval(s) pending for {who}",
                       body_md=body, kind="approval", html_body=html_body)
        log.info("sent approval backlog for %s (%d drafts)", who, len(pend))
        return {"sent": r.ok, "dry_run": r.dry_run, "count": len(pend)}
    except gmail.SendRefused as e:
        log.info("approval backlog send refused: %s", e)
        return {"sent": False, "reason": str(e), "count": len(pend)}


def remind_and_expire_approvals(cfg, log) -> dict:
    """Approval lifecycle: re-send a staged draft's approval request at 3, 7, and 14 days after it was
    staged (owner saw it but hasn't acted), then DISCARD it EXPIRE_GRACE_DAYS after the FINAL reminder --
    so the owner has a window to act on the last notice before the draft is silently dropped.
    Held drafts (!HOLD) are left untouched. One reminder per run (next milestone due); a refused
    reminder is not recorded, so it retries next run. Skips drafts whose initial request was not yet
    sent (request_sent=False) to avoid double-sending alongside retry_undelivered. SUPERVISED only."""
    out = {"reminded": [], "expired": []}
    for d in list_pending():
        tok = d.get("token")
        if not tok or d.get("held") or draft_status(d) == APPROVED_UNSENT:   # approved: retry_approved owns it
            continue
        # Skip drafts where the initial request hasn't been delivered yet; retry_undelivered handles
        # those, and sending a reminder on the same tick would double-notify the owner (M8).
        if not d.get("request_sent"):
            continue
        age = _age_days(d.get("created", ""))
        if age is None:
            continue
        sent = d.get("reminders", [])
        # Discard only after the final reminder AND the grace window, so the owner has time to act.
        if len(sent) >= len(REMINDER_DAYS):
            last_reminder_age = _age_days(sent[-1]) if sent else age
            if last_reminder_age is not None and last_reminder_age >= EXPIRE_GRACE_DAYS:
                (_pending() / f"{tok}.json").unlink(missing_ok=True)
                out["expired"].append(tok)
                log.info("approval draft %s discarded after %d reminders (%.1fd, grace %.1fd)",
                         tok, len(sent), age, last_reminder_age)
            continue
        if age >= REMINDER_DAYS[len(sent)]:
            if request_approval(tok, d.get("subject", ""), d.get("body", ""), cfg, log):
                cur = get(tok)
                if cur is not None:
                    cur["reminders"] = sent + [clock.iso()]
                    atomicio.write_text(_pending() / f"{tok}.json", json.dumps(cur, indent=2))
                out["reminded"].append(tok)
    return out
