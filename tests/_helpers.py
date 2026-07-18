"""Small shared test helpers. Kept out of conftest.py (which is for fixtures) so tests import them
explicitly. Consolidated here to stop the same loader drifting between test files."""
import dataclasses
import importlib.util

from cagent import config


def send_cfg(mode, **overrides):
    """A config for a non-DRY_RUN send test. Uses config.load()'s shape but pins EXPLICIT test
    identities so the test neither depends on ~/.config/cagent/.env being present nor trips gmail's
    placeholder-identity guard (which refuses the example.com defaults in non-DRY_RUN mode)."""
    fields = dict(MODE=mode, owner_email="owner@t.example", agent_email="agent@t.example",
                  staging_recipient="owner+staging@t.example")
    fields.update(overrides)
    return dataclasses.replace(config.load(), **fields)


def load_secret_guard():
    """Import .githooks/secret_guard.py as a module (it is a hook script, not a package member)."""
    p = config.REPO_ROOT / ".githooks" / "secret_guard.py"
    spec = importlib.util.spec_from_file_location("secret_guard", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
