"""Public CLI contract for restarting a running daemon after ``hyprial daemon upgrade``."""

from __future__ import annotations

from tests.support.runtime.environment import home_with_node_id
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


import hyprial.daemon.impl.updates as updates
from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonLaunchResult
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.common.support as _cli_owner_1
import subprocess as _cli_subprocess
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

# Captured at import time, before any test can monkeypatch the module: the
# real launch primitive this file otherwise stubs.  The production-branch
# contract test below runs THIS function over a fake ping layer, so the
# reader is proven against what the real launcher returns — not against a
# stub that could invent any shape (F2's original failure mode).
_REAL_LAUNCH_DAEMON_PROCESS = _cli_get_services()._launch_daemon_process

# The ping answer a freshly launched generation gives: flat, built by the
# daemon's ping handler shape (daemon/application.py `handle("ping")`).
FRESH_LAUNCH_PING = {
    "running": True,
    "pid": 4202,
    "epoch": "new",
    "version": "0.4.0.dev2",
    "phase": "serving",
}


@pytest.fixture(autouse=True)
def _locked_constraints(monkeypatch: pytest.MonkeyPatch) -> None:
    def export_constraints(
        url: str, commit: str, destination: Path, *, runner: object = None
    ) -> Path:
        destination.write_text("anyio==4.15.1\n", encoding="utf-8")
        return destination

    monkeypatch.setattr(updates, "export_locked_constraints", export_constraints)


def _fake_upgrade(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    daemon_running: bool = True,
    launch: Callable[..., Any] | None = None,
) -> list[str]:
    home_with_node_id(tmp_path / "home")
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    # Guard 3's positive shape: this process sits inside the tool directory
    # uv would write, so the upgrade under test reaches its (faked) install.
    monkeypatch.setenv("UV_TOOL_DIR", str(Path(sys.prefix).resolve().parent))
    monkeypatch.delenv("HARNESS_SOCKET_PATH", raising=False)
    monkeypatch.delenv("HARNESS_CONFIG", raising=False)
    import hyprial.shell.impl.cli.commands.daemon.service as service_module

    missing_unit = tmp_path / "unconfigured-service.plist"
    manager = SimpleNamespace(
        unit_path=missing_unit,
        inspect=lambda serving_pid=None: SimpleNamespace(
            platform="launchd",
            unit="com.hyprial.hyprial.daemon",
            unit_path=missing_unit,
            installed=False,
            loaded=False,
            running=False,
            pid=None,
            boot_persistent=None,
            changed=None,
            attributed=False,
            template_matches=False,
            serving_pid=serving_pid,
            supervision="unsupervised",
            relaunch_count=None,
            last_exit_code=None,
            ownership_busy=False,
            ownership_reason=None,
            backup_path=None,
        ),
    )
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)

    before = updates.Installation(
        version="0.4.0.dev1",
        commit="a" * 40,
        requested_revision="v0.4.0.dev1",
        url="ssh://git@example.test/hyprial.git",
    )
    after = updates.Installation(
        version="0.4.0.dev2",
        commit="d" * 40,
        requested_revision="v0.4.0.dev2",
        url="ssh://git@example.test/hyprial.git",
    )
    installations = [before, after]
    monkeypatch.setattr(
        updates,
        "read_installation",
        lambda: installations.pop(0) if installations else after,
    )
    monkeypatch.setattr(
        updates,
        "resolve_remote",
        lambda *_args, **_kwargs: updates.RemoteResolution(
            tag="v0.4.0.dev2", commit="d" * 40, version="0.4.0.dev2"
        ),
    )
    events: list[str] = []
    monkeypatch.setattr(
        _cli_get_services(), "_prime_post_install_restart_imports", lambda: events.append("prime")
    )
    monkeypatch.setattr(
        _cli_subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, "installed\n", ""),
    )

    phase = {"stopped": False}
    # The fake old daemon is gone once shutdown was asked of it.  Probing the
    # real pid 4101 instead made the restart wait its full stop budget (67 s
    # per test) and add a stopWarning whenever an unrelated process held that
    # pid -- as it did on the Linux CI runner for dev 72898cdb (task 51197).
    # Keyed on the shared event log, not on this fake's own phase: tests that
    # swap in their own _daemon_request still append "shutdown:<t>" to it.
    monkeypatch.setattr(
        _cli_get_services(),
        "_stopped_process",
        lambda _pid, _identity: any(event.startswith("shutdown:") for event in events),
    )

    def daemon_request(
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 15.0,
        restore_wait: float | None = None,
    ) -> dict[str, Any]:
        if method == "upgrade.steps.run":
            assert params == {"apply": True}
            return {"steps": [], "pending": ["org-membership-v1"]}
        del params
        events.append(f"{method}:{timeout:g}")
        if method == "ps":
            if not daemon_running:
                raise ipc_errors.DaemonUnavailableError("no fake daemon")
            return {"daemon": {"running": True, "pid": 4101, "epoch": "old"}}
        if method == "shutdown":
            phase["stopped"] = True
            return {"stopping": True, "pid": 4101}
        if method == "ping":
            # The pre-restart snapshot asks ping now (card b9872e94), so ping
            # answers by PHASE: the old generation until shutdown, the fresh
            # one after the launch (card 3c116ad2 P1 reads the settled phase).
            if not phase["stopped"]:
                if not daemon_running:
                    raise ipc_errors.DaemonUnavailableError("no fake daemon")
                return {
                    "running": True,
                    "pid": 4101,
                    "epoch": "old",
                    "version": "0.4.0.dev1",
                    "phase": "reconciled",
                }
            return dict(FRESH_LAUNCH_PING, phase="reconciled")
        raise AssertionError(f"unexpected daemon request: {method}")

    def default_launch(
        *, ready_timeout: float, autoinstall: bool = True
    ) -> DaemonLaunchResult:
        events.append(f"launch:{ready_timeout:g}")
        events.append(f"launchAutoinstall:{str(autoinstall).lower()}")
        return DaemonLaunchResult.launched(FRESH_LAUNCH_PING)

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setattr(
        _cli_get_services(),
        "_launch_daemon_process",
        launch or default_launch,
        raising=False,
    )
    return events


