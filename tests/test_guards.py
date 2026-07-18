"""Step-4 guard hardening: the gate-check `safety` array is enforced in code (H8), and the
deterministic secret guard catches a Gmail app password in its grouped display form (L6)."""
import json
import logging
from types import SimpleNamespace

import pytest

from _helpers import load_secret_guard as _load_secret_guard   # shared; was duplicated per-file
from cagent import cli, clock, config
from cagent.cognition import execute, research

log = logging.getLogger("t")


@pytest.fixture(autouse=True)
def _no_backlog(monkeypatch):
    """Keep the outbound-backpressure gate inert (and off the real pending/ dir) for these guard tests."""
    from cagent import supervise
    monkeypatch.setattr(supervise, "backlog_depth", lambda: 0)


def test_safety_flag_blocks_send_even_when_verdict_is_send(monkeypatch):
    # H8: the gate schema permits {"verdict":"send","safety":[...]}; a safety flag must block in CODE,
    # never be left to the model returning the right verdict.
    monkeypatch.setattr(execute, "_gate_check",
                        lambda subject, body, log: {"verdict": "send", "safety": ["leaked a credential"],
                                                    "disclosure_present": True})
    sent = []
    monkeypatch.setattr(execute.gmail, "send", lambda **k: sent.append(k))
    res = execute._do_email({"email": {"subject": "Hi", "body": "real body content here"}},
                            SimpleNamespace(MODE="LIVE", max_backlog_drafts=999), log)
    assert "blocked_by_gate" in res          # blocked despite verdict "send"
    assert sent == []                        # nothing went out


def test_clean_verdict_is_not_blocked(monkeypatch):
    # sanity: verdict "send" with an EMPTY safety array still proceeds (to the send path).
    monkeypatch.setattr(execute, "_gate_check",
                        lambda subject, body, log: {"verdict": "send", "safety": [], "disclosure_present": True})
    sent = []
    monkeypatch.setattr(execute.gmail, "send",
                        lambda **k: sent.append(k) or SimpleNamespace(ok=True, dry_run=True, to="o"))
    res = execute._do_email({"email": {"subject": "Hi", "body": "real body content here"}},
                            SimpleNamespace(MODE="LIVE", max_backlog_drafts=999), log)
    assert "blocked_by_gate" not in res and len(sent) == 1




def test_received_mail_with_raw_command_token_is_flagged(tmp_path, monkeypatch):
    # Residual leak backstop: if redaction ran with an empty/mismatched token, the RAW configured
    # COMMAND_TOKEN sits in a received-mail JSON. The guard matches the EXACT configured value(s)
    # (read from ~/.config/cagent/.env*), so it catches the token in any field yet never trips on a
    # free-text command arg -- the false-positive that wedged daily-push.
    sg = _load_secret_guard()
    k = "COMMAND_" + "TOKEN"   # split so this test file never trips the assignment PATTERN itself
    (tmp_path / ".env").write_text(f"{k}=JsutDoIt2026!\n")
    (tmp_path / ".env-owner-2").write_text(f'{k}="Sk8rB0i-tok"\n')   # a second owner's token
    monkeypatch.setenv("CAGENT_CONFIG_DIR", str(tmp_path))
    tokens = sg._configured_tokens()
    assert tokens == {"JsutDoIt2026!", "Sk8rB0i-tok"}

    def hit(s):
        return bool(sg._leaked_tokens(s, tokens))

    # Raw token present (any field, any persona's token) -> flag
    assert hit('"subject": "!PAUSE JsutDoIt2026!"')
    assert hit('"subject": "[bravo] !GOAL add a goal JsutDoIt2026!"')
    assert hit('"body_text": "!PING Sk8rB0i-tok please"')          # a cross-persona token still caught
    # Already redacted with the placeholder -> no flag (the raw value is gone)
    assert not hit('"subject": "!PAUSE «COMMAND_TOKEN»"')
    # Free-text commands whose prose contains long words / a mistyped token guess -> NO false positive
    assert not hit('"subject": "!GOAL All the collaborators of Afra Mashhadi, a deep dive"')
    assert not hit('"subject": "!GOAL Conditional versus unconditional love. What does the literature say?"')
    assert not hit('"subject": "!FEEDBACK your last summary was excellent, keep going"')
    # Normal email subjects -> no flag
    assert not hit('"subject": "Hello from the owner"')


def test_configured_tokens_absent_dir_is_empty(tmp_path, monkeypatch):
    # No secrets dir (dev clone / CI): no tokens, so the received-mail backstop is a no-op there.
    sg = _load_secret_guard()
    monkeypatch.setenv("CAGENT_CONFIG_DIR", str(tmp_path / "nope"))
    assert sg._configured_tokens() == set()
    assert sg._leaked_tokens('"subject": "!GOAL anything at all here"', set()) == set()


