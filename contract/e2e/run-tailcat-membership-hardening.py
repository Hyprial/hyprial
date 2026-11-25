#!/usr/bin/env python3
"""Tailcat v6 upgrade with the 2026-10-28 production shape: multi-device owner + deleted org.

Three device identities with real ``hyprial-tailcat`` sidecars and a local
labrelay: alice-a (org owner, upgrades first), alice-c (the owner's second
device; it only contributes a real row and never upgrades) and bob (member).
The synthetic 0.5.5 local directory copies carry what took production down:
the owner has two distinct device rows plus a stale row of a renamed host with
identical keys and a row with a corrupted ``updatedAt``, a deleted org (``directory/org.json`` state ``deleted``) with zero rows, and a
probe org with zero rows. Expected sequence:

1. First start: alice's daemon refuses fast with ``MIGRATION_AUTHORITY_MISSING``
   (no sidecar launch in that phase), ``daemon.start.failed`` carries the code,
   and ``state/daemon-start-failures.json`` records the attempt.
2. ``hyprial org pin-authority <spaceId> --device <alice> --yes`` works offline;
   ``hyprial org migration-status`` shows the pin and the skipped spaces.
3. Second start migrates: receipt complete, stale row dropped, deleted/probe
   orgs skipped, counter cleared, bob imports the pin and both exchange a
   message round-trip.

Prerequisites (RESULT BLOCKED when missing, never a silent skip):
``HYPRIAL_TAILCAT_BINARY`` and ``HYPRIAL_TAILCAT_LABRELAY``.
"""
from __future__ import annotations

import json
import os
import runpy
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _daemon_runner import IsolatedDaemon, hyprial_executable, scenario_tempdir, short_temp_root  # noqa: E402

INVITE = runpy.run_path(str(Path(__file__).with_name('run-tailcat-org-invite-join.py')))
ORG = 'group-hardening'
DEAD = 'group-deleted'
PROBE = 'group-probe-e2e'
STALE_DEVICE = 'renamed-host-stale'
CORRUPT_DEVICE = 'second-device-corrupt-updated-at'


def seed_legacy(daemons, addresses):
    from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
    from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
    from hyprial.daemon.impl.org.network.create import write_default_acl_policy
    from hyprial.identity import read_device_record

    first = daemons[0]
    runtime = OrgFsRuntime(first.root / 'state', node_id=first.node_id, author='user:alice')
    try:
        fs = runtime.facade
        store = OrgFsDirectoryStore(fs)

        def org_space(name, state):
            space = fs.create_space(name)
            store.ensure_org_directory_roots(name)
            acl = fs.create_space(name + '-acl')
            write_default_acl_policy(fs, acl.space_id)
            fs.update_space_meta(space.space_id, {'aclSpaceId': acl.space_id})
            store.put_org_meta(name, {'state': state})
            return space

        space = org_space(ORG, 'active')
        dead = org_space(DEAD, 'deleted')
        probe = org_space(PROBE, 'active')
        for d in daemons[1:]:
            owner = 'user:' + d.env['HYPRIAL_OWNER']
            for s in (space, dead, probe):
                fs.invite(s.space_id, owner, 'rw')
        made_dirs = set()
        for d in daemons:
            record = read_device_record(d.root / 'home')
            owner = d.env['HYPRIAL_OWNER']
            fs.author = 'user:' + owner
            if owner not in made_dirs:
                fs.mkdir(space.space_id, 'directory/devices/' + owner)
                made_dirs.add(owner)
            row = {'owner': owner, 'deviceId': d.node_id,
                   'clientPublic': record.client_public, 'serverPublic': record.server_public,
                   'address': addresses[d.node_id], 'keyGeneration': 1,
                   'updatedAt': '2026-10-20T00:00:00Z'}
            fs.write_text(space.space_id, f'directory/devices/{owner}/{d.node_id}.json', json.dumps(row))
            if d is first:
                # The renamed host's stale row: same keys and address, older updatedAt.
                stale = dict(row, deviceId=STALE_DEVICE, updatedAt='2026-10-17T00:00:00Z')
                fs.write_text(space.space_id, f'directory/devices/{owner}/{STALE_DEVICE}.json', json.dumps(stale))
            if d is daemons[1]:
                # The owner's second device also left a corrupted-updatedAt duplicate.
                corrupt = dict(row, deviceId=CORRUPT_DEVICE, updatedAt='2026-10-197T005:52:31Z')
                fs.write_text(space.space_id, f'directory/devices/{owner}/{CORRUPT_DEVICE}.json', json.dumps(corrupt))
        fs.author = 'user:alice'
    finally:
        runtime.close()
    for d in daemons[1:]:
        shutil.copytree(first.root / 'state' / 'orgfs', d.root / 'state' / 'orgfs', dirs_exist_ok=True)
    return space.space_id, dead.space_id, probe.space_id


def receipt(d, space):
    from hyprial.daemon.impl.forwarding.membership.migration import receipt_path
    return json.loads(receipt_path(d.root / 'home', space).read_text())


def daemon_events(d, name):
    path = d.root / 'state' / 'logs' / 'daemon.jsonl'  # HARNESS_STATE_DIR=<root>/state (IsolatedDaemon)
    rows = []
    for line in path.read_text().splitlines() if path.exists() else []:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get('event') == name:
            rows.append(entry)
    return rows


def failed_start(d):
    try:
        d.start()
    except Exception as error:  # noqa: BLE001 - the refusal is the expected outcome
        return error
    raise AssertionError('daemon start was expected to fail')


