"""draft_status is the single interpreter of a staged draft's approval-lifecycle flags. `held` is
orthogonal (tested separately in the supervise flows), so these fix the approval FSM in one place."""
from cagent import supervise


def test_awaiting_is_the_default():
    assert supervise.draft_status({}) == supervise.AWAITING
    assert supervise.draft_status({"request_sent": True}) == supervise.AWAITING


def test_unrequested_only_when_request_sent_is_explicitly_false():
    assert supervise.draft_status({"request_sent": False}) == supervise.UNREQUESTED
    # a legacy draft with no request_sent key is AWAITING, not UNREQUESTED (matches the old
    # `request_sent is False` reads that never fired on a missing key)
    assert supervise.draft_status({"created": "x"}) == supervise.AWAITING


def test_approved_wins_over_request_state():
    assert supervise.draft_status({"approved": True}) == supervise.APPROVED_UNSENT
    assert supervise.draft_status({"approved": True, "request_sent": False}) == supervise.APPROVED_UNSENT


def test_held_is_orthogonal_to_status():
    # held does not change the lifecycle state; callers test d["held"] separately
    assert supervise.draft_status({"held": True}) == supervise.AWAITING
    assert supervise.draft_status({"held": True, "approved": True}) == supervise.APPROVED_UNSENT
