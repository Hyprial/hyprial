"""``hyprial agent provider grant-key``: one user-provider entry, many agents.

The convenience is a loop over the existing ``agent.secret.grant``: each actor
gets its own grant (its own revoke), the source is always the daemon login
user's ``user-provider`` store, no secret value is read or echoed, and actors
are named one by one.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

BASE = [
    "agent", "provider", "grant-key",
    "--entry-id", "deepseek",
    "--field-name", "apiKey",
    "--environment-name", "DEEPSEEK_API_KEY",
    "--grant-id", "deepseek-key",
    "--revision", "1",
]


class _Daemon:
    def __init__(
        self,
        refuse: str | None = None,
        existing: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self.refuse = refuse
        self.existing = dict(existing or {})
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, method: str, params: dict[str, object], **_kwargs: object) -> object:
        self.calls.append((method, params))
        actor = str(params["actor"])
        if method == "agent.secret.list":
            held = self.existing.get(actor)
            return {"ok": True, "grants": [] if held is None else [held]}
        if actor == self.refuse:
            raise _cli_get_services().CliError(ipc_errors.AGENT_NOT_FOUND, "no such agent")
        self.existing[actor] = {key: value for key, value in params.items()}
        return {"ok": True, "grant": {"actor": actor, "grantId": params["grantId"]}}

    def grants(self) -> list[dict[str, object]]:
        return [params for method, params in self.calls if method == "agent.secret.grant"]


def _held(actor: str, entry: str = "deepseek", revision: int = 1) -> dict[str, object]:
    return {
        "actor": actor,
        "grantId": "deepseek-key",
        "source": "user-provider",
        "entryId": entry,
        "fieldName": "apiKey",
        "environmentNames": ["DEEPSEEK_API_KEY"],
        "revision": revision,
    }


@pytest.fixture(autouse=True)
def _initialized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)


def _run(monkeypatch: pytest.MonkeyPatch, daemon: _Daemon, *actors: str, extra: tuple[str, ...] = ()):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(_cli_support, "_daemon_request", daemon)
    arguments = [*BASE, *extra]
    for actor in actors:
        arguments += ["--actor", actor]
    return CliRunner().invoke(_cli_root_app, [*arguments, "--json"])


def test_each_actor_gets_its_own_user_provider_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = _Daemon()

    result = _run(monkeypatch, daemon, "pi-a", "pi-b")

    assert result.exit_code == 0, result.output
    assert len(daemon.grants()) == 2
    for params, actor in zip(daemon.grants(), ("pi-a", "pi-b"), strict=True):
        assert params == {
            "actor": actor,
            "grantId": "deepseek-key",
            "source": "user-provider",
            "entryId": "deepseek",
            "fieldName": "apiKey",
            "environmentNames": ["DEEPSEEK_API_KEY"],
            "revision": 1,
        }
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["granted"] == ["pi-a", "pi-b"]
    assert payload["revoke"] == [
        "hyprial agent secret revoke pi-a deepseek-key",
        "hyprial agent secret revoke pi-b deepseek-key",
    ]


def test_a_refusal_stops_the_loop_and_reports_every_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = _Daemon(refuse="pi-b")

    result = _run(monkeypatch, daemon, "pi-a", "pi-b", "pi-c")

    assert result.exit_code == 1, result.output
    assert [params["actor"] for params in daemon.grants()] == ["pi-a", "pi-b"]
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["granted"] == ["pi-a"]
    assert payload["failed"] == {
        "actor": "pi-b",
        "code": ipc_errors.AGENT_NOT_FOUND,
        "message": "no such agent",
    }
    assert payload["notAttempted"] == ["pi-c"]


def test_actors_must_be_named_and_distinct(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = _Daemon()

    missing = _run(monkeypatch, daemon)
    repeated = _run(monkeypatch, daemon, "pi-a", "pi-a")

    assert missing.exit_code != 0
    assert repeated.exit_code == 1, repeated.output
    assert json.loads(repeated.stdout)["code"] == ipc_errors.INVALID_ARGUMENT
    assert daemon.calls == []


def test_a_rerun_skips_actors_already_holding_the_same_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = _Daemon(existing={"pi-a": _held("pi-a")})

    result = _run(monkeypatch, daemon, "pi-a", "pi-b")

    assert result.exit_code == 0, result.output
    assert [params["actor"] for params in daemon.grants()] == ["pi-b"]
    payload = json.loads(result.stdout)
    assert payload["alreadyGranted"] == ["pi-a"]
    assert payload["granted"] == ["pi-b"]
    assert payload["revoke"] == [
        "hyprial agent secret revoke pi-a deepseek-key",
        "hyprial agent secret revoke pi-b deepseek-key",
    ]


def test_a_different_grant_under_the_same_id_is_not_replaced_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = _Daemon(existing={"pi-b": _held("pi-b", entry="other-vendor")})

    result = _run(monkeypatch, daemon, "pi-a", "pi-b", "pi-c")

    assert result.exit_code == 1, result.output
    assert [params["actor"] for params in daemon.grants()] == ["pi-a"]
    payload = json.loads(result.stdout)
    assert payload["failed"]["actor"] == "pi-b"
    assert payload["failed"]["code"] == ipc_errors.PROVIDER_CREDENTIAL_EXISTS
    assert "--replace" in payload["failed"]["message"]
    assert payload["notAttempted"] == ["pi-c"]


def test_replace_overwrites_and_reports_it(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = _Daemon(existing={"pi-b": _held("pi-b", entry="other-vendor")})

    result = _run(
        monkeypatch, daemon, "pi-a", "pi-b", extra=("--replace",)
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["granted"] == ["pi-a"]
    assert payload["replaced"] == ["pi-b"]


def test_a_replaced_grant_names_what_it_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = _Daemon(existing={"pi-b": _held("pi-b", entry="other-vendor", revision=3)})

    result = _run(monkeypatch, daemon, "pi-b", extra=("--replace",))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["replacedGrants"] == [
        {"actor": "pi-b", "entryId": "other-vendor", "fieldName": "apiKey", "revision": 3}
    ]
