"""Fail-closed gate-check. The mandatory second-opinion pass must BLOCK a send whenever it cannot
be consulted -- the gate prompt is missing, or the sub-call is rate-limited/errored. These paths
were previously only ever stubbed, so a refactor that let an unavailable gate return 'send' would
have shipped silently. Covers execute._gate_check and its effect on _do_email."""
import logging
from types import SimpleNamespace

import pytest

from cagent.cognition import backoff, execute

log = logging.getLogger("t")


@pytest.fixture(autouse=True)
def _no_backlog(monkeypatch):
    """Keep the outbound-backpressure gate inert (and off the real pending/ dir) for these gate tests."""
    from cagent import supervise
    monkeypatch.setattr(supervise, "backlog_depth", lambda: 0)


def test_missing_gate_prompt_fails_closed_without_calling_claude(monkeypatch):
    monkeypatch.setattr(execute, "_gate_prompt_text", lambda: "")       # no gate prompt available at all
    calls = []
    monkeypatch.setattr(execute.invoke, "run_claude", lambda *a, **k: calls.append(1))
    verdict = execute._gate_check("Subject", "draft body", log)
    assert verdict["verdict"] == "revise"                               # never 'send'
    assert "gate-check prompt missing" in verdict["fabrication"]
    assert calls == []                                                  # failed closed WITHOUT consulting claude


def test_unavailable_gate_fails_closed_and_records_backoff(monkeypatch):
    monkeypatch.setattr(backoff, "gate_open", lambda: (True, ""))        # backoff not active -> we DO call
    monkeypatch.setattr(execute, "_gate_prompt_text", lambda: "a real gate prompt")
    monkeypatch.setattr(execute, "_gate_sources", lambda *a, **k: "GROUND TRUTH")
    monkeypatch.setattr(execute.invoke, "run_claude", lambda *a, **k: object())
    # the gate sub-call comes back rate-limited: OK-status/structured-dict is the ONLY send-eligible path
    monkeypatch.setattr(execute.parse, "parse",
                        lambda env: SimpleNamespace(status="RATE_LIMIT", rate_limited=True, http=429, structured=None))
    recorded = []
    monkeypatch.setattr(backoff, "record_failure", lambda status, http=None: recorded.append((status, http)))
    verdict = execute._gate_check("Subject", "draft body", log)
    assert verdict["verdict"] == "revise"
    assert "gate-check unavailable" in verdict["fabrication"]
    assert recorded == [("RATE_LIMIT", 429)]                            # rate-limit propagated to the backoff gate


def test_backoff_active_short_circuits_gate_without_calling_claude(monkeypatch):
    # If a prior sub-call this tick already tripped the backoff, the gate must NOT make another claude
    # call (which would re-hit the 429 and re-escalate). It returns the unavailable sentinel, tagged
    # gate_unavailable so _do_email skips the revise pass too.
    monkeypatch.setattr(backoff, "gate_open", lambda: (False, "deferred until later"))
    calls = []
    monkeypatch.setattr(execute.invoke, "run_claude", lambda *a, **k: calls.append(1))
    verdict = execute._gate_check("Subject", "draft body", log)
    assert verdict["verdict"] == "revise" and verdict.get("gate_unavailable") is True
    assert calls == []                                                  # no claude call while backing off


def test_gate_unavailable_skips_the_revise_pass(monkeypatch):
    # A gate_unavailable verdict must NOT trigger _revise_draft (which would burn another claude call
    # and, on a 429, re-record backoff). _do_email should block directly.
    monkeypatch.setattr(execute, "_gate_check",
                        lambda s, b, log: {"verdict": "revise", "fabrication": ["gate-check unavailable"],
                                           "disclosure_present": False, "gate_unavailable": True})
    revised = []
    monkeypatch.setattr(execute, "_revise_draft", lambda *a, **k: revised.append(1) or (None, None))
    sent = []
    monkeypatch.setattr(execute.gmail, "send", lambda **k: sent.append(k))
    res = execute._do_email({"email": {"subject": "Hi", "body": "real body content here"}},
                            SimpleNamespace(MODE="LIVE", max_backlog_drafts=999), log)
    assert "blocked_by_gate" in res
    assert revised == [] and sent == []                                # no revise call, nothing sent


def test_errored_gate_fails_closed_without_backoff(monkeypatch):
    # a non-rate-limit failure (e.g. bad JSON) is still fail-closed, but must NOT record a backoff.
    monkeypatch.setattr(backoff, "gate_open", lambda: (True, ""))
    monkeypatch.setattr(execute, "_gate_prompt_text", lambda: "a real gate prompt")
    monkeypatch.setattr(execute, "_gate_sources", lambda *a, **k: "GROUND TRUTH")
    monkeypatch.setattr(execute.invoke, "run_claude", lambda *a, **k: object())
    monkeypatch.setattr(execute.parse, "parse",
                        lambda env: SimpleNamespace(status="BAD_JSON", rate_limited=False, http=None, structured=None))
    recorded = []
    monkeypatch.setattr(backoff, "record_failure", lambda *a, **k: recorded.append(a))
    verdict = execute._gate_check("Subject", "draft body", log)
    assert verdict["verdict"] == "revise" and "gate-check unavailable" in verdict["fabrication"]
    assert recorded == []                                               # no backoff for a non-rate-limit error


def test_fail_closed_verdict_blocks_the_send(monkeypatch):
    # end-to-end: a real _gate_check that fails closed (prompt missing) must stop _do_email sending.
    monkeypatch.setattr(execute, "_gate_prompt_text", lambda: "")       # -> _gate_check returns 'revise'
    monkeypatch.setattr(execute, "_revise_draft", lambda *a, **k: (None, None))   # no re-draft attempt
    sent = []
    monkeypatch.setattr(execute.gmail, "send", lambda **k: sent.append(k))
    res = execute._do_email({"email": {"subject": "Hi", "body": "real body content here"}},
                            SimpleNamespace(MODE="LIVE", max_backlog_drafts=999), log)
    assert "blocked_by_gate" in res
    assert sent == []                                                   # nothing went out
