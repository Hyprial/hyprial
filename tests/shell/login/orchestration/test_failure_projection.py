"""login_orchestrate_daemon: failure projection scenarios."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hyprial.daemon.impl.configuration.identity import (
    write_settings_identity,
    write_settings_owner,
)
from hyprial.identity.impl.identity_transaction import (
    IdentityTransactionLock,
)
from hyprial.kernel import DaemonLaunchResult, ipc_errors
from tests.fixtures.network.fake_oidc import fake_oidc  # noqa: E402

from .support import (
    CUSTODY_CONFLICT_LAUNCH_DATA,
    NEW,
    OLD,
    _environment,
    _invoke_login,
    _launch_result,
    _profile,
    _seed_agent_with_grant,
    _seed_agents,
    _seed_hosted_collision,
)
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


@pytest.mark.parametrize(
    ("failure", "expected_phase", "committed"),
    [
        ("stop", "stop", False),
        ("commit", "commit", False),
        ("start", "start", True),
        ("verify", "verify", True),
        ("facade", "verify", True),
    ],
)
def test_failure_phases_have_fixed_machine_readable_state(
    failure: str,
    expected_phase: str,
    committed: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, state = _environment(tmp_path, monkeypatch)
    _seed_agents(state)
    write_settings_owner(OLD, environ={"HYPRIAL_HOME": str(home)}, hyprial_home=home)
    if failure == "stop":
        monkeypatch.setattr(
            _cli_get_services(),
            "_daemon_probe",
            lambda **_kwargs: {
                "running": True,
                "pid": 111,
                "epoch": "old-epoch",
                "owner": OLD,
                "identityMode": "legacy",
                "identityIssuer": None,
            },
        )
        monkeypatch.setattr(
            _cli_get_services(),
            "_stop_daemon_for_identity_switch",
            lambda: (_ for _ in ()).throw(
                _cli_get_services().CliError("STOP_PROOF_FAILED", "old generation survived")
            ),
        )
    else:
        monkeypatch.setattr(
            _cli_get_services(),
            "_daemon_probe",
            lambda **_kwargs: (_ for _ in ()).throw(
                ipc_errors.DaemonUnavailableError("stopped")
            ),
        )
    if failure == "commit":
        import hyprial.shell.impl.login.flow as login_module

        monkeypatch.setattr(
            login_module,
            "write_settings_identity",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
        )

    with fake_oidc() as server:
        profile = _profile(server)

        def launch(**_kwargs):
            if failure == "start":
                raise RuntimeError("spawn failed")
            if failure == "facade":
                return object()
            if failure == "verify":
                return DaemonLaunchResult.launched(
                    {
                        "running": True,
                        "pid": 4242,
                        "epoch": "new-epoch",
                        "owner": "wrong-owner",
                        "identityMode": "casdoor",
                        "identityIssuer": profile.issuer,
                        "migration": {"status": "applied"},
                    }
                )
            return _launch_result(profile)

        result = _invoke_login(
            server,
            monkeypatch,
            "--switch-account",
            launch=launch,
        )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    data = payload["data"]
    assert data["failedPhase"] == expected_phase
    assert data["identityCommitted"] is committed
    assert set(("device", "daemon", "migration", "nextStep")) <= data.keys()
    if failure == "stop":
        # The stop-phase failure happens before any classification ran, so
        # the migration default must say not-attempted — "not-required"
        # read as "we checked and nothing was needed".
        assert data["migration"]["status"] == "not-attempted"
    if failure == "start":
        # Negative control for the custody named-outcome mapping: an
        # ordinary launch failure must stay the generic startup failure,
        # never swallowed by the named branch (#513 x #493 acceptance).
        assert payload["code"] == "DAEMON_START_FAILED"
        assert data["migration"]["status"] == "unknown"
    if not committed:
        assert json.loads((home / "settings.json").read_text())["owner"] == OLD


@pytest.mark.parametrize(
    ("refusal_code", "reason"),
    [
        (ipc_errors.OWNER_MIGRATION_CUSTODY_CONFLICT, "owner-migration-custody-conflict"),
        (
            ipc_errors.OWNER_MIGRATION_CUSTODY_UNREADABLE,
            "owner-migration-custody-unreadable",
        ),
    ],
)
def test_switch_start_phase_custody_refusal_is_a_named_outcome(
    refusal_code: str,
    reason: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A switch whose first start hits the custody gate is a named outcome.

    infra-op acceptance on #513 (S3 merged first, so #513 wires its codes
    into S3's failure_data): after ``login --switch-account`` commits and
    the old generation is proven gone, a daemon start refused by
    ``OwnerMigrationCustodyConflict`` / ``...Unreadable`` must surface as
    that named code with ``failedPhase=start`` / ``identityCommitted=true``
    — never a generic ``DAEMON_START_FAILED``, never a rewound identity —
    with the daemon state reported as-is and a ``nextStep`` naming the same
    real recourse the daemon's refusal message gives (charter 5e).
    """

    home, state = _environment(tmp_path, monkeypatch)
    _seed_agent_with_grant(state, home)
    write_settings_identity(
        OLD,
        mode="casdoor",
        issuer="https://old.example",
        environ={"HYPRIAL_HOME": str(home)},
        hyprial_home=home,
    )
    monkeypatch.setattr(
        _cli_get_services(),
        "_daemon_probe",
        lambda **_kwargs: (_ for _ in ()).throw(
            ipc_errors.DaemonUnavailableError("stopped")
        ),
    )

    def refused_launch(**_kwargs):
        bounded = {
            key: value
            for key, value in CUSTODY_CONFLICT_LAUNCH_DATA.items()
            if key in {"phase", "errorType", "logPath"}
        }
        if refusal_code == ipc_errors.OWNER_MIGRATION_CUSTODY_CONFLICT:
            bounded.update(
                {
                    "old": OLD,
                    "new": NEW,
                    "grants": 1,
                    "homes": 1,
                }
            )
        else:
            bounded.update(
                {
                    "database": str(state / "agents.sqlite3"),
                    "table": "agent_secret_grants",
                    "errorType": "OperationalError",
                }
            )
        raise _cli_get_services().CliError(
            refusal_code,
            "daemon refused to start: owner migration custody gate",
            bounded,
        )

    with fake_oidc() as server:
        result = _invoke_login(
            server,
            monkeypatch,
            "--switch-account",
            launch=refused_launch,
        )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == refusal_code
    data = payload["data"]
    assert data["failedPhase"] == "start"
    assert data["identityCommitted"] is True
    # The old generation is stopped and proven gone; the new one never came
    # up — the daemon field reports exactly that, as-is.
    assert data["daemon"]["state"] == "stopped"
    # The migration did not run: the gate refused it before any write.
    assert data["migration"]["status"] == "refused"
    assert data["migration"]["reason"] == reason
    # nextStep names the non-destructive first recourse from the daemon's
    # refusal message; every named command really exists (charter 5e).
    next_step = data["nextStep"]
    assert "`hyprial login --switch-account`" in next_step
    assert "`hyprial daemon run`" in next_step
    assert OLD in next_step
    # The identity is NOT rewound: the committed owner and credential stay.
    settings = json.loads((home / "settings.json").read_text())
    assert settings["owner"] == NEW
    assert (home / "secrets" / "login.json").exists()
    # And the refused start wrote nothing: the grant and the old owner row
    # are exactly as the registry left them.
    database = sqlite3.connect(state / "agents.sqlite3")
    try:
        owners = [row[0] for row in database.execute("SELECT owner FROM agents")]
        grants = database.execute(
            "SELECT count(*) FROM agent_secret_grants"
        ).fetchone()[0]
    finally:
        database.close()
    assert owners == [OLD]
    assert grants == 1
    if refusal_code == ipc_errors.OWNER_MIGRATION_CUSTODY_CONFLICT:
        assert data["migration"]["grants"] == 1
        assert data["migration"]["old"] == OLD
        assert data["migration"]["new"] == NEW
    else:
        assert data["migration"]["table"] == "agent_secret_grants"


