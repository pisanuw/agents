"""Handler-level coverage for cognition/execute: the bounded action dispatch (apply) and every
individual handler (_do_research/_do_note/_do_goals/_do_reflection, _stash_questions, the research
formatter, the gate-source assembly's overflow catalog, _revise_draft, and _do_email's supervised /
send-refused / revise-then-send branches). This is the "Python disposes" surface — the model
proposes actions but only these bounded handlers act — so every branch here is safety-relevant.

State-writing handlers run against a tmp sandbox (REPO_ROOT + state_root redirected) so notes,
goals, and request files land in tmp_path and the real repo is never touched.
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from cagent import config, goals as goals_mod
from cagent.cognition import execute

log = logging.getLogger("t")


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect REPO_ROOT + state_root() at tmp_path so memory.write_note()'s relative_to() resolves
    and goals/questions/reflect-request files stay in the sandbox."""
    sroot = tmp_path / "state"
    sroot.mkdir()
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config, "state_root", lambda *a: sroot)
    return sroot


def _cfg(**kw):
    return SimpleNamespace(**{"research_per_tick": 6, "MODE": "LIVE", "max_backlog_drafts": 999, **kw})


@pytest.fixture(autouse=True)
def _no_backlog(monkeypatch):
    """Default the outbound-backlog probe to 0 for every _do_email test, so the backpressure gate is
    inert (and no test reads the real repo's pending/ dir). Tests that exercise backpressure override
    this by re-patching supervise.backlog_depth."""
    from cagent import supervise
    monkeypatch.setattr(supervise, "backlog_depth", lambda: 0)


# --------------------------------------------------------------------------- #
# apply(): the bounded dispatch loop
# --------------------------------------------------------------------------- #

def test_apply_routes_each_action_type(monkeypatch):
    monkeypatch.setattr(execute, "_do_research", lambda a, log: {"type": "research"})
    monkeypatch.setattr(execute, "_do_note", lambda a, log: {"type": "write_note"})
    monkeypatch.setattr(execute, "_do_goals", lambda a, log: {"type": "update_goals"})
    monkeypatch.setattr(execute, "_do_email", lambda a, cfg, log: {"type": "send_email"})
    monkeypatch.setattr(execute, "_do_reflection", lambda a, log: {"type": "schedule_reflection"})
    decision = {"actions": [{"type": "write_note"}, {"type": "update_goals"},
                            {"type": "send_email"}]}
    res = execute.apply(decision, _cfg(), log)
    assert [r["type"] for r in res] == ["write_note", "update_goals", "send_email"]


def test_apply_caps_at_three_actions(monkeypatch):
    seen = []
    monkeypatch.setattr(execute, "_do_note", lambda a, log: seen.append(a["note"]["title"]) or {"type": "write_note"})
    decision = {"actions": [{"type": "write_note", "note": {"title": str(i)}} for i in range(5)]}
    res = execute.apply(decision, _cfg(), log)
    assert len(res) == 3 and seen == ["0", "1", "2"]        # only the first three run


def test_apply_noop_and_unknown_type():
    decision = {"actions": [{"type": "noop", "reason": "resting"}, {"type": "frobnicate"}]}
    res = execute.apply(decision, _cfg(), log)
    assert res[0] == {"type": "noop", "reason": "resting"}
    assert res[1] == {"type": "frobnicate", "skipped": "unknown action type"}


def test_apply_handler_exception_is_caught_not_fatal(monkeypatch):
    def _boom(a, log):
        raise ValueError("kaboom")
    monkeypatch.setattr(execute, "_do_note", _boom)
    monkeypatch.setattr(execute, "_do_goals", lambda a, log: {"type": "update_goals", "ok": True})
    res = execute.apply({"actions": [{"type": "write_note"}, {"type": "update_goals"}]}, _cfg(), log)
    assert res[0] == {"type": "write_note", "error": "kaboom"}   # caught, recorded
    assert res[1]["ok"] is True                                  # the tick continued


