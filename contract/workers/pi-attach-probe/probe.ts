/**
 * hyprial attach probe: prove the interactive (TUI) extension surface can
 *  - observe mode / session identity
 *  - inject a user message into a live idle session (pi.sendUserMessage)
 *  - observe agent_settled in interactive mode
 *  - probe streaming-time injection semantics (deliverAs required)
 * pi.sendUserMessage is fire-and-forget (errors go to emitError -> UI notify),
 * so success/failure is judged by whether an input event with the probe text
 * and source "extension" actually arrives.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { appendFileSync } from "node:fs";

const LOG = process.env.PROBE_LOG ?? "/tmp/pi-attach-probe/probe.jsonl";

function record(event: string, data: Record<string, unknown> = {}) {
  appendFileSync(
    LOG,
    JSON.stringify({ t: new Date().toISOString(), event, ...data }) + "\n",
  );
}

const seenInputs: string[] = [];

export default function (pi: ExtensionAPI) {
  let sawSettled = 0;
  let phase = 0;

  pi.on("session_start", async (_event, ctx) => {
    record("session_start", {
      mode: ctx.mode,
      hasUI: ctx.hasUI,
      sessionId: ctx.sessionManager.getSessionId(),
      isIdle: ctx.isIdle(),
      workerActor: process.env.HYPRIAL_WORKER_ACTOR ?? null,
      workerSessionRef: process.env.HYPRIAL_WORKER_SESSION_REF ?? null,
    });

    // t+2s: inject a user message into the idle TUI session.
    setTimeout(() => {
      phase = 1;
      record("inject_idle_attempt", { isIdle: ctx.isIdle() });
      pi.sendUserMessage("Reply with the single word PONG and nothing else.");
    }, 2000);
  });

  pi.on("input", async (event) => {
    seenInputs.push(event.text);
    record("input", {
      source: event.source,
      streamingBehavior: event.streamingBehavior ?? null,
      text: event.text.slice(0, 60),
    });
  });

  pi.on("agent_start", async (_event, ctx) => {
    record("agent_start", { isIdle: ctx.isIdle() });
    if (phase === 1) {
      phase = 2;
      // The agent is now streaming. Probe mid-stream injection semantics.
      setTimeout(() => {
        record("inject_streaming_attempts", { isIdle: ctx.isIdle() });
        // (a) no deliverAs while streaming -> documented to throw internally
        pi.sendUserMessage("PROBE-NO-DELIVERAS");
        // (b) followUp while streaming -> should queue, fire after settle
        pi.sendUserMessage("Reply with the single word DONE and nothing else.", {
          deliverAs: "followUp",
        });
      }, 300);
    }
  });

  pi.on("agent_end", async () => record("agent_end"));

  pi.on("agent_settled", async (_event, ctx) => {
    sawSettled += 1;
    record("agent_settled", {
      count: sawSettled,
      isIdle: ctx.isIdle(),
      seenNoDeliverasProbe: seenInputs.some((t) => t.includes("PROBE-NO-DELIVERAS")),
      seenFollowUpProbe: seenInputs.some((t) => t.includes("single word DONE")),
    });
  });

  pi.on("session_shutdown", async () =>
    record("session_shutdown", { settled: sawSettled }),
  );
}
