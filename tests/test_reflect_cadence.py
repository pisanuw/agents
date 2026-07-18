import json

from cagent import config, reflect


def _set_now(iso, monkeypatch):
    monkeypatch.setenv("CAGENT_NOW", iso)
    monkeypatch.delenv("CAGENT_TICK_SECONDS", raising=False)


def test_cadence_fires_after_interval(tmp_path, monkeypatch):
    last_path = tmp_path / "last.json"
    req_path = tmp_path / "req.json"
    journal_path = tmp_path / "journal.jsonl"
    monkeypatch.setattr(reflect, "_last", lambda: last_path)
    monkeypatch.setattr(reflect, "_request", lambda: req_path)
    monkeypatch.setattr(reflect, "_journal", lambda: journal_path)
    cfg = config.load()

    # last reflection at T0
    last_path.write_text(json.dumps({"ts": "2026-06-22T00:00:00+00:00", "kind": "light"}))

    _set_now("2026-06-22T06:00:00+00:00", monkeypatch)  # 6h later, < 24h light cadence
    due, _ = reflect.should_reflect(cfg)
    assert not due

    _set_now("2026-06-23T02:00:00+00:00", monkeypatch)  # 26h later, >= 24h
    due, why = reflect.should_reflect(cfg)
    assert due and why == "cadence"


def test_request_forces_reflection(tmp_path, monkeypatch):
    last_path = tmp_path / "last.json"
    req_path = tmp_path / "req.json"
    monkeypatch.setattr(reflect, "_last", lambda: last_path)
    monkeypatch.setattr(reflect, "_request", lambda: req_path)
    req_path.write_text("{}")
    due, why = reflect.should_reflect(config.load())
    assert due and why == "requested"


def test_failed_reflection_records_attempt_and_clears_request(tmp_path, monkeypatch):
    # M4: a reflection that yields no decision must still record the attempt + consume the request,
    # so should_reflect does not re-fire a claude call every single tick (the retry storm).
    import logging
    from types import SimpleNamespace
    last_path = tmp_path / "last.json"
    last_deep_path = tmp_path / "last_deep.json"
    req_path = tmp_path / "req.json"
    journal_path = tmp_path / "journal.jsonl"
    questions_path = tmp_path / "q.json"
    monkeypatch.setattr(reflect, "_last", lambda: last_path)
    monkeypatch.setattr(reflect, "_last_deep", lambda: last_deep_path)
    monkeypatch.setattr(reflect, "_request", lambda: req_path)
    monkeypatch.setattr(reflect, "_journal", lambda: journal_path)
    monkeypatch.setattr(reflect, "_questions_path", lambda: questions_path)
    req_path.write_text("{}")
    monkeypatch.setattr(reflect.invoke, "run_claude", lambda *a, **k: {})
    monkeypatch.setattr(reflect.parse, "parse",
                        lambda env: SimpleNamespace(status="NO_STRUCTURED_OUTPUT", structured=None))
    res = reflect.run(config.load(), logging.getLogger("t"))
    assert res["ok"] is False
    assert last_path.exists()              # attempt recorded -> cadence backs off
    assert not req_path.exists()           # request consumed -> no every-tick re-fire


def test_deep_reflection_uses_separate_timestamp(tmp_path, monkeypatch):
    # H3: _is_deep() must read last_deep_reflection.json, NOT last_reflection.json.
    # Light reflections advance last_reflection.json but must NOT reset the deep clock.
    import logging
    from types import SimpleNamespace

    last_path = tmp_path / "last.json"
    last_deep_path = tmp_path / "last_deep.json"
    req_path = tmp_path / "req.json"
    journal_path = tmp_path / "journal.jsonl"
    questions_path = tmp_path / "q.json"
    monkeypatch.setattr(reflect, "_last", lambda: last_path)
    monkeypatch.setattr(reflect, "_last_deep", lambda: last_deep_path)
    monkeypatch.setattr(reflect, "_request", lambda: req_path)
    monkeypatch.setattr(reflect, "_journal", lambda: journal_path)
    monkeypatch.setattr(reflect, "_questions_path", lambda: questions_path)

    cfg = config.load()

    # Simulate a recent light reflection (within light cadence interval).
    # last_deep_path absent -> _is_deep() should return True (never done deep).
    _set_now("2026-06-22T10:00:00+00:00", monkeypatch)
    assert reflect._is_deep(cfg) is True

    # After a deep reflection, last_deep_path is written. Deep clock resets.
    last_deep_path.write_text(json.dumps({"ts": "2026-06-22T10:00:00+00:00", "kind": "deep"}))
    _set_now("2026-06-22T12:00:00+00:00", monkeypatch)  # only 2h since deep
    assert reflect._is_deep(cfg) is False

    # Light reflections only write last_reflection.json (last_path), NOT last_deep_path.
    # Simulate many light reflections advancing last_path; deep clock unchanged.
    last_path.write_text(json.dumps({"ts": "2026-06-24T10:00:00+00:00", "kind": "light"}))
    _set_now("2026-06-25T11:00:00+00:00", monkeypatch)  # 73h since deep, >= reflect_deep_hours (72h)
    assert reflect._is_deep(cfg) is True

    # A successful deep run() must write BOTH last_reflection.json and last_deep_reflection.json.
    labels_seen = []
    def fake_run_claude(*a, **k):
        labels_seen.append(k.get("label"))
        return {}

    monkeypatch.setattr(reflect.invoke, "run_claude", fake_run_claude)
    monkeypatch.setattr(reflect.parse, "parse",
                        lambda env: SimpleNamespace(
                            status="OK", structured={
                                "goals_to_retire": [], "new_goals": [],
                                "headline_question": None, "summary_of_progress": "test",
                                "persona_arc": None,
                            }))
    monkeypatch.setattr(reflect.goals_mod, "load", lambda: [])
    monkeypatch.setattr(reflect.goals_mod, "retire", lambda *a, **k: None)
    monkeypatch.setattr(reflect.goals_mod, "upsert", lambda *a, **k: None)
    monkeypatch.setattr(reflect.memory, "recent", lambda n: [])
    monkeypatch.setattr(reflect.memory, "write_note", lambda *a, **k: "x")
    monkeypatch.setattr(reflect.persona, "load_system_prompt", lambda: "")
    monkeypatch.setattr(reflect.persona, "arc_step", lambda *a, **k: None)

    res = reflect.run(cfg, logging.getLogger("t"))
    assert res["ok"] is True
    assert res["deep"] is True
    assert last_path.exists()        # light marker also updated
    assert last_deep_path.exists()   # deep marker updated
    assert "reflect-deep" in labels_seen  # correct label passed to invoke
