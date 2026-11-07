"""``hyprial login`` CLI surface: flags, --json shape, and the no-secret boundary.

The CLI layer is what T10's stdout/stderr halves actually see, so the
secret scan runs here against the real CliRunner output (and the log files
under the home), on top of the flow-level scan in test_login.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'tests/fixtures'))

from hyprial.daemon.impl.configuration.network_profile import NetworkProfile
from hyprial.daemon.impl.network import tailcat as tailcat_module
from hyprial.identity.impl.device import DeviceRecord
from hyprial.kernel import DaemonLaunchResult, ipc_errors
from tests.fixtures.network.fake_oidc import FakeOidcConfig, fake_oidc  # noqa: E402

runner = CliRunner()


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "cli-home"
    home.mkdir(mode=0o700)
    return home


def _device_record(owner: str = "alicesmith") -> DeviceRecord:
    return DeviceRecord(
        device_id="cli-test-node",
        owner=owner,
        server_public="nodekey:server",
        client_public="nodekey:client",
        key_generation=1,
    )


def _invoke(server, home: Path, *args: str):
    state = home / "state"
    state.mkdir(exist_ok=True)
    return runner.invoke(
        _cli_root_app,
        ["login", *args],
        env={
            "HYPRIAL_HOME": str(home),
            "HARNESS_STATE_DIR": str(state),
            "HYPRIAL_PEER_DISCOVERY": "0",
            "HYPRIAL_OWNER": "",  # the autouse fixture's override would trip T7
            "HYPRIAL_ZENOH_LISTEN": "",
            "HYPRIAL_ZENOH_CONNECT": "",
        },
    )


def _seam_profile(server) -> NetworkProfile:
    """The test seam: production issuers are https by profile validation,
    the in-repo fake is plain http, so the CLI's resolve step is handed the
    fake's profile directly (everything downstream — home, writes, output —
    is real)."""

    return NetworkProfile(
        org="acme",
        issuer=server.issuer,
        client_id=server.config.client_id,
    )


@pytest.fixture()
def _wired(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Wire cli.login's resolve step to a started fake and yield the home."""

    def wire(server) -> Path:
        home = _home(tmp_path)
        profile = _seam_profile(server)
        monkeypatch.setattr(
            _cli_get_services(), "resolve_profile", lambda environ=None, **kw: (profile, "profile.json")
        )
        monkeypatch.setattr(
            _cli_get_services(),
            "_daemon_probe",
            lambda **_kwargs: (_ for _ in ()).throw(
                ipc_errors.DaemonUnavailableError("stopped")
            ),
        )
        monkeypatch.setattr(
            _cli_get_services(),
            "_launch_daemon_process",
            lambda **_kwargs: DaemonLaunchResult.launched(
                {
                    "running": True,
                    "pid": 4242,
                    "epoch": "cli-test-new",
                    "phase": "restoring",
                    "owner": "alicesmith",
                    "identityMode": "casdoor",
                    "identityIssuer": profile.issuer,
                    "migration": {"status": "applied"},
                }
            ),
        )
        # The Tailcat sidecar is not installed in the test home; the device
        # stage is stubbed at the tailcat module seam (the flow imports it
        # lazily), so the happy path sees a provisioned device key.
        monkeypatch.setattr(
            tailcat_module,
            "ensure_device_key",
            lambda home_path, *, owner, device_id, environ=None: _device_record(owner),
        )
        return home

    return wire


