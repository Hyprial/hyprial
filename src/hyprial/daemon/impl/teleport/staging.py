# Decision record (W2B-2, 2026-11-21):
# - StagingReceiver._append opens operation.stream in unbuffered binary mode
#   (buffering=0, "wb" on creation with 0600 opener + harden_private_path, "r+b"
#   thereafter) under the in-process receiver lock, loops over a memoryview to
#   handle partial RawIOBase writes, and calls os.fsync(stream.fileno()).
# - On any write or fsync exception, rollback directly executes os.ftruncate,
#   stream.seek, and os.fsync on the raw descriptor so no user-space buffered
#   stream can re-flush dirty bytes during seek() or close() and skip truncation.
"""Target-side staging receiver for one ordered byte stream per operation.

Decisions:

- The operation registry is in memory.  ``prepare`` is the only way to make an
  operationId known, so a fresh process never trusts on-disk residue; ``purge``
  (daemon start, a later slice) deletes it.
- Each operation owns ``<root>/<operationId>/stream``; directories are 0700 and
  every accepted chunk is fsynced before ``persisted`` is returned.
- The first chunk at offset 0 binds ``streamSha256`` and ``total``.  A chunk
  that overlaps persisted bytes with different content, that disagrees with the
  binding, or that reaches past ``total`` is ``STREAM_MISMATCH`` and never
  touches disk.
- A chunk that partly overlaps persisted bytes appends only its tail, after the
  overlap was verified byte for byte.
- All state changes happen under one lock because Zenoh calls handlers from its
  own threads.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

from hyprial.kernel import harden_private_path
from hyprial.daemon.impl.teleport.errors import TELEPORT_CHUNK_BYTES, TeleportError
from hyprial.daemon.impl.teleport.frames import ChunkRequest

_STREAM_NAME = "stream"
_READ_BLOCK = 1 << 20


def staging_root(
    home: str | os.PathLike[str], state_dir: str | os.PathLike[str] | None = None
) -> Path:
    base = Path(home) / "state" if state_dir is None else Path(state_dir)
    return base / "teleport" / "staging"


@dataclass(slots=True)
class _Operation:
    directory: Path
    total: int | None = None
    stream_sha256: str | None = None
    persisted: int = 0

    @property
    def stream(self) -> Path:
        return self.directory / _STREAM_NAME


def _safe_operation_id(operation_id: str) -> bool:
    return (
        isinstance(operation_id, str)
        and operation_id not in {"", ".", ".."}
        and "/" not in operation_id
        and "\\" not in operation_id
        and "\0" not in operation_id
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(_READ_BLOCK):
            digest.update(block)
    return digest.hexdigest()


class StagingReceiver:
    def __init__(self, root: Path, *, chunk_limit: int = TELEPORT_CHUNK_BYTES) -> None:
        self.root = Path(root)
        self.chunk_limit = chunk_limit
        self._operations: dict[str, _Operation] = {}
        self._lock = threading.RLock()

    def prepare(self, operation_id: str) -> Path:
        if not _safe_operation_id(operation_id):
            raise TeleportError("TELEPORT_BAD_FRAME", "unsafe operationId")
        with self._lock:
            if operation_id in self._operations:
                raise TeleportError(
                    "TELEPORT_OPERATION_CONFLICT",
                    f"operation {operation_id!r} is already prepared",
                )
            self.root.mkdir(parents=True, exist_ok=True)
            harden_private_path(self.root, mode=0o700)
            directory = self.root / operation_id
            if directory.exists():
                shutil.rmtree(directory, ignore_errors=True)
            directory.mkdir(mode=0o700)
            harden_private_path(directory, mode=0o700)
            self._operations[operation_id] = _Operation(directory=directory)
            return directory

    def persisted(self, operation_id: str) -> int:
        with self._lock:
            return self._operation(operation_id).persisted

    def accept_chunk(self, request: ChunkRequest) -> int:
        with self._lock:
            operation = self._operation(request.operation_id)
            self._check_chunk(request)
            self._bind_or_check(operation, request)
            if request.offset > operation.persisted:
                raise TeleportError(
                    "TELEPORT_OFFSET_GAP",
                    f"offset {request.offset} beyond persisted {operation.persisted}",
                )
            overlap = min(request.offset + request.size, operation.persisted) - request.offset
            if overlap > 0:
                self._verify_overlap(operation, request, overlap)
            tail = request.data[overlap:] if overlap > 0 else request.data
            if tail or not operation.stream.exists():
                self._append(operation, tail)
            return operation.persisted

    def finalize(self, operation_id: str, *, stream_sha256: str) -> Path:
        with self._lock:
            operation = self._operation(operation_id)
            if operation.total is None or operation.persisted < operation.total:
                raise TeleportError(
                    "TELEPORT_INCOMPLETE",
                    f"{operation.persisted} of {operation.total} bytes staged",
                )
            actual = _file_sha256(operation.stream)
            if actual != stream_sha256 or actual != operation.stream_sha256:
                raise TeleportError(
                    "TELEPORT_DIGEST_MISMATCH", "staged stream digest differs"
                )
            return operation.stream

    def abort(self, operation_id: str) -> None:
        with self._lock:
            self._operations.pop(operation_id, None)
            if _safe_operation_id(operation_id):
                shutil.rmtree(self.root / operation_id, ignore_errors=True)

    def purge(self) -> None:
        with self._lock:
            self._operations.clear()
            if not self.root.is_dir():
                return
            for child in self.root.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)

    def _operation(self, operation_id: str) -> _Operation:
        operation = self._operations.get(operation_id)
        if operation is None:
            raise TeleportError(
                "TELEPORT_UNKNOWN_OPERATION", f"operation {operation_id!r} is not prepared"
            )
        return operation

    def _check_chunk(self, request: ChunkRequest) -> None:
        if request.size > self.chunk_limit or len(request.data) > self.chunk_limit:
            raise TeleportError(
                "TELEPORT_CHUNK_TOO_LARGE",
                f"chunk of {request.size} bytes exceeds {self.chunk_limit}",
            )
        if hashlib.sha256(request.data).hexdigest() != request.chunk_sha256:
            raise TeleportError("TELEPORT_CHUNK_DIGEST_MISMATCH", "chunk digest differs")

    def _bind_or_check(self, operation: _Operation, request: ChunkRequest) -> None:
        if operation.total is None:
            if request.offset != 0:
                raise TeleportError(
                    "TELEPORT_OFFSET_GAP", "first chunk must start at offset 0"
                )
            self._check_range(request, request.total)
            operation.total = request.total
            operation.stream_sha256 = request.stream_sha256
            return
        if (
            request.total != operation.total
            or request.stream_sha256 != operation.stream_sha256
        ):
            raise TeleportError(
                "TELEPORT_STREAM_MISMATCH", "stream total or digest differs from first chunk"
            )
        self._check_range(request, operation.total)

    @staticmethod
    def _check_range(request: ChunkRequest, total: int) -> None:
        if request.offset + request.size > total:
            raise TeleportError(
                "TELEPORT_STREAM_MISMATCH",
                f"chunk ends at {request.offset + request.size}, beyond total {total}",
            )

    @staticmethod
    def _verify_overlap(
        operation: _Operation, request: ChunkRequest, overlap: int
    ) -> None:
        with operation.stream.open("rb") as handle:
            handle.seek(request.offset)
            staged = handle.read(overlap)
        if staged != request.data[:overlap]:
            raise TeleportError(
                "TELEPORT_STREAM_MISMATCH", "chunk differs from already persisted bytes"
            )

    @staticmethod
    def _append(operation: _Operation, data: bytes) -> None:
        exists = operation.stream.exists()
        opener = None if exists else (lambda path, flags: os.open(path, flags, 0o600))
        with open(
            operation.stream,
            "r+b" if exists else "wb",
            buffering=0,
            opener=opener,
        ) as stream:
            try:
                if not exists or operation.persisted == 0:
                    harden_private_path(operation.stream, mode=0o600)
                stream.seek(operation.persisted)
                view = memoryview(data)
                while view:
                    written = stream.write(view)
                    if not written:
                        raise OSError("staging stream write made no progress")
                    view = view[written:]
                os.fsync(stream.fileno())
            except BaseException:
                fd = stream.fileno()
                os.ftruncate(fd, operation.persisted)
                try:
                    stream.seek(operation.persisted)
                except OSError:
                    pass
                try:
                    os.fsync(fd)
                except OSError:
                    pass
                raise
        operation.persisted += len(data)
