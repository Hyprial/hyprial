from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.pac.actors.coordinator import (
    FileLaunchResolver,
    LaunchSpec,
    RuntimeObservation,
)
from hyprial.daemon.impl.pac.actors.daemon import DaemonActorRuntime


def test_daemon_runtime_creates_cwd_only_for_explicit_create_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations: list[object] = []
    application = SimpleNamespace(
        _lifecycle_spec=lambda spec: spec,
        _run_lifecycle_operation=operations.append,
    )
    runtime = DaemonActorRuntime(application)
    marker = "pac:g:worker:1:effect"
    monkeypatch.setattr(
        runtime,
        "observe",
        lambda _name: RuntimeObservation(True, marker, "codex"),
    )
    create_cwd = tmp_path / "create-cwd"
    require_cwd = tmp_path / "require-cwd"

    runtime.start(
        "create-worker",
        LaunchSpec(
            "codex",
            None,
            None,
            str(create_cwd),
            (),
            cwd_policy="create",
        ),
        operation_id="create-op",
        identity_marker=marker,
    )
    runtime.start(
        "require-worker",
        LaunchSpec(
            "codex",
            None,
            None,
            str(require_cwd),
            (),
            cwd_policy="require",
        ),
        operation_id="require-op",
        identity_marker=marker,
    )

    assert create_cwd.is_dir()
    assert not require_cwd.exists()
    assert len(operations) == 2


def test_file_launch_resolver_preserves_cwd_policy(tmp_path: Path) -> None:
    cwd = tmp_path / "worker"
    launch = tmp_path / "launch.yaml"
    launch.write_text(
        f"harness: codex\ncwd: {cwd}\ncwdPolicy: create\n",
        encoding="utf-8",
    )

    resolved = FileLaunchResolver(tmp_path)(
        str(launch), SimpleNamespace(requires=None)
    )

    assert resolved.spec.cwd == str(cwd)
    assert resolved.spec.cwd_policy == "create"
