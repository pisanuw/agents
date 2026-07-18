"""The drain half of the send-or-queue-for-retry idiom.

A few sends are QUEUED as files when the send gate refuses them (global cap exhausted / QUIET /
stop) and retried on a later tick: command acks (state/pending_acks/) and approved-but-uncapped
drafts (the pending/ dir). Every retry pass has the same shape — walk the queue, attempt each
item's send, drop a corrupt entry, unlink on success, and LEAVE the item queued on
gmail.SendRefused so the gate's cap bounds the retry (a still-refused item just waits for the next
tick, never spams). That walk lived open-coded in retry_acks and retry_approved; it lives here once.

WHAT each item's send is differs per caller, so it stays a callback. Only the drain skeleton is
shared: the single-shot `except gmail.SendRefused` catches at the ORIGINAL send sites (approve,
request_approval, the ~14 inline callers) each do something different on refusal and are left alone.
"""
from __future__ import annotations

import json

from cagent import gmail


def drain(queue_dir, attempt, log):
    """Walk *.json items under queue_dir, oldest name first. For each, load it (dropping a
    corrupt/unreadable file) and call attempt(item):
      - returns a result dict -> the item was delivered; unlink the file and collect the result;
      - returns None            -> attempt declined this item (not its turn); leave it queued as-is;
      - raises gmail.SendRefused -> the gate refused; leave it queued for a later tick (cap-bounded).
    Returns the list of results delivered this pass."""
    out = []
    if not queue_dir.exists():
        return out
    for p in sorted(queue_dir.glob("*.json")):
        try:
            item = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            p.unlink(missing_ok=True)                      # corrupt/unreadable -> drop, don't wedge
            continue
        try:
            res = attempt(item)
        except gmail.SendRefused:
            continue                                       # still over cap/quiet/stop -> leave queued
        if res is None:
            continue                                       # not this item's turn -> leave as-is
        p.unlink(missing_ok=True)                          # delivered -> remove from the queue
        out.append(res)
    return out