def test_real_daemon_custody_refusal_crosses_the_launch_log_as_a_named_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The named code survives the real process boundary.

    The refused ``daemon run`` child must emit the code on its ``--json``
    failure line, the launch-log summary must recognise it, and the real
    launcher must raise the named CliError — the stub-level test above can
    invent the shape; only a real child proves the contract end to end.
    """

    home, state = _environment(tmp_path, monkeypatch)
    _seed_agent_with_grant(state, home)
    # The switch has committed: settings now name the NEW identity while
    # the state still belongs to OLD and holds a live grant.
    write_settings_identity(
        NEW,
        mode="casdoor",
        issuer="https://new.example",
        environ={"HYPRIAL_HOME": str(home)},
        hyprial_home=home,
    )

    with pytest.raises(_cli_get_services().CliError) as caught:
        _cli_get_services()._launch_daemon_process(ready_timeout=30.0)

    error = caught.value
    assert error.code == ipc_errors.OWNER_MIGRATION_CUSTODY_CONFLICT
    assert error.data["old"] == OLD
    assert error.data["new"] == NEW
    assert error.data["grants"] == 1
    assert error.data["homes"] == 1
    assert error.data["errorType"] == "custodyRefusal"
    # The operator-facing refusal (with its real way out) outlives the
    # launch capture in the durable compatibility log.
    log = (state / "daemon-launch.log").read_text(encoding="utf-8")
    assert ipc_errors.OWNER_MIGRATION_CUSTODY_CONFLICT in log
    assert "hyprial login --switch-account" in log
    # Zero writes, again, on the real path.
    database = sqlite3.connect(state / "agents.sqlite3")
    try:
        owners = [row[0] for row in database.execute("SELECT owner FROM agents")]
        grants = database.execute(
            "SELECT count(*) FROM agent_secret_grants"
        ).fetchone()[0]
    finally:
        database.close()
    assert owners == [OLD]
    assert grants == 1


@pytest.mark.parametrize("hosted_owner", [OLD, NEW])
@pytest.mark.parametrize("no_daemon", [False, True])
def test_hosted_collision_blocks_login_before_identity_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hosted_owner: str, no_daemon: bool,
) -> None:
    from tests.daemon.organizations.invite.test_host_invite import _alias_state

    home, state = _environment(tmp_path, monkeypatch)
    _seed_hosted_collision(state, home, hosted_owner)
    write_settings_owner(OLD, environ={"HYPRIAL_HOME": str(home)})
    # Keep the existing transaction inode out of the business-state comparison.
    IdentityTransactionLock.acquire(home).close()
    before = _alias_state(tmp_path)

    def unexpected(**_kwargs):
        pytest.fail("collision must be refused before probing/stopping/starting daemon")

    monkeypatch.setattr(_cli_get_services(), "_daemon_probe", unexpected)
    monkeypatch.setattr(_cli_get_services(), "_stop_daemon_for_identity_switch", unexpected)
    with fake_oidc() as server:
        result = _invoke_login(
            server, monkeypatch, "--switch-account",
            *(["--no-daemon"] if no_daemon else []), launch=unexpected,
        )
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["code"] == ipc_errors.OWNER_MIGRATION_HOSTED_CONFLICT
    assert payload["data"]["identityCommitted"] is False
    assert _alias_state(tmp_path) == before
    assert not (home / "secrets/login.json").exists()


def test_real_daemon_hosted_refusal_names_recovery_without_rewriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.daemon.organizations.invite.test_host_invite import _alias_state

    home, state = _environment(tmp_path, monkeypatch)
    _seed_hosted_collision(state, home, OLD)
    # Model manually editing config, bypassing the normal login preview.
    write_settings_owner(NEW, environ={"HYPRIAL_HOME": str(home)})
    IdentityTransactionLock.acquire(home).close()
    before = _alias_state(tmp_path)
    with pytest.raises(_cli_get_services().CliError) as caught:
        _cli_get_services()._launch_daemon_process(ready_timeout=30.0)
    assert caught.value.code == ipc_errors.OWNER_MIGRATION_HOSTED_CONFLICT
    assert caught.value.data["old"] == OLD
    assert caught.value.data["hostedOwner"] == OLD
    assert "restore the previous login configuration" in str(caught.value)
    assert OLD in str(caught.value)
    assert "Move or migrate" in str(caught.value)
    assert _alias_state(tmp_path) == before