def test_apply_research_per_tick_cap(monkeypatch):
    calls = []
    monkeypatch.setattr(execute, "_do_research", lambda a, log: calls.append(1) or {"type": "research"})
    decision = {"actions": [{"type": "research"}, {"type": "research"}]}
    res = execute.apply(decision, _cfg(research_per_tick=1), log)
    assert len(calls) == 1                                       # second research call was capped
    assert res[1] == {"type": "research", "skipped": "per-tick research cap"}


def test_apply_skips_research_when_backlog_full(monkeypatch):
    # A (hard research gate): a full outbound queue skips research deterministically, before _do_research.
    from cagent import supervise
    monkeypatch.setattr(supervise, "backlog_depth", lambda: 6)
    calls = []
    monkeypatch.setattr(execute, "_do_research", lambda a, log: calls.append(1) or {"type": "research"})
    decision = {"actions": [{"type": "research"}]}
    res = execute.apply(decision, _cfg(max_backlog_drafts=6), log)
    assert calls == []                                           # no web-research sub-call was spent
    assert res[0] == {"type": "research", "skipped": "outbound backlog full (backpressure)"}


def test_apply_research_runs_when_backlog_below_cap(monkeypatch):
    from cagent import supervise
    monkeypatch.setattr(supervise, "backlog_depth", lambda: 5)
    calls = []
    monkeypatch.setattr(execute, "_do_research", lambda a, log: calls.append(1) or {"type": "research"})
    res = execute.apply({"actions": [{"type": "research"}]}, _cfg(max_backlog_drafts=6), log)
    assert calls == [1] and res[0] == {"type": "research"}


def test_apply_no_actions_returns_empty():
    assert execute.apply({}, _cfg(), log) == []
    assert execute.apply({"actions": None}, _cfg(), log) == []


# --------------------------------------------------------------------------- #
# _do_research + _format_research
# --------------------------------------------------------------------------- #

def test_do_research_empty_query_skipped():
    assert execute._do_research({"research": {"query": "   "}}, log)["skipped"] == "empty query"


def test_do_research_none_result_writes_no_note(monkeypatch):
    monkeypatch.setattr(execute.research, "run", lambda q: None)
    wrote = []
    monkeypatch.setattr(execute.memory, "write_note", lambda *a, **k: wrote.append(1))
    res = execute._do_research({"research": {"query": "the void"}}, log)
    assert "no findings" in res["result"] and wrote == []       # no zero-findings note polluting memory


def test_do_research_saves_note_progress_and_questions(sandbox, monkeypatch):
    out = {"query": "q", "findings": [{"claim": "c", "source_url": "u", "source_title": "t",
                                       "confidence": "high"}],
           "new_questions": ["what next"], "dead_ends": []}
    monkeypatch.setattr(execute.research, "run", lambda q: out)
    prog = []
    monkeypatch.setattr(execute.goals_mod, "add_progress", lambda gid, note: prog.append((gid, note)))
    res = execute._do_research({"research": {"query": "the query", "goal_id": "G1"}}, log)
    assert res["findings"] == 1 and res["note"].startswith("state/memory")
    assert prog and prog[0][0] == "G1" and "researched" in prog[0][1]
    assert "what next" in json.loads((sandbox / "questions.json").read_text())


def test_format_research_renders_all_sections():
    out = {"query": "Q", "findings": [{"claim": "C", "source_url": "http://u",
                                       "source_title": "T", "confidence": "high"}],
           "dead_ends": ["nowhere"], "new_questions": ["huh"]}
    s = execute._format_research(out)
    assert "# Research: Q" in s
    assert "- C" in s and "T http://u (high)" in s
    assert "## Dead ends" in s and "- nowhere" in s
    assert "## New questions" in s and "- huh" in s


# --------------------------------------------------------------------------- #
# _do_note
# --------------------------------------------------------------------------- #

def test_do_note_empty_body_skipped():
    assert execute._do_note({"note": {"title": "T", "body": "  "}}, log)["skipped"] == "empty body"


