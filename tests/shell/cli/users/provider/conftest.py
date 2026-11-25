"""Shared fixtures for the owner-key CLI: an isolated home and a fake daemon.

The fake replaces ``_daemon_request`` so every test sees exactly which IPC
method and params the CLI sent; nothing reaches a real daemon or home.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

WORKER_ENV = ("HYPRIAL_WORKER_ACTOR", "HYPRIAL_WORKER_SESSION_REF", "HYPRIAL_MANAGED_WORKER")
SENTINEL = "sk-synthetic-SENTINEL-0123456789"
KEY_ID = "pk.mfrggzdfmztwq2lknnwg23tpob"


def key_view(**overrides: Any) -> dict[str, Any]:
    view: dict[str, Any] = {
        "keyId": KEY_ID, "provider": "openai", "kind": "api-key", "label": "work",
        "owner": "owner-sub", "state": "active", "createdAt": 1, "updatedAt": 2,
        "dekVersion": 1, "environmentNames": ["OPENAI_API_KEY"],
        "recipients": [{"recipientKeyId": "x25519:" + "0" * 32, "role": "owner-device",
                        "deviceUid": "dev-1"}],
        "lastUsedAt": None, "grants": [],
    }
    view.update(overrides)
    return view


def grant_view(**overrides: Any) -> dict[str, Any]:
    view: dict[str, Any] = {
        "agent": "00000000-0000-0000-0000-000000000001", "agentId": "a.aaaaaaaaaaaaaaaaaaaaaaaaaa",
        "actor": "pi-a", "grantId": "openai", "provider": "openai", "owner": "owner-sub",
        "keyId": KEY_ID, "state": "active", "seq": 1, "dekVersion": 1, "issuedAt": 1,
        "expiresAt": 2, "environmentNames": ["OPENAI_API_KEY"], "recipientKeyId": None,
        "legacyRef": None,
    }
    view.update(overrides)
    return view


class FakeDaemon:
    """Record every request and answer from a per-method table."""

    def __init__(self, answers: Mapping[str, Any] | None = None) -> None:
        self.answers: dict[str, Any] = dict(answers or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, method: str, params: dict[str, Any] | None = None, **_kwargs: Any) -> Any:
        self.calls.append((method, dict(params or {})))
        answer = self.answers.get(method, {})
        if isinstance(answer, Exception):
            raise answer
        return answer(params) if callable(answer) else answer

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    for name in WORKER_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "hyprial"))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    monkeypatch.setattr(_cli_get_services(), "_stdin_isatty", lambda: False)


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> FakeDaemon:
    fake = FakeDaemon()
    monkeypatch.setattr(_cli_support, "_daemon_request", fake)
    return fake


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    def set_tty(value: bool) -> None:
        monkeypatch.setattr(_cli_get_services(), "_stdin_isatty", lambda: value)

    return set_tty


def invoke(arguments: list[str], *, stdin: str | bytes | None = None) -> Any:
    return CliRunner().invoke(_cli_root_app, arguments, input=stdin)
