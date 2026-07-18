"""Shared pytest fixtures for the cagent test suite.

Autouse fixtures here (process-env isolation + a real-network block) apply to every test. Opt-in
sandbox fixtures live next to the tests that use them; earlier drafts of an `approval_sandbox` /
`cmd_sandbox` / `make_persona_cfg` were never wired up and drifted from the copies tests actually
grew, so they were removed rather than left as a misleading "single source of truth".
"""
from __future__ import annotations

import imaplib
import logging
import os
import smtplib

import pytest


# ---------------------------------------------------------------------------
# Process-environment isolation (autouse)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_process_env():
    """Guarantee test order-independence. config.load()/state_root() read CAGENT_PERSONA (and
    AGENT_MODE) from the REAL process environment at call time, and a handful of tests set
    CAGENT_PERSONA directly -- correct for the live CLI, whose tick subprocess inherits the var, so
    monkeypatch's LIFO restore cannot model it. Snapshot and restore around every test so a persona
    (or mode) set or leaked in one test can never mis-resolve state_root() in the next. This is the
    root-cause fix for the suite passing in file order but failing under pytest-randomly.

    We also POP AGENT_MODE at setup: an operator who exports AGENT_MODE=SUPERVISED/LIVE (documented on
    the live host) would otherwise make gmail.send tests take the non-DRY_RUN path -- config.toml's
    DRY_RUN default is the only mode a test should see unless it sets one explicitly."""
    saved = {k: os.environ.get(k) for k in ("CAGENT_PERSONA", "AGENT_MODE")}
    os.environ.pop("AGENT_MODE", None)      # ambient mode must never leak into a send test
    os.environ.pop("CAGENT_PERSONA", None)  # default to flat/legacy state unless a test opts in
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(autouse=True)
def _isolate_logging(tmp_path_factory, monkeypatch):
    """Keep tests from writing to the real, git-TRACKED logs/ dir. A handful of tests call a module
    `main()` (dispatcher/tick/watchdog/gitpush) that runs `logging_setup.setup()`, which attaches a
    FileHandler to logs/tick-<date>.log on the PROCESS-GLOBAL 'cagent' logger and flips a one-shot
    `_configured` guard. Once attached, every later test that logs through getLogger('cagent') (gmail,
    watchdog, ...) leaks INFO/WARNING into that committed file. Redirect the log dir to tmp, reset the
    guard, and strip 'cagent' handlers around each test so setup()'s FileHandler lands in tmp and is
    discarded afterwards -- the real logs/ file is never touched."""
    from cagent import logging_setup
    monkeypatch.setattr(logging_setup, "LOGS", tmp_path_factory.mktemp("logs"))
    monkeypatch.setattr(logging_setup, "_configured", False)
    logger = logging.getLogger("cagent")
    saved = logger.handlers[:]
    logger.handlers = []
    yield
    for h in logger.handlers:            # close anything setup() opened during the test (tmp files)
        try:
            h.close()
        except Exception:
            pass
    logger.handlers = saved


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch):
    """Defense in depth: make it *impossible* for any test to open a real SMTP/IMAP socket. Every
    external I/O path in cagent is meant to be stubbed; if a test reaches the real socket (a
    mis-set AGENT_MODE, a missing monkeypatch), that is a bug we want to surface LOUDLY instead of
    silently logging into Gmail with the real app password. Tests that exercise the connection
    builders (e.g. test_imap_helpers) re-patch these after this fixture, so they are unaffected."""
    def _blocked(*_a, **_k):
        raise RuntimeError("real network blocked in tests (SMTP/IMAP); stub the send/poll path")
    monkeypatch.setattr(smtplib, "SMTP_SSL", _blocked)
    monkeypatch.setattr(smtplib, "SMTP", _blocked)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", _blocked)
