"""B0 consumes public identity state while retaining both existing OS locks."""

import json
import os
import subprocess
import sys

import pytest

from hyprial.daemon.impl.onboarding.lifecycle.admission import BootstrapAdmission
from hyprial.daemon.impl.configuration.ownership import DaemonOwnershipBusy, DaemonStateOwnershipFence
from hyprial.identity import IdentityTransactionBusy, IdentityTransactionLock, IdentityRecordError
from hyprial.kernel import process_birth_identity


@pytest.fixture(autouse=True)
def empty_override(monkeypatch):
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)


def test_empty_home_admission_holds_both_locks_without_starting_business(tmp_path):
    home = tmp_path / "home"
    state = home / "state"
    with BootstrapAdmission.acquire(home, state, home / "ipc.sock") as admission:
        assert admission.identity.snapshot.owner is None
        with pytest.raises(IdentityTransactionBusy):
            IdentityTransactionLock.acquire(home, timeout=0)
        with pytest.raises(DaemonOwnershipBusy):
            DaemonStateOwnershipFence.acquire(state, timeout=0)
        assert not (home / ".active_daemon").exists()
        assert not (state / "daemon.json").exists()
    with IdentityTransactionLock.acquire(home, timeout=0):
        pass


@pytest.mark.parametrize("record", [{"owner": "existing@example.test"}, {"identityIssuer": "issuer"}])
def test_existing_identity_or_residue_refuses_without_lock_files(tmp_path, record):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(record))
    before = settings.read_bytes()
    with pytest.raises((RuntimeError, IdentityRecordError)):
        BootstrapAdmission.acquire(tmp_path, tmp_path / "state", tmp_path / "ipc.sock")
    assert list(tmp_path.iterdir()) == [settings]
    assert settings.read_bytes() == before


def test_even_stale_live_heartbeat_refuses_and_is_not_removed(tmp_path):
    marker = tmp_path / ".active_daemon"
    marker.write_text(json.dumps({"pid": os.getpid(), "processIdentity": process_birth_identity(os.getpid()),
                                  "heartbeatMonotonic": 0}))
    before = marker.read_bytes()
    with pytest.raises(RuntimeError, match="not exited"):
        BootstrapAdmission.acquire(tmp_path, tmp_path / "state", tmp_path / "ipc.sock")
    assert marker.read_bytes() == before
    with IdentityTransactionLock.acquire(tmp_path, timeout=0):
        pass


def test_reaped_previous_process_admits_without_deleting_records(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env={"HOME": str(tmp_path)})
    try:
        birth = process_birth_identity(child.pid)
        assert birth
        child.communicate(b"x", timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)
    state = tmp_path / "state"
    state.mkdir()
    marker = tmp_path / ".active_daemon"
    marker.write_text(json.dumps({"pid": child.pid, "processIdentity": birth}))
    normal = state / "daemon.json"
    normal.write_text(json.dumps({"schemaVersion": 1, "pid": child.pid, "socket": str(tmp_path / "ipc.sock")}))
    before = marker.read_bytes(), normal.read_bytes()
    with BootstrapAdmission.acquire(tmp_path, state, tmp_path / "ipc.sock"):
        assert (marker.read_bytes(), normal.read_bytes()) == before


def test_reaped_bootstrap_record_is_removed_before_new_admission(tmp_path):
    from hyprial.daemon.impl.onboarding.records import BootstrapRecord

    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={"HOME": str(tmp_path)},
    )
    try:
        birth = process_birth_identity(child.pid)
        assert birth
        state = tmp_path / "state"
        state.mkdir()
        BootstrapRecord(child.pid, birth, "old-epoch", str(tmp_path / "ipc.sock")).publish(state)
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=10)
        with BootstrapAdmission.acquire(tmp_path, state, tmp_path / "ipc.sock"):
            assert BootstrapRecord.read(state) is None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


@pytest.mark.parametrize("raw", ['{', '{}', '{"pid":true}', '{"pid":12345,"socket":"other"}'])
def test_unverifiable_normal_record_refuses_and_releases_locks(tmp_path, raw):
    state = tmp_path / "state"
    state.mkdir()
    marker = state / "daemon.json"
    marker.write_text(raw)
    with pytest.raises((ValueError, RuntimeError)):
        BootstrapAdmission.acquire(tmp_path, state, tmp_path / "ipc.sock")
    assert marker.read_text() == raw
    with IdentityTransactionLock.acquire(tmp_path, timeout=0):
        pass
    with DaemonStateOwnershipFence.acquire(state, timeout=0):
        pass


