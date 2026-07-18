"""Regression tests for the 2026-07-02 code-improve fixes:

- config mode_override tripwire is persona-namespaced (resolves the load() arg, not ambient env)
  and fails CLOSED (existence demotes LIVE even on torn/unrecognized content);
- .env parsing strips inline `# comment`s so values copied from .env.example are not corrupted;
- goals.retire() is crash-safe + idempotent (no duplicate archive entry on re-run);
- reflection claims the cadence marker + consumes its request BEFORE applying goal mutations, so a
  crash mid-apply cannot re-fire the whole reflection next tick;
- the secret guard's BLOCKED_PATH covers `.env-*` / `.env.local` overlays without over-matching
  ordinary source files.
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from _helpers import load_secret_guard as _load_secret_guard   # shared; was duplicated per-file
from cagent import atomicio, config
from cagent import goals as goals_mod
from cagent import reflect


# --------------------------------------------------------------------------- #
# config: mode_override tripwire (persona-namespaced + fail-closed)
# --------------------------------------------------------------------------- #

@pytest.fixture
def config_sandbox(tmp_path, monkeypatch):
    """Isolate ENV_PATH + REPO_ROOT so state_root()/mode_override never touch the live tree."""
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    monkeypatch.setattr(config, "ENV_PATH", cfgdir / ".env")
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.setattr(config, "CONFIG_TOML", tmp_path / "repo" / "config.toml")
    (tmp_path / "repo").mkdir()
    config.CONFIG_TOML.write_text("[personas]\nenabled = []\n")
    d = config.REPO_ROOT / "personas" / "bravo"
    d.mkdir(parents=True)
    (d / "config.toml").write_text("[agent]\n")
    monkeypatch.delenv("CAGENT_PERSONA", raising=False)
    monkeypatch.setenv("AGENT_MODE", "LIVE")
    return tmp_path


def _write_override(content: str) -> None:
    p = config.state_root("bravo") / "mode_override"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def test_mode_override_resolves_persona_arg_not_env(config_sandbox):
    # The tripwire must check the persona PASSED to load(), not the ambient CAGENT_PERSONA (unset
    # here). The old code read state_root() (flat) and reported the demoted persona as still LIVE.
    _write_override("SUPERVISED")
    assert config.load("bravo").MODE == "SUPERVISED"


def test_mode_override_fails_closed_on_torn_content(config_sandbox):
    # Existence alone demotes LIVE: a torn/empty or unrecognized override still drops to SUPERVISED.
    _write_override("")
    assert config.load("bravo").MODE == "SUPERVISED"
    _write_override("garbage!!")
    assert config.load("bravo").MODE == "SUPERVISED"


def test_mode_override_recognized_value_can_reach_dry_run(config_sandbox):
    _write_override("DRY_RUN")
    assert config.load("bravo").MODE == "DRY_RUN"


def test_no_override_leaves_live(config_sandbox):
    # Sanity: with no override the mode is NOT demoted (so the demotion is not spurious). LIVE with no
    # password then trips the downstream password guard -- that RuntimeError is the observable proof
    # that the tripwire left the mode at LIVE.
    with pytest.raises(RuntimeError):
        config.load("bravo")


# --------------------------------------------------------------------------- #
# config: inline-comment stripping in .env parsing
# --------------------------------------------------------------------------- #

def test_parse_env_strips_inline_comments(tmp_path):
    pw = "GMAIL_APP_" + "PASSWORD"   # built from parts: this file carries no literal secret assignment
    p = tmp_path / ".env"
    p.write_text(
        "OWNER_NAME=Example Owner  # your display name\n"
        f"{pw}=abcd efgh ijkl mnop  # 16-char app password\n"
        "BLANKISH=  # fill this in\n"
        'QUOTED="a # b"\n'
        "PLAIN=nospaces\n"
        "HASHNOSPACE=ab#cd\n"
    )
    env = config._parse_env(p)
    assert env["OWNER_NAME"] == "Example Owner"      # trailing note stripped
    assert env[pw] == "abcd efgh ijkl mnop"          # internal spaces kept, note stripped
    assert env["BLANKISH"] == ""                      # an all-comment value becomes empty
    assert env["QUOTED"] == '"a # b"'                 # a quoted value (with its #) is left intact
    assert env["PLAIN"] == "nospaces"
    assert env["HASHNOSPACE"] == "ab#cd"              # a '#' not preceded by whitespace is data


# --------------------------------------------------------------------------- #
# goals.retire: crash-safe + idempotent
# --------------------------------------------------------------------------- #

def _redirect_goals(tmp_path, monkeypatch):
    monkeypatch.setattr(goals_mod, "_goals", lambda: tmp_path / "goals.json")
    monkeypatch.setattr(goals_mod, "_archive", lambda: tmp_path / "archive.json")
    monkeypatch.setattr(goals_mod, "_history_path", lambda: tmp_path / "history.jsonl")


def test_retire_idempotent_no_duplicate_archive(tmp_path, monkeypatch):
    _redirect_goals(tmp_path, monkeypatch)
    goals_mod.upsert({"id": "G1", "title": "quest"})
    # Simulate a crash between the archive write and the goals.json write: the goal is already in the
    # archive AND still present (retired) in goals.json.
    goal = dict(goals_mod.load()[0], status="retired")
    atomicio.write_text(goals_mod._archive(), json.dumps([goal], indent=2))
    atomicio.write_text(goals_mod._goals(), json.dumps([goal], indent=2))
    # Recovery: retire() again must NOT duplicate the archive entry, and must complete the removal.
    goals_mod.retire("G1", "done")
    archive = json.loads(goals_mod._archive().read_text())
    assert [a["id"] for a in archive] == ["G1"]     # exactly one entry, no duplicate
    assert goals_mod.load() == []                    # removed from goals.json


def test_retire_after_full_completion_is_noop(tmp_path, monkeypatch):
    _redirect_goals(tmp_path, monkeypatch)
    goals_mod.upsert({"id": "G1", "title": "quest"})
    goals_mod.retire("G1", "done")
    goals_mod.retire("G1", "again")                  # id already gone -> no-op, no second archive row
    archive = json.loads(goals_mod._archive().read_text())
    assert [a["id"] for a in archive] == ["G1"]


# --------------------------------------------------------------------------- #
# reflect: claim-before-apply idempotency
# --------------------------------------------------------------------------- #

def test_reflection_claims_done_before_applying_mutations(tmp_path, monkeypatch):
    last_path = tmp_path / "last.json"
    req_path = tmp_path / "req.json"
    monkeypatch.setattr(reflect, "_last", lambda: last_path)
    monkeypatch.setattr(reflect, "_last_deep", lambda: tmp_path / "last_deep.json")
    monkeypatch.setattr(reflect, "_request", lambda: req_path)
    monkeypatch.setattr(reflect, "_journal", lambda: tmp_path / "journal.jsonl")
    monkeypatch.setattr(reflect, "_questions_path", lambda: tmp_path / "q.json")
    req_path.write_text("{}")

    monkeypatch.setattr(reflect.invoke, "run_claude", lambda *a, **k: {})
    monkeypatch.setattr(reflect.parse, "parse",
                        lambda env: SimpleNamespace(status="OK", structured={
                            "goals_to_retire": [{"id": "G1", "why": "x"}], "new_goals": [],
                            "headline_question": None, "summary_of_progress": "s"}))
    monkeypatch.setattr(reflect.persona, "load_system_prompt", lambda: "")

    def boom(*a, **k):
        raise RuntimeError("crash mid-apply")
    monkeypatch.setattr(reflect.goals_mod, "retire", boom)

    with pytest.raises(RuntimeError):
        reflect.run(config.load(), logging.getLogger("t"))

    # The cadence marker + request consumption happened BEFORE the crash, so the next tick will not
    # re-fire the whole reflection (no double-retire / duplicate goals / arc double-count).
    assert last_path.exists()
    assert not req_path.exists()


# --------------------------------------------------------------------------- #
# secret guard: BLOCKED_PATH covers .env overlays without over-matching source
# --------------------------------------------------------------------------- #

def test_blocked_path_covers_env_overlays():
    sg = _load_secret_guard()

    def blocked(path):
        return sg.BLOCKED_PATH.search(path) is not None

    assert blocked(".env")
    assert blocked(".env-mailbox-1")          # per-mailbox overlay
    assert blocked("cagent/.env-golf")        # nested per-persona overlay
    assert blocked(".env.local")              # local overlay
    assert blocked(".env.example")            # matches (main() explicitly allows this one)
    assert blocked("secrets/creds.txt")       # existing coverage intact
    assert blocked("host.pem")
    # Must NOT over-match ordinary source files whose name merely contains "env":
    assert not blocked("cagent/test_env_loader.py")
    assert not blocked("docs/environment.md")


def _run_guard_main(monkeypatch, files, contents):
    """Drive secret_guard.main() with a synthetic staged set: files -> content map."""
    sg = _load_secret_guard()
    monkeypatch.setattr(sg, "staged_files", lambda: files)
    monkeypatch.setattr(sg, "staged_content", lambda f: contents.get(f, ""))
    return sg.main()


def test_guard_scans_content_of_the_template(monkeypatch):
    # The exact template is exempt from the blocked-PATH rule but NOT from the content scan:
    # a real app password pasted into cagent/.env.example must still be caught.
    secret = "GMAIL_APP_" + "PASSWORD=abcdefghijklmnop"
    assert _run_guard_main(monkeypatch, ["cagent/.env.example"],
                           {"cagent/.env.example": secret}) == 1
    # Blank template (values empty) passes.
    assert _run_guard_main(monkeypatch, ["cagent/.env.example"],
                           {"cagent/.env.example": "GMAIL_APP_" + "PASSWORD=\n"}) == 0


def test_guard_blocks_non_template_env_example(monkeypatch):
    # Any *.env.example that is NOT the one committed template is treated as a blocked path,
    # closing the "secrets/creds.env.example slips through" hole.
    assert _run_guard_main(monkeypatch, ["secrets/creds.env.example"],
                           {"secrets/creds.env.example": "nothing secret here"}) == 1


def test_guard_diff_filter_includes_renames():
    # The rename (R) status must be in the diff filter, else a rename-plus-edit that introduces a
    # secret is never content-scanned. We assert the argv rather than run git.
    import inspect
    sg = _load_secret_guard()
    src = inspect.getsource(sg.staged_files)
    assert "--diff-filter=ACMR" in src


# --------------------------------------------------------------------------- #
# atomicio: fsync path still round-trips and cleans its temp
# --------------------------------------------------------------------------- #

def test_atomic_write_roundtrip_and_temp_cleanup(tmp_path):
    p = tmp_path / "sub" / "f.json"
    atomicio.write_text(p, '{"a": 1}')
    assert json.loads(p.read_text()) == {"a": 1}      # content intact after fsync + rename
    assert list(tmp_path.glob("**/*.tmp")) == []       # temp file removed, no leak


def test_atomic_write_propagates_fsync_failure_and_cleans_temp(tmp_path, monkeypatch):
    # If the pre-rename fsync fails, write_text must NOT create/replace the target (all-or-nothing),
    # and must still clean up its temp file (the finally block), leaking nothing.
    p = tmp_path / "f.json"

    def boom(_fd):
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(atomicio.os, "fsync", boom)
    with pytest.raises(OSError):
        atomicio.write_text(p, "data")
    assert not p.exists()                          # rename never happened -> target untouched
    assert list(tmp_path.glob("*.tmp")) == []      # temp removed despite the failure


# --------------------------------------------------------------------------- #
# gmail: ingest() saves the shared cursor incrementally (crash never re-delivers)
# --------------------------------------------------------------------------- #

@pytest.fixture
def ingest_fakes(tmp_path, monkeypatch):
    """Fake the IMAP connection + routing so ingest()'s per-message persist/cursor loop is
    unit-testable without real IMAP. Three unread messages (uids 1/2/3) all route to 'bravo'."""
    from cagent import gmail

    class FakeConn:
        def login(self, u, p):
            return ("OK", [b""])

        def select(self, box, readonly=False):
            return ("OK", [b"0"])

        def status(self, box, what):
            return ("OK", [b"INBOX (UIDVALIDITY 111)"])

        def uid(self, cmd, *args):
            if cmd == "SEARCH":
                return ("OK", [b"1 2 3"])
            if cmd == "FETCH":
                return ("OK", [(args[0].encode() + b" (RFC822", b"From: owner@example.com\r\n\r\nhi")])
            return ("OK", [b""])

        def logout(self):
            return ("OK", [b"bye"])

    monkeypatch.setattr(gmail.imaplib, "IMAP4_SSL", lambda *a, **k: FakeConn())
    cfg = SimpleNamespace(imap_host="h", imap_port=993, agent_email="agent@example.com",
                          gmail_app_password="pw", command_token="tok")
    monkeypatch.setattr(gmail.config, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(gmail.config, "enabled_personas", lambda: ["bravo"])
    monkeypatch.setattr(gmail.config, "default_persona", lambda: "bravo")
    monkeypatch.setattr(gmail, "_tag_map", lambda enabled: {})
    monkeypatch.setattr(gmail, "_sent_index_map", lambda: {})
    monkeypatch.setattr(gmail, "route_persona", lambda *a, **k: "bravo")
    monkeypatch.setattr(gmail, "_seal_inbound", lambda parsed, token, all_tokens=None: None)
    monkeypatch.setattr(gmail, "_parse_message",
                        lambda uid, msg: {"message_id": f"<m{uid}>", "from": "owner@example.com",
                                          "subject": f"s{uid}", "body_text": "b"})
    received = tmp_path / "received"
    monkeypatch.setattr(gmail, "persona_received_dir", lambda persona: received)
    cursor_file = tmp_path / "cursor.json"
    monkeypatch.setattr(gmail, "SHARED_CURSOR", cursor_file)
    monkeypatch.setattr(gmail, "_load_shared_cursor",
                        lambda: {"uidvalidity": 111, "last_uid": 0, "processed_message_ids": []})
    return SimpleNamespace(gmail=gmail, cursor_file=cursor_file, received=received)


def test_ingest_saves_cursor_incrementally(ingest_fakes):
    routed = ingest_fakes.gmail.ingest(commit=True)
    assert [r["persona"] for r in routed] == ["bravo", "bravo", "bravo"]
    cur = json.loads(ingest_fakes.cursor_file.read_text())
    assert cur["last_uid"] == 3                                    # advanced to the max persisted uid
    assert {p.name for p in ingest_fakes.received.glob("*.json")} == {"1.json", "2.json", "3.json"}


def test_ingest_cursor_never_advances_past_unpersisted_mail(ingest_fakes, monkeypatch):
    # Simulate a crash while persisting uid 2: the shared cursor must stay at uid 1 (the last fully
    # persisted message), so recovery re-fetches uid 2 rather than skipping it. This is the invariant
    # the incremental cursor-save fix protects (a whole-batch save would have left the cursor at 0).
    gmail = ingest_fakes.gmail
    real_write = gmail.atomicio.write_text

    def failing_write(path, text):
        if str(path).endswith("2.json"):
            raise RuntimeError("disk full mid-loop")
        return real_write(path, text)

    monkeypatch.setattr(gmail.atomicio, "write_text", failing_write)
    with pytest.raises(RuntimeError):
        gmail.ingest(commit=True)

    cur = json.loads(ingest_fakes.cursor_file.read_text())
    assert cur["last_uid"] == 1                              # only past the fully-persisted uid 1
    assert (ingest_fakes.received / "1.json").exists()
    assert not (ingest_fakes.received / "2.json").exists()   # crashed before uid 2 was persisted
