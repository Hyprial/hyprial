"""Production Telegram component bundle and client ownership boundary."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from hyprial.daemon.impl.adapters.common.transport import (
    ResourceCloseOutcome,
    SealedNativeMediaAuthority,
)
from hyprial.daemon.impl.adapters.contracts import AdapterInstanceRef

from .contracts import TelegramCredentialResolver
from .media import TelegramMedia, TelegramMediaScopeResolver
from .transport import OFFICIAL_API_BASE_URL, HttpxTelegramTransport


@dataclass(slots=True)
class TelegramComponents:
    transport: HttpxTelegramTransport
    media: TelegramMedia

    async def aclose(self, deadline_ms: int) -> ResourceCloseOutcome:
        return await self.transport.aclose(deadline_ms)


def build_default_components(
    *,
    authority: SealedNativeMediaAuthority,
    scope_resolver: TelegramMediaScopeResolver,
    credentials: TelegramCredentialResolver,
    receiver_ref: str,
    instance: AdapterInstanceRef,
    account_scope: str,
    client: httpx.AsyncClient | None = None,
    api_base_url: str = OFFICIAL_API_BASE_URL,
    read_timeout_seconds: float = 35.0,
) -> TelegramComponents:
    transport = HttpxTelegramTransport(
        client,
        owns_client=client is None,
        api_base_url=api_base_url,
        read_timeout_seconds=read_timeout_seconds,
    )
    media = TelegramMedia(
        transport,
        authority,
        scope_resolver,
        credentials,
        receiver_ref,
        instance,
        account_scope,
    )
    return TelegramComponents(transport, media)


__all__ = ["TelegramComponents", "build_default_components"]
