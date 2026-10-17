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
 *   4. reports the turn: `session.turn.started` before the fetched read,
 *      `session.turn.ended` when the run settles, both under one reportId,
 *      held and retried until the daemon confirms (an extension restart is a
 *      new session and drops unconfirmed reports);
 *   5. after the turn ends, asks `message.settle_hint` for every injected id
 *      and obeys it: "ack" -> `message.ack`, "keep" -> send nothing. The
 *      carrier never replies on anyone's behalf and injects a summary only;
 *      the body is read on demand by the model through the harness tools;
 *   6. unregisters on `session_shutdown`.
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

import { randomUUID } from "node:crypto";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import bridgeFactory from "./pi_harness_bridge.ts";
import {
  callDaemon,
  DaemonIpcError,
  identityFromEnv,
  processBirthIdentity,
} from "./pi_daemon_ipc.ts";
import {
  Heartbeat,
  InboxWatch,
  ListInbox,
  Ok,
  Quiet,
  Refresh,
  Register,
  Rejected,
  Sleep,
  Wait,
} from "./pi_inbox_watch.ts";
import type {
  InboxChange,
  Outcome,
  Request,
} from "./pi_inbox_watch.ts";

const CARRIER_SOURCE = "pi-extension";
const CARRIER_RUNTIME = "pi_interactive";
const CARRIER_COMMAND = ["pi"];

const DEFAULT_POLL_INTERVAL_MS = 1000;
const HEARTBEAT_INTERVAL_MS = 1000;
const WAIT_HOLD_MS = 5000;
const MAX_INJECT_RETRIES = 2;

/** Daemon error codes produced by the fenced interactive-session calls. */
const CODE_SUPERSEDED = "SESSION_SUPERSEDED";
const CODE_STALE = "STALE_SESSION";
const CODE_ACK_UNAVAILABLE = "MESSAGE_ACK_UNAVAILABLE";
const SUMMARY_FREE = "(none; the sender gave no summary)";
const REPORT_RETRY_CAP_MS = 5000; // ceiling of the turn-report retry backoff

interface TurnReport {
  phase: "started" | "ended";
  reportId: string;
  status?: "completed" | "failed";
}

interface PendingMessage {
  messageId: string;
  from: string;
  to: string;
  intent: string;
  kind: "message" | "notice";
  summary: string | null;
  bodyBytes: number;
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
  const candidateUsers = Array.isArray(record.candidateUsers)
    ? record.candidateUsers.filter((item) => typeof item === "string")
    : [];
  if (record.standing === "ambiguous" && candidateUsers.length > 0) {
    // Mirrors describe_sender: user-store candidates are people, and a
    // guest among them has no owner to name.
    return `${name} (NOT verified: sender ambiguous between users ${candidateUsers.join(", ")})`;
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
  /**
   * True once agent_settled started settling this batch.  Only then may the
   * poll lane retry a settlement (a poll tick between agent_end and
   * agent_settled must not settle early).
   */
  settled: boolean;
}

function errorCode(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  const colon = message.indexOf(":");
  return colon > 0 ? message.slice(0, colon) : message;
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
    const row = item as Record<string, unknown>;
    if (typeof row.messageId !== "string" || !row.messageId) continue;
    messages.push({
      messageId: row.messageId,
      from: typeof row.from === "string" ? row.from : "unknown",
      to: typeof row.to === "string" ? row.to : "unknown",
      intent: typeof row.intent === "string" ? row.intent : "event",
      kind: row.kind === "notice" ? "notice" : "message",
      summary: typeof row.summary === "string" ? row.summary : null,
      bodyBytes: Number.isInteger(row.bodyBytes) ? (row.bodyBytes as number) : 0,
      sender: describeSender(row.origin),
    });
  }
  return messages;
}

function senderSuffix(message: PendingMessage): string {
  return message.sender === null ? "" : `; sender: ${message.sender}`;
}

