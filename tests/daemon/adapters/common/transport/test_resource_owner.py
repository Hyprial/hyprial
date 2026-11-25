from __future__ import annotations

import asyncio

import pytest

from hyprial.daemon.impl.adapters.common.transport import (
    ResourceCloseState,
    TransportResourceOwner,
)

from .support import ClosingResource, ClosingSocket


def test_reconnect_closes_only_matching_socket_generation_then_finalizes_once() -> None:
    async def scenario() -> None:
        client = ClosingResource()
        first = ClosingSocket()
        owner = TransportResourceOwner(client, owns_client=True)
        owner.bind_socket(first, generation=1, owns_socket=True)

        stale = await owner.close_socket(
            generation=2,
            code=1001,
            reason="stale",
            timeout_at=asyncio.get_running_loop().time() + 0.2,
        )
        assert stale.state is ResourceCloseState.STALE_GENERATION
        assert first.close_calls == []

        reconnect = await owner.close_socket(
            generation=1,
            code=1001,
            reason="reconnect",
            timeout_at=asyncio.get_running_loop().time() + 0.2,
        )
        assert reconnect.state is ResourceCloseState.CLOSED
        assert first.close_calls == [(1001, "reconnect")]
        assert client.close_calls == 0

        second = ClosingSocket()
        owner.bind_socket(second, generation=2, owns_socket=True)
        deadline = asyncio.get_running_loop().time() + 0.2
        left, right = await asyncio.gather(owner.aclose(deadline), owner.aclose(deadline))
        assert left == right
        assert left.state is ResourceCloseState.CLOSED
        assert second.close_calls == [(None, None)]
        assert client.close_calls == 1

    asyncio.run(scenario())


def test_terminal_finalizer_is_shielded_and_borrowed_resources_are_not_closed() -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        client = ClosingResource(wait=release)
        socket = ClosingSocket(wait=release)
        owner = TransportResourceOwner(client, owns_client=True)
        owner.bind_socket(socket, generation=3, owns_socket=True)
        deadline = asyncio.get_running_loop().time() + 1

        cancelled_waiter = asyncio.create_task(owner.aclose(deadline))
        await asyncio.sleep(0)
        cancelled_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_waiter
        release.set()
        completed = await owner.aclose(deadline)
        assert completed.state is ResourceCloseState.CLOSED
        assert len(socket.close_calls) == 1
        assert client.close_calls == 1

        borrowed_client = ClosingResource()
        borrowed_socket = ClosingSocket()
        borrowed = TransportResourceOwner(borrowed_client, owns_client=False)
        borrowed.bind_socket(borrowed_socket, generation=1, owns_socket=False)
        outcome = await borrowed.aclose(asyncio.get_running_loop().time() + 0.1)
        assert outcome.state is ResourceCloseState.CLOSED
        assert borrowed_socket.close_calls == []
        assert borrowed_client.close_calls == 0

    asyncio.run(scenario())


def test_noncooperating_peer_has_unknown_close_outcome_at_monotonic_deadline() -> None:
    async def scenario() -> None:
        never = asyncio.Event()
        socket = ClosingSocket(wait=never)
        client = ClosingResource()
        owner = TransportResourceOwner(client, owns_client=True)
        owner.bind_socket(socket, generation=9, owns_socket=True)
        outcome = await owner.aclose(asyncio.get_running_loop().time() + 0.02)
        assert outcome.state is ResourceCloseState.UNKNOWN
        assert outcome.reason_code == "transport-close-deadline"
        assert len(socket.close_calls) == 1
        assert client.close_calls == 1

    asyncio.run(scenario())


def test_two_independent_consumers_share_the_internal_owner_contract() -> None:
    class RestConsumer:
        def __init__(self, client) -> None:
            self.resources = TransportResourceOwner(client, owns_client=True)

    class GatewayConsumer:
        def __init__(self, client, socket) -> None:
            self.resources = TransportResourceOwner(client, owns_client=False)
            self.resources.bind_socket(socket, generation=1, owns_socket=True)

    rest = RestConsumer(ClosingResource())
    gateway = GatewayConsumer(ClosingResource(), ClosingSocket())
    assert type(rest.resources) is type(gateway.resources)
