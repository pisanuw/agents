#!/usr/bin/env bash
# diag-reject.sh : how did a persona come to reference a rejected draft?
# READ-ONLY. Run from the cagent repo root on the LIVE host.
#   bash diag-reject.sh <persona> <new_token> <old_token>
#
# new_token = the "what did I miss?" draft the persona just staged
# old_token = the rejected draft it claims to know about
set -uo pipefail

P="${1:?usage: diag-reject.sh <persona> <new_token> <old_token>}"
NEW="${2:?new_token required}"
OLD="${3:?old_token required}"
S="state/personas/$P"

hr(){ printf '\n==== %s ====\n' "$1"; }

hr "environment"
echo "persona=$P  new=$NEW  old=$OLD"
echo "HEAD=$(git rev-parse --short HEAD 2>/dev/null)  branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
[ -d "$S" ] || { echo "!! $S missing (wrong persona or wrong cwd)"; exit 1; }

hr "1. every occurrence of OLD token ($OLD) under $S"
grep -rn "$OLD" "$S" 2>/dev/null || echo "  (NOT FOUND anywhere in $S)"

hr "2. every occurrence of NEW token ($NEW) under $S"
grep -rn "$NEW" "$S" 2>/dev/null || echo "  (NOT FOUND)"

hr "3. journal ticks that STAGED either token (results[].token)"
python3 - "$S/journal.jsonl" "$NEW" "$OLD" <<'PY'
import json,sys
path,new,old=sys.argv[1],sys.argv[2],sys.argv[3]
try: lines=open(path).read().splitlines()
except FileNotFoundError: print("  (no journal.jsonl)"); sys.exit()
for ln in lines:
    try: e=json.loads(ln)
    except Exception: continue
    toks=[r.get("token") for r in (e.get("results") or []) if isinstance(r,dict)]
    hit=[t for t in toks if t in (new,old)]
    if hit:
        print(f"  tick_id={e.get('tick_id')}  ts={e.get('ts','')[:19]}  staged={hit}")
        print(f"     summary: {(e.get('summary') or '')[:160]}")
PY

hr "4. the tick that produced NEW ($NEW): what did the persona SEE?"
TID=$(python3 - "$S/journal.jsonl" "$NEW" <<'PY'
import json,sys
path,new=sys.argv[1],sys.argv[2]
try: lines=open(path).read().splitlines()
except FileNotFoundError: sys.exit()
for ln in lines:
    try: e=json.loads(ln)
    except Exception: continue
    if any(isinstance(r,dict) and r.get("token")==new for r in (e.get("results") or [])):
        print(e.get("tick_id","")); break
PY
)
if [ -z "$TID" ]; then
  TID=$(grep -rl "$NEW" "$S"/ticks/*/actions.json 2>/dev/null | head -1 | awk -F/ '{print $(NF-1)}')
fi
echo "  tick_id = ${TID:-<none found>}"
TD="$S/ticks/$TID"
if [ -n "$TID" ] && [ -d "$TD" ]; then
  echo "  -- decision.json (chosen actions + summary) --"
  python3 - "$TD/decision.json" <<'PY'
import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception as e: print(f"   ({e})"); sys.exit()
print("   summary:", (d.get("summary") or "")[:200])
for a in (d.get("actions") or []):
    if a.get("type")=="send_email":
        print(f"   send_email subject={a.get('subject','')!r}")
        print(f"     body[:200]={(a.get('body') or '')[:200]!r}")
    else:
        print(f"   {a.get('type')}")
PY
  echo
  echo "  -- SMOKING GUN: does this tick's context.txt contain OLD ($OLD)? --"
  if grep -n -C2 "$OLD" "$TD/context.txt" 2>/dev/null; then
    echo "  >>> the persona SAW $OLD above. Note WHICH section (NEW MAIL / SELECTED MEMORY /"
    echo "      RECENT JOURNAL / OWNER STEERING) it sits in: that is the leak channel."
  else
    echo "  >>> $OLD is NOT in this tick's context.txt."
    echo "  >>> The token was invented: the whole 'ceeeac6d rejected' claim is confabulated."
  fi
  echo
  echo "  -- context.txt lines about the draft topic / rejection --"
  grep -ni "reject\|meaning\|bibliograph\|borrowed" "$TD/context.txt" 2>/dev/null | head -20
else
  echo "  (no tick dir on disk for '$TID'; it may be older than retained ticks)"
fi

hr "5. email-reject path: did any received email ever mention OLD ($OLD)?"
grep -rln "$OLD" "$S/emails/received" 2>/dev/null \
  || echo "  (no received email mentions $OLD  ->  rules OUT an email REJECT)"

hr "6. reasoned-reject path: any rejection note in memory?"
grep -rin "reject" "$S/memory/index.jsonl" 2>/dev/null \
  || echo "  (no 'reject' entries in memory index  ->  rules OUT a reasoned-reject memory note)"

hr "verdict guide"
cat <<'TXT'
  - OLD found in a received email (step 1/5)      : you rejected/referenced it by EMAIL; the
                                                    persona read it as inbound mail (expected).
  - OLD found only in a memory note (step 1/6)    : a reasoned reject (REJECT <tok>: reason) or
                                                    reflection captured it (expected).
  - OLD in the producing tick's context.txt (4)   : trace the section: that is how it leaked.
  - OLD nowhere / NOT in that context.txt         : terminal `cagentctl reject` gave zero signal;
                                                    the rejection claim is a CONFABULATION that
                                                    passed the gate-check. That is the finding.
TXT
