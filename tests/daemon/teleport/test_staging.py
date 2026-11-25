"""Slice 2 target-side staging receiver, exercised without any transport.

Decisions pinned here:

- ``StagingReceiver(root, *, chunk_limit=TELEPORT_CHUNK_BYTES)`` keeps the
  operation registry in memory; ``prepare`` is the only way to make an
  operationId known, so a fresh process never trusts on-disk residue and
  ``purge()`` (called at daemon start by a later slice) deletes it.
- ``staging_root(home)`` is ``<home>/state/teleport/staging``; each prepared
  operation owns a direct child directory of that root with mode 0700.
- The first chunk at offset 0 binds ``streamSha256`` and ``total``.
- A chunk overlapping already persisted bytes whose content differs is not a
  duplicate; it is rejected with ``TELEPORT_STREAM_MISMATCH`` and nothing on
  disk changes.  Rejected chunks never advance ``persisted``.
- ``finalize(operation_id, stream_sha256=...)`` recomputes the digest of the
  bytes on disk and compares it with both the receive-time digest and the one
  bound by the first chunk; it returns a readable path to the whole stream
  inside the operation directory.
"""

from __future__ import annotations

import builtins
import errno
import io
import os
import stat
from pathlib import Path

import pytest

from .support import chunk_requests, sha256_hex, stream_bytes, teleport

SMALL = 1024


def _receiver(tmp_path: Path, **kwargs):
    module = teleport()
    return module.StagingReceiver(module.staging_root(tmp_path / "home"), **kwargs)


def _code(callable_, *args, **kwargs) -> str:
    module = teleport()
    with pytest.raises(module.TeleportError) as caught:
        callable_(*args, **kwargs)
    return caught.value.code


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_staging_root_layout(tmp_path: Path) -> None:
    module = teleport()
    home = tmp_path / "home"
    assert module.staging_root(home) == home / "state" / "teleport" / "staging"


def test_prepare_creates_private_operation_directory(tmp_path: Path) -> None:
    receiver = _receiver(tmp_path)
    directory = receiver.prepare("op-1")
    assert directory == receiver.root / "op-1"
    assert directory.is_dir()
    assert _mode(directory) == 0o700
    assert _mode(receiver.root) == 0o700
    assert receiver.persisted("op-1") == 0


def test_prepare_twice_is_operation_conflict(tmp_path: Path) -> None:
    receiver = _receiver(tmp_path)
    receiver.prepare("op-1")
    assert _code(receiver.prepare, "op-1") == "TELEPORT_OPERATION_CONFLICT"


@pytest.mark.parametrize("operation_id", ["../escape", "a/b", "..", "."])
def test_prepare_never_writes_outside_root(tmp_path: Path, operation_id: str) -> None:
    module = teleport()
    receiver = _receiver(tmp_path)
    before = set(tmp_path.rglob("*"))
    try:
        directory = receiver.prepare(operation_id)
    except module.TeleportError:
        directory = None
    created = set(tmp_path.rglob("*")) - before
    root = receiver.root.resolve()
    for path in created:
        resolved = path.resolve()
        assert resolved == root or root in resolved.parents or resolved in root.parents
    if directory is not None:
        assert directory.resolve().parent == root


def test_sequential_chunks_persist_and_report_growing_offset(tmp_path: Path) -> None:
    receiver = _receiver(tmp_path)
    data = stream_bytes(SMALL * 3 + 100)
    receiver.prepare("op-1")
    persisted = [receiver.accept_chunk(req) for req in chunk_requests(data, "op-1", chunk_bytes=SMALL)]
    assert persisted == [SMALL, SMALL * 2, SMALL * 3, len(data)]
    assert receiver.persisted("op-1") == len(data)


