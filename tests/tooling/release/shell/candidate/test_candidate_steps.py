"""Run the package script's stages under errexit, with fake host tools.

The macOS package is built by ``scripts/build/package-macos-shell.sh``, run
by hand since 2026-11-20 (no packaging workflow). The former manual workflow
ran it the way Forgejo runs a step without ``shell:``, as
``bash --noprofile --norc -e -o pipefail {0}``, so errexit matters: run 50856
showed the collect step dying under ``-e`` on a glob that matched nothing and
leaving a 0-byte SHA256SUMS behind, which the upload then failed on. These
tests source the script and call one stage function under the same bash
options, with a PATH built from scratch (fake ``node``, ``npm``, ``uv``,
``cargo``, ``codesign``, ``hdiutil`` plus links to the few real utilities), so
the Node of the machine running the tests never leaks in. The Node 24
fallback location is read from ``NODE24_BIN_CANDIDATES``, which the tests
point at a temporary directory instead of the Homebrew keg; the selection is
the ``node-bin`` file the node stage leaves for the following stages.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from tests.tooling.release.shell.candidate.script_support import call, executable, node_dir, tools

BUILD_PATH = ("source", "host", "node", "toolchain", "tauri_cli", "wheel", "stage", "verify_staged", "build_app")


def _node_bin(workspace: Path) -> str:
    file = workspace / "dist" / "shell-macos-work" / "node-bin"
    return file.read_text(encoding="utf-8") if file.exists() else ""


def _select(tmp_path: Path, path: list[Path], candidates: list[Path]):
    result = call("( stage_node )", tmp_path / "work", path, {"NODE24_BIN_CANDIDATES": " ".join(str(item) for item in candidates)})
    return result, _node_bin(tmp_path / "work")


def _toolchain(tmp_path: Path, path: list[Path]):
    return call("( stage_toolchain )", tmp_path / "work", path)


def test_node_already_24_on_path_is_kept(tmp_path: Path) -> None:
    on_path = node_dir(tmp_path / "path-node", "v24.22.3")
    keg = node_dir(tmp_path / "keg", "v24.22.3")
    result, selected = _select(tmp_path, [on_path, tools(tmp_path)], [keg])
    assert result.returncode == 0, result.stdout + result.stderr
    assert selected == ""
    assert "v24.22.3" in result.stdout


def test_node_26_on_path_selects_the_24_keg_for_the_following_stages(tmp_path: Path) -> None:
    on_path = node_dir(tmp_path / "path-node", "v26.13.1")
    keg = node_dir(tmp_path / "keg", "v24.22.3")
    bin_dir = tools(tmp_path)
    result, selected = _select(tmp_path, [on_path, bin_dir], [tmp_path / "absent", keg])
    assert result.returncode == 0, result.stdout + result.stderr
    assert selected == f"{keg}\n"
    assert "v26.13.1" in result.stdout and str(keg) in result.stdout

    check = _toolchain(tmp_path, [keg, on_path, bin_dir])
    assert check.returncode == 0, check.stdout + check.stderr
    assert f"{keg}/node v24.22.3" in check.stdout
    assert f"{keg}/npm 11.0.0 node:24.22.3" in check.stdout


def test_no_node_on_path_at_all_still_finds_the_keg(tmp_path: Path) -> None:
    keg = node_dir(tmp_path / "keg", "v24.22.3")
    result, selected = _select(tmp_path, [tools(tmp_path)], [keg])
    assert result.returncode == 0, result.stdout + result.stderr
    assert selected == f"{keg}\n"


def test_no_node_24_anywhere_fails_with_the_install_hint(tmp_path: Path) -> None:
    on_path = node_dir(tmp_path / "path-node", "v26.13.1")
    other_major = node_dir(tmp_path / "node22", "v22.11.0")
    without_npm = node_dir(tmp_path / "no-npm", "v24.22.3", npm=False)
    result, selected = _select(tmp_path, [on_path, tools(tmp_path)], [tmp_path / "absent", other_major, without_npm])
    assert result.returncode == 1, result.stdout + result.stderr
    assert selected == ""
    assert "brew install node@24" in result.stdout
    assert "v26.13.1" in result.stdout


def test_the_toolchain_check_rejects_node_26_first_on_path(tmp_path: Path) -> None:
    on_path = node_dir(tmp_path / "path-node", "v26.13.1")
    keg = node_dir(tmp_path / "keg", "v24.22.3")
    result = _toolchain(tmp_path, [on_path, keg, tools(tmp_path)])
    assert result.returncode == 1
    assert "the wheel's GUI build needs Node 24, found v26.13.1" in result.stdout


def test_the_toolchain_check_rejects_an_npm_that_runs_under_another_node(tmp_path: Path) -> None:
    stray = tmp_path / "stray"
    executable(stray / "npm", 'case "$*" in\n  "version --json") printf \'{\\n  "npm": "11.0.0",\\n  "node": "26.13.1"\\n}\\n\' ;;\n  *) echo "11.0.0" ;;\nesac')
    keg = node_dir(tmp_path / "keg", "v24.22.3")
    result = _toolchain(tmp_path, [stray, keg, tools(tmp_path)])
    assert result.returncode == 1
    assert "npm runs under node:26.13.1" in result.stdout


def _collect(tmp_path: Path, workspace: Path, failed: str | None):
    """Statuses before ``failed`` succeeded, ``failed`` failed, later ones were not run; ``None`` is a full success."""

    lines = []
    state = "success"
    for step in BUILD_PATH:
        if step == failed:
            lines.append(f"set_status {step} failure")
            state = "not-run"
        else:
            lines.append(f"set_status {step} {state}")
    return call("\n".join(lines) + "\n( stage_collect )", workspace, [tools(tmp_path)])


def _outcome(step: str, failed: str | None) -> str:
    seen = False
    for name in BUILD_PATH:
        if name == failed:
            seen = True
            if name == step:
                return "failure"
        elif name == step:
            return "not-run" if seen else "success"
    raise AssertionError(step)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _names(directory: Path) -> list[str]:
    return sorted(item.name for item in directory.iterdir())


def test_nothing_built_leaves_a_note_naming_the_failed_stage_and_no_checksums(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    result = _collect(tmp_path, workspace, "node")
    assert result.returncode == 0, result.stdout + result.stderr
    candidate = workspace / "candidate"
    assert _names(candidate) == ["BUILD-INFO.txt", "NOT-BUILT.txt"]
    note = (candidate / "NOT-BUILT.txt").read_text(encoding="utf-8").splitlines()
    assert note[0] == "no app or dmg was built; the first stage that did not succeed: node (failure)"
    assert note[1:] == [f"{step}={_outcome(step, 'node')}" for step in BUILD_PATH]


def test_evidence_json_only_is_kept_next_to_the_note(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    (workspace / "candidate").mkdir(parents=True)
    (workspace / "candidate" / "verify-staged.json").write_text('{"ok": true}\n', encoding="utf-8")
    result = _collect(tmp_path, workspace, "build_app")
    assert result.returncode == 0, result.stdout + result.stderr
    candidate = workspace / "candidate"
    assert _names(candidate) == ["BUILD-INFO.txt", "NOT-BUILT.txt", "verify-staged.json"]
    assert "build_app (failure)" in (candidate / "NOT-BUILT.txt").read_text(encoding="utf-8")


def test_a_staged_manifest_without_a_dmg_is_summed_alone(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    runtime = workspace / "clients" / "native" / "src-tauri" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "runtime-manifest.json").write_text('{"files": []}\n', encoding="utf-8")
    result = _collect(tmp_path, workspace, "build_app")
    assert result.returncode == 0, result.stdout + result.stderr
    candidate = workspace / "candidate"
    assert _names(candidate) == ["BUILD-INFO.txt", "NOT-BUILT.txt", "SHA256SUMS", "runtime-manifest.json"]
    sums = (candidate / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert sums == [f"{_sha256(candidate / 'runtime-manifest.json')}  runtime-manifest.json"]


def test_a_full_build_collects_the_dmg_the_manifest_and_their_checksums(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    dmg_dir = workspace / "clients" / "native" / "src-tauri" / "target" / "release" / "bundle" / "dmg"
    dmg_dir.mkdir(parents=True)
    (dmg_dir / "HyprialOS_0.0.0_aarch64.dmg").write_bytes(b"dmg bytes")
    runtime = workspace / "clients" / "native" / "src-tauri" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "runtime-manifest.json").write_text('{"files": []}\n', encoding="utf-8")
    (workspace / "candidate").mkdir()
    for name in ("verify-staged.json", "verify-bundle.json", "smoke-sigterm.json", "smoke-kill9.json"):
        (workspace / "candidate" / name).write_text("{}\n", encoding="utf-8")
    result = _collect(tmp_path, workspace, None)
    assert result.returncode == 0, result.stdout + result.stderr
    candidate = workspace / "candidate"
    assert _names(candidate) == sorted(
        [
            "BUILD-INFO.txt",
            "HyprialOS_0.0.0_aarch64.dmg",
            "SHA256SUMS",
            "runtime-manifest.json",
            "smoke-kill9.json",
            "smoke-sigterm.json",
            "verify-bundle.json",
            "verify-staged.json",
        ]
    )
    sums = (candidate / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert sums == [
        f"{_sha256(candidate / 'HyprialOS_0.0.0_aarch64.dmg')}  HyprialOS_0.0.0_aarch64.dmg",
        f"{_sha256(candidate / 'runtime-manifest.json')}  runtime-manifest.json",
    ]


def test_a_successful_build_without_a_dmg_is_an_error(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    result = _collect(tmp_path, workspace, None)
    assert result.returncode == 1
    assert "build succeeded but no dmg" in result.stdout
    assert not (workspace / "candidate" / "NOT-BUILT.txt").exists()


def test_a_stale_dmg_of_an_earlier_run_is_not_collected_as_this_runs(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    bundle = workspace / "clients" / "native" / "src-tauri" / "target" / "release" / "bundle"
    (bundle / "dmg").mkdir(parents=True)
    (workspace / "clients" / "native" / "src-tauri").mkdir(parents=True, exist_ok=True)
    (bundle / "dmg" / "HyprialOS_old.dmg").write_bytes(b"old")
    result = call("( stage_build_app )", workspace, [tools(tmp_path)], {"FAKE_LOG": str(tmp_path / "fake.log"), "FAKE_NO_DMG": "1"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (bundle / "dmg" / "HyprialOS_old.dmg").exists()
