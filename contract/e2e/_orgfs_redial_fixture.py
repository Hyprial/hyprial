"""Test-only leased roster and Unix slot proxy for E2E-021.

The source file models a completed authority exchange, not OrgFS or the product
membership store. This is not a Tailcat/DERP/control-protocol implementation.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import selectors
import socket
import tempfile
import threading
import time


@contextmanager
def scenario_root():
    with tempfile.TemporaryDirectory(prefix="hr-", dir="/tmp") as temporary:
        yield Path(temporary)


def membership_params(command):
    request_id = command.get("requestId")
    if (set(command) != {"v", "op", "requestId", "params"}
            or not isinstance(request_id, str) or not 0 < len(request_id.encode()) <= 128
            or not isinstance(command["params"], dict)):
        raise ValueError("membership fields")
    return command["params"]


class LeasedUnixFixture:
    def __init__(self, source: Path):
        self.source = source
        self.roster = None
        self.deadline = 0.0
        self.started = time.monotonic()
        self.mappings = {}
        self.listeners = {}
        self.streams = set()
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.local = None
        self.management = None
        self.monitor = threading.Thread(target=self._monitor, daemon=True)
        self.monitor.start()

    def start(self, command):
        target = command.get("inboundTarget", "")
        if not target.startswith("unix:/") or {"allow", "allowAny"} & command.keys():
            raise ValueError("private Unix inbound required")
        parent = Path(target.removeprefix("unix:")).parent
        if parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o777 != 0o700:
            raise ValueError("private Unix parent required")
        for field in ("membershipStore", "owner", "deviceId", "addressFile", "keyFile"):
            if not isinstance(command.get(field), str) or not command[field]:
                raise ValueError("missing up field")
        self.local = command["deviceId"]
        # A member starts unleased; the first status read models roster refresh.
        self.roster = None
        self.deadline = 0.0
        self.refresh()
        if not self.authority_local():
            self.roster = None
            self.deadline = 0.0
        for slot in command["slots"]:
            self.bind(slot)

    def refresh(self):
        try:
            exchange = json.loads(self.source.read_text())
        except FileNotFoundError:
            return
        roster = exchange["roster"]
        if self.management is not None:
            initialized = self.management.tailnets.get(roster["tailnetId"])
            if initialized and roster["revision"] < initialized["revision"]:
                raise ValueError("roster older than initialized table")
        if self.roster is not None:
            if roster["revision"] < self.roster["revision"]:
                raise ValueError("roster rollback")
            if roster["revision"] == self.roster["revision"] and roster != self.roster:
                raise ValueError("roster changed at same revision")
        self.roster = roster
        # Status polling cannot renew a lease. Only a new simulated exchange can.
        self.deadline = (exchange["startedMonotonic"] + roster["leaseSeconds"]
                         if exchange["startedMonotonic"] >= self.started else 0.0)

    def authority_local(self):
        return bool(self.roster and self.roster["authority"]["deviceId"] == self.local)

    def ready(self):
        return bool(self.roster and (self.authority_local() or time.monotonic() < self.deadline) and any(
            row["deviceId"] == self.local and row["state"] == "active"
            for row in self.roster["members"]
        ))

    def status(self, params):
        if set(params) - {"tailnetId"}:
            raise ValueError("unknown status field")
        self.refresh()
        ready = self.ready()
        rows = []
        if self.roster and params.get("tailnetId", self.roster["tailnetId"]) == self.roster["tailnetId"]:
            roster = self.roster
            rows.append({
                "tailnetId": roster["tailnetId"], "role": "authority" if self.authority_local() else "member",
                "revision": roster["revision"],
                "authorityDeviceId": roster["authority"]["deviceId"],
                "memberReady": ready,
                "leaseRemainingSeconds": None if self.authority_local() else max(0, min(300, math.ceil(self.deadline - time.monotonic()))),
                "activeCount": sum(row["state"] == "active" for row in roster["members"]),
                "tombstoneCount": sum(row["state"] == "tombstone" for row in roster["members"]),
                "code": None if ready else "MEMBERSHIP_LEASE_EXPIRED",
                "members": roster["members"],
            })
        if self.management is not None:
            present = {row["tailnetId"] for row in rows}
            rows.extend(row for row in self.management.membership_request("membership-status", params)["tailnets"]
                        if row["tailnetId"] not in present)
        return {"readiness": {"sidecar": True, "control": True,
                              "member": any(row["memberReady"] for row in rows)}, "tailnets": rows}

    def map_peer(self, peer, address, slot):
        self.refresh()
        if not self.ready():
            raise ValueError("unleased peer")
        if not any(row["deviceId"] == peer and row["address"] == address
                   and row["state"] == "active" for row in self.roster["members"]):
            raise ValueError("unknown peer")
        if not address.startswith("tc-e2e-unix:/"):
            raise ValueError("fixture Unix address required")
        target = address.removeprefix("tc-e2e-unix:")
        # Never connect outside the disposable scenario root.
        if not Path(target).resolve().is_relative_to(self.source.parent.resolve()):
            raise ValueError("fixture target escaped scenario")
        self.bind(slot)
        bound = next(existing for existing in self.listeners if Path(existing).resolve() == Path(slot).resolve())
        self.mappings[peer] = (bound, target)

    def bind(self, slot):
        if any(Path(existing).resolve() == Path(slot).resolve() for existing in self.listeners):
            return
        path = Path(slot)
        if not path.is_absolute() or not path.resolve().is_relative_to(self.source.parent.resolve()):
            raise ValueError("fixture slot escaped scenario")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(slot)
        listener.listen()
        listener.settimeout(0.1)
        self.listeners[slot] = listener
        threading.Thread(target=self._accept, args=(slot, listener), daemon=True).start()

    def _accept(self, slot, listener):
        while not self.closed.is_set():
            try:
                stream, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._splice, args=(slot, stream), daemon=True).start()

    def _splice(self, slot, incoming):
        outgoing = None
        try:
            # Unmapped fixed slots stay in hold mode, like the real sidecar.
            while not self.closed.wait(0.02):
                target = next((target for bound, target in list(self.mappings.values())
                               if bound == slot), None)
                if target and self.ready():
                    break
            else:
                return
            outgoing = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            outgoing.settimeout(2)
            outgoing.connect(target)
            with self.lock:
                self.streams.update((incoming, outgoing))
            with selectors.DefaultSelector() as selector:
                selector.register(incoming, selectors.EVENT_READ, outgoing)
                selector.register(outgoing, selectors.EVENT_READ, incoming)
                while self.ready() and not self.closed.is_set():
                    for key, _ in selector.select(0.1):
                        data = key.fileobj.recv(65536)
                        if not data:
                            return
                        key.data.sendall(data)
        except OSError:
            pass
        finally:
            with self.lock:
                self.streams.discard(incoming)
                self.streams.discard(outgoing)
            incoming.close()
            if outgoing is not None:
                outgoing.close()

    def _monitor(self):
        while not self.closed.wait(0.05):
            if not self.ready():
                with self.lock:
                    for stream in self.streams:
                        try:
                            stream.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass

    def close(self):
        self.closed.set()
        for slot, listener in self.listeners.items():
            listener.close()
            Path(slot).unlink(missing_ok=True)
        with self.lock:
            for stream in self.streams:
                try:
                    stream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self.monitor.join(timeout=1)
