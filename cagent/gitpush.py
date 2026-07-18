"""Daily commit + push. A SEPARATE entrypoint (its own launchd job), never the cognition
tick — the tools-off model can never run git; only this deterministic code does. Triple
secret guard: .gitignore + the pre-commit/pre-push gitleaks hooks + an in-process scan here
(non-interactive git can be told to skip hooks, so we re-scan and ABORT before committing).
"""
from __future__ import annotations

import json
import os
import subprocess

from cagent import clock, config, daymarker, locking

ROOT = config.REPO_ROOT
LAST_PUSH = ROOT / "var" / "last_push"
SECRET_GUARD = ROOT / ".githooks" / "secret_guard.py"
JOURNAL = ROOT / "state" / "journal.jsonl"
LEDGER = ROOT / "state" / "send_ledger.jsonl"
TRAILER = "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"

# The agent commits tick state onto exactly ONE branch. This single working tree is shared with any
# interactive `git checkout`, so without a guard a stray branch switch would silently divert tick
# commits (state/logs) onto that branch -- or, with no upstream, strand them locally. Every git
# MUTATION (pull/commit/push, here and in control.pull) is gated on HEAD matching this branch.
# Override only if the repo's default branch is not `main`.
EXPECTED_BRANCH = os.environ.get("CAGENT_GIT_BRANCH", "main")


