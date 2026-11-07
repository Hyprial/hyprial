from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.kernel.impl.log import Logger
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.log.log as _cli_owner_1
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_log_queries_nested_worker_files_by_scope_level_and_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    Logger.worker(state_dir, runtime="pi", name="alpha").info(
        "worker.ready", pid=100
    )
    Logger.worker(state_dir, runtime="claude", name="beta").error(
        "worker.exited", pid=200, returnCode=7
    )
    (state_dir / "logs" / "workers" / "damaged.jsonl").write_text(
        "{not-json\n", encoding="utf-8"
    )

    result = _cli_owner_1._log_result(
        component="claude-worker",
        name="beta",
        level="error",
        actor=None,
        conversation=None,
        since="2000-01-01T00:00:00Z",
        until="2100-01-01T00:00:00Z",
        correlation_id=None,
    )

    assert result["ok"] is True
    assert result["skippedLines"] == 1
    assert [entry["event"] for entry in result["entries"]] == ["worker.exited"]


def test_log_rejects_an_unknown_level(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    with pytest.raises(_cli_get_services().CliError, match="--level must be"):
        _cli_owner_1._log_result(
            component=None,
            name=None,
            level="notice",
            actor=None,
            conversation=None,
            since=None,
            until=None,
            correlation_id=None,
        )


def test_log_queries_dead_letters_through_the_shared_adapter_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    Logger.adapter(state_dir, name="main").bind(component="adapter").log(
        "warn",
        "dead-letter",
        reason="no-target",
        message_id="om_reference",
        chat_id="oc_reference",
    )

    result = _cli_owner_1._log_result(
        component="adapter",
        name="main",
        level="warn",
        actor=None,
        conversation=None,
        since=None,
        until=None,
        correlation_id=None,
    )

    assert result["ok"] is True
    assert [entry["event"] for entry in result["entries"]] == ["dead-letter"]
    assert result["entries"][0]["reason"] == "no-target"


def test_log_help_lists_global_scope_filters() -> None:
    result = CliRunner().invoke(_cli_root_app, ["log", "--help"], color=False)

    assert result.exit_code == 0
    assert "--component" in result.output
    assert "--name" in result.output
    assert "--level" in result.output
