"""Batch 1: org.execute / leave / remove / delete declarations.

These four names are the member-lifecycle half of the remaining org methods.
Every handler forwards to the real IPC entry; ``org.delete`` is the OC7
two-step deletion (preview, then confirmation of its ``operationId``) and
declares the authority, journal and reachable sidecar codes without data.
"""

from __future__ import annotations

from typing import Any

import pytest

from jsonschema import Draft202012Validator

from hyprial.daemon.impl.request_catalog.org import ORG_BINDINGS
from hyprial.daemon.impl.request_catalog.registry import MethodRegistry
from hyprial.kernel import DaemonRequestError, ipc_errors

_EXPECTED = (
    "hosts",
    "org.acl.grant",
    "org.acl.ls",
    "org.acl.revoke",
    "org.create",
    "org.delete",
    "org.execute",
    "org.fetch",
    "org.invite",
    "org.invites.accept",
    "org.invites.list",
    "org.join",
    "org.leave",
    "org.list",
    "org.network",
    "org.publish",
    "org.remove",
    "org.repair",
    "org.resolve",
    "org.use",
    "orgfs.checkout",
    "orgfs.create",
    "orgfs.export",
    "orgfs.import",
    "orgfs.invite",
    "orgfs.join",
    "orgfs.ls",
    "orgfs.members",
    "orgfs.mkdir",
    "orgfs.move",
    "orgfs.read",
    "orgfs.remove",
    "orgfs.remove_member",
    "orgfs.resolve",
    "orgfs.restore",
    "orgfs.space_purge",
    "orgfs.spaces",
    "orgfs.stat",
    "orgfs.status",
    "orgfs.trash",
    "orgfs.watch",
    "orgfs.write",
    "top.inspect",
    "top.snapshot",
)

_RESULTS = {
    "org.execute": {"org": "group-x", "spaceId": "space-x", "steps": []},
    "org.leave": {"org": "group-x", "spaceId": "space-x"},
    "org.remove": {"org": "group-x", "spaceId": "space-x"},
    "org.delete": {
        "org": "group-x",
        "spaceId": "space-x",
        "operationId": "op-1",
        "phase": "pending-confirmation",
        "deviceIds": [],
        "confirmed": False,
        "irreversible": True,
        "migrationDoc": "docs/guides/migration-oc7-org-lifecycle.md",
        "orgfsMarked": False,
        "tailnetPhase": None,
        "tailnetClosed": False,
        "tailnetForgotten": False,
    },
}

_PARAMS = {
    "org.execute": {"link": "v2.payload"},
    "org.leave": {"org": "group-x"},
    "org.remove": {"org": "group-x", "user": "user:bob"},
    "org.delete": {"org": "group-x"},
}

_ENTRYPOINTS = {
    "org.execute": "ipc_org_execute",
    "org.leave": "ipc_org_leave",
    "org.remove": "ipc_org_remove",
    "org.delete": "ipc_org_delete",
}


_ACL_IPC_ONLY = {"org.acl.grant", "org.acl.ls", "org.acl.revoke", "top.inspect"}


def test_batch_one_registry_and_profiles() -> None:
    registry = MethodRegistry.from_bindings(ORG_BINDINGS)
    assert registry.names == _EXPECTED
    for spec, *_handler in ORG_BINDINGS:
        if spec.name in _ACL_IPC_ONLY | {
            "org.invites.accept",
            "org.use", "org.resolve",
        }:
            # Owner-gated ACL stays IPC/CLI only; see org/acl/specs.py.
            assert spec.http_exposed is False
            assert spec.http_binding is None
        else:
            assert spec.http_exposed is True
            assert spec.http_binding is not None
        expected_audience = ("local-operator", "bound-agent") if spec.name in {
            "org.use", "org.resolve", "org.invites.list", "org.invites.accept",
        } else ("local-operator",)
        assert spec.audience == expected_audience
        if spec.name in _RESULTS:
            assert spec.mutates is True
            assert spec.retry == "never"
            assert spec.confirmation == "none"


def test_delete_declares_the_lifecycle_and_sidecar_codes() -> None:
    spec = next(spec for spec, *_ in ORG_BINDINGS if spec.name == "org.delete")
    codes = {error.code: error for error in spec.errors}
    assert {
        "AUTHORITY_REQUIRED",
        "REVISION_CONFLICT",
        "REMOVAL_CONFIRMATION_STALE",
        "MEMBERSHIP_NOT_INITIALIZED",
        "AUTHORITY_UNREACHABLE",
        "OPERATION_CONFLICT",
        "MEMBERSHIP_LIMIT_EXCEEDED",
        "BAD_REQUEST",
        "FORWARD_PROTOCOL",
        "SIDECAR_PROTOCOL_MISMATCH",
    } <= set(codes)
    assert "LAST_AUTHORITY_REFUSED" not in codes
    assert all(codes[code].data_schema is None for code in ("AUTHORITY_REQUIRED", "BAD_REQUEST"))
    assert "operationId" in spec.result_schema["required"]


