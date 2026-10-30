"""Operator tools for tailnets the one-time import has not migrated yet.

Both run without a daemon: the local OrgFS store is opened read-only in the
sense that nothing is written to it, directory rows are read exactly the way
``migration.offline_rows`` reads them, and the only write is the settings pin
through ``settings.pin_authority``.  Once the sidecar table exists the tool
refuses and points at ``hyprial org repin``.
"""
from __future__ import annotations

import json
from pathlib import Path

from hyprial.identity import org_from_space_name, read_device_record

from ....orgfs.runtime import OrgFsRuntime
from ...policy import ForwardingSidecarError
from ..settings import pin_authority, tailnet_settings
from .authority import AUTHORITY_KEYS, DEFAULT_CONTROL_PORT
from .spaces import SKIP_NO_ROWS, org_state, space_skip_reason
from .store import store_tailnets


def read_receipt(home: Path, tailnet_id: str) -> dict | None:
    from ..migration import receipt_path
    try:
        return json.loads(receipt_path(home, tailnet_id).read_text())
    except FileNotFoundError:
        return None


class OfflineMembership:
    def __init__(self, home: Path, state_dir: Path):
        self.home, self.state_dir = Path(home), Path(state_dir)
        self.runtime = None

    def __enter__(self):
        record = read_device_record(self.home)
        if record is None:
            raise ForwardingSidecarError('Device identity is required.', code='MEMBERSHIP_NOT_INITIALIZED')
        self.record = record
        self.runtime = OrgFsRuntime(self.state_dir, node_id=record.device_id,
                                    author="user:" + record.owner.removeprefix("user:"))
        return self

    def __exit__(self, *_error):
        if self.runtime is not None:
            self.runtime.close()
            self.runtime = None

    @property
    def fs(self):
        if self.runtime is None:
            raise RuntimeError('Open the offline membership tool with a `with` block.')
        return self.runtime.facade

    def org_spaces(self) -> list:
        return [space for space in self.fs.spaces() if org_from_space_name(space.name) is not None]

    def _space(self, space_id: str):
        for space in self.org_spaces():
            if space.space_id == space_id:
                return space
        raise ForwardingSidecarError('Unknown org space; pass the spaceId, not the org name.', code='SPACE_UNKNOWN')

    def _rows(self, space) -> tuple[list[dict], dict]:
        from ..migration import offline_rows
        return offline_rows(self.fs, space)

    def pin_authority(self, space_id: str, device_id: str, *, confirmed: bool,
                      control_port: int = DEFAULT_CONTROL_PORT) -> dict:
        if type(control_port) is not int or not 1 <= control_port <= 65535:
            raise ForwardingSidecarError('Invalid authority control port.', code='BAD_REQUEST')
        space = self._space(space_id)
        if space_id in store_tailnets(self.home):
            raise ForwardingSidecarError(
                'Membership table already exists; use `hyprial org repin` for an initialized tailnet.',
                code='MEMBERSHIP_ALREADY_INITIALIZED')
        rows, _diagnostics = self._rows(space)
        owner = space.owner.removeprefix('user:')
        candidates = sorted(row['deviceId'] for row in rows if row['owner'] == owner and row.get('address'))
        selected = next((row for row in rows if row['owner'] == owner and row['deviceId'] == device_id), None)
        if selected is None or not selected.get('address'):
            raise ForwardingSidecarError(
                f'{device_id!r} is not an owner device row with an address in this space; '
                f"candidates: {', '.join(candidates) or '(none)'}.", code='MEMBER_UNKNOWN')
        authority = {key: selected[key] for key in AUTHORITY_KEYS} | {'controlPort': control_port}
        existing = tailnet_settings(self.home)['authorities'].get(space_id)
        result = {'spaceId': space_id, 'org': org_from_space_name(space.name), 'owner': owner,
                  'deviceId': device_id, 'controlPort': control_port, 'candidates': candidates,
                  'pinned': False, 'pinnedDeviceId': existing.get('deviceId') if isinstance(existing, dict) else None,
                  'restartRequired': True}
        if confirmed:
            pin_authority(self.home, space_id, authority)  # refuses a different existing pin
            result.update(pinned=True, pinnedDeviceId=device_id)
        return result

    def migration_status(self) -> list[dict]:
        settings = tailnet_settings(self.home)
        initialized = store_tailnets(self.home)
        report = []
        for space in self.org_spaces():
            tailnet_id = space.space_id
            receipt = read_receipt(self.home, tailnet_id)
            pin = settings['authorities'].get(tailnet_id)
            skip = space_skip_reason(self.fs, space, self.home, local_owner=self.record.owner)
            if receipt is not None:
                diagnostics = receipt.get('diagnostics', {})
            else:
                _rows, diagnostics = self._rows(space)
                if skip is None and not diagnostics.get('accepted'):
                    skip = SKIP_NO_ROWS
            report.append({
                'spaceId': tailnet_id, 'org': org_from_space_name(space.name),
                'state': org_state(self.fs, tailnet_id), 'skipReason': skip,
                'pinned': pin is not None,
                'pinnedDeviceId': pin.get('deviceId') if isinstance(pin, dict) else None,
                'sidecarInitialized': tailnet_id in initialized,
                'receipt': None if receipt is None else {
                    'state': receipt.get('state'),
                    'source': receipt.get('params', {}).get('migration', {}).get('source'),
                    'authorityDeviceId': receipt.get('params', {}).get('authority', {}).get('deviceId'),
                    'skipReason': receipt.get('skipReason'),
                    'superseded': 'supersededParams' in receipt.get('diagnostics', {})},
                'rows': {'accepted': list(diagnostics.get('accepted', [])),
                         'rejected': list(diagnostics.get('rejected', [])),
                         'dropped': list(diagnostics.get('droppedDeviceIds', []))},
                'lastError': receipt.get('lastError') if receipt is not None else None,
            })
        return report
