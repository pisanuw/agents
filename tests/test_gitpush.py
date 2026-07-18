"""gitpush coverage beyond the branch guard: the today-counters, secret scan, daily_push lock
gating, tracked-log capping, and _do_push's every exit (secret-scan abort, DRY_RUN, commit failure,
no-upstream set, non-fast-forward rebase-conflict, hard push failure) plus main(). git is stubbed via
gitpush._git so nothing touches the real repo.
"""
from __future__ import annotations

import contextlib
import json
import subprocess

from cagent import config, gitpush


class _Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


LOG = _Log()


class _Cfg:
    MODE = "SUPERVISED"


class _DryCfg:
    MODE = "DRY_RUN"


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(["git"], returncode, stdout, stderr)


# --------------------------- counters --------------------------- #

def test_count_today_filters_day_kind_and_dry_run(tmp_path, monkeypatch):
    monkeypatch.setenv("CAGENT_NOW", "2026-07-03T12:00:00+00:00")
    p = tmp_path / "j.jsonl"
    p.write_text("\n".join([
        json.dumps({"ts": "2026-07-03T01:00:00+00:00", "kind": "tick"}),
        json.dumps({"ts": "2026-07-03T02:00:00+00:00", "kind": "tick", "dry_run": True}),
        json.dumps({"ts": "2026-07-02T02:00:00+00:00", "kind": "tick"}),   # other day
        json.dumps({"ts": "2026-07-03T03:00:00+00:00", "kind": "other"}),
        "{ corrupt",
    ]))
    assert gitpush._count_today(p) == 3                             # all today rows, corrupt/other-day out
    assert gitpush._count_today(p, kind="tick") == 2               # today ticks (incl dry-run)
    assert gitpush._count_today(p, kind="tick", skip_dry_run=True) == 1   # dry-run excluded


def test_count_today_missing_file(tmp_path):
    assert gitpush._count_today(tmp_path / "nope.jsonl") == 0


def test_count_today_all_falls_back_when_no_personas(tmp_path, monkeypatch):
    monkeypatch.setenv("CAGENT_NOW", "2026-07-03T12:00:00+00:00")
    monkeypatch.setattr(config, "personas_state_root", lambda: tmp_path / "empty")
    fb = tmp_path / "fallback.jsonl"
    fb.write_text(json.dumps({"ts": "2026-07-03T01:00:00+00:00", "kind": "tick"}))
    assert gitpush._count_today_all("*/journal.jsonl", fb, kind="tick") == 1


def test_count_today_all_aggregates_personas(tmp_path, monkeypatch):
    monkeypatch.setenv("CAGENT_NOW", "2026-07-03T12:00:00+00:00")
    proot = tmp_path / "personas"
    monkeypatch.setattr(config, "personas_state_root", lambda: proot)
    for name in ("a", "b"):
        d = proot / name
        d.mkdir(parents=True)
        (d / "journal.jsonl").write_text(json.dumps({"ts": "2026-07-03T01:00:00+00:00", "kind": "tick"}))
    assert gitpush._count_today_all("*/journal.jsonl", tmp_path / "fb", kind="tick") == 2


# --------------------------- secret scan --------------------------- #

def test_secret_scan_ok_and_fail(monkeypatch):
    monkeypatch.setattr(gitpush.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "clean", ""))
    assert gitpush._secret_scan() == (True, "clean")
    monkeypatch.setattr(gitpush.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "LEAK found"))
    ok, msg = gitpush._secret_scan()
    assert ok is False and "LEAK" in msg


# --------------------------- daily_push gating --------------------------- #

def test_daily_push_skips_if_pushed_today(monkeypatch):
    monkeypatch.setattr(gitpush, "_pushed_today", lambda: True)
    assert gitpush.daily_push(_Cfg(), LOG) == {"pushed": False, "reason": "already-today"}


def test_daily_push_lock_held(monkeypatch):
    monkeypatch.setattr(gitpush, "_pushed_today", lambda: False)

    def _held():
        raise gitpush.locking.LockHeld()
    monkeypatch.setattr(gitpush.locking, "single_flight", _held)
    assert gitpush.daily_push(_Cfg(), LOG) == {"pushed": False, "reason": "lock-held"}


def test_daily_push_delegates_under_lock(monkeypatch):
    monkeypatch.setattr(gitpush, "_pushed_today", lambda: False)
    monkeypatch.setattr(gitpush.locking, "single_flight", lambda: contextlib.nullcontext())
    monkeypatch.setattr(gitpush, "_do_push", lambda cfg, log, force, message: {"pushed": True})
    assert gitpush.daily_push(_Cfg(), LOG, force=True)["pushed"] is True


# --------------------------- _cap_tracked_logs --------------------------- #

def test_cap_tracked_logs_truncates_only_oversized_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(gitpush, "ROOT", tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "tick.log").write_text("\n".join(str(i) for i in range(600)) + "\n")
    (logs / "small.log").write_text("a\nb\n")
    (logs / "notes.txt").write_text("not a log\n")
    monkeypatch.setattr(gitpush, "_git", lambda *a: _completed(
        stdout="logs/tick.log\nlogs/small.log\nlogs/notes.txt\nlogs/gone.log\n"))
    assert gitpush._cap_tracked_logs(keep_lines=500) == 1
    assert len((logs / "tick.log").read_text().splitlines()) == 500     # truncated to tail
    assert (logs / "small.log").read_text() == "a\nb\n"                 # under cap, untouched


