"""UUID addressing §2.3: an adapter inbound refused at the send boundary.

``message.send`` refuses a retired name (``AGENT_ADDRESS_RETIRED``) and an
unclaimed local name (``TARGET_UNRESOLVED``) before anything is queued.  A
Lark inbound submission refused that way is a definitive pre-admission
outcome: the frozen submission is released, the user is told it was not
delivered, and it is never parked as an unknown outcome.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.adapters.lark import ActorTarget
from hyprial.kernel import ipc_errors
from tests.daemon.adapters.lark.outbound.test_lark_submission_outcome_unknown import (  # noqa: F401
    make_rig,
)

NODE_PREFIX = "agent:test-owner:lark-idempotency-node:"


@pytest.mark.parametrize(
    ("segment", "expected_code"),
    [
        ("target-agent", ipc_errors.AGENT_ADDRESS_RETIRED),
        ("ghost", ipc_errors.TARGET_UNRESOLVED),
    ],
)
def test_send_boundary_refusal_is_a_definitive_pre_admission_outcome(
    id_form, make_rig, segment, expected_code  # noqa: F811
):
    rig = make_rig(phase="unblocked")
    rig.app._presence = SimpleNamespace(
        online_actors=lambda: (),
        actor_online=lambda _actor: False,
        online_mailboxes=lambda: (),
    )
    target = NODE_PREFIX + segment
    rig.adapter._routes.target = ActorTarget(target, target, target)

    outcome = rig.adapter.handle_inbound(rig.message)

    assert outcome.status == "error"
    assert outcome.ack_error == "DaemonRequestError"
    assert rig.adapter._state.pending_inbound_submission(rig.message.message_id) is None
    [letter] = rig.adapter.dead_letters()
    assert letter.detail == expected_code
    assert any("not delivered" in text for _, text, _ in rig.platform.replies)
