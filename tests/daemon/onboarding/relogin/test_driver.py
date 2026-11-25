"""Daemon-driven serving relogin driver tests (L2).

Decision: L2-daemon-relogin-20261122.  An ``auth.relogin`` call with
``until: "serving"`` is driven to completion by the daemon alone: issuer poll,
commit verification (owner + uid), authenticated receipt, recovery
(device + user key + initialize), and terminal ``completion`` snapshot.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace


from hyprial.daemon.impl.application.onboarding.account_lifecycle import (
    ServingOnboardingGateway,
)
from hyprial.daemon.impl.application.onboarding.provisioning import (
    DeviceProvisioningError,
)
from hyprial.daemon.impl.onboarding.operations import OperationService, journal
from hyprial.daemon.impl.request_catalog.onboarding import CallContext, Surface
from hyprial.identity import (
    AuthProfile,
    DeviceRecord,
    IdentityBegin,
    IdentityPublicState,
    IdentitySnapshot,
)
from hyprial.kernel import ipc_errors

OWNER = "alice@example.test"
ISSUER = "https://issuer.example.test"
UID = "uid-alice-001"
OTHER_UID = "uid-bob-999"
RELOGIN_KEY = "relogin-key-000001"


def _serving_context(caller: str = "local-operator") -> CallContext:
    return CallContext(
        surface=Surface("home-binding", "host-control", "view", "serving-1"),
        phase="serving",
        caller=caller,
    )


def _device(owner: str = OWNER) -> DeviceRecord:
    return DeviceRecord(
        device_id="relogin-device-001",
        owner=owner,
        server_public="nodekey:server",
        client_public="nodekey:client",
        key_generation=2,
        device_uid="12345678123442348234123456789abc",
        name="relogin-device",
    )


class _FakeLock:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _PendingIdentity:
    def __init__(
        self,
        *,
        owner: str = OWNER,
        issuer: str = ISSUER,
        user_id: str | None = UID,
        hold: bool = False,
        commit_on_cancel: bool = False,
    ) -> None:
        self.owner = owner
        self.issuer = issuer
        self.user_id = user_id
        self.released = threading.Event()
        if not hold:
            self.released.set()
        self.commit_on_cancel = commit_on_cancel
        self.begins = 0
        self.polls: list[str] = []
        self.cancels = 0
        self.closed = False

    def release(self) -> None:
        self.released.set()

    def begin(self, *, profile, home, permit, lock_fd=None, transport=None):
        del profile, home, lock_fd, transport
        self.begins += 1
        return IdentityBegin(
            permit.operation_id,
            f"{ISSUER}/device",
            "RL-CODE",
            600,
            1,
        )

    def poll_public(self, operation_id: str) -> IdentityPublicState:
        self.polls.append(threading.current_thread().name)
        if not self.released.is_set():
            return IdentityPublicState(operation_id, "pending", retry_after_seconds=1.0)
        return IdentityPublicState(
            operation_id,
            "committed",
            self.owner,
            self.issuer,
            {"credentialCommitted": True, "settingsCommitted": True},
            user_id=self.user_id,
        )

    def cancel(self, operation_id: str) -> IdentityPublicState:
        self.cancels += 1
        if self.commit_on_cancel:
            return IdentityPublicState(
                operation_id,
                "committed",
                self.owner,
                self.issuer,
                {"credentialCommitted": True, "settingsCommitted": True},
                user_id=self.user_id,
            )
        return IdentityPublicState(operation_id, "cancelled")

    def close(self) -> None:
        self.closed = True


class _RecordingProvisioner:
    def __init__(self, state: dict, *, fail: bool = False) -> None:
        self.state = state
        self.fail = fail
        self.calls = 0

    def ensure(self, *, home, owner):
        del home
        self.calls += 1
        if self.fail:
            raise DeviceProvisioningError("controlled sidecar failure")
        device = _device(owner)
        self.state["device"] = device
        return device


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def _harness(
    tmp_path: Path,
    *,
    identity: _PendingIdentity | None = None,
    identity_factory=None,
    device: DeviceRecord | None = None,
    device_fail: bool = False,
    expected_uid: str | None = UID,
):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    service = OperationService.open(tmp_path / "operations")
    state: dict = {"device": device}
    lock = _FakeLock()
    ident = identity or _PendingIdentity()
    factory = identity_factory or (lambda: ident)
    provisioner = _RecordingProvisioner(state, fail=device_fail)
    key_calls: list[tuple[str, object]] = []

    def user_key(_home, _state_dir, owner, dev):
        key_calls.append((owner, dev))
        return {"state": "unbound", "code": ipc_errors.USER_KEY_UNBOUND}

    gateway = ServingOnboardingGateway(
        operations=service,
        home=home,
        identity_reader=lambda _home: IdentitySnapshot(OWNER, "casdoor", ISSUER, expected_uid),
        device_reader=lambda _home: state["device"],
        profile_resolver=lambda _home: AuthProfile(ISSUER, "client-1"),
        identity_factory=factory,
        transaction_factory=lambda _home: lock,
        provisioner=provisioner,
        user_key=user_key,
    )
    return SimpleNamespace(
        home=home,
        service=service,
        gateway=gateway,
        identity=ident,
        lock=lock,
        provisioner=provisioner,
        key_calls=key_calls,
        state=state,
    )


def test_until_serving_completes_without_client_polling(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    try:
        begun = h.gateway.relogin(
            _serving_context(), operation_key=RELOGIN_KEY, until="serving"
        )
        assert begun["state"] == "pending"
        assert begun["verification"]["userCode"] == "RL-CODE"
        assert begun["completion"]["stage"] == "authorizing"

        assert _wait_for(
            lambda: (
                h.gateway.get(_serving_context(), operation_id=begun["operationId"])[
                    "operation"
                ]["completion"]["stage"]
                == "serving"
            )
        )
        got = h.gateway.get(_serving_context(), operation_id=begun["operationId"])
        operation = got["operation"]
        assert operation["state"] == "authenticated"
        assert operation["owner"] == OWNER
        assert operation["verification"] is None
        completion = operation["completion"]
        assert completion["stage"] == "serving"
        assert completion["device"]["ready"] is True
        assert completion["device"]["deviceId"] == "relogin-device-001"
        assert completion["device"]["userKey"] == {
            "state": "unbound",
            "code": ipc_errors.USER_KEY_UNBOUND,
        }
        assert completion["failure"] is None
        assert h.provisioner.calls == 1
        assert len(h.key_calls) == 1
        records = journal.read_journal(h.service.root).snapshot.records
        assert any(r.kind == "initialize" and r.state == "completed" for r in records)
        assert h.lock.closed is True
    finally:
        h.gateway.close()


def test_driver_is_the_only_poller_and_get_is_readonly(tmp_path: Path) -> None:
    ident = _PendingIdentity(hold=True)
    h = _harness(tmp_path, identity=ident)
    try:
        begun = h.gateway.relogin(
            _serving_context(), operation_key=RELOGIN_KEY, until="serving"
        )
        assert _wait_for(lambda: len(ident.polls) >= 1)
        for _ in range(3):
            got = h.gateway.get(_serving_context(), operation_id=begun["operationId"])
            assert got["operation"]["state"] == "pending"
            assert got["operation"]["completion"]["stage"] == "authorizing"
        assert all(name != threading.current_thread().name for name in ident.polls)
        ident.release()
        assert _wait_for(
            lambda: (
                h.gateway.get(_serving_context(), operation_id=begun["operationId"])[
                    "operation"
                ]["completion"]["stage"]
                == "serving"
            )
        )
    finally:
        h.gateway.close()


def test_idempotent_replay_returns_same_operation(tmp_path: Path) -> None:
    ident = _PendingIdentity(hold=True)
    h = _harness(tmp_path, identity=ident)
    try:
        first = h.gateway.relogin(
            _serving_context(), operation_key=RELOGIN_KEY, until="serving"
        )
        replay = h.gateway.relogin(
            _serving_context(), operation_key=RELOGIN_KEY, until="serving"
        )
        assert replay["operationId"] == first["operationId"]
        assert replay["completion"]["stage"] == "authorizing"
        assert ident.begins == 1

        ident.release()
        assert _wait_for(
            lambda: (
                h.gateway.get(_serving_context(), operation_id=first["operationId"])[
                    "operation"
                ]["completion"]["stage"]
                == "serving"
            )
        )
        after = h.gateway.relogin(
            _serving_context(), operation_key=RELOGIN_KEY, until="serving"
        )
        assert after["operationId"] == first["operationId"]
        assert after["state"] == "authenticated"
        assert after["completion"]["stage"] == "serving"
        assert ident.begins == 1
    finally:
        h.gateway.close()


def test_cancel_before_authorized_stops_driver_and_preserves_identity(
    tmp_path: Path,
) -> None:
    ident = _PendingIdentity(hold=True)
    h = _harness(tmp_path, identity=ident)
    try:
        begun = h.gateway.relogin(
            _serving_context(), operation_key=RELOGIN_KEY, until="serving"
        )
        cancelled = h.gateway.cancel(
            _serving_context(), operation_id=begun["operationId"]
        )
        assert cancelled["cancelled"] is True
        assert cancelled["state"] == "cancelled"
        assert _wait_for(
            lambda: (
                h.gateway.get(_serving_context(), operation_id=begun["operationId"])[
                    "operation"
                ]["completion"]["stage"]
                == "stopped"
            )
        )
        got = h.gateway.get(_serving_context(), operation_id=begun["operationId"])
        assert got["operation"]["state"] == "cancelled"
        assert h.gateway.status(_serving_context())["authenticated"] is True
        assert h.lock.closed is True
    finally:
        h.gateway.close()


def test_cancel_after_authorized_keeps_identity_and_finishes_serving(
    tmp_path: Path,
) -> None:
    ident = _PendingIdentity(hold=True, commit_on_cancel=True)
    h = _harness(tmp_path, identity=ident)
    try:
        begun = h.gateway.relogin(
            _serving_context(), operation_key=RELOGIN_KEY, until="serving"
        )
        cancelled = h.gateway.cancel(
            _serving_context(), operation_id=begun["operationId"]
        )
        assert cancelled["cancelled"] is False
        assert cancelled["reason"] == "identity-committed"
        assert _wait_for(
            lambda: (
                h.gateway.get(_serving_context(), operation_id=begun["operationId"])[
                    "operation"
                ]["completion"]["stage"]
                == "serving"
            )
        )
        got = h.gateway.get(_serving_context(), operation_id=begun["operationId"])
        assert got["operation"]["state"] == "authenticated"
        assert got["operation"]["completion"]["device"]["ready"] is True
    finally:
        h.gateway.close()


def test_device_failure_completes_serving_without_revoking_identity(
    tmp_path: Path,
) -> None:
    h = _harness(tmp_path, device_fail=True)
    try:
        begun = h.gateway.relogin(
            _serving_context(), operation_key=RELOGIN_KEY, until="serving"
        )
        assert _wait_for(
            lambda: (
                h.gateway.get(_serving_context(), operation_id=begun["operationId"])[
                    "operation"
                ]["completion"]["stage"]
                == "serving"
            )
        )
        got = h.gateway.get(_serving_context(), operation_id=begun["operationId"])
        op = got["operation"]
        assert op["state"] == "authenticated"
        assert op["owner"] == OWNER
        device = op["completion"]["device"]
        assert device["ready"] is False
        assert device["error"]["code"] == "DEVICE_ENSURE_FAILED"
        assert device["retry"] == "serving-start"
        assert op["completion"]["failure"] is None
        assert h.gateway.account_projection(_serving_context(caller="local-gui")).credential == "committed"
        assert h.gateway.status(_serving_context())["authenticated"] is True
    finally:
        h.gateway.close()


def test_different_uid_with_same_owner_is_rejected_by_verify_commit(
    tmp_path: Path,
) -> None:
    ident = _PendingIdentity(owner=OWNER, user_id=OTHER_UID)
    h = _harness(tmp_path, identity=ident, expected_uid=UID)
    try:
        begun = h.gateway.relogin(
            _serving_context(), operation_key=RELOGIN_KEY, until="serving"
        )
        assert _wait_for(
            lambda: (
                h.gateway.get(_serving_context(), operation_id=begun["operationId"])[
                    "operation"
                ]["completion"]["stage"]
                == "stopped"
            )
        )
        got = h.gateway.get(_serving_context(), operation_id=begun["operationId"])
        assert got["operation"]["state"] == "unknown"
        assert got["operation"]["completion"]["failure"]["code"] == ipc_errors.IDENTITY_CONFLICT
        assert h.gateway.account_projection(_serving_context(caller="local-gui")).credential == "failed"
        assert h.lock.closed is True
    finally:
        h.gateway.close()


def test_relogin_without_until_keeps_legacy_client_polled_behavior(
    tmp_path: Path,
) -> None:
    ident = _PendingIdentity(hold=True)
    h = _harness(tmp_path, identity=ident)
    try:
        begun = h.gateway.relogin(_serving_context(), operation_key=RELOGIN_KEY)
        assert "completion" not in begun
        assert ident.polls == []

        polled = h.gateway.get(_serving_context(), operation_id=begun["operationId"])
        assert "completion" not in polled["operation"]
        assert polled["operation"]["state"] == "pending"
        assert len(ident.polls) == 1

        ident.release()
        done = h.gateway.get(_serving_context(), operation_id=begun["operationId"])
        assert done["operation"]["state"] == "authenticated"
        assert "completion" not in done["operation"]
    finally:
        h.gateway.close()
