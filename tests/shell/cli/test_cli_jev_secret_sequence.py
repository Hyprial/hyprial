"""Public CLI ordering and isolation for the Jev secret control plane."""

from __future__ import annotations

from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_clean_home_public_cli_sequence_and_second_instance_isolation(monkeypatch) -> None:
    state: dict[str, object] = {"actors": set(), "entry": None, "grants": {}, "calls": []}

    def daemon(method: str, params: dict[str, object] | None = None, **kwargs: object) -> dict[str, object]:
        del kwargs
        params = params or {}
        state["calls"].append(method)  # type: ignore[union-attr]
        if method == "agent.create":
            state["actors"].add(params["name"])  # type: ignore[union-attr]
            return {"ok": True, "created": True, "agent": {"name": params["name"]}}
        if method == "agent.secret.provider-write":
            state["entry"] = {"entryId": params["entryId"], "fieldName": params["fieldName"], "value": params["value"]}
            return {"ok": True, "entryId": params["entryId"], "fieldNames": [params["fieldName"]]}
        if method == "agent.secret.grant":
            actor = params["actor"]
            if actor not in state["actors"]:  # type: ignore[operator]
                raise _cli_get_services().CliError("INVALID_ARGUMENT", "actor is absent")
            state["grants"][actor] = dict(params)  # type: ignore[index]
            return {"ok": True, "grant": {"actor": actor, "grantId": params["grantId"]}}
        if method == "lifecycle.start":
            actor = params["name"]
            if actor not in state["actors"] or actor not in state["grants"]:  # type: ignore[operator]
                raise _cli_get_services().CliError("INVALID_ARGUMENT", "actor must be created and granted first")
            return {"ok": True, "id": f"jev:{actor}"}
        raise AssertionError(method)

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon)
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    runner = CliRunner()
    secret = "fake-typesafe-secret-not-real"
    assert runner.invoke(_cli_root_app, ["agent", "create", "--name", "route-chooser", "--json"]).exit_code == 0
    written = runner.invoke(
        _cli_root_app,
        ["agent", "secret", "provider-write", "typesafe", "--field", "key", "--json"],
        input=secret + "\n",
    )
    assert written.exit_code == 0
    assert secret not in written.stdout
    granted = runner.invoke(
        _cli_root_app,
        [
            "agent", "secret", "grant", "route-chooser", "--grant-id", "typesafe-key",
            "--source", "user-provider", "--entry-id", "typesafe", "--field-name", "key",
            "--environment-name", "TYPESAFE_API_KEY", "--revision", "1", "--json",
        ],
    )
    assert granted.exit_code == 0
    started = runner.invoke(_cli_root_app, ["agent", "start", "jev", "--name", "route-chooser", "--json"])
    assert started.exit_code == 0

    assert runner.invoke(_cli_root_app, ["agent", "create", "--name", "second", "--json"]).exit_code == 0
    assert "second" not in state["grants"]  # type: ignore[operator]
    assert state["calls"][:4] == [
        "agent.create", "agent.secret.provider-write", "agent.secret.grant", "agent.create"
    ]


def test_secret_cli_refusal_performs_zero_writes(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda method, params=None, **kwargs: calls.append(method) or {"ok": True})
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "secret", "provider-write", "typesafe", "--field", "key", "--json"],
        input="\n",
    )
    assert result.exit_code != 0
    assert calls == []
