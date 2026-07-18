"""goals: the evolving research agenda. Here: the progress_notes tail cap (L2)."""
from cagent import goals as g


def test_progress_notes_are_capped(tmp_path, monkeypatch):
    # L2: goals.json is rewritten + committed + pulled every tick, so an active goal's progress_notes
    # must not grow without bound over a multi-week run.
    monkeypatch.setattr(g, "_goals", lambda: tmp_path / "goals.json")
    monkeypatch.setattr(g, "_archive", lambda: tmp_path / "archive.json")
    monkeypatch.setattr(g, "_history_path", lambda: tmp_path / "history.jsonl")
    g.upsert({"id": "G1", "title": "a quest"})
    for i in range(60):
        g.add_progress("G1", f"note {i}")
    goal = next(x for x in g.load() if x["id"] == "G1")
    assert len(goal["progress_notes"]) == 50                 # capped at the tail
    assert goal["progress_notes"][-1]["note"] == "note 59"   # newest kept
    assert goal["progress_notes"][0]["note"] == "note 10"    # oldest 10 dropped


def test_upsert_dedups_identical_active_goal(tmp_path, monkeypatch):
    # P1-2/P1-3: re-applying the same id-less goal (a control-directive re-enqueue/re-drain after a
    # crash, or a re-sent !GOAL) must UPDATE the existing active goal, not spawn a second identical one.
    monkeypatch.setattr(g, "_goals", lambda: tmp_path / "goals.json")
    monkeypatch.setattr(g, "_archive", lambda: tmp_path / "archive.json")
    monkeypatch.setattr(g, "_history_path", lambda: tmp_path / "history.jsonl")
    g.upsert({"title": "study X", "description": "study X deeply"})
    g.upsert({"title": "study X", "description": "study X deeply"})   # identical, no id -> dedup
    active = [x for x in g.load() if x.get("status") == "active"]
    assert [x["title"] for x in active].count("study X") == 1
    # a genuinely different goal still gets its own id
    g.upsert({"title": "study Y", "description": "different"})
    assert len([x for x in g.load() if x.get("status") == "active"]) == 2
