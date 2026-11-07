"""No fd handoff: login commits, releases, launches with a generation check.

Concurrent logins are last-writer-wins: a login whose launch gap is taken by
a later commit is reported ``IDENTITY_SUPERSEDED`` (the later login stays in
effect), never silently verified against the wrong owner.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.kernel import ProcessFileLock, ipc_errors
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
from hyprial.identity import EXPECTED_IDENTITY_GENERATION_ENV
from hyprial.shell.impl.login.generation import (
    IdentityGenerationStale,
    next_identity_generation,
    read_identity_generation,
    set_launch_generation,
    take_expected_generation,
    verify_expected_generation,
)
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)
from tests.fixtures.network.fake_oidc import fake_oidc  # noqa: E402

from .support import _environment, _invoke_login, _launch_result, _profile


def _later_login_commits(home: Path, generation: int) -> None:
    """What a concurrent login B leaves: the next generation in login.json."""

    path = home / "secrets" / "login.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["identityGeneration"] = generation
    path.write_text(json.dumps(record), encoding="utf-8")


def _no_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _cli_get_services(),
        "_daemon_probe",
        lambda **_kwargs: (_ for _ in ()).throw(
            ipc_errors.DaemonUnavailableError("stopped")
        ),
    )


def test_login_releases_the_lock_and_launches_with_its_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _state = _environment(tmp_path, monkeypatch)
    _no_daemon(monkeypatch)
    seen: list[dict[str, object]] = []

    with fake_oidc() as server:
        profile = _profile(server)

        def launch(**kwargs):
            seen.append(
                {
                    **kwargs,
                    "lock": ProcessFileLock(
                        home / ".identity-transaction.lock"
                    ).probe(),
                    "onDisk": read_identity_generation(home),
                }
            )
            return _launch_result(profile)

        result = _invoke_login(server, monkeypatch, launch=launch)

    assert result.exit_code == 0, result.output
    assert len(seen) == 1
    # The daemon acquires the lock itself: the launcher holds nothing.
    assert seen[0]["lock"] == "free"
    assert "identity_transaction" not in seen[0]
    assert seen[0]["expected_identity_generation"] == 1
    assert seen[0]["onDisk"] == 1
    assert next_identity_generation(home) == 2


def test_a_later_login_in_the_gap_reports_this_one_superseded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _state = _environment(tmp_path, monkeypatch)
    _no_daemon(monkeypatch)

    with fake_oidc() as server:

        def launch(**kwargs):
            _later_login_commits(home, kwargs["expected_identity_generation"] + 1)
            # The launched daemon found the newer generation and exited stale.
            raise _cli_get_services().CliError(
                "IDENTITY_SUPERSEDED", "launched daemon exited as stale"
            )

        result = _invoke_login(server, monkeypatch, launch=launch)

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["code"] == "IDENTITY_SUPERSEDED"
    assert payload["data"]["superseded"] is True
    assert payload["data"]["identityGeneration"] == 1
    assert payload["data"]["currentIdentityGeneration"] == 2


def test_a_later_daemon_answering_for_another_owner_is_superseded_not_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _state = _environment(tmp_path, monkeypatch)
    _no_daemon(monkeypatch)

    with fake_oidc() as server:
        profile = _profile(server)

        def launch(**kwargs):
            _later_login_commits(home, kwargs["expected_identity_generation"] + 1)
            winner = _launch_result(profile).to_payload()
            winner["owner"] = "bob"
            from hyprial.kernel import DaemonLaunchResult

            return DaemonLaunchResult.existing(winner)

        result = _invoke_login(server, monkeypatch, launch=launch)

    payload = json.loads(result.stdout)
    assert payload["code"] == "IDENTITY_SUPERSEDED", payload
    assert payload["data"]["currentIdentityGeneration"] == 2


def test_generation_helpers_pop_and_clear_the_expectation(tmp_path: Path) -> None:
    child = {EXPECTED_IDENTITY_GENERATION_ENV: "9", "PATH": "/bin"}
    set_launch_generation(child, None)
    assert EXPECTED_IDENTITY_GENERATION_ENV not in child, "inherited value leaked"
    set_launch_generation(child, 3)
    assert child[EXPECTED_IDENTITY_GENERATION_ENV] == "3"

    environ = {EXPECTED_IDENTITY_GENERATION_ENV: "3"}
    assert take_expected_generation(environ) == 3
    assert environ == {}
    assert take_expected_generation({EXPECTED_IDENTITY_GENERATION_ENV: "x"}) == 0
    supervised = {
        EXPECTED_IDENTITY_GENERATION_ENV: "3",
        "HYPRIAL_SERVICE_MANAGED": "1",
    }
    assert take_expected_generation(supervised) is None
    assert EXPECTED_IDENTITY_GENERATION_ENV not in supervised

    assert read_identity_generation(tmp_path) is None
    with pytest.raises(IdentityGenerationStale):
        verify_expected_generation(tmp_path, 1)


def test_a_real_daemon_child_exiting_76_is_mapped_to_superseded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launcher half end-to-end: a real child exits with the stale code,
    ``_wait_for_daemon`` sees its returncode, and the launcher names it."""

    import subprocess
    import sys

    from hyprial.shell.impl.login.generation import IDENTITY_GENERATION_STALE_EXIT_CODE

    _environment(tmp_path, monkeypatch)
    _no_daemon(monkeypatch)
    real_popen = subprocess.Popen
    spawned: list[list[str]] = []

    def popen(argv, **kwargs):  # type: ignore[no-untyped-def]
        if list(argv[-3:]) == ["daemon", "run", "--json"]:
            spawned.append(list(argv))
            assert kwargs["env"][EXPECTED_IDENTITY_GENERATION_ENV] == "3"
            argv = [
                sys.executable,
                "-c",
                f"import sys; sys.exit({IDENTITY_GENERATION_STALE_EXIT_CODE})",
            ]
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    services = _cli_get_services()
    with pytest.raises(services.CliError) as raised:
        services._launch_daemon_process_locked(
            ready_timeout=30.0,
            listen=None,
            connect=None,
            expected_identity_generation=3,
        )
    assert spawned, "the launcher never spawned daemon run"
    assert raised.value.code == "IDENTITY_SUPERSEDED", raised.value
    assert raised.value.data["exitCode"] == IDENTITY_GENERATION_STALE_EXIT_CODE
    assert raised.value.data["expectedIdentityGeneration"] == 3


def test_generation_stays_monotonic_across_logout(tmp_path: Path) -> None:
    from hyprial.shell.impl.login.generation import superseded_by

    assert next_identity_generation(tmp_path) == 1
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "login.json").write_text(json.dumps({"identityGeneration": 1}))
    assert next_identity_generation(tmp_path) == 2
    (secrets / "login.json").unlink()  # logout
    assert next_identity_generation(tmp_path) == 3, "a number was reused after logout"
    # A vanished credential is "changed", not "a later login won".
    assert superseded_by(tmp_path, 3) == {
        "superseded": False,
        "identityGeneration": 3,
        "currentIdentityGeneration": None,
    }
