#!/usr/bin/env python3
# Decision notes (service grant exposing side, 2026-11-26):
# - expose now enforces admission the way the Go engine does, in two steps:
#   membership first (engine.admits: the peer key equals some active roster
#   row's clientPublicByServer entry for THIS sidecar's serverPublic), then
#   the port's gate (exposuregate.Admits: allow = any member, keys = a member
#   whose peer key is in the set, empty set = nobody).
# - The roster is test input, read once from FAKE_FORWARD_ROSTER (a JSON file:
#   {"tailnetId": str, "serverPublic": str, "members": [{"deviceId", "owner",
#   "masterPublic", "serverPublic", "state"?, "address"?}],
#   "withoutServerKey": [deviceId, ...]}). A row's clientPublicByServer entry
#   for THIS sidecar's serverPublic is derived with the same rule the
#   in-process membership fake uses (tests/support/tailnet_membership.py):
#   nodekey:sha256(<member masterPublic><target serverPublic>). It stands in
#   for the real HKDF(master private, server public); consumers pin the rule,
#   never a second copy of the algorithm. It is stored on the row and read back
#   from there, both by admit time and by membership-status, as in Go.
# - Without FAKE_FORWARD_ROSTER nothing changes: no member is known,
#   membership-status answers {"tailnets": []} as before, so every existing
#   consumer sees the same replies as before.
# - "fixture-connect" {port, device} is a test-only probe meaning "roster
#   member <device> opens a connection to exposed <port>". It answers
#   {admitted, reason}; an established connection stays recorded on the port
#   until the gate no longer admits it (re-expose with a smaller set, or
#   unexpose), which is what the Go engine does to pairs it stops admitting.
# - "fixture-reload-roster" {} re-reads FAKE_FORWARD_ROSTER, the stand-in for
#   the sidecar's own roster refresh, and closes connections the new roster no
#   longer admits (a member that became a tombstone).
# - FAKE_FORWARD_FAULT (exposing side fix, 2026-11-27) names a file a test writes while
#   the sidecar runs; the sidecar's environment cannot change under it. Its
#   JSON is {"op", "mode", "port"?, "once"?}. While it exists, the first
#   command of that op, on that port when one is given, ends as "mode" says.
#   For expose: "reject" answers the error the Go engine answers when it is
#   not up (INTERNAL) and leaves the port's set alone; "drop" applies nothing
#   and answers nothing, a command the sidecar never executed; "silent"
#   applies the set and answers nothing, an executed command whose reply was
#   lost. For membership-status only "drop", a request that is never answered.
#   The unanswered ones end for the daemon in its own response timeout. With
#   "once" the file is removed by the first command that acted on it. Without
#   the variable nothing changes.
"""Protocol-v7 fixture pinned to sidecar/hyprial-tailcat's stdio JSON lines."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
from pathlib import Path

_CANONICAL_NODEKEY_RE = re.compile(r"^nodekey:[0-9a-f]{64}$")
_ZERO_NODEKEY = "nodekey:" + ("0" * 64)
_MAX_EXPOSE_KEYS = 4096


def _validate_expose_keys(raw_keys: object) -> tuple[bool, int]:
    if not isinstance(raw_keys, list) or len(raw_keys) > _MAX_EXPOSE_KEYS:
        return False, 0
    seen: set[str] = set()
    for entry in raw_keys:
        if (
            not isinstance(entry, str)
            or not _CANONICAL_NODEKEY_RE.match(entry)
            or entry == _ZERO_NODEKEY
            or entry in seen
        ):
            return False, 0
        seen.add(entry)
    return True, len(seen)


def _derived_client_key(master_public: str, server_public: str) -> str:
    return "nodekey:" + hashlib.sha256(
        (master_public + server_public).encode()
    ).hexdigest()


def _member_row(item: dict[str, object], server_public: str, devices: list[str]) -> dict:
    """One roster row in the Go ``Member`` wire shape.

    ``clientPublicByServer`` is stored on the row, not recomputed at admit
    time: an entry exists for each active peer the member has announced for.
    A test can name the member in ``withoutServerKey`` to model a row that
    has not been completed yet, or set ``state`` to ``tombstone``.
    """

    master = str(item["masterPublic"])
    member = {
        "owner": str(item.get("owner", "")),
        "deviceId": str(item["deviceId"]),
        "masterPublic": master,
        "serverPublic": str(item["serverPublic"]),
        "address": str(item.get("address", f"{master}@127.0.0.1:7447")),
        "state": str(item.get("state", "active")),
        "revision": 1,
    }
    if member["state"] == "active" and member["deviceId"] not in devices:
        member["clientPublicByServer"] = {server_public: _derived_client_key(master, server_public)}
    else:
        member["clientPublicByServer"] = {}
    return member


def _load_roster() -> dict[str, object]:
    path = os.environ.get("FAKE_FORWARD_ROSTER")
    if not path:
        return {"serverPublic": "", "tailnetId": "", "members": []}
    with open(path, encoding="utf-8") as stream:
        raw = json.load(stream)
    missing = [str(name) for name in raw.get("withoutServerKey", [])]
    members = [_member_row(item, str(raw["serverPublic"]), missing) for item in raw["members"]]
    return {
        "serverPublic": str(raw["serverPublic"]),
        "tailnetId": str(raw.get("tailnetId", "")),
        "members": members,
    }


_roster: list[dict[str, object]] = [_load_roster()]
_established: dict[int, set[str]] = {}


def _member(device_id: str) -> dict | None:
    return next((item for item in _roster[0]["members"] if item["deviceId"] == device_id), None)


def _peer_key(device_id: str) -> str | None:
    """The key a roster member presents when it dials THIS sidecar's server."""

    member = _member(device_id)
    if member is None or member["state"] != "active":
        return None
    return member["clientPublicByServer"].get(_roster[0]["serverPublic"])


