import dataclasses
import json
import logging
from types import SimpleNamespace

import pytest

from _helpers import send_cfg
from cagent import config, gmail, supervise

log = logging.getLogger("t")


def _raise_refused(**kw):
    raise gmail.SendRefused("daily send cap reached")


def _ok_send(calls):
    def send(**kw):
        calls.append(kw.get("subject"))
        return SimpleNamespace(ok=True, dry_run=False, to="owner")
    return send


def _cfg(plus_tag="alpha", agent_email="agent@example.com"):
    return SimpleNamespace(plus_tag=plus_tag, agent_email=agent_email, from_name="cagent")


def test_stage_list_reject(monkeypatch, tmp_path):
    monkeypatch.setattr(supervise, "_pending", lambda: tmp_path / "pending")
    tok = supervise.stage_draft("Hello Master", "A finding worth your eyes.", "finding")
    pend = supervise.list_pending()
    assert len(pend) == 1 and pend[0]["token"] == tok
    assert supervise.get(tok)["subject"] == "Hello Master"
    assert supervise.reject(tok)["rejected"]
    assert supervise.list_pending() == []


def test_reply_address_persona_and_legacy():
    assert gmail.reply_address(_cfg()) == "agent+alpha@example.com"
    assert gmail.reply_address(_cfg(plus_tag="")) == "agent@example.com"  # legacy: bare address


def _patch_draft(tmp_path, token, **fields):
    p = tmp_path / "pending" / f"{token}.json"
    d = json.loads(p.read_text())
    d.update(fields)
    p.write_text(json.dumps(d))


def test_backlog_depth_counts_live_excludes_held(monkeypatch, tmp_path):
    monkeypatch.setattr(supervise, "_pending", lambda: tmp_path / "pending")
    assert supervise.backlog_depth() == 0                       # empty (missing dir) is 0, not an error
    supervise.stage_draft("S1", "awaiting the owner", "finding")
    a = supervise.stage_draft("S2", "approved but unsent", "finding")
    _patch_draft(tmp_path, a, approved=True)                    # APPROVED_UNSENT still counts as backlog
    h = supervise.stage_draft("S3", "parked by the owner", "finding")
    _patch_draft(tmp_path, h, held=True)                        # !HOLD is the owner's deliberate parking
    assert supervise.backlog_depth() == 2                       # two live drafts; the !HOLD one is excluded


def test_request_approval_emits_mailto_links(monkeypatch, tmp_path):
    monkeypatch.setattr(supervise, "_pending", lambda: tmp_path / "pending")
    sent = {}
    monkeypatch.setattr(gmail, "send", lambda **kw: sent.update(kw))
    tok = supervise.stage_draft("Greetings", "A finding.", "finding")
    supervise.request_approval(tok, "Greetings", "A finding.", _cfg(), log)
    body = sent["body_md"]
    # one-tap links carry the verb + token + the persona's tagged routing address
    assert f"mailto:agent+alpha@example.com?subject=APPROVE%20{tok}" in body
    assert f"mailto:agent+alpha@example.com?subject=REJECT%20{tok}" in body
    # and the plain-Reply trap is still spelled out
    assert "not recognized" in body


def test_mode_override_downgrades_live(monkeypatch, tmp_path):
    # Redirect state_root() so we never touch the real state/mode_override in the live tree.
    monkeypatch.setattr(config, "state_root", lambda *a, **k: tmp_path)
    ov = tmp_path / "mode_override"
    ov.write_text("SUPERVISED")
    monkeypatch.setenv("AGENT_MODE", "LIVE")
    assert config.load().MODE == "SUPERVISED"


def test_tripwire_downgrade_notice_retries_when_refused(monkeypatch, tmp_path):
    # The LIVE->SUPERVISED downgrade is durable, but its owner notice must not be lost: a refused
    # send arms a pending marker that a later tick retries until delivered.
    monkeypatch.setattr(config, "state_root", lambda *a, **k: tmp_path)
    monkeypatch.setattr(supervise, "_mode_override", lambda: tmp_path / "mode_override")
    monkeypatch.setattr(supervise, "_today_journal",
                        lambda: [{"ok": False} for _ in range(5)])   # 5/5 failed -> trips
    monkeypatch.setattr(gmail, "send", _raise_refused)
    res = supervise.check_tripwire(SimpleNamespace(MODE="LIVE"), log)
    assert res["tripped"] is True
    assert (tmp_path / "mode_override").read_text() == "SUPERVISED"      # downgrade is durable
    assert (tmp_path / "mode_downgrade_notice_pending").exists()         # notice owed
    # next tick: now SUPERVISED (not LIVE), send works -> retry delivers and clears the marker
    calls = []
    monkeypatch.setattr(gmail, "send", _ok_send(calls))
    supervise.check_tripwire(SimpleNamespace(MODE="SUPERVISED"), log)
    assert any("auto-downgraded" in (s or "") for s in calls)
    assert not (tmp_path / "mode_downgrade_notice_pending").exists()


