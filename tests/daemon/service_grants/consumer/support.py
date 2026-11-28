"""Test support for the recipient side of service grants.

Decision (service grant recipient side, 2026-11-26): one org directory as the recipient
device sees it. Alice owns the org, exposes port 8447 on ``device-a`` and
grants it to ``bob/device-b`` or ``@members``. Grants come from the real
grantor service over the real sealed-envelope profile, so the recipient opens
exactly what service.grant writes. Device rows carry the shape service-connect validates
(``nodekey:`` + 64 hex, a ``tc`` address). The recipient home has a complete
committed account and a real key store bound to this machine's fingerprint,
because the opener reads its private half through the production rules.
"""

from __future__ import annotations

import json
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from hyprial.daemon.impl.org.network.device_key import ensure_encryption_key
from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.org.network.state import add_left_org
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.service_connect import ServiceManager
from hyprial.daemon.impl.service_grants import ServiceGrantService
from hyprial.identity import DEVICE_RECORD_RELPATH, DeviceRecord, DirectoryDevice, write_device_record
from hyprial.kernel import atomic_json_write, write_private_file_atomic

from tests.daemon.service_connect.support import ManualScheduler

ORG = "group-acme"
OTHER_ORG = "group-other"
SECRET = "SYNTHETIC-RECIPIENT-KEY-0123456789ABCDEF"
PORT = 8447
OTHER_PORT = 8448
ISSUER = "https://issuer.example.test"
KEY_FIELD = "KEY"


class Clock:
    def __init__(self, now_ms: int = 1_700_000_000_000) -> None:
        self.value = now_ms

    def __call__(self) -> int:
        return self.value


class Recorder:
    """Collects daemon log events."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.events: list[tuple[str, str, str, dict[str, Any]]] = []

    def __call__(self, level: str, component: str, event: str, **fields: Any) -> None:
        with self.lock:
            self.events.append((level, component, event, fields))

    def names(self) -> list[str]:
        return [event for _level, _component, event, _fields in self.events]

    def of(self, event: str) -> list[dict[str, Any]]:
        return [fields for _level, _component, name, fields in self.events if name == event]

    def text(self) -> str:
        return json.dumps(self.events, default=str)


@contextmanager
def as_author(runtime: OrgFsRuntime, owner: str) -> Iterator[None]:
    previous = runtime.facade.author
    runtime.facade.author = f"user:{owner}"
    try:
        yield
    finally:
        runtime.facade.author = previous


def device_row(
    device_id: str, owner: str, *, keys: dict[str, str] | None = None, generation: int = 1
) -> DirectoryDevice:
    return DirectoryDevice(
        device_id=device_id,
        owner=owner,
        server_public="nodekey:" + device_id[-1].encode().hex() * 32,
        client_public="nodekey:" + format(ord(device_id[-1]) + 100, "02x") * 32,
        address=f"tc{device_id.replace('-', '')}PrivateAddressForTests",
        key_generation=generation,
        updated_at="2026-11-26T00:00:00+00:00",
        encryption_key_id=None if keys is None else keys["keyId"],
        encryption_public_key=None if keys is None else keys["publicKey"],
    )


def write_user_provider(home: Path, entry_id: str, values: dict[str, str]) -> Path:
    providers = home / "secrets" / "providers"
    providers.mkdir(parents=True, exist_ok=True, mode=0o700)
    (home / "secrets").chmod(0o700)
    providers.chmod(0o700)
    path = providers / f"{entry_id}.json"
    write_private_file_atomic(path, (json.dumps(values, sort_keys=True) + "\n").encode("utf-8"))
    return path


def device_uid(owner: str, device_id: str) -> str:
    """A reproducible UUID4-shaped device uid for ``(owner, device_id)``."""

    value = bytearray(uuid.uuid5(uuid.NAMESPACE_DNS, f"{owner}/{device_id}").bytes)
    value[6] = (value[6] & 0x0F) | 0x40
    value[8] = (value[8] & 0x3F) | 0x80
    return bytes(value).hex()


def account_home(home: Path, *, owner: str, device_id: str) -> Path:
    """A home with a complete committed account: settings, device record, secrets dir."""

    home.mkdir(mode=0o700)
    atomic_json_write(
        home / "settings.json",
        {
            "owner": owner,
            "identityMode": "casdoor",
            "identityIssuer": ISSUER,
            "userId": f"subject-{owner}",
        },
    )
    write_device_record(
        home,
        DeviceRecord(
            device_id=device_id,
            owner=owner,
            server_public="nodekey:" + "a" * 64,
            client_public="nodekey:" + "b" * 64,
            device_uid=device_uid(owner, device_id),
            name=device_id,
        ),
    )
    (home / "secrets").mkdir(mode=0o700)
    (home / "secrets" / "providers").mkdir(mode=0o700)
    return home


def build_directory(tmp_path: Path) -> dict[str, Any]:
    """Create the org, the device rows with real published keys, and Alice's Key entry."""

    runtime = OrgFsRuntime(tmp_path / "orgfs", node_id="device-a", author="user:alice")
    store = OrgFsDirectoryStore(runtime.facade)
    space = runtime.facade.create_space(ORG)
    runtime.facade.invite(space.space_id, "user:bob", "rw")
    runtime.facade.invite(space.space_id, "user:carol", "rw")
    store.ensure_org_directory_roots(ORG)
    rows: dict[str, DirectoryDevice] = {}
    for device_id, owner in (("device-a", "alice"), ("device-b", "bob"), ("device-c", "carol")):
        home = account_home(tmp_path / f"{owner}-home", owner=owner, device_id=device_id)
        keys = ensure_encryption_key(home, tmp_path / f"{owner}-state")
        assert keys is not None
        rows[device_id] = device_row(device_id, owner, keys=keys)
    store.put_device(ORG, rows["device-a"])
    with as_author(runtime, "bob"):
        store.put_device(ORG, rows["device-b"])
    with as_author(runtime, "carol"):
        store.put_device(ORG, rows["device-c"])
    write_user_provider(tmp_path / "alice-home", "gateway", {"API_KEY": SECRET})
    return {
        "runtime": runtime,
        "store": store,
        "alice_home": tmp_path / "alice-home",
        "bob_home": tmp_path / "bob-home",
        "bob_state": tmp_path / "bob-state",
        "carol_home": tmp_path / "carol-home",
        "carol_state": tmp_path / "carol-state",
    }


