"""Real-zenoh minimal E2E for the sidecar's fixed slot pool (protocol v6, N1).

Two real `hyprial-tailcat forward` processes on a local lab relay. Node B's
zenoh session connects ONLY to B-sidecar's fixed unix-socket slots; node A's
zenoh listens behind A-sidecar's PeerPort. The sidecar holds unbound slot
connections unread (hold mode) and splices them on `map-peer {slot}`.

Readings (gates follow the N1 acceptance; research values in parentheses,
notes/research-zenoh-membership-2026-10-08/REPORT.md):
  1. idle dials on a never-bound slot  <= 0.2/s/slot   (0.18/s)
  2. hold-mode first-connect p95       <  1 s          (6-24 ms)
  3. unmap-peer closes the slot's link: data stops, the old peer's
     liveliness token gets a DELETE; a later map-peer resumes data.
  4. B's zenoh session is never rebuilt: zenoh.session.closed == 0.

Isolation: no daemon, no ~/.hyprial, loopback + local DERP relay only.
Run from the repository root:
    uv run python sidecar/e2e-zenoh-slots/run_slots_e2e.py [tcp]

With ``tcp`` the slot pool is loopback TCP with kernel-assigned ports --
up sends tcp:127.0.0.1:0 grow specs and Running reports resolved ids.
The inbound backend is always a private Unix socket; this managed-v6
scenario requires a Unix host. Same readings, same gates.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("HYPRIAL_PEER_DISCOVERY", "0")

from hyprial.daemon.impl.transport.zenoh.config import ZenohConfig  # noqa: E402
from hyprial.daemon.impl.transport.zenoh.transport import ZenohTransport  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SIDECAR_DIR = REPO / "sidecar/hyprial-tailcat"
IDLE_WINDOW_SECONDS = 15.0
TRIALS = 5
TRANSPORT = "tcp" if "tcp" in sys.argv[1:] else "unix"


def slot_locator(slot_id: str) -> str:
    return (
        f"tcp/{slot_id[4:]}" if slot_id.startswith("tcp:")
        else f"unixsock-stream/{slot_id}"
    )


def free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def build_binaries(tmp: Path) -> tuple[Path, Path]:
    sidecar = tmp / "hyprial-tailcat"
    labrelay = tmp / "hyprial-tailcat-labrelay"
    for out, pkg in ((sidecar, "./cmd/hyprial-tailcat"), (labrelay, "./cmd/hyprial-tailcat-labrelay")):
        subprocess.run(
            ["go", "build", "-o", str(out), pkg],
            cwd=SIDECAR_DIR, check=True,
        )
    return sidecar, labrelay


def genkey(binary: Path, out: Path) -> None:
    completed = subprocess.run(
        [str(binary), "genkey", "--out", str(out)],
        capture_output=True, check=False,
    )
    if completed.returncode not in (0, 3):
        raise RuntimeError(f"genkey failed: {completed.stderr.decode()!r}")


class Sidecar:
    """One `forward` process on the closed v6 JSON-lines wire."""

    def __init__(self, binary: Path, name: str, stderr_path: Path) -> None:
        self.name = name
        self._stderr = stderr_path.open("wb")
        self.proc = subprocess.Popen(
            [str(binary), "forward"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
        )
        self._events: queue.Queue[dict | None] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        hello = self.expect("hello")
        if "peer-slots-v1" not in hello.get("capabilities", []):
            raise RuntimeError(f"{name}: sidecar does not announce peer-slots-v1")

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                self._events.put(json.loads(line))
            except ValueError:
                self._events.put(None)
        self._events.put(None)

    def send(self, **command: object) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(
            (json.dumps({"v": 6, **command}, separators=(",", ":")) + "\n").encode()
        )
        self.proc.stdin.flush()

    def expect(self, event: str, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{self.name}: no {event!r} event in {timeout}s")
            got = self._events.get(timeout=remaining)
            if got is None:
                raise RuntimeError(f"{self.name}: sidecar stream ended")
            if got.get("event") == "error":
                raise RuntimeError(f"{self.name}: error event {got}")
            if got.get("event") == event:
                return got

    def membership(self, op: str, params: dict) -> dict:
        request_id = f"{self.name}-{time.monotonic_ns()}"
        self.send(op=op, requestId=request_id, params=params)
        result = self.expect("membership-result")
        if result.get("requestId") != request_id:
            raise RuntimeError("membership request correlation failed")
        return result["data"]

    def close(self) -> None:
        try:
            self.send(op="down")
            self.expect("exited", timeout=5.0)
        except Exception:
            pass
        self.proc.terminate()
        self.proc.wait(timeout=10)
        self._stderr.close()


class Tap:
    """Transparent counter between zenoh and one real slot socket.

    ``upstream`` is the slot's wire id: a unix socket path, or
    ``tcp:host:port``.  The tap listens on the same family; on tcp its
    kernel-assigned address is exposed as ``self.locator``.
    """

    def __init__(self, listen_path: Path, upstream: str) -> None:
        self.count = 0
        self._upstream = upstream
        if upstream.startswith("tcp:"):
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.bind(("127.0.0.1", 0))
            self.locator = "tcp/127.0.0.1:%d" % self._sock.getsockname()[1]
        else:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.bind(str(listen_path))
            self.locator = f"unixsock-stream/{listen_path}"
        self._sock.listen(16)
        threading.Thread(target=self._accept, daemon=True).start()

    def _connect_upstream(self) -> socket.socket:
        if self._upstream.startswith("tcp:"):
            host, _, port = self._upstream[4:].rpartition(":")
            return socket.create_connection((host, int(port)))
        up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        up.connect(self._upstream)
        return up

    @staticmethod
    def _pipe(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        for sock in (src, dst):
            try:
                sock.close()
            except OSError:
                pass

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.count += 1
            try:
                up = self._connect_upstream()
            except OSError:
                conn.close()
                continue
            threading.Thread(target=self._pipe, args=(conn, up), daemon=True).start()
            threading.Thread(target=self._pipe, args=(up, conn), daemon=True).start()


def wait_until(predicate, timeout: float, what: str) -> float:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return time.monotonic()
        time.sleep(0.005)
    raise TimeoutError(f"timed out waiting for {what}")


def warm_tailcat(sidecar: Sidecar, address: str) -> None:
    """Establish B->A's Tailcat transport once via a grown tcp slot.

    The pooled client is keyed by the peer address, so slot bindings to the
    same address reuse the warm transport (clients.go acquireClientLocked).
    """
    sidecar.send(op="map-peer", peer="warm", address=address, slot="tcp:127.0.0.1:0")
    port = int(sidecar.expect("peer-mapped")["slot"].rpartition(":")[2])
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        probe = socket.create_connection(("127.0.0.1", port), timeout=5)
        probe.settimeout(1.5)
        try:
            # An end-to-end splice stays open (the far side is a zenoh
            # listener waiting for InitSyn); a failed Tailcat dial closes
            # the local connection immediately.
            data = probe.recv(1)
            if data == b"":
                time.sleep(0.5)
                continue
            return
        except TimeoutError:
            return  # still open after 1.5s: the transport is up
        finally:
            probe.close()
    raise TimeoutError("tailcat transport to A never came up")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="hr-go-slots-", dir="/tmp"))
    results: dict[str, object] = {}
    failures: list[str] = []
    relay = zenoh_a = zenoh_b = side_a = side_b = None
    try:
        sidecar_bin, labrelay_bin = build_binaries(tmp)
        relay = subprocess.Popen(
            [str(labrelay_bin)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )
        assert relay.stdout is not None
        derp = json.loads(relay.stdout.readline())["derp"]
        genkey(sidecar_bin, tmp / "a.key")
        genkey(sidecar_bin, tmp / "b.key")

        # Start both control planes before either Zenoh backend exists.
        side_a = Sidecar(sidecar_bin, "A", tmp / "a.stderr")
        side_a.send(
            op="up", requestId="up-a", keyFile=str(tmp / "a.key"),
            owner="owner", deviceId="a", membershipStore=str(tmp / "a/membership.json"),
            inboundTarget=f"unix:{tmp / 'a-backend'}", peerPort=7447,
            addressFile=str(tmp / "a.addr"), derp=derp,
        )
        side_a.expect("state")  # Starting
        running_a = side_a.expect("state")  # Running
        address_a = (tmp / "a.addr").read_text().strip()

        # Node B: four fixed slots; the last is observed through a tap
        # that counts zenoh's dials, the first takes the bind/unbind
        # trials. On tcp the up carries grow specs
        # and the resolved ids come back on the Running state.
        if TRANSPORT == "tcp":
            slot_specs = ["tcp:127.0.0.1:0"] * 4
        else:
            slot_specs = [str(tmp / f"slot-0{i}") for i in range(4)]
        tap_path = tmp / "tap-03"
        side_b = Sidecar(sidecar_bin, "B", tmp / "b.stderr")
        side_b.send(
            op="up", requestId="up-b", keyFile=str(tmp / "b.key"),
            owner="owner", deviceId="b", membershipStore=str(tmp / "b/membership.json"),
            inboundTarget=f"unix:{tmp / 'b-backend'}",
            addressFile=str(tmp / "b.addr"), derp=derp,
            slots=slot_specs,
        )
        side_b.expect("state")
        running = side_b.expect("state")
        slot_ids = running.get("slots")
        if not isinstance(slot_ids, list) or len(slot_ids) != 4:
            raise RuntimeError(f"B Running state lacks the slot ids: {running}")
        if TRANSPORT == "tcp" and any(item.endswith(":0") for item in slot_ids):
            raise RuntimeError(f"tcp slot ids were not resolved: {slot_ids}")
        results["transport"] = TRANSPORT
        results["slot_ids"] = slot_ids
        rows = [{"owner": "owner", "deviceId": name,
                 "masterPublic": state["clientPublic"], "serverPublic": state["serverPublic"],
                 "address": (tmp / f"{name}.addr").read_text().strip(),
                 "clientPublicByServer": {}, "state": "active", "revision": 1}
                for name, state in (("a", running_a), ("b", running))]
        authority = {key: rows[0][key] for key in
                     ("owner", "deviceId", "masterPublic", "serverPublic", "address")}
        authority["controlPort"] = 7448
        for side, imported in ((side_a, rows), (side_b, [])):
            side.membership("membership-init", {
                "operationId": "init", "tailnetId": "slots", "authority": authority,
                "migration": {"source": "directory-0.5.5", "sourceDigest": "fixture",
                              "completed": True}, "members": imported,
            })
        side_b.membership("roster-fetch", {"tailnetId": "slots"})
        for side, row in ((side_b, rows[1]), (side_a, rows[0])):
            side.membership("member-announce", {
                "operationId": "announce-" + row["deviceId"], "tailnetId": "slots",
                "address": row["address"], "clientPublicByServer": {},
            })
        side_b.membership("roster-fetch", {"tailnetId": "slots"})
        results["control_before_zenoh"] = not (tmp / "a-backend").exists()
        if not results["control_before_zenoh"]:
            raise RuntimeError("bootstrap unexpectedly required Zenoh")
        events_a: list[str] = []
        zenoh_a = ZenohTransport(
            ZenohConfig(listen=(f"unixsock-stream/{tmp / 'a-backend'}",)),
            event_sink=lambda event, fields: events_a.append(event),
        )
        zenoh_a.declare_liveliness("live/a")

        def pump() -> None:
            while True:
                try:
                    zenoh_a.put("bench/a", b"x")
                except Exception:
                    return
                time.sleep(0.02)

        threading.Thread(target=pump, daemon=True).start()
        tap = Tap(tap_path, slot_ids[3])

        got: list[float] = []
        live: list[tuple[float, str, str]] = []
        events_b: list[str] = []
        endpoints = tuple(
            [slot_locator(slot_id) for slot_id in slot_ids[:3]] + [tap.locator]
        )
        zenoh_b = ZenohTransport(
            ZenohConfig(connect=endpoints),
            event_sink=lambda event, fields: events_b.append(event),
        )
        zenoh_b.subscribe("bench/**", lambda s: got.append(time.monotonic()))
        zenoh_b.observe_liveliness(
            "live/**",
            lambda s: live.append((time.monotonic(), str(s.key), str(s.kind).lower())),
        )

        # Reading 1: idle dial rate on the never-bound tapped slot.
        idle_started = time.monotonic()
        time.sleep(IDLE_WINDOW_SECONDS)
        idle_rate = tap.count / (time.monotonic() - idle_started)
        results["idle_dials_per_second_per_slot"] = round(idle_rate, 3)
        if idle_rate > 0.2:
            failures.append(f"idle dial rate {idle_rate:.3f}/s > 0.2/s")
        if got:
            failures.append("data flowed before any slot was bound")

        warm_tailcat(side_b, address_a)

        # Reading 2: hold-mode first-connect latency over bind/unbind trials.
        latencies: list[float] = []
        for trial in range(TRIALS):
            time.sleep(1.0)  # zenoh has redialed slot-00; the sidecar holds it
            mark = len(got)
            bound_at = time.monotonic()
            side_b.send(op="map-peer", peer="ztrial", address=address_a, slot=slot_ids[0])
            mapped = side_b.expect("peer-mapped")
            if mapped.get("slot") != slot_ids[0] or "localPort" in mapped:
                raise RuntimeError(f"peer-mapped shape is wrong: {mapped}")
            first = wait_until(lambda: len(got) > mark, 30.0, "first sample after bind")
            latencies.append(first - bound_at)
            unmapped_at = time.monotonic()
            side_b.send(op="unmap-peer", peer="ztrial")
            side_b.expect("peer-unmapped")
            if trial == TRIALS - 1:
                # Reading 3a: the old peer's liveliness token gets a DELETE.
                wait_until(
                    lambda: any(
                        when > unmapped_at and key.endswith("live/a") and "delete" in kind
                        for when, key, kind in live
                    ),
                    20.0,
                    "liveliness DELETE after unmap",
                )
                # Reading 3b: data stops once the slot is back in hold mode.
                time.sleep(0.5)
                settled = len(got)
                time.sleep(2.0)
                if len(got) != settled:
                    failures.append("data still flowing after unmap-peer")
        results["first_connect_seconds"] = {
            "min": round(min(latencies), 4),
            "median": round(statistics.median(latencies), 4),
            "max": round(max(latencies), 4),
        }
        p95 = sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)]
        results["first_connect_p95_seconds"] = round(max(p95, max(latencies)), 4)
        if max(latencies) >= 1.0:
            failures.append(f"first-connect max {max(latencies):.3f}s >= 1s")

        # Reading 3c: a later map-peer resumes data on the same slot.
        mark = len(got)
        side_b.send(op="map-peer", peer="ztrial", address=address_a, slot=slot_ids[0])
        side_b.expect("peer-mapped")
        wait_until(lambda: len(got) > mark, 30.0, "data after remap")
        results["remap_resumes_data"] = True

        # Reading 4: B's zenoh session was never rebuilt.
        closed = events_b.count("zenoh.session.closed")
        results["zenoh_session_closed_on_B"] = closed
        if closed:
            failures.append(f"zenoh.session.closed happened {closed} times on B")
        if any(key.endswith("live/a") and kind == "put" for _, key, kind in live) is False:
            failures.append("A's liveliness token was never seen on B")
    finally:
        for transport in (zenoh_b, zenoh_a):
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass
        for sidecar in (side_b, side_a):
            if sidecar is not None:
                sidecar.close()
        if relay is not None:
            relay.terminate()
            relay.wait(timeout=10)
        shutil.rmtree(tmp, ignore_errors=True)

    print(json.dumps({"results": results, "failures": failures}, indent=2))
    print("RESULT:", "FAIL" if failures else "PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
