"""The IMAP protocol reads shared by all three pollers (poll_imap / ingest / _poll_account). Unit
tests here mean the bytes-level UIDVALIDITY/SEARCH/FETCH handling — and its empty/malformed edges —
are covered in ONE place instead of trusting three hand-synced copies."""
import ssl
import types

from cagent import gmail


class _Conn:
    """Minimal stand-in for an imaplib IMAP4_SSL connection: returns canned (typ, data) tuples in
    the exact shapes the helpers parse."""
    def __init__(self, status_line=b"INBOX (UIDVALIDITY 12)", all_uids=b"1 2 3", after=b"4 5", raw=None):
        self._status, self._all, self._after, self._raw = status_line, all_uids, after, raw

    def status(self, _box, _what):
        return ("OK", [self._status])

    def uid(self, cmd, _none, arg=None):
        if cmd == "SEARCH":
            return ("OK", [self._all if arg == "ALL" else self._after])
        if cmd == "FETCH":
            return ("OK", [(b"1 (RFC822 {5}", self._raw)]) if self._raw else ("NO", [None])


def test_uidvalidity_parses_and_defaults_to_zero():
    assert gmail._imap_uidvalidity(_Conn(status_line=b"INBOX (UIDVALIDITY 12)")) == 12
    assert gmail._imap_uidvalidity(_Conn(status_line=b"INBOX (nothing here)")) == 0
    assert gmail._imap_uidvalidity(_Conn(status_line=None)) == 0


def test_max_uid_over_all_mail():
    assert gmail._imap_max_uid(_Conn(all_uids=b"1 2 3")) == 3
    assert gmail._imap_max_uid(_Conn(all_uids=b"")) == 0            # empty inbox -> baseline at 0


def test_uids_after_filters_and_sorts():
    assert gmail._imap_uids_after(_Conn(after=b"5 4"), 3) == [4, 5]
    # a server that returns a uid <= last_uid is filtered (never re-deliver already-seen mail)
    assert gmail._imap_uids_after(_Conn(after=b"2 4 5"), 3) == [4, 5]
    assert gmail._imap_uids_after(_Conn(after=b""), 3) == []


def test_fetch_parse_returns_none_on_failed_fetch():
    assert gmail._imap_fetch_parse(_Conn(raw=None), 7) is None


def test_fetch_parse_parses_a_message():
    raw = b"From: owner@example.com\r\nSubject: Hi\r\nMessage-ID: <abc@x>\r\n\r\nbody here"
    parsed = gmail._imap_fetch_parse(_Conn(raw=raw), 7)
    assert parsed["uid"] == 7
    assert parsed["subject"] == "Hi"
    assert "owner@example.com" in parsed["from"]


def test_imap_connect_uses_verifying_tls_context(monkeypatch):
    """The Gmail app password rides this connection; a MITM would harvest it if the
    context skipped verification. Assert we pass a CERT_REQUIRED, hostname-checking
    context (imaplib's default is CERT_NONE/no-hostname)."""
    seen = {}

    def _fake_ssl(host, port, ssl_context=None):
        seen["host"], seen["port"], seen["ctx"] = host, port, ssl_context
        return object()

    monkeypatch.setattr(gmail.imaplib, "IMAP4_SSL", _fake_ssl)
    cfg = types.SimpleNamespace(imap_host="imap.gmail.com", imap_port=993)
    gmail._imap_connect(cfg)
    assert seen["host"] == "imap.gmail.com" and seen["port"] == 993
    assert isinstance(seen["ctx"], ssl.SSLContext)
    assert seen["ctx"].verify_mode == ssl.CERT_REQUIRED
    assert seen["ctx"].check_hostname is True
