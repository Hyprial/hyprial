"""Test actual carrier seams and resource ownership without starting a VM in unit CI."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import types

import pytest

from hyprial.daemon.impl.harnesses import smolvm_runtime as runtime_module
from hyprial.daemon.impl.harnesses.smolvm_runtime import (
    SmolvmWorkerRuntime,
    SmolvmRuntimeError,
    guest_channel,
    mount_roots,
    tree_digest,
    validate_materials,
)
from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel
from tests.integration.smolvm.runtime.test_smolvm_runtime_state import runtime, vm_spec


@pytest.fixture
def materials(tmp_path, monkeypatch):
    binary = tmp_path / "bundle" / "smolvm"
    binary.parent.mkdir()
    binary.write_bytes(b"not executed")
    binary.chmod(0o700)
    (binary.parent / "agent-rootfs").mkdir()
    resize = tmp_path / "resize2fs"
    resize.write_bytes(b"not executed")
    resize.chmod(0o700)
    rootfs = tmp_path / "image"
    rootfs.mkdir()
    (rootfs / "fixture").write_text("guest")
    spec = replace(
        runtime(),
        smolvm_path=str(binary),
        smolvm_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
        rootfs_path=str(rootfs),
        rootfs_digest=tree_digest(rootfs),
        resize2fs_path=str(resize),
        resize2fs_sha256=hashlib.sha256(resize.read_bytes()).hexdigest(),
        smolvm_bundle_digest=tree_digest(binary.parent),
    )
    monkeypatch.setattr(runtime_module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(runtime_module.platform, "machine", lambda: "arm64")
    return spec


@pytest.fixture
def channel(tmp_path):
    home = tmp_path / "private-agent"
    home.mkdir()
    config = home / "C"
    config.mkdir()
    state = tmp_path / "daemon-state"
    state.mkdir()
    context = types.SimpleNamespace(
        actor="agent:owner:node:worker",
        shared_credential=None,
        roots=types.SimpleNamespace(agent_home=home, config_source=config),
    )
    return build_worker_channel(
        actor=context.actor,
        session_ref="worker-session",
        hyprial_home=tmp_path / "host-private",
        state_dir=state,
        runtime_context=context,
    )


def test_material_hash_and_link_refuse_before_execution(materials, tmp_path):
    validate_materials(materials)
    Path(materials.rootfs_path, "fixture").write_text("tampered")
    with pytest.raises(SmolvmRuntimeError, match="digest mismatch"):
        validate_materials(materials)
    Path(materials.rootfs_path, "fixture").write_text("guest")
    executable = Path(materials.smolvm_path)
    executable.rename(executable.with_suffix(".real"))
    executable.symlink_to(executable.with_suffix(".real"))
    with pytest.raises(SmolvmRuntimeError, match="canonical"):
        validate_materials(materials)


def test_guest_channel_keeps_binding_and_host_context_but_replaces_transport(channel):
    guest = guest_channel(channel, runtime())
    assert guest.actor == channel.actor and guest.session_ref == channel.session_ref
    assert guest.runtime_context is channel.runtime_context
    assert guest.mcp_server["command"] == runtime().guest_python
    assert guest.identity_environment()["HARNESS_STATE_DIR"] == "/run/hyprial-worker"
    assert str(channel.state_dir) not in json.dumps(guest.mcp_server)
    assert channel.state_dir != guest.state_dir


def test_mount_boundary_does_not_follow_shared_login_or_workspace_parents(
    channel, tmp_path
):
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    home = channel.runtime_context.roots.agent_home
    assert mount_roots(channel, str(cwd)) == (home, cwd)
    with pytest.raises(SmolvmRuntimeError, match="HOME/state parent"):
        mount_roots(channel, str(tmp_path))
    (home / "foreign-auth").symlink_to(tmp_path / "other-agent-auth")
    with pytest.raises(SmolvmRuntimeError, match="external host symbolic link"):
        mount_roots(channel, str(cwd))
    (home / "foreign-auth").unlink()
    channel.runtime_context.shared_credential = object()
    with pytest.raises(SmolvmRuntimeError, match="Shared host login"):
        mount_roots(channel, str(cwd))


@pytest.fixture
def owned_runtime(materials, channel, tmp_path, monkeypatch):
    output = tmp_path / "short" / "worker"
    monkeypatch.setattr(runtime_module, "runtime_directory", lambda *_: output)
    monkeypatch.setattr(
        runtime_module, "_host_canaries", lambda: {"real-home": "unchanged"}
    )
    calls = []

    class Relay:
        def __init__(self, **kw):
            self.binding = kw["binding"]
            self.socket_path = kw["directory"] / "daemon.sock"

        def start(self):
            calls.append("relay-start")

        def close(self):
            calls.append("relay-close")

    monkeypatch.setattr(runtime_module, "BoundWorkerRelay", Relay)
    worker = SmolvmWorkerRuntime(
        materials,
        channel,
        "daemon-epoch",
        str(channel.runtime_context.roots.agent_home),
    )
    machines = []

    def admin(*args, **_):
        calls.append(args)
        if args == ("--version",):
            return b"smolvm 1.19.0"
        if args[:2] == ("machine", "create"):
            machines.append("worker")
        if args[:2] == ("machine", "ls"):
            return "\n".join(machines).encode()
        if args[:2] == ("machine", "delete"):
            machines.clear()
        return b""

    monkeypatch.setattr(worker, "_admin", admin)
    return worker, calls


def test_real_runtime_assembly_hides_values_mounts_only_payload_and_cleans(
    owned_runtime, channel
):
    worker, calls = owned_runtime
    command, env = worker.start(
        ("/usr/local/bin/codex", "app-server", "--stdio"),
        {
            "MODEL_TEST_KEY": "synthetic-secret",
            "HOME": str(channel.runtime_context.roots.agent_home),
        },
    )
    assert "synthetic-secret" not in " ".join(command)
    assert "synthetic-secret" in env.values()
    assert "--timeout" not in command  # A model turn has no wall-clock kill switch.
    create = next(
        c for c in calls if isinstance(c, tuple) and c[:2] == ("machine", "create")
    )
    assert str(channel.state_dir) not in " ".join(create)
    assert "--ssh-agent" not in create and "--port" not in create
    assert worker.binding.daemon_epoch == "daemon-epoch"
    assert worker._active_record.exists()
    worker.close()
    assert calls.index("relay-close") < next(
        i
        for i, c in enumerate(calls)
        if isinstance(c, tuple) and c[:2] == ("machine", "delete")
    )
    assert (
        not worker._active_record.exists() and not (worker.output / "engine").exists()
    )
    assert worker.cleanup_complete


def test_runtime_start_failure_still_reclaims_owned_vm_and_relay(
    owned_runtime, monkeypatch
):
    worker, calls = owned_runtime
    original = worker._admin

    def fail(*args, **kw):
        if args[:2] == ("machine", "start"):
            raise SmolvmRuntimeError("start failed")
        return original(*args, **kw)

    monkeypatch.setattr(worker, "_admin", fail)
    with pytest.raises(SmolvmRuntimeError, match="start failed"):
        worker.start(("/usr/local/bin/codex",), {})
    assert "relay-close" in calls and worker.cleanup_complete
    assert not (worker.output / "engine").exists()


def test_native_sandbox_smoke_failure_refuses_worker_and_cleans(
    owned_runtime, monkeypatch
):
    worker, calls = owned_runtime
    original = worker._admin

    def fail_smoke(*args, **kw):
        result = original(*args, **kw)
        if runtime_module._SANDBOX_SMOKE in args:
            assert ("machine", "start", "--name", "worker") in calls
            raise SmolvmRuntimeError("native sandbox smoke failed")
        return result

    monkeypatch.setattr(worker, "_admin", fail_smoke)
    with pytest.warns(RuntimeWarning, match="outside the published support matrix"):
        with pytest.raises(SmolvmRuntimeError, match="native sandbox smoke failed"):
            worker.start(("/usr/local/bin/codex", "app-server", "--stdio"), {})
    deletion = calls.index(("machine", "delete", "--name", "worker", "--force"))
    assert calls.index("relay-close") < deletion
    assert worker.cleanup_complete
    assert not (worker.output / "owner.json").exists()
    assert not worker._active_record.exists()
    assert not (worker.output / "engine").exists()
    assert original("machine", "ls", "--names") == b""


def test_failed_cleanup_retains_fence_and_never_reports_clean(
    owned_runtime, monkeypatch
):
    worker, _ = owned_runtime
    worker.start(("/usr/local/bin/codex",), {})
    monkeypatch.setattr(
        worker, "_clean_engine", lambda: (_ for _ in ()).throw(OSError("stop failed"))
    )
    with pytest.raises(SmolvmRuntimeError, match="VM cleanup incomplete"):
        worker.close()
    assert worker._active_record.exists() and not worker.cleanup_complete


def test_crashed_generation_is_reclaimed_before_new_vm(owned_runtime, monkeypatch):
    worker, calls = owned_runtime
    worker.start(("/usr/local/bin/codex",), {})
    old_generation = json.loads((worker.output / "owner.json").read_text())[
        "generation"
    ]
    # Model only process death at the unit boundary: filesystem and fake VM
    # remain, while the OS-owned lease is released. The opt-in test uses SIGKILL.
    worker._lease.close()
    worker._lease = None
    replacement = SmolvmWorkerRuntime(
        worker.spec, worker.channel, "new-epoch", worker.cwd
    )
    monkeypatch.setattr(replacement, "_admin", worker._admin)
    calls.clear()
    try:
        replacement.start(("/usr/local/bin/codex",), {})
        deletion = calls.index(("machine", "delete", "--name", "worker", "--force"))
        creation = next(
            i
            for i, c in enumerate(calls)
            if isinstance(c, tuple) and c[:2] == ("machine", "create")
        )
        assert deletion < calls.index("relay-start") < creation
        assert (
            json.loads((worker.output / "owner.json").read_text())["generation"]
            != old_generation
        )
    finally:
        replacement.close()
    assert replacement.cleanup_complete and not (worker.output / "engine").exists()


@pytest.mark.parametrize("mutation", ["link", "mode", "engine-mode"])
def test_recovery_bad_environment_preserves_ownership_proof(
    owned_runtime, monkeypatch, tmp_path, mutation
):
    worker, calls = owned_runtime
    worker.start(("/usr/local/bin/codex",), {})
    worker._lease.close()
    worker._lease = None
    home = worker.output / ("engine" if mutation == "engine-mode" else "engine/home")
    if mutation == "link":
        home.rmdir()
        home.symlink_to(tmp_path, target_is_directory=True)
    else:
        home.chmod(0o755)
    replacement = SmolvmWorkerRuntime(
        worker.spec, worker.channel, "new-epoch", worker.cwd
    )
    monkeypatch.setattr(replacement, "_admin", worker._admin)
    calls.clear()
    with pytest.raises(SmolvmRuntimeError, match="retain owned engine"):
        replacement.start(("/usr/local/bin/codex",), {})
    assert not calls and not replacement.cleanup_complete
    assert (worker.output / "owner.json").exists() and worker._active_record.exists()
    if mutation == "link":
        home.unlink()
        home.mkdir(mode=0o700)
    else:
        home.chmod(0o700)
    replacement._env = runtime_module._private_environment(worker.output, reuse=True)
    replacement.close()
    assert replacement.cleanup_complete


def test_cleanup_return_without_engine_removal_retains_fence(
    owned_runtime, monkeypatch
):
    worker, _ = owned_runtime
    worker.start(("/usr/local/bin/codex",), {})
    clean = worker._clean_engine
    monkeypatch.setattr(worker, "_clean_engine", lambda: None)
    with pytest.raises(SmolvmRuntimeError, match="retain owned engine"):
        worker.close()
    assert not worker.cleanup_complete and worker._active_record.exists()
    assert (worker.output / "owner.json").exists()
    monkeypatch.setattr(worker, "_clean_engine", clean)
    worker.close()
    assert worker.cleanup_complete


def test_manager_pid_survives_delete_and_a_new_cleanup_generation(
    owned_runtime, monkeypatch
):
    worker, calls = owned_runtime
    worker.start(("/usr/local/bin/codex",), {})
    pidfile = worker.output / "engine/manager.pid"
    pidfile.write_text("99999999")
    original_admin, original_kill = worker._admin, runtime_module.os.kill

    def admin(*args, **kwargs):
        if args[:2] == ("machine", "delete"):
            pidfile.unlink(missing_ok=True)
        return original_admin(*args, **kwargs)

    monkeypatch.setattr(worker, "_admin", admin)
    monkeypatch.setattr(
        runtime_module.os,
        "kill",
        lambda pid, sig: None if pid == 99999999 else original_kill(pid, sig),
    )
    with pytest.raises(SmolvmRuntimeError, match="VM cleanup incomplete"):
        worker.close()
    assert not pidfile.exists() and worker._active_record.exists()
    assert json.loads(
        (worker.output / "engine/cleanup-manager-pids.json").read_text()
    ) == [99999999]
    replacement = SmolvmWorkerRuntime(
        worker.spec, worker.channel, "new-epoch", worker.cwd
    )
    monkeypatch.setattr(replacement, "_admin", admin)
    calls.clear()
    with pytest.raises(SmolvmRuntimeError, match="VM cleanup incomplete"):
        replacement.start(("/usr/local/bin/codex",), {})
    assert not any(
        isinstance(c, tuple) and c[:2] == ("machine", "create") for c in calls
    )
    assert (worker.output / "owner.json").exists() and worker._active_record.exists()
    monkeypatch.setattr(runtime_module.os, "kill", original_kill)
    replacement.close()
    assert replacement.cleanup_complete


def test_host_client_cannot_ignore_runtime_spec():
    from hyprial.daemon.impl.harnesses.codex.client import CodexAppServerClient

    with pytest.raises(ValueError, match="no host fallback"):
        CodexAppServerClient(vm_spec())


def test_codex_actual_stdio_client_closes_runtime_on_normal_and_spawn_failure(tmp_path):
    import asyncio
    import sys
    from hyprial.daemon.impl.harnesses.codex.client import CodexAppServerClient

    fake = Path(__file__).resolve().parents[4] / 'tests/fixtures/harnesses/fake_codex_app_server.py'
    channel = build_worker_channel(
        actor="agent:owner:node:worker",
        session_ref="session",
        hyprial_home=tmp_path,
        state_dir=tmp_path / "state",
    )

    class Runtime:
        def __init__(self, failure=False):
            self.closed = False
            self.failure = failure

        def start(self, command, env):
            # Replace only the VM subprocess boundary, exercising real app-server
            # protocol, native thread creation and client cleanup around it.
            if self.failure:
                return ("/nonexistent-runtime-fixture",), env
            return (sys.executable, str(fake), *command[1:]), {
                **env,
                "FAKE_CODEX_MODE": "complete",
            }

        def close(self):
            self.closed = True

    async def run():
        first = Runtime()
        client = CodexAppServerClient(
            vm_spec(),
            worker_channel=channel,
            execution_runtime=first,
            command=("/guest/codex",),
        )
        async with client:
            assert client.session_ref is not None
        assert first.closed
        failed = Runtime(True)
        with pytest.raises(FileNotFoundError):
            async with CodexAppServerClient(
                vm_spec(),
                worker_channel=channel,
                execution_runtime=failed,
                command=("/guest/codex",),
            ):
                pytest.fail("missing runtime executable started")
        assert failed.closed

    asyncio.run(run())


def test_codex_spawns_the_runtime_wrapped_argv_not_the_guest_command(tmp_path, monkeypatch):
    """AT08 2b: the argv the runtime returns must be the one actually exec'd.

    Regression for the dev back-merge: resolving the conflict against dev's
    `_spawn_managed_codex` structure must not drop the runtime wrapping, or the
    guest command silently runs on the host with the guest environment.
    """

    import asyncio
    from hyprial.daemon.impl.harnesses.codex import client as codex_module
    from hyprial.daemon.impl.harnesses.codex.client import CodexAppServerClient

    channel = build_worker_channel(
        actor="agent:owner:node:worker",
        session_ref="session",
        hyprial_home=tmp_path,
        state_dir=tmp_path / "state",
    )
    captured: dict[str, object] = {}

    class Runtime:
        def start(self, command, env):
            return ("/wrapped/smolvm", "machine", "exec", "--", *command), {
                **env,
                "AT08_WRAPPED": "1",
            }

        def close(self):
            pass

    async def spy(command, process_group, **options):
        captured["command"] = tuple(command)
        captured["env"] = dict(options.get("env") or {})
        raise RuntimeError("capture-only: do not spawn")

    monkeypatch.setattr(codex_module, "_spawn_managed_codex", spy)

    async def run():
        client = CodexAppServerClient(
            vm_spec(),
            worker_channel=channel,
            execution_runtime=Runtime(),
            command=("/guest/codex", "app-server", "--stdio"),
        )
        with pytest.raises(RuntimeError, match="capture-only"):
            async with client:
                pytest.fail("the intercepted spawn never raised")

    asyncio.run(run())

    command = captured["command"]
    assert command[:4] == ("/wrapped/smolvm", "machine", "exec", "--"), command
    # The guest argv rides behind the wrapper, i.e. the runtime's argv is what
    # reaches the spawn -- not ``self.command``.
    assert command[4] == "/guest/codex", command
    assert "app-server" in command and "--stdio" in command, command
    assert captured["env"].get("AT08_WRAPPED") == "1", captured["env"]


def test_cleanup_retry_cannot_stop_replacement_generation(owned_runtime, monkeypatch):
    worker, calls = owned_runtime
    worker.start(("/usr/local/bin/codex",), {})
    clean = worker._clean_engine
    monkeypatch.setattr(
        worker, "_clean_engine", lambda: (_ for _ in ()).throw(OSError("transient"))
    )
    with pytest.raises(SmolvmRuntimeError):
        worker.close()
    marker = worker.output / "owner.json"
    record = json.loads(marker.read_text())
    record["generation"] = "replacement-generation"
    marker.write_text(json.dumps(record))
    monkeypatch.setattr(worker, "_clean_engine", clean)
    before = len(calls)
    worker.close()
    assert len(calls) == before and worker.cleanup_complete
    assert worker._active_record.exists()


def test_cleanup_can_retry_its_own_failed_generation(owned_runtime, monkeypatch):
    worker, _ = owned_runtime
    worker.start(("/usr/local/bin/codex",), {})
    clean = worker._clean_engine
    monkeypatch.setattr(
        worker, "_clean_engine", lambda: (_ for _ in ()).throw(OSError("transient"))
    )
    with pytest.raises(SmolvmRuntimeError):
        worker.close()
    monkeypatch.setattr(worker, "_clean_engine", clean)
    worker.close()
    assert worker.cleanup_complete and not worker._active_record.exists()


def test_guest_mcp_import_does_not_load_daemon_native_transport(tmp_path):
    """Run fresh imports: pytest's already imported daemon would hide this regression."""
    import os
    import subprocess
    import sys

    script = """
import sys, importlib.abc
class GuestBoundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('zenoh', 'pycrdt'):
            raise ImportError('daemon dependency in guest: ' + fullname)
sys.meta_path.insert(0, GuestBoundary())
import hyprial.cli as cli
from hyprial.daemon.impl.mcp import serve_worker_stdio
assert 'hyprial.daemon.impl.application' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={
            "PATH": os.defpath,
            "HOME": str(tmp_path),
            "PYTHONPATH": str(Path(__file__).resolve().parents[4] / 'src'),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
