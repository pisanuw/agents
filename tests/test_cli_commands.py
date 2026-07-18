"""cli.py command handlers + the main() dispatcher + _persona_flag. Each handler is a thin wrapper
over an already-tested module, so these stub the delegated call and assert the exit code / routing /
argument handling. Heavier commands (doctor -> claude, reset/migrate -> destructive, recent/mail/sent/
usage) are covered by their own dedicated test files."""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from cagent import cli, config

log = logging.getLogger("t")


# --------------------------- main() dispatcher --------------------------- #

def test_main_help_no_args(capsys):
    assert cli.main([]) == 0
    assert "cagentctl" in capsys.readouterr().out


def test_main_help_flag(capsys):
    assert cli.main(["-h"]) == 0
    assert "commands:" in capsys.readouterr().out


def test_main_unknown_command(capsys):
    assert cli.main(["bogus"]) == 2
    assert "unknown command" in capsys.readouterr().err


def test_main_dispatches_and_defaults_zero(monkeypatch):
    seen = []
    monkeypatch.setitem(cli.COMMANDS, "config", (lambda argv: seen.append(argv) or None, "help"))
    assert cli.main(["config", "x", "y"]) == 0        # handler returning None -> 0
    assert seen == [["x", "y"]]


# --------------------------- _persona_flag --------------------------- #

def test_persona_flag_noop():
    assert cli._persona_flag(["a", "b"]) == ["a", "b"]


def test_persona_flag_sets_env_space_and_equals(monkeypatch):
    monkeypatch.setattr(config, "known_personas", lambda: ["golf"])
    assert cli._persona_flag(["--persona", "golf", "5"]) == ["5"]
    assert cli._persona_flag(["--persona=golf"]) == []


def test_persona_flag_invalid_name_exits():
    with pytest.raises(SystemExit) as e:
        cli._persona_flag(["--persona", "Bad Name!"])
    assert e.value.code == 2


def test_persona_flag_unknown_persona_exits(monkeypatch):
    monkeypatch.setattr(config, "known_personas", lambda: ["golf"])
    with pytest.raises(SystemExit) as e:
        cli._persona_flag(["--persona", "ghost"])
    assert e.value.code == 2


# --------------------------- simple handlers --------------------------- #

def test_cmd_config_prints_json(capsys):
    assert cli.cmd_config([]) == 0
    assert "MODE" in capsys.readouterr().out


def test_cmd_ask_usage_error():
    assert cli.cmd_ask([]) == 2


def test_cmd_ask_success(monkeypatch, capsys):
    from cagent.cognition import invoke, parse
    monkeypatch.setattr(invoke, "run_claude", lambda *a, **k: {})
    monkeypatch.setattr(parse, "parse", lambda env: SimpleNamespace(status="OK", text="hi there", detail=""))
    assert cli.cmd_ask(["hello"]) == 0
    assert "hi there" in capsys.readouterr().out


def test_cmd_ask_error(monkeypatch, capsys):
    from cagent.cognition import invoke, parse
    monkeypatch.setattr(invoke, "run_claude", lambda *a, **k: {})
    monkeypatch.setattr(parse, "parse", lambda env: SimpleNamespace(status="ERROR", text="", detail="boom"))
    assert cli.cmd_ask(["hi"]) == 1


def test_cmd_run_tick_delegates(monkeypatch):
    from cagent import tick
    monkeypatch.setattr(cli, "_mirror_note", lambda root: None)   # simulate the live host (not a mirror)
    monkeypatch.setattr(tick, "main", lambda: 0)
    assert cli.cmd_run_tick([]) == 0


