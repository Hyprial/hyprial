"""In-process autoupdate scheduling and accept-loop isolation."""

from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root

import json
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.autoupdate import (
    AUTOUPDATE_CHILD_ENV,
    InProcessAutoUpdateScheduler,
    next_scheduled_run,
)
from hyprial.daemon.impl.autoupdate.actor import AutoUpdateAuthority
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.runtime import ReconcileSummary
from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport
from hyprial.daemon.impl.squire import UserDeliveryRequest, UserDeliveryResult


from hyprial.shell.impl.cli.commands.upgrade import commands as upgrade_commands
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections



def _home_with_auto_upgrade(tmp_path: Path, name: str = "home") -> Path:
    """A scheduler home with guard 1 explicitly opted in (spec r1: the
    autoUpgrade switch defaults OFF, so a scheduling-mechanics test must
    write the settings.json opt-in itself)."""

    home = tmp_path / name
    home.mkdir(mode=0o700)
    (home / "settings.json").write_text(
        json.dumps({"autoUpgrade": True}) + "\n", encoding="utf-8"
    )
    return home


def test_next_run_preserves_0317_and_1517_local_schedule() -> None:
    local = timezone(timedelta(hours=8))

    assert next_scheduled_run(datetime(2026, 8, 24, 3, 16, 59, tzinfo=local)) == (
        datetime(2026, 8, 24, 3, 17, tzinfo=local)
    )
    assert next_scheduled_run(datetime(2026, 8, 24, 3, 17, tzinfo=local)) == (
        datetime(2026, 8, 24, 15, 17, tzinfo=local)
    )
    assert next_scheduled_run(datetime(2026, 8, 24, 15, 17, tzinfo=local)) == (
        datetime(2026, 8, 25, 3, 17, tzinfo=local)
    )


def test_scheduler_runs_off_thread_and_preserves_daemon_path(
    monkeypatch: Any, tmp_path: Path
) -> None:
    inherited_path = "/restricted/bin:/opt/test-codex-bin"
    monkeypatch.setenv("PATH", inherited_path)
    called = threading.Event()
    observation: dict[str, Any] = {}

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observation.update(
            command=command,
            kwargs=kwargs,
            thread=threading.current_thread().name,
        )
        called.set()
        return subprocess.CompletedProcess(command, 0, '{"ok":true}\n', "")

    scheduler = InProcessAutoUpdateScheduler(
        state_dir=tmp_path,
        runner=runner,
        # Scheduling mechanics only: explicit autoUpgrade opt-in so the
        # default-off guard (spec r1) cannot silently skip the run under
        # test.
        hyprial_home=_home_with_auto_upgrade(tmp_path),
    )
    scheduler.start()
    try:
        assert scheduler.trigger("test") is True
        assert called.wait(2.0), "scheduled update never ran"
        deadline = time.monotonic() + 2.0
        while scheduler.status()["active"]:
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        scheduler.stop()

    assert observation["thread"] == "hyprial-autoupdate"
    env = observation["kwargs"]["env"]
    assert env["PATH"] == inherited_path
    assert env[AUTOUPDATE_CHILD_ENV] == "1"
    assert observation["command"][-4:] == ["daemon", "upgrade", "run", "--json"]


def test_scheduler_fires_after_sleep_crosses_calendar_point(tmp_path: Path) -> None:
    """A wake/clock jump across 03:17 must catch up instead of skipping it."""

    clock = [datetime(2026, 8, 24, 3, 16, 59)]
    called = threading.Event()

    def runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        called.set()
        return subprocess.CompletedProcess(command, 0, "{}\n", "")

    scheduler = InProcessAutoUpdateScheduler(
        state_dir=tmp_path,
        runner=runner,
        now=lambda: clock[0],
        calendar_recheck_seconds=0.01,
        hyprial_home=_home_with_auto_upgrade(tmp_path),
    )
    scheduler.start()
    try:
        deadline = time.monotonic() + 1.0
        while scheduler.status()["nextRunAt"] is None:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        clock[0] = datetime(2026, 8, 24, 3, 20)
        assert called.wait(1.0), "missed calendar point was not caught up"
        assert scheduler.status()["lastTrigger"] == "schedule"
    finally:
        scheduler.stop()


class _FakePresence:
    def online_actors(self) -> tuple[()]:
        return ()

    def actor_online(self, actor: str) -> bool:
        return False

    def online_mailboxes(self) -> tuple[()]:
        return ()


class _FakeHarnesses:
    def status(self) -> tuple[()]:
        return ()


class _FastRuntime:
    def poll(self) -> ReconcileSummary:
        return ReconcileSummary(0, 0)

    def stop(self) -> None:
        pass


class _ConfirmingUserBus:
    def __init__(self) -> None:
        self.requests: list[UserDeliveryRequest] = []

    def deliver(self, request: UserDeliveryRequest) -> UserDeliveryResult:
        self.requests.append(request)
        return UserDeliveryResult(
            request.message_id,
            True,
            native_message_id="native-confirmation",
        )


def _request(socket_path: Path, method: str) -> dict[str, Any]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(1.5)
    try:
        client.connect(str(socket_path))
        client.sendall(
            json.dumps(
                {"version": 1, "id": method, "method": method, "params": {}}
            ).encode()
            + b"\n"
        )
        return json.loads(client.makefile("rb").readline())
    finally:
        client.close()


