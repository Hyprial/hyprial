"""The org.* IPC handlers over a minimal fake application object.

The point under test is the handler layer: parameter validation, the
service wiring from ``app`` (facade access, owner/node identity, the
forwarding seams), and the error translation — not the service logic
(covered against a real runtime in the sibling modules).
"""

from __future__ import annotations

from pathlib import Path
import threading
import time
from typing import Any
from unittest.mock import patch

import pytest
from tests.support.tailnet_membership import confirm_ipc_removal

from hyprial.identity import DirectoryDevice
from hyprial.kernel import DaemonRequestError
from hyprial.daemon.impl.correlation.bounded_cadence import BoundedCadence
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.org.network.ipc import (
    ipc_org_create,
    ipc_org_delete,
    ipc_org_execute,
    ipc_org_invite,
    ipc_org_join,
    ipc_org_leave,
    ipc_org_list,
    ipc_org_network,
    ipc_org_remove,
)
from hyprial.identity.impl.device import (
    DeviceRecord,
    address_path,
    write_device_record,
)

from tests.support.tailnet_membership import FakeMembershipSidecar, key

OWNER = "owner@example"
NODE_ID = "node-a"


class _FakeApp:
    """The application surface org.network.ipc touches, nothing more."""

    def __init__(self, runtime: OrgFsRuntime, home: Path) -> None:
        self._runtime = runtime
        self.hyprial_home = home
        self.node_id = NODE_ID
        self.owner = OWNER
        self._forwarding_supervisor = FakeMembershipSidecar(now_ms=None)
        self.events: list[tuple[str, str]] = []
        self.records: list[dict[str, Any]] = []

    def _require_orgfs_runtime(self) -> OrgFsRuntime:
        return self._runtime

    def _start_forwarding_supervisor(self) -> None:
        pass  # Lifecycle writes ensure control is started; read-only paths do not.

    def _reconcile_forwarding_endpoints(self) -> None:
        self.events.append(('debug', 'redial'))

    def _await_forwarding_reconcile(self, reason: str, *, timeout: float, **fields: Any) -> None:
        self._reconcile_forwarding_endpoints()

    def _request_forwarding_reconcile(self, reason: str, **fields: Any) -> bool:
        self.events.append(('debug', f'reconcile-requested:{reason}'))
        return True

    def _forwarding_status_json(self) -> dict[str, Any]:
        return {"policy": "auto", "peers": []}

    def _log(self, level: str, domain: str, event: str, **fields: Any) -> None:
        self.events.append((level, f"{domain}.{event}"))
        self.records.append(
            {"level": level, "domain": domain, "event": event, "fields": fields}
        )


@pytest.fixture
def app(tmp_path: Path) -> _FakeApp:
    runtime = OrgFsRuntime(
        tmp_path / "state", node_id=NODE_ID, author=f"user:{OWNER}"
    )
    write_device_record(
        tmp_path,
        DeviceRecord(
            device_id=NODE_ID,
            owner=OWNER,
            server_public=key("server-node-a"),
            client_public=key("client-node-a"),
        ),
    )
    # The sidecar-written address file the service's read_own_address seam reads.
    address_path(tmp_path).write_text("tcp://10.0.0.9:7447#ipc-psk")
    application = _FakeApp(runtime, tmp_path)
    yield application
    runtime.close()


def test_ipc_org_create_and_network_round_trip(app: _FakeApp) -> None:
    created = ipc_org_create(app, {"org": "group-acme"})
    assert created["org"] == "group-acme"
    assert created["devicePublished"] is True
    spaces = {space.name: space for space in app._runtime.facade.spaces()}
    assert set(spaces) == {"group-acme", "group-acme-acl"}
    assert created["spaceId"] == spaces["group-acme"].space_id
    assert created["aclSpaceId"] == spaces["group-acme-acl"].space_id
    assert spaces["group-acme-acl"].owner == f"user:{OWNER}"
    policy, _version = app._runtime.facade.read_text(
        created["aclSpaceId"], "policy/role-grants.json"
    )
    assert '"role":"owner"' in policy

    view = ipc_org_network(app, {})
    assert view["orgs"] == ["group-acme"]
    assert [device["deviceId"] for device in view["directory"]["group-acme"]] == [NODE_ID]
    assert "address" not in view["directory"]["group-acme"][0]
    assert view["mappedPeers"] == []
    assert view["sidecar"]["policy"] == "auto"
    assert view["self"] == {
        "deviceId": NODE_ID,
        "owner": OWNER,
        "serverPublic": key("server-node-a"),
        "addressPresent": True,
    }


