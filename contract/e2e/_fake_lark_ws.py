"""The inbound half of the Lark stand-in: a websocket the real SDK connects to.

``_fake_lark`` answers the REST surface and the endpoint-discovery request, so
readiness and health work.  Events do not arrive through it, because Lark
pushes them over a long connection carrying protobuf frames.  This module
serves that connection.

What is faked stays the service.  The client is the real
``lark_oapi.ws.Client``, doing its own handshake and its own frame decoding,
and the frames are built with the SDK's own ``pbbp2`` codec.  Substituting the
client instead -- ``LarkEventStream(client_factory=...)`` exists and would be
far less work -- would replace the vendor's transport *and* protocol with our
idea of them, which is mocking external software: it would keep passing after
the vendor changed either one, and passing is exactly what it must not do
then.

The three questions from ``_fake_lark`` apply here too, and question 3 is
sharper on this side: the SDK connects to whatever host the discovery response
named, so this server's address is chosen by the fake and must be asserted to
be loopback -- which ``_fake_lark`` does for every URL it emits, including the
one this module supplies.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from types import TracebackType
from typing import Any

import websockets
from lark_oapi.ws.enum import FrameType, MessageType
from lark_oapi.ws.pb.pbbp2_pb2 import Frame

_LOOPBACK = "127.0.0.1"

#: Header keys the SDK reads off a data frame.  Taken from ``lark_oapi.ws.const``
#: rather than retyped, so a vendor rename breaks the import instead of
#: silently producing frames the client ignores.
from lark_oapi.ws.const import (  # noqa: E402 - grouped with its explanation
    HEADER_MESSAGE_ID,
    HEADER_SEQ,
    HEADER_SUM,
    HEADER_TRACE_ID,
    HEADER_TYPE,
)


class FakeLarkEventsError(AssertionError):
    """The event stand-in was used in a way that would weaken the scenario."""


def message_receive_event(
    *,
    chat_id: str = "oc_fake_chat",
    sender_open_id: str = "ou_fake_user",
    text: str = "hello",
    message_id: str | None = None,
) -> dict[str, Any]:
    """One ``im.message.receive_v1`` event in the shape the platform sends.

    Built here rather than in each test so the shape has one owner; a scenario
    that needs a different message type should add a sibling rather than
    hand-editing this dict at the call site.
    """

    return {
        "schema": "2.0",
        "header": {
            "event_id": uuid.uuid4().hex,
            "event_type": "im.message.receive_v1",
            "create_time": "0",
            "token": "fake",
            "app_id": "cli_fake_e2e_standin",
            "tenant_key": "fake-tenant",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": sender_open_id},
                "sender_type": "user",
            },
            "message": {
                "message_id": message_id or f"om_fake{uuid.uuid4().hex[:12]}",
                "chat_id": chat_id,
                "chat_type": "p2p",
                "message_type": "text",
                "create_time": "0",
                "content": json.dumps({"text": text}),
            },
        },
    }


def _data_frame(payload: bytes, *, seq: int) -> bytes:
    frame = Frame()
    frame.SeqID = seq
    frame.LogID = seq
    frame.service = 1
    frame.method = FrameType.DATA.value
    for key, value in (
        (HEADER_TYPE, MessageType.EVENT.value),
        (HEADER_MESSAGE_ID, uuid.uuid4().hex),
        (HEADER_TRACE_ID, uuid.uuid4().hex),
        (HEADER_SUM, "1"),
        (HEADER_SEQ, "1"),
    ):
        header = frame.headers.add()
        header.key = key
        header.value = value
    frame.payload = payload
    return frame.SerializeToString()


class FakeLarkEvents:
    """A loopback websocket that pushes events at a real SDK client."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="fake-lark-ws", daemon=True
        )
        self._ready = threading.Event()
        self._server: Any = None
        self._connections: set[Any] = set()
        self._acks: list[bytes] = []
        self._seq = 0
        self.port = 0

    # -- lifecycle ---------------------------------------------------------

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())
        self._loop.run_forever()

    async def _serve(self) -> None:
        self._server = await websockets.serve(self._handle, _LOOPBACK, 0)
        self.port = self._server.sockets[0].getsockname()[1]
        self._ready.set()

    async def _handle(self, connection: Any) -> None:
        self._connections.add(connection)
        try:
            async for message in connection:
                # Only data frames are acks.  The client also sends CONTROL
                # ping frames on its own schedule, and those carry no payload
                # -- treating one as an ack decodes an empty body and fails
                # with a JSON error that says nothing about what happened.
                frame = Frame()
                try:
                    frame.ParseFromString(message)
                except Exception:  # noqa: BLE001 - a malformed frame is not an ack
                    continue
                if frame.method == FrameType.DATA.value:
                    self._acks.append(message)
        except Exception:  # noqa: BLE001 - a closed socket is not a failure
            pass
        finally:
            self._connections.discard(connection)

    def __enter__(self) -> FakeLarkEvents:
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise FakeLarkEventsError("the fake event server never bound a port")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        async def shutdown() -> None:
            # Close the accepted connections, not only the listener.  The SDK
            # client runs in a thread that blocks forever in start() and
            # cannot be stopped from here, so its socket is released by this
            # side hanging up; leaving the listener closed but the connections
            # open holds three descriptors per run.
            for connection in list(self._connections):
                await connection.close()
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()

        # Stopping the loop is scheduled separately rather than done at the
        # end of the coroutine: stopping it there leaves this future forever
        # unresolved, so result() burns its whole timeout and the teardown
        # merely looks slow instead of looking wrong.  Ten silent seconds per
        # use is exactly the "did not run vs ran and was slow" confusion this
        # work exists to remove, appearing inside its own tooling.
        asyncio.run_coroutine_threadsafe(shutdown(), self._loop).result(timeout=10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        self._loop.close()

    # -- what a scenario does with it --------------------------------------

    @property
    def url(self) -> str:
        """The one-time connection URL, in the shape ``_connect`` parses.

        ``device_id`` and ``service_id`` are read straight off the query
        string by the SDK, so omitting either makes the client raise a
        ``KeyError`` during connect rather than fail visibly.
        """

        return (
            f"ws://{_LOOPBACK}:{self.port}/ws"
            "?device_id=fake-device&service_id=1"
        )

    def wait_for_connection(self, timeout: float = 10.0) -> None:
        deadline = _deadline(timeout)
        while not self._connections:
            if _expired(deadline):
                raise FakeLarkEventsError(
                    "the SDK never connected to the fake event server; the "
                    "endpoint-discovery response is what chooses this address"
                )
            _pause()

    def deliver(self, event: dict[str, Any]) -> None:
        """Push one event frame to every connected client."""

        self._seq += 1
        frame = _data_frame(json.dumps(event).encode("utf-8"), seq=self._seq)
        if not self._connections:
            raise FakeLarkEventsError("no SDK client is connected to deliver to")
        for connection in list(self._connections):
            asyncio.run_coroutine_threadsafe(
                connection.send(frame), self._loop
            ).result(timeout=10)

    def wait_for_ack(self, timeout: float = 10.0) -> Frame:
        """Wait for the client's reply frame and return it decoded.

        Delivered and handled are different facts.  The reply carries the
        handler's outcome, so a scenario can tell "the SDK received it" from
        "our code ran and did not raise".
        """

        deadline = _deadline(timeout)
        while not self._acks:
            if _expired(deadline):
                raise FakeLarkEventsError(
                    "the SDK never acknowledged the delivered event"
                )
            _pause()
        frame = Frame()
        frame.ParseFromString(self._acks.pop(0))
        return frame


def ack_status(frame: Frame) -> int:
    """The HTTP-shaped code the client put in its reply payload."""

    payload = json.loads(frame.payload.decode("utf-8"))
    return int(payload.get("code", -1))


def _deadline(timeout: float) -> float:
    import time

    return time.monotonic() + timeout


def _expired(deadline: float) -> bool:
    import time

    return time.monotonic() >= deadline


def _pause() -> None:
    import time

    time.sleep(0.02)
