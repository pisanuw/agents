"""Per-stage coverage for tick_pipeline: the named best-effort steps (poll, token-burn, approval
release/retry, status reply, control drain, reflect), the digest day-marker, and _finish's journal /
LAST_TICK / usage-meter / tripwire fan-out. run()'s end-to-end ordering is covered separately in
test_tick_pipeline; here each stage is exercised in isolation so a regression in one is pinpointed.
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from cagent import tick_pipeline

log = logging.getLogger("t")


# --- _best_effort ---------------------------------------------------------- #

def test_best_effort_returns_value_and_swallows_errors():
    assert tick_pipeline._best_effort("ok", log, lambda: 42) == 42

    def boom():
        raise RuntimeError("stage failed")
    assert tick_pipeline._best_effort("bad", log, boom) is None
    assert tick_pipeline._best_effort("bad", log, boom, warn=True) is None   # warn path also swallows


# --- _poll_own_inbox / _flag_token_burn ------------------------------------ #

def test_poll_own_inbox_handles_new_and_empty(monkeypatch):
    monkeypatch.setattr(tick_pipeline.gmail, "poll_imap", lambda commit: [1, 2])
    tick_pipeline._poll_own_inbox(log)
    monkeypatch.setattr(tick_pipeline.gmail, "poll_imap", lambda commit: [])
    tick_pipeline._poll_own_inbox(log)                                       # no crash on nothing new


def test_flag_token_burn_logs_when_exposed(monkeypatch):
    monkeypatch.setattr(tick_pipeline.commands, "note_token_exposure", lambda d, cfg, lg: True)
    tick_pipeline._flag_token_burn([("x", "reason")], SimpleNamespace(), log)


# --- SUPERVISED-only stages: skipped in other modes ------------------------ #

def test_release_approved_only_in_supervised(monkeypatch):
    calls = []
    monkeypatch.setattr(tick_pipeline.supervise, "retry_approved", lambda cfg, lg: calls.append(1) or [])
    tick_pipeline._release_approved(SimpleNamespace(MODE="LIVE"), log)
    assert calls == []                                                       # not called off SUPERVISED
    monkeypatch.setattr(tick_pipeline.supervise, "retry_approved", lambda cfg, lg: [{"token": "t"}])
    tick_pipeline._release_approved(SimpleNamespace(MODE="SUPERVISED"), log)


def test_retry_approval_requests_only_in_supervised(monkeypatch):
    monkeypatch.setattr(tick_pipeline.supervise, "retry_undelivered",
                        lambda cfg, lg: pytest.fail("must not run off SUPERVISED"))
    tick_pipeline._retry_approval_requests(SimpleNamespace(MODE="DRY_RUN"), log)   # returns early
    monkeypatch.setattr(tick_pipeline.supervise, "retry_undelivered", lambda cfg, lg: [{"token": "t"}])
    monkeypatch.setattr(tick_pipeline.supervise, "remind_and_expire_approvals",
                        lambda cfg, lg: {"reminded": ["a"], "expired": ["b"]})
    tick_pipeline._retry_approval_requests(SimpleNamespace(MODE="SUPERVISED"), log)


def test_retry_command_acks_any_mode(monkeypatch):
    monkeypatch.setattr(tick_pipeline.commands, "retry_acks", lambda cfg, lg: [1, 2])
    tick_pipeline._retry_command_acks(SimpleNamespace(), log)


# --- _answer_status_request: clear only once actually sent ----------------- #

def test_status_request_absent_does_nothing(monkeypatch):
    monkeypatch.setattr(tick_pipeline.commands, "status_requested", lambda: False)
    monkeypatch.setattr(tick_pipeline.supervise, "send_status", lambda cfg, lg: pytest.fail("no request"))
    tick_pipeline._answer_status_request(SimpleNamespace(), log)


def test_status_request_clears_flag_when_sent(monkeypatch):
    monkeypatch.setattr(tick_pipeline.commands, "status_requested", lambda: True)
    monkeypatch.setattr(tick_pipeline.supervise, "send_status", lambda cfg, lg: {"sent": True})
    cleared = []
    monkeypatch.setattr(tick_pipeline.commands, "clear_status_request", lambda: cleared.append(1))
    tick_pipeline._answer_status_request(SimpleNamespace(), log)
    assert cleared == [1]


def test_status_request_kept_when_send_refused(monkeypatch):
    monkeypatch.setattr(tick_pipeline.commands, "status_requested", lambda: True)
    monkeypatch.setattr(tick_pipeline.supervise, "send_status", lambda cfg, lg: {"sent": False})
    monkeypatch.setattr(tick_pipeline.commands, "clear_status_request", lambda: pytest.fail("not sent"))
    tick_pipeline._answer_status_request(SimpleNamespace(), log)             # flag retained for retry


# --- _apply_control_directives / _maybe_reflect ---------------------------- #

def test_apply_control_directives_logs_drained(monkeypatch):
    monkeypatch.setattr(tick_pipeline.control, "drain", lambda cfg, lg: [{"type": "goal"}])
    tick_pipeline._apply_control_directives(SimpleNamespace(), log)


def test_maybe_reflect_runs_only_when_due(monkeypatch):
    ran = []
    monkeypatch.setattr(tick_pipeline.reflect, "should_reflect", lambda cfg: (True, "cadence"))
    monkeypatch.setattr(tick_pipeline.reflect, "run", lambda cfg, lg: ran.append(1))
    tick_pipeline._maybe_reflect(SimpleNamespace(), log)
    assert ran == [1]
    monkeypatch.setattr(tick_pipeline.reflect, "should_reflect", lambda cfg: (False, ""))
    monkeypatch.setattr(tick_pipeline.reflect, "run", lambda cfg, lg: pytest.fail("not due"))
    tick_pipeline._maybe_reflect(SimpleNamespace(), log)


# --- _maybe_digest: once/day, marker only when actually sent --------------- #

def test_maybe_digest_skips_when_already_done_today(tmp_path, monkeypatch):
    monkeypatch.setattr(tick_pipeline.config, "state_root", lambda *a: tmp_path)
    monkeypatch.setattr(tick_pipeline.daymarker, "done_today", lambda p: True)
    monkeypatch.setattr(tick_pipeline.supervise, "send_digest", lambda cfg, lg: pytest.fail("already done"))
    tick_pipeline._maybe_digest(SimpleNamespace(), log)


def test_maybe_digest_marks_only_when_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(tick_pipeline.config, "state_root", lambda *a: tmp_path)
    monkeypatch.setattr(tick_pipeline.daymarker, "done_today", lambda p: False)
    marked = []
    monkeypatch.setattr(tick_pipeline.daymarker, "mark", lambda p: marked.append(p.name))
    monkeypatch.setattr(tick_pipeline.supervise, "send_digest", lambda cfg, lg: {"sent": True})
    tick_pipeline._maybe_digest(SimpleNamespace(), log)
    assert marked == ["last_digest"]


def test_maybe_digest_no_mark_when_send_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(tick_pipeline.config, "state_root", lambda *a: tmp_path)
    monkeypatch.setattr(tick_pipeline.daymarker, "done_today", lambda p: False)
    monkeypatch.setattr(tick_pipeline.daymarker, "mark", lambda p: pytest.fail("send failed"))
    monkeypatch.setattr(tick_pipeline.supervise, "send_digest", lambda cfg, lg: {"sent": False})
    tick_pipeline._maybe_digest(SimpleNamespace(), log)                      # retried next tick


# --- _finish: journal line + LAST_TICK + usage dump + tripwire ------------- #

def test_finish_writes_journal_last_tick_and_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(tick_pipeline.config, "state_root", lambda *a: tmp_path)
    monkeypatch.setattr(tick_pipeline, "LAST_TICK", tmp_path / "last_tick.json")
    monkeypatch.setattr(tick_pipeline.meter, "drain",
                        lambda: ({"cost_usd": 0.01, "total_tokens": 5}, [{"label": "tick"}]))
    tw = []
    monkeypatch.setattr(tick_pipeline.supervise, "check_tripwire", lambda cfg, lg: tw.append(1))
    tickdir = tmp_path / "td"
    tickdir.mkdir()
    tick_pipeline._finish(SimpleNamespace(MODE="LIVE"), ok=True, summary="did stuff",
                          tickdir=tickdir, log=log, journal={"kind": "tick", "ok": True})
    line = json.loads((tmp_path / "journal.jsonl").read_text().strip())
    assert line["usage"]["cost_usd"] == 0.01 and line["cost_notional"] == 0.01
    last = json.loads((tmp_path / "last_tick.json").read_text())
    assert last["summary"] == "did stuff" and last["ok"] is True
    assert (tickdir / "usage.json").exists()
    assert tw == [1]                                                        # tripwire runs on every exit


def test_finish_swallows_tripwire_error(tmp_path, monkeypatch):
    monkeypatch.setattr(tick_pipeline.config, "state_root", lambda *a: tmp_path)
    monkeypatch.setattr(tick_pipeline, "LAST_TICK", tmp_path / "last_tick.json")
    monkeypatch.setattr(tick_pipeline.meter, "drain", lambda: ({"cost_usd": 0.0}, []))

    def boom(cfg, lg):
        raise RuntimeError("tripwire broke")
    monkeypatch.setattr(tick_pipeline.supervise, "check_tripwire", boom)
    tick_pipeline._finish(SimpleNamespace(MODE="LIVE"), ok=False, summary="s",
                          journal={"kind": "tick"}, tickdir=None, log=log)
    assert (tmp_path / "journal.jsonl").exists()                            # journal still written
