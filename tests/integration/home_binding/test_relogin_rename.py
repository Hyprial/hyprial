"""Integration tests for home binding projections, D2 rename, and L3 removals."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

import hyprial.shell.impl.cli.runtime.entry  # noqa: F401
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.application.onboarding.account_lifecycle import (
    ServingOnboardingGateway,
)
from hyprial.daemon.impl.onboarding.operations import OperationService
from hyprial.daemon.impl.onboarding.runtime import BootstrapRuntime
from hyprial.daemon.impl.request_catalog.onboarding import CallContext, Surface
from hyprial.identity import (
    DIFFERENT_ACCOUNT_REASON,
    HOME_ACCOUNT_MIGRATION_GUIDE,
    LEGACY_BUSINESS_DATABASES,
    LEGACY_BUSINESS_TEXT_FILES,
    LEGACY_USER_KEYS_DIR,
    AgentRegistry,
    AuthProfile,
    DeviceRecord,
    HomeBinding,
    IdentityCredentialService,
    IdentityTransactionLock,
    device_record_path,
    home_account_conflict_message,
    read_device_record,
    read_home_binding,
    read_identity_snapshot,
    remove_logout_artifacts,
    write_device_record,
)
from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import CliError
from hyprial.shell.impl.cli.commands.login import first_login
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _deep_module_daemon_dependencies,
)
from tests.fixtures.network.fake_oidc import FakeOidcConfig, fake_oidc

from tests.identity.onboarding.home_binding.support import (
    CLIENT_ID,
    OTHER_UID,
    PERMIT_LEDGER_NAME,
    PRIMARY_OWNER,
    PRIMARY_UID,
    RENAMED_OWNER,
    create_home,
    expected_home_account,
    populate_legacy_item,
    snapshot_bytes,
    write_settings_json,
)


class _RecordingProvisioner:
    def ensure(self, *, home: Path, owner: str) -> DeviceRecord:
        existing = read_device_record(home)
        if existing is not None and existing.owner == owner:
            return existing
        device = DeviceRecord(
            device_id="home-binding-device-001",
            owner=owner,
            server_public="nodekey:server",
            client_public="nodekey:client",
            key_generation=1,
            device_uid="12345678123442348234123456789abc",
            name="home-binding-device",
        )
        write_device_record(home, device)
        return device


def _wait_until(predicate: Any, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition was not met before timeout")


def _surface() -> Surface:
    return Surface("home-binding", "host-control", "view", "generation-1")


def _seed_business_tree(home: Path, owner: str = PRIMARY_OWNER) -> list[Path]:
    seeded: list[Path] = []
    for db in LEGACY_BUSINESS_DATABASES:
        seeded.append(populate_legacy_item(home, f"state/{db}", f"db-{db}".encode()))
    for filename in LEGACY_BUSINESS_TEXT_FILES:
        seeded.append(
            populate_legacy_item(home, f"state/{filename}", f"text-{filename}".encode())
        )
    device = DeviceRecord(
        device_id="home-binding-device-001",
        owner=owner,
        server_public="nodekey:server",
        client_public="nodekey:client",
        key_generation=1,
        device_uid="12345678123442348234123456789abc",
        name="home-binding-device",
    )
    write_device_record(home, device)
    seeded.append(device_record_path(home))
    seeded.append(
        populate_legacy_item(
            home,
            "state/tailcat/machine-binding.json",
            json.dumps(
                {
                    "version": 1,
                    "machineFingerprint": "0" * 64,
                    "firstPublishedAt": None,
                }
            ).encode(),
        )
    )
    seeded.append(
        populate_legacy_item(
            home,
            f"{LEGACY_USER_KEYS_DIR}/{owner}/key.json",
            json.dumps({"owner": owner, "casdoorSub": PRIMARY_UID}).encode(),
        )
    )
    return seeded


def _make_gateway(home: Path, service: OperationService, issuer: str) -> ServingOnboardingGateway:
    return ServingOnboardingGateway(
        operations=service,
        home=home,
        identity_reader=read_identity_snapshot,
        device_reader=read_device_record,
        profile_resolver=lambda _home: AuthProfile(issuer, CLIENT_ID),
        identity_factory=IdentityCredentialService,
        transaction_factory=IdentityTransactionLock.acquire,
        provisioner=_RecordingProvisioner(),
        user_key=lambda _home, _state_dir, _owner, _dev: {
            "state": "unbound",
            "code": ipc_errors.USER_KEY_UNBOUND,
        },
    )


def test_bootstrap_home_account_conflict_projects_identity_conflict_and_cli_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bootstrap login projects ``HOME_ACCOUNT_CONFLICT`` to ``IDENTITY_CONFLICT`` with zero writes."""

    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    home = create_home(tmp_path)
    populate_legacy_item(home, "state/agents.sqlite3")
    config = FakeOidcConfig(pending_polls=0, sub=OTHER_UID, preferred_username="bob")
    with fake_oidc(config) as server:
        write_settings_json(
            home,
            {
                "version": 1,
                "homeAccount": expected_home_account(
                    server.issuer, PRIMARY_UID, PRIMARY_OWNER
                ),
            },
        )
        watched = [
            home / "settings.json",
            home / "secrets" / "login.json",
            home / PERMIT_LEDGER_NAME,
        ]
        before = snapshot_bytes(watched)
        runtime = BootstrapRuntime.admit(
            home=home,
            state_dir=home / "state",
            socket_path=home / "ipc.sock",
            identity_factory=IdentityCredentialService,
        )
        surface = _surface()
        context = CallContext(surface=surface, phase="bootstrap", caller="local-bootstrap")
        try:
            runtime.begin_authentication(
                surface=surface,
                intent="login",
                operation_key="boot-conflict-key",
                operation_id="op-boot-conflict",
                profile=AuthProfile(server.issuer, CLIENT_ID),
            )
            state = runtime.poll_authentication(surface=surface)
            assert state.state == "failed"
            assert state.error_code == "HOME_ACCOUNT_CONFLICT"
            assert runtime.phase == "awaiting-auth"

            record = runtime.get_operation(context, "op-boot-conflict")
            assert record.state == "failed"
            assert record.error is not None
            assert record.error.code == ipc_errors.IDENTITY_CONFLICT
            expected_msg = home_account_conflict_message(DIFFERENT_ACCOUNT_REASON)
            assert record.error.message == expected_msg
            assert record.error.data == {
                "reason": DIFFERENT_ACCOUNT_REASON,
                "guide": HOME_ACCOUNT_MIGRATION_GUIDE,
                "loginUserId": OTHER_UID,
                "issuer": server.issuer,
            }
            assert snapshot_bytes(watched) == before
        finally:
            runtime.close()

    answer = {
        "ok": True,
        "operation": {
            "operationId": "op-boot-conflict",
            "kind": "login",
            "state": "failed",
            "owner": None,
            "receiptId": None,
            "verification": None,
            "completion": {
                "stage": "stopped",
                "identity": None,
                "device": None,
                "failure": {
                    "code": ipc_errors.IDENTITY_CONFLICT,
                    "message": expected_msg,
                },
            },
            "error": record.error.as_dict(),
        },
    }
    with pytest.raises(CliError) as raised:
        first_login._result(
            get_services(),
            answer,
            {},
            started=False,
            json_output=True,
        )
    assert raised.value.code == ipc_errors.IDENTITY_CONFLICT
    assert str(raised.value) == expected_msg
    assert raised.value.data["reason"] == DIFFERENT_ACCOUNT_REASON
    assert raised.value.data["guide"] == HOME_ACCOUNT_MIGRATION_GUIDE
    assert raised.value.data["loginUserId"] == OTHER_UID