@pytest.mark.parametrize("method", list(_RESULTS))
def test_each_removal_handler_forwards_to_its_ipc_entry(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    from hyprial.daemon.impl.org.network import ipc as org_ipc

    calls: list[tuple[str, dict[str, Any]]] = []

    def recorder(app: Any, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((method, dict(params)))
        return _RESULTS[method]

    monkeypatch.setattr(org_ipc, _ENTRYPOINTS[method], recorder)
    result = MethodRegistry.from_bindings(ORG_BINDINGS).invoke(
        object(), method, _PARAMS[method]
    )
    assert result == _RESULTS[method]
    assert calls == [(method, _PARAMS[method])]


def test_batch_one_declarations_keep_the_real_error_codes() -> None:
    specs = {spec.name: spec for spec, *_ in ORG_BINDINGS}
    assert {
        "LAST_AUTHORITY_REFUSED",
        "SELF_REVOKE_REFUSED",
        "REMOVAL_CONFIRMATION_STALE",
        "REVISION_CONFLICT",
        "MEMBERSHIP_SIDECAR_TIMEOUT",
    } <= {error.code for error in specs["org.remove"].errors}
    for name in ("org.leave", "org.remove", "org.delete"):
        codes = {error.code for error in specs[name].errors}
        assert ipc_errors.INVALID_ARGUMENT in codes


def _removal_spec(name: str):  # noqa: ANN202 - local test helper
    return next(spec for spec, *_handler in ORG_BINDINGS if spec.name == name)


def test_revision_refresh_and_audit_accept_the_real_journal_shape() -> None:
    """RemovalJournal always emits the two extra target lists."""

    refresh = {
        "oldRevision": 4,
        "newRevision": 5,
        "remainingDeviceIds": ["device-a"],
        "alreadyRemovedDeviceIds": ["device-b"],
        "unselectedDeviceIds": ["device-c"],
    }
    result = {
        "org": "group-x",
        "spaceId": "space-x",
        "phase": "revision-confirmation",
        "revisionRefresh": refresh,
        "revisionAudit": [
            {
                **refresh,
                "confirmedAtMs": 1,
                "operator": "user:owner",
                "operatorDeviceId": "device-a",
            }
        ],
    }
    error = next(
        iter(Draft202012Validator(_removal_spec("org.leave").result_schema).iter_errors(result)),
        None,
    )
    assert error is None, error.message if error is not None else ""


def test_execute_keeps_no_holder_and_not_a_member_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hyprial.daemon.impl.org.network import ipc as org_ipc

    for code, data in (
        ("no-holder-online", {"spaceId": "space-x"}),
        ("not-a-member", {"spaceId": "space-x"}),
    ):
        def failing(
            app: Any,
            params: dict[str, Any],
            *,
            code: str = code,
            data: dict[str, Any] = data,
        ) -> dict[str, Any]:
            raise DaemonRequestError(code, "real producer", data)

        monkeypatch.setattr(org_ipc, "ipc_org_execute", failing)
        with pytest.raises(DaemonRequestError) as raised:
            MethodRegistry.from_bindings(ORG_BINDINGS).invoke(
                object(), "org.execute", {"link": "v2.payload"}
            )
        assert raised.value.code == code
        assert raised.value.data == data


def test_removal_keeps_casdoor_and_supervisor_error_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hyprial.daemon.impl.org.network import ipc as org_ipc

    for method in ("org.leave", "org.remove"):
        for code in ("CASDOOR_GROUP_WRITE_FAILED", "AUTHORITY_UNREACHABLE"):
            def failing(
                app: Any,
                params: dict[str, Any],
                *,
                code: str = code,
            ) -> dict[str, Any]:
                raise DaemonRequestError(code, "real producer")

            monkeypatch.setattr(org_ipc, _ENTRYPOINTS[method], failing)
            with pytest.raises(DaemonRequestError) as raised:
                MethodRegistry.from_bindings(ORG_BINDINGS).invoke(
                    object(), method, _PARAMS[method]
                )
            assert raised.value.code == code


