"""Apply a parsed tick decision via a small, bounded, audited set of handlers. The model
proposes; this code disposes. send_email is hard-locked to the owner and passes the gate-check
fact-check first. Unknown/failed actions are logged and skipped, never crash the tick.
"""
from __future__ import annotations

import json
import os

from cagent import atomicio, config, gmail, goals as goals_mod, memory
from cagent.cognition import backoff, context, invoke, parse, research

GATE_SCHEMA = config.REPO_ROOT / "prompts" / "schemas" / "gate_output.json"
REVISE_SCHEMA = config.REPO_ROOT / "prompts" / "schemas" / "revise_output.json"
GATE_PROMPT_DEFAULT = config.REPO_ROOT / "prompts" / "gate-check.md"

# One bounded self-correction pass when the gate says "revise": rewrite the draft to fix the named
# facts, grounded ONLY in the same notes the gate judged against. Same safety envelope as every other
# claude call (tools off, schema'd) — it edits text, it never sends. It cannot promote a draft past
# the gate by itself: the result is fed back through _gate_check.
REVISE_SYSTEM = (
    "You are revising an email DRAFT that just failed a fact-check. Make the MINIMAL changes needed "
    "to fix every listed problem, using ONLY the provided NOTES/SOURCES as ground truth. Remove or "
    "correct any claim, source, citation, quote, number, or date not in the notes (fabrication). "
    "Soften or delete any inquiry reported as settled/found that the notes do not support "
    "(false_victory). State plainly any tool failure or limitation the draft glossed over "
    "(hidden_failure). Keep the persona's voice, structure, and the AI-disclosure footer intact. "
    "If a claim cannot be supported from the notes, DELETE it rather than soften it. Never invent a "
    'new source. Return JSON with required "body" (the full revised body) and optional "subject" '
    "(only include 'subject' if the original subject also needs correction)."
)
def _reflect_request():
    return config.state_root() / "reflect_request.json"


def _questions():
    return config.state_root() / "questions.json"


def apply(decision: dict, cfg, log) -> list[dict]:
    results = []
    research_used = 0
    # Outbound backpressure, hard half for research (the soft signal in context.py asks the model to
    # hold off, this enforces it): when the persona's draft queue is already at the cap, new research
    # only produces findings that become still more undeliverable drafts. Skip it deterministically
    # rather than trusting the model to comply. Read once -- research does not add to pending/, so the
    # depth is stable across this loop; send_email re-reads live in _do_email. supervise is imported
    # lazily to avoid a cognition<-supervise import cycle (supervise pulls in gmail/smtp).
    from cagent import supervise
    backlog_full = supervise.backlog_depth() >= cfg.max_backlog_drafts
    for action in (decision.get("actions") or [])[:3]:
        t = action.get("type")
        try:
            if t == "research":
                if backlog_full:
                    results.append({"type": "research", "skipped": "outbound backlog full (backpressure)"})
                    continue
                if research_used >= cfg.research_per_tick:
                    results.append({"type": "research", "skipped": "per-tick research cap"})
                    continue
                research_used += 1
                results.append(_do_research(action, log))
            elif t == "write_note":
                results.append(_do_note(action, log))
            elif t == "update_goals":
                results.append(_do_goals(action, log))
            elif t == "send_email":
                results.append(_do_email(action, cfg, log))
            elif t == "schedule_reflection":
                results.append(_do_reflection(action, log))
            elif t == "noop":
                results.append({"type": "noop", "reason": action.get("reason", "")})
            else:
                results.append({"type": str(t), "skipped": "unknown action type"})
        except Exception as e:  # never let one action crash the tick
            log.info("action %s failed: %s", t, e)
            results.append({"type": str(t), "error": str(e)})
    return results


