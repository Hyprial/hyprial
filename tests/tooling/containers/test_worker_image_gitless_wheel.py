"""The worker image installs a thin wheel: no ``.git``, GUI payload, or sidecar.

The image is a local runtime that talks to the target machine's daemon over the
container's local IPC, so its wheel needs neither the retired GUI bundle nor a
Go toolchain.  The Dockerfile sets ``HYPRIAL_BUILD_WORKER_WHEEL=1``; the hatch
hook then returns before either payload stage, and the Dockerfile's ``tailcat``
builder stage compiles the sidecar once, lands it in the installed package and
pins the engine commit there.  The escape hatch that would let a wheel ship
without a sidecar is deliberately not set: a lost switch fails the build.

Arms:

* static (runs everywhere): ``build-worker-image.sh`` goes straight from
  ``uv build``/``uv version`` to ``docker build`` -- the retired GUI provenance
  stamp and its ``git`` probes are gone -- and the Dockerfile builds the thin
  wheel and verifies the builder sidecar.
* real (skipped when uv is unavailable): a copy of the package with no
  ``.git`` and no ``src/gui`` builds a thin wheel containing no ``_gui`` or
  ``_bin`` payload.  This is the heavy arm; run it only with a build slot.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/build/docker/build-worker-image.sh"
DOCKERFILE = ROOT / "scripts/build/docker/Dockerfile.hyprial-worker"


def _run_blocks() -> list[str]:
    """Every RUN instruction, continuations joined, in file order."""
    blocks: list[str] = []
    current: list[str] | None = None
    for line in DOCKERFILE.read_text().splitlines():
        if current is not None:
            current.append(line)
            if not line.rstrip().endswith("\\"):
                blocks.append("\n".join(current))
                current = None
            continue
        if line.startswith("RUN "):
            current = [line]
            if not line.rstrip().endswith("\\"):
                blocks.append(line)
                current = None
    return blocks


def _forbidden_payload(names: list[str]) -> list[str]:
    """Exact payload paths, never ordinary source modules.

    ``identity_binding.py`` and ``_bindings.py`` contain the substring
    ``_bin`` but are product source; matching the substring declared a thin
    wheel broken even when it carried no payload.  The payload has a precise
    location: the ``hyprial/_bin/`` directory, the retired ``hyprial/_gui/``
    directory, and the retired ``gui-source.json`` stamp.
    """
    return sorted(
        name
        for name in names
        if name.startswith("hyprial/_bin/")
        or name.startswith("hyprial/_gui/")
        or name == "hyprial/gui-source.json"
    )


def _context_copy(destination: Path) -> None:
    """The package inputs the Dockerfile needs: no ``.git`` and no ``src/gui``."""
    for name in ("pyproject.toml", "README.md", "VERSION"):
        shutil.copyfile(ROOT / name, destination / name)
    shutil.copytree(
        ROOT / "src/hyprial",
        destination / "src/hyprial",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "_bin"),
    )


def test_the_script_has_no_gui_provenance_stamp() -> None:
    text = SCRIPT.read_text()
    lines = [line.strip() for line in text.splitlines()]
    build_at = next(i for i, line in enumerate(lines) if line.startswith("docker build "))
    assert "gui-source.json" not in text
    assert "shell/impl/gui/bundle.py" not in text
    assert "git status" not in text
    assert "uv build --wheel" in text
    assert "uv run python -c 'from hyprial import __version__; print(__version__)'" in text
    # No temporary stamp file is created before the context is sent.
    assert not any(line.startswith("stamp=") for line in lines)
    assert build_at == len(lines) - 2


def test_the_dockerfile_builds_a_thin_wheel_and_pins_the_builder_sidecar() -> None:
    text = DOCKERFILE.read_text()
    installer = [
        block
        for block in _run_blocks()
        if "uv pip install --python /opt/hyprial/venv/bin/python --no-cache /tmp/hyprial-src"
        in block
    ]
    assert len(installer) == 1, "expected exactly one RUN that installs the project"
    # The thin worker wheel stages neither payload, so this step needs neither
    # a GUI tree nor a Go toolchain ...
    assert "export HYPRIAL_BUILD_WORKER_WHEEL=1" in installer[0]
    # ... and it may not fall back to the escape hatch that ships a wheel
    # without a sidecar: a lost switch has to fail the build, not pass it.
    assert "HYPRIAL_TAILCAT_ALLOW_MISSING" not in installer[0]
    assert "ENV HYPRIAL_TAILCAT_ALLOW_MISSING" not in text
    # The sidecar is compiled once in a builder stage, cross-compiled for the
    # target platform and checked out from go.mod/go.sum (the engine pin).
    assert "FROM --platform=$BUILDPLATFORM golang:1.27.1 AS tailcat" in text
    assert (
        "go build -trimpath -ldflags='-s -w' -o /out/hyprial-tailcat ./cmd/hyprial-tailcat"
        in text
    )
    assert "GOARCH=\"${TARGETARCH}\"" in text
    # The module version in go.mod and the commit the binary reports have to be
    # the same checkout; the runtime verify step ties both to the daemon's pin.
    assert "tailcat engine pin" in text
    assert "TailcatCommit" in text
    # It then lands where the package resolves it (binary.py: the installed
    # package's WHEEL_BIN_DIR) and the same resolver pins the engine commit.
    assert (
        "COPY --from=tailcat /out/hyprial-tailcat "
        "/opt/hyprial/tailcat-build/hyprial-tailcat" in text
    )
    assert '"$bindir/hyprial-tailcat"' in text
    assert "verify_tailcat_sidecar" in text


def test_the_dockerignore_no_longer_names_the_retired_gui_tree() -> None:
    text = (ROOT / ".dockerignore").read_text()
    assert "src/gui" not in text
    assert "node_modules" not in text


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")
def test_a_gitless_context_builds_a_thin_wheel_without_gui_or_sidecar(tmp_path: Path) -> None:
    context = tmp_path / "context"
    context.mkdir()
    _context_copy(context)
    assert not (context / ".git").exists()
    assert not (context / "src/gui").exists()
    env = {**os.environ, "HYPRIAL_BUILD_WORKER_WHEEL": "1"}

    built = subprocess.run(
        ["uv", "build", "--wheel"],
        cwd=context,
        env=env,
        capture_output=True,
        text=True,
        timeout=900.0,
        check=False,
    )
    assert built.returncode == 0, built.stderr[-2000:]
    wheels = list((context / "dist").glob("*.whl"))
    assert len(wheels) == 1, wheels
    names = zipfile.ZipFile(wheels[0]).namelist()
    forbidden = _forbidden_payload(names)
    assert not forbidden, forbidden
    # An ordinary binding module is source, not the sidecar payload.  The
    # old substring predicate rejected it; keep this as a positive control
    # for the exact-path rule above.
    assert not _forbidden_payload(
        [
            "hyprial/daemon/impl/application/messaging/identity_binding.py",
            "hyprial/identity/impl/agents/migration/_bindings.py",
            "hyprial/identity/impl/agents/registry/config_binding/__init__.py",
            "hyprial/identity/impl/device/machine_binding.py",
            "hyprial/daemon/impl/org/network/binding.py",
            "hyprial/shell/impl/login/binding.py",
        ]
    )


@pytest.mark.parametrize(
    "payload",
    [
        "hyprial/_bin/hyprial-tailcat",
        "hyprial/_bin/hyprial-tailcat.exe",
        "hyprial/_bin/",
        "hyprial/_gui/scripts/start-gui.sh",
        "hyprial/_gui/",
        "hyprial/gui-source.json",
    ],
)
def test_the_thin_wheel_payload_predicate_rejects_real_payload_paths(payload: str) -> None:
    """A real sidecar or GUI payload still fails the thin-wheel rule."""
    assert _forbidden_payload(["hyprial/cli.py", payload]) == [payload]