def run():
    binary = os.environ.get('HYPRIAL_TAILCAT_BINARY', '')
    relay_binary = os.environ.get('HYPRIAL_TAILCAT_LABRELAY', '')
    if not Path(binary).is_file() or not Path(relay_binary).is_file():
        print('RESULT BLOCKED tailcat-membership-hardening: explicit sidecar/labrelay binaries required')
        return 3
    relay, derp = INVITE['_start_labrelay'](relay_binary)
    try:
        with scenario_tempdir('hr-mig-hard-', directory=short_temp_root()) as root:
            daemons = [IsolatedDaemon(hyprial_executable(), root / ('a', 'c', 'b')[i], f'hard-{i}',
                                     tailcat_forwarding=True) for i in range(3)]
            try:
                addresses = {}
                for d, owner in zip(daemons, ('alice', 'alice', 'bob'), strict=True):
                    INVITE['_configure_tailcat'](d, owner=owner, listen='', binary=binary, derp=derp)
                    d.env['HYPRIAL_ZENOH_SLOT_POOL'] = '2'
                    d.start()
                    addresses[d.node_id] = INVITE['_read_address'](d)
                    assert addresses[d.node_id]
                    d.stop()
                    d.wait_stopped()
                space, dead, probe = seed_legacy(daemons, addresses)
                alice, second, bob = daemons  # `second` never upgrades: it only owns a row

                started = time.monotonic()
                failed_start(alice)
                elapsed = time.monotonic() - started
                failures = daemon_events(alice, 'daemon.start.failed')
                assert any(e.get('code') == 'MIGRATION_AUTHORITY_MISSING' for e in failures), failures
                assert all(e.get('attempt') == 1 for e in failures), failures
                counter = json.loads((alice.root / 'state' / 'daemon-start-failures.json').read_text())
                assert counter['count'] == 1 and counter['lastCode'] == 'MIGRATION_AUTHORITY_MISSING'
                assert not (alice.root / 'home' / 'tailnet' / 'migration').exists()
                print(f'RESULT PASS refused-start code=MIGRATION_AUTHORITY_MISSING attempt=1 elapsed={elapsed:.1f}s', flush=True)

                status = alice.command('org', 'migration-status', '--json')
                rows = {row['spaceId']: row for row in status['spaces']}
                assert rows[dead]['skipReason'] == 'org-deleted' and rows[probe]['skipReason'] == 'no-device-rows'
                assert rows[space]['pinned'] is False
                assert sorted(rows[space]['rows']['dropped']) == sorted([STALE_DEVICE, CORRUPT_DEVICE])
                pinned = alice.command('org', 'pin-authority', space, '--device', alice.node_id, '--yes', '--json')
                assert pinned['pinned'] is True and pinned['restartRequired'] is True
                print('RESULT PASS offline pin-authority + migration-status', flush=True)

                alice.start()
                r = receipt(alice, space)
                assert r['state'] == 'complete' and r['params']['authority']['deviceId'] == alice.node_id
                assert sorted(r['diagnostics']['droppedDeviceIds']) == sorted([STALE_DEVICE, CORRUPT_DEVICE])
                assert sorted(m['deviceId'] for m in r['params']['members']) == sorted(
                    [alice.node_id, second.node_id, bob.node_id])
                skipped = {e['spaceId']: e['reason'] for e in daemon_events(alice, 'membership.migration.skipped')}
                assert skipped[dead] == 'org-deleted' and skipped[probe] == 'no-device-rows', skipped
                assert not (alice.root / 'state' / 'daemon-start-failures.json').exists()
                print('RESULT PASS authority-migration dropped-stale-row skipped-deleted+probe counter-cleared', flush=True)

                # Bob sees the same two owner devices; he must pin too (offline) before upgrading.
                assert failed_start(bob) is not None
                bob.command('org', 'pin-authority', space, '--device', alice.node_id, '--yes', '--json')
                bob.start()
                assert receipt(bob, space)['params']['members'] == []
                INVITE['_wait']('bob member readiness', lambda: bob.ps()['forwarding']['readiness']['member'], bool, timeout=95)
                alice.command('agent', 'create', '--name', 'hard-driver', '--json')
                bob.command('agent', 'create', '--name', 'hard-driver', '--json')
                target = f"agent:bob:{bob.node_id}:hard-driver"
                sent = alice.command('message', 'send', '--from', 'hard-driver', '--to', target,
                                     '--summary', 'hardening', 'hardening', '--json')
                INVITE['_wait']('request delivery',
                                lambda: any(row.get('messageId') == sent['messageId']
                                            for row in bob.ps().get('pendingMessages', [])), bool, timeout=60)
                print('RESULT PASS two-node-upgrade member-import message-roundtrip', flush=True)
            finally:
                failures = []
                for d in reversed(daemons):
                    try:
                        if d is second:
                            continue  # never started after seeding
                        d.stop()
                        d.wait_stopped()
                    except Exception as error:  # noqa: BLE001
                        failures.append(type(error).__name__)
                if failures:
                    raise AssertionError('owned daemon cleanup failed: ' + ','.join(failures))
        print('RESULT PASS tailcat-membership-hardening cleanup=clean')
        return 0
    finally:
        INVITE['_stop_labrelay'](relay)


if __name__ == '__main__':
    try:
        raise SystemExit(run())
    except Exception as error:
        import traceback
        traceback.print_exc()
        print(f'RESULT FAIL tailcat-membership-hardening {type(error).__name__}: {error}')
        raise SystemExit(1) from error
