"""Explicit real-sidecar offline re-pin smoke; never auto-selected by pytest.

uv run python tests/daemon/forwarding/membership/repin_smoke.py --binary <v6> --relay <labrelay>
Only disposable /tmp homes and a loopback labrelay are used. No production files.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from hyprial.daemon import DaemonStateOwnershipFence, ensure_device_key, verify_tailcat_sidecar
from hyprial.daemon.impl.forwarding.controller import ForwardingSidecarController
from hyprial.daemon.impl.forwarding.membership.maintenance import TailnetMaintenance
from hyprial.daemon.impl.forwarding.membership.removal import RemovalJournal
from hyprial.daemon.impl.forwarding.policy import ForwardingSidecarError
from hyprial.identity import address_path, device_key_path


def run(binary: Path, relay_binary: Path):
    with tempfile.TemporaryDirectory(prefix='hr-rp-', dir='/tmp') as directory:
        root = Path(directory)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('HYPRIAL_', 'HARNESS_')) and key.lower() not in
               ('http_proxy', 'https_proxy', 'all_proxy')}
        env.update(HOME=str(root / 'sandbox'), NO_PROXY='localhost,127.0.0.1,::1')
        Path(env['HOME']).mkdir(mode=0o700)
        relay = subprocess.Popen([str(relay_binary)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        controllers = []
        try:
            derp = json.loads(relay.stdout.readline())['derp']
            env.update(HYPRIAL_TAILCAT_BINARY=str(binary), HYPRIAL_TAILCAT_DERP=json.dumps(derp))
            assert verify_tailcat_sidecar(root, env)['v'] == 6
            rows = []
            for device in ('old', 'new', 'victim'):
                owner = 'bob' if device == 'victim' else 'alice'
                home = root / device
                home.mkdir(mode=0o700)
                key_path = device_key_path(home)
                key_path.parent.mkdir(parents=True, mode=0o700)
                record = ensure_device_key(home, owner=owner, device_id=device,
                                           name='maintenance-' + device, environ=env)
                keys = {'serverPublic': record.server_public, 'clientPublic': record.client_public}
                private = home / 'tailnet'
                private.mkdir(mode=0o700)
                up = {'v': 6, 'op': 'up', 'keyFile': str(key_path),
                    'membershipStore': str(private / 'membership.json'), 'owner': owner,
                    'deviceId': device, 'controlPort': 7448, 'addressFile': str(address_path(home)),
                    'inboundTarget': 'unix:' + str(private / 'absent.sock'),
                    'peerPort': 7447, 'slots': [], 'derp': derp}
                controller = ForwardingSidecarController([str(binary), 'forward'], up, environ=env)
                controllers.append(controller)
                rows.append({'owner': owner, 'deviceId': device, 'masterPublic': keys['clientPublic'],
                    'serverPublic': keys['serverPublic'], 'address': address_path(home).read_text().strip(),
                    'clientPublicByServer': {}, 'state': 'active', 'revision': 1})
            authority = {key: rows[0][key] for key in ('owner', 'deviceId', 'masterPublic', 'serverPublic', 'address')}
            authority['controlPort'] = 7448
            controllers[0].membership_request('membership-init', {
                'operationId': 'initial', 'tailnetId': 'space', 'authority': authority,
                'members': rows, 'migration': {'source': 'directory-0.5.5',
                                              'sourceDigest': 'a' * 64, 'completed': True}})
            journal = RemovalJournal(root / 'old', controllers[0].membership_request,
                                     local_device='old', operator='alice')
            upper_calls = []
            def upper():
                upper_calls.append('committed')  # Isolated orchestration, not real OrgFS acceptance.
            first = journal.remove('space', 'bob', confirmed=False, upper=upper)
            controllers[0].membership_request('member-announce', {'operationId': 'announce-initial',
                'tailnetId': 'space', 'address': rows[0]['address'], 'clientPublicByServer': {}})
            try:
                journal.remove('space', 'bob', confirmed=True, upper=upper,
                               operation_id=first['operationId'], device_ids=first['deviceIds'])
                raise AssertionError('expected an explicit revision conflict')
            except ForwardingSidecarError as error:
                assert error.code == 'REVISION_CONFLICT'
            preview = journal.remove('space', 'bob', confirmed=False, upper=upper)
            assert preview['phase'] == 'revision-confirmation'
            removed = journal.remove('space', 'bob', confirmed=True, upper=upper,
                                     operation_id=preview['operationId'], device_ids=preview['deviceIds'])
            assert removed['phase'] == 'complete' and removed['operationId'] == first['operationId']
            assert upper_calls == ['committed'] and removed['revisionAudit']
            assert Path(removed['result']['backupPath']).is_file()
            for controller in controllers:
                controller.close()
            controllers.clear()
            home = root / 'old'
            (home / 'settings.json').write_text(json.dumps({'owner': 'alice'}))
            os.chmod(home / 'settings.json', 0o600)
            cli_env = env | {'HYPRIAL_HOME': str(home), 'HARNESS_STATE_DIR': str(home / 'state')}
            command = [sys.executable, '-m', 'hyprial.cli', 'org', 'repin', 'space',
                       '--authority', 'new', '--json']
            def invoke(*flags):
                completed = subprocess.run(command + list(flags), env=cli_env, capture_output=True, timeout=55)
                output = (completed.stdout + completed.stderr).decode()
                assert all(row['address'] not in output for row in rows)
                return completed.returncode, json.loads(completed.stdout)
            with DaemonStateOwnershipFence.acquire(home / 'state'):
                exit_code, payload = invoke('--yes')
                assert exit_code == 1 and payload['code'] == 'NETWORK_NOT_QUIESCENT', payload.get('code')
            exit_code, payload = invoke()
            assert exit_code == 1 and payload['code'] == 'CONFIRMATION_REQUIRED', payload.get('code')
            exit_code, payload = invoke('--yes')
            assert exit_code == 0 and payload['phase'] == 'complete', payload.get('code')
            backup = Path(payload['backupPath'])
            assert backup.is_file() and backup.stat().st_mode & 0o777 == 0o600
            assert payload['restartRequired'] is True
            assert json.loads((home / 'settings.json').read_text())['forwarding']['tailnet']['authorities']['space']['deviceId'] == 'new'
            with TailnetMaintenance(home, home / 'state', environ=env) as maintenance:
                table = maintenance.controller.membership_request('membership-status', {'tailnetId': 'space'})['tailnets'][0]
                assert table['authorityDeviceId'] == 'new'
                assert {row['deviceId'] for row in table['members'] if row['state'] == 'active'} == {'old', 'new'}
                assert any(row['deviceId'] == 'victim' and row['state'] == 'tombstone' for row in table['members'])
            return {'result': 'PASS', 'liveDaemonRefused': True, 'confirmationRequired': True,
                    'backup0600': True, 'settingsPinUpdated': True, 'membersRetained': True,
                    'realSidecar': True, 'realRemovalConflictRecovery': True,
                    'realCrossMachineTransfer': 'NOT_TESTED', 'realOrgFSRemoval': 'NOT_TESTED'}
        finally:
            for controller in controllers:
                controller.close()
            relay.terminate()
            relay.wait(timeout=10)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--relay', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.binary.resolve(), args.relay.resolve()), sort_keys=True))
