#!/usr/bin/env python3
"""E2E-021: OrgFS stays live across a v6 leased-roster Unix-slot redial."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _daemon_runner import (  # noqa: E402
    IsolatedDaemon,
    artifacts_root,
    hyprial_executable,
    launch_result_is_ready,
    scenario_tempdir,
)
from _tailcat_isolation import local_topology_violations  # noqa: E402
from _presence_trace import PresenceTrace
from _orgfs_redial_fixture import LeasedUnixFixture, membership_params, scenario_root

SCENARIO = "E2E-021"
OVERALL_TIMEOUT_SECONDS = 240
REDIAL_TIMEOUT_SECONDS = 30
DRIVER = "orgfs-redial-driver"
LIVELINESS_ACTOR = "orgfs-redial-liveness"
REPLAY_LIVELINESS_TOKENS = 64
PRESENCE_TRACE = PresenceTrace(capacity=512)
_TRACE_CAPTURE_FAILURES = 0


def _ci_capture_enabled() -> bool:
    return bool(os.environ.get("HYPRIAL_E2E_ARTIFACTS") or os.environ.get("HYPRIAL_E2E_ROOT"))


def _observed(daemon: IsolatedDaemon, command: str, *args: str, **kwargs: Any) -> Any:
    if command == "ps":
        value = daemon.ps(**kwargs)
    elif command == "hosts":
        value = daemon.command("host", "list", *args, **kwargs)
    else:
        value = daemon.command(command, *args, **kwargs)
    if isinstance(value, dict):
        try:
            PRESENCE_TRACE.record_cli(daemon.node_id, command, value)
        except Exception as error:  # noqa: BLE001 - observation never changes scenario result
            global _TRACE_CAPTURE_FAILURES
            _TRACE_CAPTURE_FAILURES += 1
            if _TRACE_CAPTURE_FAILURES == 1:
                print(f"RESULT info E2E-021 presence observation lost ({type(error).__name__})")
    return value


def _flush_presence(root: Path) -> None:
    """Retain the sanitized ring; existing failure preservation keeps daemon logs."""

    try:
        PRESENCE_TRACE.write(root / "presence-trace.json")
        if _ci_capture_enabled():
            target = artifacts_root()
            target.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            PRESENCE_TRACE.write(target / f"e2e021-presence-{stamp}.json")
    except Exception as error:  # noqa: BLE001 - diagnostics never replace the scene verdict
        print(f"RESULT info E2E-021 presence trace lost ({type(error).__name__})")


class ScenarioTimeout(Exception):
    """The runner's outer deadline fired."""


def _emit(command: dict[str, object] | None = None, **event: object) -> None:
    request_id = command.get("requestId") if command is not None else None
    if isinstance(request_id, str) and request_id:
        event["requestId"] = request_id
    print(json.dumps({"v": 6, **event}, separators=(",", ":")), flush=True)


_LOOPBACK_SCHEME = "tc-e2e-unix"
_DUMMY_SERVER_PUBLIC = "nodekey:" + "01" * 32
_DUMMY_CLIENT_PUBLIC = "nodekey:" + "02" * 32


