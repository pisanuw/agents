"""watchdog maintenance + the check_health branches the existing suite doesn't reach: log rotation,
tick/outbox pruning, run_maintenance aggregation, main(), the macOS notifier, _backoff_paths' flat
fallback, and the repeated-failures / already-alerted-today paths of check_health.
"""
from __future__ import annotations

import json
import logging

from cagent import config, watchdog

log = logging.getLogger("t")


# --- helpers --------------------------------------------------------------- #

def test_date_from_logname():
    assert watchdog._date_from_logname("tick-2026-07-03.log") == "2026-07-03T00:00:00+00:00"


def test_backoff_paths_flat_fallback(monkeypatch):
    monkeypatch.setattr(watchdog.config, "enabled_personas", lambda: [])
    paths = watchdog._backoff_paths()
    assert len(paths) == 1 and paths[0][0] == ""            # legacy flat namespace


def test_backoff_paths_per_persona(monkeypatch):
    monkeypatch.setattr(watchdog.config, "enabled_personas", lambda: ["a", "b"])
    assert [lbl for lbl, _ in watchdog._backoff_paths()] == ["a", "b"]


def test_macos_notify_runs_and_swallows(monkeypatch):
    calls = []
    monkeypatch.setattr(watchdog.subprocess, "run", lambda *a, **k: calls.append(a))
    watchdog._macos_notify("title", "msg")
    assert calls

    def boom(*a, **k):
        raise RuntimeError("no osascript")
    monkeypatch.setattr(watchdog.subprocess, "run", boom)
    watchdog._macos_notify("t", "m")                        # exception swallowed, no raise


# --- check_health branches ------------------------------------------------- #

def _wire_health(monkeypatch, tmp_path, backoff):
    bo = tmp_path / "backoff.json"
    bo.write_text(json.dumps(backoff))
    monkeypatch.setattr(watchdog, "LAST_TICK", tmp_path / "nope.json")
    monkeypatch.setattr(watchdog, "_backoff_paths", lambda: [("", bo)])
    monkeypatch.setattr(watchdog, "ALERT", tmp_path / "ALERT")
    monkeypatch.setattr(watchdog, "_health_alert_flag", lambda: tmp_path / "flag.json")
    monkeypatch.setattr(watchdog, "_macos_notify", lambda *a, **k: None)


def test_repeated_failures_alerts(monkeypatch, tmp_path):
    _wire_health(monkeypatch, tmp_path, {"reason": "RATE_LIMIT", "consecutive_failures": 5})
    monkeypatch.setattr(watchdog.gmail, "send", lambda **k: None)
    issues = watchdog.check_health(config.load(), log)
    assert any(i["kind"] == "repeated-failures" for i in issues)


def test_issues_suppressed_when_already_alerted_today(monkeypatch, tmp_path):
    _wire_health(monkeypatch, tmp_path, {"reason": "AUTH_ERROR", "consecutive_failures": 1})
    monkeypatch.setattr(watchdog.daymarker, "done_today", lambda f: True)     # already alerted today
    sent = []
    monkeypatch.setattr(watchdog.gmail, "send", lambda **k: sent.append(k))
    issues = watchdog.check_health(config.load(), log)
    assert issues and sent == []                            # issue detected but no duplicate alert


# --- maintenance ----------------------------------------------------------- #

def test_rotate_logs_gzips_old_keeps_recent(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(watchdog, "LOGS", logs)
    (logs / "tick-2020-01-01.log").write_text("old log")
    (logs / "tick-2099-01-01.log").write_text("future log")   # negative age -> kept
    assert watchdog.rotate_logs(keep_days=14) == 1
    assert not (logs / "tick-2020-01-01.log").exists()
    assert (logs / "tick-2020-01-01.log.gz").exists()
    assert (logs / "tick-2099-01-01.log").exists()


def test_rotate_logs_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "LOGS", tmp_path / "nope")
    assert watchdog.rotate_logs() == 0


def test_prune_ticks_removes_old_dirs(tmp_path, monkeypatch):
    proot = tmp_path / "personas"
    monkeypatch.setattr(config, "personas_state_root", lambda: proot)
    tdir = proot / "alpha" / "ticks"
    tdir.mkdir(parents=True)
    (tdir / "20200101T000000").mkdir()                    # old -> removed
    (tdir / "20990101T000000").mkdir()                    # future -> kept
    (tdir / "not-a-tick").mkdir()                         # unparseable name -> skipped
    (tdir / "afile.txt").write_text("x")                  # non-dir -> skipped
    assert watchdog.prune_ticks(keep_days=30) == 1
    assert not (tdir / "20200101T000000").exists()
    assert (tdir / "20990101T000000").exists() and (tdir / "not-a-tick").exists()


def test_prune_ticks_missing_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "personas_state_root", lambda: tmp_path / "nope")
    assert watchdog.prune_ticks() == 0


def test_cap_outbox_keeps_newest(tmp_path, monkeypatch):
    ob = tmp_path / "outbox"
    ob.mkdir()
    monkeypatch.setattr(watchdog, "OUTBOX", ob)
    for i in range(5):
        (ob / f"{i:02d}.json").write_text("x")
    assert watchdog.cap_outbox(keep=3) == 2                # two oldest removed
    assert (ob / "04.json").exists() and not (ob / "00.json").exists()


def test_cap_outbox_under_keep_and_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "OUTBOX", tmp_path / "nope")
    assert watchdog.cap_outbox() == 0                      # missing dir
    ob = tmp_path / "ob2"
    ob.mkdir()
    monkeypatch.setattr(watchdog, "OUTBOX", ob)
    (ob / "a.json").write_text("x")
    assert watchdog.cap_outbox(keep=200) == 0             # fewer than keep -> nothing removed


def test_run_maintenance_aggregates(monkeypatch):
    monkeypatch.setattr(watchdog, "rotate_logs", lambda: 1)
    monkeypatch.setattr(watchdog, "cap_outbox", lambda: 2)
    monkeypatch.setattr(watchdog, "prune_ticks", lambda: 3)
    monkeypatch.setattr(watchdog, "prune_processed", lambda: 4)
    assert watchdog.run_maintenance(log) == {
        "rotated_logs": 1, "capped_outbox": 2, "pruned_ticks": 3, "pruned_processed": 4}


def test_main_runs_health_and_maintenance(monkeypatch, capsys):
    from cagent import logging_setup
    monkeypatch.setattr(watchdog.config, "load", lambda *a, **k: object())
    monkeypatch.setattr(logging_setup, "setup", lambda: log)
    monkeypatch.setattr(watchdog, "check_health", lambda cfg, lg: [])
    monkeypatch.setattr(watchdog, "run_maintenance", lambda lg: {})
    assert watchdog.main() == 0
    assert "watchdog" in capsys.readouterr().out
