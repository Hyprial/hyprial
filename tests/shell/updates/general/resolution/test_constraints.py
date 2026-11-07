"""updates: constraints scenarios."""

from __future__ import annotations
import json
import subprocess
from pathlib import Path
import pytest
from typer.testing import CliRunner
import hyprial.daemon.impl.updates as updates

from .support import (
    RemoteFixture,
    _NEW_VERSION,
    _OFFICIAL_GIT_URL,
    _OLD_VERSION,
    _UpgradeHarness,
    _installation,
    _isolate_home,
    remote as remote,
)
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.commands.meta.commands as _cli_owner_1
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_export_locked_constraints_uses_exact_fetched_commit(
    tmp_path: Path,
) -> None:
    commit = "d" * 40
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        cwd = Path(str(kwargs.get("cwd")))
        if argv[:3] == ["git", "checkout", "--quiet"]:
            (cwd / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        if argv[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(argv, 0, f"{commit}\n", "")
        if argv[:2] == ["uv", "export"]:
            destination = Path(argv[argv.index("--output-file") + 1])
            destination.write_text("anyio==4.15.1\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    destination = tmp_path / "constraints.txt"
    result = updates.export_locked_constraints(
        _OFFICIAL_GIT_URL, commit, destination, runner=run
    )

    assert result == destination
    assert destination.read_text(encoding="utf-8") == "anyio==4.15.1\n"
    assert ["git", "fetch", "--quiet", "--depth", "1", "origin", commit] in calls
    export = next(argv for argv in calls if argv[:2] == ["uv", "export"])
    assert export[2:7] == [
        "--frozen",
        "--no-hashes",
        "--no-emit-project",
        "--no-dev",
        "--project",
    ]


def test_export_locked_constraints_refuses_missing_lock(tmp_path: Path) -> None:
    commit = "e" * 40
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        stdout = f"{commit}\n" if argv[:3] == ["git", "rev-parse", "HEAD"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    with pytest.raises(updates.UpdateProbeError, match="does not contain uv.lock"):
        updates.export_locked_constraints(
            _OFFICIAL_GIT_URL,
            commit,
            tmp_path / "constraints.txt",
            runner=run,
        )

    assert all(argv[:2] != ["uv", "export"] for argv in calls)


def test_upgrade_available_compares_commits_then_versions(
    remote: RemoteFixture,
) -> None:
    resolution = updates.RemoteResolution(
        tag="v0.5.0-rc.1", commit=remote.tagged_commit, version=_NEW_VERSION
    )
    assert updates.upgrade_available(
        _installation(commit=remote.old_commit), resolution
    ) is True
    assert updates.upgrade_available(
        _installation(commit=remote.tagged_commit), resolution
    ) is False
    assert updates.upgrade_available(
        _installation(commit=None, version=_OLD_VERSION), resolution
    ) is True
    assert updates.upgrade_available(
        _installation(commit=None, version=_NEW_VERSION), resolution
    ) is True
    explicit = updates.RemoteResolution(tag="emergency", commit="e" * 40)
    assert updates.upgrade_available(_installation(commit=None), explicit) is True


def test_unknown_installed_commit_reinstalls_the_exact_resolved_tag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
) -> None:
    harness = _UpgradeHarness(monkeypatch, tmp_path, remote)
    harness.installation = _installation(
        commit=None, version=_NEW_VERSION, url=harness.source_url
    )

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["upgraded"] is True
    assert payload["resolvedTag"] == "v0.5.0-rc.1"
    assert payload["resolvedCommit"] == remote.tagged_commit
    assert harness.calls[-1][-1] == f"git+{harness.source_url}@v0.5.0-rc.1"


# --- hyprial version ------------------------------------------------------------ #


def test_version_includes_retired_track_warning_without_obeying_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
) -> None:
    _isolate_home(monkeypatch, tmp_path)
    home = tmp_path / "home"
    # The residue that still warns lives in settings.json (snake_case
    # spelling). This used to seed config.json, which is no longer read at
    # all -- seeding it here would have made the test pass on a warning it
    # was not producing.
    (home / "settings.json").write_text('{"update_track":"dev"}\n', encoding="utf-8")
    monkeypatch.setattr(
        updates,
        "read_installation",
        lambda: _installation(
            commit=remote.old_commit, url=_UpgradeHarness.source_url
        ),
    )
    result = _cli_owner_1._version_result()
    assert "retired" in result["warning"]
