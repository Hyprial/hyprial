#!/usr/bin/env python3
"""E2E-002: drive a real isolated daemon through hyprial across two restarts."""

from __future__ import annotations

import sys
from pathlib import Path

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _daemon_runner import IsolatedDaemon, hyprial_executable, scenario_tempdir


# CLI probe persona (registered at daemon start; Allen b).
DRIVER = "restart-driver"


def main() -> int:
    executable = hyprial_executable()
    with scenario_tempdir("e2e-002") as temporary:
        daemon = IsolatedDaemon(executable, temporary, "restart-node")
        try:
            daemon.start()
            # resolve-or-reject (Allen b): probe self-send uses a registered
            # persona, never the bare node id.
            daemon.command("agent", "create", "--name", DRIVER, "--json")
            org = f"group-e2e002-{DRIVER}"
            daemon.command(
                "org", "create", org, "--json", exits=(0, 1)
            )
            daemon.command("org", "acl", "grant", org, DRIVER, "--json")
            first = daemon.command(
                "message",
                "send",
                "--from",
                DRIVER,
                "--to",
                DRIVER,
                "--summary",
                "first",
                "first",
                "--json",
            )
            second = daemon.command(
                "message",
                "send",
                "--from",
                DRIVER,
                "--to",
                DRIVER,
                "--summary",
                "second",
                "second",
                "--json",
            )
            expected = [first["messageId"], second["messageId"]]
            before = daemon.wait(
                lambda: daemon.ps(),
                lambda value: [
                    item["messageId"] for item in value.get("pendingMessages", [])
                ]
                == expected,
            )
            stale_delivery = before["pendingMessages"][0]["deliveryId"]

            daemon.sigterm()
            daemon.start()
            recovered = daemon.wait(
                lambda: daemon.ps(),
                lambda value: [
                    item["messageId"] for item in value.get("pendingMessages", [])
                ]
                == expected,
            )
            if recovered["pendingMessages"][0]["deliveryId"] == stale_delivery:
                raise AssertionError("restart reused the ephemeral deliveryId")

            acknowledged = daemon.command(
                "message", "ack", "--from", DRIVER, expected[0], "--json"
            )
            if acknowledged.get("acknowledged") is not True:
                raise AssertionError(f"first acknowledgement failed: {acknowledged}")
            after_first = daemon.ps()
            remaining = [item["messageId"] for item in after_first["pendingMessages"]]
            if remaining != [expected[1]]:
                raise AssertionError(f"FIFO recovery order changed: {remaining}")
            acknowledged = daemon.command(
                "message", "ack", "--from", DRIVER, expected[1], "--json"
            )
            if acknowledged.get("acknowledged") is not True:
                raise AssertionError(f"second acknowledgement failed: {acknowledged}")

            daemon.stop()
            daemon.env["HYPRIAL_HOLD_TTL_MS"] = "1000"
            daemon.start()
            final = daemon.ps()
            if final.get("pendingMessages") != []:
                raise AssertionError(f"acknowledged history replayed: {final}")

            ghost = daemon.command(
                "message",
                "send",
                "--from",
                DRIVER,
                "--to",
                "agent:contract:restart-node:does-not-exist",
                "--summary",
                "alarm-probe",
                "alarm-probe",
                "--json",
            )
            ghost_id = ghost["messageId"]
            terminal = daemon.wait(
                lambda: daemon.command(
                    "message",
                    "status",
                    "--from",
                    DRIVER,
                    "--message-id",
                    ghost_id,
                    "--json",
                ),
                lambda value: value.get("state") == "expired",
            )
            records = terminal.get("records")
            if (
                not isinstance(records, list)
                or len(records) != 1
                or not isinstance(records[0], dict)
                or records[0].get("reason") != "TTL_EXPIRED"
            ):
                raise AssertionError(f"ghost terminal reason changed: {terminal}")
            notice_result = daemon.request(
                "message.pending.list", {"from": DRIVER}
            )
            notices = notice_result.get("messages")
            if not isinstance(notices, list) or len(notices) != 1:
                raise AssertionError(f"sender did not receive one system notice: {notices}")
            notice = notices[0]
            # C07: a pending row carries no body; a system notice carries its
            # text in summary.
            if (
                not isinstance(notice, dict)
                or notice.get("intent") != "system"
                or ghost_id not in str(notice.get("summary"))
                or "TTL_EXPIRED" not in str(notice.get("summary"))
            ):
                raise AssertionError(f"system notice lost correlation/reason: {notice}")
            replay = daemon.request(
                "message.pending.list", {"from": DRIVER}
            )
            if replay.get("messages") != [notice]:
                raise AssertionError(f"unacknowledged system notice changed: {replay}")
            # inbox-batch-v1: list is a doorbell, not a consuming read. A
            # notice ACK dismisses this recipient's row without creating an
            # ordinary message receipt or FIFO-settlement obligation.
            dismissed = daemon.command(
                "message", "ack", "--from", DRIVER, notice["messageId"], "--json"
            )
            if dismissed.get("acknowledged") is not True:
                raise AssertionError(f"system notice acknowledgement failed: {dismissed}")
            after_notice_ack = daemon.request(
                "message.pending.list", {"from": DRIVER}
            )
            if after_notice_ack.get("messages") != []:
                raise AssertionError(f"acknowledged system notice replayed: {after_notice_ack}")
            daemon.stop()
            daemon.start()
            repeated = daemon.command(
                "message", "ack", "--from", DRIVER, notice["messageId"], "--json"
            )
            if (repeated.get("acknowledged") is not True
                    or repeated.get("code") != "MESSAGE_ALREADY_SETTLED"):
                raise AssertionError(f"notice tombstone lost across restart: {repeated}")
        finally:
            daemon.stop()
    print(
        "RESULT PASS E2E-002 daemon restart survival stable=2 replayed=0 "
        "system_notice=1 notice_ack=1 notice_ack_restart=1 "
        "receipt_obligation=0 terminal_floor=expired"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, OSError, KeyError) as error:
        print(f"RESULT FAIL E2E-002 {error}")
        raise SystemExit(1) from error
