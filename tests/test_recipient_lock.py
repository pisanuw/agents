"""The send gate is hard-locked to the owner. gmail.send takes NO recipient argument -- it always
sends to cfg.recipient (owner in LIVE, staging otherwise), and refuses if that is not on the owner
allowlist. This is what makes a prompt injection unable to redirect mail; it had no regression guard."""
import inspect
from types import SimpleNamespace

import pytest

from _helpers import send_cfg
from cagent import gmail


class _FakeSMTP:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def login(self, *a): pass
    def send_message(self, *a): pass


def _stub_live_gate(monkeypatch, cfg, tmp_path):
    """Wire gmail.send's gate for a real (non-DRY_RUN) send with no I/O -- caps open, SMTP faked."""
    monkeypatch.setattr(gmail.config, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(gmail, "quiet_active", lambda: False)
    monkeypatch.setattr(gmail, "ledger_counts", lambda: (0, 0))
    monkeypatch.setattr(gmail, "global_ledger_counts", lambda: (0, 0))
    monkeypatch.setattr(gmail, "_stop_sending", lambda: tmp_path / "nostop")
    monkeypatch.setattr(gmail, "_send_lock", lambda: tmp_path / "lock")
    monkeypatch.setattr(gmail, "_record", lambda rec: None)
    monkeypatch.setattr(gmail, "_record_global", lambda rec: None)
    monkeypatch.setattr(gmail, "_persist", lambda msg, rec, to_outbox: None)
    monkeypatch.setattr(gmail, "_append_sent_index", lambda mid, persona: None)
    monkeypatch.setattr(gmail.smtplib, "SMTP_SSL", lambda *a, **k: _FakeSMTP())


def test_send_has_no_recipient_parameter():
    # The model's decision can propose a subject/body but literally cannot supply a recipient:
    # the only way mail leaves is to cfg.recipient. This is the structural half of the guarantee.
    params = set(inspect.signature(gmail.send).parameters)
    assert "recipient" not in params and "to" not in params


def test_placeholder_identity_refused_in_non_dry_run(monkeypatch):
    # Fail closed: a missing ~/.config/cagent/.env leaves owner/agent at the example.com defaults.
    # A SUPERVISED/LIVE send with those must be refused rather than email a bogus address.
    cfg = SimpleNamespace(MODE="SUPERVISED", recipient="owner+cagent-staging@example.com",
                          owner_email="owner@example.com", agent_email="agent@example.com",
                          staging_recipient="owner+cagent-staging@example.com")
    monkeypatch.setattr(gmail.config, "load", lambda *a, **k: cfg)
    with pytest.raises(gmail.SendRefused, match="placeholder identity"):
        gmail.send(subject="x", body_md="y")


def test_recipient_off_the_allowlist_is_refused(monkeypatch):
    # The defensive branch: if cfg.recipient were ever something other than owner/staging (a future
    # misconfig), the send is refused rather than delivered off-allowlist.
    cfg = SimpleNamespace(recipient="attacker@evil.test",
                          owner_email="owner@example.com",
                          staging_recipient="owner+cagent-staging@example.com")
    monkeypatch.setattr(gmail.config, "load", lambda *a, **k: cfg)
    with pytest.raises(gmail.SendRefused, match="not in owner allowlist"):
        gmail.send(subject="x", body_md="y")


def test_live_send_goes_to_the_owner(monkeypatch, tmp_path):
    cfg = send_cfg("LIVE")
    _stub_live_gate(monkeypatch, cfg, tmp_path)
    r = gmail.send(subject="A finding", body_md="body", kind="alert")   # exempt kind: caps irrelevant
    assert r.ok and not r.dry_run
    assert r.to == cfg.owner_email                                       # LIVE -> owner, never elsewhere
    assert r.to.lower() in {cfg.owner_email.lower(), cfg.staging_recipient.lower()}


def test_non_live_send_goes_to_staging_not_owner(monkeypatch, tmp_path):
    # In SUPERVISED/DRY_RUN the locked recipient is the +staging tag, never the real owner inbox.
    cfg = send_cfg("SUPERVISED")
    _stub_live_gate(monkeypatch, cfg, tmp_path)
    r = gmail.send(subject="A finding", body_md="body", kind="alert")
    assert r.to == cfg.staging_recipient