def test_cmd_status(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_mirror_note", lambda root: None)
    assert cli.cmd_status([]) == 0
    assert "mode:" in capsys.readouterr().out


def test_cmd_stop_and_start_global(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    assert cli.cmd_stop([]) == 0
    assert (tmp_path / "var" / "STOP").exists()
    assert cli.cmd_start([]) == 0
    assert not (tmp_path / "var" / "STOP").exists()
    assert cli.cmd_start([]) == 0                    # idempotent: already running
    assert "already running" in capsys.readouterr().out


def test_cmd_stop_persona(tmp_path, monkeypatch):
    from cagent import control
    monkeypatch.setattr(config, "known_personas", lambda: ["golf"])
    sp = tmp_path / "golf.STOP"
    monkeypatch.setattr(control, "stop_path", lambda name: sp)
    assert cli.cmd_stop(["--persona", "golf"]) == 0
    assert sp.exists()


def test_cmd_stop_missing_persona_value():
    assert cli.cmd_stop(["--persona"]) == 2


def test_cmd_start_persona_not_paused(tmp_path, monkeypatch, capsys):
    from cagent import control
    monkeypatch.setattr(config, "known_personas", lambda: ["golf"])
    monkeypatch.setattr(control, "stop_path", lambda name: tmp_path / "golf.STOP")
    assert cli.cmd_start(["--persona", "golf"]) == 0
    assert "was not paused" in capsys.readouterr().out


def test_cmd_send_test_ok_and_refused(monkeypatch):
    from cagent import gmail
    monkeypatch.setattr(gmail, "send",
                        lambda **k: SimpleNamespace(dry_run=True, to="o@x", message_id="<1>"))
    assert cli.cmd_send_test([]) == 0

    def _refuse(**k):
        raise gmail.SendRefused("cap")
    monkeypatch.setattr(gmail, "send", _refuse)
    assert cli.cmd_send_test([]) == 1


def test_cmd_poll(monkeypatch, capsys):
    """Multi-persona poll routes via ingest()/ingest_own_accounts(); poll_imap is NOT used."""
    from cagent import config, gmail
    monkeypatch.setattr(config, "enabled_personas", lambda: ["alice", "bob"])
    monkeypatch.setattr(config, "known_personas", lambda: ["alice", "bob"])
    monkeypatch.setattr(gmail, "ingest",
                        lambda commit: [{"uid": "1", "from": "o@x", "subject": "hi", "persona": "alice"}])
    monkeypatch.setattr(gmail, "ingest_own_accounts",
                        lambda commit: [{"uid": "2", "from": "o@x", "subject": "yo", "persona": "bob"}])
    # poll_imap must never be reached in multi-persona mode.
    monkeypatch.setattr(gmail, "poll_imap",
                        lambda commit: (_ for _ in ()).throw(AssertionError("poll_imap used")))
    assert cli.cmd_poll([]) == 0
    out = capsys.readouterr().out
    assert "polled: 2" in out and "[alice" in out and "[bob" in out


def test_cmd_poll_refuses_persona(monkeypatch, capsys):
    """poll REFUSES --persona (exit 2) and does not poll: routing is always by +tag to ALL personas,
    never scoped to one. A --persona here once selected the misrouting poll_imap() path."""
    from cagent import config, gmail
    monkeypatch.setattr(config, "enabled_personas", lambda: ["alice", "bob"])
    # If the guard fails to return early, ingest below would raise, failing the test loudly.
    monkeypatch.setattr(gmail, "ingest",
                        lambda commit: (_ for _ in ()).throw(AssertionError("ingest ran despite --persona")))
    for argv in (["--persona", "alice"], ["--persona=alice"], ["--persona", "alice", "--commit"]):
        assert cli.cmd_poll(argv) == 2
        assert "does not accept --persona" in capsys.readouterr().err


def test_cmd_poll_legacy_single_persona(monkeypatch, capsys):
    """No personas configured -> fall back to the flat poll_imap (no routing)."""
    from cagent import config, gmail
    monkeypatch.setattr(config, "enabled_personas", lambda: [])
    monkeypatch.setattr(config, "known_personas", lambda: [])
    monkeypatch.setattr(gmail, "poll_imap", lambda commit: [{"uid": "1", "from": "o@x", "subject": "hi"}])
    assert cli.cmd_poll([]) == 0
    assert "polled: 1" in capsys.readouterr().out


def test_cmd_poll_baseline(monkeypatch, capsys):
    # P1-6: on a multi-persona install poll-baseline advances the SHARED ingest cursor (+ each
    # own-account cursor), not the flat poll_imap cursor nothing reads.
    from cagent import config, gmail
    monkeypatch.setattr(config, "enabled_personas", lambda: ["alpha", "bravo"])
    monkeypatch.setattr(gmail, "baseline_shared", lambda: 42)
    monkeypatch.setattr(gmail, "baseline_own_accounts", lambda: {"bravo@x": 7})
    monkeypatch.setattr(gmail, "baseline", lambda: (_ for _ in ()).throw(AssertionError("flat used")))
    assert cli.cmd_poll_baseline([]) == 0
    out = capsys.readouterr().out
    assert "42" in out and "bravo@x" in out


def test_cmd_poll_baseline_legacy_single_persona(monkeypatch, capsys):
    from cagent import config, gmail
    monkeypatch.setattr(config, "enabled_personas", lambda: [])   # legacy -> flat cursor
    monkeypatch.setattr(gmail, "baseline", lambda: 42)
    assert cli.cmd_poll_baseline([]) == 0
    assert "42" in capsys.readouterr().out


def test_cmd_watchdog(monkeypatch):
    from cagent import logging_setup, watchdog
    monkeypatch.setattr(cli, "_mirror_note", lambda root: None)   # simulate the live host (not a mirror)
    monkeypatch.setattr(config, "load", lambda *a, **k: object())
    monkeypatch.setattr(logging_setup, "setup", lambda: log)
    monkeypatch.setattr(watchdog, "check_health", lambda cfg, lg: [])
    monkeypatch.setattr(watchdog, "run_maintenance", lambda lg: {})
    assert cli.cmd_watchdog([]) == 0


def test_cmd_maintenance(monkeypatch):
    from cagent import logging_setup, watchdog
    monkeypatch.setattr(logging_setup, "setup", lambda: log)
    monkeypatch.setattr(watchdog, "run_maintenance", lambda lg: {"rotated_logs": 0})
    assert cli.cmd_maintenance([]) == 0


def test_cmd_pending(monkeypatch, capsys):
    from cagent import supervise
    monkeypatch.setattr(supervise, "list_pending",
                        lambda: [{"token": "T1", "created": "2026-07-03T10:00:00", "subject": "hi"}])
    monkeypatch.setattr(supervise, "draft_status", lambda d: supervise.APPROVED_UNSENT)
    assert cli.cmd_pending([]) == 0
    assert "T1" in capsys.readouterr().out


def test_cmd_approve_usage_and_success(monkeypatch):
    from cagent import logging_setup, supervise
    assert cli.cmd_approve([]) == 2
    monkeypatch.setattr(config, "load", lambda *a, **k: object())
    monkeypatch.setattr(logging_setup, "setup", lambda: log)
    monkeypatch.setattr(supervise, "approve", lambda tok, cfg, lg: {"ok": True})
    assert cli.cmd_approve(["TOK"]) == 0


def test_cmd_reject_usage_and_success(monkeypatch):
    from cagent import supervise
    assert cli.cmd_reject([]) == 2
    monkeypatch.setattr(supervise, "reject", lambda tok: {"discarded": True})
    assert cli.cmd_reject(["TOK"]) == 0


def test_cmd_digest(monkeypatch):
    from cagent import logging_setup, supervise
    monkeypatch.setattr(config, "load", lambda *a, **k: object())
    monkeypatch.setattr(logging_setup, "setup", lambda: log)
    monkeypatch.setattr(supervise, "send_digest", lambda cfg, lg: {"sent": True})
    assert cli.cmd_digest([]) == 0


def test_cmd_resend_approvals_persona_and_all(monkeypatch):
    from cagent import logging_setup, supervise
    monkeypatch.setattr(logging_setup, "setup", lambda: log)
    monkeypatch.setattr(config, "known_personas", lambda: ["golf"])
    monkeypatch.setattr(config, "enabled_personas", lambda: ["golf", "bob"])
    monkeypatch.setattr(config, "load", lambda name=None: object())
    monkeypatch.setattr(supervise, "send_approval_backlog", lambda cfg, lg: {"sent": 1})
    assert cli.cmd_resend_approvals(["--persona", "golf"]) == 0
    assert cli.cmd_resend_approvals(["--all"]) == 0


def test_cmd_scorecard(monkeypatch):
    from cagent import supervise
    monkeypatch.setattr(supervise, "scorecard", lambda: "SCORECARD")
    assert cli.cmd_scorecard([]) == 0


def test_cmd_readiness(tmp_path, monkeypatch, capsys):
    from cagent import supervise
    monkeypatch.setattr(config, "known_personas", lambda: ["golf"])
    monkeypatch.setattr(config, "state_root", lambda name=None: tmp_path)
    monkeypatch.setattr(config, "load", lambda name=None: SimpleNamespace(MODE="SUPERVISED"))
    monkeypatch.setattr(supervise, "persona_stats",
                        lambda sr: {"days": {"2026-07-03"}, "ticks": 10, "ok": 8, "fail": 2,
                                    "blocked": 1, "refused": 0, "pending": 0,
                                    "last_ts": "2026-07-03T10:00:00+00:00"})
    assert cli.cmd_readiness([]) == 0
    assert "golf" in capsys.readouterr().out


def test_cmd_readiness_unknown_persona(monkeypatch):
    monkeypatch.setattr(config, "known_personas", lambda: ["golf"])
    assert cli.cmd_readiness(["--persona", "ghost"]) == 2


def test_cmd_daily_push(monkeypatch):
    from cagent import gitpush, logging_setup
    monkeypatch.setattr(cli, "_mirror_note", lambda root: None)   # simulate the live host (not a mirror)
    monkeypatch.setattr(config, "load", lambda *a, **k: object())
    monkeypatch.setattr(logging_setup, "setup", lambda: log)
    monkeypatch.setattr(gitpush, "daily_push", lambda cfg, lg, force: {"pushed": False})
    assert cli.cmd_daily_push([]) == 0


def test_cmd_force_reflect_ok_and_fail(monkeypatch):
    from cagent import logging_setup, reflect
    monkeypatch.setattr(config, "load", lambda *a, **k: object())
    monkeypatch.setattr(logging_setup, "setup", lambda: log)
    monkeypatch.setattr(reflect, "run", lambda cfg, lg: {"ok": True})
    assert cli.cmd_force_reflect([]) == 0
    monkeypatch.setattr(reflect, "run", lambda cfg, lg: {"ok": False})
    assert cli.cmd_force_reflect([]) == 1


def test_cmd_dispatch(monkeypatch):
    from cagent import dispatcher
    monkeypatch.setattr(dispatcher, "main", lambda: 0)
    assert cli.cmd_dispatch([]) == 0


def test_cmd_personas(monkeypatch, capsys):
    monkeypatch.setattr(config, "enabled_personas", lambda: ["golf"])
    monkeypatch.setattr(config, "default_persona", lambda: "golf")
    monkeypatch.setattr(config, "known_personas", lambda: ["golf", "bob"])
    monkeypatch.setattr(config, "load",
                        lambda name=None: SimpleNamespace(MODE="SUPERVISED", plus_tag="golf", from_name="Golf"))
    assert cli.cmd_personas([]) == 0
    out = capsys.readouterr().out
    assert "ENABLED" in out and "draft" in out


def test_cmd_inject_inbound(tmp_path, monkeypatch, capsys):
    from cagent import gmail
    assert cli.cmd_inject_inbound([]) == 2                          # usage
    assert cli.cmd_inject_inbound([str(tmp_path / "nope.json")]) == 2   # unreadable
    rdir = tmp_path / "received"
    monkeypatch.setattr(gmail, "_received_dir", lambda: rdir)
    fixture = tmp_path / "fix.json"
    fixture.write_text(json.dumps({"uid": "99", "subject": "hi"}))
    assert cli.cmd_inject_inbound([str(fixture)]) == 0
    assert (rdir / "99.json").exists()


def test_mirror_guard_refuses_mutating_commands(monkeypatch, capsys):
    # P2-8/P2-9: run-tick / daily-push / watchdog refuse on a detected mirror unless --force-mirror,
    # matching reset/migrate-persona. On the live host _mirror_note is None, so they run normally.
    from cagent import gitpush, tick, watchdog
    monkeypatch.setattr(cli, "_mirror_note", lambda root: "MIRROR? git holds newer ticks")
    monkeypatch.setattr(tick, "main", lambda: 0)
    monkeypatch.setattr(gitpush, "daily_push", lambda *a, **k: {"pushed": False})
    monkeypatch.setattr(watchdog, "check_health", lambda *a, **k: [])
    monkeypatch.setattr(watchdog, "run_maintenance", lambda *a, **k: "")
    for cmd in (cli.cmd_run_tick, cli.cmd_daily_push, cli.cmd_watchdog):
        capsys.readouterr()
        assert cmd([]) == 2
        assert "REFUSING" in capsys.readouterr().err
        assert cmd(["--force-mirror"]) == 0
