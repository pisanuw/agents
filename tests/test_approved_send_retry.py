"""Approved-but-unsent drafts are a distinct state from 'awaiting approval'.

Regression for the stuck-draft bug: when an APPROVE landed while the send cap was exhausted, the
approval was silently dropped (the draft stayed `approved:false`, and the triggering APPROVE email is
marked processed so it never re-fires) -- so an owner-approved draft sat in the approval queue
forever. approve() now PERSISTS the approval on a refused send, and retry_approved() releases it once
capacity frees up. The two states must be kept apart everywhere the owner sees a queue.
"""
import json
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

from cagent import gmail, supervise

log = logging.getLogger("t")


def _ok(**kw):
    return SimpleNamespace(ok=True, dry_run=False, to="owner", message_id="x")


def _cfg(persona="echozz"):
    return SimpleNamespace(persona=persona, plus_tag=persona, agent_email="agent@example.com",
                           from_name="cagent", MODE="SUPERVISED")


class _Gate:
    """Toggleable send: refuse (over cap) until opened, then deliver. Records delivered subjects."""
    def __init__(self, open_=False):
        self.open = open_
        self.sent = []

    def send(self, **kw):
        if not self.open:
            raise gmail.SendRefused("daily send cap reached (3/3)")
        self.sent.append(kw)
        return _ok()


def test_approve_persists_approval_when_capped(monkeypatch, tmp_path):
    monkeypatch.setattr(supervise, "_pending", lambda: tmp_path / "pending")
    monkeypatch.setattr(gmail, "send", _Gate(open_=False).send)
    tok = supervise.stage_draft("G4 digest", "the body", "digest")

    res = supervise.approve(tok, _cfg(), log)
    # The owner's YES is recorded even though nothing could be sent yet.
    assert res["approved"] is True and res["sent"] is False and res["queued"] is True
    d = supervise.get(tok)
    assert d is not None, "draft must remain queued, not dropped"
    assert d["approved"] is True and d["body"] == "the body"
    assert d.get("send_deferred_reason")


def test_approve_edit_override_persisted_on_defer(monkeypatch, tmp_path):
    monkeypatch.setattr(supervise, "_pending", lambda: tmp_path / "pending")
    monkeypatch.setattr(gmail, "send", _Gate(open_=False).send)
    tok = supervise.stage_draft("S", "original body", "finding")

    supervise.approve(tok, _cfg(), log, override_body="owner rewrote this")
    d = supervise.get(tok)
    # The retry must send the owner's edited text, not the model's original.
    assert d["approved"] is True and d["body"] == "owner rewrote this" and d["edited"] is True


def test_retry_approved_releases_when_cap_opens(monkeypatch, tmp_path):
    monkeypatch.setattr(supervise, "_pending", lambda: tmp_path / "pending")
    gate = _Gate(open_=False)
    monkeypatch.setattr(gmail, "send", gate.send)
    approved_tok = supervise.stage_draft("Approved one", "yes body", "digest")
    supervise.approve(approved_tok, _cfg(), log)          # capped -> persisted approved
    unapproved_tok = supervise.stage_draft("Not yet", "b", "finding")   # owner hasn't acted

    assert supervise.retry_approved(_cfg(), log) == []    # still capped: nothing goes out
    assert supervise.get(approved_tok) is not None

    gate.open = True                                      # capacity frees up
    released = supervise.retry_approved(_cfg(), log)
    assert [r["token"] for r in released] == [approved_tok]
    assert supervise.get(approved_tok) is None            # sent -> removed from the queue
    assert supervise.get(unapproved_tok) is not None      # un-approved draft is left untouched
    assert gate.sent[-1]["subject"] == "Approved one" and gate.sent[-1]["body_md"] == "yes body"


def test_retry_undelivered_skips_approved(monkeypatch, tmp_path):
    monkeypatch.setattr(supervise, "_pending", lambda: tmp_path / "pending")
    monkeypatch.setattr(gmail, "send", _Gate(open_=False).send)
    tok = supervise.stage_draft("S", "b", "finding")
    supervise.approve(tok, _cfg(), log)                   # approved but unsent, request_sent=False
    # retry_undelivered must NOT re-send an approval REQUEST for an already-approved draft.
    assert supervise.retry_undelivered(_cfg(), log) == []


def test_remind_and_expire_skips_approved(monkeypatch, tmp_path):
    pend = tmp_path / "pending"
    monkeypatch.setattr(supervise, "_pending", lambda: pend)
    monkeypatch.setattr(gmail, "send", lambda **kw: _ok())
    created = "2026-07-01T00:00:00+00:00"
    pend.mkdir()
    (pend / "a.json").write_text(json.dumps(
        {"token": "a", "subject": "S", "body": "B", "created": created,
         "approved": True, "approved_at": created, "request_sent": True, "reminders": []}))
    monkeypatch.setattr(supervise.clock, "now",
                        lambda: datetime.fromisoformat(created) + timedelta(days=40))
    r = supervise.remind_and_expire_approvals(_cfg(), log)
    assert r["reminded"] == [] and r["expired"] == []     # approved: owner acted, never nag/discard
    assert supervise.get("a") is not None


def test_digest_separates_the_two_queues(monkeypatch, tmp_path):
    monkeypatch.setattr(supervise, "_pending", lambda: tmp_path / "pending")
    monkeypatch.setattr(supervise, "_journal", lambda: tmp_path / "journal.jsonl")
    monkeypatch.setattr(supervise, "_goals", lambda: tmp_path / "goals.json")
    monkeypatch.setattr(gmail, "send", _Gate(open_=False).send)
    await_tok = supervise.stage_draft("Please decide", "b1", "finding")
    appr_tok = supervise.stage_draft("Already blessed", "b2", "digest")
    supervise.approve(appr_tok, _cfg(), log)              # capped -> approved, waiting to send

    _, body = supervise.build_digest()
    assert "Drafts awaiting your approval (1)" in body and await_tok in body
    assert "Approved, waiting to send (1)" in body and appr_tok in body
    # the approved token must not appear under the awaiting header
    awaiting_section = body.split("Approved, waiting to send")[0]
    assert appr_tok not in awaiting_section


def test_backlog_excludes_approved(monkeypatch, tmp_path):
    monkeypatch.setattr(supervise, "_pending", lambda: tmp_path / "pending")
    gate = _Gate(open_=False)
    monkeypatch.setattr(gmail, "send", gate.send)
    tok = supervise.stage_draft("Only one", "b", "finding")
    supervise.approve(tok, _cfg(), log)                   # now approved
    # nothing is left awaiting approval -> the backlog email is skipped, not sent for an approved draft
    assert supervise.send_approval_backlog(_cfg(), log).get("skipped")


def test_persona_stats_splits_counts(monkeypatch, tmp_path):
    # persona_stats reads <state_dir>/emails/pending, so point _pending at the same place.
    monkeypatch.setattr(supervise, "_pending", lambda: tmp_path / "emails" / "pending")
    monkeypatch.setattr(gmail, "send", _Gate(open_=False).send)
    supervise.stage_draft("awaiting", "b", "finding")
    appr = supervise.stage_draft("approved", "b", "digest")
    supervise.approve(appr, _cfg(), log)
    s = supervise.persona_stats(tmp_path)
    assert s["pending"] == 1 and s["approved_unsent"] == 1
