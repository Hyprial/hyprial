#!/usr/bin/env python3
"""Isolated real-daemon join through three rejected regions and a local proxy.

Build the sidecar, labrelay and internal/proxyfixture first. Run this script under
an OS sandbox denying outbound UDP and non-loopback TCP. It uses disposable homes,
synthetic proxy authentication, and the existing E2E-025 daemon isolation runner.
No production configuration, credentials or daemon is read or changed.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import json
from pathlib import Path
import runpy
import re
import select
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "contract" / "e2e"))

from _daemon_runner import IsolatedDaemon, free_tcp_endpoint, hyprial_executable  # noqa: E402

HELPERS = runpy.run_path(
    str(ROOT / "contract" / "e2e" / "run-tailcat-org-invite-join.py")
)
ORG = "group-proxy-round3"


class ConnectProxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, target: tuple[str, int]):
        self.target = target
        self.counts: Counter[str] = Counter()
        self.guard = threading.Lock()
        self.connections: set[socket.socket] = set()
        self.stopping = threading.Event()
        super().__init__(("127.0.0.1", 0), ConnectHandler)

    def record(self, target: str) -> None:
        with self.guard:
            self.counts[target] += 1

    def close_connections(self) -> None:
        self.stopping.set()
        with self.guard:
            connections = list(self.connections)
        for conn in connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()

    def handle_error(self, request, client_address):
        self.record("handler-error")


class ConnectHandler(socketserver.StreamRequestHandler):
    def handle(self):
        server: ConnectProxy = self.server
        self.connection.settimeout(120)
        with server.guard:
            server.connections.add(self.connection)
        try:
            line = self.rfile.readline(8192).decode("ascii", errors="replace").strip()
            if not line:
                return
            headers = {}
            for _ in range(32):
                header = (
                    self.rfile.readline(8192).decode("ascii", errors="replace").strip()
                )
                if not header:
                    break
                name, _, value = header.partition(":")
                headers[name.lower()] = value.strip()
            if headers.get("proxy-authorization") != "Basic dXNlcjpzZWNyZXQ=":
                server.record("auth-failed")
                self.wfile.write(
                    b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n"
                )
                return
            parts = line.split()
            if len(parts) != 3 or parts[0] != "CONNECT":
                server.record("method-failed")
                return
            target = parts[1]
            server.record(target)
            if target != "relay.invalid:443":
                self.wfile.write(
                    b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                return
            with socket.create_connection(server.target, timeout=2) as remote:
                self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self.wfile.flush()
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline and not server.stopping.is_set():
                    ready, _, _ = select.select([self.connection, remote], [], [], 0.5)
                    for source in ready:
                        data = source.recv(65536)
                        if not data:
                            return
                        (
                            remote if source is self.connection else self.connection
                        ).sendall(data)
        except (OSError, ValueError):
            return
        finally:
            with server.guard:
                server.connections.discard(self.connection)


@contextmanager
def recording_proxy(target: tuple[str, int]):
    proxy = ConnectProxy(target)
    thread = threading.Thread(target=proxy.serve_forever, kwargs={"poll_interval": 0.1})
    thread.start()
    try:
        yield proxy
    finally:
        proxy.close_connections()
        proxy.shutdown()
        proxy.server_close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise AssertionError("owned proxy did not stop")


def prepend_rejected_regions(
    daemon: IsolatedDaemon, space_id: str, fixture: Path
) -> None:
    """Create the synthetic authority pin through fenced offline maintenance."""
    from hyprial.daemon.impl.forwarding.membership.maintenance import TailnetMaintenance

    daemon.stop()
    daemon.wait_stopped()
    with TailnetMaintenance(
        daemon.root / "home", daemon.root / "state", environ=daemon.env
    ) as maintenance:
        maintenance.prepare(space_id, daemon.node_id)
        authority = maintenance._plan["params"]["authority"]
        completed = subprocess.run(
            [str(fixture)],
            input=authority["address"].encode(),
            capture_output=True,
            timeout=10,
        )
        if completed.returncode or not completed.stdout.startswith(b"tc"):
            raise AssertionError("address fixture failed")
        authority["address"] = completed.stdout.decode().strip()
        maintenance.commit(confirmed=True)
    daemon.start()


def run(binary: Path, relay_binary: Path, fixture: Path) -> dict:
    for path in (binary, relay_binary, fixture):
        if not path.is_file():
            raise FileNotFoundError("explicit fixture binary missing")
    relay, local_derp = HELPERS["_start_labrelay"](str(relay_binary))
    daemons = []
    try:
        with tempfile.TemporaryDirectory(prefix="hr-px-", dir="/tmp") as directory:
            root = Path(directory)
            with recording_proxy((local_derp["host"], local_derp["derpPort"])) as proxy:
                derp = {
                    "host": "relay.invalid",
                    "derpPort": 443,
                    "stunPort": local_derp["stunPort"],
                    "insecureForTests": True,
                }
                for node, owner in (("a", "alice"), ("b", "bob")):
                    listen = free_tcp_endpoint()
                    daemon = IsolatedDaemon(
                        hyprial_executable(),
                        root / node,
                        node,
                        listen=listen,
                        tailcat_forwarding=True,
                    )
                    HELPERS["_configure_tailcat"](
                        daemon,
                        owner=owner,
                        listen=listen,
                        binary=str(binary),
                        derp=derp,
                    )
                    daemon.env.update(
                        HTTPS_PROXY=f"http://user:secret@127.0.0.1:{proxy.server_address[1]}",
                        NO_PROXY="127.0.0.1,localhost,::1",
                        GOMAXPROCS="2",
                    )
                    daemons.append(daemon)
                try:
                    for daemon in daemons:
                        daemon.start()
                        HELPERS["_wait"](
                            "sidecar address",
                            lambda d=daemon: HELPERS["_read_address"](d),
                            lambda value: isinstance(value, str),
                            timeout=45,
                        )
                    a, b = daemons
                    created = a.command("org", "create", ORG, "--json", timeout=45)
                    if not created.get("ok") or not created.get("devicePublished"):
                        raise AssertionError("authority org creation failed")
                    prepend_rejected_regions(a, created["spaceId"], fixture)
                    invited = a.command("org", "invite", ORG, "--user", "bob", "--json")
                    link = invited.get("link")
                    if not isinstance(link, str):
                        raise AssertionError("missing synthetic invitation")
                    started = time.monotonic()
                    joined = b.command(
                        "org",
                        "join",
                        "-",
                        "--json",
                        stdin=link,
                        timeout=150,
                        exits=(0, 1),
                    )
                    elapsed = time.monotonic() - started
                    if not joined.get("ok") or joined.get("org") != ORG:
                        code = joined.get("code", "")
                        return {
                            "result": "FAIL",
                            "stage": "org.join",
                            "code": code
                            if isinstance(code, str) and re.fullmatch("[A-Z_]+", code)
                            else "UNKNOWN",
                            "joinElapsedSeconds": round(elapsed, 3),
                            "proxyCounts": dict(proxy.counts),
                        }
                    if not joined.get("steps") or not all(
                        row.get("ok") for row in joined["steps"]
                    ):
                        raise AssertionError("join bootstrap steps incomplete")
                    for host in (
                        "blocked.invalid:443",
                        "also-blocked.invalid:443",
                        "third-blocked.invalid:443",
                        "relay.invalid:443",
                    ):
                        if proxy.counts[host] == 0:
                            raise AssertionError(
                                "four-region proxy path was not exercised"
                            )
                    if proxy.counts["auth-failed"] or proxy.counts["method-failed"]:
                        raise AssertionError(
                            "proxy authentication or hostname protocol failed"
                        )
                    return {
                        "result": "PASS",
                        "realDaemonCLIJoin": True,
                        "joinElapsedSeconds": round(elapsed, 3),
                        "proxyCounts": dict(proxy.counts),
                        "sandboxRequired": "deny UDP and non-loopback TCP",
                        "cloudAcceptance": "BLOCKED",
                    }
                finally:
                    failures = []
                    for daemon in reversed(daemons):
                        try:
                            daemon.stop()
                            if (daemon.root / "state").is_dir():
                                daemon.wait_stopped()
                        except Exception as error:
                            failures.append(type(error).__name__)
                    if failures:
                        raise AssertionError("isolated daemon cleanup failed")
    finally:
        HELPERS["_stop_labrelay"](relay)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--relay", type=Path, required=True)
    parser.add_argument("--address-fixture", type=Path, required=True)
    args = parser.parse_args()
    signal.signal(
        signal.SIGALRM,
        lambda *_: (_ for _ in ()).throw(TimeoutError("runner deadline")),
    )
    signal.alarm(600)
    try:
        result = run(
            args.binary.resolve(), args.relay.resolve(), args.address_fixture.resolve()
        )
    except Exception as error:
        errors = []
        cause = error
        while cause is not None:
            frames = traceback.extract_tb(cause.__traceback__)
            codes = re.findall(r'["\']code["\']:\s*["\']([A-Z_]+)["\']', str(cause))
            errors.append(
                {
                    "errorType": type(cause).__name__,
                    "codes": codes,
                    "frames": [
                        f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in frames
                    ],
                }
            )
            cause = cause.__context__
        print(json.dumps({"result": "FAIL", "errors": errors}))
        return 1
    finally:
        signal.alarm(0)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
