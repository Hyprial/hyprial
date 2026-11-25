# Decision record (W1E-2, 2026-11-21):
# - BootstrapAdmission.acquire uses manysys.path_is_reparse and
#   manysys.private_metadata_violation(mode=None) instead of raw os.getuid so
#   existing home/state directories are verified private across POSIX and Windows.
"""B0 identity and old-process admission under continuous existing OS locks.

This creates no business runtime, listener, identity resolver or credential
worker. Control registration and private permit issuance happen later, after
the bootstrap instance has recorded and verified its own process identity.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat

from hyprial.identity import IdentityAdmission, read_identity_admission
from hyprial.kernel import manysys, private_metadata_violation

from ..locks import BootstrapLocks, require_unoverridden_owner
from .previous import require_previous_instances_exited


def _has_unsafe_ancestry(target: Path) -> bool:
    lexical = Path(os.path.abspath(os.fspath(target)))
    current = Path(lexical.anchor)
    for part in lexical.parts[1:-1]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if manysys.path_is_reparse(info):
            return True
        if stat.S_ISLNK(info.st_mode):
            allowed = (
                current.anchor == "/"
                and current.parts in {("/", "tmp"), ("/", "var"), ("/", "etc")}
                and current.is_dir()
            )
            if not allowed:
                return True
            continue
        if not stat.S_ISDIR(info.st_mode):
            return True
    return False


@dataclass
class BootstrapAdmission:
    home: Path
    state_dir: Path
    socket_path: Path
    identity: IdentityAdmission
    locks: BootstrapLocks

    @classmethod
    def acquire(cls, home: Path, state_dir: Path, socket_path: Path) -> BootstrapAdmission:
        home, state_dir, socket_path = Path(home), Path(state_dir), Path(socket_path)
        if not all(path.is_absolute() for path in (home, state_dir, socket_path)):
            raise ValueError("bootstrap paths must be explicit and absolute")
        for path in (home, state_dir):
            if _has_unsafe_ancestry(path):
                raise ValueError("bootstrap home/state must be directories, not symlinks")
            try:
                observed = path.lstat()
            except FileNotFoundError:
                continue
            if (
                stat.S_ISLNK(observed.st_mode)
                or manysys.path_is_reparse(observed)
                or not stat.S_ISDIR(observed.st_mode)
            ):
                raise ValueError("bootstrap home/state must be directories, not symlinks")
            violation = private_metadata_violation(path, observed, mode=None)
            if violation == "wrong-owner":
                raise ValueError("bootstrap home/state must belong to the current OS user")
            if violation is not None:
                raise ValueError(f"bootstrap home/state must be private ({violation})")
        require_unoverridden_owner()
        before = read_identity_admission(home)
        if before.snapshot.owner is not None:
            raise RuntimeError("authenticated home requires normal startup or explicit recovery")
        locks = BootstrapLocks.acquire(home, state_dir)
        try:
            current = read_identity_admission(home)
            if current.snapshot.owner is not None or current != before:
                raise RuntimeError("identity changed before bootstrap admission")
            require_previous_instances_exited(home, state_dir, socket_path)
            return cls(home, state_dir, socket_path, current, locks)
        except BaseException:
            locks.close()
            raise

    def close(self) -> None:
        self.locks.close()

    def __enter__(self) -> BootstrapAdmission:
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()
