"""Managed v6 configuration: private inbound, identity and no admission override."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.forwarding_config import ForwardingConfigurationError, daemon_forwarding_environment
from hyprial.daemon.impl.forwarding.membership.settings import private_inbound
from hyprial.daemon.impl.forwarding.slots import slot_pool_size, slot_socket_paths
from hyprial.daemon.impl.network.tailcat import BINARY_ENV
from hyprial.identity.impl.device import DeviceRecord, address_path, device_key_path, write_device_record
from hyprial.shell.impl.cli.commands.common import daemon_start
from hyprial.shell.impl.daemon_wiring import daemon_dependencies, usage_factory
from tests.support.runtime._owned_applications import owned_applications as owned_applications


def _device_ready(home: Path) -> None:
    key_file = device_key_path(home)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text('{"v":1,"server":{},"client":{}}')
    key_file.chmod(0o600)
    write_device_record(home, DeviceRecord(device_id='node-a', owner='tester',
        server_public='nodekey:' + 'a'*64, client_public='nodekey:' + 'b'*64))


def _candidate(tmp_path: Path) -> Path:
    binary = tmp_path / 'hyprial-tailcat'
    binary.write_text('candidate')
    binary.chmod(0o755)
    return binary


def _inputs(home, binary):
    target = private_inbound(home)
    return {BINARY_ENV: str(binary), 'HYPRIAL_FORWARDING_INBOUND_TARGET': target,
            'HYPRIAL_ZENOH_LISTEN': 'unixsock-stream/' + target.removeprefix('unix:')}


def _up(home, inputs):
    return json.loads(daemon_forwarding_environment(home, inputs, node_id='node-a')['HYPRIAL_FORWARDING_UP'])


def test_forwarding_environment_uses_device_identity_and_private_target(tmp_path):
    home = tmp_path / 'home'
    _device_ready(home)
    binary = _candidate(tmp_path)
    inputs = _inputs(home, binary)
    result = daemon_forwarding_environment(home, inputs, node_id='node-a')
    assert json.loads(result['HYPRIAL_FORWARDING_COMMAND']) == [str(binary), 'forward']
    assert json.loads(result['HYPRIAL_FORWARDING_UP']) == {
        'v': 6, 'op': 'up', 'keyFile': str(device_key_path(home)),
        'owner': 'tester', 'deviceId': 'node-a', 'controlPort': 7448,
        'membershipStore': str(home / 'tailnet/membership.json'),
        'addressFile': str(address_path(home)), 'inboundTarget': inputs['HYPRIAL_FORWARDING_INBOUND_TARGET'],
        'peerPort': 7447, 'slots': list(slot_socket_paths(home, slot_pool_size()))}


def test_legacy_admission_environment_cannot_reopen_the_sidecar(tmp_path):
    home = tmp_path / 'home'
    _device_ready(home)
    inputs = _inputs(home, _candidate(tmp_path)) | {'HYPRIAL_FORWARDING_ALLOW_ANY': '1', 'HYPRIAL_FORWARDING_ALLOW': 'nodekey:anyone'}
    up = _up(home, inputs)
    assert 'allow' not in up and 'allowAny' not in up


@pytest.mark.parametrize('target', ['127.0.0.1:39111', 'tcp:127.0.0.1:39111', 'tcp:0.0.0.0:39111'])
def test_managed_tcp_inbound_is_refused(tmp_path, target):
    with pytest.raises(ForwardingConfigurationError) as caught:
        daemon_forwarding_environment(tmp_path, {'HYPRIAL_FORWARDING_INBOUND_TARGET': target}, node_id='node-a')
    assert caught.value.code == 'FORWARDING_TARGET_INVALID'


def test_no_forwarding_configuration_keeps_existing_daemon_path(tmp_path):
    assert daemon_forwarding_environment(tmp_path, {}, node_id='node-a') == {}


@pytest.mark.parametrize('missing', ['sidecar', 'device'])
def test_missing_prerequisite_is_loud(tmp_path, missing):
    home = tmp_path / 'home'
    if missing != 'device':
        _device_ready(home)
    binary = tmp_path / 'missing' if missing == 'sidecar' else _candidate(tmp_path)
    with pytest.raises(ForwardingConfigurationError) as caught:
        _up(home, _inputs(home, binary))
    assert caught.value.code == ('FORWARDING_SIDECAR_MISSING' if missing == 'sidecar' else 'FORWARDING_DEVICE_MISSING')


def test_local_derp_and_remote_map_configuration_are_preserved(tmp_path):
    home = tmp_path / 'home'
    _device_ready(home)
    inputs = _inputs(home, _candidate(tmp_path))
    derp = {'host': '127.0.0.1', 'derpPort': 3340, 'stunPort': 3478, 'insecureForTests': True}
    assert _up(home, inputs | {'HYPRIAL_TAILCAT_DERP': json.dumps(derp)})['derp'] == derp
    with pytest.raises(ForwardingConfigurationError) as caught:
        _up(home, inputs | {'HYPRIAL_TAILCAT_DERP': '{not json'})
    assert caught.value.code == 'FORWARDING_DERP_INVALID'
    (home / 'settings.json').write_text(json.dumps({'forwarding': {'derpMapURL': 'https://derpmap.example/map.json'}}))
    up = _up(home, inputs)
    assert up['derpMapURL'] == 'https://derpmap.example/map.json'
    assert up['derpMapCacheDir'] == str(address_path(home).parent / 'derpmap-cache')
    (home / 'settings.json').write_text(json.dumps({'forwarding': {'derpMapURL': 'http://derpmap.example/map.json'}}))
    with pytest.raises(ForwardingConfigurationError) as caught:
        _up(home, inputs)
    assert caught.value.code == 'FORWARDING_DERP_INVALID'
    (home / 'settings.json').write_text('{"forwarding":{}}')
    assert 'derpMapURL' not in _up(home, inputs)


def test_tailnet_settings_override_defaults(tmp_path):
    home = tmp_path / 'home'
    _device_ready(home)
    inputs = _inputs(home, _candidate(tmp_path))
    store = str(home / 'private-membership.json')
    (home / 'settings.json').write_text(json.dumps({'forwarding': {'tailnet': {
        'controlPort': 7449, 'membershipStore': store, 'trustMembersForUpperLayers': False}}}))
    up = _up(home, inputs)
    assert up['controlPort'] == 7449 and up['membershipStore'] == store
    assert 'trustMembersForUpperLayers' not in up  # Python policy, not a wire extension.


def test_detached_launcher_injects_resolved_environment():
    source = Path(daemon_start.__file__).read_text()
    launcher = source[source.index('def _launch_daemon_process'):]
    launcher = launcher[:launcher.index('\ndef ')]
    assert 'daemon_forwarding_environment(' in launcher
    assert 'environment.update(forwarding_environment)' in launcher


def _direct(tmp_path, monkeypatch, inputs):
    home = tmp_path / 'home'
    _device_ready(home)
    monkeypatch.setenv('HYPRIAL_HOME', str(home))
    monkeypatch.setenv('HYPRIAL_NODE_ID', 'node-a')
    monkeypatch.delenv('HYPRIAL_NETWORK_ISOLATED', raising=False)
    for name in ('HYPRIAL_FORWARDING_COMMAND', 'HYPRIAL_FORWARDING_UP'):
        monkeypatch.delenv(name, raising=False)
    for name, value in inputs.items():
        monkeypatch.setenv(name, value)
    from hyprial.daemon.impl.application import DaemonApplication
    return DaemonApplication.from_environment(state_dir=tmp_path / 'state',
        socket_path=tmp_path / 'state/daemon.sock', usage_factory=usage_factory, **daemon_dependencies())


def test_direct_daemon_resolves_operator_inputs(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    inputs = _inputs(home, _candidate(tmp_path))
    app = _direct(tmp_path, monkeypatch, inputs)
    try:
        assert app._forwarding_environment == daemon_forwarding_environment(home, inputs, node_id='node-a')
    finally:
        app._close()


def test_supervisor_receives_resolved_environment(tmp_path, monkeypatch):
    import hyprial.daemon.impl.application.netendpoints.forwarding as module
    seen = []
    class Supervisor:
        def __init__(self, environ, **kwargs):
            seen.append(dict(environ))
        def ensure_started(self):
            pass
        def endpoints(self):
            return None
        def close(self):
            pass
    monkeypatch.setattr(module, 'ForwardingSidecarSupervisor', Supervisor)
    inputs = _inputs(tmp_path / 'home', _candidate(tmp_path))
    app = _direct(tmp_path, monkeypatch, inputs | {'HYPRIAL_PEER_DISCOVERY': '0'})
    try:
        app._discover_peer_endpoints()
        assert len(seen) == 1
        assert all(seen[0][name] == value for name, value in app._forwarding_environment.items())
    finally:
        app._close()
