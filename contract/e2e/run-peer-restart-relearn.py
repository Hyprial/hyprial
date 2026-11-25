#!/usr/bin/env python3
"""E2E-024: positive control for a v6 leased-roster slot-pool-growth rebuild.

Directory-address changes now rebind fixed slots without rebuilding. This
scenario deliberately exhausts a one-slot pool so growth exercises the sole
remaining forwarding-driven ``reconfigure_connect`` path, then proves a
restarted peer re-learns the rebuilt zid and messaging recovers.
"""

from __future__ import annotations

import json
import os
import runpy
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _daemon_runner import (  # noqa: E402
    IsolatedDaemon,
    artifacts_root,
    hyprial_executable,
    launch_result_is_ready,
    scenario_tempdir,
    short_temp_root,
)
from _tailcat_isolation import local_topology_violations  # noqa: E402
from _peer_restart_stream import RespliceForwarder  # noqa: E402
from _peer_restart_roster import local_member, plant_device  # noqa: E402
from _peer_restart_parallel import run_cases  # noqa: E402
from _peer_restart_send import ScenarioRequestError, SubmissionFailure, submit_once  # noqa: E402
from hyprial.kernel import RequestPortError, daemon_request_once  # noqa: E402
from hyprial.daemon.impl.transport.zenoh.config import (  # noqa: E402
    DEFAULT_CONNECT_RETRY_MAX_MS,
    DEFAULT_LEASE_MS,
)

SCENARIO = "E2E-024"
DRIVER = "peer-restart-driver"
LEASE_MS = DEFAULT_LEASE_MS
CONNECT_RETRY_MAX_MS = DEFAULT_CONNECT_RETRY_MAX_MS
STALE_THRESHOLD_SECONDS = (LEASE_MS + CONNECT_RETRY_MAX_MS) / 1_000
OBSERVATION_TIMEOUT_SECONDS = 3 * STALE_THRESHOLD_SECONDS
OVERALL_TIMEOUT_SECONDS = 300


def _emit(**event: object) -> None:
    print(json.dumps({"v": 1, **event}, separators=(",", ":")), flush=True)


_FORWARDING = runpy.run_path(str(Path(__file__).with_name("run-orgfs-forwarding-redial.py")))
_LOCAL_MEMBER = partial(local_member, _FORWARDING["_fixture_member"])
ScenarioTimeout = _FORWARDING["ScenarioTimeout"]
_INTERRUPTED: BaseException | None = None


def _configure_forwarding(
    daemon: IsolatedDaemon, *, listen: str, receipt: Path, pid_path: Path
) -> None:
    _FORWARDING["_prepare_loopback"](daemon)
    _FORWARDING["_configure_forwarding"](
        daemon, listen=listen, receipt=receipt, pid_path=pid_path,
        member_factory=_LOCAL_MEMBER,
        sidecar_command=[sys.executable, str(Path(__file__).resolve()), "--sidecar", str(receipt), str(pid_path)],
    )


def _start_forwarding_loopback(daemon: IsolatedDaemon) -> dict[str, Any]:
    daemon.require_no_external_triggers()
    result = daemon.command("init", "--json", timeout=60)
    if not launch_result_is_ready(result):
        raise AssertionError(f"daemon did not become ready: {result}")
    daemon.require_no_external_triggers()
    status = daemon.ps()
    zenoh = status.get("zenoh")
    endpoints = (
        [*zenoh.get("listen", []), *zenoh.get("connect", [])]
        if isinstance(zenoh, dict)
        else []
    )
    if not endpoints or local_topology_violations(endpoints, getattr(daemon, 'root', None)):
        raise AssertionError(f"daemon escaped the loopback topology: {endpoints}")
    return status

def _events(daemon: IsolatedDaemon, name: str | None = None) -> list[dict[str, Any]]:
    path = daemon.root / "state" / "logs" / "daemon.jsonl"
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and (name is None or value.get("event") == name):
            events.append(value)
    return events


def _wait(
    description: str,
    probe: Callable[[], Any],
    accept: Callable[[Any], bool],
    *,
    timeout: float,
) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = probe()
        except (
            AssertionError,
            OSError,
            socket.timeout,
            subprocess.TimeoutExpired,
        ) as error:
            last = f"{type(error).__name__}: {error}"
        else:
            if accept(last):
                return last
        time.sleep(0.1)
    raise AssertionError(
        f"{description} did not complete within {timeout:g}s; last={last!r}"
    )


