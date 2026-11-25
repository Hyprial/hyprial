from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from hyprial.daemon.impl.adapters.common.transport import (
    ResourceCloseState,
    SealedNativeMediaAuthority,
)
from hyprial.daemon.impl.adapters.telegram import TelegramCredentials
from hyprial.daemon.impl.adapters.telegram.components import build_default_components
from hyprial.daemon.impl.adapters.telegram.errors import TelegramPollRetry
from hyprial.daemon.impl.adapters.telegram.transport import HttpxTelegramTransport

from .support import LocalHttpServer, Request, json_response
from .test_media import TOKEN, _Credentials, _Scopes
from .test_provider import _instance


def test_component_bundle_shares_one_hardened_client_and_borrowed_stays_open(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        authority = SealedNativeMediaAuthority(
            tmp_path / "components.sqlite3", max_blob_bytes=20 * 1024 * 1024
        )
        borrowed = httpx.AsyncClient(trust_env=False, follow_redirects=False)
        bundle = build_default_components(
            authority=authority,
            scope_resolver=_Scopes(),
            credentials=_Credentials(),
            receiver_ref="receiver:telegram",
            instance=_instance(),
            account_scope="123456",
            client=borrowed,
        )
        assert bundle.transport.client is borrowed
        assert bundle.media.transport is bundle.transport
        assert not hasattr(bundle.media, "close") and not hasattr(bundle.media, "aclose")
        first = await bundle.aclose(1000)
        second = await bundle.aclose(1000)
        assert first == second
        assert first.state is ResourceCloseState.CLOSED
        assert not borrowed.is_closed
        await borrowed.aclose()
        authority.close()

    asyncio.run(scenario())


def test_terminal_close_cancels_long_poll_and_owned_client_once() -> None:
    async def scenario() -> None:
        async def handler(request: Request):  # type: ignore[no-untyped-def]
            await asyncio.sleep(10)
            return json_response(200, {"ok": True, "result": []})

        async with LocalHttpServer(handler) as server:
            transport = HttpxTelegramTransport(
                api_base_url=server.base_url, owns_client=True
            )
            poll = asyncio.create_task(
                transport.get_updates(
                    TelegramCredentials(TOKEN), offset=1, limit=1
                )
            )
            while not server.requests:
                await asyncio.sleep(0)
            outcome = await transport.aclose(1000)
            assert outcome.state is ResourceCloseState.CLOSED
            with pytest.raises(TelegramPollRetry, match="cancelled"):
                await poll
            assert transport.client.is_closed
            assert await transport.aclose(1000) == outcome

    asyncio.run(scenario())


def test_close_deadline_reports_unknown_without_closing_borrowed_client() -> None:
    class SlowClient(httpx.AsyncClient):
        async def aclose(self) -> None:
            await asyncio.sleep(1)
            await super().aclose()

    async def scenario() -> None:
        client = SlowClient(trust_env=False, follow_redirects=False)
        transport = HttpxTelegramTransport(client=client, owns_client=True)
        outcome = await transport.aclose(1)
        assert outcome.state is ResourceCloseState.UNKNOWN
        assert outcome.reason_code == "telegram-close-deadline"
        assert await transport.aclose(1) == outcome

    asyncio.run(scenario())
