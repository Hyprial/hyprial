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


class _WindowsLikeOs:
    """The parts of ``os`` that exist on Windows for this module's path."""

    O_RDONLY = os.O_RDONLY
    supports_dir_fd = frozenset()


def test_import_time_flags_do_not_require_posix_only_constants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native_credential, "os", _WindowsLikeOs)

    assert native_credential._directory_flags() == os.O_RDONLY
    assert native_credential._private_descriptor_io() is False


def test_writers_refuse_instead_of_attempting_a_path_based_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.identity.impl.agents.home.native_credential import (
        install_native_credential_set,
        remove_native_credentials,
    )

    monkeypatch.setattr(native_credential, "_PRIVATE_DESCRIPTOR_IO", False)
    home = _home(tmp_path)
    calls = (
        lambda: install_native_credential(
            home, "claude", ".credentials.json", PAYLOAD
        ),
        lambda: install_native_credential_set(
            home, "claude", [(".credentials.json", PAYLOAD)]
        ),
        lambda: remove_native_credentials(home, "claude"),
    )
    for call in calls:
        with pytest.raises(NativeCredentialError) as refused:
            call()
        assert refused.value.code == ipc_errors.UNSUPPORTED
    assert not native_secrets_root(home, "claude").exists()
