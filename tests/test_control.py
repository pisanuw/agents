"""Phase 6: the git control plane -- directive parsing, routing, idempotent inbox processing,
and per-persona drain (goal -> upsert, instruction -> steering, note -> journal only)."""
import json

import pytest

from cagent import config, control, goals as goals_mod


class _Log:
    def __getattr__(self, _name):        # info / warning / error -> all no-ops
        return lambda *a, **k: None


LOG = _Log()
ENABLED = ["alpha", "data", "echozz"]


# --- pure parsing -----------------------------------------------------------------------

def test_parse_json_directive():
    d = control.parse_directive('{"persona":"data","type":"goal","text":"study androids"}')
    assert d == {"persona": "data", "type": "goal", "text": "study androids", "id": None}


def test_parse_header_body_directive():
    raw = "persona: alpha\ntype: instruction\n\nConsolidate findings this week."
    d = control.parse_directive(raw)
    assert d["persona"] == "alpha" and d["type"] == "instruction"
    assert d["text"] == "Consolidate findings this week."


def test_explicit_text_header_beats_body():
    raw = "type: note\ntext: short note\n\nignored body"
    assert control.parse_directive(raw)["text"] == "short note"


def test_unknown_type_is_rejected():
    assert control.parse_directive('{"type":"deploy","text":"x"}') is None
    assert control.parse_directive("type: shutdown\n\nnow") is None


def test_empty_and_malformed_return_none():
    assert control.parse_directive("") is None
    assert control.parse_directive("   ") is None
    assert control.parse_directive("{not json") is None
    assert control.parse_directive('["a","list"]') is None


def test_directive_id_prefers_explicit():
    assert control.directive_id({"id": "abc"}, "whatever") == "abc"


def test_directive_id_hashes_when_absent():
    h = control.directive_id({}, "some content")
    assert len(h) == 16 and control.directive_id({}, "some content") == h


# --- routing ----------------------------------------------------------------------------

def test_route_named_enabled_persona():
    assert control.target_persona({"persona": "data"}, ENABLED, "alpha") == "data"


def test_route_unknown_persona_falls_to_default():
    assert control.target_persona({"persona": "ghost"}, ENABLED, "alpha") == "alpha"


def test_route_missing_persona_falls_to_default():
    assert control.target_persona({}, ENABLED, "alpha") == "alpha"


# --- inbox processing (idempotent, side-effecting; redirected to tmp) --------------------

@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    inbox = tmp_path / "control" / "inbox"
    processed = tmp_path / "control" / "processed"
    inbox.mkdir(parents=True)
    monkeypatch.setattr(control, "CONTROL_INBOX", inbox)
    monkeypatch.setattr(control, "CONTROL_PROCESSED", processed)
    monkeypatch.setattr(control, "SEEN_LEDGER", tmp_path / "var" / "control_seen.jsonl")
    monkeypatch.setattr(control, "PERSONA_STOP_DIR", tmp_path / "var" / "persona")
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)   # queue_path / state_root resolve here
    return tmp_path, inbox, processed


def test_goal_directive_enqueued_and_archived(sandbox):
    tmp, inbox, processed = sandbox
    (inbox / "g1.json").write_text('{"persona":"data","type":"goal","text":"study androids"}')
    out = control.process_inbox(ENABLED, "alpha", LOG)
    assert out == [{"id": out[0]["id"], "type": "goal", "persona": "data"}]
    q = control.queue_path("data")
    assert q.exists()
    assert json.loads(q.read_text().splitlines()[0])["text"] == "study androids"
    assert not (inbox / "g1.json").exists()                      # moved out of the inbox
    assert list(processed.rglob("*g1.json"))                     # archived


def test_pause_then_resume_toggle_stop_file(sandbox):
    tmp, inbox, processed = sandbox
    (inbox / "p.json").write_text('{"persona":"echozz","type":"pause"}')
    control.process_inbox(ENABLED, "alpha", LOG)
    assert control.is_paused("echozz")
    (inbox / "r.json").write_text('{"persona":"echozz","type":"resume"}')
    control.process_inbox(ENABLED, "alpha", LOG)
    assert not control.is_paused("echozz")


def test_cli_stop_start_persona(sandbox, monkeypatch):
    # `cagentctl stop --persona data` pauses ONLY data (var/persona/data.STOP -- the same flag the
    # dispatcher and git-control/!PAUSE use), leaving the global var/STOP untouched; start clears it.
    from cagent import cli, config
    tmp, _, _ = sandbox
    monkeypatch.setattr(config, "known_personas", lambda: ENABLED)   # _persona_flag validates on this
    monkeypatch.delenv("CAGENT_PERSONA", raising=False)
    assert cli.cmd_stop(["--persona", "data"]) == 0
    assert control.is_paused("data")
    assert not (tmp / "var" / "STOP").exists()                       # global kill switch NOT engaged
    assert cli.cmd_start(["--persona", "data"]) == 0
    assert not control.is_paused("data")


def test_cli_stop_start_global_unchanged(sandbox, monkeypatch):
    # No --persona -> the original global behavior: writes/clears var/STOP, no per-persona file.
    from cagent import cli
    tmp, _, _ = sandbox
    monkeypatch.delenv("CAGENT_PERSONA", raising=False)
    assert cli.cmd_stop([]) == 0
    assert (tmp / "var" / "STOP").exists() and not (tmp / "var" / "persona").exists()
    assert cli.cmd_start([]) == 0
    assert not (tmp / "var" / "STOP").exists()


