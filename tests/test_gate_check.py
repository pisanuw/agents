"""Gate-check ground-truth fixes (2026-07): the gate was starved of the notes/state the drafter
wrote from, so true claims read as fabrication and personas stalled silently for days. These cover
(1) draft-ranked note selection, (2) enriched gate sources incl. agent state, and (3) the stall alarm."""
import json
import logging
from types import SimpleNamespace

from cagent import clock, config, gmail, goals as goals_mod, memory, supervise
from cagent.cognition import execute

log = logging.getLogger("t")


def test_select_by_text_ranks_by_draft_overlap(monkeypatch):
    entries = [
        {"id": "n1", "title": "honeybee quorum sensing", "summary": "scouts waggle dance competition"},
        {"id": "n2", "title": "climate tipping points", "summary": "AMOC collapse hysteresis"},
        {"id": "n3", "title": "immune Treg circuits", "summary": "antigen derivative sensitivity"},
    ]
    monkeypatch.setattr(memory, "index_entries", lambda: entries)
    draft = "The honeybee colony uses quorum sensing and waggle dance competition to decide."
    got = memory.select_by_text(draft, 2)
    assert got[0]["id"] == "n1"                    # ranked by overlap with the DRAFT, not by goals


def test_gate_sources_includes_agent_state_and_draft_notes(monkeypatch):
    monkeypatch.setattr(goals_mod, "load", lambda: [{"id": "G1", "status": "active", "title": "immune systems",
                                                      "created": "2026-06-20", "parent": "G0"}])
    monkeypatch.setattr(goals_mod, "archived", lambda: [{"id": "G6", "title": "retired inquiry"}])
    monkeypatch.setattr(execute, "_recent_journal", lambda n=8: "- 2026-07-01 ok five research passes dispatched")
    monkeypatch.setattr(execute, "_latest_reflection", lambda: "Open question: the illusionism fork")
    note = {"id": "29-x", "kind": "research", "title": "honeybee quorum"}
    monkeypatch.setattr(memory, "select_by_text", lambda text, n=24: [note])
    monkeypatch.setattr(memory, "body_of", lambda e: "Seeley & Visscher; Bell et al 2021 quorum threshold")
    src = execute._gate_sources("draft citing honeybee quorum and Bell et al 2021")
    assert "=== AGENT STATE" in src and "=== RESEARCH NOTES" in src
    assert "[G1] active (created 2026-06-20, parent G0): immune systems" in src  # id/status/created/parent verifiable
    assert "[G6] retired (created ?): retired inquiry" in src   # retired-goal claim verifiable (no created -> placeholder)
    assert "five research passes" in src             # journal -> 'the Nth pass' / tick-count claims
    assert "illusionism fork" in src                 # reflection included
    assert "Bell et al 2021" in src                  # the cited note's BODY is present (Class-2 fix)


def test_gate_block_streak_counts_until_delivery(tmp_path):
    rows = [
        {"kind": "tick", "results": [{"type": "send_email", "supervised": True}]},   # delivered (oldest)
        {"kind": "tick", "results": [{"type": "send_email", "blocked_by_gate": {"fabrication": ["x"]}}]},
        {"kind": "tick", "results": [{"type": "send_email", "blocked_by_gate": {"metaphor_leak": ["y"]},
                                      "revised": True}]},
    ]
    (tmp_path / "journal.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    streak, reason = supervise.gate_block_streak(tmp_path)
    assert streak == 2                               # both recent blocks, stopping at the delivered one
    assert "metaphor_leak" in reason and "after revise" in reason


def test_check_gate_stall_alerts_once_per_day_then_clears(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "state_root", lambda: tmp_path)
    monkeypatch.setattr(clock, "today", lambda: "2026-07-01")
    sent = []
    monkeypatch.setattr(gmail, "send", lambda **kw: sent.append(kw) or SimpleNamespace(dry_run=True))
    cfg = SimpleNamespace(persona="data", MODE="SUPERVISED")
    (tmp_path / "journal.jsonl").write_text("\n".join(json.dumps(
        {"kind": "tick", "results": [{"type": "send_email", "blocked_by_gate": {"fabrication": ["x"]}}]})
        for _ in range(3)))

    r1 = supervise.check_gate_stall(cfg, log, threshold=3)
    assert r1["alerted"] and r1["streak"] == 3 and len(sent) == 1
    assert "[stall]" in sent[0]["subject"]
    r2 = supervise.check_gate_stall(cfg, log, threshold=3)   # same day -> no duplicate alert
    assert r2["alerted"] is False and len(sent) == 1

    # a delivered send ends the stall and clears the dedupe flag
    (tmp_path / "journal.jsonl").write_text(json.dumps(
        {"kind": "tick", "results": [{"type": "send_email", "supervised": True}]}))
    r3 = supervise.check_gate_stall(cfg, log, threshold=3)
    assert r3["stalled"] is False and not (tmp_path / "gate_stall_alert.json").exists()
