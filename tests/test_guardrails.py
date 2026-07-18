import dataclasses
import json
import logging
import types
from pathlib import Path

from cagent import commands, config, control, guardrails, supervise

log = logging.getLogger("test")

_FIXTURES = Path(__file__).parent / "fixtures"


def fx(name):
    # Resolve relative to THIS file, not the cwd -- `open("tests/fixtures/...")` only worked when
    # pytest was invoked from the repo root and FileNotFound'd from any other directory / IDE runner.
    with open(_FIXTURES / name) as f:
        return json.load(f)


def cfg_with_token(tok="SECRETTOKEN"):
    # owner pinned to the generic fixture sender so these tests do not depend on the operator's
    # real ~/.config/cagent/.env owner address.
    return dataclasses.replace(config.load(), command_token=tok,
                               owner_email="owner@example.com",
                               staging_recipient="owner+cagent-staging@example.com")


def test_filter_drops_nonowner_and_autoreply():
    cfg = cfg_with_token()
    msgs = [fx("inbound_injection_nonowner_cmd.json"), fx("inbound_autoreply.json"), fx("inbound_hello.json")]
    kept, dropped = guardrails.filter_inbound(msgs, cfg)
    kept_uids = {m["uid"] for m in kept}
    reasons = {m["uid"]: r for m, r in dropped}
    assert kept_uids == {"fix-hello"}                       # only the owner's real reply survives
    assert reasons["inj2"] == "not-owner"                    # attacker dropped
    assert reasons["inj3"] in ("auto-submitted", "bulk-precedence", "no-reply/daemon")


def test_owner_reply_quoting_footer_is_kept():
    # Gmail's default reply quotes the original, disclosure footer included. That reply is how
    # APPROVE/REJECT and !commands arrive, so the echo guard must not eat owner mail (the
    # 2026-07-01 incident); a stranger echoing our footer is still dropped as an echo.
    cfg = cfg_with_token()
    owner_reply = {"uid": "q1", "from": "owner@example.com", "subject": "Re: [cagent] finding",
                   "body_text": "APPROVE abc123\n> sent autonomously by an AI research agent"}
    stranger = {"uid": "q2", "from": "rando@example.com", "subject": "Re: [cagent] finding",
                "body_text": "> autonomously by an AI research agent"}
    kept, dropped = guardrails.filter_inbound([owner_reply, stranger], cfg)
    assert [m["uid"] for m in kept] == ["q1"]
    assert {m["uid"]: r for m, r in dropped} == {"q2": "own-footer-echo"}


def test_injection_body_triggers_no_command():
    cfg = cfg_with_token()
    kept, _ = guardrails.filter_inbound([fx("inbound_injection_body.json")], cfg)
    assert len(kept) == 1                                    # owner message kept
    applied = commands.parse_and_apply(kept, cfg, log)
    assert applied == []                                     # body instructions are NOT commands


def test_nonowner_command_never_reaches_parser():
    cfg = cfg_with_token()
    kept, _ = guardrails.filter_inbound([fx("inbound_injection_nonowner_cmd.json")], cfg)
    assert kept == []                                        # attacker filtered before command parse
    assert commands.parse_and_apply(kept, cfg, log) == []


def test_owner_pause_requires_token(monkeypatch, tmp_path):
    monkeypatch.delenv("CAGENT_PERSONA", raising=False)   # legacy single-persona -> global var/STOP
    monkeypatch.setattr(commands, "STOP", tmp_path / "STOP")
    owner_pause = {"uid": "p1", "from": "owner@example.com", "subject": "!PAUSE", "body_text": ""}
    # no token in subject -> refused
    applied = commands.parse_and_apply([owner_pause], cfg_with_token(), log)
    assert applied and applied[0].get("refused")
    assert not (tmp_path / "STOP").exists()
    # with token -> applied, STOP written
    owner_pause["subject"] = "!PAUSE SECRETTOKEN"
    applied = commands.parse_and_apply([owner_pause], cfg_with_token(), log)
    assert applied and applied[0].get("ok")
    assert (tmp_path / "STOP").exists()


def test_owner_pause_is_per_persona(monkeypatch, tmp_path):
    # Under a persona, !PAUSE writes var/persona/<persona>.STOP (the same flag the dispatcher and
    # the git-control 'pause' directive use), so it pauses ONLY that persona, not everyone.
    monkeypatch.setenv("CAGENT_PERSONA", "scout")
    monkeypatch.setattr(control, "PERSONA_STOP_DIR", tmp_path / "persona")
    owner_pause = {"uid": "p2", "from": "owner@example.com",
                   "subject": "!PAUSE SECRETTOKEN", "body_text": ""}
    applied = commands.parse_and_apply([owner_pause], cfg_with_token(), log)
    assert applied and applied[0].get("ok") and applied[0].get("scope") == "scout"
    assert (tmp_path / "persona" / "scout.STOP").exists()
    assert control.is_paused("scout")


def test_status_request_flag_roundtrip(monkeypatch, tmp_path):
    # !STATUS is owner-only + token-authenticated and leaves a one-shot flag the tick consumes.
    monkeypatch.setattr(commands, "_status_request", lambda: tmp_path / "status_request.flag")
    owner = {"uid": "s1", "from": "owner@example.com",
             "subject": "!STATUS SECRETTOKEN", "body_text": ""}
    applied = commands.parse_and_apply([owner], cfg_with_token(), log)
    assert applied and applied[0].get("ok")
    assert commands.status_requested() is True
    commands.clear_status_request()
    assert commands.status_requested() is False


def test_status_request_needs_token(monkeypatch, tmp_path):
    monkeypatch.setattr(commands, "_status_request", lambda: tmp_path / "status_request.flag")
    owner = {"uid": "s2", "from": "owner@example.com", "subject": "!STATUS", "body_text": ""}
    applied = commands.parse_and_apply([owner], cfg_with_token(), log)
    assert applied and applied[0].get("refused")
    assert commands.status_requested() is False


def test_send_status_goes_through_send_gate(monkeypatch):
    # The !STATUS reply is composed and handed to the normal send gate with kind="status".
    captured = {}

    def fake_send(subject, body_md, kind="finding", **kw):
        captured.update(subject=subject, kind=kind, body=body_md)
        return types.SimpleNamespace(ok=True, dry_run=False)

    monkeypatch.setattr(supervise.gmail, "send", fake_send)
    res = supervise.send_status(config.load(), log)
    assert res["sent"] is True
    assert captured["kind"] == "status"
    assert captured["subject"].startswith("[cagent] status")
    assert "Mode:" in captured["body"]


def test_resume_is_not_an_email_command(monkeypatch, tmp_path):
    monkeypatch.setattr(commands, "STOP", tmp_path / "STOP")
    owner_resume = {"uid": "r1", "from": "owner@example.com", "subject": "!RESUME SECRETTOKEN", "body_text": ""}
    assert commands.parse_and_apply([owner_resume], cfg_with_token(), log) == []  # escalate-only
