"""Current document loading is read-only and does not enforce task history."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.pac.views import missions


def mission_text(item_id: str = "M-owner-0001", status: str = "done", pacs: str = "[wf-a]") -> str:
    return (
        f"## {item_id} Current task\n\n```yaml\nid: {item_id}\nstatus: {status}\n"
        f"owner: ordinary owner\npacs: {pacs}\n"
        "checklist: [{text: unfinished, done: false}]\n"
        "ownerActions: [{action: anything, verified: true, nested: keep}]\n"
        "```\n\nOriginal body.\n## Log\nFreely edited prose.\n"
    )


def test_grouping_reads_current_fields_without_approval_or_status_rules() -> None:
    text = mission_text(status="custom-state")
    view = missions.parse_mission(text)
    assert view["id"] == "M-owner-0001"
    assert view["title"] == "Current task"
    assert view["status"] == "custom-state"
    assert view["pacs"] == ["wf-a"]
    assert view["documentText"] == text
    assert "ownerActions" not in view and "unverifiedOperator" not in view


@pytest.mark.parametrize("text", ["ordinary prose", "```yaml\nid: [broken\n```", mission_text(pacs="42"), "```yaml\nstatus: custom\npacs: []\n```"])
def test_only_unusable_grouping_metadata_is_skipped(text: str) -> None:
    with pytest.raises(missions.MissionParseError):
        missions.parse_mission(text)


class Documents:
    def __init__(self, entries):
        self.entries = entries
        self.calls = []

    def __call__(self, method, params):
        self.calls.append((method, dict(params)))
        if method == "orgfs.ls":
            return {"nodes": [node for node, _ in self.entries]}
        assert method == "orgfs.read"
        entry = next(value for node, value in self.entries if params["path"] == f"id:{node['node_id']}")
        if isinstance(entry, Exception):
            raise entry
        return {"text": entry, "version": "current-v1"}


def test_load_preserves_plain_broken_and_conflicting_documents_by_node() -> None:
    good = mission_text()
    plain = "# Notes\n\nOrdinary non-task document."
    broken = "```yaml\nid: [broken\n```\nbody still readable"
    other = mission_text("M-owner-0002")
    request = Documents([
        ({"name": "task.md", "node_id": "good", "kind": "doc"}, good),
        ({"name": "README.md", "node_id": "plain"}, plain),
        ({"name": "broken.md", "node_id": "broken"}, broken),
        ({"name": "same.md", "node_id": "c1", "name_conflict": True}, other),
        ({"name": "same.md", "node_id": "c2", "name_conflict": True}, plain),
        ({"name": "removed.md", "node_id": "deleted", "deleted": True}, "deleted"),
        ({"name": "subdir", "node_id": "dir", "kind": "dir"}, "not read"),
    ])
    result = missions.load_missions(request, "space")
    documents = {entry["nodeId"]: entry for entry in result["documents"]}
    assert set(documents) == {"good", "plain", "broken", "c1", "c2"}
    assert [entry["id"] for entry in result["missions"]] == ["M-owner-0001"]
    assert documents["plain"]["documentText"] == plain
    assert documents["broken"]["documentText"] == broken
    assert documents["c1"]["documentText"] == other
    assert documents["c2"]["documentText"] == plain
    assert documents["c1"]["id"] != documents["c2"]["id"]
    assert all(documents[node]["diagnostics"] for node in ("plain", "broken", "c1", "c2"))
    assert result["conflicts"] == [{"path": "work/same.md", "nodeIds": ["c1", "c2"]}]
    assert {method for method, _ in request.calls} == {"orgfs.ls", "orgfs.read"}


def test_unreadable_document_reports_failure_without_hiding_other_documents() -> None:
    request = Documents([
        ({"name": "offline.md", "node_id": "offline"}, RuntimeError("content pending")),
        ({"name": "task.md", "node_id": "good"}, mission_text()),
    ])
    result = missions.load_missions(request, "space")
    assert len(result["documents"]) == 2
    assert result["documents"][0]["documentText"] == ""
    assert "content pending" in result["documents"][0]["diagnostics"][0]
    assert len(result["missions"]) == 1


def test_duplicate_metadata_ids_keep_both_documents_without_ambiguous_grouping() -> None:
    text = mission_text()
    request = Documents([
        ({"name": "a.md", "node_id": "a"}, text),
        ({"name": "b.md", "node_id": "b"}, text + "\nsecond body"),
    ])
    result = missions.load_missions(request, "space")
    assert result["missions"] == []
    assert [item["id"] for item in result["documents"]] == ["document:space:a", "document:space:b"]
    assert result["documents"][0]["documentText"] == text
    assert result["documents"][1]["documentText"] == text + "\nsecond body"
    assert all("duplicate metadata id" in item["reason"] for item in result["unparseable"])


def test_yaml_constructor_failure_does_not_hide_other_readable_documents() -> None:
    malformed = "```yaml\nopened: 2001-13-01\n```\nReadable body."
    request = Documents([
        ({"name": "broken.md", "node_id": "broken"}, malformed),
        ({"name": "task.md", "node_id": "good"}, mission_text()),
    ])
    loaded = missions.load_missions(request, "space")
    assert len(loaded["documents"]) == 2
    assert loaded["documents"][0]["documentText"] == malformed
    assert loaded["documents"][0]["diagnostics"]
    assert len(loaded["missions"]) == 1


def test_document_loader_does_not_swallow_memory_failure(monkeypatch) -> None:
    def exhausted(_text):
        raise MemoryError("synthetic memory failure")

    monkeypatch.setattr(missions, "parse_work_item", exhausted)
    request = Documents([({"name": "task.md", "node_id": "task"}, mission_text())])
    with pytest.raises(MemoryError, match="synthetic memory failure"):
        missions.load_missions(request, "space")


def test_nested_document_retains_its_path_and_current_body() -> None:
    request = Documents([({"name": "notes.md", "path": "work/archive/notes.md", "node_id": "nested"}, "nested plain text")])
    loaded = missions.load_missions(request, "space")
    assert request.calls[0] == ("orgfs.ls", {"spaceId": "space", "path": "work", "recursive": True})
    assert loaded["documents"][0]["path"] == "work/archive/notes.md"
    assert loaded["documents"][0]["documentText"] == "nested plain text"
