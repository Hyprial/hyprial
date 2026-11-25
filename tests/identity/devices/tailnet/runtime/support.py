"""Synthetic network facts plus real socket frames, crypto and isolated files."""

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import threading
from uuid import uuid4

from cryptography.hazmat.primitives import serialization

from hyprial.identity import (
    TailnetAuthorityPin, TailnetCredentials, TailnetMember, TailnetMembership, write_device_record,
)
from tests.identity.devices.binding import support as b


def encoded(value):
    return json.dumps(value, separators=(",", ":")).encode()


def frame(body):
    return b"HCA1\n" + struct.pack("!I", len(body)) + body


class Context:
    def __init__(self, home):
        self.home = Path(home)
        self.home.mkdir(parents=True, mode=0o700, exist_ok=True)
        (self.home / "state").mkdir(mode=0o700, exist_ok=True)
        self.record = b.device_record(uuid4().hex)
        write_device_record(self.home, self.record)
        self.ca = b.make_ca()
        self.targets = []
        self.requests = []
        self.errors = []
        self.threads = []
        self.change = lambda target, value: value
        self.raw = None
        self.observations = {}
        self.rows_override = None
        self.on_dial = None

    def pin(self, tailnet_id):
        return TailnetAuthorityPin(tailnet_id, "network-authority", "authority-node",
                                  "nodekey:authority-master", "nodekey:authority-server",
                                  "opaque-tailcat-address", 7448)

    def membership(self, tailnet_id):
        return self.observations.get(tailnet_id) or TailnetMembership(
            tailnet_id, "member", 1, "authority-node", True, 250, None,
            TailnetMember(self.record.owner, self.record.device_id, "active"), self.pin(tailnet_id))

    def members(self, tailnet_id):
        if self.rows_override is not None:
            return self.rows_override
        record = self.record
        p = self.pin(tailnet_id)
        row = TailnetCredentials.MemberProjection
        return (row(record.owner, record.device_id, record.client_public, record.server_public,
                    (), "own-tailcat-address", "active", 1),
                row(p.owner, p.device_id, p.master_public, p.server_public, (), p.address, "active", 1))

    def descriptor(self, target):
        p = target.authority
        return {"version": "hyprial.tailnet.credential-authority.v1", "tailnetId": p.tailnet_id,
                "authority": {"owner": p.owner, "deviceId": p.device_id, "masterPublic": p.master_public,
                              "serverPublic": p.server_public, "address": p.address, "controlPort": p.control_port},
                "origin": "https://test.credential.invalid:" + str(target.remote_port),
                "serverName": "test.credential.invalid", "remotePort": target.remote_port,
                "caCertificatePem": self.ca.pem.decode(),
                "caSha256": hashlib.sha256(self.ca.certificate.public_bytes(serialization.Encoding.DER)).hexdigest()}

    def dial(self, target):
        self.targets.append(target)
        if self.on_dial:
            self.on_dial(target)
        body = self.change(target, self.descriptor(target))
        response = self.raw if self.raw is not None else frame(encoded(body))
        client, server = socket.socketpair()

        def serve():
            try:
                server.settimeout(5)
                request = bytearray()
                while len(request) < 9:
                    request.extend(server.recv(9 - len(request)))
                size = struct.unpack("!I", request[5:9])[0]
                while len(request) < 9 + size:
                    chunk = server.recv(9 + size - len(request))
                    if not chunk:
                        raise ValueError("truncated test request")
                    request.extend(chunk)
                self.requests.append(bytes(request))
                server.sendall(response)
                server.shutdown(socket.SHUT_WR)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Expected rejection may close before reading invalid bytes.
            except BaseException as error:
                self.errors.append(type(error).__name__)
            finally:
                server.close()

        thread = threading.Thread(target=serve)
        self.threads.append(thread)
        thread.start()
        return client

    def facade(self):
        return TailnetCredentials(self.home, membership=self, dial=self.dial)

    def runtime(self, tailnet_id="tailnet-A"):
        return self.facade().runtime(tailnet_id=tailnet_id)

    def provision(self, tailnet_id="tailnet-A"):
        return self.facade().bootstrap(tailnet_id=tailnet_id)

    def close(self):
        for thread in self.threads:
            thread.join(6)
            assert not thread.is_alive()
        assert not self.errors


def state(home, tailnet_id="tailnet-A"):
    token = hashlib.sha256(tailnet_id.encode()).hexdigest()
    return Path(home) / "state/transport/tailnets" / token


def secret(home, tailnet_id="tailnet-A"):
    token = hashlib.sha256(tailnet_id.encode()).hexdigest()
    return Path(home) / "secrets/transport/tailnets" / token


def inventory(home):
    return {str(path.relative_to(home)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in Path(home).rglob("*") if path.is_file()}


def prepare(context, identifier=None, tailnet_id="tailnet-A"):
    return context.runtime(tailnet_id).prepare(kind="enroll", operation_id=identifier or str(uuid4()), expected_generation=0)


def child(code, *args, timeout=30):
    environment = dict(os.environ)
    environment["HYPRIAL_NETWORK_ISOLATED"] = "1"
    return subprocess.run(["uv", "run", "--no-sync", "python", "-c", code, *map(str, args)],
                          env=environment, capture_output=True, text=True, timeout=timeout)


def unavailable(context, code="MEMBERSHIP_LEASE_EXPIRED"):
    context.observations["tailnet-A"] = replace(context.membership("tailnet-A"), member_ready=False, code=code)
