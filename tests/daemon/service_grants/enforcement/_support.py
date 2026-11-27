"""Shared setup for the exposing-side enforcement tests.

Decision (service grant exposing side, 2026-11-26): everything the admission set is
derived from is produced by product code over a real OrgFS runtime, so grant
documents verify against their real author. Device rows come from the real
``OrgNetworkService`` publish path and grants from the real
``ServiceGrantService``. The sidecar roster is the one test input, because in
production it comes from the sidecar; it is given to the stdio fixture, whose
admission mirrors ``engine.admits`` plus ``exposuregate.Admits``. Nothing
here fills a directory or roster field from a test: roster rows are built by
the fixture from a device's identity.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from hyprial.daemon.impl.forwarding.store import ExposureStore
from hyprial.daemon.impl.org.network.directory import (
    InvalidDeviceQuarantine,
    OrgFsDirectoryStore,
)
from hyprial.daemon.impl.org.network.service import OrgNetworkService
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.service_grants import ServiceGrantService
from hyprial.identity import DeviceRecord, write_device_record
from tests.support.tailnet_membership import FakeMembershipSidecar, key

ALICE = "alice"
BOB = "bob"
CAROL = "carol"
ORG = "group-acme"
PORT = 8447
NOW_MS = 1_790_000_000_000
FROZEN_NOW = datetime(2026, 11, 26, tzinfo=UTC)
TAILNET_ID = "space-group-acme"
FIXTURE = (
    Path(__file__).resolve().parents[4] / "tests/fixtures/network/fake_forwarding_sidecar.py"
)


@dataclass(frozen=True)
class Device:
    """One organisation member: the identity its device record carries."""

    owner: str
    device_id: str

    @property
    def server_public(self) -> str:
        return key(f"server-{self.device_id}")

    @property
    def master_public(self) -> str:
        return key(f"client-{self.device_id}")

    @property
    def address(self) -> str:
        return f"{self.server_public}@127.0.0.1:7447"

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.device_id}"


ALICE_NODE = Device(ALICE, "device-a")
BOB_NODE = Device(BOB, "device-b")
CAROL_NODE = Device(CAROL, "device-c")
MEMBERS = (ALICE_NODE, BOB_NODE, CAROL_NODE)


def peer_key(member: Device, exposing: Device) -> str:
    """The key ``member`` presents when it dials ``exposing``'s Tailcat server.

    One client key per (member, target server): the rule the sidecar derives
    and the membership fakes store in the announcing member's roster row.
    """

    return key(member.master_public + exposing.server_public)


class Harness:
    """One organisation with three devices, every row written by product code."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.runtime = OrgFsRuntime(
            root / "orgfs-state", node_id=ALICE_NODE.device_id, author=f"user:{ALICE}"
        )
        self.fs = self.runtime.facade
        self.membership = FakeMembershipSidecar(
            owner=ALICE, device_id=ALICE_NODE.device_id, now_ms=NOW_MS
        )
        self.store = OrgFsDirectoryStore(self.fs)
        space = self.fs.create_space(ORG)
        self.fs.invite(space.space_id, f"user:{BOB}")
        self.fs.invite(space.space_id, f"user:{CAROL}")
        self.space_id = space.space_id
        self.store.ensure_org_directory_roots(ORG)
        self.homes: dict[str, Path] = {}
        for member in MEMBERS:
            home = root / f"{member.owner}-home"
            home.mkdir(mode=0o700)
            self.homes[member.owner] = home
            write_device_record(
                home,
                DeviceRecord(
                    device_id=member.device_id,
                    owner=member.owner,
                    server_public=member.server_public,
                    client_public=member.master_public,
                ),
            )
        self.clock = [NOW_MS]
        self.events: list[tuple[str, str, str, dict[str, Any]]] = []
        self.exposures = ExposureStore(root / "network-exposures.json")
        self.exposures.set(
            {"port": PORT, "target": f"tcp:127.0.0.1:{PORT}", "proxyProtocol": "none"}
        )

    @contextmanager
    def as_(self, owner: str) -> Iterator[None]:
        """Write as ``owner``, the way a daemon owned by that member would."""

        previous = self.fs.author
        self.fs.author = f"user:{owner}"
        try:
            yield
        finally:
            self.fs.author = previous

    def publisher(self, member: Device) -> OrgNetworkService:
        """The real service for ``member``, over the real OrgFS facade."""

        return OrgNetworkService(
            fs=self.fs,
            home=self.homes[member.owner],
            node_id=member.device_id,
            owner=member.owner,
            store=self.store,
            membership_request=self.membership.for_device(
                member.owner, member.device_id
            ).membership_request,
            invalid_devices=InvalidDeviceQuarantine(),
            derive_client_key=lambda server: key(f"derive|{server}"),
            read_own_address=lambda: member.address,
            sleep=lambda _seconds: None,
            now=lambda: FROZEN_NOW,
        )

    def publish(self, member: Device) -> dict[str, Any]:
        """Run the real publish for ``member`` and return the row it wrote."""

        with self.as_(member.owner):
            result = self.publisher(member).publish_self()
        assert result == {"published": [ORG], "skipped": [], "errors": []}, result
        return self.device_row(member)

    def device_row(self, member: Device) -> dict[str, Any]:
        text, _version = self.fs.read_text(
            self.space_id, f"directory/devices/{member.owner}/{member.device_id}.json"
        )
        return json.loads(text)

    def grants(self, member: Device = ALICE_NODE) -> ServiceGrantService:
        return ServiceGrantService(
            store=self.store,
            home=self.homes[member.owner],
            owner=member.owner,
            local_device_id=member.device_id,
            exposed_ports=lambda: frozenset(
                int(item["port"]) for item in self.exposures.list()
            ),
            now_ms=lambda: self.clock[0],
            logger=self.log,
        )

    def log(self, level: str, component: str, event: str, **fields: Any) -> None:
        self.events.append((level, component, event, fields))

    def close(self) -> None:
        self.runtime.close()