def test_do_note_writes_note_and_progress(sandbox, monkeypatch):
    prog = []
    monkeypatch.setattr(execute.goals_mod, "add_progress", lambda gid, note: prog.append((gid, note)))
    res = execute._do_note({"note": {"title": "My Note", "body": "Hello world",
                                     "goal_id": "G2", "tags": ["essay"]}}, log)
    assert res["note"].endswith(".md")
    assert prog and prog[0][0] == "G2"
    assert (sandbox / "memory" / "index.jsonl").exists()        # indexed


# --------------------------------------------------------------------------- #
# _do_goals
# --------------------------------------------------------------------------- #

def test_do_goals_upsert_and_retire(sandbox):
    goals_mod.upsert({"id": "G1", "title": "to retire"})
    action = {"update_goals": {"upsert": [{"id": "G2", "title": "new"}],
                               "retire": ["G1"], "rationale": "spring cleaning"}}
    res = execute._do_goals(action, log)
    assert ("upsert", "G2") in res["changes"] and ("retire", "G1") in res["changes"]
    assert res["rationale"] == "spring cleaning"
    ids = [g["id"] for g in goals_mod.load()]
    assert "G2" in ids and "G1" not in ids
    assert any(a["id"] == "G1" for a in goals_mod.archived())    # retired goal preserved in archive


# --------------------------------------------------------------------------- #
# _do_reflection + _stash_questions
# --------------------------------------------------------------------------- #

def test_do_reflection_writes_request(sandbox):
    res = execute._do_reflection({"schedule_reflection": {"when": "weekly", "focus": "goals"}}, log)
    assert res["when"] == "weekly"
    req = json.loads((sandbox / "reflect_request.json").read_text())
    assert req == {"when": "weekly", "focus": "goals"}


def test_do_reflection_defaults_when_missing(sandbox):
    res = execute._do_reflection({}, log)
    assert res["when"] == "daily"
    assert json.loads((sandbox / "reflect_request.json").read_text())["when"] == "daily"


def test_stash_questions_appends_and_caps_at_200(sandbox):
    execute._stash_questions([f"q{i}" for i in range(150)])
    execute._stash_questions([f"r{i}" for i in range(100)])
    stored = json.loads((sandbox / "questions.json").read_text())
    assert len(stored) == 200 and stored[-1] == "r99"           # tail kept, oldest dropped


def test_stash_questions_noop_on_empty(sandbox):
    execute._stash_questions([])
    assert not (sandbox / "questions.json").exists()


# --------------------------------------------------------------------------- #
# _recent_journal + _latest_reflection (gate ground-truth helpers)
# --------------------------------------------------------------------------- #

