"""Public DTO and slot safety contracts.

Decision (2026-11-26, KP-140): the lock-hardening tests record calls at the
kernel's post-hardening verification, which every ``harden_private_path`` call
reaches after it changes the descriptor, so they observe hardening without
depending on how ``slot`` imports the function and fail on code that never
hardens.  The refusal tests replace ``slot.harden_private_path`` directly
because hardening a refused entry may fail before that verification runs.
The vanished-lock test removes ``operation.lock`` at the first ``lstat`` of it
after the OS lock is acquired, which is the same call in code with and without
the hardening step, so the real ``FileNotFoundError`` decides the outcome.  It
runs only where POSIX mode bits are enforced: removing a file while a lock is
held on it is not established on Windows.
"""

import os
import stat

import pytest

from hyprial.identity import TailnetCredentials
from hyprial.kernel import PrivateAccessUnsupported, ProcessFileLock, posix_metadata_enforced

from .support import Context, state


@pytest.mark.parametrize("port", [True, 0, 65536])
def test_dial_target_rejects_mutable_authority_and_invalid_port(port):
    with pytest.raises(ValueError):
        TailnetCredentials.DialTarget({"owner": "alice"}, port)


def test_frozen_transport_rejects_mutable_descriptor():
    with pytest.raises(ValueError):
        TailnetCredentials.FrozenTransport({"ca": bytearray(b"mutable")}, None)


def test_tombstone_projection_can_represent_the_real_sidecar_shape():
    row = TailnetCredentials.MemberProjection("alice", "node", "master", "server", (), "", "tombstone", 1)
    assert row.address == ""


def test_descriptor_version_cannot_retain_a_mutable_equality_object(tmp_path):
    context = Context(tmp_path / "home")
    descriptor = context.descriptor(TailnetCredentials.DialTarget(context.pin("tailnet-A"), 7449))

    class MutableVersion:
        def __eq__(self, other):
            return True

    with pytest.raises(ValueError):
        TailnetCredentials.Descriptor(MutableVersion(), "tailnet-A",
            TailnetCredentials.DialTarget(context.pin("tailnet-A"), 7449),
            descriptor["origin"], descriptor["serverName"], context.ca.pem, descriptor["caSha256"])


@pytest.mark.parametrize("mutation", ["symlink", "hardlink", "fifo", "mode", "directory", "ancestor"])
def test_read_rejects_unsafe_active_paths(tmp_path, mutation):
    from hyprial.identity import TailnetCredentialError
    import os

    context = Context(tmp_path / "home")
    runtime = context.runtime()
    path = state(context.home) / "active.json"
    path.parent.mkdir(parents=True, mode=0o700)
    for parent in path.parents:
        if parent == context.home:
            break
        parent.chmod(0o700)
    if mutation in ("symlink", "hardlink"):
        target = tmp_path / "outside"
        target.write_bytes(b"outside")
        target.chmod(0o600)
        path.symlink_to(target) if mutation == "symlink" else os.link(target, path)
    elif mutation == "fifo":
        os.mkfifo(path, 0o600)
    elif mutation == "directory":
        path.mkdir(mode=0o700)
    else:
        path.write_bytes(b"{}")
        path.chmod(0o644 if mutation == "mode" else 0o600)
        if mutation == "ancestor":
            path.parent.chmod(0o755)
    with pytest.raises(TailnetCredentialError) as caught:
        runtime.read()
    assert caught.value.code == "TAILNET_STORAGE_UNSAFE"


def test_frozen_mtls_generation_rejects_bool_and_nested_values():
    with pytest.raises(ValueError):
        TailnetCredentials.MtlsReference(True, *(["a" * 64] * 4))
    context_pin = {"owner": []}
    with pytest.raises(ValueError):
        TailnetCredentials.DialTarget(context_pin, 7449)


