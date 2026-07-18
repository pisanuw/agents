"""Gmail transport: SMTP send (this session) + IMAP receive (Session 4). The Mac only
makes outbound connections. The recipient is ALWAYS the owner (staging in non-LIVE) —
hard-locked in code so no model output or injected instruction can redirect mail.
"""
from __future__ import annotations

import contextlib
import email
import fcntl
import html as htmlmod
import imaplib
import json
import logging
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from pathlib import Path
from urllib.parse import quote

from cagent import atomicio, clock, config

log = logging.getLogger("cagent")

OUTBOX = config.REPO_ROOT / "var" / "outbox"


# EVERY per-persona state path is resolved at CALL time (NOT frozen at import) so a CLI command that
# selects a persona at runtime (cagentctl approve/digest/poll --persona X) reads and writes under X's
# namespace, not the flat legacy state/. The tick subprocess (CAGENT_PERSONA set before import) is
# unaffected either way. Freezing these at import was the bug behind the per-persona kill switch
# (!STOP-SENDING/!QUIET/!THROTTLE) being silently bypassed and sent-mail landing in the wrong dir on
# the CLI path. The SHARED_* paths below are global (state/shared/), so they stay module constants.
def _ledger():
    return config.state_root() / "send_ledger.jsonl"


def _sent_dir():
    return config.state_root() / "emails" / "sent"


def _received_dir():
    return config.state_root() / "emails" / "received"


def _cursor():
    return config.state_root() / "imap_cursor.json"          # legacy single-persona poll_imap cursor


def _stop_sending():
    return config.state_root() / "stop_sending.flag"


def _send_lock():
    return config.state_root() / "send_cap.lock"


def _global_send_lock():
    # Shared across ALL personas: serializes the GLOBAL anti-flood cap check + send + record so two
    # personas (or a manual `cagentctl approve` racing a tick) cannot both read gday < cap and both
    # send. The per-persona _send_lock() only serializes one persona against itself.
    return config.shared_root() / "send_cap.lock"


def _quiet_until():
    return config.state_root() / "quiet_until.json"          # !QUIET: outbound muted until a timestamp


def _throttle():
    return config.state_root() / "throttle.json"             # !THROTTLE: today's lowered daily cap
# Phase 4 (multi-persona mailbox): ONE shared cursor for dispatcher ingest, and a sent-index
# mapping each outbound Message-ID to the persona that sent it (so replies route back).
SHARED_CURSOR = config.shared_root() / "imap_cursor.json"
SENT_INDEX = config.shared_root() / "sent_index.jsonl"
SHARED_LEDGER = config.shared_root() / "send_ledger.jsonl"   # GLOBAL send cap across all personas
SIGNATURE = config.REPO_ROOT / "persona" / "signature.txt"   # shared/default; per-persona overrides below
DISCLOSURE_MARKER = "autonomously by an AI research agent"
# Config defaults used when ~/.config/cagent/.env is absent. A non-DRY_RUN send with any of these
# still set means the host is unconfigured -- refuse rather than email a bogus address (see send()).
PLACEHOLDER_IDENTITIES = frozenset({
    "owner@example.com", "agent@example.com", "owner+cagent-staging@example.com"})


@dataclass
class SendResult:
    ok: bool
    dry_run: bool
    to: str
    message_id: str
    reason: str = ""


class SendRefused(Exception):
    """Outbound gate refused the send (allowlist, cap, or missing disclosure)."""


def _signature(cfg) -> str:
    # Per-persona signature: personas/<name>/signature.txt overrides the shared default, so each
    # persona signs in its own voice. Fallback is generic (no persona name, no specific email).
    # The literal token {REPLY_ADDRESS} is substituted with this persona's live reply address, so
    # the committed signature files hold no real address (the address comes from the gitignored
    # config at send time) while the disclosed reply-to is always correct.
    persona = os.environ.get("CAGENT_PERSONA", "").strip()
    text = None
    if persona:
        p = config.REPO_ROOT / "personas" / persona / "signature.txt"
        if p.exists():
            text = p.read_text().strip()
    if text is None and SIGNATURE.exists():
        text = SIGNATURE.read_text().strip()
    if text is None:
        text = ("This message was written and sent autonomously by an AI research agent "
                "({REPLY_ADDRESS}). It corresponds only with you. Replies are read; nothing "
                "here was reviewed by a human first.")
    return text.replace("{REPLY_ADDRESS}", reply_address(cfg))


def _command_footer(cfg) -> tuple[str, str]:
    """The "steer me by email" menu appended to every outbound message: each email command rendered
    as a mailto: link whose SUBJECT is pre-filled, mirroring the one-tap APPROVE/REJECT links. The
    COMMAND_TOKEN is a shared secret that must never land in git-committed sent-mail (see gmail's
    _redact_command_token / commands.note_token_exposure), so the links pre-fill a literal <TOKEN>
    PLACEHOLDER the owner replaces before sending -- never the live token. commands.HELP_LINES is the
    single source of truth for the command list. Returns (plain_text, html)."""
    from cagent import commands   # lazy: commands imports gmail, so a top-level import would cycle
    addr = reply_address(cfg)
    intro = ("Steer me by email: send a NEW message to this address with one of the subjects below, "
             "replacing <TOKEN> with your command token. Email can only tighten, never relax.")
    text_lines = [intro, ""]
    html_items = []
    for label, desc in commands.HELP_LINES:
        cmd = label.split(" / ")[0]                # "!HELP / !COMMANDS" -> "!HELP"; keeps arg hints
        subject = f"{cmd} <TOKEN>"
        link = f"mailto:{addr}?subject={quote(subject)}"     # <TOKEN> -> %3CTOKEN%3E, safe in the href
        text_lines.append(f"  {subject:<26} {desc}")
        html_items.append(f'<li><a href="{link}">{htmlmod.escape(cmd)}</a> '
                          f"&mdash; {htmlmod.escape(desc)}</li>")
    tail = "SUPERVISED drafts: reply with subject 'APPROVE|REJECT|EDIT|HOLD <token>'."
    text_lines += ["", tail]
    text = "\n".join(text_lines)
    html = ('<div style="color:#777;font-size:0.9em">'
            f"<p>{htmlmod.escape(intro)}</p>\n<ul>" + "".join(html_items) + "</ul>\n"
            "<p>SUPERVISED drafts: reply with subject "
            "<code>APPROVE|REJECT|EDIT|HOLD &lt;token&gt;</code>.</p></div>")
    return text, html