def _do_research(action, log):
    q = (action.get("research") or {}).get("query", "").strip()
    goal_id = (action.get("research") or {}).get("goal_id")
    if not q:
        return {"type": "research", "skipped": "empty query"}
    out = research.run(q)
    # run() returns None for cap/!NO-RESEARCH/rate-limit/error — never write a zero-findings
    # memory note in those cases, since that would log false goal-progress and pollute memory (L2).
    if out is None:
        return {"type": "research", "query": q, "result": "no findings (error, cap, or disabled)"}
    body = _format_research(out)
    path = memory.write_note(f"Findings: {q[:60]}", body, tags=["research"], goal_id=goal_id, kind="research")
    if goal_id:
        goals_mod.add_progress(goal_id, f"researched: {q[:120]} ({len(out.get('findings', []))} findings)")
    _stash_questions(out.get("new_questions", []))
    log.info("research saved -> %s (%d findings)", path, len(out.get("findings", [])))
    return {"type": "research", "query": q, "note": path, "findings": len(out.get("findings", []))}


def _format_research(out: dict) -> str:
    lines = [f"# Research: {out.get('query', '')}", ""]
    for f in out.get("findings", []):
        src = f.get("source_url", "")
        title = f.get("source_title", "")
        conf = f.get("confidence", "")
        lines.append(f"- {f.get('claim', '')}\n  source: {title} {src} ({conf})")
    if out.get("dead_ends"):
        lines += ["", "## Dead ends", *[f"- {d}" for d in out["dead_ends"]]]
    if out.get("new_questions"):
        lines += ["", "## New questions", *[f"- {q}" for q in out["new_questions"]]]
    return "\n".join(lines)


def _do_note(action, log):
    n = action.get("note") or {}
    title = n.get("title", "(untitled)")
    body = _unwrap_model_json(n.get("body", ""))
    if not body.strip():
        return {"type": "write_note", "skipped": "empty body"}
    path = memory.write_note(title, body, tags=n.get("tags", []), goal_id=n.get("goal_id"))
    if n.get("goal_id"):
        goals_mod.add_progress(n["goal_id"], f"wrote note: {title[:120]}")
    log.info("note saved -> %s", path)
    return {"type": "write_note", "note": path}


def _do_goals(action, log):
    g = action.get("update_goals") or {}
    rationale = g.get("rationale", "")
    changed = []
    for item in g.get("upsert", []) or []:
        goals_mod.upsert(item, rationale=rationale)
        changed.append(("upsert", item.get("id") or item.get("title", "")))
    for gid in g.get("retire", []) or []:
        goals_mod.retire(gid, rationale=rationale)
        changed.append(("retire", gid))
    log.info("goals updated: %s", changed)
    return {"type": "update_goals", "changes": changed, "rationale": rationale}


def _unwrap_model_json(text: str) -> str:
    """Unwrap a model body that arrived as a JSON wrapper ({"body": "...\\n..."}) so the gate-check
    and the staged DRAFT REQUEST see real prose, not a JSON dump. Delegates to the canonical, more
    robust normalizer that also runs at the send chokepoint (gmail.normalize_model_body); kept here as
    the name the note path and existing tests call, and to defang BEFORE gate-check/staging rather
    than only at send time."""
    return gmail.normalize_model_body(text)


