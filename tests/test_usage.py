"""Per-tick token accounting: the meter aggregates across every claude sub-call, parse extracts
the envelope's usage block, the tick journal + usage.json carry the totals, and the read-only
roll-up (cagentctl usage / usage_report) reconciles from committed journals on the mirror."""
import json
from types import SimpleNamespace

from cagent import usage_report
from cagent.cognition import invoke, meter, parse


def _envelope(inp, out, cache_read=0, cache_creation=0, cost=0.01, structured=None):
    """A minimal claude JSON envelope as run_claude captures it on stdout."""
    return invoke.RawEnvelope(
        stdout=json.dumps({
            "result": "ok", "total_cost_usd": cost, "num_turns": 1,
            "structured_output": structured if structured is not None else {"summary": "s", "actions": []},
            "usage": {"input_tokens": inp, "output_tokens": out,
                      "cache_read_input_tokens": cache_read,
                      "cache_creation_input_tokens": cache_creation},
        }),
        stderr="", code=0)


def test_parse_extracts_usage():
    r = parse.parse(_envelope(100, 20, cache_read=5, cache_creation=3))
    assert r.status == "OK"
    assert r.usage == {"input": 100, "output": 20, "cache_read": 5, "cache_creation": 3}


def test_parse_usage_none_when_absent():
    env = invoke.RawEnvelope(stdout=json.dumps({"structured_output": {}}), stderr="", code=0)
    assert parse.parse(env).usage is None


def test_meter_aggregates_across_calls():
    meter.reset()
    meter.record("tick", "sonnet", {"input": 100, "output": 20, "cache_read": 0, "cache_creation": 0}, 0.02)
    meter.record("gatecheck", "sonnet", {"input": 50, "output": 5, "cache_read": 10, "cache_creation": 0}, 0.01)
    meter.record("research", "sonnet", {"input": 200, "output": 40, "cache_read": 0, "cache_creation": 0}, 0.05)
    s, rows = meter.drain()
    assert len(rows) == 3 and s["calls"] == 3
    assert s["input"] == 350 and s["output"] == 65 and s["cache_read"] == 10
    assert s["total_tokens"] == 350 + 65 + 10
    assert round(s["cost_usd"], 4) == 0.08
    assert set(s["by_kind"]) == {"tick", "gatecheck", "research"}
    assert s["by_kind"]["research"]["input"] == 200 and s["by_kind"]["research"]["calls"] == 1
    # drained -> empty
    assert meter.drain()[0]["calls"] == 0


def test_meter_records_zero_row_for_unparseable_output():
    meter.reset()
    invoke._meter_call("tick", "sonnet", "not json", ms=5)
    s, rows = meter.drain()
    assert s["calls"] == 1 and s["total_tokens"] == 0 and rows[0]["kind"] == "tick"


def test_run_claude_meters(monkeypatch):
    """run_claude is the chokepoint: a call records exactly one metered row from the envelope."""
    meter.reset()

    class _Proc:
        returncode = 0
        def communicate(self, input=None, timeout=None):
            return _envelope(300, 30).stdout, ""
    monkeypatch.setattr(invoke.subprocess, "Popen", lambda *a, **k: _Proc())
    monkeypatch.setattr(invoke.config, "load", lambda *a, **k: SimpleNamespace(
        claude_bin="claude", model_tick="sonnet", tick_timeout_s=180))
    invoke.run_claude("hi", label="tick")
    s, _ = meter.drain()
    assert s["calls"] == 1 and s["by_kind"]["tick"]["input"] == 300


