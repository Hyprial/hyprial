"""The wheel GUI stage accepts publish-stamped provenance without a checkout.

``scripts/ci/github_publish.py`` snapshots are ``git archive`` extractions
with no ``.git``, so ``gui_build_hook._stage_gui`` cannot run the tracked-
input, clean-source, and HEAD/remote checks a checkout build uses.  The
snapshot step instead writes ``gui/gui-source.json`` (schema
``hyprial.gui-snapshot/v1``, see the stamp writer in
``scripts/ci/github_publish_policy/guistamp.py``): the pinned 40-hex commit,
the publish target remote, and a sha256 per shipped ``gui/`` file.

These tests drive a real snapshot-shaped tree (no ``.git``), the real stamp
writer, and the recording npm stub, so the stamp validation and the build are
exercised together rather than mocked away.  The git-checkout path stays
covered by ``test_gui_build_hook_source_guard.py``; a stamp inside a checkout
is just an untracked ``gui/`` file that the existing clean-source check
refuses, and one test pins that.
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
STAMP_SCRIPT = REPO / "scripts/ci/github_publish_policy/guistamp.py"

STAMP_NAME = "gui-source.json"
FAKE_COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
PUBLISH_REMOTE = "https://github.com/Hyprial/hyprial.git"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


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
    return _load_module("hyprial_gui_build_hook_snapshot_stamp", HOOK_PATH)


def _stamp_module():
    return _load_module("hyprial_guistamp_snapshot_stamp", STAMP_SCRIPT)


module = _hook_module()


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _snapshot_tree(tmp_path: Path) -> Path:
    """A snapshot-shaped GUI tree: no ``.git`` anywhere above ``gui/``."""
    root = tmp_path / "snapshot"
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
    (gui / "package-lock.json").write_text(
        '{"lockfileVersion": 3}\n', encoding="utf-8"
    )
    (gui / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
    (gui / "src" / "main.js").write_text(
        "console.log('source');\n", encoding="utf-8"
    )
    return root


def _write_stamp(root: Path, **overrides) -> Path:
    arguments: dict = {
        "commit": FAKE_COMMIT,
        "remote": PUBLISH_REMOTE,
        "error": RuntimeError,
    }
    arguments.update(overrides)
    _stamp_module().write_gui_stamp(root, **arguments)
    return root / "gui" / STAMP_NAME


def _checkout_tree(tmp_path: Path) -> Path:
    """A real git checkout of the same GUI tree (for the no-relaxation pin)."""
    root = tmp_path / "checkout"
    (root / "gui" / "src").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "remote", "add", "origin", "ssh://git@git.internal.example/x.git")
    gui = root / "gui"
    (gui / "package.json").write_text(
        json.dumps({"name": "@hyprial/gui-app", "version": "1.2.3"}),
        encoding="utf-8",
    )
    (gui / "package-lock.json").write_text(
        '{"lockfileVersion": 3}\n', encoding="utf-8"
    )
    (gui / "src" / "main.js").write_text(
        "console.log('source');\n", encoding="utf-8"
    )
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


def test_the_stamp_contract_agrees_on_both_sides() -> None:
    """The hook's accepted stamp and the publish writer's stamp are the same
    schema and file name: neither side may drift from the other."""
    assert module.SNAPSHOT_STAMP_SCHEMA == _stamp_module().STAMP_SCHEMA
    assert module.SNAPSHOT_STAMP_NAME == _stamp_module().STAMP_FILE_NAME


def test_snapshot_build_uses_the_stamp_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _snapshot_tree(tmp_path)
    _write_stamp(root)
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
    assert manifest["sourceCommit"] == FAKE_COMMIT
    assert manifest["sourceRemote"] == PUBLISH_REMOTE
    assert [entry["path"] for entry in manifest["files"]] == ["index.html"]
    hook.finalize("wheel", build_data, "unused")


def test_snapshot_without_a_stamp_is_refused_before_npm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkout-free tree with no stamp has no provenance at all: refuse."""
    root = _snapshot_tree(tmp_path)
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match="stamp"):
        _stage(root)

    assert not log.exists()


