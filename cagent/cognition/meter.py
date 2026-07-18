"""Per-tick token/cost meter. Every `claude` subprocess funnels through invoke.run_claude, which
records ONE row here (cognition, gate-check, revise, research, reflection alike). tick_pipeline
resets at the top of a tick and drains into the journal line + ticks/<id>/usage.json. A tick runs
as its own dispatcher subprocess, so this module-level accumulator is scoped to exactly one tick
and can never bleed across personas -- reset() is belt-and-suspenders, not correctness.

Kept deliberately dependency-free (stdlib only) so both invoke and parse can import it with no
cycle. The envelope key -> short-name map lives here as the single source of truth for token names.
"""
from __future__ import annotations

# claude JSON-envelope `usage` key -> our short field name (the single naming source of truth).
_TOKEN_KEYS = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_creation_input_tokens": "cache_creation",
    "cache_read_input_tokens": "cache_read",
}
_FIELDS = tuple(_TOKEN_KEYS.values())

_ROWS: list[dict] = []


def usage_fields(envelope: dict) -> dict | None:
    """Normalize the envelope's `usage` block to our short field names, or None if absent.
    Defensive: the exact key set has drifted across claude CLI versions, so a missing key is 0."""
    u = envelope.get("usage") if isinstance(envelope, dict) else None
    if not isinstance(u, dict):
        return None
    return {short: int(u.get(k) or 0) for k, short in _TOKEN_KEYS.items()}


def reset() -> None:
    _ROWS.clear()


def record(kind: str, model: str, usage: dict | None, cost_usd, ms: int | None = None,
           timed_out: bool = False) -> None:
    """Append one claude call. Always records (even a token-less failed/empty call) so `calls`
    reflects the true number of subprocess invocations this tick. `timed_out` flags a call the
    subprocess was killed before it emitted its JSON envelope: it still counts as a call, but its
    token/cost fields are 0 (the usage block never arrived), so the meter would silently undercount
    unless the timeout is surfaced separately."""
    row = {"kind": kind or "?", "model": model or "", "ms": ms,
           "cost_usd": float(cost_usd) if cost_usd is not None else 0.0,
           "timed_out": bool(timed_out)}
    for f in _FIELDS:
        row[f] = int((usage or {}).get(f, 0) or 0)
    _ROWS.append(row)


def summary() -> dict:
    """Aggregate the current rows: grand totals + a per-kind breakdown + call count."""
    agg = {f: 0 for f in _FIELDS}
    agg.update(cost_usd=0.0, calls=len(_ROWS), timeouts=0)
    by_kind: dict[str, dict] = {}
    for r in _ROWS:
        bk = by_kind.setdefault(r["kind"],
                                {**{f: 0 for f in _FIELDS}, "cost_usd": 0.0, "calls": 0, "timeouts": 0})
        for f in _FIELDS:
            agg[f] += r[f]
            bk[f] += r[f]
        agg["cost_usd"] += r["cost_usd"]
        bk["cost_usd"] += r["cost_usd"]
        bk["calls"] += 1
        if r.get("timed_out"):
            agg["timeouts"] += 1
            bk["timeouts"] += 1
    agg["total_tokens"] = sum(agg[f] for f in _FIELDS)
    agg["cost_usd"] = round(agg["cost_usd"], 6)
    for bk in by_kind.values():
        bk["cost_usd"] = round(bk["cost_usd"], 6)
    agg["by_kind"] = by_kind
    return agg


def drain() -> tuple[dict, list[dict]]:
    """Return (summary, per-call rows) and clear the meter."""
    s = summary()
    rows = list(_ROWS)
    _ROWS.clear()
    return s, rows
