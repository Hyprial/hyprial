"""Positive local-volume detection for private filesystem coordination.

Unknown, FUSE and network filesystems fail closed. This reports mount facts,
not whether a caller is authorized to use the path. It never mounts volumes.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import re
import sys


def _linux(path: Path) -> bool:
    selected: tuple[int, str] | None = None
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, separator, right = line.partition(" - ")
        fields = left.split()
        if not separator or len(fields) < 6 or len(right.split()) < 3:
            return False
        mount = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4]))
        if path.is_relative_to(mount):
            length = len(mount.parts)
            if selected is None or length >= selected[0]:
                selected = length, right.split()[0]
    return selected is not None and selected[1] in {
        "ext2", "ext3", "ext4", "xfs", "btrfs", "tmpfs", "ramfs", "zfs", "f2fs",
    }


def _darwin(path: Path) -> bool:
    class StatFS(ctypes.Structure):
        _fields_ = [
            ("bsize", ctypes.c_uint32), ("iosize", ctypes.c_int32),
            ("blocks", ctypes.c_uint64), ("bfree", ctypes.c_uint64),
            ("bavail", ctypes.c_uint64), ("files", ctypes.c_uint64),
            ("ffree", ctypes.c_uint64), ("fsid", ctypes.c_int32 * 2),
            ("owner", ctypes.c_uint32), ("type", ctypes.c_uint32),
            ("flags", ctypes.c_uint32), ("subtype", ctypes.c_uint32),
            ("fstype", ctypes.c_char * 16), ("mount", ctypes.c_char * 1024),
            ("source", ctypes.c_char * 1024), ("reserved", ctypes.c_uint32 * 8),
        ]
    statfs = ctypes.CDLL(None, use_errno=True).statfs
    statfs.argtypes = [ctypes.c_char_p, ctypes.POINTER(StatFS)]
    statfs.restype = ctypes.c_int
    facts = StatFS()
    return (statfs(os.fsencode(path), ctypes.byref(facts)) == 0
            and bool(facts.flags & 0x1000) and facts.fstype in {b"apfs", b"hfs", b"ufs"})


def _windows(path: Path) -> bool:
    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    get_path = api.GetVolumePathNameW
    get_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    get_path.restype = wintypes.BOOL
    get_type = api.GetDriveTypeW
    get_type.argtypes = [wintypes.LPCWSTR]
    get_type.restype = wintypes.UINT
    get_info = api.GetVolumeInformationW
    get_info.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
                        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
    get_info.restype = wintypes.BOOL
    root, kind = ctypes.create_unicode_buffer(32768), ctypes.create_unicode_buffer(256)
    if not get_path(str(path), root, len(root)) or get_type(root.value) not in {3, 6}:
        return False
    return (bool(get_info(root.value, None, 0, None, None, None, kind, len(kind)))
            and kind.value in {"NTFS", "ReFS", "FAT32", "exFAT"})


def local_filesystem(path: Path) -> bool:
    """Return true only for an existing path on an identified local volume."""
    try:
        canonical = Path(path).resolve(strict=True)
        probe = {"linux": _linux, "darwin": _darwin, "win32": _windows}.get(sys.platform)
        return probe(canonical) if probe is not None else False
    except (OSError, ValueError, AttributeError):
        return False
