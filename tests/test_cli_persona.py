"""Phase 7: persona-aware CLI readers. `--persona <name>` sets CAGENT_PERSONA so the reader
commands resolve config.state_root() to that persona's namespace; with no flag the legacy
flat state/ is read unchanged."""
import json

import pytest

from cagent import cli, config


@pytest.fixture
def clean_persona():
    # _persona_flag sets os.environ["CAGENT_PERSONA"] DIRECTLY (correct for the real CLI: the tick
    # subprocess inherits it). monkeypatch.delenv cannot undo that cleanly -- its LIFO restore
    # re-applies the value -- so snapshot and pop by hand, guaranteeing no persona leaks into the
    # next test (which would mis-resolve state_root() and break e.g. test_mode_override).
    import os
    before = os.environ.pop("CAGENT_PERSONA", None)
    yield
    os.environ.pop("CAGENT_PERSONA", None)
    if before is not None:
        os.environ["CAGENT_PERSONA"] = before


def test_flag_sets_env_and_strips(clean_persona):
    rest = cli._persona_flag(["12", "--persona", "scout"])
    assert rest == ["12"]
    assert config.state_root() == config.REPO_ROOT / "state" / "personas" / "scout"


def test_flag_equals_form(clean_persona):
    assert cli._persona_flag(["--persona=pharos", "5"]) == ["5"]
    assert config.state_root() == config.REPO_ROOT / "state" / "personas" / "pharos"


def test_no_flag_is_legacy(clean_persona):
    assert cli._persona_flag(["7"]) == ["7"]
    assert config.state_root() == config.REPO_ROOT / "state"


def test_invalid_persona_exits(clean_persona):
    with pytest.raises(SystemExit):
        cli._persona_flag(["--persona", "../evil"])


def test_unknown_persona_lists_and_exits(clean_persona, capsys):
    # regex-valid but no such persona on disk: list the real ones and exit, do NOT pretend it exists
    with pytest.raises(SystemExit) as e:
        cli._persona_flag(["--persona", "zzz"])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "unknown persona 'zzz'" in err
    assert "scout" in err                          # the listing names the actual personas
    import os
    assert os.environ.get("CAGENT_PERSONA") != "zzz"   # never set for a rejected name


def test_unknown_persona_allowed_when_no_personas_dir(clean_persona, tmp_path, monkeypatch):
    # legacy single-persona repo (no personas/ dir): nothing to validate against, so don't block
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    assert cli._persona_flag(["--persona", "solo"]) == []
    assert config.state_root() == tmp_path / "state" / "personas" / "solo"


def test_migrate_seeds_shared_cursor(clean_persona, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "var").mkdir()
    (tmp_path / "var" / "STOP").touch()   # M17: must be stopped before migration
    (state / "goals.json").write_text("[]")
    (state / "imap_cursor.json").write_text('{"uidvalidity": 7, "last_uid": 42, "processed_message_ids": []}')
    assert cli.cmd_migrate_persona(["alpha", "--yes"]) == 0
    # brain moved under the persona, cursor seeded into shared/ (lossless resume)
    assert (state / "personas" / "alpha" / "goals.json").exists()
    assert (state / "personas" / "alpha" / "imap_cursor.json").exists()
    seeded = state / "shared" / "imap_cursor.json"
    assert seeded.exists()
    assert '"last_uid": 42' in seeded.read_text()


def test_migrate_baseline_skips_cursor_seed(clean_persona, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "var").mkdir()
    (tmp_path / "var" / "STOP").touch()   # M17: must be stopped before migration
    (state / "imap_cursor.json").write_text('{"uidvalidity": 1, "last_uid": 5, "processed_message_ids": []}')
    assert cli.cmd_migrate_persona(["alpha", "--yes", "--baseline"]) == 0
    assert not (state / "shared" / "imap_cursor.json").exists()


def _status_root(tmp_path, monkeypatch):
    """Minimal repo root so cmd_status runs: a var/ dir and a stale last_tick.json."""
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    var = tmp_path / "var"
    var.mkdir()
    last = var / "last_tick.json"
    last.write_text('{"ts": "2026-06-26T01:10:05+00:00", "ok": false, "summary": "TIMEOUT"}')
    import os
    os.utime(last, (1_000_000, 1_000_000))  # ancient mtime -> any tick commit is "newer"
    return last