def test_login_json_output_shape_and_no_secrets(_wired, tmp_path: Path) -> None:
    with fake_oidc() as server:
        home = _wired(server)
        result = _invoke(server, home, "--json", "--no-open")
    assert result.exit_code == 0, result.output
    # stdout is exactly one JSON value (the result object).
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["identity"] == {
        "status": "authenticated",
        "owner": "alicesmith",
        "mode": "casdoor",
        "issuer": server.issuer,
        "source": "profile.json",
        "committed": True,
    }
    # The tailnet-cutover device stage replaces the join report: no
    # control-plane kind, no join mode, just the provisioned device key.
    assert payload["device"] == {
        "ready": True,
        "deviceId": "cli-test-node",
        "owner": "alicesmith",
        "keyGeneration": 1,
    }
    assert "network" not in payload
    assert payload["userCode"] == "FAKE-CODE"
    assert payload["verificationUri"].startswith(server.base_url)
    # The device code and both tokens appear nowhere: not in stdout, not in
    # stderr (the progress channel), not in any file under the home.
    config = server.config
    for secret in (
        config.device_code,
        config.access_token,
        config.refresh_token,
        config.id_token,
    ):
        assert secret not in result.stdout
        assert secret not in (result.stderr or "")
        for path in home.rglob("*"):
            if path.is_file() and path.name != "login.json":
                assert secret not in path.read_text(encoding="utf-8", errors="replace")
    # The credential carries only the refresh token, at 0600.
    login_file = home / "secrets" / "login.json"
    record = json.loads(login_file.read_text(encoding="utf-8"))
    assert set(record) == {
        "version", "issuer", "refreshToken", "obtainedAt", "identityGeneration",
    }
    assert record["identityGeneration"] == 1
    assert record["refreshToken"] == config.refresh_token
    assert config.access_token not in login_file.read_text(encoding="utf-8")
    assert login_file.stat().st_mode & 0o777 == 0o600
    # Owner landed.
    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert settings["owner"] == "alicesmith"


def test_login_human_output_prints_uri_and_code(_wired, tmp_path: Path) -> None:
    with fake_oidc() as server:
        home = _wired(server)
        result = _invoke(server, home, "--no-open")
    assert result.exit_code == 0, result.output
    assert "FAKE-CODE" in result.output
    assert any(
        server.config.device_code not in line
        for line in result.output.splitlines()
    )
    assert server.config.device_code not in result.output
    assert "authenticated as alicesmith" in result.output
    # Member retest 2026-09-28: the Feishu path on the device page never
    # returns to the confirmation, so the prompt names the path that works.
    assert "使用已有账户登录" in result.output
    assert "sign in with an existing account" in result.output
    # A person with no account has nowhere to go on the device page: its sign-in
    # form has no sign-up entry, so the code just expires.  The hint therefore
    # names the page that creates the account (infra-ops, 2026-10-20).
    assert f"{server.issuer}/signup/app-hyprial" in result.output


def test_login_prefers_verification_uri_complete(_wired, tmp_path: Path) -> None:
    """RFC 8628's optional convenience URL wins when the server sends it
    (Casdoor v3.144.0 does not — the fake only does when told to)."""

    config = FakeOidcConfig(verification_uri_complete="http://127.0.0.1:9/complete")
    with fake_oidc(config) as server:
        home = _wired(server)
        result = _invoke(server, home, "--json", "--no-open")
    payload = json.loads(result.stdout)
    assert payload["verificationUri"] == "http://127.0.0.1:9/complete"


def test_login_browser_failure_degrades_to_print(_wired, tmp_path: Path) -> None:
    """webbrowser failing is not an error — the printed URI is the fallback
    (same surface as --no-open)."""

    import hyprial.shell.impl.login.flow as login_module

    original = login_module._default_open
    login_module._default_open = lambda _url: False
    try:
        with fake_oidc() as server:
            home = _wired(server)
            result = _invoke(server, home)
    finally:
        login_module._default_open = original
    assert result.exit_code == 0, result.output
    assert "FAKE-CODE" in result.output


def test_login_owner_mismatch_json_names_the_flag(_wired, tmp_path: Path) -> None:
    with fake_oidc() as server:
        home = _wired(server)
        (home / "settings.json").write_text(
            json.dumps({"owner": "old-owner"}), encoding="utf-8"
        )
        result = _invoke(server, home, "--json", "--no-open")
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == "OWNER_MISMATCH"
    assert payload["data"]["flag"] == "--switch-account"
    assert payload["data"]["currentOwner"] == "old-owner"
    assert payload["data"]["loginOwner"] == "alicesmith"
    # D-U2-4: the structured error itself carries the flag name.
    assert "--switch-account" in json.dumps(payload)
    # Zero writes: the pre-existing settings file is untouched, no secrets.
    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert settings == {"owner": "old-owner"}
    assert not (home / "secrets" / "login.json").exists()


def test_login_switch_account_switches_when_daemon_stopped(_wired, tmp_path: Path) -> None:
    """U5: with the daemon stopped, ``--json --switch-account`` is the
    confirmed switch (the flag is the confirmation under --json) — it
    writes and reports identity.status=switched."""

    with fake_oidc() as server:
        home = _wired(server)
        (home / "settings.json").write_text(
            json.dumps({"owner": "old-owner"}), encoding="utf-8"
        )
        result = _invoke(server, home, "--switch-account", "--json", "--no-open")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    identity = payload["identity"]
    assert identity["status"] == "switched"
    assert identity["previousOwner"] == "old-owner"
    assert identity["owner"] == "alicesmith"
    assert payload["daemon"]["state"] == "running"
    assert payload["migration"]["status"] == "applied"
    assert (home / "secrets" / "login.json").exists()


