"""The weekly run state machine (pure)."""

import pytest

from app.runs import TRANSITIONS, InvalidTransition, validate_transition

HAPPY_PATH = ["collecting", "asking", "assembling", "awaiting_approval", "sent"]


def test_happy_path_is_allowed():
    for current, nxt in zip(HAPPY_PATH, HAPPY_PATH[1:], strict=False):
        validate_transition(current, nxt)


@pytest.mark.parametrize(
    "current, nxt",
    [
        ("asking", "sent"),  # skipping assembly and approval
        ("collecting", "awaiting_approval"),
        ("asking", "awaiting_approval"),  # must go through assembling
        ("assembling", "sent"),
        ("sent", "asking"),  # sent is final
        ("sent", "needs_attention"),
        ("rejected", "sent"),  # a rejected packet must be rebuilt and re-approved
    ],
)
def test_skipping_or_going_back_is_refused(current, nxt):
    with pytest.raises(InvalidTransition, match=f"{current} -> {nxt}"):
        validate_transition(current, nxt)


def test_rejected_packet_can_be_rebuilt():
    validate_transition("awaiting_approval", "rejected")
    validate_transition("rejected", "assembling")


@pytest.mark.parametrize(
    "state", ["collecting", "asking", "assembling", "awaiting_approval", "rejected"]
)
def test_any_open_run_can_need_attention(state):
    validate_transition(state, "needs_attention")


def test_needs_attention_can_resume():
    validate_transition("needs_attention", "asking")
    validate_transition("needs_attention", "assembling")


def test_every_state_in_the_schema_is_covered():
    # Same list as the CHECK constraint on weekly_runs.state.
    assert set(TRANSITIONS) == {
        "collecting",
        "asking",
        "assembling",
        "awaiting_approval",
        "sent",
        "rejected",
        "needs_attention",
    }
    assert TRANSITIONS["sent"] == frozenset()
