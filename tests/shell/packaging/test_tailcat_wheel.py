"""Wheel packaging of the tailcat sidecar: targets, tags, and the hatch hook.

The wheel helper is import-pure (it must stay loadable by ``runpy`` inside the
build hook), and the hook itself is exercised through a fake
``BuildHookInterface`` carrier so no hatchling install is required.  The fake
roots carry no ``.git`` and no retired GUI marker: the tailcat branch runs for
real.
"""

from __future__ import annotations

import importlib.util
import stat
import sys
import types
from pathlib import Path

import pytest

from hyprial.daemon.impl.network.tailcat import binary as binary_module
from hyprial.shell.impl.packaging.tailcat_wheel import (
    ALLOW_MISSING_ENV,
    BINARY_BASENAME,
    GO_ENV,
    PLATFORM_TAGS,
    PREBUILT_ENV,
    SIDECAR_BUILD_PACKAGE,
    SIDECAR_SOURCE_DIR,
    TARGET_ENV,
    TailcatWheelError,
    binary_name,
    build_tailcat,
    find_go,
    host_target,
    platform_tag,
    resolve_target,
)

REPO = Path(__file__).resolve().parents[3]
HOOK_PATH = REPO / "src/hyprial/gui_build_hook.py"


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


# --- targets and platform tags -------------------------------------------------


@pytest.mark.parametrize(
    ("target", "tag"),
    sorted(PLATFORM_TAGS.items()),
)
def test_platform_tag_matrix(target: str, tag: str) -> None:
    assert platform_tag(target) == tag


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("darwin/arm64", "macosx_13_0_arm64"),
        ("darwin/amd64", "macosx_13_0_x86_64"),
    ],
)
def test_macos_tags_match_the_supported_native_floor(target: str, expected: str) -> None:
    """Pin OS compatibility independently of the provider's own lookup table."""

    assert platform_tag(target) == expected


@pytest.mark.parametrize("target", ["linux/386", "plan9/amd64", "darwin/riscv64", ""])
def test_platform_tag_rejects_unknown_targets(target: str) -> None:
    with pytest.raises(ValueError, match="unsupported tailcat sidecar target"):
        platform_tag(target)


def test_resolve_target_reads_the_environment() -> None:
    assert resolve_target({TARGET_ENV: "linux/amd64"}) == "linux/amd64"


@pytest.mark.parametrize("value", ["", "   "])
def test_resolve_target_blank_falls_back_to_the_host(value: str) -> None:
    assert resolve_target({TARGET_ENV: value}) == host_target()


def test_resolve_target_defaults_to_a_supported_host_target() -> None:
    assert host_target() in PLATFORM_TAGS
    assert resolve_target({}) == host_target()


@pytest.mark.parametrize("value", ["darwin", "a/b/c", "linux/", "/amd64"])
def test_resolve_target_rejects_malformed_values(value: str) -> None:
    with pytest.raises(ValueError):
        resolve_target({TARGET_ENV: value})


def test_resolve_target_rejects_unsupported_but_wellformed_values() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        resolve_target({TARGET_ENV: "freebsd/amd64"})


def test_binary_name_appends_exe_only_for_windows() -> None:
    assert binary_name("linux/amd64") == BINARY_BASENAME
    assert binary_name("darwin/arm64") == BINARY_BASENAME
    assert binary_name("windows/amd64") == f"{BINARY_BASENAME}.exe"


def test_find_go_prefers_the_explicit_path(tmp_path: Path) -> None:
    go = _script(tmp_path / "custom-go", "exit 0\n")
    assert find_go({GO_ENV: str(go), "PATH": ""}) == str(go)


def test_find_go_scans_the_path(tmp_path: Path) -> None:
    go = _script(tmp_path / "go", "exit 0\n")
    assert find_go({"PATH": str(tmp_path)}) == str(go)


def test_find_go_without_any_toolchain_is_none(tmp_path: Path) -> None:
    assert find_go({GO_ENV: "", "PATH": str(tmp_path)}) is None


# --- binary acquisition --------------------------------------------------------


def _sidecar_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / SIDECAR_SOURCE_DIR).mkdir(parents=True)
    (root / SIDECAR_SOURCE_DIR / "go.mod").write_text("module example\n", encoding="utf-8")
    return root


def test_build_tailcat_copies_the_prebuilt_binary(tmp_path: Path) -> None:
    root = _sidecar_root(tmp_path)
    origin = tmp_path / "prebuilt" / "hyprial-tailcat"
    origin.parent.mkdir()
    origin.write_bytes(b"prebuilt-payload")
    origin.chmod(0o644)  # even a mode-less input must come out executable
    output = tmp_path / "stage"

    built = build_tailcat(
        root, "linux/arm64", output=output, environ={PREBUILT_ENV: str(origin)}
    )

    assert built == output / BINARY_BASENAME
    assert built.read_bytes() == b"prebuilt-payload"
    assert built.stat().st_mode & stat.S_IXUSR