def _to_header(cfg, recipient: str) -> str:
    """The To: field value. Renders the owner's display name when known ("Owner Name <addr>");
    the address is the hard-locked recipient, so the name is cosmetic and cannot redirect mail."""
    name = getattr(cfg, "owner_name", "")
    return formataddr((name, recipient)) if name else recipient


def reply_address(cfg) -> str:
    """The address an owner reply must go TO so the dispatcher routes it back to the right
    persona: the persona's plus-tagged address (local+tag@domain), or the bare agent address
    in legacy single-persona mode. Single source of truth for both the Reply-To header and the
    APPROVE/REJECT mailto links."""
    if cfg.plus_tag:
        local, _, domain = cfg.agent_email.partition("@")
        return f"{local}+{cfg.plus_tag}@{domain}"
    return cfg.agent_email


# The send cap throttles the agent's UNSOLICITED, self-generated output to the owner (findings,
# digests, reflections). These kinds are NOT that: they are supervision plumbing (approval requests /
# reminders / backlog), owner-SOLICITED command replies (status/help/ping/goals/ack), or safety
# notifications (alert). Counting them against the content cap is backwards -- it starves the very
# channel the owner steers with, and once left owner-APPROVED drafts unsendable behind a wall of
# approval-REQUEST emails (9 of 10 weekly slots). So they neither consume nor are blocked by the cap.
# (The owner's hard halts -- !STOP-SENDING and !QUIET -- still silence everything; only the anti-flood
# volume cap is waived here.)
# "approval" covers approval requests, the 3/7/14-day reminders, and the consolidated backlog (all
# routed through request_approval / send_approval_backlog).
CAP_EXEMPT_KINDS = frozenset({"approval", "ack", "status", "help", "ping", "goals", "alert"})


def _count_ledger(path) -> tuple[int, int]:
    """(real CONTENT sends in last 24h, in last 7d) from a ledger file. DRY_RUN records and
    CAP_EXEMPT_KINDS (supervision/operational mail) do not count -- the cap governs agent-generated
    content only."""
    day = week = 0
    if not path.exists():
        return 0, 0
    now = clock.now()
    for line in path.read_text().splitlines():
        if not line.strip():
            continue                                  # blank line is not a record
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            # Corrupt row: fail CLOSED. Count it as a recent send so a mangled ledger TIGHTENS the
            # anti-flood cap rather than silently lifting it (skipping every bad row could read a
            # fully-corrupt ledger as 0 sends = unlimited). Over-counting a legitimate dry_run/exempt
            # row that got mangled is the safe direction here.
            day += 1
            week += 1
            continue
        if e.get("dry_run") or e.get("kind") in CAP_EXEMPT_KINDS:
            continue
        try:
            ts = datetime.fromisoformat(e["ts"])
        except (KeyError, ValueError):
            day += 1                                  # real row with an unreadable timestamp: fail closed too
            week += 1
            continue
        age = (now - ts).total_seconds()
        if age < 86400:
            day += 1
        if age < 7 * 86400:
            week += 1
    return day, week


def ledger_counts() -> tuple[int, int]:
    """Per-persona (or legacy) send counts (last 24h, 7d)."""
    return _count_ledger(_ledger())


def quiet_active() -> bool:
    """True while an owner !QUIET mute window is still in the future. Read at call time, so the
    window auto-clears at expiry with no local un-mute (the un-mute is a pre-committed local
    policy -- the timestamp -- not an email-driven relaxation)."""
    qf = _quiet_until()
    if not qf.exists():
        return False
    try:
        until = datetime.fromisoformat(json.loads(qf.read_text())["until"])
    except (json.JSONDecodeError, KeyError, ValueError):
        # Fail CLOSED: an existing-but-unreadable mute file must NOT silently un-mute the owner's
        # !QUIET. Stay muted (it clears at expiry normally, or the owner clears it locally).
        log.warning("quiet_until.json unreadable; treating as QUIET-active (fail closed)")
        return True
    return clock.now() < until


def throttle_cap(configured: int) -> int:
    """Today's effective per-persona daily cap. !THROTTLE may only LOWER it (raising stays local),
    and only for the day it was set; a stale entry is ignored."""
    tf = _throttle()
    if not tf.exists():
        return configured
    try:
        d = json.loads(tf.read_text())
    except (json.JSONDecodeError, ValueError):
        # Fail CLOSED: an existing-but-corrupt throttle means the owner asked to LOWER today's cap but
        # we cannot read the value; don't silently revert to the higher configured cap. Apply a
        # conservative floor (owner re-issues !THROTTLE or clears it locally).
        log.warning("throttle.json unreadable; applying conservative cap floor (fail closed)")
        return min(configured, 1)
    if d.get("date") != clock.today():
        return configured                                   # stale entry (different day) is ignored
    try:
        return min(configured, int(d["cap"]))
    except (KeyError, ValueError, TypeError):
        log.warning("throttle.json cap unreadable; applying conservative cap floor (fail closed)")
        return min(configured, 1)


def global_ledger_counts() -> tuple[int, int]:
    """Global send counts across all personas (last 24h, 7d)."""
    return _count_ledger(SHARED_LEDGER)


