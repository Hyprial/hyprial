"""The production onboarding assembly serves a real daemon-owned logout.

These cases drive the real ``_assemble_onboarding`` path — the one production
construction point of the serving port — over a real home, the real identity
snapshot reader, the real operation journal and the real ``auth.logout``
handler binding.  No daemon process, socket or credential worker is started.

The daemon is the single onboarding authority, so a logout accepted by this
port is completed by this port: the identity-owned artifacts are removed, the
receipt reaches a truthful terminal state and the daemon's own stop is
requested after the receipt is durable.  An ownerless home still assembles no
port at all, and ``auth.logout`` answers DAEMON_NOT_READY truthfully there.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.application.onboarding import _OnboardingAssemblyMixin
from hyprial.daemon.impl.application.onboarding.account_lifecycle import ServingOnboardingGateway
from hyprial.daemon.impl.application.onboarding.first_run.port import FirstRunAttemptPort
from hyprial.daemon.impl.onboarding.operations import OperationService
from hyprial.daemon.impl.onboarding.operations import journal
from hyprial.daemon.impl.request_catalog.onboarding.handlers import auth_logout
from hyprial.identity import inspect_logout_artifacts
from hyprial.kernel import DaemonRequestError, ipc_errors

ISSUER = "https://issuer.example.test"
OWNER = "alice@example.test"
EPOCH = "serving-epoch-1"
KEY = "logout-key-0000001"

_LOGIN_FIELDS = {
    "identityIssuer": ISSUER,
    "identityMode": "casdoor",
    "owner": "alice",
    "userId": "subject-1",
    "username": "alice",
}


class _AssemblyApp(_OnboardingAssemblyMixin):
    """The attributes the real assembly mixin reads from its application."""

    def __init__(self, home: Path) -> None:
        self.hyprial_home = home
        self.state_dir = home / "state"
        self.epoch = EPOCH
        self.stop_requests: list[str] = []

    def _request_stop(self, source: str, signal_number: int | None = None) -> None:
        self.stop_requests.append(source)


def _seed_login(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({**_LOGIN_FIELDS, "unrelated": {"keep": True}}), encoding="utf-8"
    )
    (home / "secrets").mkdir(exist_ok=True)
    (home / "secrets" / "login.json").write_text(
        json.dumps({"version": 1, "issuer": ISSUER, "refreshToken": "token-abc"}),
        encoding="utf-8",
    )


def _logout(app: _AssemblyApp, **overrides: object) -> dict[str, object]:
    params: dict[str, object] = {"operationKey": KEY, "confirm": True}
    params.update(overrides)
    return auth_logout(app, params, trusted_message_origin="ipc")


def test_the_assembled_serving_port_completes_logout_and_requests_stop(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _seed_login(home)
    app = _AssemblyApp(home)

    app._assemble_onboarding()
    result = _logout(app)

    assert result["ok"] is True
    assert result["state"] == "completed"
    assert result["loggedOut"] is True
    assert result["nextAction"] == "login"
    assert result["issuerRevocation"] == "not-attempted"
    assert result["removed"] == [
        "secrets/login.json",
        "settings.identityIssuer",
        "settings.identityMode",
        "settings.owner",
        "settings.userId",
        "settings.username",
    ]
    assert not (home / "secrets" / "login.json").exists()
    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert settings == {"unrelated": {"keep": True}}
    assert inspect_logout_artifacts(home).present is False
    # The receipt is durable and terminal before the daemon's own stop.
    state = journal.read_journal(app.state_dir / "onboarding")
    assert state is not None
    assert [
        record.state for record in state.snapshot.records if record.kind == "logout"
    ] == ["completed"]
    assert app.stop_requests == ["auth-logout"]


def test_the_assembled_logout_replays_one_receipt_per_view(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_login(home)
    app = _AssemblyApp(home)
    app._assemble_onboarding()

    first = _logout(app)
    again = _logout(app)
    other_key = _logout(app, operationKey="another-logout-key-02")

    assert other_key["operationId"] == again["operationId"] == first["operationId"]
    state = journal.read_journal(app.state_dir / "onboarding")
    assert state is not None
    assert len(
        [record for record in state.snapshot.records if record.kind == "logout"]
    ) == 1
    assert app.stop_requests == ["auth-logout"]


def test_an_ownerless_home_assembles_no_port_and_stays_not_ready(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True)
    app = _AssemblyApp(home)

    app._assemble_onboarding()

    with pytest.raises(DaemonRequestError) as refused:
        _logout(app)
    assert refused.value.code == ipc_errors.DAEMON_NOT_READY
    assert app.stop_requests == []


def test_known_problem_kp120_env_owner_without_login_is_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registered known problem KP-120, pinned as it is today, not as desired.

    With ``HYPRIAL_OWNER`` set the daemon serves an owner from the environment,
    but a home with no login has no owner in its identity snapshot, so the
    serving assembly installs no account port and a repeated
    ``hyprial logout`` (one ``auth.logout``) is refused with DAEMON_NOT_READY
    instead of an idempotent not-logged-in.  Change this test together with
    the KP-120 entry when the owner decides the behaviour.
    """

    monkeypatch.setenv("HYPRIAL_OWNER", "alice")
    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / "settings.json").write_text(
        json.dumps({"unrelated": {"keep": True}}), encoding="utf-8"
    )
    app = _AssemblyApp(home)

    app._assemble_onboarding()

    with pytest.raises(DaemonRequestError) as refused:
        _logout(app)
    assert refused.value.code == ipc_errors.DAEMON_NOT_READY
    assert app.stop_requests == []
    assert journal.read_journal(app.state_dir / "onboarding") is None


def test_a_stale_expected_epoch_is_refused_before_any_identity_work(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _seed_login(home)
    app = _AssemblyApp(home)
    app._assemble_onboarding()

    with pytest.raises(DaemonRequestError) as refused:
        _logout(app, expectedEpoch="another-generation")

    assert refused.value.code == ipc_errors.STALE_DAEMON_GENERATION
    assert (home / "secrets" / "login.json").exists()
    assert inspect_logout_artifacts(home).present is True
    assert journal.read_journal(app.state_dir / "onboarding") is None
    assert app.stop_requests == []


def test_the_matching_expected_epoch_completes_logout(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_login(home)
    app = _AssemblyApp(home)
    app._assemble_onboarding()

    result = _logout(app, expectedEpoch=EPOCH)

    assert result["state"] == "completed"
    assert result["loggedOut"] is True
    assert not (home / "secrets" / "login.json").exists()
    assert app.stop_requests == ["auth-logout"]


def test_preinstalled_promoted_gateway_gets_reply_service_without_reopening_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    _seed_login(home)
    app = _AssemblyApp(home)
    operations = OperationService.open(tmp_path / "bootstrap-onboarding")
    gateway = ServingOnboardingGateway(operations=operations, home=home)
    app.install_onboarding(gateway)
    app._onboarding_serving = True

    def no_second_writer(*args, **kwargs):
        pytest.fail("promotion must retain its existing operation journal")

    monkeypatch.setattr(OperationService, "open", no_second_writer)
    app._assemble_onboarding()
    assert app.onboarding is gateway
    attempt = getattr(gateway, "first_run_attempt", None)
    assert isinstance(attempt, FirstRunAttemptPort)
    assert attempt._service._onboarding_root == operations.root
    app._assemble_onboarding()
    assert gateway.first_run_attempt is attempt