/** wp5.md 2.3, byte for byte; tests/daemon/harnesses/carrier/injection_golden.json. */
function injectionBlock(message: PendingMessage): string {
  if (message.kind === "notice") return `notice: ${message.summary ?? ""}`;
  const id = message.messageId;
  return (
    `summary: ${message.summary ? message.summary : SUMMARY_FREE}\n` +
    `(intent: ${message.intent}, kind: message, messageId: ${id}, ` +
    `bodyBytes: ${message.bodyBytes})\n` +
    `Full body: harness_read_body("${id}") or \`hyprial read ${id}\`. ` +
    `Reply with harness_reply("${id}", …); if no reply is needed, harness_ack("${id}").`
  );
}

function formatBatch(messages: PendingMessage[]): string {
  if (messages.length === 1) {
    const message = messages[0];
    return (
      `[Harness Network message from ${message.from} to ${message.to} ` +
      `(intent: ${message.intent}, messageId: ${message.messageId})` +
      senderSuffix(message) +
      `]\n` +
      injectionBlock(message)
    );
  }
  const body = messages
    .map(
      (message, index) =>
        `[${index + 1}] from: ${message.from} to: ${message.to} ` +
        `(intent: ${message.intent}, messageId: ${message.messageId})` +
        senderSuffix(message) +
        `\n` +
        injectionBlock(message),
    )
    .join("\n\n");
  return (
    `[Harness Network: ${messages.length} pending messages. ` +
    `Each item below is a summary only; read a body on demand and reply or ack each item explicitly.]\n\n${body}`
  );
}

