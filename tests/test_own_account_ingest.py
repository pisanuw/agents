"""Own-account ingest (Bravo et al.): each enabled persona whose agent_email differs from the
global account gets its dedicated mailbox polled. Personas sharing ONE account (same agent_email)
are grouped so the account is polled once and inbound is routed among them by +tag — letting
bravo@ host bravo (bravo+bravo@) alongside future bravo+other@ personas. ingest() covers the
shared mailbox. The live IMAP poll (_poll_account) is exercised in integration, not unit-tested."""
import types

from cagent import gmail


def _cfg(email, plus_tag=""):
    return types.SimpleNamespace(agent_email=email, plus_tag=plus_tag)


def test_groups_personas_sharing_one_account(monkeypatch):
    cfgs = {
        "": _cfg("agent@example.com"),
        "alpha": _cfg("agent@example.com", "alpha"),
        "bravo": _cfg("bravo@example.com", "bravo"),
        "fir": _cfg("bravo@example.com", "fir"),
    }
    monkeypatch.setattr(gmail.config, "load", lambda persona=None: cfgs[persona or ""])
    monkeypatch.setattr(gmail.config, "enabled_personas", lambda: ["alpha", "bravo", "fir"])
    groups = gmail._own_accounts()
    assert set(groups) == {"bravo@example.com"}            # alpha (global account) excluded
    _c, personas = groups["bravo@example.com"]
    assert personas == ["bravo", "fir"]                 # both personas grouped under the one account


def test_polls_each_account_once(monkeypatch):
    cfgs = {
        "": _cfg("agent@example.com"),
        "bravo": _cfg("bravo@example.com", "bravo"),
        "fir": _cfg("bravo@example.com", "fir"),
    }
    monkeypatch.setattr(gmail.config, "load", lambda persona=None: cfgs[persona or ""])
    monkeypatch.setattr(gmail.config, "enabled_personas", lambda: ["bravo", "fir"])
    calls = []
    monkeypatch.setattr(gmail, "_poll_account",
                        lambda personas, cfg, commit: (calls.append(tuple(personas)), [])[1])
    gmail.ingest_own_accounts(commit=False)
    assert calls == [("bravo", "fir")]                  # ONE poll for the shared account, not per-persona


def test_account_primary_prefers_native_then_first(monkeypatch):
    cfgs = {"bravo": _cfg("bravo@example.com", "bravo"), "fir": _cfg("bravo@example.com", "fir")}
    monkeypatch.setattr(gmail.config, "load", lambda persona=None: cfgs[persona])
    # native tag wins regardless of list order -> bravo's cursor home is stable as personas are added
    assert gmail._account_primary("bravo", ["fir", "bravo"]) == "bravo"
    assert gmail._account_primary("bravo", ["fir"]) == "fir"            # fallback: first listed


def test_none_when_all_shared(monkeypatch):
    monkeypatch.setattr(gmail.config, "load", lambda persona=None: _cfg("agent@example.com"))
    monkeypatch.setattr(gmail.config, "enabled_personas", lambda: ["alpha", "data"])
    monkeypatch.setattr(gmail, "_poll_account",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not poll")))
    assert gmail.ingest_own_accounts(commit=False) == []


def test_one_bad_account_does_not_block_others(monkeypatch):
    cfgs = {
        "": _cfg("agent@example.com"),
        "bravo": _cfg("bravo@example.com", "bravo"),
        "birch": _cfg("birch@example.com", "birch"),
    }
    monkeypatch.setattr(gmail.config, "load", lambda persona=None: cfgs[persona or ""])
    monkeypatch.setattr(gmail.config, "enabled_personas", lambda: ["bravo", "birch"])

    def poll(personas, cfg, commit):
        if "bravo" in personas:
            raise RuntimeError("login failed")
        return [{"persona": personas[0]}]

    monkeypatch.setattr(gmail, "_poll_account", poll)
    assert gmail.ingest_own_accounts(commit=False) == [{"persona": "birch"}]