# --------------------------- _do_push exits --------------------------- #

def _wire_do_push(monkeypatch, fake_git, *, secret_ok=True):
    monkeypatch.setattr(gitpush, "current_branch", lambda: "main")
    monkeypatch.setattr(gitpush, "_cap_tracked_logs", lambda: 0)
    monkeypatch.setattr(gitpush, "_secret_scan", lambda: (secret_ok, "" if secret_ok else "LEAK in file"))
    monkeypatch.setattr(gitpush, "_count_today_all_personas", lambda kind=None: 1)
    monkeypatch.setattr(gitpush, "_count_today_all_ledgers", lambda: 0)
    monkeypatch.setattr(gitpush, "_mark_pushed", lambda: None)
    monkeypatch.setattr(gitpush, "_git", fake_git)


def test_do_push_secret_scan_abort_resets_index(monkeypatch):
    calls = []

    def fake_git(*a):
        calls.append(a)
        return _completed(stdout=" M state/x\n") if a[:2] == ("status", "--porcelain") else _completed()
    _wire_do_push(monkeypatch, fake_git, secret_ok=False)
    res = gitpush._do_push(_Cfg(), LOG, force=True)
    assert res["reason"] == "secret-scan-abort" and "LEAK" in res["detail"]
    assert ("reset",) in calls                                          # index unstaged after abort


def test_do_push_dry_run_resets_without_commit(monkeypatch):
    calls = []

    def fake_git(*a):
        calls.append(a)
        if a[:2] == ("status", "--porcelain"):
            return _completed(stdout=" M state/x\n")
        if a[:2] == ("diff", "--cached"):
            return _completed(stdout="state/x | 2 +-\n")
        return _completed()
    _wire_do_push(monkeypatch, fake_git)
    res = gitpush._do_push(_DryCfg(), LOG, force=False)                 # force=False -> DRY_RUN branch
    assert res["reason"] == "dry_run" and "state/x" in res["diffstat"]
    assert ("reset",) in calls and not any(a[0] == "commit" for a in calls)


def test_do_push_commit_failure(monkeypatch):
    def fake_git(*a):
        if a[:2] == ("status", "--porcelain"):
            return _completed(stdout=" M x\n")
        if a[0] == "commit":
            return _completed(returncode=1, stderr="commit boom")
        return _completed()
    _wire_do_push(monkeypatch, fake_git)
    res = gitpush._do_push(_Cfg(), LOG, force=True)
    assert res["reason"] == "commit-failed" and "boom" in res["detail"]


def test_do_push_sets_upstream_when_missing(monkeypatch):
    pushes = []

    def fake_git(*a):
        if a[:2] == ("status", "--porcelain"):
            return _completed(stdout=" M x\n")
        if a[0] == "push":
            pushes.append(a)
            if len(pushes) == 1:
                return _completed(returncode=1, stderr="fatal: The current branch main has no upstream branch")
            return _completed(returncode=0)
        return _completed()
    _wire_do_push(monkeypatch, fake_git)
    res = gitpush._do_push(_Cfg(), LOG, force=True)
    assert res["pushed"] is True
    assert pushes[1][:3] == ("push", "-u", "origin")                   # set upstream then push


def test_do_push_rebase_conflict_keeps_commit_local(monkeypatch):
    calls = []

    def fake_git(*a):
        calls.append(a)
        if a[:2] == ("status", "--porcelain"):
            return _completed(stdout=" M x\n")
        if a[0] == "push":
            return _completed(returncode=1, stderr="! [rejected] main -> main (non-fast-forward)")
        if a[:2] == ("pull", "--rebase"):
            return _completed(returncode=1, stderr="CONFLICT (content)")
        return _completed()
    _wire_do_push(monkeypatch, fake_git)
    res = gitpush._do_push(_Cfg(), LOG, force=True)
    assert res["reason"] == "push-rebase-conflict"
    assert ("rebase", "--abort") in calls                              # wedged rebase recovered


def test_do_push_hard_push_failure(monkeypatch):
    def fake_git(*a):
        if a[:2] == ("status", "--porcelain"):
            return _completed(stdout=" M x\n")
        if a[0] == "push":
            return _completed(returncode=1, stderr="fatal: unable to access remote (network)")
        return _completed()
    _wire_do_push(monkeypatch, fake_git)
    res = gitpush._do_push(_Cfg(), LOG, force=True)
    assert res["reason"] == "push-failed" and "network" in res["detail"]


def test_do_push_success(monkeypatch):
    def fake_git(*a):
        return _completed(stdout=" M x\n") if a[:2] == ("status", "--porcelain") else _completed()
    _wire_do_push(monkeypatch, fake_git)
    res = gitpush._do_push(_Cfg(), LOG, force=True)
    assert res == {"pushed": True, "ticks": 1, "emails": 0}


# --------------------------- main --------------------------- #

def test_main_prints_result(monkeypatch, capsys):
    from cagent import logging_setup
    monkeypatch.setattr(gitpush.config, "load", lambda *a, **k: _Cfg())
    monkeypatch.setattr(logging_setup, "setup", lambda: LOG)
    monkeypatch.setattr(gitpush, "daily_push", lambda cfg, log: {"pushed": False, "reason": "clean"})
    assert gitpush.main() == 0
    assert "daily_push" in capsys.readouterr().out
