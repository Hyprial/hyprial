#!/usr/bin/env python3
"""Reproduce the managed Pi post-turn wake/registration gap in isolation.

This is a focused defect runner, not a numbered release scenario.  It launches
one daemon-managed Pi RPC worker, waits for every message to reach a worker turn
terminal event before sending the next one, and preserves the public trajectory
and log evidence for each message.  Every HYPRIAL path is rooted below a disposable
directory and Zenoh listens on a dynamically allocated loopback port.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.application import DaemonApplication

import sys

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _daemon_runner import (
    ROOT,
    IsolatedDaemon,
    free_tcp_endpoint,
    hyprial_executable,
    short_temp_root,
    teardown_guard,
)


TERMINAL_TURN_EVENTS = {
    "worker.turn.completed",
    "worker.turn.failed",
    "worker.turn.interrupted",
}
WAKE_EVENTS = {
    "wake.signalled",
    "wake.busy",
    "wake.offline",
    "wake.failed",
}


# CLI probe-sender persona (registered at daemon start; Allen b).
DRIVER = "e2e011-driver"


def _json_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _daemon_pid(daemon: IsolatedDaemon) -> int:
    value = daemon.ps()
    metadata = value.get("daemon")
    pid = metadata.get("pid") if isinstance(metadata, dict) else None
    if not isinstance(pid, int) or pid <= 0:
        raise AssertionError(f"daemon ps returned invalid pid: {metadata}")
    return pid


def _process_rows() -> list[tuple[int, int, str, str, str]]:
    completed = subprocess.run(
        ["ps", "-ww", "-axo", "pid=,ppid=,stat=,time=,command="],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise OSError(completed.stderr.strip() or "ps process inspection failed")
    rows: list[tuple[int, int, str, str, str]] = []
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=4)
        if len(fields) != 5:
            continue
        try:
            rows.append(
                (int(fields[0]), int(fields[1]), fields[2], fields[3], fields[4])
            )
        except ValueError:
            continue
    return rows


def _alive(pids: tuple[int, ...]) -> tuple[int, ...]:
    current = {
        pid for pid, _parent, _stat, _cpu_time, _command in _process_rows()
    }
    return tuple(pid for pid in pids if pid in current)


def _ps_snapshot(pid: int | None) -> dict[str, Any]:
    """Capture an explicit pid+ps liveness fact without logging command args."""

    if pid is None:
        return {"pid": None, "psVisible": False, "alive": False}
    row = next((item for item in _process_rows() if item[0] == pid), None)
    if row is None:
        return {"pid": pid, "psVisible": False, "alive": False}
    _pid, parent, status, cpu_time, command = row
    return {
        "pid": pid,
        "ppid": parent,
        "psVisible": True,
        "alive": True,
        "status": status,
        "cpuTime": cpu_time,
        "executable": Path(command.split(maxsplit=1)[0]).name,
    }


def _nodes(trajectory: dict[str, Any]) -> list[dict[str, Any]]:
    value = trajectory.get("nodes", [])
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def _events(trajectory: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(item["event"])
        for item in _nodes(trajectory)
        if isinstance(item.get("event"), str)
    )


def _wait_for_turn_terminal(
    daemon: IsolatedDaemon, message_id: str, timeout: float
) -> tuple[dict[str, Any], bool]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = daemon.command("message", "trajectory", message_id, "--json")
        if any(event in TERMINAL_TURN_EVENTS for event in _events(last)):
            return last, True
        time.sleep(0.25)
    return last, False


def _wait_for_event(
    daemon: IsolatedDaemon,
    message_id: str,
    event: str,
    timeout: float,
) -> tuple[dict[str, Any], bool]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = daemon.command("message", "trajectory", message_id, "--json")
        if event in _events(last):
            return last, True
        time.sleep(0.1)
    return last, False


def _wait_for_reply(
    daemon: IsolatedDaemon,
    *,
    conversation_id: str,
    request_id: str,
    timeout: float = 20.0,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = daemon.ps()
        pending = state.get("pendingMessages", [])
        if isinstance(pending, list):
            reply = next(
                (
                    item
                    for item in pending
                    if isinstance(item, dict)
                    and item.get("conversationId") == conversation_id
                    and item.get("messageId") != request_id
                ),
                None,
            )
            if reply is not None:
                return reply
        time.sleep(0.25)
    return None


def _classify(trajectory: dict[str, Any]) -> str:
    events = _events(trajectory)
    wakes = [event for event in events if event in WAKE_EVENTS]
    if wakes:
        return wakes[-1]
    if "worker.turn.started" in events:
        return "managed-streaming-dispatch (wake coordinator bypassed)"
    return "no wake event and no worker turn"


def _registration_state(registration: object | None) -> dict[str, Any]:
    """Describe registration handles recursively, without mutating them."""

    if registration is None:
        return {"present": False}
    value: dict[str, Any] = {
        "present": True,
        "type": type(registration).__name__,
    }
    closed = getattr(registration, "_closed", None)
    if isinstance(closed, bool):
        value["closed"] = closed
    actor = getattr(registration, "_actor", None)
    if isinstance(actor, str):
        value["actor"] = actor
        owner = getattr(registration, "_owner", None)
        owner_lock = getattr(owner, "_lock", None)
        owner_actors = getattr(owner, "_actors", None)
        if owner_lock is not None and isinstance(owner_actors, dict):
            with owner_lock:
                value["ownerActorCount"] = owner_actors.get(actor, 0)
                value["ownerHasActor"] = actor in owner_actors
    token = getattr(registration, "_token", None)
    endpoint = getattr(registration, "_endpoint", None)
    if token is not None:
        value["token"] = _registration_state(token)
    if endpoint is not None:
        value["endpoint"] = _registration_state(endpoint)
    registrations = getattr(registration, "_registrations", None)
    if isinstance(registrations, list):
        children = [_registration_state(item) for item in registrations]
        value["registrationCount"] = len(children)
        value["registrations"] = children
        value["allHandlesOpen"] = all(
            child.get("closed") is False for child in children
        )
    return value


def _rpc_session_state(process: object | None, events: tuple[str, ...]) -> tuple[
    dict[str, Any], int | None
]:
    if process is None:
        return ({"wrapperPresent": False, "assessment": "missing"}, None)
    lock = getattr(process, "_lock", None)
    if lock is None:
        return (
            {
                "wrapperPresent": True,
                "wrapperType": type(process).__name__,
                "assessment": "uninspectable",
            },
            None,
        )
    with lock:
        client = getattr(process, "_client", None)
        child = getattr(client, "_process", None) if client is not None else None
        pid = getattr(child, "pid", None)
        returncode = getattr(child, "returncode", None)
        stdin = getattr(child, "stdin", None)
        stdout = getattr(child, "stdout", None)
        stdin_open = (
            stdin is not None
            and callable(getattr(stdin, "is_closing", None))
            and not stdin.is_closing()
        )
        stdout_at_eof = (
            stdout.at_eof()
            if stdout is not None and callable(getattr(stdout, "at_eof", None))
            else None
        )
        if "worker.turn.completed" in events:
            assessment = "turn-roundtrip-confirmed"
        elif "worker.turn.started" in events:
            assessment = "dispatch-entered-no-terminal-response"
        elif client is not None and returncode is None and stdin_open:
            # Open asyncio objects plus a live pid are only structural facts.
            # There is no concurrency-safe idle Pi ping: the pump owns stdout.
            assessment = "transport-open-unprobed"
        elif client is None:
            assessment = "client-missing"
        else:
            assessment = "transport-not-open"
        return (
            {
                "wrapperPresent": True,
                "wrapperType": type(process).__name__,
                "pumpThreadAlive": bool(
                    getattr(
                        getattr(process, "_thread", None),
                        "is_alive",
                        lambda: False,
                    )()
                ),
                "stopping": bool(
                    getattr(
                        getattr(process, "_stopping", None),
                        "is_set",
                        lambda: False,
                    )()
                ),
                "clientPresent": client is not None,
                "clientType": type(client).__name__ if client is not None else None,
                "childPid": pid if isinstance(pid, int) else None,
                "childReturncode": returncode,
                "stdinOpen": stdin_open,
                "stdoutAtEof": stdout_at_eof,
                "turnId": getattr(client, "_turn_id", None),
                "activeDeliveryId": getattr(process, "_active_delivery_id", None),
                "knownDeliveryIds": sorted(getattr(process, "_known", set())),
                "lastError": getattr(process, "last_error", None),
                "assessment": assessment,
                "assessmentLimit": (
                    "idle pipe state is not a protocol roundtrip; only worker.ready "
                    "or a terminal turn confirms RPC response flow"
                ),
            },
            pid if isinstance(pid, int) else None,
        )


def _three_layer_snapshot(
    app: DaemonApplication,
    daemon: IsolatedDaemon,
    *,
    actor: str,
    canonical_actor: str,
    label: str,
    trajectory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Take a near-simultaneous process/session/registration observation."""

    events = _events(trajectory or {})
    captured = datetime.now(UTC).isoformat()
    with app._ipc_request_lock:
        runtime = app._runtime
        harnesses = app._harnesses
        process = (
            harnesses._processes.get(f"pi:{actor}")
            if harnesses is not None
            else None
        )
        session, child_pid = _rpc_session_state(process, events)
        registrations = runtime._actor_registrations if runtime is not None else {}
        registration = registrations.get(actor)
        registration_layer = {
            "registryKind": "daemon-managed-harness-actor",
            "lookupKey": actor,
            "canonicalActor": canonical_actor,
            "mapEntryPresent": actor in registrations,
            "mapKeys": sorted(registrations),
            "entry": _registration_state(registration),
        }
    # ps and public CLI probes happen immediately after the locked in-memory
    # read. They are intentionally outside the lock so IPC cannot deadlock.
    process_layer = _ps_snapshot(child_pid)
    public = {
        "ps": daemon.ps(),
        "targets": daemon.command("message", "targets", "--kind", "agent", "--json"),
    }
    return {
        "label": label,
        "capturedAt": captured,
        "completedAt": datetime.now(UTC).isoformat(),
        "eventEvidence": list(events),
        "processLayer": process_layer,
        "rpcStdioLayer": session,
        "registrationLayer": registration_layer,
        "public": public,
    }


