"""reflect.run apply path: a successful reflection retires goals, spawns new ones (enforcing the
5-active-goal code cap regardless of model output), stashes the headline question, writes a
reflection note, and steps the persona arc. Cadence/deep-marker/failure paths are covered in
test_reflect_cadence; this covers the mutation fan-out the cadence tests stub away."""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

from cagent import config, reflect

log = logging.getLogger("t")


def test_run_applies_retire_create_cap_and_headline(tmp_path, monkeypatch):
    monkeypatch.setattr(reflect, "_last", lambda: tmp_path / "last.json")
    monkeypatch.setattr(reflect, "_last_deep", lambda: tmp_path / "last_deep.json")
    monkeypatch.setattr(reflect, "_request", lambda: tmp_path / "req.json")
    monkeypatch.setattr(reflect, "_journal", lambda: tmp_path / "journal.jsonl")
    monkeypatch.setattr(reflect, "_questions_path", lambda: tmp_path / "q.json")
    monkeypatch.setattr(reflect.persona, "load_system_prompt", lambda: "")
    monkeypatch.setattr(reflect.memory, "recent", lambda n: [])
    monkeypatch.setattr(reflect.memory, "write_note", lambda *a, **k: "note-path")
    monkeypatch.setattr(reflect.goals_mod, "load", lambda: [])

    retired, created, arc = [], [], []
    active_count = {"n": 0}
    monkeypatch.setattr(reflect.goals_mod, "retire", lambda gid, why="": retired.append(gid))
    monkeypatch.setattr(reflect.goals_mod, "active", lambda: list(range(active_count["n"])))

    def _upsert(item, rationale=""):
        active_count["n"] += 1
        created.append(item["title"])
    monkeypatch.setattr(reflect.goals_mod, "upsert", _upsert)
    monkeypatch.setattr(reflect.persona, "arc_step", lambda o: arc.append(o))
    monkeypatch.setattr(reflect.invoke, "run_claude", lambda *a, **k: {})

    out = {"goals_to_retire": [{"id": "G1", "why": "done"}],
           "new_goals": [{"title": f"new{i}", "description": "d", "priority": 2} for i in range(7)],
           "headline_question": "what next?", "summary_of_progress": "made progress"}
    monkeypatch.setattr(reflect.parse, "parse", lambda env: SimpleNamespace(status="OK", structured=out))

    res = reflect.run(config.load(), log)
    assert res["ok"] and res["retired"] == ["G1"]
    assert len(created) == 5                              # 7 proposed, capped at 5 active
    assert res["headline_question"] == "what next?"
    assert "what next?" in json.loads((tmp_path / "q.json").read_text())   # headline stashed
    assert arc == [{"victories": 5, "tribulations": 1}]  # arc stepped with the mutation counts
    assert (tmp_path / "last.json").exists()              # cadence marker recorded


def test_path_helpers_and_build_context(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "state_root", lambda *a: tmp_path)
    assert reflect._last() == tmp_path / "last_reflection.json"
    assert reflect._last_deep() == tmp_path / "last_deep_reflection.json"
    assert reflect._request() == tmp_path / "reflect_request.json"
    assert reflect._questions_path() == tmp_path / "questions.json"
    assert reflect._journal() == tmp_path / "journal.jsonl"
    (tmp_path / "journal.jsonl").write_text(
        json.dumps({"ts": "2026-07-03T10:00:00Z", "summary": "tick summary"}) + "\n{ corrupt\n")
    assert reflect._journal_count() == 2
    monkeypatch.setattr(reflect.goals_mod, "load",
                        lambda: [{"id": "G1", "status": "active", "title": "T", "description": "d"}])
    monkeypatch.setattr(reflect.memory, "recent",
                        lambda n: [{"date": "2026-07-03", "title": "note", "summary": "s"}])
    ctx = reflect._build_context(config.load())
    assert "ALL GOALS" in ctx and "tick summary" in ctx and "REFLECTION TASK" in ctx


def test_should_reflect_bootstrap_and_no_timestamp(tmp_path, monkeypatch):
    monkeypatch.setattr(reflect, "_request", lambda: tmp_path / "noreq.json")
    monkeypatch.setattr(reflect, "_last", lambda: tmp_path / "last.json")
    monkeypatch.setattr(reflect, "_journal", lambda: tmp_path / "journal.jsonl")
    cfg = config.load()
    (tmp_path / "journal.jsonl").write_text("a\nb\n")
    assert reflect.should_reflect(cfg) == (False, "bootstrap")       # <3 ticks, no marker -> wait
    (tmp_path / "journal.jsonl").write_text("a\nb\nc\n")
    assert reflect.should_reflect(cfg) == (True, "bootstrap")        # >=3 ticks -> first reflection
    (tmp_path / "last.json").write_text(json.dumps({"ts": "not-a-date"}))
    assert reflect.should_reflect(cfg) == (True, "no-timestamp")     # unreadable marker -> reflect
