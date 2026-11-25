"""Local UsageReceipt log (contract §3.4): exact fields, private, append-only, no symlinks."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.provider_keys.storage import receipts
from hyprial.kernel.impl.manysys.fs import private_access

KEY_ID = "pk.mfrggzdfmztwq2lknnwg23tpob"
OTHER_KEY = "pk.aaaaaaaaaaaaaaaaaaaaaaaaaa"
SENTINEL = "sk-test-SENTINEL-not-a-real-key"


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "at": 1_700_000_000_000,
        "owner": "casdoor-sub-1",
        "keyId": KEY_ID,
        "provider": "openai",
        "agent": "0123abcd-4567-4890-abcd-ef0123456789",
        "agentId": "a.abcdefghijklmnopqrstuvwxyz",
        "deviceUid": "device-1",
        "dekVersion": 1,
        "grantDigest": "a" * 64,
        "outcome": "injected",
    }
    row.update(overrides)
    return row


def _log(state: Path) -> Path:
    return state / "provider-keys" / "receipts.jsonl"


def _assert_local_refusal(error: BaseException) -> None:
    assert isinstance(error, OSError)
    assert not hasattr(error, "code") or error.code != "PROVIDER_STORE_UNAVAILABLE"
    assert str(error) == receipts.ReceiptLogUnavailable().args[0]
    assert error.__cause__ is None and error.__suppress_context__


def test_fields_are_exactly_contract_fields() -> None:
    assert receipts.RECEIPT_FIELDS == frozenset({
        "at", "owner", "keyId", "provider", "agent", "agentId", "deviceUid", "dekVersion", "grantDigest", "outcome",
    })


def test_append_creates_private_dir_and_file(tmp_path: Path) -> None:
    state = tmp_path / "state"
    receipts.append_receipt(state, _row())
    log = _log(state)
    assert stat.S_IMODE(log.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    lines = log.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [_row()]


def test_append_only_preserves_previous_rows_and_fsyncs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(private_access.os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
    receipts.append_receipt(tmp_path, _row())
    receipts.append_receipt(tmp_path, _row(at=2, outcome="refused:PROVIDER_GRANT_REVOKED"))
    rows = [json.loads(line) for line in _log(tmp_path).read_text(encoding="utf-8").splitlines()]
    assert [row["outcome"] for row in rows] == ["injected", "refused:PROVIDER_GRANT_REVOKED"]
    assert len(synced) == 2


def test_existing_loose_permissions_are_tightened(tmp_path: Path) -> None:
    directory = tmp_path / "provider-keys"
    directory.mkdir(mode=0o755)
    os.chmod(directory, 0o755)
    (directory / "receipts.jsonl").write_text("", encoding="utf-8")
    os.chmod(directory / "receipts.jsonl", 0o644)
    receipts.append_receipt(tmp_path, _row())
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / "receipts.jsonl").stat().st_mode) == 0o600


def test_symlinked_directory_is_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / "provider-keys").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(receipts.ReceiptLogUnavailable) as caught:
        receipts.append_receipt(state, _row())
    _assert_local_refusal(caught.value)
    assert list(elsewhere.iterdir()) == []
    with pytest.raises(receipts.ReceiptLogUnavailable):
        receipts.last_used(state)


def test_symlinked_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("keep", encoding="utf-8")
    directory = tmp_path / "provider-keys"
    directory.mkdir(mode=0o700)
    (directory / "receipts.jsonl").symlink_to(target)
    with pytest.raises(receipts.ReceiptLogUnavailable) as caught:
        receipts.append_receipt(tmp_path, _row())
    _assert_local_refusal(caught.value)
    assert target.read_text(encoding="utf-8") == "keep"
    with pytest.raises(receipts.ReceiptLogUnavailable):
        receipts.last_used(tmp_path)


def test_hard_linked_file_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "provider-keys"
    directory.mkdir(mode=0o700)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("", encoding="utf-8")
    os.link(outside, directory / "receipts.jsonl")
    with pytest.raises(receipts.ReceiptLogUnavailable):
        receipts.append_receipt(tmp_path, _row())
    assert outside.read_text(encoding="utf-8") == ""


def test_non_directory_partition_is_refused(tmp_path: Path) -> None:
    (tmp_path / "provider-keys").write_text("", encoding="utf-8")
    with pytest.raises(receipts.ReceiptLogUnavailable):
        receipts.append_receipt(tmp_path, _row())


@pytest.mark.parametrize("field", ["value", "secret", "dek", "plaintext", "Outcome"])
def test_extra_field_is_rejected_without_echo(tmp_path: Path, field: str) -> None:
    with pytest.raises(receipts.InvalidReceipt) as caught:
        receipts.append_receipt(tmp_path, _row(**{field: SENTINEL}))
    assert SENTINEL not in str(caught.value) and SENTINEL not in repr(caught.value)
    assert not _log(tmp_path).exists()


@pytest.mark.parametrize("field", sorted(receipts.RECEIPT_FIELDS))
def test_missing_field_is_rejected(tmp_path: Path, field: str) -> None:
    row = _row()
    del row[field]
    with pytest.raises(receipts.InvalidReceipt):
        receipts.append_receipt(tmp_path, row)


@pytest.mark.parametrize("overrides", [
    {"at": -1}, {"at": True}, {"at": 1.5}, {"at": "1"}, {"at": 2**53}, {"dekVersion": 2**53},
    {"at": 10**30}, {"dekVersion": 2**64},
    {"owner": ""}, {"owner": "a\x00b"}, {"owner": "x" * 257}, {"owner": None},
    {"keyId": "pk.short"}, {"keyId": SENTINEL},
    {"provider": "../x"}, {"provider": ""}, {"provider": None},
    {"agent": "0123ABCD-4567-4890-ABCD-EF0123456789"}, {"agent": "0123abcd45674890abcdef0123456789"},
    {"agentId": "a.ABC"}, {"agentId": "abcdefghijklmnopqrstuvwxyz"},
    {"deviceUid": ""}, {"deviceUid": "d\n1"}, {"deviceUid": None},
    {"dekVersion": 0}, {"dekVersion": True}, {"dekVersion": "1"},
    {"grantDigest": "A" * 64}, {"grantDigest": "a" * 63}, {"grantDigest": SENTINEL},
    {"outcome": "ok"}, {"outcome": "refused:"}, {"outcome": "refused:lower"}, {"outcome": f"refused:{SENTINEL}"},
    {"outcome": "injected", "keyId": None}, {"outcome": "injected", "dekVersion": None},
    {"outcome": "injected", "grantDigest": None},
])
def test_bad_values_are_rejected_without_echo(tmp_path: Path, overrides: dict[str, Any]) -> None:
    with pytest.raises(receipts.InvalidReceipt) as caught:
        receipts.append_receipt(tmp_path, _row(**overrides))
    assert SENTINEL not in str(caught.value)
    assert not _log(tmp_path).exists()


def test_non_dict_row_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(receipts.InvalidReceipt):
        receipts.append_receipt(tmp_path, [("at", 1)])  # type: ignore[arg-type]


def test_refusal_may_lack_key_material(tmp_path: Path) -> None:
    row = _row(keyId=None, dekVersion=None, grantDigest=None, outcome="refused:PROVIDER_KEY_NOT_FOUND")
    receipts.append_receipt(tmp_path, row)
    assert json.loads(_log(tmp_path).read_text(encoding="utf-8")) == row


def test_last_used_takes_latest_injection_per_key(tmp_path: Path) -> None:
    assert receipts.last_used(tmp_path) == {}
    receipts.append_receipt(tmp_path, _row(at=10))
    receipts.append_receipt(tmp_path, _row(at=30))
    receipts.append_receipt(tmp_path, _row(at=20))
    receipts.append_receipt(tmp_path, _row(at=99, outcome="refused:PROVIDER_KEY_UNSEAL_FAILED"))
    receipts.append_receipt(tmp_path, _row(at=5, keyId=OTHER_KEY))
    with _log(tmp_path).open("a", encoding="utf-8") as stream:
        stream.write("{corrupt\n")
        stream.write(json.dumps(_row(at=500, extra=1)) + "\n")
        stream.write("\n")
    assert receipts.last_used(tmp_path) == {KEY_ID: 30, OTHER_KEY: 5}