def _invalidate_registration(app: DaemonApplication, actor: str) -> dict[str, Any]:
    """Test-only deterministic fault: close the retained entry but keep its key."""

    with app._ipc_request_lock:
        if app._runtime is None:
            raise AssertionError("daemon runtime unavailable for fault injection")
        registration = app._runtime._actor_registrations.get(actor)
        if registration is None:
            raise AssertionError(
                f"actor registration missing before injection: {actor}"
            )
        before = _registration_state(registration)
        registration.close(
            reason="fault-injection",
            initiator="piwake-runner",
        )
        after = _registration_state(registration)
    return {
        "mode": "close-registration-retain-map-key",
        "injectedAt": datetime.now(UTC).isoformat(),
        "before": before,
        "after": after,
    }


def _actor_registration_present(app: DaemonApplication, actor: str) -> bool:
    with app._ipc_request_lock:
        return (
            app._runtime is not None
            and actor in app._runtime._actor_registrations
        )


def _send_one(
    daemon: IsolatedDaemon,
    app: DaemonApplication,
    *,
    actor: str,
    canonical_actor: str,
    target: str,
    send_mode: str,
    sequence: int,
    timeout: float,
) -> dict[str, Any]:
    conversation_id = f"piwake-{time.time_ns()}-{sequence}"
    prompt = f"Reply with exactly PIWAKE_{sequence}_OK and nothing else."
    snapshots = [
        _three_layer_snapshot(
            app,
            daemon,
            actor=actor,
            canonical_actor=canonical_actor,
            label="before-send",
        )
    ]
    if send_mode == "cli":
        sent = daemon.command(
            "message",
            "send",
            "--from",
            DRIVER,
            "--to",
            target,
            "--conversation",
            conversation_id,
            "--summary",
            prompt,
            prompt,
            "--json",
        )
    else:
        native_message_id = f"om-piwake-{time.time_ns()}-{sequence}"
        channel_actor = "adapter:lark:piwake-isolated:bot"
        # This is the exact daemon IPC shape used by
        # hyprial.adapters.lark.worker._Harness.send_request after the live adapter
        # has routed an inbound event.  It deliberately does not claim a live
        # Lark SDK/websocket roundtrip.
        sent = daemon.request(
            "message.send",
            {
                "actor": channel_actor,
                "to": [target],
                "summary": prompt,
                "message": prompt,
                "conversationId": conversation_id,
                "providerMetadata": {
                    "provider": "lark",
                    "eventId": f"evt-piwake-{sequence}",
                    "messageId": native_message_id,
                    "chatId": "oc-piwake-isolated",
                    "chatType": "p2p",
                    "chatKind": "dm",
                    "senderId": "ou-piwake-isolated",
                    "senderType": "user",
                    "messageType": "text",
                },
                "idempotencyKey": (
                    f"lark-inbound:{channel_actor}:{native_message_id}"
                ),
            },
        )
    message_id = sent.get("messageId")
    if not isinstance(message_id, str) or not message_id:
        raise AssertionError(f"send returned no messageId: {sent}")
    received_trajectory, send_received = _wait_for_event(
        daemon,
        message_id,
        "send.received",
        min(timeout, 10.0),
    )
    snapshots.append(
        _three_layer_snapshot(
            app,
            daemon,
            actor=actor,
            canonical_actor=canonical_actor,
            label="after-send-received-or-timeout",
            trajectory=received_trajectory,
        )
    )
    trajectory, terminal = _wait_for_turn_terminal(daemon, message_id, timeout)
    snapshots.append(
        _three_layer_snapshot(
            app,
            daemon,
            actor=actor,
            canonical_actor=canonical_actor,
            label="after-turn-terminal-or-timeout",
            trajectory=trajectory,
        )
    )
    related_log = daemon.command(
        "log", "--correlation-id", message_id, "--json"
    )
    reply = _wait_for_reply(
        daemon,
        conversation_id=conversation_id,
        request_id=message_id,
    ) if terminal else None
    ack: dict[str, Any] | None = None
    if reply is not None and isinstance(reply.get("messageId"), str):
        ack = daemon.command(
            "message",
            "ack",
            "--from",
            DRIVER,
            str(reply["messageId"]),
            "--json",
        )
    events = _events(trajectory)
    terminal_events = [event for event in events if event in TERMINAL_TURN_EVENTS]
    return {
        "sequence": sequence,
        "sendMode": send_mode,
        "conversationId": conversation_id,
        "messageId": message_id,
        "prompt": prompt,
        "send": sent,
        "sendReceivedObserved": send_received,
        "terminalObserved": terminal,
        "terminalEvents": terminal_events,
        "wakeEvents": [event for event in events if event in WAKE_EVENTS],
        "observedBranch": _classify(trajectory),
        "trajectory": trajectory,
        "log": related_log,
        "reply": reply,
        "replyAck": ack,
        "layerSnapshots": snapshots,
    }