def _request(daemon: IsolatedDaemon, method: str, params: dict[str, Any], *, timeout: float = 4) -> dict[str, Any]:
    try:
        result = daemon_request_once(method, params, socket_path=daemon.root / "state/daemon.sock", timeout=timeout)
    except RequestPortError as error:
        raise ScenarioRequestError(method, error) from error
    if not isinstance(result, dict):
        raise AssertionError(f"{method} returned no object: {result}")
    return result


def _daemon_pid(daemon: IsolatedDaemon) -> int | None:
    path = daemon.root / "state" / "daemon.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    pid = value.get("pid") if isinstance(value, dict) else None
    return pid if isinstance(pid, int) and pid > 1 else None


def _pid_from(path: Path) -> int | None:
    try:
        value = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return value if value > 1 else None


def _alive(pid: int) -> bool:
    return _FORWARDING["_alive"](pid)


def _stop_pid(pid: int | None, *, label: str) -> None:
    _FORWARDING["_stop_pid"](pid, label=label)


def _kill(daemon: IsolatedDaemon) -> int:
    pid = _daemon_pid(daemon)
    if pid is None:
        raise AssertionError(f"{daemon.node_id} has no daemon pid")
    os.kill(pid, signal.SIGKILL)
    _wait(
        f"{daemon.node_id} pid exit",
        lambda: _alive(pid),
        lambda alive: not alive,
        timeout=5,
    )
    return pid


def _host_online(daemon: IsolatedDaemon, node_id: str) -> bool:
    value = _request(daemon, "hosts", {})
    return any(
        isinstance(item, dict)
        and item.get("nodeId") == node_id
        and item.get("status") == "online"
        for item in value.get("hosts", [])
    )


def _target_online(daemon: IsolatedDaemon, actor: str) -> bool:
    value = _request(daemon, "targets", {})
    return any(
        isinstance(item, dict)
        and item.get("actor") == actor
        and item.get("status") == "online"
        for item in value.get("targets", [])
    )


def _owner(daemon: IsolatedDaemon) -> str:
    value = daemon.ps()
    owner = value.get("daemon", {}).get("owner")
    if not isinstance(owner, str) or not owner:
        raise AssertionError(f"missing daemon owner: {value}")
    return owner


def _actor_uri(daemon: IsolatedDaemon) -> str:
    row = next(row for row in _request(daemon, "agent.list", {})["agents"] if row.get("actor") == DRIVER)
    uri = row.get("uri")
    if not isinstance(uri, str) or not uri.startswith(f"agent:{_owner(daemon)}:{daemon.node_id}:a."):
        raise AssertionError(f"driver has no official actor identity: {row}")
    return uri


def _pending_ids(daemon: IsolatedDaemon) -> set[str]:
    value = daemon.ps()
    return {
        str(item["messageId"])
        for item in value.get("pendingMessages", [])
        if isinstance(item, dict) and isinstance(item.get("messageId"), str)
    }


def _try_send(
    sender: IsolatedDaemon, receiver: IsolatedDaemon, text: str
) -> dict[str, object]:
    submission = {}
    phase = "target"
    try:
        target = _actor_uri(receiver)
        phase = "submit"
        result, submission = submit_once(sender, request=_request, sender=DRIVER, target=target, text=text)
        message_id = result.get("messageId")
        if not isinstance(message_id, str):
            return {"delivered": False, "error": f"no message id: {result}"}
        try:
            phase = "delivery"
            _wait(
                f"delivery {sender.node_id} to {receiver.node_id}",
                lambda: _pending_ids(receiver),
                lambda ids: message_id in ids,
                timeout=3,
            )
        except AssertionError as error:
            return {
                "delivered": False,
                "messageId": message_id,
                "submission": submission,
                "phase": phase,
                "error": str(error),
            }
        phase = "ack"
        acknowledged = _request(receiver, "message.ack", {"from": DRIVER, "messageId": message_id})
        return {
            "delivered": acknowledged.get("acknowledged") is True,
            "messageId": message_id,
            "acknowledged": acknowledged.get("acknowledged") is True,
            "submission": submission,
            "phase": "acknowledged" if acknowledged.get("acknowledged") is True else phase,
        }
    except SubmissionFailure as error:
        return {"delivered": False, "phase": phase, "error": f"{type(error).__name__}: {error}", "submission": error.evidence}
    except (AssertionError, OSError, subprocess.SubprocessError) as error:
        return {"delivered": False, "phase": phase, "error": f"{type(error).__name__}: {error}", "submission": submission}


