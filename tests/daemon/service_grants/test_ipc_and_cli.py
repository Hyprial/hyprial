"""Red tests for ``service.grant/revoke/grants`` IPC routing and CLI surface.

Decision (service grant S3, 2026-11-26): exercises the real ``METHOD_REGISTRY``
and ``_IpcDispatchMixin.handle`` dispatch path (unknown fields -> ``INVALID_ARGUMENT``,
audience/http_exposed contract) plus ``hyprial org service grant|revoke|grants`` CLI.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.forwarding.store import ExposureStore
from hyprial.daemon.impl.ipc.router import _IpcDispatchMixin
from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.identity import DeviceRecord, DirectoryDevice, write_device_record
from hyprial.kernel import DaemonRequestError, RequestPortError, ipc_errors
from hyprial.shell.impl.cli.commands.service import commands, grants as cli_grants

ORG = "group-acme"
GRANT_ID = "sg.abcdefghijklmnopqrstuvwxyz"


class _RouterApp(_IpcDispatchMixin):
    def __init__(self, tmp_path: Path, runtime: OrgFsRuntime, exposures: ExposureStore) -> None:
        self.hyprial_home = tmp_path / "home"
        self.hyprial_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_dir = tmp_path / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.owner = "alice"
        self.node_id = "device-a"
        self._restore_done = threading.Event()
        self._restore_done.set()
        self._runtime = runtime
        self._exposure_store = exposures
        self._inbox = None
        self._harnesses = None
        self._presence = None
        self._ipc_peer = None
        self.events: list[tuple[str, str, str, dict[str, Any]]] = []
        write_device_record(
            self.hyprial_home,
            DeviceRecord(
                device_id="device-a",
                owner="alice",
                server_public="nodekey:server-device-a",
                client_public="nodekey:client-device-a",
                key_generation=1,
            ),
        )

    def _require_orgfs_runtime(self) -> OrgFsRuntime:
        return self._runtime

    def _log(self, level: str, component: str, event: str, **fields: Any) -> None:
        self.events.append((level, component, event, fields))


class _FakeCliServices:
    CliError = RequestPortError

    def __init__(self, response: Any) -> None:
        self.response = response
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.runtime = SimpleNamespace(
            request=self._request,
            execute=self._execute,
        )

    def _request(
        self, method: str, params: dict[str, object] | None = None, **_: object
    ) -> Any:
        self.requests.append((method, params or {}))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response

    def _execute(self, operation: Any, *, json_output: bool, **_: object) -> None:
        from hyprial.shell.impl.cli.output import run_command

        run_command(operation, json_output=json_output, require_home=None)


def _invoke_cli(monkeypatch: pytest.MonkeyPatch, response: Any, args: list[str]):
    services = _FakeCliServices(response)
    monkeypatch.setattr(commands, "get_services", lambda: services)
    monkeypatch.setattr(cli_grants, "get_services", lambda: services)
    result = CliRunner().invoke(commands.service_app, args)
    return result, services


def _json_out(result) -> dict[str, Any]:
    assert result.stdout, result.stderr
    data = json.loads(result.stdout)
    assert isinstance(data, dict)
    return data


def test_ipc_specs_have_local_operator_audience_and_http_disabled() -> None:
    """All three new methods are local-operator only and not HTTP-exposed."""

    for name in ("service.grant", "service.revoke", "service.grants"):
        assert name in METHOD_REGISTRY
        binding = METHOD_REGISTRY.get(name)
        assert binding is not None
        spec = binding.spec
        assert spec.audience == ("local-operator",)
        assert spec.http_exposed is False


def test_ipc_router_rejects_unknown_fields_and_plaintext_secret_params(
    tmp_path: Path,
) -> None:
    """Routing unknown keys or plaintext secret fields returns ``INVALID_ARGUMENT``."""

    runtime = OrgFsRuntime(
        tmp_path / "orgfs", node_id="device-a", author="user:alice"
    )
    try:
        store = OrgFsDirectoryStore(runtime.facade)
        space = runtime.facade.create_space(ORG)
        runtime.facade.invite(space.space_id, "user:bob", "rw")
        store.ensure_org_directory_roots(ORG)
        exposures = ExposureStore(tmp_path / "exposures.json")
        exposures.set(
            {"port": 8447, "target": "tcp:127.0.0.1:8447", "proxyProtocol": "none"}
        )
        app = _RouterApp(tmp_path, runtime, exposures)

        bad_calls = [
            (
                "service.grant",
                {"org": ORG, "port": 8447, "device": "@members", "extra": True},
            ),
            (
                "service.grant",
                {
                    "org": ORG,
                    "port": 8447,
                    "device": "bob/device-b",
                    "credential": {"entryId": "gw", "secret": "plaintext-nope"},
                },
            ),
            ("service.revoke", {"org": ORG, "grantId": GRANT_ID, "unknown": 1}),
            ("service.grants", {"org": ORG, "unexpected": "x"}),
        ]
        for method, params in bad_calls:
            with pytest.raises(DaemonRequestError) as caught:
                app.handle(method, params)
            assert caught.value.code == ipc_errors.INVALID_ARGUMENT
    finally:
        runtime.close()


def test_ipc_router_end_to_end_grant_list_revoke(tmp_path: Path) -> None:
    """``service.grant`` -> ``service.grants`` -> ``service.revoke`` work through the IPC router."""

    runtime = OrgFsRuntime(
        tmp_path / "orgfs", node_id="device-a", author="user:alice"
    )
    try:
        store = OrgFsDirectoryStore(runtime.facade)
        space = runtime.facade.create_space(ORG)
        runtime.facade.invite(space.space_id, "user:bob", "rw")
        store.ensure_org_directory_roots(ORG)
        store.put_device(
            ORG,
            DirectoryDevice(
                device_id="device-a",
                owner="alice",
                server_public="nodekey:server-device-a",
                client_public="nodekey:client-device-a",
                address="tcp://127.0.0.1:7447#a-psk",
                key_generation=1,
                updated_at="2026-11-26T00:00:00+00:00",
            ),
        )
        exposures = ExposureStore(tmp_path / "exposures.json")
        exposures.set(
            {"port": 8447, "target": "tcp:127.0.0.1:8447", "proxyProtocol": "none"}
        )
        app = _RouterApp(tmp_path, runtime, exposures)

        created = app.handle(
            "service.grant",
            {"org": ORG, "port": 8447, "device": "@members", "name": "gateway", "protocol": "http"},
        )
        grant_id = created["grant"]["grantId"]

        listed = app.handle("service.grants", {"org": ORG, "port": 8447})
        assert [item["grantId"] for item in listed["granted"]] == [grant_id]
        assert listed["received"] == []

        revoked = app.handle("service.revoke", {"org": ORG, "grantId": grant_id})
        assert revoked == {"grantId": grant_id, "revoked": True}

        after_default = app.handle("service.grants", {"org": ORG})
        assert after_default["granted"] == []
        after_all = app.handle(
            "service.grants", {"org": ORG, "includeRevoked": True}
        )
        assert [item["grantId"] for item in after_all["granted"]] == [grant_id]
    finally:
        runtime.close()


def test_cli_grant_revoke_grants_rpc_shapes_and_no_plaintext_secret_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI builds the frozen RPC params and never exposes plaintext key flags."""

    sample_grant = {
        "org": ORG,
        "grantor": "alice",
        "schemaVersion": 1,
        "grantId": GRANT_ID,
        "service": {
            "deviceId": "device-a",
            "port": 8447,
            "name": "magpie",
            "protocol": "http",
            "note": None,
        },
        "recipient": {"owner": "bob", "deviceId": "device-b"},
        "grantedAtMs": 1_700_000_000_000,
        "expiresAtMs": None,
        "revokedAtMs": None,
        "hasCredential": True,
        "effective": True,
    }
    res, services = _invoke_cli(
        monkeypatch,
        {"grant": sample_grant},
        [
            "grant",
            "8447",
            "--org",
            ORG,
            "--device",
            "bob/device-b",
            "--name",
            "magpie",
            "--protocol",
            "http",
            "--credential",
            "magpie:API_KEY",
            "--json",
        ],
    )
    assert res.exit_code == 0, res.stderr
    assert _json_out(res) == {"ok": True, "grant": sample_grant}
    assert services.requests == [
        (
            "service.grant",
            {
                "org": ORG,
                "port": 8447,
                "device": "bob/device-b",
                "name": "magpie",
                "protocol": "http",
                "credential": {"entryId": "magpie", "fieldName": "API_KEY"},
            },
        )
    ]

    for forbidden_flag in ("--secret", "--key", "--value"):
        bad, bad_services = _invoke_cli(
            monkeypatch,
            {},
            ["grant", "8447", "--org", ORG, "--device", "bob/device-b", forbidden_flag, "plain"],
        )
        assert bad.exit_code == 2
        assert bad_services.requests == []

    bare_cred, bare_cred_services = _invoke_cli(
        monkeypatch,
        {"grant": sample_grant},
        [
            "grant",
            "8447",
            "--org",
            ORG,
            "--device",
            "bob/device-b",
            "--credential",
            "magpie",
            "--json",
        ],
    )
    assert bare_cred.exit_code == 0, bare_cred.stderr
    assert bare_cred_services.requests == [
        (
            "service.grant",
            {
                "org": ORG,
                "port": 8447,
                "device": "bob/device-b",
                "credential": {"entryId": "magpie"},
            },
        )
    ]

    for malformed in ("", ":API_KEY", "magpie:", "magpie:a:b"):
        bad_cred, bad_cred_services = _invoke_cli(
            monkeypatch,
            {},
            [
                "grant",
                "8447",
                "--org",
                ORG,
                "--device",
                "bob/device-b",
                "--credential",
                malformed,
                "--json",
            ],
        )
        assert bad_cred.exit_code == 1
        assert _json_out(bad_cred)["code"] == "INVALID_ARGUMENT"
        assert bad_cred_services.requests == []

    rev, rev_services = _invoke_cli(
        monkeypatch,
        {"grantId": GRANT_ID, "revoked": True},
        ["revoke", GRANT_ID, "--org", ORG, "--json"],
    )
    assert rev.exit_code == 0, rev.stderr
    assert _json_out(rev) == {"ok": True, "grantId": GRANT_ID, "revoked": True}
    assert rev_services.requests == [
        ("service.revoke", {"org": ORG, "grantId": GRANT_ID})
    ]

    listed, listed_services = _invoke_cli(
        monkeypatch,
        {"granted": [sample_grant], "received": []},
        ["grants", "--org", ORG, "--port", "8447", "--include-revoked", "--json"],
    )
    assert listed.exit_code == 0, listed.stderr
    assert _json_out(listed) == {"ok": True, "granted": [sample_grant], "received": []}
    assert listed_services.requests == [
        ("service.grants", {"org": ORG, "port": 8447, "includeRevoked": True})
    ]
