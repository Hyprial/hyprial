"""`hyprial version` reports local identity only and never probes the registry."""

from __future__ import annotations

import tomllib
from importlib.metadata import version as distribution_version
from pathlib import Path

import pytest

from hyprial import __version__

import hyprial.daemon.impl.updates as updates
from hyprial.daemon.impl.updates import installation as installation_module
import hyprial.shell.impl.cli.commands.meta.commands as _cli_owner_1


def test_declared_distribution_and_runtime_versions_match() -> None:
    """Every place that states the version must state the same one.

    The declared version moved out of ``pyproject.toml`` into ``VERSION``
    (pyproject now marks it dynamic), so this reads the file the build reads.
    The check matters more after that move, not less: the number lives in one
    place and is copied into others, and a copy that drifts is exactly what
    "0.4.6 names 358 trees" looked like from the outside.
    """

    root = Path(__file__).resolve().parents[4]
    declared = (root / "VERSION").read_text(encoding="utf-8").strip()

    assert distribution_version("hyprial") == declared
    assert __version__ == declared


def test_the_lockfile_does_not_restate_the_version() -> None:
    """⭐ uv records no version for a dynamic-version package -- keep it that way.

    ``uv lock`` replaced this project's ``version = "0.4.6"`` line with
    ``(dynamic)`` and dropped the key entirely. That is the point of the move,
    not an accident of it: a lockfile that restated the number would be a
    second place to change, and the whole defect being fixed is that one
    number lived in a place nobody had to update.

    So this asserts the ABSENCE, which needs its own positive control --
    otherwise a typo in the package name would produce the same pass. The
    control is that the entry is found at all, and that other packages in the
    same lockfile do carry versions, which proves the key is one this reader
    can see.
    """

    root = Path(__file__).resolve().parents[4]
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))

    ours = [p for p in lock["package"] if p["name"] == "hyprial"]
    assert len(ours) == 1, f"expected exactly one hyprial entry, got {len(ours)}"
    assert "version" not in ours[0], (
        "uv.lock restates the version; with a dynamic version it must not, or "
        "VERSION stops being the only place the number lives"
    )

    # Positive control: this reader CAN see a `version` key when one is there.
    others_with_version = [p for p in lock["package"] if "version" in p]
    assert others_with_version, (
        "no package in uv.lock has a version key at all -- the assertion above "
        "would pass even if the key were simply unreadable"
    )


def test_version_reports_pep440_package_version_and_semver_local_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        updates,
        "read_installation",
        lambda: updates.Installation(
            version=__version__, commit=None, requested_revision=None, url=None
        ),
    )

    result = _cli_owner_1._version_result()

    assert result["packageVersion"] == __version__
    assert result["localVersion"] == _cli_owner_1._semantic_version(__version__)
    assert result["ok"] is True
    assert result["registry"] == "https://github.com/Hyprial/hyprial.git"
    assert result["persistentUpdateSource"] == result["registry"]
    assert result["installOrigin"] is None
    assert result["dependencyLock"]["status"] == "unknown"
    # Remote-probe artifacts were removed; those belong to hyprial daemon upgrade.
    assert "latestVersion" not in result
    assert "upgradeAvailable" not in result
    assert "resolvedTag" not in result
    assert "resolvedCommit" not in result
    assert "registryCheck" not in result


def test_version_fallback_uses_package_version_constant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        updates,
        "read_installation",
        lambda: updates.Installation(
            version=None, commit=None, requested_revision=None, url=None
        ),
    )

    result = _cli_owner_1._version_result()

    assert result["packageVersion"] == __version__
    assert result["localVersion"] == _cli_owner_1._semantic_version(__version__)


def test_version_reports_dependency_lock_mismatches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    constraints = tmp_path / "constraints.txt"
    constraints.write_text(
        "anyio==4.15.1\nmissing-package==1.2.3\n",
        encoding="utf-8",
    )
    installation = updates.Installation(
        version=__version__,
        commit="c" * 40,
        requested_revision="v1.0.0",
        url="ssh://git@example.test/early.git",
    )
    updates.write_lock_receipt(
        home,
        source=updates.OFFICIAL_GIT_URL,
        commit=installation.commit,
        constraints=constraints,
    )
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(updates, "read_installation", lambda: installation)

    def installed_version(name: str) -> str:
        if name == "anyio":
            return "4.14.2"
        raise installation_module.PackageNotFoundError(name)

    monkeypatch.setattr(installation_module, "package_version", installed_version)

    result = _cli_owner_1._version_result()

    assert result["installOrigin"] == "ssh://git@example.test/early.git"
    assert result["dependencyLock"] == {
        "status": "mismatch",
        "mismatches": [
            {"name": "anyio", "expected": "4.15.1", "installed": "4.14.2"},
            {"name": "missing-package", "expected": "1.2.3", "installed": None},
        ],
    }


def test_version_never_probes_the_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mutation guard: resolve_remote must stay out of hyprial version.

    Even with an unreachable registry configured, version must not touch the
    network.  If someone re-adds remote probing here, this test goes red.
    """

    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    calls: list[tuple[object, ...]] = []

    def must_not_probe(*args: object, **kwargs: object) -> updates.RemoteResolution:
        calls.append((args, kwargs))
        raise AssertionError("hyprial version must not resolve the remote registry")

    monkeypatch.setattr(updates, "resolve_remote", must_not_probe)
    monkeypatch.setattr(
        updates,
        "read_installation",
        lambda: updates.Installation(
            version=__version__,
            commit=None,
            requested_revision=None,
            url="file:///nonexistent/hyprial.git",
        ),
    )

    result = _cli_owner_1._version_result()

    assert calls == []
    assert result["ok"] is True
    assert result["packageVersion"] == __version__
    assert result["registry"] == "https://github.com/Hyprial/hyprial.git"
    assert result["installOrigin"] == "file:///nonexistent/hyprial.git"