def _copy_evidence(root: Path, output: Path, report: dict[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    _json_write(output / "report.json", report)
    logs = root / "state" / "logs"
    if logs.is_dir():
        shutil.copytree(logs, output / "logs", dirs_exist_ok=True)
    for name in ("desired-state.json.v1", "daemon-launch.log"):
        source = root / "state" / name
        if source.is_file():
            shutil.copy2(source, output / name)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default="deepseek")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--count", type=int, default=2)
    parser.add_argument("--send-mode", choices=("cli", "adapter-ipc"), default="cli")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--turn-timeout", type=float, default=180.0)
    parser.add_argument(
        "--fault-after-first",
        choices=("none", "close-registration"),
        default="none",
        help="test-only fault injection, always reported separately",
    )
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()
    if args.count < 2:
        parser.error("--count must be at least 2")
    if args.interval < 0 or args.turn_timeout <= 0:
        parser.error("--interval must be non-negative and --turn-timeout positive")
    return args


def main() -> int:
    args = parse_args()
    executable = hyprial_executable()
    actor = "piwake-squire"
    report: dict[str, Any] = {
        "schemaVersion": 2,
        "scenario": "pi-wake-registration-gap",
        "startedAt": datetime.now(UTC).isoformat(),
        "checkoutSha": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        ).stdout.strip(),
        "hyprialExecutable": str(executable),
        "provider": args.provider,
        "model": args.model,
        "sendMode": args.send_mode,
        "faultAfterFirst": args.fault_after_first,
        "daemonMode": "in-process-readonly-inspection",
        "messages": [],
    }
    prefix = "hyprial-piwake-"
    with tempfile.TemporaryDirectory(prefix=prefix, dir=short_temp_root()) as temporary:
        root = Path(temporary)
        (root / "work").mkdir()
        # Only `hyprial init` creates a home in the product; the runner creates
        # its disposable home explicitly, like a fixture.
        (root / "home").mkdir(mode=0o700)
        endpoint = free_tcp_endpoint()
        if (root / "home").resolve() == (Path.home() / ".hyprial").resolve():
            raise AssertionError("isolated HYPRIAL_HOME resolved to production ~/.hyprial")
        if endpoint.endswith(":7447"):
            raise AssertionError(
                "dynamic endpoint unexpectedly selected forbidden port 7447"
            )
        daemon = IsolatedDaemon(
            executable,
            root,
            f"piwake-node-{os.getpid()}",
            listen=endpoint,
        )
        daemon.env["HYPRIAL_BIN"] = str(executable)
        daemon.env["PI_CODING_AGENT_SESSION_DIR"] = str(root / "pi-sessions")
        daemon.env["PI_TELEMETRY"] = "0"
        daemon.env.pop("HYPRIAL_ZENOH_CONNECT", None)
        # The application runs in this isolated runner process so its private
        # lifecycle state can be sampled read-only. Managed Pi children inherit
        # os.environ, so pin the same disposable paths there as the public CLI.
        for key in (
            "HYPRIAL_HOME",
            "HARNESS_STATE_DIR",
            "HYPRIAL_NODE_ID",
            "HYPRIAL_ZENOH_LISTEN",
            "HYPRIAL_BIN",
            "PI_CODING_AGENT_SESSION_DIR",
            "PI_TELEMETRY",
        ):
            os.environ[key] = daemon.env[key]
        os.environ.pop("HYPRIAL_ZENOH_CONNECT", None)
        os.environ.pop("HARNESS_SOCKET_PATH", None)
        report["isolation"] = {
            "hyprialHome": str(root / "home"),
            "stateDir": str(root / "state"),
            "socket": str(root / "state" / "daemon.sock"),
            "zenohListen": endpoint,
            "piSessionDir": str(root / "pi-sessions"),
            "ambientSocketOverrideRemoved": "HARNESS_SOCKET_PATH" not in daemon.env,
        }
        app = DaemonApplication(
            state_dir=root / "state",
            socket_path=root / "state" / "daemon.sock",
            node_id=daemon.node_id,
            hyprial_home=root / "home",
            zenoh_listen=(endpoint,),
            zenoh_connect=(),
        )
        started = False
        owner_pid: int | None = None
        watched_child_pids: set[int] = set()
        daemon_errors: list[str] = []

        def run_daemon() -> None:
            try:
                app.run()
            except BaseException as error:
                daemon_errors.append(f"{type(error).__name__}: {error}")

        daemon_thread = threading.Thread(
            target=run_daemon,
            name="piwake-isolated-daemon",
            daemon=True,
        )

        def stop_connectors() -> None:
            if started:
                daemon.command("agent", "stop", "--all", "--json")

        def stop_daemon() -> None:
            if started:
                daemon.stop()
                daemon_thread.join(timeout=10.0)
                if daemon_thread.is_alive():
                    raise AssertionError("in-process daemon thread did not stop")

        try:
            daemon_thread.start()
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                if daemon_errors:
                    raise AssertionError(
                        f"in-process daemon failed to start: {daemon_errors[-1]}"
                    )
                if (root / "state" / "daemon.json").exists():
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("in-process daemon did not become ready")
            started = True
            # resolve-or-reject (Allen b): the CLI probe sender must be a
            # registered identity — no more bare node_id as sender.
            daemon.command("agent", "create", "--name", DRIVER, "--json")
            owner_pid = _daemon_pid(daemon)
            if owner_pid != os.getpid():
                raise AssertionError(
                    f"in-process daemon reported unexpected pid {owner_pid}"
                )
            launched = daemon.command(
                "agent",
                "start",
                "pi",
                "--name",
                actor,
                "--nickname",
                actor,
                "--cwd",
                str(root / "work"),
                "--headless",
                "--json",
                "--",
                "--provider",
                args.provider,
                "--model",
                args.model,
                "--system-prompt",
                "Return only the exact short token requested by the user.",
                "--no-builtin-tools",
                "--no-skills",
                "--no-context-files",
                "--no-extensions",
                "--no-prompt-templates",
                "--no-themes",
                "--session-dir",
                str(root / "pi-sessions"),
                "--approve",
            )
            report["launch"] = launched
            canonical_actor = launched.get("actor")
            if not isinstance(canonical_actor, str) or not canonical_actor:
                raise AssertionError(f"launch returned no canonical actor: {launched}")
            daemon.wait(
                lambda: daemon.ps(),
                lambda value: any(
                    isinstance(item, dict)
                    and item.get("id") == f"pi:{actor}"
                    and item.get("running") is True
                    for item in value.get("connectors", [])
                ),
                timeout=20.0,
            )
            ready_log = daemon.wait(
                lambda: daemon.command(
                    "log",
                    "--component",
                    "pi-worker",
                    "--name",
                    actor,
                    "--json",
                ),
                lambda value: any(
                    isinstance(item, dict) and item.get("event") == "worker.ready"
                    for item in value.get("entries", [])
                ),
                timeout=20.0,
            )
            report["startupRpcProbe"] = {
                "workerReadyObserved": True,
                "entries": [
                    item
                    for item in ready_log.get("entries", [])
                    if isinstance(item, dict)
                    and item.get("event") in {"worker.started", "worker.ready"}
                ],
            }
            daemon.wait(
                lambda: _actor_registration_present(app, actor),
                bool,
                timeout=5.0,
            )
            with teardown_guard(stop_connectors, stop_daemon):
                for sequence in range(1, args.count + 1):
                    if sequence > 1 and args.interval:
                        time.sleep(args.interval)
                    observation = _send_one(
                        daemon,
                        app,
                        actor=actor,
                        canonical_actor=canonical_actor,
                        target=actor if args.send_mode == "cli" else canonical_actor,
                        send_mode=args.send_mode,
                        sequence=sequence,
                        timeout=args.turn_timeout,
                    )
                    report["messages"].append(observation)
                    for snapshot in observation["layerSnapshots"]:
                        child_pid = snapshot["processLayer"].get("pid")
                        if isinstance(child_pid, int):
                            watched_child_pids.add(child_pid)
                    status = "PASS" if observation["terminalObserved"] else "FAIL"
                    print(
                        f"RESULT {status} PIWAKE turn-{sequence} "
                        f"messageId={observation['messageId']} "
                        f"branch={observation['observedBranch']} "
                        f"terminal={observation['terminalEvents']}",
                        flush=True,
                    )
                    if (
                        sequence == 1
                        and args.fault_after_first == "close-registration"
                    ):
                        report["faultInjection"] = _invalidate_registration(app, actor)
                        report["faultInjection"]["snapshot"] = _three_layer_snapshot(
                            app,
                            daemon,
                            actor=actor,
                            canonical_actor=canonical_actor,
                            label="immediately-after-registration-fault",
                            trajectory=observation["trajectory"],
                        )
                        attribution_log = daemon.command(
                            "log", "--component", "daemon", "--json"
                        )
                        report["faultInjection"]["attributionEvents"] = [
                            item
                            for item in attribution_log.get("entries", [])
                            if isinstance(item, dict)
                            and str(item.get("event", "")).startswith(
                                "harness.actor.registration."
                            )
                        ]
        except BaseException as error:
            report["error"] = f"{type(error).__name__}: {error}"
            print(f"RESULT FAIL PIWAKE runner {report['error']}", flush=True)
        finally:
            if started and (root / "state" / "daemon.sock").exists():
                try:
                    stop_connectors()
                except BaseException as error:
                    report.setdefault("teardownErrors", []).append(
                        f"down: {type(error).__name__}: {error}"
                    )
                try:
                    stop_daemon()
                except BaseException as error:
                    report.setdefault("teardownErrors", []).append(
                        f"daemon: {type(error).__name__}: {error}"
                    )
            if daemon_thread.is_alive():
                app.stop_event.set()
                if app._server is not None:
                    app._server.close()
                daemon_thread.join(timeout=10.0)
                if daemon_thread.is_alive():
                    report.setdefault("teardownErrors", []).append(
                        "daemon: in-process daemon thread did not stop"
                    )
            if daemon_errors:
                report["daemonThreadErrors"] = daemon_errors
            watched = tuple(sorted(watched_child_pids))
            deadline = time.monotonic() + 5.0
            leaked = _alive(watched)
            while leaked and time.monotonic() < deadline:
                time.sleep(0.1)
                leaked = _alive(watched)
            report["cleanup"] = {
                "watchedPids": list(watched),
                "leakedPids": list(leaked),
                "clean": (
                    not leaked
                    and not report.get("teardownErrors")
                    and not daemon_thread.is_alive()
                ),
                "daemonThreadAlive": daemon_thread.is_alive(),
                "daemonPidWasRunnerPid": owner_pid == os.getpid(),
            }
            evidence = args.evidence_dir
            if evidence is not None:
                _copy_evidence(root, evidence.expanduser().resolve(), report)

    messages = report.get("messages", [])
    complete = (
        len(messages) == args.count
        and all(item.get("terminalObserved") for item in messages)
        and report.get("cleanup", {}).get("clean") is True
        and "error" not in report
    )
    print(
        "RESULT " + ("PASS" if complete else "FAIL")
        + " PIWAKE pi-wake-registration-gap "
        + f"turns={len(messages)}/{args.count} "
        + f"cleanup={report.get('cleanup', {}).get('clean')}",
        flush=True,
    )
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
