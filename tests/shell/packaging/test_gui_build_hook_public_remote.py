"""Release wheels record the public repository as the GUI source remote.

``HYPRIAL_BUILD_SOURCE_REMOTE`` is the one explicit build input that replaces
a checkout's ``remote.origin.url`` in ``hyprial/_gui/release.json``.  It is
fail-closed: only the published public remote is accepted, an empty value or
any other value is a build error, and it is refused on a snapshot tree whose
stamp already decides.  Unset keeps the checkout remote (so an internal
developer's wheel still drives ``hyprial gui dev``) and prints a notice that
the wheel is for local use only.  The fixture origin uses a reserved
``.invalid`` host.
"""

from __future__ import annotations

import json
from pathlib import Path
import runpy

import pytest

from .test_gui_build_hook_snapshot_stamp import (
    PUBLISH_REMOTE,
    _snapshot_tree,
    _write_stamp,
)
from .test_gui_build_hook_source_guard import (
    REPO,
    _fake_npm,
    _git,
    _repo,
    _stage,
    _staged_files,
    module,
)

ENV = "HYPRIAL_BUILD_SOURCE_REMOTE"
INTERNAL_REMOTE = "ssh://git@git.internal.invalid/org/repo.git"
RESOURCES = REPO / "src/hyprial/shell/impl/gui/install/resources.py"
PUBLISH_SCRIPT = REPO / "scripts/ci/github_publish.py"


def _internal_repo(tmp_path: Path) -> Path:
    root = _repo(tmp_path)
    _git(root, "remote", "set-url", "origin", INTERNAL_REMOTE)
    return root


def _manifest(build_data: dict) -> dict:
    staged = _staged_files(build_data)
    return json.loads(
        staged["hyprial/_gui/release.json"].read_text(encoding="utf-8")
    )


def test_the_public_remote_is_one_definition_shared_with_the_snapshot_stamp() -> None:
    resources = runpy.run_path(str(RESOURCES))
    assert resources["PUBLIC_SOURCE_REMOTE"] == PUBLISH_REMOTE
    assert module.PUBLIC_SOURCE_REMOTE == PUBLISH_REMOTE
    assert ENV == module.SOURCE_REMOTE_ENV
    source = PUBLISH_SCRIPT.read_text(encoding="utf-8")
    assert f'DEFAULT_TARGET_REMOTE = "{resources["PUBLIC_SOURCE_REMOTE"]}"' in source


def test_unset_records_the_checkout_remote_and_prints_the_local_use_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.delenv(ENV, raising=False)
    root = _internal_repo(tmp_path)
    _fake_npm(tmp_path, monkeypatch)

    _hook, build_data = _stage(root)

    manifest = _manifest(build_data)
    assert manifest["sourceRemote"] == INTERNAL_REMOTE
    assert manifest["sourceCommit"] == _git(root, "rev-parse", "HEAD")
    err = capsys.readouterr().err
    assert "notice:" in err
    assert "not the public repository" in err
    assert "must not be distributed" in err
    assert ENV in err


def test_public_remote_replaces_the_checkout_remote_and_keeps_the_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv(ENV, PUBLISH_REMOTE)
    root = _internal_repo(tmp_path)
    _fake_npm(tmp_path, monkeypatch)

    _hook, build_data = _stage(root)

    manifest = _manifest(build_data)
    assert manifest["sourceRemote"] == PUBLISH_REMOTE
    assert manifest["sourceCommit"] == _git(root, "rev-parse", "HEAD")
    assert "must not be distributed" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "https://example.test/org/repo.git",
        INTERNAL_REMOTE,
        PUBLISH_REMOTE + "\n",
        " " + PUBLISH_REMOTE,
        "https://github.com/Hyprial/hyprial",
        "https://user:secret@github.com/Hyprial/hyprial.git",
        "https://github.com/Hyprial/hyprial.git?token=x",
    ],
)
def test_any_other_value_is_a_build_error_before_npm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(ENV, value)
    root = _internal_repo(tmp_path)
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match=ENV) as raised:
        _stage(root)

    assert "secret" not in str(raised.value)
    assert not log.exists()


@pytest.mark.parametrize("value", [PUBLISH_REMOTE, "", "https://example.test/x.git"])
def test_set_on_a_snapshot_tree_is_a_build_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(ENV, value)
    root = _snapshot_tree(tmp_path)
    _write_stamp(root)
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match="snapshot"):
        _stage(root)

    assert not log.exists()


def test_snapshot_without_the_variable_still_uses_the_stamp_and_prints_no_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.delenv(ENV, raising=False)
    root = _snapshot_tree(tmp_path)
    _write_stamp(root)
    _fake_npm(tmp_path, monkeypatch)

    _hook, build_data = _stage(root)

    assert _manifest(build_data)["sourceRemote"] == PUBLISH_REMOTE
    assert "must not be distributed" not in capsys.readouterr().err
