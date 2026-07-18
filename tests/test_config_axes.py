"""Mailbox/owner as two INDEPENDENT, non-secret axes (separating the sending account from the
owner it reports to). A persona names a `mailbox` id and an `owner` id in its config.toml; each
selects a ~/.config/cagent/.env-<id> overlay. Layering is global .env < mailbox < owner < persona,
and owner identity is env-ONLY (a config.toml owner_email is ignored), so an owner's address can
never be committed. These tests redirect ~/.config/cagent and the repo root to tmp_path."""
import types

import pytest

from cagent import config, gmail


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Isolated ~/.config/cagent (ENV_PATH) + repo root (personas/, config.toml). Returns helpers."""
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    monkeypatch.setattr(config, "ENV_PATH", cfgdir / ".env")
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.setattr(config, "CONFIG_TOML", tmp_path / "repo" / "config.toml")
    (tmp_path / "repo").mkdir()
    monkeypatch.delenv("CAGENT_PERSONA", raising=False)
    monkeypatch.delenv("AGENT_MODE", raising=False)

    def write_env(name: str, **kv):
        path = cfgdir / name
        path.write_text("".join(f"{k}={v}\n" for k, v in kv.items()))

    def write_persona(name: str, body: str):
        d = config.REPO_ROOT / "personas" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.toml").write_text(body)

    def write_global_config(body: str):
        config.CONFIG_TOML.write_text(body)

    write_global_config("[personas]\nenabled = []\n")
    return types.SimpleNamespace(write_env=write_env, write_persona=write_persona,
                                 write_global_config=write_global_config)


def _base_global(sandbox):
    # the global account + owner: the legacy single-persona defaults
    sandbox.write_env(".env", AGENT_EMAIL="agent@example.com", GMAIL_APP_PASSWORD="globalpw",
                      OWNER_EMAIL="owner@example.com", OWNER_NAME="Example Owner",
                      STAGING_RECIPIENT="owner+cagent-staging@example.com", COMMAND_TOKEN="gtok")


def test_axes_select_overlays(sandbox):
    """mailbox-2 swaps the account, owner-2 swaps the recipient; together they form the cross product."""
    _base_global(sandbox)
    sandbox.write_env(".env-mailbox-2", AGENT_EMAIL="bravo@example.com", GMAIL_APP_PASSWORD="bravopw")
    sandbox.write_env(".env-owner-2", OWNER_EMAIL="owner2@example.com", OWNER_NAME="Second Owner",
                      STAGING_RECIPIENT="owner2+staging@example.com", COMMAND_TOKEN="mtok")
    sandbox.write_persona("bravo", '[agent]\nmailbox = "mailbox-2"\nowner = "owner-2"\n')

    c = config.load("bravo")
    assert c.agent_email == "bravo@example.com"          # from the mailbox overlay
    assert c.gmail_app_password == "bravopw"
    assert c.owner_email == "owner2@example.com"  # from the owner overlay
    assert c.owner_name == "Second Owner"
    assert c.staging_recipient == "owner2+staging@example.com"
    assert c.command_token == "mtok"                   # token lives with the owner
    assert (c.mailbox, c.owner) == ("mailbox-2", "owner-2")


def test_axes_are_independent(sandbox):
    """A persona on the bravo account but reporting to the global owner: mailbox-2 x owner-1."""
    _base_global(sandbox)
    sandbox.write_env(".env-mailbox-2", AGENT_EMAIL="bravo@example.com", GMAIL_APP_PASSWORD="bravopw")
    sandbox.write_persona("hybrid", '[agent]\nmailbox = "mailbox-2"\n')   # no owner -> global owner

    c = config.load("hybrid")
    assert c.agent_email == "bravo@example.com"
    assert c.owner_email == "owner@example.com"    # global owner, not overridden


def test_owner_is_env_only(sandbox):
    """A config.toml owner_email is IGNORED: owner identity is structurally env-only, never repo."""
    _base_global(sandbox)
    sandbox.write_persona("sneaky", '[agent]\nowner_email = "attacker@evil.test"\n')

    c = config.load("sneaky")
    assert c.owner_email == "owner@example.com"    # the repo value did not leak through


def test_persona_overlay_still_wins(sandbox):
    """The legacy per-persona .env-<persona> overlay remains the highest-priority escape hatch."""
    _base_global(sandbox)
    sandbox.write_env(".env-owner-2", OWNER_EMAIL="owner2@example.com")
    sandbox.write_env(".env-alpha", OWNER_EMAIL="override@example.com")
    sandbox.write_persona("alpha", '[agent]\nowner = "owner-2"\n')

    assert config.load("alpha").owner_email == "override@example.com"


def test_missing_overlay_falls_back_to_global(sandbox):
    """Naming an overlay that does not exist silently inherits the global value (validate flags it)."""
    _base_global(sandbox)
    sandbox.write_persona("ghost", '[agent]\nmailbox = "mailbox-9"\nowner = "owner-9"\n')

    c = config.load("ghost")
    assert c.agent_email == "agent@example.com"
    assert c.owner_email == "owner@example.com"


@pytest.mark.parametrize("field", ["mailbox", "owner"])
def test_invalid_ref_id_raises(sandbox, field):
    _base_global(sandbox)
    sandbox.write_persona("bad", f'[agent]\n{field} = "../evil"\n')
    with pytest.raises(ValueError):
        config.load("bad")


def test_validate_personas_flags_missing_file_and_collision(sandbox, monkeypatch):
    _base_global(sandbox)
    # good: mailbox-2 + owner-2 both exist and define their key
    sandbox.write_env(".env-mailbox-2", AGENT_EMAIL="bravo@example.com", GMAIL_APP_PASSWORD="x")
    sandbox.write_env(".env-owner-2", OWNER_EMAIL="owner2@example.com")
    sandbox.write_persona("bravo", '[agent]\nmailbox = "mailbox-2"\nowner = "owner-2"\n')
    # bad: owner-9 file is absent -> must flag "file exists" False
    sandbox.write_persona("ghost", '[agent]\nowner = "owner-9"\n')
    # bad: owner resolves to the agent's own account -> "owner != agent account" False
    sandbox.write_env(".env-owner-self", OWNER_EMAIL="agent@example.com")
    sandbox.write_persona("narcissist", '[agent]\nowner = "owner-self"\n')
    monkeypatch.setattr(config, "enabled_personas", lambda: ["bravo", "ghost", "narcissist"])

    rows = {name: ok for name, ok, _ in config.validate_personas()}
    assert rows["persona bravo: mailbox 'mailbox-2' file exists"] is True
    assert rows["persona bravo: owner != agent account"] is True
    assert rows["persona ghost: owner 'owner-9' file exists"] is False
    assert rows["persona narcissist: owner != agent account"] is False


def test_to_header_renders_owner_name():
    cfg = types.SimpleNamespace(owner_name="Second Owner")
    assert gmail._to_header(cfg, "owner2@example.com") == "Second Owner <owner2@example.com>"


def test_to_header_bare_address_without_name():
    cfg = types.SimpleNamespace(owner_name="")
    assert gmail._to_header(cfg, "owner@example.com") == "owner@example.com"