def _zids(daemon: IsolatedDaemon) -> list[str]:
    return [
        str(event["zid"])
        for event in _events(daemon, "zenoh.session.opened")
        if isinstance(event.get("zid"), str)
    ]


def _current_zid(daemon: IsolatedDaemon) -> str | None:
    zids = [
        str(event["zid"])
        for event in _events(daemon)
        if event.get("event")
        in {"zenoh.session.opened", "zenoh.session.rebuilt"}
        and isinstance(event.get("zid"), str)
    ]
    return zids[-1] if zids else None


def _stream_states(forwarder: RespliceForwarder) -> list[dict[str, object]]:
    latest: dict[tuple[object, ...], dict[str, object]] = {}
    for event in forwarder.snapshot():
        edge = event.get("edge")
        if edge in {"client", "reverse-client"}:
            key = (edge, event.get("generation"))
        elif edge in {"upstream", "upstream-to-client"}:
            key = (
                edge,
                event.get("clientGeneration"),
                event.get("generation"),
            )
        else:
            key = (edge, event.get("clientGeneration"))
        latest[key] = event
    return list(latest.values())


def _run_case(
    root: Path,
    *,
    mode: str,
    resplice_delay_seconds: float,
) -> dict[str, object]:
    delay_name = str(resplice_delay_seconds).replace(".", "-")
    name = f"{mode}-delay-{delay_name}"
    root.mkdir(parents=True, mode=0o700)
    for node in ("a", "b"):
        (root / node).mkdir(mode=0o700)
    a_listen = f"unixsock-stream/{root}/a/zenoh.sock"
    b_listen = f"unixsock-stream/{root}/b/zenoh.sock"
    receipt = root / "forwarding-receipt.jsonl"
    sidecar_pid_path = root / "forwarding-sidecar.pid"
    sidecar_pid_b = root / "forwarding-sidecar-b.pid"
    daemon_a = IsolatedDaemon(
        hyprial_executable(), root / "a", f"orgfs-redial-a-{name}", listen=a_listen
    )
    daemon_b = IsolatedDaemon(
        hyprial_executable(),
        root / "b",
        f"peer-restart-b-{name}",
        listen=b_listen,
    )
    daemon_a.env["HYPRIAL_OWNER"] = f"restart-a-{name}"
    daemon_b.env["HYPRIAL_OWNER"] = f"restart-b-{name}"
    _configure_forwarding(
        daemon_a, listen=a_listen, receipt=receipt, pid_path=sidecar_pid_path,
    )
    _configure_forwarding(
        daemon_b, listen=b_listen, receipt=root / "forwarding-receipt-b.jsonl", pid_path=sidecar_pid_b,
    )
    daemon_a.env["HYPRIAL_ZENOH_SLOT_POOL"] = "1"
    forwarder = RespliceForwarder(
        a_listen, directory=root, listener_directory=root / "b",
        mode=mode, resplice_delay_seconds=resplice_delay_seconds,
    )
    cleanup_errors: list[str] = []
    try:
        forwarder.start()
        status_a = _start_forwarding_loopback(daemon_a)
        forwarding = status_a.get("forwarding")
        initial_endpoints = (
            forwarding.get("endpoints") if isinstance(forwarding, dict) else None
        )
        if not isinstance(initial_endpoints, list) or len(initial_endpoints) != 1:
            raise AssertionError(
                f"forwarding did not start with one fixed slot: {forwarding}"
            )
        _start_forwarding_loopback(daemon_b)
        daemon_a.command("agent", "create", "--name", DRIVER, "--json")
        daemon_b.command("agent", "create", "--name", DRIVER, "--json")
        directory = daemon_a.command(
            "org", "create", "group-restart", "--json", exits=(0, 1)
        )
        directory_id = directory.get("space_id", directory.get("spaceId"))
        if not isinstance(directory_id, str) or not directory_id:
            raise AssertionError(f"org create returned no space id: {directory}")
        daemon_a.command(
            "org", "fs", "member", "invite", directory_id, f"user:{_owner(daemon_b)}",
            "--no-notify", "--json",
        )
        roster_source = root / "roster-fixture.json"
        owner_a = _owner(daemon_a)
        members = [
            _LOCAL_MEMBER(owner_a, daemon_a.node_id,
                          "tc-e2e-unix:" + forwarder.endpoint.removeprefix("unixsock-stream/"), local=True),
            _LOCAL_MEMBER(_owner(daemon_b), daemon_b.node_id, f"tc-e2e-unix:{root}/b/zenoh.sock"),
        ]
        plant_device(daemon_a, directory_id, members[0])
        _FORWARDING["_publish_roster"](roster_source, directory_id, members, 3)
        _wait(
            f"{name} initial B host view",
            lambda: _host_online(daemon_b, daemon_a.node_id),
            bool,
            timeout=15,
        )
        _wait(
            f"{name} initial A host view",
            lambda: _host_online(daemon_a, daemon_b.node_id),
            bool,
            timeout=15,
        )
        _wait(
            f"{name} org directory join",
            lambda: daemon_b.command("org", "fs", "space", "join", directory_id, "--json", timeout=5, exits=(0, 1)),
            lambda value: isinstance(value, dict) and value.get("ok") is True,
            timeout=20,
        )
        redials_before_fill = len(_events(daemon_a, "zenoh.forwarding.redialed"))
        first_peer = "000-pool-one"
        _FORWARDING["_plant_directory_device"](
            daemon_a,
            directory_id,
            device_id="000-pool-one",
            owner=owner_a,
            address="tc-e2e-unix:" + forwarder.reverse_endpoint.removeprefix("unixsock-stream/"),
        )
        members.append(_FORWARDING["_fixture_member"](owner_a, first_peer,
                       "tc-e2e-unix:" + forwarder.reverse_endpoint.removeprefix("unixsock-stream/")))
        _FORWARDING["_publish_roster"](roster_source, directory_id, members, 4)
        _wait(
            f"{name} first peer to bind the pre-opened slot",
            lambda: daemon_a.ps(transient_timeout=4).get("forwarding", {}),
            lambda value: any(
                isinstance(item, dict) and item.get("peer") == first_peer
                for item in value.get("peers", [])
            ),
            timeout=30,
        )
        if len(_events(daemon_a, "zenoh.forwarding.redialed")) != redials_before_fill:
            raise AssertionError(f"{name} binding inside the fixed pool redialed")

        old_zids = _wait(
            f"{name} initial session zid",
            lambda: _zids(daemon_a),
            bool,
            timeout=5,
        )
        old_zid = old_zids[-1]
        a_pid = _daemon_pid(daemon_a)
        if a_pid is None:
            raise AssertionError(f"{name} A has no daemon pid before redial")
        b_zid_before = _zids(daemon_b)[-1]

        daemon_b.command("org", "pin-authority", directory_id, "--device", daemon_a.node_id, "--yes", "--json")
        killed_pid = _kill(daemon_b)
        _stop_pid(_pid_from(sidecar_pid_b), label=f"{daemon_b.node_id} pre-restart sidecar")
        restarted = _start_forwarding_loopback(daemon_b)
        if restarted["forwarding"]["readiness"]["member"] is not False:
            raise AssertionError("restarted member restored a usable lease")
        _FORWARDING["_publish_roster"](roster_source, directory_id, members, 5)
        b_zid_after = _wait(
            f"{name} B restarted session zid",
            lambda: _zids(daemon_b),
            lambda zids: bool(zids) and zids[-1] != b_zid_before,
            timeout=5,
        )[-1]
        _wait(
            f"{name} restarted B host view",
            lambda: _host_online(daemon_b, daemon_a.node_id),
            bool,
            timeout=15,
        )
        _wait(
            f"{name} A view of restarted B",
            lambda: _host_online(daemon_a, daemon_b.node_id),
            bool,
            timeout=15,
        )

        target_a = _actor_uri(daemon_a)
        baseline_transport_count = len(_events(daemon_b, "zenoh.transport.event"))
        baseline_link_count = len(_events(daemon_b, "zenoh.link.event"))
        triggered_at = time.monotonic()
        _FORWARDING["_plant_directory_device"](
            daemon_a,
            directory_id,
            device_id="001-pool-two",
            owner=owner_a,
            address="tc-e2e-unix:" + forwarder.reverse_endpoint.removeprefix("unixsock-stream/"),
        )
        members.append(_FORWARDING["_fixture_member"](owner_a, "001-pool-two",
                       "tc-e2e-unix:" + forwarder.reverse_endpoint.removeprefix("unixsock-stream/")))
        _FORWARDING["_publish_roster"](roster_source, directory_id, members, 6)
        redial_events = _wait(
            f"{name} slot-pool-growth redial",
            lambda: _events(daemon_a, "zenoh.forwarding.redialed")[
                redials_before_fill:
            ],
            bool,
            timeout=30,
        )
        dialed = redial_events[-1].get("dialed", [])
        if not isinstance(dialed, list) or len(dialed) != 2:
            raise AssertionError(
                f"{name} pool growth did not dial exactly two slots: {redial_events[-1]}"
            )
        if initial_endpoints[0] not in dialed:
            raise AssertionError(
                f"{name} pool growth dropped the original slot: {redial_events[-1]}"
            )
        new_zid = _wait(
            f"{name} rebuilt session zid",
            lambda: [
                str(event["zid"])
                for event in _events(daemon_a, "zenoh.session.rebuilt")
                if isinstance(event.get("zid"), str)
            ],
            bool,
            timeout=5,
        )[-1]
        if new_zid == old_zid:
            raise AssertionError(
                f"{name} baseline unexpectedly kept zenoh zid {old_zid}; "
                "the RED control no longer discriminates the pin"
            )
        if _daemon_pid(daemon_a) != a_pid:
            raise AssertionError(f"{name} A restarted during the in-process redial")

        observations: list[dict[str, object]] = []
        relearn_seconds: float | None = None
        observation_deadline = triggered_at + OBSERVATION_TIMEOUT_SECONDS
        next_observation = triggered_at
        while time.monotonic() < observation_deadline:
            now = time.monotonic()
            new_transport_events = _events(daemon_b, "zenoh.transport.event")[
                baseline_transport_count:
            ]
            new_link_events = _events(daemon_b, "zenoh.link.event")[
                baseline_link_count:
            ]
            if any(
                event.get("kind") == "put" and event.get("zid") == new_zid
                for event in new_transport_events
            ) or any(
                event.get("kind") == "put" and event.get("zid") == new_zid
                for event in new_link_events
            ):
                relearn_seconds = now - triggered_at
            if now >= next_observation or relearn_seconds is not None:
                observations.append(
                    {
                        "atSeconds": round(now - triggered_at, 3),
                        "aZid": _current_zid(daemon_a),
                        "bZid": _current_zid(daemon_b),
                        "bHostA": _host_online(daemon_b, daemon_a.node_id),
                        "bTargetA": _target_online(daemon_b, target_a),
                        "streamStates": _stream_states(forwarder),
                    }
                )
                next_observation = now + 1
            if relearn_seconds is not None:
                break
            time.sleep(0.1)

        b_to_a = _try_send(daemon_b, daemon_a, f"{name}-b-to-a")
        a_to_b = _try_send(daemon_a, daemon_b, f"{name}-a-to-b")
        stale = (
            relearn_seconds is None
            or relearn_seconds > STALE_THRESHOLD_SECONDS
            or b_to_a.get("delivered") is not True
            or a_to_b.get("delivered") is not True
        )
        evidence = {
            "path": name,
            "mode": mode,
            "respliceDelaySeconds": resplice_delay_seconds,
            "aPid": a_pid,
            "killedPid": killed_pid,
            "bZidBefore": b_zid_before,
            "bZidAfter": b_zid_after,
            "oldZid": old_zid,
            "newZid": new_zid,
            "redialCount": len(redial_events),
            "slotCount": len(dialed),
            "relearnSeconds": (
                round(relearn_seconds, 3)
                if relearn_seconds is not None
                else "not re-learned"
            ),
            "staleBeyondLeaseAndBackoff": stale,
            "bToA": b_to_a,
            "aToB": a_to_b,
            "observations": observations,
            "bTransportEvents": _events(daemon_b, "zenoh.transport.event"),
            "bLinkEvents": _events(daemon_b, "zenoh.link.event"),
            "streamTimeline": forwarder.snapshot(),
            "transport": "private-unix",
            "aDaemonLog": str(daemon_a.root / "state/logs/daemon.jsonl"),
            "bDaemonLog": str(daemon_b.root / "state/logs/daemon.jsonl"),
        }
        (root / "forwarding-evidence.json").write_text(json.dumps(evidence, separators=(",", ":")))
        _emit(event="peer-restart-summary", **{key: evidence[key] for key in (
            "path", "mode", "respliceDelaySeconds", "redialCount", "slotCount",
            "oldZid", "newZid", "bZidBefore", "bZidAfter", "aPid", "killedPid",
            "relearnSeconds", "staleBeyondLeaseAndBackoff", "bToA", "aToB",
        )}, budgets={"submitSeconds": 4, "deliverySeconds": 3, "ackRpcSeconds": 4, "relearnSeconds": STALE_THRESHOLD_SECONDS,
                    "observationSeconds": OBSERVATION_TIMEOUT_SECONDS, "overallSeconds": OVERALL_TIMEOUT_SECONDS})
        _emit(event="peer-restart-path", **evidence)
        return evidence
    finally:
        original_error = sys.exc_info()[1]
        for daemon in (daemon_b, daemon_a):
            try:
                _FORWARDING["_cleanup"](daemon, sidecar_pid_path if daemon is daemon_a else sidecar_pid_b)
            except BaseException as error:  # noqa: BLE001 - attempt every owned PID
                cleanup_errors.append(
                    f"{daemon.node_id}: {type(error).__name__}: {error}"
                )
        try:
            forwarder.stop()
        except BaseException as error:  # noqa: BLE001 - preserve cleanup verdict
            cleanup_errors.append(f"forwarder: {type(error).__name__}: {error}")
        if cleanup_errors:
            if original_error is None:
                raise AssertionError(f"PID cleanup failed: {cleanup_errors}")
            original_error.add_note(f"PID cleanup failed: {cleanup_errors}")
            print(f"RESULT diagnostic {SCENARIO} cleanup failed: {cleanup_errors}")


