from __future__ import annotations

import json
from pathlib import Path
import pytest
from typer.testing import CliRunner
from hyprial.daemon.impl.adapters.lark import Identity, LarkStateStore

from ..support import (
    FakeCollectionGateway,
    _cli_env,
)
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_cli_list_find_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = _cli_env(tmp_path, monkeypatch)
    runner = CliRunner()

    direct = LarkStateStore(state_dir / "adapters.sqlite3", adapter="lark:main")
    direct.upsert_identity(
        Identity(
            kind="user",
            platform_id="ou_lin",
            display_name="林懿伦",
            hyprial_owner="alicesmith",
            standing="verified",
            source="manual:coordination 2026-08-14",
        )
    )
    assert direct.identity("user", "ou_lin") is not None
    direct.close()

    listed = runner.invoke(
        _cli_root_app, ["adapter", "identities", "list", "main", "--json"]
    )
    assert listed.exit_code == 0, listed.output
    body = json.loads(listed.output)
    assert body["count"] == 1
    assert body["identities"][0]["platformId"] == "ou_lin"

    found = runner.invoke(
        _cli_root_app,
        ["adapter", "identities", "find", "main", "--name", "懿", "--json"],
    )
    assert found.exit_code == 0, found.output
    assert json.loads(found.output)["identities"][0]["platformId"] == "ou_lin"

    empty = runner.invoke(
        _cli_root_app,
        ["adapter", "identities", "find", "main", "--name", "无此人", "--json"],
    )
    assert json.loads(empty.output)["count"] == 0


def test_cli_rejects_invalid_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_env(tmp_path, monkeypatch)
    runner = CliRunner()

    bad_kind = runner.invoke(
        _cli_root_app,
        ["adapter", "identities", "list", "main", "--kind", "human", "--json"],
    )
    assert bad_kind.exit_code == 1
    assert json.loads(bad_kind.output)["code"] == "INVALID_ARGUMENT"

    no_filter = runner.invoke(
        _cli_root_app, ["adapter", "identities", "find", "main", "--json"]
    )
    assert no_filter.exit_code == 1
    assert json.loads(no_filter.output)["code"] == "INVALID_ARGUMENT"


def test_cli_sync_runs_the_collector_against_the_adapter_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = _cli_env(tmp_path, monkeypatch)
    from hyprial.daemon.impl.adapters.lark.credentials import identities as identities_module

    monkeypatch.setattr(
        identities_module,
        "configured_identity_gateway",
        lambda home, state, adapter: ("cli_app", FakeCollectionGateway()),
    )
    runner = CliRunner()

    result = runner.invoke(
        _cli_root_app, ["adapter", "identities", "sync", "main", "--json"]
    )

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["adapter"] == "main"
    assert report["ok"] is True
    assert report["identitiesRecorded"] == 12
    store = LarkStateStore(state_dir / "adapters.sqlite3", adapter="lark:main")
    assert len(store.identities(kind="user")) == 10
    store.close()


def test_cli_sync_unconfigured_adapter_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_env(tmp_path, monkeypatch)
    runner = CliRunner()

    result = runner.invoke(
        _cli_root_app, ["adapter", "identities", "sync", "ghost", "--json"]
    )

    assert result.exit_code == 1
    assert json.loads(result.output)["code"] == "ADAPTER_NOT_FOUND"