def test_digest_and_scorecard_build(monkeypatch, tmp_path):
    # Redirect state paths so scorecard() doesn't overwrite the live state/soft_launch_report.md.
    monkeypatch.setattr(supervise, "_pending", lambda: tmp_path / "pending")
    monkeypatch.setattr(supervise, "_journal", lambda: tmp_path / "journal.jsonl")
    monkeypatch.setattr(supervise, "_scorecard_path", lambda: tmp_path / "soft_launch_report.md")
    subj, body = supervise.build_digest()
    assert "daily digest" in subj
    assert "quests" in body.lower()
    md = supervise.scorecard()
    assert "scorecard" in md.lower() and "Graduation criteria" in md


# --------------------------- approval-request delivery tracking + retry --------------------------- #

def test_stage_draft_marks_request_unsent(monkeypatch, tmp_path):
    monkeypatch.setattr(supervise, "_pending", lambda: tmp_path / "pending")
    tok = supervise.stage_draft("S", "B", "finding")
    assert supervise.get(tok)["request_sent"] is False


def test_request_approval_success_marks_sent(monkeypatch, tmp_path):
    monkeypatch.setattr(supervise, "_pending", lambda: tmp_path / "pending")
    monkeypatch.setattr(gmail, "send", lambda **kw: SimpleNamespace(ok=True, dry_run=False))
    tok = supervise.stage_draft("S", "B", "finding")
    assert supervise.request_approval(tok, "S", "B", _cfg(), log) is True
    assert supervise.get(tok)["request_sent"] is True


def test_request_approval_refused_marks_unsent(monkeypatch, tmp_path):
    monkeypatch.setattr(supervise, "_pending", lambda: tmp_path / "pending")
    monkeypatch.setattr(gmail, "send", _raise_refused)
    tok = supervise.stage_draft("S", "B", "finding")
    assert supervise.request_approval(tok, "S", "B", _cfg(), log) is False
    assert supervise.get(tok)["request_sent"] is False


def test_retry_resends_only_undelivered(monkeypatch, tmp_path):
    monkeypatch.setattr(supervise, "_pending", lambda: tmp_path / "pending")
    monkeypatch.setattr(gmail, "send", _raise_refused)
    bad = supervise.stage_draft("bad subj", "B", "finding")
    supervise.request_approval(bad, "bad subj", "B", _cfg(), log)     # refused -> request_sent False
    good = supervise.stage_draft("good subj", "B", "finding")
    supervise._mark_request_sent(good, True)                          # pretend already delivered

    calls = []
    monkeypatch.setattr(gmail, "send", _ok_send(calls))
    out = supervise.retry_undelivered(_cfg(), log)
    assert [o["token"] for o in out] == [bad]                        # only the undelivered one re-sent
    assert supervise.get(bad)["request_sent"] is True               # now delivered
    assert len(calls) == 1                                          # good draft NOT re-sent again


