# Decision record (contract freeze r3 §4.5, §9.5 layer 1; B1 review fix, rollback after restart)
# - Rollback marks remember the highest (seq, digest) this device has accepted per
#   owner record path. A lower seq, or the same seq with other content, refuses as
#   PROVIDER_KEY_SIGNATURE_INVALID. Without OrgFS admission any writer to the space
#   can put back an older owner-signed record, and launch renews an expired grant,
#   so the marks must survive a daemon restart: memory alone is not enough.
# - The marks are durable and owner-local: H/state/provider-keys/high-water-<h>.json
#   with h = sha256(Casdoor sub)[:32], never in OrgFS, bundles or transfer. The file
#   names the sub it belongs to; it holds paths, seqs and record digests, no values.
#   Directory 0700, file 0600, opened through the directory fd with O_NOFOLLOW;
#   symlinks, non-regular or foreign-owned files refuse instead of being followed.
# - A mark is persisted before the read (and so the commit readback) that raised it
#   returns. Saves hold an flock and merge with the file (highest seq per path wins),
#   so two instances in one process or a racing process never lower a mark. A save
#   that fails puts the in-memory mark back where it was, so the next read of the same
#   record retries the save instead of finding the mark already raised and returning.
# - Every durable observation, mark lookup and slot inventory refreshes the atomic
#   file snapshot before trusting its cache. Another instance can raise a mark after
#   construction; a cached match cannot accept that instance's now-stale record.
#   Equal sequence numbers with different digests refuse at every merge boundary.
# - The temp file is written in full (short writes loop) before it replaces the marks
#   file; a truncated document never replaces a good one.
# - Fail closed: a mark file that cannot be read, parsed or written is
#   PROVIDER_STORE_UNAVAILABLE; it is never treated as empty. A lost or wiped state
#   directory starts without marks: the signature, the tombstone and the grant TTL
#   remain, and this is the documented limit of the local guard.
"""Durable per-owner rollback marks for owner records."""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Mapping

from hyprial.identity import ProviderKeys
from hyprial.kernel import (
    PrivateAccessUnsupported,
    ProcessFileLock,
    ipc_errors,
    read_private_file,
    secure_private_directory,
    write_private_file_atomic,
)

PARTITION = "provider-keys"
_LOCK_NAME = "high-water.lock"
_DIGEST_HEX = frozenset("0123456789abcdef")


def _refuse(code: str) -> ProviderKeys.Error:
    return ProviderKeys.Error(code)


def marks_name(owner: str) -> str:
    return "high-water-" + hashlib.sha256(owner.encode("utf-8")).hexdigest()[:32] + ".json"


def _mark(value: Any) -> tuple[int, str] | None:
    if (type(value) is list and len(value) == 2 and type(value[0]) is int and value[0] >= 1
            and type(value[1]) is str and len(value[1]) == 64 and set(value[1]) <= _DIGEST_HEX):
        return value[0], value[1]
    return None


def _higher(left: tuple[int, str] | None, right: tuple[int, str]) -> tuple[int, str]:
    if left is not None and left[0] == right[0] and left[1] != right[1]:
        raise _refuse(ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID)
    return right if left is None or right[0] > left[0] else left


