#!/usr/bin/env python3
"""E2E-004: two real isolated daemons exchange and acknowledge messages."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import sys

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _daemon_runner import IsolatedDaemon, free_tcp_endpoint, hyprial_executable

STATUS_FETCHED_EVENT = "message.status.fetched"


def pending_ids(value: dict[str, object]) -> list[object]:
    pending = value.get("pendingMessages", [])
    return (
        [item.get("messageId") for item in pending if isinstance(item, dict)]
        if isinstance(pending, list)
        else []
    )


# CLI probe persona, registered on both daemons (Allen b).
DRIVER = "e2e004-driver"


def main() -> int:
    executable = hyprial_executable()
    endpoint = free_tcp_endpoint()
    with tempfile.TemporaryDirectory(prefix="hyprial-e2e-004-") as temporary:
        root = Path(temporary)
        left = IsolatedDaemon(
            executable, root / "left", "roundtrip-left", listen=endpoint
        )
        right = IsolatedDaemon(
            executable, root / "right", "roundtrip-right", connect=endpoint
        )
        try:
            left.start()
            right.start()
            # resolve-or-reject (Allen b): probe identities are registered
            # personas; nodes are never senders or targets.
            left.command("agent", "create", "--name", DRIVER, "--json")
            right.command("agent", "create", "--name", DRIVER, "--json")
            left.wait(
                lambda: left.command("host", "list", "--json"),
                lambda value: any(
                    item.get("nodeId") == "roundtrip-right"
                    for item in value.get("hosts", [])
                    if isinstance(item, dict)
                ),
            )
            right_info = right.ps().get("daemon", {})
            right_owner = str(right_info.get("owner") or "")
            right_driver = f"agent:{right_owner}:roundtrip-right:{DRIVER}"
            outbound = left.command(
                "message",
                "send",
                "--from",
                DRIVER,
                "--to",
                right_driver,
                "--summary",
                "left-to-right",
                "left-to-right",
                "--json",
            )
            message_id = outbound["messageId"]
            right.wait(
                lambda: right.ps(),
                lambda value: pending_ids(value) == [message_id],
            )
            reply = right.request(
                "message.reply",
                {
                    "actor": DRIVER,
                    "messageId": message_id,
                    "summary": "right-to-left",
                    "message": "right-to-left",
                },
            )
            if (
                reply.get("replied") is not True
                or reply.get("acknowledged") is not True
            ):
                raise AssertionError(f"right reply failed: {reply}")
            reply_id = reply.get("replyMessageId")
            if not isinstance(reply_id, str) or not reply_id:
                raise AssertionError(f"right reply returned no message id: {reply}")
            left.wait(
                lambda: left.ps(),
                lambda value: pending_ids(value) == [reply_id],
            )
            ack = left.command("message", "ack", "--from", DRIVER, reply_id, "--json")
            if ack.get("acknowledged") is not True:
                raise AssertionError(f"left reply acknowledgement failed: {ack}")
            trajectory = left.wait(
                lambda: left.command("message", "trajectory", message_id, "--json"),
                lambda value: any(
                    isinstance(item, dict)
                    and item.get("event") == STATUS_FETCHED_EVENT
                    for item in value.get("nodes", [])
                ),
            )
            nodes = trajectory.get("nodes", [])
            if trajectory.get("found") is not True:
                raise AssertionError(
                    f"trajectory omitted the local roundtrip path: {trajectory}"
                )
            if [item.get("event") for item in nodes if isinstance(item, dict)] != [
                "send.received",
                STATUS_FETCHED_EVENT,
            ]:
                raise AssertionError(f"trajectory event path mismatch: {trajectory}")
            for node in nodes:
                if not isinstance(node, dict) or not {
                    "ts",
                    "node",
                    "component",
                    "name",
                    "event",
                    "state",
                    "messageId",
                    "correlationId",
                    "source",
                    "details",
                }.issubset(node):
                    raise AssertionError(f"trajectory node schema mismatch: {node}")
                if (
                    node.get("messageId") != message_id
                    or node.get("correlationId") != message_id
                    or node.get("state") != "completed"
                ):
                    raise AssertionError(f"trajectory node identity mismatch: {node}")
            print(
                "TRAJECTORY "
                + json.dumps(trajectory, ensure_ascii=False, separators=(",", ":"))
            )
        finally:
            right.stop()
            left.stop()
    print("RESULT PASS E2E-004 dual-instance roundtrip request=1 reply=1 acknowledged=2")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, OSError, KeyError) as error:
        print(f"RESULT FAIL E2E-004 {error}")
        raise SystemExit(1) from error
