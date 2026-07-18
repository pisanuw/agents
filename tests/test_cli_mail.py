"""`cagentctl mail`: the HIS REPLIES section flags an APPROVE/REJECT reply that is already in the
inbox but not yet applied (replies act only on that persona's own tick), so a draft the owner
already answered doesn't read as un-acted-upon."""
import json
import types

import pytest

from cagent import cli, config


@pytest.fixture
def persona_state(tmp_path, monkeypatch):
    state = tmp_path / "state" / "personas" / "alpha"
    (state / "emails" / "pending").mkdir(parents=True, exist_ok=True)
    (state / "emails" / "received").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "state_root", lambda persona=None: state)
    monkeypatch.setattr(config, "load", lambda persona=None: types.SimpleNamespace(
        agent_email="agent@example.com"))
    return state


def _pending(state, token, subject):
    (state / "emails" / "pending" / f"{token}.json").write_text(json.dumps(
        {"token": token, "subject": subject, "body": "letter body", "kind": "finding"}))


def _received(state, uid, subject, processed=False, received_at="2026-06-29T12:00:00+00:00"):
    (state / "emails" / "received" / f"{uid}.json").write_text(json.dumps(
        {"uid": uid, "subject": subject, "from": "owner@example.com",
         "processed": processed, "received_at": received_at}))


def test_mail_flags_unprocessed_reply(persona_state, capsys):
    _pending(persona_state, "abc12345", "his letter")
    _received(persona_state, 7, "REJECT abc12345")
    assert cli.cmd_mail([]) == 0
    out = capsys.readouterr().out
    assert "REPLY IN INBOX: REJECT" in out and "applies on the next tick" in out


def test_mail_marks_processed_reply(persona_state, capsys):
    _pending(persona_state, "abc12345", "his letter")
    _received(persona_state, 7, "APPROVE abc12345", processed=True)
    assert cli.cmd_mail([]) == 0
    out = capsys.readouterr().out
    assert "REPLY IN INBOX: APPROVE" in out and "already processed" in out


def test_mail_no_reply_line_without_matching_inbox(persona_state, capsys):
    _pending(persona_state, "abc12345", "his letter")
    _received(persona_state, 7, "REJECT deadbeef")   # different token
    assert cli.cmd_mail([]) == 0
    out = capsys.readouterr().out
    assert "REPLY IN INBOX" not in out
    assert "abc12345" in out                          # draft still listed
