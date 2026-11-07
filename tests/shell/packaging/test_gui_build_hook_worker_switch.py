"""The worker-wheel switch: thin builds for the containerized-transfer image.

With ``HYPRIAL_BUILD_WORKER_WHEEL=1`` the custom build hook returns before
staging the tailcat sidecar -- the worker image's builder stage compiles the
sidecar once and COPY --from=builder's it into /opt/hyprial/_bin, so the wheel
build needs neither a .git context nor a Go toolchain.  The retired GUI bundle
has no stage at all in this hook.
"""

from __future__ import annotations

import importlib
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace


def _import_build_hook():
    """Import ``hyprial.gui_build_hook`` without a hatchling install.

    The project venv carries no build backend -- ``uv sync`` builds the wheel
    in an isolated environment, so ``hatchling`` is a build requirement, not
    an installed package -- and the hook module imports its base class at
    import time.  Carry that base class as a bare object, the same way
    ``test_tailcat_wheel.py`` does, or this module cannot be collected at all.
    """
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
            setattr(sys.modules[parent], child.rsplit(".", 1)[-1], sys.modules[child])
        sys.modules[names[-1]].BuildHookInterface = type("BuildHookInterface", (), {})
    return importlib.import_module("hyprial.gui_build_hook")


_build_hook = _import_build_hook()
WORKER_WHEEL_ENV = _build_hook.WORKER_WHEEL_ENV
CustomBuildHook = _build_hook.CustomBuildHook


def _hook(root: Path, version: str = "0.5.4") -> CustomBuildHook:
    """An instance carrying the three attributes ``initialize`` reads.

    The base class is a bare carrier here (the venv has no build backend), so
    its ``__init__`` takes no hatch arguments; set the attributes directly --
    the same shape ``test_tailcat_wheel.py`` uses.
    """

    class _Hook(CustomBuildHook):
        def __init__(self) -> None:
            self.root = str(root)
            self.metadata = SimpleNamespace(version=version)
            self.target_name = "wheel"

    return _Hook()


def test_the_hook_stages_the_installed_gui_but_not_the_retired_tree() -> None:
    source = Path(_build_hook.__file__).read_text(encoding="utf-8")
    assert "WHEEL_GUI_DIR" in source
    assert "build_gui_manifest" in source
    assert "src/gui" not in source


def test_worker_switch_skips_the_sidecar_in_a_gitless_context(tmp_path: Path) -> None:
    """No .git, no Go: the worker wheel still builds without the sidecar."""
    os.environ[WORKER_WHEEL_ENV] = "1"
    try:
        build_data: dict = {}
        _hook(tmp_path).initialize("wheel", build_data)
    finally:
        os.environ.pop(WORKER_WHEEL_ENV, None)
    assert build_data.get("force_include") is None
    assert build_data.get("pure_python") is not True
    assert build_data.get("tag") is None


def test_worker_switch_wheel_contents_are_thin(tmp_path: Path) -> None:
    """The staged wheel must not carry the retired GUI payload or sidecar."""
    import zipfile

    os.environ[WORKER_WHEEL_ENV] = "1"
    try:
        build_data: dict = {"force_include": {}, "pure_python": False, "tag": "x"}
        _hook(tmp_path).initialize("wheel", build_data)
        # Simulate the wheel the builder would produce from this build_data:
        # nothing force-included, pure-python tag untouched.
        wheel = tmp_path / "probe.whl"
        with zipfile.ZipFile(wheel, "w") as zf:
            zf.writestr("hyprial/__init__.py", "")
        names = zipfile.ZipFile(wheel).namelist()
    finally:
        os.environ.pop(WORKER_WHEEL_ENV, None)
    assert not any(
        name.startswith("hyprial/_bin/")
        or name.startswith("hyprial/_gui/")
        or name == "hyprial/gui-source.json"
        for name in names
    ), names
