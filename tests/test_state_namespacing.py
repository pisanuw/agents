"""Phase 1: state-path namespacing. state_root() is the single indirection that lets each
persona run in its own state/personas/<name>/ namespace while the legacy flat state/ layout
stays byte-for-byte unchanged when CAGENT_PERSONA is unset."""
import os

import pytest

from cagent import config


def _set_persona(value):
    if value is None:
        os.environ.pop("CAGENT_PERSONA", None)
    else:
        os.environ["CAGENT_PERSONA"] = value


def test_legacy_layout_when_unset():
    _set_persona(None)
    assert config.state_root() == config.REPO_ROOT / "state"


def test_empty_value_is_legacy():
    try:
        _set_persona("   ")
        assert config.state_root() == config.REPO_ROOT / "state"
    finally:
        _set_persona(None)


def test_namespaced_when_set():
    try:
        _set_persona("alpha")
        assert config.state_root() == config.REPO_ROOT / "state" / "personas" / "alpha"
    finally:
        _set_persona(None)


@pytest.mark.parametrize("bad", ["../evil", "a/b", "Alpha", "-lead", "x y"])
def test_rejects_unsafe_persona(bad):
    try:
        _set_persona(bad)
        with pytest.raises(ValueError):
            config.state_root()
    finally:
        _set_persona(None)
