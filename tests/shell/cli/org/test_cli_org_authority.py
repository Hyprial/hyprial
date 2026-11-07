"""`org pin-authority` / `org migration-status` run offline against the local store."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.runtime import entry as cli
from tests.daemon.forwarding.membership.hardening.support import LOCAL_DEVICE, OWNER, LegacyHome, legacy_row


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    node = LegacyHome(tmp_path)
    space = node.seed_org('group-two', [legacy_row(OWNER, LOCAL_DEVICE), legacy_row(OWNER, 'alice-old')])
    node.seed_org('group-dead', [], state='deleted')
    node.close()
    monkeypatch.setenv('HYPRIAL_HOME', str(node.home))
    monkeypatch.setenv('HARNESS_STATE_DIR', str(node.state_dir))
    monkeypatch.setenv('HYPRIAL_NETWORK_ISOLATED', '1')
    return node, space


def invoke(*args):
    result = CliRunner().invoke(cli.app, ['org', *args, '--json'])
    return result.exit_code, json.loads(result.stdout)


def test_pin_authority_requires_confirmation_then_writes_the_pin(seeded):
    node, space = seeded
    code, payload = invoke('pin-authority', space, '--device', 'alice-old')
    assert (code, payload['ok'], payload['code']) == (1, False, 'CONFIRMATION_REQUIRED')
    assert node.settings() == {}
    code, payload = invoke('pin-authority', space, '--device', 'alice-old', '--yes')
    assert code == 0 and payload['ok'] is True and payload['pinned'] is True
    assert payload['deviceId'] == 'alice-old' and payload['restartRequired'] is True
    assert 'nodekey:' not in json.dumps(payload)
    assert node.settings()['forwarding']['tailnet']['authorities'][space]['deviceId'] == 'alice-old'


def test_pin_authority_reports_unknown_device_with_candidates(seeded):
    _node, space = seeded
    code, payload = invoke('pin-authority', space, '--device', 'nope', '--yes')
    assert code == 1 and payload['code'] == 'MEMBER_UNKNOWN'


def test_migration_status_lists_spaces_without_a_daemon(seeded):
    _node, space = seeded
    code, payload = invoke('migration-status')
    assert code == 0 and payload['ok'] is True
    rows = {row['spaceId']: row for row in payload['spaces']}
    assert rows[space]['pinned'] is False and rows[space]['receipt'] is None
    assert rows[space]['rows']['accepted'] == [LOCAL_DEVICE, 'alice-old']
    assert any(row['skipReason'] == 'org-deleted' for row in payload['spaces'])


# --- Round 2 item 3: cold-process import must work (tests/conftest preloads the app) ---

ROOT = Path(__file__).resolve().parents[4]


def _cold_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / 'cold-home'
    home.mkdir(parents=True, exist_ok=True)
    return {**os.environ, 'HOME': str(home), 'HYPRIAL_HOME': str(home / 'hyprial'),
            'HARNESS_STATE_DIR': str(home / 'state'), 'HYPRIAL_NETWORK_ISOLATED': '1',
            'NO_COLOR': '1', 'TERM': 'dumb'}


def test_offline_membership_imports_in_a_fresh_process(tmp_path):
    completed = subprocess.run(
        [sys.executable, '-c', 'from hyprial.daemon import OfflineMembership; print("import-ok")'],
        cwd=ROOT, env=_cold_env(tmp_path), text=True, capture_output=True, check=False, timeout=120)
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert completed.stdout.strip() == 'import-ok'


def test_cold_process_cli_smoke_for_both_operator_commands(seeded, tmp_path):
    node, space = seeded
    env = {**_cold_env(tmp_path), 'HYPRIAL_HOME': str(node.home), 'HARNESS_STATE_DIR': str(node.state_dir)}
    status = subprocess.run([sys.executable, '-m', 'hyprial.cli', 'org', 'migration-status', '--json'],
                            cwd=ROOT, env=env, text=True, capture_output=True, check=False, timeout=120)
    assert status.returncode == 0, status.stderr[-2000:]
    payload = json.loads(status.stdout)
    assert payload['ok'] is True and any(row['spaceId'] == space for row in payload['spaces'])
    helped = subprocess.run([sys.executable, '-m', 'hyprial.cli', 'org', 'pin-authority', '--help'],
                            cwd=ROOT, env=env, text=True, capture_output=True, check=False, timeout=120)
    assert helped.returncode == 0, helped.stderr[-2000:]
    assert '--device' in helped.stdout
