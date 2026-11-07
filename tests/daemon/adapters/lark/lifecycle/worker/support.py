"""Unit tests for the Lark worker launcher's ready-signal channel.

Regression coverage for the production startup defect where the Lark SDK's
own stdout connection log was mistaken for the worker's ready signal.
"""

from __future__ import annotations

import json
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import hyprial.daemon.impl.adapters.lark.worker.entry as lark_worker
from hyprial.daemon.impl.adapters.lark.runtime.health import (
    LarkStreamHealthMonitor,
)
from hyprial.daemon.impl.adapters.lark.runtime.runtime import AdapterRuntime
from hyprial.daemon.impl.adapters.lark.worker.process import (
    LarkWorkerLauncher,
)
from hyprial.kernel import ChannelConfiguration, LarkGatewayConfig, PersistentConfigStore






def _launcher(tmp_path: Path, script: Path, *, startup_timeout: float = 5.0) -> LarkWorkerLauncher:
    home = tmp_path / "home"
    secret = home / "secrets" / "lark-test.json"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text(json.dumps({"appSecret": "unit-test-secret"}), encoding="utf-8")
    secret.chmod(0o600)
    return LarkWorkerLauncher(
        hyprial_home=home,
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        config_store=PersistentConfigStore(home, tmp_path / "state"),
        command=[sys.executable, str(script)],
        startup_timeout=startup_timeout,
    )


def _gateway() -> LarkGatewayConfig:
    return LarkGatewayConfig(
        name="lark-test",
        app_id="cli_unit_test",
        credential_ref="lark-test",
        routes=(),
    )


def _write(tmp_path: Path, source: str) -> Path:
    script = tmp_path / "fake_worker.py"
    script.write_text(textwrap.dedent(source), encoding="utf-8")
    return script






























































@dataclass
class _MainCallbackSeam:
    stream: Any
    adapter: Any
    health: LarkStreamHealthMonitor
    rebuilds: list[str]
    adapter_kwargs: dict[str, Any]
    notifications: list[tuple[str, str, str]]


#: Health monitors started by _worker_main_callback_seam in the current test.
_SEAM_MONITORS: list[LarkStreamHealthMonitor] = []


@pytest.fixture(autouse=True)
def _close_seam_health_monitors():  # type: ignore[no-untyped-def]
    """Drain and close every health monitor a test's worker main started.

    The monitor reports through a background thread; left running past the
    test, it printed a late ``healthy`` line into the next test's capsys
    (intermittent dev red in
    ``test_worker_main_ready_runs_one_reconcile_then_becomes_healthy``).
    Test modules that use the seam import this fixture by name.
    """

    yield
    while _SEAM_MONITORS:
        monitor = _SEAM_MONITORS.pop()
        monitor.drain_telemetry()
        monitor.close()


