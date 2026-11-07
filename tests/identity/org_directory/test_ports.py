"""The directory ports: protocols and the LAX allow-all policy (§4.1)."""

from __future__ import annotations

from hyprial.identity.impl.org_directory.models import DirectoryDevice
from hyprial.identity.impl.org_directory.ports import (
    AllowAllPolicy,
    DirectoryStore,
    OrgPolicy,
)


def test_allow_all_policy_allows_everything() -> None:
    policy = AllowAllPolicy()
    # LAX(tailnet-cutover): a real policy distinguishes these; the stand-in
    # must not, or half-configured homes would deadlock during bootstrap.
    assert (
        policy.allows(
            org="group-acme",
            subject="node-7f3a",
            action="directory:write",
            resource="directory/devices/owner@example/node-7f3a.json",
        )
        is True
    )
    assert (
        policy.allows(
            org="other", subject="whoever", action="anything", resource="x"
        )
        is True
    )


def test_allow_all_policy_satisfies_the_org_policy_port() -> None:
    assert isinstance(AllowAllPolicy(), OrgPolicy)


class _InMemoryStore:
    """The smallest structural twin of the daemon's OrgFS implementation."""

    def __init__(self) -> None:
        self._devices: dict[str, list[DirectoryDevice]] = {}

    def list_devices(self, org: str) -> list[DirectoryDevice]:
        return list(self._devices.get(org, ()))

    def put_device(self, org: str, device: DirectoryDevice) -> None:
        rows = self._devices.setdefault(org, [])
        rows[:] = [row for row in rows if row.device_id != device.device_id]
        rows.append(device)

    def orgs(self) -> list[str]:
        return sorted(self._devices)


def test_a_store_shaped_object_satisfies_the_directory_store_port() -> None:
    assert isinstance(_InMemoryStore(), DirectoryStore)
