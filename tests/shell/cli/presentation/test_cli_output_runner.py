"""The command runner lets control-flow exits through and emits one result."""

import json
import sys

import typer
from typer.testing import CliRunner

import hyprial.shell.impl.cli.runtime.entry as cli
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app


def _probe_app(raised: BaseException) -> typer.Typer:
    app = typer.Typer()

    @app.command()
    def probe(json_output: bool = typer.Option(False, "--json")) -> None:
        def operation():
            raise raised

        get_services().runtime.execute(operation, json_output=json_output)

    return app


def _json_values(stdout: str) -> list[object]:
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def test_typer_exit_inside_an_operation_keeps_its_code_and_prints_nothing():
    result = CliRunner().invoke(_probe_app(typer.Exit(code=3)), ["--json"])
    assert result.exit_code == 3
    assert result.stdout == ""


def test_abort_inside_an_operation_reaches_main_as_aborted(monkeypatch, capsys):
    monkeypatch.setattr(cli, "app", _probe_app(typer.Abort()))
    monkeypatch.setattr(sys, "argv", ["hyprial", "--json"])
    try:
        cli.main()
    except SystemExit as exit_:
        assert exit_.code == 1
    else:  # pragma: no cover - main must exit non-zero
        raise AssertionError("main() returned after an abort")
    values = _json_values(capsys.readouterr().out)
    assert values == [{"ok": False, "code": "ABORTED", "error": "operation aborted"}]


def test_managed_worker_without_binding_emits_exactly_one_json_value(monkeypatch):
    monkeypatch.setenv("HYPRIAL_MANAGED_WORKER", "1")
    monkeypatch.delenv("HYPRIAL_WORKER_ACTOR", raising=False)
    monkeypatch.delenv("HYPRIAL_WORKER_SESSION_REF", raising=False)
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda *a, **k: {"ok": True})
    result = CliRunner().invoke(_cli_root_app, ["workflow", "inspect", "wf-x", "--json"])
    assert result.exit_code == 1
    values = _json_values(result.stdout)
    assert len(values) == 1, result.stdout
    assert values[0]["ok"] is False
    assert values[0]["code"] == "PAC_PRINCIPAL_UNVERIFIED"


def _returning_app(value: object) -> typer.Typer:
    app = typer.Typer()

    @app.command()
    def probe(json_output: bool = typer.Option(False, "--json")) -> None:
        get_services().runtime.execute(lambda: value, json_output=json_output)

    return app


def test_a_success_without_ok_gets_ok_true_and_keeps_every_field():
    result = CliRunner().invoke(_returning_app({"items": [1], "next": None}), ["--json"])
    assert result.exit_code == 0
    assert result.stdout == '{"ok":true,"items":[1],"next":null}\n'


def test_a_result_that_carries_ok_keeps_it_unchanged():
    result = CliRunner().invoke(_returning_app({"count": 2, "ok": True}), ["--json"])
    assert result.exit_code == 0
    assert result.stdout == '{"count":2,"ok":true}\n'


def test_a_result_reporting_ok_false_exits_1_in_both_modes():
    value = {"ok": False, "checks": [{"status": "fail"}]}
    as_json = CliRunner().invoke(_returning_app(value), ["--json"])
    assert as_json.exit_code == 1
    assert _json_values(as_json.stdout) == [value]
    human = CliRunner().invoke(_returning_app(value), [])
    assert human.exit_code == 1


def test_an_operation_returning_text_is_a_programming_error():
    result = CliRunner().invoke(_returning_app("hand-built text"), [])
    assert isinstance(result.exception, TypeError)
    assert "CliResult" in str(result.exception)