def grantor(
    directory: dict[str, Any], clock: Clock, *, ports: tuple[int, ...] = (PORT,)
) -> ServiceGrantService:
    """Alice's grantor side. A grant supersedes her earlier grant of the same port
    to the same recipient (that is service.grant's rule), so tests that need several grants
    at once pass distinct ports here."""

    return ServiceGrantService(
        store=directory["store"],
        home=directory["alice_home"],
        owner="alice",
        local_device_id="device-a",
        exposed_ports=lambda: frozenset(ports),
        now_ms=clock,
    )


def service_manager(world: dict[str, Any], source: Any) -> ServiceManager:
    """A manager over the recipient home, which has no protected access record."""

    return ServiceManager(
        orgfs=world["runtime"].facade,
        state=world["state"],
        persistence_late_result=None,
        controller_factory=lambda: world["controller"],
        access_path=world["access"],
        scheduler=ManualScheduler(),
        actor_host=world["actor_host"],
        derived_source=source,
    )


def hand_placed(world: dict[str, Any], *, name: str = "api") -> Any:
    """Give the recipient home a hand-placed catalog: a services space plus the protected record.

    A catalog lives in a space of its own whose root holds only catalog
    documents (that is service-connect's rule), never beside an org directory.
    """

    from tests.daemon.service_connect.support import catalog_document, write_access

    runtime = world["runtime"]
    space = runtime.facade.create_space(f"services-{name}")
    runtime.facade.write_text(space.space_id, f"{name}.md", catalog_document(name=name))
    write_access(world["access"], space_id=space.space_id, owner=space.owner)
    return space


def providers_dir(home: Path) -> Path:
    return home / "secrets" / "providers"


def provider_ids(home: Path) -> list[str]:
    """Ids of the regular entry files; a symlink at an entry path is not an entry."""

    return sorted(
        path.stem
        for path in providers_dir(home).glob("*.json")
        if path.is_file() and not path.is_symlink()
    )


def drop_device_record(home: Path) -> None:
    """Simulate a device that is not logged in: no committed account at all."""

    (home / "settings.json").unlink()
    (home / DEVICE_RECORD_RELPATH).unlink()


def leave_org(home: Path, org: str = ORG) -> None:
    add_left_org(home, org)


__all__ = [
    "ISSUER",
    "KEY_FIELD",
    "ORG",
    "OTHER_ORG",
    "OTHER_PORT",
    "PORT",
    "SECRET",
    "Clock",
    "Recorder",
    "account_home",
    "as_author",
    "build_directory",
    "device_row",
    "device_uid",
    "drop_device_record",
    "grantor",
    "hand_placed",
    "leave_org",
    "provider_ids",
    "providers_dir",
    "service_manager",
    "write_user_provider",
]