def _append_ledger(path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")


def _record(rec: dict) -> None:
    _append_ledger(_ledger(), rec)


def _record_global(rec: dict) -> None:
    _append_ledger(SHARED_LEDGER, rec)


def _plain_text(msg: EmailMessage) -> str:
    """The text/plain body, robust to a multipart/alternative message (which carries an HTML twin
    and so has no single get_content())."""
    try:
        return msg.get_content()
    except KeyError:
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return part.get_content()
        return ""


def _persist(msg: EmailMessage, rec: dict, to_outbox: bool) -> None:
    sent_dir = _sent_dir()
    sent_dir.mkdir(parents=True, exist_ok=True)
    stamp = rec["message_id"].strip("<>").split("@")[0]
    payload = {**rec, "from": msg["From"], "subject": msg["Subject"],
               "body": _plain_text(msg)}
    (sent_dir / f"{stamp}.json").write_text(json.dumps(payload, indent=2))
    if to_outbox:
        OUTBOX.mkdir(parents=True, exist_ok=True)
        (OUTBOX / f"{stamp}.json").write_text(json.dumps(payload, indent=2))


# --------------------------- model-body normalization --------------------------- #

_MODEL_BODY_KEYS = ("body", "text", "content", "message")


def _unescape_json_string(s: str) -> str:
    r"""Turn the JSON string escapes we care about (\n \t \r \" \\ \/ \uXXXX \b \f) into their
    characters, leaving already-decoded UTF-8 text (em dashes, curly quotes) untouched. Used ONLY by
    the lenient fallback in normalize_model_body, where strict json parsing has already failed
    (usually an unescaped quote inside the body), so json.loads is not an option."""
    out, i, n = [], 0, len(s)
    simple = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\",
              "/": "/", "b": "\b", "f": "\f"}
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt in simple:
                out.append(simple[nxt])
                i += 2
                continue
            if nxt == "u" and i + 5 < n:
                try:
                    out.append(chr(int(s[i + 2:i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
        out.append(c)
        i += 1
    return "".join(out)


def normalize_model_body(text: str) -> str:
    r"""Defensive normalization for a model-authored email/note body. The model is asked for raw prose
    in a string field but occasionally returns a JSON wrapper instead, e.g. {"body": "...\n..."} --
    which otherwise reaches the owner as a literal JSON dump with escaped newlines. If `text` is (or
    begins with) such a wrapper, return the inner prose with real newlines; otherwise return `text`
    unchanged. Conservative: only a LEADING object carrying a recognized text key is unwrapped, so an
    ordinary letter that merely contains a brace is never altered.

    Applied at the single send chokepoint (gmail.send) so EVERY outbound path is defanged -- a direct
    send, an approved SUPERVISED draft released a day later by supervise.approve, a retried/deferred
    send, the digest -- not just the one drafting call in execute._do_email (that left the release
    paths, which re-read the raw staged body, sending JSON dumps; observed across personas). Two
    stages: strict JSON first (well-formed, tolerates trailing prose after the object), then a lenient
    regex that recovers the common single-key {"body": "..."} shape even when the model left a quote
    inside the string unescaped -- which makes strict json.loads fail (observed in practice)."""
    s = text.strip()
    if not s.startswith("{"):
        return text
    # 1) strict: raw_decode tolerates trailing prose after the closing brace.
    try:
        obj, end = json.JSONDecoder().raw_decode(s)
        if isinstance(obj, dict):
            inner = next((obj[k] for k in _MODEL_BODY_KEYS
                          if isinstance(obj.get(k), str) and obj[k].strip()), None)
            if inner is not None:
                trailing = s[end:].strip()
                return (inner + ("\n\n" + trailing if trailing else "")).strip()
    except ValueError:
        pass
    # 2) lenient: a single {"body": "..."} wrapper whose inner text has an unescaped quote/brace that
    # defeats strict parsing. Capture from the first opening quote to the final closing quote before
    # the object's brace, then unescape the JSON escapes; anything after the brace is trailing prose.
    m = re.match(r'\s*\{\s*"(?:' + "|".join(_MODEL_BODY_KEYS) + r')"\s*:\s*"(.*)"\s*\}(.*)\Z',
                 s, re.DOTALL)
    if m:
        inner = _unescape_json_string(m.group(1)).strip()
        if inner:
            trailing = m.group(2).strip()
            return (inner + ("\n\n" + trailing if trailing else "")).strip()
    return text


def send(subject: str, body_md: str, kind: str = "finding",
         in_reply_to: str | None = None, references: str | None = None,
         thread_id: str | None = None, html_body: str | None = None) -> SendResult:
    cfg = config.load()
    # Defang a model body that arrived as a JSON wrapper ({"body": "...\n..."}). Done HERE, at the
    # single send gate, so every path is covered -- crucially the SUPERVISED release paths
    # (supervise.approve / retry_approved) that re-read the raw staged body and previously mailed the
    # owner a JSON dump. Prose/system bodies (digest, approval request, alerts) do not start with a
    # brace, so they pass through untouched.
    body_md = normalize_model_body(body_md)
    recipient = cfg.recipient  # owner in LIVE, staging otherwise — hard-locked
    allowed = {cfg.owner_email.lower(), cfg.staging_recipient.lower()}
    if recipient.lower() not in allowed:
        raise SendRefused(f"recipient {recipient!r} not in owner allowlist")

    # Fast pre-check for DRY_RUN and obvious over-cap: avoids message building when clearly refused.
    if cfg.MODE != "DRY_RUN":
        # Fail closed on placeholder identities. A missing ~/.config/cagent/.env leaves owner/agent at
        # the example.com defaults; DRY_RUN is harmless (nothing leaves), but a SUPERVISED/LIVE send
        # would email a bogus address. Refuse so a misconfigured host cannot send to a placeholder.
        if cfg.owner_email.lower() in PLACEHOLDER_IDENTITIES or cfg.agent_email.lower() in PLACEHOLDER_IDENTITIES:
            raise SendRefused("placeholder identity (example.com) in non-DRY_RUN mode; configure "
                              "~/.config/cagent/.env before sending")
        if quiet_active():
            raise SendRefused("quiet window active (!QUIET); outbound muted until it expires")

    body = body_md.rstrip() + "\n\n" + _signature(cfg) + "\n"
    if DISCLOSURE_MARKER not in body:
        raise SendRefused("refusing to send: AI-disclosure footer missing")
    # "Steer me by email" command menu, appended below the disclosure signature on every send (the
    # links carry a <TOKEN> placeholder, never the live secret, so this is safe to commit). Gated by
    # [commands].footer (default on). The disclosure check above runs on the signature only, before
    # the footer, so the footer can never satisfy the marker on its own.
    foot_text, foot_html = _command_footer(cfg) if cfg.command_footer else ("", "")
    if foot_text:
        body = body + "\n" + foot_text + "\n"

    # Persona mode (Phase 4): tag the subject and set Reply-To so the owner's reply routes back
    # to the right persona. Legacy (plus_tag == "") leaves the message byte-identical.
    subj, reply_to = subject, None
    if cfg.plus_tag:
        reply_to = f"{cfg.from_name} <{reply_address(cfg)}>"
        if not subj.lstrip().lower().startswith(f"[{cfg.plus_tag.lower()}]"):
            subj = f"[{cfg.plus_tag}] {subject}"

    msg = EmailMessage()
    msg["From"] = f"{cfg.from_name} <{cfg.agent_email}>"
    msg["To"] = _to_header(cfg, recipient)
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Subject"] = subj
    mid = make_msgid(domain=cfg.agent_email.split("@", 1)[-1])
    msg["Message-ID"] = mid
    msg["Date"] = formatdate(localtime=True)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    refs = references or in_reply_to
    if refs:
        msg["References"] = refs
    msg.set_content(body)
    if html_body is not None or foot_html:
        # Send an HTML twin (multipart/alternative) so clients render real <a href="mailto:...
        # ?subject=..."> anchors as clickable links with the subject pre-filled. Plain-text
        # auto-linkers (Gmail) only linkify the bare address and drop the ?subject query, which
        # breaks one-tap APPROVE/REJECT and the command menu. The main body is the caller's HTML
        # (approval requests) or the escaped plain body; the signature/disclosure and the command
        # footer are mirrored in so the rendered version carries both the AI-disclosure footer and
        # the same one-tap command menu.
        sig_html = htmlmod.escape(_signature(cfg)).replace("\n", "<br>\n")
        main_html = (f"<div>{html_body.rstrip()}</div>" if html_body is not None
                     else '<pre style="white-space:pre-wrap;font-family:inherit;margin:0">'
                          f"{htmlmod.escape(body_md.rstrip())}</pre>")
        twin = (f"{main_html}\n<br>\n"
                f'<div style="color:#777;font-size:0.9em">{sig_html}</div>\n')
        if foot_html:
            twin += f"<br>\n{foot_html}\n"
        msg.add_alternative(twin, subtype="html")

    rec = {"ts": clock.iso(), "to": recipient, "subject": subj, "message_id": mid,
           "kind": kind, "mode": cfg.MODE, "thread_id": thread_id,
           "in_reply_to": in_reply_to, "dry_run": cfg.MODE == "DRY_RUN"}
    if cfg.MODE == "DRY_RUN":
        if cfg.plus_tag:
            _append_sent_index(mid, cfg.persona)
            _record_global(rec)   # dry-run row; ignored by the caps but keeps index/outbox coherent
        _persist(msg, rec, to_outbox=True)
        _record(rec)
        return SendResult(ok=True, dry_run=True, to=recipient, message_id=mid)

    # Serialize cap-check + SMTP + ledger-record with a per-persona lock so a concurrent
    # tick and cagentctl approve can't both read cap-1, both pass, and both send (L9).
    # Message building is outside the lock; only the real-send decision is serialized.
    lock_path = _send_lock()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as _locks:
        _lf = _locks.enter_context(open(lock_path, "a"))
        fcntl.flock(_lf.fileno(), fcntl.LOCK_EX)
        if cfg.plus_tag:
            # Multi-persona: also hold the SHARED lock across the global-cap check + send + record.
            # Lock order is always per-persona THEN shared (every send goes through here), so no
            # deadlock. Legacy single-persona (no plus_tag) never touches the global ledger.
            glock = _global_send_lock()
            glock.parent.mkdir(parents=True, exist_ok=True)
            _glf = _locks.enter_context(open(glock, "a"))
            fcntl.flock(_glf.fileno(), fcntl.LOCK_EX)
        # Authoritative cap check under lock (fast pre-check above only caught quiet_active).
        # Supervision/operational mail (CAP_EXEMPT_KINDS) waives the anti-flood volume cap entirely:
        # it must never be blocked by content volume, and ledger_counts() already excludes it so it
        # cannot consume content budget either. The owner's hard halt (!STOP-SENDING) below still
        # applies to every kind.
        if kind not in CAP_EXEMPT_KINDS:
            day, week = ledger_counts()
            day_cap = throttle_cap(cfg.emails_per_day)
            if day >= day_cap:
                raise SendRefused(f"daily send cap reached ({day}/{day_cap})")
            if week >= cfg.emails_per_week:
                raise SendRefused(f"weekly send cap reached ({week}/{cfg.emails_per_week})")
            if cfg.plus_tag:
                gday, gweek = global_ledger_counts()
                if gday >= cfg.global_emails_per_day:
                    raise SendRefused(f"global daily cap reached ({gday}/{cfg.global_emails_per_day})")
                if gweek >= cfg.global_emails_per_week:
                    raise SendRefused(f"global weekly cap reached ({gweek}/{cfg.global_emails_per_week})")
        if _stop_sending().exists():
            raise SendRefused("STOP-SENDING flag is set; outbound halted by owner")
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=ctx) as s:
            s.login(cfg.agent_email, cfg.gmail_app_password)
            s.send_message(msg)
        # Record AFTER a successful send; recording before (old order) meant a refused/failed
        # send still consumed a global anti-flood slot and wrongly throttled other personas.
        if cfg.plus_tag:
            _append_sent_index(mid, cfg.persona)
            _record_global(rec)
        _persist(msg, rec, to_outbox=False)
        _record(rec)
    return SendResult(ok=True, dry_run=False, to=recipient, message_id=mid)


# --------------------------------------------------------------------------- #
# IMAP receive (Session 4). The Mac only polls; it never listens. Idempotency
# via (UIDVALIDITY, last_uid) + a processed Message-ID set in state/imap_cursor.json.
# --------------------------------------------------------------------------- #

def _dec(s: str | None) -> str:
    if not s:
        return ""
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return s


def _extract_text(m) -> str:
    if m.is_multipart():
        for part in m.walk():
            disp = str(part.get("Content-Disposition", ""))
            if part.get_content_type() == "text/plain" and "attachment" not in disp:
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace")
                except Exception:
                    continue
        for part in m.walk():
            if part.get_content_type() == "text/html":
                try:
                    html = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace")
                    return re.sub(r"<[^>]+>", " ", html)
                except Exception:
                    continue
        return ""
    try:
        return m.get_payload(decode=True).decode(m.get_content_charset() or "utf-8", "replace")
    except Exception:
        return m.get_payload() or ""


def _parse_message(uid: int, m) -> dict:
    return {
        "uid": uid,
        "message_id": (m.get("Message-ID") or "").strip(),
        "from": _dec(m.get("From", "")),
        "to": _dec(m.get("To", "")),
        "delivered_to": _dec(m.get("Delivered-To", "")),
        "subject": _dec(m.get("Subject", "")),
        "date": m.get("Date", ""),
        "in_reply_to": (m.get("In-Reply-To") or "").strip(),
        "references": m.get("References", ""),
        "auto_submitted": m.get("Auto-Submitted", ""),
        "precedence": m.get("Precedence", ""),
        "body_text": _extract_text(m),
        "received_at": clock.iso(),
    }


def _load_cursor() -> dict:
    cur = _cursor()
    if cur.exists():
        try:
            return json.loads(cur.read_text())
        except json.JSONDecodeError:
            pass
    return {"uidvalidity": None, "last_uid": 0, "processed_message_ids": []}


def _save_cursor(c: dict) -> None:
    cur = _cursor()
    cur.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write (matches the shared cursor + _poll_account). A torn imap_cursor.json makes
    # _load_cursor fall back to uidvalidity=None, which the next poll treats as a UIDVALIDITY change
    # and re-baselines PAST all unread mail -- silently discarding pending owner messages.
    atomicio.write_text(cur, json.dumps(c, indent=2))


# --- shared IMAP protocol reads (the bytes-level bits that were kept in sync by hand across the
# three pollers). Each poller still owns its distinct cursor store + routing policy; only these
# verbatim-identical reads are factored out. -------------------------------------------------------

def _imap_uidvalidity(conn) -> int:
    """INBOX UIDVALIDITY (0 if absent). A change means the server renumbered UIDs, so the cursor's
    last_uid no longer refers to the same messages and the caller must re-baseline."""
    typ, data = conn.status("INBOX", "(UIDVALIDITY)")
    mv = re.search(rb"UIDVALIDITY (\d+)", data[0]) if data and data[0] else None
    return int(mv.group(1)) if mv else 0


def _imap_max_uid(conn) -> int:
    """High-water UID over ALL present mail — used to baseline the cursor forward without importing
    the existing messages."""
    typ, data = conn.uid("SEARCH", None, "ALL")
    uids = [int(x) for x in data[0].split()] if data and data[0] else []
    return max(uids) if uids else 0


def _imap_uids_after(conn, last_uid: int) -> list[int]:
    """Sorted UIDs strictly greater than last_uid (SEARCH last_uid+1:*)."""
    typ, data = conn.uid("SEARCH", None, f"{last_uid + 1}:*")
    return sorted(u for u in (int(x) for x in (data[0].split() if data and data[0] else []))
                  if u > last_uid)


def _imap_fetch_parse(conn, uid: int) -> dict | None:
    """FETCH one message and parse it into the inbound dict, or None if the fetch failed."""
    typ, fd = conn.uid("FETCH", str(uid), "(RFC822)")
    if typ != "OK" or not fd or not fd[0]:
        return None
    return _parse_message(uid, email.message_from_bytes(fd[0][1]))


def _imap_connect(cfg) -> imaplib.IMAP4_SSL:
    """Open an IMAP-over-TLS connection with certificate + hostname verification.

    imaplib.IMAP4_SSL defaults to an *unverified* context (CERT_NONE, no hostname
    check), so without an explicit ssl_context the Gmail app password would be sent
    over TLS that a MITM could terminate. Mirror the verified context SMTP already
    uses (see send()). This is the single chokepoint for all IMAP connections."""
    ctx = ssl.create_default_context()
    return imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, ssl_context=ctx)


def poll_imap(commit: bool = False) -> list[dict]:
    """Pull inbox messages past the cursor. commit=True writes them to
    state/emails/received/ and advances the cursor (at-most-once). commit=False is
    read-only and repeatable. Self-addressed mail is skipped (self-loop guard).
    Full owner-allowlist + RFC 3834 filtering arrives in Session 7."""
    cfg = config.load()
    conn = _imap_connect(cfg)
    try:
        conn.login(cfg.agent_email, cfg.gmail_app_password)
        conn.select("INBOX", readonly=True)
        uidvalidity = _imap_uidvalidity(conn)

        cur = _load_cursor()
        if cur.get("uidvalidity") != uidvalidity:
            # UIDVALIDITY changed: baseline only (advance to current max without importing old mail),
            # matching what ingest() does on first run. Re-fetching would re-deliver old messages.
            log.warning("UIDVALIDITY changed (%s -> %s): re-baselining, pending owner mail discarded",
                        cur.get("uidvalidity"), uidvalidity)
            if commit:
                _save_cursor({"uidvalidity": uidvalidity, "last_uid": _imap_max_uid(conn),
                              "processed_message_ids": []})
            return []
        last_uid = int(cur.get("last_uid", 0))
        processed = set(cur.get("processed_message_ids", []))

        uids = _imap_uids_after(conn, last_uid)

        out: list[dict] = []
        new_last = last_uid
        for uid in uids:
            parsed = _imap_fetch_parse(conn, uid)
            if parsed is None:
                # A transient FETCH failure for this UID. UIDs are processed in ascending order, so
                # STOP here rather than `continue`: a continue would let a later successful UID push
                # new_last (and the saved cursor) PAST this one, permanently dropping the message on a
                # recoverable network hiccup. Breaking leaves the cursor at the last good UID; the next
                # poll retries from here (the message is re-fetched, never skipped).
                log.warning("IMAP FETCH failed for uid %s; deferring it and the rest of this batch", uid)
                break
            new_last = max(new_last, uid)
            if parsed["message_id"] and parsed["message_id"] in processed:
                continue
            if parseaddr(parsed["from"])[1].lower() == cfg.agent_email.lower():
                if commit and parsed["message_id"]:
                    processed.add(parsed["message_id"])
                continue
            _seal_inbound(parsed, cfg.command_token)
            out.append(parsed)
            if commit:
                rdir = _received_dir()
                rdir.mkdir(parents=True, exist_ok=True)
                atomicio.write_text(rdir / f"{uid}.json",
                                    json.dumps({**parsed, "processed": False}, indent=2))
                if parsed["message_id"]:
                    processed.add(parsed["message_id"])

        if commit:
            cur.update(uidvalidity=uidvalidity, last_uid=new_last,
                       processed_message_ids=list(processed)[-1000:])
            _save_cursor(cur)
        return out
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _baseline_cursor(cfg, save) -> int:
    """Connect to `cfg`'s IMAP account and advance a cursor past all present inbox mail WITHOUT
    importing it, persisting it via save({uidvalidity, last_uid, processed_message_ids}). Returns the
    high-water UID. Shared by every baseline variant so they agree on the cursor shape."""
    conn = _imap_connect(cfg)
    try:
        conn.login(cfg.agent_email, cfg.gmail_app_password)
        conn.select("INBOX", readonly=True)
        uidvalidity = _imap_uidvalidity(conn)
        maxuid = _imap_max_uid(conn)
        save({"uidvalidity": uidvalidity, "last_uid": maxuid, "processed_message_ids": []})
        return maxuid
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def baseline() -> int:
    """Legacy single-persona: baseline the FLAT poll_imap cursor (state/imap_cursor.json). On a
    multi-persona host use baseline_shared()/baseline_own_accounts() -- the cursors ingest reads."""
    return _baseline_cursor(config.load(), _save_cursor)


def baseline_shared() -> int:
    """Multi-persona: baseline the SHARED ingest cursor (state/shared/imap_cursor.json) -- the one
    ingest() actually reads. The legacy baseline() wrote a per-persona flat cursor that nothing reads
    on a multi-persona host, so `poll-baseline` was a silent no-op for the real ingest path (P1-6)."""
    return _baseline_cursor(config.load(), _save_shared_cursor)


def baseline_own_accounts() -> dict:
    """Baseline the per-account cursor for each own-account persona group (the cursors
    ingest_own_accounts() reads), so their dedicated mailboxes also start clean. Best-effort per
    account. Returns {account_email: high_water_uid}."""
    out: dict = {}
    for account, (cfg, personas) in _own_accounts().items():
        primary = _account_primary(cfg.agent_email.split("@", 1)[0], personas)
        path = _persona_cursor_path(primary)
        try:
            out[account] = _baseline_cursor(
                cfg, lambda c, p=path: atomicio.write_text(p, json.dumps(c, indent=2)))
        except Exception as e:
            log.warning("baseline_own_accounts: %r failed (continuing): %s", account, e)
    return out


def pending_inbound() -> list[dict]:
    """Unprocessed inbound (from real polls and injected fixtures alike)."""
    out = []
    rdir = _received_dir()
    if not rdir.exists():
        return out
    for p in sorted(rdir.glob("*.json")):
        try:
            d = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        if not d.get("processed"):
            d["_path"] = str(p)
            out.append(d)
    return out


TOKEN_REDACTED = "«COMMAND_TOKEN»"


def _all_command_tokens() -> set[str]:
    """Every configured COMMAND_TOKEN: the global one plus each enabled persona's own (personas may
    carry distinct tokens via their owner overlays). Redaction scrubs this WHOLE set from each stored
    mail record, so a message routed to persona B that still carries persona A's token -- one shared
    mailbox, per-owner tokens -- is never persisted with a live secret. Best-effort: a persona whose
    config fails to load is skipped; a load failure never wedges the caller."""
    tokens: set[str] = set()
    try:
        gt = config.load().command_token
        if gt:
            tokens.add(gt)
    except Exception:
        pass
    try:
        names = config.enabled_personas()
    except Exception:
        names = []
    for name in names:
        try:
            t = config.load(name).command_token
        except Exception:
            continue
        if t:
            tokens.add(t)
    return tokens


def _redact_command_token(rec: dict, tokens) -> None:
    """Scrub every known COMMAND_TOKEN out of a persisted mail record so a live secret can never land
    in committed state. Redacts across ALL string fields -- the subject/body AND every header we keep
    (to, delivered_to, in_reply_to, references, from, ...), not just four -- closing the header-leak
    gap. `tokens` may be a single token (legacy callers) or an iterable; passing the full set makes
    redaction complete regardless of which persona the message was routed to. Safe because the auth
    witness (cmd_token_ok) is recorded before this scrub, so downstream parse_and_apply still works."""
    toks = [tokens] if isinstance(tokens, str) else [t for t in (tokens or ()) if isinstance(t, str)]
    toks = [t for t in toks if t]
    if not toks:
        return
    for k, v in list(rec.items()):
        if not isinstance(v, str):
            continue
        for t in toks:
            if t in v:
                v = v.replace(t, TOKEN_REDACTED)
        rec[k] = v


def _seal_inbound(parsed: dict, token: str, all_tokens=None) -> None:
    """Ingest-time COMMAND_TOKEN handling. The auth witnesses are set from the ROUTED persona's own
    `token` (cmd_token_ok is what parse_and_apply trusts; token_seen feeds the exposure tripwire), but
    REDACTION scrubs the full `all_tokens` set across EVERY field. So no persona's live token is ever
    persisted, even when a message routed to one persona carries another's token, or the routed persona
    has no token of its own (the empty-`token` no-op that previously left the record raw on disk -- the
    2026-07 leak class). token_seen (the exposure witness feeding the burn tripwire) is set if ANY
    known token appears in ANY field, computed BEFORE redaction wipes the raw value -- so a non-owner
    leaking a DIFFERENT persona's token, or a token in a header, is still flagged (P1-7). Both witness
    keys live on a closed key set inbound mail cannot forge."""
    toks = list(all_tokens) if all_tokens is not None else ([token] if token else [])
    if token and token in (parsed.get("subject", "") or ""):
        parsed["cmd_token_ok"] = True
    if not parsed.get("token_seen"):
        blob = " ".join(v for v in parsed.values() if isinstance(v, str))
        if any(t and t in blob for t in toks):
            parsed["token_seen"] = True
    _redact_command_token(parsed, all_tokens if all_tokens is not None else token)


def mark_processed(entries: list[dict]) -> None:
    # mark_processed is load-bearing for at-most-once mail dedup, so token lookup must never wedge it.
    tokens = _all_command_tokens()
    for d in entries:
        p = Path(d.get("_path", ""))
        if p and p.exists():
            dd = {k: v for k, v in d.items() if k != "_path"}
            dd["processed"] = True
            _redact_command_token(dd, tokens)
            p.write_text(json.dumps(dd, indent=2))


# --------------------------------------------------------------------------- #
# Phase 4: multi-persona mailbox. The dispatcher is the ONE IMAP reader; ingest()
# polls past the SHARED cursor and routes each message to the target persona's
# received dir by +tag (To/Delivered-To) -> In-Reply-To (sent index) -> [tag]
# subject -> default persona. Per-persona ticks then consume only their own dir.
# --------------------------------------------------------------------------- #

def _append_sent_index(message_id: str, persona: str) -> None:
    if not message_id:
        return
    SENT_INDEX.parent.mkdir(parents=True, exist_ok=True)
    with open(SENT_INDEX, "a") as f:
        f.write(json.dumps({"message_id": message_id, "persona": persona, "ts": clock.iso()}) + "\n")


def _sent_index_map() -> dict:
    m: dict = {}
    if SENT_INDEX.exists():
        for line in SENT_INDEX.read_text().splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("message_id"):
                m[e["message_id"]] = e.get("persona", "")
    return m


def _tag_map(personas: list[str]) -> dict:
    """{tag(lower): persona} from each persona's plus_tag, also accepting the bare name."""
    out: dict = {}
    for name in personas:
        c = config.load(name)
        out[(c.plus_tag or name).lower()] = name
        out[name.lower()] = name
    return out


_ADDR_RE = re.compile(r"[\w.+-]+@[\w.-]+")


def _extract_tag(fields: list[str], agent_local: str) -> str:
    """The +tag from any address whose local part is <agent_local>+tag, else ''."""
    for field in fields:
        for addr in _ADDR_RE.findall(field or ""):
            local = addr.split("@", 1)[0]
            if "+" in local and local.split("+", 1)[0].lower() == agent_local.lower():
                return local.split("+", 1)[1].lower()
    return ""


def route_persona(parsed: dict, tag_map: dict, sent_map: dict, default: str, agent_email: str) -> str:
    """Owner of an inbound message: +tag, then In-Reply-To, then [tag] subject, then default."""
    agent_local = agent_email.split("@", 1)[0]
    tag = _extract_tag([parsed.get("delivered_to", ""), parsed.get("to", "")], agent_local)
    if tag and tag in tag_map:
        return tag_map[tag]
    irt = (parsed.get("in_reply_to") or "").strip()
    if irt and sent_map.get(irt):
        return sent_map[irt]
    m = re.match(r"\s*\[([a-z0-9 ()_-]+)\]", parsed.get("subject", "") or "", re.I)
    if m and m.group(1).strip().lower() in tag_map:
        return tag_map[m.group(1).strip().lower()]
    return default


def persona_received_dir(persona: str):
    return config.state_root(persona) / "emails" / "received"


def _load_shared_cursor() -> dict:
    if SHARED_CURSOR.exists():
        try:
            return json.loads(SHARED_CURSOR.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def _save_shared_cursor(c: dict) -> None:
    atomicio.write_text(SHARED_CURSOR, json.dumps(c, indent=2))


def _route_seal_persist(parsed, uid, tag_map, sent_map, fallback, cfg, processed, commit, all_tokens=None) -> dict:
    """Route one parsed message to its target persona, set the auth witness from the TARGET persona's
    own token (a persona may carry a different COMMAND_TOKEN via its owner overlay), but REDACT the
    full `all_tokens` set across every field so no persona's live token is ever persisted -- even when
    the routed persona's token is empty or the message carries a different persona's token (the old
    leak windows). Persist into that persona's received dir (when commit) and record its id in
    `processed`. Shared by ingest() (shared mailbox) and _poll_account() (a persona's own account)."""
    persona = route_persona(parsed, tag_map, sent_map, fallback, cfg.agent_email)
    try:
        persona_token = config.load(persona).command_token if persona else getattr(cfg, "command_token", "")
    except Exception:
        persona_token = getattr(cfg, "command_token", "")
    _seal_inbound(parsed, persona_token, all_tokens=all_tokens if all_tokens is not None else _all_command_tokens())
    if commit:
        dest = persona_received_dir(persona)
        dest.mkdir(parents=True, exist_ok=True)
        atomicio.write_text(dest / f"{uid}.json", json.dumps({**parsed, "processed": False}, indent=2))
        if parsed["message_id"]:
            processed.add(parsed["message_id"])
    return {"persona": persona, **parsed}


def ingest(commit: bool = True) -> list[dict]:
    """Dispatcher-side single reader. Polls INBOX past the SHARED cursor and routes each new
    message into the target persona's received dir. First run baselines (skips existing mail).
    Returns [{persona, ...parsed}]. Self-addressed mail is dropped (loop guard)."""
    cfg = config.load()                          # global identity/creds (no persona)
    enabled = config.enabled_personas()
    default = config.default_persona() or (enabled[0] if enabled else "")
    if not default:
        return []
    tag_map = _tag_map(enabled)
    sent_map = _sent_index_map()
    all_tokens = _all_command_tokens()   # computed once; redact every persona's token from each record

    conn = _imap_connect(cfg)
    try:
        conn.login(cfg.agent_email, cfg.gmail_app_password)
        conn.select("INBOX", readonly=True)
        uidvalidity = _imap_uidvalidity(conn)

        cur = _load_shared_cursor()
        if not cur or cur.get("uidvalidity") != uidvalidity:
            log.warning("shared UIDVALIDITY changed (%s -> %s): re-baselining, pending owner mail discarded",
                        cur.get("uidvalidity") if cur else None, uidvalidity)
            if commit:                                            # baseline: skip existing mail
                _save_shared_cursor({"uidvalidity": uidvalidity, "last_uid": _imap_max_uid(conn),
                                     "processed_message_ids": []})
            return []

        last_uid = int(cur.get("last_uid", 0))
        processed = set(cur.get("processed_message_ids", []))
        uids = _imap_uids_after(conn, last_uid)

        routed: list[dict] = []
        new_last = last_uid
        for uid in uids:
            parsed = _imap_fetch_parse(conn, uid)
            if parsed is None:
                # A transient FETCH failure for this UID. UIDs are processed in ascending order, so
                # STOP here rather than `continue`: a continue would let a later successful UID push
                # new_last (and the saved cursor) PAST this one, permanently dropping the message on a
                # recoverable network hiccup. Breaking leaves the cursor at the last good UID; the next
                # poll retries from here (the message is re-fetched, never skipped).
                log.warning("IMAP FETCH failed for uid %s; deferring it and the rest of this batch", uid)
                break
            new_last = max(new_last, uid)
            if parsed["message_id"] and parsed["message_id"] in processed:
                continue
            if parseaddr(parsed["from"])[1].lower() == cfg.agent_email.lower():
                if commit and parsed["message_id"]:
                    processed.add(parsed["message_id"])
                continue
            routed.append(_route_seal_persist(parsed, uid, tag_map, sent_map, default, cfg,
                                               processed, commit, all_tokens))
            if commit:
                # Advance the shared cursor AFTER each message is persisted, so a crash later in the
                # loop leaves the cursor covering every already-written message (idempotent re-ingest)
                # instead of re-fetching and re-routing the whole batch on recovery.
                cur.update(uidvalidity=uidvalidity, last_uid=new_last,
                           processed_message_ids=list(processed)[-1000:])
                _save_shared_cursor(cur)

        if commit:
            # Final save also captures new_last advancing past trailing skipped (self/dup) UIDs.
            cur.update(uidvalidity=uidvalidity, last_uid=new_last,
                       processed_message_ids=list(processed)[-1000:])
            _save_shared_cursor(cur)
        return routed
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _persona_cursor_path(persona: str) -> Path:
    return config.state_root(persona) / "imap_cursor.json"


def _account_primary(account_local: str, personas: list[str]) -> str:
    """The persona 'native' to an own-account: the one whose tag matches the account's local part
    (scout on scout@). Its state dir is the STABLE home for the shared account cursor, so adding a
    second persona to the account never moves the cursor and forces a re-baseline. It is also the
    default owner of untagged mail to that account. Falls back to the first listed persona."""
    for name in personas:
        c = config.load(name)
        if (c.plus_tag or name).lower() == account_local.lower():
            return name
    return personas[0]


def _poll_account(personas: list[str], cfg, commit: bool) -> list[dict]:
    """Poll ONE dedicated Gmail account, shared by `personas`, and route each new message to the
    right persona by +tag, then In-Reply-To, then [tag] subject, else the account's native persona
    — exactly as the shared mailbox's ingest() does, so multiple personas can live on one account
    (scout replies to scout+scout@, a second persona to scout+other@). One poll, one per-account
    cursor (homed on the native persona so scout keeps its existing cursor), at-most-once via the
    processed-id set; the first run baselines (skips existing mail so a new account does not import
    prior history)."""
    account_local = cfg.agent_email.split("@", 1)[0]
    primary = _account_primary(account_local, personas)
    tag_map = _tag_map(personas)
    sent_map = _sent_index_map()
    all_tokens = _all_command_tokens()   # redact every persona's token from each stored record
    cursor_path = _persona_cursor_path(primary)
    conn = _imap_connect(cfg)
    try:
        conn.login(cfg.agent_email, cfg.gmail_app_password)
        conn.select("INBOX", readonly=True)
        uidvalidity = _imap_uidvalidity(conn)

        cur = {}
        if cursor_path.exists():
            try:
                cur = json.loads(cursor_path.read_text())
            except json.JSONDecodeError:
                cur = {}

        def write_cur(last_uid, processed):
            if commit:
                atomicio.write_text(cursor_path, json.dumps(
                    {"uidvalidity": uidvalidity, "last_uid": last_uid,
                     "processed_message_ids": list(processed)[-1000:]}, indent=2))

        if not cur or cur.get("uidvalidity") != uidvalidity:        # baseline: skip existing mail
            log.warning("UIDVALIDITY changed (%s -> %s): re-baselining, pending owner mail discarded",
                        cur.get("uidvalidity") if cur else None, uidvalidity)
            write_cur(_imap_max_uid(conn), [])
            return []

        last_uid = int(cur.get("last_uid", 0))
        processed = set(cur.get("processed_message_ids", []))
        uids = _imap_uids_after(conn, last_uid)

        out: list[dict] = []
        new_last = last_uid
        for uid in uids:
            parsed = _imap_fetch_parse(conn, uid)
            if parsed is None:
                # A transient FETCH failure for this UID. UIDs are processed in ascending order, so
                # STOP here rather than `continue`: a continue would let a later successful UID push
                # new_last (and the saved cursor) PAST this one, permanently dropping the message on a
                # recoverable network hiccup. Breaking leaves the cursor at the last good UID; the next
                # poll retries from here (the message is re-fetched, never skipped).
                log.warning("IMAP FETCH failed for uid %s; deferring it and the rest of this batch", uid)
                break
            new_last = max(new_last, uid)
            if parsed["message_id"] and parsed["message_id"] in processed:
                continue
            if parseaddr(parsed["from"])[1].lower() == cfg.agent_email.lower():   # self-loop guard
                if parsed["message_id"]:
                    processed.add(parsed["message_id"])
                continue
            out.append(_route_seal_persist(parsed, uid, tag_map, sent_map, primary, cfg,
                                           processed, commit, all_tokens))
            if commit:
                # Advance the cursor per persisted message (see ingest()): crash-safe re-poll.
                write_cur(new_last, processed)
        write_cur(new_last, processed)
        return out
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _own_accounts() -> dict:
    """{account_email_lower: (cfg, [persona, ...])} for every enabled persona whose agent_email is
    NOT the global account. Personas that share one Gmail account (same agent_email via the same
    .env-<name> overlay) are GROUPED, so the account is polled once and routed among them by +tag.
    Skips personas with no loadable config; the first persona's cfg carries the account-level login."""
    global_email = config.load().agent_email.lower()
    groups: dict = {}
    for name in config.enabled_personas():
        try:
            c = config.load(name)
        except Exception:
            continue
        key = c.agent_email.lower()
        if key == global_email:
            continue                                  # shares the main mailbox -> ingest() covers it
        _cfg, personas = groups.setdefault(key, (c, []))
        personas.append(name)
    return groups


def ingest_own_accounts(commit: bool = True) -> list[dict]:
    """Poll each dedicated Gmail account one or more enabled personas run on (agent_email differs
    from the global account, e.g. scout via ~/.config/cagent/.env-scout). The shared mailbox is
    handled by ingest(); this covers personas whose mail never lands there. Best-effort per account:
    a failed login never blocks the others. Multiple personas on one account are polled ONCE and
    routed by +tag, so scout@ can host scout (scout+scout@) alongside future scout+other@ personas."""
    routed: list[dict] = []
    for _account, (cfg, personas) in _own_accounts().items():
        try:
            routed.extend(_poll_account(personas, cfg, commit))
        except Exception as exc:
            log.warning("ingest_own_accounts: failed to poll account %r (personas %s): %s",
                        _account, personas, exc)
            continue
    return routed
