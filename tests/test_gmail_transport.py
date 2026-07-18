"""gmail transport coverage beyond the already-tested IMAP protocol helpers / own-account grouping /
content-cap exemption / recipient lock. Three groups:

  A. deterministic gate logic  -- ledger fail-closed edges, !QUIET / !THROTTLE fail-closed reads,
     signature assembly, body extraction, cursor + sent-index + tag-map helpers, seal/redact/mark;
  B. the three IMAP pollers     -- poll_imap / baseline / ingest / _poll_account driven through their
     full loop (baseline, fetch, self-loop, deferred-fetch) by an in-memory FakeIMAP;
  C. the send gate              -- DRY_RUN persistence + the disclosure / quiet / weekly / global-cap
     refusals (SMTP is never reached; conftest blocks the real socket regardless).
"""
from __future__ import annotations

import email
import json
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from types import SimpleNamespace

import pytest

from _helpers import send_cfg
from cagent import gmail


@pytest.fixture
def gmail_env(tmp_path, monkeypatch):
    """Redirect every per-persona + shared state path at tmp so ledgers/cursors/indexes/outbox stay
    in the sandbox."""
    sroot = tmp_path / "state"
    sroot.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(gmail.config, "state_root", lambda *a: sroot)
    monkeypatch.setattr(gmail.config, "shared_root", lambda: shared)
    monkeypatch.setattr(gmail, "SHARED_CURSOR", shared / "imap_cursor.json")
    monkeypatch.setattr(gmail, "SENT_INDEX", shared / "sent_index.jsonl")
    monkeypatch.setattr(gmail, "SHARED_LEDGER", shared / "send_ledger.jsonl")
    return sroot


def _raw(frm="owner@x.com", to="agent@x.com", subject="Hi", body="hello",
         mid="<m1@x>", delivered_to=None, in_reply_to=None):
    m = EmailMessage()
    m["From"] = frm
    m["To"] = to
    if delivered_to:
        m["Delivered-To"] = delivered_to
    m["Subject"] = subject
    m["Message-ID"] = mid
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    m.set_content(body)
    return m.as_bytes()


class FakeIMAP:
    """In-memory IMAP connection: a {uid: raw_bytes} inbox at a fixed UIDVALIDITY, driving the exact
    status/SEARCH/FETCH shapes the pollers parse. `fail_uids` makes a FETCH return NO (transient)."""
    def __init__(self, messages, uidvalidity=100, fail_uids=frozenset()):
        self.messages = dict(messages)
        self.uidvalidity = uidvalidity
        self.fail_uids = set(fail_uids)
        self.logged_out = False

    def login(self, u, p):
        return ("OK", [b"ok"])

    def select(self, box, readonly=False):
        return ("OK", [b"1"])

    def status(self, box, what):
        return ("OK", [f"INBOX (UIDVALIDITY {self.uidvalidity})".encode()])

    def uid(self, cmd, _none, arg=None):
        if cmd == "SEARCH":
            uids = sorted(self.messages)
            sel = uids if arg == "ALL" else [u for u in uids if u >= int(arg.split(":")[0])]
            return ("OK", [" ".join(str(u) for u in sel).encode()])
        if cmd == "FETCH":
            uid = int(_none)
            if uid in self.fail_uids or uid not in self.messages:
                return ("NO", [None])
            return ("OK", [(b"1 (RFC822 {n}", self.messages[uid])])
        return ("NO", [None])

    def logout(self):
        self.logged_out = True
        return ("BYE", [b"bye"])


def _imap_cfg(agent_email="agent@x.com", plus_tag="q", command_token="TOK"):
    return SimpleNamespace(agent_email=agent_email, gmail_app_password="pw",
                           command_token=command_token, imap_host="", imap_port=993,
                           plus_tag=plus_tag)


# ==================================================================================== #
# A. deterministic gate logic
# ==================================================================================== #

def test_count_ledger_fail_closed_on_bad_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("CAGENT_NOW", "2026-07-03T12:00:00+00:00")
    p = tmp_path / "l.jsonl"
    p.write_text("\n".join([
        json.dumps({"ts": "2026-07-03T06:00:00+00:00", "kind": "finding"}),  # 6h old -> day+week
        json.dumps({"ts": "2026-06-30T12:00:00+00:00", "kind": "finding"}),  # 3d old  -> week only
        json.dumps({"ts": "2026-06-20T12:00:00+00:00", "kind": "finding"}),  # 13d old -> neither
        "{ corrupt row",                                                     # unparseable -> fail closed
        json.dumps({"kind": "finding"}),                                    # missing ts -> fail closed
        "",                                                                 # blank -> skipped
    ]))
    assert gmail._count_ledger(p) == (3, 4)


