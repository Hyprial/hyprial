"""Real local OrgFS copies of 0.5.5-shaped org directories for migration tests.

Only the sidecar is a double (``FakeMembershipSidecar``); directory rows,
org markers, receipts and settings are real files under a disposable home.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.org.network.create import write_default_acl_policy
from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.identity import address_path
from hyprial.identity.impl.device import DeviceRecord, write_device_record
from tests.support.tailnet_membership import FakeMembershipSidecar, key

OWNER = 'alice'
LOCAL_DEVICE = 'alice-mbp'
ADDRESS = 'address-of-' + LOCAL_DEVICE


def legacy_row(owner: str, device: str, *, updated_at: str = '2026-10-20T00:00:00Z',
               client: str | None = None, server: str | None = None, address: str | None = None) -> dict:
    return {'owner': owner, 'deviceId': device,
            'clientPublic': client or key('client-' + device),
            'serverPublic': server or key('server-' + device),
            'address': address or 'address-of-' + device,
            'keyGeneration': 1, 'updatedAt': updated_at}


class LegacyHome:
    """One node's home + state dir with a real OrgFS store and a fake sidecar."""

    def __init__(self, root: Path, *, owner: str = OWNER, device: str = LOCAL_DEVICE):
        self.home = root / 'home'
        self.state_dir = root / 'state'
        self.home.mkdir(parents=True)
        self.state_dir.mkdir(parents=True)
        self.owner, self.device = owner, device
        write_device_record(self.home, DeviceRecord(
            device_id=device, owner=owner,
            client_public=key('client-' + device), server_public=key('server-' + device)))
        address_path(self.home).write_text('address-of-' + device)
        self.sidecar = FakeMembershipSidecar(owner=owner, device_id=device)
        self.sidecar.local['address'] = 'address-of-' + device
        self.runtime = OrgFsRuntime(self.state_dir, node_id=device, author='user:' + owner)
        self.events: list[tuple[str, str, dict]] = []
        self.supervisor_starts = 0

    @property
    def fs(self):
        return self.runtime.facade

    def close(self) -> None:
        self.runtime.close()

    def reopen(self) -> None:
        self.runtime.close()
        self.runtime = OrgFsRuntime(self.state_dir, node_id=self.device, author='user:' + self.owner)

    def seed_org(self, org: str, rows: list[dict], *, state: str | None = 'active',
                 members: tuple[str, ...] = (), space_owner: str | None = None) -> str:
        fs = self.fs
        fs.author = 'user:' + (space_owner or self.owner)
        space = fs.create_space(org)
        store = OrgFsDirectoryStore(fs)
        store.ensure_org_directory_roots(org)
        acl = fs.create_space(org + '-acl')
        write_default_acl_policy(fs, acl.space_id)
        fs.update_space_meta(space.space_id, {'aclSpaceId': acl.space_id})
        if state is not None:
            store.put_org_meta(org, {'state': state})
        for user in members:
            fs.invite(space.space_id, 'user:' + user, 'rw')
        made_dirs: set[str] = set()
        for row in rows:
            owner = row['owner']
            fs.author = 'user:' + owner
            if owner not in made_dirs:
                fs.mkdir(space.space_id, 'directory/devices/' + owner)
                made_dirs.add(owner)
            fs.write_text(space.space_id, f"directory/devices/{owner}/{row['deviceId']}.json", json.dumps(row))
        fs.author = 'user:' + self.owner
        return space.space_id

    def settings(self) -> dict:
        try:
            return json.loads((self.home / 'settings.json').read_text())
        except FileNotFoundError:
            return {}

    def write_settings(self, section: dict) -> None:
        (self.home / 'settings.json').write_text(json.dumps({'forwarding': {'tailnet': section}}))

    def app(self) -> Any:
        return _App(self)


class _App:
    def __init__(self, node: LegacyHome):
        self._node = node
        self.hyprial_home = node.home
        self.state_dir = node.state_dir
        self.node_id = node.device
        self.owner = 'user:' + node.owner
        self.zenoh_listen = ()
        self._forwarding_supervisor = _Supervisor(node.sidecar)

    def _start_forwarding_supervisor(self) -> None:
        self._node.supervisor_starts += 1

    def _require_orgfs_runtime(self):
        return self._node.runtime

    def _log(self, level: str, domain: str, event: str, **fields: Any) -> None:
        self._node.events.append((level, event, fields))


class _Supervisor:
    def __init__(self, sidecar: FakeMembershipSidecar):
        self._sidecar = sidecar
        self.last_membership_observation = None

    def membership_request(self, op: str, params: dict) -> dict:
        return self._sidecar.membership_request(op, params)
