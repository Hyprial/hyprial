"""In-memory OrgFS facade for membership tests.

Only the facade calls the membership services make are implemented, and every
write goes through the same protected-id function the real facade uses, so a
record that the real store would refuse under a wrong path is refused here
too.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from hyprial.daemon.impl.orgfs.api import NodeInfo, OrgFsError, SpaceInfo
from hyprial.identity import org_from_space_name, protected_directory_doc_id


@dataclass
class _Node:
    node_id: str
    name: str
    kind: str
    path: str
    doc_id: str | None = None
    version: str = ""


@dataclass
class FakeFacade:
    """A tiny OrgFS: enough for personal orgs, agent rows and receivers."""

    owner: str = "owner@example"
    _spaces: dict[str, SpaceInfo] = field(default_factory=dict)
    _meta: dict[str, dict[str, object]] = field(default_factory=dict)
    _nodes: dict[str, dict[str, _Node]] = field(default_factory=dict)
    _content: dict[tuple[str, str], str] = field(default_factory=dict)

    # -- spaces -------------------------------------------------------------

    def create_space(self, name: str, *, space_id: str | None = None) -> SpaceInfo:
        space_id = space_id or f"space-{len(self._spaces) + 1}"
        info = SpaceInfo(space_id, name, self.owner, "2026-10-31T00:00:00Z")
        self._spaces[space_id] = info
        self._meta[space_id] = {"name": name, "owner": self.owner, "spaceId": space_id}
        self._nodes[space_id] = {}
        return info

    def spaces(self) -> tuple[SpaceInfo, ...]:
        return tuple(self._spaces.values())

    def space_meta(self, space_id: str) -> dict[str, object]:
        return dict(self._meta.get(space_id, {}))

    def update_space_meta(
        self, space_id: str, values: dict[str, object]
    ) -> dict[str, object]:
        self._meta.setdefault(space_id, {}).update(values)
        return dict(self._meta[space_id])

    # -- tree ---------------------------------------------------------------

    def _doc_id(self, space_id: str, path: str) -> str | None:
        space = self._spaces[space_id]
        if org_from_space_name(space.name) is None:
            return None
        return protected_directory_doc_id(
            space_id, path, space_owner=space.owner
        )

    def _resolve(self, space_id: str, node: str) -> _Node | None:
        nodes = self._nodes.get(space_id, {})
        if node.startswith("id:"):
            wanted = node[3:]
            return next(
                (entry for entry in nodes.values() if entry.node_id == wanted), None
            )
        return nodes.get(node)

    def stat(self, space_id: str, node: str) -> NodeInfo:
        entry = self._resolve(space_id, node)
        if entry is None:
            raise OrgFsError("unknown-doc", {"path": node})
        return self._info(space_id, entry)

    def mkdir(self, space_id: str, path: str) -> NodeInfo:
        if path not in self._nodes.get(space_id, {}):
            self._nodes[space_id][path] = _Node(
                node_id=f"node-{path}", name=path.rsplit("/", 1)[-1], kind="dir", path=path
            )
        return self.stat(space_id, path)

    def listdir(self, space_id: str, node: str) -> tuple[NodeInfo, ...]:
        prefix = node.rstrip("/") + "/"
        children: list[NodeInfo] = []
        seen: set[str] = set()
        for path, entry in self._nodes.get(space_id, {}).items():
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix):]
            name = rest.split("/", 1)[0]
            if name in seen:
                continue
            seen.add(name)
            child = self._nodes[space_id].get(prefix + name)
            if child is None or "/" in rest:
                child = _Node(
                    node_id=f"node-{prefix + name}",
                    name=name,
                    kind="dir",
                    path=prefix + name,
                )
            children.append(self._info(space_id, child))
        return tuple(children)

    def _info(self, space_id: str, entry: _Node) -> NodeInfo:
        return NodeInfo(
            space_id=space_id,
            node_id=entry.node_id,
            kind=entry.kind,  # type: ignore[arg-type]
            name=entry.name,
            path=entry.path,
            size=None,
            blob_hash=None,
            doc_id=entry.doc_id,
            version=entry.version,
            modified_by=self.owner,
            modified_via=None,
            name_conflict=False,
            deleted=False,
            uri=f"orgfs:{self.owner}:{space_id}:{entry.node_id}",
        )

    # -- documents ----------------------------------------------------------

    def write_text(
        self,
        space_id: str,
        path: str,
        content: str,
        *,
        base_version: str | None = None,
        expect_version: str | None = None,
        create_only: bool = False,
    ) -> NodeInfo:
        nodes = self._nodes[space_id]
        existing = nodes.get(path)
        if create_only and existing is not None:
            raise OrgFsError("stale-write", {"reason": "create_only"})
        if expect_version is not None and (
            existing is None or existing.version != expect_version
        ):
            raise OrgFsError("stale-write", {"expected": expect_version})
        doc_id = self._doc_id(space_id, path)
        version = hashlib.sha256(
            json.dumps([content, existing.version if existing else ""]).encode()
        ).hexdigest()[:12]
        node_id = existing.node_id if existing is not None else f"node-{path}"
        nodes[path] = _Node(
            node_id=node_id,
            name=path.rsplit("/", 1)[-1],
            kind="doc",
            path=path,
            doc_id=doc_id,
            version=version,
        )
        self._content[(space_id, node_id)] = content
        return self.stat(space_id, path)

    def read_text(self, space_id: str, node: str) -> tuple[str, str]:
        entry = self.stat(space_id, node)
        return self._content[(space_id, entry.node_id)], entry.version