def test_status_warns_on_mirror(clean_persona, tmp_path, monkeypatch, capsys):
    _status_root(tmp_path, monkeypatch)
    # git reports a tick commit far newer than the local file -> mirror.
    monkeypatch.setattr(cli, "_latest_tick_commit",
                        lambda root: (9_000_000_000.0, "cagent tick: data @ 2026-06-27 20:10:28 -0700"))
    assert cli.cmd_status([]) == 0
    out = capsys.readouterr().out
    assert "MIRROR?" in out
    assert "git log --oneline" in out


def test_status_quiet_on_host(clean_persona, tmp_path, monkeypatch, capsys):
    last = _status_root(tmp_path, monkeypatch)
    import os
    os.utime(last, (9_000_000_000.0, 9_000_000_000.0))  # file as fresh as the commit -> host
    monkeypatch.setattr(cli, "_latest_tick_commit",
                        lambda root: (9_000_000_000.0, "cagent tick: data @ now"))
    assert cli.cmd_status([]) == 0
    assert "MIRROR?" not in capsys.readouterr().out


def test_status_quiet_when_no_tick_commits(clean_persona, tmp_path, monkeypatch, capsys):
    _status_root(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_latest_tick_commit", lambda root: None)  # no git / no commits
    assert cli.cmd_status([]) == 0
    assert "MIRROR?" not in capsys.readouterr().out


def test_status_reports_persona_pause(clean_persona, tmp_path, monkeypatch, capsys):
    # Regression: `stop --persona data` writes var/persona/data.STOP, but status only checked the
    # global var/STOP, so a paused persona read as "off" -- identical to a running one.
    from cagent import control
    _status_root(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_latest_tick_commit", lambda root: None)
    stop_dir = tmp_path / "var" / "persona"
    monkeypatch.setattr(control, "PERSONA_STOP_DIR", stop_dir)   # frozen at import; redirect for the test
    stop_dir.mkdir(parents=True)
    (stop_dir / "data.STOP").write_text("paused\n")

    import os
    os.environ["CAGENT_PERSONA"] = "data"                       # paused persona -> ON
    assert cli.cmd_status([]) == 0
    out = capsys.readouterr().out
    assert "kill switch: ON" in out and "var/persona/data.STOP" in out

    os.environ["CAGENT_PERSONA"] = "bravo"                      # no flag file -> off
    assert cli.cmd_status([]) == 0
    assert "kill switch: off" in capsys.readouterr().out


def test_status_last_tick_is_persona_journal(clean_persona, tmp_path, monkeypatch, capsys):
    # status --persona X shows X's OWN latest journal tick, not the global var/last_tick.json (which
    # is whatever persona ran last, so it read identically for every persona).
    _status_root(tmp_path, monkeypatch)                         # global var/last_tick.json -> "TIMEOUT"
    monkeypatch.setattr(cli, "_latest_tick_commit", lambda root: None)
    jdir = tmp_path / "state" / "personas" / "data"
    jdir.mkdir(parents=True)
    (jdir / "journal.jsonl").write_text(
        json.dumps({"ts": "2026-06-30T00:00:00+00:00", "kind": "tick", "ok": True, "summary": "old tick"}) + "\n"
        + json.dumps({"ts": "2026-07-01T00:00:00+00:00", "kind": "tick", "ok": False, "status": "GATE_BLOCK"}) + "\n")

    import os
    os.environ["CAGENT_PERSONA"] = "data"
    assert cli.cmd_status([]) == 0
    out = capsys.readouterr().out
    assert "GATE_BLOCK" in out and "FAIL" in out                # this persona's newest tick
    assert "old tick" not in out                                # newest wins, not an earlier entry
    assert "TIMEOUT" not in out                                 # NOT the global var/last_tick.json


def test_run_tick_honors_persona(clean_persona, monkeypatch):
    # Regression: cmd_run_tick ignored --persona (no _persona_flag), so `run-tick --persona X` ran
    # the legacy/global tick instead of X's.
    import os
    from cagent import tick
    monkeypatch.setattr(cli, "_mirror_note", lambda root: None)   # simulate the live host (not a mirror)
    captured = {}
    monkeypatch.setattr(tick, "main", lambda: captured.update(persona=os.environ.get("CAGENT_PERSONA")) or 0)
    assert cli.cmd_run_tick(["--persona", "scout"]) == 0
    assert captured["persona"] == "scout"


def test_readiness_tabulates_all_personas(clean_persona, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    (tmp_path / "personas" / "data").mkdir(parents=True)            # a known persona on disk
    ns = tmp_path / "state" / "personas" / "data"
    ns.mkdir(parents=True)
    (ns / "journal.jsonl").write_text(
        json.dumps({"kind": "tick", "ok": True, "ts": "2026-06-26T10:00:00+00:00",
                    "results": [{"type": "send_email", "blocked_by_gate": {"verdict": "revise"}}]}) + "\n"
        + json.dumps({"kind": "tick", "ok": False, "ts": "2026-06-26T11:00:00+00:00", "status": "TIMEOUT"}) + "\n")
    assert cli.cmd_readiness([]) == 0
    row = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("data"))
    f = row.split()
    # persona mode paused days ticks ok fail ok% gateblk refused pend ...
    assert f[2] == "-"                                             # not paused
    assert f[4] == "2" and f[5] == "1" and f[6] == "1"             # ticks / ok / fail
    assert f[8] == "1"                                              # one gate-blocked draft


def test_readiness_rejects_unknown_persona(clean_persona, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    (tmp_path / "personas" / "data").mkdir(parents=True)
    assert cli.cmd_readiness(["--persona", "nope"]) == 2
    assert "unknown persona 'nope'" in capsys.readouterr().err


def test_pending_reads_persona_namespace(clean_persona, tmp_path, monkeypatch, capsys):
    # `pending --persona X` must read state/personas/X/emails/pending (call-time resolver). And P1-5:
    # bare `pending` (no --persona) MERGES every enabled persona's drafts, tagged -- it must NOT read
    # the empty flat state/ and hide a real draft.
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config, "enabled_personas", lambda: ["data", "golf"])
    monkeypatch.setattr(config, "default_persona", lambda: "data")
    (tmp_path / "personas" / "data").mkdir(parents=True)
    pend = tmp_path / "state" / "personas" / "data" / "emails" / "pending"
    pend.mkdir(parents=True)
    (pend / "abc123.json").write_text(json.dumps(
        {"token": "abc123", "created": "2026-06-28T10:00:00", "subject": "A staged finding"}))
    assert cli.cmd_pending(["--persona", "data"]) == 0
    assert "abc123" in capsys.readouterr().out
    # bare pending merges across personas (tagged): the data draft still surfaces (P1-5 fix).
    import os
    os.environ.pop("CAGENT_PERSONA", None)
    assert cli.cmd_pending([]) == 0
    out = capsys.readouterr().out
    assert "abc123" in out and "data" in out


def test_config_honors_persona_flag(clean_persona, tmp_path, monkeypatch, capsys):
    # P1-4: `config --persona X` must resolve X's namespace/identity, not the flat/global default.
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    (tmp_path / "personas" / "data").mkdir(parents=True)
    assert cli.cmd_config(["--persona", "data"]) == 0
    assert '"persona": "data"' in capsys.readouterr().out


def test_scorecard_reads_persona_namespace(clean_persona, tmp_path, monkeypatch, capsys):
    # regression: `scorecard --persona X` must read state/personas/X/, not the empty flat state/.
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    (tmp_path / "personas" / "data").mkdir(parents=True)
    ns = tmp_path / "state" / "personas" / "data"
    ns.mkdir(parents=True)
    (ns / "journal.jsonl").write_text(
        "\n".join(json.dumps({"kind": "tick", "ok": True, "ts": f"2026-06-2{i}T10:00:00+00:00"})
                  for i in (1, 2, 3)) + "\n")
    assert cli.cmd_scorecard(["--persona", "data"]) == 0
    out = capsys.readouterr().out
    assert "Persona: data" in out
    assert "Ticks: 3 (ok: 3)" in out                        # X's ticks, not zeros from flat state/
    assert (ns / "soft_launch_report.md").exists()          # report written into X's namespace


def test_recent_reads_persona_namespace(clean_persona, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    ns = tmp_path / "state" / "personas" / "data"
    ns.mkdir(parents=True)
    (ns / "goals.json").write_text(json.dumps([
        {"id": "G1", "title": "study positronic ethics", "status": "active", "priority": 1}]))
    (ns / "journal.jsonl").write_text(
        json.dumps({"kind": "tick", "ok": True, "ts": "2026-06-26T10:00:00", "summary": "did a thing",
                    "actions": ["research"]}) + "\n")
    assert cli.cmd_recent(["--persona", "data"]) == 0
    out = capsys.readouterr().out
    assert "persona data" in out
    assert "study positronic ethics" in out
