"""UsageReceipt log recovery: local failures are ReceiptLogUnavailable, torn rows never merge."""
from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.provider_keys import storage
from hyprial.daemon.impl.provider_keys.storage import receipts
from hyprial.identity import ProviderKeys
from hyprial.kernel.impl.manysys.fs import private_access

KEY_ID = "pk.mfrggzdfmztwq2lknnwg23tpob"
MAX_SAFE = 2**53 - 1


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "at": 1_700_000_000_000, "owner": "casdoor-sub-1", "keyId": KEY_ID, "provider": "openai",
        "agent": "0123abcd-4567-4890-abcd-ef0123456789", "agentId": "a.abcdefghijklmnopqrstuvwxyz",
        "deviceUid": "device-1", "dekVersion": 1, "grantDigest": "a" * 64, "outcome": "injected",
    }
    row.update(overrides)
    return row


def _log(state: Path) -> Path:
    return state / "provider-keys" / "receipts.jsonl"


def test_unavailable_is_a_fixed_message_os_error_not_a_store_code() -> None:
    error = receipts.ReceiptLogUnavailable()
    assert isinstance(error, OSError)
    assert not isinstance(error, ProviderKeys.Error)
    assert str(error) == "usage receipt log unavailable"
    assert storage.ReceiptLogUnavailable is receipts.ReceiptLogUnavailable


def test_foreign_owner_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    receipts.append_receipt(tmp_path, _row())
    real_uid = os.getuid()
    monkeypatch.setattr(private_access.os, "getuid", lambda: real_uid + 1)
    with pytest.raises(receipts.ReceiptLogUnavailable):
        receipts.append_receipt(tmp_path, _row(at=2))
    with pytest.raises(receipts.ReceiptLogUnavailable):
        receipts.last_used(tmp_path)


class _ForeignOwned:
    """A stat result whose st_uid is replaced; every other field is the real one."""

    def __init__(self, real: os.stat_result, uid: int) -> None:
        self._real = real
        self.st_uid = uid

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def test_foreign_owned_log_file_in_own_partition_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    receipts.append_receipt(tmp_path, _row(at=10))
    before = _log(tmp_path).read_bytes()
    real_fstat = os.fstat
    foreign = os.getuid() + 1
    seen: list[str] = []

    def fstat(fd: int) -> Any:
        info = real_fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            seen.append("dir-own" if info.st_uid == os.getuid() else "dir-foreign")
            return info
        seen.append("file")
        return _ForeignOwned(info, foreign)

    monkeypatch.setattr(private_access.os, "fstat", fstat)
    with pytest.raises(receipts.ReceiptLogUnavailable) as caught:
        receipts.append_receipt(tmp_path, _row(at=20))
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert seen and set(seen) == {"file"}
    seen.clear()
    with pytest.raises(receipts.ReceiptLogUnavailable):
        receipts.last_used(tmp_path)
    assert seen and set(seen) == {"file"}
    monkeypatch.undo()
    assert _log(tmp_path).read_bytes() == before
    assert receipts.last_used(tmp_path) == {KEY_ID: 10}


def test_max_safe_integers_round_trip(tmp_path: Path) -> None:
    receipts.append_receipt(tmp_path, _row(at=MAX_SAFE, dekVersion=MAX_SAFE))
    assert receipts.last_used(tmp_path) == {KEY_ID: MAX_SAFE}


def test_unsafe_integers_in_the_log_are_ignored(tmp_path: Path) -> None:
    receipts.append_receipt(tmp_path, _row(at=7))
    with _log(tmp_path).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(_row(at=2**53)) + "\n")
    assert receipts.last_used(tmp_path) == {KEY_ID: 7}


def test_append_after_a_torn_trailing_line_starts_a_new_line(tmp_path: Path) -> None:
    receipts.append_receipt(tmp_path, _row(at=10))
    with _log(tmp_path).open("a", encoding="utf-8") as stream:
        stream.write('{"at":99,"owner":"casdoor-sub-1","keyId":"' + KEY_ID)
    receipts.append_receipt(tmp_path, _row(at=20))
    lines = _log(tmp_path).read_text(encoding="utf-8").split("\n")
    assert lines[-1] == ""
    assert json.loads(lines[-2]) == _row(at=20)
    assert receipts.last_used(tmp_path) == {KEY_ID: 20}