def test_retry_backfills_legacy_from_ledger(monkeypatch, tmp_path):
    monkeypatch.delenv("CAGENT_PERSONA", raising=False)
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(supervise, "_pending", lambda: tmp_path / "pending")
    (tmp_path / "pending").mkdir(parents=True)
    # two LEGACY drafts (no request_sent key)
    (tmp_path / "pending" / "aaaa1111.json").write_text(json.dumps(
        {"token": "aaaa1111", "subject": "already notified", "body": "x", "kind": "finding"}))
    (tmp_path / "pending" / "bbbb2222.json").write_text(json.dumps(
        {"token": "bbbb2222", "subject": "never notified", "body": "x", "kind": "finding"}))
    # the shared ledger shows aaaa1111's approval request WAS delivered
    shared = tmp_path / "state" / "shared"
    shared.mkdir(parents=True)
    (shared / "send_ledger.jsonl").write_text(json.dumps(
        {"kind": "approval", "dry_run": False, "subject": "[cagent] DRAFT REQUEST aaaa1111: x"}) + "\n")

    calls = []
    monkeypatch.setattr(gmail, "send", _ok_send(calls))
    out = supervise.retry_undelivered(_cfg(), log)
    assert [o["token"] for o in out] == ["bbbb2222"]                # only the never-notified one re-sent
    assert supervise.get("aaaa1111")["request_sent"] is True        # backfilled True from the ledger
    assert supervise.get("bbbb2222")["request_sent"] is True        # backfilled False, then delivered
    assert len(calls) == 1


def test_request_approval_html_has_clickable_anchors(monkeypatch, tmp_path):
    monkeypatch.setattr(supervise, "_pending", lambda: tmp_path / "pending")
    sent = {}
    monkeypatch.setattr(gmail, "send", lambda **kw: sent.update(kw))
    tok = supervise.stage_draft("Greetings", "A finding.", "finding")
    supervise.request_approval(tok, "Greetings", "A finding.", _cfg(), log)
    h = sent["html_body"]
    # real anchors carry the FULL mailto incl. the ?subject= query (clickable in Gmail)
    assert f'href="mailto:agent+alpha@example.com?subject=APPROVE%20{tok}"' in h
    assert f'href="mailto:agent+alpha@example.com?subject=REJECT%20{tok}"' in h
    assert "Greetings" in h                                    # proposed subject rendered
    assert "A finding." in h                                   # proposed body rendered
    assert f"subject=APPROVE%20{tok}" in sent["body_md"]       # plain-text fallback unchanged


def test_gmail_send_attaches_html_alternative(monkeypatch):
    monkeypatch.setattr(gmail, "_signature", lambda cfg: "written autonomously by an AI research agent (x@y)")
    captured = {}
    monkeypatch.setattr(gmail, "_persist", lambda msg, rec, to_outbox: captured.update(msg=msg))
    monkeypatch.setattr(gmail, "_record", lambda rec: None)
    monkeypatch.setattr(gmail, "_record_global", lambda rec: None)
    r = gmail.send(subject="Hi", body_md="plain content here", kind="approval",
                   html_body='<a href="mailto:x?subject=APPROVE%201a">APPROVE</a>')
    assert r.dry_run                                           # config.toml default mode is DRY_RUN
    msg = captured["msg"]
    assert msg.is_multipart()
    html_part = next(p.get_content() for p in msg.walk() if p.get_content_type() == "text/html")
    plain = next(p.get_content() for p in msg.walk() if p.get_content_type() == "text/plain")
    assert 'href="mailto:x?subject=APPROVE%201a"' in html_part
    assert gmail.DISCLOSURE_MARKER in html_part                # disclosure mirrored into the HTML twin
    assert "plain content here" in plain


def _capture_send(monkeypatch, cfg, **kw):
    """Run gmail.send with a controlled cfg, capturing the built EmailMessage (DRY_RUN, no I/O)."""
    monkeypatch.setattr(gmail.config, "load", lambda: cfg)
    monkeypatch.setattr(gmail, "quiet_active", lambda: False)
    cap = {}
    monkeypatch.setattr(gmail, "_persist", lambda msg, rec, to_outbox: cap.update(msg=msg))
    monkeypatch.setattr(gmail, "_record", lambda rec: None)
    monkeypatch.setattr(gmail, "_record_global", lambda rec: None)
    monkeypatch.setattr(gmail, "_append_sent_index", lambda mid, persona: None)
    gmail.send(**kw)
    return cap["msg"]


def _parts(msg):
    plain = next((p.get_content() for p in msg.walk() if p.get_content_type() == "text/plain"), "")
    html = next((p.get_content() for p in msg.walk() if p.get_content_type() == "text/html"), "")
    return plain, html


