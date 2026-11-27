import hashlib
import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.terminals.acquisition.lock import HcAcquisitionError
from hyprial.daemon.impl.terminals.acquisition.lock import HcLock
from hyprial.daemon.impl.terminals.acquisition.lock import load_bundled_hc_lock
from hyprial.kernel import cleanup_private_hc_runtime_dirs
from hyprial.kernel import hc_bin_root
from hyprial.kernel import hc_core_root
from hyprial.kernel import hc_core_socket_path
from hyprial.kernel import hc_session_name
from hyprial.kernel import hc_state_root
from hyprial.kernel import hc_version_root
from hyprial.kernel import host_hc_release_target


def test_bundled_lock_has_all_verified_targets() -> None:
    lock = load_bundled_hc_lock()
    assert lock.version == "0.1.22"
    assert lock.generation == 1
    assert set(lock.targets) == {
        "linux-aarch64",
        "linux-x86_64",
        "macos-aarch64",
        "macos-x86_64",
        "windows-x86_64",
    }
    for target in lock.targets.values():
        assert len(target.sha256) == 64
        assert target.sha256 == target.sha256.lower()
        assert all(char in "0123456789abcdef" for char in target.sha256)
        assert target.size > 0


def test_lock_rejects_invalid_entries() -> None:
    raw = {
        "version": "0.1.22",
        "generation": 1,
        "targets": {
            "linux-x86_64": {
                "url": "https://example.test/hc.tar.gz",
                "sha256": "not-a-digest",
                "size": 0,
            }
        },
    }
    with pytest.raises(HcAcquisitionError):
        HcLock.from_mapping(raw)


def test_hc_paths_and_version_validation(tmp_path: Path) -> None:
    assert hc_state_root(tmp_path) == tmp_path.resolve() / "state" / "hc"
    assert hc_bin_root(tmp_path) == tmp_path.resolve() / "state" / "hc" / "bin"
    assert hc_version_root("0.1.22", tmp_path) == hc_bin_root(tmp_path) / "0.1.22"
    assert hc_core_root(tmp_path) == tmp_path.resolve() / "state" / "hc" / "core"
    assert hc_core_socket_path(tmp_path) == hc_core_root(tmp_path) / "hc.sock"
    with pytest.raises(ValueError):
        hc_version_root("../escape", tmp_path)
    with pytest.raises(ValueError):
        hc_version_root("C:escape", tmp_path)


def test_session_name_is_stable_and_home_specific(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    assert hc_session_name(first) == hc_session_name(Path(str(first) + "/"))
    assert hc_session_name(first).startswith("hyprial-")
    assert len(hc_session_name(first)) == 20
    assert hc_session_name(first) != "default"
    assert hc_session_name(first) != hc_session_name(second)


def test_host_target_mapping() -> None:
    assert host_hc_release_target("Darwin", "arm64") == "macos-aarch64"
    assert host_hc_release_target("darwin", "x86_64") == "macos-x86_64"
    assert host_hc_release_target("Linux", "aarch64") == "linux-aarch64"
    assert host_hc_release_target("linux", "amd64") == "linux-x86_64"
    assert host_hc_release_target("Windows", "amd64") == "windows-x86_64"
    assert host_hc_release_target("Windows", "arm64") is None
    assert host_hc_release_target("Plan9", "x86_64") is None


def test_cleanup_runtime_dirs_is_session_scoped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    owned = runtime / "hc-serve-owned"
    other = runtime / "hc-serve-other"
    ignored = runtime / "unrelated"
    for path in (owned, other, ignored):
        path.mkdir()
    (owned / "env").write_text(f"HC_SESSION={hc_session_name(tmp_path)}\n", encoding="utf-8")
    (other / "env").write_text("HC_SESSION=another-session\n", encoding="utf-8")
    (ignored / "env").write_text(f"HC_SESSION={hc_session_name(tmp_path)}\n", encoding="utf-8")
    monkeypatch.setattr("hyprial.kernel.impl.manysys.process.ports.sys.platform", "linux")
    failures = cleanup_private_hc_runtime_dirs(hc_session_name(tmp_path), runtime)
    assert failures == ()
    assert not owned.exists()
    assert other.exists()
    assert ignored.exists()


def test_lock_json_is_serializable() -> None:
    lock = load_bundled_hc_lock()
    encoded = json.dumps(lock.to_mapping(), sort_keys=True)
    assert hashlib.sha256(encoded.encode()).hexdigest()
