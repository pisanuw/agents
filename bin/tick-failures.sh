#!/usr/bin/env bash
# Why ticks failed. Counts ok:false ticks by status across personas. TIMEOUT / RATE_LIMIT /
# API_ERROR are transport/throttle no-ops (the claude call never produced a decision), NOT the
# agent making a bad call -- so they speak to claude-CLI reliability, not the agent's judgment.
# Optional args (any order): a persona name to focus on; a number to also LIST that many of the
# most-recent failures with timestamps. e.g. bin/tick-failures.sh scout 10
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

exec "$PY" - "$@" <<'PY'
import json
import sys
from collections import Counter
from datetime import datetime, timezone

from cagent import config

args = sys.argv[1:]
only = next((a for a in args if not a.isdigit()), None)
listn = next((int(a) for a in args if a.isdigit()), 0)


def jl(p):
    out = []
    if p.exists():
        for ln in p.read_text().splitlines():
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
    return out


def loc(ts):
    try:
        d = datetime.fromisoformat(ts)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone().strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return "-"


personas = [only] if only else config.known_personas()
allfail = []
for name in personas:
    sr = config.REPO_ROOT / "state" / "personas" / name
    bad = [e for e in jl(sr / "journal.jsonl") if e.get("kind") == "tick" and not e.get("ok")]
    for e in bad:
        e["_p"] = name
    allfail += bad
    c = Counter(e.get("status") or "NO_STATUS" for e in bad)
    detail = ", ".join(f"{k}×{v}" for k, v in c.most_common()) if c else "(clean)"
    print(f"{name:9} {len(bad):>3} failed:  {detail}")

total = Counter(e.get("status") or "NO_STATUS" for e in allfail)
print("\nall personas, by status: " + (", ".join(f"{k}×{v}" for k, v in total.most_common()) or "none"))
print("TIMEOUT / RATE_LIMIT / API_ERROR = transport no-ops (claude call failed/throttled), not bad decisions.")

if listn:
    allfail.sort(key=lambda e: e.get("ts", ""), reverse=True)
    print(f"\n{min(listn, len(allfail))} most-recent failures:")
    for e in allfail[:listn]:
        print(f"  {loc(e.get('ts'))}  {e['_p']:8} {e.get('status')}")
PY