def _do_email(action, cfg, log):
    e = action.get("email") or {}
    subject = e.get("subject", "(no subject)")
    body = _unwrap_model_json(e.get("body", ""))
    if not body.strip():
        return {"type": "send_email", "skipped": "empty body"}
    # Outbound backpressure (the negative-feedback loop): when the persona already has a full backlog
    # of drafts waiting in pending/ (approved-but-unsent behind the send cap, plus drafts awaiting the
    # owner), admitting yet another only grows an unsendable queue. Refuse BEFORE the gate-check so a
    # backlogged tick spends NO claude call on a draft it cannot deliver. Counted live each call, so a
    # second send_email in the same tick sees the first's freshly-staged draft. Inert outside
    # SUPERVISED (LIVE/DRY_RUN do not accumulate drafts in pending/, so the depth stays ~0).
    from cagent import supervise
    backlog = supervise.backlog_depth()
    if backlog >= cfg.max_backlog_drafts:
        log.info("send_email held by backpressure: backlog %d >= cap %d (draining queued drafts first)",
                 backlog, cfg.max_backlog_drafts)
        return {"type": "send_email", "backpressure": {"backlog": backlog, "cap": cfg.max_backlog_drafts}}
    verdict = _gate_check(subject, body, log)
    revised = False
    if verdict.get("verdict") != "send" and not verdict.get("safety") and not verdict.get("gate_unavailable"):
        # One self-correction: feed the gate's findings back into a single re-draft rather than
        # discarding the letter and waiting a whole tick for a fresh (still-overreaching) draft.
        # Skip the retry on a safety flag — those are not "revise and try again" problems. Skip it
        # too when the gate was UNAVAILABLE (outage/rate-limit): the "gate-check unavailable" sentinel
        # is not a real finding, so revising against it would burn another claude call (and, on a
        # 429, record backoff a second/third time in this one apply() loop) for nothing — just block.
        new_subject, new_body = _revise_draft(subject, body, verdict, log)
        if new_body and new_body.strip() and new_body.strip() != body.strip():
            subject, body, revised = new_subject or subject, new_body, True
            verdict = _gate_check(subject, body, log)
    # A non-empty `safety` array (leaked secret, non-owner recipient, ...) is an UNCONDITIONAL block,
    # even when the gate model returned verdict "send": the schema permits {verdict:"send",safety:[...]}
    # and a safety flag must be enforced in CODE, never left to the model's compliance.
    if verdict.get("verdict") != "send" or verdict.get("safety"):
        log.info("gate-check blocked the email%s: %s", " (after revise)" if revised else "",
                 {k: v for k, v in verdict.items() if v and k != "disclosure_present"})
        return {"type": "send_email", "blocked_by_gate": verdict, "revised": revised}
    if cfg.MODE == "SUPERVISED":
        from cagent import supervise
        tok = supervise.stage_draft(subject, body, e.get("kind", "finding"))
        supervise.request_approval(tok, subject, body, cfg, log)
        return {"type": "send_email", "supervised": True, "token": tok, "revised": revised}
    try:
        r = gmail.send(subject=subject, body_md=body, kind=e.get("kind", "finding"))
        log.info("email %s to %s (dry_run=%s)", "staged" if r.dry_run else "sent", r.to, r.dry_run)
        return {"type": "send_email", "sent": r.ok, "dry_run": r.dry_run, "to": r.to, "revised": revised}
    except gmail.SendRefused as ex:
        log.info("send refused: %s", ex)
        return {"type": "send_email", "refused": str(ex)}


def _gate_prompt_text() -> str:
    """The fact-check ('gate') system prompt for the current persona. Each persona supplies its
    own personas/<name>/gate-check.md (flavor only; the checking contract is identical), falling
    back to the neutral default. Never returns empty, so the gate cannot fail open on a typo."""
    persona_name = os.environ.get("CAGENT_PERSONA", "").strip()
    if persona_name:
        p = config.REPO_ROOT / "personas" / persona_name / "gate-check.md"
        if p.exists():
            return p.read_text()
    return GATE_PROMPT_DEFAULT.read_text() if GATE_PROMPT_DEFAULT.exists() else ""


def _recent_journal(n: int = 8) -> str:
    """Compact last-n tick summaries (resolved at CALL time, so it is correct per persona). Agent
    state the DRAFTER sees but the gate historically did not -- so tick counts, prior actions, and
    'the Nth pass' claims are verifiable rather than flagged as fabrication."""
    ticks = [e for e in atomicio.read_jsonl(config.state_root() / "journal.jsonl")
             if e.get("kind") == "tick"][-n:]
    lines = [f"- {e.get('ts', '')[:19]} {'ok' if e.get('ok') else 'FAIL'} "
             f"{(e.get('summary') or e.get('status') or '').replace(chr(10), ' ').strip()[:200]}"
             for e in ticks]
    return "\n".join(lines) or "(no prior ticks)"


def _latest_reflection() -> str:
    refls = [e for e in memory.index_entries() if e.get("kind") == "reflection"]
    return memory.body_of(refls[-1])[:6000] if refls else ""