def test_count_ledger_missing_file():
    assert gmail._count_ledger(gmail.OUTBOX / "does-not-exist.jsonl") == (0, 0)


def test_quiet_active_variants(gmail_env, monkeypatch):
    monkeypatch.setenv("CAGENT_NOW", "2026-07-03T12:00:00+00:00")
    assert gmail.quiet_active() is False                       # no file
    qf = gmail._quiet_until()
    qf.parent.mkdir(parents=True, exist_ok=True)
    qf.write_text(json.dumps({"until": "2026-07-03T18:00:00+00:00"}))
    assert gmail.quiet_active() is True                        # window still in the future
    qf.write_text(json.dumps({"until": "2026-07-03T06:00:00+00:00"}))
    assert gmail.quiet_active() is False                       # window expired
    qf.write_text("{ corrupt")
    assert gmail.quiet_active() is True                        # unreadable -> fail closed (stay muted)


def test_throttle_cap_variants(gmail_env, monkeypatch):
    monkeypatch.setenv("CAGENT_NOW", "2026-07-03T12:00:00+00:00")
    assert gmail.throttle_cap(3) == 3                          # no file
    tf = gmail._throttle()
    tf.parent.mkdir(parents=True, exist_ok=True)
    tf.write_text(json.dumps({"date": "2026-07-03", "cap": 1}))
    assert gmail.throttle_cap(3) == 1                          # lowered today
    tf.write_text(json.dumps({"date": "2026-07-03", "cap": 9}))
    assert gmail.throttle_cap(3) == 3                          # may only LOWER, never raise
    tf.write_text(json.dumps({"date": "2000-01-01", "cap": 1}))
    assert gmail.throttle_cap(3) == 3                          # stale (other day) ignored
    tf.write_text("{ corrupt")
    assert gmail.throttle_cap(3) == 1                          # unreadable -> conservative floor
    tf.write_text(json.dumps({"date": "2026-07-03"}))
    assert gmail.throttle_cap(3) == 1                          # missing cap -> floor


def test_signature_persona_override_substitutes_reply_address(gmail_env, tmp_path, monkeypatch):
    monkeypatch.setattr(gmail.config, "REPO_ROOT", tmp_path)
    d = tmp_path / "personas" / "q"
    d.mkdir(parents=True)
    (d / "signature.txt").write_text("Signed by Q ({REPLY_ADDRESS}).")
    monkeypatch.setenv("CAGENT_PERSONA", "q")
    cfg = SimpleNamespace(plus_tag="q", agent_email="agent@x.com")
    assert gmail._signature(cfg) == "Signed by Q (agent+q@x.com)."