def test_a_modified_snapshot_input_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bytes that changed between the scanned snapshot and this build are
    caught by the stamp hashes before npm runs."""
    root = _snapshot_tree(tmp_path)
    _write_stamp(root)
    (root / "gui" / "src" / "main.js").write_text(
        "tampered();\n", encoding="utf-8"
    )
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match="changed"):
        _stage(root)

    assert not log.exists()


def test_an_unlisted_snapshot_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file injected into the snapshot after the scans is not covered by
    the stamp: the wheel build refuses the incomplete attestation."""
    root = _snapshot_tree(tmp_path)
    _write_stamp(root)
    (root / "gui" / "src" / "extra.js").write_text(
        "injected();\n", encoding="utf-8"
    )
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match="cover"):
        _stage(root)

    assert not log.exists()


def test_a_stamp_listing_an_absent_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _snapshot_tree(tmp_path)
    _write_stamp(root)
    (root / "gui" / "index.html").unlink()
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match="missing"):
        _stage(root)

    assert not log.exists()


def _mutated_stamp(root: Path, mutate) -> None:
    path = root / "gui" / STAMP_NAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_a_stamp_with_a_wrong_schema_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _snapshot_tree(tmp_path)
    _write_stamp(root)
    _mutated_stamp(root, lambda p: p.update(schema="hyprial.gui-source/v1"))
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match="schema"):
        _stage(root)

    assert not log.exists()


def test_a_stamp_with_a_short_commit_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _snapshot_tree(tmp_path)
    _write_stamp(root)
    _mutated_stamp(root, lambda p: p.update(commit=FAKE_COMMIT[:12]))
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match="commit"):
        _stage(root)

    assert not log.exists()


def test_a_stamp_with_a_credential_remote_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stamp remote flows into the wheel's ``release.json``; the hook
    enforces the same no-credentials rule as the product manifest reader."""
    root = _snapshot_tree(tmp_path)
    _write_stamp(root)
    _mutated_stamp(
        root, lambda p: p.update(remote="https://user:secret@github.com/x.git")
    )
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match="remote"):
        _stage(root)

    assert not log.exists()


def test_a_stamp_with_an_unsafe_file_path_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _snapshot_tree(tmp_path)
    _write_stamp(root)
    _mutated_stamp(
        root,
        lambda p: p["files"].append(
            {"path": "../outside.txt", "sha256": "0" * 64}
        ),
    )
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match="unsafe"):
        _stage(root)

    assert not log.exists()


def test_a_stamp_with_a_malformed_digest_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _snapshot_tree(tmp_path)
    _write_stamp(root)
    _mutated_stamp(root, lambda p: p["files"][0].update(sha256="zz"))
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match="sha256"):
        _stage(root)

    assert not log.exists()


def test_a_generated_tree_is_not_stamp_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``node_modules`` present before the build is not a stamping input and
    is still removed by the fresh-install rule."""
    root = _snapshot_tree(tmp_path)
    _write_stamp(root)
    stale = root / "gui" / "node_modules" / "stale-marker"
    stale.parent.mkdir()
    stale.write_text("old", encoding="utf-8")
    log = _fake_npm(tmp_path, monkeypatch)

    hook, build_data = _stage(root)

    assert not stale.exists()
    assert log.read_text(encoding="utf-8").splitlines() == [
        "ci --ignore-scripts",
        "run build",
    ]
    assert "hyprial/_gui/release.json" in _staged_files(build_data)
    hook.finalize("wheel", build_data, "unused")


def test_a_stamp_in_a_git_checkout_does_not_bypass_the_source_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No relaxation where ``.git`` exists: a stamp written into a checkout
    is an untracked ``gui/`` file, and the existing clean-source check must
    refuse it exactly as it refuses any other one."""
    root = _checkout_tree(tmp_path)
    _write_stamp(root)
    log = _fake_npm(tmp_path, monkeypatch)

    with pytest.raises(module.GuiWheelError, match="untracked"):
        _stage(root)

    assert not log.exists()
