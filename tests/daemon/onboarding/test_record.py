"""Bootstrap records are private lifecycle metadata, never ordinary readiness."""

import json
import os
import stat
from dataclasses import replace

import pytest

from hyprial.daemon.impl.onboarding.records import BootstrapRecord


def test_publish_private_record_without_normal_daemon_marker(tmp_path):
    record = BootstrapRecord.current(str(tmp_path / "ipc.sock"))
    record.publish(tmp_path)
    path = tmp_path / "bootstrap.json"
    value = json.loads(path.read_text())
    assert value == {
        "pid": os.getpid(), "birthIdentity": record.birth_identity,
        "epoch": record.epoch, "socket": str(tmp_path / "ipc.sock"),
        "phase": "awaiting-auth",
    }
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not (tmp_path / "daemon.json").exists()
    assert not (tmp_path / ".active_daemon").exists()
    assert BootstrapRecord.read(tmp_path) == record


def test_phase_update_and_matching_cleanup(tmp_path):
    record = BootstrapRecord.current(str(tmp_path / "ipc.sock"))
    record.publish(tmp_path)
    updated = replace(record, phase="authenticating")
    updated.publish(tmp_path)
    assert BootstrapRecord.read(tmp_path) == updated
    assert record.remove_if_owned(tmp_path)
    assert BootstrapRecord.read(tmp_path) is None
    assert not record.remove_if_owned(tmp_path)


@pytest.mark.parametrize("field,value", [
    ("epoch", "other-epoch"), ("pid", 123456),
    ("birth_identity", "other-birth"), ("socket", "/other/socket"),
])
def test_foreign_record_is_never_overwritten_or_removed(tmp_path, field, value):
    record = BootstrapRecord.current(str(tmp_path / "ipc.sock"))
    foreign = replace(record, **{field: value})
    foreign.publish(tmp_path)
    before = (tmp_path / "bootstrap.json").read_bytes()
    with pytest.raises(RuntimeError, match="another bootstrap"):
        record.publish(tmp_path)
    assert not record.remove_if_owned(tmp_path)
    assert (tmp_path / "bootstrap.json").read_bytes() == before


@pytest.mark.parametrize("raw", ['{', '[]', '{}', '{"pid":true}'])
def test_corrupt_record_is_not_empty_and_is_preserved(tmp_path, raw):
    path = tmp_path / "bootstrap.json"
    path.write_text(raw)
    record = BootstrapRecord.current(str(tmp_path / "ipc.sock"))
    with pytest.raises(ValueError):
        BootstrapRecord.read(tmp_path)
    with pytest.raises(ValueError):
        record.remove_if_owned(tmp_path)
    assert path.read_text() == raw


def test_unknown_birth_cannot_publish_record(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "hyprial.daemon.impl.onboarding.records.process_birth_identity", lambda pid: None
    )
    with pytest.raises(RuntimeError, match="birth identity"):
        BootstrapRecord.current(str(tmp_path / "ipc.sock"))
    assert list(tmp_path.iterdir()) == []


def test_record_rejects_extra_fields_and_ready_phase(tmp_path):
    record = BootstrapRecord.current(str(tmp_path / "ipc.sock"))
    record.publish(tmp_path)
    path = tmp_path / "bootstrap.json"
    value = json.loads(path.read_text())
    value["token"] = "not-a-real-token"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        BootstrapRecord.read(tmp_path)
    with pytest.raises(ValueError):
        replace(record, phase="ready")


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink filesystem regression")
def test_symlink_record_cannot_target_another_file(tmp_path):
    target = tmp_path / "other-record"
    target.write_text("preserve this file")
    path = tmp_path / "bootstrap.json"
    path.symlink_to(target)
    record = BootstrapRecord.current(str(tmp_path / "ipc.sock"))
    with pytest.raises(ValueError, match="symlink"):
        record.publish(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        record.remove_if_owned(tmp_path)
    assert path.is_symlink()
    assert target.read_text() == "preserve this file"


def test_record_rejects_oversized_regular_file(tmp_path):
    record = BootstrapRecord.current(str(tmp_path / "ipc.sock"))
    record.publish(tmp_path)
    path = tmp_path / "bootstrap.json"
    value = json.loads(path.read_text())
    value["epoch"] = "e" * 65537
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="size bound"):
        BootstrapRecord.read(tmp_path)


def test_record_rejects_replacement_between_stat_and_open(tmp_path, monkeypatch):
    from pathlib import Path

    record = BootstrapRecord.current(str(tmp_path / "ipc.sock"))
    record.publish(tmp_path)
    path = tmp_path / "bootstrap.json"
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(path.read_bytes())
    original = Path.lstat
    replaced = False

    def switch_after_stat(self, *args, **kwargs):
        nonlocal replaced
        observed = original(self, *args, **kwargs)
        if self == path and not replaced:
            replaced = True
            replacement.replace(path)
        return observed

    monkeypatch.setattr(Path, "lstat", switch_after_stat)
    with pytest.raises(ValueError, match="changed during read"):
        BootstrapRecord.read(tmp_path)


def test_record_directory_rejected_before_open(tmp_path):
    (tmp_path / "bootstrap.json").mkdir()
    with pytest.raises(ValueError, match="regular file"):
        BootstrapRecord.read(tmp_path)
