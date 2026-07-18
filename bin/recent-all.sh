#!/usr/bin/env bash
# Merged recent-activity dashboard across ALL personas (not a per-persona loop):
#   1) every persona and its current mode (enabled = runs in the round-robin; draft = not)
#   2) all recent ticks from every persona, merged newest-first, each line TAGGED with its
#      persona. The dispatcher is round-robin, so consecutive ticks belong to different
#      personas; the tag is what tells them apart.
# Goals and memory notes are intentionally omitted (use `cagentctl recent --persona <p>` for those).
# Optional arg: tick count to show (default 30), or `all` for every tick. e.g. bin/recent-all.sh 50
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

exec "$PY" - "$@" <<'PY'
import json
import sys
from datetime import datetime, timezone

from cagent import config

# ticks are stored as aware UTC (clock.now); display everything in the viewer's local zone.
LOCAL_TZ = datetime.now().astimezone().tzname() or "local"

# arg parsing: a number caps the tick list; `all` removes the cap; default 30.
limit = 30
for a in sys.argv[1:]:
    if a.isdigit():
        limit = int(a)
    elif a.lower() == "all":
        limit = 0  # 0 => no cap

root = config.REPO_ROOT
known = config.known_personas()
enabled = set(config.enabled_personas())

# 1) personas and their current mode -------------------------------------------------
print("personas (current mode):")
if not known:
    print("  (none)")
for p in known:
    try:
        mode = config.load(p).MODE
    except Exception:
        mode = "?"
    print(f"  {p:9s} {mode:10s} {'enabled' if p in enabled else 'draft'}")


def parse_ts(ts):
    """ISO-8601 journal ts (stored as aware UTC by clock.now) -> aware datetime in the viewer's
    LOCAL timezone. A naive value is assumed UTC (matching clock.now), never local."""
    try:
        d = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone()   # convert to local zone for display


def fmt_ts(ts):
    d = parse_ts(ts)
    return d.strftime("%m-%d %H:%M") if d else (ts or "")[:16]


# 2) merged ticks across every persona, newest first ---------------------------------
ticks = []
for p in known:
    jp = root / "state" / "personas" / p / "journal.jsonl"
    if not jp.exists():
        continue
    for line in jp.read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("kind") == "tick":
            e["_p"] = p
            ticks.append(e)

# sort by a float epoch so mixed/odd timestamps never raise (naive vs aware comparison).
ticks.sort(key=lambda e: (parse_ts(e.get("ts")).timestamp() if parse_ts(e.get("ts")) else 0.0),
           reverse=True)

shown = ticks if limit == 0 else ticks[:limit]
span = "all" if limit == 0 else f"{len(shown)} of {len(ticks)}"
print(f"\nrecent ticks ({span}, newest first, times in {LOCAL_TZ}):")
if not ticks:
    print("  (no ticks yet)")
for e in shown:
    mark = "ok " if e.get("ok") else "ERR"
    acts = ",".join(e.get("actions") or []) or e.get("status", "")
    summ = (e.get("summary") or e.get("status") or "").replace("\n", " ")
    print(f"  {fmt_ts(e.get('ts'))}  {e['_p']:8s} {mark} [{acts}] {summ[:80]}")
PY