def test_never_finishing_update_does_not_block_accept() -> None:
    """Mutation: running the update on the accept flow makes every ps red."""

    release = threading.Event()
    entered = threading.Event()

    def hung_runner(
        command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        entered.set()
        release.wait()
        return subprocess.CompletedProcess(command, 0, "{}", "")

    root = Path(tempfile.mkdtemp(prefix="hyprial-au-", dir=hyprial_test_tmp_root()))
    state = root / "state"
    state.mkdir()
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="autoupdate-accept-test",
        hyprial_home=root / "home",
        **_app_injections(),
    )
    app._inbox = InboxService(state / "inbox.sqlite3", MemoryDeliveryTransport())
    app._harnesses = _FakeHarnesses()  # type: ignore[assignment]
    app._presence = _FakePresence()  # type: ignore[assignment]
    app._runtime = _FastRuntime()  # type: ignore[assignment]
    scheduler = AutoUpdateAuthority(
        state_dir=state,
        runner=hung_runner,
        hyprial_home=_home_with_auto_upgrade(root),
    )
    app._autoupdate = scheduler
    scheduler.start()
    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    try:
        triggered = _request(app.socket_path, "autoupdate.trigger")
        assert triggered["result"]["triggered"] is True
        assert entered.wait(1.0), "hung update never started"
        for _ in range(5):
            started = time.monotonic()
            response = _request(app.socket_path, "ps")
            assert response["result"]["daemon"]["running"] is True
            assert time.monotonic() - started < 1.0
    finally:
        release.set()
        assert scheduler.stop(timeout=2.0)
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        dispatcher.join(timeout=2.0)
        app._close()
        shutil.rmtree(root, ignore_errors=True)


def test_restart_notice_contains_recovery_facts_and_waits_for_bus_receipt() -> None:
    root = Path(tempfile.mkdtemp(prefix="hyprial-au-notify-", dir=hyprial_test_tmp_root()))
    state = root / "state"
    state.mkdir()
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="notify-node",
        owner="allen",
        hyprial_home=root / "home",
        **_app_injections(),
    )
    bus = _ConfirmingUserBus()
    app._user_delivery = bus  # type: ignore[assignment]
    try:
        result = app._deliver_autoupdate_restart_notification(
            {
                "oldVersion": "0.4.1",
                "newVersion": "0.4.2",
                "resolvedTag": "v0.4.2",
                "resolvedCommit": "b" * 40,
                "expectedInterruptionSeconds": 90,
            }
        )
        assert result["deliveryConfirmed"] is True
        assert result["nativeMessageId"] == "native-confirmation"
        [request] = bus.requests
        assert request.sender == "agent:allen:notify-node:squire"
        assert "0.4.1 → 0.4.2" in request.message
        # 3c116ad2 P3: no duration is promised any more -- fleet restores
        # measured 2-10 minutes against the old "不超过 N 秒" wording, and
        # the follow-up notice, not a number, closes the interruption.
        assert "恢复完成会再通知" in request.message
        assert "90" not in request.message and "不超过" not in request.message

        def assert_read_only_restart_guidance(text: str) -> None:
            assert "--connect" not in text
            assert "hyprial init" not in text

        # Non-empty control: this is the exact unsafe clause from dev@87f43cdc.
        old_notice = (
            "若长时间仍未恢复，请运行 hyprial daemon status --json 检查状态，并用 "
            "hyprial init --connect '' --json 重新拉起"
        )
        with pytest.raises(AssertionError):
            assert_read_only_restart_guidance(old_notice)
        assert_read_only_restart_guidance(request.message)
        assert "hyprial daemon status --json" in request.message
        assert "daemon.jsonl" in request.message
    finally:
        app._close()
        shutil.rmtree(root, ignore_errors=True)


def test_restart_notice_id_changes_for_same_version_new_commit() -> None:
    """Moving track tags must not collapse every upgrade into one old receipt."""

    root = Path(tempfile.mkdtemp(prefix="hyprial-au-notify-id-", dir=hyprial_test_tmp_root()))
    state = root / "state"
    state.mkdir()
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="notify-node",
        owner="allen",
        hyprial_home=root / "home",
        **_app_injections(),
    )
    bus = _ConfirmingUserBus()
    app._user_delivery = bus  # type: ignore[assignment]
    try:
        base = {
            "oldVersion": "0.4.2",
            "newVersion": "0.4.2",
            "resolvedTag": "dev",
            "expectedInterruptionSeconds": 90,
        }
        app._deliver_autoupdate_restart_notification(
            {**base, "resolvedCommit": "a" * 40}
        )
        app._deliver_autoupdate_restart_notification(
            {**base, "resolvedCommit": "b" * 40}
        )
        first, second = bus.requests
        assert first.message_id != second.message_id
        assert first.idempotency_key != second.idempotency_key
    finally:
        app._close()
        shutil.rmtree(root, ignore_errors=True)


def test_the_restart_notice_no_longer_promises_a_fixed_duration() -> None:
    """3c116ad2 T5 (source scan): the notice wording promises no duration.

    The old promise -- "预计服务中断不超过 N 秒" with N a CLI-side literal
    90 -- was written against a wait that no longer exists: fleet restores
    measured 2-10 minutes against it.  The daemon's fallback default for the
    (still-validated) field stays, but neither side's notice text may state
    a duration any more.
    """

    from tests.daemon.startup.test_startup_event_reconciliation import _native_application_source
    cli_source = Path(upgrade_commands.__file__).read_text("utf-8")
    app_source = _native_application_source()
    assert '"expectedInterruptionSeconds": 90' not in cli_source, (
        "the CLI sends a literal 90 as the interruption budget again; it "
        "must send the fleet-derived value"
    )
    assert "不超过" not in app_source and "不超过" not in cli_source, (
        "the notice promises an upper bound on interruption time again"
    )
    assert "预计服务中断" not in app_source, (
        "the daemon's notice still speaks a specific interruption duration"
    )
    assert "恢复完成会再通知" in app_source, (
        "the promised closure is the follow-up notice, and the wording that "
        "carries it is gone"
    )
