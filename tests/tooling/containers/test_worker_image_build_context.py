"""The worker image build context: every COPY source the Dockerfile names.

An argv-level test that fakes the build tool proves the script's *arguments*,
not that the build can run.  The 2026-10-01 refactors moved three files the
Dockerfile copies -- ``docker/`` into ``scripts/build/docker/`` (``b1023d69b``)
and ``src/hyprial/harnesses/`` under ``daemon/impl/harnesses/``
(``9ee7d1035``) -- and moved two of them behind ``.dockerignore`` (``scripts``,
and the harness files only exist below ``src``, which is copied wholesale).
Three COPY lines therefore read paths that no longer exist, and nothing in the
suite noticed: ``test_build_worker_image.py`` fakes the build tool out.

Two arms:

* the static arm (runs everywhere) reads the Dockerfile and fails when a COPY
  source no longer exists at the context root, or when nothing supplies the
  ENTRYPOINT file;
* the docker arm (skipped when no daemon is reachable) replays the same COPY
  lines onto ``FROM scratch`` with the real context, which is the only reading
  that can see ``.dockerignore`` -- the static arm cannot decide ignore rules
  without re-implementing them.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = ROOT / "scripts/build/docker/Dockerfile.hyprial-worker"


def _docker_reachable() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        probe = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=30.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def _copy_instructions() -> list[list[str]]:
    """Every COPY that reads the build context, as ``[src..., dest]``."""
    text = DOCKERFILE.read_text()
    text = re.sub(r"\\\n\s*", " ", text)  # join continuations
    instructions: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("COPY "):
            continue
        tokens = stripped.split()[1:]
        if any(token.startswith("--from=") for token in tokens):
            continue  # a stage copy cannot see the context
        tokens = [token for token in tokens if not token.startswith("--")]
        if len(tokens) >= 2:
            instructions.append(tokens)
    return instructions


def test_every_context_copy_source_exists() -> None:
    instructions = _copy_instructions()
    assert instructions, "the Dockerfile copies nothing from the context"
    missing = [
        (src, instruction)
        for instruction in instructions
        for src in instruction[:-1]
        if not (ROOT / src).exists()
    ]
    assert not missing, f"COPY reads paths that are not in the tree: {missing}"


def test_the_entrypoint_file_is_supplied_by_a_copy() -> None:
    text = DOCKERFILE.read_text()
    entrypoint = re.search(r'^ENTRYPOINT \["(.+?)"\]', text, re.MULTILINE)
    assert entrypoint is not None, "the Dockerfile declares no ENTRYPOINT"
    dest = entrypoint.group(1)
    assert any(instruction[-1] == dest for instruction in _copy_instructions()), (
        f"nothing copies {dest}; the built image cannot start"
    )


@pytest.mark.skipif(not _docker_reachable(), reason="no Docker daemon reachable")
def test_the_context_really_carries_every_copy_source(tmp_path: Path) -> None:
    """Replay the COPY lines onto ``FROM scratch`` with the real build context.

    This is the reading that sees ``.dockerignore``: the worker entrypoint now
    lives under ``scripts/``, so the Dockerfile's copy depends on the
    re-inclusion chain in ``.dockerignore`` staying intact.
    """
    lines = ["FROM scratch", *(f"COPY {' '.join(i)}" for i in _copy_instructions())]
    (tmp_path / "Dockerfile.probe").write_text("\n".join(lines) + "\n")
    result = subprocess.run(
        ["docker", "build", "-q", "-f", str(tmp_path / "Dockerfile.probe"), str(ROOT)],
        capture_output=True,
        text=True,
        timeout=600.0,
        check=False,
    )
    assert result.returncode == 0, f"build context rejects a COPY source:\n{result.stderr[-2000:]}"
