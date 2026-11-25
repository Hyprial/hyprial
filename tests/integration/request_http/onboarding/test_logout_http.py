"""`auth.logout` over the declared request entry: terminal states and replay.

These cases drive the daemon-side request projection only — the handler binding, the declared wire
shape and the daemon-owned transaction — over the real journal and the real
identity primitive.  No daemon, socket or credential worker is started.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.application.onboarding.account_lifecycle import (
    ServingOnboardingGateway,
)
from hyprial.daemon.impl.onboarding.operations import journal
from hyprial.daemon.impl.onboarding.operations.service import (
    ControllerIdentity,
    OperationService,
)
from hyprial.daemon.impl.request_catalog.onboarding import CallContext, Surface
from hyprial.daemon.impl.request_catalog.onboarding.handlers import auth_logout
from hyprial.identity import IdentitySnapshot
from hyprial.kernel import DaemonRequestError, ipc_errors

ISSUER = "https://issuer.example.test"
OWNER = "alice@example.test"
KEY = "logout-key-0000001"
NOW = 1_700_000_000.0

_FIELDS = {
    "identityIssuer": ISSUER,
    "identityMode": "casdoor",
    "owner": "alice",
    "userId": "subject-1",
    "username": "alice",
}

_REMOVED = [
    "secrets/login.json",
    "settings.identityIssuer",
    "settings.identityMode",
    "settings.owner",
    "settings.userId",
    "settings.username",
]


def _surface() -> Surface:
    return Surface("home-binding", "host-control", "view", "gen-1")


def _context(owner: str | None = OWNER) -> CallContext:
    return CallContext(
        surface=_surface(), phase="serving", caller="local-gui", owner=owner
    )


class _App:
    """The two attributes a daemon request host exposes to the binding."""

    def __init__(self, port) -> None:
        self.onboarding = port

    def onboarding_context(self, *, trusted_message_origin):
        return _context()


class _NotAssembled:
    """The ownerless-home truth: no serving port exists at all."""

    @property
    def onboarding(self):
        raise DaemonRequestError(
            ipc_errors.DAEMON_NOT_READY, "onboarding services are not assembled"
        )


class _Harness:
    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        seed: bool = True,
        writer: ControllerIdentity | None = None,
    ) -> None:
        self.home = tmp_path / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        if seed:
            (self.home / "settings.json").write_text(
                json.dumps({**_FIELDS, "unrelated": {"keep": True}}), encoding="utf-8"
            )
            (self.home / "secrets").mkdir(exist_ok=True)
            (self.home / "secrets" / "login.json").write_text(
                '{"refreshToken":"x"}', encoding="utf-8"
            )
        self.root = tmp_path / "operations"
        self.writer = writer or ControllerIdentity(
            pid=4242, birth_identity="dead-birth"
        )
        monkeypatch.setattr(
            ControllerIdentity, "current", classmethod(lambda cls: self.writer)
        )
        self.service = OperationService.open(self.root, controller=self.writer)
        self.gateway = ServingOnboardingGateway(
            operations=self.service,
            home=self.home,
            identity_reader=lambda _home: IdentitySnapshot(
                owner=OWNER, mode="casdoor", issuer=ISSUER
            ),
            device_reader=lambda _home: None,
            journal_reader=journal.read_journal,
            clock=lambda: NOW,
        )
        self.app = _App(self.gateway)

    def logout(self, *, key: str = KEY, confirm: bool = True, **overrides):
        params: dict[str, object] = {"operationKey": key, "confirm": confirm}
        params.update(overrides)
        return auth_logout(self.app, params)

    def states(self) -> dict[str, str]:
        state = journal.read_journal(self.root)
        if state is None:
            # Nothing was ever accepted, so there is not even a journal yet.
            return {}
        return {item.operation_id: item.state for item in state.snapshot.records}

    def credential(self) -> str:
        return hashlib.sha256(
            (self.home / "secrets" / "login.json").read_bytes()
        ).hexdigest()


def test_without_an_assembled_port_the_entry_is_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)
    credential_before = harness.credential()
    app = _App(_NotAssembled())

    with pytest.raises(DaemonRequestError) as refused:
        auth_logout(app, {"operationKey": KEY, "confirm": True})

    assert refused.value.code == ipc_errors.DAEMON_NOT_READY
    assert harness.states() == {}
    assert harness.credential() == credential_before


def test_a_completed_logout_projects_the_declared_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)

    result = harness.logout()

    assert result["ok"] is True
    assert result["state"] == "completed"
    assert result["loggedOut"] is True
    assert result["removed"] == _REMOVED
    assert result["nextAction"] == "login"
    assert result["issuerRevocation"] == "not-attempted"
    assert result["operationId"]
    assert result["receiptId"]
    assert harness.states() == {result["operationId"]: "completed"}
    assert not (harness.home / "secrets" / "login.json").exists()
    assert json.loads((harness.home / "settings.json").read_text(encoding="utf-8")) == {
        "unrelated": {"keep": True}
    }


def test_the_same_key_replays_the_same_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)

    first = harness.logout()
    again = harness.logout()
    other_key = harness.logout(key="another-logout-key-1")

    assert again["operationId"] == first["operationId"]
    assert other_key["operationId"] == first["operationId"]
    assert len(harness.states()) == 1


def test_an_empty_home_completes_with_no_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch, seed=False)

    result = harness.logout()

    assert result["state"] == "completed"
    assert result["loggedOut"] is True
    assert result["removed"] == []
    assert result["nextAction"] == "login"
    assert harness.states() == {result["operationId"]: "completed"}


def test_a_reconciled_receipt_projects_unknown_and_is_not_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)
    credential_before = harness.credential()
    accepted = harness.service.begin_logout(
        _context(), KEY, "operation-1", "receipt-1"
    )

    # A restart (different writer) reconciles the dead writer's receipt.
    restart = ControllerIdentity(pid=999_999, birth_identity="restart-birth")
    monkeypatch.setattr(ControllerIdentity, "current", classmethod(lambda cls: restart))
    service = OperationService.open(harness.root)
    gateway = ServingOnboardingGateway(
        operations=service,
        home=harness.home,
        identity_reader=lambda _home: IdentitySnapshot(
            owner=OWNER, mode="casdoor", issuer=ISSUER
        ),
        device_reader=lambda _home: None,
        journal_reader=journal.read_journal,
        clock=lambda: NOW,
    )

    replayed = auth_logout(
        _App(gateway), {"operationKey": KEY, "confirm": True}
    )

    assert replayed["operationId"] == accepted.operation_id
    assert replayed["state"] == "unknown"
    assert replayed["loggedOut"] is False
    assert replayed["nextAction"] == "inspect"
    assert harness.states() == {accepted.operation_id: "unknown"}
    assert harness.credential() == credential_before


def test_a_logout_without_confirm_is_refused_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)

    with pytest.raises(DaemonRequestError) as refused:
        harness.logout(confirm=False)

    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert harness.states() == {}


def test_a_stale_expected_epoch_is_refused_over_the_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)
    credential_before = harness.credential()

    with pytest.raises(DaemonRequestError) as refused:
        harness.logout(expectedEpoch="another-generation")

    assert refused.value.code == ipc_errors.STALE_DAEMON_GENERATION
    assert harness.states() == {}
    assert harness.credential() == credential_before


def test_the_matching_expected_epoch_completes_over_the_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch)

    result = harness.logout(expectedEpoch="gen-1")

    assert result["state"] == "completed"
    assert result["loggedOut"] is True
    assert not (harness.home / "secrets" / "login.json").exists()
