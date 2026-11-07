"""Reject invalid host identities before opening any live GUI resources."""
import pytest

from hyprial.shell.impl.gui.live import runtime


def test_personal_squire_uses_trusted_daemon_node_not_process_hostname():
    def request(method, params):
        assert (method, params) == ('ps', {})
        return {'daemon': {
            'nodeId': 'configured-node', 'owner': 'alice@example.test', 'running': True,
        }}
    assert runtime._personal_squire_uri(request, 'alice@example.test') == (
        'agent:alice@example.test:configured-node:squire'
    )


def test_personal_squire_refuses_daemon_owner_mismatch():
    def request(method, params):
        assert (method, params) == ('ps', {})
        return {'daemon': {
            'nodeId': 'other-node', 'owner': 'bob@example.test', 'running': True,
        }}
    with pytest.raises(ValueError, match='owner'):
        runtime._personal_squire_uri(request, 'alice@example.test')


@pytest.mark.parametrize('owner', ['', ' owner ', 'owner:other'])
def test_invalid_trusted_owner_cannot_authorize_a_gui_principal(monkeypatch, tmp_path, owner):
    monkeypatch.setenv('HYPRIAL_GUI_BACKEND_DRIVER', 'daemon')
    monkeypatch.setenv('HYPRIAL_HOME', str(tmp_path))
    monkeypatch.setenv('HYPRIAL_GUI_PRINCIPAL', 'user:' + owner)
    monkeypatch.setattr(runtime, 'require_initialized_hyprial_home', lambda: tmp_path)
    monkeypatch.setattr(runtime, 'resolve_node_owner', lambda **_: owner)

    def unexpected_state_access():
        pytest.fail('invalid identity reached live state initialization')

    monkeypatch.setattr(runtime, 'configured_harness_state_dir', unexpected_state_access)
    with pytest.raises(ValueError):
        runtime.backend_from_environment()