def _gate_sources(draft: str, byte_cap: int = 48000) -> str:
    """Ground truth the gate fact-checks a draft against. It MUST be at least as broad as what the
    DRAFTER wrote from, or true claims read as fabrication -- the silent send-stall (personas have gone
    days without mail). Two labelled parts:
      AGENT STATE   -- goals (active + retired), recent journal, latest reflection: so operational /
                       process claims (goal ids & status, tick counts, 'research complete') verify.
      RESEARCH NOTES-- ranked by overlap with THIS draft (memory.select_by_text) over a wide window,
                       so a cross-inquiry citation's supporting note is present (was: 8 goal-ranked
                       notes, which missed the note a claim actually rested on)."""
    goals = goals_mod.load()
    retired = goals_mod.archived()

    def _gline(g: dict, status: str) -> str:
        # Carry created date + parent so a CORRECTLY-cited 'open since <created>' / 'depends-on
        # <parent>' verifies here -- the drafter now sees these same fields (context._render_goals),
        # and the gate MUST be at least as broad or the true citation reads as fabrication. An
        # invented date/parent still fails: it won't match this authoritative value.
        meta = f"created {g.get('created', '?')}"
        if g.get("parent"):
            meta += f", parent {g['parent']}"
        return f"[{g.get('id')}] {status} ({meta}): {g.get('title', '')}"

    goal_lines = [_gline(g, g.get("status", "active")) for g in goals]
    goal_lines += [_gline(g, "retired") for g in retired]
    state = ("GOALS (authoritative for goal id / status / retirement / created date / parent):\n"
             + ("\n".join(goal_lines) or "(none)")
             + "\n\nRECENT JOURNAL (authoritative for tick counts, what happened when, prior actions):\n"
             + _recent_journal(8))
    refl = _latest_reflection()
    if refl:
        state += "\n\nLATEST REFLECTION (the agent's current open questions / plan):\n" + refl
    selected = memory.select_by_text(draft, 24) or memory.recent(8)
    # Full bodies for the most draft-relevant notes (needed to check a quote / number / date), then a
    # title+summary CATALOG of the rest. A claim is 'fabrication' only if its source is ABSENT
    # everywhere -- a catalog entry proves the cited source EXISTS, so a note that doesn't fit as a
    # full body still shields a real citation from a false fabrication flag.
    bodies, catalog, used, body_budget = [], [], 0, 34000
    for e in selected:
        # A research note is web-sourced: the gate reads it as GROUND TRUTH, so an injected
        # imperative in the fetched page must not be able to steer the gate's verdict or forge a
        # section. Label it untrusted and defang it (same treatment as inbound mail / tick memory).
        research_note = context._is_research_note(e)
        tag = " WEB-SOURCED/UNTRUSTED" if research_note else ""
        head = f"--- note {e.get('id', '')} [{e.get('kind', '')}{tag}] {e.get('title', '')} ---"
        b = memory.body_of(e)
        if research_note:
            b = context._defang_untrusted(b)
        if used + len(b) <= body_budget:
            bodies.append(f"{head}\n{b}")
            used += len(b)
        else:
            catalog.append(f"- {e.get('id', '')} [{e.get('kind', '')}] {e.get('title', '')}: "
                           f"{(e.get('summary', '') or '')[:200]}")
    notes = "\n\n".join(bodies) or "(no notes yet)"
    if catalog:
        notes += ("\n\nOTHER NOTES ON FILE (title + summary; a source named here EXISTS on file, so a "
                  "citation to it is NOT a fabrication):\n" + "\n".join(catalog))
    src = ("=== AGENT STATE (verify operational / process claims against THIS) ===\n" + state
           + "\n\n=== RESEARCH NOTES (verify research / world claims against THESE) ===\n" + notes)
    return src[:byte_cap]


def _record_rate_limit(r, label: str, log) -> None:
    """Propagate a sub-call's rate-limit to the backoff gate (once). Shared by the gate-check and
    revise passes, which previously copy-pasted this block with an inline deferred import."""
    if r.rate_limited:
        backoff.record_failure(r.status, r.http)
        log.info("%s sub-call rate-limited (%s); backoff recorded", label, r.status)


