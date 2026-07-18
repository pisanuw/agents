"""Reflection / goal-evolution. On a cadence (light daily, deep weekly) or on request,
the agent reviews its body of work and reshapes its quests: retire stale goals, spawn new
ones from the question backlog, and step its persona arc. Cadence is read through clock.now()
so FAST_CLOCK can compress days into seconds for testing.
"""
from __future__ import annotations

import json

from cagent import atomicio, clock, config, goals as goals_mod, memory, persona
from cagent.cognition import invoke, parse

SCHEMA = config.REPO_ROOT / "prompts" / "schemas" / "reflect_output.json"


def _last():
    return config.state_root() / "last_reflection.json"


def _last_deep():
    return config.state_root() / "last_deep_reflection.json"


def _request():
    return config.state_root() / "reflect_request.json"


def _questions_path():
    return config.state_root() / "questions.json"


def _journal():
    return config.state_root() / "journal.jsonl"


def _load_last() -> dict | None:
    return atomicio.read_json(_last(), default=None)


def _save_last(kind: str) -> None:
    atomicio.write_text(_last(), json.dumps({"ts": clock.iso(), "kind": kind}, indent=2))
    if kind == "deep":
        atomicio.write_text(_last_deep(), json.dumps({"ts": clock.iso(), "kind": kind}, indent=2))


def _journal_count() -> int:
    return len(_journal().read_text().splitlines()) if _journal().exists() else 0


def should_reflect(cfg) -> tuple[bool, str]:
    if _request().exists():
        return True, "requested"
    last = _load_last()                  # read the last-reflection marker ONCE (was read twice)
    if not last:
        return (_journal_count() >= 3, "bootstrap")
    age_h = clock.hours_since(last.get("ts", ""))   # last exists here, so None => unreadable timestamp
    if age_h is None:
        return True, "no-timestamp"
    return (True, "cadence") if age_h >= cfg.reflect_light_hours else (False, "")


def _is_deep(cfg) -> bool:
    """True when enough time has elapsed since the last DEEP reflection.
    Uses a separate timestamp file so light reflections (updating last_reflection.json)
    do not reset the 72h deep-reflection clock. Accepts the already-loaded cfg to avoid
    a redundant config.load() call (L5)."""
    p = _last_deep()
    if not p.exists():
        return True  # never done a deep reflection -> do one now
    d = atomicio.read_json(p, default={})   # tolerant read: unparseable -> {} -> age None -> deep
    age_h = clock.hours_since(d.get("ts", ""))
    return age_h is None or age_h >= cfg.reflect_deep_hours


def _questions() -> list[str]:
    return atomicio.read_json(_questions_path(), default=[])


def _build_context(cfg) -> str:
    gs = goals_mod.load()
    glines = [f"- [{g['id']}] ({g.get('status')}) {g['title']}: {g.get('description', '')[:200]}" for g in gs]
    notes = memory.recent(12)
    nlines = [f"- {e.get('date', '')[:10]} {e.get('title', '')}: {e.get('summary', '')}" for e in notes]
    qs = _questions()[-15:]
    journal_tail = _journal().read_text().splitlines()[-10:] if _journal().exists() else []
    jlines = []
    for ln in journal_tail:
        try:
            e = json.loads(ln)
            jlines.append(f"- {e.get('ts', '')[:19]} {str(e.get('summary', e.get('status', '')))[:110]}")
        except json.JSONDecodeError:
            continue
    return (
        "===== ALL GOALS =====\n" + ("\n".join(glines) or "(none)") +
        "\n\n===== RECENT NOTES =====\n" + ("\n".join(nlines) or "(none)") +
        "\n\n===== OPEN QUESTIONS BACKLOG =====\n" + ("\n".join(f"- {q}" for q in qs) or "(none)") +
        "\n\n===== RECENT TICKS =====\n" + ("\n".join(jlines) or "(none)") +
        "\n\n===== REFLECTION TASK =====\n"
        "Step back from the day-to-day and review your body of work. Decide how your goals "
        "should evolve: which goals to RETIRE (done, dead, or "
        "superseded), which to KEEP, and what NEW goals to spawn (often from the question "
        "backlog). Sincere reinvention is welcome but must be deliberate and traceable.\n"
        "GOAL CAP: keep at most 5 active goals at any time. Retire before you create. "
        "If you already have 5 active goals you must retire at least one before spawning a new one. "
        "Depth on a small number of quests is better than breadth across many half-worked threads. "
        "Return ONLY the JSON object matching the schema."
    )


def run(cfg, log) -> dict:
    deep = _is_deep(cfg)
    ctx = _build_context(cfg)
    env = invoke.run_claude(ctx, append_system_prompt=persona.load_system_prompt(),
                            tools="", schema_path=str(SCHEMA),
                            model=cfg.model_reflect if deep else cfg.model_tick,
                            timeout_s=240, label="reflect-deep" if deep else "reflect-light")
    r = parse.parse(env)
    if r.status != "OK" or not isinstance(r.structured, dict):
        log.info("reflection produced no decision (%s)", r.status)
        # Record the attempt and consume any request EVEN on failure, so a persistently failing
        # reflection backs off to the normal cadence instead of re-firing a claude call every tick
        # (should_reflect would otherwise see the stale request/old timestamp as "due" forever).
        _save_last("failed")
        if _request().exists():
            _request().unlink()
        return {"ok": False, "status": r.status}

    out = r.structured
    # Claim this reflection as DONE before applying goal mutations: record the cadence marker and
    # consume any request now, so a crash mid-apply does NOT re-fire the whole reflection next tick
    # (which would double-retire, duplicate new goals, and double-count the persona arc). Reflection
    # is a full re-evaluation of all goals, so a dropped partial apply self-heals at the next cadence.
    _save_last("deep" if deep else "light")
    if _request().exists():
        _request().unlink()

    retired, created = [], []
    for g in out.get("goals_to_retire", []) or []:
        goals_mod.retire(g.get("id", ""), g.get("why", ""))
        retired.append(g.get("id"))
    # Code-side cap: at most 5 active goals (the prompt states the limit but the model
    # can violate it; enforce here so the cap is guaranteed regardless of model compliance).
    GOAL_CAP = 5
    for ng in out.get("new_goals", []) or []:
        if len(goals_mod.active()) >= GOAL_CAP:
            log.info("goal cap reached (%d); skipping new goal %r", GOAL_CAP, ng.get("title"))
            break
        goals_mod.upsert({"title": ng.get("title", ""), "description": ng.get("description", ""),
                          "priority": int(ng.get("priority", 2))}, rationale=ng.get("rationale", ""))
        created.append(ng.get("title"))

    hq = out.get("headline_question")
    if hq:
        qs = _questions()
        qs.append(hq)
        atomicio.write_text(_questions_path(), json.dumps(qs[-200:], indent=2))

    memory.write_note(f"Reflection {clock.today()}", out.get("summary_of_progress", ""),
                      tags=["reflection"], kind="reflection")
    persona.arc_step({"victories": len(created), "tribulations": len(retired)})
    log.info("reflection done (deep=%s): retired=%s new=%s", deep, retired, created)
    return {"ok": True, "deep": deep, "retired": retired, "created": created, "headline_question": hq}
