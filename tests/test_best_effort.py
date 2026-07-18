"""_best_effort is the one wrapper the non-critical tick stages run through: a failing stage must
be logged and swallowed so it can never abort the tick (the next tick retries)."""
import logging

from cagent import tick_pipeline

log = logging.getLogger("t")


def test_returns_value_on_success():
    assert tick_pipeline._best_effort("x", log, lambda: 42) == 42


def test_swallows_and_logs_on_failure(caplog):
    def boom():
        raise RuntimeError("stage blew up")
    with caplog.at_level(logging.INFO):
        assert tick_pipeline._best_effort("digest", log, boom) is None
    assert "digest failed (continuing)" in caplog.text
    assert "stage blew up" in caplog.text