def _worker_main_callback_seam(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reconcile: Any,
    *,
    gateway_routes: tuple[Any, ...] = (),
    default_route: str | None = None,
) -> _MainCallbackSeam:
    gateway = SimpleNamespace(
        name="lark-test",
        app_id="cli_test_app",
        credential_ref="lark-test",
        routes=gateway_routes,
        default_route=default_route,
    )

    class Store:
        def load(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                channels=SimpleNamespace(gateways=(gateway,))
            )

        def lark_app_secret(self, credential_ref: str) -> str:
            return "unit-test-secret"

    class Adapter:
        def __init__(self, **_kwargs: object) -> None:
            self.calls = 0

        def begin_backfill(self) -> None:
            pass

        def reconcile_recent(self, *, lookback_seconds: int):  # type: ignore[no-untyped-def]
            self.calls += 1
            return reconcile(self.calls, lookback_seconds)

        def handle_sdk_event(self, _event: object) -> None:
            pass

        def handle_member_change_event(self, _event: object) -> None:
            pass

    class Gateway:
        app_id = "cli_test_app"
        app_secret = "unit-test-secret"

        def send_chat(
            self, native_id: str, text: str, *, idempotency_key: str
        ) -> str:
            notifications.append((native_id, text, idempotency_key))
            return "om_notification"

    class Stream:
        instance: Any = None

        def __init__(self, **callbacks: Any) -> None:
            self.callbacks = callbacks
            Stream.instance = self

        def probe_rest_endpoint(self) -> None:
            pass

        def start(self) -> None:
            pass

    adapter = Adapter()
    adapter_kwargs: dict[str, Any] = {}
    notifications: list[tuple[str, str, str]] = []
    health_instances: list[LarkStreamHealthMonitor] = []
    rebuilds: list[str] = []
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("HYPRIAL_LARK_READY_FD", raising=False)
    monkeypatch.setattr(lark_worker, "PersistentConfigStore", lambda *_args: Store())
    monkeypatch.setattr(
        lark_worker.LarkSdkGateway,
        "from_credentials",
        lambda *_args, **_kwargs: Gateway(),
    )
    def build_adapter(**kwargs: Any) -> Adapter:
        adapter_kwargs.update(kwargs)
        return adapter

    monkeypatch.setattr(lark_worker, "LarkAdapter", build_adapter)
    monkeypatch.setattr(lark_worker, "LarkEventStream", Stream)
    real_monitor = lark_worker.LarkStreamHealthMonitor

    def monitor(**kwargs: Any) -> LarkStreamHealthMonitor:
        instance = real_monitor(**kwargs)
        health_instances.append(instance)
        _SEAM_MONITORS.append(instance)
        return instance

    monkeypatch.setattr(lark_worker, "LarkStreamHealthMonitor", monitor)
    monkeypatch.setattr(
        lark_worker,
        "_recovery_timing_from_environment",
        lambda: (900.0, 3600.0, 0.02, 0.02),
    )
    monkeypatch.setattr(
        lark_worker,
        "_rebuild_after_stale",
        lambda reason: rebuilds.append(reason),
    )

    assert lark_worker.main(["lark-test"]) == 0
    assert Stream.instance is not None
    assert len(health_instances) == 1
    return _MainCallbackSeam(
        Stream.instance,
        adapter,
        health_instances[0],
        rebuilds,
        adapter_kwargs,
        notifications,
    )


def _wait_for_reconcile_settlement(
    health: LarkStreamHealthMonitor,
    timeout: float = 1.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with health._lock:
            reconciling = health._reconciling
        if not reconciling:
            return
        time.sleep(0.001)
    raise AssertionError("reconciliation did not settle")


























_ONLINE_WORKER_SCRIPT = """
import json, os, sys, time

name = sys.argv[1]
fd = int(os.environ["HYPRIAL_LARK_READY_FD"])
os.write(fd, json.dumps({"status": "online", "name": name}).encode() + b"\\n")
while True:
    time.sleep(0.05)
"""


def _online_supervisor(tmp_path: Path) -> AdapterRuntime:
    supervisor = AdapterRuntime(
        _launcher(tmp_path, _write(tmp_path, _ONLINE_WORKER_SCRIPT)),
        ChannelConfiguration(gateways=(_gateway(),)),
    )
    assert supervisor.start("lark-test") is True
    return supervisor










def _files_containing(root: Path, needle: str) -> list[str]:
    """Every file under ``root`` whose bytes contain ``needle``."""

    hits: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            if needle.encode() in path.read_bytes():
                hits.append(str(path.relative_to(root)))
        except OSError:
            hits.append(f"{path.relative_to(root)}:UNREADABLE")
    return sorted(hits)










# Shared builders/data and fixture registrations used by the focused modules.
__all__ = [
    '_MainCallbackSeam',
    '_ONLINE_WORKER_SCRIPT',
    '_files_containing',
    '_gateway',
    '_launcher',
    '_online_supervisor',
    '_wait_for_reconcile_settlement',
    '_worker_main_callback_seam',
    '_write',
]
