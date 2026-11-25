"""Uncertain deletion never becomes a fresh offline mutation."""

from pathlib import Path
from typing import Any
from typer.testing import CliRunner
from hyprial.kernel import ipc_errors
from tests.shell.cli.adapters.test_cli_adapter_remove import (
    _isolate, _seed_adapter, _daemon_script, _stopped_status, _cli_root_app,
)


def test_remove_never_falls_back_offline_after_execute_connection_loss(monkeypatch: Any, tmp_path: Path) -> None:
    home = _isolate(monkeypatch, tmp_path)
    _seed_adapter(home)
    before = (home / "channels.json").read_bytes()
    calls: list[str] = []
    _daemon_script(monkeypatch, {
        "adapter.status": _stopped_status(),
        "management.adapter.remove.preview": {"ok": True, "operationId": "remove-preview"},
        "management.adapter.remove": ipc_errors.DaemonUnavailableError("connection lost"),
    }, calls)
    result = CliRunner().invoke(_cli_root_app, ["adapter", "remove", "feishu", "--json"])
    assert result.exit_code == 1
    assert (home / "channels.json").read_bytes() == before
    assert (home / "secrets" / "lark-feishu.json").exists()


def test_offline_delete_preserves_pending_durable_removal(monkeypatch: Any, tmp_path: Path) -> None:
    from hyprial.daemon.impl.operations.adapter_removal.journal import AdapterRemovalJournal
    from hyprial.daemon.impl.state_db import StateDatabase
    from tests.daemon.management.removal.test_journal import intent

    home = _isolate(monkeypatch, tmp_path)
    _seed_adapter(home)
    before = (home / "channels.json").read_bytes()
    AdapterRemovalJournal(StateDatabase(home / "lifecycle-operations.sqlite3")).prepare(intent())
    _daemon_script(monkeypatch, {
        "adapter.status": ipc_errors.DaemonUnavailableError("no daemon"),
        "management.adapter.remove.preview": ipc_errors.DaemonUnavailableError("no daemon"),
    }, [])
    result = CliRunner().invoke(_cli_root_app, ["adapter", "remove", "feishu", "--json"])
    assert result.exit_code == 1
    assert (home / "channels.json").read_bytes() == before
    assert (home / "secrets/lark-feishu.json").exists()