def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def current_branch() -> str:
    """The working tree's checked-out branch, or '' if git fails; 'HEAD' when detached."""
    r = _git("rev-parse", "--abbrev-ref", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else ""


def on_expected_branch() -> tuple[bool, str]:
    """(ok, branch). ok is False when HEAD is off EXPECTED_BRANCH, including detached HEAD."""
    b = current_branch()
    return b == EXPECTED_BRANCH, b


def _pushed_today() -> bool:
    return daymarker.done_today(LAST_PUSH)


def _mark_pushed() -> None:
    daymarker.mark(LAST_PUSH)


def _count_today(path, kind=None, skip_dry_run=False) -> int:
    if not path.exists():
        return 0
    n = 0
    for line in path.read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not str(e.get("ts", "")).startswith(clock.today()):
            continue
        if kind and e.get("kind") != kind:
            continue
        if skip_dry_run and e.get("dry_run"):     # ledgers: dry-run sends don't count against caps
            continue
        n += 1
    return n


def _count_today_all(glob_pattern: str, fallback, *, kind=None, skip_dry_run=False) -> int:
    """Aggregate _count_today across every per-persona file matching glob_pattern under
    state/personas/*/; fall back to the legacy flat `fallback` path when no persona dirs exist. One
    helper for both the journal (tick count) and ledger (send count) aggregates -- this fixes the
    always-'0 ticks' commit message in multi-persona deployments (the flat paths are empty once the
    agent migrated to namespaced state)."""
    per_persona = list(config.personas_state_root().glob(glob_pattern))
    if not per_persona:
        return _count_today(fallback, kind=kind, skip_dry_run=skip_dry_run)
    return sum(_count_today(p, kind=kind, skip_dry_run=skip_dry_run) for p in per_persona)


def _count_today_all_personas(kind=None) -> int:
    return _count_today_all("*/journal.jsonl", JOURNAL, kind=kind)


def _count_today_all_ledgers() -> int:
    return _count_today_all("*/send_ledger.jsonl", LEDGER, skip_dry_run=True)


def _is_non_fast_forward(proc: subprocess.CompletedProcess) -> bool:
    """True when a push was rejected because origin moved ahead (so a rebase+retry can recover),
    vs. a hard failure (auth/network) where retrying would not help."""
    out = (proc.stderr + proc.stdout).lower()
    return "non-fast-forward" in out or "fetch first" in out or "[rejected]" in out


def _secret_scan() -> tuple[bool, str]:
    r = subprocess.run(["python3", str(SECRET_GUARD)], cwd=ROOT, capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or r.stdout)


def daily_push(cfg, log, force: bool = False, message: str | None = None) -> dict:
    if not force and _pushed_today():
        log.info("daily push: already pushed today")
        return {"pushed": False, "reason": "already-today"}
    try:
        with locking.single_flight():
            return _do_push(cfg, log, force, message)
    except locking.LockHeld:
        log.info("daily push: lock held by a tick; will retry next run")
        return {"pushed": False, "reason": "lock-held"}


def _cap_tracked_logs(keep_lines: int = 500) -> int:
    """Truncate oversized tracked log files to keep_lines before committing. Only touches files
    git already tracks (logs/ is deliberately tracked for remote monitoring despite being listed
    as gitignored in CLAUDE.md). Prevents append-only logs from adding a new full-file blob every
    daily push and bloating git history unboundedly."""
    r = _git("ls-files", "logs/")
    n = 0
    for rel in r.stdout.splitlines():
        p = ROOT / rel
        if not p.exists() or p.suffix != ".log":
            continue
        lines = p.read_text(errors="replace").splitlines()
        if len(lines) > keep_lines:
            p.write_text("\n".join(lines[-keep_lines:]) + "\n")
            n += 1
    return n


def _do_push(cfg, log, force: bool, message: str | None = None) -> dict:
    # Refuse to commit/push unless HEAD is on the agent's branch. Checked BEFORE touching the index,
    # so a stray checkout leaves tick state local + uncommitted (recoverable) rather than diverted.
    ok, branch = on_expected_branch()
    if not ok:
        log.info("daily push: REFUSING git -- HEAD on %r, not the agent branch %r; tick state stays "
                 "local + uncommitted so a stray checkout cannot divert commits. Run `git switch %s`.",
                 branch or "(detached)", EXPECTED_BRANCH, EXPECTED_BRANCH)
        return {"pushed": False, "reason": "wrong-branch", "branch": branch}

    capped_logs = _cap_tracked_logs()
    if capped_logs:
        log.info("daily push: truncated %d tracked log file(s) to 500 lines", capped_logs)
    _git("add", "-A")
    if not _git("status", "--porcelain").stdout.strip():
        log.info("daily push: nothing to commit")
        return {"pushed": False, "reason": "clean"}

    ok, err = _secret_scan()
    if not ok:
        log.info("daily push: SECRET SCAN ABORTED commit: %s", err.strip()[:200])
        _git("reset")
        return {"pushed": False, "reason": "secret-scan-abort", "detail": err.strip()[:300]}

    ticks = _count_today_all_personas(kind="tick")
    emails = _count_today_all_ledgers()
    msg = f"{message or f'cagent daily {clock.today()}: {ticks} ticks, {emails} emails'}\n\n{TRAILER}"

    if cfg.MODE == "DRY_RUN" and not force:
        diff = _git("diff", "--cached", "--stat").stdout
        _git("reset")
        log.info("daily push: DRY_RUN, would commit:\n%s", diff[:400])
        return {"pushed": False, "reason": "dry_run", "diffstat": diff[:400]}

    # Log the outcome BEFORE committing, then re-stage. logs/ is a tracked directory (committed for
    # remote monitoring), so a log line written AFTER the push would append to an already-committed
    # file and leave the repo dirty every single cycle. Emitting it here folds the line into THIS
    # commit; the flush-on-emit FileHandler guarantees it is on disk before the `git add`.
    log.info("daily push: committing + pushing (%d ticks, %d emails)", ticks, emails)
    _git("add", "-A")

    c = _git("commit", "-m", msg)
    if c.returncode != 0:
        log.info("daily push: commit failed: %s", (c.stderr or c.stdout)[:300])
        return {"pushed": False, "reason": "commit-failed", "detail": (c.stderr or c.stdout)[:300]}

    p = _git("push")
    if p.returncode != 0 and "no upstream" in (p.stderr + p.stdout).lower():
        # We verified HEAD == EXPECTED_BRANCH above, so this pushes the current branch (not a
        # hardcoded `main` that could mismatch HEAD and push stale refs).
        p = _git("push", "-u", "origin", EXPECTED_BRANCH)
    if p.returncode != 0 and _is_non_fast_forward(p):
        # origin advanced since our last pull (on this two-host setup the other host pushes its own
        # tick/state commits). Rebase our just-made commit on top and retry once, rather than leaving
        # it stranded locally (which silently stalls remote monitoring until a much later cycle).
        log.info("daily push: non-fast-forward; rebasing on origin/%s and retrying once", EXPECTED_BRANCH)
        pr = _git("pull", "--rebase", "--autostash")
        if pr.returncode != 0:
            _git("rebase", "--abort")   # leave a clean HEAD; the commit stays local for the next run
            log.info("daily push: rebase-before-retry conflicted; commit kept local: %s",
                     (pr.stderr or pr.stdout)[:200])
            return {"pushed": False, "reason": "push-rebase-conflict", "detail": (pr.stderr or pr.stdout)[:300]}
        p = _git("push")
    if p.returncode != 0:
        log.info("daily push: push failed: %s", (p.stderr or p.stdout)[:300])
        return {"pushed": False, "reason": "push-failed", "detail": (p.stderr or p.stdout)[:300]}

    _mark_pushed()
    return {"pushed": True, "ticks": ticks, "emails": emails}


def main() -> int:
    from cagent import logging_setup
    cfg = config.load()
    log = logging_setup.setup()
    res = daily_push(cfg, log)
    print("daily_push:", res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
