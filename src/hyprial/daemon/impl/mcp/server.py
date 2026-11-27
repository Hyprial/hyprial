# Decision record (OrgFS package D4b, 2026-11-25, design §1.1 / §1.2):
# ``orgfs_remove`` takes ``permanent`` (default false, a soft removal) and
# always forwards it; ``orgfs_restore`` takes no ``version``.
"""Official MCP Python SDK facade for Harness daemon operations.

Decision (2026-11-19, slice GH): an agent reads a conversation's history,
above all a group's it was not woken for, through ``harness_read`` with a
``conversationId``; the tool then calls ``conversations.history`` signed
with this server's fixed ``(actor, sessionRef)``, so the daemon's session
fence and the group membership check decide.  No new tool: every harness
already allows ``harness_read`` and none of the other tools takes a
conversation id.  That branch never goes through the channel adapter, whose
``harness_read`` takes custody of the inbox and opens a turn; without
``conversationId`` the tool is the inbox batch read it always was.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import TYPE_CHECKING, Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from hyprial import __version__
from hyprial.kernel import IPC_MESSAGE_READ_BODY, ipc_errors

from hyprial.daemon.impl.mcp.api  import DaemonRequestRejected
from hyprial.daemon.impl.mcp.proxy  import StatelessDaemonProxy

if TYPE_CHECKING:
    from hyprial.daemon.impl.mcp.channel.adapter  import ClaudeChannelAdapter

NonEmpty = Annotated[str, Field(min_length=1)]
ExpansionText = Annotated[str, Field(min_length=1, max_length=65_536)]


def _bounded_expansion(value: str) -> str:
    """Enforce the wire bound in bytes, not only the MCP character schema."""

    if len(value.encode("utf-8")) > 65_536:
        raise ToolError("PAC_EXPANSION_INVALID: expansion exceeds 65536 UTF-8 bytes")
    return value


def _plain_stderr_logging() -> None:
    """Claim the root logger before the MCP SDK installs rich logging.

    The SDK's ``configure_logging`` calls ``logging.basicConfig`` with a
    ``RichHandler``.  Rich renders lazily, importing submodules on the first
    record, and an in-place ``hyprial daemon upgrade`` swaps the package files under
    a running channel: that first warning then raised ModuleNotFoundError out
    of ``logger.warning`` and killed the channel (E2E-010).  A plain stream
    handler imports nothing at log time; with a root handler present the
    SDK's ``basicConfig`` is a no-op.
    """

    root = logging.getLogger()
    # Embedders may have configured Rich before constructing this server.
    # Replace those handlers too: leaving even one attached lets its lazy
    # render imports kill the same warning despite a plain handler beside it.
    for existing in tuple(root.handlers):
        if any(
            cls.__module__ == "rich.logging" and cls.__name__ == "RichHandler"
            for cls in type(existing).__mro__
        ):
            replacement = logging.StreamHandler(sys.stderr)
            replacement.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
            replacement.setLevel(existing.level)
            for log_filter in existing.filters:
                replacement.addFilter(log_filter)
            root.removeHandler(existing)
            root.addHandler(replacement)
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.WARNING)


def create_mcp_server(
    proxy: StatelessDaemonProxy,
    *,
    actor: str,
    session_ref: str,
    channel_adapter: ClaudeChannelAdapter | None = None,
    workflow_tools: bool = False,
) -> MCPServer:
    """Build the SDK high-level server (called FastMCP before SDK 2.0).

    ``workflow_tools`` adds ``workflow_complete`` / ``workflow_fail`` for a
    daemon-managed worker: a headless Agent SDK worker is pre-authorized for
    this server's tools only, so without them it could not flip its own PAC
    node -- ``hyprial workflow complete`` needs Bash, which it may not run.
    They sign with the same fixed ``(actor, sessionRef)`` the CLI sends, so
    the daemon's owner fence is unchanged: a worker completes only its own
    node.
    """

    if not actor or not session_ref:
        raise ValueError("MCP server requires a fixed actor and session_ref")

    _plain_stderr_logging()
    server = MCPServer(
        "harness-bridge",
        version=__version__,
        instructions=(
            "Harness inbox state is daemon-authoritative. Read before replying, "
            "and acknowledge terminal messages that require no reply."
        ),
        log_level="WARNING",
    )

    def refuse_stale_channel() -> None:
        """Surface a version/schema-skewed stdio channel as a stable error.

        The refusal is minted by the daemon at registration and carried back
        by the channel; without it the old process would keep failing at the
        MCP schema edge (for example SUMMARY_REQUIRED with no remedy).
        """

        if channel_adapter is None:
            return
        stale = channel_adapter.stale_error()
        if stale is not None:
            raise ToolError(json.dumps(stale, ensure_ascii=False, sort_keys=True))

    async def invoke(
        ctx: Context, method: str, params: dict[str, Any], *, mutation: bool
    ) -> dict[str, Any]:
        refuse_stale_channel()
        try:
            return await proxy.call(
                actor=actor,
                session_ref=session_ref,
                method=method,
                params=params,
                mutation=mutation,
            )
        except DaemonRequestRejected as exc:
            if exc.code not in {
                ipc_errors.ORGFS_CONTENT_PENDING,
                ipc_errors.SUBMIT_OUTCOME_UNKNOWN,
            }:
                raise
            raise ToolError(
                json.dumps(
                    {"code": exc.code, "data": exc.data},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ) from exc

    @server.tool(
        description=(
            "Send one independent durable asynchronous Harness Network message "
            "to one or more explicit agent or user:<owner> targets. "
            "``to`` may be an array for "
            "fan-out. ``summary`` is required and is the only part the "
            "recipient's context shows first (at most 512 UTF-8 bytes). How a "
            "``user:`` target is reached is decided by the receiving side, not "
            "by this contract: the result reports the path the message took in "
            "``deliveries``, and callers must not assume one. "
            "Any other address scheme is rejected. "
            "A send is never a reply: to answer a request use harness_reply, "
            "otherwise the request stays open."
        )
    )
    async def harness_send(
        to: NonEmpty | Annotated[list[NonEmpty], Field(min_length=1)],
        summary: NonEmpty,
        message: NonEmpty,
        ctx: Context,
    ) -> dict[str, Any]:
        # The tool contract is one target or a non-empty list; the daemon
        # contract is always a non-empty target array. Wrap here so the string
        # schema never reaches the daemon unwrapped (INVALID_ARGUMENT: to must
        # be a non-empty array).
        targets = [to] if isinstance(to, str) else list(to)
        params: dict[str, Any] = {"to": targets, "summary": summary, "message": message}
        return await invoke(ctx, "message.send", params, mutation=True)

    @server.tool(
        description=(
            "Reply to one pending Harness request and acknowledge it only after "
            "the daemon durably accepts the reply. ``summary`` is required and "
            "is what the recipient sees first. Only a reply closes a request. "
            "In a group conversation (g.…), the reply mentions and wakes the "
            "replied-to author by default when that author is another current "
            "agent member (never the human owner, self, or a removed member); "
            "pass ``mentions`` to add member ids or ``noMention=True`` to "
            "suppress the default mention. "
        )
    )
    async def harness_reply(
        messageId: NonEmpty,
        summary: NonEmpty,
        message: NonEmpty,
        ctx: Context,
        mentions: list[NonEmpty] | None = None,
        noMention: bool = False,
    ) -> dict[str, Any]:
        if channel_adapter is not None:
            refuse_stale_channel()
            return await channel_adapter.harness_reply(
                messageId,
                summary,
                message,
                mentions=mentions,
                no_mention=noMention,
            )
        params: dict[str, Any] = {
            "messageId": messageId,
            "summary": summary,
            "message": message,
        }
        if mentions:
            params["mentions"] = list(mentions)
        if noMention:
            params["noMention"] = True
        return await invoke(
            ctx,
            "message.reply",
            params,
            mutation=True,
        )

    @server.tool(
        description=(
            "Read one message body by ID. A consuming action: the daemon "
            "records it as fetched, but the message stays pending until "
            "harness_reply or harness_ack."
        )
    )
    async def harness_read_body(
        messageId: NonEmpty, ctx: Context
    ) -> dict[str, Any]:
        return await invoke(
            ctx,
            IPC_MESSAGE_READ_BODY,
            {"messageId": messageId},
            mutation=True,
        )

    @server.tool(
        description=(
            "Read the daemon-owned durable inbox as one coalesced batch "
            "(inbox-batch-v1): every pending item with its full body, or the "
            "one private spill file when it exceeds the inline budget. Inject "
            "delivery.text as-is, settle every listed messageId separately, "
            "and do not re-read bodies that are already inline. With "
            "``conversationId`` (a group's g. id, or a c. id) it instead reads "
            "one page of that conversation's history and settles nothing: "
            "``mode`` tail (default), older or after, ``cursor`` from the "
            "previous page's nextOlderCursor or nextCursor, ``limit`` 1-200. "
            "A group's history is readable only by its current members."
        )
    )
    async def harness_read(
        ctx: Context,
        conversationId: NonEmpty | None = None,
        cursor: NonEmpty | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] | None = None,
        mode: Literal["tail", "older", "after"] | None = None,
    ) -> dict[str, Any]:
        page = {
            name: value
            for name, value in (("cursor", cursor), ("limit", limit), ("mode", mode))
            if value is not None
        }
        if conversationId is not None:
            return await invoke(
                ctx, "conversations.history",
                {"conversationId": conversationId, **page}, mutation=False,
            )
        if page:
            raise ToolError("cursor, limit and mode page a conversation: pass conversationId")
        if channel_adapter is not None:
            refuse_stale_channel()
            return await channel_adapter.harness_read()
        return await invoke(ctx, "message.pending.batch", {}, mutation=False)

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
            refuse_stale_channel()
            return await channel_adapter.harness_ack(messageId)
        return await invoke(ctx, "message.ack", {"messageId": messageId}, mutation=True)

    @server.tool(description="Inspect the authenticated Harness actor identity.")
    async def harness_whoami(ctx: Context) -> dict[str, Any]:
        return await invoke(ctx, "identity.whoami", {}, mutation=False)

    @server.tool(description="List live Harness targets visible to this actor.")
    async def harness_targets(ctx: Context, kind: str | None = None) -> dict[str, Any]:
        if kind not in {None, "agent", "user"}:
            raise ValueError("harness_targets kind must be agent or user")
        # The daemon registers this as "targets" (see cli.py); "targets.list"
        # is METHOD_NOT_FOUND.
        return await invoke(
            ctx,
            "targets",
            {} if kind is None else {"kind": kind},
            mutation=False,
        )

    @server.tool(description="List orgfs spaces joined on this node.")
    async def orgfs_spaces(ctx: Context) -> dict[str, Any]:
        return await invoke(ctx, "orgfs.spaces", {}, mutation=False)

    @server.tool(description="Create an orgfs shared folder.")
    async def orgfs_create(name: NonEmpty, ctx: Context) -> dict[str, Any]:
        return await invoke(ctx, "orgfs.create", {"name": name}, mutation=True)

    @server.tool(description="Resolve an orgfs path, including ambiguous same-name nodes; path may be id:<nodeId> or a full orgfs:<owner>:<spaceId>:<nodeId> URI (must match spaceId).")
    async def orgfs_resolve(spaceId: NonEmpty, path: str, ctx: Context) -> dict[str, Any]:
        return await invoke(ctx, "orgfs.resolve", {"spaceId": spaceId, "path": path}, mutation=False)

    @server.tool(description="List an orgfs directory; path may be id:<nodeId> or a full orgfs:<owner>:<spaceId>:<nodeId> URI (must match spaceId).")
    async def orgfs_ls(spaceId: NonEmpty, path: str, ctx: Context) -> dict[str, Any]:
        return await invoke(ctx, "orgfs.ls", {"spaceId": spaceId, "path": path}, mutation=False)

    @server.tool(description="Stat an orgfs node; node may be id:<nodeId> or a full orgfs:<owner>:<spaceId>:<nodeId> URI (must match spaceId).")
    async def orgfs_stat(spaceId: NonEmpty, node: NonEmpty, ctx: Context) -> dict[str, Any]:
        return await invoke(ctx, "orgfs.stat", {"spaceId": spaceId, "node": node}, mutation=False)

    @server.tool(description="Read an orgfs text or small binary node; node may be id:<nodeId> or a full orgfs:<owner>:<spaceId>:<nodeId> URI (must match spaceId).")
    async def orgfs_read(
        spaceId: NonEmpty,
        node: NonEmpty,
        ctx: Context,
        waitSeconds: float = 10.0,
    ) -> dict[str, Any]:
        return await invoke(
            ctx,
            "orgfs.read",
            {"spaceId": spaceId, "node": node, "waitSeconds": waitSeconds},
            mutation=False,
        )

    @server.tool(
        description=(
            "Export an orgfs node to a local file. The destination must be an "
            "absolute path inside your workspace or session working directory; "
            "an existing file is replaced only with overwrite=true."
        )
    )
    async def orgfs_export(
        spaceId: NonEmpty,
        node: NonEmpty,
        destination: NonEmpty,
        ctx: Context,
        waitSeconds: float = 10.0,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        return await invoke(
            ctx,
            "orgfs.export",
            {
                "spaceId": spaceId,
                "node": node,
                "destination": destination,
                "waitSeconds": waitSeconds,
                "overwrite": overwrite,
                # The local-file MethodSpec declares ``from`` as its selector;
                # MCP's operator-equivalent claim is the fixed session actor
                # the proxy also appends.
                "from": actor,
            },
            mutation=True,
        )

    @server.tool(
        description=(
            "Import a local file into orgfs. The source must be an absolute "
            "path inside your workspace or session working directory."
        )
    )
    async def orgfs_import(
        spaceId: NonEmpty, node: NonEmpty, source: NonEmpty, ctx: Context
    ) -> dict[str, Any]:
        return await invoke(
            ctx,
            "orgfs.import",
            {
                "spaceId": spaceId,
                "node": node,
                "source": source,
                "from": actor,
            },
            mutation=True,
        )

    @server.tool(description="Write orgfs text with optional merge and strict versions; node may be id:<nodeId> or a full orgfs:<owner>:<spaceId>:<nodeId> URI (must match spaceId).")
    async def orgfs_write(
        spaceId: NonEmpty,
        node: NonEmpty,
        text: str,
        ctx: Context,
        baseVersion: str | None = None,
        expectVersion: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"spaceId": spaceId, "node": node, "text": text}
        if baseVersion is not None:
            params["baseVersion"] = baseVersion
        if expectVersion is not None:
            params["expectVersion"] = expectVersion
        return await invoke(ctx, "orgfs.write", params, mutation=True)

    @server.tool(description="Create an orgfs directory.")
    async def orgfs_mkdir(spaceId: NonEmpty, path: NonEmpty, ctx: Context) -> dict[str, Any]:
        return await invoke(ctx, "orgfs.mkdir", {"spaceId": spaceId, "path": path}, mutation=True)

    @server.tool(description="Move or rename one orgfs node; sourcePath/destinationPath may be id:<nodeId> or a full orgfs:<owner>:<spaceId>:<nodeId> URI (must match spaceId).")
    async def orgfs_move(
        spaceId: NonEmpty, sourcePath: NonEmpty, destinationPath: NonEmpty, ctx: Context
    ) -> dict[str, Any]:
        return await invoke(
            ctx,
            "orgfs.move",
            {"spaceId": spaceId, "sourcePath": sourcePath, "destinationPath": destinationPath},
            mutation=True,
        )

    @server.tool(description="Remove an orgfs node into the trash, or delete it for good with permanent=true; node may be id:<nodeId> or a full orgfs:<owner>:<spaceId>:<nodeId> URI (must match spaceId).")
    async def orgfs_remove(
        spaceId: NonEmpty, node: NonEmpty, ctx: Context, permanent: bool = False
    ) -> dict[str, Any]:
        return await invoke(
            ctx,
            "orgfs.remove",
            {"spaceId": spaceId, "node": node, "permanent": permanent},
            mutation=True,
        )

    @server.tool(description="List soft-deleted orgfs nodes.")
    async def orgfs_trash(spaceId: NonEmpty, ctx: Context, limit: int = 100) -> dict[str, Any]:
        return await invoke(ctx, "orgfs.trash", {"spaceId": spaceId, "limit": limit}, mutation=False)

    @server.tool(description="Restore a removed orgfs node from the trash as a new attributed operation; node may be id:<nodeId> or a full orgfs:<owner>:<spaceId>:<nodeId> URI (must match spaceId).")
    async def orgfs_restore(
        spaceId: NonEmpty,
        node: NonEmpty,
        ctx: Context,
        recursive: bool = True,
    ) -> dict[str, Any]:
        return await invoke(
            ctx,
            "orgfs.restore",
            {"spaceId": spaceId, "node": node, "recursive": recursive},
            mutation=True,
        )

    @server.tool(description="Poll orgfs changes since a version.")
    async def orgfs_watch(
        spaceId: NonEmpty, ctx: Context, glob: str = "*", sinceVersion: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"spaceId": spaceId, "glob": glob}
        if sinceVersion is not None:
            params["sinceVersion"] = sinceVersion
        return await invoke(ctx, "orgfs.watch", params, mutation=False)

    @server.tool(description="Show orgfs sync state, online holders, pending content and rejected updates.")
    async def orgfs_status(spaceId: NonEmpty, ctx: Context) -> dict[str, Any]:
        return await invoke(ctx, "orgfs.status", {"spaceId": spaceId}, mutation=False)

    @server.tool(description="Invite a user into an orgfs space; inbox notification is a separate send.")
    async def orgfs_invite(
        spaceId: NonEmpty, user: NonEmpty, ctx: Context, mode: str = "rw"
    ) -> dict[str, Any]:
        return await invoke(
            ctx, "orgfs.invite", {"spaceId": spaceId, "user": user, "mode": mode}, mutation=True
        )

    @server.tool(description="Remove an orgfs member from future writes.")
    async def orgfs_remove_member(
        spaceId: NonEmpty, user: NonEmpty, ctx: Context
    ) -> dict[str, Any]:
        return await invoke(
            ctx, "orgfs.remove_member", {"spaceId": spaceId, "user": user}, mutation=True
        )

    @server.tool(description="List current orgfs members.")
    async def orgfs_members(spaceId: NonEmpty, ctx: Context) -> dict[str, Any]:
        return await invoke(ctx, "orgfs.members", {"spaceId": spaceId}, mutation=False)

    @server.tool(description="Join an invited orgfs space by cloning from an online holder.")
    async def orgfs_join(spaceId: NonEmpty, ctx: Context) -> dict[str, Any]:
        return await invoke(ctx, "orgfs.join", {"spaceId": spaceId}, mutation=True)

    if workflow_tools:

        @server.tool(
            description=(
                "Preview a PAC child expansion for one existing graph placeholder. "
                "The daemon validates the expansion against the authenticated "
                "creator or planner and creates no graph, lease, or worker."
            )
        )
        async def workflow_expansion_plan(
            graphId: NonEmpty,
            nodeId: NonEmpty,
            yaml: ExpansionText,
            ctx: Context,
        ) -> dict[str, Any]:
            return await invoke(
                ctx,
                "workflow.expansion.plan",
                {
                    "graphId": graphId,
                    "nodeId": nodeId,
                    "yaml": _bounded_expansion(yaml),
                },
                mutation=False,
            )

        @server.tool(
            description=(
                "Complete your own PAC workflow node against its exact current "
                "request (the Graph, node and request-id from the dispatch "
                "message). reasonRef names the completion evidence and optional "
                "outputText stores the result inline. A reply is "
                "not completion; call this when the node's work is done."
            )
        )
        async def workflow_complete(
            graphId: NonEmpty,
            nodeId: NonEmpty,
            requestId: NonEmpty,
            reasonRef: NonEmpty,
            ctx: Context,
            outputText: str | None = None,
            expansion: ExpansionText | None = None,
        ) -> dict[str, Any]:
            return await invoke(
                ctx,
                "workflow.complete",
                {
                    "graphId": graphId,
                    "nodeId": nodeId,
                    "requestId": requestId,
                    "reasonRef": reasonRef,
                    **({"outputText": outputText} if outputText is not None else {}),
                    **(
                        {"expansion": _bounded_expansion(expansion)}
                        if expansion is not None
                        else {}
                    ),
                },
                mutation=True,
            )

        @server.tool(
            description=(
                "Report explicit failure of your own PAC workflow node against "
                "its exact current request; the graph's declared failure "
                "policy applies. reasonRef names the failure evidence and "
                "optional outputText stores the result inline."
            )
        )
        async def workflow_fail(
            graphId: NonEmpty,
            nodeId: NonEmpty,
            requestId: NonEmpty,
            reasonRef: NonEmpty,
            ctx: Context,
            outputText: str | None = None,
        ) -> dict[str, Any]:
            return await invoke(
                ctx,
                "workflow.fail",
                {
                    "graphId": graphId,
                    "nodeId": nodeId,
                    "requestId": requestId,
                    "reasonRef": reasonRef,
                    **({"outputText": outputText} if outputText is not None else {}),
                },
                mutation=True,
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
    server = create_mcp_server(
        proxy, actor=actor, session_ref=session_ref, workflow_tools=True
    )
    await server.run_stdio_async()
