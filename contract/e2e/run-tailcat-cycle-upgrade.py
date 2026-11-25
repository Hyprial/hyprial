#!/usr/bin/env python3
"""Tailcat v6 integration: legacy directory migration, staged join and lease expiry.

Uses synthetic 0.5.5-shaped local directory copies (no derived map), not a
production backup. Real daemons, sidecars and local labrelay carry all traffic.
This is state-migration acceptance, not an installer or mixed-version gate.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import runpy
import shutil
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _daemon_runner import IsolatedDaemon, hyprial_executable, scenario_tempdir, short_temp_root  # noqa: E402

INVITE = runpy.run_path(str(Path(__file__).with_name('run-tailcat-org-invite-join.py')))
ORG = 'group-cycle-upgrade'


def seed_legacy(daemons, addresses):
    from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
    from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
    from hyprial.daemon.impl.org.network.create import write_default_acl_policy
    from hyprial.identity import read_device_record

    first = daemons[0]
    runtime = OrgFsRuntime(first.root / 'state', node_id=first.node_id, author='user:alice')
    try:
        fs = runtime.facade
        space = fs.create_space(ORG)
        store = OrgFsDirectoryStore(fs)
        store.ensure_org_directory_roots(ORG)
        acl = fs.create_space(ORG + '-acl')
        write_default_acl_policy(fs, acl.space_id)
        fs.update_space_meta(space.space_id, {'aclSpaceId': acl.space_id})
        store.put_org_meta(ORG, {'state': 'active'})
        for d in daemons[1:]:
            fs.invite(space.space_id, 'user:' + d.env['HYPRIAL_OWNER'], 'rw')
            fs.invite(acl.space_id, 'user:' + d.env['HYPRIAL_OWNER'], 'ro')
        for d in daemons:
            record = read_device_record(d.root / 'home')
            owner = d.env['HYPRIAL_OWNER']
            fs.author = 'user:' + owner
            fs.mkdir(space.space_id, 'directory/devices/' + owner)
            # The old-directory compatibility case deliberately omits derived keys.
            row = {'owner': owner, 'deviceId': d.node_id,
                   'clientPublic': record.client_public, 'serverPublic': record.server_public,
                   'address': addresses[d.node_id], 'keyGeneration': 1,
                   'updatedAt': '2026-10-26T00:00:00Z'}
            fs.write_text(space.space_id, f'directory/devices/{owner}/{d.node_id}.json', json.dumps(row))
        fs.author = 'user:alice'
        fs.write_text(space.space_id, 'before-upgrade.md', 'legacy-local-copy')
    finally:
        runtime.close()
    for d in daemons[1:]:
        shutil.copytree(first.root / 'state' / 'orgfs', d.root / 'state' / 'orgfs', dirs_exist_ok=True)
    return space.space_id


def pending(d, mid):
    return any(row.get('messageId') == mid for row in d.ps().get('pendingMessages', []))


def roundtrip(a, b, marker):
    driver = 'cycle-driver'
    target = f"agent:{b.env['HYPRIAL_OWNER']}:{b.node_id}:{driver}"
    sent = a.command('message', 'send', '--from', driver, '--to', target,
                     '--summary', marker, marker, '--json')
    mid = sent['messageId']
    try:
        INVITE['_wait']('request delivery', lambda: pending(b, mid), bool, timeout=60)
    except AssertionError:
        print('delivery-failure', marker, json.dumps(sent), flush=True)
        for d in (a, b):
            status = d.ps()
            print('delivery-status', d.node_id, json.dumps({
                'forwarding': status.get('forwarding'),
                'pendingMessages': status.get('pendingMessages'),
                'maintenance': status.get('maintenance'),
            }), flush=True)
        raise
    replied = b.request('message.reply', {'actor': driver, 'messageId': mid,
                                        'summary': marker + '-reply', 'message': marker + '-reply'})
    assert replied.get('replied') is True
    rid = replied['replyMessageId']
    INVITE['_wait']('reply delivery', lambda: pending(a, rid), bool, timeout=60)
    assert a.command('message', 'ack', '--from', driver, rid, '--json')['acknowledged']


def directories(daemons):
    wanted = sorted(d.node_id for d in daemons)
    for d in daemons:
        INVITE['_wait']('both directory views',
            lambda d=d: sorted(row['deviceId'] for row in d.command(
                'org', 'network', ORG, '--json')['directory'][ORG]),
            lambda value: value == wanted, timeout=75)


def receipt(d, space):
    from hyprial.daemon.impl.forwarding.membership.migration import receipt_path
    return json.loads(receipt_path(d.root / 'home', space).read_text())


def run(count, lease):
    binary = os.environ.get('HYPRIAL_TAILCAT_BINARY', '')
    relay_binary = os.environ.get('HYPRIAL_TAILCAT_LABRELAY', '')
    if not Path(binary).is_file() or not Path(relay_binary).is_file():
        print('RESULT BLOCKED tailcat-cycle-upgrade: explicit sidecar/labrelay binaries required')
        return 3
    relay, derp = INVITE['_start_labrelay'](relay_binary)
    try:
        with scenario_tempdir('hr-cycle-upgrade-', directory=short_temp_root()) as root:
            daemons = [IsolatedDaemon(hyprial_executable(), root / ('a', 'b', 'd')[i], f'cycle-{i}',
                                     tailcat_forwarding=True) for i in range(count)]
            try:
                addresses = {}
                for d, owner in zip(daemons, ('alice', 'bob', 'charlie'), strict=False):
                    INVITE['_configure_tailcat'](d, owner=owner, listen='', binary=binary, derp=derp)
                    d.env['HYPRIAL_ZENOH_SLOT_POOL'] = '2'
                    d.start()
                    addresses[d.node_id] = INVITE['_read_address'](d)
                    assert addresses[d.node_id]
                    d.stop()
                    d.wait_stopped()
                space = seed_legacy(daemons, addresses)
                assert all(not (d.root / 'home' / 'tailnet' / 'migration').exists() for d in daemons)
                # 0.5.5.1.1: authority is a local fact -- member nodes import a pin (offline) before upgrading.
                for d in daemons[1:]:
                    pinned = d.command('org', 'pin-authority', space, '--device', daemons[0].node_id, '--yes', '--json')
                    assert pinned['pinned'] is True
                # Maintenance window: authority first, final member remains offline.
                for index, d in enumerate(daemons):
                    if index == 2:
                        roundtrip(daemons[0], daemons[1], 'third-node-offline')
                    d.start()
                    d.command('agent', 'create', '--name', 'cycle-driver', '--json')
                    r = receipt(d, space)
                    assert r['state'] == 'complete'
                    assert r['params']['migration']['source'] == 'directory-0.5.5'
                    assert all(row['clientPublicByServer'] == {} for row in r['params']['members'])
                    assert len(r['params']['members']) == (count if index == 0 else 0)
                    print(f'RESULT PASS migration-stage-{index} authority-first master-only-import', flush=True)
                directories(daemons)
                for index, d in enumerate(daemons[1:], 1):
                    roundtrip(daemons[0], d, f'upgrade-{index}')
                    d.command('org', 'fs', 'write', space, f'node-{index}.md', f'after-upgrade-{index}', '--json')
                    INVITE['_wait']('OrgFS post-upgrade replication',
                        lambda: daemons[0].command('org', 'fs', 'read', space, f'node-{index}.md', '--json'),
                        lambda value: value.get('text') == f'after-upgrade-{index}', timeout=60)
                print(f'RESULT PASS {count}-node-upgrade directory+message-roundtrip+orgfs', flush=True)
                # Restart must preserve migration receipt, not reimport directories.
                before = receipt(daemons[1], space)
                daemons[1].stop()
                daemons[1].wait_stopped()
                daemons[1].start()
                assert receipt(daemons[1], space) == before
                roundtrip(daemons[0], daemons[1], 'after-member-restart')
                print('RESULT PASS member-restart fresh-lease unchanged-migration-receipt', flush=True)
                if lease:
                    lease_expiry(daemons, space)
                remove_member(daemons[0], daemons[-1], space)
            finally:
                failures = []
                for d in reversed(daemons):
                    try:
                        d.stop()
                        d.wait_stopped()
                    except Exception as error:
                        failures.append(type(error).__name__)
                if failures:
                    raise AssertionError('owned daemon cleanup failed: ' + ','.join(failures))
        print('RESULT PASS tailcat-cycle-upgrade cleanup=clean')
        return 0
    finally:
        INVITE['_stop_labrelay'](relay)


def remove_member(authority, member, space):
    owner = member.env['HYPRIAL_OWNER']
    preview = authority.request('org.remove', {'org': ORG, 'user': owner, 'confirmed': False})
    assert preview['phase'] == 'pending-confirmation'
    assert preview['deviceIds'] == [member.node_id]
    result = authority.command('org', 'remove', ORG, owner, '--yes', '--json')
    assert result['phase'] == 'complete', result
    assert result['upperLayerReceipt'] == 'committed'
    assert result['zenohDeny'] == 'not-implemented-0.5.5.1'
    backup = Path(result['result']['backupPath'])
    assert backup.is_relative_to(authority.root) and backup.is_file()
    assert backup.stat().st_mode & 0o777 == 0o600
    old = json.loads(backup.read_text())['tailnets'][space]
    assert old['members'][member.node_id]['state'] == 'active'
    current = json.loads((authority.root / 'home/tailnet/membership.json').read_text())
    assert current['tailnets'][space]['members'][member.node_id]['state'] == 'tombstone'
    INVITE['_wait']('revoked member lease invalidation',
        lambda: member.ps()['forwarding']['readiness']['member'],
        lambda value: value is False, timeout=80)
    print('RESULT PASS confirmed-remove orgfs-first backup=0600 tombstone member=false', flush=True)


def lease_expiry(daemons, space):
    authority = daemons[0]
    authority.stop()
    authority.wait_stopped()
    stopped = time.monotonic()
    if len(daemons) == 3:
        roundtrip(daemons[1], daemons[2], 'authority-offline-valid-leases')
        print('RESULT PASS authority-offline member-to-member message-roundtrip', flush=True)
    # A real wall-clock outage. No clock injection or shortened lease setting.
    while time.monotonic() - stopped < 310:
        readiness = [d.ps()['forwarding']['readiness'] for d in daemons[1:]]
        if all(row['member'] is False for row in readiness):
            break
        time.sleep(2)
    elapsed = time.monotonic() - stopped
    assert elapsed <= 310 and all(row['member'] is False for row in readiness), readiness
    assert all(row['control'] is True for row in readiness), readiness
    print(f'RESULT PASS authority-outage real-lease-expiry elapsed={elapsed:.2f}s control=ready', flush=True)
    authority.start()
    for index, member in enumerate(daemons[1:], 1):
        INVITE['_wait']('lease recovery', lambda: member.ps()['forwarding']['readiness']['member'], bool, timeout=95)
        marker = f'after-authority-return-{index}'
        roundtrip(authority, member, marker)
        member.command('org', 'fs', 'write', space, marker + '.md', marker, '--json')
        INVITE['_wait']('OrgFS after authority return',
            lambda: authority.command('org', 'fs', 'read', space, marker + '.md', '--json'),
            lambda value: value.get('text') == marker, timeout=60)
    print('RESULT PASS authority-return all-member-message-roundtrip+orgfs', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--nodes', type=int, choices=(2, 3), default=2)
    parser.add_argument('--lease', action='store_true')
    args = parser.parse_args()
    try:
        raise SystemExit(run(args.nodes, args.lease))
    except Exception as error:
        import traceback
        traceback.print_exc()
        print(f'RESULT FAIL tailcat-cycle-upgrade {type(error).__name__}: {error}')
        raise SystemExit(1) from error
