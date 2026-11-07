"""updates: track installation scenarios."""

from __future__ import annotations
import json
import subprocess
import sys
import time
from pathlib import Path
import pytest
from typer.testing import CliRunner
import hyprial.daemon.impl.updates as updates

from .support import (
    RemoteFixture,
    _ProbeClock,
    _UpgradeHarness,
    _git,
    _installation,
    _isolate_home,
    _write_track,
    remote as remote,
)
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_internal_track_installs_the_movable_tag_pinned_to_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
) -> None:
    """An explicit internal track resolves its movable tag and pins the commit."""

    harness = _UpgradeHarness(monkeypatch, tmp_path, remote)
    _write_track(tmp_path / "home", "internal")

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["track"] == "internal"
    assert payload["resolvedTag"] == "internal"
    assert payload["resolvedCommit"] == remote.dev_commit
    assert "warning" not in payload
    # Pinned to the exact commit, never the movable tag name.
    assert harness.calls == [
        ["uv", "tool", "install", "--force", "--compile-bytecode", "--python", sys.executable, f"git+{harness.source_url}@{remote.dev_commit}"]
    ]


def test_upgrade_uses_the_configured_ls_remote_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
) -> None:
    harness = _UpgradeHarness(monkeypatch, tmp_path, remote)
    (tmp_path / "home" / "settings.json").write_text(
        '{"lsRemoteTimeoutSeconds":60}\n', encoding="utf-8"
    )

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 0, result.output
    assert harness.resolve_timeouts == [60.0]


def test_upgrade_check_failure_reports_each_timeout_without_the_remote_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setattr(
        updates,
        "read_installation",
        lambda: _installation(
            commit="1" * 40,
            url="ssh://user:secret@example.test/private.git",
        ),
    )
    clock = _ProbeClock()
    monkeypatch.setattr(time, "monotonic", clock)
    elapsed = iter((45.2, 45.8))

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        clock.advance(next(elapsed))
        raise subprocess.TimeoutExpired(argv, float(kwargs["timeout"]))

    monkeypatch.setattr(updates.installation.subprocess, "run", run)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["code"] == "UPGRADE_CHECK_FAILED"
    assert payload["error"] == (
        "git ls-remote --tags timed out: attempt 1 45.2s/45s, "
        "attempt 2 45.8s/45s; maximum 90s"
    )
    assert "example.test" not in payload["error"]
    assert "secret" not in payload["error"]


def test_retired_track_name_still_resolves_and_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
) -> None:
    """`updateTrack: dev` keeps working, resolves `internal`, and warns.

    The migration case this protects: a machine configured before the
    2026-09-18 rename upgrades to a client that knows only the new name. It
    asked for exactly the track it still wants; refusing it would break the one
    group that did nothing wrong, right after an upgrade, unattended.
    ⚠️ The alias resolves to the CURRENT tag -- the retired tag can be deleted
    as soon as every node runs a client carrying this table.
    """

    harness = _UpgradeHarness(monkeypatch, tmp_path, remote)
    _write_track(tmp_path / "home", "dev")

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 0, result.output
    # ⚠️ CliRunner merges stderr into output, while the real CLI keeps them
    # apart -- the deprecation notice goes to stderr precisely so that
    # `--json` stdout stays exactly one JSON value. Parse the JSON line.
    json_line = next(
        line for line in result.output.splitlines() if line.startswith("{")
    )
    payload = json.loads(json_line)
    # It followed the new tag, not a resurrected old one.
    assert payload["track"] == "internal"
    assert payload["resolvedTag"] == "internal"
    assert payload["resolvedCommit"] == remote.dev_commit
    # And it said so: silent acceptance would leave settings.json stale forever.
    assert "was renamed to 'internal'" in result.output
    # Pinned to the resolved commit, exactly as the current name would be --
    # the alias must not become a second, slightly different code path.
    assert harness.calls == [
        [
            "uv",
            "tool",
            "install",
            "--force",
            "--compile-bytecode",
            "--python",
            sys.executable,
            f"git+{harness.source_url}@{remote.dev_commit}",
        ]
    ]


