"""updates: remote tags scenarios."""

from __future__ import annotations
import json
import logging
import subprocess
import time
from itertools import pairwise
from pathlib import Path
import pytest
from typer.testing import CliRunner
import hyprial.daemon.impl.updates as updates

from .support import (
    RemoteFixture,
    _ProbeClock,
    _git,
    _isolate_home,
    remote as remote,
)
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


# --- PEP 440 tag resolution ------------------------------------------------ #


@pytest.mark.parametrize(
    ("tags", "expected"),
    (
        (("v1.0.0.dev1", "v1.0.0.dev2"), "v1.0.0.dev2"),
        (("v1.0.0.dev99", "v1.0.0a1"), "v1.0.0a1"),
        (("v1.0.0a9", "v1.0.0b1"), "v1.0.0b1"),
        (("v1.0.0b9", "v1.0.0rc1"), "v1.0.0rc1"),
        (("v1.0.0.dev99", "v1.0.0rc1"), "v1.0.0rc1"),
        (("v1.0.0rc1", "v1.0.0rc2"), "v1.0.0rc2"),
        (("v1.0.0rc9", "v1.0.0"), "v1.0.0"),
        (("v1.0.0", "v1.0.0+team.2"), "v1.0.0+team.2"),
        (("v1.0.0+team.2", "v1.0.0.post1"), "v1.0.0.post1"),
        (("v1.0.0", "v1.1.0.dev1"), "v1.1.0.dev1"),
        (("v2.0.0", "v10.0.0.dev1"), "v10.0.0.dev1"),
        (("not-a-version", "1.2.3", "v1.2", "v1.2.3"), "v1.2.3"),
    ),
)
def test_latest_tag_uses_pep440_order_including_prereleases(
    tags: tuple[str, ...], expected: str
) -> None:
    candidates = {tag: f"{index:040x}" for index, tag in enumerate(tags, 1)}
    assert updates.select_latest_version_tag(candidates)[0] == expected


def test_pep440_representative_order_is_complete_across_release_phases() -> None:
    ordered = (
        "v1.0.0.dev1",
        "v1.0.0a1.dev1",
        "v1.0.0a1",
        "v1.0.0b1.dev1",
        "v1.0.0b1",
        "v1.0.0rc1.dev1",
        "v1.0.0rc1",
        "v1.0.0",
        "v1.0.0+team.1",
        "v1.0.0.post1.dev1",
        "v1.0.0.post1",
        "v1.0.1.dev1",
    )

    for lower, higher in pairwise(ordered):
        candidates = {lower: "1" * 40, higher: "2" * 40}
        assert updates.select_latest_version_tag(candidates)[0] == higher


def test_default_candidates_require_one_v_no_epoch_and_at_least_three_release_parts() -> None:
    candidates = {
        "V9.0.0": "1" * 40,
        "vv8.0.0": "2" * 40,
        "v1!7.0.0": "3" * 40,
        "v6.0": "4" * 40,
        "v4.0.0": "6" * 40,
    }

    assert updates.select_latest_version_tag(candidates)[0] == "v4.0.0"


@pytest.mark.parametrize(
    ("tags", "expected"),
    (
        # main-release publishes hotfix tags with extra release parts; a
        # resolver that only sees X.Y.Z picks v0.5.5, which the downgrade
        # guard then refuses against an installed 0.5.5.1.1 -- a silent no-op.
        (("v0.5.4", "v0.5.5", "v0.5.5.1", "v0.5.5.2"), "v0.5.5.2"),
        # ...and the hotfix of a hotfix is five parts (0.5.5.1.1, 0.5.5.2.1).
        (("v0.5.5", "v0.5.5.1.1", "v0.5.5.2", "v0.5.5.2.1"), "v0.5.5.2.1"),
        (("v0.5.5.2.1", "v0.5.6"), "v0.5.6"),
    ),
)
def test_hotfix_tags_with_extra_release_parts_are_upgrade_candidates(
    tags: tuple[str, ...], expected: str
) -> None:
    candidates = {tag: f"{index:040x}" for index, tag in enumerate(tags, 1)}

    assert updates.select_latest_version_tag(candidates)[0] == expected


def test_pep440_equivalent_spellings_use_raw_tag_tie_break() -> None:
    candidates = {"v1.0.0-rc.1": "1" * 40, "v1.0.0rc1": "2" * 40}
    assert updates.select_latest_version_tag(candidates)[:2] == (
        "v1.0.0rc1",
        "2" * 40,
    )


def test_resolve_remote_uses_highest_tag_and_peeled_annotated_commit(
    remote: RemoteFixture,
) -> None:
    latest = updates.resolve_remote(remote.url)
    assert latest.tag == "v0.5.0-rc.1"
    assert latest.commit == remote.tagged_commit
    assert latest.version == "0.5.0rc1"
    annotated = updates.resolve_remote(remote.url, tag="v0.4.0")
    assert annotated.commit == remote.tagged_commit


