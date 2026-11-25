"""Read ordinary OrgFS documents for the optional PAC overview projection.

Every readable document remains available by node with its current source and
parse diagnostics. A usable id/pacs view may group existing PAC graphs; this
module has no association-round state, task transitions, writes or approvals.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from hyprial.daemon.impl.pac.views.overview import Request
from hyprial.daemon.impl.pac.views.work_items import WorkItem, parse_work_item

WORK_DIR = "work"


class MissionParseError(ValueError):
    """Optional graph-grouping metadata is unavailable; the source is readable."""


def parse_mission(text: str) -> dict[str, Any]:
    """Project only current id/pacs metadata for read-only graph grouping."""

    item = parse_work_item(text)
    if item.diagnostics:
        raise MissionParseError("; ".join(item.diagnostics))
    if not item.id.strip():
        raise MissionParseError("optional graph grouping needs a non-empty text id")
    pacs = item.head.get("pacs", [])
    if not isinstance(pacs, list) or any(not isinstance(graph, str) for graph in pacs):
        raise MissionParseError("optional graph grouping needs pacs as a list of strings")
    return {**item.to_dict(), "pacs": [graph.strip() for graph in pacs if graph.strip()], "associationEligible": True}


def load_missions(request: Request, space_id: str, *, base: str = WORK_DIR) -> dict[str, Any]:
    """Read each current document, including conflicting and malformed files."""

    listing = request("orgfs.ls", {"spaceId": space_id, "path": base, "recursive": True})
    missions: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    conflict_nodes: dict[str, list[str]] = {}
    for node in listing.get("nodes") or []:
        if not isinstance(node, dict) or node.get("deleted") or node.get("kind") not in (None, "doc"):
            continue
        name, node_id = node.get("name"), node.get("node_id")
        if not isinstance(name, str) or not name or not isinstance(node_id, str) or not node_id:
            continue
        path = node.get("path")
        if not isinstance(path, str) or not path:
            path = f"{base}/{name}"
        conflict = bool(node.get("name_conflict"))
        if conflict:
            conflict_nodes.setdefault(path, []).append(node_id)
        text = ""
        version = None
        try:
            read = request("orgfs.read", {"spaceId": space_id, "path": f"id:{node_id}", "waitSeconds": 10})
            if not isinstance(read.get("text"), str):
                raise ValueError("document content is not available as text")
            text, version = read["text"], read.get("version")
            document = parse_work_item(text).to_dict()
            try:
                mission = parse_mission(text)
            except MissionParseError as error:
                problem = str(error)
                if problem not in document["diagnostics"]:
                    document["diagnostics"].append(problem)
            else:
                if not conflict:
                    document = mission
                    missions.append({**mission, "path": path, "nodeId": node_id, "version": version})
        except MemoryError:
            raise
        except Exception as error:
            document = WorkItem(text, {}, name, (), (f"unreadable: {error}",)).to_dict()
        if conflict:
            document["diagnostics"].append("name conflict: showing this node's current document; graph grouping is unavailable")
        document.setdefault("associationEligible", False)
        document.update(path=path, nodeId=node_id, version=version, modifiedBy=node.get("modified_by"))
        if not document["id"] or conflict:
            document["id"] = f"document:{space_id}:{node_id}"
        document["title"] = document["title"] or name
        documents.append(document)
    ids = Counter(document["id"] for document in documents)
    duplicate_nodes = set()
    for document in documents:
        if ids[document["id"]] > 1:
            duplicate_nodes.add(document["nodeId"])
            document["id"] = f"document:{space_id}:{document['nodeId']}"
            document["associationEligible"] = False
            document["diagnostics"].append("duplicate metadata id: showing this node's current document; graph grouping is unavailable")
    return {
        "missions": sorted((item for item in missions if item["nodeId"] not in duplicate_nodes), key=lambda item: item["id"]),
        "documents": sorted(documents, key=lambda item: (item["path"], item["nodeId"])),
        "unparseable": [{"path": item["path"], "reason": "; ".join(item["diagnostics"])} for item in documents if item["diagnostics"]],
        "conflicts": [{"path": path, "nodeIds": sorted(set(ids))} for path, ids in sorted(conflict_nodes.items())],
    }
