"""Phase 3: the dispatcher round-robins the enabled personas, one per fire."""
import contextlib
import logging
import subprocess

import pytest

from cagent import dispatcher

log = logging.getLogger("t")


def test_round_robin_cycles():
    enabled = ["alpha", "data", "delta"]
    picks = [dispatcher.select(enabled, i) for i in range(7)]
    assert picks == ["alpha", "data", "delta", "alpha", "data", "delta", "alpha"]


def test_single_persona_is_stable():
    assert [dispatcher.select(["alpha"], i) for i in range(3)] == ["alpha", "alpha", "alpha"]


def test_tick_timeout_derives_from_per_call_budget(monkeypatch):
    # The subprocess cap must exceed the SUM of a tick's chained claude calls, so it scales with the
    # per-call budget rather than a fixed 900s (which was < two 500s calls).
    monkeypatch.setattr(dispatcher.config, "load",
                        lambda: type("C", (), {"tick_timeout_s": 500})())
    assert dispatcher._tick_timeout_s() > 500 * 2          # never SIGKILLs a legitimately slow tick


def test_paused_persona_still_pushes(monkeypatch):
    # A paused persona skips only its TICK, not the auto-push: _pull_and_ingest may have routed owner
    # mail into committed state this cycle, and remote monitoring must still see it.
    monkeypatch.setattr(dispatcher.config, "enabled_personas", lambda: ["alpha"])
    monkeypatch.setattr(dispatcher.gitpush, "on_expected_branch", lambda: (True, "main"))
    monkeypatch.setattr(dispatcher, "_pull_and_ingest", lambda enabled, log: None)
    monkeypatch.setattr(dispatcher, "_read_index", lambda: 0)
    monkeypatch.setattr(dispatcher, "_write_index", lambda i: None)
    monkeypatch.setattr(dispatcher.control, "is_paused", lambda p: True)   # persona is paused
    monkeypatch.setattr(dispatcher.config, "load", lambda: object())
    ran = {"tick": False, "push": False}

    def _no_tick(*a, **k):
        ran["tick"] = True
        return subprocess.CompletedProcess(a, 0)
    monkeypatch.setattr(dispatcher.subprocess, "run", _no_tick)
    monkeypatch.setattr(dispatcher.gitpush, "daily_push",
                        lambda *a, **k: ran.__setitem__("push", True) or "pushed")
    # single_flight is a context manager; make it a trivial one
    import contextlib
    monkeypatch.setattr(dispatcher.locking, "single_flight", lambda: contextlib.nullcontext())

    assert dispatcher.main() == 0
    assert ran["tick"] is False        # paused -> tick subprocess NOT run
    assert ran["push"] is True         # ...but the auto-push STILL happened


# --------------------------------------------------------------------------- #
# cursor + timeout helpers
# --------------------------------------------------------------------------- #

def test_read_write_index_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatcher, "CURSOR", tmp_path / "cursor.json")
    assert dispatcher._read_index() == 0               # missing file -> 0
    dispatcher._write_index(3)
    assert dispatcher._read_index() == 3


def test_read_index_malformed_returns_zero(tmp_path, monkeypatch):
    p = tmp_path / "cursor.json"
    p.write_text("{ not json")
    monkeypatch.setattr(dispatcher, "CURSOR", p)
    assert dispatcher._read_index() == 0               # torn cursor never crashes the cycle


def test_tick_timeout_falls_back_when_config_unreadable(monkeypatch):
    def boom():
        raise RuntimeError("no config")
    monkeypatch.setattr(dispatcher.config, "load", boom)
    assert dispatcher._tick_timeout_s() == 500 * 6 + 120   # default per-call budget


# --------------------------------------------------------------------------- #
# _pull_and_ingest: happy path + each step independently best-effort
# --------------------------------------------------------------------------- #

def test_pull_and_ingest_runs_all_three_steps(monkeypatch):
    calls = []
    monkeypatch.setattr(dispatcher.control, "pull", lambda lg: calls.append("pull"))
    monkeypatch.setattr(dispatcher.control, "process_inbox", lambda en, dp, lg: ["d1"])
    monkeypatch.setattr(dispatcher.config, "default_persona", lambda: "alpha")
    monkeypatch.setattr(dispatcher.gmail, "ingest", lambda commit: calls.append("ingest") or [{"persona": "data"}])
    monkeypatch.setattr(dispatcher.gmail, "ingest_own_accounts",
                        lambda commit: calls.append("own") or [{"persona": "bravo"}])
    dispatcher._pull_and_ingest(["data"], log)
    assert calls == ["pull", "ingest", "own"]


