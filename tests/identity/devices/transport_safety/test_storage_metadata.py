"""Private metadata validation contracts for transport credential storage."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from hyprial.identity import TransportCredentialError
from hyprial.identity.impl.device import transport_storage
from hyprial.kernel import PrivateAccessUnsupported


def _synthetic_stat(mode: int, *, uid: int = 0, nlink: int = 1) -> os.stat_result:
    return os.stat_result((mode, 1, 1, nlink, uid, 0, 0, 0, 0, 0))


def _credential_paths(home: Path) -> tuple[Path, Path, Path, Path, Path]:
    return (
        home / "secrets/transport",
        home / "state/transport",
        home / "secrets/transport/device-key.pem",
        home / "state/transport/credential.json",
        home / "state/transport/trust-bundle.pem",
    )


def _patch_lstat(
    monkeypatch: pytest.MonkeyPatch,
    stat_map: dict[Path, os.stat_result | BaseException],
) -> None:
    real_lstat = os.lstat

    def fake_lstat(target, *args, **kwargs):
        target_path = Path(target)
        if target_path in stat_map:
            value = stat_map[target_path]
            if isinstance(value, BaseException):
                raise value
            return value
        return real_lstat(target, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", fake_lstat)


def test_secure_directory_accepts_non_posix_mode_when_kernel_secures_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "home"
    root.mkdir(mode=0o700)
    target = root / "secrets" / "transport"
    calls: list[tuple[Path, bool]] = []

    def fake_secure(path: Path, *, create: bool = False, mode: int | None = 0o700) -> Path:
        del mode
        calls.append((path, create))
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o755)
        return path

    monkeypatch.setattr(transport_storage, "secure_private_directory", fake_secure)
    transport_storage._secure_directory(target, root=root)
    assert calls == [(root / "secrets", True), (target, True)]


def test_secure_directory_wraps_private_access_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "home"
    target = root / "secrets" / "transport"

    def fake_secure(path: Path, *, create: bool = False, mode: int | None = 0o700) -> Path:
        del path, create, mode
        raise PrivateAccessUnsupported("unsafe-acl")

    monkeypatch.setattr(transport_storage, "secure_private_directory", fake_secure)
    with pytest.raises(TransportCredentialError) as caught:
        transport_storage._secure_directory(target, root=root)
    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"


def test_complete_paths_present_accepts_non_posix_modes_when_metadata_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _credential_paths(tmp_path / "home")
    stat_map: dict[Path, os.stat_result | BaseException] = {
        paths[0]: _synthetic_stat(stat.S_IFDIR | 0o777, uid=999999, nlink=2),
        paths[1]: _synthetic_stat(stat.S_IFDIR | 0o777, uid=999999, nlink=2),
        paths[2]: _synthetic_stat(stat.S_IFREG | 0o666, uid=999999, nlink=1),
        paths[3]: _synthetic_stat(stat.S_IFREG | 0o666, uid=999999, nlink=1),
        paths[4]: _synthetic_stat(stat.S_IFREG | 0o666, uid=999999, nlink=1),
    }
    _patch_lstat(monkeypatch, stat_map)
    calls: list[tuple[Path, int | None]] = []

    def fake_violation(
        path: Path, metadata: os.stat_result, *, mode: int | None
    ) -> str | None:
        del metadata
        calls.append((path, mode))
        return None

    monkeypatch.setattr(transport_storage, "private_metadata_violation", fake_violation)
    assert transport_storage._complete_paths_present(paths) is True
    assert calls == list(zip(paths, (0o700, 0o700, 0o600, 0o600, 0o644), strict=True))


@pytest.mark.parametrize("target_index", range(5))
@pytest.mark.parametrize(
    "failure",
    [
        "unsafe-acl",
        OSError("cannot inspect security descriptor"),
        PrivateAccessUnsupported("unsafe-acl"),
    ],
)
def test_complete_paths_present_rejects_private_metadata_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_index: int,
    failure: object,
) -> None:
    paths = _credential_paths(tmp_path / "home")
    stat_map: dict[Path, os.stat_result | BaseException] = {
        paths[0]: _synthetic_stat(stat.S_IFDIR | 0o777, uid=999999, nlink=1),
        paths[1]: _synthetic_stat(stat.S_IFDIR | 0o777, uid=999999, nlink=1),
        paths[2]: _synthetic_stat(stat.S_IFREG | 0o666, uid=999999, nlink=1),
        paths[3]: _synthetic_stat(stat.S_IFREG | 0o666, uid=999999, nlink=1),
        paths[4]: _synthetic_stat(stat.S_IFREG | 0o666, uid=999999, nlink=1),
    }
    _patch_lstat(monkeypatch, stat_map)

    def fake_violation(
        path: Path, metadata: os.stat_result, *, mode: int | None
    ) -> str | None:
        del metadata, mode
        if path == paths[target_index]:
            if isinstance(failure, BaseException):
                raise failure
            return str(failure)
        return None

    monkeypatch.setattr(transport_storage, "private_metadata_violation", fake_violation)
    with pytest.raises(TransportCredentialError) as caught:
        transport_storage._complete_paths_present(paths)
    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"


@pytest.mark.parametrize(
    ("target_index", "bad_stat"),
    [
        (0, _synthetic_stat(stat.S_IFLNK | 0o777, uid=1000, nlink=1)),
        (0, _synthetic_stat(stat.S_IFREG | 0o700, uid=1000, nlink=1)),
        (2, _synthetic_stat(stat.S_IFLNK | 0o777, uid=1000, nlink=1)),
        (2, _synthetic_stat(stat.S_IFDIR | 0o600, uid=1000, nlink=1)),
        (2, _synthetic_stat(stat.S_IFREG | 0o600, uid=1000, nlink=2)),
        (4, _synthetic_stat(stat.S_IFREG | 0o644, uid=1000, nlink=2)),
    ],
)
def test_complete_paths_present_rejects_symlink_wrong_type_and_hardlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_index: int,
    bad_stat: os.stat_result,
) -> None:
    paths = _credential_paths(tmp_path / "home")
    stat_map: dict[Path, os.stat_result | BaseException] = {
        paths[0]: _synthetic_stat(stat.S_IFDIR | 0o700, uid=1000, nlink=2),
        paths[1]: _synthetic_stat(stat.S_IFDIR | 0o700, uid=1000, nlink=2),
        paths[2]: _synthetic_stat(stat.S_IFREG | 0o600, uid=1000, nlink=1),
        paths[3]: _synthetic_stat(stat.S_IFREG | 0o600, uid=1000, nlink=1),
        paths[4]: _synthetic_stat(stat.S_IFREG | 0o644, uid=1000, nlink=1),
    }
    stat_map[paths[target_index]] = bad_stat
    _patch_lstat(monkeypatch, stat_map)
    monkeypatch.setattr(
        transport_storage, "private_metadata_violation", lambda *_a, **_kw: None
    )
    with pytest.raises(TransportCredentialError) as caught:
        transport_storage._complete_paths_present(paths)
    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"


def test_complete_paths_present_enforces_posix_modes_owner_and_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _credential_paths(tmp_path / "home")
    monkeypatch.setattr(
        "hyprial.kernel.impl.manysys.fs.private_access.posix_metadata_enforced",
        lambda: True,
    )
    monkeypatch.setattr(
        "hyprial.kernel.impl.manysys.fs.private_access.current_uid",
        lambda: 1000,
    )
    valid: dict[Path, os.stat_result | BaseException] = {
        paths[0]: _synthetic_stat(stat.S_IFDIR | 0o700, uid=1000, nlink=2),
        paths[1]: _synthetic_stat(stat.S_IFDIR | 0o700, uid=1000, nlink=2),
        paths[2]: _synthetic_stat(stat.S_IFREG | 0o600, uid=1000, nlink=1),
        paths[3]: _synthetic_stat(stat.S_IFREG | 0o600, uid=1000, nlink=1),
        paths[4]: _synthetic_stat(stat.S_IFREG | 0o644, uid=1000, nlink=1),
    }
    _patch_lstat(monkeypatch, valid)
    assert transport_storage._complete_paths_present(paths) is True

    missing = dict(valid)
    missing[paths[4]] = FileNotFoundError(paths[4])
    _patch_lstat(monkeypatch, missing)
    assert transport_storage._complete_paths_present(paths) is False

    bad_modes = (
        (0, stat.S_IFDIR | 0o755),
        (1, stat.S_IFDIR | 0o755),
        (2, stat.S_IFREG | 0o644),
        (3, stat.S_IFREG | 0o644),
        (4, stat.S_IFREG | 0o600),
    )
    for index, bad_mode in bad_modes:
        mutated = dict(valid)
        mutated[paths[index]] = _synthetic_stat(bad_mode, uid=1000, nlink=1)
        _patch_lstat(monkeypatch, mutated)
        with pytest.raises(TransportCredentialError) as caught:
            transport_storage._complete_paths_present(paths)
        assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"

    for index, expected_type, mode in (
        (0, stat.S_IFDIR, 0o700),
        (2, stat.S_IFREG, 0o600),
        (4, stat.S_IFREG, 0o644),
    ):
        mutated = dict(valid)
        mutated[paths[index]] = _synthetic_stat(expected_type | mode, uid=2000, nlink=1)
        _patch_lstat(monkeypatch, mutated)
        with pytest.raises(TransportCredentialError) as caught:
            transport_storage._complete_paths_present(paths)
        assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"