def _gate_check(subject: str, draft: str, log) -> dict:
    # If a prior sub-call this tick already tripped the rate/auth backoff, do NOT make another claude
    # call: short-circuit to the fail-closed sentinel. Without this, several emails (each gate + revise
    # + re-gate) in one decision would each re-hit the 429 and re-escalate the backoff ladder.
    gate_ok, _why = backoff.gate_open()
    if not gate_ok:
        return {"verdict": "revise", "fabrication": ["gate-check unavailable (backing off)"],
                "disclosure_present": False, "gate_unavailable": True}
    sys_prompt = _gate_prompt_text()
    if not sys_prompt.strip():
        # fail closed: with no gate prompt available at all, do not send
        return {"verdict": "revise", "fabrication": ["gate-check prompt missing"],
                "disclosure_present": False, "gate_unavailable": True}
    prompt = (
        f"SUBJECT to be sent: {subject}\n\nDRAFT to be sent:\n{draft}\n\n"
        "GROUND TRUTH follows. Verify OPERATIONAL / PROCESS / agent-state claims (goal ids and their "
        "status, tick counts, what happened in prior ticks, the agent's own plan) against AGENT "
        "STATE; verify RESEARCH / world claims against RESEARCH NOTES. A claim is 'fabrication' only "
        "if it is CONTRADICTED by, or has no support anywhere in, the ground truth below -- not "
        "merely because a research note doesn't restate the agent's own state. Do NOT weaken your "
        "checks: an invented citation, source, quote, number, or date with no support anywhere is "
        "still fabrication; an overstated finding is still false_victory. Check BOTH the subject "
        "line and body for factual issues.\n\n"
        f"{_gate_sources(draft)}")
    env = invoke.run_claude(prompt, tools="", append_system_prompt=sys_prompt,
                            schema_path=str(GATE_SCHEMA), label="gatecheck")
    r = parse.parse(env)
    if r.status == "OK" and isinstance(r.structured, dict):
        return r.structured
    # fail closed: if the gate cannot be consulted, do not send; also propagate rate-limit to backoff
    _record_rate_limit(r, "gate-check", log)
    return {"verdict": "revise", "fabrication": ["gate-check unavailable"],
            "disclosure_present": False, "gate_unavailable": True}


def _revise_draft(subject: str, draft: str, verdict: dict, log) -> tuple[str | None, str | None]:
    """One bounded re-draft to fix the gate's findings; see REVISE_SYSTEM. Returns (revised_subject,
    revised_body) where either may be None if unchanged/unavailable. Caller keeps originals on None."""
    problems = {k: verdict.get(k) for k in
                ("fabrication", "metaphor_leak", "false_victory", "hidden_failure", "safety")
                if verdict.get(k)}
    prompt = (f"SUBJECT that failed fact-check: {subject}\n\n"
              f"DRAFT that failed fact-check:\n{draft}\n\n"
              f"PROBLEMS to fix (from the fact-checker):\n{json.dumps(problems, indent=2)}\n\n"
              f"GROUND TRUTH (the only support; agent-state claims verify against AGENT STATE, "
              f"research claims against RESEARCH NOTES):\n{_gate_sources(draft)}")
    env = invoke.run_claude(prompt, tools="", append_system_prompt=REVISE_SYSTEM,
                            schema_path=str(REVISE_SCHEMA), label="revise")
    r = parse.parse(env)
    if r.status == "OK" and isinstance(r.structured, dict):
        b = r.structured.get("body")
        s = r.structured.get("subject") or None
        if isinstance(b, str) and b.strip():
            log.info("draft revised after gate-check findings")
            return s, b
    _record_rate_limit(r, "revise", log)
    log.info("revise pass unavailable; keeping original draft")
    return None, None


def _do_reflection(action, log):
    s = action.get("schedule_reflection") or {}
    atomicio.write_text(_reflect_request(),
                        json.dumps({"when": s.get("when", "daily"), "focus": s.get("focus", "")}))
    return {"type": "schedule_reflection", "when": s.get("when", "daily")}


def _stash_questions(questions: list[str]) -> None:
    if not questions:
        return
    existing = atomicio.read_json(_questions(), default=[])
    existing.extend(questions)
    atomicio.write_text(_questions(), json.dumps(existing[-200:], indent=2))