def test_serving_relogin_driven_and_polled_reject_different_uid_zero_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Serving relogin rejects different-uid login on both driven and polled paths."""

    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    home = create_home(tmp_path)
    populate_legacy_item(home, "state/inbox.sqlite3")
    service = OperationService.open(tmp_path / "operations")
    surface = _surface()
    context = CallContext(surface=surface, phase="serving", caller="local-operator")
    config = FakeOidcConfig(pending_polls=0, sub=OTHER_UID, preferred_username="bob")
    with fake_oidc(config) as server:
        write_settings_json(
            home,
            {
                "version": 1,
                "owner": PRIMARY_OWNER,
                "username": PRIMARY_OWNER,
                "identityMode": "casdoor",
                "identityIssuer": server.issuer,
                "userId": PRIMARY_UID,
                "homeAccount": expected_home_account(
                    server.issuer, PRIMARY_UID, PRIMARY_OWNER
                ),
            },
        )
        watched = [
            home / "settings.json",
            home / "secrets" / "login.json",
            home / PERMIT_LEDGER_NAME,
        ]
        before = snapshot_bytes(watched)
        gateway = _make_gateway(home, service, server.issuer)
        try:
            driven = gateway.relogin(
                context, operation_key="driven-conflict-0001", until="serving"
            )
            _wait_until(
                lambda: (
                    gateway.get(context, operation_id=driven["operationId"])["operation"][
                        "completion"
                    ]["stage"]
                    == "stopped"
                )
            )
            driven_op = gateway.get(context, operation_id=driven["operationId"])["operation"]
            assert driven_op["state"] == "failed"
            assert driven_op["error"]["code"] == ipc_errors.IDENTITY_CONFLICT
            assert driven_op["error"]["message"] == home_account_conflict_message(
                DIFFERENT_ACCOUNT_REASON
            )
            assert driven_op["error"]["data"] == {
                "reason": DIFFERENT_ACCOUNT_REASON,
                "guide": HOME_ACCOUNT_MIGRATION_GUIDE,
                "loginUserId": OTHER_UID,
                "issuer": server.issuer,
            }
            assert (
                driven_op["completion"]["failure"]["code"]
                == ipc_errors.IDENTITY_CONFLICT
            )
            assert snapshot_bytes(watched) == before

            polled_begin = gateway.relogin(context, operation_key="polled-conflict-0001")
            polled_op = gateway.get(
                context, operation_id=polled_begin["operationId"]
            )["operation"]
            assert polled_op["state"] == "failed"
            assert polled_op["error"]["code"] == ipc_errors.IDENTITY_CONFLICT
            assert polled_op["error"]["data"]["reason"] == DIFFERENT_ACCOUNT_REASON
            assert snapshot_bytes(watched) == before
        finally:
            gateway.close()


def test_serving_relogin_and_post_logout_bootstrap_rename_update_status_username(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same-uid rename updates ``onboarding.status.username`` while preserving ``owner``."""

    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    serving_home = create_home(tmp_path, "serving-home")
    seeded = _seed_business_tree(serving_home)
    service = OperationService.open(tmp_path / "operations")
    surface = _surface()
    context = CallContext(surface=surface, phase="serving", caller="local-operator")
    config = FakeOidcConfig(
        pending_polls=0, sub=PRIMARY_UID, preferred_username=RENAMED_OWNER
    )
    with fake_oidc(config) as server:
        write_settings_json(
            serving_home,
            {
                "version": 1,
                "owner": PRIMARY_OWNER,
                "username": PRIMARY_OWNER,
                "identityMode": "casdoor",
                "identityIssuer": server.issuer,
                "userId": PRIMARY_UID,
                "homeAccount": expected_home_account(
                    server.issuer, PRIMARY_UID, PRIMARY_OWNER
                ),
            },
        )
        before = snapshot_bytes(seeded)
        gateway = _make_gateway(serving_home, service, server.issuer)
        try:
            begun = gateway.relogin(
                context, operation_key="same-uid-rename-serving", until="serving"
            )
            _wait_until(
                lambda: (
                    gateway.get(context, operation_id=begun["operationId"])["operation"][
                        "completion"
                    ]["stage"]
                    == "serving"
                )
            )
            op = gateway.get(context, operation_id=begun["operationId"])["operation"]
            assert op["state"] == "authenticated"
            assert op["owner"] == PRIMARY_OWNER
            status = gateway.status(context)
            assert status["owner"] == PRIMARY_OWNER
            assert status["username"] == RENAMED_OWNER
            assert snapshot_bytes(seeded) == before
        finally:
            gateway.close()

        boot_home = create_home(tmp_path, "boot-home")
        boot_seeded = _seed_business_tree(boot_home)
        write_settings_json(
            boot_home,
            {
                "version": 1,
                "owner": PRIMARY_OWNER,
                "username": PRIMARY_OWNER,
                "identityMode": "casdoor",
                "identityIssuer": server.issuer,
                "userId": PRIMARY_UID,
                "homeAccount": expected_home_account(
                    server.issuer, PRIMARY_UID, PRIMARY_OWNER
                ),
            },
        )
        populate_legacy_item(
            boot_home, "secrets/login.json", b'{"version":1,"refreshToken":"old"}'
        )
        remove_logout_artifacts(boot_home)
        assert read_home_binding(boot_home) == HomeBinding(
            issuer=server.issuer, user_id=PRIMARY_UID, owner=PRIMARY_OWNER
        )
        boot_before = snapshot_bytes(boot_seeded)
        runtime = BootstrapRuntime.admit(
            home=boot_home,
            state_dir=boot_home / "state",
            socket_path=boot_home / "ipc.sock",
            identity_factory=IdentityCredentialService,
        )
        try:
            runtime.begin_authentication(
                surface=surface,
                intent="login",
                operation_key="post-logout-boot-rename",
                operation_id="op-boot-rename",
                profile=AuthProfile(server.issuer, CLIENT_ID),
            )
            state = runtime.poll_authentication(surface=surface)
            assert state.state == "committed"
            assert state.owner == PRIMARY_OWNER
            authenticated_context = CallContext(
                surface=surface,
                phase="bootstrap",
                caller="local-bootstrap",
                owner=PRIMARY_OWNER,
            )
            status = runtime.status(authenticated_context)
            assert status["owner"] == PRIMARY_OWNER
            assert status["username"] == RENAMED_OWNER
            assert snapshot_bytes(boot_seeded) == boot_before
            assert (
                runtime.get_operation(authenticated_context, "op-boot-rename").account_owner
                == PRIMARY_OWNER
            )
        finally:
            runtime.close()