@pytest.mark.parametrize(
    ("track", "expected_commit"),
    (("nightly", "tagged_commit"), ("stable", "old_commit")),
)
def test_nightly_and_stable_tracks_resolve_their_own_movable_tag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    remote: RemoteFixture,
    track: str,
    expected_commit: str,
) -> None:
    harness = _UpgradeHarness(monkeypatch, tmp_path, remote)
    harness.installation = _installation(
        commit=remote.dev_commit, requested=remote.dev_commit, url=harness.source_url
    )
    _write_track(tmp_path / "home", track)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    commit = getattr(remote, expected_commit)
    assert payload["track"] == track
    assert payload["resolvedTag"] == track
    assert payload["resolvedCommit"] == commit
    assert harness.calls == [
        ["uv", "tool", "install", "--force", "--compile-bytecode", "--python", sys.executable, f"git+{harness.source_url}@{commit}"]
    ]


def test_missing_track_tag_fails_loud_without_falling_back_to_version_tags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
) -> None:
    """The anti-fallback contract: a missing track tag is NEVER the latest v*."""

    _git("tag", "-d", "nightly", cwd=remote.bare)
    harness = _UpgradeHarness(monkeypatch, tmp_path, remote)
    _write_track(tmp_path / "home", "nightly")

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["code"] == "UPGRADE_CHECK_FAILED"
    assert "'nightly'" in payload["error"]
    assert "v0.5.0-rc.1" not in result.output
    assert harness.calls == []


def test_invalid_track_value_refuses_upgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
) -> None:
    harness = _UpgradeHarness(monkeypatch, tmp_path, remote)
    _write_track(tmp_path / "home", "canary")

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["code"] == "UPGRADE_CHECK_FAILED"
    assert "canary" in payload["error"]
    assert harness.calls == []


def test_malformed_settings_json_refuses_upgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
) -> None:
    harness = _UpgradeHarness(monkeypatch, tmp_path, remote)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text("[not json", encoding="utf-8")

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["code"] == "UPGRADE_CHECK_FAILED"
    assert harness.calls == []


def test_explicit_tag_overrides_configured_track(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
) -> None:
    harness = _UpgradeHarness(monkeypatch, tmp_path, remote)
    _write_track(tmp_path / "home", "stable")

    result = CliRunner().invoke(
        _cli_root_app, ["daemon", "upgrade", "--tag", "emergency", "--force", "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "track" not in payload
    assert payload["resolvedTag"] == "emergency"
    assert harness.calls[-1][-1] == f"git+{harness.source_url}@emergency"


def test_track_noop_when_already_at_the_track_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
) -> None:
    harness = _UpgradeHarness(monkeypatch, tmp_path, remote)
    harness.installation = _installation(
        commit=remote.dev_commit, requested=remote.dev_commit, url=harness.source_url
    )
    _write_track(tmp_path / "home", "internal")

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["upgraded"] is False
    assert payload["track"] == "internal"
    assert harness.calls == []


def test_track_reinstall_with_unchanged_commit_skips_daemon_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
) -> None:
    """requested_revision == pinned commit => no restart, even on reinstall.

    This pins the anti-degradation contract: had the track requirement kept
    the movable tag name, uv would record the tag as requested_revision and
    every timer tick would see a changed ref and restart the daemon.
    """

    harness = _UpgradeHarness(monkeypatch, tmp_path, remote)
    # A realistic receipt: a pinned commit installs with that commit_id.
    harness.installation = _installation(
        commit=remote.dev_commit, requested=remote.dev_commit, url=harness.source_url
    )
    _write_track(tmp_path / "home", "internal")

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--force", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["upgraded"] is True
    assert payload["restart"] == {
        "attempted": False,
        "restarted": False,
        "supervision": "unsupervised",
        "reason": "resolved ref unchanged; restart not needed",
    }
    assert harness.calls == [
        ["uv", "tool", "install", "--force", "--compile-bytecode", "--python", sys.executable, f"git+{harness.source_url}@{remote.dev_commit}"]
    ]


def test_legacy_path_keeps_byte_identical_reason_strings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
) -> None:
    """Transition contract: no settings.json => the pre-track behavior, byte
    for byte, including the restart-skip reason wording."""

    harness = _UpgradeHarness(monkeypatch, tmp_path, remote)
    # A realistic receipt: installed from the tag, so commit_id is the tag's.
    harness.installation = _installation(
        commit=remote.tagged_commit, requested="v0.5.0-rc.1", url=harness.source_url
    )

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--force", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "track" not in payload
    assert payload["restart"]["reason"] == (
        "resolved tag unchanged; restart not needed"
    )
    assert harness.calls == [
        ["uv", "tool", "install", "--force", "--compile-bytecode", "--python", sys.executable, f"git+{harness.source_url}@v0.5.0-rc.1"]
    ]
