"""Phase 4: inbound routing -- +tag, then In-Reply-To (sent index), then [tag] subject, then default."""
from cagent import gmail

TAG_MAP = {"alpha": "alpha", "data": "data", "echozz": "echozz"}
SENT_MAP = {"<m1@example.com>": "data"}
AGENT = "agent@example.com"


def _msg(**kw):
    base = {"to": "", "delivered_to": "", "in_reply_to": "", "subject": ""}
    base.update(kw)
    return base


def test_route_by_plus_tag():
    m = _msg(to="Don Q <agent+echozz@example.com>")
    assert gmail.route_persona(m, TAG_MAP, SENT_MAP, "alpha", AGENT) == "echozz"


def test_route_by_delivered_to():
    m = _msg(delivered_to="agent+data@example.com", to="agent@example.com")
    assert gmail.route_persona(m, TAG_MAP, SENT_MAP, "alpha", AGENT) == "data"


def test_route_by_in_reply_to():
    m = _msg(to="agent@example.com", in_reply_to="<m1@example.com>")
    assert gmail.route_persona(m, TAG_MAP, SENT_MAP, "alpha", AGENT) == "data"


def test_route_by_subject_prefix():
    m = _msg(to="agent@example.com", subject="[data] re: your finding")
    assert gmail.route_persona(m, TAG_MAP, SENT_MAP, "alpha", AGENT) == "data"


def test_route_default_when_untagged():
    m = _msg(to="agent@example.com", subject="hello there")
    assert gmail.route_persona(m, TAG_MAP, SENT_MAP, "alpha", AGENT) == "alpha"


def test_plus_tag_beats_subject():
    m = _msg(to="agent+echozz@example.com", subject="[data] hi")
    assert gmail.route_persona(m, TAG_MAP, SENT_MAP, "alpha", AGENT) == "echozz"


def test_unknown_tag_falls_through_to_default():
    m = _msg(to="agent+ghost@example.com", subject="hi")
    assert gmail.route_persona(m, TAG_MAP, SENT_MAP, "alpha", AGENT) == "alpha"
