"""ACL-space IPC admission: reads pass; mutations stop at the entry gate.

Lifecycle/membership methods keep the flat external-mutation refusal on
every node.  Content writes on the owner node go through the entry gate:
the owner's terminal and the owner's daemon-launched user-proxy pass, a
granted agent passes until revoked/expired, and everything else — including
a session that merely *claims* the user-proxy runtime — is denied with
``ORG_ACL_WRITE_DENIED``.
"""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.application.messaging.orgfs_bridge import (
    _ORG_ACL_CONTENT_WRITE_METHODS,
    _ORGFS_MUTATING_METHODS,
    _ORGFS_READ_METHODS,
)
from hyprial.daemon.impl.ipc.router import _IPC_STATS_METHODS
from hyprial.daemon.impl.org.network.ipc import (
    ipc_org_acl_grant,
    ipc_org_acl_ls,
    ipc_org_acl_revoke,
    ipc_org_create,
)
from hyprial.daemon.impl.org.network.state import add_org_acl_grant
from hyprial.identity import ORG_META_DOC
from hyprial.identity.impl.device import DeviceRecord, address_path, write_device_record
from tests.support.tailnet_membership import FakeMembershipSidecar, key
from hyprial.kernel import DaemonRequestError, HarnessLaunchSpec, ipc_errors
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _deep_module_daemon_dependencies,
)

OWNER = "owner@example.com"
OPERATOR = {"from": f"user:{OWNER}"}
AGENT_NAME = "worker"
AGENT_SESSION = "session-worker"
IMPOSTOR_NAME = "not-a-proxy"
IMPOSTOR_SESSION = "session-not-a-proxy"
PROXY_NAME = "proxy"
PROXY_SESSION = "session-proxy"


class _FakeTransport:
    def close(self) -> None:
        return None


class _WorkerRefs:
    """The harness authority's worker session-ref projection, minimal."""

    def __init__(self, refs: dict[tuple[str, str], str]) -> None:
        self._refs = refs

    def projected_worker_session_refs(self) -> dict[tuple[str, str], str]:
        return dict(self._refs)


@pytest.fixture
def application(tmp_path, monkeypatch):
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node-a",
        hyprial_home=tmp_path / "home",
        owner=OWNER,
        **_deep_module_daemon_dependencies(),
    )
    app._transport = _FakeTransport()  # type: ignore[assignment]
    # Org creation now initializes sidecar membership before upper-layer ACL work.
    # Keep all local management gates real; no upper-layer trust bypass is used.
    app._forwarding_supervisor = FakeMembershipSidecar(owner=OWNER, now_ms=None)
    monkeypatch.setattr(app, "_start_forwarding_supervisor", lambda: None)
    monkeypatch.setattr(app, "_reconcile_forwarding_endpoints", lambda: None)
    monkeypatch.setattr(app, "_request_forwarding_reconcile", lambda reason, **fields: True)
    monkeypatch.setattr(app, "_await_forwarding_reconcile", lambda reason, **fields: None)
    write_device_record(app.hyprial_home, DeviceRecord(
        device_id="node-a", owner=OWNER,
        server_public=key("server-node-a"), client_public=key("client-node-a"),
    ))
    address_path(app.hyprial_home).write_text("tcp://10.0.0.9:7447#ipc-psk")
    yield app
    app._forwarding_supervisor = None  # In-memory fixture has no process to close.
    app._close()
    app._close_home_lease()


def _acl_space_id(application) -> str:
    return ipc_org_create(application, {"org": "group-acme"})["aclSpaceId"]


def _prime_agent_dependencies(application) -> None:
    """The minimal fakes ``agent.create``/``session.register`` dispatch needs."""

    if application._inbox is None:
        from tests.daemon.routing.test_route_outbound import FakeInbox

        application._inbox = FakeInbox()
    if application._harnesses is None:
        application._harnesses = _WorkerRefs({})  # type: ignore[assignment]
    if application._presence is None:
        application._presence = object()  # type: ignore[assignment]