def test_build_tailcat_missing_prebuilt_is_loud(tmp_path: Path) -> None:
    root = _sidecar_root(tmp_path)
    with pytest.raises(TailcatWheelError, match=PREBUILT_ENV):
        build_tailcat(
            root,
            "linux/amd64",
            output=tmp_path / "stage",
            environ={PREBUILT_ENV: str(tmp_path / "does-not-exist")},
        )


def test_build_tailcat_without_toolchain_or_prebuilt_is_loud(tmp_path: Path) -> None:
    root = _sidecar_root(tmp_path)
    with pytest.raises(TailcatWheelError) as raised:
        build_tailcat(
            root,
            "linux/amd64",
            output=tmp_path / "stage",
            environ={"PATH": str(tmp_path)},
        )
    assert PREBUILT_ENV in str(raised.value)
    assert GO_ENV in str(raised.value)


def test_build_tailcat_without_sidecar_sources_is_loud(tmp_path: Path) -> None:
    go = _script(tmp_path / "go", "exit 0\n")
    root = tmp_path / "empty-root"
    root.mkdir()
    with pytest.raises(TailcatWheelError, match="go.mod"):
        build_tailcat(
            root,
            "linux/amd64",
            output=tmp_path / "stage",
            environ={GO_ENV: str(go)},
        )


def _fake_go(tmp_path: Path) -> tuple[Path, Path]:
    """A recording ``go`` stub: saves argv/pwd/env, writes the ``-o`` target."""

    record = tmp_path / "record"
    record.mkdir()
    go = _script(
        tmp_path / "go",
        "printf '%s\\n' \"$@\" > \"$FAKE_GO_RECORD/argv\"\n"
        "pwd > \"$FAKE_GO_RECORD/pwd\"\n"
        "env | grep -E '^(CGO_ENABLED|GOOS|GOARCH)=' | sort > \"$FAKE_GO_RECORD/env\"\n"
        "out=\"\"\nprev=\"\"\nfor arg in \"$@\"; do\n"
        '  if [ "$prev" = "-o" ]; then out="$arg"; fi\n'
        '  prev="$arg"\ndone\n'
        'printf "fake-sidecar-from-go" > "$out"\n'
        "chmod 755 \"$out\"\n",
    )
    return go, record


def test_build_tailcat_runs_go_with_cross_compile_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _sidecar_root(tmp_path)
    go, record = _fake_go(tmp_path)
    monkeypatch.setenv("FAKE_GO_RECORD", str(record))
    output = tmp_path / "stage"

    built = build_tailcat(
        root, "linux/arm64", output=output, environ={GO_ENV: str(go)}
    )

    assert built == output / BINARY_BASENAME
    assert built.read_bytes() == b"fake-sidecar-from-go"
    assert built.stat().st_mode & stat.S_IXUSR
    assert (record / "argv").read_text().splitlines() == [
        "build",
        "-trimpath",
        "-o",
        str(built),
        SIDECAR_BUILD_PACKAGE,
    ]
    assert Path((record / "pwd").read_text().strip()).resolve() == (
        root / SIDECAR_SOURCE_DIR
    ).resolve()
    assert (record / "env").read_text().splitlines() == [
        "CGO_ENABLED=0",
        "GOARCH=arm64",
        "GOOS=linux",
    ]


def test_build_tailcat_go_failure_is_loud(tmp_path: Path) -> None:
    root = _sidecar_root(tmp_path)
    go = _script(
        tmp_path / "go",
        "echo 'boom: no such target' >&2\nexit 2\n",
    )
    with pytest.raises(TailcatWheelError) as raised:
        build_tailcat(
            root, "linux/amd64", output=tmp_path / "stage", environ={GO_ENV: str(go)}
        )
    assert "boom: no such target" in str(raised.value)


# --- the hatch hook -----------------------------------------------------------


def _hook_module():
    try:
        import hatchling.builders.hooks.plugin.interface  # noqa: F401
    except ModuleNotFoundError:
        # The project venv has no build backend; a bare object carrier is all
        # the hook needs from the interface (it never calls super()).
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
        "hyprial_gui_build_hook_under_test", HOOK_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _sdist_shaped_root(tmp_path: Path) -> Path:
    """A source root with no ``.git`` and no retired GUI payload."""

    root = tmp_path / "root"
    (root / SIDECAR_SOURCE_DIR).mkdir(parents=True)
    (root / SIDECAR_SOURCE_DIR / "go.mod").write_text("module example\n", encoding="utf-8")
    return root