def test_command_token_assignment_is_flagged():
    # H4 follow-up: COMMAND_TOKEN=<value> in a staged file must be caught by the guard. A bare
    # reference to the variable name (as in code or docs) must NOT be flagged.
    sg = _load_secret_guard()

    def hit(s):
        return any(pat.search(s) for pat in sg.PATTERNS)

    k = "COMMAND_" + "TOKEN"
    assert hit(f"{k}=mysecrettoken42")        # plain assignment
    assert hit(f'{k}="mysecrettoken42"')      # quoted
    assert not hit(f"{k}=")                   # blank template
    assert not hit(k)                         # bare variable name in code
    assert not hit(f"# {k} controls auth")    # comment mentioning the name


def test_gmail_password_grouped_form_is_flagged():
    # L6: the old base64 pattern let the spaced display form slip past. NOTE: the key name is built
    # from a variable so this test file itself never contains the literal `<KEY>=<value>` that the
    # secret guard (which scans every staged file, including this one) would otherwise block.
    sg = _load_secret_guard()
    k = "GMAIL_APP_" + "PASSWORD"

    def hit(s):
        return any(pat.search(s) for pat in sg.PATTERNS)

    assert hit(f"{k}=abcdefghijklmnop")                              # spaceless
    assert hit(f"{k}=abcd efgh ijkl mnop")                           # grouped display form (old miss)
    assert hit(f'{k}="abcdefghijklmnop"')                            # quoted
    assert not hit(f"{k}=")                                          # blank template value
    assert not hit(f"{k}=            # 16-char Google app password")  # doc placeholder


def test_research_today_count_skips_malformed_line(tmp_path, monkeypatch):
    # L1: one bad line in the research ledger must not abort the daily-cap count (which would make
    # the cap un-enforceable that day).
    led = tmp_path / "research_ledger.jsonl"
    today = clock.today()
    led.write_text(json.dumps({"date": today}) + "\nNOT JSON\n" + json.dumps({"date": today}) + "\n")
    monkeypatch.setattr(research, "_ledger", lambda: led)
    assert research._today_count() == 2          # counted the two valid rows, skipped the junk


def test_validate_personas_flags_bad_default(monkeypatch):
    # L7: an untagged-mail `default` that is not an enabled persona must be surfaced.
    monkeypatch.setattr(config, "default_persona", lambda: "doesnotexist")
    rows = config.validate_personas()
    dflt = [r for r in rows if r[0] == "[personas].default is enabled"]
    assert dflt and dflt[0][1] is False


def test_reset_refuses_on_mirror(monkeypatch, tmp_path):
    # M8: the destructive reset must refuse on a mirror (where it would collide with the host's pull)
    # unless --force-mirror is given. The guard must return BEFORE any deletion.
    #
    # This test previously relied on the ambient absence of var/STOP so it returned 2 at the
    # "not stopped" guard -- never reaching the mirror check it claims to test, and (if var/STOP
    # happened to exist and the mirror guard regressed) it would have deleted the REAL repo state.
    # Here we redirect REPO_ROOT to a throwaway tree, create var/STOP so the "not stopped" guard
    # passes, and make single_flight() explode if reached -- so the assertions can ONLY pass by
    # returning at the mirror guard, and a regression that fell through would fail loudly instead of
    # destroying anything.
    from cagent import locking
    (tmp_path / "var").mkdir()
    (tmp_path / "var" / "STOP").write_text("")                 # get past the "agent is not stopped" guard
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cli, "_mirror_note", lambda root: "MIRROR detected")

    def _boom():
        raise AssertionError("reached single_flight(): the mirror guard did NOT stop the deletion")
    monkeypatch.setattr(locking, "single_flight", _boom)

    assert cli.cmd_reset(["--yes"]) == 2                       # refused at the mirror guard
    assert cli.cmd_migrate_persona(["alpha", "--yes"]) == 2    # same guard on migrate (M9)


def test_reset_seeds_persona_state_where_the_reader_looks(monkeypatch, tmp_path):
    # cmd_reset used to write the seed persona-state.json under state/<...>/, which persona.state_path()
    # never reads -- so `reset` left the real arc (stage, victories, invariants_hash) untouched and
    # committed a dead orphan. It must write to persona.state_path() (personas/<name>/persona-state.json).
    import contextlib

    from cagent import locking, persona
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    (tmp_path / "var").mkdir()
    (tmp_path / "var" / "STOP").write_text("")                 # satisfy the "agent stopped" guard
    monkeypatch.setattr(cli, "_mirror_note", lambda root: None)   # not a mirror
    monkeypatch.setattr(persona, "invariants_hash", lambda: "deadbeef")
    monkeypatch.setattr(locking, "single_flight", lambda: contextlib.nullcontext())

    assert cli.cmd_reset(["--yes", "--persona", "alpha"]) == 0
    read_path = tmp_path / "personas" / "alpha" / "persona-state.json"
    assert read_path.exists()                                  # written where the reader looks
    d = json.loads(read_path.read_text())
    assert d["arc_stage"] == "idealism" and d["invariants_hash"] == "deadbeef"
    # and NOT dumped into the state/ namespace where nothing reads it
    assert not (config.state_root() / "persona-state.json").exists()
