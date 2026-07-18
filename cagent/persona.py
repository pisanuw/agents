"""Persona/constitution loading. Session 1 provides the constitution (the immutable
override + invariants); Session 5 adds the full persona voice (PERSONA.md) and the
clamped persona-state. load_system_prompt() returns the text appended to every
cognition call via --append-system-prompt.
"""
from __future__ import annotations

import hashlib
import json
import os

from cagent import atomicio, clock, config

SHARED_DIR = config.REPO_ROOT / "persona"   # shared constitution + legacy single-persona voice/state


def persona_dir():
    """Where this run's persona voice + arc-state live: personas/<name>/ when CAGENT_PERSONA is set,
    else the legacy persona/ directory (single-persona, unchanged)."""
    persona = os.environ.get("CAGENT_PERSONA", "").strip()
    return config.REPO_ROOT / "personas" / persona if persona else SHARED_DIR


def state_path():
    return persona_dir() / "persona-state.json"

# The persona's arc, advanced one step at a time by experience, never by a clock.
ARC_ORDER = ["idealism", "tribulation", "wisdom"]
MIN_DWELL = 5  # reflections in a stage before it may advance
TONE_FLOOR, TONE_CEIL = 0.3, 0.8


def constitution() -> str:
    p = SHARED_DIR / "constitution.md"   # shared across all personas, never per-persona
    return p.read_text() if p.exists() else ""


def invariants_hash() -> str:
    """Short SHA-1 of constitution + persona voice. Stored in persona-state.json on first arc_step;
    a mismatch on subsequent calls means the immutable files were edited and evolution is halted."""
    content = constitution() + persona_voice()
    return hashlib.sha1(content.encode()).hexdigest()[:16]


def persona_voice() -> str:
    d = persona_dir()
    for name in ("persona.md", "PERSONA.md"):
        p = d / name
        if p.exists():
            return p.read_text()
    return ""


def load_state() -> dict:
    return atomicio.read_json(state_path(), default={
        "schema_version": 1, "arc_stage": "idealism", "arc_stage_since": clock.today(),
        "heartbeats_in_stage": 0, "tone_temperature": 0.7, "quests_completed": 0,
        "hard_problems_named": 0, "victories": 0, "tribulations": 0,
        "recurring_motifs": [], "last_self_reflection": None,
    })


def save_state(s: dict) -> None:
    # Atomic: a torn write here would make load_state hit JSONDecodeError and silently reset the
    # whole clamped arc (stage/victories/tribulations/tone) to the idealism seed, discarding drift.
    atomicio.write_text(state_path(), json.dumps(s, indent=2) + "\n")


def arc_step(outcome: dict | None = None) -> dict:
    """Advance the clamped persona state by one reflection's worth of experience.
    At most one arc stage per call, after a minimum dwell and a met trigger; never
    auto-regresses. Tone is hard-clamped.
    Halts with RuntimeError if the constitution or persona voice was edited (invariants_hash
    mismatch), enforcing CLAUDE.md's "invariants_hash mismatch halts evolution" guarantee."""
    outcome = outcome or {}
    s = load_state()
    current_hash = invariants_hash()
    stored_hash = s.get("invariants_hash")
    if stored_hash and stored_hash != current_hash:
        raise RuntimeError(
            f"invariants_hash mismatch: constitution or persona voice was edited without a reset "
            f"(stored={stored_hash} current={current_hash}). Run cagentctl reset to reseed.")
    s["invariants_hash"] = current_hash
    s["heartbeats_in_stage"] = int(s.get("heartbeats_in_stage", 0)) + 1
    for k in ("quests_completed", "hard_problems_named", "victories", "tribulations"):
        s[k] = int(s.get(k, 0)) + int(outcome.get(k, 0))

    stage = s.get("arc_stage", "idealism")
    dwell = s["heartbeats_in_stage"]
    if stage == "idealism" and dwell >= MIN_DWELL and s["tribulations"] >= max(2, s["victories"]):
        stage, s["heartbeats_in_stage"], s["arc_stage_since"] = "tribulation", 0, clock.today()
    elif stage == "tribulation" and dwell >= MIN_DWELL and s["hard_problems_named"] >= 3:
        stage, s["heartbeats_in_stage"], s["arc_stage_since"] = "wisdom", 0, clock.today()
    s["arc_stage"] = stage

    # tone drifts gently toward the stage's register, hard-clamped
    target = {"idealism": 0.75, "tribulation": 0.6, "wisdom": 0.45}.get(stage, 0.7)
    cur = float(s.get("tone_temperature", 0.7))
    cur += max(-0.05, min(0.05, target - cur))
    s["tone_temperature"] = round(min(TONE_CEIL, max(TONE_FLOOR, cur)), 3)
    save_state(s)
    return s


def state_block() -> str:
    """A short rendered block appended to cognition so the voice reflects the current arc."""
    s = load_state()
    return (f"[persona state: arc={s.get('arc_stage')} (since {s.get('arc_stage_since')}), "
            f"arc_tone={s.get('tone_temperature')}, victories={s.get('victories')}, "
            f"tribulations={s.get('tribulations')}, hard_problems_named={s.get('hard_problems_named')}]")


def load_system_prompt(include_voice: bool = True) -> str:
    parts = [constitution()]
    if include_voice:
        v = persona_voice()
        if v:
            parts.append(v)
        parts.append(state_block())
    return "\n\n".join(p for p in parts if p.strip())
