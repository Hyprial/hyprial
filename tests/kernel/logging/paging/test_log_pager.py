from pathlib import Path
import json

import pytest

from hyprial.kernel import LogPager, LogCursorInvalid


def line(number=1):
    return json.dumps({"ts": "2026-11-03T00:00:00Z", "level": "info", "component": "daemon", "event": "daemon.started", "number": number}).encode() + b"\n"


def test_snapshot_append_and_page_limit(tmp_path):
    log = tmp_path / "daemon.jsonl"
    log.write_bytes(line(1) + line(2))
    pager = LogPager(tmp_path)
    first = pager.read(limit=1)
    assert first["entries"][0]["number"] == 1
    assert first["nextCursor"]
    with log.open("ab") as stream:
        stream.write(line(3))
    second = pager.read(limit=1, cursor=first["nextCursor"])
    assert [entry["number"] for entry in second["entries"]] == [2]
    assert second["nextCursor"] is None


def test_rotated_truncated_and_tampered_cursor(tmp_path):
    log = tmp_path / "daemon.jsonl"
    log.write_bytes(line() * 3)
    pager = LogPager(tmp_path)
    cursor = pager.read(limit=1)["nextCursor"]
    with pytest.raises(LogCursorInvalid):
        pager.read(cursor=cursor + "a")
    log.rename(tmp_path / "old.jsonl")
    log.write_bytes(line() * 3)
    with pytest.raises(LogCursorInvalid):
        pager.read(cursor=cursor)


def test_damaged_and_missing_logs_are_partial(tmp_path):
    pager = LogPager(tmp_path / "missing")
    assert pager.read()["partial"] is True
    (tmp_path / "daemon.jsonl").write_bytes(b"garbage\n" + line())
    page = LogPager(tmp_path).read()
    assert len(page["entries"]) == 1
    assert page["partial"] is True
    assert "invalid-record" in page["partialReasons"]


def test_byte_budget_continues_without_full_file_read(tmp_path, monkeypatch):
    (tmp_path / "daemon.jsonl").write_bytes(b"x" * 5000 + b"\n" + line())
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: pytest.fail("whole-file read"))
    pager = LogPager(tmp_path, byte_budget=512, line_bytes=128)
    cursor = None
    pages = []
    for _ in range(20):
        page = pager.read(cursor=cursor)
        pages.append(page)
        cursor = page["nextCursor"]
        if cursor is None:
            break
    assert sum(len(page["entries"]) for page in pages) == 1
    assert pages[0]["partial"] is True
    assert pages[0]["nextCursor"]


def test_symlink_and_file_boundaries(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.jsonl").write_bytes(line())
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "escape").symlink_to(outside, target_is_directory=True)
    page = LogPager(logs).read()
    assert not page["entries"]
    assert page["partial"]
    for index in range(4):
        (logs / f"{index}.jsonl").write_bytes(line())
    page = LogPager(logs, max_files=2).read()
    assert len(page["entries"]) == 2
    assert "discovery-limit" in page["partialReasons"]


def test_budget_boundary_does_not_drop_a_valid_record(tmp_path):
    (tmp_path / "daemon.jsonl").write_bytes(line(1) + line(2) + line(3))
    pager = LogPager(tmp_path, byte_budget=len(line(1)) + 2)
    first = pager.read()
    second = pager.read(cursor=first["nextCursor"])
    assert [row["number"] for row in first["entries"] + second["entries"]] == [1, 2, 3]
    assert first["partialReasons"] == ["scan-limit"]


def test_truncation_and_expiry_fail_closed(tmp_path, monkeypatch):
    import hyprial.kernel.impl.log.paging as paging
    path = tmp_path / "daemon.jsonl"
    path.write_bytes(line() * 3)
    pager = LogPager(tmp_path)
    cursor = pager.read(limit=1)["nextCursor"]
    path.write_bytes(line())
    with pytest.raises(LogCursorInvalid):
        pager.read(cursor=cursor)
    path.write_bytes(line() * 3)
    cursor = pager.read(limit=1)["nextCursor"]
    monkeypatch.setattr(paging.time, "monotonic", lambda: 1e15)
    with pytest.raises(LogCursorInvalid):
        pager.read(cursor=cursor)


def test_rotated_directory_link_cannot_escape_snapshot(tmp_path):
    logs = tmp_path / "logs"
    nested = logs / "nested"
    nested.mkdir(parents=True)
    (nested / "events.jsonl").write_bytes(line() * 3)
    pager = LogPager(logs)
    cursor = pager.read(limit=1)["nextCursor"]
    outside = tmp_path / "outside"
    nested.rename(outside)
    nested.symlink_to(outside, target_is_directory=True)
    with pytest.raises(LogCursorInvalid):
        pager.read(cursor=cursor)


@pytest.mark.parametrize('level', [[], {}, None, False, 1, 'unknown'])
def test_invalid_level_preserves_neighbors_and_partial_accounting(tmp_path, level):
    broken = json.loads(line())
    broken['level'] = level
    (tmp_path / 'daemon.jsonl').write_bytes(line(1) + json.dumps(broken).encode() + b'\n' + line(2))
    page = LogPager(tmp_path).read()
    assert [entry['number'] for entry in page['entries']] == [1, 2]
    assert page['partial'] is True
    assert 'invalid-record' in page['partialReasons']
    assert page['skippedLines'] == 1