def _wheel_shaped_root(tmp_path: Path) -> Path:
    """A wheel root with the smallest current GUI payload and npm/Git stubs.

    A wheel stages both the sidecar and the built GUI.  These tailcat-focused
    tests still exercise the real ``_stage_gui`` path rather than skipping it;
    the stubs only keep npm and Git outside the synthetic root.  Since
    0.5.5.3.2 the hook reads a ``.git`` marker to tell a checkout (git-stub
    provenance) from a publish snapshot (stamp-file provenance), so the root
    carries the marker and stays on the checkout path these tests pin.
    """

    root = _sdist_shaped_root(tmp_path)
    (root / ".git").mkdir()
    gui = root / "gui"
    (gui / "node_modules").mkdir(parents=True)
    (gui / "dist").mkdir()
    (gui / "package.json").write_text('{"version": "0.0.0"}\n', encoding="utf-8")
    (gui / "package-lock.json").write_text(
        '{"lockfileVersion": 3}\n', encoding="utf-8"
    )
    (gui / "dist" / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
    _script(
        tmp_path / "npm",
        'case "$*" in\n'
        '  "ci --ignore-scripts") /bin/mkdir -p node_modules; exit 0 ;;\n'
        '  "run build") /bin/mkdir -p dist; '
        'printf "<!doctype html>\\n" > dist/index.html; exit 0 ;;\n'
        '  *) echo "unexpected npm arguments: $*" >&2; exit 1 ;;\n'
        "esac\n",
    )
    _script(
        tmp_path / "git",
        'case "$*" in\n'
        '  *"ls-files --error-unmatch -- gui/package.json gui/package-lock.json"*) '
        "exit 0 ;;\n"
        '  *"diff --quiet HEAD --"*) exit 0 ;;\n'
        '  *"ls-files -v -z"*) exit 0 ;;\n'
        '  *"diff --cached --raw --no-renames -z HEAD --"*) exit 0 ;;\n'
        '  *"status --porcelain=v1 -z --untracked-files=all -- gui"*) exit 0 ;;\n'
        '  *"rev-parse HEAD"*) echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ;;\n'
        '  *"config --get remote.origin.url"*) '
        "echo ssh://git@git.internal.example.com/HyprialOS/harness-bridge.git ;;\n"
        '  *) echo "unexpected git arguments: $*" >&2; exit 1 ;;\n'
        "esac\n",
    )
    return root


def _hook(module, root: Path, *, version: str = "9.9.9", target_name: str = "wheel"):
    class _Hook(module.CustomBuildHook):
        def __init__(self) -> None:
            self.root = str(root)
            self.metadata = types.SimpleNamespace(version=version)
            self.target_name = target_name

    return _Hook()


@pytest.fixture()
def prebuilt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    binary = _script(tmp_path / "prebuilt-hyprial-tailcat", "exit 0\n")
    monkeypatch.setenv(PREBUILT_ENV, str(binary))
    monkeypatch.setenv(GO_ENV, "")
    monkeypatch.setenv("PATH", str(tmp_path))
    return binary


def test_hook_wheel_stages_the_binary_and_platform_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prebuilt: Path
) -> None:
    module = _hook_module()
    root = _wheel_shaped_root(tmp_path)
    monkeypatch.setenv(TARGET_ENV, "linux/arm64")
    hook = _hook(module, root)
    build_data: dict = {"force_include": {}}

    hook.initialize("standard", build_data)

    staged = build_data["force_include"]
    assert sorted(staged.values()) == [
        f"hyprial/_bin/{BINARY_BASENAME}",
        "hyprial/_gui/index.html",
        "hyprial/_gui/release.json",
    ]
    sidecar = next(
        path
        for path, destination in staged.items()
        if destination == f"hyprial/_bin/{BINARY_BASENAME}"
    )
    assert Path(sidecar).read_bytes() == prebuilt.read_bytes()
    assert build_data["pure_python"] is False
    assert build_data["tag"] == "py3-none-manylinux_2_17_aarch64.manylinux2014_aarch64"

    staged_dir = Path(sidecar).parent
    hook.finalize("standard", build_data, "unused")
    assert not staged_dir.exists()


