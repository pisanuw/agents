"""execute._unwrap_model_json: defensive normalization for the model occasionally returning a JSON
wrapper ({"body": "...\\n..."}) in a string body field, which otherwise reaches the owner as a JSON
dump with escaped newlines in the DRAFT REQUEST (observed for bravo, 2026-06-29)."""
from cagent.cognition.execute import _unwrap_model_json


def test_plain_prose_is_unchanged():
    s = "The river has recorded every flood.\n\nWhat stands is real."
    assert _unwrap_model_json(s) == s


def test_unwraps_json_body_wrapper_to_real_newlines():
    wrapped = '{"body": "Line one.\\n\\n**Heading**\\n\\nLine two."}'
    out = _unwrap_model_json(wrapped)
    assert out == "Line one.\n\n**Heading**\n\nLine two."
    assert "\\n" not in out                      # no literal backslash-n survives


def test_unwraps_with_trailing_prose_after_object():
    # the bravo shape: a json object immediately followed by more text
    out = _unwrap_model_json('{"body": "inner letter"}\n\nsincerely, the grove')
    assert out == "inner letter\n\nsincerely, the grove"


def test_accepts_alternate_text_keys():
    assert _unwrap_model_json('{"text": "hello"}') == "hello"
    assert _unwrap_model_json('{"content": "hi"}') == "hi"


def test_leaves_brace_prose_and_non_text_objects_alone():
    # a body that merely starts with a brace but is not a text wrapper must not be altered
    assert _unwrap_model_json("{this is not json} and prose") == "{this is not json} and prose"
    assert _unwrap_model_json('{"count": 3}') == '{"count": 3}'        # dict, but no text field
    assert _unwrap_model_json('{"body": "   "}') == '{"body": "   "}'  # empty inner -> untouched


def test_empty_and_non_object_inputs():
    assert _unwrap_model_json("") == ""
    assert _unwrap_model_json("[1, 2, 3]") == "[1, 2, 3]"             # leading [ is not unwrapped
