"""Crash and compaction windows of the durable custody journal."""
import json
import os

import pytest

from hyprial.kernel import ProcessLivenessProbeError, atomic_json_write
from hyprial.daemon.impl.processes.storage.journal import CustodyJournal


def test_snapshot_commit_before_log_truncation_cannot_resurrect(tmp_path, monkeypatch):
    path = tmp_path / "orphans.json"
    journal = CustodyJournal(path, atomic_json_write)
    journal.commit({"p": {"orphan_id": "p"}})
    journal.commit({"q": {"orphan_id": "q"}})
    journal.commit({"q": None})
    old_log = journal.log_path.read_bytes()
    journal.compact()
    journal.log_path.write_bytes(old_log)  # crash left pre-compaction log in place
    recovered = CustodyJournal(path, atomic_json_write)
    assert set(recovered.rows) == {"p"}
    recovered.commit({"r": {"orphan_id": "r"}})
    assert set(CustodyJournal(path, atomic_json_write).rows) == {"p", "r"}


def test_torn_tail_is_discarded_before_next_append(tmp_path):
    path = tmp_path / "orphans.json"
    journal = CustodyJournal(path, atomic_json_write)
    journal.commit({"p": {"orphan_id": "p"}})
    journal.commit({"q": {"orphan_id": "q"}})
    with journal.log_path.open("ab") as stream:
        stream.write(b'{"seq":2,"upsert":')
    recovered = CustodyJournal(path, atomic_json_write)
    assert set(recovered.rows) == {"p", "q"}
    recovered.commit({"q": None})
    assert set(CustodyJournal(path, atomic_json_write).rows) == {"p"}


@pytest.mark.parametrize("line", [b"not-json\n", b'{"seq":99,"upsert":[],"delete":[]}\n'])
def test_complete_invalid_record_fails_loud(tmp_path, line):
    path = tmp_path / "orphans.json"
    journal = CustodyJournal(path, atomic_json_write)
    journal.commit({"p": {"orphan_id": "p"}})
    journal.log_path.write_bytes(line)
    with pytest.raises(ProcessLivenessProbeError, match="journal invalid"):
        CustodyJournal(path, atomic_json_write)


def test_failed_append_cannot_be_acknowledged_by_subsequent_noop(tmp_path, monkeypatch):
    journal = CustodyJournal(tmp_path / "orphans.json", atomic_json_write)
    journal.commit({"p": {"orphan_id": "p"}})
    monkeypatch.setattr(os, "fsync", lambda _: (_ for _ in ()).throw(OSError("disk failed")))
    with pytest.raises(OSError, match="disk failed"):
        journal.commit({"p": None})
    with pytest.raises(ProcessLivenessProbeError, match="reopen required"):
        journal.commit({"p": None})


def test_unchanged_and_changed_bytes_are_counted(tmp_path):
    journal = CustodyJournal(tmp_path / "orphans.json", atomic_json_write)
    row = {"orphan_id": "p"}
    journal.commit({"p": row})
    for _ in range(1000):
        journal.commit({"p": row})
    assert journal.stats["snapshotWrites"] == 1
    assert journal.stats["appendWrites"] == 0
    assert journal.stats["unchangedSaves"] == 1000
    journal.commit({"p": {**row, "owner": None}})
    assert journal.stats["appendWrites"] == 1
    assert journal.stats["appendBytes"] == len(journal.log_path.read_bytes())
    assert json.loads(journal.log_path.read_text())["upsert"][0]["owner"] is None
