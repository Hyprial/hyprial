"""Helpers of the in-process daemon driver suites: the scripted world and a driven host."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from hyprial.shell.impl.gui.host.config import read_local_config
from hyprial.shell.impl.gui.host.local import LocalDaemon
from tests.shell.gui.host.support import GRANT, run_gateway
from tests.shell.request_http.rebind.support import OWNER, PhaseDaemon, install_daemon, list_result, write_home

ENV = {"HYPRIAL_GUI_BACKEND_DRIVER": "daemon", "HYPRIAL_GUI_LAUNCH_GRANT": GRANT}
NODE = "daemon-node"
FAILURE_BOUND_SECONDS = 20.0


def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, phase: str) -> PhaseDaemon:
    owner = OWNER if phase == "serving" else None
    write_home(tmp_path, owner=owner)
    daemon = install_daemon(monkeypatch, tmp_path, phase=phase, owner=owner)
    daemon.handlers["conversations.list"] = list_result
    return daemon


def open_local(tmp_path: Path, **kwargs: Any) -> LocalDaemon:
    home = tmp_path / "home"
    state = home / "state"
    return LocalDaemon.open(read_local_config(ENV), home=home, state_dir=state, socket=state / "daemon.sock", node=NODE, **kwargs)


def drive(local: LocalDaemon, scenario: Any) -> Any:
    return run_gateway(scenario, upstream=local.transport, config=local.config, authority=local.authority)


async def bearer_status(local: LocalDaemon, token: str, path: str = "/readyz") -> int:
    transport = local.transport
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        return (await client.get(path, headers={"Authorization": f"Bearer {token}"})).status_code