def test_cli_stop_unknown_persona_rejected(sandbox, monkeypatch):
    from cagent import cli, config
    monkeypatch.setattr(config, "known_personas", lambda: ENABLED)
    monkeypatch.delenv("CAGENT_PERSONA", raising=False)
    with pytest.raises(SystemExit):                                  # _persona_flag rejects a typo
        cli.cmd_stop(["--persona", "nope"])


def test_same_directive_applied_once(sandbox):
    tmp, inbox, processed = sandbox
    (inbox / "g.json").write_text('{"persona":"data","type":"goal","text":"once","id":"fixed"}')
    control.process_inbox(ENABLED, "alpha", LOG)
    # same id reappears (e.g. re-pulled); must NOT enqueue twice
    (inbox / "g-again.json").write_text('{"persona":"data","type":"goal","text":"once","id":"fixed"}')
    out = control.process_inbox(ENABLED, "alpha", LOG)
    assert out == []
    assert len(control.queue_path("data").read_text().splitlines()) == 1


def test_unparseable_directive_archived_as_rejected(sandbox):
    tmp, inbox, processed = sandbox
    (inbox / "bad.txt").write_text("type: explode\n\nboom")
    out = control.process_inbox(ENABLED, "alpha", LOG)
    assert out == []
    assert list(processed.rglob("rejected-bad.txt"))


def test_empty_default_rejects_untargeted_directive(sandbox):
    # An untargeted directive with an empty/misconfigured default must NOT be silently applied to
    # persona "" (a dead namespace) and marked done -- it is rejected visibly instead.
    tmp, inbox, processed = sandbox
    (inbox / "g.json").write_text('{"type":"goal","text":"a goal with no target"}')
    out = control.process_inbox(ENABLED, "", LOG)                 # empty default
    assert out == []                                             # nothing applied
    assert list(processed.rglob("rejected-g.json"))              # archived as rejected, not swallowed
    assert not control.queue_path("").exists()                  # no dead flat queue written


# --- drain (tick side) ------------------------------------------------------------------

@pytest.fixture
def persona_run(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("CAGENT_PERSONA", "data")
    state = tmp_path / "state" / "personas" / "data"
    monkeypatch.setattr(goals_mod, "_goals", lambda: state / "goals.json")
    monkeypatch.setattr(goals_mod, "_archive", lambda: state / "goals_archive.json")
    monkeypatch.setattr(goals_mod, "_history_path", lambda: state / "goals_history.jsonl")
    cfg = config.load("data")
    return tmp_path, state, cfg


def test_drain_applies_goal_instruction_note(persona_run):
    tmp, state, cfg = persona_run
    q = control.queue_path("data")
    q.parent.mkdir(parents=True, exist_ok=True)
    q.write_text(
        json.dumps({"type": "goal", "text": "find positronic calm"}) + "\n" +
        json.dumps({"type": "instruction", "text": "favor depth"}) + "\n" +
        json.dumps({"type": "note", "text": "fyi only"}) + "\n")
    applied = control.drain(cfg, LOG)
    assert [a["type"] for a in applied] == ["goal", "instruction", "note"]
    # goal landed in this persona's goal list
    assert any(g["title"] == "find positronic calm" for g in goals_mod.load())
    # instruction surfaced as steering; note did NOT
    steer = control.recent_steering()
    assert [s["text"] for s in steer] == ["favor depth"]
    # queue consumed
    assert not q.exists()


def test_drain_noop_without_queue(persona_run):
    tmp, state, cfg = persona_run
    assert control.drain(cfg, LOG) == []


def test_same_named_rejects_do_not_clobber_archive(sandbox):
    # P2-1: two different bad files sharing a name on the same day both REJECT to did='rejected'; the
    # second must not silently overwrite the first in the audit trail.
    tmp, inbox, processed = sandbox
    (inbox / "x.md").write_text("type: explode\n\nfirst bad body")
    control.process_inbox(ENABLED, "alpha", LOG)
    (inbox / "x.md").write_text("type: explode\n\nsecond bad body")
    control.process_inbox(ENABLED, "alpha", LOG)
    bodies = sorted(p.read_text() for p in processed.rglob("rejected-x*.md"))
    assert len(bodies) == 2                                             # both preserved (no clobber)
    assert any("first bad body" in b for b in bodies)
    assert any("second bad body" in b for b in bodies)


def test_drain_goal_is_idempotent_on_reapply(persona_run):
    # P1-3: a crash after upsert but before the queue is unlinked re-drains the same directive next
    # tick. The identical goal must be updated in place, not duplicated.
    tmp, state, cfg = persona_run

    def write_q():
        q = control.queue_path("data")
        q.parent.mkdir(parents=True, exist_ok=True)
        q.write_text(json.dumps({"type": "goal", "text": "unify the theory"}) + "\n")

    write_q()
    control.drain(cfg, LOG)
    write_q()
    control.drain(cfg, LOG)                                             # same directive drained twice
    matches = [x for x in goals_mod.load() if x["title"] == "unify the theory"]
    assert len(matches) == 1                                            # deduped, not duplicated