def test_signature_default_carries_disclosure_marker(gmail_env, tmp_path, monkeypatch):
    monkeypatch.setattr(gmail.config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(gmail, "SIGNATURE", tmp_path / "nosig.txt")
    monkeypatch.delenv("CAGENT_PERSONA", raising=False)
    cfg = SimpleNamespace(plus_tag="", agent_email="agent@x.com")
    sig = gmail._signature(cfg)
    assert gmail.DISCLOSURE_MARKER in sig and "agent@x.com" in sig


def test_unescape_json_string_handles_escapes():
    assert gmail._unescape_json_string(r"a\nb\tc") == "a\nb\tc"
    assert gmail._unescape_json_string("\\u0041\\u0042") == "AB"           # \uXXXX decoded
    assert gmail._unescape_json_string("bad \\uZZZZ stays") == "bad \\uZZZZ stays"  # invalid hex left literal


def test_reply_address_and_to_header():
    tagged = SimpleNamespace(plus_tag="q", agent_email="agent@x.com", owner_name="Owner Name")
    assert gmail.reply_address(tagged) == "agent+q@x.com"
    assert gmail._to_header(tagged, "owner@x.com") == "Owner Name <owner@x.com>"
    bare = SimpleNamespace(plus_tag="", agent_email="agent@x.com", owner_name="")
    assert gmail.reply_address(bare) == "agent@x.com"
    assert gmail._to_header(bare, "o@x.com") == "o@x.com"    # no display name -> bare address


def test_plain_text_from_multipart_and_single():
    # _plain_text is always handed the EmailMessage that send() built (has get_content()).
    single = EmailMessage()
    single.set_content("single body text")
    assert "single body text" in gmail._plain_text(single)
    m = EmailMessage()
    m.set_content("plain twin")
    m.add_alternative("<p>html twin</p>", subtype="html")     # multipart -> get_content() raises KeyError
    assert "plain twin" in gmail._plain_text(m)


def test_extract_text_plain_html_and_single():
    outer = MIMEMultipart("alternative")
    outer.attach(MIMEText("the plain part", "plain"))
    outer.attach(MIMEText("<p>html</p>", "html"))
    assert "the plain part" in gmail._extract_text(outer)     # plain preferred

    html_only = MIMEMultipart("alternative")
    html_only.attach(MIMEText("<p>hi <b>there</b></p>", "html"))
    txt = gmail._extract_text(html_only)
    assert "hi" in txt and "there" in txt and "<" not in txt  # html fallback strips tags

    single = email.message_from_bytes(_raw(body="single part body"))
    assert "single part body" in gmail._extract_text(single)


def test_cursor_roundtrip_and_corrupt(gmail_env):
    assert gmail._load_cursor() == {"uidvalidity": None, "last_uid": 0, "processed_message_ids": []}
    gmail._save_cursor({"uidvalidity": 7, "last_uid": 42, "processed_message_ids": ["<a>"]})
    assert gmail._load_cursor()["last_uid"] == 42
    gmail._cursor().write_text("{ corrupt")
    assert gmail._load_cursor()["last_uid"] == 0              # torn cursor -> fresh sentinel


def test_pending_inbound_filters(gmail_env):
    assert gmail.pending_inbound() == []                      # dir absent
    rdir = gmail._received_dir()
    rdir.mkdir(parents=True)
    (rdir / "1.json").write_text(json.dumps({"subject": "new", "processed": False}))
    (rdir / "2.json").write_text(json.dumps({"subject": "old", "processed": True}))
    (rdir / "3.json").write_text("{ corrupt")
    out = gmail.pending_inbound()
    assert len(out) == 1 and out[0]["subject"] == "new" and out[0]["_path"].endswith("1.json")


def test_seal_inbound_and_redaction():
    parsed = {"subject": "!GOAL SEKRET add a goal", "body_text": "body"}
    gmail._seal_inbound(parsed, "SEKRET")
    assert parsed["cmd_token_ok"] is True and parsed["token_seen"] is True
    assert "SEKRET" not in parsed["subject"] and gmail.TOKEN_REDACTED in parsed["subject"]

    body_only = {"subject": "hello", "body_text": "contains SEKRET here"}
    gmail._seal_inbound(body_only, "SEKRET")
    assert "cmd_token_ok" not in body_only                    # token not in subject
    assert body_only["token_seen"] is True                    # ...but seen in the body
    assert "SEKRET" not in body_only["body_text"]

    untouched = {"subject": "hi", "body_text": "b"}
    gmail._seal_inbound(untouched, "")                        # no token configured -> no-op
    assert untouched == {"subject": "hi", "body_text": "b"}


def test_mark_processed_sets_flag_and_redacts(tmp_path, monkeypatch):
    monkeypatch.setattr(gmail.config, "load", lambda *a, **k: SimpleNamespace(command_token="SEKRET"))
    f = tmp_path / "m.json"
    f.write_text(json.dumps({"subject": "!STOP SEKRET", "processed": False}))
    gmail.mark_processed([{"subject": "!STOP SEKRET", "_path": str(f)}])
    saved = json.loads(f.read_text())
    assert saved["processed"] is True and "SEKRET" not in saved["subject"] and "_path" not in saved


def test_sent_index_append_and_map(gmail_env):
    gmail._append_sent_index("<mid1@x>", "q")
    gmail._append_sent_index("<mid2@x>", "data")
    gmail._append_sent_index("", "ignored")                   # empty id -> no-op
    assert gmail._sent_index_map() == {"<mid1@x>": "q", "<mid2@x>": "data"}


def test_sent_index_map_skips_corrupt(gmail_env):
    gmail.SENT_INDEX.parent.mkdir(parents=True, exist_ok=True)
    gmail.SENT_INDEX.write_text("{ bad\n" + json.dumps({"message_id": "<ok@x>", "persona": "q"}) + "\n")
    assert gmail._sent_index_map() == {"<ok@x>": "q"}


def test_tag_map_includes_tag_and_name(monkeypatch):
    cfgs = {"q": SimpleNamespace(plus_tag="quix"), "data": SimpleNamespace(plus_tag="")}
    monkeypatch.setattr(gmail.config, "load", lambda name=None: cfgs[name])
    tm = gmail._tag_map(["q", "data"])
    assert tm["quix"] == "q" and tm["q"] == "q"               # tag + bare name both route
    assert tm["data"] == "data"                               # empty plus_tag -> name is the tag


def test_persona_received_dir_and_shared_cursor(gmail_env):
    d = gmail.persona_received_dir("q")
    assert d.name == "received" and "emails" in str(d)
    assert gmail._load_shared_cursor() == {}
    gmail._save_shared_cursor({"uidvalidity": 9, "last_uid": 3})
    assert gmail._load_shared_cursor()["last_uid"] == 3
    gmail.SHARED_CURSOR.write_text("{ corrupt")
    assert gmail._load_shared_cursor() == {}                  # torn shared cursor -> re-baseline sentinel


# ==================================================================================== #
# B. IMAP pollers, driven by FakeIMAP
# ==================================================================================== #

def test_poll_imap_baselines_on_fresh_cursor(gmail_env, monkeypatch):
    monkeypatch.setattr(gmail.config, "load", lambda *a, **k: _imap_cfg())
    monkeypatch.setattr(gmail, "_imap_connect", lambda c: FakeIMAP({1: _raw(mid="<m1@x>")}))
    assert gmail.poll_imap(commit=True) == []                 # first run baselines, imports nothing
    assert gmail._load_cursor()["last_uid"] == 1


def test_poll_imap_fetches_owner_skips_self(gmail_env, monkeypatch):
    monkeypatch.setattr(gmail.config, "load", lambda *a, **k: _imap_cfg())
    gmail._save_cursor({"uidvalidity": 100, "last_uid": 0, "processed_message_ids": []})
    fake = FakeIMAP({1: _raw(frm="owner@x.com", subject="from owner", mid="<m1@x>"),
                     2: _raw(frm="agent@x.com", subject="self loop", mid="<m2@x>")})
    monkeypatch.setattr(gmail, "_imap_connect", lambda c: fake)
    out = gmail.poll_imap(commit=True)
    assert [m["subject"] for m in out] == ["from owner"]      # self-addressed mail dropped
    assert fake.logged_out
    pend = gmail.pending_inbound()
    assert len(pend) == 1 and pend[0]["subject"] == "from owner"
    assert gmail._load_cursor()["last_uid"] == 2              # cursor advanced past both


def test_poll_imap_defers_on_fetch_failure(gmail_env, monkeypatch):
    monkeypatch.setattr(gmail.config, "load", lambda *a, **k: _imap_cfg())
    gmail._save_cursor({"uidvalidity": 100, "last_uid": 0, "processed_message_ids": []})
    fake = FakeIMAP({1: _raw(mid="<m1@x>"), 2: _raw(mid="<m2@x>")}, fail_uids={1})
    monkeypatch.setattr(gmail, "_imap_connect", lambda c: fake)
    assert gmail.poll_imap(commit=True) == []                 # broke before the first message
    assert gmail._load_cursor()["last_uid"] == 0              # cursor NOT pushed past the deferred uid


def test_baseline_advances_without_import(gmail_env, monkeypatch):
    monkeypatch.setattr(gmail.config, "load", lambda *a, **k: _imap_cfg())
    monkeypatch.setattr(gmail, "_imap_connect",
                        lambda c: FakeIMAP({5: _raw(mid="<m5@x>"), 3: _raw(mid="<m3@x>")}))
    assert gmail.baseline() == 5
    assert gmail._load_cursor()["last_uid"] == 5


def test_ingest_baselines_then_routes(gmail_env, monkeypatch):
    monkeypatch.setattr(gmail.config, "load", lambda *a, **k: _imap_cfg())
    monkeypatch.setattr(gmail.config, "enabled_personas", lambda: ["q"])
    monkeypatch.setattr(gmail.config, "default_persona", lambda: "q")
    monkeypatch.setattr(gmail, "_imap_connect",
                        lambda c: FakeIMAP({1: _raw(to="agent+q@x.com", mid="<m1@x>")}))
    assert gmail.ingest(commit=True) == []                    # first run baselines
    fake2 = FakeIMAP({1: _raw(to="agent+q@x.com", mid="<m1@x>"),
                      2: _raw(frm="owner@x.com", to="agent+q@x.com", subject="routed", mid="<m2@x>")})
    monkeypatch.setattr(gmail, "_imap_connect", lambda c: fake2)
    routed = gmail.ingest(commit=True)
    assert routed and routed[0]["persona"] == "q" and routed[0]["subject"] == "routed"


def test_ingest_returns_empty_without_default_persona(gmail_env, monkeypatch):
    monkeypatch.setattr(gmail.config, "load", lambda *a, **k: _imap_cfg())
    monkeypatch.setattr(gmail.config, "enabled_personas", lambda: [])
    monkeypatch.setattr(gmail.config, "default_persona", lambda: "")
    monkeypatch.setattr(gmail, "_imap_connect", lambda c: pytest.fail("must not connect"))
    assert gmail.ingest(commit=True) == []


def test_poll_account_baselines_then_routes(gmail_env, monkeypatch):
    monkeypatch.setattr(gmail.config, "load",
                        lambda *a, **k: _imap_cfg(agent_email="bravo@x.com", plus_tag="bravo"))
    acct = _imap_cfg(agent_email="bravo@x.com", plus_tag="bravo")
    monkeypatch.setattr(gmail, "_imap_connect",
                        lambda c: FakeIMAP({1: _raw(to="bravo@x.com", mid="<c1@x>")}))
    assert gmail._poll_account(["bravo"], acct, commit=True) == []      # baseline
    fake2 = FakeIMAP({1: _raw(to="bravo@x.com", mid="<c1@x>"),
                      2: _raw(frm="owner@x.com", to="bravo@x.com", subject="hey bravo", mid="<c2@x>")})
    monkeypatch.setattr(gmail, "_imap_connect", lambda c: fake2)
    out = gmail._poll_account(["bravo"], acct, commit=True)
    assert out and out[0]["persona"] == "bravo" and out[0]["subject"] == "hey bravo"


# ==================================================================================== #
# C. send gate: DRY_RUN persistence + refusals
# ==================================================================================== #

def test_send_dry_run_persists_index_ledger_and_outbox(gmail_env, monkeypatch):
    monkeypatch.setattr(gmail, "OUTBOX", gmail_env / "outbox")
    monkeypatch.setattr(gmail, "SIGNATURE", gmail_env / "nosig.txt")     # force default (marker) signature
    cfg = send_cfg("DRY_RUN", plus_tag="q", persona="q")                 # build BEFORE patching load()
    monkeypatch.setattr(gmail.config, "load", lambda *a, **k: cfg)
    r = gmail.send(subject="Hello", body_md="the body", kind="finding",
                   in_reply_to="<prev@x>", references="<root@x>")
    assert r.dry_run is True and r.ok
    assert gmail._ledger().exists() and gmail.SHARED_LEDGER.exists() and gmail.SENT_INDEX.exists()
    assert list((gmail_env / "outbox").glob("*.json"))       # staged to outbox in DRY_RUN
    assert list(gmail._sent_dir().glob("*.json"))


def test_send_refuses_when_disclosure_missing(gmail_env, monkeypatch):
    cfg = send_cfg("DRY_RUN")
    monkeypatch.setattr(gmail.config, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(gmail, "_signature", lambda c: "no marker here")
    with pytest.raises(gmail.SendRefused, match="AI-disclosure footer missing"):
        gmail.send(subject="x", body_md="body", kind="finding")


def test_send_refuses_during_quiet_window(gmail_env, monkeypatch):
    cfg = send_cfg("SUPERVISED")
    monkeypatch.setattr(gmail.config, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(gmail, "quiet_active", lambda: True)
    with pytest.raises(gmail.SendRefused, match="quiet window active"):
        gmail.send(subject="x", body_md="body", kind="finding")


def test_send_refuses_on_weekly_cap(gmail_env, monkeypatch):
    cfg = send_cfg("SUPERVISED", emails_per_day=100, emails_per_week=2)
    monkeypatch.setattr(gmail.config, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(gmail, "quiet_active", lambda: False)
    monkeypatch.setattr(gmail, "ledger_counts", lambda: (0, 2))          # day under, week at cap
    monkeypatch.setattr(gmail, "global_ledger_counts", lambda: (0, 0))
    monkeypatch.setattr(gmail, "_stop_sending", lambda: gmail_env / "nostop")
    monkeypatch.setattr(gmail, "_send_lock", lambda: gmail_env / "lock")
    with pytest.raises(gmail.SendRefused, match="weekly send cap"):
        gmail.send(subject="x", body_md="body", kind="finding")


def test_send_refuses_on_global_daily_cap(gmail_env, monkeypatch):
    cfg = send_cfg("SUPERVISED", emails_per_day=100, emails_per_week=100,
                   plus_tag="q", persona="q", global_emails_per_day=1)
    monkeypatch.setattr(gmail.config, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(gmail, "quiet_active", lambda: False)
    monkeypatch.setattr(gmail, "ledger_counts", lambda: (0, 0))
    monkeypatch.setattr(gmail, "global_ledger_counts", lambda: (1, 0))   # global daily at cap
    monkeypatch.setattr(gmail, "_stop_sending", lambda: gmail_env / "nostop")
    monkeypatch.setattr(gmail, "_send_lock", lambda: gmail_env / "lock")
    monkeypatch.setattr(gmail, "_global_send_lock", lambda: gmail_env / "glock")
    with pytest.raises(gmail.SendRefused, match="global daily cap"):
        gmail.send(subject="x", body_md="body", kind="finding")


def test_send_refuses_on_global_weekly_cap(gmail_env, monkeypatch):
    cfg = send_cfg("SUPERVISED", emails_per_day=100, emails_per_week=100, plus_tag="q", persona="q",
                   global_emails_per_day=100, global_emails_per_week=1)
    monkeypatch.setattr(gmail.config, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(gmail, "quiet_active", lambda: False)
    monkeypatch.setattr(gmail, "ledger_counts", lambda: (0, 0))
    monkeypatch.setattr(gmail, "global_ledger_counts", lambda: (0, 1))    # global weekly at cap
    monkeypatch.setattr(gmail, "_stop_sending", lambda: gmail_env / "nostop")
    monkeypatch.setattr(gmail, "_send_lock", lambda: gmail_env / "lock")
    monkeypatch.setattr(gmail, "_global_send_lock", lambda: gmail_env / "glock")
    with pytest.raises(gmail.SendRefused, match="global weekly cap"):
        gmail.send(subject="x", body_md="body", kind="finding")


def test_seal_inbound_redacts_all_fields_and_all_tokens():
    """P0-1/P0-2: redaction scrubs EVERY persisted field (subject, body AND headers) and EVERY
    persona's token -- even a token that belongs to a different persona than the one this message
    routed to. The auth witness is still set from the routed persona's own token only."""
    import json
    from cagent import gmail
    ta, tb = "tokenAAA111", "tokenBBB222"
    rec = {"subject": f"!PAUSE {tb}",            # routed persona (bob) token in subject -> authenticates
           "body_text": "hello",
           "to": "agent+bob@x",
           "in_reply_to": f"<ref-{ta}@x>",       # ANOTHER persona's token, riding in a header
           "references": ta,
           "from": "owner@x"}
    gmail._seal_inbound(rec, tb, all_tokens={ta, tb})
    blob = json.dumps(rec)
    assert ta not in blob and tb not in blob                 # no live token survives in ANY field
    assert gmail.TOKEN_REDACTED in rec["subject"]            # routed token scrubbed
    assert gmail.TOKEN_REDACTED in rec["in_reply_to"]        # header field scrubbed (P0-2)
    assert gmail.TOKEN_REDACTED in rec["references"]         # cross-persona token scrubbed (P0-1)
    assert rec.get("cmd_token_ok") is True                   # authenticated on the routed token


def test_seal_inbound_empty_routed_token_still_redacts():
    """P0-1: a persona with NO token of its own must never persist another persona's live token."""
    import json
    from cagent import gmail
    t = "otherpersontoken99"
    rec = {"subject": f"!GOAL do a thing {t}", "body_text": "b", "from": "owner@x"}
    gmail._seal_inbound(rec, "", all_tokens={t})             # routed token empty; global set has t
    assert t not in json.dumps(rec)
    assert gmail.TOKEN_REDACTED in rec["subject"]
    assert "cmd_token_ok" not in rec                         # empty routed token -> not authenticated
