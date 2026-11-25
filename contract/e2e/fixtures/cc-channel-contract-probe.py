#!/usr/bin/env python3
"""CC channel-contract probe (#94): re-verify the contract on any CC upgrade.

A minimal stdio MCP "channel" server that answers `server/discover` with an
error (so CC stays on the initialize handshake version), declares
`experimental["claude/channel"]`, answers initialize with a legacy
protocolVersion (2025-06-18), and emits one `notifications/claude/channel`
wake a few seconds after startup.

Usage (interactive, needs a logged-in Claude Code and tmux):

    mkdir -p /tmp/cc-chan-probe && cd /tmp/cc-chan-probe
    cat > mcp.json <<'EOF'
    {"mcpServers": {"probe": {"type": "stdio", "command": "python3",
      "args": ["contract/e2e/fixtures/cc-channel-contract-probe.py"]}}}
    EOF
    tmux new-session -d -s cc-chan-probe -c /tmp/cc-chan-probe -- \
      env TERM=xterm-256color claude \
      --mcp-config /tmp/cc-chan-probe/mcp.json \
      --dangerously-load-development-channels server:probe \
      --strict-mcp-config --debug-file /tmp/cc-chan-probe/debug.log
    # approve the workspace-trust and dev-channels dialogs, then inspect:
    #   grep -E "protocolEra|Channel notifications" /tmp/cc-chan-probe/debug.log

Healthy contract (CC 2.1.235): `protocolEra":"legacy"` and
`MCP server "probe": Channel notifications registered`, then the wake text
appears in the pane. Broken contract: `Channel notifications skipped: ...`
or a modern `protocolEra` — the #94 failure mode.

History: CC >=2.1.227 gates development channels on a legacy protocol era
(the modern wire has no unsolicited-notification path for stdio) and reads
channel capabilities from the initialize result. The MCP python SDK's
default `server/discover` handler advertises 2026-07-28, upgrading the
connection into the modern era and silently disabling channels; this probe
answers discover with method-not-found to stay legacy, which is also why
CC's `server/discover` must be pinned to handshake versions in the product
server. `claude/channel/permission` is intentionally NOT declared: live
mutation showed wakes register and land without it (that capability gates
the channel-permission flow this probe does not implement).
"""

import json
import sys
import threading
import time

WAKE_TEXT = "CHANNEL_PROBE_WAKE — if you can read this, the channel works"


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def log(msg: str) -> None:
    print(f"[probe] {msg}", file=sys.stderr, flush=True)


def wake_later() -> None:
    time.sleep(8)
    send(
        {
            "jsonrpc": "2.0",
            "method": "notifications/claude/channel",
            "params": {
                "content": WAKE_TEXT,
                "meta": {"delivery_id": "probe-wake-1"},
            },
        }
    )
    log("wake notification emitted")


def main() -> None:
    threading.Thread(target=wake_later, daemon=True).start()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method")
        rid = req.get("id")
        log(f"recv method={method} id={rid}")
        if method == "initialize":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {
                            "tools": {},
                            "experimental": {"claude/channel": {}},
                        },
                        "serverInfo": {"name": "probe", "version": "0.1.0"},
                    },
                }
            )
        elif method == "server/discover":
            # Deliberately unanswered (method-not-found): keeps CC on the
            # initialize handshake version instead of upgrading to the
            # modern era — see the module docstring.
            send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": -32601, "message": "no such method"},
                }
            )
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": []}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif rid is not None:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": -32601, "message": f"no such method {method}"},
                }
            )


if __name__ == "__main__":
    main()