@pytest.mark.parametrize(
    ("target", "expected_tag"),
    [
        ("darwin/arm64", "py3-none-macosx_13_0_arm64"),
        ("darwin/amd64", "py3-none-macosx_13_0_x86_64"),
    ],
)
def test_hook_wheel_macos_gets_the_supported_floor_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prebuilt: Path,
    target: str,
    expected_tag: str,
) -> None:
    module = _hook_module()
    root = _wheel_shaped_root(tmp_path)
    monkeypatch.setenv(TARGET_ENV, target)
    hook = _hook(module, root)
    build_data: dict = {"force_include": {}}

    hook.initialize("standard", build_data)

    assert build_data["tag"] == expected_tag
    assert build_data["pure_python"] is False
    staged = build_data["force_include"]
    assert sorted(staged.values()) == [
        f"hyprial/_bin/{BINARY_BASENAME}",
        "hyprial/_gui/index.html",
        "hyprial/_gui/release.json",
    ]
    sidecar = next(
        path
        for path, destination in staged.items()
        if destination == f"hyprial/_bin/{BINARY_BASENAME}"
    )
    assert Path(sidecar).read_bytes() == prebuilt.read_bytes()
    hook.finalize("standard", build_data, "unused")


def test_hook_wheel_windows_gets_exe_suffix_and_win_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prebuilt: Path
) -> None:
    module = _hook_module()
    root = _wheel_shaped_root(tmp_path)
    monkeypatch.setenv(TARGET_ENV, "windows/amd64")
    hook = _hook(module, root)
    build_data: dict = {"force_include": {}}

    hook.initialize("standard", build_data)

    assert sorted(build_data["force_include"].values()) == [
        f"hyprial/_bin/{BINARY_BASENAME}.exe",
        "hyprial/_gui/index.html",
        "hyprial/_gui/release.json",
    ]
    assert build_data["tag"] == "py3-none-win_amd64"
    assert build_data["pure_python"] is False


def test_hook_windows_output_matches_the_daemon_platform_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prebuilt: Path
) -> None:
    module = _hook_module()
    root = _wheel_shaped_root(tmp_path)
    monkeypatch.setenv(TARGET_ENV, "windows/amd64")
    hook = _hook(module, root)
    build_data: dict = {"force_include": {}}

    hook.initialize("standard", build_data)

    # Set the simulated host only for the daemon-path comparison: the GUI
    # stage needs the real host's shutil.which implementation.
    monkeypatch.setattr(sys, "platform", "win32")
    destination = next(
        destination
        for destination in build_data["force_include"].values()
        if destination.startswith("hyprial/_bin/")
    )
    assert Path(destination).name == binary_module.wheel_binary_path(tmp_path).name


def test_hook_sdist_ships_sources_not_a_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prebuilt: Path
) -> None:
    module = _hook_module()
    root = _sdist_shaped_root(tmp_path)
    monkeypatch.setenv(TARGET_ENV, "linux/amd64")
    hook = _hook(module, root, target_name="sdist")
    build_data: dict = {"force_include": {}}

    hook.initialize("standard", build_data)

    assert build_data["force_include"] == {
        str(root / SIDECAR_SOURCE_DIR): SIDECAR_SOURCE_DIR
    }
    assert "tag" not in build_data
    assert "pure_python" not in build_data


def test_hook_missing_sidecar_without_escape_hatch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _hook_module()
    root = _sdist_shaped_root(tmp_path)
    monkeypatch.delenv(PREBUILT_ENV, raising=False)
    monkeypatch.setenv(GO_ENV, "")
    monkeypatch.setenv("PATH", str(tmp_path))  # no go anywhere
    monkeypatch.delenv(ALLOW_MISSING_ENV, raising=False)
    hook = _hook(module, root)
    build_data: dict = {"force_include": {}}

    with pytest.raises(module.TailcatWheelError):
        hook.initialize("standard", build_data)


def test_hook_allow_missing_yields_a_pure_python_wheel_with_a_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _hook_module()
    root = _wheel_shaped_root(tmp_path)
    monkeypatch.delenv(PREBUILT_ENV, raising=False)
    monkeypatch.setenv(GO_ENV, "")
    monkeypatch.setenv("PATH", str(tmp_path))  # no go anywhere
    monkeypatch.setenv(ALLOW_MISSING_ENV, "1")
    hook = _hook(module, root)
    build_data: dict = {"force_include": {}}

    hook.initialize("standard", build_data)

    assert sorted(build_data["force_include"].values()) == [
        "hyprial/_gui/index.html",
        "hyprial/_gui/release.json",
    ]
    assert not any(
        destination.startswith("hyprial/_bin/")
        for destination in build_data["force_include"].values()
    )
    assert "tag" not in build_data
    assert "pure_python" not in build_data
    assert ALLOW_MISSING_ENV in capsys.readouterr().out


def test_hook_editable_build_is_a_no_op(tmp_path: Path) -> None:
    module = _hook_module()
    hook = _hook(module, _sdist_shaped_root(tmp_path))
    build_data: dict = {"force_include": {}}

    hook.initialize("editable", build_data)

    assert build_data == {"force_include": {}}
    assert not hasattr(hook, "_stages")
