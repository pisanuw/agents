"""The command table is the single source for VERBS, HELP_LINES and dispatch. These guard against
re-introducing the drift that let a verb be accepted by the parser but hit 'unhandled command'
(actual dispatch behaviour is covered end-to-end in test_email_commands.py)."""
from cagent import commands


def test_every_verb_has_a_handler():
    # VERBS is the parser's allow-set; _HANDLERS is what _dispatch can actually run. Derived from the
    # same table, they must agree exactly -- no verb accepted-then-unhandled, none handled-but-unlisted.
    assert set(commands.VERBS) == set(commands._HANDLERS)


def test_every_verb_is_documented():
    # Every verb the parser accepts appears (as !VERB) in the help menu that seeds !HELP and the footer.
    labels = " ".join(label for label, _desc in commands.HELP_LINES)
    for verb in commands.VERBS:
        assert f"!{verb}" in labels, f"{verb} handled but missing from HELP_LINES"
