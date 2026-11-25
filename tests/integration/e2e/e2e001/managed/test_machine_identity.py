"""The real fixture bootstrap prepares OS identity before public device keys."""

import ast
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from uuid import UUID

import pytest

from tests.support.runtime.environment import isolated_environment


ROOT = Path(__file__).resolve().parents[5]
MODULE = ROOT / "contract/e2e/docker/machine_identity.py"
ENTRYPOINT = ROOT / "contract/e2e/docker/entrypoint.sh"


def load_helper():
    spec = importlib.util.spec_from_file_location("e2e_machine_identity", MODULE)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


@pytest.mark.parametrize("initial", [None, b"", b"\n \n"])
def test_first_boot_creates_a_valid_id_and_restart_preserves_it(tmp_path, initial):
    path = tmp_path / "machine-id"
    if initial is not None:
        path.write_bytes(initial)
    helper = load_helper()
    first = helper.ensure_machine_id(path)
    assert UUID(first).version == 4
    content = path.read_bytes()
    assert helper.ensure_machine_id(path) == first
    assert path.read_bytes() == content


def test_independent_nodes_do_not_inherit_one_shared_identity(tmp_path):
    helper = load_helper()
    first = helper.ensure_machine_id(tmp_path / "node-a")
    second = helper.ensure_machine_id(tmp_path / "node-b")
    assert first != second


def test_existing_valid_os_id_keeps_bytes_and_permissions(tmp_path):
    path = tmp_path / "machine-id"
    path.write_bytes(b"a" * 32 + b"\n")
    path.chmod(0o444)
    mode = path.stat().st_mode
    assert load_helper().ensure_machine_id(path) == "a" * 32
    assert path.read_bytes() == b"a" * 32 + b"\n"
    assert path.stat().st_mode == mode


@pytest.mark.parametrize("content", [b"0" * 32, b"A" * 32, b"uninitialized", b"a" * 31, b"a" * 33, b"\xff", b" " * 129])
def test_invalid_existing_identity_is_refused_without_replacing_it(tmp_path, content):
    path = tmp_path / "machine-id"
    path.write_bytes(content)
    with pytest.raises(ValueError, match="machine-id"):
        load_helper().ensure_machine_id(path)
    assert path.read_bytes() == content


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_nonregular_identity_is_refused(tmp_path, kind):
    path = tmp_path / "machine-id"
    if kind == "symlink":
        target = tmp_path / "outside"
        target.write_text("a" * 32)
        path.symlink_to(target)
    else:
        path.mkdir()
    with pytest.raises(ValueError, match="machine-id"):
        load_helper().ensure_machine_id(path)


def test_two_fresh_processes_keep_the_container_writable_layer_id(tmp_path):
    code = (
        "import importlib.util, pathlib, sys;"
        "s=importlib.util.spec_from_file_location('fixture_machine_id', sys.argv[1]);"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        "print(m.ensure_machine_id(pathlib.Path(sys.argv[2])))"
    )
    env = isolated_environment(tmp_path / "env")
    path = tmp_path / "machine-id"
    command = [sys.executable, "-c", code, str(MODULE), str(path)]
    first = subprocess.check_output(command, env=env, text=True, timeout=10)
    second = subprocess.check_output(command, env=env, text=True, timeout=10)
    assert first == second and UUID(first.strip()).version == 4


def test_actual_entrypoint_prepares_os_identity_before_device_provisioning(tmp_path, monkeypatch):
    helper = load_helper()
    machine_id = tmp_path / "machine-id"
    calls = []

    def device_key(home, **kwargs):
        kwargs.pop("environ")
        assert machine_id.is_file(), "OS identity must exist before device key provisioning"
        assert len(machine_id.read_text().strip()) == 32
        calls.append("device")

    class Store:
        def __init__(self, path):
            assert path == tmp_path / "state/desired-state.json"

        def set_mailbox_role(self, enabled):
            assert enabled is False
            calls.append("role")

    fake_daemon = ModuleType("hyprial.daemon")
    fake_daemon.ensure_device_key = device_key
    fake_daemon.DesiredStateStore = Store
    monkeypatch.setitem(sys.modules, "hyprial.daemon", fake_daemon)
    monkeypatch.setitem(sys.modules, "e2e_machine_identity", helper)
    for key, value in {"HYPRIAL_HOME": str(tmp_path / "home"), "HARNESS_STATE_DIR": str(tmp_path / "state"), "HYPRIAL_OWNER": "e2e", "HYPRIAL_NODE_ID": "e2e001-node"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("HYPRIAL_AS_MAILBOX", raising=False)
    source = ENTRYPOINT.read_text().split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
    tree = ast.parse(source)
    tree.body = [node for node in tree.body if not (isinstance(node, ast.ImportFrom) and node.module == "pathlib")]
    def path(value):
        return machine_id if os.fspath(value) == "/etc/machine-id" else Path(value)

    exec(compile(tree, str(ENTRYPOINT), "exec"), {"Path": path})
    assert calls == ["device", "role"]


def test_stable_os_id_preserves_public_device_identity_and_clone_guard(tmp_path, monkeypatch):
    from hyprial.daemon import ensure_device_key
    import hyprial.daemon.impl.network.tailcat.keys as keys
    from hyprial.identity import device_key_path, read_machine_binding
    import hyprial.identity.impl.device.machine_binding as binding

    helper = load_helper()
    path = tmp_path / "machine-id"
    machine_id = helper.ensure_machine_id(path)
    calls = []

    def genkey(home, *, environ, force):
        calls.append(force)
        key = device_key_path(home)
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_text("fixture-only-key-marker")
        return {"serverPublic": f"nodekey:server-{len(calls)}", "clientPublic": f"nodekey:client-{len(calls)}"}

    monkeypatch.setattr(keys, "_run_genkey", genkey)
    monkeypatch.setattr(binding, "_machine_identifier", lambda: ("linux-machine-id", helper.ensure_machine_id(path)))
    home = tmp_path / "home"
    first = ensure_device_key(home, owner="e2e", device_id="node-a", environ={})
    second = ensure_device_key(home, owner="e2e", device_id="node-a", environ={})
    assert first == second and second.key_generation == 1 and calls == [False]
    replacement = "a" * 32 if machine_id != "a" * 32 else "b" * 32
    path.write_text(replacement + "\n")
    changed = ensure_device_key(home, owner="e2e", device_id="node-a", environ={})
    assert changed.key_generation == 2 and changed.server_public != first.server_public
    assert calls == [False, True]
    assert read_machine_binding(home).last_reason == "copied-home"
