"""Single-flight lock shared by EVERY entrypoint (heartbeat tick + daily push), so
overlapping runs are impossible and the push can never fire mid-write. Uses Python
fcntl.flock (the `flock` CLI is absent on this host)."""
from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager

from cagent import config

LOCK_PATH = config.REPO_ROOT / "var" / "agent.lock"


class LockHeld(Exception):
    """Another entrypoint holds the lock."""


@contextmanager
def single_flight():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Open in append mode so the loser's open() does NOT truncate the winner's PID before
    # the flock decides (L6). We truncate and write our own PID only AFTER we hold the lock.
    f = open(LOCK_PATH, "a")
    try:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise LockHeld() from e
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        f.close()
