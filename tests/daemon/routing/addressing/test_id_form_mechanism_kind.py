"""UUID addressing: a python_worker row keyed at the ``a.`` id URI is a mechanism.

Once records are id-minted, a request to a python_worker agent (``jev``)
is queued at ``agent:<owner>:<node>:a.<id>``.  The id
segment is not the worker's name, so the harness kind is read through the
worker whose network identity that URI is, never from the raw segment;
otherwise the worker's ``result`` frame is not answered by the mechanism
(ruling 59) and falls through to the ordinary settle-hint path.
"""

from __future__ import annotations

from tests.support.runtime.cell_delivery import AdmittingCellDelivery

import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from tests.daemon.routing.addressing.test_id_form_presence import NAME_URI, NODE, OWNER

ID_URI = f"agent:{OWNER}:{NODE}:a.bl5ksbh3pghtmvnfo5peihbyke"


def _request(message_id: str, recipient: str) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id=f"conversation-{message_id}",
        sender=f"agent:{OWNER}:{NODE}:requester",
        recipient=recipient,
        payload=json.dumps({"message": "please do the thing"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


@pytest.fixture(params=["jev", "pi"])
def worker(tmp_path: Path, request, *, actor_host):
    """A real inbox and runtime for the id-minted headless ``worker``."""

    from hyprial.daemon.impl import DaemonEventBridge
    from hyprial.daemon.impl.desired_state import DesiredStateStore
    from hyprial.kernel import HarnessLaunchSpec
    from tests.identity.providers.lifecycle.availability.support import (
        FakeHarnessFarm,
        _NullTransport,
    )

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    inbox = InboxService(
        state_dir / "inbox.sqlite3", MemoryDeliveryTransport(), node_id=NODE,
    )
    desired = DesiredStateStore(state_dir / "desired-state.json")
    desired.upsert_harness(
        HarnessLaunchSpec(harness=request.param, name="worker", headless=True)
    )
    runtime = DaemonEventBridge(
        state_dir=state_dir, node_id=NODE, desired_state=desired,
        transport=_NullTransport(), inbox=inbox,
        harnesses=FakeHarnessFarm(actors=("worker",)),
        harness_actor_uri=lambda _name: ID_URI,
        harness_read_keys=lambda _name: (ID_URI, NAME_URI),
        recipient_read_keys=lambda address: (
            (ID_URI, NAME_URI) if address == ID_URI else (address,)
        ),
        official_actor_uri=lambda address: ID_URI if address == NAME_URI else address,
        clock_ms=lambda: 1_000, actor_host=actor_host,
        cell_delivery=AdmittingCellDelivery(),
    )
    try:
        runtime.start()
        yield request.param, inbox, runtime
    finally:
        runtime.stop()
        inbox.close()


@pytest.mark.parametrize("recipient", [ID_URI, NAME_URI])
def test_python_worker_kind_is_read_through_the_official_address(
    worker, recipient: str
) -> None:
    harness, _inbox, runtime = worker

    assert runtime._mechanism_is_python_worker(_request("m1", recipient)) is (  # noqa: SLF001
        harness != "pi"
    )


def test_python_worker_result_at_the_id_uri_is_answered_by_the_mechanism(
    worker, monkeypatch
) -> None:
    from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus

    harness, inbox, runtime = worker
    if harness == "pi":
        pytest.skip("a model harness result is not a mechanism exchange")
    original = _request("post-flip", ID_URI)
    assert inbox.receive(original).accepted
    submitted: list[InboxMessage] = []
    submit = inbox.submit

    def recording_submit(message: InboxMessage):
        submitted.append(message)
        return submit(message)

    monkeypatch.setattr(inbox, "submit", recording_submit)
    result = HarnessResult(
        "post-flip", ID_URI, HarnessResultStatus.COMPLETED, output="answered"
    )

    assert runtime._settle_mechanism_answer(original, result) is True  # noqa: SLF001

    assert inbox.pending_messages(ID_URI) == ()
    (reply,) = submitted
    assert reply.sender == ID_URI
    assert reply.recipient == original.sender
    body = json.loads(reply.payload)
    assert body["replyTo"] == "post-flip"
    assert body["origin"] == {
        "via": "daemon", "mechanism": "python_worker", "onBehalfOf": ID_URI,
    }


@pytest.mark.parametrize("live", [True, False])
def test_python_worker_requester_is_recognised_at_its_id_uri(worker, live: bool) -> None:
    """The declared worker answers when it is not streaming."""

    harness, _inbox, runtime = worker
    if not live:
        runtime.harnesses.actors = ()

    assert runtime._mechanism_is_python_worker(_request("m1", ID_URI)) is (  # noqa: SLF001
        harness != "pi"
    )


def test_an_id_no_local_worker_owns_names_no_python_worker(worker) -> None:
    _harness, _inbox, runtime = worker
    stranger = f"agent:{OWNER}:{NODE}:a.aaaaaaaaaaaaaaaaaaaaaaaaaa"

    assert runtime._mechanism_is_python_worker(_request("m1", stranger)) is False  # noqa: SLF001
