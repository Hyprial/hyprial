"""updates: upgrade scenarios."""

from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path
import pytest
from typer.testing import CliRunner
import hyprial.daemon.impl.updates as updates

from .support import (
    RemoteFixture,
    _NEW_VERSION,
    _OFFICIAL_GIT_URL,
    _OLD_GIT_URL,
    _RENAMED_GIT_URL,
    _UpgradeHarness,
    _git,
    _installation,
    _record_restart_decision,
    remote as remote,
)
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import subprocess as _cli_subprocess
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_upgrade_explicit_tag_overrides_latest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
) -> None:
    harness = _UpgradeHarness(monkeypatch, tmp_path, remote)
    result = CliRunner().invoke(
        _cli_root_app, ["daemon", "upgrade", "--tag", "v0.3.0", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["resolvedTag"] == "v0.3.0"
    assert payload["resolvedCommit"] == remote.old_commit
    assert harness.calls == []  # already installed at that exact commit

    forced = CliRunner().invoke(
        _cli_root_app, ["daemon", "upgrade", "--tag", "emergency", "--force", "--json"]
    )
    assert forced.exit_code == 0, forced.output
    assert json.loads(forced.output)["resolvedTag"] == "emergency"
    assert harness.calls[-1][-1] == f"git+{harness.source_url}@emergency"


def test_upgrade_noop_and_force_always_report_resolved_tag_and_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
) -> None:
    harness = _UpgradeHarness(monkeypatch, tmp_path, remote)
    harness.installation = _installation(
        commit=remote.tagged_commit, version=_NEW_VERSION, url=harness.source_url
    )
    runner = CliRunner()
    result = runner.invoke(_cli_root_app, ["daemon", "upgrade", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["upgraded"] is False
    assert payload["reason"] == "already at the resolved tag's commit"
    assert payload["resolvedTag"] == "v0.5.0-rc.1"
    assert payload["resolvedCommit"] == remote.tagged_commit
    assert harness.calls == []

    forced = runner.invoke(_cli_root_app, ["daemon", "upgrade", "--force", "--json"])
    assert forced.exit_code == 0, forced.output
    forced_payload = json.loads(forced.output)
    assert forced_payload["upgraded"] is True
    assert forced_payload["resolvedTag"] == "v0.5.0-rc.1"
    assert forced_payload["resolvedCommit"] == remote.tagged_commit
    assert len(harness.calls) == 1


def test_upgrade_human_output_prints_resolved_tag_and_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
) -> None:
    harness = _UpgradeHarness(monkeypatch, tmp_path, remote)
    harness.installation = _installation(
        commit=remote.tagged_commit, version=_NEW_VERSION, url=harness.source_url
    )

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade"])

    assert result.exit_code == 0, result.output
    assert "resolvedTag" in result.output and "v0.5.0-rc.1" in result.output
    assert "resolvedCommit" in result.output
    assert remote.tagged_commit in result.output


def test_upgrade_retargets_pre_rename_installation_to_official_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
) -> None:
    harness = _UpgradeHarness(monkeypatch, tmp_path, remote)
    harness.installation = _installation(commit=remote.old_commit, url=_OLD_GIT_URL)
    observed: list[tuple[str, str | None, float]] = []

    def resolve(
        url: str,
        *,
        tag: str | None = None,
        timeout: float = updates.LS_REMOTE_TIMEOUT,
    ) -> updates.RemoteResolution:
        observed.append((url, tag, timeout))
        return updates.RemoteResolution(
            tag="v0.5.0-rc.1", commit=remote.tagged_commit, version=_NEW_VERSION
        )

    monkeypatch.setattr(updates, "resolve_remote", resolve)
    harness.fetched_commit = remote.tagged_commit  # uv fetches what was resolved
    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])
    assert result.exit_code == 0, result.output
    assert observed == [(_OFFICIAL_GIT_URL, None, 45.0)]
    assert harness.calls == [
        [
            "uv", "tool", "install", "--force", "--compile-bytecode",
            "--python", sys.executable,
            f"git+{_OFFICIAL_GIT_URL}@v0.5.0-rc.1",
        ]
    ]


