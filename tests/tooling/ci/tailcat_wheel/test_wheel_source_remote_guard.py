"""The release-script guard loads a product file that must stay stdlib-only.

``scripts/ci/wheel_source_remote.py`` runs ``resources.py`` by path.  The
release scripts that import the guard (``attach_wheel_assets.py``,
``publish_wheels.py``, ``verify_tailcat_wheels.py``) run under the publish
runner's system python3, outside the project environment, so these tests load
it in an isolated interpreter and statically bound its imports.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from scripts.ci import wheel_source_remote as guard
from tests.support.ci.ci_workflow_policy import ROOT

GUARD = ROOT / "scripts/ci/wheel_source_remote.py"
RESOURCES = ROOT / "src/hyprial/shell/impl/gui/install/resources.py"
DEPENDENTS = "attach_wheel_assets.py, publish_wheels.py, verify_tailcat_wheels.py"
PUBLIC = "https://github.com/Hyprial/hyprial.git"


def _isolated(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-S", "-c", code],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "")},
        cwd=str(ROOT.parent),
        check=False,
    )


def test_guard_loads_in_an_isolated_interpreter_without_hyprial() -> None:
    probe = (
        "import runpy, sys\n"
        f"ns = runpy.run_path({str(GUARD)!r})\n"
        "loaded = [m for m in sys.modules if m == 'hyprial' or m.startswith('hyprial.')]\n"
        "assert not loaded, loaded\n"
        f"assert ns['PUBLIC_SOURCE_REMOTE'] == {PUBLIC!r}, ns['PUBLIC_SOURCE_REMOTE']\n"
    )
    result = _isolated(probe)
    assert result.returncode == 0, result.stderr


def test_resources_imports_only_the_standard_library() -> None:
    tree = ast.parse(RESOURCES.read_text(encoding="utf-8"))
    offenders = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = ["." * node.level + (node.module or "")] if node.level == 0 else ["relative"]
        else:
            continue
        offenders += [n for n in names if n.split(".")[0] not in sys.stdlib_module_names]
    assert not offenders, (
        f"{RESOURCES.relative_to(ROOT)} must stay standard-library only; the release "
        f"scripts {DEPENDENTS} load it by path outside the project environment. "
        f"Non-stdlib imports: {offenders}"
    )


def _wheel(path: Path, size: int) -> zipfile.ZipFile:
    manifest = json.dumps({"sourceRemote": PUBLIC, "pad": "x" * size}).encode()
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(guard.MANIFEST_MEMBER, manifest)
    return zipfile.ZipFile(path)


def test_manifest_above_the_cap_is_refused_before_reading(tmp_path: Path) -> None:
    with _wheel(tmp_path / "big.whl", guard.MAX_MANIFEST_BYTES + 1) as archive:
        with pytest.raises(guard.SourceRemoteError, match="above the"):
            guard.check_archive(archive, "big.whl")


def test_manifest_within_the_cap_is_accepted(tmp_path: Path) -> None:
    with _wheel(tmp_path / "ok.whl", 100) as archive:
        guard.check_archive(archive, "ok.whl")


def test_as_a_program_it_exits_0_for_the_public_remote_and_1_otherwise(tmp_path: Path) -> None:
    public = tmp_path / "public.whl"
    _wheel(public, 10).close()
    internal = tmp_path / "internal.whl"
    with zipfile.ZipFile(internal, "w") as archive:
        archive.writestr(guard.MANIFEST_MEMBER, json.dumps({"sourceRemote": "http://internal.invalid/x.git"}))
    run = [sys.executable, "-I", str(GUARD)]
    assert subprocess.run([*run, str(public)], capture_output=True, text=True, check=False).returncode == 0
    refused = subprocess.run([*run, str(public), str(internal)], capture_output=True, text=True, check=False)
    assert refused.returncode == 1 and "not the public repository" in refused.stderr
    assert subprocess.run([*run, str(tmp_path / "missing.whl")], capture_output=True, text=True, check=False).returncode == 1
    assert subprocess.run(run, capture_output=True, text=True, check=False).returncode == 2