def _sidecar(receipt: Path, pid_path: Path, *, member_factory: Callable[..., dict] | None = None) -> int:
    """Protocol-v6 leased-roster fixture; Unix proxies, no Tailcat/DERP.

    The explicit source models authority exchanges independently of directory
    publications. Business mappings require an active row and a fresh lease.
    """

    receipt.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    mappings: dict[str, str] = {}
    slots: list[str] = []
    from tests.support.tailnet_membership import FakeMembershipSidecar
    from hyprial.daemon.impl.forwarding.policy import ForwardingSidecarError

    fixture = LeasedUnixFixture(receipt.parent / "roster-fixture.json")
    public = {"serverPublic": _DUMMY_SERVER_PUBLIC, "clientPublic": _DUMMY_CLIENT_PUBLIC}
    _emit(
        event="hello",
        sidecar="e2e-loopback",
        tailcat="b4dc28e8aa8936f0a90a41ad8293a64e3d6b645f",
        capabilities=[
            "map-peer",
            "unmap-peer",
            "tailnet-membership-v1",
            "tailnet-control-v1",
            "unix-inbound-v1",
            "forwarding-request-id-v1",
            "peer-slots-v1",
            "client-key-derivation-v1",
        ],
    )
    for raw in sys.stdin:
        command = json.loads(raw)
        with receipt.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(command, separators=(",", ":")) + "\n")
        operation = command.get("op")
        if command.get("v") != 6:
            _emit(command, event="error", code="SIDECAR_PROTOCOL_MISMATCH", message="Invalid protocol-v6 request.")
            continue
        if operation == "up":
            address_file = command.get("addressFile")
            target = str(command.get("inboundTarget") or "")
            requested_slots = command.get("slots")
            try:
                if not isinstance(address_file, str) or not isinstance(requested_slots, list):
                    raise ValueError("up fields")
                fixture.start(command)
            except (ValueError, OSError):
                _emit(command, event="error", code="BAD_REQUEST", message="up fields")
                continue
            slots[:] = requested_slots
            row = (member_factory or _fixture_member)(command["owner"], command["deviceId"],
                                  f"{_LOOPBACK_SCHEME}:{target.removeprefix('unix:')}",
                                  local=command["deviceId"] == "orgfs-redial-a")
            fixture.management = FakeMembershipSidecar(local=row)
            public = {"serverPublic": row["serverPublic"], "clientPublic": row["masterPublic"]}
            path = Path(address_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"{_LOOPBACK_SCHEME}:{target.removeprefix('unix:')}\n", encoding="utf-8"
            )
            path.chmod(0o600)
            _emit(event="state", state="Starting", peers=[])
            _emit(
                command,
                event="state",
                state="Running",
                readiness={"sidecar": True, "control": True, "member": fixture.ready()},
                **public,
                peers=[],
                slots=slots,
            )
        elif operation in {"membership-status", "roster-fetch"}:
            try:
                params = membership_params(command)
                data = fixture.status(params)
                if operation == "roster-fetch":
                    if set(params) != {"tailnetId"} or not data["tailnets"]:
                        raise ValueError("unknown tailnet")
                    if not fixture.ready():
                        _emit(command, event="error", op=operation, code="AUTHORITY_UNREACHABLE",
                              message="Authority is unreachable.")
                        continue
                    data = fixture.roster
                _emit(command, event="membership-result", op=operation, data=data)
            except (ValueError, TypeError):
                _emit(command, event="error", op=operation, code="BAD_REQUEST", message="membership fields")
        elif operation in FakeMembershipSidecar.fields:
            try:
                data = fixture.management.membership_request(operation, membership_params(command))
                _emit(command, event="membership-result", op=operation, data=data)
            except (ForwardingSidecarError, ValueError) as error:
                _emit(command, event="error", op=operation, code=getattr(error, "code", "BAD_REQUEST"), message="Fixture operation refused.")
        elif operation == "status":
            _emit(
                command,
                event="state",
                state="Running",
                **public,
                peers=[
                    {"peer": peer, "slot": slot}
                    for peer, slot in sorted(mappings.items())
                ],
                slots=slots,
            )
        elif operation == "map-peer":
            peer = str(command.get("peer") or "")
            slot = command.get("slot")
            try:
                fixture.map_peer(peer, command.get("address", ""), slot)
            except (ValueError, TypeError, OSError):
                _emit(command, event="error", code="PEER_UNKNOWN", message="Peer unavailable.")
                continue
            mappings[peer] = slot
            if slot not in slots:
                slots.append(slot)
            _emit(command, event="peer-mapped", peer=peer, slot=slot)
        elif operation == "unmap-peer":
            peer = str(command.get("peer") or "")
            mappings.pop(peer, None)
            fixture.mappings.pop(peer, None)
            _emit(command, event="peer-unmapped", peer=peer)
        elif operation == "derive-client-key":
            server_public = command.get("serverPublic")
            if not isinstance(server_public, str) or not server_public.startswith(
                "nodekey:"
            ):
                _emit(command, event="error", code="BAD_REQUEST", message="serverPublic")
                continue
            _emit(
                command,
                event="client-key-derived",
                serverPublic=server_public,
                clientPublic=_derived_client_public(server_public),
            )
        elif operation == "down":
            fixture.close()
            _emit(event="exited")
            return 0
        else:
            _emit(
                command,
                event="error",
                code="BAD_REQUEST",
                message=f"unknown op {operation!r}",
            )
    fixture.close()
    return 0


def _derived_client_public(server_public: str) -> str:
    """Return the fixture's deterministic public-only derivation result."""

    return "nodekey:" + hashlib.sha256(server_public.encode()).hexdigest()


