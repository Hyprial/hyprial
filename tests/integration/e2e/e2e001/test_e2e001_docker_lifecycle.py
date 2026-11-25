"""Guard test: the E2E-001 runner's Docker lifecycle actually reaps resources.

The runner promises "no `hyprial-e2e001-*` resource survives a run" (prefix +
label discovery, `remove_resources()` in a finally, `self_check()` failing
loudly).  This test locks that promise in against the real Docker daemon:

1. create the same resource shapes the runner creates (prefixed container,
   prefixed network, labeled volume — including a PAUSED container, the
   state an interrupted suspend/resume phase would leave behind);
2. prove `self_check()` detects them, so a real leak turns a run red;
3. prove `remove_resources()` reaps every one of them, ending with a clean
   `self_check()` and zero matches from the Docker CLI itself.

Skipped when no Docker daemon is reachable (the unit-test CI container has
none), and skipped when prefixed resources already exist — removing them
would tear down a live E2E-001 run.

The test obeys the same lifecycle rule it guards: whatever it creates is
removed in a finally, even when an assertion fails halfway through.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
RUNNER_PATH = ROOT / "contract" / "e2e" / "run-two-machine-roundtrip.py"


def load_runner():  # type: ignore[no-untyped-def]
    scenario_dir = str(RUNNER_PATH.parent)
    if scenario_dir not in sys.path:
        sys.path.insert(0, scenario_dir)
    spec = importlib.util.spec_from_file_location(
        "two_machine_roundtrip_e2e_runner", RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def docker_reachable() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        capture_output=True,
        text=True,
        timeout=30.0,
        check=False,
    )
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    not docker_reachable(), reason="no Docker daemon reachable"
)

# A tiny throwaway image: the guard must not depend on the runner's own image
# having been built.  Pausing requires a real process, hence `sleep`.
GUARD_IMAGE = "alpine:3"


@pytest.fixture()
def runner():  # type: ignore[no-untyped-def]
    module = load_runner()
    if module.prefixed_containers() or module.prefixed_networks():
        pytest.skip(
            f"{module.PREFIX}-* resources exist; refusing to interfere with "
            "what may be a live E2E-001 run"
        )
    created: list[str] = []
    try:
        yield module, created
    finally:
        # Same rule the runner lives by: this test's resources die with it,
        # on pass, fail, or error.  remove_resources is prefix/label based,
        # so it reaps anything created above even if an assertion fired.
        problems = module.remove_resources()
        assert not module.self_check(), (
            f"guard test left {module.PREFIX}-* residue behind: {problems}"
        )


def _create_runner_shaped_resources(module, created: list[str]) -> None:  # type: ignore[no-untyped-def]
    """Mirror exactly what run_docker() creates: prefixed network + container
    carrying the label, plus a labeled volume (the shape compose-style
    leftovers take)."""

    module.docker(["network", "create", f"{module.PREFIX}-guardtest-net"])
    created.append("network")
    module.docker(
        [
            "run",
            "-d",
            "--name",
            f"{module.PREFIX}-guardtest-a",
            "--network",
            f"{module.PREFIX}-guardtest-net",
            "--label",
            "hyprial-e2e001=1",
            GUARD_IMAGE,
            "sleep",
            "300",
        ]
    )
    created.append("container")
    # The state an interrupted suspend/resume phase leaves behind: a PAUSED
    # prefixed container.  `docker rm -f` must still reap it (verified
    # against Docker 29.4 when this guard was written).
    module.docker(
        [
            "run",
            "-d",
            "--name",
            f"{module.PREFIX}-guardtest-paused",
            "--label",
            "hyprial-e2e001=1",
            GUARD_IMAGE,
            "sleep",
            "300",
        ]
    )
    created.append("paused-container")
    module.docker(["pause", f"{module.PREFIX}-guardtest-paused"])
    module.docker(
        [
            "volume",
            "create",
            "--label",
            "hyprial-e2e001=1",
            f"{module.PREFIX}-guardtest-vol",
        ]
    )
    created.append("volume")
    assert created, "fixture bookkeeping sanity"


def test_self_check_detects_and_remove_resources_reaps_everything(runner) -> None:  # type: ignore[no-untyped-def]
    module, _created = runner
    _create_runner_shaped_resources(module, _created)

    # 1. The guard must SEE the leak — a self-check that passes over live
    #    residue would let a real run report cleaned=true while leaking.
    detected = module.self_check()
    assert any("containers" in item for item in detected), detected
    assert any("networks" in item for item in detected), detected
    assert any("volumes" in item for item in detected), detected

    # 2. Cleanup must reap every shape, including the paused container.
    problems = module.remove_resources()
    assert problems == [], f"remove_resources reported problems: {problems}"

    # 3. Nothing under the prefix survives — checked both through the
    #    runner's own self-check and the raw Docker CLI (no self-dealing).
    assert module.self_check() == []
    for args in (
        ["ps", "-aq", "--filter", f"name={module.PREFIX}"],
        ["network", "ls", "-q", "--filter", f"name={module.PREFIX}"],
        ["volume", "ls", "-q", "--filter", "label=hyprial-e2e001=1"],
    ):
        probe = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=30.0,
            check=True,
        )
        assert probe.stdout.split() == [], f"docker {args}: {probe.stdout!r}"
