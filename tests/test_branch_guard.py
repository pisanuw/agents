"""Branch guard: this one working tree is shared with any interactive `git checkout`, so every git
MUTATION the agent makes (control.pull's rebase + gitpush's commit/push) is gated on HEAD matching
gitpush.EXPECTED_BRANCH. A stray branch must never silently divert tick commits (state/logs) onto
another branch, nor strand them. These tests pin that contract."""
import subprocess

from cagent import control, gitpush


class _Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


LOG = _Log()


class _Cfg:
    MODE = "SUPERVISED"


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(["git"], returncode, stdout, stderr)


# --- branch detection -------------------------------------------------------------------

def test_on_expected_branch_true(monkeypatch):
    monkeypatch.setattr(gitpush, "_git", lambda *a: _completed(stdout="main\n"))
    assert gitpush.current_branch() == "main"
    assert gitpush.on_expected_branch() == (True, "main")


def test_on_expected_branch_false(monkeypatch):
    monkeypatch.setattr(gitpush, "_git", lambda *a: _completed(stdout="feature/x\n"))
    assert gitpush.on_expected_branch() == (False, "feature/x")


def test_detached_head_is_off_branch(monkeypatch):
    # `git rev-parse --abbrev-ref HEAD` prints "HEAD" when detached -> not the agent branch.
    monkeypatch.setattr(gitpush, "_git", lambda *a: _completed(stdout="HEAD\n"))
    assert gitpush.on_expected_branch() == (False, "HEAD")


# --- gitpush refuses to commit/push off-branch, WITHOUT touching the index ---------------

def test_do_push_refuses_off_branch_and_leaves_index_untouched(monkeypatch):
    calls = []
    monkeypatch.setattr(gitpush, "current_branch", lambda: "feature-x")
    monkeypatch.setattr(gitpush, "_git", lambda *a: calls.append(a) or _completed())

    res = gitpush._do_push(_Cfg(), LOG, force=True)

    assert res == {"pushed": False, "reason": "wrong-branch", "branch": "feature-x"}
    # The guard returns BEFORE `git add -A`, so the index/working tree are never staged: nothing
    # the tick wrote can land on the stray branch.
    assert calls == []


def test_do_push_proceeds_on_branch(monkeypatch):
    monkeypatch.setattr(gitpush, "current_branch", lambda: "main")

    def fake_git(*a):
        if a[:2] == ("status", "--porcelain"):
            return _completed(stdout="")          # clean tree -> _do_push stops at "nothing to commit"
        return _completed()

    monkeypatch.setattr(gitpush, "_git", fake_git)
    res = gitpush._do_push(_Cfg(), LOG, force=True)
    # Reaching "clean" proves the branch guard let `main` through rather than refusing.
    assert res == {"pushed": False, "reason": "clean"}


# --- control.pull skips the rebase when off-branch --------------------------------------

def test_pull_skips_off_branch(monkeypatch):
    monkeypatch.setattr(gitpush, "on_expected_branch", lambda: (False, "feature"))

    def boom(*a, **k):
        raise AssertionError("git pull must not run while off the agent branch")

    monkeypatch.setattr(control.subprocess, "run", boom)
    assert control.pull(LOG) is False


def test_pull_runs_on_branch(monkeypatch):
    monkeypatch.setattr(gitpush, "on_expected_branch", lambda: (True, "main"))
    ran = []
    monkeypatch.setattr(control.subprocess, "run",
                        lambda *a, **k: ran.append(a) or _completed(returncode=0))
    assert control.pull(LOG) is True
    assert ran, "on-branch pull should actually shell out to git pull"


def test_pull_aborts_wedged_rebase_on_conflict(monkeypatch):
    # H3: a failed `git pull --rebase` leaves the tree wedged mid-rebase; control.pull must abort it
    # so the later tick + auto-push run against a clean HEAD (both hosts commit state/ on main).
    monkeypatch.setattr(gitpush, "on_expected_branch", lambda: (True, "main"))
    calls = []

    def fake_run(args, **k):
        calls.append(tuple(args))
        if args[:3] == ["git", "pull", "--rebase"]:
            return _completed(returncode=1, stderr="CONFLICT (content): Merge conflict in state/x\n")
        return _completed(returncode=0)                      # the rebase --abort

    monkeypatch.setattr(control.subprocess, "run", fake_run)
    assert control.pull(LOG) is False
    assert ("git", "rebase", "--abort") in calls             # wedged rebase was recovered


def test_do_push_retries_push_on_non_fast_forward(monkeypatch):
    # M3: when origin advanced (the other host pushed), rebase our commit on top and retry once,
    # rather than stranding it locally.
    monkeypatch.setattr(gitpush, "current_branch", lambda: "main")
    monkeypatch.setattr(gitpush, "_secret_scan", lambda: (True, ""))
    monkeypatch.setattr(gitpush, "_count_today", lambda *a, **k: 0)
    monkeypatch.setattr(gitpush, "_mark_pushed", lambda: None)
    pushes = {"n": 0}
    calls = []

    def fake_git(*a):
        calls.append(a)
        if a[:2] == ("status", "--porcelain"):
            return _completed(stdout=" M state/x\n")          # dirty -> proceeds to commit
        if a[0] == "push":
            pushes["n"] += 1
            if pushes["n"] == 1:
                return _completed(returncode=1, stderr="! [rejected] main -> main (non-fast-forward)\n")
            return _completed(returncode=0)                   # retry succeeds
        return _completed(returncode=0)                       # commit, pull --rebase, etc.

    monkeypatch.setattr(gitpush, "_git", fake_git)
    res = gitpush._do_push(_Cfg(), LOG, force=True)
    assert res.get("pushed") is True
    assert pushes["n"] == 2                                    # pushed twice (retry after rebase)
    assert ("pull", "--rebase", "--autostash") in calls       # rebased our commit before retrying
