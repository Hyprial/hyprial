#!/usr/bin/env python3
"""F10a diagnostic: real Pi RPC, synthetic loopback provider, held native boundary.

This is native lifecycle evidence, not live provider/Lark acceptance. No daemon
is launched. All native configuration and sessions live below --output-root.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from hyprial.daemon.impl.harnesses.pi.rpc_client import PiRpcClient
from hyprial.daemon.impl.harnesses.pi.messages import _PiTurnOutcome
from hyprial.kernel import HarnessLaunchSpec

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fake_provider import FakeProvider


EXTENSION = r'''
import { existsSync, writeFileSync } from "node:fs";
export default function(pi) {
  pi.registerProvider("f10-local", {
    baseUrl: process.env.F10_PROVIDER_URL, apiKey: "synthetic-local-only",
    api: "openai-completions",
    models: [{ id: "f10", name: "F10 local", reasoning: false, input: ["text"],
      cost: {input:0, output:0, cacheRead:0, cacheWrite:0},
      contextWindow: 32768, maxTokens: 512 }],
  });
  let held = false;
  pi.on("agent_before_settle", async () => {
    if (held) return;
    held = true;
    writeFileSync(process.env.F10_OBSERVED, "native agent_before_settle\n");
    while (!existsSync(process.env.F10_RELEASE)) {
      await new Promise(resolve => setTimeout(resolve, 20));
    }
    return {continue:true, entries:[{type:"custom_message", customType:"f10",
      content:"Continue to the final answer.", display:false}]};
  });
}
'''


def boundary_available(pi: str) -> bool:
    for parent in Path(pi).resolve().parents:
        manifest = parent / "package.json"
        if not manifest.is_file():
            continue
        if json.loads(manifest.read_text()).get("name") != "@earendil-works/pi-coding-agent":
            continue
        declarations = parent / "dist/core/extensions/types.d.ts"
        return declarations.is_file() and 'event: "agent_before_settle"' in declarations.read_text()
    return False


class ObservedClient(PiRpcClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.events: list[dict] = []
        self.started = time.monotonic()

    async def __aexit__(self, *args):
        result = await super().__aexit__(*args)
        assert self.force_stopped(), "native child/pipe cleanup remains unsettled"
        return result

    async def _read(self, *, timeout=None):
        message = await super()._read(timeout=timeout)
        kind = message.get("type")
        if kind in {"agent_start", "agent_end", "agent_settled", "response"}:
            event = {"type": kind, "elapsedMs": round((time.monotonic()-self.started)*1000)}
            if kind == "response":
                event.update(command=message.get("command"), id=message.get("id"))
                data = message.get("data")
                if message.get("command") == "get_state" and isinstance(data, dict):
                    event["state"] = {key: data.get(key) for key in (
                        "isStreaming", "isCompacting", "pendingMessageCount",
                    )}
            if kind == "agent_end":
                event["willRetry"] = message.get("willRetry")
                event["assistant"] = [{"stopReason": row.get("stopReason"),
                    "text": "".join(part.get("text", "") for part in row.get("content", [])
                                    if part.get("type") == "text")}
                    for row in message.get("messages", []) if row.get("role") == "assistant"]
            self.events.append(event)
        return message


async def run(root: Path, pi: str, provider: FakeProvider) -> dict:
    extension = root / "boundary.ts"
    extension.write_text(EXTENSION)
    observed, release = root / "observed", root / "release"
    os.environ.update(F10_PROVIDER_URL=provider.openai_base_url,
                      F10_OBSERVED=str(observed), F10_RELEASE=str(release))
    launch = HarnessLaunchSpec(harness="pi", name="f10", headless=True,
        cwd=str(root), model_provider="f10-local", model="f10",
        args=("--no-extensions", "--no-skills", "--no-prompt-templates", "--no-tools",
              "--extension", str(extension), "--session-dir", str(root / "sessions")))
    client = ObservedClient(launch, "f10-settlement", command=(pi,))
    outcomes = []
    async with client:
        await client.query("Perform the long task.")

        async def collect():
            async for item in client.receive_response():
                if isinstance(item, _PiTurnOutcome):
                    outcomes.append(item)

        task = asyncio.create_task(collect())
        try:
            deadline = time.monotonic() + 20
            while not observed.exists() and not task.done() and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert observed.exists(), "native before-settle boundary was not reached"
            await asyncio.sleep(2.1)  # Outlive the old 1.5s silence heuristic.
            assert not task.done() and not outcomes, "unfinished native run produced a result"
            assert any(e.get("state", {}).get("isStreaming") is True
                       and str(e.get("id", "")).startswith("hyprial-settle-")
                       for e in client.events), "no native busy-state settlement probe"
            provider.reply = "f10-final"
            release.touch()
            await asyncio.wait_for(task, 20)
            assert len(outcomes) == 1 and not outcomes[0].is_error, "not one successful final"
            assert outcomes[0].result.strip() == "f10-final", "wrong native final"
            assert client.events[-1]["type"] == "agent_settled", "no native terminal proof"
            return {"result": "PASS", "nativeEvents": client.events,
                    "providerRequests": len(provider.requests), "final": outcomes[0].result}
        finally:
            (root / "native-events.json").write_text(json.dumps(client.events, indent=2) + "\n")
            release.touch()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.output_root.resolve()
    if not str(root).startswith(("/private/tmp/", "/tmp/")):
        parser.error("output-root must be a fresh disposable /tmp directory")
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    pi = shutil.which("pi")
    if pi is None:
        print("RESULT BLOCKED: native pi executable missing")
        return 2
    # Do not inherit user config, credential variables, sockets or proxies.
    clean = {key: os.environ[key] for key in ("PATH", "TMPDIR", "LANG") if key in os.environ}
    os.environ.clear()
    os.environ.update(clean, HOME=str(root / "home"), XDG_CONFIG_HOME=str(root / "config"),
        XDG_CACHE_HOME=str(root / "cache"), PI_CODING_AGENT_DIR=str(root / "pi-agent"),
        PI_OFFLINE="1", PI_TELEMETRY="0",
        HYPRIAL_HOME=str(root / "hyprial"), HARNESS_STATE_DIR=str(root / "state"),
        NO_PROXY="127.0.0.1,localhost")
    version = subprocess.run([pi, "--version"], text=True, capture_output=True, check=True).stdout.strip()
    if not boundary_available(pi):
        result = {"result": "BLOCKED", "piVersion": version,
                  "reason": "installed Pi SDK lacks agent_before_settle fixture boundary"}
        (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(f"RESULT BLOCKED: {result['reason']}")
        return 2
    try:
        with FakeProvider(reply="") as provider:
            result = asyncio.run(run(root, pi, provider))
    except Exception as error:
        result = {"result": "FAIL", "error": f"{type(error).__name__}: {error}"}
    result["piVersion"] = version
    if result["result"] == "PASS":
        result["cleanupConfirmed"] = True
    from inspect import getfile
    source = Path(getfile(PiRpcClient)).resolve()
    result.update(clientSource=str(source),
                  clientSha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                  runnerSha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(f"RESULT {result['result']}: native Pi settlement race; evidence={root / 'result.json'}")
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
