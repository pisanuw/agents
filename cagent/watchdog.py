"""Watchdog + maintenance. Health is alerted on a TRANSPORT-INDEPENDENT channel (a loud
local flag file + a macOS notification) PLUS email when Gmail is reachable, so a total comms
outage still surfaces over months. The watchdog depends on neither claude nor a healthy tick.
"""
from __future__ import annotations

import gzip
import json
import shutil
import subprocess
from datetime import datetime, timedelta

from cagent import atomicio, clock, config, daymarker, gmail

VAR = config.REPO_ROOT / "var"
LOGS = config.REPO_ROOT / "logs"
ALERT = VAR / "ALERT"
LAST_TICK = VAR / "last_tick.json"
OUTBOX = VAR / "outbox"


def _health_alert_flag():
    """Once-per-day dedup marker for health alerts (mirrors _gate_stall_flag in supervise)."""
    return config.state_root() / "health_alert.json"


def _backoff_paths() -> list[tuple[str, object]]:
    """(persona-label, backoff.json path) for every namespace to health-check. In the multi-persona
    layout each persona's backoff lives at state/personas/<name>/backoff.json; a single import-frozen
    path (the watchdog process sets no CAGENT_PERSONA) would silently never see any real persona's
    rate/auth backoff. Iterate the enabled roster, falling back to the flat legacy namespace."""
    personas = config.enabled_personas()
    if personas:
        return [(p, config.state_root(p) / "backoff.json") for p in personas]
    return [("", config.state_root() / "backoff.json")]


def _macos_notify(title: str, msg: str) -> None:
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification "{msg[:180]}" with title "{title[:60]}"'],
                       capture_output=True, timeout=10)
    except Exception:
        pass


def alert(kind: str, message: str, cfg, log) -> bool:
    """Emit a health alert. Returns True iff the owner EMAIL was actually sent; the local alert-log
    line and the macOS notification are always written regardless. The caller uses the return to
    decide whether to record today's dedupe -- an UNDELIVERED alert must NOT be marked 'alerted
    today', else an auth-lapse could stay email-silent for a full day (precisely when caps/quiet are
    likely engaged). kind='alert' is cap-exempt (CAP_EXEMPT_KINDS) so a full send cap cannot swallow
    a health alarm."""
    line = f"{clock.iso()} [{kind}] {message}"
    ALERT.parent.mkdir(parents=True, exist_ok=True)
    with open(ALERT, "a") as f:
        f.write(line + "\n")
    _macos_notify(f"cagent: {kind}", message)
    emailed = False
    try:  # email is best-effort for the local log/notify, but its delivery drives the dedupe
        gmail.send(subject=f"[cagent watchdog] {kind}", body_md=message, kind="alert")
        emailed = True
    except Exception as e:
        log.info("watchdog email alert not delivered (will retry next tick): %s", e)
    log.info("ALERT %s: %s", kind, message)
    return emailed


def check_health(cfg, log, max_idle_h: float = 2.0) -> list[dict]:
    issues: list[dict] = []

    # atomicio.read_json tolerates a missing/torn file (returns the default), replacing the
    # exists()-then-read()-then-except idiom with the codebase's standard tolerant read.
    age = clock.hours_since(atomicio.read_json(LAST_TICK, {}).get("ts", ""))
    if age is not None and age > max_idle_h:
        issues.append({"kind": "stale-heartbeat", "msg": f"no successful tick in {age:.1f}h"})

    for label, path in _backoff_paths():
        if not path.exists():
            continue
        try:
            d = json.loads(path.read_text())
        except json.JSONDecodeError:
            d = {}
        who = f"[{label}] " if label else ""
        reason = str(d.get("reason", "")).lower()
        if "auth" in reason:
            issues.append({"kind": "auth-lapse", "msg": f"{who}claude CLI auth appears to have lapsed; re-login needed"})
        elif int(d.get("consecutive_failures", 0)) >= 3:
            issues.append({"kind": "repeated-failures", "msg": f"{who}{d.get('consecutive_failures')} consecutive failures ({d.get('reason')})"})

    flag = _health_alert_flag()
    if issues:
        if daymarker.done_today(flag):
            log.info("watchdog: %d issue(s) suppressed (already alerted today)", len(issues))
        else:
            delivered = [alert(i["kind"], i["msg"], cfg, log) for i in issues]
            # Only record the daily dedupe once EVERY alert email actually went out. If any was
            # refused (cap/quiet/transient), leave the flag unset so the next tick retries instead of
            # going silent for the rest of the day (daymarker's own rule: mark only after success).
            if all(delivered):
                daymarker.mark(flag, kinds=[i["kind"] for i in issues])
            else:
                log.info("watchdog: %d/%d alert email(s) undelivered; not marking dedupe (will retry)",
                         delivered.count(False), len(issues))
    else:
        daymarker.clear(flag)
        log.info("watchdog: healthy")
    return issues


