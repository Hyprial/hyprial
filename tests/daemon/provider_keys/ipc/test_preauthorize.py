"""preauthorize: only an ipc-origin, non-agent, non-interactive caller reaches the service."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.provider_keys import api
from hyprial.daemon.impl.provider_keys.ipc import preauthorize
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.kernel import DaemonRequestError, ipc_errors

from .conftest import KEY_ID, SECRET

VALID = {
    api.USER_PROVIDER_LOGIN: {"provider": "anthropic", "kind": "api-key", "secret": SECRET},
    api.USER_PROVIDER_LIST: {},
    api.USER_PROVIDER_LOGOUT: {"keyId": KEY_ID},
    api.USER_PROVIDER_ROTATE: {"keyId": KEY_ID},
    api.AGENT_PROVIDER_ALLOW: {"agent": "worker", "provider": "anthropic"},
    api.AGENT_PROVIDER_REVOKE: {"agent": "worker", "provider": "anthropic"},
    api.AGENT_PROVIDER_STATUS: {"agent": "worker"},
}
METHODS = sorted(VALID)


def _refused(app, method, params, origin):
    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(app, method, params, trusted_message_origin=origin)
    assert caught.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    assert SECRET not in str(caught.value)
    return caught.value


@pytest.mark.parametrize("method", METHODS)
def test_operator_over_ipc_reaches_the_service(app, service, method):
    METHOD_REGISTRY.invoke(app, method, dict(VALID[method]), trusted_message_origin="ipc")
    assert len(service.calls) == 1


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("origin", [None, "daemon", "worker", "IPC", "http", ""])
def test_any_origin_other_than_ipc_is_refused(app, service, method, origin):
    _refused(app, method, dict(VALID[method]), origin)
    assert service.calls == []


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("key", ["actor", "sessionRef"])
def test_actor_or_session_param_is_refused_before_schema(app, service, method, key):
    params = {**VALID[method], key: "agent:alice:node:worker"}
    _refused(app, method, params, "ipc")
    _refused(app, method, {key: 7, "unknown": object()}, "ipc")
    assert service.calls == []


@pytest.mark.parametrize("method", METHODS)
def test_interactive_session_peer_is_refused(app, service, method, monkeypatch):
    seen = []

    def interactive(pid):
        seen.append(pid)
        return pid == 4242

    monkeypatch.setattr(app, "_interactive_identity_binding_peer", interactive)
    app._ipc_peer.pid = 4242
    _refused(app, method, dict(VALID[method]), "ipc")
    assert seen == [4242] and service.calls == []
    app._ipc_peer.pid = 4343
    METHOD_REGISTRY.invoke(app, method, dict(VALID[method]), trusted_message_origin="ipc")
    assert len(service.calls) == 1


def test_interactive_peer_detection_uses_the_real_router_check(app, service):
    """A registered interactive session whose pid and birth identity match the peer is refused."""
    from hyprial.daemon.impl.mcp.channel import ownership

    identity = ownership._read_process_identity(os.getpid())
    if identity is None:
        pytest.skip("process identity is unreadable on this platform")
    session = SimpleNamespace(process_pid=os.getpid(), process_identity=identity)
    app.desired_state = SimpleNamespace(load=lambda: SimpleNamespace(interactive_sessions=[session]))
    app._ipc_peer.pid = os.getpid()
    _refused(app, api.USER_PROVIDER_LIST, {}, "ipc")
    app.desired_state = SimpleNamespace(load=lambda: SimpleNamespace(interactive_sessions=[]))
    METHOD_REGISTRY.invoke(app, api.USER_PROVIDER_LIST, {}, trusted_message_origin="ipc")
    assert [name for name, _ in service.calls] == ["list_keys"]


def test_preauthorize_is_callable_directly_and_fails_closed_without_peer_check(tmp_path):
    from .conftest import make_app

    bare = SimpleNamespace(_ipc_peer=SimpleNamespace(pid=99))
    with pytest.raises(DaemonRequestError) as caught:
        preauthorize(api.USER_PROVIDER_LIST, bare, {}, trusted_message_origin="ipc")
    assert caught.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    preauthorize(api.USER_PROVIDER_LIST, make_app(tmp_path), {}, trusted_message_origin="ipc")


@pytest.mark.parametrize("method", METHODS)
def test_handler_reruns_preauthorize(app, service, method):
    binding = METHOD_REGISTRY.get(method)
    with pytest.raises(DaemonRequestError) as caught:
        binding.handler(app, dict(VALID[method]), trusted_message_origin="worker")
    assert caught.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    assert service.calls == []


@pytest.mark.parametrize("method", METHODS)
def test_router_refuses_worker_origin(app, service, method):
    with pytest.raises(DaemonRequestError) as caught:
        app.handle(method, dict(VALID[method]), _trusted_message_origin="worker")
    assert caught.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    assert service.calls == []
