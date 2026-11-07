"""PAC expansion CLI behaviour against a mocked daemon IPC boundary."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.workflow.internal.expansion import (
    parse_expansion_target,
    read_expansion_file,
)
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_expansion_target_is_named_and_rejects_uri_like_values() -> None:
    assert parse_expansion_target("wf-123:build") == {
        "graphId": "wf-123",
        "nodeId": "build",
    }
    for value in ("", ":build", "wf-123:", "wf:build:extra", "wf:bad node"):
        try:
            parse_expansion_target(value)
        except ValueError:
            pass
        else:
            raise AssertionError(value)


def test_complete_reads_utf8_expansion_and_never_sends_the_filename(
    monkeypatch, tmp_path: Path
) -> None:
    expansion = tmp_path / "child.yaml"
    expansion.write_text("version: 2\nnodes: []\n含 Unicode\n", encoding="utf-8")
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, params, **_: calls.append((method, params))
        or {"event": {"type": "flag_set"}, "childGraphId": "child-1"},
    )

    result = CliRunner().invoke(
        _cli_root_app,
        [
            "workflow",
            "complete",
            "parent",
            "plan",
            "--request-id",
            "req-1",
            "--reason-ref",
            "evidence",
            "--expansion",
            str(expansion),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][0] == "workflow.complete"
    assert calls[0][1]["expansion"] == expansion.read_text(encoding="utf-8")
    assert str(expansion) not in json.dumps(calls[0][1])


def test_expansion_file_is_bounded_before_ipc(tmp_path: Path) -> None:
    path = tmp_path / "too-large.yaml"
    path.write_bytes(b"x" * 65_537)
    try:
        read_expansion_file(path)
    except ValueError as error:
        assert "65536" in str(error)
    else:
        raise AssertionError("oversized expansion was accepted")


def test_complete_reports_expansion_read_failure_as_json_without_ipc(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, params, **_: calls.append((method, params)),
    )
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "workflow",
            "complete",
            "parent",
            "plan",
            "--request-id",
            "req-1",
            "--reason-ref",
            "evidence",
            "--expansion",
            str(tmp_path / "missing.yaml"),
            "--json",
        ],
    )

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == "PAC_EXPANSION_INVALID"
    assert calls == []


def test_plan_expansion_for_uses_special_ipc_envelope(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "child.yaml"
    source.write_text("version: 2\nnodes: []\n", encoding="utf-8")
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, params, **_: calls.append((method, params))
        or {"graphId": "parent", "nodeId": "build", "plan": {}, "warnings": []},
    )
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "workflow",
            "plan",
            "--expansion-for",
            "parent:build",
            str(source),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "workflow.expansion.plan",
            {
                "graphId": "parent",
                "nodeId": "build",
                "yaml": source.read_text(encoding="utf-8"),
                "actor": "user:hyprial-unit-owner",
            },
        )
    ]


def test_fail_has_no_expansion_option_and_ordinary_plan_stays_offline(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "workflow.yaml"
    source.write_text(
        "version: 2\nname: ordinary\nnodes: [{id: work, task: Do it.}]\n",
        encoding="utf-8",
    )
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda method, params, **_: calls.append((method, params)))
    result = CliRunner().invoke(_cli_root_app, ["workflow", "plan", str(source), "--json"])
    assert result.exit_code == 0, result.output
    assert calls == []
