"""Synthetic declaration eligibility over a real isolated daemon and Uvicorn.

The synthetic declarations are injected into both subprocesses with a
PYTHONPATH ``sitecustomize`` hook.  This keeps the product registry untouched
while exercising the real daemon IPC and the real HTTP entry.  No business
method name is special-cased.
"""

from __future__ import annotations

from pathlib import Path
import textwrap

from hyprial.kernel import daemon_request_once

from tests.integration.request_http.conftest import (
    ACTOR,
    DaemonProcess,
    Isolation,
    running_http,
)

OTHER = "agent:h01-owner:h01-node:other"
PROBE_NAMES = {
    "synthetic.echo",
    "synthetic.internal",
    "synthetic.hidden",
    "synthetic.write",
}

_PROBE = textwrap.dedent(
    '''
    """Test-only synthetic declarations; no business handler is copied."""
    from __future__ import annotations

    from hyprial.kernel import IdempotencySpec, MethodError, MethodSpec, ipc_errors

    _PARAMS = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "additionalProperties": False,
    }
    _RESULT = {
        "type": "object",
        "properties": {
            "ok": {"const": True},
            "value": {"type": "string"},
            "seen": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["ok", "value", "seen"],
        "additionalProperties": False,
    }
    _ERRORS = (MethodError(ipc_errors.DAEMON_ERROR),)


    def _echo(app, params, **_kwargs):
        return {
            "ok": True,
            "value": str(params.get("value", "")),
            "seen": sorted(str(key) for key in params),
        }


    def _spec(name, owner, audience, *, exposed, mutates=False):
        return MethodSpec(
            name=name,
            owner=owner,
            params_schema=_PARAMS,
            result_schema=_RESULT,
            errors=_ERRORS,
            mode="unary",
            mutates=mutates,
            retry="never" if mutates else "safe",
            audience=audience,
            http_exposed=exposed,
            http_binding=None,
            confirmation="none",
            idempotency=IdempotencySpec(mode="never" if mutates else "safe"),
        )


    SYNTHETIC_BINDINGS = (
        (_spec("synthetic.echo", "synthetic.probe", ("local-operator",), exposed=True), _echo),
        (_spec("synthetic.internal", "synthetic.probe", ("internal",), exposed=True), _echo),
        (_spec("synthetic.hidden", "synthetic.probe", ("local-operator",), exposed=False), _echo),
        (
            _spec(
                "synthetic.write",
                "synthetic.probe",
                ("local-operator",),
                exposed=True,
                mutates=True,
            ),
            _echo,
        ),
    )
    '''
).lstrip()

_SITECUSTOMIZE = textwrap.dedent(
    '''
    """Extend the frozen registry only inside the synthetic test subprocesses."""
    from hyprial.daemon.impl.request_catalog import registry as _registry
    import hyprial.daemon.impl.request_catalog as _package
    from synthetic_probe import SYNTHETIC_BINDINGS


    def _extended():
        existing = [
            (
                binding.spec,
                binding.handler,
                binding.param_error,
            )
            for name in _registry.METHOD_REGISTRY.names
            for binding in (_registry.METHOD_REGISTRY.get(name),)
        ]
        return _registry.MethodRegistry.from_bindings(
            (*existing, *SYNTHETIC_BINDINGS)
        )


    _REGISTRY = _extended()
    _registry.METHOD_REGISTRY = _REGISTRY
    _registry.BUILD_REGISTRY = _extended
    _package.METHOD_REGISTRY = _REGISTRY
    '''
).lstrip()


def _write_probe(root: Path) -> Path:
    root.mkdir(mode=0o700)
    (root / "synthetic_probe.py").write_text(_PROBE, encoding="utf-8")
    (root / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
    return root


def test_declaration_only_synthetic_read_is_callable_over_real_http(
    isolation: Isolation, tmp_path: Path
) -> None:
    probe = _write_probe(tmp_path / "synthetic")
    daemon = DaemonProcess(isolation, pythonpath=probe)
    daemon.start()
    try:
        declared = daemon_request_once(
            "daemon.catalog", {}, socket_path=isolation.socket_path, timeout=5
        )
        assert PROBE_NAMES <= {item["name"] for item in declared["methods"]}

        with running_http(isolation, pythonpath=probe) as http:
            status, _, catalog = http.request("GET", "/daemon/request")
            assert status == 200, catalog
            methods = {item["name"]: item for item in catalog["methods"]}
            echo = methods["synthetic.echo"]
            default_binding = {
                "identity": {"mode": "none", "param": None},
                "readiness": None,
            }
            assert echo["state"] == "available"
            assert echo["httpBinding"] == default_binding
            assert echo["paramsSchema"]["additionalProperties"] is False
            assert set(methods).isdisjoint(
                {"synthetic.internal", "synthetic.hidden", "synthetic.write"}
            )

            status, _, payload = http.request(
                "POST", "/daemon/request/synthetic.echo", body={"value": "hello"}
            )
            assert status == 200, payload
            assert payload["dispatch"] == "dispatched"
            assert payload["result"] == {
                "ok": True,
                "value": "hello",
                "seen": ["value"],
            }

            # No selector is inferred from a caller-looking body field: the
            # declared schema rejects extras before any daemon dispatch.
            for extra in ("actor", "from", "caller"):
                status, _, payload = http.request(
                    "POST",
                    "/daemon/request/synthetic.echo",
                    body={"value": "x", extra: OTHER},
                )
                assert (status, payload["code"], payload["dispatch"]) == (
                    400,
                    "INVALID_ARGUMENT",
                    "not-dispatched",
                )

            # Restricted audience, unexposed and write-profile declarations
            # are visible to the daemon but never reach an H-01 route.
            for name in ("synthetic.internal", "synthetic.hidden", "synthetic.write"):
                status, _, payload = http.request(
                    "POST", f"/daemon/request/{name}", body={}
                )
                assert (status, payload["code"]) == (404, "METHOD_NOT_FOUND")

            # Explicit selector binding keeps its strict principal check.
            status, _, payload = http.request(
                "POST", "/daemon/request/link.list", body={"from": OTHER}
            )
            assert (status, payload["code"], payload["dispatch"]) == (
                403,
                "PRINCIPAL_MISMATCH",
                "not-dispatched",
            )
            status, _, payload = http.request(
                "POST", "/daemon/request/link.list", body={}
            )
            assert status == 200 and payload["result"]["actor"] == ACTOR

            status, _, document = http.request("GET", "/openapi.json")
            assert status == 200
            operation = document["paths"]["/daemon/request/synthetic.echo"]["post"]
            assert operation["x-hyprial-http-binding"] == default_binding
            assert (
                operation["requestBody"]["content"]["application/json"]["schema"]
                == echo["paramsSchema"]
            )
            assert (
                operation["responses"]["200"]["content"]["application/json"][
                    "schema"
                ]["properties"]["result"]
                == echo["resultSchema"]
            )
            assert "/docs" not in document["paths"]
    finally:
        daemon.stop()