@pytest.mark.parametrize(('failure', 'code'), [
    ('membership-init', 'MEMBERSHIP_STORE_INVALID'),
    ('address', 'AUTHORITY_UNREACHABLE'),
    ('settings', 'BAD_REQUEST'),
])
def test_ipc_org_create_translates_membership_initialization_errors(app, failure, code):
    if failure == 'address':
        address_path(app.hyprial_home).unlink()
    elif failure == 'settings':
        (app.hyprial_home / 'settings.json').write_text(
            '{"forwarding":{"tailnet":{"controlPort":0}}}')
    else:
        app._forwarding_supervisor.failures[failure] = code
    with pytest.raises(DaemonRequestError) as caught:
        ipc_org_create(app, {'org': 'group-acme'})
    assert caught.value.code == code
    assert app._forwarding_supervisor.tailnets == {}


def test_ipc_org_network_presence_cannot_remove_orgfs_devices(app: _FakeApp) -> None:
    ipc_org_create(app, {"org": "group-acme"})
    OrgFsDirectoryStore(app._runtime.facade).put_device(
        "group-acme",
        DirectoryDevice(
            device_id="node-b",
            owner=OWNER,
            server_public="nodekey:server-b",
            client_public="nodekey:client-b",
            address="tcp://10.0.0.10:7447#other-psk",
            key_generation=1,
            updated_at="2026-10-20T00:00:00+00:00",
        ),
    )

    view = ipc_org_network(app, {})

    devices = view["directory"]["group-acme"]
    assert [device["deviceId"] for device in devices] == [NODE_ID, "node-b"]
    assert {device["presence"] for device in devices} == {"unknown"}
    assert view["mappedPeers"] == []


