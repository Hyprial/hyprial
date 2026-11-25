"""All seven owner-key commands refuse a worker shell before any IPC (contract §6).

The refusal also precedes reading stdin, so a secret piped inside a worker
shell is never consumed or forwarded.
"""

from __future__ import annotations

import json

import pytest

from hyprial.kernel import ipc_errors

from .conftest import KEY_ID, SENTINEL, WORKER_ENV, FakeDaemon, invoke

COMMANDS = [
    ["user", "provider", "login", "openai", "--api-key-stdin"],
    ["user", "provider", "list"],
    ["user", "provider", "logout", KEY_ID, "--yes"],
    ["user", "provider", "rotate", KEY_ID, "--replace-stdin"],
    ["agent", "provider", "allow", "--actor", "pi-a", "--provider", "openai"],
    ["agent", "provider", "revoke", "--actor", "pi-a", "--provider", "openai"],
    ["agent", "provider", "status", "--actor", "pi-a"],
]


@pytest.mark.parametrize("variable", WORKER_ENV)
@pytest.mark.parametrize("command", COMMANDS, ids=lambda command: " ".join(command[:3]))
def test_worker_shell_is_refused_before_ipc(
    monkeypatch: pytest.MonkeyPatch, daemon: FakeDaemon, command: list[str], variable: str,
) -> None:
    monkeypatch.setenv(variable, "hyprial://synthetic/worker")

    result = invoke([*command, "--json"], stdin=SENTINEL)

    assert result.exit_code == 1
    assert daemon.calls == []
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == ipc_errors.CALLER_NOT_AUTHORIZED
    assert SENTINEL not in result.output


SECRET_COMMANDS = [
    ["user", "provider", "login", "openai", "--api-key-stdin"],
    ["user", "provider", "rotate", KEY_ID, "--replace-stdin"],
]


def _spy_reader(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from hyprial.shell.impl.cli.commands.user import provider as user_provider

    reads: list[str] = []

    def spy(services, *, requested: bool, flag: str) -> str:  # type: ignore[no-untyped-def]
        reads.append(flag)
        raise AssertionError("stdin was read")

    monkeypatch.setattr(user_provider, "read_owner_secret", spy)
    return reads


@pytest.mark.parametrize("command", SECRET_COMMANDS, ids=lambda command: command[2])
def test_secret_reader_is_wired_outside_a_worker_shell(
    monkeypatch: pytest.MonkeyPatch, daemon: FakeDaemon, command: list[str],
) -> None:
    reads = _spy_reader(monkeypatch)

    invoke([*command, "--json"], stdin=SENTINEL)

    assert reads == [command[-1]]
    assert daemon.calls == []


@pytest.mark.parametrize("variable", WORKER_ENV)
@pytest.mark.parametrize("command", SECRET_COMMANDS, ids=lambda command: command[2])
def test_worker_refusal_precedes_reading_stdin(
    monkeypatch: pytest.MonkeyPatch, daemon: FakeDaemon, command: list[str], variable: str,
) -> None:
    reads = _spy_reader(monkeypatch)
    monkeypatch.setenv(variable, "1")

    result = invoke([*command, "--json"], stdin=SENTINEL)

    assert reads == []
    assert daemon.calls == []
    payload = json.loads(result.stdout)
    assert payload["code"] == ipc_errors.CALLER_NOT_AUTHORIZED
    assert SENTINEL not in result.output


@pytest.mark.parametrize("command", SECRET_COMMANDS, ids=lambda command: command[2])
def test_worker_refusal_wins_over_a_terminal_stdin_refusal(
    monkeypatch: pytest.MonkeyPatch, daemon: FakeDaemon, command: list[str], tty,  # type: ignore[no-untyped-def]
) -> None:
    tty(True)
    monkeypatch.setenv("HYPRIAL_WORKER_ACTOR", "hyprial://synthetic/worker")

    result = invoke([*command, "--json"], stdin=SENTINEL)

    assert result.exit_code == 1
    assert daemon.calls == []
    assert json.loads(result.stdout)["code"] == ipc_errors.CALLER_NOT_AUTHORIZED


def test_logout_without_yes_in_worker_shell_is_refused_as_unauthorized(
    monkeypatch: pytest.MonkeyPatch, daemon: FakeDaemon, tty,  # type: ignore[no-untyped-def]
) -> None:
    tty(True)
    monkeypatch.setenv("HYPRIAL_MANAGED_WORKER", "1")

    result = invoke(["user", "provider", "logout", KEY_ID], stdin="y\n")

    assert result.exit_code == 1
    assert daemon.calls == []
