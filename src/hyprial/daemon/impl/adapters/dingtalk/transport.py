from __future__ import annotations

import asyncio
import importlib

from collections.abc import Mapping

from .errors import DingTalkProtocolError
from .runtime import DingTalkCredentials


class DingTalkSdkTransport:
    """Official Stream SDK bridge, loaded only inside the adapter worker."""

    def __init__(self) -> None:
        self._sdk: object | None = None
        self._client: object | None = None
        self._task: asyncio.Task[object] | None = None

    async def start(self, credentials: DingTalkCredentials, receiver: object) -> None:
        try:
            sdk = importlib.import_module("dingtalk_stream")
        except ImportError as exc:
            raise DingTalkProtocolError("dingtalk-stream-dependency-missing") from exc
        client = sdk.DingTalkStreamClient(sdk.Credential(credentials.client_id, credentials.client_secret))
        class Handler(sdk.ChatbotHandler):
            async def process(self, callback: object):  # type: ignore[no-untyped-def]
                headers = getattr(callback, "headers", None)
                delivery_id = getattr(headers, "message_id", None)
                if not isinstance(delivery_id, str) or not delivery_id:
                    raise DingTalkProtocolError("dingtalk-delivery-id-missing")
                payload = getattr(callback, "data", callback)
                await receiver.ingest_stream(delivery_id, payload)
                return sdk.AckMessage.STATUS_OK, "OK"

        client.register_callback_handler(sdk.ChatbotMessage.TOPIC, Handler())
        self._sdk = sdk
        self._client = client
        self._task = asyncio.create_task(client.start())

    async def stop(self) -> None:
        websocket = getattr(self._client, "websocket", None)
        close = getattr(websocket, "close", None)
        if callable(close):
            result = close()
            if hasattr(result, "__await__"):
                await result
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._client = None

    async def reply(self, webhook: str, payload: dict[str, object]) -> Mapping[str, object]:
        try:
            import httpx
        except ImportError as exc:
            raise DingTalkProtocolError("httpx-dependency-missing") from exc
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            response = await client.post(webhook, json=payload)
        if response.status_code != 200:
            raise DingTalkProtocolError(f"reply-http-{response.status_code}")
        value = response.json()
        if not isinstance(value, dict):
            raise DingTalkProtocolError("reply-invalid-response")
        return value


__all__ = ["DingTalkSdkTransport"]