def test_login_switch_account_refuses_while_daemon_runs(_wired, tmp_path: Path) -> None:
    """U5/D-U5-1: a live .active_daemon heartbeat turns the switch into a
    DAEMON_RUNNING refusal with zero writes — login never stops the daemon."""

    import os
    import time

    from hyprial.daemon.impl.mcp.channel.ownership import _read_process_identity

    with fake_oidc() as server:
        home = _wired(server)
        (home / "settings.json").write_text(
            json.dumps({"owner": "old-owner"}), encoding="utf-8"
        )
        (home / ".active_daemon").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "pid": os.getpid(),
                    "processIdentity": _read_process_identity(os.getpid()),
                    "generation": "cli-test",
                    "heartbeatMonotonic": time.monotonic(),
                    "keepaliveDurationSeconds": 5.0,
                }
            ),
            encoding="utf-8",
        )
        result = _invoke(
            server,
            home,
            "--switch-account",
            "--no-daemon",
            "--json",
            "--no-open",
        )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "DAEMON_RUNNING"
    assert "hyprial daemon stop" in payload["error"]
    assert not (home / "secrets" / "login.json").exists()


def test_login_override_conflict_json(_wired, tmp_path: Path) -> None:
    with fake_oidc() as server:
        home = _wired(server)
        result = runner.invoke(
            _cli_root_app,
            ["login", "--json", "--no-open"],
            env={
                "HYPRIAL_HOME": str(home),
                "HYPRIAL_OWNER": "env-owner",  # different from userinfo's owner
                "HYPRIAL_ZENOH_LISTEN": "",
                "HYPRIAL_ZENOH_CONNECT": "",
            },
        )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "OWNER_OVERRIDE_CONFLICT"
    assert payload["data"]["override"] == "env-owner"
    assert payload["data"]["loginOwner"] == "alicesmith"
    assert not (home / "secrets" / "login.json").exists()


def test_login_no_provider_flag_exists(_wired, tmp_path: Path) -> None:
    """D-U2-3 + tailnet cutover: the parameter face is exactly
    --no-open/--json/--switch-account/--dry-run/--no-daemon; the retired
    sidecar/join flags are usage errors, not silently accepted."""

    help_text = runner.invoke(_cli_root_app, ["login", "--help"]).output
    for option in ("--no-open", "--json", "--switch-account"):
        assert option in help_text
    for retired in (
        "--preauthkey-file",
        "--join-timeout",
        "--skip-join",
        "--install-sidecar",
        "--only-install-sidecar",
        "--provider",
    ):
        assert retired not in help_text
    with fake_oidc() as server:
        home = _wired(server)
        for retired in ("--skip-join", "--install-sidecar", "--provider"):
            result = _invoke(server, home, retired)
            assert result.exit_code == 2  # NoSuchOption
            assert "No such option" in result.output


