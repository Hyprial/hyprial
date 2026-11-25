#!/usr/bin/env python3
"""Isolated two-daemon proof for canonical actor registration compatibility."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any

from hyprial.kernel.impl.contracts.daemon.ipc_errors import TransientDaemonError

import sys

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _daemon_runner import (
    IsolatedDaemon,
    free_tcp_endpoint,
    hyprial_executable,
    teardown_guard,
)

OWNER = "regfix-owner"
LEFT_ACTOR = "left-adjutant"
LEFT_URI = f"agent:{OWNER}:regfix-left:{LEFT_ACTOR}"
RIGHT_ACTOR = "hq-adjutant"
RIGHT_URI = f"agent:{OWNER}:regfix-right:{RIGHT_ACTOR}"


def register(daemon: IsolatedDaemon, actor: str, session_ref: str) -> dict[str, Any]:
    return daemon.request(
        "session.register",
        {
            "actor": actor,
            "sessionRef": session_ref,
            "cwd": str(daemon.root),
            "command": ["claude"],
            "source": "claude-channel",
            "channelConfirmed": True,
        },
    )


def pending(daemon: IsolatedDaemon, actor: str, session_ref: str) -> dict[str, Any]:
    return daemon.request(
        "message.pending.list",
        {"actor": actor, "sessionRef": session_ref},
    )


def message_ids(value: dict[str, Any]) -> list[str]:
    messages = value.get("messages", [])
    return [
        str(item["messageId"])
        for item in messages
        if isinstance(item, dict) and isinstance(item.get("messageId"), str)
    ]


def read_body(
    daemon: IsolatedDaemon, actor: str, session_ref: str, message_id: str
) -> dict[str, Any]:
    """The message body: pending rows no longer carry it (C07)."""

    return daemon.request(
        "message.read_body",
        {"actor": actor, "sessionRef": session_ref, "messageId": message_id},
    )


def join_space(daemon: IsolatedDaemon, space_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = daemon.command(
            "org", "fs", "space", "join", space_id, "--json", exits=(0, 1)
        )
        if last.get("ok") is True:
            return last
        time.sleep(0.25)
    raise AssertionError(f"org space {space_id} never joined: {last}")


def ack(daemon: IsolatedDaemon, actor: str, session_ref: str, message_id: str) -> None:
    result = daemon.request(
        "message.ack",
        {
            "actor": actor,
            "sessionRef": session_ref,
            "messageId": message_id,
        },
    )
    if result.get("acknowledged") is not True:
        raise AssertionError(f"message acknowledgement failed: {result}")


def main() -> int:
    executable = hyprial_executable()
    endpoint = free_tcp_endpoint()
    with tempfile.TemporaryDirectory(prefix="hyprial-regfix-") as temporary:
        root = Path(temporary)
        left = IsolatedDaemon(executable, root / "left", "regfix-left", listen=endpoint)
        right = IsolatedDaemon(
            executable, root / "right", "regfix-right", connect=endpoint
        )
        left.env["HYPRIAL_OWNER"] = OWNER
        right.env["HYPRIAL_OWNER"] = OWNER
        with teardown_guard(right.stop, left.stop):
            left.start()
            right.start()

            registration = register(right, RIGHT_ACTOR, "right-session")
            if registration.get("actor") != RIGHT_URI:
                raise AssertionError(
                    f"MCP registration was not canonical: {registration}"
                )
            left_registration = register(left, LEFT_ACTOR, "left-session")
            if left_registration.get("actor") != LEFT_URI:
                raise AssertionError(
                    f"left MCP registration was not canonical: {left_registration}"
                )
            left.wait(
                lambda: left.command("message", "targets", "--json"),
                lambda value: any(
                    isinstance(item, dict) and item.get("actor") == RIGHT_URI
                    for item in value.get("targets", [])
                ),
            )

            org = left.command(
                "org", "create", "group-regfix-shared", "--json", exits=(0, 1)
            )
            org_id = org.get("space_id", org.get("spaceId"))
            if not isinstance(org_id, str) or not org_id:
                raise AssertionError(f"org create returned no space id: {org}")
            left.command(
                "org", "acl", "grant", "group-regfix-shared", LEFT_URI, "--json"
            )
            join_space(right, org_id)

            direct = left.command(
                "message",
                "send",
                "--from",
                "regfix-left",
                "--to",
                RIGHT_URI,
                "--summary",
                "canonical-direct",
                "canonical-direct",
                "--json",
            )
            direct_id = str(direct["messageId"])
            right.wait(
                lambda: pending(right, RIGHT_ACTOR, "right-session"),
                lambda value: message_ids(value) == [direct_id],
            )
            ack(right, RIGHT_ACTOR, "right-session", direct_id)

            reverse = right.command(
                "message",
                "send",
                "--from",
                "regfix-right",
                "--to",
                LEFT_URI,
                "--summary",
                "canonical-reverse",
                "canonical-reverse",
                "--json",
            )
            reverse_id = str(reverse["messageId"])
            left.wait(
                lambda: pending(left, LEFT_ACTOR, "left-session"),
                lambda value: message_ids(value) == [reverse_id],
            )
            ack(left, LEFT_ACTOR, "left-session", reverse_id)

            compatible = left.command(
                "message",
                "send",
                "--from",
                "regfix-left",
                "--to",
                RIGHT_ACTOR,
                "--summary",
                "bare-compatible",
                "bare-compatible",
                "--json",
            )
            compatible_id = str(compatible["messageId"])
            compatible_pending = right.wait(
                lambda: pending(right, RIGHT_ACTOR, "right-session"),
                lambda value: message_ids(value) == [compatible_id],
            )
            (compatible_message,) = compatible_pending["messages"]
            if compatible_message.get("summary") != "bare-compatible":
                raise AssertionError(
                    f"bare delivery summary mismatch: {compatible_pending}"
                )
            body = read_body(right, RIGHT_ACTOR, "right-session", compatible_id)
            if body.get("message") != "bare-compatible":
                raise AssertionError(
                    f"bare delivery body mismatch: {body}"
                )
            ack(right, RIGHT_ACTOR, "right-session", compatible_id)

            left_shared = register(left, "shared", "left-shared-session")
            right_shared = register(right, "shared", "right-shared-session")
            shared_uris = {
                str(left_shared["actor"]),
                str(right_shared["actor"]),
            }
            left.wait(
                lambda: left.command("message", "targets", "--json"),
                lambda value: shared_uris.issubset(
                    {
                        str(item.get("actor"))
                        for item in value.get("targets", [])
                        if isinstance(item, dict)
                    }
                ),
            )
            before = left.ps()["outboxCount"]
            bounced = left.command(
                "message",
                "send",
                "--from",
                "regfix-left",
                "--to",
                "shared",
                "--summary",
                "ambiguous-must-bounce",
                "ambiguous-must-bounce",
                "--json",
                exits=(1,),
            )
            if bounced.get("code") != "AMBIGUOUS_TARGET":
                raise AssertionError(f"ambiguous bare send did not bounce: {bounced}")
            if left.ps()["outboxCount"] != before:
                raise AssertionError("ambiguous bare send created an outbox row")

            print(
                "EVIDENCE "
                + json.dumps(
                    {
                        "registration": registration["actor"],
                        "reverseRegistration": left_registration["actor"],
                        "canonicalTarget": direct["deliveries"][0]["target"],
                        "reverseCanonicalTarget": reverse["deliveries"][0]["target"],
                        "bareResolvedTarget": compatible["deliveries"][0]["target"],
                        "ambiguousCode": bounced["code"],
                        "ambiguousCandidates": bounced.get("data", {}).get(
                            "candidates"
                        ),
                        "outboxUnchanged": True,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
    print(
        "RESULT PASS actor-registration canonical=1 direct-bidirectional=1 "
        "bare-unique=1 ambiguous-bounce=1 cleanup=1"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    # PR #332 F4②: a surfaced runner transient is the shared registry class
    # (RuntimeError-based), no longer an AssertionError subclass; keep it in
    # the failure report so the RESULT FAIL line still prints for it.
    except (AssertionError, KeyError, OSError, TransientDaemonError) as error:
        print(f"RESULT FAIL actor-registration {error}")
        raise SystemExit(1) from error
