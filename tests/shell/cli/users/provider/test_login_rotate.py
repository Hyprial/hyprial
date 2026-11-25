"""``hyprial user provider login|rotate``: secrets come only from non-TTY stdin.

Contract §6: one line, TTY refused, one trailing newline stripped, at most
16384 bytes, never echoed; IPC params exactly as §5.2.
"""

from __future__ import annotations

import json

import pytest

from hyprial.kernel import ipc_errors

from .conftest import KEY_ID, SENTINEL, FakeDaemon, invoke, key_view

LOGIN = ["user", "provider", "login", "openai", "--api-key-stdin"]


def _login_answer(daemon: FakeDaemon) -> None:
    daemon.answers["user.provider.login"] = {"key": key_view()}


def test_login_sends_exact_params_and_never_echoes_the_secret(daemon: FakeDaemon) -> None:
    _login_answer(daemon)

    result = invoke([*LOGIN, "--label", "work", "--env", "OPENAI_API_KEY",
                     "--env", "OPENAI_KEY", "--json"], stdin=SENTINEL + "\n")

    assert result.exit_code == 0, result.output
    assert daemon.calls == [("user.provider.login", {
        "provider": "openai", "kind": "api-key", "secret": SENTINEL, "label": "work",
        "environmentNames": ["OPENAI_API_KEY", "OPENAI_KEY"],
    })]
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["key"]["keyId"] == KEY_ID
    assert SENTINEL not in result.output


def test_login_omits_optional_params(daemon: FakeDaemon) -> None:
    _login_answer(daemon)

    result = invoke([*LOGIN], stdin=SENTINEL)

    assert result.exit_code == 0, result.output
    assert daemon.calls == [("user.provider.login", {
        "provider": "openai", "kind": "api-key", "secret": SENTINEL,
    })]
    assert KEY_ID in result.stdout
    assert SENTINEL not in result.output


def test_login_requires_the_stdin_flag(daemon: FakeDaemon) -> None:
    result = invoke(["user", "provider", "login", "openai", "--json"], stdin=SENTINEL)

    assert result.exit_code == 1
    assert daemon.calls == []
    assert json.loads(result.stdout)["code"] == ipc_errors.INVALID_ARGUMENT
    assert SENTINEL not in result.output


def test_login_refuses_a_tty(daemon: FakeDaemon, tty) -> None:  # type: ignore[no-untyped-def]
    tty(True)

    result = invoke([*LOGIN, "--json"], stdin=SENTINEL)

    assert result.exit_code == 1
    assert daemon.calls == []
    assert json.loads(result.stdout)["ok"] is False


@pytest.mark.parametrize("raw", [
    "", "\n", "fst9q\nsnd9q", "fst9q\nsnd9q\n", "cr9q\rrt9q", "zq9x\r\n", "\n\n",
])
def test_login_refuses_empty_or_multi_line_stdin(daemon: FakeDaemon, raw: str) -> None:
    result = invoke([*LOGIN, "--json"], stdin=raw)

    assert result.exit_code == 1
    assert daemon.calls == []
    error = json.loads(result.stdout)
    assert error["code"] == ipc_errors.INVALID_ARGUMENT
    if raw.strip():
        assert raw.strip().splitlines()[0] not in error["error"]


def test_login_accepts_exactly_the_byte_cap(daemon: FakeDaemon) -> None:
    _login_answer(daemon)
    secret = "k" * 16384

    result = invoke([*LOGIN, "--json"], stdin=secret + "\n")

    assert result.exit_code == 0, result.output
    assert daemon.calls[0][1]["secret"] == secret


@pytest.mark.parametrize("raw", ["k" * 16385, "k" * 16385 + "\n", "k" * 40000])
def test_login_refuses_secrets_over_the_byte_cap(daemon: FakeDaemon, raw: str) -> None:
    result = invoke([*LOGIN, "--json"], stdin=raw)

    assert result.exit_code == 1
    assert daemon.calls == []
    assert json.loads(result.stdout)["code"] == ipc_errors.INVALID_ARGUMENT


def test_login_counts_the_cap_in_utf8_bytes(daemon: FakeDaemon) -> None:
    result = invoke([*LOGIN, "--json"], stdin="é" * 8193)

    assert result.exit_code == 1
    assert daemon.calls == []


def test_login_refuses_invalid_utf8(daemon: FakeDaemon) -> None:
    result = invoke([*LOGIN, "--json"], stdin=b"\xff\xfe-key")

    assert result.exit_code == 1
    assert daemon.calls == []
    assert json.loads(result.stdout)["code"] == ipc_errors.INVALID_ARGUMENT


def test_rotate_without_replace_sends_only_the_key_id(daemon: FakeDaemon) -> None:
    daemon.answers["user.provider.rotate"] = {"key": key_view(dekVersion=2), "reissuedGrants": 3}

    result = invoke(["user", "provider", "rotate", KEY_ID, "--json"], stdin=SENTINEL)

    assert result.exit_code == 0, result.output
    assert daemon.calls == [("user.provider.rotate", {"keyId": KEY_ID})]
    payload = json.loads(result.stdout)
    assert payload["ok"] is True and payload["reissuedGrants"] == 3


def test_rotate_replace_reads_the_new_secret_from_stdin(daemon: FakeDaemon) -> None:
    daemon.answers["user.provider.rotate"] = {"key": key_view(dekVersion=2), "reissuedGrants": 0}

    result = invoke(["user", "provider", "rotate", KEY_ID, "--replace-stdin"], stdin=SENTINEL + "\n")

    assert result.exit_code == 0, result.output
    assert daemon.calls == [("user.provider.rotate", {"keyId": KEY_ID, "replaceSecret": SENTINEL})]
    assert KEY_ID in result.stdout
    assert SENTINEL not in result.output


def test_rotate_replace_refuses_a_tty(daemon: FakeDaemon, tty) -> None:  # type: ignore[no-untyped-def]
    tty(True)

    result = invoke(["user", "provider", "rotate", KEY_ID, "--replace-stdin", "--json"], stdin=SENTINEL)

    assert result.exit_code == 1
    assert daemon.calls == []


def test_rotate_replace_refuses_multi_line(daemon: FakeDaemon) -> None:
    result = invoke(["user", "provider", "rotate", KEY_ID, "--replace-stdin", "--json"],
                    stdin="a\nb\n")

    assert result.exit_code == 1
    assert daemon.calls == []


def test_daemon_refusal_is_ok_false_and_exit_one(daemon: FakeDaemon) -> None:
    from hyprial.shell.impl.cli.commands.common.services import get_services

    daemon.answers["user.provider.login"] = get_services().CliError(
        ipc_errors.PROVIDER_STORE_UNAVAILABLE, "owner key store is unavailable")

    result = invoke([*LOGIN, "--json"], stdin=SENTINEL)

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == ipc_errors.PROVIDER_STORE_UNAVAILABLE
    assert SENTINEL not in result.output
