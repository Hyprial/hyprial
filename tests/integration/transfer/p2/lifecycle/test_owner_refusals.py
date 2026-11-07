"""Unsupported lifecycle owners must reject entity fences before side effects."""

from queue import Queue
import pytest

from hyprial.kernel import LifecycleMutationRequest
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.daemon.impl.harnesses.actor.facade import HarnessRuntimeFacade
from hyprial.daemon.impl.harnesses.runtime.ports import RemoveHarnessCommand
from hyprial.daemon.impl.session_actor import SessionActor
from hyprial.daemon.impl.operations.session_ports import UnregisterSessionCommand
from hyprial.daemon.impl.route_registration import RouteRegistrationIo
from hyprial.daemon.impl.network.route_ports import DropRouteCommand
from hyprial.daemon.impl.correlation.correlation import CorrelationEventRouter

from ..source.hold_support import Launcher


class Sink:
    def __init__(self):
        self.events = Queue()

    def publish(self, event):
        self.events.put(event)


def envelope(payload):
    return LifecycleMutationRequest('call', 'attempt', 'operation', None, payload,
                                    expected_entity_token='incarnation')


@pytest.mark.parametrize('deadline', [False, True])
def test_unwired_harness_owner_refuses_before_persistence(deadline):
    launcher = Launcher()
    owner = HarnessRuntimeActor(launcher)
    runtime = HarnessRuntimeFacade(owner)
    sink = Sink()
    owner.subscribe_events(sink.publish)
    try:
        command = envelope(RemoveHarnessCommand('call', 'pi', 'source'))
        if deadline:
            from hyprial.daemon.impl.lifecycle.lifecycle_receipts import FailHarnessLifecycleCommand
            command = FailHarnessLifecycleCommand('deadline', command, 'TEST_TIMEOUT', 'test')
        owner.submit(command)
        event = sink.events.get(timeout=5)
        assert event.code == 'LIFECYCLE_INCARNATION_UNSUPPORTED'
        assert not launcher.processes
    finally:
        runtime.stop(5)


def test_unwired_session_owner_refuses_before_persistence(tmp_path):
    store = DesiredStateStore(tmp_path/'desired.json')
    sink = Sink()
    owner = SessionActor(store, daemon_epoch='test', event_sink=sink)
    try:
        owner.submit(envelope(UnregisterSessionCommand('call', 'source', 'session')))
        event = sink.events.get(timeout=5)
        assert event.code == 'LIFECYCLE_INCARNATION_UNSUPPORTED'
        assert not store.load().lifecycle_receipts
    finally:
        assert owner.drain(5).complete


def test_unwired_route_owner_refuses_before_native_work():
    router = CorrelationEventRouter()
    waiter = router.register('call', attempt_token='attempt', generation=1,
                             versions=frozenset({1}))
    # Any native call is an error: object has no transport implementation.
    owner = RouteRegistrationIo(object(), lambda _: None, router)
    try:
        owner.submit(envelope(DropRouteCommand('call', 'attempt', 1, 1, 'source', 'managed:source')))
        event = waiter.wait(5)
        assert event.code == 'LIFECYCLE_INCARNATION_UNSUPPORTED'
        assert owner.registered() == ()
    finally:
        assert owner.drain(5)
