"""Read-only token/cost roll-up from committed journals. Zero claude, zero network, so it is
correct on the mirror (and safe to run anywhere). The per-tick `usage` block that tick_pipeline
writes into journal.jsonl is the source of truth; ticks predating token accounting still contribute
their tick count and legacy `cost_notional` but carry no token split.

One home for the aggregation + text rendering shared by `cagentctl usage`, `bin/usage-all.sh`,
and the daily fleet usage email (`cagentctl usage-email`).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cagent import atomicio, clock, config

FIELDS = ("input", "output", "cache_read", "cache_creation")


def _parse_ts(ts):
    try:
        d = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _blank() -> dict:
    d = {f: 0 for f in FIELDS}
    d.update(ticks=0, calls=0, timeouts=0, cost_usd=0.0, total_tokens=0, by_kind={})
    return d


def aggregate(names: list | None = None, days: int | None = None) -> dict:
    """{persona_name: totals}. names=None -> all known personas (or the flat legacy state, keyed
    '(flat)', if no personas exist). days=N -> only ticks whose ts is within the last N days."""
    if names is None:
        names = config.known_personas() or [None]
    cutoff = (clock.now() - timedelta(days=days)) if days else None
    out: dict[str, dict] = {}
    for name in names:
        root = config.state_root(name)
        agg = _blank()
        for e in atomicio.read_jsonl(root / "journal.jsonl"):
            if e.get("kind") != "tick":
                continue
            if cutoff:
                d = _parse_ts(e.get("ts"))
                if d and d < cutoff:
                    continue
            agg["ticks"] += 1
            u = e.get("usage")
            if not isinstance(u, dict):
                agg["cost_usd"] += float(e.get("cost_notional") or 0)   # legacy pre-accounting tick
                continue
            for f in FIELDS:
                agg[f] += int(u.get(f, 0) or 0)
            agg["calls"] += int(u.get("calls", 0) or 0)
            agg["timeouts"] += int(u.get("timeouts", 0) or 0)
            agg["cost_usd"] += float(u.get("cost_usd", 0) or 0)
            for kind, bk in (u.get("by_kind") or {}).items():
                d2 = agg["by_kind"].setdefault(
                    kind, {**{f: 0 for f in FIELDS}, "cost_usd": 0.0, "calls": 0, "timeouts": 0})
                for f in FIELDS:
                    d2[f] += int(bk.get(f, 0) or 0)
                d2["calls"] += int(bk.get("calls", 0) or 0)
                d2["timeouts"] += int(bk.get("timeouts", 0) or 0)
                d2["cost_usd"] += float(bk.get("cost_usd", 0) or 0)
        agg["total_tokens"] = sum(agg[f] for f in FIELDS)
        agg["cost_usd"] = round(agg["cost_usd"], 6)
        out[name or "(flat)"] = agg
    return out


def render_text(per: dict, days: int | None = None, by_kind: bool = False) -> str:
    span = f"last {days}d" if days else "all time"
    hdr = (f"{'persona':10} {'ticks':>6} {'calls':>6} {'input':>12} {'output':>10} "
           f"{'cache_rd':>12} {'total':>13} {'cost$':>9}")
    lines = [f"cagent token usage ({span})", "", hdr, "-" * len(hdr)]
    tot = _blank()
    for name, a in sorted(per.items()):
        lines.append(f"{name:10} {a['ticks']:>6} {a['calls']:>6} {a['input']:>12,} {a['output']:>10,} "
                     f"{a['cache_read']:>12,} {a['total_tokens']:>13,} {a['cost_usd']:>9.4f}")
        if by_kind:
            for kind, bk in sorted(a["by_kind"].items()):
                kt = sum(bk[f] for f in FIELDS)
                to = f"  ({bk['timeouts']} timed out)" if bk.get("timeouts") else ""
                lines.append(f"    {kind:12} {bk['calls']:>5} calls  {kt:>13,} tok  "
                             f"${bk['cost_usd']:.4f}{to}")
        for f in FIELDS:
            tot[f] += a[f]
        for k in ("ticks", "calls", "timeouts", "total_tokens", "cost_usd"):
            tot[k] += a[k]
    if len(per) > 1:
        lines.append("-" * len(hdr))
        lines.append(f"{'TOTAL':10} {tot['ticks']:>6} {tot['calls']:>6} {tot['input']:>12,} "
                     f"{tot['output']:>10,} {tot['cache_read']:>12,} {tot['total_tokens']:>13,} "
                     f"{tot['cost_usd']:>9.4f}")
    if tot["timeouts"]:
        # Timed-out calls are killed before their JSON envelope arrives, so their tokens/cost read as
        # 0 -- surface the count (with per-persona attribution) so the totals aren't silently low.
        who = ", ".join(f"{n} {a['timeouts']}" for n, a in sorted(per.items()) if a.get("timeouts"))
        lines += ["", f"{tot['timeouts']} call(s) timed out (tokens/cost unrecorded for those, so "
                      f"totals undercount): {who}"]
    return "\n".join(lines)


def build_email(days: int = 1, extra: str | None = None) -> tuple[str, str]:
    """(subject, plain-text body) for the daily fleet usage email, across ALL personas.

    `extra` is an already-formatted text block appended verbatim below the table (e.g. the
    account-level Anthropic rate-limit snapshot from bin/oauth-usage.sh). The caller supplies it as
    text so this module stays hermetic -- no network, correct on the mirror -- rather than making
    the live OAuth call itself."""
    per = aggregate(days=days)
    total = round(sum(a["cost_usd"] for a in per.values()), 4)
    toks = sum(a["total_tokens"] for a in per.values())
    subject = f"cagent usage: last {days}d — {toks:,} tokens, ${total:.4f}"
    body = (render_text(per, days=days, by_kind=True)
            + "\n\nTokens/cost summed from each persona's committed tick journal. "
              "cache_rd = cached-input reads (billed at a discount).")
    if extra and extra.strip():
        body += "\n\n" + extra.strip() + "\n"
    return subject, body