def test_pull_and_ingest_swallows_every_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(dispatcher.control, "pull", boom)
    monkeypatch.setattr(dispatcher.gmail, "ingest", boom)
    monkeypatch.setattr(dispatcher.gmail, "ingest_own_accounts", boom)
    dispatcher._pull_and_ingest(["data"], log)             # must not raise -> tick still runs


# --------------------------------------------------------------------------- #
# main(): the running branch and its failure-tolerant scaffolding
# --------------------------------------------------------------------------- #

def _wire_main(monkeypatch, *, on_branch=True, single_flight=None, run=None,
               daily_push=None, enabled=("alpha", "data")):
    """Stub main()'s collaborators; each test overrides the piece it exercises."""
    monkeypatch.setattr(dispatcher.logging_setup, "setup", lambda: log)
    monkeypatch.setattr(dispatcher.config, "enabled_personas", lambda: list(enabled))
    monkeypatch.setattr(dispatcher.gitpush, "on_expected_branch",
                        lambda: (on_branch, "main" if on_branch else "feature"))
    monkeypatch.setattr(dispatcher.locking, "single_flight",
                        single_flight or (lambda: contextlib.nullcontext()))
    monkeypatch.setattr(dispatcher, "_pull_and_ingest", lambda en, lg: None)
    monkeypatch.setattr(dispatcher, "_read_index", lambda: 0)
    monkeypatch.setattr(dispatcher, "_write_index", lambda i: None)
    monkeypatch.setattr(dispatcher.control, "is_paused", lambda p: False)
    monkeypatch.setattr(dispatcher.config, "load", lambda: object())
    monkeypatch.setattr(dispatcher, "_tick_timeout_s", lambda: 100)
    monkeypatch.setattr(dispatcher.subprocess, "run",
                        run or (lambda *a, **k: subprocess.CompletedProcess(a, 0)))
    monkeypatch.setattr(dispatcher.gitpush, "daily_push", daily_push or (lambda *a, **k: "ok"))


def test_main_no_enabled_personas_is_noop(monkeypatch):
    monkeypatch.setattr(dispatcher.logging_setup, "setup", lambda: log)
    monkeypatch.setattr(dispatcher.config, "enabled_personas", lambda: [])
    assert dispatcher.main() == 0


def test_main_runs_selected_persona_and_pushes(monkeypatch):
    ran = {}

    def _run(cmd, env, cwd, timeout):
        ran["persona"] = env["CAGENT_PERSONA"]
        ran["mode_absent"] = "AGENT_MODE" not in env
        return subprocess.CompletedProcess(cmd, 0)
    pushed = []
    _wire_main(monkeypatch, run=_run,
               daily_push=lambda cfg, lg, force, message: pushed.append(message) or "ok")
    assert dispatcher.main() == 0
    assert ran["persona"] == "alpha"                 # slot 0 of the enabled list
    assert ran["mode_absent"] is True                  # AGENT_MODE stripped (mode is per-persona)
    assert pushed == ["cagent tick: alpha"]


def test_main_off_branch_still_runs_and_pushes(monkeypatch):
    pushed = []
    _wire_main(monkeypatch, on_branch=False,
               daily_push=lambda cfg, lg, force, message: pushed.append(message) or "ok")
    assert dispatcher.main() == 0                       # warns loudly, still runs the tick + push
    assert pushed == ["cagent tick: alpha"]


def test_main_lock_held_skips_pull_but_still_ticks(monkeypatch):
    def _held():
        raise dispatcher.locking.LockHeld()
    _wire_main(monkeypatch, single_flight=_held)
    # override _wire_main's no-op stub: if the lock is held, _pull_and_ingest must not be reached
    monkeypatch.setattr(dispatcher, "_pull_and_ingest", lambda en, lg: pytest.fail("pull must be skipped"))
    assert dispatcher.main() == 0                       # LockHeld caught; tick + push proceed


def test_main_tick_timeout_is_caught(monkeypatch):
    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="tick", timeout=100)
    pushed = []
    _wire_main(monkeypatch, run=_timeout,
               daily_push=lambda cfg, lg, force, message: pushed.append(1) or "ok")
    assert dispatcher.main() == 0                       # TimeoutExpired handled
    assert pushed == [1]                                # push still happens after a killed tick


def test_main_push_failure_is_swallowed(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("push failed")
    _wire_main(monkeypatch, daily_push=_boom)
    assert dispatcher.main() == 0                       # a push failure never fails the cycle
