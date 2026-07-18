"""`cagentctl sent`: merged outbound-mail reader across personas, including draft-approval
requests (kind=approval) and the drafts still awaiting APPROVE/REJECT."""
import json

import pytest

from cagent import cli, config


@pytest.fixture
def clean_persona(monkeypatch):
    monkeypatch.delenv("CAGENT_PERSONA", raising=False)
    yield
    monkeypatch.delenv("CAGENT_PERSONA", raising=False)


def _ledger(root, name, rows):
    d = root / "state" / "personas" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "send_ledger.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _pending(root, name, token, subject, held=False):
    d = root / "state" / "personas" / name / "emails" / "pending"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{token}.json").write_text(json.dumps(
        {"token": token, "subject": subject, "body": "b", "kind": "finding", "held": held}))


def _received(root, name, uid, subject, processed=False, received_at="2026-06-29T12:00:00+00:00"):
    d = root / "state" / "personas" / name / "emails" / "received"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{uid}.json").write_text(json.dumps(
        {"uid": uid, "subject": subject, "from": "owner@example.com",
         "processed": processed, "received_at": received_at}))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config, "known_personas", lambda: ["alpha", "beta"])
    _ledger(tmp_path, "alpha", [
        {"ts": "2026-06-27T10:00:00+00:00", "kind": "finding", "mode": "SUPERVISED",
         "dry_run": False, "subject": "alpha real dispatch"},
        {"ts": "2026-06-28T09:00:00+00:00", "kind": "approval", "mode": "SUPERVISED",
         "dry_run": False, "subject": "[alpha] DRAFT REQUEST abc: hello"}])
    _ledger(tmp_path, "beta", [
        {"ts": "2026-06-26T08:00:00+00:00", "kind": "digest", "mode": "DRY_RUN",
         "dry_run": True, "subject": "beta staged digest"}])
    _pending(tmp_path, "alpha", "abc12345", "alpha awaiting draft")
    return tmp_path


def test_sent_merges_all_personas_newest_first(clean_persona, repo, capsys):
    assert cli.cmd_sent([]) == 0
    out = capsys.readouterr().out
    # all three sends present, tagged by persona, approval request visible as a kind
    assert "alpha" in out and "beta" in out
    assert "approval" in out and "alpha real dispatch" in out and "beta staged digest" in out
    assert "staged" in out                                   # the dry-run row is marked staged
    # newest (alpha approval, 06-28) sorts above the older beta digest (06-26)
    assert out.index("DRAFT REQUEST abc") < out.index("beta staged digest")
    # pending section lists the awaiting draft
    assert "DRAFTS AWAITING YOUR APPROVAL (1)" in out and "abc12345" in out


def test_sent_single_persona_via_flag(clean_persona, repo, capsys):
    assert cli.cmd_sent(["--persona", "alpha"]) == 0
    out = capsys.readouterr().out
    assert "persona alpha" in out and "alpha real dispatch" in out
    assert "beta staged digest" not in out                   # narrowed to alpha only


def test_sent_count_limit(clean_persona, repo, capsys):
    assert cli.cmd_sent(["1"]) == 0
    out = capsys.readouterr().out
    assert "1 of 3" in out                                   # 3 total across personas, showing 1


def test_sent_all_shows_everything(clean_persona, repo, capsys):
    assert cli.cmd_sent(["all"]) == 0
    assert "3 of 3" in capsys.readouterr().out


def test_sent_flags_inbox_reply_awaiting_processing(clean_persona, repo, capsys):
    # A REJECT for the awaiting draft is in the inbox but not yet processed (it only applies on the
    # persona's own tick): the REPLY column surfaces it so the draft doesn't read as un-acted-upon.
    _received(repo, "alpha", 42, "REJECT abc12345")
    assert cli.cmd_sent(["--persona", "alpha"]) == 0
    out = capsys.readouterr().out
    assert "REPLY column" in out
    assert "REJECT" in out and "(pending tick)" in out
    assert "abc12345" in out


def test_sent_no_reply_column_without_inbox_replies(clean_persona, repo, capsys):
    # No matching inbox reply -> no REPLY legend, draft still listed plainly.
    assert cli.cmd_sent(["--persona", "alpha"]) == 0
    out = capsys.readouterr().out
    assert "REPLY column" not in out and "(pending tick)" not in out
    assert "abc12345" in out


def test_sent_ignores_reply_for_other_token(clean_persona, repo, capsys):
    # A reply whose token matches no awaiting draft must not light up the column.
    _received(repo, "alpha", 43, "APPROVE deadbeef")
    assert cli.cmd_sent(["--persona", "alpha"]) == 0
    out = capsys.readouterr().out
    assert "REPLY column" not in out


def test_sent_empty_repo(clean_persona, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config, "known_personas", lambda: [])
    assert cli.cmd_sent([]) == 0
    out = capsys.readouterr().out
    assert "nothing sent yet" in out and "DRAFTS AWAITING YOUR APPROVAL (0)" in out
