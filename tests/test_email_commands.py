"""New email commands (Group A + B) and the cross-cutting niceties: per-persona [tag] targeting,
acknowledgement replies, and the burned-COMMAND_TOKEN tripwire. Plus the enforcement points the
escalations hook into (gmail QUIET/THROTTLE gate, research NO-RESEARCH gate)."""
import dataclasses
import json
import logging
from datetime import timedelta
from types import SimpleNamespace

import pytest

from cagent import clock, commands, config, gmail, goals as goals_mod, memory, supervise
from cagent.cognition import research

log = logging.getLogger("test")
TOK = "SECRETTOKEN"


def cfg(ack=True):
    return dataclasses.replace(config.load(), command_token=TOK, ack_commands=ack,
                               owner_email="owner@example.com",
                               staging_recipient="owner+cagent-staging@example.com")


def owner(subject, mid="<m1@x>"):
    return {"uid": "u1", "from": "owner@example.com", "subject": subject,
            "body_text": "", "message_id": mid}


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect every per-persona flag/state path the command layer touches into tmp, legacy
    single-persona (no CAGENT_PERSONA), and capture outbound mail instead of sending it."""
    monkeypatch.delenv("CAGENT_PERSONA", raising=False)
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(commands, "STOP", tmp_path / "var" / "STOP")
    monkeypatch.setattr(commands, "_status_request", lambda: tmp_path / "state" / "status_request.flag")
    monkeypatch.setattr(commands, "_quiet_until", lambda: tmp_path / "state" / "quiet_until.json")
    monkeypatch.setattr(commands, "_throttle", lambda: tmp_path / "state" / "throttle.json")
    monkeypatch.setattr(commands, "_no_research", lambda: tmp_path / "state" / "no_research.flag")
    monkeypatch.setattr(commands, "_token_burned", lambda: tmp_path / "state" / "token_burned.flag")
    monkeypatch.setattr(commands, "_pending_acks", lambda: tmp_path / "state" / "pending_acks")
    # stop_sending uses config.state_root() directly via _stop_sending_path; REPO_ROOT patch covers it.
    monkeypatch.setattr(goals_mod, "_goals", lambda: tmp_path / "state" / "goals.json")
    monkeypatch.setattr(goals_mod, "_archive", lambda: tmp_path / "state" / "goals_archive.json")
    monkeypatch.setattr(goals_mod, "_history_path", lambda: tmp_path / "state" / "goals_history.jsonl")
    monkeypatch.setattr(memory, "_mem", lambda: tmp_path / "state" / "memory")
    monkeypatch.setattr(memory, "_notes", lambda: tmp_path / "state" / "memory" / "notes")
    monkeypatch.setattr(memory, "_index", lambda: tmp_path / "state" / "memory" / "index.jsonl")
    sent = []
    monkeypatch.setattr(gmail, "send",
                        lambda **kw: sent.append(kw) or SimpleNamespace(ok=True, dry_run=True,
                                                                        to="owner", message_id="<o>"))
    return tmp_path, sent


# --------------------------- Group A: read-only replies --------------------------- #

def test_help_replies_with_command_list(sandbox):
    _, sent = sandbox
    applied = commands.parse_and_apply([owner(f"!HELP {TOK}")], cfg(), log)
    assert applied[0]["ok"] and applied[0]["_self_replied"]
    assert len(sent) == 1 and "!PAUSE" in sent[0]["body_md"] and "!QUIET" in sent[0]["body_md"]


def test_ping_replies_alive(sandbox):
    _, sent = sandbox
    applied = commands.parse_and_apply([owner(f"!PING {TOK}")], cfg(), log)
    assert applied[0]["ok"]
    assert sent[0]["kind"] == "ping" and "alive @" in sent[0]["body_md"]


def test_goals_reply_lists_ids(sandbox):
    _, sent = sandbox
    goals_mod.upsert({"title": "study cathedrals"}, rationale="seed")
    commands.parse_and_apply([owner(f"!GOALS {TOK}")], cfg(), log)
    body = sent[0]["body_md"]
    assert "study cathedrals" in body and "[G1]" in body


def test_commands_require_token(sandbox):
    _, sent = sandbox
    applied = commands.parse_and_apply([owner("!HELP")], cfg(), log)   # no token in subject
    assert applied[0].get("refused") and not sent                       # nothing sent


# --------------------------- Group A: state-changing escalations --------------------------- #

def test_goal_strips_token_from_text(sandbox):
    commands.parse_and_apply([owner(f"!GOAL read more Borges {TOK}")], cfg(), log)
    g = goals_mod.load()
    assert len(g) == 1 and g[0]["title"] == "read more Borges"   # token scrubbed out of the goal


def test_drop_goal_archives(sandbox):
    goals_mod.upsert({"title": "a quest"}, rationale="seed")
    applied = commands.parse_and_apply([owner(f"!DROP-GOAL G1 {TOK}")], cfg(), log)
    assert applied[0]["ok"] and applied[0]["dropped"] == "G1"
    assert goals_mod.load() == []                                # retired -> archived


def test_drop_goal_unknown_id_refused(sandbox):
    applied = commands.parse_and_apply([owner(f"!DROP-GOAL G9 {TOK}")], cfg(), log)
    assert applied[0].get("refused")


def test_focus_queues_steering(sandbox):
    from cagent import control
    applied = commands.parse_and_apply([owner(f"!FOCUS cathedrals {TOK}")], cfg(), log)
    assert applied[0]["ok"] and applied[0]["queued"]
    q = control.queue_path("").read_text()
    assert "Focus bias: cathedrals" in q and '"instruction"' in q


def test_feedback_writes_memory(sandbox):
    applied = commands.parse_and_apply([owner(f"!FEEDBACK be more concise {TOK}")], cfg(), log)
    assert applied[0]["ok"]
    idx = memory.index_entries()
    assert len(idx) == 1 and idx[0]["kind"] == "feedback"


def test_quiet_writes_window(sandbox):
    tmp, _ = sandbox
    applied = commands.parse_and_apply([owner(f"!QUIET 6 {TOK}")], cfg(), log)
    assert applied[0]["ok"] and applied[0]["hours"] == 6
    assert "until" in json.loads((tmp / "state" / "quiet_until.json").read_text())


def test_quiet_needs_positive_hours(sandbox):
    applied = commands.parse_and_apply([owner(f"!QUIET 0 {TOK}")], cfg(), log)
    assert applied[0].get("refused")


def test_throttle_writes_today_cap(sandbox):
    tmp, _ = sandbox
    applied = commands.parse_and_apply([owner(f"!THROTTLE 1 {TOK}")], cfg(), log)
    assert applied[0]["ok"] and applied[0]["cap"] == 1
    d = json.loads((tmp / "state" / "throttle.json").read_text())
    assert d["cap"] == 1 and d["date"] == clock.today()


def test_no_research_sets_flag(sandbox):
    tmp, _ = sandbox
    applied = commands.parse_and_apply([owner(f"!NO-RESEARCH {TOK}")], cfg(), log)
    assert applied[0]["ok"] and (tmp / "state" / "no_research.flag").exists()


def test_stop_sending_sets_flag(sandbox):
    tmp, _ = sandbox
    applied = commands.parse_and_apply([owner(f"!STOP-SENDING {TOK}")], cfg(), log)
    assert applied[0]["ok"] and (tmp / "state" / "stop_sending.flag").exists()


def test_pause_all_pauses_every_persona(sandbox, monkeypatch):
    tmp, _ = sandbox
    from cagent import control
    monkeypatch.setattr(config, "enabled_personas", lambda: ["alpha", "beta"])
    monkeypatch.setattr(control, "PERSONA_STOP_DIR", tmp / "var" / "persona")
    applied = commands.parse_and_apply([owner(f"!PAUSE-ALL {TOK}")], cfg(), log)
    assert applied[0]["ok"] and set(applied[0]["paused"]) == {"alpha", "beta"}
    assert control.is_paused("alpha") and control.is_paused("beta")


# --------------------------- cross-cutting: [persona] targeting --------------------------- #

def test_pause_targets_named_persona(sandbox, monkeypatch):
    tmp, _ = sandbox
    from cagent import control
    monkeypatch.setenv("CAGENT_PERSONA", "alpha")              # running persona
    monkeypatch.setattr(config, "enabled_personas", lambda: ["bravo", "data"])
    monkeypatch.setattr(control, "PERSONA_STOP_DIR", tmp / "var" / "persona")
    applied = commands.parse_and_apply([owner(f"!PAUSE [data] {TOK}")], cfg(), log)
    assert applied[0]["ok"] and applied[0]["scope"] == "data"
    assert control.is_paused("data") and not control.is_paused("alpha")   # regardless of threading


def test_unknown_target_persona_refused(sandbox, monkeypatch):
    monkeypatch.setattr(config, "enabled_personas", lambda: ["bravo", "data"])
    applied = commands.parse_and_apply([owner(f"!PAUSE [ghost] {TOK}")], cfg(), log)
    assert applied[0].get("refused") and "ghost" in applied[0]["refused"]


def test_plain_pause_with_bogus_current_persona_refused(sandbox, monkeypatch):
    """Regression (2026-07-03 alpha.STOP): a plain !PAUSE with no [bracket], while CAGENT_PERSONA is
    a persona that is NOT enabled (a manual run-tick typo / stale env), must be refused and leave no
    orphan var/persona/<name>.STOP. The fallback target comes from the unvalidated env var, so the
    bracket guard above does not catch it -- the resolved-target guard must."""
    tmp, _ = sandbox
    from cagent import control
    monkeypatch.setenv("CAGENT_PERSONA", "alpha")                  # bogus running persona
    monkeypatch.setattr(config, "enabled_personas", lambda: ["bravo", "data"])
    monkeypatch.setattr(control, "PERSONA_STOP_DIR", tmp / "var" / "persona")
    applied = commands.parse_and_apply([owner(f"!PAUSE {TOK}")], cfg(), log)
    assert applied[0].get("refused") and "alpha" in applied[0]["refused"]
    assert not control.is_paused("alpha")
    assert not (tmp / "var" / "persona" / "alpha.STOP").exists()


def test_cmd_pause_chokepoint_rejects_unknown_persona(sandbox, monkeypatch):
    """The _cmd_pause writer itself refuses an unknown persona even if reached directly, so the one
    function that materializes a .STOP stays honest for any future caller."""
    tmp, _ = sandbox
    from cagent import control
    monkeypatch.setattr(config, "enabled_personas", lambda: ["bravo", "data"])
    monkeypatch.setattr(control, "PERSONA_STOP_DIR", tmp / "var" / "persona")
    res = commands._cmd_pause("alpha")
    assert res.get("refused") and "alpha" in res["refused"]
    assert not (tmp / "var" / "persona" / "alpha.STOP").exists()


# --------------------------- cross-cutting: acknowledgement replies --------------------------- #

def test_acknowledge_summarizes_applied_and_refused(sandbox):
    _, sent = sandbox
    applied = [{"cmd": "PAUSE", "ok": True, "scope": "data"},
               {"cmd": "QUIET", "refused": "needs hours"},
               {"cmd": "HELP", "ok": True, "_self_replied": True}]      # self-replied -> skipped
    res = commands.acknowledge(applied, [owner("x")], cfg(ack=True), log)
    assert res["acked"] == 2
    body = sent[0]["body_md"]
    assert "applied: !PAUSE (data)" in body and "refused: !QUIET" in body and "!HELP" not in body


def test_acknowledge_disabled_by_config(sandbox):
    _, sent = sandbox
    res = commands.acknowledge([{"cmd": "PAUSE", "ok": True}], [owner("x")], cfg(ack=False), log)
    assert res is None and not sent


def _refuse_send(**kw):
    raise gmail.SendRefused("global daily cap reached (6/6)")


def _pending_ack_files(tmp):
    return list((tmp / "state" / "pending_acks").glob("*.json"))


def test_acknowledge_refused_queues_for_retry(sandbox, monkeypatch):
    tmp, sent = sandbox
    monkeypatch.setattr(gmail, "send", _refuse_send)              # cap exhausted at send time
    res = commands.acknowledge([{"cmd": "GOAL", "ok": True, "title": "polyamory"}],
                               [owner("x", mid="<g1@x>")], cfg(ack=True), log)
    assert res["queued"] and res["acked"] == 0 and not sent      # nothing went out
    files = _pending_ack_files(tmp)
    assert len(files) == 1                                        # but it was staged for retry
    d = json.loads(files[0].read_text())
    assert d["in_reply_to"] == "<g1@x>" and any("!GOAL" in ln for ln in d["lines"])


def test_retry_acks_resends_and_clears(sandbox, monkeypatch):
    tmp, sent = sandbox
    monkeypatch.setattr(gmail, "send", _refuse_send)             # stage a refused ack
    commands.acknowledge([{"cmd": "GOAL", "ok": True, "title": "polyamory"}],
                         [owner("x", mid="<g1@x>")], cfg(ack=True), log)
    assert _pending_ack_files(tmp)
    monkeypatch.setattr(gmail, "send",                            # cap clears: send succeeds again
                        lambda **kw: sent.append(kw) or SimpleNamespace(ok=True, dry_run=False,
                                                                        to="owner", message_id="<o>"))
    out = commands.retry_acks(cfg(ack=True), log)
    assert len(out) == 1 and out[0]["in_reply_to"] == "<g1@x>"
    assert sent and sent[0]["kind"] == "ack" and "!GOAL" in sent[0]["body_md"]
    assert sent[0]["in_reply_to"] == "<g1@x>"                     # threaded onto the command
    assert not _pending_ack_files(tmp)                           # cleared once delivered


def test_retry_acks_leaves_queued_when_still_refused(sandbox, monkeypatch):
    tmp, sent = sandbox
    monkeypatch.setattr(gmail, "send", _refuse_send)
    commands.acknowledge([{"cmd": "PAUSE", "ok": True, "scope": "data"}],
                         [owner("x", mid="<p1@x>")], cfg(ack=True), log)
    out = commands.retry_acks(cfg(ack=True), log)                # still over cap -> no-op
    assert out == [] and not sent
    assert len(_pending_ack_files(tmp)) == 1                     # stays queued for a later tick


def test_retry_acks_noop_when_disabled(sandbox, monkeypatch):
    tmp, sent = sandbox
    monkeypatch.setattr(gmail, "send", _refuse_send)
    commands.acknowledge([{"cmd": "GOAL", "ok": True}], [owner("x")], cfg(ack=True), log)
    assert commands.retry_acks(cfg(ack=False), log) == [] and not sent   # disabled -> don't flush


# --------------------------- cross-cutting: burned token tripwire --------------------------- #

def test_token_burned_on_nonowner_exposure(sandbox):
    tmp, sent = sandbox
    dropped = [({"from": "Attacker <attacker@example.com>", "subject": f"hi {TOK}", "body_text": ""},
                "not-owner")]
    assert commands.note_token_exposure(dropped, cfg(), log) is True
    assert (tmp / "state" / "token_burned.flag").exists()
    assert sent and sent[0]["kind"] == "alert"   # cap-exempt so a full send cap cannot swallow it
    # the alert names the sender so the owner can tell "that was me from my other address" from a
    # real leak, and knows which OWNER_EMAIL to fix
    assert "(from attacker@example.com)" in sent[0]["body_md"]
    # once burned, even a valid owner command is refused until rotated locally
    applied = commands.parse_and_apply([owner(f"!PING {TOK}")], cfg(), log)
    assert applied[0].get("refused") and "burned" in applied[0]["refused"]


def test_token_burn_detects_other_personas_token(sandbox, monkeypatch):
    # P1-7: a non-owner leaking a DIFFERENT persona's token (not the running persona's) is still a
    # compromise. Detection scans the FULL set of persona tokens, not just cfg.command_token.
    tmp, sent = sandbox
    other = "OTHERPERSONATOKEN"
    monkeypatch.setattr(commands.gmail, "_all_command_tokens", lambda: {other, TOK})
    dropped = [({"from": "Stranger <s@evil.example>", "subject": f"here {other}", "body_text": ""},
                "not-owner")]
    assert commands.note_token_exposure(dropped, cfg(), log) is True
    assert (tmp / "state" / "token_burned.flag").exists()
    assert sent and sent[0]["kind"] == "alert"


def test_token_burn_alert_sender_unknown_when_from_missing(sandbox):
    tmp, sent = sandbox
    dropped = [({"subject": f"hi {TOK}", "body_text": ""}, "not-owner")]   # no From header
    assert commands.note_token_exposure(dropped, cfg(), log) is True
    assert "(from unknown)" in sent[0]["body_md"]


def test_no_exposure_when_token_absent(sandbox):
    tmp, _ = sandbox
    dropped = [({"subject": "ordinary spam", "body_text": "buy now"}, "not-owner")]
    assert commands.note_token_exposure(dropped, cfg(), log) is False
    assert not (tmp / "state" / "token_burned.flag").exists()


def test_owner_footer_echo_reply_does_not_burn(sandbox):
    # Regression, 2026-07-01 Golf incident: an OWNER reply to one of our emails is dropped as
    # own-footer-echo (it quotes our disclosure footer) but its subject still carries the token. That
    # is the owner's own command, not a leak, and must NOT burn.
    tmp, sent = sandbox
    dropped = [({"from": "owner@example.com", "subject": f"!GOAL love {TOK}",
                 "body_text": "...written and sent autonomously by an AI research agent..."},
                "own-footer-echo")]
    assert commands.note_token_exposure(dropped, cfg(), log) is False
    assert not (tmp / "state" / "token_burned.flag").exists()
    assert not sent


def test_nonowner_footer_echo_still_burns(sandbox):
    # But a genuine stranger who quoted our footer AND holds the token is a real leak -- ownership is
    # judged by the From address, not the drop reason, so this still burns.
    tmp, sent = sandbox
    dropped = [({"from": "leaker@evil.example", "subject": f"got it {TOK}",
                 "body_text": "quoted: ...autonomously by an AI research agent..."}, "own-footer-echo")]
    assert commands.note_token_exposure(dropped, cfg(), log) is True
    assert (tmp / "state" / "token_burned.flag").exists()
    assert sent and sent[0]["kind"] == "alert"   # cap-exempt so a full send cap cannot swallow it
    assert "[dropped as own-footer-echo]" in sent[0]["body_md"]


def test_token_burn_notice_retries_when_first_send_refused(sandbox, monkeypatch):
    # A refused token-burn alert must NOT be lost: the burn is durable, and a pending marker drives a
    # retry on a later tick until the owner is actually told the token is compromised.
    tmp, sent = sandbox
    monkeypatch.setattr(gmail, "send", lambda **kw: (_ for _ in ()).throw(gmail.SendRefused("cap")))
    dropped = [({"from": "attacker@example.com", "subject": f"hi {TOK}", "body_text": ""}, "not-owner")]
    assert commands.note_token_exposure(dropped, cfg(), log) is True
    assert (tmp / "state" / "token_burned.flag").exists()            # burned regardless of delivery
    assert (tmp / "state" / "token_burn_notice_pending").exists()    # notice owed
    assert not sent                                                  # nothing delivered yet

    # next tick: send works now; the retry (via the already-burned path) delivers and clears the marker
    monkeypatch.setattr(gmail, "send",
                        lambda **kw: sent.append(kw) or SimpleNamespace(ok=True, dry_run=False, to="o"))
    assert commands.note_token_exposure([], cfg(), log) is False     # already burned -> retry path
    assert sent and sent[0]["kind"] == "alert"
    assert not (tmp / "state" / "token_burn_notice_pending").exists()  # delivered -> stop retrying


# --------------------------- Group B: approval-flow enhancements --------------------------- #

@pytest.fixture
def staged(tmp_path, monkeypatch):
    monkeypatch.setattr(supervise, "_pending", lambda: tmp_path / "pending")
    monkeypatch.setattr(memory, "_mem", lambda: tmp_path / "mem")
    monkeypatch.setattr(memory, "_notes", lambda: tmp_path / "mem" / "notes")
    monkeypatch.setattr(memory, "_index", lambda: tmp_path / "mem" / "index.jsonl")
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    sent = []
    monkeypatch.setattr(gmail, "send",
                        lambda **kw: sent.append(kw) or SimpleNamespace(ok=True, dry_run=False, to="o"))
    tok = supervise.stage_draft("A subject", "original body", "finding")
    return tok, sent


def test_edit_replaces_body_then_sends(staged):
    tok, sent = staged
    out = commands.handle_approvals([owner(f"EDIT {tok}: the owner rewrote this")], cfg(), log)
    assert out[0]["approved"] and out[0]["edited"]
    assert sent[0]["body_md"] == "the owner rewrote this"          # owner's text, not the draft's
    assert supervise.get(tok) is None                              # released


def test_hold_defers_and_digest_skips_it(staged):
    tok, _ = staged
    out = commands.handle_approvals([owner(f"HOLD {tok}")], cfg(), log)
    assert out[0]["held"] and supervise.get(tok)["held"] is True   # still staged
    _, body = supervise.build_digest()
    assert tok not in body                                         # no longer re-prompted


def test_reject_with_reason_feeds_memory(staged):
    tok, _ = staged
    out = commands.handle_approvals([owner(f"REJECT {tok}: off topic")], cfg(), log)
    assert out[0]["rejected"] and out[0]["reason"] == "off topic"
    assert supervise.get(tok) is None
    idx = memory.index_entries()
    assert idx and idx[0]["kind"] == "feedback"


def test_plain_reject_still_works(staged):
    tok, _ = staged
    out = commands.handle_approvals([owner(f"REJECT {tok}")], cfg(), log)
    assert out[0]["rejected"] and out[0]["reason"] is None


# --------------------------- enforcement: gmail + research gates --------------------------- #

def test_quiet_active_blocks_until_expiry(tmp_path, monkeypatch):
    p = tmp_path / "quiet.json"
    monkeypatch.setattr(gmail, "_quiet_until", lambda: p)
    p.write_text(json.dumps({"until": (clock.now() + timedelta(hours=1)).isoformat()}))
    assert gmail.quiet_active() is True
    p.write_text(json.dumps({"until": (clock.now() - timedelta(hours=1)).isoformat()}))
    assert gmail.quiet_active() is False


def test_throttle_cap_only_lowers(tmp_path, monkeypatch):
    monkeypatch.setattr(gmail, "_throttle", lambda: tmp_path / "throttle.json")
    (tmp_path / "throttle.json").write_text(json.dumps({"date": clock.today(), "cap": 1}))
    assert gmail.throttle_cap(3) == 1                              # lowered
    (tmp_path / "throttle.json").write_text(json.dumps({"date": clock.today(), "cap": 9}))
    assert gmail.throttle_cap(3) == 3                              # cannot raise above configured
    (tmp_path / "throttle.json").write_text(json.dumps({"date": "1999-01-01", "cap": 1}))
    assert gmail.throttle_cap(3) == 3                              # stale entry ignored


def test_no_research_flag_disables_subcall(tmp_path, monkeypatch):
    monkeypatch.setattr(research, "_no_research", lambda: tmp_path / "no_research.flag")
    (tmp_path / "no_research.flag").write_text("x")
    out = research.run("anything")                                 # returns WITHOUT a claude call
    assert out is None   # L2: !NO-RESEARCH returns None, not a stub dict


# --------------------------- _seal_inbound: ingest-time redaction + auth witnesses --------------------------- #

def test_seal_inbound_redacts_token_before_disk_write():
    # H4: the COMMAND_TOKEN must be stripped at ingest time (before first write to disk) so it
    # never ends up in a committed received/*.json. cmd_token_ok witnesses the auth so
    # parse_and_apply still accepts the command from the on-disk redacted record.
    tok = "SeCrEtT0k3n"
    msg = {"uid": 99, "from": "owner@example.com",
           "subject": f"!PAUSE {tok}", "body_text": ""}
    gmail._seal_inbound(msg, tok)
    assert tok not in msg.get("subject", "")        # token wiped from subject
    assert msg.get("cmd_token_ok") is True           # auth witness set
    assert msg.get("token_seen") is True             # exposure witness set
    assert gmail.TOKEN_REDACTED in msg["subject"]    # replaced with placeholder


def test_seal_inbound_cross_persona_wrong_token_leaves_raw_value():
    # H4 residual: if ingest() calls _seal_inbound with the GLOBAL token but the mail carries
    # a different PERSONA token, the real token stays unredacted. This documents the behaviour
    # _seal_inbound exhibits when passed the wrong token — and motivates ingest() using
    # config.load(persona).command_token rather than the global cfg.command_token.
    real_tok = "PersonaBToken123"
    wrong_tok = "GlobalToken456xx"
    msg = {"uid": 1, "subject": f"!PAUSE {real_tok}", "body_text": ""}
    gmail._seal_inbound(msg, wrong_tok)
    assert real_tok in msg["subject"]       # still raw — wrong token can't redact it
    assert not msg.get("cmd_token_ok")      # auth witness not set — command will be refused


def test_seal_inbound_sets_token_seen_from_body():
    tok = "B0dyT0k3n"
    msg = {"uid": 100, "from": "stranger@example.com",
           "subject": "hi there", "body_text": f"got it: {tok}"}
    gmail._seal_inbound(msg, tok)
    assert tok not in msg["body_text"]
    assert msg.get("token_seen") is True
    assert not msg.get("cmd_token_ok")     # was in body, not subject => no cmd_token_ok


def test_parse_and_apply_accepts_sealed_record(tmp_path, monkeypatch):
    # Commands from sealed (redacted) records still authenticate via cmd_token_ok witness.
    monkeypatch.setattr(commands, "STOP", tmp_path / "STOP")
    monkeypatch.setattr(commands, "_token_burned", lambda: tmp_path / "token_burned.flag")
    tok = "R3a1T0k3n"
    msg = {"uid": 101, "from": "owner@example.com",
           "subject": f"!PAUSE {gmail.TOKEN_REDACTED}",  # already redacted on disk
           "body_text": "", "cmd_token_ok": True}
    c = dataclasses.replace(config.load(), command_token=tok,
                            owner_email="owner@example.com",
                            staging_recipient="owner+s@example.com")
    applied = commands.parse_and_apply([msg], c, log)
    assert applied and applied[0].get("ok"), applied


def test_note_token_exposure_uses_token_seen_witness(tmp_path, monkeypatch):
    # note_token_exposure must detect exposure via the token_seen witness even when the record was
    # already redacted (the live token is no longer in the plaintext).
    monkeypatch.setattr(commands, "_token_burned", lambda: tmp_path / "token_burned.flag")
    tok = "W1tN3ssT0k"
    sent = []
    monkeypatch.setattr(gmail, "send",
                        lambda **k: sent.append(k) or type("R", (), {"ok": True, "dry_run": True})())
    msg = {"from": "leaker@evil.example", "subject": "hi [REDACTED]",
           "body_text": "", "token_seen": True}
    c = dataclasses.replace(config.load(), command_token=tok,
                            owner_email="owner@example.com",
                            staging_recipient="owner+s@example.com")
    dropped = [(msg, "not-owner")]
    result = commands.note_token_exposure(dropped, c, log)
    assert result is True
    assert (tmp_path / "token_burned.flag").exists()