def test_explicit_tag_accepts_non_version_escape_hatch(remote: RemoteFixture) -> None:
    resolution = updates.resolve_remote(remote.url, tag="emergency")
    assert resolution.tag == "emergency"
    assert resolution.commit == remote.old_commit
    assert resolution.version is None


def test_resolver_uses_one_all_tags_probe() -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, "a" * 40 + "\trefs/tags/v1.2.3\n", ""
        )

    resolved = updates.resolve_remote("ssh://git@example.test/hyprial.git", runner=run)
    assert resolved.tag == "v1.2.3"
    assert calls == [
        ["git", "ls-remote", "--tags", "ssh://git@example.test/hyprial.git"]
    ]


def test_resolver_retries_one_timeout_then_succeeds_and_logs_each_attempt(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = _ProbeClock()
    monkeypatch.setattr(time, "monotonic", clock)
    calls: list[float] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        timeout = float(kwargs["timeout"])
        calls.append(timeout)
        if len(calls) == 1:
            clock.advance(45.1)
            raise subprocess.TimeoutExpired(argv, timeout)
        clock.advance(0.2)
        return subprocess.CompletedProcess(
            argv, 0, "a" * 40 + "\trefs/tags/v1.2.3\n", ""
        )

    with caplog.at_level(logging.INFO, logger="hyprial.daemon.impl.updates.installation"):
        resolved = updates.resolve_remote(
            "ssh://git@example.test/hyprial.git", runner=run
        )

    assert resolved.tag == "v1.2.3"
    assert calls == [45.0, 45.0]
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "git ls-remote --tags attempt 1/2 timed out in 45.1s (budget 45s)",
        "git ls-remote --tags attempt 2/2 succeeded in 0.2s (budget 45s)",
    ]


def test_resolver_reports_both_timeout_elapsed_values_and_total_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _ProbeClock()
    monkeypatch.setattr(time, "monotonic", clock)
    elapsed = iter((45.2, 45.8))

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        timeout = float(kwargs["timeout"])
        clock.advance(next(elapsed))
        raise subprocess.TimeoutExpired(argv, timeout)

    with pytest.raises(updates.UpdateProbeError) as raised:
        updates.resolve_remote("ssh://git@example.test/hyprial.git", runner=run)

    assert str(raised.value) == (
        "git ls-remote --tags timed out: attempt 1 45.2s/45s, "
        "attempt 2 45.8s/45s; maximum 90s"
    )


def test_resolver_does_not_retry_non_timeout_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _ProbeClock()
    monkeypatch.setattr(time, "monotonic", clock)
    calls = 0

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        clock.advance(0.1)
        return subprocess.CompletedProcess(argv, 128, "", "permission denied")

    with pytest.raises(updates.UpdateProbeError, match="permission denied"):
        updates.resolve_remote("ssh://git@example.test/hyprial.git", runner=run)

    assert calls == 1


def test_ls_remote_timeout_settings_default_and_override(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert updates.read_ls_remote_timeout(home) == 45.0
    home.mkdir()
    (home / "settings.json").write_text(
        '{"version":1,"lsRemoteTimeoutSeconds":60}\n', encoding="utf-8"
    )
    assert updates.read_ls_remote_timeout(home) == 60.0


def test_ls_remote_timeout_config_cli_preserves_other_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_home(monkeypatch, tmp_path)
    settings = tmp_path / "home" / "settings.json"
    settings.write_text('{"owner":"someone","keep":true}\n', encoding="utf-8")

    result = CliRunner().invoke(
        _cli_root_app,
        ["config", "set", "lsRemoteTimeoutSeconds", "60", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["key"] == "lsRemoteTimeoutSeconds"
    assert payload["value"] == 60.0
    assert json.loads(settings.read_text(encoding="utf-8")) == {
        "owner": "someone",
        "keep": True,
        "lsRemoteTimeoutSeconds": 60.0,
    }


@pytest.mark.parametrize("value", (0, -1, True, "45", float("inf")))
def test_ls_remote_timeout_settings_reject_invalid_values(
    tmp_path: Path, value: object
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps({"lsRemoteTimeoutSeconds": value}), encoding="utf-8"
    )
    with pytest.raises(updates.UpdateProbeError, match="positive finite number"):
        updates.read_ls_remote_timeout(home)


def test_missing_or_unparseable_tags_fail_loud(tmp_path: Path) -> None:
    bare = tmp_path / "empty.git"
    _git("init", "--bare", "--initial-branch=dev", bare, cwd=tmp_path)
    with pytest.raises(updates.UpdateProbeError, match="no parseable"):
        updates.resolve_remote(bare.as_uri())
    with pytest.raises(updates.UpdateProbeError, match="does not exist"):
        updates.resolve_remote(bare.as_uri(), tag="v1.0.0")


def test_probe_errors_on_unreachable_remote() -> None:
    with pytest.raises(updates.UpdateProbeError):
        updates.resolve_remote("file:///nonexistent/hyprial-remote.git")
