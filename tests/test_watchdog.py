import json
import logging

from cagent import config, watchdog

log = logging.getLogger("t")


def test_stale_heartbeat_alerts(monkeypatch, tmp_path):
    monkeypatch.setattr(watchdog, "LAST_TICK", tmp_path / "last_tick.json")
    monkeypatch.setattr(watchdog, "_backoff_paths", lambda: [("", tmp_path / "backoff.json")])
    monkeypatch.setattr(watchdog, "ALERT", tmp_path / "ALERT")
    # Isolate the once-per-day dedup marker to tmp_path: otherwise it resolves to the real
    # state/health_alert.json, leaks into the working tree, AND makes this test flaky by calendar
    # date (a second run the same day sees the dedup flag and skips alert(), so ALERT is never written).
    monkeypatch.setattr(watchdog, "_health_alert_flag", lambda: tmp_path / "health_alert.json")
    monkeypatch.setattr(watchdog, "_macos_notify", lambda *a, **k: None)
    monkeypatch.setattr(watchdog.gmail, "send", lambda **k: None)
    watchdog.LAST_TICK.write_text(json.dumps({"ts": "2020-01-01T00:00:00+00:00"}))
    issues = watchdog.check_health(config.load(), log)
    kinds = {i["kind"] for i in issues}
    assert "stale-heartbeat" in kinds
    assert (tmp_path / "ALERT").exists()


def test_auth_lapse_alerts(monkeypatch, tmp_path):
    bo = tmp_path / "backoff.json"
    monkeypatch.setattr(watchdog, "LAST_TICK", tmp_path / "nope.json")
    monkeypatch.setattr(watchdog, "_backoff_paths", lambda: [("", bo)])
    monkeypatch.setattr(watchdog, "ALERT", tmp_path / "ALERT")
    monkeypatch.setattr(watchdog, "_health_alert_flag", lambda: tmp_path / "health_alert.json")
    monkeypatch.setattr(watchdog, "_macos_notify", lambda *a, **k: None)
    monkeypatch.setattr(watchdog.gmail, "send", lambda **k: None)
    bo.write_text(json.dumps({"reason": "AUTH_ERROR", "consecutive_failures": 1}))
    issues = watchdog.check_health(config.load(), log)
    assert any(i["kind"] == "auth-lapse" for i in issues)


def test_healthy_no_issues(monkeypatch, tmp_path):
    monkeypatch.setattr(watchdog, "LAST_TICK", tmp_path / "nope.json")
    monkeypatch.setattr(watchdog, "_backoff_paths", lambda: [("", tmp_path / "nope2.json")])
    monkeypatch.setattr(watchdog, "ALERT", tmp_path / "ALERT")
    assert watchdog.check_health(config.load(), log) == []


def test_refused_alert_does_not_mark_daily_dedupe(monkeypatch, tmp_path):
    # If the alert email is refused (cap/quiet/transient), the once-per-day dedupe flag must NOT be
    # set -- otherwise an auth-lapse could stay email-silent for a full day. The next tick retries.
    flag = tmp_path / "health_alert.json"
    bo = tmp_path / "backoff.json"
    monkeypatch.setattr(watchdog, "LAST_TICK", tmp_path / "nope.json")
    monkeypatch.setattr(watchdog, "_backoff_paths", lambda: [("", bo)])
    monkeypatch.setattr(watchdog, "ALERT", tmp_path / "ALERT")
    monkeypatch.setattr(watchdog, "_health_alert_flag", lambda: flag)
    monkeypatch.setattr(watchdog, "_macos_notify", lambda *a, **k: None)
    monkeypatch.setattr(watchdog.gmail, "send",
                        lambda **k: (_ for _ in ()).throw(watchdog.gmail.SendRefused("cap")))
    bo.write_text(json.dumps({"reason": "AUTH_ERROR", "consecutive_failures": 1}))
    watchdog.check_health(config.load(), log)
    assert not flag.exists()                                  # undelivered -> not deduped, will retry
    assert (tmp_path / "ALERT").exists()                      # local alert log still written


def test_delivered_alert_marks_daily_dedupe(monkeypatch, tmp_path):
    flag = tmp_path / "health_alert.json"
    bo = tmp_path / "backoff.json"
    monkeypatch.setattr(watchdog, "LAST_TICK", tmp_path / "nope.json")
    monkeypatch.setattr(watchdog, "_backoff_paths", lambda: [("", bo)])
    monkeypatch.setattr(watchdog, "ALERT", tmp_path / "ALERT")
    monkeypatch.setattr(watchdog, "_health_alert_flag", lambda: flag)
    monkeypatch.setattr(watchdog, "_macos_notify", lambda *a, **k: None)
    monkeypatch.setattr(watchdog.gmail, "send", lambda **k: None)   # delivered
    bo.write_text(json.dumps({"reason": "AUTH_ERROR", "consecutive_failures": 1}))
    watchdog.check_health(config.load(), log)
    assert flag.exists()                                     # delivered -> dedupe recorded


def test_prune_processed_drops_old_archive_dirs(monkeypatch, tmp_path):
    # Archived control/processed/<date>/ dirs older than keep_days are removed; recent ones kept.
    # (Dedup uses the seen-ledger, so removing these audit dirs is safe.)
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    from datetime import timedelta
    processed = tmp_path / "control" / "processed"
    now = watchdog.clock.now()
    old = (now - timedelta(days=200)).strftime("%Y-%m-%d")
    recent = (now - timedelta(days=5)).strftime("%Y-%m-%d")
    for name in (old, recent):
        (processed / name).mkdir(parents=True)
        (processed / name / "x.md").write_text("archived directive")
    assert watchdog.prune_processed(keep_days=90) == 1        # only the 200-day-old dir removed
    assert not (processed / old).exists()
    assert (processed / recent).exists()                     # recent archive retained
    # H4 regression: the watchdog must inspect EACH enabled persona's namespaced backoff.json,
    # not one import-frozen flat path (which never sees a real persona's backoff).
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config, "enabled_personas", lambda: ["alpha", "beta"])
    monkeypatch.setattr(watchdog, "LAST_TICK", tmp_path / "nope.json")
    monkeypatch.setattr(watchdog, "ALERT", tmp_path / "ALERT")
    monkeypatch.setattr(watchdog, "_macos_notify", lambda *a, **k: None)
    monkeypatch.setattr(watchdog.gmail, "send", lambda **k: None)
    bo = config.state_root("alpha") / "backoff.json"
    bo.parent.mkdir(parents=True, exist_ok=True)
    bo.write_text(json.dumps({"reason": "AUTH_ERROR", "consecutive_failures": 1}))
    issues = watchdog.check_health(config.load(), log)
    assert any(i["kind"] == "auth-lapse" and "[alpha]" in i["msg"] for i in issues)
