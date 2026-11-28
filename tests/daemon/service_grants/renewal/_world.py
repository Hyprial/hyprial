"""One org, three devices, every row and grant written by product code, plus each recipient's keys.

Decision (service grant integration fix, 2026-11-27): the renewal, cleanup
and bounded-listing tests need both ends of a grant at once: the grantor's
real ``ServiceGrantService`` over a real OrgFS runtime, and the recipient's
real consumer and service-connect manager over the same runtime. Device rows
are published by the real ``OrgNetworkService`` with each member's encryption
key, so a credential grant seals to a key the recipient can open.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.org.network.device_key import (
    EncryptionKeyCache,
    encryption_key_reader,
    ensure_encryption_key,
)
from hyprial.daemon.impl.org.network.directory import InvalidDeviceQuarantine
from hyprial.daemon.impl.org.network.service import OrgNetworkService
from hyprial.daemon.impl.service_connect import ServiceManager
from hyprial.daemon.impl.service_grants import ServiceGrantService
from hyprial.daemon.impl.service_grants.consumer import GrantConsumer
from hyprial.identity import DeviceRecord, write_device_record
from hyprial.kernel import atomic_json_write
from tests.daemon.service_connect.support import FakeController, FakeStatePort, ManualScheduler
from tests.daemon.service_grants.consumer.support import (
    ISSUER,
    Recorder,
    device_uid,
    write_user_provider,
)
from tests.daemon.service_grants.enforcement._support import (
    ALICE_NODE,
    BOB_NODE,
    CAROL_NODE,
    FROZEN_NOW,
    MEMBERS,
    ORG,
    PORT,
    Device,
    Harness,
)
from tests.support.tailnet_membership import key

SECRET = "SYNTHETIC-RENEWAL-KEY-0123456789"
ROTATED = "SYNTHETIC-RENEWAL-KEY-ROTATED-9876543210"
DAY_MS = 86_400_000


def tailcat_address(member: Device) -> str:
    """A Tailcat address of the shape service-connect accepts for a protected device."""

    return "tc" + member.device_id.replace("-", "") + "RenewalAddress"


class World(Harness):
    """The enforcement harness, with encryption keys, a consumer and a manager per member."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.states: dict[str, Path] = {}
        self.caches: dict[str, EncryptionKeyCache] = {}
        for member in MEMBERS:
            home = self.homes[member.owner]
            atomic_json_write(
                home / "settings.json",
                {
                    "owner": member.owner,
                    "identityMode": "casdoor",
                    "identityIssuer": ISSUER,
                    "userId": f"subject-{member.owner}",
                },
            )
            write_device_record(
                home,
                DeviceRecord(
                    device_id=member.device_id,
                    owner=member.owner,
                    server_public=member.server_public,
                    client_public=member.master_public,
                    device_uid=device_uid(member.owner, member.device_id),
                    name=member.device_id,
                ),
            )
            (home / "secrets").mkdir(mode=0o700, exist_ok=True)
            (home / "secrets" / "providers").mkdir(mode=0o700, exist_ok=True)
            state = root / f"{member.owner}-state"
            self.states[member.owner] = state
            self.caches[member.owner] = EncryptionKeyCache()
            ensure_encryption_key(home, state)
        write_user_provider(self.homes["alice"], "gateway", {"API_KEY": SECRET})
        self.recorder = Recorder()

    def publisher(self, member: Device) -> OrgNetworkService:
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
            read_own_address=lambda: tailcat_address(member),
            sleep=lambda _seconds: None,
            now=lambda: FROZEN_NOW,
            encryption_key=encryption_key_reader(
                home=self.homes[member.owner],
                state_dir=self.states[member.owner],
                cache=self.caches[member.owner],
            ),
        )

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

    def consumer(self, member: Device = BOB_NODE) -> GrantConsumer:
        return GrantConsumer(
            orgfs=self.fs,
            home=self.homes[member.owner],
            state_dir=self.states[member.owner],
            owner=member.owner,
            device_id=member.device_id,
            now_ms=lambda: self.clock[0],
            logger=self.recorder,
        )

    def manager(self, source: Any, actor_host: Any, member: Device = BOB_NODE) -> tuple[ServiceManager, FakeController]:
        controller = FakeController()
        manager = ServiceManager(
            orgfs=self.fs,
            state=FakeStatePort(),
            persistence_late_result=None,
            controller_factory=lambda: controller,
            access_path=self.homes[member.owner] / "state" / "tailcat" / "service-access.json",
            scheduler=ManualScheduler(),
            actor_host=actor_host,
            derived_source=source,
        )
        return manager, controller

    def grant_docs(self, owner: str = "alice") -> list[dict[str, Any]]:
        try:
            nodes = self.fs.listdir(self.space_id, f"directory/service-grants/{owner}")
        except Exception:
            return []
        docs = []
        for node in nodes:
            text, _version = self.fs.read_text(self.space_id, node.path.lstrip("/"))
            docs.append(json.loads(text))
        return docs

    def fill(self, owner: str, count: int, *, revoked_at_ms: int) -> list[str]:
        """Write ``count`` revoked documents under ``owner``'s path, as ``owner``."""

        from hyprial.daemon.impl.service_grants.models import (
            SCHEMA_VERSION,
            GrantRecipient,
            ServiceEndpoint,
            ServiceGrant,
            generate_grant_id,
        )

        member = next(item for item in MEMBERS if item.owner == owner)
        ids = []
        with self.as_(owner):
            for _index in range(count):
                grant_id = generate_grant_id()
                grant = ServiceGrant(
                    schema_version=SCHEMA_VERSION,
                    grant_id=grant_id,
                    service=ServiceEndpoint(device_id=member.device_id, port=PORT),
                    recipient=GrantRecipient(owner="carol", device_id=CAROL_NODE.device_id),
                    granted_at_ms=revoked_at_ms - 1,
                    revoked_at_ms=revoked_at_ms,
                )
                self.store.put_service_grant(ORG, owner, grant_id, grant.as_record())
                ids.append(grant_id)
        return ids

    def events_of(self, name: str) -> list[dict[str, Any]]:
        return [fields for _level, _component, event, fields in self.events if event == name]


__all__ = [
    "ALICE_NODE",
    "BOB_NODE",
    "CAROL_NODE",
    "DAY_MS",
    "MEMBERS",
    "ORG",
    "PORT",
    "ROTATED",
    "SECRET",
    "World",
]
