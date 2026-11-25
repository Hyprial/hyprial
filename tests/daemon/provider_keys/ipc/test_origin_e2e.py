"""F-9 proof over a real isolated daemon socket and a real request-http process.

End to end (real daemon subprocess, real Unix socket, real Uvicorn shell):
- an HTTP-forwarded request reaches the daemon with origin "ipc" and passes the
  provider-key preauthorize (the request-http process is a non-interactive peer);
- a plain IPC frame for user.provider.list passes preauthorize and stops at the
  closed default gate with PROVIDER_STORE_UNAVAILABLE;
- an IPC frame carrying actor is refused with CALLER_NOT_AUTHORIZED;
- worker.channel.request cannot relay any provider-key method: the worker-channel
  relay allowlist answers METHOD_NOT_FOUND before any provider-key code runs;
- the seven methods are HTTP-routed; a forwarded request reaches the daemon with
  origin "ipc" and passes the provider-key preauthorize (the request-http process
  is a non-interactive peer).
The probe declaration is injected only into the subprocesses with a sitecustomize
hook and is bound with the production provider-key preauthorize.

Unit level only (tests/daemon/provider_keys/ipc/test_preauthorize.py), not proven here:
- preauthorize refusing origin "worker" (test_router_refuses_worker_origin,
  test_any_origin_other_than_ipc_is_refused); the worker relay loop above stops at
  the allowlist, so it would still pass if preauthorize admitted "worker";
- refusal of an interactive-session peer (test_interactive_session_peer_is_refused,
  test_interactive_peer_detection_uses_the_real_router_check), which uses the real
  router check but no real interactive socket peer.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from hyprial.daemon.impl.provider_keys import api
from hyprial.kernel import daemon_request_once, ipc_errors

from tests.integration.request_http.conftest import (  # noqa: F401
    ACTOR,
    DaemonProcess,
    Isolation,
    isolation,
    running_http,
)

PROBE = "synthetic.pkOriginProbe"

_SITECUSTOMIZE = textwrap.dedent(
    '''
    """Extend the frozen registry with one origin probe inside the test subprocesses."""
    from functools import partial

    from hyprial.daemon.impl.provider_keys.ipc import preauthorize
    from hyprial.daemon.impl.request_catalog import registry as _registry
    import hyprial.daemon.impl.request_catalog as _package
    from hyprial.kernel import IdempotencySpec, MethodError, MethodSpec, ipc_errors

    _NAME = "synthetic.pkOriginProbe"
    _SPEC = MethodSpec(
        name=_NAME,
        owner="synthetic.probe",
        params_schema={"type": "object", "properties": {}, "additionalProperties": False},
        result_schema={
            "type": "object",
            "properties": {"origin": {"type": "string"}},
            "required": ["origin"],
            "additionalProperties": False,
        },
        errors=(MethodError(ipc_errors.CALLER_NOT_AUTHORIZED, description="refused"),),
        mode="unary",
        mutates=False,
        retry="safe",
        audience=("local-operator",),
        http_exposed=True,
        confirmation="none",
        idempotency=IdempotencySpec(mode="safe"),
    )


    def _origin(app, params, *, trusted_message_origin=None):
        preauthorize(_NAME, app, params, trusted_message_origin=trusted_message_origin)
        return {"origin": str(trusted_message_origin)}


    def _extended():
        existing = [_registry.METHOD_REGISTRY.get(name) for name in _registry.METHOD_REGISTRY.names]
        probe = _registry.BoundMethod(_SPEC, _origin, preauthorize=partial(preauthorize, _NAME))
        return _registry.MethodRegistry.from_bindings((*existing, probe))


    _REGISTRY = _extended()
    _registry.METHOD_REGISTRY = _REGISTRY
    _registry.BUILD_REGISTRY = _extended
    _package.METHOD_REGISTRY = _REGISTRY
    '''
).lstrip()


def _code(socket_path: Path, method: str, params: dict) -> str:
    with pytest.raises(Exception) as caught:
        daemon_request_once(method, params, socket_path=socket_path, timeout=10)
    return str(getattr(caught.value, "code", type(caught.value).__name__))


def test_http_forwarded_requests_arrive_as_ipc_and_workers_cannot_reach_provider_methods(
    isolation: Isolation, tmp_path: Path,  # noqa: F811
) -> None:
    probe_root = tmp_path / "probe"
    probe_root.mkdir(mode=0o700)
    (probe_root / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
    daemon = DaemonProcess(isolation, pythonpath=probe_root)
    daemon.start()
    try:
        catalog = daemon_request_once(
            "daemon.catalog", {}, socket_path=isolation.socket_path, timeout=5
        )
        names = {item["name"] for item in catalog["methods"]}
        assert PROBE in names and api.PROVIDER_KEY_METHODS <= names

        assert daemon_request_once(
            PROBE, {}, socket_path=isolation.socket_path, timeout=5
        ) == {"origin": "ipc"}
        assert _code(isolation.socket_path, api.USER_PROVIDER_LIST, {}) == \
            ipc_errors.PROVIDER_STORE_UNAVAILABLE
        for key in ("actor", "sessionRef"):
            assert _code(isolation.socket_path, api.USER_PROVIDER_LIST, {key: ACTOR}) == \
                ipc_errors.CALLER_NOT_AUTHORIZED
            assert _code(isolation.socket_path, PROBE, {key: ACTOR}) == ipc_errors.CALLER_NOT_AUTHORIZED
        assert _code(isolation.socket_path, "user.provider.import", {}) == ipc_errors.METHOD_NOT_FOUND

        for inner in (api.USER_PROVIDER_LIST, api.AGENT_PROVIDER_STATUS, PROBE):
            envelope = {
                "actor": "agent:h01-owner:h01-node:worker", "sessionRef": "synthetic-session", "daemonEpoch": "0",
                "request": {"version": 1, "id": "w1", "method": inner, "params": {}},
            }
            assert _code(isolation.socket_path, "worker.channel.request", envelope) == \
                ipc_errors.METHOD_NOT_FOUND

        with running_http(isolation, pythonpath=probe_root) as http:
            status, _, payload = http.request("POST", f"/daemon/request/{PROBE}", body={})
            assert status == 200, payload
            assert payload["dispatch"] == "dispatched"
            assert payload["result"] == {"origin": "ipc"}

            status, _, payload = http.request(
                "POST", f"/daemon/request/{PROBE}", body={"actor": ACTOR}
            )
            assert (status, payload["code"], payload["dispatch"]) == (
                400, ipc_errors.INVALID_ARGUMENT, "not-dispatched",
            )

            status, _, catalog = http.request("GET", "/daemon/request")
            assert status == 200, catalog
            routed = {item["name"] for item in catalog["methods"]}
            assert PROBE in routed
            assert api.PROVIDER_KEY_METHODS <= routed
            status, _, payload = http.request(
                "POST", f"/daemon/request/{api.USER_PROVIDER_LIST}", body={}
            )
            assert payload["code"] == ipc_errors.PROVIDER_STORE_UNAVAILABLE, (status, payload)
            for name in sorted(api.PROVIDER_KEY_METHODS - {api.USER_PROVIDER_LIST}):
                status, _, payload = http.request("POST", f"/daemon/request/{name}", body={})
                assert payload["code"] == ipc_errors.INVALID_ARGUMENT, (name, status, payload)
    finally:
        daemon.stop()
