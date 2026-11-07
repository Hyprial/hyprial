"""Route owner checks incarnation before admission to native effects."""

import inspect
import threading

import pytest

from hyprial.kernel import LifecycleMutationRequest
from hyprial.daemon.impl.route_registration import RouteRegistrationIo
from hyprial.daemon.impl.network.route_ports import EnsureRouteCommand, RouteSpec
from hyprial.daemon.impl.correlation.correlation import CorrelationEventRouter


class Transport:
    calls = 0
    def declare_liveliness(self, key):
        self.calls += 1
        return self
    def declare_queryable(self, key, callback):
        self.calls += 1
        return self
    def close(self):
        pass


@pytest.mark.parametrize('token', ['old', 'current', None, 'unavailable'])
def test_route_owner_enforces_fence_before_native_registration(token):
    assert 'agent_identity' in inspect.signature(RouteRegistrationIo).parameters, \
        'route owner lacks incarnation projection port'
    reads = []
    class Identity:
        def entity_token(self, actor):
            assert actor == 'source'
            reads.append(threading.get_ident())
            if token == 'unavailable':
                raise RuntimeError('projection unavailable')
            return 'current'
    transport = Transport()
    router = CorrelationEventRouter()
    waiter = router.register('route', attempt_token='attempt', generation=1,
                             versions=frozenset({1}))
    owner = RouteRegistrationIo(transport, lambda _: lambda key: None, router,
                                agent_identity=Identity())
    try:
        payload = EnsureRouteCommand('route', 'attempt', 1, 1,
                                     RouteSpec('source', 'live/source', 'inbox/source'),
                                     'managed:source')
        owner.submit(LifecycleMutationRequest('route', 'attempt', 'op', None, payload,
                                              expected_entity_token=token))
        event = waiter.wait(5)
        if token in {'old', 'unavailable'}:
            expected = 'AGENT_INCARNATION_MISMATCH' if token == 'old' else 'TRANSFER_CUSTODY_UNAVAILABLE'
            assert getattr(event, 'code', None) == expected
            assert transport.calls == 0
        else:
            assert event.changed
            assert transport.calls == 2
            assert owner.retire_receipt('attempt', event.provenance.resource_token)
            owner.confirm_receipt_retired('attempt', event.provenance.resource_token)
        if token is not None:
            assert reads and all(t != threading.get_ident() for t in reads)
    finally:
        owner.close_registrations(5)
        assert owner.drain(5)