def _write_journal(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def test_aggregate_and_render(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_report.config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(usage_report.config, "known_personas", lambda: ["golf", "data"])
    u = {"input": 100, "output": 20, "cache_read": 0, "cache_creation": 0, "cost_usd": 0.03,
         "calls": 2, "total_tokens": 120, "by_kind": {"tick": {"input": 100, "output": 20,
         "cache_read": 0, "cache_creation": 0, "cost_usd": 0.03, "calls": 2}}}
    _write_journal(tmp_path / "state" / "personas" / "golf" / "journal.jsonl",
                   [{"kind": "tick", "ts": "2026-06-30T12:00:00+00:00", "usage": u},
                    {"kind": "tick", "ts": "2026-06-30T13:00:00+00:00", "cost_notional": 0.5}])  # legacy tick
    _write_journal(tmp_path / "state" / "personas" / "data" / "journal.jsonl",
                   [{"kind": "tick", "ts": "2026-06-30T12:00:00+00:00", "usage": u}])
    per = usage_report.aggregate()
    assert per["golf"]["ticks"] == 2 and per["golf"]["input"] == 100
    assert round(per["golf"]["cost_usd"], 4) == 0.53          # 0.03 metered + 0.5 legacy cost_notional
    assert per["data"]["total_tokens"] == 120
    txt = usage_report.render_text(per, by_kind=True)
    assert "TOTAL" in txt and "golf" in txt and "tick" in txt
    assert "timed out" not in txt                             # no footnote when there are no timeouts


def test_meter_flags_timeout():
    """A timed-out call is still one call, with 0 tokens, tagged so the report can surface it."""
    meter.reset()
    meter.record("tick", "sonnet", {"input": 200, "output": 30, "cache_read": 0, "cache_creation": 0}, 0.05)
    meter.record("tick", "sonnet", None, None, ms=300000, timed_out=True)   # killed before envelope
    s, rows = meter.drain()
    assert s["calls"] == 2 and s["timeouts"] == 1 and s["total_tokens"] == 230
    assert s["by_kind"]["tick"]["timeouts"] == 1
    assert rows[1]["timed_out"] is True and rows[0]["timed_out"] is False


def test_run_claude_meters_timeout(monkeypatch):
    """On timeout run_claude kills the process group and still records a zero, timed-out row."""
    meter.reset()

    class _Proc:
        pid = 4321
        returncode = -9
        def __init__(self):
            self._n = 0
        def communicate(self, input=None, timeout=None):
            self._n += 1
            if self._n == 1:
                raise invoke.subprocess.TimeoutExpired("claude", timeout)
            return "", "killed"                                 # post-kill drain: empty stdout
    monkeypatch.setattr(invoke.subprocess, "Popen", lambda *a, **k: _Proc())
    monkeypatch.setattr(invoke.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(invoke.os, "killpg", lambda pgid, sig: None)
    monkeypatch.setattr(invoke.config, "load", lambda *a, **k: SimpleNamespace(
        claude_bin="claude", model_tick="sonnet", tick_timeout_s=1))
    env = invoke.run_claude("hi", label="tick", timeout_s=1)
    assert env.timed_out is True
    s, rows = meter.drain()
    assert s["calls"] == 1 and s["timeouts"] == 1 and s["total_tokens"] == 0
    assert rows[0]["timed_out"] is True


def test_build_email_appends_extra(tmp_path, monkeypatch):
    """The oauth-usage snapshot rides along in the email body when passed as `extra`; without it the
    body is unchanged (so a failed/absent snapshot never corrupts the report)."""
    monkeypatch.setattr(usage_report.config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(usage_report.config, "known_personas", lambda: ["golf"])
    u = {"input": 10, "output": 2, "cache_read": 0, "cache_creation": 0, "cost_usd": 0.01,
         "calls": 1, "timeouts": 0, "total_tokens": 12, "by_kind": {}}
    _write_journal(tmp_path / "state" / "personas" / "golf" / "journal.jsonl",
                   [{"kind": "tick", "ts": "2026-06-30T12:00:00+00:00", "usage": u}])
    subj_plain, body_plain = usage_report.build_email(days=1)
    snapshot = "=== rate-limit windows ===\nfive_hour  45%  ok"
    subj, body = usage_report.build_email(days=1, extra=snapshot)
    assert subj == subj_plain                                  # extra changes only the body
    assert snapshot in body and "five_hour  45%  ok" in body
    assert snapshot not in body_plain
    for blank in ("", "   ", None):                            # empty/absent extra is a no-op
        assert usage_report.build_email(days=1, extra=blank)[1] == body_plain


def test_render_surfaces_timeouts(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_report.config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(usage_report.config, "known_personas", lambda: ["alpha"])
    u = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "cost_usd": 0.0,
         "calls": 1, "timeouts": 1, "total_tokens": 0,
         "by_kind": {"tick": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0,
                              "cost_usd": 0.0, "calls": 1, "timeouts": 1}}}
    _write_journal(tmp_path / "state" / "personas" / "alpha" / "journal.jsonl",
                   [{"kind": "tick", "ts": "2026-06-30T12:00:00+00:00", "usage": u}])
    per = usage_report.aggregate()
    assert per["alpha"]["timeouts"] == 1
    txt = usage_report.render_text(per, by_kind=True)
    assert "1 call(s) timed out" in txt and "alpha 1" in txt
    assert "(1 timed out)" in txt                              # by-kind annotation
