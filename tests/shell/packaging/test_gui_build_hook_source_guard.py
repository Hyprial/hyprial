"""The wheel GUI stage only ships an exact, clean, symlink-free source tree.

The hook rebuilds ``gui/dist`` from a fresh ``npm ci`` and records the committed
HEAD in ``release.json``.  These tests use a real temporary Git repository and
a recording npm stub so the source guards, generated-path allowance, and the
copy into the wheel are exercised together rather than mocked away.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import types
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[3]
HOOK_PATH = REPO / "src/hyprial/gui_build_hook.py"


def _hook_module():
    try:
        import hatchling.builders.hooks.plugin.interface  # noqa: F401
    except ModuleNotFoundError:
        names = [
            "hatchling",
            "hatchling.builders",
            "hatchling.builders.hooks",
            "hatchling.builders.hooks.plugin",
            "hatchling.builders.hooks.plugin.interface",
        ]
        for name in names:
            sys.modules.setdefault(name, types.ModuleType(name))
        for parent, child in zip(names, names[1:]):
            setattr(sys.modules[parent], child.split(".")[-1], sys.modules[child])
        sys.modules[names[-1]].BuildHookInterface = type("BuildHookInterface", (), {})
    spec = importlib.util.spec_from_file_location(
        "hyprial_gui_build_hook_source_guard", HOOK_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


module = _hook_module()


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repo(tmp_path: Path, *, lockfile: bool = True) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(
        root,
        "remote",
        "add",
        "origin",
        "ssh://git@example.invalid/HyprialOS/harness-bridge.git",
    )
    gui = root / "gui"
    (gui / "src").mkdir(parents=True)
    (gui / "package.json").write_text(
        json.dumps(
            {
                "name": "@hyprial/gui-app",
                "version": "1.2.3",
                "scripts": {"build": "vite build"},
            }
        ),
        encoding="utf-8",
    )
    if lockfile:
        (gui / "package-lock.json").write_text(
            '{"lockfileVersion": 3}\n', encoding="utf-8"
        )
    (gui / "src" / "main.js").write_text(
        "console.log('source');\n", encoding="utf-8"
    )
    (root / "VERSION").write_text("1.2.3\n", encoding="utf-8")
    (root / "NOTES.md").write_text("notes\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "fixture")
    return root


def _fake_npm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A recording npm that refuses to reuse stale node_modules."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "npm.log"
    script = bin_dir / "npm"
    script.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(log))}\n"
        'case "$*" in\n'
        '  "ci --ignore-scripts")\n'
        "    if [ -e node_modules/stale-marker ]; then\n"
        "      echo 'reused stale node_modules' >&2\n"
        "      exit 7\n"
        "    fi\n"
        "    mkdir -p node_modules\n"
        "    : > node_modules/fresh-marker\n"
        "    exit 0\n"
        "    ;;\n"
        '  "run build")\n'
        "    mkdir -p dist\n"
        '    printf "<!doctype html>\\n" > dist/index.html\n'
        '    if [ -n "$FAKE_NPM_SYMLINK_TARGET" ]; then\n'
        '      ln -sfn "$FAKE_NPM_SYMLINK_TARGET" dist/linked.js\n'
        "    fi\n"
        "    exit 0\n"
        "    ;;\n"
        '  *) echo "unexpected npm arguments: $*" >&2; exit 9 ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv(
        "PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    )
    return log


def _hook(root: Path):
    class _Hook(module.CustomBuildHook):
        def __init__(self) -> None:
            self.root = str(root)
            self.metadata = SimpleNamespace(version="9.9.9")
            self.target_name = "wheel"
            self._stages = []

    return _Hook()


def _stage(root: Path) -> tuple[object, dict]:
    hook = _hook(root)
    build_data: dict = {"force_include": {}}
    hook._stage_gui(root, build_data)
    return hook, build_data


def _staged_files(build_data: dict) -> dict[str, Path]:
    return {
        destination: Path(source)
        for source, destination in build_data["force_include"].items()
    }


def test_stage_gui_ships_only_clean_tracked_source_and_fresh_dist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    log = _fake_npm(tmp_path, monkeypatch)

    hook, build_data = _stage(root)

    staged = _staged_files(build_data)
    assert sorted(staged) == [
        "hyprial/_gui/index.html",
        "hyprial/_gui/release.json",
    ]
    assert log.read_text(encoding="utf-8").splitlines() == [
        "ci --ignore-scripts",
        "run build",
    ]
    manifest = json.loads(
        staged["hyprial/_gui/release.json"].read_text(encoding="utf-8")
    )
    assert manifest["sourceCommit"] == _git(root, "rev-parse", "HEAD")
    assert manifest["sourceRemote"] == _git(
        root, "config", "--get", "remote.origin.url"
    )
    assert [entry["path"] for entry in manifest["files"]] == ["index.html"]
    assert not any("src/gui" in name or "app/src" in name for name in staged)
    hook.finalize("wheel", build_data, "unused")


def test_stage_gui_always_runs_a_clean_install_over_stale_node_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    stale = root / "gui" / "node_modules" / "stale-marker"
    stale.parent.mkdir()
    stale.write_text("old", encoding="utf-8")
    log = _fake_npm(tmp_path, monkeypatch)

    hook, build_data = _stage(root)

    assert not stale.exists()
    assert log.read_text(encoding="utf-8").splitlines()[0] == (
        "ci --ignore-scripts"
    )
    assert (root / "gui" / "node_modules" / "fresh-marker").is_file()
    hook.finalize("wheel", build_data, "unused")


def test_stage_gui_rejects_untracked_source_before_npm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    (root / "gui" / "src" / "new.js").write_text("new\n", encoding="utf-8")
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match="untracked"):
        _stage(root)

    assert not log.exists()


def test_stage_gui_allows_generated_node_modules_and_dist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    (root / "gui" / "node_modules").mkdir()
    (root / "gui" / "node_modules" / "stale-marker").write_text(
        "old", encoding="utf-8"
    )
    (root / "gui" / "dist").mkdir()
    (root / "gui" / "dist" / "stale.html").write_text(
        "stale\n", encoding="utf-8"
    )
    log = _fake_npm(tmp_path, monkeypatch)

    hook, build_data = _stage(root)

    assert (root / "gui" / "dist" / "stale.html").exists() is False
    assert log.read_text(encoding="utf-8").splitlines()[0] == (
        "ci --ignore-scripts"
    )
    hook.finalize("wheel", build_data, "unused")


def test_stage_gui_rejects_symlinks_in_the_source_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = root / "gui" / "src" / "linked.js"
    link.symlink_to(outside)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "tracked symlink")
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match="symbolic link"):
        _stage(root)

    assert not log.exists()
    assert outside.read_text(encoding="utf-8") == "secret\n"


def test_stage_gui_rejects_symlinks_created_in_dist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    _fake_npm(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_NPM_SYMLINK_TARGET", str(outside))

    with pytest.raises(module.GuiWheelError, match="symbolic link"):
        _stage(root)


def test_stage_gui_requires_a_tracked_lockfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path, lockfile=False)
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match="package-lock.json"):
        _stage(root)

    assert not log.exists()


def test_stage_gui_rejects_tracked_modifications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    (root / "gui" / "src" / "main.js").write_text(
        "console.log('changed');\n", encoding="utf-8"
    )
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match="tracked modifications"):
        _stage(root)

    assert not log.exists()


def _stamp_version(root: Path, abbrev: str) -> None:
    committed = _git(root, "show", "HEAD:VERSION").strip()
    (root / "VERSION").write_text(
        f"{committed}+g{abbrev}\n", encoding="utf-8"
    )


def test_stage_gui_tolerates_only_the_wheel_release_version_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    _stamp_version(root, _git(root, "rev-parse", "--short=7", "HEAD"))
    log = _fake_npm(tmp_path, monkeypatch)

    hook, build_data = _stage(root)

    manifest = json.loads(
        _staged_files(build_data)["hyprial/_gui/release.json"].read_text(
            encoding="utf-8"
        )
    )
    assert manifest["sourceCommit"] == _git(root, "rev-parse", "HEAD")
    assert log.read_text(encoding="utf-8").splitlines() == [
        "ci --ignore-scripts",
        "run build",
    ]
    hook.finalize("wheel", build_data, "unused")


def test_stage_gui_tolerates_a_longer_head_prefix_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    _stamp_version(root, _git(root, "rev-parse", "HEAD")[:12])
    _fake_npm(tmp_path, monkeypatch)

    hook, build_data = _stage(root)

    assert _staged_files(build_data)["hyprial/_gui/release.json"].is_file()
    hook.finalize("wheel", build_data, "unused")


def test_stage_gui_rejects_a_stamp_with_a_foreign_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    (root / "scratch.txt").write_text("second\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "second")
    foreign = _git(root, "rev-parse", "HEAD")
    _git(root, "reset", "-q", "--hard", "HEAD~1")
    _stamp_version(root, foreign[:7])
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match="tracked modifications"):
        _stage(root)

    assert not log.exists()


def test_stage_gui_rejects_a_stamp_shorter_than_seven_chars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    _stamp_version(root, _git(root, "rev-parse", "HEAD")[:6])
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match="tracked modifications"):
        _stage(root)

    assert not log.exists()


def test_stage_gui_rejects_a_version_with_any_other_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    (root / "VERSION").write_text("1.2.3+dirty\n", encoding="utf-8")
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match="tracked modifications"):
        _stage(root)

    assert not log.exists()


def test_stage_gui_rejects_a_stamp_with_leading_tab_and_blank_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    committed = _git(root, "show", "HEAD:VERSION").strip()
    stamp = f"{committed}+g{_git(root, 'rev-parse', '--short=7', 'HEAD')}"
    (root / "VERSION").write_text(f"\t{stamp}\n\n", encoding="utf-8")
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match="tracked modifications"):
        _stage(root)

    assert not log.exists()


def test_stage_gui_rejects_a_stamp_with_leading_spaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    committed = _git(root, "show", "HEAD:VERSION").strip()
    stamp = f"{committed}+g{_git(root, 'rev-parse', '--short=7', 'HEAD')}"
    (root / "VERSION").write_text(f"  {stamp}\n", encoding="utf-8")
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match="tracked modifications"):
        _stage(root)

    assert not log.exists()


def test_stage_gui_rejects_a_stamp_with_extra_trailing_blank_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    committed = _git(root, "show", "HEAD:VERSION").strip()
    stamp = f"{committed}+g{_git(root, 'rev-parse', '--short=7', 'HEAD')}"
    (root / "VERSION").write_text(f"{stamp}\n\n", encoding="utf-8")
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match="tracked modifications"):
        _stage(root)

    assert not log.exists()


def test_stage_gui_rejects_a_stamp_without_trailing_newline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    committed = _git(root, "show", "HEAD:VERSION").strip()
    stamp = f"{committed}+g{_git(root, 'rev-parse', '--short=7', 'HEAD')}"
    (root / "VERSION").write_text(stamp, encoding="utf-8")
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match="tracked modifications"):
        _stage(root)

    assert not log.exists()


def test_stage_gui_rejects_a_version_stamp_plus_any_other_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    _stamp_version(root, _git(root, "rev-parse", "--short=7", "HEAD"))
    (root / "gui" / "src" / "main.js").write_text(
        "console.log('changed');\n", encoding="utf-8"
    )
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match="tracked modifications"):
        _stage(root)

    assert not log.exists()


def test_stage_gui_rejects_a_second_tracked_change_after_a_valid_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    _stamp_version(root, _git(root, "rev-parse", "--short=7", "HEAD"))
    (root / "NOTES.md").write_text("changed\n", encoding="utf-8")
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match="tracked modifications"):
        _stage(root)

    assert not log.exists()


@pytest.mark.parametrize(
    "render",
    [
        lambda stamp: f"{stamp}",  # no trailing newline
        lambda stamp: f"{stamp}\n\n",  # extra newline
        lambda stamp: f" {stamp}\n",  # leading space
        lambda stamp: f"\t{stamp}\n",  # leading tab
        lambda stamp: f"{stamp} \n",  # trailing space
        lambda stamp: f"{stamp}\r\n",  # CRLF
        lambda stamp: f"{stamp.upper()}\n",  # upper-case hex
    ],
    ids=["no-newline", "extra-newline", "leading-space", "leading-tab",
         "trailing-space", "crlf", "upper-hex"],
)
def test_stage_gui_rejects_whitespace_or_case_variants_of_a_valid_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, render
) -> None:
    root = _repo(tmp_path)
    committed = _git(root, "show", "HEAD:VERSION").strip()
    abbrev = _git(root, "rev-parse", "--short=7", "HEAD")
    stamp = f"{committed}+g{abbrev}"
    (root / "VERSION").write_bytes(render(stamp).encode("utf-8"))
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match="tracked modifications"):
        _stage(root)

    assert not log.exists()


def test_stage_gui_rejects_a_valid_stamp_with_a_mode_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    _stamp_version(root, _git(root, "rev-parse", "--short=7", "HEAD"))
    (root / "VERSION").chmod(0o755)
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match="tracked modifications"):
        _stage(root)

    assert not log.exists()


def _expect_refused(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = _fake_npm(tmp_path, monkeypatch)
    with pytest.raises(module.GuiWheelError, match="tracked modifications"):
        _stage(root)
    assert not log.exists()


def test_stage_gui_rejects_a_valid_stamp_with_a_staged_only_mode_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    _stamp_version(root, _git(root, "rev-parse", "--short=7", "HEAD"))
    _git(root, "add", "VERSION")
    _git(root, "update-index", "--chmod=+x", "VERSION")
    _expect_refused(root, tmp_path, monkeypatch)


def test_stage_gui_rejects_a_valid_stamp_with_a_staged_gitlink_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    head = _git(root, "rev-parse", "HEAD")
    _stamp_version(root, head[:7])
    _git(root, "update-index", "--cacheinfo", f"160000,{head},VERSION")
    _expect_refused(root, tmp_path, monkeypatch)


@pytest.mark.parametrize("flag", ["--skip-worktree", "--assume-unchanged"])
def test_stage_gui_rejects_tracked_edits_hidden_by_index_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    root = _repo(tmp_path)
    _stamp_version(root, _git(root, "rev-parse", "--short=7", "HEAD"))
    _git(root, "update-index", flag, "gui/src/main.js")
    (root / "gui" / "src" / "main.js").write_text(
        "console.log('hidden');\n", encoding="utf-8"
    )
    _expect_refused(root, tmp_path, monkeypatch)


def test_stage_gui_tolerates_a_staged_content_only_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    _stamp_version(root, _git(root, "rev-parse", "--short=7", "HEAD"))
    _git(root, "add", "VERSION")
    _fake_npm(tmp_path, monkeypatch)

    hook, build_data = _stage(root)

    assert _staged_files(build_data)["hyprial/_gui/release.json"].is_file()
    hook.finalize("wheel", build_data, "unused")


@pytest.mark.parametrize("target", ["VERSION", "gui/src/main.js"])
def test_stage_gui_rejects_chmod_hidden_by_core_filemode_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    root = _repo(tmp_path)
    _stamp_version(root, _git(root, "rev-parse", "--short=7", "HEAD"))
    _git(root, "config", "core.fileMode", "false")
    (root / target).chmod(0o755)
    _expect_refused(root, tmp_path, monkeypatch)


def _hide_with_fsmonitor(root: Path, tmp_path: Path, path: str) -> None:
    hook = tmp_path / "fsmonitor-hook"
    hook.write_text("#!/bin/sh\nprintf 'token\\0'\n", encoding="utf-8")
    hook.chmod(0o755)
    _git(root, "config", "core.fsmonitor", str(hook))
    _git(root, "update-index", "--fsmonitor-valid", path)


def test_stage_gui_rejects_edits_hidden_by_fsmonitor_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    _stamp_version(root, _git(root, "rev-parse", "--short=7", "HEAD"))
    _hide_with_fsmonitor(root, tmp_path, "gui/src/main.js")
    (root / "gui" / "src" / "main.js").write_text(
        "console.log('hidden');\n", encoding="utf-8"
    )
    _expect_refused(root, tmp_path, monkeypatch)


def test_stage_gui_rejects_any_version_hidden_by_fsmonitor_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    _hide_with_fsmonitor(root, tmp_path, "VERSION")
    (root / "VERSION").write_text("9.9.9+not-a-stamp\n", encoding="utf-8")
    _expect_refused(root, tmp_path, monkeypatch)


def test_stage_gui_ignores_a_redirected_git_index_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    _stamp_version(root, _git(root, "rev-parse", "--short=7", "HEAD"))
    (root / "gui" / "src" / "main.js").write_text(
        "console.log('changed');\n", encoding="utf-8"
    )
    decoy = tmp_path / "decoy-index"
    monkeypatch.setenv("GIT_INDEX_FILE", str(decoy))
    _expect_refused(root, tmp_path, monkeypatch)
