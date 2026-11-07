"""Shared isolated-daemon support for the P2 config rebind recovery suites.

Every helper here starts from a fresh temp state directory: no production
daemon, home, socket or credential is read or written by these tests.
"""

from __future__ import annotations

import json
import pytest
import socket
from pathlib import Path
from typing import ClassVar

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.kernel import HarnessLaunchSpec
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _deep_module_daemon_dependencies,
)


class StubPiProcess:
    """Only the final process boundary is replaced; daemon launch wiring is real."""

    instances: ClassVar[list["StubPiProcess"]] = []

    def __init__(
        self,
        spec: HarnessLaunchSpec,
        *,
        worker_channel: object,
        runtime_launch_custody: object,
        **_kwargs: object,
    ) -> None:
        self.spec = spec
        self.worker_channel = worker_channel
        self.running = True
        self.pid = None
        context = worker_channel.runtime_context  # type: ignore[attr-defined]
        assert context is not None
        self._custody = runtime_launch_custody(context)  # type: ignore[operator]
        self._custody.__enter__()
        self.instances.append(self)

    @property
    def in_flight(self) -> int:
        return 0

    @property
    def queue_depth(self) -> int:
        return 0

    def stop(self) -> None:
        if self.running:
            self.running = False
            self._custody.__exit__(None, None, None)


def new_application(root: Path) -> DaemonApplication:
    """Build a real daemon over a private temp state directory."""

    root.mkdir(mode=0o700, exist_ok=True)
    home = root / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    state = root / "state"
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        listen = f"tcp/127.0.0.1:{reservation.getsockname()[1]}"
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="node-1",
        owner="owner",
        hyprial_home=home,
        zenoh_listen=(listen,),
        zenoh_connect=(),
        **_deep_module_daemon_dependencies(),
    )
    app.network_isolated = True
    app._start_runtime()
    return app


def close_application(app: DaemonApplication) -> None:
    try:
        app._close()
    finally:
        app._close_home_lease()


def private_tree(root: Path, files: dict[str, bytes]) -> Path:
    """Create a mode-0700 staging tree whose files are all mode 0600."""

    root.mkdir(mode=0o700)
    for relative, body in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        for parent in (target.parent, *target.parent.parents):
            if parent == root.parent:
                break
            parent.chmod(0o700)
        target.write_bytes(body)
        target.chmod(0o600)
    return root


def workspace(root: Path) -> Path:
    """A launch cwd outside every agent home, revision and staging tree."""

    path = root / "workspace"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def launch_spec(name: str, cwd: Path) -> HarnessLaunchSpec:
    return HarnessLaunchSpec(
        harness="pi", name=name, headless=True, cwd=str(cwd), args=()
    )


def race_desired_state_before_binding(
    app: DaemonApplication,
    monkeypatch: pytest.MonkeyPatch,
    spec: HarnessLaunchSpec,
) -> dict[str, int]:
    """Write durable desired state between the last proof and the binding CAS.

    The hook sits on the identity owner call that performs the rebind binding
    CAS: the write lands after the service's final quiescence proof and before
    the mutation it authorises, which is exactly the reviewer's injection.
    One-shot: later calls (a resumed retry) are not raced again.
    """

    agent_actor = app._agent_session_domains.agent
    original = agent_actor.migration_owner_call
    counter = {"inserted": 0}

    def racing_call(command: object) -> object:
        if counter["inserted"] == 0 and str(
            getattr(command, "kind", "")
        ).startswith("bind-config"):
            counter["inserted"] += 1
            app.desired_state.upsert_harness(spec)
        return original(command)

    monkeypatch.setattr(agent_actor, "migration_owner_call", racing_call)
    return counter


def receipt_phases(app: DaemonApplication, actor: str) -> dict[str, str]:
    """Read every durable rebind receipt phase for one actor, keyed by id."""

    home = Path(app._agent_registry.home_receipt(actor).path)
    root = home / "state" / "config-rebind"
    phases: dict[str, str] = {}
    for path in sorted(root.glob("*/receipt.json")):
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if receipt.get("actor") == actor:
            phases[str(receipt.get("receiptId"))] = str(receipt.get("phase"))
    return phases
