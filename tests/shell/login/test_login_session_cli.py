"""CLI login-session UX: idempotent login and the login/logout help text.

Decision: L1 (owner 2026-11-21).  The in-process logout cases moved to
``tests/shell/login/request_logout`` (one ``auth.logout``) and the daemon logout
suites; only the help and documentation checks for logout stay here.
"""

from __future__ import annotations

from tests.support.runtime.environment import home_with_node_id
import json
from pathlib import Path

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from hyprial.daemon.impl.configuration.network_profile import NetworkProfile
from hyprial.identity import (
    DeviceRecord,
    device_key_path,
    write_device_record,
)
from hyprial.kernel import resolve_node_id
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

runner = CliRunner()
_ISSUER = "https://issuer.example.test"


def _home(tmp_path: Path) -> Path:
    home = home_with_node_id(tmp_path / "home")
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
            device_id=resolve_node_id(_env(home)),
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


def test_login_force_and_switch_options_are_removed(
    tmp_path: Path, profile: NetworkProfile
) -> None:
    del profile
    home = _home(tmp_path)
    _seed_login(home)

    for flag in ("--force", "--switch-account", "--dry-run", "--provider"):
        result = runner.invoke(_cli_root_app, ["login", flag, "--json"], env=_env(home))
        assert result.exit_code == 2, (flag, result.output)
        assert "No such option" in result.output, (flag, result.output)


def test_login_logout_help_documents_session_semantics() -> None:
    root = get_command(_cli_root_app)
    assert "logout" in root.commands

    login_help = runner.invoke(_cli_root_app, ["login", "--help"])
    logout_help = runner.invoke(_cli_root_app, ["logout", "--help"])

    assert login_help.exit_code == 0
    assert "--force" not in login_help.output
    assert "--switch-account" not in login_help.output
    assert "--dry-run" not in login_help.output
    assert "until it reaches serving" in login_help.output
    assert "reauthentication" in login_help.output
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
