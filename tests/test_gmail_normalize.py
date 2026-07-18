"""gmail.normalize_model_body: the model sometimes returns a JSON wrapper ({"body": "...\\n..."})
where raw prose was asked for. Normalization now lives at the single send chokepoint (gmail.send) so
EVERY outbound path is defanged -- including the SUPERVISED release paths (supervise.approve /
retry_approved) that re-read the raw staged body and used to mail the owner a JSON dump (observed
across personas, 2026-06-29..07-03), not just the one drafting call in execute._do_email."""
from cagent import gmail


def test_unwraps_leading_body_wrapper():
    assert gmail.normalize_model_body('{"body": "Line one.\\n\\nLine two."}') == "Line one.\n\nLine two."


def test_alternate_text_keys_and_trailing_prose():
    assert gmail.normalize_model_body('{"text": "hello"}') == "hello"
    assert gmail.normalize_model_body('{"content": "hi"}') == "hi"
    # a well-formed object immediately followed by more prose (the strict-parse path keeps the tail)
    assert gmail.normalize_model_body('{"body": "letter"}\n\nsincerely') == "letter\n\nsincerely"


def test_recovers_wrapper_with_unescaped_inner_quotes():
    """The bravo 2026-07-03 shape: a {"body": "..."} wrapper whose prose contains an UNESCAPED double
    quote, so strict json.loads fails. The lenient fallback must still recover the prose (with real
    newlines) and leave already-decoded unicode (em dash) intact."""
    raw = '{"body": "The grove traced failures framed as "significant" risk.\\nThe chain — documented."}'
    out = gmail.normalize_model_body(raw)
    assert out == 'The grove traced failures framed as "significant" risk.\nThe chain — documented.'
    assert "\\n" not in out and not out.startswith("{")


def test_plain_prose_and_non_wrappers_untouched():
    prose = "The river has recorded every flood.\n\nWhat stands is real."
    assert gmail.normalize_model_body(prose) == prose
    # system/prose bodies that merely start with a brace, or carry no text key, are never altered
    assert gmail.normalize_model_body("{not json} and prose") == "{not json} and prose"
    assert gmail.normalize_model_body('{"count": 3}') == '{"count": 3}'
    assert gmail.normalize_model_body('{"body": "   "}') == '{"body": "   "}'  # empty inner
    assert gmail.normalize_model_body("Daily dispatch, 2026-07-03.") == "Daily dispatch, 2026-07-03."


def test_send_gate_normalizes_the_body(monkeypatch):
    """The load-bearing regression: a raw JSON body handed straight to gmail.send (as the SUPERVISED
    release paths do with the staged draft) must reach the wire as clean prose, not a JSON dump."""
    monkeypatch.setattr(gmail, "_signature", lambda cfg: "written autonomously by an AI research agent (x@y)")
    monkeypatch.setattr(gmail, "quiet_active", lambda: False)
    cap = {}
    monkeypatch.setattr(gmail, "_persist", lambda msg, rec, to_outbox: cap.update(msg=msg))
    monkeypatch.setattr(gmail, "_record", lambda rec: None)
    monkeypatch.setattr(gmail, "_record_global", lambda rec: None)
    monkeypatch.setattr(gmail, "_append_sent_index", lambda mid, persona: None)
    r = gmail.send(subject="Finding", body_md='{"body": "First.\\n\\nSecond."}', kind="finding")
    assert r.dry_run                                             # config.toml default mode is DRY_RUN
    plain = next(p.get_content() for p in cap["msg"].walk() if p.get_content_type() == "text/plain")
    assert "First.\n\nSecond." in plain
    assert '{"body"' not in plain and "\\n" not in plain         # no JSON dump, no literal backslash-n
