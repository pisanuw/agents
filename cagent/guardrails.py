"""Inbound filtering and egress checks. Inbound bodies are already wrapped as UNTRUSTED in
the tick context; this module decides what inbound is even worth showing cognition and
prevents reply-loops. The owner allowlist + RFC 3834 suppression run in deterministic code
BEFORE the model sees anything.
"""
from __future__ import annotations

from email.utils import parseaddr

DISCLOSURE_MARKER = "autonomously by an AI research agent"
NOREPLY_HINTS = ("no-reply", "noreply", "do-not-reply", "donotreply", "mailer-daemon", "postmaster")
BULK_PRECEDENCE = ("bulk", "list", "auto_reply", "junk")


def addr_of(s: str) -> str:
    return parseaddr(s or "")[1].lower()


def is_owner(addr: str, cfg) -> bool:
    return addr_of(addr) in (cfg.owner_email.lower(), cfg.staging_recipient.lower())


def loop_reason(msg: dict, cfg) -> str | None:
    """Why this message must NOT be replied to (RFC 3834 + self/echo guards), else None."""
    frm = addr_of(msg.get("from", ""))
    if not frm:
        return "no-from"
    if frm == cfg.agent_email.lower():
        return "self"
    if any(h in frm for h in NOREPLY_HINTS):
        return "no-reply/daemon"
    if (msg.get("auto_submitted", "") or "").strip().lower() not in ("", "no"):
        return "auto-submitted"
    if (msg.get("precedence", "") or "").strip().lower() in BULK_PRECEDENCE:
        return "bulk-precedence"
    if DISCLOSURE_MARKER in (msg.get("body_text", "") or ""):
        return "own-footer-echo"
    return None


def filter_inbound(messages: list[dict], cfg) -> tuple[list[dict], list[tuple[dict, str]]]:
    """(kept owner messages worth acting on, [(dropped, reason)])."""
    kept, dropped = [], []
    for m in messages:
        reason = loop_reason(m, cfg)
        # An OWNER reply quoting our disclosure footer (Gmail's default reply includes the quoted
        # original) is real mail -- APPROVE/REJECT and !commands arrive exactly this way. The echo
        # guard exists for the agent's own mail bouncing back, which the "self" check still catches;
        # owner mail keeps every other RFC 3834 guard (a vacation auto-reply stays dropped).
        if reason == "own-footer-echo" and is_owner(m.get("from", ""), cfg):
            reason = None
        if reason:
            dropped.append((m, reason))
            continue
        if not is_owner(m.get("from", ""), cfg):
            dropped.append((m, "not-owner"))
            continue
        kept.append(m)
    return kept, dropped