def test_command_footer_appended_to_every_send(monkeypatch):
    # Every outbound message carries the "steer me by email" command menu as one-tap mailto links.
    cfg = dataclasses.replace(config.load(), command_footer=True, command_token="")
    msg = _capture_send(monkeypatch, cfg, subject="Hi", body_md="a short update")
    plain, html = _parts(msg)
    assert msg.is_multipart()                                  # HTML twin attached so the links render
    assert "Steer me by email" in plain and "Steer me by email" in html
    assert "!PAUSE <TOKEN>" in plain                           # every command listed, with placeholder
    assert "!STOP-SENDING <TOKEN>" in plain
    # one-tap link: subject pre-filled and percent-encoded so Gmail keeps the ?subject query
    assert 'subject=%21PAUSE%20%3CTOKEN%3E"' in html
    assert gmail.DISCLOSURE_MARKER in plain                    # disclosure still present, above the menu


def test_command_footer_uses_placeholder_never_live_token(monkeypatch):
    # The COMMAND_TOKEN is a secret and sent-mail is committed to git, so the footer must carry the
    # <TOKEN> placeholder, never the configured token value.
    cfg = dataclasses.replace(config.load(), command_footer=True, command_token="s3cr3t-TOKEN-xyz")
    msg = _capture_send(monkeypatch, cfg, subject="Hi", body_md="a short update")
    plain, html = _parts(msg)
    assert "<TOKEN>" in plain
    assert "s3cr3t-TOKEN-xyz" not in plain and "s3cr3t-TOKEN-xyz" not in html


def test_command_footer_can_be_disabled(monkeypatch):
    # [commands].footer = false -> no menu, and with no caller HTML the message stays single text/plain.
    cfg = dataclasses.replace(config.load(), command_footer=False)
    msg = _capture_send(monkeypatch, cfg, subject="Hi", body_md="a short update")
    plain, html = _parts(msg)
    assert not msg.is_multipart()
    assert "Steer me by email" not in plain
    assert html == ""


def test_refused_send_does_not_consume_global_cap(monkeypatch, tmp_path):
    # H5 regression: a non-DRY_RUN send refused by STOP-SENDING must NOT write the global anti-flood
    # ledger (the old order recorded it before the STOP check + SMTP, wrongly throttling others).
    cfg = send_cfg("SUPERVISED", plus_tag="test")
    monkeypatch.setattr(gmail.config, "load", lambda: cfg)
    monkeypatch.setattr(gmail, "quiet_active", lambda: False)
    monkeypatch.setattr(gmail, "ledger_counts", lambda: (0, 0))
    monkeypatch.setattr(gmail, "global_ledger_counts", lambda: (0, 0))
    stop = tmp_path / "stop_sending.flag"
    stop.write_text("x")
    monkeypatch.setattr(gmail, "_stop_sending", lambda: stop)
    recorded = []
    monkeypatch.setattr(gmail, "_record_global", lambda rec: recorded.append(rec))
    monkeypatch.setattr(gmail, "_append_sent_index", lambda mid, persona: recorded.append("idx"))
    with pytest.raises(gmail.SendRefused):
        gmail.send(subject="hi", body_md="some real body content")
    assert recorded == []                                      # nothing recorded on a refused send


def test_persist_handles_multipart_html(monkeypatch, tmp_path):
    from email.message import EmailMessage
    monkeypatch.setattr(gmail, "_sent_dir", lambda: tmp_path / "sent")
    msg = EmailMessage()
    msg["From"], msg["Subject"] = "a@b", "s"
    msg.set_content("PLAIN BODY")
    msg.add_alternative("<p>HTML BODY</p>", subtype="html")
    gmail._persist(msg, {"message_id": "<abc@x>"}, to_outbox=False)   # must NOT crash on multipart
    saved = json.loads((tmp_path / "sent" / "abc.json").read_text())
    assert saved["body"].strip() == "PLAIN BODY"               # text/plain part extracted (no crash)


def test_digest_flags_undelivered(monkeypatch, tmp_path):
    monkeypatch.setattr(supervise, "_pending", lambda: tmp_path / "pending")
    monkeypatch.setattr(supervise, "_journal", lambda: tmp_path / "journal.jsonl")
    monkeypatch.setattr(supervise, "_goals", lambda: tmp_path / "goals.json")
    monkeypatch.setattr(gmail, "send", _raise_refused)
    tok = supervise.stage_draft("undelivered draft", "B", "finding")
    supervise.request_approval(tok, "undelivered draft", "B", _cfg(), log)   # refused -> unsent
    _, body = supervise.build_digest()
    assert tok in body and "not yet delivered" in body
