"""Descriptor-relative safe storage and per-slot OS ownership.

Decision (2026-11-26, KP-140): ``locked()`` hardens ``operation.lock`` after
the OS lock is acquired and before the first ``_file_safe`` check.  On
Windows ``filelock`` creates the file with ``mode=0o600``, which applies a
protected descriptor that grants access only to ``SYSTEM``,
``BUILTIN\\Administrators`` and ``OWNER RIGHTS``; it is not inherited from the
parent directory.  Under an elevated token the new file is owned by
``BUILTIN\\Administrators``, and the kernel counts an ``OWNER RIGHTS`` entry as
current-user access only when the current user is the owner, so ``_file_safe``
rejected every such lock file.  ``harden_private_path`` replaces the
descriptor with a protected one for the current user.

The lock file may have been created by another process or placed in advance,
so changing its permissions is allowed only for an entry whose owner the
kernel accepts (this account; on Windows also ``SYSTEM`` and
``BUILTIN\\Administrators``, which the platform trusts regardless of the
descriptor): it must be a regular file (no symlink or reparse point) with
exactly one link, and ``private_metadata_violation`` must not report
``wrong-owner``, ``unsafe-reparse`` or ``unsupported-private-acl``; otherwise
the slot is rejected without touching the file.

This is a known change of behaviour on POSIX, not a defect or an open item.  An
existing lock file owned by this account with a mode other than ``0600`` used
to be refused by ``_file_safe``; it is now repaired to ``0600`` and accepted.
What is newly accepted is exactly: a regular file, single link, owned by the
current account, mode not ``0600``, whose ``chmod`` succeeds.  A wrong owner,
a symlink, a hard link or a failed repair is still refused.

Unlike the ``.cas.lock`` preparation in credential storage, hardening runs
even when no violation is reported, so both platforms take the same path and
an already private file is re-applied to the same mode.  ``_file_safe`` stays
unchanged before and after the body; hardening corrects the permissions and
never relaxes that check itself.  A lock file that disappears between
acquiring the lock and this check (``lstat`` raising ``FileNotFoundError``) is
now refused as ``TAILNET_STORAGE_UNSAFE`` instead of surfacing the bare
``FileNotFoundError``; failing closed is intended.
"""

from contextlib import contextmanager
from pathlib import Path
import stat

from hyprial.kernel import (
    PrivateAccessUnsupported,
    ProcessFileLock,
    harden_private_path,
    open_private_file,
    path_is_reparse,
    private_metadata_violation,
    read_private_file,
    secure_private_directory,
    write_private_file_atomic,
)

from .schema import fail, sha
from .ports import text