def test_recent_journal_filters_ticks_and_formats(sandbox):
    rows = [
        {"kind": "tick", "ts": "2026-07-01T10:00:00Z", "ok": True, "summary": "did a thing"},
        {"kind": "reflection", "summary": "should be ignored"},
        {"kind": "tick", "ts": "2026-07-02T11:00:00Z", "ok": False, "status": "boom"},
    ]
    (sandbox / "journal.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    out = execute._recent_journal(8)
    assert "did a thing" in out and "boom" in out
    assert "FAIL" in out                                        # ok:False -> FAIL marker
    assert "should be ignored" not in out                       # non-tick rows excluded


def test_recent_journal_empty_sentinel(sandbox):
    assert execute._recent_journal() == "(no prior ticks)"


def test_latest_reflection_returns_last_body(monkeypatch):
    entries = [{"kind": "note", "id": "1"}, {"kind": "reflection", "id": "2"},
               {"kind": "reflection", "id": "3"}]
    monkeypatch.setattr(execute.memory, "index_entries", lambda: entries)
    monkeypatch.setattr(execute.memory, "body_of", lambda e: f"body-{e['id']}")
    assert execute._latest_reflection() == "body-3"


def test_latest_reflection_none_when_absent(monkeypatch):
    monkeypatch.setattr(execute.memory, "index_entries", lambda: [{"kind": "note"}])
    assert execute._latest_reflection() == ""


# --------------------------------------------------------------------------- #
# _gate_prompt_text: persona override vs neutral default
# --------------------------------------------------------------------------- #

def test_gate_prompt_text_prefers_persona_override(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    d = tmp_path / "personas" / "bravo"
    d.mkdir(parents=True)
    (d / "gate-check.md").write_text("CEDAR GATE PROMPT")
    monkeypatch.setenv("CAGENT_PERSONA", "bravo")
    assert execute._gate_prompt_text() == "CEDAR GATE PROMPT"


def test_gate_prompt_text_falls_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(execute, "GATE_PROMPT_DEFAULT", tmp_path / "gate.md")
    (tmp_path / "gate.md").write_text("NEUTRAL DEFAULT")
    monkeypatch.delenv("CAGENT_PERSONA", raising=False)
    assert execute._gate_prompt_text() == "NEUTRAL DEFAULT"


# --------------------------------------------------------------------------- #
# _gate_check happy path + _gate_sources overflow catalog
# --------------------------------------------------------------------------- #

def test_gate_check_returns_structured_verdict(monkeypatch):
    monkeypatch.setattr(execute.backoff, "gate_open", lambda: (True, ""))
    monkeypatch.setattr(execute, "_gate_prompt_text", lambda: "a gate prompt")
    monkeypatch.setattr(execute, "_gate_sources", lambda *a, **k: "GROUND TRUTH")
    monkeypatch.setattr(execute.invoke, "run_claude", lambda *a, **k: object())
    monkeypatch.setattr(execute.parse, "parse",
                        lambda env: SimpleNamespace(status="OK", rate_limited=False, http=None,
                                                    structured={"verdict": "send", "disclosure_present": True}))
    assert execute._gate_check("S", "draft body", log)["verdict"] == "send"


def test_gate_sources_overflow_notes_go_to_catalog(monkeypatch):
    monkeypatch.setattr(execute.goals_mod, "load", lambda: [])
    monkeypatch.setattr(execute.goals_mod, "archived", lambda: [])
    monkeypatch.setattr(execute, "_recent_journal", lambda n=8: "(none)")
    monkeypatch.setattr(execute, "_latest_reflection", lambda: "")
    monkeypatch.setattr(execute.context, "_is_research_note", lambda e: False)
    big = {"id": "big", "kind": "note", "title": "Big", "summary": "big summary"}
    small = {"id": "small", "kind": "note", "title": "Small", "summary": "overflowed summary"}
    monkeypatch.setattr(execute.memory, "select_by_text", lambda text, n=24: [big, small])
    monkeypatch.setattr(execute.memory, "body_of", lambda e: "X" * 34000 if e["id"] == "big" else "tiny")
    src = execute._gate_sources("draft text", byte_cap=200000)
    assert "OTHER NOTES ON FILE" in src                         # catalog section emitted
    assert "overflowed summary" in src                          # the note that didn't fit as a full body


# --------------------------------------------------------------------------- #
# _revise_draft
# --------------------------------------------------------------------------- #

def test_revise_draft_returns_new_subject_and_body(monkeypatch):
    monkeypatch.setattr(execute, "_gate_sources", lambda *a, **k: "GT")
    monkeypatch.setattr(execute.invoke, "run_claude", lambda *a, **k: object())
    monkeypatch.setattr(execute.parse, "parse",
                        lambda env: SimpleNamespace(status="OK", rate_limited=False, http=None,
                                                    structured={"body": "fixed body", "subject": "Fixed"}))
    s, b = execute._revise_draft("Subj", "draft", {"fabrication": ["x"]}, log)
    assert s == "Fixed" and b == "fixed body"


def test_revise_draft_unavailable_records_backoff_and_keeps_original(monkeypatch):
    monkeypatch.setattr(execute, "_gate_sources", lambda *a, **k: "GT")
    monkeypatch.setattr(execute.invoke, "run_claude", lambda *a, **k: object())
    monkeypatch.setattr(execute.parse, "parse",
                        lambda env: SimpleNamespace(status="RATE_LIMIT", rate_limited=True, http=429,
                                                    structured=None))
    rec = []
    monkeypatch.setattr(execute.backoff, "record_failure", lambda status, http=None: rec.append((status, http)))
    assert execute._revise_draft("Subj", "draft", {}, log) == (None, None)
    assert rec == [("RATE_LIMIT", 429)]


# --------------------------------------------------------------------------- #
# _do_email: empty-body, supervised, refused, revise-then-send branches
# --------------------------------------------------------------------------- #

def test_do_email_empty_body_skipped():
    res = execute._do_email({"email": {"subject": "Hi", "body": "  "}}, _cfg(), log)
    assert res["skipped"] == "empty body"


def test_do_email_supervised_stages_and_requests_approval(monkeypatch):
    from cagent import supervise
    monkeypatch.setattr(execute, "_gate_check", lambda s, b, log: {"verdict": "send", "disclosure_present": True})
    monkeypatch.setattr(supervise, "stage_draft", lambda subj, body, kind: "TOK123")
    reqs = []
    monkeypatch.setattr(supervise, "request_approval",
                        lambda tok, subj, body, cfg, log: reqs.append((tok, subj)))
    res = execute._do_email({"email": {"subject": "Hi", "body": "a real body", "kind": "finding"}},
                            _cfg(MODE="SUPERVISED"), log)
    assert res["supervised"] is True and res["token"] == "TOK123"
    assert reqs == [("TOK123", "Hi")]


def test_do_email_send_refused_is_caught(monkeypatch):
    monkeypatch.setattr(execute, "_gate_check", lambda s, b, log: {"verdict": "send"})

    def _refuse(**k):
        raise execute.gmail.SendRefused("weekly cap exceeded")
    monkeypatch.setattr(execute.gmail, "send", _refuse)
    res = execute._do_email({"email": {"subject": "Hi", "body": "a real body"}}, _cfg(MODE="LIVE"), log)
    assert res["refused"] == "weekly cap exceeded"


def test_do_email_backpressure_refuses_at_cap_before_gatecheck(monkeypatch):
    # Queue is at the cap -> the draft is refused BEFORE any claude call (gate-check) is spent.
    from cagent import supervise
    monkeypatch.setattr(supervise, "backlog_depth", lambda: 6)
    gate_calls = []
    monkeypatch.setattr(execute, "_gate_check", lambda s, b, log: gate_calls.append(1) or {"verdict": "send"})
    res = execute._do_email({"email": {"subject": "Hi", "body": "a real body"}},
                            _cfg(MODE="SUPERVISED", max_backlog_drafts=6), log)
    assert res["backpressure"] == {"backlog": 6, "cap": 6}
    assert gate_calls == []          # no gate-check claude call was spent on an undeliverable draft


def test_do_email_backpressure_inert_below_cap(monkeypatch):
    from cagent import supervise
    monkeypatch.setattr(supervise, "backlog_depth", lambda: 5)
    monkeypatch.setattr(execute, "_gate_check", lambda s, b, log: {"verdict": "send", "disclosure_present": True})
    monkeypatch.setattr(supervise, "stage_draft", lambda subj, body, kind: "TOK9")
    monkeypatch.setattr(supervise, "request_approval", lambda tok, subj, body, cfg, log: True)
    res = execute._do_email({"email": {"subject": "Hi", "body": "a real body", "kind": "finding"}},
                            _cfg(MODE="SUPERVISED", max_backlog_drafts=6), log)
    assert res.get("supervised") is True and "backpressure" not in res


def test_do_email_revises_then_sends(monkeypatch):
    verdicts = iter([{"verdict": "revise", "fabrication": ["x"]}, {"verdict": "send"}])
    monkeypatch.setattr(execute, "_gate_check", lambda s, b, log: next(verdicts))
    monkeypatch.setattr(execute, "_revise_draft",
                        lambda s, b, v, log: ("New Subject", "a different revised body"))
    sent = []
    monkeypatch.setattr(execute.gmail, "send",
                        lambda **k: sent.append(k) or SimpleNamespace(ok=True, dry_run=False, to="owner@x"))
    res = execute._do_email({"email": {"subject": "Hi", "body": "original body"}}, _cfg(MODE="LIVE"), log)
    assert res["revised"] is True and res["sent"] is True
    assert sent[0]["subject"] == "New Subject" and sent[0]["body_md"] == "a different revised body"
