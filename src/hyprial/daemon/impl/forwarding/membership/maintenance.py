"""Offline re-pin: hold daemon ownership until sidecar exit and durable pin commit."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path

from hyprial.identity import read_device_record
from hyprial.kernel import FORWARDING_UP_ENV
from ...configuration.ownership import DaemonOwnershipBusy, DaemonStateOwnershipFence
from ...forwarding_config import FORWARDING_TARGET_ENV, daemon_forwarding_environment
from ..controller import ForwardingSidecarController
from ..policy import ForwardingSidecarError
from .settings import _settings_record, durable_json_write


class TailnetMaintenance:
    def __init__(self, home: Path, state_dir: Path, *, environ=None):
        self.home, self.state_dir = Path(home), Path(state_dir)
        self.environ = dict(os.environ if environ is None else environ)
        self.controller = None
        self._fence = None
        self._temporary = None
        self._plan = None

    def __enter__(self):
        try:
            self._fence = DaemonStateOwnershipFence.acquire(self.state_dir, timeout=0.0)
        except DaemonOwnershipBusy as error:
            raise ForwardingSidecarError('Stop the daemon before re-pinning.', code='NETWORK_NOT_QUIESCENT') from error
        try:
            self.controller = self._start_controller()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_error):
        try:
            if self.controller is not None:
                self.controller.close()
        finally:
            self.controller = None
            try:
                if self._temporary is not None:
                    self._temporary.cleanup()
                    self._temporary = None
            finally:
                if self._fence is not None:
                    self._fence.close()
                    self._fence = None

    def _start_controller(self):
        if os.name != 'posix':
            raise ForwardingSidecarError('Managed private Unix maintenance is unsupported on this platform.', code='FORWARDING_TARGET_INVALID')
        record = read_device_record(self.home)
        if record is None:
            raise ForwardingSidecarError('Device identity is required.', code='MEMBERSHIP_NOT_INITIALIZED')
        # No listener exists at this unique backend path. The maintenance process
        # owns no Zenoh session, mapped slots, services or exposures.
        self._temporary = tempfile.TemporaryDirectory(prefix='hr-repin-', dir='/tmp')
        root = Path(self._temporary.name)
        environment = self.environ | {FORWARDING_TARGET_ENV: 'unix:' + str(root / 'absent.sock')}
        launch = daemon_forwarding_environment(self.home, environment, node_id=record.device_id)
        up = json.loads(launch[FORWARDING_UP_ENV])
        up['slots'] = []
        up['addressFile'] = str(root / 'address')
        launch[FORWARDING_UP_ENV] = json.dumps(up)
        return ForwardingSidecarController.from_environment(environment | launch)

    def prepare(self, tailnet_id: str, authority_device: str, *, control_port: int = 7448) -> dict:
        if type(control_port) is not int or not 1 <= control_port <= 65535:
            raise ForwardingSidecarError('Invalid authority control port.', code='BAD_REQUEST')
        if self.controller is None:
            raise RuntimeError('Maintenance ownership is not held.')
        root = self.home / 'tailnet' / 'maintenance'
        self._path = root / (hashlib.sha256(tailnet_id.encode()).hexdigest() + '.json')
        try:
            previous = json.loads(self._path.read_text())
        except FileNotFoundError:
            previous = None
        if previous is not None and previous['phase'] == 'revision-conflict':
            # Go receipt replay precedes revision checks. This refusal proves
            # no pin was committed for the operation; retain it as history and
            # require a fresh preview/confirmation with a fresh operation ID.
            archive = self._path.with_name(previous['params']['operationId'] + '.conflict.json')
            durable_json_write(archive, previous)
            previous = None
        if previous is not None and previous['phase'] != 'complete':
            if (previous['params']['authority']['deviceId'] != authority_device
                    or previous['params']['authority']['controlPort'] != control_port):
                raise ForwardingSidecarError('Resume the pending re-pin before choosing another authority.', code='OPERATION_CONFLICT')
            self._plan = previous
            return self._public_plan()
        status = self.controller.membership_request('membership-status', {'tailnetId': tailnet_id})
        table = next((row for row in status['tailnets'] if row['tailnetId'] == tailnet_id), None)
        if table is None:
            raise ForwardingSidecarError('Tailnet not initialized.', code='MEMBERSHIP_NOT_INITIALIZED')
        members = [row for row in table['members'] if row['state'] == 'active']
        selected = next((row for row in members if row['deviceId'] == authority_device), None)
        if selected is None:
            raise ForwardingSidecarError('The new authority must be an active member.', code='MEMBER_UNKNOWN')
        authority = {key: selected[key] for key in ('owner', 'deviceId', 'masterPublic', 'serverPublic', 'address')}
        authority['controlPort'] = control_port
        if (previous is not None and table['authorityDeviceId'] == authority_device
                and previous['params']['authority'] == authority):
            self._plan = previous
            return self._public_plan()
        self._plan = {'phase': 'pending', 'previousAuthorityDeviceId': table['authorityDeviceId'],
            'deviceIds': sorted(row['deviceId'] for row in members),
            'params': {'operationId': uuid.uuid4().hex, 'tailnetId': tailnet_id,
                       'authority': authority, 'expectedRevision': table['revision'], 'confirmed': True}}
        return self._public_plan()

    def _public_plan(self):
        params = self._plan['params']
        result = self._plan.get('result', {})
        return {'tailnetId': params['tailnetId'], 'operationId': params['operationId'],
                'authorityDeviceId': params['authority']['deviceId'],
                'previousAuthorityDeviceId': self._plan['previousAuthorityDeviceId'],
                'deviceIds': list(self._plan['deviceIds']), 'phase': self._plan['phase'],
                'expectedRevision': params['expectedRevision'], 'restartRequired': True,
                'backupPath': result.get('backupPath')}

    def commit(self, *, confirmed: bool) -> dict:
        if self.controller is None or self._plan is None:
            raise RuntimeError('Prepare a re-pin while holding maintenance ownership.')
        if not confirmed:
            raise ForwardingSidecarError('Explicit re-pin confirmation required.', code='CONFIRMATION_REQUIRED')
        if self._plan['phase'] == 'complete':
            return self._public_plan()
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        durable_json_write(self._path, self._plan)
        try:
            result = self.controller.membership_request('authority-repin', self._plan['params'])
        except ForwardingSidecarError as error:
            if error.code == 'REVISION_CONFLICT':
                self._plan['phase'] = 'revision-conflict'
                durable_json_write(self._path, self._plan)
            raise
        backup = result.get('backupPath')
        if result.get('restartRequired') is not True or not isinstance(backup, str) or not Path(backup).is_file():
            raise ForwardingSidecarError('Re-pin backup is unavailable; keep networking stopped and resume maintenance.', code='MEMBERSHIP_BACKUP_FAILED')
        # Only this explicitly confirmed, fenced maintenance path may replace a
        # pin. Normal startup pin_authority continues to reject mismatches.
        settings = _settings_record(self.home)
        forwarding = settings.setdefault('forwarding', {})
        section = forwarding.setdefault('tailnet', {})
        section.setdefault('authorities', {})[self._plan['params']['tailnetId']] = self._plan['params']['authority']
        durable_json_write(self.home / 'settings.json', settings)
        self._plan.update(phase='complete', result=result)
        durable_json_write(self._path, self._plan)
        return self._public_plan()