def test_malformed_active_is_rejected_instead_of_absent_or_an_observed_active(tmp_path):
    from hyprial.identity import TailnetCredentialError
    context = Context(tmp_path / "home")
    path = state(context.home) / "active.json"
    path.parent.mkdir(parents=True, mode=0o700)
    for parent in path.parents:
        if parent == context.home:
            break
        parent.chmod(0o700)
    path.write_bytes(b'{"tailnetId":"wrong-tailnet"}')
    path.chmod(0o600)
    for method in (context.runtime().read, context.runtime().status):
        with pytest.raises(TailnetCredentialError) as caught:
            method()
        assert caught.value.code == "TAILNET_SLOT_INVALID"


def test_bound_home_disappearance_is_not_an_unconfigured_slot(tmp_path):
    from hyprial.identity import TailnetCredentialError
    context = Context(tmp_path / "home")
    runtime = context.runtime()
    assert runtime.read() is None  # True slot absence remains valid.
    context.home.rename(tmp_path / "moved-home")
    with pytest.raises(TailnetCredentialError) as caught:
        runtime.read()
    assert caught.value.code == "TAILNET_HOME_MISMATCH"


@pytest.mark.parametrize("port", [True, 0, 65536])
def test_invalid_remote_port_is_rejected_with_an_otherwise_valid_full_pin(tmp_path, port):
    context = Context(tmp_path / "home")
    with pytest.raises(ValueError):
        TailnetCredentials.DialTarget(context.pin("tailnet-A"), port)


def _synthetic_stat(mode: int, *, uid: int = 0, nlink: int = 1):
    import os

    return os.stat_result((mode, 1, 1, nlink, uid, 0, 0, 0, 0, 0))


def test_file_safe_accepts_non_posix_mode_when_private_metadata_passes(tmp_path, monkeypatch):
    import stat
    from hyprial.identity.impl.device.tailnet.runtime import slot as slot_module

    target = tmp_path / "active.json"
    metadata = _synthetic_stat(stat.S_IFREG | 0o666, uid=999999, nlink=1)
    calls = []

    def fake_violation(path, observed, *, mode):
        calls.append((path, observed, mode))
        return None

    monkeypatch.setattr(slot_module, "private_metadata_violation", fake_violation)
    slot_module.Slot._file_safe(target, metadata, 0o600)
    assert calls == [(target, metadata, 0o600)]


@pytest.mark.parametrize(
    "failure",
    [
        "unsafe-acl",
        "wrong-owner",
        "unsafe-mode",
        OSError("cannot inspect security descriptor"),
    ],
)
def test_file_safe_rejects_private_metadata_failure(tmp_path, monkeypatch, failure):
    import stat
    from hyprial.identity import TailnetCredentialError
    from hyprial.identity.impl.device.tailnet.runtime import slot as slot_module

    target = tmp_path / "active.json"
    metadata = _synthetic_stat(stat.S_IFREG | 0o600, uid=1000, nlink=1)

    def fake_violation(_path, _observed, *, mode):
        del mode
        if isinstance(failure, BaseException):
            raise failure
        return failure

    monkeypatch.setattr(slot_module, "private_metadata_violation", fake_violation)
    with pytest.raises(TailnetCredentialError) as caught:
        slot_module.Slot._file_safe(target, metadata, 0o600)
    assert caught.value.code == "TAILNET_STORAGE_UNSAFE"


def test_file_safe_rejects_private_access_unsupported(tmp_path, monkeypatch):
    import stat
    from hyprial.identity import TailnetCredentialError
    from hyprial.identity.impl.device.tailnet.runtime import slot as slot_module
    from hyprial.kernel import PrivateAccessUnsupported

    target = tmp_path / "active.json"
    metadata = _synthetic_stat(stat.S_IFREG | 0o600, uid=1000, nlink=1)

    def fake_violation(_path, _observed, *, mode):
        del mode
        raise PrivateAccessUnsupported("unsafe-acl")

    monkeypatch.setattr(slot_module, "private_metadata_violation", fake_violation)
    with pytest.raises(TailnetCredentialError) as caught:
        slot_module.Slot._file_safe(target, metadata, 0o600)
    assert caught.value.code == "TAILNET_STORAGE_UNSAFE"


