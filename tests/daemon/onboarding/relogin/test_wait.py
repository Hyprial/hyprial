"""Parked ``auth.operation.get(waitSeconds)`` and IPC deadline for serving relogin."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.application.onboarding.account_lifecycle import (
    ServingOnboardingGateway,
)
from hyprial.daemon.impl.ipc import server as ipc_server
from hyprial.daemon.impl.onboarding.operations import OperationService, journal
from hyprial.kernel import ipc_errors
from hyprial.identity.impl.identity_transaction import IdentityTransactionLock
from hyprial.identity.impl.onboarding.ports import (
    AuthProfile,
    HttpTransport,
    IdentitySnapshot,
)
from hyprial.identity.impl.onboarding.identity_record import write_settings_identity
from hyprial.identity.impl.onboarding.service import IdentityCredentialService
from tests.fixtures.network.fake_oidc import FakeOidcConfig, fake_oidc

from .test_driver import (
    OWNER,
    RELOGIN_KEY,
    _PendingIdentity,
    _RecordingProvisioner,
    _device,
    _harness,
    _serving_context,
    _wait_for,
)


class _BlockingPollIdentity(_PendingIdentity):
    def __init__(self) -> None:
        super().__init__(hold=True)
        self.poll_started = threading.Event()
        self.poll_release = threading.Event()

    def poll_public(self, operation_id: str):
        self.poll_started.set()
        self.poll_release.wait(timeout=5.0)
        self.release()
        return super().poll_public(operation_id)


class _IdentityFactory:
    def __init__(self, *workers: _PendingIdentity) -> None:
        self._workers = list(workers)

    def __call__(self) -> _PendingIdentity:
        return self._workers.pop(0)


def test_wait_seconds_parks_without_blocking_cancel_and_wakes_on_serving(
    tmp_path: Path,
) -> None:
    ident = _PendingIdentity(hold=True)
    h = _harness(tmp_path, identity=ident, device=_device())
    try:
        begun = h.gateway.relogin(
            _serving_context(), operation_key=RELOGIN_KEY, until="serving"
        )
        results: list[dict] = []
        errors: list[BaseException] = []

        def waiter() -> None:
            try:
                results.append(
                    h.gateway.get(
                        _serving_context(),
                        operation_id=begun["operationId"],
                        wait_seconds=10.0,
                    )
                )
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        assert _wait_for(lambda: len(ident.polls) >= 1)

        status = h.gateway.status(_serving_context())
        assert status["authenticated"] is True and status["owner"] == OWNER

        ident.release()
        thread.join(timeout=10.0)
        assert not thread.is_alive()
        assert errors == []
        assert len(results) == 1
        assert results[0]["operation"]["state"] == "authenticated"
        assert results[0]["operation"]["completion"]["stage"] == "serving"
    finally:
        h.gateway.close()


def test_wait_seconds_timeout_returns_current_projection(tmp_path: Path) -> None:
    ident = _PendingIdentity(hold=True)
    h = _harness(tmp_path, identity=ident, device=_device())
    try:
        begun = h.gateway.relogin(
            _serving_context(), operation_key=RELOGIN_KEY, until="serving"
        )
        started = time.monotonic()
        got = h.gateway.get(
            _serving_context(),
            operation_id=begun["operationId"],
            wait_seconds=0.3,
        )
        elapsed = time.monotonic() - started
        assert 0.25 <= elapsed < 3.0
        assert got["operation"]["state"] == "pending"
        assert got["operation"]["completion"]["stage"] == "authorizing"
    finally:
        h.gateway.close()


def test_cancel_wakes_parked_wait_with_stopped(tmp_path: Path) -> None:
    ident = _PendingIdentity(hold=True)
    h = _harness(tmp_path, identity=ident, device=_device())
    try:
        begun = h.gateway.relogin(
            _serving_context(), operation_key=RELOGIN_KEY, until="serving"
        )
        results: list[dict] = []

        def waiter() -> None:
            results.append(
                h.gateway.get(
                    _serving_context(),
                    operation_id=begun["operationId"],
                    wait_seconds=15.0,
                )
            )

        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        assert _wait_for(lambda: len(ident.polls) >= 1)

        started = time.monotonic()
        cancelled = h.gateway.cancel(
            _serving_context(), operation_id=begun["operationId"]
        )
        thread.join(timeout=5.0)
        assert cancelled["cancelled"] is True
        assert time.monotonic() - started < 3.0
        assert results[0]["operation"]["state"] == "cancelled"
        assert results[0]["operation"]["completion"]["stage"] == "stopped"
    finally:
        h.gateway.close()


def test_cancel_does_not_wait_for_a_late_issuer_poll(tmp_path: Path) -> None:
    ident = _BlockingPollIdentity()
    h = _harness(tmp_path, identity=ident, device=_device())
    try:
        begun = h.gateway.relogin(
            _serving_context(), operation_key=RELOGIN_KEY, until="serving"
        )
        assert ident.poll_started.wait(timeout=2.0)

        result: list[dict] = []
        cancel_thread = threading.Thread(
            target=lambda: result.append(
                h.gateway.cancel(
                    _serving_context(), operation_id=begun["operationId"]
                )
            ),
            daemon=True,
        )
        cancel_thread.start()
        cancel_thread.join(timeout=1.0)
        assert not cancel_thread.is_alive()
        assert result[0]["cancelled"] is True

        ident.poll_release.set()
        assert _wait_for(lambda: not cancel_thread.is_alive())
        assert _wait_for(
            lambda: h.gateway.get(
                _serving_context(), operation_id=begun["operationId"]
            )["operation"]["completion"]["stage"]
            == "stopped"
        )
        got = h.gateway.get(_serving_context(), operation_id=begun["operationId"])
        assert got["operation"]["state"] == "cancelled"
        assert h.provisioner.calls == 0
    finally:
        ident.poll_release.set()
        h.gateway.close()


def test_late_cancelled_poll_does_not_close_the_next_relogin_worker(
    tmp_path: Path,
) -> None:
    first = _BlockingPollIdentity()
    second = _PendingIdentity(hold=True)
    h = _harness(
        tmp_path,
        identity=first,
        identity_factory=_IdentityFactory(first, second),
        device=_device(),
    )
    try:
        first_begin = h.gateway.relogin(
            _serving_context(), operation_key="relogin-key-first", until="serving"
        )
        assert first.poll_started.wait(timeout=2.0)

        cancelled = h.gateway.cancel(
            _serving_context(), operation_id=first_begin["operationId"]
        )
        assert cancelled["cancelled"] is True

        second_begin = h.gateway.relogin(
            _serving_context(), operation_key="relogin-key-second", until="serving"
        )
        assert _wait_for(lambda: len(second.polls) >= 1)

        first.poll_release.set()
        assert _wait_for(
            lambda: h.gateway.get(
                _serving_context(), operation_id=first_begin["operationId"]
            )["operation"]["completion"]["stage"]
            == "stopped"
        )
        assert second.closed is False

        h.gateway._relogin_driver.poke(second_begin["operationId"])
        assert _wait_for(lambda: len(second.polls) >= 2)
        assert second.closed is False

        second.release()
        h.gateway._relogin_driver.poke(second_begin["operationId"])
        assert _wait_for(
            lambda: h.gateway.get(
                _serving_context(), operation_id=second_begin["operationId"]
            )["operation"]["completion"]["stage"]
            == "serving"
        )
    finally:
        first.poll_release.set()
        h.gateway.close()


def test_shutdown_fences_real_worker_after_a_blocked_issuer_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    from hyprial.identity.impl.onboarding.credentials import oidc

    poll_started = threading.Event()
    poll_release = threading.Event()
    real_client = oidc.OidcClient

    class _BlockingClient(real_client):
        def poll_device_token_once(self, endpoints, profile, authorization):
            poll_started.set()
            assert poll_release.wait(timeout=5.0)
            return super().poll_device_token_once(endpoints, profile, authorization)

    monkeypatch.setattr(oidc, "OidcClient", _BlockingClient)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    operations = OperationService.open(tmp_path / "operations")
    state: dict[str, object] = {"device": None}
    real_user_id = "ou_0123456789abcdef0123456789abcdef"
    with fake_oidc(
        FakeOidcConfig(pending_polls=0, preferred_username=OWNER, sub=real_user_id)
    ) as server:
        write_settings_identity(
            OWNER,
            mode="casdoor",
            issuer=server.issuer,
            user_id=real_user_id,
            hyprial_home=home,
        )
        settings_before = (home / "settings.json").read_bytes()
        identity = IdentityCredentialService()
        gateway = ServingOnboardingGateway(
            operations=operations,
            home=home,
            identity_reader=lambda _home: IdentitySnapshot(
                OWNER, "casdoor", server.issuer, real_user_id
            ),
            device_reader=lambda _home: state["device"],
            profile_resolver=lambda _home: AuthProfile(server.issuer, "fake-client-id"),
            identity_factory=lambda: identity,
            transaction_factory=IdentityTransactionLock.acquire,
            transport=HttpTransport(),
            provisioner=_RecordingProvisioner(state),
            user_key=lambda *_args: {"state": "unbound", "code": ipc_errors.USER_KEY_UNBOUND},
        )
        try:
            begun = gateway.relogin(
                _serving_context(), operation_key=RELOGIN_KEY, until="serving"
            )
            assert poll_started.wait(timeout=2.0)
            shutdown_thread = threading.Thread(
                target=gateway._relogin_driver.shutdown, daemon=True
            )
            shutdown_thread.start()
            assert gateway._relogin_driver.stopped_event.wait(timeout=1.0)
            poll_release.set()
            shutdown_thread.join(timeout=2.0)
            assert not shutdown_thread.is_alive()

            assert not (home / "secrets" / "login.json").exists()
            assert (home / "settings.json").read_bytes() == settings_before
            records = journal.read_journal(operations.root).snapshot.records
            assert not any(
                record.operation_id == begun["operationId"]
                and record.state == "authenticated"
                for record in records
            )
        finally:
            poll_release.set()
            gateway.close()


def test_ipc_server_relaxes_deadline_only_for_auth_operation_get_with_wait_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _DummyServer(ipc_server._IpcServerMixin):
        def __init__(self) -> None:
            self._ipc_peer = threading.local()

        def _response(self, line: bytes):
            del line
            return {"version": 1, "id": "req-1", "result": {"ok": True}}

    class _FakeConnection:
        def peer_disconnected(self) -> bool:
            return False

    seen_timeouts: list[float] = []

    class _FakeEvent:
        def wait(self, timeout: float | None = None) -> bool:
            if timeout is not None:
                seen_timeouts.append(timeout)
            return True

        def set(self) -> None:
            return None

    class _FakeResult:
        def __init__(self) -> None:
            self.ready = _FakeEvent()
            self.response = None
            self.error = None

    monkeypatch.setattr(ipc_server, "_IpcRequestResult", _FakeResult)
    monkeypatch.setattr(ipc_server, "_IPC_CLIENT_POLL_INTERVAL", 1000.0)
    server = _DummyServer()

    def run_request(method: str, params: dict) -> float:
        seen_timeouts.clear()
        line = json.dumps({"version": 1, "id": "1", "method": method, "params": params}).encode()
        reply = server._dispatch_ipc_response(
            _FakeConnection(),
            line,
            request_id="1",
            method=method,
            peer_pid=1234,
        )
        assert reply is not None
        assert len(seen_timeouts) == 1
        return seen_timeouts[0]

    assert run_request("auth.operation.get", {"operationId": "op-1", "waitSeconds": 900}) == pytest.approx(915.0)
    assert run_request("auth.operation.get", {"operationId": "op-1", "waitSeconds": 20}) == pytest.approx(35.0)
    assert run_request("auth.operation.get", {"operationId": "op-1"}) == pytest.approx(ipc_server._IPC_REQUEST_TIMEOUT)
    assert run_request("ping", {"waitSeconds": 900}) == pytest.approx(ipc_server._IPC_REQUEST_TIMEOUT)
    assert ipc_errors.IPC_TIMEOUT == "IPC_TIMEOUT"
