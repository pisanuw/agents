"""The backoff gate: on a rate/auth limit the agent makes ZERO claude calls until next_allowed_ts,
then resumes automatically. This enforces 'zero claude calls while rate-limited' and the exponential
escalation -- previously untested. Uses a controllable clock (see clock.py) and a tmp state dir."""
from datetime import datetime, timedelta, timezone

from cagent.cognition import backoff

T0 = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)


def _at(monkeypatch, when):
    monkeypatch.setattr(backoff.clock, "now", lambda: when)
    monkeypatch.setattr(backoff.clock, "iso", lambda: when.isoformat())


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(backoff.config, "state_root", lambda *a, **k: tmp_path)


def test_gate_open_when_no_state(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert backoff.gate_open() == (True, "")


def test_failure_closes_gate_then_reopens_after_window(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _at(monkeypatch, T0)
    st = backoff.record_failure("RATE_LIMIT", http=429)
    assert st["consecutive_failures"] == 1 and st["reason"] == "RATE_LIMIT"

    _at(monkeypatch, T0 + timedelta(minutes=30))                 # inside the 1h window
    ok, reason = backoff.gate_open()
    assert not ok and "deferred until" in reason                 # zero claude calls while backing off

    _at(monkeypatch, T0 + timedelta(hours=1, minutes=1))         # past the window
    assert backoff.gate_open()[0] is True                        # resumes automatically


def test_backoff_escalates_then_caps_at_12h(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _at(monkeypatch, T0)
    for i, hours in enumerate([1, 2, 4, 8, 12, 12, 12], start=1):  # SCHEDULE_HOURS then capped
        st = backoff.record_failure("RATE_LIMIT")
        assert st["consecutive_failures"] == i
        delay = datetime.fromisoformat(st["next_allowed_ts"]) - T0
        assert delay == timedelta(hours=hours), f"failure {i}: {delay} != {hours}h"


def test_success_clears_the_backoff(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _at(monkeypatch, T0)
    backoff.record_failure("AUTH_ERROR")
    _at(monkeypatch, T0 + timedelta(minutes=5))
    assert backoff.gate_open()[0] is False                       # still closed
    backoff.record_success()
    assert backoff.gate_open()[0] is True                        # a good call reopens the gate immediately


def test_corrupt_backoff_fails_closed_but_self_heals(monkeypatch, tmp_path):
    # A corrupt/torn backoff file could be MASKING an active rate/auth limit, so gate_open must NOT
    # immediately resume (the old fail-open). It fails CLOSED but heals into a 1h fixed backoff that
    # auto-clears -- so it never wedges the agent forever either.
    _isolate(monkeypatch, tmp_path)
    _at(monkeypatch, T0)
    (tmp_path / "backoff.json").write_text('{"next_allowed_ts": "not-a-timestamp"}')
    assert backoff.gate_open()[0] is False                       # fail closed now
    _at(monkeypatch, T0 + timedelta(hours=1, minutes=1))         # ...but the healed 1h window elapses
    assert backoff.gate_open()[0] is True                        # resumes automatically (no permanent wedge)


def test_totally_corrupt_backoff_json_fails_closed(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _at(monkeypatch, T0)
    (tmp_path / "backoff.json").write_text("{ this is not json")
    assert backoff.gate_open()[0] is False                       # unreadable file -> hold, don't hammer the API