def _plant_directory_device(
    daemon: IsolatedDaemon,
    space_id: str,
    *,
    device_id: str,
    owner: str,
    address: str,
) -> None:
    """Write one ``DirectoryDevice`` row through the public fs CLI.

    This owner-scoped document is now a negative control: writing it must NOT
    create a forwarding candidate. Only a leased sidecar roster can do that.
    """

    from hyprial.identity.impl.org_directory.models import directory_device_path

    path = directory_device_path(owner, device_id)
    try:
        daemon.command("org", "fs", "mkdir", space_id, path.rsplit("/", 1)[0], "--json")
    except AssertionError as error:
        if "node already exists" not in str(error):
            raise
    member = _fixture_member(owner, device_id, address, local=device_id == "orgfs-redial-a")
    record = {
        "deviceId": device_id,
        "owner": owner,
        "serverPublic": member["serverPublic"],
        "clientPublic": member["masterPublic"],
        "clientPublicByServer": {},
        "address": address,
        "keyGeneration": 1,
        "updatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    daemon.command(
        "org", "fs",
        "write",
        space_id,
        path,
        json.dumps(record, sort_keys=True, separators=(",", ":")),
        "--json",
    )


def _fixture_member(owner: str, device_id: str, address: str, *, local=False) -> dict:
    return {
        "owner": owner, "deviceId": device_id,
        "masterPublic": _DUMMY_CLIENT_PUBLIC if local else "nodekey:" + (
            "01" if device_id == "001-pool-two" else "00"
        ) + hashlib.sha256(device_id.encode()).hexdigest()[:62],
        "serverPublic": _DUMMY_SERVER_PUBLIC if local else _derived_client_public(device_id),
        "address": address, "clientPublicByServer": {}, "state": "active", "revision": 1,
    }


def _publish_roster(source: Path, space_id: str, members: list[dict], revision: int) -> None:
    authority = {key: members[0][key] for key in (
        "owner", "deviceId", "masterPublic", "serverPublic", "address"
    )} | {"controlPort": 7448}
    for row in members:
        row["clientPublicByServer"] = {
            peer["serverPublic"]: _derived_client_public(row["masterPublic"] + peer["serverPublic"])
            for peer in members
        }
        row["revision"] = revision
    roster = {"tailnetId": space_id, "authority": authority, "revision": revision,
              "members": sorted(members, key=lambda row: row["deviceId"]), "leaseSeconds": 300}
    temporary = source.with_suffix(".tmp")
    temporary.write_text(json.dumps({"roster": roster, "startedMonotonic": time.monotonic()}))
    temporary.chmod(0o600)
    temporary.replace(source)


def _prepare_loopback(daemon: IsolatedDaemon) -> None:
    """Opt out of the isolation veto while preserving a closed loopback mesh."""

    for name in tuple(daemon.env):
        if name.startswith("HYPRIAL_FORWARDING_"):
            daemon.env.pop(name, None)
    daemon.env.update(
        {
            "HYPRIAL_NETWORK_ISOLATED": "0",
            "HYPRIAL_PEER_DISCOVERY": "0",
            "HYPRIAL_USAGE_DISABLE": "1",
            "HYPRIAL_ZENOH_GOSSIP": "0",
            "HYPRIAL_FORWARDING": "off",
            "NO_PROXY": "127.0.0.1,localhost,::1",
        }
    )
    daemon.env.pop("HYPRIAL_PEER_DISCOVERY_COMMAND", None)
    if _ci_capture_enabled():
        daemon.env["HYPRIAL_E2E_TRACE_PRESENCE"] = "1"