def test_identity_is_rechecked_under_both_locks(tmp_path, monkeypatch):
    import hyprial.daemon.impl.onboarding.lifecycle.admission as module
    original = module.read_identity_admission
    calls = []

    def changing_read(home):
        calls.append(home)
        if len(calls) == 2:
            with pytest.raises(IdentityTransactionBusy):
                IdentityTransactionLock.acquire(home, timeout=0)
            with pytest.raises(DaemonOwnershipBusy):
                DaemonStateOwnershipFence.acquire(home / "state", timeout=0)
            (home / "settings.json").write_text('{"owner":"new@example.test"}')
        return original(home)

    monkeypatch.setattr(module, "read_identity_admission", changing_read)
    with pytest.raises(RuntimeError):
        BootstrapAdmission.acquire(tmp_path, tmp_path / "state", tmp_path / "ipc.sock")
    assert len(calls) == 2
    with IdentityTransactionLock.acquire(tmp_path, timeout=0):
        pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink regression")
def test_symlink_home_does_not_create_locks_in_target(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        BootstrapAdmission.acquire(alias, alias / "state", alias / "ipc.sock")
    assert list(actual.iterdir()) == []


def test_live_bootstrap_record_is_rejected_without_removal(tmp_path):
    from hyprial.daemon.impl.onboarding.records import BootstrapRecord
    state = tmp_path / "state"
    record = BootstrapRecord.current(str(tmp_path / "ipc.sock"))
    record.publish(state)
    before = (state / "bootstrap.json").read_bytes()
    with pytest.raises(RuntimeError, match="not exited"):
        BootstrapAdmission.acquire(tmp_path, state, tmp_path / "ipc.sock")
    assert (state / "bootstrap.json").read_bytes() == before


def _bootstrap_marker(socket, pid, birth, **extra):
    return {"schemaVersion": 1, "pid": pid, "socket": str(socket), "phase": "bootstrap",
            "birthIdentity": birth, **extra}


def _reaped_child(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env={"HOME": str(tmp_path)})
    try:
        birth = process_birth_identity(child.pid)
        assert birth
        child.communicate(b"x", timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)
    return child.pid, birth


@pytest.mark.parametrize("with_record", [True, False])
def test_dead_bootstrap_marker_and_record_are_reaped_then_admitted(tmp_path, with_record):
    from hyprial.daemon.impl.onboarding.records import BootstrapRecord

    pid, birth = _reaped_child(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    socket = tmp_path / "ipc.sock"
    marker = state / "daemon.json"
    marker.write_text(json.dumps(_bootstrap_marker(socket, pid, birth, launchId="old")))
    if with_record:
        BootstrapRecord(pid, birth, "old-epoch", str(socket)).publish(state)
    with BootstrapAdmission.acquire(tmp_path, state, socket):
        assert not marker.exists()
        assert BootstrapRecord.read(state) is None


def test_live_bootstrap_marker_refuses_and_keeps_both_records(tmp_path):
    from hyprial.daemon.impl.onboarding.records import BootstrapRecord

    state = tmp_path / "state"
    state.mkdir()
    socket = tmp_path / "ipc.sock"
    birth = process_birth_identity(os.getpid())
    marker = state / "daemon.json"
    marker.write_text(json.dumps(_bootstrap_marker(socket, os.getpid(), birth)))
    BootstrapRecord(os.getpid(), birth, "epoch", str(socket)).publish(state)
    before = marker.read_bytes(), (state / "bootstrap.json").read_bytes()
    with pytest.raises(RuntimeError, match="not exited"):
        BootstrapAdmission.acquire(tmp_path, state, socket)
    assert (marker.read_bytes(), (state / "bootstrap.json").read_bytes()) == before


@pytest.mark.parametrize("with_record", [True, False])
def test_bootstrap_marker_with_a_reused_pid_is_reaped_then_admitted(tmp_path, with_record):
    """Decision (KP-079): a different birth at the recorded pid proves the recorded
    bootstrap process is gone (births are unique per process start), so the stale
    records are reaped under the held locks; nothing is adopted or signalled."""
    from hyprial.daemon.impl.onboarding.records import BootstrapRecord

    state = tmp_path / "state"
    state.mkdir()
    socket = tmp_path / "ipc.sock"
    marker = state / "daemon.json"
    marker.write_text(json.dumps(_bootstrap_marker(socket, os.getpid(), "proc:not-this-birth")))
    if with_record:
        BootstrapRecord(os.getpid(), "proc:not-this-birth", "epoch", str(socket)).publish(state)
    with BootstrapAdmission.acquire(tmp_path, state, socket):
        assert not marker.exists()
        assert BootstrapRecord.read(state) is None


def test_bootstrap_record_alone_with_a_reused_pid_is_reaped(tmp_path):
    from hyprial.daemon.impl.onboarding.records import BootstrapRecord

    state = tmp_path / "state"
    state.mkdir()
    socket = tmp_path / "ipc.sock"
    BootstrapRecord(os.getpid(), "proc:not-this-birth", "epoch", str(socket)).publish(state)
    with BootstrapAdmission.acquire(tmp_path, state, socket):
        assert BootstrapRecord.read(state) is None


def test_bootstrap_marker_when_birth_identities_are_unreadable_still_refuses(tmp_path, monkeypatch):
    from hyprial.daemon.impl.onboarding import processes

    state = tmp_path / "state"
    state.mkdir()
    socket = tmp_path / "ipc.sock"
    marker = state / "daemon.json"
    marker.write_text(json.dumps(_bootstrap_marker(socket, os.getpid(), "proc:not-this-birth")))
    monkeypatch.setattr(processes, "process_birth_identity", lambda _pid: None)
    with pytest.raises(RuntimeError, match="unverified"):
        BootstrapAdmission.acquire(tmp_path, state, socket)
    assert marker.exists()


def test_serving_marker_with_a_reused_pid_still_refuses(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    socket = tmp_path / "ipc.sock"
    marker = state / "daemon.json"
    marker.write_text(json.dumps({"schemaVersion": 1, "pid": os.getpid(), "socket": str(socket), "epoch": "e"}))
    heartbeat = tmp_path / ".active_daemon"
    heartbeat.write_text(json.dumps({"pid": os.getpid(), "processIdentity": "proc:not-this-birth",
                                     "heartbeatMonotonic": 0}))
    with pytest.raises(RuntimeError, match="reused"):
        BootstrapAdmission.acquire(tmp_path, state, socket)
    assert marker.exists() and heartbeat.exists()


@pytest.mark.parametrize("birth", [None, "", "  ", 7])
def test_bootstrap_marker_without_a_birth_identity_refuses(tmp_path, birth):
    pid, _ = _reaped_child(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    socket = tmp_path / "ipc.sock"
    marker = state / "daemon.json"
    marker.write_text(json.dumps(_bootstrap_marker(socket, pid, birth)))
    with pytest.raises(RuntimeError, match="no birth identity"):
        BootstrapAdmission.acquire(tmp_path, state, socket)
    assert marker.exists()


def test_bootstrap_marker_disagreeing_with_the_bootstrap_record_refuses(tmp_path):
    from hyprial.daemon.impl.onboarding.records import BootstrapRecord

    pid, birth = _reaped_child(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    socket = tmp_path / "ipc.sock"
    marker = state / "daemon.json"
    marker.write_text(json.dumps(_bootstrap_marker(socket, pid, birth)))
    BootstrapRecord(pid, "proc:other", "epoch", str(socket)).publish(state)
    with pytest.raises(RuntimeError, match="disagrees"):
        BootstrapAdmission.acquire(tmp_path, state, socket)
    assert marker.exists() and (state / "bootstrap.json").exists()


def test_serving_marker_without_a_heartbeat_still_refuses_for_a_dead_pid(tmp_path):
    pid, _ = _reaped_child(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    socket = tmp_path / "ipc.sock"
    marker = state / "daemon.json"
    marker.write_text(json.dumps({"schemaVersion": 1, "pid": pid, "socket": str(socket), "epoch": "e"}))
    with pytest.raises(RuntimeError, match="cannot be verified"):
        BootstrapAdmission.acquire(tmp_path, state, socket)
    assert marker.exists()
