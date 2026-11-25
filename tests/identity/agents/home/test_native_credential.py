"""RED-first tests for installing a harness-native credential into an agent home.

The rules asserted here come from the read side: the claude runtime rejects a
symlinked, non-regular, empty, or non-0600 credentials file, and the runtime
projects ``secrets/native/<harness>/`` into the isolated HOME.  A writer that
does not hold those rules produces agents that still fail to start.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from hyprial.kernel import ipc_errors

from hyprial.identity.impl.agents.home import native_credential
from hyprial.identity.impl.agents.home.native_credential import (
    NATIVE_CREDENTIAL_FILES,
    NativeCredentialError,
    install_native_credential,
    native_secrets_root,
)

PAYLOAD = b'{"claudeAiOauth": {"accessToken": "test-only"}}\n'


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "agent-home"
    home.mkdir(mode=0o700)
    return home


def test_installs_a_private_regular_file_at_the_canonical_path(tmp_path: Path) -> None:
    home = _home(tmp_path)

    receipt = install_native_credential(
        home, "claude", ".credentials.json", PAYLOAD
    )

    target = native_secrets_root(home, "claude") / ".credentials.json"
    assert target.read_bytes() == PAYLOAD
    metadata = target.lstat()
    assert stat.S_ISREG(metadata.st_mode)
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert stat.S_IMODE(target.parent.lstat().st_mode) == 0o700
    assert receipt["path"] == str(target)
    # Fact only: the writer must not claim the material is valid.
    assert "valid" not in receipt
    assert receipt["replaced"] is False


def test_refuses_to_overwrite_without_force_and_replaces_with_it(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    install_native_credential(home, "claude", ".credentials.json", PAYLOAD)

    with pytest.raises(NativeCredentialError) as refused:
        install_native_credential(home, "claude", ".credentials.json", b"second")

    assert refused.value.code == ipc_errors.PROVIDER_CREDENTIAL_EXISTS
    target = native_secrets_root(home, "claude") / ".credentials.json"
    assert target.read_bytes() == PAYLOAD

    receipt = install_native_credential(
        home, "claude", ".credentials.json", b"second", force=True
    )
    assert target.read_bytes() == b"second"
    assert stat.S_IMODE(target.lstat().st_mode) == 0o600
    assert receipt["replaced"] is True


def test_refuses_empty_content_and_unknown_provider(tmp_path: Path) -> None:
    home = _home(tmp_path)

    with pytest.raises(NativeCredentialError) as empty:
        install_native_credential(home, "claude", ".credentials.json", b"")
    assert empty.value.code == ipc_errors.INVALID_ARGUMENT

    with pytest.raises(NativeCredentialError) as unknown:
        install_native_credential(home, "not-a-harness", "auth.json", PAYLOAD)
    assert unknown.value.code == ipc_errors.PROVIDER_UNSUPPORTED
    assert not native_secrets_root(home, "not-a-harness").exists()


def test_only_the_names_the_runtime_reads_may_be_installed(tmp_path: Path) -> None:
    home = _home(tmp_path)

    with pytest.raises(NativeCredentialError) as refused:
        install_native_credential(home, "claude", "settings.json", PAYLOAD)

    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert NATIVE_CREDENTIAL_FILES["claude"] == frozenset({".credentials.json"})
    assert NATIVE_CREDENTIAL_FILES["pi"] == frozenset(
        {"auth.json", "models-store.json"}
    )
    assert NATIVE_CREDENTIAL_FILES["agy"] == frozenset({"antigravity-oauth-token"})


def test_agy_native_credential_install_and_remove_allowlist(tmp_path: Path) -> None:
    from hyprial.identity.impl.agents.home.native_credential import (
        install_native_credential_set,
        remove_native_credentials,
    )

    home = _home(tmp_path)
    with pytest.raises(NativeCredentialError) as refused:
        install_native_credential(home, "agy", "auth.json", PAYLOAD)
    assert refused.value.code == ipc_errors.INVALID_ARGUMENT

    receipt = install_native_credential_set(
        home, "agy", [("antigravity-oauth-token", PAYLOAD)]
    )
    target = native_secrets_root(home, "agy") / "antigravity-oauth-token"
    assert target.read_bytes() == PAYLOAD
    assert stat.S_IMODE(target.lstat().st_mode) == 0o600
    assert receipt["provider"] == "agy"

    removed = remove_native_credentials(home, "agy")
    assert removed["removed"] == [str(target)]
    assert not target.exists()


def test_refuses_to_write_through_a_symlinked_target(tmp_path: Path) -> None:
    home = _home(tmp_path)
    root = native_secrets_root(home, "claude")
    root.mkdir(parents=True, mode=0o700)
    for level in (root.parent.parent, root.parent):
        level.chmod(0o700)
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"untouched")
    (root / ".credentials.json").symlink_to(outside)

    with pytest.raises(NativeCredentialError) as refused:
        install_native_credential(home, "claude", ".credentials.json", PAYLOAD)

    assert refused.value.code == ipc_errors.PROVIDER_UNSAFE_PATH
    assert outside.read_bytes() == b"untouched"


def test_a_set_is_checked_whole_before_the_first_write(tmp_path: Path) -> None:
    from hyprial.identity.impl.agents.home.native_credential import (
        install_native_credential_set,
    )

    home = _home(tmp_path)
    root = native_secrets_root(home, "pi")
    root.mkdir(parents=True, mode=0o700)
    for level in (root.parent.parent, root.parent):
        level.chmod(0o700)
    (root / "models-store.json").write_bytes(b"older store")
    (root / "models-store.json").chmod(0o600)

    with pytest.raises(NativeCredentialError) as refused:
        install_native_credential_set(
            home,
            "pi",
            [("auth.json", b'{"auth": 1}'), ("models-store.json", b'{"store": 1}')],
        )

    assert refused.value.code == ipc_errors.PROVIDER_CREDENTIAL_EXISTS
    assert not (root / "auth.json").exists()
    assert (root / "models-store.json").read_bytes() == b"older store"


def test_a_set_must_name_every_file_the_harness_reads(tmp_path: Path) -> None:
    from hyprial.identity.impl.agents.home.native_credential import (
        install_native_credential_set,
    )

    home = _home(tmp_path)

    with pytest.raises(NativeCredentialError) as refused:
        install_native_credential_set(home, "pi", [("auth.json", b'{"auth": 1}')])

    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert not native_secrets_root(home, "pi").exists()


def test_a_set_writes_every_file_and_reports_each(tmp_path: Path) -> None:
    from hyprial.identity.impl.agents.home.native_credential import (
        install_native_credential_set,
    )

    home = _home(tmp_path)

    receipt = install_native_credential_set(
        home,
        "pi",
        [("auth.json", b'{"auth": 1}'), ("models-store.json", b'{"store": 1}')],
    )

    root = native_secrets_root(home, "pi")
    assert (root / "auth.json").read_bytes() == b'{"auth": 1}'
    assert (root / "models-store.json").read_bytes() == b'{"store": 1}'
    for name in ("auth.json", "models-store.json"):
        assert stat.S_IMODE((root / name).lstat().st_mode) == 0o600
    assert receipt["provider"] == "pi"
    assert [item["path"] for item in receipt["files"]] == [
        str(root / "auth.json"),
        str(root / "models-store.json"),
    ]
    assert "valid" not in receipt


def test_rerunning_with_identical_bytes_is_unchanged_not_refused(
    tmp_path: Path,
) -> None:
    from hyprial.identity.impl.agents.home.native_credential import (
        install_native_credential_set,
    )

    home = _home(tmp_path)
    install_native_credential_set(home, "claude", [(".credentials.json", PAYLOAD)])
    target = native_secrets_root(home, "claude") / ".credentials.json"
    before = target.stat().st_mtime_ns

    receipt = install_native_credential_set(
        home, "claude", [(".credentials.json", PAYLOAD)]
    )

    assert receipt["files"][0]["unchanged"] is True
    assert receipt["files"][0]["replaced"] is False
    assert target.stat().st_mtime_ns == before


def test_removal_deletes_only_the_files_the_harness_reads(tmp_path: Path) -> None:
    from hyprial.identity.impl.agents.home.native_credential import (
        install_native_credential_set,
        remove_native_credentials,
    )

    home = _home(tmp_path)
    install_native_credential_set(
        home,
        "pi",
        [("auth.json", b'{"auth": 1}'), ("models-store.json", b'{"store": 1}')],
    )
    root = native_secrets_root(home, "pi")
    (root / "settings.json").write_bytes(b"kept")

    receipt = remove_native_credentials(home, "pi")

    assert receipt["removed"] == [str(root / "auth.json"), str(root / "models-store.json")]
    assert receipt["absent"] == []
    assert not (root / "auth.json").exists()
    assert not (root / "models-store.json").exists()
    assert (root / "settings.json").read_bytes() == b"kept"

    again = remove_native_credentials(home, "pi")
    assert again["removed"] == []
    assert len(again["absent"]) == 2


def test_removal_refuses_an_unknown_provider(tmp_path: Path) -> None:
    from hyprial.identity.impl.agents.home.native_credential import (
        remove_native_credentials,
    )

    with pytest.raises(NativeCredentialError) as refused:
        remove_native_credentials(_home(tmp_path), "not-a-harness")

    assert refused.value.code == ipc_errors.PROVIDER_UNSUPPORTED


def test_identity_facade_exposes_the_native_credential_surface() -> None:
    """The daemon imports these names through the facade while starting."""

    from hyprial.identity import NATIVE_CREDENTIAL_FILES as facade_files

    assert facade_files is NATIVE_CREDENTIAL_FILES


def test_portable_operations_succeed_without_posix_dir_fd_primitives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from hyprial.identity.impl.agents.home.native_credential import (
        inspect_native_credentials,
        install_native_credential_set,
        prepare_legacy_claude_native_root,
        read_native_credential_set,
        remove_native_credentials,
    )

    windows_like_os = SimpleNamespace(
        O_RDONLY=os.O_RDONLY,
        O_WRONLY=os.O_WRONLY,
        O_CREAT=os.O_CREAT,
        O_EXCL=os.O_EXCL,
        supports_dir_fd=frozenset(),
        open=os.open,
        write=os.write,
        fsync=os.fsync,
        close=os.close,
        fstat=os.fstat,
        rename=os.rename,
        replace=os.replace,
    )
    monkeypatch.setattr(native_credential, "os", windows_like_os)
    home = _home(tmp_path)
    root, provisioned = prepare_legacy_claude_native_root(home)
    assert root == native_secrets_root(home, "claude")
    assert provisioned is True

    install_native_credential_set(home, "claude", [(".credentials.json", PAYLOAD)])
    assert read_native_credential_set(home, "claude") == [(".credentials.json", PAYLOAD)]
    observed = inspect_native_credentials(home, "claude")
    assert observed["state"] == "stored"
    assert observed["files"] == [".credentials.json"]
    _, still_provisioned = prepare_legacy_claude_native_root(home)
    assert still_provisioned is False

    removed = remove_native_credentials(home, "claude")
    assert removed["removed"] == [str(root / ".credentials.json")]
    assert inspect_native_credentials(home, "claude")["state"] == "absent"


def test_refuses_windows_reparse_point_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from hyprial.identity.impl.agents.home.native_credential import (
        inspect_native_credentials,
        install_native_credential_set,
        read_native_credential_set,
    )

    home = _home(tmp_path)
    install_native_credential_set(home, "claude", [(".credentials.json", PAYLOAD)])
    target = native_secrets_root(home, "claude") / ".credentials.json"
    real_lstat = Path.lstat

    def reparse_lstat(self: Path) -> os.stat_result:
        metadata = real_lstat(self)
        if self == target:
            return SimpleNamespace(  # type: ignore[return-value]
                st_mode=metadata.st_mode,
                st_size=metadata.st_size,
                st_file_attributes=0x400,
            )
        return metadata

    monkeypatch.setattr(Path, "lstat", reparse_lstat)
    for operation in (
        lambda: install_native_credential_set(home, "claude", [(".credentials.json", PAYLOAD)], force=True),
        lambda: read_native_credential_set(home, "claude"),
        lambda: inspect_native_credentials(home, "claude"),
    ):
        with pytest.raises(NativeCredentialError) as refused:
            operation()
        assert refused.value.code == ipc_errors.PROVIDER_UNSAFE_PATH


def test_previous_cleanup_oserror_raises_daemon_error_and_clears_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno
    import sqlite3
    from threading import RLock
    from types import SimpleNamespace
    from hyprial.identity.impl.agents.home import native_imports
    from hyprial.identity.impl.agents.home.native_credential import (
        inspect_native_credentials,
        install_native_credential_set,
    )
    from hyprial.identity.impl.agents.home.native_imports import NativeImportRecord

    home = _home(tmp_path)
    clock = [1000]
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        "CREATE TABLE agents (actor TEXT NOT NULL, entity_token TEXT NOT NULL, PRIMARY KEY(actor, entity_token))"
    )
    connection.execute("INSERT INTO agents VALUES ('agent-1', 'entity-1')")
    native_imports.initialize(connection)
    receipt = SimpleNamespace(
        actor="agent-1",
        path=str(home),
        entity_token="entity-1",
        resource_token="home-1",
    )
    registry = SimpleNamespace(
        _db=connection,
        _lock=RLock(),
        home_receipt=lambda _actor, require_ready=False: receipt,
        require=lambda _actor: SimpleNamespace(entity_token="entity-1"),
        now_ms=lambda: clock[0],
    )
    record = NativeImportRecord(registry, "agent-1")

    initial = [("auth.json", b'{"auth": "old-secret"}'), ("models-store.json", b'{"store": "old-secret"}')]
    replacement = [("auth.json", b'{"auth": "new-secret"}'), ("models-store.json", b'{"store": "new-secret"}')]
    install_native_credential_set(home, "pi", initial, import_record=record)
    assert inspect_native_credentials(home, "pi", import_record=record)["importedAt"] == 1000

    root = native_secrets_root(home, "pi")
    locked_backup = root / "models-store.json.previous"
    promoted_store = root / "models-store.json"
    real_unlink = Path.unlink

    def locked_previous_unlink(self: Path, missing_ok: bool = False) -> None:
        if (
            self == locked_backup
            and promoted_store.exists()
            and promoted_store.read_bytes() == b'{"store": "new-secret"}'
        ):
            raise PermissionError(errno.EACCES, "Sharing violation", str(self))
        real_unlink(self, missing_ok=missing_ok)

    clock[0] = 2000
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", locked_previous_unlink)
        with pytest.raises(NativeCredentialError) as refused:
            install_native_credential_set(
                home, "pi", replacement, force=True, import_record=record
            )

    assert refused.value.code == ipc_errors.DAEMON_ERROR
    assert "models-store.json.previous" in refused.value.message
    assert "old-secret" not in refused.value.message
    assert "new-secret" not in refused.value.message
    assert (root / "auth.json").read_bytes() == b'{"auth": "new-secret"}'
    assert promoted_store.read_bytes() == b'{"store": "new-secret"}'
    assert not (root / "auth.json.previous").exists()
    assert locked_backup.read_bytes() == b'{"store": "old-secret"}'
    observed = inspect_native_credentials(home, "pi", import_record=record)
    assert observed["state"] == "partial"
    assert observed["importedAt"] is None

    clock[0] = 3000
    install_native_credential_set(
        home, "pi", replacement, force=True, import_record=record
    )
    recovered = inspect_native_credentials(home, "pi", import_record=record)
    assert recovered["state"] == "stored"
    assert recovered["importedAt"] == 3000
    assert not locked_backup.exists()
    connection.close()
