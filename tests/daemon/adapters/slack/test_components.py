from __future__ import annotations

import asyncio

import httpx

from hyprial.daemon.impl.adapters.slack import build_default_components

from .support import BOT_TOKEN, Credentials, Ledger


def test_default_bundle_captures_official_endpoint_and_owns_exactly_one_client() -> (
    None
):
    async def scenario() -> None:
        requests: list[httpx.Request] = []

        async def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={"ok": True, "team_id": "T1", "bot_id": "BOT1"},
                request=request,
            )

        bundle = build_default_components(
            checkpoints=Ledger(),
            credentials=Credentials(),
            receiver_ref="receiver:slack",
            organization_id="org-1",
            instance_id="slack-1",
            local_name="main",
            media_authority=None,
            media_scope_resolver=None,
            http_transport=httpx.MockTransport(respond),
        )
        assert bundle.transport.client is bundle.media.client
        await bundle.transport.authenticate(
            await Credentials().resolve("receiver:slack")
        )
        assert requests[0].url == httpx.URL("https://slack.com/api/auth.test")
        assert requests[0].headers["authorization"] == f"Bearer {BOT_TOKEN}"
        assert not requests[0].url.params
        outcome = await bundle.transport.aclose(asyncio.get_running_loop().time() + 1)
        assert outcome.client_closed is True
        assert bundle.transport.client.is_closed

    asyncio.run(scenario())
