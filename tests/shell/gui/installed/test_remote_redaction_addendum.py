"""Remote redaction for cache conflicts and automatic-worktree startup data."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.shell.impl.gui import worktree as auto_worktree
from hyprial.shell.impl.gui.errors import GuiError
from hyprial.shell.impl.gui.install import dev_source

_CLEAN = "https://example.invalid/hyprial.git"


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("https://user:s3cr3t@example.invalid/hyprial.git", _CLEAN),
        ("https://s3cr3t@example.invalid/hyprial.git", _CLEAN),
        ("https://example.invalid/hyprial.git?token=s3cr3t", _CLEAN),
        ("https://example.invalid/hyprial.git#s3cr3t", _CLEAN),
        ("git:s3cr3t@example.invalid:hyprial.git", "example.invalid:hyprial.git"),
    ],
)
def test_cache_conflict_redacts_a_secret_configured_remote(
    tmp_path: Path, configured: str, expected: str
) -> None:
    cache = tmp_path / "source.git"
    cache.mkdir()

    def run(command, **kwargs):
        del kwargs
        if "config" in command:
            return SimpleNamespace(returncode=0, stdout=configured + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="true\n", stderr="")

    with pytest.raises(GuiError) as caught:
        dev_source._prepare_cache("git", cache, _CLEAN, {}, run)

    assert caught.value.code == "GUI_DEV_SOURCE_CONFLICT"
    assert caught.value.data["expectedRemote"] == _CLEAN
    assert caught.value.data["actualRemote"] == expected
    for rendered in (str(caught.value), repr(caught.value), json.dumps(caught.value.data)):
        assert "s3cr3t" not in rendered


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://user:s3cr3t@example.invalid/hyprial.git", _CLEAN),
        ("https://example.invalid/hyprial.git?token=s3cr3t", _CLEAN),
        ("https://example.invalid/hyprial.git#s3cr3t", _CLEAN),
        ("git:s3cr3t@example.invalid:hyprial.git", "example.invalid:hyprial.git"),
        ("ssh://git@example.invalid/hyprial.git", "ssh://git@example.invalid/hyprial.git"),
        ("git@example.invalid:hyprial.git", "git@example.invalid:hyprial.git"),
    ],
)
def test_auto_worktree_payload_redacts_its_own_remote(
    tmp_path: Path, remote: str, expected: str
) -> None:
    auto = auto_worktree.AutoGuiWorktree(
        source=tmp_path / "source",
        path=tmp_path / "auto",
        branch="(detached)",
        base="a" * 40,
        home=tmp_path / "gui",
        created=True,
        install=auto_worktree.DependencyInstall(
            command=(), cwd=tmp_path / "gui", ran=False, reason="test"
        ),
        managed=True,
        remote=remote,
    )

    payload = auto_worktree.auto_worktree_payload(auto)

    assert payload is not None
    assert payload["remote"] == expected
    assert "s3cr3t" not in json.dumps(payload)
