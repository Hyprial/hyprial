"""Only positively identified local filesystems support durable session claims."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.kernel import manysys
from hyprial.kernel.impl.manysys.fs import locality


def test_native_local_directory(tmp_path):
    assert manysys.local_filesystem(tmp_path) is True
    assert manysys.local_filesystem(tmp_path / "absent") is False


def test_linux_mount_selection_and_unknown_fail_closed(tmp_path, monkeypatch):
    root = str(tmp_path.resolve()).replace(" ", "\\040")
    monkeypatch.setattr(locality.sys, "platform", "linux")
    mountinfo = f"1 0 0:1 / / rw - ext4 /dev/sda rw\n2 1 0:2 / {root} rw - nfs host:/home rw\n"
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw: mountinfo)
    assert manysys.local_filesystem(tmp_path) is False
    mountinfo = mountinfo.replace("nfs host:/home", "tmpfs tmpfs")
    assert manysys.local_filesystem(tmp_path) is True
    mountinfo = mountinfo.replace("tmpfs tmpfs", "fuse.sshfs remote")
    assert manysys.local_filesystem(tmp_path) is False
    mountinfo = "malformed"
    assert manysys.local_filesystem(tmp_path) is False


def test_probe_failure_and_unknown_platform_are_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(locality.sys, "platform", "unrecognized")
    assert manysys.local_filesystem(tmp_path) is False


@pytest.mark.parametrize("drive,fs,query,expected", [(3, "NTFS", True, True), (4, "NTFS", True, False), (3, "FUSE", True, False), (3, "NTFS", False, False)])
def test_windows_requires_local_drive_and_identified_volume(tmp_path, monkeypatch, drive, fs, query, expected):
    class Function:
        def __init__(self, call):
            self.call = call
        def __call__(self, *args):
            return self.call(*args)

    def get_path(_path, result, _size):
        result.value = "C:\\"
        return query

    def get_info(_root, _label, _size, _serial, _maximum, _flags, result, _length):
        result.value = fs
        return True

    api = SimpleNamespace(GetVolumePathNameW=Function(get_path), GetDriveTypeW=Function(lambda _: drive), GetVolumeInformationW=Function(get_info))
    monkeypatch.setattr(locality.ctypes, "WinDLL", lambda *a, **kw: api, raising=False)
    monkeypatch.setattr(locality.sys, "platform", "win32")
    assert manysys.local_filesystem(tmp_path) is expected
