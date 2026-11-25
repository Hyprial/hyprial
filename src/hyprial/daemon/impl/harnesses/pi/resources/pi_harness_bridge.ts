/**
 * hyprial harness-bridge extension for daemon-managed headless pi workers.
 *
 * Pi has no MCP support by design, so a managed pi worker cannot receive the
 * harness-bridge MCP server that a Claude worker gets.  This extension is the
 * pi carrier for the same capability: it registers the harness_* toolset and
 * answers each call with ONE request against the owning daemon's version-1
 * newline-delimited JSON IPC (the exact wire `hyprial.mcp.unix.UnixDaemonConnection`
 * speaks), signed with the worker's own canonical actor + session ref.
 *
 * Identity and daemon pinning arrive through the environment the launcher
 * injects (see `WorkerChannel.pi_environment`):
 *
 *   HYPRIAL_WORKER_ACTOR        the worker's canonical agent URI
 *   HYPRIAL_WORKER_SESSION_REF  the session ref the daemon's fence accepts
 *   HYPRIAL_HOME / HARNESS_STATE_DIR  locate THIS daemon's socket, never an
 *                           ambient production daemon
 *
 * Without HYPRIAL_WORKER_* the tools stay registered but fail closed with a clear
 * error, so loading the extension outside a managed launch is harmless.  The
 * factory performs no I/O: the daemon may be absent at startup.
 *
 * Decision (2026-11-19, slice GH): harness_read with a conversationId reads one
 * page of conversations.history under this worker's session, as the MCP
 * server's harness_read does (see mcp/server.py); without it, the inbox batch.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import {
  callDaemon, DaemonIpcError, identityFromEnv, type DaemonResult,
} from "./pi_daemon_ipc.ts";

function textResult(value: DaemonResult) {
  return {
    content: [{ type: "text" as const, text: JSON.stringify(value, null, 2) }],
    details: value,
    ...(value.ok === false ? { isError: true } : {}),
  };
}

function errorResult(error: unknown) {
  const message = error instanceof Error ? error.message : String(error);
  const details = {
    error: message,
    ...(error instanceof DaemonIpcError ? {
      code: error.code,
      ...(error.data !== undefined ? { data: error.data } : {}),
    } : {}),
  };
  return {
    content: [{
      type: "text" as const,
      text: error instanceof DaemonIpcError && error.data !== undefined
        ? JSON.stringify(details, null, 2)
        : message,
    }],
    details,
    isError: true,
  };
}

const UNMANAGED =
  "harness tools are unavailable: HYPRIAL_WORKER_ACTOR / HYPRIAL_WORKER_SESSION_REF " +
  "are not set, so this pi session was not launched as a managed hyprial worker";

/** Lets a carrier see every tool call's verdict on the session. */
export interface BridgeObserver {
  result(result: Record<string, unknown>): void;
  rejection(message: string): void;
}