class Slot:
    def __init__(self, home, tailnet_id):
        self.home = Path(home).absolute()
        self.tailnet_id = text(tailnet_id)
        token = sha(tailnet_id.encode("utf-8"))
        self.state = ("state", "transport", "tailnets", token)
        self.secrets = ("secrets", "transport", "tailnets", token)
        with self.directory(()) as directory:
            metadata = directory.lstat()
            self.identity = {"device": metadata.st_dev, "inode": metadata.st_ino}

    @staticmethod
    def _directory_safe(path, metadata):
        if not stat.S_ISDIR(metadata.st_mode) or private_metadata_violation(path, metadata, mode=0o700):
            fail("TAILNET_STORAGE_UNSAFE", "storage")

    @contextmanager
    def directory(self, parts, *, create=False):
        try:
            current = Path(self.home)
            try:
                self._directory_safe(current, current.lstat())
            except FileNotFoundError:
                fail("TAILNET_HOME_MISMATCH", "storage")
            identity_metadata = current.lstat()
            identity = {"device": identity_metadata.st_dev, "inode": identity_metadata.st_ino}
            if hasattr(self, "identity") and identity != self.identity:
                fail("TAILNET_HOME_MISMATCH", "storage")
            for part in parts:
                if type(part) is not str or part in ("", ".", "..") or "/" in part:
                    fail("TAILNET_STORAGE_UNSAFE", "storage")
                current = current / part
                secure_private_directory(current, create=create)
            yield current
        except FileNotFoundError:
            raise
        except (OSError, PrivateAccessUnsupported):
            fail("TAILNET_STORAGE_UNSAFE", "storage")

    @staticmethod
    def _file_safe(path, metadata, mode):
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            fail("TAILNET_STORAGE_UNSAFE", "storage")
        try:
            if private_metadata_violation(path, metadata, mode=mode) is not None:
                fail("TAILNET_STORAGE_UNSAFE", "storage")
        except (OSError, PrivateAccessUnsupported):
            fail("TAILNET_STORAGE_UNSAFE", "storage")

    def read(self, parts, *, mode=0o600, maximum=1024 * 1024):
        value = self.read_with_marker(parts, mode=mode, maximum=maximum)
        return value[0] if value is not None else None

    @staticmethod
    def _marker(metadata, data):
        return {"device": metadata.st_dev, "inode": metadata.st_ino,
                "owner": metadata.st_uid, "mode": stat.S_IMODE(metadata.st_mode),
                "links": metadata.st_nlink, "sha256": sha(data)}

    def read_with_marker(self, parts, *, mode=0o600, maximum=1024 * 1024):
        try:
            with self.directory(parts[:-1]) as directory:
                path = directory / parts[-1]
                data, metadata = read_private_file(
                    path, mode=mode, max_bytes=maximum
                )
                self._file_safe(path, metadata, mode)
                return data, self._marker(metadata, data)
        except FileNotFoundError:
            return None
        except (OSError, PrivateAccessUnsupported):
            fail("TAILNET_STORAGE_UNSAFE", "storage")

    def write(self, parts, raw, *, exclusive=False):
        if type(raw) is not bytes:
            raise ValueError("slot writes require bytes")
        with self.directory(parts[:-1], create=True) as directory:
            path = directory / parts[-1]
            try:
                metadata = write_private_file_atomic(
                    path, raw, exclusive=exclusive
                )
            except FileExistsError:
                fail("TAILNET_STAGING_CONFLICT", "storage")
            except (OSError, PrivateAccessUnsupported):
                fail("TAILNET_STORAGE_UNSAFE", "storage")
            self._file_safe(path, metadata, 0o600)
            return self._marker(metadata, raw)

    def _existing_target(self, directory, name):
        """Validate replacement safety without reading or imposing a payload cap."""
        path = directory / name
        try:
            with open_private_file(path) as (_descriptor, metadata):
                self._file_safe(path, metadata, 0o600)
                return True
        except FileNotFoundError:
            return False

    @staticmethod
    def _harden_lock(path):
        """Make the lock file private; refuse an entry whose owner the kernel rejects."""
        try:
            metadata = path.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or path_is_reparse(metadata)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
            ):
                fail("TAILNET_STORAGE_UNSAFE", "storage")
            violation = private_metadata_violation(path, metadata, mode=0o600)
            if violation in ("wrong-owner", "unsafe-reparse", "unsupported-private-acl"):
                fail("TAILNET_STORAGE_UNSAFE", "storage")
            harden_private_path(path, mode=0o600)
        except (OSError, PrivateAccessUnsupported):
            fail("TAILNET_STORAGE_UNSAFE", "storage")

    @contextmanager
    def locked(self):
        with self.directory(self.state, create=True) as directory:
            path = directory / "operation.lock"
            lock = ProcessFileLock(path)
            try:
                lock.acquire(timeout=15.0)
                self._harden_lock(path)
                self._file_safe(path, path.lstat(), 0o600)
                yield
                self._file_safe(path, path.lstat(), 0o600)
            finally:
                lock.release()

    def journals(self):
        try:
            with self.directory((*self.state, "journal")) as directory:
                return tuple(sorted(entry.name for entry in directory.iterdir()))
        except FileNotFoundError:
            return ()
