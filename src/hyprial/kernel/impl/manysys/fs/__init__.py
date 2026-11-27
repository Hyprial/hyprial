# Decision record (manysys/fs, W1-MANYSYS 2026-11-21):
# - Subpackage aggregating cross-platform filesystem durability, exclusive
#   locks, account/umask helpers, directory links, and private-access fences.
"""Cross-platform filesystem, locking, link, and private-access primitives."""

from __future__ import annotations

from hyprial.kernel.impl.manysys.fs.account import (
    apply_private_umask,
    current_uid,
    host_machine_identifier,
    posix_metadata_enforced,
)
from hyprial.kernel.impl.manysys.fs.durability import fsync_directory
from hyprial.kernel.impl.manysys.fs.file_lock import (
    FileLockBusy,
    ProcessFileLock,
    lock_exclusive,
    unlock,
)
from hyprial.kernel.impl.manysys.fs.links import (
    create_relative_file_symlink,
    is_directory_link,
    link_directory,
    mark_executable,
    unlink_directory_link,
)
from hyprial.kernel.impl.manysys.fs.private_access import (
    PrivateAccessUnsupported,
    append_private_file,
    harden_private_path,
    inspect_private_file,
    open_private_file,
    path_is_reparse,
    private_access_supported,
    private_config_file_violation,
    private_metadata_violation,
    private_tree_violation,
    read_private_file,
    secure_private_directory,
    write_private_file_atomic,
)

__all__ = [
    "FileLockBusy",
    "PrivateAccessUnsupported",
    "ProcessFileLock",
    "append_private_file",
    "apply_private_umask",
    "create_relative_file_symlink",
    "current_uid",
    "fsync_directory",
    "harden_private_path",
    "host_machine_identifier",
    "inspect_private_file",
    "is_directory_link",
    "link_directory",
    "lock_exclusive",
    "mark_executable",
    "open_private_file",
    "path_is_reparse",
    "posix_metadata_enforced",
    "private_access_supported",
    "private_config_file_violation",
    "private_metadata_violation",
    "private_tree_violation",
    "read_private_file",
    "secure_private_directory",
    "unlink_directory_link",
    "unlock",
    "write_private_file_atomic",
]
