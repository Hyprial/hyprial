"""Public CLI contract for restarting a running daemon after ``hyprial daemon upgrade``."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


import hyprial.daemon.impl.updates as updates
from hyprial.daemon.impl.adapters.lark import sdk as lark_sdk
from hyprial.daemon.impl.autoupdate.alert import AlertOutcome
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
    (tmp_path / "home").mkdir(mode=0o700)
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

    def default_launch(*, ready_timeout: float) -> DaemonLaunchResult:
        events.append(f"launch:{ready_timeout:g}")
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


def _silence_owner_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub only the send; the marker write and its write-back stay real."""

    # ⚠️ `cli.AlertOutcome` does not exist -- `cli` re-exports several alert
    # names but not this one. The first version used it anyway, so every caller
    # raised AttributeError *after* `delivered.update(kwargs)`, and the tests
    # that only read the marker passed without the write-back ever running.
    # The real class keeps the stub's shape identical to production's.
    monkeypatch.setattr(
        _cli_get_services(),
        "notify_upgrade_failure",
        lambda **_kwargs: AlertOutcome(
            attempted=True, delivered=True, message_id="om_fixture"
        ),
    )


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

    def fail_launch(*, ready_timeout: float) -> None:
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


# --------------------------------------------------------------------------- #
# Card 3c116ad2: confirmation reads ping's phase; the unconfirmed state gets
# exactly one closing follow-up.  Only the daemon boundary is stubbed -- the
# owner alert runs its real path (binding lookup, gateway resolution,
# credential read) with the Lark SDK gateway substituted by a recorder.
# --------------------------------------------------------------------------- #


class _OwnerInbox:
    """The recorder the fake gateway hands to the alert's sender seam."""

    def __init__(self, sent: list[str]) -> None:
        self._sent = sent

    def send_owner_dm(
        self, open_id: str, text: str, *, idempotency_key: str
    ) -> str:
        self._sent.append(text)
        return "om_fake"


def _bind_owner(tmp_path: Path) -> None:
    """Bind an owner DM channel the alert's real lookup path can resolve."""

    home = tmp_path / "home"
    state = tmp_path / "state"
    (home / "secrets").mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    (home / "channels.json").write_text(
        json.dumps(
            {
                "version": 1,
                "gateways": [
                    {
                        "provider": "lark",
                        "name": "allen-squire",
                        "appId": "cli_test",
                        "credentialRef": "lark-allen-squire",
                        "routes": [
                            {"name": "r", "type": "direct", "nativeId": "ou_x"}
                        ],
                    }
                ],
            }
        ),
        "utf-8",
    )
    (home / "secrets" / "lark-allen-squire.json").write_text(
        json.dumps({"appSecret": "s"}), "utf-8"
    )
    (state / "users.json").write_text(
        json.dumps(
            {
                "version": 1,
                "users": [
                    {
                        "owner": "allen",
                        "ownerKey": "allen",
                        "loginName": "aaron@example",
                        "agents": [],
                        "preferredReceiver": {"machine": "m", "machineKey": "m"},
                        "notificationRules": [],
                        "ownerOpenId": {
                            "channel": "allen-squire",
                            "openId": "ou_allen",
                            "boundAt": "2026-08-31T00:00:00Z",
                        },
                    }
                ],
            }
        ),
        "utf-8",
    )


def _record_owner_messages(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Substitute the Lark SDK gateway; record every owner DM it is asked for."""

    sent: list[str] = []

    class _RecordingGateway:
        @classmethod
        def from_credentials(cls, app_id: str, secret: str, **_kwargs: object):
            del app_id, secret
            return _OwnerInbox(sent)

    monkeypatch.setattr(lark_sdk, "LarkSdkGateway", _RecordingGateway)
    return sent


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
