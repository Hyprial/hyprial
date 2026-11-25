"""The host launcher stages source but never mounts its Hyprial state."""

from __future__ import annotations

import os
from pathlib import Path
import select
import shutil
import subprocess
import pytest


DOCKER_FILES = Path(__file__).resolve().parents[3] / "scripts/build/docker"


def run(*args: str, cwd: Path) -> None:
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("mode", ["normal", "dev"])
@pytest.mark.parametrize("source_kind", ["remote", "acceptance"])
def test_host_launcher_builds_self_contained_image_without_host_daemon_or_mounts(tmp_path: Path, mode: str, source_kind: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run("git", "init", "-q", "-b", "dev", cwd=source)
    run("git", "config", "user.email", "gui-test@example.invalid", cwd=source)
    run("git", "config", "user.name", "GUI test", cwd=source)
    (source / "gui").mkdir()
    (source / "gui/package.json").write_text('{"name":"gui"}\n')
    (source / "gui/package-lock.json").write_text('{"name":"gui"}\n')
    run("git", "add", ".", cwd=source)
    run("git", "commit", "-qm", "dev snapshot", cwd=source)
    remote = tmp_path / "remote.git"
    run("git", "clone", "-q", "--bare", str(source), str(remote), cwd=tmp_path)

    caller = tmp_path / "caller"
    caller.mkdir()
    run("git", "init", "-q", "-b", "feature", cwd=caller)
    run("git", "config", "user.email", "gui-test@example.invalid", cwd=caller)
    run("git", "config", "user.name", "GUI test", cwd=caller)
    run("git", "remote", "add", "origin", remote.as_uri(), cwd=caller)
    (caller / "src").mkdir()
    (caller / "src/hyprial").mkdir()
    (caller / "src/hyprial/__init__.py").write_text("")
    (caller / "gui").mkdir()
    (caller / "gui/package-lock.json").write_text("local committed GUI\n")
    sidecar = caller / "sidecar/hyprial-tailcat"
    sidecar.mkdir(parents=True)
    (sidecar / "go.mod").write_text("module example.invalid/tailcat\n")
    for name in ("pyproject.toml", "uv.lock", "VERSION", "README.md"):
        (caller / name).write_text("test\n")
    scripts = caller / "scripts/build/docker"
    scripts.mkdir(parents=True)
    for name in (
        "run-gui-debug.sh",
        "gui-debug-proxy.py",
        "gui-debug-container-entrypoint.sh",
        "Dockerfile.gui-live",
    ):
        shutil.copy2(DOCKER_FILES / name, scripts / name)
    (scripts / "Dockerfile.gui-acceptance").write_text("FROM scratch\n")
    run("git", "add", ".", cwd=caller)
    run("git", "commit", "-qm", "backend snapshot", cwd=caller)
    (caller / "gui/package-lock.json").write_text("dirty GUI must not be staged\n")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_DOCKER_CALLS\"\n"
        "case \"$1\" in\n"
        "  version) exit 0 ;;\n"
        "  build) context=\"${@: -1}\"; test -f \"$context/src/hyprial/__init__.py\"; test -f \"$context/gui/package-lock.json\"; test -f \"$context/scripts/build/docker/gui-debug-container-entrypoint.sh\"; test ! -e \"$context/gui/node_modules\"; if [ \"$FAKE_SOURCE\" = acceptance ]; then test -f \"$context/sidecar/hyprial-tailcat/go.mod\"; test \"$(cat \"$context/gui/package-lock.json\")\" = 'local committed GUI'; else test \"$(cat \"$context/gui/package-lock.json\")\" = '{\"name\":\"gui\"}'; fi; exit 0 ;;\n"
        "  run) echo fake-container; exit 0 ;;\n"
        "  logs) echo \"GUI_CONTAINER_READY mode=$FAKE_MODE\"; exit 0 ;;\n"
        "  cp) echo 'tmpfs files are unavailable to docker cp' >&2; exit 1 ;;\n"
        "  exec) test \"$3\" = cat; test \"$4\" = /runtime/public-launch.url; printf 'http://127.0.0.1:4382/#grant=secret\\n'; exit 0 ;;\n"
        "  inspect) echo true; exit 0 ;;\n"
        "  wait) sleep 20; echo 0; exit 0 ;;\n"
        "  rm) exit 0 ;;\n"
        "esac\n"
        "exit 2\n"
    )
    docker.chmod(0o755)
    env = dict(os.environ)
    env.update(
        PATH=f"{fake_bin}:{env['PATH']}",
        TMPDIR=str(tmp_path),
        FAKE_DOCKER_CALLS=str(tmp_path / "docker-calls"),
        FAKE_MODE=mode,
        FAKE_SOURCE=source_kind,
    )
    process = subprocess.Popen(
        ("bash", str(scripts / "run-gui-debug.sh"), mode, "4382", source_kind),
        cwd=caller,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], 10)
        assert ready, "host launcher did not become ready"
        line = process.stdout.readline()
        assert "GUI debug ready" in line
        assert "secret" not in line
        handoff = Path(line.split("handoff=", 1)[1].strip())
        assert handoff.read_text() == "http://127.0.0.1:4382/#grant=secret\n"
        assert handoff.stat().st_mode & 0o777 == 0o600
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=5)
    calls = (tmp_path / "docker-calls").read_text().splitlines()
    build_call = next(line for line in calls if line.startswith("build "))
    if source_kind == "acceptance":
        assert "Dockerfile.gui-acceptance" in build_call
    else:
        assert "Dockerfile.gui-live" in build_call
    run_call = next(line for line in calls if line.startswith("run "))
    assert "127.0.0.1:4382:4174" in run_call
    assert "--mount" not in run_call
    assert "--volume" not in run_call
    assert " -v " not in run_call
    assert run_call.endswith(f" {mode}")
    assert any(line.startswith("exec fake-container cat /runtime/public-launch.url") for line in calls)
    assert not any(line.startswith("cp ") for line in calls)
    assert any(line.startswith("rm ") for line in calls)
