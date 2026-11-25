"""CLI contract for persistent managed harness arguments."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.kernel import RequestPortError, ipc_errors
from hyprial.shell.impl.cli.commands.agent.runtime import start as start_module
from hyprial.shell.impl.cli.commands.common import support as cli_support
from hyprial.shell.impl.cli.commands.common.root import app as root_app
from hyprial.shell.impl.cli.commands.common.services import get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401


def _get_result(harness_args: dict[str, list[str]] | None = None) -> dict[str, Any]:
    stored = deepcopy(harness_args or {})
    return {
        "ok": True,
        "agent": {"actor": "worker"},
        "harnessArgs": stored,
        "harnesses": [
            {
                "harness": harness,
                "args": list(args),
                "valid": True,
                "issues": [],
                "warnings": [],
                "dangerous": [],
            }
            for harness, args in sorted(stored.items())
        ],
    }


class _Daemon:
    def __init__(
        self,
        *,
        harness_args: dict[str, list[str]] | None = None,
        missing_harness_methods: bool = False,
        conflict: bool = False,
        old_start: bool = False,
    ) -> None:
        self.harness_args = deepcopy(harness_args or {})
        self.missing_harness_methods = missing_harness_methods
        self.conflict = conflict
        self.old_start = old_start
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        payload = deepcopy(params or {})
        self.calls.append((method, payload))
        if self.missing_harness_methods and method.startswith("agent.harness-args."):
            raise RequestPortError(ipc_errors.METHOD_NOT_FOUND, "unknown daemon method")
        if method == "agent.harness-args.get":
            return _get_result(self.harness_args)
        if method == "agent.harness-args.validate":
            args = payload["args"]
            invalid = args == ["--json"]
            dangerous = (
                [
                    {
                        "token": "--settings",
                        "category": "code-execution",
                        "reason": "loads managed launch settings",
                    }
                ]
                if "--settings" in args
                else []
            )
            return {
                "ok": True,
                "harness": payload["harness"],
                "args": args,
                "valid": not invalid,
                "issues": ["--json is not supported"] if invalid else [],
                "warnings": [],
                "dangerous": dangerous,
            }
        if method == "agent.harness-args.set":
            if self.conflict:
                raise RequestPortError(
                    ipc_errors.HARNESS_ARGS_CONFLICT,
                    "stored harness args changed",
                    {"action": "re-read", "method": "agent.harness-args.get"},
                )
            self.harness_args[payload["harness"]] = list(payload["args"])
            return {
                "ok": True,
                "changed": True,
                "agent": {"actor": payload["name"]},
                "harnessArgs": deepcopy(self.harness_args),
                "auditId": "agent_harness_args_audit:1",
            }
        if method == "agent.harness-args.clear":
            if payload.get("all") is True:
                self.harness_args = {}
            else:
                self.harness_args.pop(payload["harness"], None)
            return {
                "ok": True,
                "changed": True,
                "agent": {"actor": payload["name"]},
                "harnessArgs": deepcopy(self.harness_args),
                "auditId": "agent_harness_args_audit:2",
            }
        if method == "agent.start":
            if self.old_start and payload.get("ignoreStoredHarnessArgs") is True:
                raise RequestPortError(
                    ipc_errors.INVALID_ARGUMENT,
                    "agent.start params do not match the declared schema at <root>",
                )
            return {"ok": True, "accepted": True, "running": False}
        if method == "agent.create":
            return {"ok": True, "agent": {"actor": payload["name"]}}
        if method == "lifecycle.start":
            return {"ok": True, "accepted": True, "running": False}
        raise AssertionError(f"unexpected daemon method {method}")


def _install(monkeypatch: pytest.MonkeyPatch, daemon: _Daemon) -> None:
    monkeypatch.setattr(cli_support, "_daemon_request", daemon)


def _invoke(*arguments: str, input: str | None = None):  # type: ignore[no-untyped-def]
    return CliRunner().invoke(
        root_app,
        ["agent", *arguments],
        input=input,
        color=False,
    )


def test_args_help_and_start_help_publish_the_new_surface() -> None:
    args_help = _invoke("args", "--help")
    start_help = _invoke("start", "--help")

    assert args_help.exit_code == 0, args_help.output
    assert all(name in args_help.output for name in ("show", "validate", "set", "clear"))
    assert start_help.exit_code == 0, start_help.output
    assert "--ignore-stored-args" in start_help.output


def test_show_json_is_the_daemon_result(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = _Daemon(harness_args={"pi": ["--offline"]})
    _install(monkeypatch, daemon)

    result = _invoke("args", "show", "worker", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == _get_result({"pi": ["--offline"]})
    assert daemon.calls == [("agent.harness-args.get", {"name": "worker"})]


def test_validate_requires_separator_and_passes_every_token(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    daemon = _Daemon()
    _install(monkeypatch, daemon)

    missing = _invoke("args", "validate", "claude", "--model", "sonnet")
    valid = _invoke(
        "args", "validate", "claude", "--json", "--", "--model", "sonnet"
    )

    assert missing.exit_code != 0
    assert valid.exit_code == 0, valid.output
    assert json.loads(valid.stdout)["valid"] is True
    assert daemon.calls == [
        (
            "agent.harness-args.validate",
            {"harness": "claude", "args": ["--model", "sonnet"]},
        )
    ]


def test_invalid_validation_is_json_failure_with_exit_one(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    daemon = _Daemon()
    _install(monkeypatch, daemon)

    result = _invoke("args", "validate", "claude", "--json", "--", "--json")

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["valid"] is False
    assert payload["issues"] == ["--json is not supported"]


def test_set_uses_full_map_cas_reason_and_passthrough(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    daemon = _Daemon(harness_args={"pi": ["--offline"]})
    _install(monkeypatch, daemon)

    result = _invoke(
        "args", "set", "worker", "claude", "--reason", "model pin",
        "--json", "--", "--model", "sonnet",
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["changed"] is True
    assert daemon.calls == [
        ("agent.harness-args.get", {"name": "worker"}),
        (
            "agent.harness-args.validate",
            {"harness": "claude", "args": ["--model", "sonnet"]},
        ),
        (
            "agent.harness-args.set",
            {
                "name": "worker",
                "harness": "claude",
                "args": ["--model", "sonnet"],
                "expectedHarnessArgs": {"pi": ["--offline"]},
                "reason": "model pin",
            },
        ),
    ]


def test_set_empty_group_refuses_instead_of_clearing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    daemon = _Daemon(harness_args={"claude": ["--model", "sonnet"]})
    _install(monkeypatch, daemon)

    result = _invoke("args", "set", "worker", "claude", "--json", "--")

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["code"] == ipc_errors.INVALID_ARGUMENT
    assert "agent args clear" in payload["error"]
    assert daemon.calls == []


def test_dangerous_set_requires_noninteractive_flag(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    daemon = _Daemon()
    _install(monkeypatch, daemon)
    monkeypatch.setattr(get_services(), "_stdin_isatty", lambda: False)

    result = _invoke(
        "args", "set", "worker", "claude", "--json", "--", "--settings", "{}"
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "--yes-dangerous" in payload["error"]
    assert [method for method, _params in daemon.calls] == [
        "agent.harness-args.get",
        "agent.harness-args.validate",
    ]


@pytest.mark.parametrize("interactive", [False, True])
def test_dangerous_set_confirms_explicitly(monkeypatch, interactive: bool) -> None:  # type: ignore[no-untyped-def]
    daemon = _Daemon()
    _install(monkeypatch, daemon)
    monkeypatch.setattr(get_services(), "_stdin_isatty", lambda: interactive)
    options = [] if interactive else ["--yes-dangerous", "--json"]

    result = _invoke(
        "args", "set", "worker", "claude", *options,
        "--", "--settings", "{}", input="y\n" if interactive else None,
    )

    assert result.exit_code == 0, result.output
    method, params = daemon.calls[-1]
    assert method == "agent.harness-args.set"
    assert params["confirmDangerous"] is True
    if interactive:
        assert "code-execution" in result.stderr


def test_conflict_is_actionable_and_never_retried(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    daemon = _Daemon(conflict=True)
    _install(monkeypatch, daemon)

    result = _invoke(
        "args", "set", "worker", "pi", "--json", "--", "--offline"
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["code"] == ipc_errors.HARNESS_ARGS_CONFLICT
    assert "re-run" in payload["error"]
    methods = [method for method, _params in daemon.calls]
    assert methods.count("agent.harness-args.set") == 1
    assert methods.count("agent.harness-args.get") == 1


def test_clear_all_requires_confirmation_then_sends_full_map(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    stored = {"legacy": ["--unknown"], "pi": ["--offline"]}
    refused_daemon = _Daemon(harness_args=stored)
    _install(monkeypatch, refused_daemon)
    monkeypatch.setattr(get_services(), "_stdin_isatty", lambda: False)

    refused = _invoke("args", "clear", "worker", "--all", "--json")
    assert refused.exit_code == 1, refused.output
    assert "--yes-all" in json.loads(refused.stdout)["error"]
    assert [method for method, _params in refused_daemon.calls] == [
        "agent.harness-args.get"
    ]

    daemon = _Daemon(harness_args=stored)
    _install(monkeypatch, daemon)
    cleared = _invoke(
        "args", "clear", "worker", "--all", "--yes-all",
        "--reason", "retire legacy", "--json",
    )
    assert cleared.exit_code == 0, cleared.output
    assert json.loads(cleared.stdout)["harnessArgs"] == {}
    assert daemon.calls[-1] == (
        "agent.harness-args.clear",
        {
            "name": "worker",
            "all": True,
            "confirmAll": True,
            "expectedHarnessArgs": stored,
            "reason": "retire legacy",
        },
    )


def test_clear_all_accepts_interactive_confirmation(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    daemon = _Daemon(harness_args={"pi": ["--offline"]})
    _install(monkeypatch, daemon)
    monkeypatch.setattr(get_services(), "_stdin_isatty", lambda: True)

    result = _invoke("args", "clear", "worker", "--all", input="y\n")

    assert result.exit_code == 0, result.output
    assert "stored groups: pi" in result.stderr
    assert daemon.calls[-1][1]["confirmAll"] is True


def test_clear_one_group_is_idempotent_without_confirmation(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    daemon = _Daemon()
    _install(monkeypatch, daemon)

    result = _invoke("args", "clear", "worker", "claude", "--json")

    assert result.exit_code == 0, result.output
    assert daemon.calls[-1] == (
        "agent.harness-args.clear",
        {"name": "worker", "harness": "claude", "expectedHarnessArgs": {}},
    )


def test_new_cli_tells_old_daemon_to_upgrade(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    daemon = _Daemon(missing_harness_methods=True)
    _install(monkeypatch, daemon)

    result = _invoke("args", "show", "worker", "--json")

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["code"] == ipc_errors.METHOD_NOT_FOUND
    assert "upgrade" in payload["error"]
    assert len(daemon.calls) == 1


def test_start_ignore_reaches_fast_and_lifecycle_requests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    daemon = _Daemon()
    _install(monkeypatch, daemon)
    monkeypatch.setattr(start_module.shutil, "which", lambda _name: "/test/bin/claude")
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))

    fast = _invoke(
        "start", "claude", "--name", "fast", "--headless",
        "--ignore-stored-args", "--json",
    )
    lifecycle = _invoke(
        "start", "claude", "--name", "full", "--headless",
        "--cwd", str(tmp_path), "--ignore-stored-args", "--json",
    )

    assert fast.exit_code == 0, fast.output
    assert lifecycle.exit_code == 0, lifecycle.output
    fast_params = next(
        params for method, params in daemon.calls if method == "agent.start"
    )
    lifecycle_params = next(
        params for method, params in daemon.calls if method == "lifecycle.start"
    )
    assert fast_params["ignoreStoredHarnessArgs"] is True
    assert lifecycle_params["ignoreStoredHarnessArgs"] is True
    assert "--ignore-stored-args" not in lifecycle_params["args"]


def test_start_without_new_flag_stays_compatible_with_new_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    daemon = _Daemon()
    _install(monkeypatch, daemon)
    monkeypatch.setattr(start_module.shutil, "which", lambda _name: "/test/bin/claude")
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))

    result = _invoke(
        "start", "claude", "--name", "worker", "--headless", "--json"
    )

    assert result.exit_code == 0, result.output
    params = next(params for method, params in daemon.calls if method == "agent.start")
    assert "ignoreStoredHarnessArgs" not in params


def test_start_ignore_translates_old_daemon_schema_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    daemon = _Daemon(old_start=True)
    _install(monkeypatch, daemon)
    monkeypatch.setattr(start_module.shutil, "which", lambda _name: "/test/bin/claude")
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))

    result = _invoke(
        "start", "claude", "--name", "worker", "--headless",
        "--ignore-stored-args", "--json",
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["code"] == ipc_errors.INVALID_ARGUMENT
    assert "upgrade" in payload["error"]
    assert [method for method, _params in daemon.calls] == ["agent.start"]