def main() -> int:
    if len(sys.argv) == 5 and sys.argv[1] == "--case":
        case_root = Path(sys.argv[2])
        parent = case_root.parent.stat()
        if (not case_root.is_absolute() or not case_root.parent.name.startswith("hyprial-e2e-024-")
                or parent.st_uid != os.getuid() or parent.st_mode & 0o777 != 0o700):
            raise AssertionError("worker root is not a private E2E-024 scenario")
        _run_case(case_root, mode=sys.argv[3], resplice_delay_seconds=float(sys.argv[4]))
        return 0
    if len(sys.argv) != 1:
        raise AssertionError("unexpected E2E-024 worker arguments")
    cases = (
        ("swallow-close", 0.0),
        ("blackhole", 0.0),
        ("swallow-close", 2.0),
        ("swallow-close", 10.0),
    )
    with scenario_tempdir(SCENARIO.lower(), directory=short_temp_root()) as root:
        results = run_cases(root, cases, runner=Path(__file__).resolve(),
                            executable=hyprial_executable(), stop_pid=_stop_pid,
                            interrupted=lambda: _INTERRUPTED, evidence_root=artifacts_root() / root.name)
    stale = [
        str(result["path"])
        for result in results
        if result["staleBeyondLeaseAndBackoff"] is True
    ]
    if stale:
        raise AssertionError(f"RED stale modes: {', '.join(stale)}")
    print(
        "RESULT PASS E2E-024 forwarding-zid-relearn "
        + f"redialed={sum(int(result['redialCount']) for result in results)} "
        + " ".join(
            f"{result['path']}={result['relearnSeconds']}s" for result in results
        )
        + " messages=both-directions cleanup=clean"
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--sidecar":
        raise SystemExit(_FORWARDING["_sidecar"](Path(sys.argv[2]), Path(sys.argv[3]), member_factory=_LOCAL_MEMBER))

    def _expired(_signum: int, _frame: object) -> None:
        if _signum in {signal.SIGTERM, signal.SIGINT}:
            error = ScenarioTimeout(f"{SCENARIO} interrupted by {signal.Signals(_signum).name}")
        else:
            error = ScenarioTimeout(f"{SCENARIO} exceeded its {OVERALL_TIMEOUT_SECONDS}s overall deadline")
        if len(sys.argv) == 5 and sys.argv[1] == "--case":
            raise error
        global _INTERRUPTED
        _INTERRUPTED = error

    previous = signal.signal(signal.SIGALRM, _expired)
    previous_term = signal.signal(signal.SIGTERM, _expired)
    previous_int = signal.signal(signal.SIGINT, _expired)
    signal.alarm(OVERALL_TIMEOUT_SECONDS)
    try:
        raise SystemExit(main())
    except (
        ScenarioTimeout,
        AssertionError,
        OSError,
        KeyError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        print(f"RESULT FAIL E2E-024 forwarding-zid-relearn {error}")
        raise SystemExit(1) from error
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
