"""Goals: the evolving research agenda. Atomic writes; retired goals go to an archive
and are never lost. The agent reshapes these over time (Session 6 reflection)."""
from __future__ import annotations

import json

from cagent import atomicio, clock, config


def _goals():
    return config.state_root() / "goals.json"


def _archive():
    return config.state_root() / "goals_archive.json"


def _history_path():
    return config.state_root() / "goals_history.jsonl"


def load() -> list[dict]:
    return atomicio.read_json(_goals(), default=[])


def save(goals: list[dict]) -> None:
    atomicio.write_text(_goals(), json.dumps(goals, indent=2))


def _load_archive() -> list[dict]:
    return atomicio.read_json(_archive(), default=[])


def active(goals: list[dict] | None = None) -> list[dict]:
    goals = load() if goals is None else goals
    return [g for g in goals if g.get("status") == "active"]


def archived() -> list[dict]:
    """Retired goals (moved out of goals.json by retire()). Exposed so the gate-check can verify a
    draft's 'G6 retired' claim instead of flagging it as fabrication -- the archive is agent state."""
    return _load_archive()


def _append_history(entry: dict) -> None:
    hp = _history_path()
    hp.parent.mkdir(parents=True, exist_ok=True)
    with open(hp, "a") as f:
        f.write(json.dumps({"ts": clock.iso(), **entry}) + "\n")


def _find(goals: list[dict], gid: str) -> dict | None:
    return next((g for g in goals if g.get("id") == gid), None)


def upsert(item: dict, rationale: str = "") -> list[dict]:
    """Add or update a goal by id. When NO id is given, an existing ACTIVE goal with identical title
    and description is updated in place instead of duplicated -- so re-applying the same owner input
    (a control-directive re-enqueue or re-drain after a crash, or a re-sent !GOAL) never spawns a
    second identical goal (P1-2/P1-3). Unknown fields are kept minimal/normalized."""
    goals = load()
    title = item.get("title", "(untitled quest)")
    desc = item.get("description", "")
    gid = item.get("id")
    if not gid:
        dup = next((g for g in goals if g.get("status") == "active"
                    and g.get("title") == title and g.get("description") == desc), None)
        gid = dup["id"] if dup else _next_id(goals)
    now = clock.today()
    existing = _find(goals, gid)
    if existing:
        existing.update({k: v for k, v in item.items() if k != "id"})
        existing["updated"] = now
        _append_history({"op": "update", "id": gid, "rationale": rationale})
    else:
        goals.append({
            "id": gid,
            "title": title,
            "description": desc,
            "status": item.get("status", "active"),
            "priority": int(item.get("priority", 2)),
            "rationale": rationale or item.get("rationale", ""),
            "parent": item.get("parent"),
            "created": now,
            "updated": now,
            "progress_notes": [],
        })
        _append_history({"op": "create", "id": gid, "title": title, "rationale": rationale})
    save(goals)
    return goals


def retire(gid: str, rationale: str = "") -> list[dict]:
    goals = load()
    g = _find(goals, gid)
    if not g:
        return goals
    g["status"] = "retired"
    g["updated"] = clock.today()
    g["retire_rationale"] = rationale
    # Idempotent + crash-safe: only archive if this id is not already there, so a re-run after a
    # crash between the archive write and the goals.json write (below) does not append a duplicate.
    # Archive-first ordering means such a crash leaves the goal in BOTH (recoverable) rather than
    # LOST; the next retire() of the same id finds it still in goals.json and completes the removal.
    archive = _load_archive()
    if not any(a.get("id") == gid for a in archive):
        archive.append(g)
        atomicio.write_text(_archive(), json.dumps(archive, indent=2))
    goals = [x for x in goals if x.get("id") != gid]
    save(goals)
    _append_history({"op": "retire", "id": gid, "rationale": rationale})
    return goals


def add_progress(gid: str, note: str) -> None:
    goals = load()
    g = _find(goals, gid)
    if not g:
        return
    notes = g.setdefault("progress_notes", [])
    notes.append({"ts": clock.iso(), "note": note[:500]})
    # Cap the tail: goals.json is rewritten + committed + pulled every tick, so an active goal's
    # note list would otherwise grow without bound over a multi-week/month run.
    g["progress_notes"] = notes[-50:]
    g["updated"] = clock.today()
    save(goals)


def _next_id(goals: list[dict]) -> str:
    n = 1
    existing = {g.get("id") for g in goals} | {g.get("id") for g in _load_archive()}
    while f"G{n}" in existing:
        n += 1
    return f"G{n}"
