/**
 * hyprial interactive attach carrier for pi TUI sessions.
 *
 * The headless RPC connector owns pi's stdin/stdout, so the daemon can drive
 * turns directly; an interactive TUI owns its own terminal, and the ONLY way
 * a Harness message reaches the model is an in-process extension injecting a
 * real user message. This extension is that carrier. It:
 *
 *   1. registers the session with the daemon on `session_start`
 *      (source "pi-extension", runtime "pi_interactive") — the interactive
 *      session protocol shared with the Claude Channel carrier;
 *   2. polls the daemon-owned durable inbox (observation reads: no `fetched`
 *      flag) on a ~1s cadence with failure backoff;
 *   3. on new pending messages, performs the explicit consumption read
 *      (`fetched: true` — the SESSION_FETCH_PARAM contract boundary) and
 *      injects ONE batched user message via `pi.sendUserMessage(text,
 *      { deliverAs: "followUp" })`. deliverAs is ALWAYS passed: it is ignored
 *      when idle (message sends immediately) and queues safely while the user
 *      is mid-turn, so there is no idle/streaming check to race;
 *   4. completes deliveries only after the run actually settled: request
 *      intents get `message.reply` with the run's final assistant text,
 *      everything else gets `message.ack`. Injection/acceptance is never
 *      treated as delivery — completion is reply/ack or nothing;
 *   5. unregisters on `session_shutdown`.
 *
 * Fence semantics inherited from the Claude Channel carrier:
 *   - SESSION_SUPERSEDED is terminal: go quiet, never re-register;
 *   - STALE_SESSION is transient: re-register (the daemon restart self-heal);
 *   - connection failures back off (1s base, 5s cap) and never crash the TUI.
 *
 * Identity arrives through the same injected environment as the headless
 * harness-bridge (see pi_daemon_ipc.ts); without it the carrier stays
 * inert and only the harness_* toolset remains (fail-closed, as before).
 *
 * Test hook: HYPRIAL_PI_ATTACH_POLL_MS overrides the poll cadence.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import bridgeFactory from "./pi_harness_bridge.ts";
import { callDaemon, identityFromEnv } from "./pi_daemon_ipc.ts";

const CARRIER_SOURCE = "pi-extension";
const CARRIER_RUNTIME = "pi_interactive";
const CARRIER_COMMAND = ["pi"];

const POLL_BASE_MS = 1000;
const POLL_CAP_MS = 5000;
const MAX_INJECT_RETRIES = 2;

/** Daemon error codes produced by the fenced interactive-session calls. */
const CODE_SUPERSEDED = "SESSION_SUPERSEDED";
const CODE_STALE_SESSION = "STALE_SESSION";
const CODE_REPLY_UNAVAILABLE = "MESSAGE_REPLY_UNAVAILABLE";

interface PendingMessage {
  messageId: string;
  from: string;
  to: string;
  intent: string;
  text: string;
  /** Resolved human sender line, or null when the adapter reported none. */
  sender: string | null;
}

/**
 * Mirrors hyprial.daemon.api.describe_sender: only a verified row names an
 * owner, and every other standing says so, because a platform display name
 * is free text and can equal an owner's real name.
 */
function describeSender(origin: unknown): string | null {
  if (typeof origin !== "object" || origin === null) return null;
  const sender = (origin as Record<string, unknown>).sender;
  if (typeof sender !== "object" || sender === null) return null;
  const record = sender as Record<string, unknown>;
  const name =
    (typeof record.displayName === "string" && record.displayName) ||
    (typeof record.platformId === "string" && record.platformId) ||
    "unknown";
  const owner = typeof record.owner === "string" ? record.owner : "";
  if (record.standing === "verified" && owner) {
    return `${name} (owner ${owner}, verified)`;
  }
  if (record.standing === "verified") {
    return `${name} (verified person, no hyprial owner)`;
  }
  if (record.standing === "ambiguous") {
    const candidates = Array.isArray(record.candidateOwners)
      ? record.candidateOwners.filter((item) => typeof item === "string")
      : [];
    return `${name} (NOT verified: owner ambiguous between ${candidates.join(", ")})`;
  }
  if (record.standing === "observed") {
    return `${name} (NOT verified: display name only, no owner)`;
  }
  return `${name} (NOT verified: sender unresolved)`;
}