def _register_agent_session(
    application, name: str, session_ref: str, *, runtime: str = "claude_interactive"
) -> None:
    _prime_agent_dependencies(application)
    application.handle("agent.create", {"name": name})
    cwd = application.hyprial_home.parent / "repo"
    cwd.mkdir(parents=True, exist_ok=True)
    application.handle(
        "session.register",
        {
            "actor": name,
            "sessionRef": session_ref,
            "cwd": str(cwd),
            "command": ["test-carrier"],
            "source": "claude-channel",
            "runtime": runtime,
        },
    )


def _write(application, space_id: str, caller: dict[str, str], **extra):
    return application._handle_orgfs(
        "orgfs.write",
        {
            "spaceId": space_id,
            "path": "policy/role-grants.json",
            "text": "{}",
            **caller,
        },
        **extra,
    )


def _actor_uri(name: str) -> str:
    return f"agent:{OWNER}:node-a:{name}"


def test_every_orgfs_router_method_is_classified() -> None:
    routed = {method for method in _IPC_STATS_METHODS if method.startswith("orgfs.")}
    assert _ORGFS_MUTATING_METHODS.isdisjoint(_ORGFS_READ_METHODS)
    assert _ORGFS_MUTATING_METHODS | _ORGFS_READ_METHODS == routed
    assert _ORG_ACL_CONTENT_WRITE_METHODS < _ORGFS_MUTATING_METHODS


def test_org_ipc_create_is_the_internal_acl_creation_path(application) -> None:
    created = ipc_org_create(application, {"org": "group-acme"})
    assert created["aclSpaceId"]
    assert application._handle_orgfs(
        "orgfs.read",
        {
            "spaceId": created["aclSpaceId"],
            "path": "policy/role-grants.json",
        },
    )["text"]


@pytest.mark.parametrize(
    "method,params",
    [
        ("orgfs.invite", {"user": "user:bob@example.com", "mode": "rw"}),
        ("orgfs.remove_member", {"user": "user:bob@example.com"}),
        ("orgfs.serve", {}),
    ],
)
def test_acl_lifecycle_mutations_stay_internal_even_for_the_operator(
    application, method: str, params: dict[str, object]
) -> None:
    acl_space_id = _acl_space_id(application)
    with pytest.raises(DaemonRequestError) as raised:
        application._handle_orgfs(
            method, {"spaceId": acl_space_id, **OPERATOR, **params}
        )
    assert raised.value.code == ipc_errors.ORGFS_ACL_SPACE_EXTERNAL_MUTATION


def test_external_acl_space_creation_is_rejected(application) -> None:
    with pytest.raises(DaemonRequestError) as raised:
        application._handle_orgfs(
            "orgfs.create", {"name": "group-acme-acl", **OPERATOR}
        )
    assert raised.value.code == ipc_errors.ORGFS_ACL_SPACE_EXTERNAL_MUTATION


def test_owner_terminal_writes_the_acl_space(application) -> None:
    acl_space_id = _acl_space_id(application)
    written = _write(application, acl_space_id, OPERATOR)
    assert written["path"] == "policy/role-grants.json"


def test_unauthenticated_write_is_denied(application) -> None:
    acl_space_id = _acl_space_id(application)
    with pytest.raises(DaemonRequestError) as raised:
        _write(application, acl_space_id, {})
    assert raised.value.code == ipc_errors.ORG_ACL_WRITE_DENIED
    assert raised.value.data["reason"] == "caller-not-authenticated"


def test_agent_session_write_is_denied(application) -> None:
    acl_space_id = _acl_space_id(application)
    _register_agent_session(application, AGENT_NAME, AGENT_SESSION)
    with pytest.raises(DaemonRequestError) as raised:
        _write(
            application,
            acl_space_id,
            {"from": AGENT_NAME, "sessionRef": AGENT_SESSION},
        )
    assert raised.value.code == ipc_errors.ORG_ACL_WRITE_DENIED
    assert raised.value.data["reason"] == "not-granted"
    assert raised.value.data["caller"] == _actor_uri(AGENT_NAME)