def _configure_forwarding(
    daemon: IsolatedDaemon,
    *,
    listen: str,
    receipt: Path,
    pid_path: Path,
    member_factory: Callable[..., dict] | None = None,
    sidecar_command: list[str] | None = None,
) -> None:
    sidecar_state = daemon.root / "sidecar-state"
    sidecar_state.mkdir(parents=True, exist_ok=True, mode=0o700)
    from hyprial.identity import DeviceRecord, address_path, device_key_path, write_device_record
    key_file = device_key_path(Path(daemon.env["HYPRIAL_HOME"]))
    key_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    row = (member_factory or _fixture_member)(daemon.env["HYPRIAL_OWNER"], daemon.node_id, "", local=daemon.node_id == "orgfs-redial-a")
    write_device_record(Path(daemon.env["HYPRIAL_HOME"]), DeviceRecord(
        device_id=daemon.node_id, owner=daemon.env["HYPRIAL_OWNER"],
        server_public=row["serverPublic"], client_public=row["masterPublic"],
    ))
    key_file.write_text(
        json.dumps(
            {
                "v": 5,
                "server": {"private": "privkey:fixture", "presharedKey": "fixture"},
                "client": {"private": "privkey:fixture"},
            }
        ),
        encoding="utf-8",
    )
    key_file.chmod(0o600)
    daemon.env.update(
        {
            "HYPRIAL_FORWARDING": "on",
            "HYPRIAL_FORWARDING_EXCLUSIVE": "1",
            "HYPRIAL_FORWARDING_COMMAND": json.dumps(
                sidecar_command or [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--sidecar",
                    str(receipt),
                    str(pid_path),
                ]
            ),
            "HYPRIAL_FORWARDING_UP": json.dumps(
                {
                    "v": 6,
                    "op": "up",
                    "keyFile": str(key_file),
                    "membershipStore": str(sidecar_state / "membership.json"),
                    "owner": daemon.env["HYPRIAL_OWNER"],
                    "deviceId": daemon.node_id,
                    "addressFile": str(address_path(Path(daemon.env["HYPRIAL_HOME"]))),
                    "inboundTarget": f"unix:{listen.removeprefix('unixsock-stream/')}",
                    "peerPort": 7447,
                    "slots": [
                        str(
                            Path(daemon.env["HYPRIAL_HOME"])
                            / "state/tailcat/slots/slot-00"
                        )
                    ],
                },
                separators=(",", ":"),
            ),
        }
    )


def _start_loopback(daemon: IsolatedDaemon) -> dict[str, Any]:
    daemon.require_no_external_triggers()
    result = daemon.command("init", "--json", timeout=60)
    if not launch_result_is_ready(result):
        raise AssertionError(f"daemon did not become ready: {result}")
    daemon.require_no_external_triggers()
    status = daemon.ps()
    zenoh = status.get("zenoh")
    if not isinstance(zenoh, dict):
        raise AssertionError(f"daemon omitted zenoh status: {status}")
    endpoints = [*zenoh.get("listen", []), *zenoh.get("connect", [])]
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


def _retry_join(daemon: IsolatedDaemon, space_id: str) -> dict[str, Any] | None:
    try:
        return daemon.command("org", "fs", "space", "join", space_id, "--json", timeout=5)
    except AssertionError:
        return None


def _pending_ids(value: dict[str, Any]) -> list[str]:
    pending = value.get("pendingMessages")
    if not isinstance(pending, list):
        return []
    return [
        str(item["messageId"])
        for item in pending
        if isinstance(item, dict) and isinstance(item.get("messageId"), str)
    ]


def _actor_uri(daemon: IsolatedDaemon, name: str) -> str:
    agents = daemon.command("agent", "list", "--json")["agents"]
    actor = next(row for row in agents if row.get("actor") == name)
    uri = actor.get("uri")
    if not isinstance(uri, str) or ":a." not in uri:
        raise AssertionError(f"agent has no official id address: {actor}")
    return uri


def _pid_from(path: Path) -> int | None:
    try:
        value = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return value if value > 1 else None


def _daemon_pid(daemon: IsolatedDaemon) -> int | None:
    path = daemon.root / "state" / "daemon.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    pid = value.get("pid") if isinstance(value, dict) else None
    return pid if isinstance(pid, int) and pid > 1 else None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                               capture_output=True, text=True, timeout=1)
    except (OSError, subprocess.SubprocessError):
        return True
    return not (state.returncode == 0 and state.stdout.strip().startswith("Z"))


def _stop_pid(pid: int | None, *, label: str) -> None:
    if pid is None:
        return
    try:
        if not _alive(pid):
            return
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and _alive(pid):
            time.sleep(0.05)
    except ProcessLookupError:
        pass
    finally:
        if _alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and _alive(pid):
                time.sleep(0.05)
    if _alive(pid):
        raise AssertionError(f"owned {label} pid {pid} survived SIGKILL")


def _cleanup(daemon: IsolatedDaemon, sidecar_pid_path: Path | None = None) -> None:
    daemon_pid = _daemon_pid(daemon)
    sidecar_pid = _pid_from(sidecar_pid_path) if sidecar_pid_path is not None else None
    try:
        if daemon_pid is not None and _alive(daemon_pid):
            try:
                daemon.command("daemon", "stop", "--json", timeout=3)
                daemon.wait_stopped()
            except (AssertionError, OSError, subprocess.TimeoutExpired):
                pass
    finally:
        try:
            _stop_pid(daemon_pid, label=f"{daemon.node_id} daemon")
        finally:
            _stop_pid(sidecar_pid, label=f"{daemon.node_id} sidecar")


