"""tick_pipeline ordering invariant (M2): inbound mail is marked processed BEFORE bounded actions
run, so a crash mid-execute (after a send_email) cannot re-feed the same owner mail next tick and
reply twice -- file-level dedup matches the IMAP cursor's advance-before-cognition (at-most-once)."""
import logging
from types import SimpleNamespace

from cagent import tick_pipeline


def _wire_run(monkeypatch, tmp_path, *, msg, parse_result, order):
    """Stub every external of tick_pipeline.run() so only the ordering/marking under test is real."""
    monkeypatch.setenv("CAGENT_PERSONA", "tester")          # skip the legacy self-poll IMAP branch
    monkeypatch.setattr(tick_pipeline, "_ticks_dir", lambda: tmp_path / "ticks")
    monkeypatch.setattr(tick_pipeline, "_finish", lambda *a, **k: None)
    monkeypatch.setattr(tick_pipeline.gmail, "pending_inbound", lambda: [msg])
    monkeypatch.setattr(tick_pipeline.guardrails, "filter_inbound", lambda inb, cfg: (inb, []))
    monkeypatch.setattr(tick_pipeline.commands, "note_token_exposure", lambda *a, **k: False)
    monkeypatch.setattr(tick_pipeline.commands, "parse_and_apply", lambda *a, **k: [])
    monkeypatch.setattr(tick_pipeline.commands, "handle_approvals", lambda *a, **k: [])
    monkeypatch.setattr(tick_pipeline.commands, "status_requested", lambda: False)
    monkeypatch.setattr(tick_pipeline.commands, "retry_acks", lambda *a, **k: [])
    monkeypatch.setattr(tick_pipeline.supervise, "retry_undelivered", lambda *a, **k: [])
    monkeypatch.setattr(tick_pipeline.supervise, "check_tripwire", lambda *a, **k: None)
    monkeypatch.setattr(tick_pipeline.control, "drain", lambda *a, **k: [])
    monkeypatch.setattr(tick_pipeline.context, "build", lambda cfg, inb: "ctx")
    monkeypatch.setattr(tick_pipeline.persona, "load_system_prompt", lambda: "")
    monkeypatch.setattr(tick_pipeline.invoke, "run_claude", lambda *a, **k: {})
    monkeypatch.setattr(tick_pipeline.parse, "parse", lambda env: parse_result)
    monkeypatch.setattr(tick_pipeline.backoff, "record_success", lambda: None)
    monkeypatch.setattr(tick_pipeline.backoff, "record_failure", lambda *a, **k: None)
    monkeypatch.setattr(tick_pipeline.reflect, "should_reflect", lambda cfg: (False, ""))
    monkeypatch.setattr(tick_pipeline, "_maybe_digest", lambda *a, **k: None)
    marked = []
    monkeypatch.setattr(tick_pipeline.gmail, "mark_processed",
                        lambda inb: (marked.extend(m.get("subject") for m in inb),
                                     order.append("mark") if inb else None))
    monkeypatch.setattr(tick_pipeline.execute, "apply", lambda *a, **k: order.append("execute") or [])
    return marked


def test_inbound_marked_processed_before_execute(tmp_path, monkeypatch):
    order = []
    msg = {"uid": "1", "from": "owner@example.com", "subject": "hi", "message_id": "<m1>", "_path": "x"}
    _wire_run(monkeypatch, tmp_path, msg=msg,
              parse_result=SimpleNamespace(status="OK", structured={"summary": "s", "actions": []},
                                           rate_limited=False, http=None, cost_usd=0),
              order=order)
    tick_pipeline.run(SimpleNamespace(MODE="SUPERVISED", persona=""), logging.getLogger("t"))
    assert order == ["mark", "execute"]                     # content mail marked BEFORE actions execute


def test_consumed_command_mail_withheld_from_context(tmp_path, monkeypatch):
    # Regression (silent-reject leak): a deterministically-handled command/approval email must NOT
    # reach cognition's context. A reason-less `REJECT <token>` deletes its draft and writes NO memory
    # note (delete-and-nothing-more); but if the raw REJECT email still surfaced in the NEW MAIL
    # section, the persona would read that its draft was rejected and fire a spurious "what did I
    # miss?" reply. context.build must see only genuine CONTENT mail, never the consumed command.
    order = []
    content = {"uid": "1", "from": "owner@example.com", "subject": "a real question",
               "message_id": "<m1>", "_path": "x"}
    reject = {"uid": "2", "from": "owner@example.com", "subject": "REJECT ceeeac6d",
              "message_id": "<m2>", "_path": "y"}
    _wire_run(monkeypatch, tmp_path, msg=content,
              parse_result=SimpleNamespace(status="OK", structured={"summary": "s", "actions": []},
                                           rate_limited=False, http=None, cost_usd=0),
              order=order)
    monkeypatch.setattr(tick_pipeline.gmail, "pending_inbound", lambda: [content, reject])
    captured = []                                            # consumed_messages runs for real here
    monkeypatch.setattr(tick_pipeline.context, "build",
                        lambda cfg, inb: (captured.append([m.get("subject") for m in inb]), "ctx")[1])
    tick_pipeline.run(SimpleNamespace(MODE="SUPERVISED", persona=""), logging.getLogger("t"))
    assert captured == [["a real question"]]                # content kept, REJECT command withheld


def test_rate_limited_tick_still_marks_command_mail(tmp_path, monkeypatch):
    # Idempotency: a rate-limited cognition exit does NOT mark content mail (so it retries), but the
    # command/approval mail whose side effects already fired MUST be marked so it can't re-apply next
    # tick (duplicate goals/acks). Here a !GOAL command message is the only inbound; after a
    # rate-limited parse, it must have been marked processed.
    order = []
    cmd_msg = {"uid": "9", "from": "owner@example.com", "message_id": "<c1>", "_path": "y",
               "subject": "!GOAL deadbeef add a new goal", "cmd_token_ok": True}
    marked = _wire_run(monkeypatch, tmp_path, msg=cmd_msg,
                       parse_result=SimpleNamespace(status="RATE_LIMIT", structured=None,
                                                    rate_limited=True, http=429, cost_usd=0),
                       order=order)
    tick_pipeline.run(SimpleNamespace(MODE="SUPERVISED", persona=""), logging.getLogger("t"))
    assert "!GOAL deadbeef add a new goal" in marked         # command mail marked despite rate-limit
    assert "execute" not in order                            # cognition did no work (rate limited)


def test_no_structured_decision_marks_all_and_skips_execute(tmp_path, monkeypatch):
    # A parse that succeeds transport-wise but yields no structured decision (e.g. BAD_JSON) is a
    # no-op tick: it records success (not a rate-limit), marks ALL inbound so the same mail can't loop,
    # and runs no bounded actions. Distinct from the rate-limit path (which leaves content mail unmarked).
    order = []
    msg = {"uid": "1", "from": "owner@example.com", "subject": "hi", "message_id": "<m1>", "_path": "x"}
    marked = _wire_run(monkeypatch, tmp_path, msg=msg,
                       parse_result=SimpleNamespace(status="BAD_JSON", structured=None,
                                                    rate_limited=False, http=None, cost_usd=0),
                       order=order)
    tick_pipeline.run(SimpleNamespace(MODE="SUPERVISED", persona=""), logging.getLogger("t"))
    assert "hi" in marked                                    # inbound_all marked so it won't re-loop
    assert "execute" not in order                            # no bounded actions on a no-decision tick
