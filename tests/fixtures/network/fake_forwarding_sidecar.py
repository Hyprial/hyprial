#!/usr/bin/env python3
"""Protocol-v5 fixture pinned to sidecar/hyprial-tailcat's stdio JSON lines."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from pathlib import Path


def emit(command: dict[str, object] | None = None, **event: object) -> None:
    request_id = command.get("requestId") if command is not None else None
    if isinstance(request_id, str) and request_id:
        event["requestId"] = request_id
    print(json.dumps({"v": 6, **event}, separators=(",", ":")), flush=True)


receipt = Path(os.environ["FAKE_FORWARD_RECEIPT"])
receipt.parent.mkdir(parents=True, exist_ok=True)
mappings: dict[str, str] = {}
addresses: dict[str, str] = {}
exposures: dict[int, dict[str, object]] = {}
allow_keys: list[str] = []
allow_any = False
# The up request's slots, resolved like the real sidecar resolves them:
# unix paths stay themselves; a tcp:127.0.0.1:0 spec gets a fake
# kernel-assigned port.  Reported back in every state event.
slot_ids: list[str] = []
next_tcp_slot_port = 51000
_non_v5_status_emitted = False


def resolve_slot_spec(spec: str) -> str:
    global next_tcp_slot_port
    if spec == "tcp:127.0.0.1:0":
        next_tcp_slot_port += 1
        return f"tcp:127.0.0.1:{next_tcp_slot_port}"
    return spec

emit(
    event="hello",
    sidecar="dev",
    tailcat="b4dc28e8aa8936f0a90a41ad8293a64e3d6b645f",
    capabilities=[
        "map-peer",
        "unmap-peer",
        "tailnet-membership-v1",
        "tailnet-control-v1",
        "unix-inbound-v1",
        "expose",
        "unexpose",
        "peer-key",
        "forwarding-request-id-v1",
        "peer-slots-v1",
        "connection-telemetry-v1",
        "client-key-derivation-v1",
        "derive",
    ],
)

# The real sidecar writes its failure explanation to stderr. Modelling that is
# the point: the daemon used to read this stream and discard every chunk.
_stderr_text = os.environ.get("FAKE_FORWARD_STDERR")
if _stderr_text:
    print(_stderr_text, file=sys.stderr, flush=True)


def state_event(state: str) -> dict[str, object]:
    # Protocol v5 slots-only: every peers entry is {peer, slot}.  A
    # slotted up also reports the resolved slot ids, in request order,
    # growth appended.
    event: dict[str, object] = {
        "event": "state",
        "state": state,
        'readiness': {'sidecar': True, 'control': True, 'member': False},
        "serverPublic": "nodekey:fixture-server",
        "clientPublic": "nodekey:fixture-client",
        "peers": [
            {"peer": peer, "slot": bound}
            for peer, bound in sorted(mappings.items())
        ],
        "connectionTelemetry": {"ringCapacity": 16, "slots": []},
    }
    if slot_ids:
        event["slots"] = list(slot_ids)
    return event


for raw in sys.stdin:
    command = json.loads(raw)
    with receipt.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(command, separators=(",", ":")) + "\n")
    op = command["op"]
    if op == "up":
        # The manager.go first-start race, in its own wire shape: the first
        # ``up`` answers an error and dies; the next launch is ready.  One-shot
        # across launches via a marker file, because the relaunch inherits
        # the same environment.
        _error_once = os.environ.get("FAKE_FORWARD_UP_ERROR_ONCE")
        if _error_once:
            marker = Path(_error_once)
            if not marker.exists():
                marker.write_text("1", encoding="utf-8")
                emit(
                    event="error",
                    code="START_FAILED",
                    message="Running status is incomplete",
                )
                raise SystemExit(1)
        # The sidecar writes the machine's Tailcat address to addressFile
        # (0600); the fake writes a placeholder so consumers see a real file.
        address_file = command.get("addressFile")
        if isinstance(address_file, str) and address_file:
            path = Path(address_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("tailcat://fixture-address\n", encoding="utf-8")
            path.chmod(0o600)
        slot_ids = [
            resolve_slot_spec(str(spec)) for spec in command.get("slots") or []
        ]
        emit(**{"event": "state", "state": "Starting"})
        emit(command, **state_event("Running"))
        code = int(os.environ.get("FAKE_FORWARD_EXIT_AFTER_READY", "0"))
        if code:
            raise SystemExit(code)
    elif op == 'membership-status':
        emit(command, event='membership-result', op=op, data={'tailnets': []})
    elif op == "status":
        if os.environ.get("FAKE_FORWARD_STATUS_NON_V5_ONCE"):
            if not _non_v5_status_emitted:
                _non_v5_status_emitted = True
                print(
                    json.dumps({"v": 4, **state_event("Running")}, separators=(",", ":")),
                    flush=True,
                )
            continue
        if os.environ.get("FAKE_FORWARD_STATUS_BROKEN"):
            # The wedged child: alive, stdin loop keeps running, but the
            # status channel answers an error forever.
            emit(
                command,
                event="error",
                code="INTERNAL",
                message="cannot read node status",
            )
            continue
        status = state_event("Running")
        # Builds that do not report peers simply omit the key; the daemon
        # must read absence as "no mapped peer yet", not a violation.
        if os.environ.get("FAKE_FORWARD_STATUS_OMITS_PEERS"):
            status.pop("peers")
        elif os.environ.get("FAKE_FORWARD_STATUS_PEERS_BAD"):
            status["peers"] = [{"peer": "", "slot": 404}]
        emit(command, **status)
    elif op == "map-peer":
        peer = command["peer"]
        slot = command.get("slot")
        if not isinstance(slot, str) or not slot:
            # Slots-only v5: the transition shape is retired; the real
            # sidecar rejects a slot-free map-peer at parse time.
            emit(command, event="error", code="BAD_REQUEST", message="missing slot")
            continue
        # Pool growth: a tcp grow spec (or an unknown unix path) is
        # lazily bound and answered with its resolved id.
        resolved = resolve_slot_spec(slot)
        if resolved not in slot_ids:
            slot_ids.append(resolved)
        mappings[peer] = resolved
        addresses[peer] = command.get("address", "")
        emit(command, event="peer-mapped", peer=peer, slot=resolved)
    elif op == "unmap-peer":
        peer = command["peer"]
        mappings.pop(peer, None)
        addresses.pop(peer, None)
        emit(command, event="peer-unmapped", peer=peer)
    elif op == "allow":
        allow_keys = list(command.get("keys") or [])
        allow_any = bool(command.get("allowAny"))
        emit(command, event="allowed", count=len(allow_keys), allowAny=allow_any)
    elif op == "expose":
        exposure = {
            "port": command["port"],
            "target": command["target"],
            "proxyProtocol": command["proxyProtocol"],
        }
        exposures[int(command["port"])] = exposure
        emit(command, event="exposed", **exposure)
    elif op == "unexpose":
        port = int(command["port"])
        exposures.pop(port, None)
        emit(command, event="unexposed", port=port)
    elif op == "peer-key":
        emit(
            command,
            event="peer-key",
            addr=command["addr"],
            found=True,
            key="nodekey:fixture-peer",
        )
    elif op == "derive-client-key":
        server_public = str(command["serverPublic"])
        client_public = "nodekey:" + hashlib.sha256(server_public.encode()).hexdigest()
        reply_command = command
        request_id_mode = os.environ.get("FAKE_FORWARD_DERIVE_REQUEST_ID")
        if request_id_mode == "missing":
            reply_command = {}
        elif request_id_mode == "wrong":
            reply_command = {
                **command,
                "requestId": f"{command.get('requestId')}-wrong",
            }
        emit(
            reply_command,
            event="client-key-derived",
            serverPublic=server_public,
            clientPublic=client_public,
        )
    elif op == "derive":
        # The #1293 request-key derivation, in the fixture's deterministic
        # shape: the command carries Role('client'|'server') plus Peer and the
        # answer is {"event": "derived", "key": base64(32 bytes)}.  Same
        # (role, peer) always answers the same key, and no real X25519/HKDF
        # runs here -- consumers pin the field order and the reply shape,
        # never a second copy of the algorithm.
        role = str(command["role"])
        if role not in {"client", "server"}:
            emit(
                command,
                event="error",
                code="INVALID_ARGUMENT",
                message=f"unknown role {role!r}",
            )
            continue
        peer = str(command["peer"])
        key_bytes = hashlib.sha256(f"fixture-derive|{role}|{peer}".encode()).digest()
        emit(
            command,
            event="derived",
            key=base64.b64encode(key_bytes).decode("ascii"),
        )
    elif op == "down":
        emit(event="exited")
        break
