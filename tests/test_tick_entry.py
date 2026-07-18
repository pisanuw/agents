"""tick.main entrypoint: the guard ladder (persona validation -> STOP flag -> per-persona pause ->
backoff gate -> single-flight lock -> pipeline). The expected-skip guards exit 0 (launchd sees no
crash for an expected skip); a bogus CAGENT_PERSONA exits non-zero. None reach tick_pipeline.run
when tripped."""
from __future__ import annotations

import contextlib
import logging

import pytest

from cagent import tick

log = logging.getLogger("t")


def _base(monkeypatch, tmp_path):
    monkeypatch.setattr(tick.logging_setup, "setup", lambda: log)
    monkeypatch.setattr(tick.config, "load", lambda *a, **k: object())
    monkeypatch.setattr(tick, "STOP", tmp_path / "nostop")
    monkeypatch.delenv("CAGENT_PERSONA", raising=False)


def test_bogus_persona_refused_at_source(tmp_path, monkeypatch):
    """Stop bogus manual runs at the source: a CAGENT_PERSONA with no personas/<name>/ dir (a typo
    or stale env -- the 2026-07-03 alpha.STOP incident) is refused with a non-zero exit before any
    other guard and before tick_pipeline.run touches state."""
    _base(monkeypatch, tmp_path)
    monkeypatch.setenv("CAGENT_PERSONA", "alpha")
    monkeypatch.setattr(tick.config, "known_personas", lambda: ["bravo", "data"])
    monkeypatch.setattr(tick.tick_pipeline, "run",
                        lambda cfg, lg: pytest.fail("bogus persona must never reach cognition"))
    assert tick.main() == 2


def test_draft_persona_passes_source_guard(tmp_path, monkeypatch):
    """A persona WITH a personas/<name>/ dir -- enabled OR draft -- passes the guard, so testing a
    draft via run-tick still runs (the guard keys on directories, not the enabled list)."""
    _base(monkeypatch, tmp_path)
    monkeypatch.setenv("CAGENT_PERSONA", "draftpersona")
    monkeypatch.setattr(tick.config, "known_personas", lambda: ["draftpersona"])
    monkeypatch.setattr(tick.control, "is_paused", lambda p: False)
    monkeypatch.setattr(tick.backoff, "gate_open", lambda: (True, ""))
    monkeypatch.setattr(tick.locking, "single_flight", lambda: contextlib.nullcontext())
    ran = []
    monkeypatch.setattr(tick.tick_pipeline, "run", lambda cfg, lg: ran.append(1) or 0)
    assert tick.main() == 0 and ran == [1]


def test_stop_flag_halts_before_cognition(tmp_path, monkeypatch):
    _base(monkeypatch, tmp_path)
    (tmp_path / "STOP").write_text("")
    monkeypatch.setattr(tick, "STOP", tmp_path / "STOP")
    monkeypatch.setattr(tick.tick_pipeline, "run", lambda cfg, lg: pytest.fail("must not tick when STOPPED"))
    assert tick.main() == 0


def test_persona_pause_halts(tmp_path, monkeypatch):
    _base(monkeypatch, tmp_path)
    monkeypatch.setenv("CAGENT_PERSONA", "scout")
    monkeypatch.setattr(tick.control, "is_paused", lambda p: True)
    monkeypatch.setattr(tick.tick_pipeline, "run", lambda cfg, lg: pytest.fail("must not tick when paused"))
    assert tick.main() == 0


def test_backoff_defers(tmp_path, monkeypatch):
    _base(monkeypatch, tmp_path)
    monkeypatch.setattr(tick.backoff, "gate_open", lambda: (False, "429 backoff active"))
    monkeypatch.setattr(tick.tick_pipeline, "run", lambda cfg, lg: pytest.fail("must not tick while backing off"))
    assert tick.main() == 0


def test_runs_pipeline_under_lock(tmp_path, monkeypatch):
    _base(monkeypatch, tmp_path)
    monkeypatch.setattr(tick.backoff, "gate_open", lambda: (True, ""))
    monkeypatch.setattr(tick.locking, "single_flight", lambda: contextlib.nullcontext())
    ran = []
    monkeypatch.setattr(tick.tick_pipeline, "run", lambda cfg, lg: ran.append(1) or 0)
    assert tick.main() == 0 and ran == [1]


def test_lock_held_skips_cleanly(tmp_path, monkeypatch):
    _base(monkeypatch, tmp_path)
    monkeypatch.setattr(tick.backoff, "gate_open", lambda: (True, ""))

    def _held():
        raise tick.locking.LockHeld()
    monkeypatch.setattr(tick.locking, "single_flight", _held)
    monkeypatch.setattr(tick.tick_pipeline, "run", lambda cfg, lg: pytest.fail("lock held -> no tick"))
    assert tick.main() == 0
