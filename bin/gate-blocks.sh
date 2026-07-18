#!/usr/bin/env bash
# What the fact-check ("gate") turned back before it reached the owner: every draft a persona
# tried to send that the gate-check blocked, newest first, with the SPECIFIC reasons it flagged
# (fabrication / false_victory / hidden_failure / metaphor_leak / safety / missing disclosure).
# A high count means the guard is WORKING, but it also marks a persona that overreaches on facts.
# Optional args (any order): a persona name to focus on; a number to cap how many to show (default 12).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

exec "$PY" - "$@" <<'PY'
import json
import sys
import textwrap
from datetime import datetime, timezone

from cagent import config

args = sys.argv[1:]
only = next((a for a in args if not a.isdigit()), None)
cap = next((int(a) for a in args if a.isdigit()), 12)

FLAGS = ("fabrication", "false_victory", "hidden_failure", "metaphor_leak", "safety")


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


rows = []
for name in ([only] if only else config.known_personas()):
    sr = config.REPO_ROOT / "state" / "personas" / name
    for e in jl(sr / "journal.jsonl"):
        if e.get("kind") != "tick":
            continue
        for r in (e.get("results") or []):
            v = r.get("blocked_by_gate")
            if v:
                rows.append((e.get("ts", ""), name, v))

rows.sort(key=lambda t: t[0], reverse=True)
print(f"gate-blocked drafts ({min(cap, len(rows))} of {len(rows)}, newest first):\n")
for ts, name, v in rows[:cap]:
    print(f"{loc(ts)}  {name}  (verdict={v.get('verdict')})")
    reasons = [(f, v.get(f)) for f in FLAGS if v.get(f)]
    if v.get("disclosure_present") is False:
        reasons.append(("disclosure", "missing AI-disclosure footer"))
    if not reasons:
        print("    (blocked, no specific flag recorded)")
    for f, val in reasons:
        for it in (val if isinstance(val, list) else [val]):
            for j, ln in enumerate(textwrap.wrap(f"[{f}] {it}", 100)):
                print(("    " if j == 0 else "        ") + ln)
    print()
PY
