"""CLI login-session UX: idempotent login and scoped logout."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from hyprial.daemon.impl.configuration.network_profile import NetworkProfile
from hyprial.identity import (
    DeviceRecord,
    IdentityTransactionLock,
    device_key_path,
    write_device_record,
)
from hyprial.kernel import ipc_errors, resolve_node_id
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

runner = CliRunner()
_ISSUER = "https://issuer.example.test"


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    (home / "state").mkdir()
    return home


def _env(home: Path) -> dict[str, str]:
    return {
        "HYPRIAL_HOME": str(home),
        "HARNESS_STATE_DIR": str(home / "state"),
        "HYPRIAL_OWNER": "",
        "HYPRIAL_NETWORK_ISOLATED": "1",
    }


def _seed_device(home: Path) -> None:
    key_path = device_key_path(home)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(b"device-secret\n")
    write_device_record(
        home,
        DeviceRecord(
            device_id=resolve_node_id(),
            owner="alice",
            server_public="nodekey:server",
            client_public="nodekey:client",
            key_generation=1,
        ),
    )


def _seed_login(home: Path) -> None:
    (home / "settings.json").write_text(
        json.dumps(
            {
                "owner": "alice",
                "identityMode": "casdoor",
                "identityIssuer": _ISSUER,
                "userId": "subject-1",
                "username": "alice",
                "identityClaims": {
                    "properties": {"oauth_Lark_unionId": "union-1"}
                },
                "identityBinding": {
                    "user": "alice",
                    "larkUnionId": "union-1",
                    "proof": "proof-1",
                    "publishedAt": "2026-10-20T00:00:00+00:00",
                },
                "unrelated": {"keep": True},
            }
        ),
        encoding="utf-8",
    )
    secrets = home / "secrets"
    secrets.mkdir(mode=0o700)
    (secrets / "login.json").write_text(
        json.dumps(
            {
                "version": 1,
                "issuer": _ISSUER,
                "refreshToken": "secret-refresh-token",
                "obtainedAt": "2026-10-20T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def profile(monkeypatch: pytest.MonkeyPatch) -> NetworkProfile:
    value = NetworkProfile(org="test", issuer=_ISSUER, client_id="public-client")
    monkeypatch.setattr(_cli_get_services(), "resolve_profile", lambda **_kwargs: (value, "profile.json"))
    return value


def test_login_reports_existing_session_without_running_login_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: NetworkProfile
) -> None:
    home = _home(tmp_path)
    _seed_login(home)
    _seed_device(home)
    before = {
        path.relative_to(home): path.read_bytes()
        for path in home.rglob("*")
        if path.is_file()
    }
    monkeypatch.setattr(
        _cli_get_services(),
        "_run_login_cli_flow",
        lambda **_kwargs: pytest.fail("an existing login must not authenticate again"),
    )

    result = runner.invoke(_cli_root_app, ["login", "--json"], env=_env(home))

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "status": "already-logged-in",
        "owner": "alice",
        "issuer": _ISSUER,
        "home": str(home),
        "obtainedAt": "2026-10-20T00:00:00+00:00",
        "expiresAt": None,
    }
    after = {
        path.relative_to(home): path.read_bytes()
        for path in home.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_login_with_complete_session_reauthenticates_when_env_owner_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: NetworkProfile
) -> None:
    home = _home(tmp_path)
    _seed_login(home)
    _seed_device(home)
    calls = 0

    def run(**_kwargs):
        nonlocal calls
        calls += 1
        return {"identity": {"status": "authenticated", "owner": "bob"}}

    monkeypatch.setattr(_cli_get_services(), "_run_login_cli_flow", run)
    environment = _env(home)
    environment["HYPRIAL_OWNER"] = "bob"

    result = runner.invoke(_cli_root_app, ["login", "--json"], env=environment)

    assert result.exit_code == 0, result.output
    assert calls == 1
    assert json.loads(result.stdout)["identity"]["owner"] == "bob"


def test_login_with_incomplete_device_reauthenticates_when_env_owner_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: NetworkProfile
) -> None:
    home = _home(tmp_path)
    _seed_login(home)
    calls = 0

    def run(**_kwargs):
        nonlocal calls
        calls += 1
        return {"identity": {"status": "authenticated", "owner": "bob"}}

    monkeypatch.setattr(_cli_get_services(), "_run_login_cli_flow", run)
    monkeypatch.setattr(
        "hyprial.daemon.impl.network.tailcat.ensure_device_key",
        lambda *_args, **_kwargs: pytest.fail(
            "an env-owner conflict must not resume alice's device stage"
        ),
    )
    environment = _env(home)
    environment["HYPRIAL_OWNER"] = "bob"

    result = runner.invoke(_cli_root_app, ["login", "--json"], env=environment)

    assert result.exit_code == 0, result.output
    assert calls == 1
    assert json.loads(result.stdout)["identity"]["owner"] == "bob"


def test_login_device_resume_refuses_when_identity_transaction_is_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: NetworkProfile
) -> None:
    home = _home(tmp_path)
    _seed_login(home)
    monkeypatch.setattr(
        "hyprial.daemon.impl.network.tailcat.ensure_device_key",
        lambda *_args, **_kwargs: pytest.fail(
            "a busy identity transaction must fence device provisioning"
        ),
    )

    with IdentityTransactionLock.acquire(home):
        result = runner.invoke(_cli_root_app, ["login", "--json"], env=_env(home))

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == "IDENTITY_TRANSACTION_BUSY"


def test_login_device_resume_rejects_session_changed_before_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: NetworkProfile
) -> None:
    home = _home(tmp_path)
    _seed_login(home)
    original_acquire = IdentityTransactionLock.acquire

    def acquire_after_session_change(*args, **kwargs):
        transaction = original_acquire(*args, **kwargs)
        settings_path = home / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        settings["owner"] = "rival-owner"
        settings_path.write_text(json.dumps(settings), encoding="utf-8")
        return transaction

    monkeypatch.setattr(IdentityTransactionLock, "acquire", acquire_after_session_change)
    monkeypatch.setattr(
        "hyprial.daemon.impl.network.tailcat.ensure_device_key",
        lambda *_args, **_kwargs: pytest.fail(
            "a stale stored session must not provision a device"
        ),
    )

    result = runner.invoke(_cli_root_app, ["login", "--json"], env=_env(home))

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == "IDENTITY_TRANSACTION_STALE"


def test_login_device_resume_rechecks_completed_device_under_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: NetworkProfile
) -> None:
    home = _home(tmp_path)
    _seed_login(home)
    original_acquire = IdentityTransactionLock.acquire

    def acquire_after_device_completion(*args, **kwargs):
        transaction = original_acquire(*args, **kwargs)
        _seed_device(home)
        return transaction

    monkeypatch.setattr(IdentityTransactionLock, "acquire", acquire_after_device_completion)
    monkeypatch.setattr(
        "hyprial.daemon.impl.network.tailcat.ensure_device_key",
        lambda *_args, **_kwargs: pytest.fail(
            "a device completed before the fence must not be reprovisioned"
        ),
    )

    result = runner.invoke(_cli_root_app, ["login", "--json"], env=_env(home))

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "already-logged-in"


def test_login_force_runs_login_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: NetworkProfile
) -> None:
    home = _home(tmp_path)
    _seed_login(home)
    seen: list[dict[str, object]] = []

    def run(**kwargs):
        seen.append(kwargs)
        return {"ok": True, "identity": {"status": "authenticated"}}

    monkeypatch.setattr(_cli_get_services(), "_run_login_cli_flow", run)

    result = runner.invoke(_cli_root_app, ["login", "--force", "--json"], env=_env(home))

    assert result.exit_code == 0, result.output
    assert len(seen) == 1
    assert json.loads(result.stdout)["identity"]["status"] == "authenticated"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("identityMode", "local-usage"),
        ("identityIssuer", "https://other-issuer.example.test"),
    ],
)
def test_login_reauthenticates_when_persisted_identity_is_not_coherent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: NetworkProfile,
    field: str,
    value: str,
) -> None:
    home = _home(tmp_path)
    _seed_login(home)
    settings_path = home / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings[field] = value
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    calls = 0

    def run(**_kwargs):
        nonlocal calls
        calls += 1
        return {"identity": {"status": "authenticated"}}

    monkeypatch.setattr(_cli_get_services(), "_run_login_cli_flow", run)

    result = runner.invoke(_cli_root_app, ["login", "--json"], env=_env(home))

    assert result.exit_code == 0, result.output
    assert calls == 1
    assert json.loads(result.stdout)["identity"]["status"] == "authenticated"


def test_logout_removes_only_user_login_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    _seed_login(home)
    agent_credential = home / "agents" / "worker" / "config" / "auth.json"
    agent_credential.parent.mkdir(parents=True)
    agent_credential.write_bytes(b"agent-secret\n")
    device_key = home / "secrets" / "tailcat" / "device-key.json"
    device_key.parent.mkdir(parents=True)
    device_key.write_bytes(b"device-secret\n")
    monkeypatch.setattr(
        _cli_get_services(),
        "_daemon_probe",
        lambda **_kwargs: (_ for _ in ()).throw(
            ipc_errors.DaemonUnavailableError("stopped")
        ),
    )

    result = runner.invoke(_cli_root_app, ["logout", "--yes", "--json"], env=_env(home))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "logged-out"
    assert payload["home"] == str(home)
    assert payload["removed"] == [
        "secrets/login.json",
        "settings.identityBinding",
        "settings.identityClaims",
        "settings.identityIssuer",
        "settings.identityMode",
        "settings.owner",
        "settings.userId",
        "settings.username",
    ]
    assert not (home / "secrets" / "login.json").exists()
    assert json.loads((home / "settings.json").read_text(encoding="utf-8")) == {
        "unrelated": {"keep": True}
    }
    assert agent_credential.read_bytes() == b"agent-secret\n"
    assert device_key.read_bytes() == b"device-secret\n"

    repeated = runner.invoke(
        _cli_root_app, ["logout", "--yes", "--json"], env=_env(home)
    )
    assert repeated.exit_code == 0, repeated.output
    assert json.loads(repeated.stdout) == {
        "ok": True,
        "status": "not-logged-in",
        "home": str(home),
        "removed": [],
    }


def test_logout_stops_live_daemon_before_removing_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    _seed_login(home)
    observed: list[str] = []
    monkeypatch.setattr(
        _cli_get_services(),
        "_daemon_probe",
        lambda **_kwargs: {"running": True, "pid": 42, "epoch": "old"},
    )

    def stop() -> dict[str, object]:
        assert (home / "secrets" / "login.json").exists()
        settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
        assert settings["owner"] == "alice"
        observed.append("stopped")
        return {"pid": 42, "epoch": "old"}

    monkeypatch.setattr(_cli_get_services(), "_stop_daemon_for_identity_switch", stop)

    result = runner.invoke(_cli_root_app, ["logout", "--yes", "--json"], env=_env(home))

    assert result.exit_code == 0, result.output
    assert observed == ["stopped"]
    assert json.loads(result.stdout)["daemon"]["state"] == "stopped"
    assert not (home / "secrets" / "login.json").exists()


def test_logout_requires_yes_in_json_mode_without_writing(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _seed_login(home)
    before = (home / "secrets" / "login.json").read_bytes()

    result = runner.invoke(_cli_root_app, ["logout", "--json"], env=_env(home))

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "CONFIRMATION_REQUIRED"
    assert "--yes" in payload["error"]
    assert (home / "secrets" / "login.json").read_bytes() == before


def test_logout_preserves_non_casdoor_local_identity(tmp_path: Path) -> None:
    home = _home(tmp_path)
    settings = {
        "owner": "local-user",
        "identityMode": "local-usage",
        "identityIssuer": None,
    }
    (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    result = runner.invoke(_cli_root_app, ["logout", "--yes", "--json"], env=_env(home))

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "not-logged-in"
    assert json.loads((home / "settings.json").read_text(encoding="utf-8")) == settings


def test_login_logout_help_documents_session_semantics() -> None:
    root = get_command(_cli_root_app)
    assert "logout" in root.commands

    login_help = runner.invoke(_cli_root_app, ["login", "--help"])
    logout_help = runner.invoke(_cli_root_app, ["logout", "--help"])

    assert login_help.exit_code == 0
    assert "--force" in login_help.output
    assert "valid secrets/login.json" in login_help.output
    assert "Casdoor owner" in login_help.output
    assert "issuers match" in login_help.output
    assert logout_help.exit_code == 0
    assert "--yes" in logout_help.output
    assert "agent credentials" in logout_help.output
    assert "device keys" in logout_help.output
    assert "does not revoke already-issued access tokens at the issuer" in (
        logout_help.output
    )
    assert "valid until expiry" in logout_help.output


def test_logout_docs_warn_that_issuer_tokens_are_not_revoked() -> None:
    repository = Path(__file__).resolve().parents[3]
    guide = (repository / "docs/guides/operator-guide.md").read_text(encoding="utf-8")
    skill = (repository / ".agents/skills/hyprial-e2e/SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "不会撤销 issuer 已签发的 access token" in guide
    assert "过期前仍可能有效" in guide
    assert "does not revoke already-issued access tokens at the issuer" in skill
    assert "valid until expiry" in skill
