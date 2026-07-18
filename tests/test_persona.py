"""persona.py: constitution/voice loading, the invariants hash that halts evolution on tamper, the
clamped arc_step drift, and load_system_prompt assembly. Pure logic over files — the fixture points
SHARED_DIR + REPO_ROOT at tmp so nothing touches the real persona/ tree.
"""
from __future__ import annotations

import pytest

from cagent import config, persona


@pytest.fixture
def psandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(persona, "SHARED_DIR", tmp_path / "persona")
    (tmp_path / "persona").mkdir()
    monkeypatch.delenv("CAGENT_PERSONA", raising=False)
    (tmp_path / "persona" / "constitution.md").write_text("THE CONSTITUTION")
    return tmp_path


def test_constitution_and_voice_loading(psandbox):
    assert persona.constitution() == "THE CONSTITUTION"
    assert persona.persona_voice() == ""                        # no voice file yet
    (psandbox / "persona" / "persona.md").write_text("MY VOICE")
    assert persona.persona_voice() == "MY VOICE"


def test_persona_dir_switches_on_env(psandbox, monkeypatch):
    assert persona.persona_dir() == psandbox / "persona"        # legacy flat when unset
    monkeypatch.setenv("CAGENT_PERSONA", "bravo")
    assert persona.persona_dir() == psandbox / "personas" / "bravo"


def test_invariants_hash_changes_with_content(psandbox):
    h1 = persona.invariants_hash()
    assert len(h1) == 16
    (psandbox / "persona" / "persona.md").write_text("added voice changes the hash")
    assert persona.invariants_hash() != h1


def test_load_state_default_then_roundtrip(psandbox):
    s = persona.load_state()
    assert s["arc_stage"] == "idealism" and s["tone_temperature"] == 0.7
    s["victories"] = 9
    persona.save_state(s)
    assert persona.load_state()["victories"] == 9              # persisted and re-read


def test_arc_step_advances_idealism_to_tribulation(psandbox):
    s = None
    for _ in range(persona.MIN_DWELL):                          # 5 tribulations over 5 reflections
        s = persona.arc_step({"tribulations": 1})
    assert s["arc_stage"] == "tribulation"
    assert s["heartbeats_in_stage"] == 0                        # reset on stage change
    assert s["invariants_hash"] == persona.invariants_hash()    # stamped on first step


def test_arc_step_full_arc_to_wisdom(psandbox):
    for _ in range(persona.MIN_DWELL):
        persona.arc_step({"tribulations": 1})                  # -> tribulation
    s = None
    for _ in range(persona.MIN_DWELL):
        s = persona.arc_step({"hard_problems_named": 1})       # -> wisdom (>=3 hard problems)
    assert s["arc_stage"] == "wisdom"
    assert persona.TONE_FLOOR <= s["tone_temperature"] <= persona.TONE_CEIL   # tone stays clamped


def test_arc_step_halts_on_invariants_mismatch(psandbox):
    persona.save_state({**persona.load_state(), "invariants_hash": "deadbeefdeadbeef"})
    with pytest.raises(RuntimeError, match="invariants_hash mismatch"):
        persona.arc_step({})


def test_state_block_renders_current_arc(psandbox):
    persona.save_state({**persona.load_state(), "arc_stage": "wisdom", "victories": 4})
    block = persona.state_block()
    assert "arc=wisdom" in block and "victories=4" in block


def test_load_system_prompt_includes_voice_and_state(psandbox):
    (psandbox / "persona" / "persona.md").write_text("VOICE TEXT")
    prompt = persona.load_system_prompt(include_voice=True)
    assert "THE CONSTITUTION" in prompt and "VOICE TEXT" in prompt and "arc=" in prompt
    bare = persona.load_system_prompt(include_voice=False)
    assert bare == "THE CONSTITUTION"                            # no voice, no state block
