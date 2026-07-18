import json

from cagent.cognition.invoke import RawEnvelope
from cagent.cognition.parse import parse


def env(stdout="", stderr="", code=0, timed_out=False):
    return RawEnvelope(stdout=stdout, stderr=stderr, code=code, timed_out=timed_out)


def test_ok_structured():
    e = env(stdout=json.dumps({"is_error": False, "result": "hi",
                               "structured_output": {"reply": "PONG"},
                               "total_cost_usd": 0.01, "num_turns": 2}))
    r = parse(e)
    assert r.status == "OK" and r.ok and r.structured["reply"] == "PONG" and r.num_turns == 2


def test_no_structured_output():
    r = parse(env(stdout=json.dumps({"is_error": False, "result": "PONG"})))
    assert r.status == "NO_STRUCTURED_OUTPUT" and r.text == "PONG"


def test_rate_limit_by_status():
    r = parse(env(stdout=json.dumps({"is_error": True, "api_error_status": 429, "result": "slow down"})))
    assert r.status == "RATE_LIMIT" and r.rate_limited


def test_rate_limit_by_text():
    r = parse(env(stdout=json.dumps({"is_error": True, "result": "You have reached your usage limit"})))
    assert r.status == "RATE_LIMIT"


def test_auth_error_in_result():
    r = parse(env(stdout=json.dumps({"is_error": True, "result": "Not logged in - Please run /login"})))
    assert r.status == "AUTH_ERROR" and r.rate_limited


def test_auth_error_empty_stdout():
    assert parse(env(stdout="", stderr="Not logged in")).status == "AUTH_ERROR"


def test_flag_error():
    assert parse(env(stdout="", stderr="error: unknown option '--nope'")).status == "FLAG_ERROR"


def test_bad_json():
    assert parse(env(stdout="this is not json")).status == "BAD_JSON"


def test_empty_output():
    assert parse(env(stdout="", stderr="")).status == "EMPTY_OUTPUT"


def test_timeout():
    assert parse(env(timed_out=True, code=124, stderr="[timeout]")).status == "TIMEOUT"


def test_api_error_generic():
    r = parse(env(stdout=json.dumps({"is_error": True, "api_error_status": 500, "result": "server error"})))
    assert r.status == "API_ERROR"
