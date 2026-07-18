"""memory selection + body_of. The gate-check draft-ranking (select_by_text) is exercised in
test_gate_check; here we cover goal-keyword select(), the no-keyword recency fallback, and body_of's
missing-file path."""
from __future__ import annotations

from cagent import config, memory


def test_select_ranks_by_goal_overlap(monkeypatch):
    # A big goal-keyword overlap outranks a more-recent but irrelevant note (recency is only a 0.6
    # weight vs overlap's 0.4, so >=5 overlaps can overcome one recency slot).
    entries = [
        {"id": "match", "title": "honeybee quorum sensing waggle dance colony",
         "summary": "scouts nectar foragers hive"},                 # oldest but heavy overlap
        {"id": "recent", "title": "climate tipping points", "summary": "amoc hysteresis"},  # newest, no overlap
    ]
    monkeypatch.setattr(memory, "index_entries", lambda: entries)
    goal = {"title": "honeybee quorum waggle dance", "description": "colony scouts foraging"}
    assert memory.select([goal], 1)[0]["id"] == "match"


def test_select_empty_index(monkeypatch):
    monkeypatch.setattr(memory, "index_entries", lambda: [])
    assert memory.select([{"title": "x"}], 5) == []


def test_select_by_text_no_keywords_falls_back_to_recent(monkeypatch):
    entries = [{"id": str(i), "title": f"t{i}", "summary": "s"} for i in range(3)]
    monkeypatch.setattr(memory, "index_entries", lambda: entries)
    got = memory.select_by_text("a b c", 2)               # no >=4-letter token -> recency fallback
    assert [e["id"] for e in got] == ["2", "1"]           # most-recent first


def test_body_of_missing_and_present(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    assert memory.body_of({"path": "does/not/exist.md"}) == ""
    (tmp_path / "note.md").write_text("hello body")
    assert memory.body_of({"path": "note.md"}) == "hello body"