/** One injected batch of Harness messages and its completion lifecycle. */
interface OutstandingBatch {
  messages: PendingMessage[];
  /** The exact text handed to pi.sendUserMessage (consumption proof match). */
  injectedText: string;
  /** True once a user message_start carried injectedText into a real run. */
  consumed: boolean;
  injectRetries: number;
  /** Final assistant text captured at agent_end of the consuming run. */
  finalText: string | null;
}

function errorCode(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  const colon = message.indexOf(":");
  return colon > 0 ? message.slice(0, colon) : message;
}

/** Mirror hyprial.harnesses.pi_rpc._final_assistant_text. */
function finalAssistantText(messages: unknown): string | null {
  if (!Array.isArray(messages)) return null;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const candidate = messages[index] as {
      role?: unknown;
      content?: unknown;
    };
    if (candidate?.role !== "assistant" || !Array.isArray(candidate.content)) {
      continue;
    }
    const parts = (candidate.content as Array<{ type?: unknown; text?: unknown }>)
      .filter(
        (item) => item?.type === "text" && typeof item.text === "string",
      )
      .map((item) => item.text as string);
    const text = parts.join("\n");
    if (text) return text;
  }
  return null;
}

/** Extract the plain text of a message_start user message, mirroring pi's
 * own contentText matching for queued deliveries. */
function messageText(message: unknown): string | null {
  const record = message as { role?: unknown; content?: unknown };
  if (record?.role !== "user" || !Array.isArray(record.content)) return null;
  const parts = (record.content as Array<{ type?: unknown; text?: unknown }>)
    .filter((item) => item?.type === "text" && typeof item.text === "string")
    .map((item) => item.text as string);
  return parts.length ? parts.join("") : null;
}

function parsePending(result: Record<string, unknown>): PendingMessage[] {
  const raw = result.messages;
  if (!Array.isArray(raw)) return [];
  const messages: PendingMessage[] = [];
  for (const item of raw) {
    const record = item as Record<string, unknown>;
    if (typeof record.messageId !== "string" || !record.messageId) continue;
    messages.push({
      messageId: record.messageId,
      from: typeof record.from === "string" ? record.from : "unknown",
      to: typeof record.to === "string" ? record.to : "unknown",
      intent: typeof record.intent === "string" ? record.intent : "event",
      text: typeof record.message === "string" ? record.message : "",
      sender: describeSender(record.origin),
    });
  }
  return messages;
}

function senderSuffix(message: PendingMessage): string {
  return message.sender === null ? "" : `; sender: ${message.sender}`;
}

function formatBatch(messages: PendingMessage[]): string {
  if (messages.length === 1) {
    const message = messages[0];
    return (
      `[Harness Network message from ${message.from} to ${message.to} ` +
      `(intent: ${message.intent}, messageId: ${message.messageId})` +
      senderSuffix(message) +
      `]\n` +
      message.text
    );
  }
  const body = messages
    .map(
      (message, index) =>
        `[${index + 1}] from: ${message.from} to: ${message.to} ` +
        `(intent: ${message.intent}, messageId: ${message.messageId})` +
        senderSuffix(message) +
        `\n` +
        message.text,
    )
    .join("\n\n");
  return (
    `[Harness Network: ${messages.length} pending messages. ` +
    `Answer each in this transcript; your final reply is sent back ` +
    `to every requester.]\n\n${body}`
  );
}

