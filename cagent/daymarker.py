"""Once-per-day dedup markers — the single home for the "did this already happen today?" idiom.

A day-marker records that some periodic action fired on the current calendar day (clock.today()),
so it runs at most once per day. Two on-disk shapes share this one primitive:
  - the PLAIN marker (a file holding today's date) — daily push, daily digest, daily usage email;
  - the JSON marker ({"date": today, **payload}) — alerts that also carry WHY they fired (health
    alert kinds, gate-stall streak) and are cleared when the condition resolves.

The load-bearing invariant, previously re-derived at five call sites: write the marker only AFTER
the action succeeds (mark()), so a failed/refused action retries on the next run rather than
silently burning the day. A torn/unreadable marker reads as "not done today" — fail toward retrying.
"""
from __future__ import annotations

import json

from cagent import clock


def done_today(path) -> bool:
    """True if `path` records today's date. Accepts both the plain (bare date) and JSON
    ({"date": ...}) shapes; missing/empty/torn markers return False (so the action retries)."""
    if not path.exists():
        return False
    try:
        raw = path.read_text().strip()
    except OSError:
        return False
    if not raw:
        return False
    if raw[:1] == "{":
        try:
            return json.loads(raw).get("date") == clock.today()
        except (json.JSONDecodeError, ValueError):
            return False
    return raw == clock.today()


def mark(path, **payload) -> None:
    """Record today's date at `path`. No payload -> writes the bare date (plain marker); with
    payload -> writes {"date": today, **payload} (JSON marker recording why it fired)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": clock.today(), **payload}) if payload else clock.today())


def clear(path) -> None:
    """Remove the marker: the condition resolved, so the next occurrence may fire again."""
    path.unlink(missing_ok=True)