class HighWater:
    """Highest (seq, digest) accepted per record path; durable when given a state directory."""

    def __init__(self, state_dir: Path | None = None, owner: str | None = None) -> None:
        self._lock = threading.Lock()
        self._seen: dict[str, tuple[int, str]] = {}
        self._dir = None if state_dir is None else Path(state_dir) / PARTITION
        self._owner = owner
        if self._dir is not None:
            if not isinstance(owner, str) or not owner:
                raise _refuse(ipc_errors.PROVIDER_STORE_UNAVAILABLE)
            self._name = marks_name(owner)
            self._seen = self._load(self._open_dir())

    def __repr__(self) -> str:
        return f"HighWater(durable={self._dir is not None}, paths={len(self._seen)})"

    def observe(self, path: str, payload: Mapping[str, Any]) -> None:
        """Accept (seq, digest) for path, or refuse a rollback as SIGNATURE_INVALID."""
        seq, digest = payload["seq"], ProviderKeys.record_digest(dict(payload))
        with self._lock:
            self._refresh()
            known = self._seen.get(path)
            if known is not None and (seq < known[0] or (seq == known[0] and digest != known[1])):
                raise _refuse(ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID)
            if known == (seq, digest):
                return
            self._seen[path] = (seq, digest)
            if self._dir is None:
                return
            try:
                self._save(path)
            except ProviderKeys.Error:
                if self._seen.get(path) == (seq, digest):
                    if known is None:
                        del self._seen[path]
                    else:
                        self._seen[path] = known
                raise

    def mark(self, path: str) -> tuple[int, str] | None:
        """The (seq, digest) accepted for path, or None when this device never accepted one."""
        with self._lock:
            self._refresh()
            return self._seen.get(path)

    def marked_names(self, directory: str) -> tuple[str, ...]:
        """Child names directly under directory that carry a mark."""
        prefix = directory + "/"
        with self._lock:
            self._refresh()
            return tuple(sorted(path[len(prefix):] for path in self._seen
                                if path.startswith(prefix) and "/" not in path[len(prefix):]))

    def _refresh(self) -> None:
        if self._dir is None:
            return
        disk = self._load(self._open_dir())
        merged = dict(self._seen)
        for path, mark in disk.items():
            merged[path] = _higher(merged.get(path), mark)
        self._seen = merged

    def _open_dir(self) -> Path:
        if self._dir is None:
            raise _refuse(ipc_errors.PROVIDER_STORE_UNAVAILABLE)
        try:
            return secure_private_directory(self._dir, create=True)
        except (OSError, PrivateAccessUnsupported):
            raise _refuse(ipc_errors.PROVIDER_STORE_UNAVAILABLE) from None

    def _load(self, directory: Path) -> dict[str, tuple[int, str]]:
        try:
            raw, _ = read_private_file(directory / self._name)
        except FileNotFoundError:
            return {}
        except (OSError, PrivateAccessUnsupported):
            raise _refuse(ipc_errors.PROVIDER_STORE_UNAVAILABLE) from None
        return self._parse(raw)

    def _parse(self, raw: bytes) -> dict[str, tuple[int, str]]:
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            value = None
        if (type(value) is not dict or set(value) != {"v", "owner", "marks"} or value["v"] != 1
                or value["owner"] != self._owner or type(value["marks"]) is not dict):
            raise _refuse(ipc_errors.PROVIDER_STORE_UNAVAILABLE)
        seen = {}
        for path, raw_mark in value["marks"].items():
            mark = _mark(raw_mark)
            if type(path) is not str or mark is None:
                raise _refuse(ipc_errors.PROVIDER_STORE_UNAVAILABLE)
            seen[path] = mark
        return seen

    def _save(self, path: str) -> None:
        directory = self._open_dir()
        lock = ProcessFileLock(directory / _LOCK_NAME)
        try:
            lock.acquire(timeout=15.0)
            disk = self._load(directory)
            ours, theirs = self._seen[path], disk.pop(path, None)
            if theirs is not None and (theirs[0] > ours[0] or (theirs[0] == ours[0] and theirs[1] != ours[1])):
                self._seen[path] = theirs
                raise _refuse(ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID)
            for other, mark in disk.items():
                self._seen[other] = _higher(self._seen.get(other), mark)
            self._write(directory)
        except (OSError, PrivateAccessUnsupported):
            raise _refuse(ipc_errors.PROVIDER_STORE_UNAVAILABLE) from None
        finally:
            lock.release()

    def _write(self, directory: Path) -> None:
        body = json.dumps({"v": 1, "owner": self._owner,
                           "marks": {key: list(mark) for key, mark in sorted(self._seen.items())}},
                          sort_keys=True, separators=(",", ":")).encode("utf-8")
        try:
            write_private_file_atomic(directory / self._name, body)
        except (OSError, PrivateAccessUnsupported):
            raise _refuse(ipc_errors.PROVIDER_STORE_UNAVAILABLE) from None
