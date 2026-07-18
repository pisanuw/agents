"""Per-heartbeat backoff gate. On a rate/usage/auth limit the agent makes ZERO claude
calls until next_allowed_ts, then resumes automatically. A missed hour is harmless for
an agent that lives for months.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from cagent import atomicio, clock, config


def _backoff_state():
    return config.state_root() / "backoff.json"


SCHEDULE_HOURS = [1, 2, 4, 8, 12]  # grows with consecutive failures, capped at 12h


def _load() -> dict:
    return atomicio.read_json(_backoff_state(), default={})


def _save(d: dict) -> None:
    # Atomic: a torn write makes _load fail open ({}), silently clearing an active rate/auth backoff
    # so the agent resumes claude calls before next_allowed_ts -- the wrong failure direction.
    atomicio.write_text(_backoff_state(), json.dumps(d, indent=2))


def _heal_corrupt(why: str) -> tuple[bool, str]:
    """A corrupt/torn backoff file could be MASKING an active rate/auth limit; failing open (the old
    behavior) would let the agent immediately hammer a limited API. Fail CLOSED, but heal the file
    into a SHORT fixed backoff so it auto-clears within the hour -- never wedging the agent forever
    (the concern that motivated the old fail-open)."""
    nt = clock.now() + timedelta(hours=1)
    _save({"consecutive_failures": 1, "reason": "corrupt-backoff-heal",
           "next_allowed_ts": nt.isoformat(), "set_at": clock.iso()})
    return False, f"{why}; set a 1h fixed backoff (fail closed)"


def gate_open() -> tuple[bool, str]:
    """(True, "") if a claude call is allowed now; (False, reason) if still backing off."""
    path = _backoff_state()
    if not path.exists():
        return True, ""                         # no backoff recorded -> allowed
    try:
        st = json.loads(path.read_text())
    except (json.JSONDecodeError, ValueError, OSError):
        return _heal_corrupt("backoff.json unreadable")
    nxt = st.get("next_allowed_ts")
    if not nxt:
        return True, ""
    try:
        nt = datetime.fromisoformat(nxt)
    except (TypeError, ValueError):
        return _heal_corrupt("backoff.json next_allowed_ts unreadable")
    if clock.now() >= nt:
        return True, ""
    return False, f"deferred until {nxt} (reason={st.get('reason')}, n={st.get('consecutive_failures')})"


def record_failure(reason: str, http=None) -> dict:
    st = _load()
    n = int(st.get("consecutive_failures", 0)) + 1
    hours = SCHEDULE_HOURS[min(n - 1, len(SCHEDULE_HOURS) - 1)]
    nxt = clock.now() + timedelta(hours=hours)
    out = {
        "consecutive_failures": n,
        "reason": reason,
        "last_status": http,
        "next_allowed_ts": nxt.isoformat(),
        "set_at": clock.iso(),
    }
    _save(out)
    return out


def record_success() -> None:
    if _load().get("consecutive_failures"):
        _save({"consecutive_failures": 0, "cleared_at": clock.iso()})