def test_login_sidecar_missing_still_succeeds_with_device_not_ready(
    _wired, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D12: a failing device stage never rolls the identity back — login
    exits 0 with device.ready=false, the typed error and a next step."""

    def fail(home_path, *, owner, device_id, environ=None):
        raise tailcat_module.TailcatSidecarError(
            "SIDECAR_MISSING", "tailcat sidecar is not an executable file"
        )

    with fake_oidc() as server:
        home = _wired(server)
        monkeypatch.setattr(tailcat_module, "ensure_device_key", fail)
        result = _invoke(server, home, "--json", "--no-open")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["identity"]["committed"] is True
    device = payload["device"]
    assert device["ready"] is False
    assert device["error"]["code"] == "SIDECAR_MISSING"
    assert "hyprial-tailcat" in device["nextStep"]
    # The identity commit landed and was not rolled back.
    settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert settings["owner"] == "alicesmith"
    assert (home / "secrets" / "login.json").exists()


def test_plain_login_retries_failed_device_stage_without_reauthentication(
    _wired, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The device failure receipt's bare command resumes provisioning."""

    attempts: list[str] = []

    def fail(home_path, *, owner, device_id, environ=None):
        attempts.append("failed")
        raise tailcat_module.TailcatSidecarError(
            "SIDECAR_MISSING", "tailcat sidecar is not an executable file"
        )

    with fake_oidc() as server:
        home = _wired(server)
        monkeypatch.setattr(tailcat_module, "ensure_device_key", fail)
        first = _invoke(server, home, "--json", "--no-open")

    first_payload = json.loads(first.stdout)
    assert first.exit_code == 0, first.output
    assert first_payload["device"]["ready"] is False
    assert "re-run `hyprial login`" in first_payload["device"]["nextStep"]

    def succeed(home_path, *, owner, device_id, environ=None):
        attempts.append("succeeded")
        return _device_record(owner)

    monkeypatch.setattr(tailcat_module, "ensure_device_key", succeed)
    # Follow the receipt exactly after the issuer is gone. A reauthentication
    # attempt would fail; a post-auth resume provisions the missing device.
    second = _invoke(server, home)

    assert second.exit_code == 0, second.output
    assert attempts == ["failed", "succeeded"]


# -- invite pickup after a verified login (§5.3, D12) -------------------------


def test_login_runs_org_pending_once_after_the_daemon_verifies(
    _wired, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§5.3: a successful login picks up account-bound invites once."""
    from hyprial.shell.impl.invites import pending as pending_module

    seen: dict[str, object] = {}

    def fake_run_pending(home, *, accept_all, confirm, ipc_join, now, **kwargs):
        seen.update(home=home, accept_all=accept_all, confirm=confirm)
        ipc_join("v1.payload-1")
        return {
            "ok": True,
            "joined": [{"org": "acme", "inviter": "bob"}],
            "failed": [],
            "skipped": 0,
            "pending": [],
            "warnings": [],
        }

    joins: list[dict[str, object]] = []

    def request(method, params=None, **_kwargs):
        assert _kwargs["timeout"] == 120.0
        joins.append({"method": method, "params": params})
        return {"org": "acme"}

    with fake_oidc() as server:
        home = _wired(server)
        # An existing owner keeps login on the orchestrated (non-first-time)
        # route, where the daemon is launched and verified inside the flow.
        (home / "settings.json").write_text(
            json.dumps({"owner": "old-owner"}), encoding="utf-8"
        )
        monkeypatch.setattr(pending_module, "run_pending", fake_run_pending)
        monkeypatch.setattr(_cli_support, "_daemon_request", request)
        result = _invoke(server, home, "--switch-account", "--json", "--no-open")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["pending"] == {
        "status": "ok",
        "joined": [{"org": "acme", "inviter": "bob"}],
        "failed": [],
        "skipped": 0,
        "warnings": [],
    }
    assert seen["home"] == home
    assert seen["accept_all"] is True
    assert joins == [{"method": "org.join", "params": {"link": "v1.payload-1"}}]


def test_login_invite_pickup_failure_never_fails_the_login(
    _wired, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D12: a pickup failure lands in ``pending`` and changes nothing else."""
    from hyprial.shell.impl.invites import pending as pending_module

    def fail(home, **kwargs):
        raise pending_module.InvitePickupError(
            "CASDOOR_UNREACHABLE", "cannot reach the account server"
        )

    with fake_oidc() as server:
        home = _wired(server)
        (home / "settings.json").write_text(
            json.dumps({"owner": "old-owner"}), encoding="utf-8"
        )
        monkeypatch.setattr(pending_module, "run_pending", fail)
        result = _invoke(server, home, "--switch-account", "--json", "--no-open")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["identity"]["committed"] is True
    assert payload["daemon"]["state"] == "running"
    assert payload["pending"]["status"] == "failed"
    assert payload["pending"]["code"] == "CASDOOR_UNREACHABLE"


def test_login_pickup_runs_for_real_and_degrades_when_no_device_record(
    _wired, tmp_path: Path
) -> None:
    """Unmocked pickup: the stubbed device stage writes no device record, so
    the real run_pending stops at DEVICE_NOT_READY — best effort, login ok."""
    with fake_oidc() as server:
        home = _wired(server)
        (home / "settings.json").write_text(
            json.dumps({"owner": "old-owner"}), encoding="utf-8"
        )
        result = _invoke(server, home, "--switch-account", "--json", "--no-open")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["pending"]["status"] == "failed"
    assert payload["pending"]["code"] == "DEVICE_NOT_READY"
