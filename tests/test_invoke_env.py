"""The claude subprocess env is scrubbed of secrets. Beyond the explicit SCRUB_ENV names, any var
whose NAME looks like a credential is dropped too, so a newly-exported secret can't leak into a
tools-ON research sub-call. Non-secret vars (PATH/HOME/CAGENT_*) must survive -- the subscription CLI
needs a broad environment."""
from cagent.cognition import invoke


def test_child_env_scrubs_named_and_pattern_secrets(monkeypatch):
    for k in ("GMAIL_APP_PASSWORD", "COMMAND_TOKEN", "GITHUB_TOKEN", "AWS_SESSION_TOKEN",
              "SOME_API_KEY", "MY_CLIENT_SECRET", "DB_PASSWORD"):
        monkeypatch.setenv(k, "sensitive")
    for k in ("PATH", "HOME"):
        monkeypatch.setenv(k, "/keep")
    monkeypatch.setenv("CAGENT_PERSONA", "alpha")

    env = invoke._child_env()
    # every secret-named var is gone (explicit list + name pattern)
    for k in ("GMAIL_APP_PASSWORD", "COMMAND_TOKEN", "GITHUB_TOKEN", "AWS_SESSION_TOKEN",
              "SOME_API_KEY", "MY_CLIENT_SECRET", "DB_PASSWORD"):
        assert k not in env, f"{k} should have been scrubbed"
    # the broad environment the CLI needs survives
    assert env.get("PATH") == "/keep" and env.get("HOME") == "/keep"
    assert env.get("CAGENT_PERSONA") == "alpha"
