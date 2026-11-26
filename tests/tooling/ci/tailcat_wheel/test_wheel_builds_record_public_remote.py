"""Every workflow step that builds a publishable wheel records the public remote.

``HYPRIAL_BUILD_SOURCE_REMOTE`` makes the build hook write the public
repository address into ``hyprial/_gui/release.json`` instead of the runner
checkout's origin.  The declaration table is fail-closed: a workflow that
builds a wheel must be declared here.  It is empty -- no workflow in the tree
builds a wheel -- so any build step that appears in one reddens the first test
below.  The per-kind obligations that used to be checked against the declared
workflows (a publishing build sets the variable, a snapshot build never does,
a candidate-only build publishes nothing) went with those workflow files; a
workflow that starts building wheels needs those checks written against it,
not only a table entry.

The macOS package script ``package-macos-shell.sh`` still counts as a build
step, so a workflow that called it would have to be declared here. Owner
decision 2026-11-20: no workflow does (packaging is not done in CI). The
operator sets the variable by hand; the commands in
``packaging/clients/README.md`` "Package by hand" are pinned here to
``PUBLIC_SOURCE_REMOTE``, so that literal cannot drift.
"""

from __future__ import annotations

from pathlib import Path
import re
import runpy

import pytest
import yaml

from tests.support.ci.ci_workflow_policy import ROOT, workflow_files

ENV = "HYPRIAL_BUILD_SOURCE_REMOTE"
PUBLIC_REMOTE = runpy.run_path(
    str(ROOT / "src/hyprial/shell/impl/gui/install/resources.py")
)["PUBLIC_SOURCE_REMOTE"]
BUILD = re.compile(
    r"uv build\b|python3? -m build\b|pip wheel\b|hatch build\b|build-wheel\.sh|package-macos-shell\.sh"
)
PUBLISHING = re.compile(
    r"publish_wheels\.py|attach_wheel_assets\.py|uv publish|api/packages|r2_upload|gh release"
)
DECLARED: dict[str, str] = {}
MANUAL_PACKAGING = ROOT / "packaging" / "clients" / "README.md"


def _steps(path: Path):
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    for job_name, job in (document.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            yield job_name, step


def _is_build(run: str) -> bool:
    return bool(BUILD.search(re.sub(r"\\\r?\n\s*", " ", run)))


def _build_steps(path: Path):
    return [
        (job, step)
        for job, step in _steps(path)
        if _is_build(str(step.get("run") or ""))
    ]


def _sets_public_remote(step: dict) -> bool:
    env = step.get("env") or {}
    inline = f"{ENV}={PUBLIC_REMOTE}" in str(step.get("run") or "")
    return env.get(ENV) == PUBLIC_REMOTE or inline


def test_every_wheel_building_workflow_is_declared() -> None:
    building = {path.name for path in workflow_files() if _build_steps(path)}
    assert building == set(DECLARED)


def test_the_manual_packaging_commands_set_exactly_the_public_remote_constant() -> None:
    hook_env = runpy.run_path(str(ROOT / "src/hyprial/shell/impl/gui/install/resources.py"))["SOURCE_REMOTE_ENV"]
    assert hook_env == ENV
    section = MANUAL_PACKAGING.read_text(encoding="utf-8").split("\n## Package by hand\n", 1)[1].split("\n## ", 1)[0]
    settings = re.findall(rf"{ENV}=(\S+)", section)
    assert len(settings) == 2
    assert set(settings) == {PUBLIC_REMOTE}


def test_the_package_script_never_sets_the_public_remote_or_publishes() -> None:
    script = ROOT / "scripts" / "build" / "package-macos-shell.sh"
    assert ENV not in script.read_text(encoding="utf-8")
    assert not PUBLISHING.search(script.read_text(encoding="utf-8"))


def test_a_build_step_without_the_variable_is_detected() -> None:
    assert not _sets_public_remote({"run": "uv build --wheel"})
    assert not _sets_public_remote({"env": {ENV: "https://example.test/x.git"}, "run": "uv build --wheel"})
    assert _sets_public_remote({"env": {ENV: PUBLIC_REMOTE}, "run": "uv build --wheel"})

@pytest.mark.parametrize(
    "run",
    [
        "uv build",
        "uv build --wheel",
        "uv build \\\n  --wheel",
        "uv build \\\n  --out-dir dist",
        "python -m build",
        "python3 -m build --wheel",
        "pip wheel .",
        "hatch build -t wheel",
        "bash scripts/build/build-wheel.sh",
        "bash scripts/build/package-macos-shell.sh --source-sha x --out y",
        "echo start\nuv build --out-dir dist\necho done",
    ],
)


def test_every_wheel_build_form_is_detected(run: str) -> None:
    assert _is_build(run)

@pytest.mark.parametrize("run", ["uv sync --extra test", "uv run pytest tests/", "pip install ."])


def test_non_build_commands_are_not_detected(run: str) -> None:
    assert not _is_build(run)
