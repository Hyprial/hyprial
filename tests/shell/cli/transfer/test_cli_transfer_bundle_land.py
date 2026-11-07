"""``hyprial transfer-bundle-land``: the CLI face of the daemon's transfer.land.

The daemon is injected (no live socket); what these tests pin is the mapping
the CLI owns -- which parameters cross the IPC boundary, and that a refused
landing keeps the daemon's code instead of being flattened.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.kernel.impl.contracts.daemon.ipc_errors import TRANSFER_CONFLICT
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    return home


def test_land_sends_the_bundle_and_reports_the_landing(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def local_request(method, params, **_kwargs):
        calls.append((method, params))
        return {
            "ok": True,
            "actor": "wk",
            "owner": "alice",
            "sourceMachine": "laptop",
            "destination": str(tmp_path / "agents" / "wk"),
            "files": 3,
            "bytes": 42,
            "grants": ["g1"],
            "identityRebound": True,
            "dryRun": False,
        }

    monkeypatch.setattr(_cli_support, "_daemon_request", local_request)
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "transfer", "bundle-land", str(bundle), "--expect-actor", "wk", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        ("transfer.land", {"bundleDir": str(bundle.resolve()), "actor": "wk"})
    ]
    assert json.loads(result.stdout)["actor"] == "wk"


def test_land_passes_only_the_options_that_were_given(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def local_request(method, params, **_kwargs):
        calls.append((method, params))
        return {"ok": True, "dryRun": True}

    monkeypatch.setattr(_cli_support, "_daemon_request", local_request)
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    result = CliRunner().invoke(
        _cli_root_app, ["agent", "transfer", "bundle-land", str(bundle), "--dry-run", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert calls == [("transfer.land", {"bundleDir": str(bundle), "dryRun": True})]


def test_a_refused_landing_keeps_the_daemons_code(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def local_request(method, params, **_kwargs):
        # What _daemon_request raises for a refused call: the daemon's own code
        # carried on a CliError (cli.py, transient registry, non-transient path).
        raise _cli_get_services().CliError(
            TRANSFER_CONFLICT, "an agent named 'wk' is already taken on this node"
        )

    monkeypatch.setattr(_cli_support, "_daemon_request", local_request)
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    result = CliRunner().invoke(_cli_root_app, ["agent", "transfer", "bundle-land", str(bundle), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "TRANSFER_CONFLICT"
    assert "already taken" in payload["error"]


def test_land_resolves_a_relative_bundle_path_before_it_crosses_ipc(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def local_request(method, params, **_kwargs):
        calls.append((method, params))
        return {"ok": True, "actor": "wk", "dryRun": True}

    monkeypatch.setattr(_cli_support, "_daemon_request", local_request)
    (tmp_path / "bundle").mkdir()
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(_cli_root_app, ["agent", "transfer", "bundle-land", "./bundle"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    method, params = calls[0]
    assert method == "transfer.land"
    # The daemon would otherwise resolve "./bundle" against its own cwd.
    assert params["bundleDir"] == str((tmp_path / "bundle").resolve())
    assert Path(params["bundleDir"]).is_absolute()
