"""Mutation controls for the E2E-020-C durable delivery assertion."""

import copy
import importlib.util
from pathlib import Path

import pytest


SCENARIO = (
    Path(__file__).resolve().parents[4] / 'contract/e2e/run-pac-public-events.py'
)
spec = importlib.util.spec_from_file_location("pac_public_events_candidate", SCENARIO)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert_single_turn_receipt = module.assert_single_turn_receipt


def proof():
    pending = {"kind": "turn", "eventId": "turn-1", "edge": "a -> b", "messageId": None,
               "sender": "owner", "recipient": "sink", "text": "turn notification"}
    delivery = [{"type": "delivery_changed", "data": {
        "eventId": "turn-1", "edge": "a -> b", "messageId": "msg-1",
    }}]
    snapshot = {"graphId": "graph-1", "notifications": [{
        "kind": "turn", "eventId": "turn-1", "edge": "a -> b", "messageId": "msg-1",
    }]}
    inbox = {"messages": [{"messageId": "msg-1", "conversationId": "pac-graph-1",
                            "from": "owner", "to": "sink", "summary": "turn notification"}]}
    resent = {"delivered": 0, "undelivered": 0, "error": None}
    return pending, delivery, snapshot, inbox, resent


@pytest.mark.parametrize("delivered", [0, 1])
def test_exact_durable_receipt_accepts_either_sender_winner(delivered):
    inputs = proof()
    inputs[-1]["delivered"] = delivered
    assert_single_turn_receipt(*inputs)


@pytest.mark.parametrize("mutation", [
    lambda p: p[3]["messages"].clear(),
    lambda p: p[1][0]["data"].update(eventId="other-turn"),
    lambda p: p[1][0]["data"].update(edge="other -> b"),
    lambda p: p[1].append(copy.deepcopy(p[1][0])),
    lambda p: p[3]["messages"].append({"messageId": "msg-1"}),
    lambda p: p[2]["notifications"].clear(),
    lambda p: p[3]["messages"].append({**p[3]["messages"][0], "messageId": "different-id"}),
    lambda p: p[3]["messages"][0].update(conversationId="wrong-graph"),
])
def test_zero_cannot_pass_with_missing_mismatched_or_duplicate_delivery(mutation):
    inputs = list(proof())
    mutation(inputs)
    with pytest.raises(AssertionError):
        assert_single_turn_receipt(*inputs)