def test_directory_discovery_quarantines_invalid_version_across_service_builds(
    app: _FakeApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.daemon.impl.org.network import directory as directory_module

    clock = [100.0]
    monkeypatch.setattr(directory_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(directory_module, "_INVALID_DEVICE_QUIET_SECONDS", 10.0)
    monkeypatch.setattr(directory_module, "_INVALID_DEVICE_RETRY_MAX_SECONDS", 20.0)
    created = ipc_org_create(app, {"org": "group-acme"})
    path = f"directory/devices/{OWNER}/{NODE_ID}.json"
    app._runtime.facade.write_text(created["spaceId"], path, "not json")
    node = app._runtime.facade.stat(created["spaceId"], path)
    original_read = app._runtime.facade.read_text
    invalid_reads = 0

    def counted_read(space_id: str, target: str) -> tuple[str, str]:
        nonlocal invalid_reads
        if space_id == created["spaceId"] and target == f"id:{node.node_id}":
            invalid_reads += 1
        return original_read(space_id, target)

    monkeypatch.setattr(app._runtime.facade, "read_text", counted_read)

    def scan():
        # The network view owns directory diagnostics, not candidate selection.
        assert ipc_org_network(app, {})["directory"]["group-acme"] == []

    scan()
    clock[0] = 105.0
    scan()
    assert invalid_reads == 1
    assert [event for _level, event in app.events].count(
        "org.directory.device_invalid"
    ) == 1

    clock[0] = 110.0
    scan()
    clock[0] = 125.0
    scan()
    clock[0] = 130.0
    scan()
    clock[0] = 145.0
    scan()
    assert invalid_reads == 3, "retry delay is capped at twenty seconds"

    app._runtime.facade.write_text(created["spaceId"], path, "changed, still bad")
    scan()
    assert invalid_reads == 4, "a new content version bypasses quarantine"
    assert [event for _level, event in app.events].count(
        "org.directory.device_invalid"
    ) == 4


def test_ipc_org_network_keeps_sidecar_when_directory_scan_hangs(
    app: _FakeApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    ipc_org_create(app, {"org": "group-acme"})

    def hung_scan(
        _store: OrgFsDirectoryStore, _org: str
    ) -> tuple[list[Any], dict[str, Any]]:
        time.sleep(2.0)
        return [], {"contentPending": [], "parkedCommits": []}

    monkeypatch.setattr(OrgFsDirectoryStore, "scan_devices", hung_scan)
    started = time.monotonic()
    view = ipc_org_network(app, {})

    assert time.monotonic() - started < 1.5
    assert view["mappedPeers"] == []
    assert view["paneStatus"]["directory"] == {
        "available": False, "reason": "timeout"
    }
    assert view["paneStatus"]["mappedPeers"] == {"available": True}


def test_ipc_org_invite_requires_user(app: _FakeApp) -> None:
    ipc_org_create(app, {"org": "group-acme"})
    with pytest.raises(DaemonRequestError) as raised:
        ipc_org_invite(app, {"org": "group-acme"})
    assert raised.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize(
    ("handler", "side_effect_method"),
    [
        (ipc_org_invite, "invite"),
        (ipc_org_remove, "remove_member"),
    ],
)
def test_ipc_org_member_mutations_reject_invalid_user_before_orgfs_side_effect(
    app: _FakeApp,
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
    side_effect_method: str,
) -> None:
    ipc_org_create(app, {"org": "group-acme"})
    facade = app._require_orgfs_runtime().facade
    called = False

    def forbidden(*_args: object, **_kwargs: object) -> None:
        nonlocal called
        called = True
        raise AssertionError("invalid IPC user reached an OrgFS mutation")

    monkeypatch.setattr(facade, side_effect_method, forbidden)
    with pytest.raises(DaemonRequestError) as raised:
        handler(app, {"org": "group-acme", "user": "a:b"})

    assert raised.value.code == "INVALID_ARGUMENT"
    assert called is False


def test_ipc_org_network_rejects_non_string_org(app: _FakeApp) -> None:
    with pytest.raises(DaemonRequestError) as raised:
        ipc_org_network(app, {"org": 42})
    assert raised.value.code == "INVALID_ARGUMENT"


def test_ipc_org_create_rejects_bad_names(app: _FakeApp) -> None:
    for bad in ("Not A Name", "acme", "org-acme"):
        with pytest.raises(DaemonRequestError) as raised:
            ipc_org_create(app, {"org": bad})
        assert raised.value.code == "INVALID_ARGUMENT"
        assert "group-" in str(raised.value)


def test_ipc_org_network_unknown_org_is_a_typed_error(app: _FakeApp) -> None:
    with pytest.raises(DaemonRequestError) as raised:
        ipc_org_network(app, {"org": "group-nosuchorg"})
    assert raised.value.code == "unknown-space"


# -- the lifecycle family (join/leave/remove/delete/list) -----------------------


def test_ipc_org_list_reports_the_created_org(app: _FakeApp) -> None:
    ipc_org_create(app, {"org": "group-acme"})

    result = ipc_org_list(app, {})

    assert [item["org"] for item in result["orgs"]] == ["group-acme"]
    item = result["orgs"][0]
    assert item["role"] == "owner"
    assert item["devices"] == 1
    assert item["state"] == "active"


def test_ipc_authority_leave_refuses_without_changing_local_state(app: _FakeApp) -> None:
    ipc_org_create(app, {"org": "group-acme"})
    for confirmed in (False, True):
        with pytest.raises(DaemonRequestError) as refused:
            ipc_org_leave(app, {"org": "group-acme", "confirmed": confirmed})
        assert refused.value.code == (
            "REMOVAL_CONFIRMATION_STALE" if confirmed else "LAST_AUTHORITY_REFUSED")
    assert ipc_org_list(app, {})["leftOrgs"] == []
    assert [row["org"] for row in ipc_org_list(app, {})["orgs"]] == ["group-acme"]


def test_ipc_org_remove_and_delete_round_trip(app: _FakeApp) -> None:
    ipc_org_create(app, {"org": "group-acme"})
    ipc_org_invite(app, {"org": "group-acme", "user": "user:bob@example"})

    pending = ipc_org_remove(app, {"org": "group-acme", "user": "user:bob@example"})
    assert pending["phase"] == "pending-confirmation"
    removed = confirm_ipc_removal(ipc_org_remove, app, {"org": "group-acme", "user": "user:bob@example"})
    assert removed["user"] == "user:bob@example"
    assert removed["phase"] == "complete"
    with pytest.raises(DaemonRequestError) as refused:
        ipc_org_delete(app, {"org": "group-acme"})
    assert refused.value.code == "LAST_AUTHORITY_REFUSED"
    assert [row["org"] for row in ipc_org_list(app, {})["orgs"]] == ["group-acme"]


def test_ipc_org_join_rejects_a_malformed_link(app: _FakeApp) -> None:
    with pytest.raises(DaemonRequestError) as raised:
        ipc_org_join(app, {"link": "https://invite.example.org/wrong#v2.abc"})
    assert raised.value.code == "INVITE_MALFORMED"


@pytest.mark.parametrize(
    ("handler", "service_method"),
    [(ipc_org_join, "join"), (ipc_org_execute, "execute")],
)
def test_ipc_org_join_and_execute_retry_identity_binding_publication(
    app: _FakeApp, handler: Any, service_method: str
) -> None:
    published: list[bool] = []
    app._publish_identity_binding = lambda: published.append(True)

    with patch(
        "hyprial.daemon.impl.org.network.ipc._build_service"
    ) as build_service:
        getattr(build_service.return_value, service_method).return_value = {
            "org": "group-acme",
            "spaceId": "space-1",
            "steps": [],
        }
        result = handler(app, {"link": "v1.payload"})

    assert result["org"] == "group-acme"
    assert published == [True]


def test_a_failed_binding_publication_never_fails_a_completed_join(
    app: _FakeApp,
) -> None:
    logged: list[str] = []

    def publish() -> None:
        raise RuntimeError("orgfs facade is closing")

    app._publish_identity_binding = publish
    app._log = lambda _level, _area, event, **_fields: logged.append(event)

    with patch(
        "hyprial.daemon.impl.org.network.ipc._build_service"
    ) as build_service:
        build_service.return_value.join.return_value = {
            "org": "group-acme",
            "spaceId": "space-1",
            "steps": [],
        }
        result = ipc_org_join(app, {"link": "v1.payload"})

    assert result["org"] == "group-acme"
    assert logged == ["identity.org-binding.publish-after-join-failed"]


@pytest.mark.parametrize(
    ("handler", "params"),
    [
        (ipc_org_join, {}),
        (ipc_org_leave, {}),
        (ipc_org_remove, {"org": "group-acme"}),
        (ipc_org_remove, {"user": "user:bob@example"}),
        (ipc_org_delete, {}),
    ],
)
def test_ipc_org_lifecycle_handlers_validate_required_params(
    app: _FakeApp, handler: Any, params: dict
) -> None:
    with pytest.raises(DaemonRequestError) as raised:
        handler(app, params)
    assert raised.value.code == "INVALID_ARGUMENT"


def _settled(app: _FakeApp, event: str) -> list[dict[str, Any]]:
    return [record for record in app.records if record["event"] == event]


def test_ipc_org_invite_replies_before_the_forwarding_reconcile(app: _FakeApp) -> None:
    """0.5.5.1 production stall (2026-10-28): the reply waited on the reconcile.

    ``org.invite`` returned IPC_TIMEOUT after the CLI's 15s budget although
    the invite was stored every time: the handler ran the post-invite
    forwarding reconcile (``membership-status`` + slot rebinding through the
    serial sidecar I/O owner) synchronously before replying.  Here that
    reconcile is held past a bounded budget: the reply must come back while
    it is still held, and the reconcile must still run on the daemon's
    forwarding cadence and settle with its own ``org.invite.reconcile`` event.
    """
    from hyprial.daemon.impl.application.netendpoints.reconcile_requests import (
        ForwardingReconcileRequests,
    )

    ipc_org_create(app, {"org": "group-acme"})
    held = threading.Event()
    reconciled = threading.Event()

    def reconcile(_observed_at_ms: int | None = None) -> None:
        # The membership read the real reconcile issues, then the stall;
        # bounded so a regression fails instead of hanging the suite.
        app._forwarding_supervisor.membership_request("membership-status", {})
        held.wait(1.0)
        reconciled.set()

    requests = ForwardingReconcileRequests(app._log)
    cadence: BoundedCadence[None] = BoundedCadence(
        "forwarding-reconcile", reconcile, requests.settle
    )
    app._reconcile_forwarding_endpoints = lambda: reconcile()  # type: ignore[method-assign]
    app._request_forwarding_reconcile = (  # type: ignore[method-assign]
        lambda reason, **fields: cadence.submit(requests.record(reason, **fields))
    )
    try:
        result = ipc_org_invite(app, {"org": "group-acme", "user": "bob"})
        assert result["user"] == "bob"
        assert result["link"].startswith("https://")
        assert requests.pending() == 1
        assert not reconciled.is_set(), "the invite reply waited on the forwarding reconcile"
        held.set()
        assert reconciled.wait(2.0)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not _settled(app, "org.invite.reconcile"):
            time.sleep(0.01)
    finally:
        held.set()
        assert cadence.close(2.0)

    settled = _settled(app, "org.invite.reconcile")
    assert len(settled) == 1
    assert settled[0]["level"] == "info"
    assert settled[0]["domain"] == "org"
    fields = settled[0]["fields"]
    assert fields["ok"] is True
    assert fields["code"] is None
    assert fields["org"] == "group-acme"
    assert fields["user"] == "bob"
    assert isinstance(fields["durationMs"], int) and fields["durationMs"] >= 0
    # The invite itself was stored before the reply: the directory holds it.
    assert ("info", "org.org.invite") in app.events


def test_ipc_org_invite_bounds_the_sidecar_call_and_keeps_its_code(app: _FakeApp) -> None:
    """Request-path membership calls are bounded (<=10s) and their code survives.

    0.5.5.1: `invite-create` had the 35s background allowance, and a timeout
    surfaced as an uncoded DAEMON_ERROR after the CLI had already given up.
    The IPC path must ask the sidecar owner for the request bound, and a
    sidecar code (timeout or the sidecar's own refusal) must reach the reply
    as `code`, never flattened.
    """
    from hyprial.daemon.impl.forwarding.policy import REQUEST_MEMBERSHIP_WAIT_SECONDS
    from hyprial.daemon.impl.org.network.ipc import _build_service
    from hyprial.kernel import ipc_errors

    ipc_org_create(app, {"org": "group-acme"})
    sidecar = app._forwarding_supervisor
    sidecar.timeouts.clear()

    ipc_org_invite(app, {"org": "group-acme", "user": "bob"})
    assert ("invite-create", REQUEST_MEMBERSHIP_WAIT_SECONDS) in sidecar.timeouts
    assert REQUEST_MEMBERSHIP_WAIT_SECONDS <= 10.0
    assert all(timeout == REQUEST_MEMBERSHIP_WAIT_SECONDS for _op, timeout in sidecar.timeouts)

    sidecar.failures["invite-create"] = ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT
    with pytest.raises(DaemonRequestError) as timed_out:
        ipc_org_invite(app, {"org": "group-acme", "user": "carol"})
    assert timed_out.value.code == ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT

    sidecar.failures["invite-create"] = "AUTHORITY_REQUIRED"
    with pytest.raises(DaemonRequestError) as refused:
        ipc_org_invite(app, {"org": "group-acme", "user": "dave"})
    assert refused.value.code == "AUTHORITY_REQUIRED"

    # The ``peerKey`` network read is a CLI request path too.
    sidecar.timeouts.clear()
    ipc_org_network(app, {"peerKey": key("server-node-a")})
    assert sidecar.timeouts == [("membership-status", REQUEST_MEMBERSHIP_WAIT_SECONDS)]

    sidecar.timeouts.clear()
    _build_service(app)._membership_request("membership-status", {})
    assert sidecar.timeouts == [("membership-status", REQUEST_MEMBERSHIP_WAIT_SECONDS)]
def test_org_invite_does_not_run_forwarding_reconcile_inline(
    app: _FakeApp,
) -> None:
    """A slow reconcile item must not occupy the management IPC handler."""

    ipc_org_create(app, {"org": "group-acme"})

    class Cadence:
        def __init__(self) -> None:
            self.submitted = 0

        def submit(self, _observed_at_ms: int) -> None:
            self.submitted += 1

    cadence = Cadence()
    app._request_forwarding_reconcile = (  # type: ignore[method-assign]
        lambda _reason, **_fields: (cadence.submit(0), True)[1]
    )
    app._reconcile_forwarding_endpoints = lambda: time.sleep(7)  # type: ignore[method-assign]
    started = time.monotonic()
    ipc_org_invite(app, {"org": "group-acme", "user": "user:bob@example"})
    assert time.monotonic() - started < 1.0
    assert cadence.submitted == 1


def test_org_invite_without_scheduler_never_runs_reconcile_inline(
    app: _FakeApp,
) -> None:
    """No scheduler: record the request, return, and never run I/O on the IPC thread."""

    ipc_org_create(app, {"org": "group-acme"})
    app._request_forwarding_reconcile = None  # type: ignore[method-assign]
    calls: list[str] = []

    def reconcile() -> None:
        calls.append(threading.current_thread().name)

    app._reconcile_forwarding_endpoints = reconcile  # type: ignore[method-assign]
    ipc_org_invite(app, {"org": "group-acme", "user": "user:bob@example"})

    assert calls == []
    assert ("warn", "org.org.reconcile.unscheduled") in app.events
