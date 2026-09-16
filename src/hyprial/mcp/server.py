"""Official MCP Python SDK facade for Harness daemon operations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from pydantic import Field

from hyprial import __version__
from hyprial.contracts.session import session_fetch_params

from .proxy import StatelessDaemonProxy

if TYPE_CHECKING:
    from .channel import ClaudeChannelAdapter

NonEmpty = Annotated[str, Field(min_length=1)]


def create_mcp_server(
    proxy: StatelessDaemonProxy,
    *,
    actor: str,
    session_ref: str,
    channel_adapter: ClaudeChannelAdapter | None = None,
) -> MCPServer:
    """Build the SDK high-level server (called FastMCP before SDK 2.0)."""

    if not actor or not session_ref:
        raise ValueError("MCP server requires a fixed actor and session_ref")

    server = MCPServer(
        "harness-bridge",
        version=__version__,
        instructions=(
            "Harness inbox state is daemon-authoritative. Read before replying, "
            "and acknowledge terminal messages that require no reply."
        ),
        log_level="WARNING",
    )

    async def invoke(
        ctx: Context, method: str, params: dict[str, Any], *, mutation: bool
    ) -> dict[str, Any]:
        return await proxy.call(
            actor=actor,
            session_ref=session_ref,
            method=method,
            params=params,
            mutation=mutation,
        )

    @server.tool(
        description=(
            "Send one independent durable asynchronous Harness Network message "
            "to an explicit agent, user:<owner>, or route:<adapter>:<route> "
            "target. User delivery is performed by the receiving user's own "
            "Squire adapter; route delivery posts to the Lark chat configured "
            "as that route's nativeId and requires the adapter's app to be a "
            "member of the chat. Any other address scheme is rejected."
        )
    )
    async def harness_send(
        to: NonEmpty, message: NonEmpty, ctx: Context
    ) -> dict[str, Any]:
        # The tool contract is one explicit target; the daemon contract is a
        # non-empty target array. Wrap here so the string schema never reaches
        # the daemon unwrapped (INVALID_ARGUMENT: to must be a non-empty array).
        return await invoke(
            ctx, "message.send", {"to": [to], "message": message}, mutation=True
        )

    @server.tool(
        description=(
            "Reply to one pending Harness request and acknowledge it only after "
            "the daemon durably accepts the reply."
        )
    )
    async def harness_reply(
        messageId: NonEmpty, message: NonEmpty, ctx: Context
    ) -> dict[str, Any]:
        if channel_adapter is not None:
            return await channel_adapter.harness_reply(messageId, message)
        return await invoke(
            ctx,
            "message.reply",
            {"messageId": messageId, "message": message},
            mutation=True,
        )

    @server.tool(
        description=(
            "Read the daemon-owned durable inbox. Messages remain pending until "
            "harness_reply or harness_ack succeeds."
        )
    )
    async def harness_read(ctx: Context) -> dict[str, Any]:
        if channel_adapter is not None:
            return await channel_adapter.harness_read()
        return await invoke(
            ctx, "message.pending.list", session_fetch_params(), mutation=False
        )

    @server.tool(
        description=(
            "List non-authoritative progress events for this actor. Events are "
            "self-contained, may have sequence gaps, are read non-destructively, "
            "and never replace the terminal Harness reply."
        )
    )
    async def harness_progress(
        ctx: Context,
        deliveryId: str | None = None,
        sinceSeq: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if deliveryId is not None:
            params["deliveryId"] = deliveryId
        if sinceSeq is not None:
            params["sinceSeq"] = sinceSeq
        return await invoke(ctx, "progress.list", params, mutation=False)

    @server.tool(
        description=(
            "Acknowledge one pending Harness message without replying. Use for "
            "terminal replies or events that require no response."
        )
    )
    async def harness_ack(messageId: NonEmpty, ctx: Context) -> dict[str, Any]:
        if channel_adapter is not None:
            return await channel_adapter.harness_ack(messageId)
        return await invoke(ctx, "message.ack", {"messageId": messageId}, mutation=True)

    @server.tool(description="Inspect the authenticated Harness actor identity.")
    async def harness_whoami(ctx: Context) -> dict[str, Any]:
        return await invoke(ctx, "identity.whoami", {}, mutation=False)

    @server.tool(description="List live Harness targets visible to this actor.")
    async def harness_targets(ctx: Context, kind: str | None = None) -> dict[str, Any]:
        # Only kinds the daemon can actually list are accepted.  targets is
        # the delivery-promise view: agents, users (squire DM with sender
        # attribution), and channel routes (configured outbound posts with
        # the same attribution).  Nodes are observable via `hyprial hosts`.
        if kind not in {None, "agent", "user", "channel_route"}:
            raise ValueError(
                "harness_targets kind must be agent, user, or channel_route"
            )
        # The daemon registers this as "targets" (see cli.py); "targets.list"
        # is METHOD_NOT_FOUND.
        return await invoke(
            ctx,
            "targets",
            {} if kind is None else {"kind": kind},
            mutation=False,
        )

    # NOTE: no harness_delegate tool. Structured delegation (defer the parent
    # turn, resume it with the child result) is a stateful subsystem that this
    # runtime does not implement: the daemon has no message.delegate dispatch
    # branch nor the delegation.inspect/observe/complete methods, and there is
    # no harness-side turn suppression/resume plumbing. The prior tool also
    # could not have driven a faithful implementation: it forwarded only
    # {to, message} with no parentMessageId to bind the child to the deferred
    # parent. It is intentionally not advertised rather than shipped broken
    # (it invoked message.delegate -> METHOD_NOT_FOUND). If delegation is
    # wanted later, add the full subsystem and reintroduce the tool with a
    # parent-request-bound contract.

    return server


def create_channel_mcp_server(adapter: ClaudeChannelAdapter) -> MCPServer:
    """Build the per-session stdio server with fixed, non-header identity."""

    return create_mcp_server(
        adapter.proxy,
        actor=adapter.actor,
        session_ref=adapter.session_ref,
        channel_adapter=adapter,
    )


async def serve_worker_stdio(
    proxy: StatelessDaemonProxy, *, actor: str, session_ref: str
) -> None:
    """Serve harness tools over stdio with a fixed, daemon-pinned identity.

    Unlike the interactive Claude Channel server, a managed worker does NOT
    register an interactive session or run a wake loop: the daemon that launched
    the worker already owns the worker's canonical actor route and has recorded
    the worker's ``(actor, sessionRef)`` as a session its fence accepts.  This
    server only signs the worker's own tool calls with that fixed identity.
    """

    if not actor or not session_ref:
        raise ValueError("worker stdio server requires actor and session_ref")
    server = create_mcp_server(proxy, actor=actor, session_ref=session_ref)
    await server.run_stdio_async()