def test_self_reported_user_proxy_runtime_is_still_an_agent(application) -> None:
    """Claiming the user-proxy runtime at session.register mints nothing."""

    acl_space_id = _acl_space_id(application)
    _register_agent_session(
        application,
        IMPOSTOR_NAME,
        IMPOSTOR_SESSION,
        runtime="user-proxy_interactive",
    )
    with pytest.raises(DaemonRequestError) as raised:
        _write(
            application,
            acl_space_id,
            {"from": IMPOSTOR_NAME, "sessionRef": IMPOSTOR_SESSION},
        )
    assert raised.value.code == ipc_errors.ORG_ACL_WRITE_DENIED


def _launch_user_proxy(application) -> None:
    """A user-proxy the daemon launched: launch spec + worker session ref."""

    _prime_agent_dependencies(application)
    application.handle("agent.create", {"name": PROXY_NAME})
    application.desired_state.upsert_harness(
        HarnessLaunchSpec(harness="user-proxy", name=PROXY_NAME, headless=True)
    )
    application._harnesses = _WorkerRefs(  # type: ignore[assignment]
        {("user-proxy", PROXY_NAME): PROXY_SESSION}
    )


def test_daemon_launched_user_proxy_writes_the_acl_space(application) -> None:
    acl_space_id = _acl_space_id(application)
    _launch_user_proxy(application)
    written = _write(
        application,
        acl_space_id,
        {"from": PROXY_NAME, "sessionRef": PROXY_SESSION},
    )
    assert written["path"] == "policy/role-grants.json"


def test_granted_agent_writes_until_revoked(application) -> None:
    acl_space_id = _acl_space_id(application)
    _register_agent_session(application, AGENT_NAME, AGENT_SESSION)
    caller = {"from": AGENT_NAME, "sessionRef": AGENT_SESSION}

    granted = ipc_org_acl_grant(
        application, {**OPERATOR, "org": "group-acme", "agent": AGENT_NAME}
    )
    assert granted["agent"] == _actor_uri(AGENT_NAME)
    assert _write(application, acl_space_id, caller)["path"]

    revoked = ipc_org_acl_revoke(
        application, {**OPERATOR, "org": "group-acme", "agent": AGENT_NAME}
    )
    assert revoked["revoked"] is True
    with pytest.raises(DaemonRequestError) as raised:
        _write(application, acl_space_id, caller)
    assert raised.value.code == ipc_errors.ORG_ACL_WRITE_DENIED


def test_expired_grant_no_longer_admits(application) -> None:
    acl_space_id = _acl_space_id(application)
    _register_agent_session(application, AGENT_NAME, AGENT_SESSION)
    add_org_acl_grant(
        application.hyprial_home,
        "group-acme",
        _actor_uri(AGENT_NAME),
        granted_at_ms=1,
        until_ms=2,  # long past
    )
    with pytest.raises(DaemonRequestError) as raised:
        _write(
            application,
            acl_space_id,
            {"from": AGENT_NAME, "sessionRef": AGENT_SESSION},
        )
    assert raised.value.code == ipc_errors.ORG_ACL_WRITE_DENIED


def test_member_replica_keeps_the_flat_external_mutation_refusal(
    application, tmp_path
) -> None:
    """The gate exists only on the owner node: a replica whose space owner is
    someone else refuses every mutation, operator included."""

    from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime

    foreign = OrgFsRuntime(
        tmp_path / "foreign-state",
        node_id="node-a",
        author="user:someone-else@example.com",
    )
    application._orgfs_runtime = foreign  # type: ignore[assignment]
    space = foreign.facade.create_space("group-acme-acl")
    with pytest.raises(DaemonRequestError) as raised:
        _write(application, space.space_id, OPERATOR)
    assert raised.value.code == ipc_errors.ORGFS_ACL_SPACE_EXTERNAL_MUTATION


