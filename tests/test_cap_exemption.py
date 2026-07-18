"""Supervision/operational mail is exempt from the content send cap.

The cap exists to throttle the agent's UNSOLICITED, self-generated output (findings, digests). It must
NOT throttle the supervision channel (approval requests/reminders/backlog), owner-solicited command
replies (status/ack/help/ping/goals), or safety alerts -- doing so once left owner-APPROVED drafts
unsendable behind a wall of approval-REQUEST emails. Exempt kinds neither consume nor are blocked by
the cap; the owner's hard halt (!STOP-SENDING) still silences everything.
"""
import json
from datetime import datetime, timezone

import pytest

from _helpers import send_cfg
from cagent import gmail


def test_count_ledger_excludes_exempt_kinds(monkeypatch, tmp_path):
    now = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(gmail.clock, "now", lambda: now)
    led = tmp_path / "send_ledger.jsonl"
    rows = [
        {"ts": now.isoformat(), "kind": "finding", "dry_run": False},   # content: counts
        {"ts": now.isoformat(), "kind": "digest", "dry_run": False},    # content: counts
        {"ts": now.isoformat(), "kind": "approval", "dry_run": False},  # exempt
        {"ts": now.isoformat(), "kind": "approval", "dry_run": False},  # exempt
        {"ts": now.isoformat(), "kind": "status", "dry_run": False},    # exempt
        {"ts": now.isoformat(), "kind": "ack", "dry_run": False},       # exempt
        {"ts": now.isoformat(), "kind": "alert", "dry_run": False},     # exempt
        {"ts": now.isoformat(), "kind": "finding", "dry_run": True},    # dry-run: ignored
    ]
    led.write_text("\n".join(json.dumps(r) for r in rows))
    day, week = gmail._count_ledger(led)
    assert (day, week) == (2, 2)   # only the two real content sends


# --- send-gate behaviour (SUPERVISED path, SMTP + ledger stubbed) ------------------------------- #

class _FakeSMTP:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, *a):
        pass

    def send_message(self, *a):
        pass


def _sup_cfg():
    return send_cfg("SUPERVISED")


def _stub_gate(monkeypatch, tmp_path, cfg, content_full=True):
    """Wire gmail.send's gate for a real (non-DRY_RUN) send with no I/O. content_full=True simulates
    an exhausted CONTENT cap via ledger_counts; global cap is left open."""
    monkeypatch.setattr(gmail.config, "load", lambda *a, **k: cfg)   # tolerate args (matches the sibling stub)
    monkeypatch.setattr(gmail, "quiet_active", lambda: False)
    monkeypatch.setattr(gmail, "ledger_counts", lambda: ((99, 99) if content_full else (0, 0)))
    monkeypatch.setattr(gmail, "global_ledger_counts", lambda: (0, 0))
    monkeypatch.setattr(gmail, "_stop_sending", lambda: tmp_path / "nostop")
    monkeypatch.setattr(gmail, "_send_lock", lambda: tmp_path / "lock")
    monkeypatch.setattr(gmail, "_record", lambda rec: None)
    monkeypatch.setattr(gmail, "_record_global", lambda rec: None)
    monkeypatch.setattr(gmail, "_persist", lambda msg, rec, to_outbox: None)
    monkeypatch.setattr(gmail, "_append_sent_index", lambda mid, persona: None)
    monkeypatch.setattr(gmail.smtplib, "SMTP_SSL", lambda *a, **k: _FakeSMTP())


def test_content_send_refused_when_cap_full(monkeypatch, tmp_path):
    _stub_gate(monkeypatch, tmp_path, _sup_cfg(), content_full=True)
    with pytest.raises(gmail.SendRefused, match="cap reached"):
        gmail.send(subject="A finding", body_md="body", kind="finding")


def test_approval_send_bypasses_full_content_cap(monkeypatch, tmp_path):
    _stub_gate(monkeypatch, tmp_path, _sup_cfg(), content_full=True)
    r = gmail.send(subject="DRAFT REQUEST abc", body_md="please approve", kind="approval")
    assert r.ok and not r.dry_run          # went out despite the content cap being full


def test_all_operational_kinds_bypass_cap(monkeypatch, tmp_path):
    _stub_gate(monkeypatch, tmp_path, _sup_cfg(), content_full=True)
    for k in sorted(gmail.CAP_EXEMPT_KINDS):
        assert gmail.send(subject="x", body_md="y", kind=k).ok, f"{k} should bypass the cap"


def test_stop_sending_still_halts_exempt_kinds(monkeypatch, tmp_path):
    # The anti-flood cap is waived for exempt kinds, but the owner's hard halt is not.
    _stub_gate(monkeypatch, tmp_path, _sup_cfg(), content_full=False)
    stop = tmp_path / "stopflag"
    stop.write_text("x")
    monkeypatch.setattr(gmail, "_stop_sending", lambda: stop)
    with pytest.raises(gmail.SendRefused, match="STOP-SENDING"):
        gmail.send(subject="x", body_md="y", kind="approval")