export default function (pi: ExtensionAPI, observer?: BridgeObserver) {
  /** Shared call path: identity check, daemon call, error mapping. */
  async function invoke(
    method: string,
    params: Record<string, unknown>,
    mutation: boolean,
  ) {
    const identity = identityFromEnv();
    if (identity === null) return errorResult(UNMANAGED);
    try {
      const result = await callDaemon(
        method,
        { actor: identity.actor, sessionRef: identity.sessionRef, ...params },
        mutation,
      );
      observer?.result(result);
      return textResult(result);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      observer?.rejection(message);
      return errorResult(error);
    }
  }

  pi.registerTool({
    name: "harness_send",
    label: "Harness Send",
    description:
      "Send one independent durable asynchronous Harness Network message to one " +
      "or more explicit agent or user:<owner> targets. " +
      "``to`` may be an array for fan-out. ``summary`` is required and is the only " +
      "part the recipient's context shows first (at most 512 UTF-8 bytes). User " +
      "delivery is performed by the receiving user's own Squire adapter. Any " +
      "other address scheme is rejected. A send is never a reply: to answer a request use " +
      "harness_reply, otherwise the request stays open.",
    parameters: Type.Object({
      to: Type.Union([
        Type.String({
          description: "Target agent URI or user:<owner>",
        }),
        Type.Array(
          Type.String({ description: "One target URI" }),
          { minItems: 1, description: "One or more target URIs (fan-out)" },
        ),
      ]),
      summary: Type.String({
        description: "Short summary the recipient reads first; required",
      }),
      message: Type.String({ description: "Message text" }),
    }),
    // The MCP contract is one target or a non-empty list; the daemon contract
    // is always a non-empty target array. Wrap here, mirroring server.py.
    async execute(_toolCallId, params) {
      const targets = typeof params.to === "string" ? [params.to] : params.to;
      return invoke(
        "message.send",
        { to: targets, summary: params.summary, message: params.message },
        true,
      );
    },
  });

  pi.registerTool({
    name: "harness_reply",
    label: "Harness Reply",
    description:
      "Reply to one pending Harness request and acknowledge it only after the " +
      "daemon durably accepts the reply. ``summary`` is required and is what the " +
      "recipient sees first. Only a reply closes a request. In a group " +
      "conversation (g.…), the reply mentions and wakes the replied-to author " +
      "by default when that author is another current agent member (never the " +
      "human owner, self, or a removed member); pass mentions to add member " +
      "ids or noMention=true to suppress the default mention.",
    parameters: Type.Object({
      messageId: Type.String({ description: "Pending message ID to reply to" }),
      summary: Type.String({
        description: "Short summary the recipient reads first; required",
      }),
      message: Type.String({ description: "Reply text" }),
      mentions: Type.Optional(
        Type.Array(Type.String(), {
          description:
            "Optional group member ids to additionally mention in a group reply",
        }),
      ),
      noMention: Type.Optional(
        Type.Boolean({
          description:
            "Set true in a group reply to omit mentioning the replied-to author by default",
        }),
      ),
    }),
    async execute(_toolCallId, params) {
      const requestParams: Record<string, unknown> = {
        messageId: params.messageId,
        summary: params.summary,
        message: params.message,
      };
      if (params.mentions && params.mentions.length > 0) {
        requestParams.mentions = params.mentions;
      }
      if (params.noMention === true) {
        requestParams.noMention = true;
      }
      return invoke("message.reply", requestParams, true);
    },
  });

  pi.registerTool({
    name: "harness_read_body",
    label: "Harness Read Body",
    description:
      "Read one message body by ID. A consuming action: the daemon records it " +
      "as fetched, but the message stays pending until harness_reply or " +
      "harness_ack.",
    parameters: Type.Object({
      messageId: Type.String({ description: "Message ID whose body to read" }),
    }),
    async execute(_toolCallId, params) {
      return invoke("message.read_body", { messageId: params.messageId }, true);
    },
  });

  pi.registerTool({
    name: "harness_read",
    label: "Harness Read",
    description:
      "Read the daemon-owned durable inbox as one coalesced batch " +
      "(inbox-batch-v1): every pending item with its full body, or the single " +
      "private spill file when it exceeds the inline budget. Inject " +
      "delivery.text as-is and settle every listed messageId separately with " +
      "harness_reply or harness_ack; do not re-read a body that is inline. " +
      "With conversationId (a group's g. id, or a c. id) it instead reads one " +
      "page of that conversation's history and settles nothing: mode tail " +
      "(default), older or after, cursor from the previous page's " +
      "nextOlderCursor or nextCursor, limit 1-200. A group's history is " +
      "readable only by its current members.",
    parameters: Type.Object({
      conversationId: Type.Optional(Type.String({
        minLength: 1, description: "Conversation whose history to read (g. or c. id)",
      })),
      cursor: Type.Optional(Type.String({ minLength: 1, description: "Page cursor" })),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 200, description: "Page size" })),
      mode: Type.Optional(Type.Union([
        Type.Literal("tail"), Type.Literal("older"), Type.Literal("after"),
      ])),
    }),
    async execute(_toolCallId, params) {
      const page: Record<string, unknown> = {};
      if (params.cursor !== undefined) page.cursor = params.cursor;
      if (params.limit !== undefined) page.limit = params.limit;
      if (params.mode !== undefined) page.mode = params.mode;
      if (params.conversationId !== undefined) {
        return invoke(
          "conversations.history",
          { conversationId: params.conversationId, ...page },
          false,
        );
      }
      if (Object.keys(page).length > 0) {
        return errorResult("cursor, limit and mode page a conversation: pass conversationId");
      }
      // One daemon-rendered batch; this extension never formats a message.
      return invoke("message.pending.batch", {}, false);
    },
  });

  pi.registerTool({
    name: "harness_progress",
    label: "Harness Progress",
    description:
      "List non-authoritative progress events for this actor. Events are " +
      "self-contained, may have sequence gaps, are read non-destructively, and " +
      "never replace the terminal Harness reply.",
    parameters: Type.Object({
      deliveryId: Type.Optional(Type.String({ description: "Filter by delivery ID" })),
      sinceSeq: Type.Optional(
        Type.Integer({ description: "Only events with seq greater than this value" }),
      ),
    }),
    async execute(_toolCallId, params) {
      const query: Record<string, unknown> = {};
      if (params.deliveryId !== undefined) query.deliveryId = params.deliveryId;
      if (params.sinceSeq !== undefined) query.sinceSeq = params.sinceSeq;
      return invoke("progress.list", query, false);
    },
  });

  pi.registerTool({
    name: "harness_ack",
    label: "Harness Ack",
    description:
      "Acknowledge one pending Harness message without replying. Use for " +
      "terminal replies or events that require no response.",
    parameters: Type.Object({
      messageId: Type.String({ description: "Pending message ID to acknowledge" }),
    }),
    async execute(_toolCallId, params) {
      return invoke("message.ack", { messageId: params.messageId }, true);
    },
  });

  pi.registerTool({
    name: "harness_whoami",
    label: "Harness Whoami",
    description: "Inspect the authenticated Harness actor identity.",
    parameters: Type.Object({}),
    async execute() {
      return invoke("identity.whoami", {}, false);
    },
  });

  pi.registerTool({
    name: "harness_targets",
    label: "Harness Targets",
    description: "List live Harness targets visible to this actor.",
    parameters: Type.Object({
      kind: Type.Optional(
        Type.String({ description: "Filter: agent or user" }),
      ),
    }),
    async execute(_toolCallId, params) {
      if (params.kind !== undefined && !["agent", "user"].includes(params.kind)) {
        return errorResult("harness_targets kind must be agent or user");
      }
      return invoke(
        "targets",
        params.kind === undefined ? {} : { kind: params.kind },
        false,
      );
    },
  });
}