export default function (pi: ExtensionAPI) {
  // The harness_* toolset is identical to the headless worker's; the attach
  // carrier adds the interactive session lifecycle around it.
  bridgeFactory(pi);

  const identity = identityFromEnv();
  if (identity === null) {
    // Unmanaged interactive session: no carrier, tools stay fail-closed.
    return;
  }

  const pollBaseMs = (() => {
    const override = Number(process.env.HYPRIAL_PI_ATTACH_POLL_MS);
    return Number.isFinite(override) && override > 0 ? override : POLL_BASE_MS;
  })();

  let quiet = false; // SESSION_SUPERSEDED: terminal ownership verdict
  let registered = false;
  let pollInFlight = false;
  let failures = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let outstanding: OutstandingBatch | null = null;
  const completed = new Set<string>(); // reply/ack accepted (or already done)

  function signed(params: Record<string, unknown> = {}) {
    return { actor: identity.actor, sessionRef: identity.sessionRef, ...params };
  }

  function schedule(delayMs: number) {
    if (quiet || timer !== null) return;
    timer = setTimeout(() => {
      timer = null;
      void pollOnce();
    }, delayMs);
  }

  function backoffMs(): number {
    if (failures <= 0) return pollBaseMs;
    return Math.min(POLL_CAP_MS, pollBaseMs * 2 ** Math.min(failures - 1, 6));
  }

  async function register(ui?: { notify(msg: string, type?: "info" | "warning" | "error"): void }) {
    try {
      await callDaemon(
        "session.register",
        signed({
          cwd: process.cwd(),
          command: CARRIER_COMMAND,
          source: CARRIER_SOURCE,
          runtime: CARRIER_RUNTIME,
          // Detached-tmux launches (`hyprial start pi --tmux`, the #218 pattern):
          // the daemon records the session name on the registration so
          // `hyprial ps` shows where to attach. No claude-style owner-fence
          // rewiring is needed here: this carrier is in-process, so its
          // lifetime already equals the pane's (kill-session -> SIGHUP ->
          // pi dies -> this carrier dies with it; session_shutdown
          // unregisters best-effort, the daemon TTL reclaims the rest).
          ...(identity.tmuxSession ? { tmuxSession: identity.tmuxSession } : {}),
        }),
        true,
      );
      registered = true;
      failures = 0;
    } catch (error) {
      const code = errorCode(error);
      if (code === CODE_SUPERSEDED) {
        goQuiet(ui, `Harness attach superseded: ${String(error)}`);
        return;
      }
      // AGENT_ALREADY_RUNNING and transient failures share one remedy: stay
      // attached-but-unregistered and let the next poll retry registration.
      ui?.notify(`Harness attach registration pending: ${String(error)}`, "warning");
    }
  }

  function goQuiet(
    ui: { notify(msg: string, type?: "info" | "warning" | "error"): void } | undefined,
    reason: string,
  ) {
    quiet = true;
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
    ui?.notify(reason, "warning");
  }

  /** The explicit carrier consumption boundary: fetched=true, fenced. */
  async function fetchPending(): Promise<PendingMessage[]> {
    const result = await callDaemon(
      "message.pending.list",
      signed({ fetched: true }),
      false,
    );
    return parsePending(result);
  }

  function inject(text: string) {
    // deliverAs is ALWAYS "followUp": ignored when idle (sends immediately),
    // queued safely mid-turn. Never branch on isIdle — that check races the
    // async send, and a bare call while streaming is rejected and lost.
    pi.sendUserMessage(text, { deliverAs: "followUp" });
  }

  async function injectFresh(): Promise<void> {
    const fresh = (await fetchPending()).filter(
      (message) => !completed.has(message.messageId),
    );
    if (fresh.length === 0) return;
    const text = formatBatch(fresh);
    outstanding = {
      messages: fresh,
      injectedText: text,
      consumed: false,
      injectRetries: 0,
      finalText: null,
    };
    inject(text);
  }

  async function completeOutstanding(): Promise<void> {
    if (outstanding === null || outstanding.finalText === null) return;
    for (const message of outstanding.messages) {
      const isRequest = message.intent === "request";
      try {
        if (isRequest) {
          await callDaemon(
            "message.reply",
            signed({ messageId: message.messageId, message: outstanding.finalText }),
            true,
          );
        } else {
          await callDaemon(
            "message.ack",
            signed({ messageId: message.messageId }),
            true,
          );
        }
        completed.add(message.messageId);
      } catch (error) {
        // Another path (the model called harness_reply itself, or an
        // out-of-band hyprial ack) already completed this delivery: benign.
        if (errorCode(error) === CODE_REPLY_UNAVAILABLE) {
          completed.add(message.messageId);
          continue;
        }
        throw error;
      }
    }
    outstanding = null;
  }

  async function failOutstanding(reason: string): Promise<void> {
    if (outstanding === null) return;
    for (const message of outstanding.messages) {
      try {
        if (message.intent === "request") {
          await callDaemon(
            "message.reply",
            signed({ messageId: message.messageId, message: reason }),
            true,
          );
        } else {
          await callDaemon("message.ack", signed({ messageId: message.messageId }), true);
        }
        completed.add(message.messageId);
      } catch (error) {
        if (errorCode(error) === CODE_REPLY_UNAVAILABLE) {
          completed.add(message.messageId);
        }
      }
    }
    outstanding = null;
  }

  async function pollOnce(): Promise<void> {
    if (quiet || pollInFlight) return;
    pollInFlight = true;
    try {
      if (!registered) {
        await register();
        if (!registered) {
          failures += 1;
          schedule(backoffMs());
          return;
        }
      }
      // Observation read: never marks fetched (the SESSION_FETCH contract).
      const observed = parsePending(
        await callDaemon("message.pending.list", signed(), false),
      );
      failures = 0;
      if (outstanding !== null && outstanding.finalText !== null) {
        // A previous completion attempt hit a transient error; retry it.
        await completeOutstanding();
      }
      if (
        outstanding === null &&
        observed.some((message) => !completed.has(message.messageId))
      ) {
        await injectFresh();
      }
      schedule(pollBaseMs);
    } catch (error) {
      const code = errorCode(error);
      if (code === CODE_SUPERSEDED) {
        goQuiet(undefined, `Harness attach superseded: ${String(error)}`);
        return;
      }
      if (code === CODE_STALE_SESSION) {
        // Transient: the daemon lost this registration (restart without
        // persisted state). Re-registering is the documented self-heal.
        registered = false;
      }
      failures += 1;
      schedule(backoffMs());
    } finally {
      pollInFlight = false;
    }
  }

  pi.on("session_start", async (_event, ctx) => {
    if (quiet) return;
    await register(ctx.ui);
    schedule(0);
  });

  pi.on("message_start", async (event) => {
    // The ONLY delivery-consumption proof: our exact injected text entered a
    // real run as a user message. The `input` event is NOT a proof (it also
    // fires for injections the runtime later rejects).
    if (outstanding === null || outstanding.consumed) return;
    if (messageText(event.message) === outstanding.injectedText) {
      outstanding.consumed = true;
    }
  });

  pi.on("agent_end", async (event) => {
    if (outstanding === null || !outstanding.consumed) return;
    const text = finalAssistantText(event.messages);
    outstanding.finalText =
      text ??
      "[hyprial] the interactive session produced no final assistant text " +
        "(the turn was aborted or empty)";
  });

  pi.on("agent_settled", async () => {
    if (outstanding === null || quiet) return;
    if (!outstanding.consumed) {
      // The run settled without our message ever entering it (aborted queue,
      // /new mid-flight). Re-inject a bounded number of times, then complete
      // with an honest failure instead of leaving the delivery pending.
      if (outstanding.injectRetries < MAX_INJECT_RETRIES) {
        outstanding.injectRetries += 1;
        inject(outstanding.injectedText);
      } else {
        await failOutstanding(
          "[hyprial] the interactive session never ran this message " +
            "(repeatedly aborted before delivery)",
        );
      }
      return;
    }
    try {
      await completeOutstanding();
    } catch {
      // Transient completion failure: retried on the next poll tick.
    }
  });

  pi.on("session_shutdown", async () => {
    quiet = true;
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
    if (!registered) return;
    try {
      await callDaemon("session.unregister", signed(), true);
    } catch {
      // Best effort: the daemon TTL reclaims a dead session's actor.
    }
  });
}