def test_plain_upgrade_uses_official_source_not_forgejo_install_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
) -> None:
    harness = _UpgradeHarness(monkeypatch, tmp_path, remote)
    harness.installation = _installation(
        commit=remote.old_commit,
        requested="v0.4.0",
        url=_RENAMED_GIT_URL,
    )
    observed: list[tuple[str, str | None]] = []

    def resolve(
        url: str,
        *,
        tag: str | None = None,
        timeout: float = updates.LS_REMOTE_TIMEOUT,
    ) -> updates.RemoteResolution:
        del timeout
        observed.append((url, tag))
        return updates.RemoteResolution(
            tag="v0.5.0-rc.1",
            commit=remote.tagged_commit,
            version=_NEW_VERSION,
        )

    monkeypatch.setattr(updates, "resolve_remote", resolve)
    harness.fetched_commit = remote.tagged_commit

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 0, result.output
    assert observed == [(_OFFICIAL_GIT_URL, None)]
    assert harness.calls[-1][-1] == (
        f"git+{_OFFICIAL_GIT_URL}@v0.5.0-rc.1"
    )


def test_upgrade_install_uses_exported_constraints_for_resolved_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
) -> None:
    harness = _UpgradeHarness(monkeypatch, tmp_path, remote)
    exported: list[tuple[str, str]] = []

    def export_constraints(
        url: str,
        commit: str,
        destination: Path,
        *,
        runner: object = None,
    ) -> Path:
        exported.append((url, commit))
        destination.write_text("anyio==4.15.1\n", encoding="utf-8")
        return destination

    monkeypatch.setattr(
        updates,
        "export_locked_constraints",
        export_constraints,
        raising=False,
    )
    captured_constraints: list[str] = []
    real_run = _cli_subprocess.run

    def capture(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["uv", "tool", "install"]:
            if "--constraints" in argv:
                index = argv.index("--constraints")
                captured_constraints.append(
                    Path(argv[index + 1]).read_text(encoding="utf-8")
                )
        return harness._run(argv, **kwargs)

    monkeypatch.setattr(_cli_subprocess, "run", capture)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 0, result.output
    assert exported == [(_OFFICIAL_GIT_URL, remote.tagged_commit)]
    assert captured_constraints == ["anyio==4.15.1\n"]
    monkeypatch.setattr(_cli_subprocess, "run", real_run)


def test_missing_lock_fails_loudly_without_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
) -> None:
    harness = _UpgradeHarness(monkeypatch, tmp_path, remote)

    def missing_lock(*args: object, **kwargs: object) -> Path:
        raise updates.UpdateProbeError(
            "resolved source commit does not contain uv.lock; "
            "refusing an unpinned install"
        )

    monkeypatch.setattr(updates, "export_locked_constraints", missing_lock)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["code"] == "UPGRADE_CHECK_FAILED"
    assert "does not contain uv.lock" in payload["error"]
    assert harness.calls == []


def test_one_shot_forgejo_ref_does_not_change_next_plain_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
) -> None:
    harness = _UpgradeHarness(monkeypatch, tmp_path, remote)
    observed: list[tuple[str, str | None]] = []
    real_resolve = updates.resolve_remote

    def resolve(
        url: str,
        *,
        tag: str | None = None,
        timeout: float = updates.LS_REMOTE_TIMEOUT,
    ) -> updates.RemoteResolution:
        del timeout
        observed.append((url, tag))
        return real_resolve(remote.url, tag=tag)

    monkeypatch.setattr(updates, "resolve_remote", resolve)

    one_shot = CliRunner().invoke(
        _cli_root_app,
        [
            "daemon",
            "upgrade",
            "--source",
            "forgejo",
            "--ref",
            "emergency",
            "--force",
            "--json",
        ],
    )
    plain = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert one_shot.exit_code == 0, one_shot.output
    assert plain.exit_code == 0, plain.output
    assert observed == [
        (updates.FORGEJO_GIT_URL, "emergency"),
        (_OFFICIAL_GIT_URL, None),
    ]
    one_shot_payload = json.loads(one_shot.output)
    assert one_shot_payload["oneShotSource"] is True
    assert one_shot_payload["persistentUpdateSource"] == _OFFICIAL_GIT_URL
    assert harness.exported_constraints == [
        (updates.FORGEJO_GIT_URL, remote.old_commit),
        (_OFFICIAL_GIT_URL, remote.tagged_commit),
    ]


def test_plain_upgrade_ahead_of_official_reports_and_skips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
) -> None:
    harness = _UpgradeHarness(monkeypatch, tmp_path, remote)
    harness.installation = _installation(
        version="9.0.0",
        commit="9" * 40,
        requested="early-test",
        url=_RENAMED_GIT_URL,
    )

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["upgraded"] is False
    assert payload["declinedDowngrade"] is True
    assert "refusing to move backwards" in payload["reason"]
    assert payload["source"] == "official"
    assert harness.calls == []


