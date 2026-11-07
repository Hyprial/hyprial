"""``hyprial agent create`` seeds the new agent from the machine default.

Decision 1 (Allen, 2026-10-14): a newly created agent takes its provider from
hyprial. The daemon reports the default on ``agent.create``; the CLI reads the
operator's host login and seeds through ``agent.provider.login``. Creation never
fails because of seeding: the result says ``seeded``, ``refused`` or
``not-configured`` and the hint names both fixes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

PAYLOAD = b'{"claudeAiOauth": {"accessToken": "test-only"}}\n'


class _Daemon:
    def __init__(self, default: str | None, refuse_login: str | None = None) -> None:
        self.default = default
        self.refuse_login = refuse_login
        self.calls: list[tuple[str, object]] = []

    def __call__(self, method: str, params: object = None, **_kwargs: object) -> object:
        self.calls.append((method, params))
        if method == "agent.create":
            created: dict[str, object] = {"ok": True, "agent": {"actor": "fresh"}}
            if self.default is not None:
                created["providerDefault"] = self.default
            return created
        if method == "agent.provider.login":
            if self.refuse_login is not None:
                raise _cli_get_services().CliError(self.refuse_login, "refused by the daemon")
            return {
                "ok": True,
                "actor": "fresh",
                "provider": "claude",
                "files": [{"path": "/agent/home/x"}],
            }
        raise AssertionError(f"unexpected {method}")


@pytest.fixture
def operator_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "operator-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.delenv("HYPRIAL_WORKER_ACTOR", raising=False)
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    return home


def _install(monkeypatch: pytest.MonkeyPatch, daemon: _Daemon) -> _Daemon:
    monkeypatch.setattr(_cli_support, "_daemon_request", daemon)
    return daemon


def _host_login(home: Path) -> None:
    source = home / ".claude" / ".credentials.json"
    source.parent.mkdir(parents=True)
    source.write_bytes(PAYLOAD)


def _create(*extra: str):  # type: ignore[no-untyped-def]
    return CliRunner().invoke(_cli_root_app, ["agent", "create", "--name", "fresh", *extra])


def test_without_a_default_create_prompts_for_either_fix(
    operator_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = _install(monkeypatch, _Daemon(default=None))

    result = _create("--json")

    assert result.exit_code == 0, result.output
    assert [method for method, _ in daemon.calls] == ["agent.create"]
    seed = json.loads(result.stdout)["providerSeed"]
    assert seed["state"] == "not-configured"
    hint = " ".join(seed["hint"])
    assert "hyprial agent provider login <provider> --actor fresh" in hint
    assert "hyprial agent secret grant fresh" in hint


def test_without_a_default_the_human_view_prints_the_hint_on_stderr(
    operator_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _Daemon(default=None))

    result = _create()

    assert result.exit_code == 0, result.output
    assert "hyprial agent provider login" in result.stderr


def test_with_a_default_create_seeds_the_new_agent(
    operator_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host_login(operator_home)
    daemon = _install(monkeypatch, _Daemon(default="claude"))

    result = _create("--json")

    assert result.exit_code == 0, result.output
    assert [method for method, _ in daemon.calls] == [
        "agent.create",
        "agent.provider.login",
    ]
    sent = daemon.calls[1][1]
    assert isinstance(sent, dict)
    assert (sent["actor"], sent["provider"], sent["force"]) == ("fresh", "claude", False)
    seed = json.loads(result.stdout)["providerSeed"]
    assert seed["state"] == "seeded"
    assert seed["revoke"] == "hyprial agent provider logout claude --actor fresh"
    assert "test-only" not in result.stdout + result.stderr


def test_a_missing_host_login_is_refused_but_the_agent_is_created(
    operator_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = _install(monkeypatch, _Daemon(default="claude"))

    result = _create("--json")

    assert result.exit_code == 0, result.output
    assert [method for method, _ in daemon.calls] == ["agent.create"]
    payload = json.loads(result.stdout)
    assert payload["agent"] == {"actor": "fresh"}
    seed = payload["providerSeed"]
    assert seed["state"] == "refused"
    assert seed["code"] == ipc_errors.PROVIDER_SOURCE_MISSING
    assert any("provider login claude --actor fresh" in line for line in seed["hint"])


def test_a_daemon_refusal_is_reported_with_its_code(
    operator_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host_login(operator_home)
    _install(
        monkeypatch,
        _Daemon(default="claude", refuse_login=ipc_errors.PROVIDER_UNSAFE_PATH),
    )

    result = _create("--json")

    assert result.exit_code == 0, result.output
    seed = json.loads(result.stdout)["providerSeed"]
    assert (seed["state"], seed["code"]) == ("refused", ipc_errors.PROVIDER_UNSAFE_PATH)


def test_a_default_for_another_harness_is_skipped_not_copied(
    operator_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host_login(operator_home)
    daemon = _install(monkeypatch, _Daemon(default="claude"))

    result = _create("--preferred-harness", "pi", "--json")

    assert result.exit_code == 0, result.output
    assert [method for method, _ in daemon.calls] == ["agent.create"]
    seed = json.loads(result.stdout)["providerSeed"]
    assert (seed["state"], seed["provider"]) == ("skipped", "claude")
    assert any("provider login pi --actor fresh" in line for line in seed["hint"])


def test_custom_codex_provider_does_not_receive_openai_auth_projection(
    operator_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = _install(monkeypatch, _Daemon(default="codex"))

    result = _create(
        "--provider", "deepseek", "--preferred-harness", "codex", "--json"
    )

    assert result.exit_code == 0, result.output
    assert [method for method, _ in daemon.calls] == ["agent.create"]
    seed = json.loads(result.stdout)["providerSeed"]
    assert seed == {
        "state": "skipped",
        "provider": "codex",
        "modelProvider": "deepseek",
        "hint": [
            "the agent selects custom Codex provider deepseek; the machine's "
            "renewable OpenAI auth.json is not projected",
            "grant its API key: hyprial agent secret grant fresh --source "
            "user-provider ...",
        ],
    }


def test_a_matching_preferred_harness_is_seeded(
    operator_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host_login(operator_home)
    daemon = _install(monkeypatch, _Daemon(default="claude"))

    result = _create("--preferred-harness", "claude", "--json")

    assert result.exit_code == 0, result.output
    assert [method for method, _ in daemon.calls] == ["agent.create", "agent.provider.login"]


def test_a_source_that_vanishes_before_reading_is_a_refusal(
    operator_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pathlib import Path as _Path

    _host_login(operator_home)
    daemon = _install(monkeypatch, _Daemon(default="claude"))

    def vanished(self: _Path) -> bytes:
        raise FileNotFoundError(2, "No such file or directory", str(self))

    monkeypatch.setattr(_Path, "read_bytes", vanished)

    result = _create("--json")

    assert result.exit_code == 0, result.output
    seed = json.loads(result.stdout)["providerSeed"]
    assert (seed["state"], seed["code"]) == ("refused", ipc_errors.PROVIDER_SOURCE_MISSING)
    assert [method for method, _ in daemon.calls] == ["agent.create"]
