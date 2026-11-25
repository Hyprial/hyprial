"""CLI contract tests for the registered ``agent cell`` sub-application (I6-4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.kernel import RequestPortError, ipc_errors
from hyprial.shell.impl.cli.commands.agent.cell import cell_app
from hyprial.shell.impl.cli.commands.common import support as cli_support
from hyprial.shell.impl.cli.commands.common.root import app as root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401


class _FakeDaemon:
    def __init__(self, *, conflict: bool = False) -> None:
        self.conflict = conflict
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        payload = dict(params or {})
        self.calls.append((method, payload))
        if method == "agent.cell.get":
            return {
                "ok": True,
                "cell": {
                    "cellId": "c.example",
                    "revision": 7,
                    "members": [{"ref": "worker", "role": "harness"}],
                    "params": {"cell.entry": "worker"},
                },
                "snapshot": {
                    "cellId": "c.example",
                    "epoch": "ce-00000000000000000001-example",
                    "sequence": 1,
                    "revision": 7,
                    "effectiveRevision": 7,
                    "observedDocumentDigest": "d1",
                    "effectiveDocumentDigest": "d1",
                    "entryMember": "worker",
                    "state": "active",
                    "reason": None,
                    "sourceStale": False,
                    "updatedAtMs": 1000,
                    "members": [],
                    "activeWork": None,
                    "queuedWork": 0,
                    "completedWork": 0,
                    "lastTerminal": None,
                    "connections": [],
                    "failOpen": None,
                },
            }
        if method == "agent.cell.update":
            if self.conflict:
                raise RequestPortError(
                    ipc_errors.INVALID_ARGUMENT,
                    "cell revision conflict: expected 6, current 7",
                    {"expected": 6, "current": 7},
                )
            return {
                "ok": True,
                "cellId": "c.example",
                "revision": 8,
                "appliedRevision": 7,
                "state": "active",
            }
        raise AssertionError(f"unexpected daemon method {method}")


def _invoke(*args: str) -> Any:
    return CliRunner().invoke(cell_app, list(args), color=False)


def test_cell_app_is_mounted_onto_public_cli_in_i6_4() -> None:
    result = CliRunner().invoke(root_app, ["agent", "cell", "--help"], color=False)
    assert result.exit_code == 0, result.output
    assert "show" in result.output
    assert "apply" in result.output


def test_show_json_and_human_render(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = _FakeDaemon()
    monkeypatch.setattr(cli_support, "_daemon_request", daemon)

    json_result = _invoke("show", "worker", "--json")
    assert json_result.exit_code == 0, json_result.output
    parsed = json.loads(json_result.stdout)
    assert parsed["ok"] is True
    assert parsed["cell"]["cellId"] == "c.example"
    assert parsed["snapshot"]["state"] == "active"

    human_result = _invoke("show", "worker")
    assert human_result.exit_code == 0, human_result.output
    assert "c.example" in human_result.stdout
    assert "revision: 7" in human_result.stdout
    assert "state: active" in human_result.stdout
    assert daemon.calls == [
        ("agent.cell.get", {"name": "worker"}),
        ("agent.cell.get", {"name": "worker"}),
    ]


def test_apply_reads_file_and_sends_cas_update(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    daemon = _FakeDaemon()
    monkeypatch.setattr(cli_support, "_daemon_request", daemon)
    cell_file = tmp_path / "cell.json"
    cell_file.write_text(
        json.dumps(
            {
                "members": [{"ref": "worker", "role": "harness"}],
                "params": {"cell.entry": "worker"},
            }
        ),
        encoding="utf-8",
    )

    result = _invoke(
        "apply",
        "worker",
        "--expected-revision",
        "7",
        "--file",
        str(cell_file),
        "--json",
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed == {
        "ok": True,
        "cellId": "c.example",
        "revision": 8,
        "appliedRevision": 7,
        "state": "active",
    }
    assert daemon.calls == [
        (
            "agent.cell.update",
            {
                "name": "worker",
                "expectedRevision": 7,
                "members": [{"ref": "worker", "role": "harness"}],
                "params": {"cell.entry": "worker"},
            },
        )
    ]


def test_apply_rejects_immutable_fields_and_invalid_files_locally(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    daemon = _FakeDaemon()
    monkeypatch.setattr(cli_support, "_daemon_request", daemon)

    bad_file = tmp_path / "bad.json"
    bad_file.write_text(
        json.dumps({"id": "c.evil", "members": [], "params": {}}),
        encoding="utf-8",
    )
    refused = _invoke(
        "apply",
        "worker",
        "--expected-revision",
        "7",
        "--file",
        str(bad_file),
        "--json",
    )
    assert refused.exit_code == 1
    payload = json.loads(refused.stdout)
    assert payload["ok"] is False
    assert payload["code"] == ipc_errors.INVALID_ARGUMENT
    assert daemon.calls == []

    missing_file = tmp_path / "missing.json"
    missing = _invoke(
        "apply",
        "worker",
        "--expected-revision",
        "7",
        "--file",
        str(missing_file),
        "--json",
    )
    assert missing.exit_code == 1
    assert json.loads(missing.stdout)["ok"] is False
    assert daemon.calls == []


def test_apply_surfaces_cas_conflict_from_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    daemon = _FakeDaemon(conflict=True)
    monkeypatch.setattr(cli_support, "_daemon_request", daemon)
    cell_file = tmp_path / "cell.json"
    cell_file.write_text(json.dumps({"members": [], "params": {}}), encoding="utf-8")

    result = _invoke(
        "apply",
        "worker",
        "--expected-revision",
        "6",
        "--file",
        str(cell_file),
        "--json",
    )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == ipc_errors.INVALID_ARGUMENT
    assert payload["data"] == {"expected": 6, "current": 7}


def test_apply_maps_cell_name_and_rejects_nonpositive_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    daemon = _FakeDaemon()
    monkeypatch.setattr(cli_support, "_daemon_request", daemon)
    cell_file = tmp_path / "cell.json"
    cell_file.write_text(
        json.dumps({"name": "renamed-cell", "members": [], "params": {}}),
        encoding="utf-8",
    )

    bad_rev = _invoke(
        "apply",
        "worker",
        "--expected-revision",
        "0",
        "--file",
        str(cell_file),
        "--json",
    )
    assert bad_rev.exit_code == 1
    assert json.loads(bad_rev.stdout)["code"] == ipc_errors.INVALID_ARGUMENT
    assert daemon.calls == []

    ok = _invoke(
        "apply",
        "worker",
        "--expected-revision",
        "7",
        "--file",
        str(cell_file),
        "--json",
    )
    assert ok.exit_code == 0, ok.output
    assert daemon.calls == [
        (
            "agent.cell.update",
            {
                "name": "worker",
                "expectedRevision": 7,
                "cellName": "renamed-cell",
                "members": [],
                "params": {},
            },
        )
    ]
