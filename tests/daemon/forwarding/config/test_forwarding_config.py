"""Managed v6 configuration: private inbound, identity and no admission override."""
from __future__ import annotations

import json
import subprocess
import sys
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
from tests.support.runtime.environment import isolated_environment


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


def test_managed_loopback_tcp_inbound_is_accepted_on_windows(tmp_path, monkeypatch):
    import hyprial.daemon.impl.forwarding.policy as policy
    gate_calls = []
    monkeypatch.setattr(
        policy, 'loopback_inbound_supported', lambda: gate_calls.append(True) or True
    )
    home = tmp_path / 'home'
    _device_ready(home)
    inputs = {
        'HYPRIAL_TAILCAT_BINARY': str(_candidate(tmp_path)),
        'HYPRIAL_FORWARDING_INBOUND_TARGET': 'tcp:127.0.0.1:39111',
        'HYPRIAL_ZENOH_LISTEN': 'tcp/127.0.0.1:39111',
    }
    up = _up(home, inputs)
    assert up['inboundTarget'] == 'tcp:127.0.0.1:39111'
    assert gate_calls


@pytest.mark.parametrize('target', [
    '127.0.0.1:39111', 'tcp:0.0.0.0:39111', 'tcp:192.168.1.2:39111',
    'tcp:127.0.0.2:39111', 'tcp:[::1]:39111', 'tcp:localhost:39111',
    'tcp:127.0.0.1:0', 'tcp:127.0.0.1:70000', 'tcp:127.0.0.1',
    'unix:relative.sock',
])
def test_unsafe_or_malformed_inbound_target_is_refused(tmp_path, target):
    with pytest.raises(ForwardingConfigurationError) as caught:
        daemon_forwarding_environment(tmp_path, {'HYPRIAL_FORWARDING_INBOUND_TARGET': target}, node_id='node-a')
    assert caught.value.code == 'FORWARDING_TARGET_INVALID'


def test_no_forwarding_configuration_keeps_existing_daemon_path(tmp_path):
    assert daemon_forwarding_environment(tmp_path, {}, node_id='node-a') == {}


def test_listener_helper_derives_the_managed_target_on_windows(tmp_path, monkeypatch):
    import hyprial.daemon.impl.forwarding.policy as policy
    from hyprial.daemon import managed_inbound_target_from_listener
    gate_calls = []
    monkeypatch.setattr(
        policy, 'loopback_inbound_supported', lambda: gate_calls.append(True) or True
    )
    assert managed_inbound_target_from_listener(
        'tcp/127.0.0.1:39111'
    ) == 'tcp:127.0.0.1:39111'
    assert gate_calls


def test_listener_helper_refuses_loopback_tcp_off_windows(tmp_path, monkeypatch):
    import hyprial.daemon.impl.forwarding.policy as policy
    from hyprial.daemon import managed_inbound_target_from_listener
    gate_calls = []
    monkeypatch.setattr(
        policy, 'loopback_inbound_supported', lambda: gate_calls.append(True) or False
    )
    with pytest.raises(ForwardingConfigurationError) as caught:
        managed_inbound_target_from_listener('tcp/127.0.0.1:39111')
    assert caught.value.code == 'FORWARDING_TARGET_INVALID'
    assert gate_calls


def test_listener_helper_keeps_unix_targets_platform_neutral(tmp_path, monkeypatch):
    import hyprial.daemon.impl.forwarding.policy as policy
    from hyprial.daemon import managed_inbound_target_from_listener
    directory = tmp_path / 'tailnet'
    directory.mkdir(mode=0o700)
    gate_calls = []
    monkeypatch.setattr(
        policy, 'loopback_inbound_supported', lambda: gate_calls.append(True) or False
    )
    target = 'unix:' + str(directory / 'zenoh.sock')
    assert managed_inbound_target_from_listener(
        'unixsock-stream/' + str(directory / 'zenoh.sock')
    ) == target
    assert gate_calls == []


@pytest.mark.parametrize('listener', [
    'tcp:127.0.0.1:39111', 'tcp/0.0.0.0:39111', 'tcp/127.0.0.1:0',
    'unixsock-stream/relative.sock',
])
def test_listener_helper_refuses_malformed_listeners(listener):
    from hyprial.daemon import managed_inbound_target_from_listener
    with pytest.raises(ForwardingConfigurationError) as caught:
        managed_inbound_target_from_listener(listener)
    assert caught.value.code == 'FORWARDING_TARGET_INVALID'


@pytest.mark.parametrize('configured', [False, True])
@pytest.mark.parametrize('missing', ['kernel-budget', 'machine-binding'])
def test_cold_config_import_keeps_unconfigured_path_independent_of_forwarding_policy(
    tmp_path: Path, configured: bool, missing: str,
) -> None:
    environment = isolated_environment(tmp_path / 'child')
    environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[4] / 'src')
    script = f'''
from pathlib import Path
import sys
import hyprial.kernel as kernel
import hyprial.kernel.interface as kernel_interface
import hyprial.identity as identity
import hyprial.identity.interface as identity_interface
assert 'hyprial.daemon.impl.forwarding' not in sys.modules
if {missing!r} == 'kernel-budget':
    del kernel.MEMBERSHIP_BUDGETS
    del kernel_interface.MEMBERSHIP_BUDGETS
    missing_name = 'MEMBERSHIP_BUDGETS'
else:
    identity_interface._FACADE_EXPORTS.pop('MachineBinding')
    vars(identity_interface).pop('MachineBinding', None)
    vars(identity).pop('MachineBinding', None)
    missing_name = 'MachineBinding'
from hyprial.daemon.impl.forwarding_config import daemon_forwarding_environment
if {configured!r}:
    try:
        daemon_forwarding_environment(Path(sys.argv[1]), {{'HYPRIAL_FORWARDING_INBOUND_TARGET': 'unix:/not-used.sock'}}, node_id='e2e')
    except ImportError as error:
        assert missing_name in str(error)
    else:
        raise AssertionError('configured forwarding hid its missing policy dependency')
else:
    assert daemon_forwarding_environment(Path(sys.argv[1]), {{}}, node_id='e2e') == {{}}
    assert 'hyprial.daemon.impl.forwarding' not in sys.modules
'''
    child = subprocess.run(
        [sys.executable, '-c', script, str(tmp_path / 'home')], env=environment,
        text=True, capture_output=True, timeout=30, check=False,
    )
    assert child.returncode == 0, child.stderr


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