def _is_member(peer: str | None) -> bool:
    return peer is not None and any(
        item["state"] == "active"
        and item["clientPublicByServer"].get(_roster[0]["serverPublic"]) == peer
        for item in _roster[0]["members"]
    )


def _gate_admits(exposure: dict[str, object], peer: str) -> bool:
    if not _is_member(peer):
        return False
    if exposure["admission"] == "allow":
        return True
    return peer in _keys.get(int(exposure["port"]), [])


def _close_no_longer_admitted() -> None:
    for port, exposure in exposures.items():
        _established[port] = {
            device for device in _established.get(port, set())
            if _gate_admits(exposure, _peer_key(device))
        }


def _fault(op: str, port: int | None = None) -> dict[str, object] | None:
    fault_path = os.environ.get("FAKE_FORWARD_FAULT")
    if not fault_path:
        return None
    try:
        fault = json.loads(Path(fault_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    modes = {"reject", "drop", "silent"} if op == "expose" else {"drop"}
    if not isinstance(fault, dict) or fault.get("mode") not in modes or fault.get("op") != op:
        return None
    wanted = fault.get("port")
    if isinstance(wanted, int) and wanted != port:
        return None
    if fault.get("once"):
        try:
            Path(fault_path).unlink()
        except OSError:
            pass
    return fault


def emit(command: dict[str, object] | None = None, **event: object) -> None:
    request_id = command.get("requestId") if command is not None else None
    if isinstance(request_id, str) and request_id:
        event["requestId"] = request_id
    print(json.dumps({"v": 7, **event}, separators=(",", ":")), flush=True)


receipt = Path(os.environ["FAKE_FORWARD_RECEIPT"])
receipt.parent.mkdir(parents=True, exist_ok=True)
mappings: dict[str, str] = {}
addresses: dict[str, str] = {}
exposures: dict[int, dict[str, object]] = {}
_keys: dict[int, list[str]] = {}
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
        "serverPublic": _roster[0]["serverPublic"] or "nodekey:fixture-server",
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
        if _fault(op) is not None:
            continue
        tailnets = []
        if _roster[0]["members"] and _roster[0]["tailnetId"]:
            tailnets = [{
                "tailnetId": _roster[0]["tailnetId"],
                "role": "authority",
                "revision": 1,
                "authorityDeviceId": "",
                "memberReady": True,
                "leaseRemainingSeconds": None,
                "activeCount": sum(m["state"] == "active" for m in _roster[0]["members"]),
                "tombstoneCount": sum(m["state"] == "tombstone" for m in _roster[0]["members"]),
                "code": None,
                "members": _roster[0]["members"],
            }]
        emit(command, event='membership-result', op=op, data={'tailnets': tailnets})
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
        admission = command.get("admission")
        if admission not in {"allow", "keys"}:
            emit(
                command,
                event="error",
                code="BAD_REQUEST",
                message="Invalid protocol-v7 request.",
            )
            continue
        has_keys = "keys" in command
        exposure = {
            "port": command["port"],
            "target": command["target"],
            "proxyProtocol": command["proxyProtocol"],
            "admission": admission,
        }
        if admission == "keys":
            valid_keys, key_count = _validate_expose_keys(command.get("keys"))
            if not has_keys or not valid_keys:
                emit(
                    command,
                    event="error",
                    code="BAD_REQUEST",
                    message="Invalid protocol-v7 request.",
                )
                continue
            exposure["keyCount"] = key_count
        elif has_keys:
            emit(
                command,
                event="error",
                code="BAD_REQUEST",
                message="Invalid protocol-v7 request.",
            )
            continue
        port = int(command["port"])
        fault = _fault(op, port)
        if fault is not None and fault["mode"] == "drop":
            continue
        if fault is not None and fault["mode"] == "reject":
            emit(
                command,
                event="error",
                code="INTERNAL",
                message="expose refused by the fixture fault file",
            )
            continue
        previous = exposures.get(port)
        if previous is not None and previous["admission"] == "allow" and admission == "allow":
            emit(command, event="exposed", **previous)
            continue
        if (
            previous is not None
            and previous["admission"] == "keys"
            and admission == "keys"
            and sorted(_keys.get(port, [])) == sorted(command["keys"])
        ):
            emit(command, event="exposed", **previous)
            continue
        exposures[port] = exposure
        _keys[port] = list(command["keys"]) if admission == "keys" else []
        _close_no_longer_admitted()
        if fault is not None:
            continue
        emit(command, event="exposed", **exposure)
    elif op == "unexpose":
        port = int(command["port"])
        exposures.pop(port, None)
        _keys.pop(port, None)
        _established.pop(port, None)
        emit(command, event="unexposed", port=port)
    elif op == "fixture-reload-roster":
        _roster[0] = _load_roster()
        for port, exposure in exposures.items():
            _established[port] = {
                device for device in _established.get(port, set())
                if _gate_admits(exposure, _peer_key(device))
            }
        emit(
            command,
            event="fixture-roster-reloaded",
            members=[item["deviceId"] for item in _roster[0]["members"]],
        )
    elif op == "fixture-connect":
        port = int(command["port"])
        exposure = exposures.get(port)
        peer = _peer_key(str(command["device"]))
        if exposure is None:
            reason = "not-exposed"
        elif peer is None:
            reason = "not-a-member"
        elif not _gate_admits(exposure, peer):
            reason = "denied-by-gate"
        else:
            reason = ""
        admitted = reason == ""
        if admitted:
            _established.setdefault(port, set()).add(str(command["device"]))
        emit(
            command,
            event="fixture-connected",
            port=port,
            device=str(command["device"]),
            admitted=admitted,
            reason=reason,
            established=sorted(_established.get(port, set())),
        )
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
