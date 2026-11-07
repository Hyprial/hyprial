"""Offline operator maintenance must not race daemon ownership or widen targets."""
import json
from types import SimpleNamespace

import pytest

from hyprial.daemon import DaemonStateOwnershipFence
from hyprial.daemon.impl.forwarding.membership.maintenance import TailnetMaintenance
from hyprial.daemon.impl.forwarding.policy import ForwardingSidecarError
from tests.support.tailnet_membership import FakeMembershipSidecar, member


@pytest.fixture
def maintenance(tmp_path):
    fake = FakeMembershipSidecar(owner='alice', device_id='old')
    fake.seed('space', members=[fake.local, member('bob', 'new')])
    calls = []
    def rpc(op, params):
        calls.append((op, params))
        result = fake.membership_request(op, params)
        if op == 'authority-repin':
            backup = tmp_path / 'pre-change.backup'
            backup.write_text('fixture backup')
            result['backupPath'] = str(backup)
        return result
    controller = SimpleNamespace(membership_request=rpc, close=lambda: calls.append(('close', {})))
    tool = TailnetMaintenance(tmp_path, tmp_path / 'state')
    tool._start_controller = lambda: controller
    return tool, fake, calls


def test_live_daemon_refuses_before_starting_sidecar(maintenance):
    tool, _fake, calls = maintenance
    with DaemonStateOwnershipFence.acquire(tool.state_dir):
        with pytest.raises(ForwardingSidecarError) as caught:
            with tool:
                pass
    assert caught.value.code == 'NETWORK_NOT_QUIESCENT'
    assert calls == []


def test_confirmation_before_repin_and_backup_before_settings(maintenance):
    tool, fake, calls = maintenance
    with tool:
        preview = tool.prepare('space', 'new')
        assert preview['deviceIds'] == ['new', 'old']
        assert 'nodekey:' not in str(preview) and 'address' not in str(preview)
        with pytest.raises(ForwardingSidecarError) as caught:
            tool.commit(confirmed=False)
        assert caught.value.code == 'CONFIRMATION_REQUIRED'
        assert not any(op == 'authority-repin' for op, _ in calls)
        result = tool.commit(confirmed=True)
    assert result['restartRequired'] is True
    assert result['phase'] == 'complete'
    settings = json.loads((tool.home / 'settings.json').read_text())
    assert settings['forwarding']['tailnet']['authorities']['space']['deviceId'] == 'new'
    assert fake.tailnets['space']['members']['old']['state'] == 'active'
    assert len(fake.backups) == 1
    assert calls[-1][0] == 'close'
    with DaemonStateOwnershipFence.acquire(tool.state_dir):
        pass  # The command releases ownership only after the child is closed.


def test_unknown_authority_and_go_quiescence_refuse_without_settings(maintenance):
    tool, fake, _calls = maintenance
    with tool:
        with pytest.raises(ForwardingSidecarError) as caught:
            tool.prepare('space', 'missing')
        assert caught.value.code == 'MEMBER_UNKNOWN'
        tool.prepare('space', 'new')
        fake.business_running = True
        with pytest.raises(ForwardingSidecarError) as caught:
            tool.commit(confirmed=True)
        assert caught.value.code == 'NETWORK_NOT_QUIESCENT'
    assert not (tool.home / 'settings.json').exists()
    assert fake.backups == []


def test_lost_response_replays_exact_operation(maintenance):
    tool, _fake, calls = maintenance
    with tool:
        tool.prepare('space', 'new')
        original = tool.controller.membership_request
        def lose(op, params):
            result = original(op, params)
            if op == 'authority-repin':
                raise TimeoutError('lost reply')
            return result
        tool.controller.membership_request = lose
        with pytest.raises(TimeoutError):
            tool.commit(confirmed=True)
        tool.controller.membership_request = original
    with tool:
        tool.prepare('space', 'new')
        result = tool.commit(confirmed=True)
    requests = [params for op, params in calls if op == 'authority-repin']
    assert requests[0] == requests[1]
    assert result['phase'] == 'complete'


def test_pending_operation_cannot_silently_change_target(maintenance):
    tool, fake, _calls = maintenance
    with tool:
        tool.prepare('space', 'new')
        fake.failures['authority-repin'] = 'MEMBERSHIP_BACKUP_FAILED'
        with pytest.raises(ForwardingSidecarError):
            tool.commit(confirmed=True)
        with pytest.raises(ForwardingSidecarError) as caught:
            tool.prepare('space', 'old')
        assert caught.value.code == 'OPERATION_CONFLICT'


def test_revision_conflict_requires_new_preview_and_confirmation(maintenance):
    tool, fake, _calls = maintenance
    with tool:
        first = tool.prepare('space', 'new')
        fake.tailnets['space']['revision'] += 1
        with pytest.raises(ForwardingSidecarError) as caught:
            tool.commit(confirmed=True)
        assert caught.value.code == 'REVISION_CONFLICT'
        second = tool.prepare('space', 'new')
        assert second['operationId'] != first['operationId']
        assert second['expectedRevision'] == 2
        with pytest.raises(ForwardingSidecarError):
            tool.commit(confirmed=False)
        assert tool.commit(confirmed=True)['phase'] == 'complete'
    archives = list((tool.home / 'tailnet' / 'maintenance').glob('*.conflict.json'))
    assert len(archives) == 1


def test_maintenance_launch_has_no_backend_slots_or_public_inbound(tmp_path, monkeypatch):
    from pathlib import Path
    from hyprial.daemon.impl.forwarding.membership import maintenance as module
    from hyprial.kernel import FORWARDING_UP_ENV
    observed = {}
    monkeypatch.setattr(module, 'read_device_record', lambda _home: SimpleNamespace(device_id='local'))
    def environment(_home, env, *, node_id):
        observed.update(env)
        return {FORWARDING_UP_ENV: json.dumps({'slots': ['must-not-open'], 'addressFile': 'must-not-write'})}
    monkeypatch.setattr(module, 'daemon_forwarding_environment', environment)
    def start(env):
        up = json.loads(env[FORWARDING_UP_ENV])
        assert up['slots'] == []
        target = Path(env[module.FORWARDING_TARGET_ENV].removeprefix('unix:'))
        assert not target.exists() and target.parent.stat().st_mode & 0o777 == 0o700
        assert Path(up['addressFile']).parent == target.parent
        def close():
            with pytest.raises(Exception) as caught:
                DaemonStateOwnershipFence.acquire(tmp_path / 'state')
            assert caught.value.code == 'DAEMON_OWNERSHIP_BUSY'
        return SimpleNamespace(close=close)
    monkeypatch.setattr(module.ForwardingSidecarController, 'from_environment', start)
    with TailnetMaintenance(tmp_path, tmp_path / 'state', environ={}):
        pass
    assert observed[module.FORWARDING_TARGET_ENV].startswith('unix:/tmp/hr-repin-')
