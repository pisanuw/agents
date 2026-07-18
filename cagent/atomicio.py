"""Shared low-level file IO for the agent's on-disk state: one atomic write, plus the two tolerant
readers (.jsonl and whole-JSON) that were open-coded in a dozen places.

The agent's continuity lives in committed files that the OTHER host pulls, so a torn write (process
killed mid-write, disk full) would otherwise become a corrupt committed-then-pulled file that silently
resets an arc, drops a backlog, or clears a backoff. tmp-in-the-same-dir + os.replace makes a write
all-or-nothing: a reader (or `git add`) only ever sees the complete old file or the complete new one.
The readers tolerate a partially-written / truncated line so one bad row never aborts a whole read.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def _fsync_dir(d: Path) -> None:
    # Fsync the directory so the os.replace rename itself is durable across a power loss, not merely
    # visible to the current process. Best-effort: some filesystems reject a directory fsync.
    try:
        dfd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())         # data on disk BEFORE the rename (durability, not just atomicity)
        os.replace(tmp, path)            # atomic on POSIX (same filesystem as the temp)
        _fsync_dir(path.parent)          # the rename survives a crash, not only a clean unmount
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_jsonl(path) -> list:
    """A .jsonl file -> list of parsed objects, skipping blank and undecodable lines (the tolerant
    read open-coded across cli/control/supervise). Missing file -> []. Callers apply their own
    filter/slice/tag on the returned list."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def read_json(path, default=None):
    """A whole JSON file -> its value, or `default` when missing or unparseable (the tolerant read
    open-coded for questions.json and friends)."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default
