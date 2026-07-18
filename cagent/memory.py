"""Semantic memory: notes and essays as markdown under state/memory/, with a JSONL index.
Selection for the tick context is recency + goal-keyword overlap (no embeddings in v1)."""
from __future__ import annotations

import json
import re

from cagent import atomicio, clock, config


def _mem():
    return config.state_root() / "memory"


def _notes():
    return _mem() / "notes"


def _index():
    return _mem() / "index.jsonl"


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:60] or "note"


def write_note(title: str, body: str, tags: list[str] | None = None,
               goal_id: str | None = None, kind: str = "note") -> str:
    now = clock.now()
    sub = _notes() / f"{now.year:04d}" / f"{now.month:02d}"
    sub.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%d-%H%M%S")
    path = sub / f"{stamp}-{_slug(title)}.md"
    front = (f"---\ntitle: {title}\ndate: {clock.iso()}\nkind: {kind}\n"
             f"goal_id: {goal_id or ''}\ntags: {', '.join(tags or [])}\n---\n\n")
    atomicio.write_text(path, front + body.rstrip() + "\n")   # atomic: no half-written note file
    rel = str(path.relative_to(config.REPO_ROOT))
    summary = " ".join(body.split())[:160]
    _append_index({"id": stamp, "date": clock.iso(), "title": title, "path": rel,
                   "tags": tags or [], "goal_id": goal_id, "kind": kind, "summary": summary})
    return rel


def _append_index(entry: dict) -> None:
    _index().parent.mkdir(parents=True, exist_ok=True)
    with open(_index(), "a") as f:
        f.write(json.dumps(entry) + "\n")


def index_entries() -> list[dict]:
    return atomicio.read_jsonl(_index())


def recent(n: int = 8) -> list[dict]:
    return list(reversed(index_entries()))[:n]


def _tokens(text: str) -> set[str]:
    """Lowercased words of >=4 letters -- the shared keyword tokenizer for both selectors."""
    return set(re.findall(r"[a-z]{4,}", (text or "").lower()))


def select(goals: list[dict], n: int = 8) -> list[dict]:
    """Top-n index entries by recency + goal-keyword overlap."""
    entries = index_entries()
    if not entries:
        return []
    kw = set()
    for g in goals:
        kw |= _tokens(g.get("title", "") + " " + g.get("description", ""))
    scored = []
    total = len(entries)
    for i, e in enumerate(entries):
        recency = (i + 1) / total  # newer entries scored higher
        overlap = len(kw & _tokens(e.get("title", "") + " " + e.get("summary", "")))
        scored.append((0.6 * recency + 0.4 * min(overlap / 5.0, 1.0), e))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:n]]


def select_by_text(text: str, n: int = 24) -> list[dict]:
    """Top-n index entries ranked by keyword overlap with `text` (a DRAFT being fact-checked), with
    a light recency tiebreak. Unlike select(), which ranks by GOAL keywords, this ranks by what the
    draft itself references -- so the note a specific citation rests on surfaces even across many
    parallel inquiries. The gate-check needs the source a claim rests on, not the goal-relevant set;
    ranking by goals (+ a small window) is what let true, sourced claims read as 'fabrication'."""
    entries = index_entries()
    if not entries:
        return []
    kw = _tokens(text)
    if not kw:
        return recent(n)
    total = len(entries)
    scored = []
    for i, e in enumerate(entries):
        overlap = len(kw & _tokens(e.get("title", "") + " " + e.get("summary", "")))
        scored.append((overlap + 0.3 * ((i + 1) / total), e))   # overlap dominates; recency breaks ties
    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:n]]


def body_of(entry: dict) -> str:
    p = config.REPO_ROOT / entry.get("path", "")
    return p.read_text() if p.exists() else ""