def _assert_clean_logs(*daemons: IsolatedDaemon) -> None:
    for daemon in daemons:
        if _events(daemon, "daemon.maintenance.stalled"):
            raise AssertionError(f"{daemon.node_id} logged daemon.maintenance.stalled")
        launch = daemon.root / "state" / "daemon-launch.log"
        text = (
            launch.read_text(encoding="utf-8", errors="replace")
            if launch.is_file()
            else ""
        )
        if "zenoh.lock.wait_exceeded" in text:
            raise AssertionError(f"{daemon.node_id} logged zenoh.lock.wait_exceeded")


def _run() -> int:
    executable = hyprial_executable()
    with scenario_root() as temporary, scenario_tempdir(SCENARIO.lower(), directory=temporary) as root:
        for node in ("a", "b"):
            (root / node).mkdir(mode=0o700)
        a_listen = f"unixsock-stream/{root}/a/zenoh.sock"
        b_listen = f"unixsock-stream/{root}/b/zenoh.sock"
        source = root / "roster-fixture.json"
        receipt = root / "forwarding-receipt.jsonl"
        sidecar_pid = root / "forwarding-sidecar.pid"
        daemon_a = IsolatedDaemon(
            executable, root / "a", "orgfs-redial-a", listen=a_listen
        )
        daemon_b = IsolatedDaemon(
            executable,
            root / "b",
            "orgfs-redial-b",
            listen=b_listen,
        )
        _prepare_loopback(daemon_a)
        _prepare_loopback(daemon_b)
        daemon_a.env["HYPRIAL_OWNER"] = "redial-a"
        daemon_b.env["HYPRIAL_OWNER"] = "redial-b"
        daemon_a.env["HYPRIAL_ZENOH_SLOT_POOL"] = "1"
        _configure_forwarding(
            daemon_a,
            listen=a_listen,
            receipt=receipt,
            pid_path=sidecar_pid,
        )
        sidecar_pid_b = root / "forwarding-sidecar-b.pid"
        _configure_forwarding(daemon_b, listen=b_listen, receipt=root / "forwarding-receipt-b.jsonl", pid_path=sidecar_pid_b)
        cleanup_errors: list[str] = []
        try:
            status_a = _start_loopback(daemon_a)
            forwarding = status_a.get("forwarding")
            initial_endpoints = (
                forwarding.get("endpoints") if isinstance(forwarding, dict) else None
            )
            if not isinstance(initial_endpoints, list) or len(initial_endpoints) != 1:
                raise AssertionError(
                    f"forwarding did not start with one fixed slot: {forwarding}"
                )
            daemon_a.command("agent", "create", "--name", DRIVER, "--json")
            created = daemon_a.command("org", "fs", "space", "create", "redial-shared", "--json")
            space_id = created.get("space_id", created.get("spaceId"))
            if not isinstance(space_id, str) or not space_id:
                raise AssertionError(f"orgfs create returned no space id: {created}")
            org_created = daemon_a.command("org", "create", "group-redialnet", "--json", exits=(0, 1))
            org_space_id = org_created.get("space_id", org_created.get("spaceId"))
            if not isinstance(org_space_id, str) or not org_space_id:
                raise AssertionError(f"org create returned no space id: {org_created}")
            _plant_directory_device(daemon_a, org_space_id, device_id=daemon_a.node_id, owner="redial-a", address=f"{_LOOPBACK_SCHEME}:{root}/a/zenoh.sock")
            daemon_a.command("org", "fs", "member", "invite", org_space_id, "user:redial-b", "--no-notify", "--json")
            daemon_a.command(
                "org", "fs",
                "member",
                "invite",
                space_id,
                "user:redial-b",
                "--no-notify",
                "--json",
            )
            replay_actors: dict[str, str] = {}
            for index in range(REPLAY_LIVELINESS_TOKENS):
                name = f"orgfs-redial-local-{index:03d}"
                workspace = root / "workspaces" / name
                workspace.mkdir(parents=True, mode=0o700)
                registered = daemon_a.request("session.register", {
                    "actor": name, "sessionRef": f"e2e-021-local-liveliness-{index:03d}",
                    "cwd": str(workspace), "command": ["e2e-local-liveliness-fixture"],
                    "source": "e2e", "runtime": "e2e_interactive",
                })
                actor = registered.get("actor")
                if not isinstance(actor, str) or not actor.startswith(f"agent:redial-a:{daemon_a.node_id}:a."):
                    raise AssertionError(f"registration returned no official id: {registered}")
                replay_actors[name] = actor
            expected_replays = set(replay_actors.values())
            listed = {row["actor"]: row["uri"] for row in daemon_a.command("agent", "list", "--json")["agents"]
                      if row.get("actor") in replay_actors}
            if listed != replay_actors or len(expected_replays) != REPLAY_LIVELINESS_TOKENS:
                raise AssertionError(f"registered replay identities changed: {listed}")

            _start_loopback(daemon_b)
            redials_before_fill = len(_events(daemon_a, "zenoh.forwarding.redialed"))
            roster_members = [
                _fixture_member("redial-a", daemon_a.node_id, f"{_LOOPBACK_SCHEME}:{root}/a/zenoh.sock", local=True),
                _fixture_member("redial-b", daemon_b.node_id, f"{_LOOPBACK_SCHEME}:{root}/b/zenoh.sock"),
            ]
            _publish_roster(source, org_space_id, roster_members, 3)
            _wait(
                "first leased peer to bind the pre-opened slot",
                lambda: _observed(daemon_a, "ps", "--json").get("forwarding", {}),
                lambda value: isinstance(value, dict) and any(isinstance(item, dict) and item.get("peer") == daemon_b.node_id
                                  for item in value.get("peers", [])),
                timeout=REDIAL_TIMEOUT_SECONDS,
            )
            if len(_events(daemon_a, "zenoh.forwarding.redialed")) != redials_before_fill:
                raise AssertionError("binding inside the fixed pool unexpectedly redialed")
            daemon_b.command("agent", "create", "--name", DRIVER, "--json")
            workspace = root / "workspaces" / LIVELINESS_ACTOR
            workspace.mkdir(parents=True, mode=0o700)
            daemon_b.command("agent", "create", "--name", LIVELINESS_ACTOR, "--cwd", str(workspace), "--json")
            registered = daemon_b.request(
                "session.register",
                {
                    "actor": _actor_uri(daemon_b, LIVELINESS_ACTOR),
                    "sessionRef": "e2e-021-liveliness",
                    "cwd": str(workspace),
                    "command": ["e2e-liveliness-fixture"],
                    "source": "e2e",
                    "runtime": "e2e_interactive",
                },
            )
            live_actor = registered.get("actor")
            if not isinstance(live_actor, str):
                raise AssertionError(
                    f"liveliness registration has no actor: {registered}"
                )
            _wait(
                "mutual product-visible host discovery",
                lambda: (
                    _observed(daemon_a, "hosts", "--json"),
                    _observed(daemon_b, "hosts", "--json"),
                ),
                lambda pair: (
                    isinstance(pair, tuple)
                    and any(
                        item.get("nodeId") == daemon_b.node_id
                        for item in pair[0].get("hosts", [])
                        if isinstance(item, dict)
                    )
                    and any(
                        item.get("nodeId") == daemon_a.node_id
                        for item in pair[1].get("hosts", [])
                        if isinstance(item, dict)
                    )
                ),
                timeout=20,
            )
            _wait(
                "A to observe B's actor liveliness",
                lambda: daemon_a.command("message", "targets", "--json"),
                lambda value: any(
                    isinstance(item, dict)
                    and item.get("actor") == live_actor
                    and item.get("status") == "online"
                    for item in value.get("targets", [])
                ),
                timeout=20,
            )
            _wait(
                "orgfs member join",
                lambda: _retry_join(daemon_b, space_id),
                lambda value: isinstance(value, dict),
                timeout=20,
            )
            _wait(
                "org directory member join",
                lambda: _retry_join(daemon_b, org_space_id),
                lambda value: isinstance(value, dict), timeout=20,
            )
            members_b = daemon_b.command("org", "fs", "member", "list", space_id, "--json")
            member_rows = members_b.get("members", [])
            member_users = {
                str(item.get("user")) for item in member_rows if isinstance(item, dict)
            }
            if not {"user:redial-a", "user:redial-b"} <= member_users:
                raise AssertionError(
                    f"shared orgfs membership is incomplete: {members_b}"
                )
            members_a = daemon_a.command("org", "fs", "member", "list", space_id, "--json")
            member_rows_a = members_a.get("members", [])
            member_users_a = {
                str(item.get("user"))
                for item in member_rows_a
                if isinstance(item, dict)
            }
            if member_users_a != member_users:
                raise AssertionError(
                    f"orgfs membership differs across nodes: A={members_a} B={members_b}"
                )
            daemon_a.command(
                "org", "fs",
                "write",
                space_id,
                "before-redial.md",
                "orgfs-active-before-redial",
                "--json",
            )
            _wait(
                "A orgfs update to replicate to B",
                lambda: daemon_b.command(
                    "org", "fs", "read", space_id, "before-redial.md", "--json"
                ),
                lambda value: value.get("text") == "orgfs-active-before-redial",
                timeout=20,
            )

            yielded_before = len(_events(daemon_a, "inbox.retry_pump.yielded"))
            _plant_directory_device(
                daemon_a, org_space_id, device_id="001-pool-two", owner="redial-a",
                address=f"{_LOOPBACK_SCHEME}:{root}/b/zenoh.sock",
            )
            time.sleep(2)
            if len(_events(daemon_a, "zenoh.forwarding.redialed")) != redials_before_fill:
                raise AssertionError("directory-only publication unexpectedly redialed")
            if len(daemon_a.ps()["forwarding"]["peers"]) != 1:
                raise AssertionError("directory-only publication changed roster candidates")
            roster_members.append(_fixture_member(
                "redial-a", "001-pool-two", f"{_LOOPBACK_SCHEME}:{root}/b/zenoh.sock",
            ))
            _publish_roster(source, org_space_id, roster_members, 4)
            try:
                redial_events = _wait(
                    "slot-pool-growth redial while orgfs is active",
                    lambda: _events(daemon_a, "zenoh.forwarding.redialed")[
                        redials_before_fill:
                    ],
                    bool,
                    timeout=REDIAL_TIMEOUT_SECONDS,
                )
            except AssertionError as error:
                operations = (
                    [
                        json.loads(line).get("op")
                        for line in receipt.read_text(encoding="utf-8").splitlines()
                    ]
                    if receipt.is_file()
                    else []
                )
                try:
                    daemon_a.ps()
                    maintenance = "responsive"
                except BaseException as probe_error:  # noqa: BLE001 - RED evidence
                    maintenance = f"unreachable ({type(probe_error).__name__})"
                yielded_after = len(_events(daemon_a, "inbox.retry_pump.yielded"))
                raise AssertionError(
                    "pool-growth redial did not finish after sidecar discovery; "
                    f"map-peer={'map-peer' in operations} maintenance={maintenance} "
                    f"retryPumpDelta={yielded_after - yielded_before}: {error}"
                ) from error
            dialed = redial_events[-1].get("dialed", [])
            if not isinstance(dialed, list) or len(dialed) != 2:
                raise AssertionError(
                    "pool growth did not rebuild with exactly two fixed slots: "
                    f"{redial_events[-1]}"
                )
            if initial_endpoints[0] not in dialed:
                raise AssertionError(
                    f"pool growth dropped the original fixed slot: {redial_events[-1]}"
                )

            post_redial = _wait(
                "maintenance status to answer stalled=false after redial",
                lambda: daemon_a.ps(),
                lambda value: (
                    isinstance(value, dict)
                    and isinstance(value.get("maintenance"), dict)
                    and value["maintenance"].get("stalled") is False
                ),
                timeout=40,
            )
            maintenance = post_redial.get("maintenance")
            if (
                not isinstance(maintenance, dict)
                or maintenance.get("stalled") is not False
            ):
                raise AssertionError(
                    f"maintenance did not report stalled=false: {maintenance}"
                )

            _wait(
                "B to observe all 64 replayed A liveliness tokens",
                lambda: daemon_b.command("message", "targets", "--json"),
                lambda value: expected_replays <= {
                    item.get("actor") for item in value.get("targets", [])
                    if item.get("status") == "online"
                }, timeout=20,
            )
            daemon_a.command("org", "fs", "write", space_id, "after-redial.md", "orgfs-active-after-redial", "--json")
            _wait(
                "post-redial OrgFS update to replicate to B",
                lambda: daemon_b.command("org", "fs", "read", space_id, "after-redial.md", "--json"),
                lambda value: value.get("text") == "orgfs-active-after-redial", timeout=20,
            )
            target_b = _actor_uri(daemon_b, DRIVER)
            completed_before = len(_events(daemon_a, "inbox.retry_pump.completed"))
            failed_before = len(_events(daemon_a, "inbox.retry_pump.failed"))
            daemon_b.command("org", "pin-authority", org_space_id, "--device", daemon_a.node_id, "--yes", "--json")
            daemon_b.stop()
            daemon_b.wait_stopped()
            outbound = daemon_a.command(
                "message",
                "send",
                "--from",
                DRIVER,
                "--to",
                target_b,
                "--summary",
                "after-orgfs-redial",
                "after-orgfs-redial",
                "--json",
            )
            message_id = outbound.get("messageId")
            if not isinstance(message_id, str):
                raise AssertionError(
                    f"post-redial send returned no message id: {outbound}"
                )
            restarted = _start_loopback(daemon_b)
            if restarted["forwarding"]["readiness"]["member"] is not False:
                raise AssertionError("restarted member restored a usable lease")
            _publish_roster(source, org_space_id, roster_members, 4)
            _wait("B to obtain a fresh post-restart lease", lambda: daemon_b.ps(),
                  lambda value: value["forwarding"]["readiness"]["member"] is True, timeout=20)
            _wait(
                "A to B delivery after redial",
                lambda: daemon_b.ps(),
                lambda value: message_id in _pending_ids(value),
                timeout=20,
            )
            _wait(
                "retry pump completed queued work after redial",
                lambda: _events(daemon_a, "inbox.retry_pump.completed")[completed_before:],
                lambda rows: any(row.get("inboxResults", 0) >= 1 for row in rows),
                timeout=20,
            )
            if len(_events(daemon_a, "inbox.retry_pump.failed")) != failed_before:
                raise AssertionError("retry pump failed during the queued-delivery window")
            reply = daemon_b.request(
                "message.reply",
                {
                    "actor": DRIVER,
                    "messageId": message_id,
                    "summary": "reply-after-redial",
                    "message": "reply-after-redial",
                },
            )
            reply_id = reply.get("replyMessageId")
            if reply.get("replied") is not True or not isinstance(reply_id, str):
                raise AssertionError(f"B to A reply failed: {reply}")
            _wait(
                "B to A reply delivery after redial",
                lambda: daemon_a.ps(),
                lambda value: reply_id in _pending_ids(value),
                timeout=20,
            )
            acknowledged = daemon_a.command("message", "ack", "--from", DRIVER, reply_id, "--json")
            if acknowledged.get("acknowledged") is not True:
                raise AssertionError(f"reply acknowledgement failed: {acknowledged}")
            _assert_clean_logs(daemon_a, daemon_b)
            print(
                "RESULT evidence E2E-021 "
                f"space={space_id} redial=slot-pool-growth slots={len(dialed)} "
                "maintenance.stalled=false "
                f"replayTokens={REPLAY_LIVELINESS_TOKENS} request=delivered "
                "reply=delivered retry-pump=continued locks=clean"
            )
        finally:
            for daemon, pid_path in ((daemon_b, sidecar_pid_b), (daemon_a, sidecar_pid)):
                try:
                    _cleanup(daemon, pid_path)
                except BaseException as error:  # noqa: BLE001 - attempt every PID cleanup
                    cleanup_errors.append(
                        f"{daemon.node_id}: {type(error).__name__}: {error}"
                    )
            _flush_presence(root)
            if cleanup_errors and (original_error := sys.exc_info()[1]) is not None:
                original_error.add_note(f"PID cleanup failed: {cleanup_errors}")
                print(f"RESULT diagnostic {SCENARIO} cleanup failed: {cleanup_errors}")
        if cleanup_errors:
            raise AssertionError(f"PID cleanup failed: {cleanup_errors}")
    print(
        "RESULT PASS E2E-021 orgfs-forwarding-redial pass=6 fail=0 blocked=0 cleanup=clean"
    )
    return 0


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--sidecar":
        return _sidecar(Path(sys.argv[2]), Path(sys.argv[3]))

    def expired(_signum: int, _frame: object) -> None:
        if _signum in {signal.SIGTERM, signal.SIGINT}:
            raise ScenarioTimeout(f"{SCENARIO} interrupted by {signal.Signals(_signum).name}")
        raise ScenarioTimeout(
            f"{SCENARIO} exceeded its {OVERALL_TIMEOUT_SECONDS}s overall deadline"
        )

    previous = signal.signal(signal.SIGALRM, expired)
    previous_term = signal.signal(signal.SIGTERM, expired)
    previous_int = signal.signal(signal.SIGINT, expired)
    signal.alarm(OVERALL_TIMEOUT_SECONDS)
    try:
        return _run()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


if __name__ == "__main__":
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
        print(f"RESULT FAIL E2E-021 orgfs-forwarding-redial {error}")
        raise SystemExit(1) from error
