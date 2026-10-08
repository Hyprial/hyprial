"""Official ``lark-oapi`` SDK boundary used by the adapter."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from hyprial.daemon.impl.adapters.lark.contracts.endpoint import lark_base_url

from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import lark
if TYPE_CHECKING:
    pass

EventClientFactory = Callable[[str, str, Any], Any]


class LarkEventStream:
    """Blocking official-SDK WebSocket event source.

    Process supervision belongs to the daemon. ``start`` deliberately preserves
    the SDK's blocking contract so the daemon can own its thread/process policy.

    ⚠️ **One event stream per process.**  ``lark_oapi.ws.client`` holds a
    module-level event loop and drives it with ``run_until_complete`` inside
    ``start``, so a second stream started in the same process raises
    ``RuntimeError: This event loop is already running``.  Two Lark adapters
    therefore cannot both run their inbound streams under one interpreter: a
    multi-adapter topology needs a process per stream, not a thread per
    stream.  There is also no stop -- ``start`` never returns and the SDK
    exposes no shutdown -- so ending a stream means ending its process.

    Both facts were established by running it (see
    ``contract/e2e/run-lark-standin-selfcheck.py``, which owns a
    process for exactly this reason), not read off the vendor's
    documentation, which states neither.
    """

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        on_message: Callable[[Any], Any],
        on_ready: Callable[[], None] | None = None,
        on_reconnect: Callable[[], None] | None = None,
        on_transport_activity: Callable[[], None] | None = None,
        on_event_activity: Callable[[], None] | None = None,
        on_member_change: Callable[[Any], Any] | None = None,
        client_factory: EventClientFactory | None = None,
    ) -> None:
        def observed_message(event: Any) -> Any:
            if on_event_activity is not None:
                on_event_activity()
            return on_message(event)

        dispatcher_builder = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(observed_message)
        )
        if on_member_change is not None:
            # Delivered only when the App's platform-side event subscription
            # includes im.chat.member.user.added/deleted_v1; registering the
            # handler alone does not subscribe (harmless when unsubscribed).
            def observed_member_change(event: Any) -> Any:
                if on_event_activity is not None:
                    on_event_activity()
                return on_member_change(event)

            dispatcher_builder = (
                dispatcher_builder.register_p2_im_chat_member_user_added_v1(
                    observed_member_change
                ).register_p2_im_chat_member_user_deleted_v1(
                    observed_member_change
                )
            )
        dispatcher = dispatcher_builder.build()
        if client_factory is not None:
            self._client = client_factory(app_id, app_secret, dispatcher)
        else:
            self._client = self._default_factory(
                app_id,
                app_secret,
                dispatcher,
                on_ready=on_ready,
                on_reconnect=on_reconnect,
                on_transport_activity=on_transport_activity,
            )

    @staticmethod
    def _default_factory(
        app_id: str,
        app_secret: str,
        dispatcher: Any,
        *,
        on_ready: Callable[[], None] | None = None,
        on_reconnect: Callable[[], None] | None = None,
        on_transport_activity: Callable[[], None] | None = None,
    ) -> Any:
        class ReadyClient(lark.ws.Client):
            # Websocket events are never replayed by the platform: messages
            # sent during a disconnect window are lost unless fetched.  The
            # first connect signals readiness; every later (re)connect fires
            # ``on_reconnect`` so the worker can reconcile missed history.
            ever_connected = False

            async def _handle_message(self, message: bytes) -> None:
                # Count control frames (notably PONG) as transport activity.
                # This is deliberately separate from business events so a quiet
                # chat remains healthy while the subscribed websocket is alive.
                if on_transport_activity is not None:
                    on_transport_activity()
                await super()._handle_message(message)

            async def _connect(self) -> None:
                was_connected = self._conn is not None
                await super()._connect()
                if was_connected or self._conn is None:
                    return
                if not self.ever_connected:
                    self.ever_connected = True
                    if on_ready is not None:
                        on_ready()
                elif on_reconnect is not None:
                    on_reconnect()

        # ``domain`` steers two things at once here: the HTTP
        # endpoint-discovery request that precedes every handshake (what
        # ``probe_rest_endpoint`` re-runs) and the websocket the returned URL
        # points at.  Passing it is therefore not optional for a redirected
        # process -- omitting it leaves the inbound half on production while
        # the outbound half has moved.
        return ReadyClient(
            app_id,
            app_secret,
            event_handler=dispatcher,
            domain=lark_base_url(),
            auto_reconnect=True,
        )

    def start(self) -> None:
        self._client.start()

    def probe_rest_endpoint(self) -> None:
        """Actively re-run the SDK's credentialed endpoint-discovery request.

        ``_get_conn_url`` is the HTTP/REST request the official client itself
        uses before every websocket handshake.  It verifies network reachability
        and app credentials without sending a chat message or consuming a
        production conversation.  The returned one-time URL is intentionally
        discarded and never logged.
        """

        self._client._get_conn_url()