@pytest.mark.parametrize(
    ("mode", "nlink"),
    [
        (0o040700, 1),
        (0o120777, 1),
        (0o100600, 2),
    ],
)
def test_file_safe_rejects_non_regular_or_hardlinked_even_when_metadata_passes(
    tmp_path, monkeypatch, mode, nlink
):
    from hyprial.identity import TailnetCredentialError
    from hyprial.identity.impl.device.tailnet.runtime import slot as slot_module

    target = tmp_path / "active.json"
    metadata = _synthetic_stat(mode, uid=1000, nlink=nlink)
    monkeypatch.setattr(slot_module, "private_metadata_violation", lambda *_a, **_kw: None)
    with pytest.raises(TailnetCredentialError) as caught:
        slot_module.Slot._file_safe(target, metadata, 0o600)
    assert caught.value.code == "TAILNET_STORAGE_UNSAFE"


def test_file_safe_enforces_posix_mode_and_owner_when_posix_metadata_active(
    tmp_path, monkeypatch
):
    import stat
    from hyprial.identity import TailnetCredentialError
    from hyprial.identity.impl.device.tailnet.runtime import slot as slot_module

    target = tmp_path / "active.json"
    monkeypatch.setattr(
        "hyprial.kernel.impl.manysys.fs.private_access.posix_metadata_enforced",
        lambda: True,
    )
    monkeypatch.setattr(
        "hyprial.kernel.impl.manysys.fs.private_access.current_uid",
        lambda: 1000,
    )
    slot_module.Slot._file_safe(
        target, _synthetic_stat(stat.S_IFREG | 0o600, uid=1000, nlink=1), 0o600
    )
    for bad in (
        _synthetic_stat(stat.S_IFREG | 0o644, uid=1000, nlink=1),
        _synthetic_stat(stat.S_IFREG | 0o600, uid=2000, nlink=1),
    ):
        with pytest.raises(TailnetCredentialError) as caught:
            slot_module.Slot._file_safe(target, bad, 0o600)
        assert caught.value.code == "TAILNET_STORAGE_UNSAFE"


def test_store_preflight_passes_target_path_to_file_safe(tmp_path):
    from hyprial.identity.impl.device.tailnet.authority.store import DATABASE, Store

    database = tmp_path / DATABASE
    database.write_bytes(b"sqlite")
    calls = []

    class RecordingSlot:
        state = ("state",)

        @staticmethod
        def _file_safe(path, metadata, mode):
            calls.append((path, metadata, mode))

    Store(RecordingSlot(), "a" * 64)._preflight(tmp_path)
    assert len(calls) == 1
    assert calls[0][0] == database
    assert calls[0][2] == 0o600


def _lock_slot(tmp_path):
    from hyprial.identity.impl.device.tailnet.runtime.slot import Slot

    context = Context(tmp_path / "home")
    slot = Slot(context.home, "tailnet-A")
    with slot.directory(slot.state, create=True) as directory:
        return slot, directory / "operation.lock"


def _record_lock_hardening(monkeypatch, failure=None):
    from hyprial.kernel.impl.manysys.fs import private_access

    real = private_access._verify_after_hardening
    calls = []

    def verify(path, mode):
        if path.name == "operation.lock":
            calls.append((path, mode))
            if failure is not None:
                raise failure
        real(path, mode)

    monkeypatch.setattr(private_access, "_verify_after_hardening", verify)
    return calls


def test_locked_hardens_operation_lock_before_body(tmp_path, monkeypatch):
    slot, path = _lock_slot(tmp_path)
    calls = _record_lock_hardening(monkeypatch)
    with slot.locked():
        assert calls == [(path, 0o600)]
    assert calls == [(path, 0o600)]


