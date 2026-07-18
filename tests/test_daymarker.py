"""Once-per-day marker primitive. Covers both on-disk shapes (plain date + JSON-with-payload),
the torn-marker-retries invariant, and clear()."""
import json

from cagent import clock, daymarker


def test_plain_marker_roundtrip(tmp_path):
    p = tmp_path / "last_push"
    assert daymarker.done_today(p) is False        # missing -> not done
    daymarker.mark(p)
    assert daymarker.done_today(p) is True
    assert p.read_text() == clock.today()          # bare-date format preserved (no migration)


def test_json_marker_carries_payload(tmp_path):
    p = tmp_path / "health_alert.json"
    daymarker.mark(p, kinds=["stale-heartbeat"], streak=3)
    assert daymarker.done_today(p) is True
    d = json.loads(p.read_text())
    assert d["date"] == clock.today()
    assert d["kinds"] == ["stale-heartbeat"] and d["streak"] == 3


def test_stale_date_is_not_today(tmp_path):
    p = tmp_path / "m"
    p.write_text("2020-01-01")                      # a previous day
    assert daymarker.done_today(p) is False
    q = tmp_path / "m.json"
    q.write_text(json.dumps({"date": "2020-01-01", "kinds": []}))
    assert daymarker.done_today(q) is False


def test_torn_marker_retries(tmp_path):
    p = tmp_path / "torn"
    p.write_text("{ not valid json")               # a half-written marker
    assert daymarker.done_today(p) is False         # fail toward retrying, never crash
    p.write_text("")
    assert daymarker.done_today(p) is False


def test_clear_removes_marker(tmp_path):
    p = tmp_path / "m"
    daymarker.mark(p)
    daymarker.clear(p)
    assert not p.exists()
    daymarker.clear(p)                              # idempotent (missing_ok)
