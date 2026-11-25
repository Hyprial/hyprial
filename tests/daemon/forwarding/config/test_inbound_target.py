"""Inbound target grammar shared by the daemon, sidecar and desktop worker."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import hyprial.daemon.impl.forwarding.policy as policy
from hyprial.daemon.impl.forwarding.membership import settings
from hyprial.daemon.impl.forwarding.policy import (
    ForwardingSidecarError,
    inbound_listener_from_target,
    inbound_target_from_listener,
    is_managed_private_inbound_listener,
    is_private_inbound_listener,
    reserve_loopback_listener,
    validate_inbound_target,
    validate_managed_inbound_target,
)


def _private_directory(tmp_path):
    directory = tmp_path / 'tailnet'
    directory.mkdir(mode=0o700)
    return directory


def test_loopback_target_and_listener_round_trip():
    target = 'tcp:127.0.0.1:39111'
    assert validate_inbound_target(target) == target
    assert inbound_listener_from_target(target) == 'tcp/127.0.0.1:39111'
    assert inbound_target_from_listener('tcp/127.0.0.1:39111') == target
    assert is_private_inbound_listener('tcp/127.0.0.1:39111')


def test_unix_target_and_listener_round_trip(tmp_path):
    target = 'unix:' + str(_private_directory(tmp_path) / 'zenoh.sock')
    assert validate_inbound_target(target) == target
    listener = inbound_listener_from_target(target)
    assert listener == 'unixsock-stream/' + target.removeprefix('unix:')
    assert inbound_target_from_listener(listener) == target
    assert is_private_inbound_listener(listener)


@pytest.mark.parametrize('target', [
    '127.0.0.1:39111', 'tcp:0.0.0.0:39111', 'tcp:192.168.1.2:39111',
    'tcp:127.0.0.2:39111', 'tcp:[::1]:39111', 'tcp:localhost:39111',
    'tcp:127.0.0.1:0', 'tcp:127.0.0.1:65536', 'tcp:127.0.0.1',
    'unix:relative.sock', '',
])
def test_unsafe_or_malformed_inbound_target_is_refused(target):
    with pytest.raises(ForwardingSidecarError):
        validate_inbound_target(target)
    assert not is_private_inbound_listener(target)


def test_listener_syntax_is_not_accepted_as_a_target():
    listener = 'tcp/127.0.0.1:39111'
    with pytest.raises(ForwardingSidecarError):
        validate_inbound_target(listener)
    assert is_private_inbound_listener(listener)


def test_managed_gate_refuses_loopback_tcp_off_windows(monkeypatch):
    gate_calls = []
    monkeypatch.setattr(
        policy, 'loopback_inbound_supported', lambda: gate_calls.append(True) or False
    )
    with pytest.raises(ForwardingSidecarError, match='only supported on Windows'):
        validate_managed_inbound_target('tcp:127.0.0.1:39111')
    assert not is_managed_private_inbound_listener('tcp/127.0.0.1:39111')
    assert validate_inbound_target('tcp:127.0.0.1:39111') == 'tcp:127.0.0.1:39111'
    assert len(gate_calls) == 2


def test_managed_gate_accepts_loopback_tcp_on_windows(monkeypatch):
    gate_calls = []
    monkeypatch.setattr(
        policy, 'loopback_inbound_supported', lambda: gate_calls.append(True) or True
    )
    assert validate_managed_inbound_target('tcp:127.0.0.1:39111') == 'tcp:127.0.0.1:39111'
    assert is_managed_private_inbound_listener('tcp/127.0.0.1:39111')
    assert len(gate_calls) == 2


def test_managed_gate_keeps_unix_targets_platform_neutral(tmp_path, monkeypatch):
    target = 'unix:' + str(_private_directory(tmp_path) / 'zenoh.sock')
    gate_calls = []
    monkeypatch.setattr(
        policy, 'loopback_inbound_supported', lambda: gate_calls.append(True) or False
    )
    assert validate_managed_inbound_target(target) == target
    assert is_managed_private_inbound_listener(inbound_listener_from_target(target))
    assert gate_calls == []


@pytest.mark.parametrize('listener', [
    'tcp/0.0.0.0:39111', 'tcp/127.0.0.2:39111', 'tcp/[::1]:39111',
    'tcp/localhost:39111', 'tcp/127.0.0.1:0', 'unixsock-stream/relative.sock',
    'unix:relative.sock', '127.0.0.1:39111',
])
def test_non_private_listeners_are_refused(listener):
    assert not is_private_inbound_listener(listener)
    with pytest.raises(ForwardingSidecarError):
        inbound_target_from_listener(listener)


def test_reserved_loopback_listener_uses_a_strict_tcp_port():
    listener = reserve_loopback_listener()
    prefix = 'tcp/127.0.0.1:'
    assert listener.startswith(prefix)
    port = listener.removeprefix(prefix)
    assert port.isdigit() and 1 <= int(port) <= 65535
    assert validate_inbound_target(inbound_target_from_listener(listener)) == (
        'tcp:127.0.0.1:' + port
    )


def test_private_inbound_is_private_unix_off_windows(tmp_path, monkeypatch):
    capability_calls = []
    monkeypatch.setattr(
        settings, 'loopback_inbound_supported',
        lambda: capability_calls.append(True) or False,
    )
    target = settings.private_inbound(tmp_path)
    assert target == 'unix:' + str(tmp_path / 'tailnet' / 'zenoh.sock')
    assert (tmp_path / 'tailnet').is_dir()
    assert capability_calls == [True]


def test_private_inbound_is_a_loopback_pair_on_windows(tmp_path, monkeypatch):
    capability_calls = []
    monkeypatch.setattr(
        settings, 'loopback_inbound_supported',
        lambda: capability_calls.append(True) or True,
    )
    target = settings.private_inbound(tmp_path)
    assert target.startswith('tcp:127.0.0.1:')
    assert (tmp_path / 'tailnet').is_dir()
    listener = inbound_listener_from_target(target)
    assert listener.startswith('tcp/127.0.0.1:')
    assert listener.removeprefix('tcp/') == target.removeprefix('tcp:')
    assert capability_calls == [True]


@pytest.mark.parametrize('listener', [
    'tcp/127.0.0.1:39111', 'tcp/0.0.0.0:39111', 'tcp/192.168.1.2:39111',
    'unixsock-stream/relative.sock',
])
def test_upper_layers_trust_only_managed_private_inbound(tmp_path, listener):
    app = SimpleNamespace(
        _forwarding_environment={'HYPRIAL_FORWARDING_UP': '{}'},
        zenoh_listen=(listener,),
        hyprial_home=tmp_path,
    )
    assert settings.trust_upper_layers(app) is False


def test_upper_layers_tcp_is_platform_gated(tmp_path, monkeypatch):
    app = SimpleNamespace(
        _forwarding_environment={'HYPRIAL_FORWARDING_UP': '{}'},
        zenoh_listen=('tcp/127.0.0.1:39111',),
        hyprial_home=tmp_path,
    )
    gate_calls = []
    monkeypatch.setattr(
        policy, 'loopback_inbound_supported', lambda: gate_calls.append(True) or False
    )
    assert settings.trust_upper_layers(app) is False
    assert is_managed_private_inbound_listener('tcp/127.0.0.1:39111') is False
    assert is_private_inbound_listener('tcp/127.0.0.1:39111') is True
    monkeypatch.setattr(
        policy, 'loopback_inbound_supported', lambda: gate_calls.append(True) or True
    )
    assert is_managed_private_inbound_listener('tcp/127.0.0.1:39111') is True
    assert settings.trust_upper_layers(app) is False
    assert gate_calls


def test_durable_json_write_flushes_the_directory_through_the_kernel_seam(tmp_path, monkeypatch):
    flushed = []
    monkeypatch.setattr(settings, 'fsync_directory', lambda path: flushed.append(path) or True)
    path = tmp_path / 'state' / 'record.json'
    path.parent.mkdir()
    settings.durable_json_write(path, {'ok': True})
    assert path.exists()
    assert flushed == [path.parent]
