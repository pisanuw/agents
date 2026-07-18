"""Single time source. All time reads go through now() so FAST_CLOCK can compress
days into seconds for testing reflection/goal-evolution and daily-push cadence.

CAGENT_NOW       ISO-8601 override for "current" time (anchors the simulated clock).
CAGENT_TICK_SECONDS  if set with CAGENT_NOW, each call advances the simulated clock
                     by this many seconds (lets a loop march time forward fast).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

_sim_offset_calls = 0


def now() -> datetime:
    override = os.environ.get("CAGENT_NOW")
    if not override:
        return datetime.now(timezone.utc)
    global _sim_offset_calls
    base = datetime.fromisoformat(override)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    step = os.environ.get("CAGENT_TICK_SECONDS")
    if step:
        out = base + timedelta(seconds=int(step) * _sim_offset_calls)
        _sim_offset_calls += 1
        return out
    return base


def iso() -> str:
    return now().isoformat()


def today() -> str:
    return now().date().isoformat()


def hours_since(iso: str) -> float | None:
    """Hours elapsed since an ISO-8601 timestamp, using the controllable clock.
    Returns None if iso is empty, None, or unparseable. Tz-naive strings are
    treated as UTC (consistent with how stored timestamps are written via iso())."""
    try:
        ts = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    # now() is tz-aware (UTC). A tz-naive stored value (e.g. a bare "2026-06-22" date) would raise
    # TypeError on the subtraction below and be swallowed as None -- silently breaking any caller that
    # trusts the "naive == UTC" contract in this docstring. Normalize instead, matching how iso() writes.
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now() - ts).total_seconds() / 3600.0
