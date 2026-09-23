#!/usr/bin/env python3
"""Operator-only setup. Run with the Python interpreter containing hyprial."""
import argparse
import importlib.util
import json
import os
from pathlib import Path

from hyprial.home import reject_retired_environment
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dsh_pac_service', ROOT / 'integration/pac-service.py')
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


def main():
    reject_retired_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=Path(os.environ.get('HARNESS_STATE_DIR') or Path(os.environ.get('HYPRIAL_HOME', str(Path.home() / '.hyprial'))) / 'state'))
    for role in ('coordinator', 'worker', 'verifier'):
        parser.add_argument('--' + role, help='existing GUI Session ID, not a display title')
    parser.add_argument('--allow-remote', action='append', default=[], help='exact authorized sender Agent URI; absent = no unattended remote creation')
    parser.add_argument('--enable', action='store_true', help='enable Host polling after role setup')
    parser.add_argument('--disable', action='store_true')
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()
    state = args.state_dir.resolve()
    root = state / 'dsh-pac'
    config_path = root / 'config.json'
    if args.status:
        print(config_path.read_text() if config_path.exists() else json.dumps({'configured': False}))
        return
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    import fcntl
    with (root / 'service.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = json.loads(config_path.read_text()) if config_path.exists() else None
        if args.disable:
            if old:
                old['enabled'] = False
                service.atomic(config_path, old)
            print(json.dumps({'ok': True, 'enabled': False}))
            return
        if not all(getattr(args, role) for role in ('coordinator', 'worker', 'verifier')):
            parser.error('provide all three role Session IDs')
        if len({args.coordinator, args.worker, args.verifier}) != 3:
            parser.error('three independent sessions are required')
        import re
        if any(not re.fullmatch(r'agent:[^:\s]+:[^:\s]+:[^:\s]+', actor) for actor in args.allow_remote):
            parser.error('--allow-remote requires exact four-part Agent URIs')
        env = {k: v for k, v in os.environ.items() if not k.startswith(('HARNESS_',))}
        env['HARNESS_STATE_DIR'] = str(state)
        roles = {}
        for role in ('coordinator', 'worker', 'verifier'):
            sid = getattr(args, role)
            result = subprocess.run(['node', str(ROOT / 'h2b-session-bridge.mjs'), 'rpc'],
                                    input=json.dumps({'operation': 'session-tool', 'sessionId': sid, 'tool': 'prepare', 'args': {}}),
                                    env=env, capture_output=True, text=True, timeout=15)
            if result.returncode:
                raise RuntimeError(result.stdout or result.stderr)
            identity = json.loads(result.stdout)
            roles[role] = {'sessionId': sid, 'actor': identity['actor']}
        # Active graphs pin their role bindings. Avoid silently orphaning their delivery.
        if old and old['roles'] != roles:
            for path in root.glob('task-*.json'):
                task = json.loads(path.read_text())
                if task.get('graphId') and not service.snapshot(state / 'pac-graph.sqlite3', task['graphId'])['closed']:
                    raise RuntimeError('active tasks exist; finish/close them before changing roles')
        config = {'version': 1, 'enabled': args.enable, 'python': sys.executable, 'roles': roles, 'remoteDispatchers': sorted(set(args.allow_remote))}
        if old:
            service.atomic(root / 'config.backup.json', old)
        service.atomic(config_path, config)
        print(json.dumps({'ok': True, 'config': str(config_path), **config}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