def test_chunk_is_fsynced_before_ack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    receiver = _receiver(tmp_path)
    receiver.prepare("op-1")
    real_fsync = os.fsync
    calls: list[int] = []

    def spy(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    data = stream_bytes(SMALL)
    receiver.accept_chunk(chunk_requests(data, "op-1", chunk_bytes=SMALL)[0])
    assert calls, "accept_chunk must fsync staged bytes before returning persisted"


def test_duplicate_chunk_is_idempotent(tmp_path: Path) -> None:
    receiver = _receiver(tmp_path)
    data = stream_bytes(SMALL * 3)
    requests = chunk_requests(data, "op-1", chunk_bytes=SMALL)
    receiver.prepare("op-1")
    receiver.accept_chunk(requests[0])
    receiver.accept_chunk(requests[1])
    assert receiver.accept_chunk(requests[0]) == SMALL * 2
    assert receiver.accept_chunk(requests[1]) == SMALL * 2
    assert receiver.accept_chunk(requests[2]) == SMALL * 3
    path = receiver.finalize("op-1", stream_sha256=sha256_hex(data))
    assert path.read_bytes() == data


def test_overlapping_chunk_with_different_content_is_rejected(tmp_path: Path) -> None:
    receiver = _receiver(tmp_path)
    data = stream_bytes(SMALL * 2)
    requests = chunk_requests(data, "op-1", chunk_bytes=SMALL)
    receiver.prepare("op-1")
    receiver.accept_chunk(requests[0])
    forged_piece = bytes(SMALL)
    forged = teleport().ChunkRequest(
        operation_id="op-1",
        offset=0,
        total=len(data),
        size=SMALL,
        stream_sha256=sha256_hex(data),
        chunk_sha256=sha256_hex(forged_piece),
        data=forged_piece,
    )
    assert _code(receiver.accept_chunk, forged) == "TELEPORT_STREAM_MISMATCH"
    assert receiver.persisted("op-1") == SMALL
    receiver.accept_chunk(requests[1])
    assert receiver.finalize("op-1", stream_sha256=sha256_hex(data)).read_bytes() == data


def test_chunk_ending_beyond_total_is_stream_mismatch_and_leaves_disk_unchanged(
    tmp_path: Path,
) -> None:
    module = teleport()
    receiver = _receiver(tmp_path)
    data = stream_bytes(SMALL * 2)
    requests = chunk_requests(data, "op-1", chunk_bytes=SMALL)
    directory = receiver.prepare("op-1")
    first_too_long = chunk_requests(
        data, "op-1", chunk_bytes=SMALL, total=SMALL - 1
    )[0]
    assert _code(receiver.accept_chunk, first_too_long) == "TELEPORT_STREAM_MISMATCH"
    assert receiver.persisted("op-1") == 0
    assert list(directory.iterdir()) == []
    receiver.accept_chunk(requests[0])
    staged = (directory / "stream").read_bytes()
    past_total = module.ChunkRequest(
        operation_id="op-1",
        offset=SMALL,
        total=SMALL * 2,
        size=SMALL + 1,
        stream_sha256=sha256_hex(data),
        chunk_sha256=sha256_hex(data[SMALL:] + b"x"),
        data=data[SMALL:] + b"x",
    )
    assert _code(receiver.accept_chunk, past_total) == "TELEPORT_STREAM_MISMATCH"
    assert receiver.persisted("op-1") == SMALL
    assert (directory / "stream").read_bytes() == staged
    receiver.accept_chunk(requests[1])
    assert receiver.finalize("op-1", stream_sha256=sha256_hex(data)).read_bytes() == data


def test_chunk_beyond_persisted_is_offset_gap(tmp_path: Path) -> None:
    receiver = _receiver(tmp_path)
    requests = chunk_requests(stream_bytes(SMALL * 3), "op-1", chunk_bytes=SMALL)
    receiver.prepare("op-1")
    assert _code(receiver.accept_chunk, requests[1]) == "TELEPORT_OFFSET_GAP"
    receiver.accept_chunk(requests[0])
    assert _code(receiver.accept_chunk, requests[2]) == "TELEPORT_OFFSET_GAP"
    assert receiver.persisted("op-1") == SMALL
    assert receiver.accept_chunk(requests[1]) == SMALL * 2


def test_chunk_over_limit_is_too_large(tmp_path: Path) -> None:
    receiver = _receiver(tmp_path, chunk_limit=SMALL)
    data = stream_bytes(SMALL + 1)
    receiver.prepare("op-1")
    request = chunk_requests(data, "op-1", chunk_bytes=SMALL + 1)[0]
    assert _code(receiver.accept_chunk, request) == "TELEPORT_CHUNK_TOO_LARGE"
    assert receiver.persisted("op-1") == 0


def test_default_chunk_limit_is_wire_constant(tmp_path: Path) -> None:
    receiver = _receiver(tmp_path)
    data = stream_bytes(768_001)
    receiver.prepare("op-1")
    over = chunk_requests(data, "op-1", chunk_bytes=768_001)[0]
    assert _code(receiver.accept_chunk, over) == "TELEPORT_CHUNK_TOO_LARGE"
    exact = chunk_requests(data, "op-1", chunk_bytes=768_000)[0]
    assert receiver.accept_chunk(exact) == 768_000


def test_chunk_digest_mismatch(tmp_path: Path) -> None:
    module = teleport()
    receiver = _receiver(tmp_path)
    data = stream_bytes(SMALL)
    receiver.prepare("op-1")
    tampered = module.ChunkRequest(
        operation_id="op-1",
        offset=0,
        total=SMALL,
        size=SMALL,
        stream_sha256=sha256_hex(data),
        chunk_sha256=sha256_hex(b"something else"),
        data=data,
    )
    assert _code(receiver.accept_chunk, tampered) == "TELEPORT_CHUNK_DIGEST_MISMATCH"
    assert receiver.persisted("op-1") == 0


def test_stream_digest_change_after_first_chunk_is_stream_mismatch(tmp_path: Path) -> None:
    receiver = _receiver(tmp_path)
    data = stream_bytes(SMALL * 2)
    receiver.prepare("op-1")
    receiver.accept_chunk(chunk_requests(data, "op-1", chunk_bytes=SMALL)[0])
    other = chunk_requests(data, "op-1", chunk_bytes=SMALL, stream_sha256="0" * 64)[1]
    assert _code(receiver.accept_chunk, other) == "TELEPORT_STREAM_MISMATCH"
    assert receiver.persisted("op-1") == SMALL


def test_total_change_after_first_chunk_is_stream_mismatch(tmp_path: Path) -> None:
    receiver = _receiver(tmp_path)
    data = stream_bytes(SMALL * 2)
    receiver.prepare("op-1")
    receiver.accept_chunk(chunk_requests(data, "op-1", chunk_bytes=SMALL)[0])
    other = chunk_requests(data, "op-1", chunk_bytes=SMALL, total=SMALL * 3)[1]
    assert _code(receiver.accept_chunk, other) == "TELEPORT_STREAM_MISMATCH"
    assert receiver.persisted("op-1") == SMALL


def test_unprepared_operation_is_unknown(tmp_path: Path) -> None:
    receiver = _receiver(tmp_path)
    request = chunk_requests(stream_bytes(SMALL), "ghost", chunk_bytes=SMALL)[0]
    assert _code(receiver.accept_chunk, request) == "TELEPORT_UNKNOWN_OPERATION"
    assert _code(receiver.persisted, "ghost") == "TELEPORT_UNKNOWN_OPERATION"
    assert _code(receiver.finalize, "ghost", stream_sha256="0" * 64) == "TELEPORT_UNKNOWN_OPERATION"
    assert not (receiver.root / "ghost").exists()


def test_finalize_short_stream_is_incomplete(tmp_path: Path) -> None:
    receiver = _receiver(tmp_path)
    data = stream_bytes(SMALL * 2 + 5)
    receiver.prepare("op-1")
    digest = sha256_hex(data)
    assert _code(receiver.finalize, "op-1", stream_sha256=digest) == "TELEPORT_INCOMPLETE"
    receiver.accept_chunk(chunk_requests(data, "op-1", chunk_bytes=SMALL)[0])
    assert _code(receiver.finalize, "op-1", stream_sha256=digest) == "TELEPORT_INCOMPLETE"


def test_finalize_recomputes_digest_of_staged_bytes(tmp_path: Path) -> None:
    receiver = _receiver(tmp_path)
    data = stream_bytes(SMALL * 2 + 5)
    lie = sha256_hex(b"declared but not what was sent")
    receiver.prepare("op-1")
    for request in chunk_requests(data, "op-1", chunk_bytes=SMALL, stream_sha256=lie):
        receiver.accept_chunk(request)
    assert _code(receiver.finalize, "op-1", stream_sha256=lie) == "TELEPORT_DIGEST_MISMATCH"


def test_finalize_rejects_receive_digest_that_differs(tmp_path: Path) -> None:
    receiver = _receiver(tmp_path)
    data = stream_bytes(SMALL * 2 + 5)
    receiver.prepare("op-1")
    for request in chunk_requests(data, "op-1", chunk_bytes=SMALL):
        receiver.accept_chunk(request)
    assert _code(receiver.finalize, "op-1", stream_sha256="f" * 64) == "TELEPORT_DIGEST_MISMATCH"


def test_finalize_success_returns_readable_stream(tmp_path: Path) -> None:
    receiver = _receiver(tmp_path)
    data = stream_bytes(SMALL * 3 + 17)
    directory = receiver.prepare("op-1")
    for request in chunk_requests(data, "op-1", chunk_bytes=SMALL):
        receiver.accept_chunk(request)
    path = receiver.finalize("op-1", stream_sha256=sha256_hex(data))
    assert Path(path).is_file()
    assert directory in Path(path).parents
    assert Path(path).read_bytes() == data


def test_abort_removes_staging_and_is_idempotent(tmp_path: Path) -> None:
    receiver = _receiver(tmp_path)
    data = stream_bytes(SMALL * 2)
    requests = chunk_requests(data, "op-1", chunk_bytes=SMALL)
    directory = receiver.prepare("op-1")
    receiver.accept_chunk(requests[0])
    receiver.abort("op-1")
    assert not directory.exists()
    receiver.abort("op-1")
    receiver.abort("never-prepared")
    assert _code(receiver.accept_chunk, requests[1]) == "TELEPORT_UNKNOWN_OPERATION"


def test_abort_leaves_other_operations_alone(tmp_path: Path) -> None:
    receiver = _receiver(tmp_path)
    keep = receiver.prepare("op-keep")
    receiver.prepare("op-drop")
    receiver.abort("op-drop")
    assert keep.is_dir()
    assert receiver.persisted("op-keep") == 0


def test_purge_removes_residue_from_previous_process(tmp_path: Path) -> None:
    module = teleport()
    home = tmp_path / "home"
    root = module.staging_root(home)
    (root / "old-op").mkdir(parents=True)
    (root / "old-op" / "stream").write_bytes(b"left over")
    (root / "stray-file").write_bytes(b"x")
    sibling = root.parent / "keep-me"
    sibling.write_bytes(b"not staging")
    receiver = module.StagingReceiver(root)
    receiver.purge()
    assert not root.exists() or list(root.iterdir()) == []
    assert sibling.read_bytes() == b"not staging"
    request = chunk_requests(stream_bytes(SMALL), "old-op", chunk_bytes=SMALL)[0]
    assert _code(receiver.accept_chunk, request) == "TELEPORT_UNKNOWN_OPERATION"


def test_purge_without_root_and_forgets_prepared_operations(tmp_path: Path) -> None:
    receiver = _receiver(tmp_path)
    receiver.purge()
    receiver.prepare("op-1")
    receiver.purge()
    assert _code(receiver.persisted, "op-1") == "TELEPORT_UNKNOWN_OPERATION"
    assert not (receiver.root / "op-1").exists()


def test_segmented_and_overlapping_binary_chunks_preserve_exact_bytes(
    tmp_path: Path,
) -> None:
    """Segmented binary appends preserve CRLF/LF/NUL/EOF bytes and partial-overlap tails."""
    from hyprial.kernel import posix_metadata_enforced

    module = teleport()
    receiver = _receiver(tmp_path)
    pattern = b"\x00\n\r\n\x1a\xffteleport-binary-segment\r\n\x00"
    data = (pattern * 120)[: SMALL * 2 + 333]
    digest = sha256_hex(data)
    directory = receiver.prepare("op-bin")

    first = module.ChunkRequest(
        operation_id="op-bin",
        offset=0,
        total=len(data),
        size=SMALL,
        stream_sha256=digest,
        chunk_sha256=sha256_hex(data[:SMALL]),
        data=data[:SMALL],
    )
    overlap_start = SMALL // 2
    overlap_end = SMALL + 500
    overlapping_piece = data[overlap_start:overlap_end]
    second = module.ChunkRequest(
        operation_id="op-bin",
        offset=overlap_start,
        total=len(data),
        size=len(overlapping_piece),
        stream_sha256=digest,
        chunk_sha256=sha256_hex(overlapping_piece),
        data=overlapping_piece,
    )
    tail_piece = data[overlap_end:]
    third = module.ChunkRequest(
        operation_id="op-bin",
        offset=overlap_end,
        total=len(data),
        size=len(tail_piece),
        stream_sha256=digest,
        chunk_sha256=sha256_hex(tail_piece),
        data=tail_piece,
    )

    assert receiver.accept_chunk(first) == SMALL
    if posix_metadata_enforced():
        assert _mode(directory / "stream") == 0o600
    assert receiver.accept_chunk(second) == overlap_end
    assert receiver.accept_chunk(third) == len(data)
    staged = receiver.finalize("op-bin", stream_sha256=digest)
    assert staged.read_bytes() == data


def test_append_failure_truncates_stream_back_to_persisted_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any write or fsync error rolls the on-disk stream back to operation.persisted."""
    receiver = _receiver(tmp_path)
    data = stream_bytes(SMALL * 2 + 77)
    requests = chunk_requests(data, "op-rollback", chunk_bytes=SMALL)
    directory = receiver.prepare("op-rollback")
    stream_path = directory / "stream"

    real_fsync = os.fsync
    fail_once = {"armed": True}

    def flaky_fsync(fd: int) -> None:
        if fail_once["armed"]:
            fail_once["armed"] = False
            raise OSError("simulated initial fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", flaky_fsync)
    with pytest.raises(OSError, match="simulated initial fsync failure"):
        receiver.accept_chunk(requests[0])
    assert receiver.persisted("op-rollback") == 0
    assert not stream_path.exists() or stream_path.read_bytes() == b""

    assert receiver.accept_chunk(requests[0]) == SMALL
    assert stream_path.read_bytes() == data[:SMALL]

    fail_once["armed"] = True
    with pytest.raises(OSError, match="simulated initial fsync failure"):
        receiver.accept_chunk(requests[1])
    assert receiver.persisted("op-rollback") == SMALL
    assert stream_path.read_bytes() == data[:SMALL]

    assert receiver.accept_chunk(requests[1]) == SMALL * 2
    assert receiver.accept_chunk(requests[2]) == len(data)
    assert receiver.finalize("op-rollback", stream_sha256=sha256_hex(data)).read_bytes() == data


class _ControlledRawStream(io.RawIOBase):
    def __init__(self, raw: io.FileIO, on_write) -> None:
        self._raw = raw
        self._on_write = on_write

    def fileno(self) -> int:
        return self._raw.fileno()

    def readable(self) -> bool:
        return self._raw.readable()

    def writable(self) -> bool:
        return self._raw.writable()

    def seekable(self) -> bool:
        return self._raw.seekable()

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self._raw.seek(offset, whence)

    def tell(self) -> int:
        return self._raw.tell()

    def truncate(self, size: int | None = None) -> int:
        return self._raw.truncate(size)

    def readinto(self, buffer) -> int | None:
        return self._raw.readinto(buffer)

    def write(self, buffer) -> int | None:
        return self._on_write(self._raw, memoryview(buffer))

    def close(self) -> None:
        try:
            self._raw.close()
        finally:
            super().close()


def test_partial_raw_writes_loop_until_all_chunk_bytes_persist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unbuffered RawIOBase partial writes advance a memoryview until the full chunk lands."""
    import hyprial.daemon.impl.teleport.staging as staging_module

    receiver = _receiver(tmp_path)
    data = stream_bytes(SMALL * 2 + 91)
    requests = chunk_requests(data, "op-partial", chunk_bytes=SMALL)
    directory = receiver.prepare("op-partial")
    stream_path = directory / "stream"
    observed_buffering: list[int] = []
    partial_calls = 0
    real_open = builtins.open

    def wrapped_open(file, mode="r", buffering=-1, *args, **kwargs):
        nonlocal partial_calls
        if Path(file) != stream_path:
            return real_open(file, mode, buffering=buffering, *args, **kwargs)
        observed_buffering.append(buffering)
        raw = real_open(file, mode, buffering=0, *args, **kwargs)

        def step_write(inner: io.FileIO, view: memoryview) -> int:
            nonlocal partial_calls
            partial_calls += 1
            return inner.write(view[:137])

        controlled = _ControlledRawStream(raw, step_write)
        if buffering == 0:
            return controlled
        if "+" in mode:
            return io.BufferedRandom(controlled, buffer_size=8192)
        return io.BufferedWriter(controlled, buffer_size=8192)

    monkeypatch.setattr(staging_module, "open", wrapped_open, raising=False)
    for request in requests:
        receiver.accept_chunk(request)
    assert observed_buffering and set(observed_buffering) == {0}
    assert partial_calls >= (len(data) // 137)
    staged = receiver.finalize("op-partial", stream_sha256=sha256_hex(data))
    assert staged.read_bytes() == data


def test_mid_write_oserror_truncates_back_to_persisted_without_buffered_reflush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-write ENOSPC and secondary rollback fsync errors still truncate back to persisted."""
    import hyprial.daemon.impl.teleport.staging as staging_module

    receiver = _receiver(tmp_path)
    data = stream_bytes(SMALL * 2 + 63)
    requests = chunk_requests(data, "op-mid-fail", chunk_bytes=SMALL)
    directory = receiver.prepare("op-mid-fail")
    stream_path = directory / "stream"

    observed_buffering: list[int] = []
    fault = {"mode": "enospc_after_prefix", "prefix": 250, "writes_in_open": 0, "fail_fsync": True}
    real_open = builtins.open
    real_fsync = os.fsync

    def wrapped_open(file, mode="r", buffering=-1, *args, **kwargs):
        if Path(file) != stream_path:
            return real_open(file, mode, buffering=buffering, *args, **kwargs)
        observed_buffering.append(buffering)
        fault["writes_in_open"] = 0
        raw = real_open(file, mode, buffering=0, *args, **kwargs)

        def faulty_write(inner: io.FileIO, view: memoryview) -> int | None:
            fault["writes_in_open"] += 1
            if fault["mode"] == "ok":
                return inner.write(view)
            if fault["writes_in_open"] == 1:
                return inner.write(view[: fault["prefix"]])
            if fault["mode"] == "zero_progress":
                if fault["writes_in_open"] > 8:
                    raise RuntimeError("unbounded zero-progress write loop")
                return 0
            if fault["mode"] == "none_progress":
                if fault["writes_in_open"] > 8:
                    raise RuntimeError("unbounded none-progress write loop")
                return None
            raise OSError(errno.ENOSPC, "simulated mid-write enospc")

        controlled = _ControlledRawStream(raw, faulty_write)
        if buffering == 0:
            return controlled
        if "+" in mode:
            return io.BufferedRandom(controlled, buffer_size=8192)
        return io.BufferedWriter(controlled, buffer_size=8192)

    def faulty_fsync(fd: int) -> None:
        if fault["fail_fsync"]:
            raise OSError(errno.ENOSPC, "simulated rollback fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(staging_module, "open", wrapped_open, raising=False)
    monkeypatch.setattr(os, "fsync", faulty_fsync)

    with pytest.raises(OSError, match="simulated mid-write enospc"):
        receiver.accept_chunk(requests[0])
    assert receiver.persisted("op-mid-fail") == 0
    assert not stream_path.exists() or stream_path.stat().st_size == 0

    fault["mode"] = "ok"
    fault["fail_fsync"] = False
    assert receiver.accept_chunk(requests[0]) == SMALL
    assert stream_path.read_bytes() == data[:SMALL]

    fault["mode"] = "enospc_after_prefix"
    fault["prefix"] = 300
    fault["fail_fsync"] = True
    with pytest.raises(OSError, match="simulated mid-write enospc"):
        receiver.accept_chunk(requests[1])
    assert receiver.persisted("op-mid-fail") == SMALL
    assert stream_path.stat().st_size == SMALL
    assert stream_path.read_bytes() == data[:SMALL]

    for stall_mode in ("zero_progress", "none_progress"):
        fault["mode"] = stall_mode
        fault["prefix"] = 120
        fault["fail_fsync"] = False
        with pytest.raises(OSError, match="no progress"):
            receiver.accept_chunk(requests[1])
        assert receiver.persisted("op-mid-fail") == SMALL
        assert stream_path.stat().st_size == SMALL
        assert stream_path.read_bytes() == data[:SMALL]

    assert observed_buffering and set(observed_buffering) == {0}

    fault["mode"] = "ok"
    assert receiver.accept_chunk(requests[1]) == SMALL * 2
    assert receiver.accept_chunk(requests[2]) == len(data)
    staged = receiver.finalize("op-mid-fail", stream_sha256=sha256_hex(data))
    assert staged.read_bytes() == data