export default function (pi: ExtensionAPI) {
  // The harness_* toolset is identical to the headless worker's; the attach
  // carrier adds the interactive session lifecycle around it.
  const identity = identityFromEnv();
  if (identity === null) {
    // Unmanaged interactive session: no carrier, tools stay fail-closed.
    bridgeFactory(pi);
    return;
  }

  const pollBaseMs = (() => {
    const override = Number(process.env.HYPRIAL_PI_ATTACH_POLL_MS);
    return Number.isFinite(override) && override > 0
      ? override
      : DEFAULT_POLL_INTERVAL_MS;
  })();

  const watch = new InboxWatch({
    pollIntervalMs: pollBaseMs,
    heartbeatIntervalMs: HEARTBEAT_INTERVAL_MS,
    holdMs: WAIT_HOLD_MS,
  });
  // A tool call's verdict on the session counts like a lane's (a new epoch
  // owes a refresh; SUPERSEDED silences both lanes).
  bridgeFactory(pi, {
    result: (result) => watch.observe(result),
    rejection: (message) => {
      watch.observeRejection(errorCode(new Error(message)));
      if (watch.quiet) stopLaneTimers();
    },
  });
  let stopped = false;
  let injecting = false;
  let pollInFlight = false;
  let heartbeatInFlight = false;
  let pollTimer: ReturnType<typeof setTimeout> | null = null;
  let heartbeatTimer: ReturnType<typeof setTimeout> | null = null;
  let outstanding: OutstandingBatch | null = null;
  const completed = new Set<string>(); // ack accepted (or already done)
  const kept = new Set<string>();
  let openTurn: string | null = null;
  const reportQueue: TurnReport[] = [];
  let reportFlight: Promise<boolean> | null = null;
  let reportFailures = 0;
  let reportRetryAt = 0;
  let reportTimer: ReturnType<typeof setTimeout> | null = null;
  let agentRunning = false;
  let lastUi: { notify(msg: string, type?: "info" | "warning" | "error"): void } | null = null;
  const queuedForInjection = new Set<string>();

  function signed(params: Record<string, unknown> = {}) {
    return { actor: identity.actor, sessionRef: identity.sessionRef, ...params };
  }

  function schedulePoll(delayMs: number) {
    if (stopped || watch.quiet || pollTimer !== null) return;
    pollTimer = setTimeout(() => {
      pollTimer = null;
      void runPollLane();
    }, delayMs);
  }

  function scheduleHeartbeat(delayMs: number) {
    if (stopped || watch.quiet || heartbeatTimer !== null) return;
    heartbeatTimer = setTimeout(() => {
      heartbeatTimer = null;
      void runHeartbeatLane();
    }, delayMs);
  }

  function stopLaneTimers() {
    if (pollTimer !== null) clearTimeout(pollTimer);
    if (heartbeatTimer !== null) clearTimeout(heartbeatTimer);
    pollTimer = null;
    heartbeatTimer = null;
    if (reportTimer !== null) clearTimeout(reportTimer);
    reportTimer = null;
  }

  function notify(message: string, type: "info" | "warning" | "error" = "warning") {
    lastUi?.notify(message, type);
  }

  /** One wakeup for the held queue head; re-armed wherever it is found early. */
  function armReportTimer() {
    if (stopped || watch.quiet || reportTimer !== null) return;
    reportTimer = setTimeout(() => {
      reportTimer = null;
      void flushReports();
    }, Math.max(reportRetryAt - Date.now(), 0));
  }

  async function sendHeadReport(): Promise<boolean> {
    const head = reportQueue[0];
    try {
      const started = head.phase === "started";
      await callDaemon(
        started
          ? "session.turn.started"
          : "session.turn.ended",
        signed(started ? { reportId: head.reportId } : { reportIds: [head.reportId], status: head.status }),
        true,
      );
    } catch (error) {
      const code = errorCode(error);
      if (code === CODE_SUPERSEDED || code === CODE_STALE) {
        // Never acceptable for this session: drop, do not retry.
        reportQueue.length = 0;
        openTurn = null;
        watch.observeRejection(code);
        if (watch.quiet) stopLaneTimers();
        notify(`Harness attach dropped its turn reports: ${String(error)}`);
        return false;
      }
      reportFailures += 1;
      const delay = Math.min(pollBaseMs * 2 ** (reportFailures - 1), REPORT_RETRY_CAP_MS);
      reportRetryAt = Date.now() + delay;
      armReportTimer();
      return false;
    }
    reportQueue.shift();
    reportFailures = reportRetryAt = 0;
    return true;
  }

  /** In order; the head must be confirmed before the next report is sent. */
  async function flushReports(force = false): Promise<void> {
    while (reportQueue.length > 0) {
      if (reportFlight !== null) {
        await reportFlight;
        continue;
      }
      if (!force && Date.now() < reportRetryAt) return armReportTimer(); // never strand a held head
      reportFlight = sendHeadReport().finally(() => {
        reportFlight = null;
      });
      if (!(await reportFlight)) return;
    }
  }

  async function openTurnIfNeeded(): Promise<boolean> {
    if (openTurn === null) {
      openTurn = randomUUID();
      reportQueue.push({ phase: "started", reportId: openTurn });
    }
    await flushReports();
    const id = openTurn;
    return id !== null && !reportQueue.some((r) => r.phase === "started" && r.reportId === id);
  }

  function endTurn(status: "completed" | "failed") {
    if (openTurn === null) return;
    reportQueue.push({ phase: "ended", reportId: openTurn, status });
    openTurn = null;
  }

  function registerParams(): Record<string, unknown> {
    const processIdentity = processBirthIdentity(process.pid);
    return signed({
          cwd: process.cwd(),
          command: CARRIER_COMMAND,
          source: CARRIER_SOURCE,
          runtime: CARRIER_RUNTIME,
          ...(processIdentity
            ? { processPid: process.pid, processIdentity }
            : {}),
          // Detached-tmux launches (`hyprial start pi --tmux`, #218): the daemon
          // records the session name so `hyprial ps` shows where to attach. This
          // carrier is in-process: its lifetime already equals the pane's, and
          // session_shutdown unregisters best-effort (the TTL reclaims the rest).
          ...(identity.tmuxSession ? { tmuxSession: identity.tmuxSession } : {}),
    });
  }

  async function perform(action: Request): Promise<Record<string, unknown>> {
    if (action instanceof Register) {
      // A resumed session registers guarded: the daemon must confirm it
      // checked for a live incumbent, or registration fails and retries.
      const guarded = process.env.HYPRIAL_RESUME_GUARDED === "1";
      const result = await callDaemon(
        guarded ? "session.register_guarded" : "session.register",
        registerParams(),
        true,
      );
      if (guarded && result.liveIncumbentGuardApplied !== true) {
        throw new Error(
          "CODEX_RESUME_GUARD_UNCONFIRMED: daemon did not confirm guarded registration",
        );
      }
      return result;
    }
    if (action instanceof Refresh) {
      return callDaemon("session.refresh", signed(), false);
    }
    if (action instanceof Heartbeat) {
      return callDaemon("session.heartbeat", signed(), false);
    }
    if (action instanceof ListInbox) {
      return callDaemon("message.pending.list", signed(), false);
    }
    const params: Record<string, unknown> = {
      knownMessageIds: [...action.knownMessageIds],
    };
    if (action.holdMs !== null) params.holdMs = action.holdMs;
    return callDaemon("message.pending.wait", signed(params), false);
  }

  /** The explicit carrier consumption boundary: fetched=true, fenced. */
  async function fetchPending(): Promise<{
    result: Record<string, unknown>;
    messages: PendingMessage[];
  }> {
    const result = await callDaemon(
      "message.pending.list",
      signed({ fetched: true }),
      false,
    );
    watch.observe(result);
    return { result, messages: parsePending(result) };
  }

  function inject(text: string) {
    // deliverAs is ALWAYS "followUp": ignored when idle (sends immediately),
    // queued safely mid-turn. Never branch on isIdle — that check races the
    // async send, and a bare call while streaming is rejected and lost.
    pi.sendUserMessage(text, { deliverAs: "followUp" });
  }

  async function injectQueued(): Promise<void> {
    if (
      stopped ||
      watch.quiet ||
      injecting ||
      outstanding !== null ||
      queuedForInjection.size === 0
    ) {
      return;
    }
    injecting = true;
    try {
      await injectQueuedOnce();
    } finally {
      injecting = false;
    }
  }

  async function injectQueuedOnce(): Promise<void> {
    // wp5.md 4.4: turn.started must be accepted before the fetched read.
    const turnWasOpen = openTurn !== null;
    const abandonEmptyTurn = () => {
      if (!turnWasOpen && !agentRunning && outstanding === null) {
        endTurn("completed");
        void flushReports();
      }
    };
    if (!(await openTurnIfNeeded())) return;
    let fetched: PendingMessage[];
    try {
      ({ messages: fetched } = await fetchPending());
    } catch (error) {
      watch.observeRejection(errorCode(error));
      if (watch.quiet) stopLaneTimers();
      abandonEmptyTurn();
      return;
    }
    // The fetched read is the consumption boundary. Include every still
    // pending row it returns so a message arriving between observation and
    // fetch is never marked fetched without being injected.
    const fresh = fetched.filter((m) => !completed.has(m.messageId) && !kept.has(m.messageId));
    for (const messageId of [...queuedForInjection]) {
      if (!fetched.some((message) => message.messageId === messageId)) {
        queuedForInjection.delete(messageId);
      }
    }
    if (fresh.length === 0) {
      abandonEmptyTurn();
      return;
    }
    for (const message of fresh) queuedForInjection.delete(message.messageId);
    const text = formatBatch(fresh);
    outstanding = {
      messages: fresh,
      injectedText: text,
      consumed: false,
      injectRetries: 0,
      settled: false,
    };
    inject(text);
  }

  async function applyChange(change: InboxChange | null): Promise<void> {
    if (change !== null) applyChangeRows(change);
    await injectQueued();
  }

  function applyChangeRows(change: InboxChange) {
    // A new daemon epoch allows exactly one more injection of what was kept.
    if (change.rewakeReason !== null) kept.clear();
    for (const messageId of change.completed) {
      completed.add(messageId);
      kept.delete(messageId);
      queuedForInjection.delete(messageId);
    }
    for (const row of [...change.newMessages, ...change.rewake]) {
      const messageId = row.messageId;
      if (typeof messageId === "string" && !completed.has(messageId) && !kept.has(messageId)) {
        queuedForInjection.add(messageId);
      }
    }
    if (change.rewake.length > 0 && outstanding !== null) {
      // Only the in-flight batch is re-announced; messages merely queued are
      // injected once, by injectQueued, when that batch completes.
      const inFlight = new Set(outstanding.messages.map((m) => m.messageId));
      const backlog = parsePending({ messages: change.rewake }).filter(
        (message) =>
          inFlight.has(message.messageId) && !completed.has(message.messageId),
      );
      if (backlog.length > 0) {
        // A generation change explicitly bypasses ordinary de-duplication:
        // re-inject the backlog notice even when an earlier copy is still
        // queued in pi, so a daemon restart cannot strand the wake.
        inject(formatBatch(backlog));
      }
    }
  }

  // Single flight: concurrent callers share the one settlement in progress,
  // so a batch is never settled twice.
  let completing: Promise<void> | null = null;

  function completeOutstanding(): Promise<void> {
    if (completing === null) {
      completing = completeOutstandingOnce().finally(() => {
        completing = null;
      });
    }
    return completing;
  }

  async function completeOutstandingOnce(): Promise<void> {
    if (outstanding === null) return;
    for (const message of outstanding.messages) {
      const id = message.messageId;
      if (completed.has(id) || kept.has(id)) continue;
      const answer = await callDaemon("message.settle_hint", signed({ messageId: id }), false);
      if (answer.hint === "ack") {
        try {
          const result = await callDaemon("message.ack", signed({ messageId: id }), true);
          if (result.ok !== true && result.acknowledged !== true && result.code !== CODE_ACK_UNAVAILABLE) {
            throw new DaemonIpcError(
              String(result.code ?? "MESSAGE_SETTLEMENT_PENDING"),
              "daemon has not confirmed the ack",
              result,
            );
          }
        } catch (error) {
          // Already acked or gone: settled all the same.
          if (errorCode(error) !== CODE_ACK_UNAVAILABLE) throw error;
        }
        completed.add(id);
      } else if (answer.hint === "keep") {
        kept.add(id);
      } else {
        throw new DaemonIpcError(
          "MESSAGE_SETTLEMENT_PENDING",
          "daemon returned no usable settle hint",
        );
      }
      queuedForInjection.delete(id);
    }
    outstanding = null;
    watch.listNow();
    schedulePoll(0);
  }

  async function runPollLane(
    ui?: {
      notify(msg: string, type?: "info" | "warning" | "error"): void;
    },
  ): Promise<void> {
    if (stopped || watch.quiet || pollInFlight) return;
    // Busy (a batch in flight, or messages queued): a message arriving now
    // is already "known" to the doorbell, so a hold would sit out its full
    // length (review 849). The core then paces by the interval instead --
    // no hold, and no fake "unheld" answer inflating wait.unheld.
    watch.useWait =
      outstanding === null &&
      queuedForInjection.size === 0 &&
      reportQueue.length === 0;
    const action = watch.pollNext(Date.now());
    if (action instanceof Quiet) return;
    if (action instanceof Sleep) {
      schedulePoll(action.ms);
      return;
    }
    pollInFlight = true;
    let outcome: Outcome;
    let errorSeen: unknown = null;
    try {
      outcome = new Ok(await perform(action));
    } catch (error) {
      errorSeen = error;
      // callDaemon normalizes daemon and transport errors as "CODE: detail".
      outcome = new Rejected(errorCode(error));
    }
    try {
      const change = watch.pollDone(action, outcome, Date.now());
      if (watch.quiet) {
        stopLaneTimers();
        ui?.notify(
          `Harness attach superseded: ${String(errorSeen ?? CODE_SUPERSEDED)}`,
          "warning",
        );
        return;
      }
      if (action instanceof Register && !(outcome instanceof Ok)) {
        // AGENT_ALREADY_RUNNING and transient failures share one remedy:
        // remain unregistered and let the state machine retry with backoff.
        ui?.notify(
          `Harness attach registration pending: ${String(errorSeen)}`,
          "warning",
        );
      }
      if (reportQueue.length > 0) void flushReports(); // poll lane retries the head too
      if (outstanding !== null && outstanding.settled) {
        // A previous settlement attempt hit a transient error; retry it.
        try {
          await completeOutstanding();
        } catch (error) {
          watch.observeRejection(errorCode(error));
          if (watch.quiet) {
            stopLaneTimers();
            return;
          }
        }
      }
      try {
        await applyChange(change);
      } catch (error) {
        // One unusable answer must not stop the lane; a re-wake handed over
        // with it is owed again at the ordinary pace (#1152, review 848).
        if (change !== null && change.rewakeReason !== null) {
          watch.requestRewake(change.rewakeReason, false);
        }
        ui?.notify(`Harness attach could not apply a list: ${String(error)}`, "warning");
      }
    } finally {
      pollInFlight = false;
    }
    schedulePoll(0);
  }

  async function runHeartbeatLane(): Promise<void> {
    if (stopped || watch.quiet || heartbeatInFlight) return;
    const action = watch.heartbeatNext(Date.now());
    if (action instanceof Quiet) return;
    if (action instanceof Sleep) {
      scheduleHeartbeat(action.ms);
      return;
    }
    heartbeatInFlight = true;
    let outcome: Outcome;
    try {
      outcome = new Ok(await perform(action));
    } catch (error) {
      outcome = new Rejected(errorCode(error));
    }
    try {
      watch.heartbeatDone(action, outcome, Date.now());
      if (watch.quiet) {
        stopLaneTimers();
        return;
      }
    } finally {
      heartbeatInFlight = false;
    }
    scheduleHeartbeat(0);
    // A heartbeat can owe a refresh. Wake the poll lane so it does not wait
    // for a stale local timer after the currently held wait returns.
    if (watch.refreshOwed) schedulePoll(0);
  }

  pi.on("session_start", async (_event, ctx) => {
    lastUi = ctx.ui;
    if (stopped || watch.quiet) return;
    scheduleHeartbeat(0);
    await runPollLane(ctx.ui);
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

  pi.on("agent_start", async (_event, ctx) => {
    lastUi = ctx?.ui ?? lastUi;
    agentRunning = true;
    if (stopped || watch.quiet) return;
    await openTurnIfNeeded();
  });

  pi.on("agent_settled", async (_event, ctx) => {
    lastUi = ctx?.ui ?? lastUi;
    agentRunning = false;
    if (watch.quiet) return;
    if (outstanding !== null && !outstanding.consumed) {
      // The run settled without our message ever entering it (aborted queue,
      // /new mid-flight). Re-inject a bounded number of times, then give up
      // honestly: the turn failed, nothing is acked or replied, and the
      // messages stay pending (held back until the next epoch).
      if (outstanding.injectRetries < MAX_INJECT_RETRIES) {
        outstanding.injectRetries += 1;
        inject(outstanding.injectedText);
        return;
      }
      for (const message of outstanding.messages) kept.add(message.messageId);
      outstanding = null;
      endTurn("failed");
      await flushReports();
      notify(
        "Harness attach: pending messages were never run (repeatedly aborted " +
          "before delivery); they stay pending",
      );
      return;
    }
    endTurn("completed");
    await flushReports();
    if (outstanding === null) return;
    outstanding.settled = true;
    try {
      await completeOutstanding();
    } catch (error) {
      // Transient settlement failure: retried on the next poll tick.
      watch.observeRejection(errorCode(error));
      if (watch.quiet) stopLaneTimers();
    }
  });

  pi.on("session_shutdown", async () => {
    stopped = true;
    stopLaneTimers();
    if (!watch.registered) return;
    if (openTurn !== null) endTurn("failed");
    try {
      await flushReports(true);
    } catch {
      // Best effort: the daemon drops the session's reports with the session.
    }
    try {
      await callDaemon("session.unregister", signed(), true);
    } catch {
      // Best effort: the daemon TTL reclaims a dead session's actor.
    }
  });
}
