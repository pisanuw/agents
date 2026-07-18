"""The send-or-queue drain skeleton: unlink-on-success, leave-on-SendRefused (cap-bounded retry),
skip-on-None (not this item's turn), drop-on-corrupt."""
import json

from cagent import deferred, gmail


def _queue(tmp_path, **items):
    q = tmp_path / "queue"
    q.mkdir()
    for name, payload in items.items():
        (q / f"{name}.json").write_text(json.dumps(payload))
    return q


def test_delivered_items_are_unlinked_and_returned(tmp_path):
    q = _queue(tmp_path, a={"n": 1}, b={"n": 2})
    out = deferred.drain(q, lambda d: {"sent": d["n"]}, log=None)
    assert sorted(o["sent"] for o in out) == [1, 2]
    assert list(q.glob("*.json")) == []                    # both dequeued


def test_refused_item_stays_queued(tmp_path):
    q = _queue(tmp_path, a={"n": 1})

    def attempt(_d):
        raise gmail.SendRefused("global cap reached")
    out = deferred.drain(q, attempt, log=None)
    assert out == []
    assert (q / "a.json").exists()                         # left for a later tick, not dropped


def test_none_result_skips_without_dequeue(tmp_path):
    q = _queue(tmp_path, keep={"approved": False}, go={"approved": True})

    def attempt(d):
        return {"ok": True} if d.get("approved") else None
    out = deferred.drain(q, attempt, log=None)
    assert out == [{"ok": True}]
    assert (q / "keep.json").exists()                      # declined -> untouched
    assert not (q / "go.json").exists()                    # delivered -> removed


def test_corrupt_item_is_dropped(tmp_path):
    q = tmp_path / "queue"
    q.mkdir()
    (q / "bad.json").write_text("{ not json")
    calls = []
    out = deferred.drain(q, lambda d: calls.append(d) or {"ok": True}, log=None)
    assert out == [] and calls == []                       # never handed to attempt
    assert not (q / "bad.json").exists()                   # dropped so it can't wedge the queue


def test_missing_dir_is_empty(tmp_path):
    assert deferred.drain(tmp_path / "nope", lambda d: {"ok": True}, log=None) == []