# ------------------------------- maintenance ------------------------------- #

def rotate_logs(keep_days: int = 14) -> int:
    n = 0
    if not LOGS.exists():
        return 0
    for p in LOGS.glob("tick-*.log"):
        age = clock.hours_since(_date_from_logname(p.name))
        if age is not None and age > keep_days * 24:
            with open(p, "rb") as src, gzip.open(str(p) + ".gz", "wb") as dst:
                shutil.copyfileobj(src, dst)
            p.unlink()
            n += 1
    return n


def _date_from_logname(name: str) -> str:
    # tick-YYYY-MM-DD.log -> ISO midnight. Pure slicing (never raises), so no try/except is needed.
    return name[len("tick-"):-len(".log")] + "T00:00:00+00:00"


def prune_ticks(keep_days: int = 30) -> int:
    """Delete per-tick audit dirs older than keep_days across all persona state dirs.
    At ~33 dirs/day, 30-day retention caps steady-state at ~1000 dirs instead of unbounded growth."""
    now_naive = clock.now().replace(tzinfo=None)
    cutoff = now_naive - timedelta(days=keep_days)
    n = 0
    personas_root = config.personas_state_root()
    if not personas_root.exists():
        return 0
    for ticks_dir in personas_root.glob("*/ticks"):
        if not ticks_dir.is_dir():
            continue
        for d in ticks_dir.iterdir():
            if not d.is_dir():
                continue
            try:
                ts = datetime.strptime(d.name, "%Y%m%dT%H%M%S")
                if ts < cutoff:
                    shutil.rmtree(d)
                    n += 1
            except ValueError:
                pass
    return n


def cap_outbox(keep: int = 200) -> int:
    if not OUTBOX.exists():
        return 0
    files = sorted(OUTBOX.glob("*.json"))
    extra = files[:-keep] if len(files) > keep else []
    for p in extra:
        p.unlink()
    return len(extra)


def prune_processed(keep_days: int = 90) -> int:
    """Delete archived control/processed/<date>/ dirs older than keep_days. These are an audit trail
    ONLY -- directive dedup uses the seen-ledger (control._load_seen), never these files -- so pruning
    them is safe and bounds the otherwise-unbounded git-control archive."""
    processed = config.REPO_ROOT / "control" / "processed"
    if not processed.exists():
        return 0
    cutoff = clock.now().replace(tzinfo=None) - timedelta(days=keep_days)
    n = 0
    for d in processed.iterdir():
        if not d.is_dir():
            continue
        try:
            if datetime.strptime(d.name, "%Y-%m-%d") < cutoff:
                shutil.rmtree(d)
                n += 1
        except ValueError:
            pass
    return n


def run_maintenance(log) -> dict:
    rotated = rotate_logs()
    capped = cap_outbox()
    pruned = prune_ticks()
    pruned_processed = prune_processed()
    log.info("maintenance: rotated %d logs, capped %d outbox files, pruned %d tick dirs, "
             "%d processed-control dirs", rotated, capped, pruned, pruned_processed)
    return {"rotated_logs": rotated, "capped_outbox": capped, "pruned_ticks": pruned,
            "pruned_processed": pruned_processed}


def main() -> int:
    from cagent import logging_setup
    cfg = config.load()
    log = logging_setup.setup()
    issues = check_health(cfg, log)
    run_maintenance(log)
    print("watchdog:", {"issues": issues})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
