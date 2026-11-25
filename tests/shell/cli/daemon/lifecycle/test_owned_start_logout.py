"""An owned ``hyprial daemon run`` re-enters the lifecycle loop after a pure logout.

Decision: daemon-first-z6pre-owned-logout-20261117.  The owned start asks the
application to own the process exit unless the run stops for ``logout``; after
such a stop the same process opens the bootstrap phase through the lifecycle
loop.  The expected identity generation is a launch check and is not repeated
there: the login record is gone after a logout.  A home whose owner survives
the logout cannot be admitted by the bootstrap phase and ends as before.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import hyprial.daemon as _daemon_public
import hyprial.identity
import hyprial.shell.impl.cli.commands.daemon.commands as _daemon_commands
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401
import hyprial.shell.impl.daemon_wiring.onboarding as _onboarding
import hyprial.shell.impl.daemon_wiring.onboarding.launch as _wiring_launch
from hyprial.identity import EXPECTED_IDENTITY_GENERATION_ENV
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
from hyprial.shell.impl.login.flow import credential_path
from hyprial.shell.impl.login.generation import IDENTITY_GENERATION_STALE_EXIT_CODE

OWNED = {"interpreterOwned": True}
OWNER = "z6pre-owner@example.test"


class _Transaction:
    def __enter__(self) -> _Transaction:
        return self

    def __exit__(self, *_error: object) -> None:
        return None

    def detach(self) -> io.StringIO:
        return io.StringIO()

    def close(self) -> None:
        return None


class _IdentityTransactions:
    @staticmethod
    def acquire(*_args: object, **_kwargs: object) -> _Transaction:
        return _Transaction()

    @staticmethod
    def adopt_legacy(*_args: object, **_kwargs: object) -> None:
        return None


class _Runtime:
    def __init__(self, reason: str, on_run: Any) -> None:
        self.epoch = "epoch-z6pre"
        self.reason = reason
        self.on_run = on_run
        self.calls: list[str] = []

    @property
    def stop_reason(self) -> str:
        return self.reason

    def owns_process_exit(self) -> None:
        self.calls.append("owns_process_exit")

    def exit_unless_logout(self) -> None:
        self.calls.append("exit_unless_logout")

    def run(self, **_kwargs: object) -> None:
        self.calls.append("run")
        self.on_run()


class _Setup:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.loops: list[dict[str, Any]] = []
        self.leaves: list[dict[str, Any]] = []
        self.runtime: _Runtime | None = None

    def logout(self) -> None:
        (self.home / "settings.json").write_text(json.dumps({"version": 1}))
        credential_path(hyprial_home=self.home).unlink(missing_ok=True)


@pytest.fixture
def owned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Setup:
    home, state = tmp_path / "home", tmp_path / "state"
    home.mkdir(mode=0o700)
    (home / "settings.json").write_text(json.dumps({"version": 1, "owner": OWNER}))
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    services = _cli_get_services()
    monkeypatch.setattr(services, "require_initialized_hyprial_home", lambda: home)
    monkeypatch.setattr(services, "_hyprial_home", lambda: home)
    monkeypatch.setattr(services, "_state_dir", lambda: state)
    monkeypatch.setattr(services, "_socket_path", lambda: state / "daemon.sock")
    monkeypatch.setattr(hyprial.identity, "IdentityTransactionLock", _IdentityTransactions)
    monkeypatch.setattr(_daemon_public, "check_native_engine", lambda: None)
    monkeypatch.setattr(
        _daemon_commands, "_acquire_daemon_fence", lambda *_a, **_k: _Transaction()
    )
    setup = _Setup(home)
    monkeypatch.setattr(
        _wiring_launch, "launch_daemon_lifecycle", lambda **kw: setup.loops.append(kw)
    )
    monkeypatch.setattr(
        _onboarding, "leave_ownerless_loop", lambda **kw: setup.leaves.append(kw) or ()
    )
    return setup


def _install(monkeypatch: pytest.MonkeyPatch, setup: _Setup, reason: str, on_run: Any) -> None:
    runtime = _Runtime(reason, on_run)
    setup.runtime = runtime

    class _Factory:
        @staticmethod
        def from_environment(**_kwargs: Any) -> _Runtime:
            return runtime

    monkeypatch.setattr(_daemon_public, "DaemonApplication", _Factory)


def _run(*, obj: dict[str, Any] | None = None) -> Any:
    return CliRunner().invoke(_cli_root_app, ["daemon", "run", "--json"], obj=obj)


def test_a_pure_logout_re_enters_the_lifecycle_loop(monkeypatch, owned):
    _install(monkeypatch, owned, "logout", owned.logout)

    result = _run()

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["ok"] is True
    assert owned.runtime.calls == ["exit_unless_logout", "run"]
    assert len(owned.loops) == 1
    loop = owned.loops[0]
    assert loop["home"] == owned.home
    assert loop["state_dir"] == owned.home.parent / "state"
    assert loop["socket_path"] == owned.home.parent / "state" / "daemon.sock"
    assert loop["exit_unless_logout"] is True
    assert callable(loop["bind_epoch"])
    assert "launch_id" in loop


def test_any_other_stop_reason_does_not_enter_the_loop(monkeypatch, owned):
    _install(monkeypatch, owned, "shutdown", lambda: None)

    result = _run()

    assert result.exit_code == 0, result.output
    assert owned.loops == []


def test_a_surviving_owner_after_logout_ends_the_process(monkeypatch, owned):
    monkeypatch.setenv("HYPRIAL_OWNER", OWNER)
    _install(monkeypatch, owned, "logout", lambda: None)

    result = _run(obj=OWNED)

    assert result.exit_code == 0, result.output
    assert owned.loops == []
    assert owned.leaves == [{}]


def test_the_expected_generation_is_not_checked_again_after_logout(monkeypatch, owned):
    credential = credential_path(hyprial_home=owned.home)
    credential.parent.mkdir(parents=True, exist_ok=True)
    credential.write_text(json.dumps({"identityGeneration": 3}))
    monkeypatch.setenv(EXPECTED_IDENTITY_GENERATION_ENV, "3")
    _install(monkeypatch, owned, "logout", owned.logout)

    result = _run()

    assert result.exit_code != IDENTITY_GENERATION_STALE_EXIT_CODE, result.output
    assert result.exit_code == 0, result.output
    assert len(owned.loops) == 1


@pytest.mark.parametrize("survives", [False, True])
def test_the_interpreter_is_left_once_after_the_envelope(monkeypatch, owned, survives):
    if survives:
        monkeypatch.setenv("HYPRIAL_OWNER", OWNER)
    _install(monkeypatch, owned, "logout", owned.logout)

    result = _run(obj=OWNED)

    assert result.exit_code == 0, result.output
    assert owned.leaves == [{}]
    assert json.loads(result.stdout)["ok"] is True


def _boom() -> None:
    raise RuntimeError("home lease authority did not release before deadline")


def test_a_logout_whose_teardown_raises_still_leaves_the_interpreter_failed(monkeypatch, owned):
    _install(monkeypatch, owned, "logout", _boom)

    result = _run(obj=OWNED)

    assert result.exit_code != 0
    assert owned.loops == []
    assert owned.leaves == [{"failed": True}]


def test_a_stop_that_arrives_during_a_logout_teardown_still_leaves_the_interpreter(
    monkeypatch, owned
):
    _install(monkeypatch, owned, "shutdown", lambda: None)

    result = _run(obj=OWNED)

    assert result.exit_code == 0, result.output
    assert owned.loops == []
    assert owned.leaves == [{}]
