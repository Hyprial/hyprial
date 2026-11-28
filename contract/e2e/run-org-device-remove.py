"""Real v7 sidecar + OrgFS device removal; SQLite groups are a protocol fixture.

Only disposable homes and a loopback labrelay are used. This does not prove
deployed Casdoor behavior. Run with --binary and --relay; no production state.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from hyprial.daemon import ensure_device_key
from hyprial.daemon.impl.forwarding.controller import ForwardingSidecarController
from hyprial.daemon.impl.forwarding.policy import FORWARD_PROTOCOL_VERSION
from hyprial.daemon.impl.org.network.service import OrgNetworkError, OrgNetworkService
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.identity import address_path, device_key_path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.daemon.org.network.status.test_device_removal import SQLiteGroups  # noqa: E402

ORG = 'group-device-smoke'
OWNER = 'owner@example'
USER = 'user:bob@example'
GROUP = 'hyprial/' + ORG


def run(binary, relay_binary):
    with tempfile.TemporaryDirectory(prefix='hr-device-', dir='/private/tmp') as directory:
        root = Path(directory)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('HYPRIAL_', 'HARNESS_'))
               and key.lower() not in ('http_proxy', 'https_proxy', 'all_proxy')}
        env.update(HOME=str(root / 'sandbox'), NO_PROXY='localhost,127.0.0.1,::1')
        Path(env['HOME']).mkdir(mode=0o700)
        relay = subprocess.Popen([str(relay_binary)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        controllers = []
        runtime = None
        try:
            derp = json.loads(relay.stdout.readline())['derp']
            env.update(HYPRIAL_TAILCAT_BINARY=str(binary), HYPRIAL_TAILCAT_DERP=json.dumps(derp))
            homes, ups = [], []
            for device in ('a1', 'b1', 'b2'):
                owner = OWNER if device == 'a1' else 'bob@example'
                home = root / device
                home.mkdir(mode=0o700)
                key_file = device_key_path(home)
                key_file.parent.mkdir(parents=True, mode=0o700)
                ensure_device_key(home, owner=owner, device_id=device,
                                  name='device-smoke-' + device, environ=env)
                private = home / 'tailnet'
                private.mkdir(mode=0o700)
                up = {'v': FORWARD_PROTOCOL_VERSION, 'op': 'up', 'keyFile': str(key_file),
                      'membershipStore': str(private / 'membership.json'), 'owner': owner,
                      'deviceId': device, 'controlPort': 7448, 'addressFile': str(address_path(home)),
                      'inboundTarget': 'unix:' + str(private / 'absent.sock'),
                      'peerPort': 7447, 'slots': [], 'derp': derp}
                controllers.append(ForwardingSidecarController([str(binary), 'forward'], up, environ=env))
                homes.append(home)
                ups.append(up)
            groups_path = root / 'groups.sqlite'
            groups = SQLiteGroups(groups_path)
            groups.add_member(GROUP, 'hyprial/' + OWNER)
            runtime = OrgFsRuntime(root / 'orgfs', node_id='a1', author='user:' + OWNER)

            def service():
                return OrgNetworkService(fs=runtime.facade, home=homes[0], node_id='a1', owner=OWNER,
                                         casdoor=SQLiteGroups(groups_path),
                                         membership_request=controllers[0].membership_request,
                                         read_own_address=lambda: address_path(homes[0]).read_text().strip())

            created = service().create(ORG)
            space = created['spaceId']
            service().invite(ORG, USER)
            for index, device in enumerate(('b1', 'b2'), start=1):
                invite = controllers[0].membership_request('invite-create', {
                    'operationId': 'invite-' + device, 'tailnetId': space, 'owner': 'bob@example',
                    'expiresAtMs': int(time.time() * 1000) + 600000})['invite']
                controllers[index].membership_request('invite-redeem', {
                    'operationId': 'redeem-' + device, 'invite': invite})

            def read():
                table = controllers[0].membership_request('membership-status', {'tailnetId': space})['tailnets'][0]
                directory = runtime.facade.members(space)
                acl = next(row for row in runtime.facade.spaces() if row.name == ORG + '-acl')
                return {'devices': {row['deviceId']: row['state'] for row in table['members']},
                        'casdoorProtocolGroup': SQLiteGroups(groups_path).member_state(GROUP, 'hyprial/bob@example'),
                        'directoryMember': USER in {row.user for row in directory},
                        'aclMember': USER in {row.user for row in runtime.facade.members(acl.space_id)}}

            before = read()
            preview = service().remove(ORG, USER, device_ids=['b1'])
            assert preview['deviceIds'] == ['b1'], preview['deviceIds']
            done = service().remove(ORG, USER, confirmed=True,
                                    operation_id=preview['operationId'], device_ids=preview['deviceIds'])
            assert done['phase'] == 'complete'
            assert Path(done['result']['backupPath']).is_file()
            after = read()
            assert after == {'devices': {'a1': 'active', 'b1': 'tombstone', 'b2': 'active'},
                             'casdoorProtocolGroup': 'member', 'directoryMember': True, 'aclMember': True}
            runtime.close()
            runtime = None
            for controller in controllers:
                controller.close()
            controllers.clear()
            for up in ups:
                controllers.append(ForwardingSidecarController([str(binary), 'forward'], up, environ=env))
            runtime = OrgFsRuntime(root / 'orgfs', node_id='a1', author='user:' + OWNER)
            restarted = read()
            assert restarted == after
            errors = {}
            for name, devices, expected in (
                ('notFound', ['absent'], 'DEVICE_NOT_FOUND'),
                ('lastDevice', ['b2'], 'LAST_DEVICE_REFUSED'),
            ):
                try:
                    service().remove(ORG, USER, device_ids=devices)
                except OrgNetworkError as error:
                    assert error.code == expected
                    errors[name] = {'code': error.code, 'message': str(error)}
                else:
                    raise AssertionError('selection was silently accepted: ' + name)
            assert read() == after
            full = service().remove(ORG, USER)
            full_done = service().remove(ORG, USER, confirmed=True,
                                         operation_id=full['operationId'], device_ids=full['deviceIds'])
            assert full_done['deviceIds'] == ['b2']
            unfiltered = read()
            assert unfiltered == {'devices': {'a1': 'active', 'b1': 'tombstone', 'b2': 'tombstone'},
                                  'casdoorProtocolGroup': 'not-member', 'directoryMember': False, 'aclMember': False}
            return {'result': 'PASS', 'targets': preview['deviceIds'], 'before': before, 'after': after,
                    'afterRestart': restarted, 'unfilteredAfter': unfiltered, 'errors': errors,
                    'realOrgFS': True, 'realSidecar': True, 'journalRestart': True,
                    'casdoor': 'SQLite protocol fixture only; deployed Casdoor NOT_COVERED'}
        finally:
            if runtime is not None:
                runtime.close()
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