def _start_failed_entry(
    ts: str, *, phase: str, error_type: str, error: str
) -> dict[str, Any]:
    """One durable startup-failure record, in the daemon log's own shape."""

    return {
        "ts": ts,
        "level": "warn",
        "component": "daemon",
        "event": "daemon.start.failed",
        "phase": phase,
        "errorType": error_type,
        "error": error,
    }


def _read_failure_marker(tmp_path: Path) -> dict[str, Any]:
    return json.loads(
        (tmp_path / "state" / "upgrade-restart-failed.json").read_text("utf-8")
    )


def _fail_launch_writing_log(
    tmp_path: Path, entry: Callable[[], dict[str, Any]] | None
) -> Callable[..., None]:
    """A launch that leaves `entry()` (if any) in the log and then fails.

    `entry` is a factory, not a dict: the timestamp has to be produced *when
    the launch runs*, after the CLI captured `restart_started_at`. A dict built
    in the test body would predate that boundary and be excluded as an older
    generation -- which is a real failure mode of the fixture, not of the code.
    """

    def fail_launch(
        *, ready_timeout: float, autoinstall: bool = True
    ) -> None:
        del autoinstall
        assert ready_timeout == _cli_owner_1.DAEMON_START_HARD_CAP_SECONDS
        if entry is not None:
            log_path = tmp_path / "state" / "logs" / "daemon.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                json.dumps(entry(), ensure_ascii=False) + "\n", "utf-8"
            )
        raise _cli_get_services().CliError(
            ipc_errors.DAEMON_START_TIMEOUT,
            "daemon did not become ready within 15s",
        )

    return fail_launch




def _seed_desired_rows(tmp_path: Path, count: int) -> None:
    """Give this machine a desired state of `count` connector rows."""

    from hyprial.daemon.impl.desired_state import (
        DesiredState,
        DesiredStateStore,
        )

    from hyprial.daemon.impl.adapters.spec import AdapterSpec

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    DesiredStateStore(state / "desired-state.json").save(
        DesiredState(
            adapters=tuple(
                AdapterSpec(provider="lark", name=f"c{index:02d}")
                for index in range(count)
            )
        )
    )
