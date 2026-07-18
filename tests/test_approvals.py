"""Approval backlog: consolidated per-persona resend + the 3/7/14-day reminder-then-discard lifecycle."""
import json
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

from cagent import gmail, supervise

log = logging.getLogger("t")


def _ok(**kw):
    return SimpleNamespace(ok=True, dry_run=True, to="owner", message_id="x")


def _cfg(persona="data"):
    return SimpleNamespace(persona=persona, plus_tag=persona, agent_email="agent@example.com", from_name="cagent")


def test_approval_links():
    a, r = supervise._approval_links(_cfg(), "1a2b3c4d")
    assert a == "mailto:agent+data@example.com?subject=APPROVE%201a2b3c4d"
    assert r == "mailto:agent+data@example.com?subject=REJECT%201a2b3c4d"


def test_send_approval_backlog_consolidates(monkeypatch, tmp_path):
    monkeypatch.setattr(supervise, "_pending", lambda: tmp_path / "pending")
    t1 = supervise.stage_draft("First finding", "body one", "finding")
    t2 = supervise.stage_draft("Second finding", "body two", "finding")
    sent = {}
    monkeypatch.setattr(gmail, "send", lambda **kw: sent.update(kw) or _ok())
    res = supervise.send_approval_backlog(_cfg(), log)
    assert res["count"] == 2                              # ONE email covering both drafts
    h = sent["html_body"]
    assert f"subject=APPROVE%20{t1}" in h and f"subject=REJECT%20{t1}" in h
    assert f"subject=APPROVE%20{t2}" in h
    assert "2 approval" in sent["subject"]


def test_send_approval_backlog_empty_and_held(monkeypatch, tmp_path):
    monkeypatch.setattr(supervise, "_pending", lambda: tmp_path / "pending")
    assert supervise.send_approval_backlog(_cfg(), log).get("skipped")   # nothing pending
    tok = supervise.stage_draft("Kept", "b", "finding")
    supervise.hold(tok)                                                  # !HOLD -> excluded
    assert supervise.send_approval_backlog(_cfg(), log).get("skipped")


def _write_draft(pend, token, created, reminders):
    pend.mkdir(exist_ok=True)
    (pend / f"{token}.json").write_text(json.dumps(
        {"token": token, "subject": "S", "body": "B", "kind": "finding",
         "created": created, "approved": False, "request_sent": True, "reminders": reminders}))


def test_remind_and_expire_cadence(monkeypatch, tmp_path):
    pend = tmp_path / "pending"
    monkeypatch.setattr(supervise, "_pending", lambda: pend)
    monkeypatch.setattr(gmail, "send", lambda **kw: _ok())
    monkeypatch.setattr(supervise.clock, "iso", lambda: "REM_TS")
    created = "2026-07-01T00:00:00+00:00"
    base = datetime.fromisoformat(created)

    def at(days):
        monkeypatch.setattr(supervise.clock, "now", lambda: base + timedelta(days=days))

    _write_draft(pend, "tok", created, [])                 # day 4 -> reminder #1
    at(4)
    assert supervise.remind_and_expire_approvals(_cfg(), log)["reminded"] == ["tok"]
    assert supervise.get("tok")["reminders"] == ["REM_TS"]

    _write_draft(pend, "tok", created, ["a"])              # day 5, 1 reminder -> next is 7d, nothing yet
    at(5)
    r = supervise.remind_and_expire_approvals(_cfg(), log)
    assert r["reminded"] == [] and r["expired"] == []

    _write_draft(pend, "tok", created, ["a"])              # day 8 -> reminder #2
    at(8)
    assert supervise.remind_and_expire_approvals(_cfg(), log)["reminded"] == ["tok"]

    # day 15: 3 reminders with last one just sent at day 14 (1 day ago < EXPIRE_GRACE_DAYS=3) -> grace
    last_reminder_ts = (base + timedelta(days=14)).isoformat()
    _write_draft(pend, "tok", created, ["a", "b", last_reminder_ts])
    at(15)
    r = supervise.remind_and_expire_approvals(_cfg(), log)
    assert r["expired"] == [] and r["reminded"] == []   # still in grace window

    # day 17: last reminder 3 days ago (= EXPIRE_GRACE_DAYS) -> discard
    at(17)
    assert supervise.remind_and_expire_approvals(_cfg(), log)["expired"] == ["tok"]
    assert supervise.get("tok") is None


def test_remind_leaves_held_alone(monkeypatch, tmp_path):
    pend = tmp_path / "pending"
    monkeypatch.setattr(supervise, "_pending", lambda: pend)
    monkeypatch.setattr(gmail, "send", lambda **kw: _ok())
    created = "2026-07-01T00:00:00+00:00"
    pend.mkdir()
    (pend / "h.json").write_text(json.dumps(
        {"token": "h", "subject": "S", "body": "B", "created": created, "held": True, "reminders": []}))
    monkeypatch.setattr(supervise.clock, "now", lambda: datetime.fromisoformat(created) + timedelta(days=20))
    r = supervise.remind_and_expire_approvals(_cfg(), log)
    assert r["reminded"] == [] and r["expired"] == []      # held: never reminded, never discarded
    assert supervise.get("h") is not None