@pytest.mark.skipif(not posix_metadata_enforced(), reason="POSIX mode bits only")
@pytest.mark.parametrize("initial", [0o600, 0o644])
def test_locked_hardens_existing_owned_lock_on_posix(tmp_path, monkeypatch, initial):
    slot, path = _lock_slot(tmp_path)
    path.write_bytes(b"")
    path.chmod(initial)
    calls = _record_lock_hardening(monkeypatch)
    entered = []
    with slot.locked():
        assert calls == [(path, 0o600)]
        assert stat.S_IMODE(path.lstat().st_mode) == 0o600
        entered.append(True)
    assert entered == [True]
    assert stat.S_IMODE(path.lstat().st_mode) == 0o600


@pytest.mark.parametrize("failure", ["OSError", "PrivateAccessUnsupported"])
def test_locked_rejects_and_releases_when_lock_hardening_fails(tmp_path, monkeypatch, failure):
    from hyprial.identity import TailnetCredentialError

    slot, path = _lock_slot(tmp_path)
    error = OSError("descriptor rejected") if failure == "OSError" else PrivateAccessUnsupported("unsafe-acl")
    calls = _record_lock_hardening(monkeypatch, error)
    entered = []
    with pytest.raises(TailnetCredentialError) as caught:
        with slot.locked():
            entered.append(True)
    assert caught.value.code == "TAILNET_STORAGE_UNSAFE"
    assert entered == []
    assert calls == [(path, 0o600)]
    assert ProcessFileLock(path).probe() == "free"
    monkeypatch.undo()
    with slot.locked():
        entered.append(True)
    assert entered == [True]


@pytest.mark.parametrize(
    "refusal",
    ["symlink", "hardlink", "wrong-owner", "unsafe-reparse", "unsupported-private-acl", "inspection-error"],
)
def test_locked_refuses_lock_it_does_not_own_without_hardening(tmp_path, monkeypatch, refusal):
    from hyprial.identity import TailnetCredentialError
    from hyprial.identity.impl.device.tailnet.runtime import slot as slot_module

    slot, path = _lock_slot(tmp_path)
    hardened = []
    monkeypatch.setattr(slot_module, "harden_private_path", lambda target, *, mode: hardened.append((target, mode)))
    if refusal in ("symlink", "hardlink"):
        target = tmp_path / "outside.lock"
        target.write_bytes(b"")
        target.chmod(0o600)
        path.symlink_to(target) if refusal == "symlink" else os.link(target, path)
    else:
        real = slot_module.private_metadata_violation

        def verdict(observed_path, metadata, *, mode):
            if observed_path.name != "operation.lock":
                return real(observed_path, metadata, mode=mode)
            if refusal == "inspection-error":
                raise OSError("cannot inspect security descriptor")
            return refusal

        monkeypatch.setattr(slot_module, "private_metadata_violation", verdict)
    entered = []
    with pytest.raises(TailnetCredentialError) as caught:
        with slot.locked():
            entered.append(True)
    assert caught.value.code == "TAILNET_STORAGE_UNSAFE"
    assert entered == []
    assert hardened == []


@pytest.mark.skipif(not posix_metadata_enforced(), reason="removing a held lock file is POSIX only")
def test_locked_refuses_a_lock_file_removed_after_acquisition(tmp_path, monkeypatch):
    from pathlib import Path

    from hyprial.identity import TailnetCredentialError

    slot, path = _lock_slot(tmp_path)
    real_lstat = Path.lstat
    removed = []

    def remove_then_lstat(self):
        if self == path and not removed:
            path.unlink()
            removed.append(True)
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", remove_then_lstat)
    with pytest.raises(TailnetCredentialError) as caught:
        with slot.locked():
            pass
    assert caught.value.code == "TAILNET_STORAGE_UNSAFE"
    assert removed == [True]
    assert ProcessFileLock(path).probe() == "free"