def test_upgrade_fails_loud_when_no_parseable_tag_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
) -> None:
    harness = _UpgradeHarness(monkeypatch, tmp_path, remote)
    empty = tmp_path / "empty.git"
    _git("init", "--bare", "--initial-branch=dev", empty, cwd=tmp_path)
    # Resolution target (empty repo) stays separate from the recorded
    # install source (remote-shaped): guard 2 must not fire here.
    harness.resolve_url = empty.as_uri()
    harness.installation = _installation(
        commit=remote.old_commit, url=_UpgradeHarness.source_url
    )
    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["code"] == "UPGRADE_CHECK_FAILED"
    assert "no parseable" in payload["error"]
    assert harness.calls == []


def test_upgrade_reports_uv_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
) -> None:
    harness = _UpgradeHarness(monkeypatch, tmp_path, remote)

    def failing(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[0] != "uv":
            return harness._real_run(argv, **kwargs)  # type: ignore[arg-type]
        harness.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

    monkeypatch.setattr(_cli_subprocess, "run", failing)
    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["code"] == "UPGRADE_FAILED" and "boom" in payload["error"]
    # The resolved tag and commit are still asserted exactly -- the point of
    # this payload is that a failure names what it was trying to install.
    assert payload["data"]["resolvedTag"] == "v0.5.0-rc.1"
    assert payload["data"]["resolvedCommit"] == remote.tagged_commit
    # ⭐ And it now also carries the post-upgrade report. This is an addition,
    # not a relaxation: a failed upgrade reports too, because a channel that
    # only carries bad news is a channel nobody has seen work. Asserting the
    # keys are present keeps a future change from silently dropping them.
    assert set(payload["data"]) == {
        "resolvedTag",
        "resolvedCommit",
        "source",
        "persistentUpdateSource",
        "selfCheck",
        "ownerAlert",
    }
    assert payload["data"]["selfCheck"]["ok"] in (True, False)
    assert "attempted" in payload["data"]["ownerAlert"]
    assert "resolvedTag=v0.5.0-rc.1" in payload["error"]
    assert f"resolvedCommit={remote.tagged_commit}" in payload["error"]


def test_explicit_movable_tag_on_a_new_commit_takes_the_restart_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
) -> None:
    harness = _UpgradeHarness(monkeypatch, tmp_path, remote)
    harness.installation = _installation(
        commit=remote.old_commit, requested="internal", url=harness.source_url
    )
    entered = _record_restart_decision(monkeypatch)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--tag", "internal", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["upgraded"] is True
    assert payload["resolvedCommit"] == remote.dev_commit
    assert entered, "same tag name, new commit: the restart path must be taken"
    assert payload["restart"]["reason"] != "resolved tag unchanged; restart not needed"


def test_explicit_tag_reinstalled_at_the_same_commit_does_not_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
) -> None:
    """Negative control: same name AND same commit is still a no-restart."""

    harness = _UpgradeHarness(monkeypatch, tmp_path, remote)
    harness.installation = _installation(
        commit=remote.dev_commit, requested="internal", url=harness.source_url
    )
    entered = _record_restart_decision(monkeypatch)

    result = CliRunner().invoke(
        _cli_root_app, ["daemon", "upgrade", "--tag", "internal", "--force", "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["upgraded"] is True
    assert entered == []
    assert payload["restart"]["reason"] == "resolved tag unchanged; restart not needed"


def test_an_install_that_did_not_land_the_resolved_commit_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
) -> None:
    """uv fetched something else (the tag moved again, a cache): never report
    upgraded=true for a commit that is not the one resolved."""

    harness = _UpgradeHarness(monkeypatch, tmp_path, remote)
    harness.installation = _installation(
        commit=remote.old_commit, requested="internal", url=harness.source_url
    )
    harness.fetched_commit = remote.tagged_commit
    _record_restart_decision(monkeypatch)

    result = CliRunner().invoke(_cli_root_app, ["daemon", "upgrade", "--tag", "internal", "--json"])

    assert result.exit_code != 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload.get("upgraded") is not True
    assert payload["code"] == "UPGRADE_COMMIT_MISMATCH"
    assert remote.dev_commit in payload["error"]
    assert remote.tagged_commit in payload["error"]
    assert payload["data"]["installedCommit"] == remote.tagged_commit