class DirectSidecar:
    """The stdio fixture driven directly, for cases about the fixture's own gate."""

    def __init__(self, root: Path, *, members=MEMBERS, without_server_key: tuple = ()) -> None:
        self.receipt = root / "direct-receipt.jsonl"
        self.roster_path = root / "direct-roster.json"
        self.write_roster(members, without_server_key=without_server_key)
        environment = dict(os.environ)
        environment.update(
            FAKE_FORWARD_RECEIPT=str(self.receipt), FAKE_FORWARD_ROSTER=str(self.roster_path)
        )
        self.process = subprocess.Popen(
            [sys.executable, str(FIXTURE)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=environment,
        )
        self.request_id = 0
        self.request(_up(root), "state")

    def write_roster(self, members: tuple, *, without_server_key: tuple = ()) -> None:
        self.roster_path.write_text(
            json.dumps(roster_document(members, without_server_key=without_server_key)),
            encoding="utf-8",
        )

    def _exchange(self, payload: dict[str, Any], event: str) -> dict[str, Any]:
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()
        while True:
            line = self.process.stdout.readline()
            assert line, f"sidecar exited while waiting for {event!r}"
            answer = json.loads(line)
            if answer.get("event") in {event, "error"} and answer.get(
                "requestId"
            ) == payload.get("requestId"):
                return answer

    def request(self, payload: dict[str, Any], event: str) -> dict[str, Any]:
        self.request_id += 1
        answer = self._exchange(
            {"v": 7, "requestId": f"r{self.request_id}", **payload}, event
        )
        assert answer["event"] != "error", (payload.get("op"), answer)
        return answer

    def expose(self, admission: str, keys: list[str], *, port: int = PORT) -> dict[str, Any]:
        return self.request(
            {
                "op": "expose",
                "port": port,
                "target": f"tcp:127.0.0.1:{port}",
                "proxyProtocol": "",
                "admission": admission,
                "keys": keys,
            },
            "exposed",
        )

    def connect(self, member: Device, *, port: int = PORT) -> dict[str, Any]:
        return self.request(
            {"op": "fixture-connect", "port": port, "device": member.device_id},
            "fixture-connected",
        )

    def close(self) -> None:
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
        self.process.stdout.close()


def roster_document(members: tuple, *, without_server_key: tuple = ()) -> dict[str, Any]:
    """What the test tells the fixture about the sidecar's roster."""

    return {
        "tailnetId": TAILNET_ID,
        "serverPublic": ALICE_NODE.server_public,
        "members": [
            {
                "deviceId": member.device_id,
                "owner": member.owner,
                "masterPublic": member.master_public,
                "serverPublic": member.server_public,
            }
            for member in members
        ],
        "withoutServerKey": [member.device_id for member in without_server_key],
    }


def _up(root: Path) -> dict[str, Any]:
    return {
        "v": 7,
        "requestId": "up-fixture",
        "op": "up",
        "keyFile": str(root / "device-key.json"),
        "membershipStore": str(root / "membership.json"),
        "owner": "fixture",
        "deviceId": "fixture",
        "controlPort": 7448,
        "addressFile": str(root / "address"),
        "inboundTarget": "tcp:127.0.0.1:0",
        "peerPort": 7447,
        "slots": [],
    }
