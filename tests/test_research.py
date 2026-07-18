"""cognition/research.run: the ONLY tool-enabled sub-call. Covers every exit — the !NO-RESEARCH
flag, the per-day cap, an OK structured result, a parse error, and a rate-limit — plus the ledger
accounting (`_today_count`/`_record`) that enforces the cap. All claude/parse I/O is stubbed; the
real network is already blocked by conftest.

Key invariants pinned here: the flag and the cap short-circuit BEFORE any claude call; every real
sub-call attempt is recorded against the daily cap regardless of parse outcome (a persistently
failing web call must not re-fire forever); a rate-limit propagates to the backoff gate.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cagent import clock, config
from cagent.cognition import backoff, research


@pytest.fixture
def rsandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "state_root", lambda *a: tmp_path)
    monkeypatch.setattr(research.config, "load",
                        lambda: SimpleNamespace(research_per_day=3, model_tick="sonnet"))
    monkeypatch.setattr(research.persona, "constitution", lambda: "CONSTITUTION")
    return tmp_path


def _rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_no_research_flag_disables_call(rsandbox, monkeypatch):
    (rsandbox / "no_research.flag").write_text("")
    calls = []
    monkeypatch.setattr(research.invoke, "run_claude", lambda *a, **k: calls.append(1))
    assert research.run("q") is None and calls == []            # short-circuits before any claude call


def test_daily_cap_blocks_further_research(rsandbox, monkeypatch):
    (rsandbox / "research_ledger.jsonl").write_text(
        "\n".join(json.dumps({"date": clock.today()}) for _ in range(3)))
    calls = []
    monkeypatch.setattr(research.invoke, "run_claude", lambda *a, **k: calls.append(1))
    assert research.run("q") is None and calls == []            # cap reached -> no call


def test_ok_result_returns_structured_and_records_findings(rsandbox, monkeypatch):
    monkeypatch.setattr(research.invoke, "run_claude", lambda *a, **k: object())
    out = {"query": "q", "findings": [{"claim": "a"}, {"claim": "b"}]}
    monkeypatch.setattr(research.parse, "parse",
                        lambda env: SimpleNamespace(status="OK", structured=out, rate_limited=False, http=None))
    assert research.run("what is x") == out
    rows = _rows(rsandbox / "research_ledger.jsonl")
    assert rows[-1]["findings"] == 2 and rows[-1]["query"] == "what is x"


def test_parse_error_returns_none_but_still_counts(rsandbox, monkeypatch):
    monkeypatch.setattr(research.invoke, "run_claude", lambda *a, **k: object())
    monkeypatch.setattr(research.parse, "parse",
                        lambda env: SimpleNamespace(status="BAD_JSON", structured=None,
                                                    rate_limited=False, http=None))
    assert research.run("q") is None
    assert _rows(rsandbox / "research_ledger.jsonl")[-1]["findings"] == 0   # counted even on failure


def test_rate_limited_records_backoff(rsandbox, monkeypatch):
    monkeypatch.setattr(research.invoke, "run_claude", lambda *a, **k: object())
    monkeypatch.setattr(research.parse, "parse",
                        lambda env: SimpleNamespace(status="RATE_LIMIT", structured=None,
                                                    rate_limited=True, http=429))
    rec = []
    monkeypatch.setattr(backoff, "record_failure", lambda status, http=None: rec.append((status, http)))
    assert research.run("q") is None
    assert rec == [("RATE_LIMIT", 429)]


def test_today_count_ignores_other_days(rsandbox):
    (rsandbox / "research_ledger.jsonl").write_text(
        json.dumps({"date": clock.today()}) + "\n" + json.dumps({"date": "2000-01-01"}) + "\n")
    assert research._today_count() == 1


def test_record_appends_ledger_line(rsandbox):
    research._record("my query", 3)
    row = _rows(rsandbox / "research_ledger.jsonl")[0]
    assert row["findings"] == 3 and row["query"] == "my query" and row["date"] == clock.today()
