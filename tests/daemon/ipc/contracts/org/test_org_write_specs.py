"""org.create / org.invite / org.join declarations, bindings and error data.

Decision record (org lifecycle batch 2, 2026-11-02)
- The three declarations are exercised through a registry assembled from the
  org binding tuple for schema/locality tests, and through the process-wide
  ``METHOD_REGISTRY`` for binding, profile and example assertions.
- Handlers are checked by replacing the real ``ipc_org_*`` entry points with
  recorders: the test proves forwarding, not a second lifecycle
  implementation.
- Result fixtures are the shapes a real isolated daemon returned over AF_UNIX
  (see docs/notes/org-lifecycle-batch2-2026-11-02); the negative cases pin the
  fields the GUI depends on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from hyprial.daemon import daemon_catalog, daemon_openapi
from hyprial.daemon.impl.request_catalog.org import (
    ORG_BINDINGS,
    ORG_CREATE_SPEC,
    ORG_INVITE_SPEC,
    ORG_JOIN_SPEC,
    org_create,
    org_invite,
    org_join,
)
from hyprial.daemon.impl.org.network.service import OrgNetworkError
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.request_catalog.registry import (
    METHOD_REGISTRY,
    MIGRATED_FAMILIES,
    MethodRegistry,
)
from hyprial.kernel import LEGACY_KEBAB_CODES, DaemonRequestError, ipc_errors
from hyprial.shell.impl.request_http.catalog import build_view

PING = {
    "version": "test",
    "nodeId": "org-node",
    "owner": "org-owner",
    "epoch": "epoch-1",
    "phase": "serving",
}

_SPECS = (ORG_CREATE_SPEC, ORG_INVITE_SPEC, ORG_JOIN_SPEC)

_SPACE = {
    "spaceId": "space-hyprial-dev",
    "name": "group-hyprial-dev",
    "owner": "user:lin",
    "createdAt": "2026-11-02T09:30:00Z",
}

_ACL_SPACE = {
    "spaceId": "space-hyprial-dev-acl",
    "name": "group-hyprial-dev-acl",
    "owner": "user:lin",
    "createdAt": "2026-11-02T09:30:00Z",
}

_ORG_CREATE_RESULT = {
    "org": "group-hyprial-dev",
    "spaceId": "space-hyprial-dev",
    "aclSpaceId": "space-hyprial-dev-acl",
    "space": _SPACE,
    "aclSpace": _ACL_SPACE,
    "existed": False,
    "aclExisted": False,
    "casdoorGroup": "verified",
    "published": True,
    "tailnetInitialized": True,
    "devicePublished": True,
}

_ORG_INVITE_RESULT = {
    "org": "group-hyprial-dev",
    "spaceId": "space-hyprial-dev",
    "aclSpaceId": "space-hyprial-dev-acl",
    "user": "user:bob@example",
    "link": "https://hyprial.ai/join#v2.example-payload",
    "deepLink": "hyprial://join#v2.example-payload",
    "command": "hyprial org join 'https://hyprial.ai/join#v2.example-payload'",
    "invite": "hyprial-invite:v2:example-payload",
    "expiresAt": "2026-11-02T10:30:00Z",
    "member": {
        "spaceId": "space-hyprial-dev",
        "user": "user:bob@example",
        "mode": "rw",
    },
    "aclMember": {
        "spaceId": "space-hyprial-dev-acl",
        "user": "user:bob@example",
        "mode": "ro",
    },
}

_ORG_JOIN_RESULT = {
    "org": "group-hyprial-dev",
    "spaceId": "space-hyprial-dev",
    "steps": [
        {
            "kind": "connect-inviter",
            "ok": True,
            "detail": "tailnet redeemed; selected roster candidates mapped",
        },
        {
            "kind": "join-org",
            "ok": True,
            "detail": "joined the org directory and ACL spaces",
        },
        {
            "kind": "register-device",
            "ok": True,
            "detail": "registered this device in the org directory",
        },
        {
            "kind": "connect-members",
            "ok": True,
            "detail": "mapped 2 member device(s)",
        },
    ],
}


def _accepts(schema: Any, value: Any) -> bool:
    return not list(Draft202012Validator(schema).iter_errors(value))


def _view() -> Any:
    entries = [
        dict(row, declared=True, state="available", callable=True)
        for row in daemon_catalog()["methods"]
    ]
    return build_view(
        PING,
        {
            "schemaVersion": 1,
            "catalogVersion": "1",
            "epoch": "epoch-1",
            "packageVersion": "test",
            "methods": entries,
        },
        socket_path="/tmp/org-write-declaration-test.sock",
        local_entries=entries,
    )


def test_the_three_lifecycle_methods_are_declared_and_bound_one_to_one() -> None:
    assert [spec.name for spec in _SPECS] == [
        "org.create",
        "org.invite",
        "org.join",
    ]
    registry = MethodRegistry.from_bindings(ORG_BINDINGS)
    assert registry.names == (
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
        "orgfs.history",
        "orgfs.import",
        "orgfs.invite",
        "orgfs.join",
        "orgfs.ls",
        "orgfs.members",
        "orgfs.mkdir",
        "orgfs.move",
        "orgfs.purge",
        "orgfs.purge_plan",
        "orgfs.purge_status",
        "orgfs.read",
        "orgfs.read_at",
        "orgfs.remove",
        "orgfs.remove_member",
        "orgfs.resolve",
        "orgfs.restore",
        "orgfs.serve",
        "orgfs.space_purge",
        "orgfs.spaces",
        "orgfs.stat",
        "orgfs.stat_at",
        "orgfs.status",
        "orgfs.trash",
        "orgfs.unban",
        "orgfs.watch",
        "orgfs.write",
        "top.inspect",
        "top.snapshot",
    )
    assert METHOD_REGISTRY.get("org.create").handler is org_create
    assert METHOD_REGISTRY.get("org.invite").handler is org_invite
    assert METHOD_REGISTRY.get("org.join").handler is org_join
    assert "org" not in MIGRATED_FAMILIES


def test_the_lifecycle_profiles_are_write_shaped_unary_declarations() -> None:
    for spec in _SPECS:
        assert spec.mode == "unary"
        assert spec.mutates is True
        assert spec.retry == "never"
        assert spec.confirmation == "none"
        assert spec.audience == ("local-operator",)
        assert spec.http_exposed is True
        assert spec.http_binding is not None
        assert spec.http_binding.identity.mode == "none"
        assert spec.http_binding.identity.param is None
        assert spec.http_binding.readiness is None
        assert spec.idempotency is not None
        assert spec.idempotency.mode == "never"
        assert spec.idempotency.key_field is None


@pytest.mark.parametrize(
    ("spec", "accepted"),
    [
        (ORG_CREATE_SPEC, {"org": "group-hyprial-dev"}),
        (ORG_CREATE_SPEC, {"org": "group-dept", "parent": "group-hyprial-dev"}),
        (ORG_INVITE_SPEC, {"org": "group-hyprial-dev", "user": "user:bob"}),
        (ORG_INVITE_SPEC, {"org": "group-hyprial-dev", "user": "bob"}),
        (ORG_JOIN_SPEC, {"link": "https://hyprial.ai/join#v2.payload"}),
    ],
)
def test_lifecycle_params_accept_the_real_wire_shapes(
    spec: Any, accepted: dict[str, Any]
) -> None:
    assert _accepts(spec.params_schema, accepted)


@pytest.mark.parametrize(
    ("spec", "rejected"),
    [
        (ORG_CREATE_SPEC, {}),
        (ORG_CREATE_SPEC, {"org": ""}),
        (ORG_CREATE_SPEC, {"org": 1}),
        (ORG_CREATE_SPEC, {"org": "group-x", "extra": True}),
        (ORG_CREATE_SPEC, []),
        (ORG_INVITE_SPEC, {}),
        (ORG_INVITE_SPEC, {"org": "group-x"}),
        (ORG_INVITE_SPEC, {"org": "group-x", "user": ""}),
        (ORG_INVITE_SPEC, {"org": "group-x", "user": "user:bob", "extra": 1}),
        (ORG_JOIN_SPEC, {}),
        (ORG_JOIN_SPEC, {"link": ""}),
        (ORG_JOIN_SPEC, {"link": "v1.payload", "extra": True}),
        (ORG_JOIN_SPEC, "link"),
    ],
)
def test_lifecycle_params_reject_missing_empty_and_extra_fields(
    spec: Any, rejected: Any
) -> None:
    assert not _accepts(spec.params_schema, rejected)


def test_create_result_needs_both_spaces_and_both_publish_flags() -> None:
    assert _accepts(ORG_CREATE_SPEC.result_schema, _ORG_CREATE_RESULT)
    assert _accepts(
        ORG_CREATE_SPEC.result_schema,
        {
            **_ORG_CREATE_RESULT,
            "parent": "group-root",
            "tailnetId": "space-root",
        },
    )
    assert _accepts(
        ORG_CREATE_SPEC.result_schema,
        {
            **_ORG_CREATE_RESULT,
            "existed": True,
            "aclExisted": True,
            "published": False,
            "devicePublished": False,
        },
    )
    for field in ("spaceId", "space", "aclSpace", "published", "devicePublished"):
        assert not _accepts(
            ORG_CREATE_SPEC.result_schema,
            {key: value for key, value in _ORG_CREATE_RESULT.items() if key != field},
        ), field
    assert not _accepts(
        ORG_CREATE_SPEC.result_schema,
        {**_ORG_CREATE_RESULT, "unexpected": True},
    )
    assert not _accepts(
        ORG_CREATE_SPEC.result_schema,
        {**_ORG_CREATE_RESULT, "space": {"spaceId": "space-x"}},
    )


def test_invite_result_carries_the_three_forms_and_both_member_modes() -> None:
    assert _accepts(ORG_INVITE_SPEC.result_schema, _ORG_INVITE_RESULT)
    for field in ("link", "deepLink", "command", "invite", "member", "aclMember"):
        assert not _accepts(
            ORG_INVITE_SPEC.result_schema,
            {key: value for key, value in _ORG_INVITE_RESULT.items() if key != field},
        ), field
    wrong_mode = {
        **_ORG_INVITE_RESULT,
        "member": {**_ORG_INVITE_RESULT["member"], "mode": "admin"},
    }
    assert not _accepts(ORG_INVITE_SPEC.result_schema, wrong_mode)
    assert not _accepts(
        ORG_INVITE_SPEC.result_schema,
        {**_ORG_INVITE_RESULT, "short": "https://hyprial.ai/j/example"},
    )


def test_join_result_carries_the_executed_steps_only() -> None:
    assert _accepts(ORG_JOIN_SPEC.result_schema, _ORG_JOIN_RESULT)
    assert not _accepts(
        ORG_JOIN_SPEC.result_schema,
        {**_ORG_JOIN_RESULT, "steps": [{"kind": "join-org", "ok": False, "detail": "x"}]},
    )
    assert not _accepts(
        ORG_JOIN_SPEC.result_schema,
        {**_ORG_JOIN_RESULT, "steps": [{"kind": "delete-org", "ok": True, "detail": "x"}]},
    )
    assert not _accepts(
        ORG_JOIN_SPEC.result_schema,
        {
            **_ORG_JOIN_RESULT,
            "steps": [
                {"kind": "join-org", "ok": True, "detail": "x", "unexpected": 1}
            ],
        },
    )
    assert not _accepts(
        ORG_JOIN_SPEC.result_schema,
        {"org": "group-x", "spaceId": "space-x"},
    )


def test_every_handler_forwards_to_the_real_ipc_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hyprial.daemon.impl.org.network import ipc as org_ipc

    calls: list[tuple[str, Any, dict[str, Any]]] = []
    results = {
        "create": _ORG_CREATE_RESULT,
        "invite": _ORG_INVITE_RESULT,
        "join": _ORG_JOIN_RESULT,
    }

    def recorder(name: str):
        def record(app: Any, params: dict[str, Any]) -> dict[str, Any]:
            calls.append((name, app, dict(params)))
            return results[name]

        return record

    monkeypatch.setattr(org_ipc, "ipc_org_create", recorder("create"))
    monkeypatch.setattr(org_ipc, "ipc_org_invite", recorder("invite"))
    monkeypatch.setattr(org_ipc, "ipc_org_join", recorder("join"))

    app = object()
    assert METHOD_REGISTRY.invoke(
        app, "org.create", {"org": "group-hyprial-dev"}
    ) == _ORG_CREATE_RESULT
    assert METHOD_REGISTRY.invoke(
        app, "org.invite", {"org": "group-hyprial-dev", "user": "user:bob"}
    ) == _ORG_INVITE_RESULT
    assert METHOD_REGISTRY.invoke(
        app, "org.join", {"link": "v2.payload"}
    ) == _ORG_JOIN_RESULT
    assert calls == [
        ("create", app, {"org": "group-hyprial-dev"}),
        ("invite", app, {"org": "group-hyprial-dev", "user": "user:bob"}),
        ("join", app, {"link": "v2.payload"}),
    ]


@pytest.mark.parametrize(
    ("method", "params", "code", "data"),
    [
        (
            "org.create",
            {"org": "group-hyprial-dev"},
            "ambiguous-space",
            {"space": "group-hyprial-dev", "count": 2},
        ),
        (
            "org.create",
            {"org": "group-hyprial-dev"},
            "unknown-space",
            {"spaceId": "space-hyprial-dev"},
        ),
        (
            "org.invite",
            {"org": "group-hyprial-dev", "user": "user:bob"},
            "unknown-space",
            {"org": "group-hyprial-dev", "reason": "unknown-acl-space-id"},
        ),
        (
            "org.join",
            {"link": "https://hyprial.ai/join#v2.payload"},
            "no-holder-online",
            {"spaceId": "space-hyprial-dev"},
        ),
        (
            "org.join",
            {"link": "https://hyprial.ai/join#v2.payload"},
            "not-a-member",
            {},
        ),
        (
            "org.join",
            {"link": "https://hyprial.ai/join#v2.payload"},
            "unavailable",
            {"leg": "join-orgfs", "budgetSeconds": 1.5},
        ),
        (
            "org.join",
            {"link": "https://hyprial.ai/join#v2.payload"},
            ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT,
            {
                "operation": "join",
                "leg": "sidecar-queue",
                "budgetSeconds": 2.0,
                "stage": "control",
                "cause": "transport-failed",
                "proxy": "configured",
                "bytesReceived": 120,
                "attempts": [{"node": "10.0.0.7", "cause": "dns-failed"}],
            },
        ),
        (
            "org.join",
            {"link": "https://hyprial.ai/join#v2.payload"},
            "invalid-argument",
            {},
        ),
        (
            "org.join",
            {"link": "https://hyprial.ai/join#v2.payload"},
            "no-holder-online",
            {
                "reason": "sync-timeout",
                "pages": 2,
                "requestBytes": 4096,
                "responseBytes": 8192,
                "elapsedMs": 30000,
                "missingStep": "join",
            },
        ),
        (
            "org.join",
            {"link": "https://hyprial.ai/join#v2.payload"},
            "not-a-member",
            {"status": "partial", "missingStep": "join", "inviteId": "inv-1"},
        ),
    ],
)
def test_declared_lifecycle_errors_keep_code_and_data_through_the_registry(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    params: dict[str, Any],
    code: str,
    data: dict[str, Any],
) -> None:
    from hyprial.daemon.impl.org.network import ipc as org_ipc

    def failing(_app: Any, _params: dict[str, Any], **_kwargs: Any) -> Any:
        raise DaemonRequestError(code, "refused", data)

    entrypoint = {
        "org.create": "ipc_org_create",
        "org.invite": "ipc_org_invite",
        "org.join": "ipc_org_join",
    }[method]
    monkeypatch.setattr(org_ipc, entrypoint, failing)
    with pytest.raises(DaemonRequestError) as raised:
        METHOD_REGISTRY.invoke(object(), method, params)
    assert raised.value.code == code
    assert raised.value.data == data


@pytest.mark.parametrize(
    ("method", "params", "producer", "code", "data"),
    [
        (
            "org.create",
            {"org": "group-hyprial-dev"},
            OrgFsError("invalid-argument", {"message": "org metadata is not valid JSON"}),
            "invalid-argument",
            {},
        ),
        (
            "org.create",
            {"org": "group-hyprial-dev"},
            OrgNetworkError(
                ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT,
                "Tailnet membership command create timed out",
            ),
            ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT,
            None,
        ),
        (
            "org.join",
            {"link": "https://hyprial.ai/join#v2.payload"},
            OrgNetworkError(
                ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT,
                "Tailnet membership command membership-status timed out",
            ),
            ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT,
            None,
        ),
        (
            "org.join",
            {"link": "https://hyprial.ai/join#v2.payload"},
            OrgNetworkError(
                ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT,
                "Tailnet membership command join timed out",
                {"operation": "join", "leg": "join-orgfs", "budgetSeconds": 1.5},
            ),
            ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT,
            {"operation": "join", "leg": "join-orgfs", "budgetSeconds": 1.5},
        ),
        (
            "org.join",
            {"link": "https://hyprial.ai/join#v2.payload"},
            OrgFsError(
                "unavailable",
                {
                    "message": "OrgFS lifecycle wait timed out",
                    "leg": "join-orgfs",
                    "budgetSeconds": 1.5,
                },
            ),
            "unavailable",
            {"leg": "join-orgfs", "budgetSeconds": 1.5},
        ),
        (
            "org.join",
            {"link": "https://hyprial.ai/join#v2.payload"},
            OrgNetworkError(
                "DEVICE_RECORD_MISSING",
                "no device record under the hyprial home",
            ),
            "DEVICE_RECORD_MISSING",
            None,
        ),
    ],
)
def test_real_ipc_producer_errors_reach_the_registry_with_code_and_data(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    params: dict[str, Any],
    producer: BaseException,
    code: str,
    data: dict[str, Any] | None,
) -> None:
    """Translate real OrgFS/OrgNetwork producers through the real IPC entry.

    The fake service only raises; ``ipc_org_create``/``ipc_org_join`` and
    ``_translate`` stay real, so the registry sees the same code/data as an
    AF_UNIX caller.  ``OrgFsError`` consumes its internal message key, which
    is why the declared lowercase ``invalid-argument`` data is ``{}``.
    """
    from hyprial.daemon.impl.org.network import ipc as org_ipc

    class FailingService:
        def create(self, _org: str) -> dict[str, Any]:
            raise producer

        def join(self, _link: str) -> dict[str, Any]:
            raise producer

    monkeypatch.setattr(
        org_ipc, "_request_service", lambda _app: FailingService()
    )
    with pytest.raises(DaemonRequestError) as raised:
        METHOD_REGISTRY.invoke(object(), method, params)
    assert raised.value.code == code
    assert raised.value.data == data


def test_an_undeclared_lowercase_code_cannot_claim_the_lifecycle_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hyprial.daemon.impl.org.network import ipc as org_ipc

    def failing(_app: Any, _params: dict[str, Any], **_kwargs: Any) -> Any:
        raise DaemonRequestError("brand-new-code", "not declared")

    monkeypatch.setattr(org_ipc, "ipc_org_create", failing)
    with pytest.raises(DaemonRequestError) as raised:
        METHOD_REGISTRY.invoke(object(), "org.create", {"org": "group-x"})
    assert raised.value.code == ipc_errors.DAEMON_ERROR


def test_the_lifecycle_declarations_cover_the_reachable_lowercase_codes() -> None:
    declared = {
        spec.name: {error.code for error in spec.errors} for spec in _SPECS
    }
    assert {
        "ambiguous-space",
        "invalid-argument",
        "unknown-space",
        "unavailable",
    } <= declared["org.create"]
    assert {"unknown-space", "unavailable"} <= declared["org.invite"]
    assert {
        "no-holder-online",
        "not-a-member",
        "unknown-space",
        "unavailable",
    } <= declared["org.join"]
    assert ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT in declared["org.create"]
    assert ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT in declared["org.join"]
    assert "DEVICE_RECORD_MISSING" in declared["org.join"]
    for codes in declared.values():
        assert {code for code in codes if not code.isupper()} <= LEGACY_KEBAB_CODES
    for spec in _SPECS:
        codes = {error.code for error in spec.errors}
        assert ipc_errors.INVALID_ARGUMENT in codes
        assert "ORG_NETWORK_ERROR" not in codes


def test_the_error_data_schemas_accept_every_real_producer_shape() -> None:
    create_ambiguous = next(
        error for error in ORG_CREATE_SPEC.errors if error.code == "ambiguous-space"
    )
    assert create_ambiguous.data_schema is not None
    assert _accepts(create_ambiguous.data_schema, {"space": "group-x", "count": 2})

    create_invalid = next(
        error for error in ORG_CREATE_SPEC.errors if error.code == "invalid-argument"
    )
    assert create_invalid.data_schema is not None
    assert _accepts(create_invalid.data_schema, {})
    assert not _accepts(create_invalid.data_schema, {"message": "internal detail"})

    create_timeout = next(
        error
        for error in ORG_CREATE_SPEC.errors
        if error.code == ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT
    )
    assert create_timeout.data_schema is None
    join_timeout = next(
        error
        for error in ORG_JOIN_SPEC.errors
        if error.code == ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT
    )
    assert join_timeout.data_schema is not None
    assert _accepts(
        join_timeout.data_schema,
        {"operation": "join", "leg": "join-orgfs", "budgetSeconds": 1.5},
    )
    assert _accepts(
        join_timeout.data_schema,
        {
            "operation": "membership-status",
            "leg": "sidecar-queue",
            "budgetSeconds": 2,
            "stage": "control",
            "cause": "deadline-exceeded",
            "proxy": "configured",
            "bytesReceived": 4096,
            "attempts": [
                {"node": "10.0.0.7", "cause": "dns-failed"},
                {
                    "node": "10.0.0.8",
                    "cause": "transport-failed",
                    "source": "diagnostic-probe",
                },
            ],
        },
    )
    assert not _accepts(
        join_timeout.data_schema,
        {"operation": "join", "leg": "x", "budgetSeconds": 1, "unexpected": 1},
    )
    assert not _accepts(
        join_timeout.data_schema, {"leg": "join-orgfs", "budgetSeconds": 1}
    )
    assert not _accepts(
        join_timeout.data_schema,
        {"operation": "join", "leg": "x", "budgetSeconds": "soon"},
    )
    join_device_missing = next(
        error for error in ORG_JOIN_SPEC.errors if error.code == "DEVICE_RECORD_MISSING"
    )
    assert join_device_missing.data_schema is None

    join_invalid = next(
        error for error in ORG_JOIN_SPEC.errors if error.code == "invalid-argument"
    )
    assert join_invalid.data_schema is not None
    assert _accepts(join_invalid.data_schema, {})
    assert _accepts(join_invalid.data_schema, {"doc_id": "not-a-doc"})
    assert not _accepts(join_invalid.data_schema, {"message": "internal detail"})

    for spec in (ORG_CREATE_SPEC, ORG_INVITE_SPEC, ORG_JOIN_SPEC):
        unknown = next(error for error in spec.errors if error.code == "unknown-space")
        assert unknown.data_schema is not None
        assert _accepts(unknown.data_schema, {"spaceId": "space-x"})
        assert _accepts(unknown.data_schema, {"org": "group-x", "space": "group-x"})
        assert _accepts(unknown.data_schema, {"org": "group-x", "reason": "missing-acl-space"})

        unavailable = next(
            error for error in spec.errors if error.code == "unavailable"
        )
        assert unavailable.data_schema is not None
        assert _accepts(unavailable.data_schema, {})
        assert _accepts(unavailable.data_schema, {"message": "orgfs is closing"})
        assert _accepts(
            unavailable.data_schema, {"leg": "join-orgfs", "budgetSeconds": 1.5}
        )
        assert not _accepts(
            unavailable.data_schema,
            {"leg": "join-orgfs", "budgetSeconds": 1.5, "unexpected": 1},
        )

    join_no_holder = next(
        error for error in ORG_JOIN_SPEC.errors if error.code == "no-holder-online"
    )
    assert join_no_holder.data_schema is not None
    assert _accepts(join_no_holder.data_schema, {"spaceId": "space-x"})
    assert _accepts(join_no_holder.data_schema, {})
    assert _accepts(
        join_no_holder.data_schema,
        {
            "reason": "sync-timeout",
            "pages": 2,
            "requestBytes": 4096,
            "responseBytes": 8192,
            "elapsedMs": 30000,
        },
    )
    assert _accepts(join_no_holder.data_schema, {"missingStep": "join"})
    assert _accepts(
        join_no_holder.data_schema,
        {"status": "partial", "missingStep": "tailnet-invite", "inviteId": "inv-1"},
    )
    assert not _accepts(join_no_holder.data_schema, {"unexpected": 1})

    join_not_member = next(
        error for error in ORG_JOIN_SPEC.errors if error.code == "not-a-member"
    )
    assert join_not_member.data_schema is not None
    assert _accepts(join_not_member.data_schema, {})
    assert _accepts(join_not_member.data_schema, {"spaceId": "space-x"})
    assert _accepts(
        join_not_member.data_schema,
        {"reason": "supplier-not-member"},
    )
    assert _accepts(
        join_not_member.data_schema,
        {"status": "partial", "missingStep": "join", "inviteId": "inv-1"},
    )
    assert not _accepts(join_not_member.data_schema, {"unexpected": 1})


def test_the_lifecycle_methods_are_admitted_with_write_profiles() -> None:
    view = _view()
    for name in ("org.create", "org.invite", "org.join"):
        assert name in view.routes
        route = view.routes[name]
        assert route.mutates is True
        assert route.retry == "never"
        assert route.confirmation == "none"
        assert route.http_binding["identity"] == {"mode": "none", "param": None}


def test_the_openapi_projection_carries_the_lifecycle_examples_and_errors() -> None:
    methods = {entry["name"]: entry for entry in daemon_openapi()["x-hyprial-methods"]}
    for name in ("org.create", "org.invite", "org.join"):
        assert methods[name]["httpExposed"] is True
        assert methods[name]["mutates"] is True
        cases = methods[name]["x-hyprial-examples"]
        assert any(case["kind"] == "success" for case in cases)
        assert any(case["kind"] == "invalid-params" for case in cases)
        assert any(case["kind"] == "domain-error" for case in cases)

    create_cases = methods["org.create"]["x-hyprial-examples"]
    assert any(case.get("result", {}).get("published") is True
               and case.get("result", {}).get("existed") is True
               for case in create_cases)
    join_cases = methods["org.join"]["x-hyprial-examples"]
    assert any(
        [step["kind"] for step in case["result"]["steps"]]
        == ["connect-inviter", "join-org", "register-device", "connect-members"]
        for case in join_cases
        if "result" in case
    )
    invite_codes = {error["code"] for error in methods["org.invite"]["errors"]}
    assert "CASDOOR_USER_NOT_FOUND" in invite_codes
    create_codes = {error["code"] for error in methods["org.create"]["errors"]}
    join_codes = {error["code"] for error in methods["org.join"]["errors"]}
    assert "invalid-argument" in create_codes
    assert ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT in create_codes
    assert ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT in join_codes
    assert "DEVICE_RECORD_MISSING" in join_codes


def test_the_legacy_router_does_not_shadow_the_lifecycle_methods() -> None:
    router = (
        Path(__file__).resolve().parents[5]
        / "src"
        / "hyprial"
        / "daemon"
        / "impl"
        / "ipc"
        / "router.py"
    ).read_text(encoding="utf-8")
    for name in (
        "org.create",
        "org.delete",
        "org.execute",
        "org.invite",
        "org.join",
        "org.leave",
        "org.network",
        "org.remove",
        "org.repair",
    ):
        assert f'"{name}": "ipc_org_' not in router
    assert 'if method == "org.publish"' not in router
