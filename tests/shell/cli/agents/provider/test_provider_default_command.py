"""``--default`` on ``agent provider login`` and ``agent provider default``.

The default is recorded by the daemon and only read by future ``agent create``
runs; these tests pin what the CLI sends and in which order, with the daemon
seam stubbed and the operator's HOME a temporary directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

PAYLOAD = b'{"claudeAiOauth": {"accessToken": "test-only"}}\n'


class _Captured:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def __call__(self, method: str, params: object = None, **_kwargs: object) -> object:
        self.calls.append((method, params))
        if method == "agent.provider.default":
            return {"ok": True, "provider": "claude", "previous": "codex"}
        return {
            "ok": True,
            "actor": "worker",
            "provider": "claude",
            "files": [{"path": "/agent/home/x"}],
            "default": {"provider": "claude", "previous": "codex"},
        }


@pytest.fixture
def operator_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "operator-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("HYPRIAL_WORKER_ACTOR", raising=False)
    return home


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> _Captured:
    captured = _Captured()
    monkeypatch.setattr(_cli_support, "_daemon_request", captured)
    return captured


def _seed_host_login(home: Path) -> None:
    source = home / ".claude" / ".credentials.json"
    source.parent.mkdir(parents=True)
    source.write_bytes(PAYLOAD)


def _invoke(*arguments: str):  # type: ignore[no-untyped-def]
    return CliRunner().invoke(_cli_root_app, ["agent", "provider", *arguments])


def test_default_with_an_actor_seeds_then_records_and_names_the_previous(
    operator_home: Path, capture: _Captured
) -> None:
    _seed_host_login(operator_home)

    result = _invoke("login", "claude", "--actor", "worker", "--default", "--json")

    assert result.exit_code == 0, result.output
    assert [method for method, _ in capture.calls] == ["agent.provider.login"]
    assert capture.calls[0][1]["makeDefault"] is True
    payload = json.loads(result.stdout)
    assert payload["default"] == {"provider": "claude", "previous": "codex"}


def test_default_without_an_actor_requires_named_owner(
    operator_home: Path, capture: _Captured
) -> None:
    _seed_host_login(operator_home)

    result = _invoke("login", "claude", "--default", "--json")

    assert result.exit_code == 1, result.output
    assert capture.calls == []
    assert json.loads(result.stdout)["code"] == ipc_errors.INVALID_ARGUMENT


def test_default_refuses_a_provider_whose_host_login_is_missing(
    operator_home: Path, capture: _Captured
) -> None:
    result = _invoke("login", "claude", "--actor", "worker", "--default", "--json")

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == ipc_errors.PROVIDER_SOURCE_MISSING
    assert capture.calls == []


def test_default_command_reads_and_clears(
    operator_home: Path, capture: _Captured
) -> None:
    shown = _invoke("default", "--json")
    cleared = _invoke("default", "--clear", "--json")

    assert shown.exit_code == 0, shown.output
    assert cleared.exit_code == 0, cleared.output
    assert capture.calls == [
        ("agent.provider.default", {}),
        ("agent.provider.default", {"clear": True}),
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        ("login", "claude", "--default", "--json"),
        ("login", "claude", "--actor", "worker", "--default", "--json"),
        ("default", "--clear", "--json"),
    ],
)
def test_a_worker_shell_cannot_change_the_machine_default(
    operator_home: Path,
    capture: _Captured,
    monkeypatch: pytest.MonkeyPatch,
    arguments: tuple[str, ...],
) -> None:
    _seed_host_login(operator_home)
    monkeypatch.setenv("HYPRIAL_WORKER_ACTOR", "agent:owner:node:self")

    result = _invoke(*arguments)

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == ipc_errors.CALLER_NOT_AUTHORIZED
    assert capture.calls == []


def test_a_worker_shell_may_still_read_the_default(
    operator_home: Path, capture: _Captured, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HYPRIAL_WORKER_ACTOR", "agent:owner:node:self")

    result = _invoke("default", "--json")

    assert result.exit_code == 0, result.output
    assert capture.calls == [("agent.provider.default", {})]
