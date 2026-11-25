"""Real isolated HTTP/IPC identity composition with test-only registration.

The public registry remains owned by the common line. This composition proves
the real user registry, host selector injection, and HTTP refusal paths; it
does not claim the ordinary product entry has enabled the identity methods.
"""

from __future__ import annotations

import textwrap

from tests.integration.request_http.conftest import (
    DaemonProcess,
    running_http,
)

_REGISTRATION = '''
from hyprial.daemon.impl.request_catalog import registry as registry
import hyprial.daemon.impl.request_catalog as package
from hyprial.daemon.impl.request_catalog.identity import IDENTITY_BINDINGS

def build():
    names = {spec.name for spec, _ in IDENTITY_BINDINGS}
    existing = [(binding.spec, binding.handler, binding.param_error)
                for name in registry.METHOD_REGISTRY.names if name not in names
                for binding in (registry.METHOD_REGISTRY.get(name),)]
    return registry.MethodRegistry.from_bindings((*existing, *IDENTITY_BINDINGS))

registry.METHOD_REGISTRY = build()
registry.BUILD_REGISTRY = build
package.METHOD_REGISTRY = registry.METHOD_REGISTRY
'''


def _registration(tmp_path):
    path = tmp_path / "registration"
    path.mkdir()
    (path / "sitecustomize.py").write_text(
        textwrap.dedent(_REGISTRATION), encoding="utf-8"
    )
    return path


def _post(http, method, body, *, token=None):
    return http.request(
        "POST", "/daemon/request/" + method, body=body, token=token
    )


def _started(isolation, registration):
    daemon = DaemonProcess(isolation, pythonpath=registration)
    daemon.start()
    return daemon


def test_identity_http_chain_uses_real_registry_and_host_selector(isolation, tmp_path):
    registration = _registration(tmp_path)
    daemon = _started(isolation, registration)
    try:
        with running_http(isolation, pythonpath=registration) as http:
            status, _, payload = _post(
                http,
                "identity.users.add",
                {"kind": "guest", "displayName": "HTTP Guest"},
            )
            assert status == 200, payload
            assert payload["dispatch"] == "dispatched"
            created = payload["result"]["user"]
            key = created["userKey"]
            assert (created["kind"], created["displayName"]) == (
                "guest",
                "HTTP Guest",
            )

            status, _, payload = _post(
                http, "identity.users.list", {"kind": "guest"}
            )
            assert status == 200, payload
            assert payload["dispatch"] == "dispatched"
            assert any(
                row["userKey"] == key for row in payload["result"]["users"]
            )

    finally:
        daemon.stop()


def test_browser_supplied_confirmed_by_is_refused_before_dispatch(
    isolation, tmp_path
):
    registration = _registration(tmp_path)
    daemon = _started(isolation, registration)
    try:
        with running_http(isolation, pythonpath=registration) as http:
            status, _, payload = _post(
                http,
                "identity.users.add",
                {
                    "kind": "guest",
                    "displayName": "Forged Guest",
                    "confirmedBy": "user:forged",
                },
            )
            assert (status, payload["code"], payload["dispatch"]) == (
                400,
                "INVALID_ARGUMENT",
                "not-dispatched",
            )

            status, _, payload = _post(
                http, "identity.users.list", {"kind": "guest"}
            )
            assert status == 200, payload
            assert all(
                row["displayName"] != "Forged Guest"
                for row in payload["result"]["users"]
            )

    finally:
        daemon.stop()


def test_wrong_bearer_is_rejected_before_any_dispatch(isolation, tmp_path):
    registration = _registration(tmp_path)
    daemon = _started(isolation, registration)
    try:
        with running_http(isolation, pythonpath=registration) as http:
            status, _, payload = _post(
                http,
                "identity.users.list",
                {"kind": "guest"},
                token="wrong-test-token",
            )
            assert (status, payload["code"], payload["dispatch"]) == (
                401,
                "HTTP_UNAUTHORIZED",
                "not-dispatched",
            )

            status, _, payload = _post(
                http,
                "identity.users.add",
                {"kind": "guest", "displayName": "Wrong Bearer Guest"},
                token="wrong-test-token",
            )
            assert (status, payload["code"], payload["dispatch"]) == (
                401,
                "HTTP_UNAUTHORIZED",
                "not-dispatched",
            )

            status, _, payload = _post(
                http, "identity.users.list", {"kind": "guest"}
            )
            assert status == 200, payload
            assert all(
                row["displayName"] != "Wrong Bearer Guest"
                for row in payload["result"]["users"]
            )
    finally:
        daemon.stop()
