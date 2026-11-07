import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { appendFileSync } from "node:fs";

const LOG = process.env.PROBE_LOG ?? "/tmp/pi-attach-probe/probe2.jsonl";
const record = (event: string, data: Record<string, unknown> = {}) =>
  appendFileSync(LOG, JSON.stringify({ t: new Date().toISOString(), event, ...data }) + "\n");

export default function (pi: ExtensionAPI) {
  pi.on("session_start", async (_event, ctx) => {
    record("session_start", { mode: ctx.mode, hasUI: ctx.hasUI });
    // Ask the agent to call the bridge tool; unmanaged env must fail closed.
    setTimeout(() => {
      pi.sendUserMessage(
        "Call the harness_whoami tool exactly once, then reply with the tool result verbatim.",
      );
    }, 2000);
  });
  pi.on("tool_result", async (event) => {
    if (event.toolName === "harness_whoami") {
      record("harness_whoami_result", {
        isError: event.isError,
        text: event.content
          ?.filter((p: { type: string }) => p.type === "text")
          .map((p: { text?: string }) => p.text)
          .join(" ")
          .slice(0, 300),
      });
    }
  });
  pi.on("agent_settled", async () => record("agent_settled"));
}