@pytest.mark.parametrize("flag", ["--switch-account", "--dry-run", "--force"])
def test_cli_login_retired_switch_flags_report_unknown_option(flag: str) -> None:
    """Removed account-switch CLI flags fail as unknown options (exit code 2)."""

    result = CliRunner().invoke(_cli_root_app, ["login", flag])
    assert result.exit_code == 2
    output = (result.stdout + result.stderr).lower()
    assert "no such option" in output or flag in output


def test_daemon_startup_omits_ping_migration_and_never_rewrites_databases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Daemon startup never runs owner_migration and ``ping`` omits ``migration``."""

    home = create_home(tmp_path)
    state_dir = home / "state"
    state_dir.mkdir(parents=True)
    agents_db = state_dir / "agents.sqlite3"
    registry = AgentRegistry(agents_db, owner="old-owner", machine="l3-node")
    registry.create("worker")
    registry.close()

    application = DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id="l3-node",
        hyprial_home=home,
        owner=PRIMARY_OWNER,
        **_deep_module_daemon_dependencies(),
    )
    try:
        assert not hasattr(application, "_owner_migration_rewrites")
        monkeypatch.setattr(application, "_start_runtime", lambda: None)
        monkeypatch.setattr(application, "_start_server", lambda: None)
        monkeypatch.setattr(application, "_restore_adapters", lambda: None)
        monkeypatch.setattr(application, "_write_pid_file", lambda: None)
        monkeypatch.setattr(application, "_serve", lambda: None)
        application._ipc_peer = SimpleNamespace(pid=None)
        application.run()
        assert application._restore_done.wait(5.0)

        ping = application.handle("ping", {})
        assert ping["running"] is True
        assert "migration" not in ping
    finally:
        domains = getattr(application, "_agent_session_domains", None)
        if domains is not None:
            domains.close(2.0)

    check = sqlite3.connect(agents_db)
    try:
        owners = [
            row[0]
            for row in check.execute("SELECT owner FROM agents WHERE actor = 'worker'")
        ]
    finally:
        check.close()
    assert owners == ["old-owner"]


def test_serving_and_bootstrap_share_the_default_conflict_reason() -> None:
    """A ``HOME_ACCOUNT_CONFLICT`` without a usable reason reads the same on both surfaces."""

    from hyprial.daemon.impl.application.onboarding.lifecycle.relogin import (
        _translated_identity_error,
    )
    from hyprial.daemon.impl.onboarding.lifecycle.identity import (
        project_home_account_conflict,
    )
    from hyprial.identity import IdentityError, IdentityPublicState

    expected = home_account_conflict_message(DIFFERENT_ACCOUNT_REASON)
    serving = _translated_identity_error(
        IdentityError("HOME_ACCOUNT_CONFLICT", "conflict", data={"reason": None})
    )
    bootstrap = project_home_account_conflict(
        IdentityPublicState(
            operation_id="op-default-reason",
            state="failed",
            receipt={"reason": None},
            error_code="HOME_ACCOUNT_CONFLICT",
        )
    )

    assert serving.code == ipc_errors.IDENTITY_CONFLICT
    assert bootstrap.code == ipc_errors.IDENTITY_CONFLICT
    assert str(serving) == expected
    assert bootstrap.message == expected


def test_serving_relogin_binds_a_logged_in_pre_l3_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The KP-121 workaround: daemon relogin on a logged-in home without ``homeAccount``."""

    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    home = create_home(tmp_path, "pre-l3-home")
    seeded = _seed_business_tree(home)
    service = OperationService.open(tmp_path / "operations")
    context = CallContext(surface=_surface(), phase="serving", caller="local-operator")
    config = FakeOidcConfig(
        pending_polls=0, sub=PRIMARY_UID, preferred_username=PRIMARY_OWNER
    )
    with fake_oidc(config) as server:
        write_settings_json(
            home,
            {
                "version": 1,
                "owner": PRIMARY_OWNER,
                "identityMode": "casdoor",
                "identityIssuer": server.issuer,
                "userId": PRIMARY_UID,
            },
        )
        before = snapshot_bytes(seeded)
        gateway = _make_gateway(home, service, server.issuer)
        try:
            begun = gateway.relogin(
                context, operation_key="pre-upgrade-relogin", until="serving"
            )
            _wait_until(
                lambda: (
                    gateway.get(context, operation_id=begun["operationId"])["operation"][
                        "completion"
                    ]["stage"]
                    == "serving"
                )
            )
            op = gateway.get(context, operation_id=begun["operationId"])["operation"]
            assert op["state"] == "authenticated"
            assert op["owner"] == PRIMARY_OWNER
        finally:
            gateway.close()

    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert settings["homeAccount"] == expected_home_account(
        server.issuer, PRIMARY_UID, PRIMARY_OWNER
    )
    assert snapshot_bytes(seeded) == before