def test_failed_fsync_truncates_the_row_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    receipts.append_receipt(tmp_path, _row(at=10))
    before = _log(tmp_path).read_bytes()

    def broken_fsync(fd: int) -> None:
        raise OSError(5, "synthetic fsync failure")

    monkeypatch.setattr(private_access.os, "fsync", broken_fsync)
    with pytest.raises(receipts.ReceiptLogUnavailable) as caught:
        receipts.append_receipt(tmp_path, _row(at=20))
    assert "synthetic" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert _log(tmp_path).read_bytes() == before


def test_short_write_failure_truncates_the_partial_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    receipts.append_receipt(tmp_path, _row(at=10))
    before = _log(tmp_path).read_bytes()
    real_write = os.write
    attempts: list[int] = []

    def tearing_write(fd: int, data: Any) -> int:
        attempts.append(fd)
        if len(attempts) == 1:
            return real_write(fd, bytes(data)[:7])
        raise OSError(28, "synthetic disk full")

    monkeypatch.setattr(private_access.os, "write", tearing_write)
    with pytest.raises(receipts.ReceiptLogUnavailable):
        receipts.append_receipt(tmp_path, _row(at=20))
    monkeypatch.undo()
    assert len(attempts) == 2
    assert _log(tmp_path).read_bytes() == before
    receipts.append_receipt(tmp_path, _row(at=30))
    assert receipts.last_used(tmp_path) == {KEY_ID: 30}


def test_rollback_never_cuts_a_concurrent_committed_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    receipts.append_receipt(tmp_path, _row(at=1))
    real_fsync = os.fsync
    failing_thread = threading.get_ident()
    outcome: list[BaseException | None] = []

    def concurrent_append() -> None:
        try:
            receipts.append_receipt(tmp_path, _row(at=2))
        except BaseException as error:  # pragma: no cover - surfaced by the assertion below
            outcome.append(error)
        else:
            outcome.append(None)

    other = threading.Thread(target=concurrent_append)

    def failing_fsync(fd: int) -> None:
        if threading.get_ident() != failing_thread:
            real_fsync(fd)
            return
        other.start()
        other.join(timeout=0.5)
        raise OSError(28, "synthetic disk full")

    monkeypatch.setattr(private_access.os, "fsync", failing_fsync)
    with pytest.raises(receipts.ReceiptLogUnavailable):
        receipts.append_receipt(tmp_path, _row(at=3))
    other.join(timeout=5)
    monkeypatch.undo()
    assert not other.is_alive()
    assert outcome == [None]
    rows = [json.loads(line) for line in _log(tmp_path).read_text(encoding="utf-8").splitlines()]
    assert rows == [_row(at=1), _row(at=2)]


def _partition(tmp_path: Path) -> Path:
    receipts.append_receipt(tmp_path, _row())
    _log(tmp_path).unlink()
    return _log(tmp_path)


def test_a_fifo_at_the_log_path_refuses_the_read_without_blocking(tmp_path: Path) -> None:
    os.mkfifo(_partition(tmp_path), 0o600)
    outcome: list[object] = []

    def read() -> None:
        try:
            outcome.append(receipts.last_used(tmp_path))
        except BaseException as error:
            outcome.append(error)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(timeout=5)
    if reader.is_alive():
        os.close(os.open(_log(tmp_path), os.O_WRONLY | os.O_NONBLOCK))
        reader.join(timeout=5)
        pytest.fail("reading the receipt log blocked on a FIFO")
    assert len(outcome) == 1 and isinstance(outcome[0], receipts.ReceiptLogUnavailable)


def test_a_fifo_at_the_log_path_refuses_the_append(tmp_path: Path) -> None:
    os.mkfifo(_partition(tmp_path), 0o600)
    with pytest.raises(receipts.ReceiptLogUnavailable):
        receipts.append_receipt(tmp_path, _row())


def test_a_deeply_nested_row_is_skipped_like_any_unparseable_row(tmp_path: Path) -> None:
    receipts.append_receipt(tmp_path, _row(at=1_700_000_000_005))
    with _log(tmp_path).open("a", encoding="ascii") as handle:
        handle.write("[" * 100_000 + "\n")
    assert receipts.last_used(tmp_path) == {KEY_ID: 1_700_000_000_005}