def test_directory_org_document_is_not_treated_as_the_acl_binding(application) -> None:
    created = ipc_org_create(application, {"org": "group-acme"})
    written = application._handle_orgfs(
        "orgfs.write",
        {
            "spaceId": created["spaceId"],
            "path": ORG_META_DOC,
            "text": '{"aclSpaceId":"untrusted"}',
        },
    )
    assert written["path"] == ORG_META_DOC


# -- org.acl.* custody --------------------------------------------------------


def test_grant_requires_the_owner_terminal_or_user_proxy(application) -> None:
    _acl_space_id(application)
    _register_agent_session(application, AGENT_NAME, AGENT_SESSION)
    with pytest.raises(DaemonRequestError) as raised:
        ipc_org_acl_grant(
            application,
            {
                "from": AGENT_NAME,
                "sessionRef": AGENT_SESSION,
                "org": "group-acme",
                "agent": AGENT_NAME,
            },
        )
    assert raised.value.code == ipc_errors.ORG_ACL_GRANT_FORBIDDEN


def test_user_proxy_may_manage_grants(application) -> None:
    _acl_space_id(application)
    _launch_user_proxy(application)
    application.handle("agent.create", {"name": AGENT_NAME})
    caller = {"from": PROXY_NAME, "sessionRef": PROXY_SESSION}
    granted = ipc_org_acl_grant(
        application, {**caller, "org": "group-acme", "agent": AGENT_NAME}
    )
    assert granted["agent"] == _actor_uri(AGENT_NAME)
    listed = ipc_org_acl_ls(application, {**caller, "org": "group-acme"})
    assert [entry["agent"] for entry in listed["grants"]] == [
        _actor_uri(AGENT_NAME)
    ]


def test_grant_refuses_an_agent_nobody_registered(application) -> None:
    _acl_space_id(application)
    with pytest.raises(DaemonRequestError) as raised:
        ipc_org_acl_grant(
            application, {**OPERATOR, "org": "group-acme", "agent": "stranger"}
        )
    assert raised.value.code == ipc_errors.ORG_ACL_GRANT_NOT_OWNED


def test_grant_refuses_a_foreign_agent_uri(application) -> None:
    _acl_space_id(application)
    with pytest.raises(DaemonRequestError) as raised:
        ipc_org_acl_grant(
            application,
            {
                **OPERATOR,
                "org": "group-acme",
                "agent": "agent:someone-else@example.com:node-b:worker",
            },
        )
    assert raised.value.code == ipc_errors.ORG_ACL_GRANT_NOT_OWNED


def test_grant_refuses_an_unknown_org(application) -> None:
    with pytest.raises(DaemonRequestError) as raised:
        ipc_org_acl_grant(
            application, {**OPERATOR, "org": "group-nowhere", "agent": AGENT_NAME}
        )
    assert raised.value.code == "unknown-space"


def test_grant_until_must_be_in_the_future(application) -> None:
    _acl_space_id(application)
    _prime_agent_dependencies(application)
    application.handle("agent.create", {"name": AGENT_NAME})
    with pytest.raises(DaemonRequestError) as raised:
        ipc_org_acl_grant(
            application,
            {**OPERATOR, "org": "group-acme", "agent": AGENT_NAME, "untilMs": 2},
        )
    assert raised.value.code == ipc_errors.INVALID_ARGUMENT


def test_revoke_without_a_grant_reports_revoked_false(application) -> None:
    _acl_space_id(application)
    _prime_agent_dependencies(application)
    application.handle("agent.create", {"name": AGENT_NAME})
    result = ipc_org_acl_revoke(
        application, {**OPERATOR, "org": "group-acme", "agent": AGENT_NAME}
    )
    assert result == {
        "org": "group-acme",
        "agent": _actor_uri(AGENT_NAME),
        "revoked": False,
    }
