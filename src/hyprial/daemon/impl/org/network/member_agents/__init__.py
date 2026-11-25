"""Owner-published Agent inventory for organization member views."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.orgfs.api import OrgFs, OrgFsError
from hyprial.daemon.impl.org.membership.context import for_app
from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.org.network.ipc import _invalid_device_diagnostics
from hyprial.daemon.impl.org.network.member_agents.exclusions import (
    OrgAgentExclusion as OrgAgentExclusion,
    OrgAgentExclusionStore as OrgAgentExclusionStore,
    derive_agent_org_labels as derive_agent_org_labels,
    exclusions_path as exclusions_path,
)
from hyprial.identity import (
    PEOPLE_DIR,
    directory_device_path,
    directory_owner_principal,
    identity_slug,
    protected_directory_doc_id,
)
from hyprial.kernel import DaemonRequestError, canonical_user_uri, is_agent_id_segment, parse_agent_uri


_ROW_KEYS = frozenset({"agentId", "agent", "name", "owner", "deviceId"})


def _bare(user: str) -> str:
    return directory_owner_principal(user).removeprefix("user:")


def _person_dir(owner: str) -> str:
    return f"{PEOPLE_DIR}/{identity_slug(_bare(owner))}"


def _path(owner: str, node_id: str) -> str:
    directory_device_path(owner, node_id)
    return f"{_person_dir(owner)}/agents-{node_id}.json"


def _rows(value: object, owner: str, node_id: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError("Agent inventory is not a list")
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != _ROW_KEYS:
            raise ValueError("Agent inventory row has the wrong fields")
        if not all(isinstance(field, str) and field for field in item.values()):
            raise ValueError("Agent inventory row has an invalid field")
        agent_id = item["agentId"]
        if not is_agent_id_segment(agent_id) or agent_id in seen:
            raise ValueError("Agent inventory row has a duplicate or invalid ID")
        if item["owner"] != canonical_user_uri(owner) or item["deviceId"] != node_id:
            raise ValueError("Agent inventory row names another owner or device")
        if parse_agent_uri(item["agent"]) != (owner, node_id, agent_id):
            raise ValueError("Agent inventory row has an invalid URI")
        seen.add(agent_id)
        rows.append(dict(item))
    return rows


class MemberAgentDirectory:
    def __init__(self, fs: OrgFs) -> None:
        self._fs = fs

    def publish(self, org: str, owner: str, node_id: str, agents: list[dict[str, str]]) -> bool:
        owner = _bare(owner)
        space = self._space(org)
        if owner not in self._members(space.space_id, space.owner):
            raise ValueError("publisher is not a current organization member")
        rows = _rows(agents, owner, node_id)
        path = _path(owner, node_id)
        payload = json.dumps(rows, sort_keys=True, separators=(",", ":"))
        try:
            current, _ = self._fs.read_text(space.space_id, path)
        except OrgFsError as error:
            if error.code != "unknown-doc":
                raise
        else:
            if current == payload:
                return False
        for parent in ("directory", "directory/people", path.rsplit("/", 1)[0]):
            try:
                self._fs.mkdir(space.space_id, parent)
            except OrgFsError as error:
                if error.code == "already-exists":
                    continue
                if error.code == "invalid-argument":
                    self._fs.listdir(space.space_id, parent)
                    continue
                raise
        self._fs.write_text(space.space_id, path, payload)
        return True

    def list(self, org: str) -> dict[str, object]:
        space = self._space(org)
        members = self._members(space.space_id, space.owner)
        by_id: dict[str, dict[str, str]] = {}
        partial = False
        for owner in sorted(members):
            parent = _person_dir(owner)
            try:
                devices = self._fs.listdir(space.space_id, f"directory/devices/{owner}")
            except OrgFsError as error:
                if error.code != "unknown-doc":
                    raise
                devices = ()
            try:
                files = self._fs.listdir(space.space_id, parent)
            except OrgFsError as error:
                if error.code == "unknown-doc":
                    partial = True
                    continue
                raise
            inventories = [
                node for node in files
                if node.name.startswith("agents-") and node.name.endswith(".json")
            ]
            expected_nodes = {
                node.name.removesuffix(".json") for node in devices
                if node.kind == "doc" and node.name.endswith(".json")
            }
            published_nodes = {
                node.name[len("agents-"):-len(".json")] for node in inventories
            }
            if expected_nodes - published_nodes:
                partial = True
            if not inventories:
                partial = True
            for node in inventories:
                path = f"{parent}/{node.name}"
                node_id = node.name[len("agents-"):-len(".json")]
                try:
                    if node.kind != "doc" or node.doc_id != protected_directory_doc_id(
                        space.space_id, path, space_owner=space.owner
                    ):
                        raise ValueError("inventory document identity is invalid")
                    authors = {
                        _bare(author)
                        for author in self._fs.document_authors(
                            space.space_id, f"id:{node.node_id}"
                        )
                    }
                    if authors != {owner}:
                        raise ValueError("inventory document author is invalid")
                    content, _ = self._fs.read_text(space.space_id, f"id:{node.node_id}")
                    rows = _rows(json.loads(content), owner, node_id)
                except (OrgFsError, TypeError, ValueError, KeyError):
                    partial = True
                    continue
                for row in rows:
                    existing = by_id.setdefault(row["agentId"], row)
                    if existing != row:
                        partial = True
        return {
            "org": org,
            "status": "partial" if partial else "ok",
            "agents": [by_id[key] for key in sorted(by_id)],
        }

    def _space(self, org: str) -> Any:
        matches = [space for space in self._fs.spaces() if space.name == org]
        if len(matches) != 1:
            raise OrgFsError("unknown-space" if not matches else "ambiguous-space", {"org": org})
        return matches[0]

    def _members(self, space_id: str, space_owner: str) -> set[str]:
        return {_bare(space_owner), *(_bare(member.user) for member in self._fs.members(space_id))}


def publish_for_app(app: Any, orgs: list[str]) -> dict[str, object]:
    fs = app._require_orgfs_runtime().facade
    owner = _bare(str(app.owner))
    node_id = str(app.node_id)
    rows = [
        {
            "agentId": agent.agent_id,
            "agent": agent.uri,
            "name": agent.actor,
            "owner": canonical_user_uri(owner),
            "deviceId": node_id,
        }
        for agent in app.agents.list()
        if _bare(str(agent.owner)) == owner and agent.machine == node_id
    ]
    directory = MemberAgentDirectory(fs)
    home = getattr(app, "hyprial_home", None)
    exclusions = OrgAgentExclusionStore(Path(home)) if home else None
    published: list[str] = []
    errors: list[dict[str, str]] = []
    for org in orgs:
        try:
            org_rows = rows
            if exclusions is not None:
                space = directory._space(org)
                excluded = exclusions.excluded_agent_ids(space.space_id)
                if excluded:
                    org_rows = [row for row in rows if row["agentId"] not in excluded]
            if directory.publish(org, owner, node_id, org_rows):
                published.append(org)
        except (OrgFsError, ValueError) as error:
            errors.append({"org": org, "code": getattr(error, "code", "INVALID_INVENTORY")})
    return {"published": published, "errors": errors}


def chat_directory_for_app(app: Any) -> dict[str, object]:
    owner_value = getattr(app, "owner", None)
    if not isinstance(owner_value, str) or not owner_value:
        return {"targets": [], "orgDirectoryStatus": {}}
    try:
        runtime = app._require_orgfs_runtime()
        fs = runtime.facade
        active = for_app(app, repair_current=False).active
    except (DaemonRequestError, OrgFsError, RuntimeError):
        return {"targets": [], "orgDirectoryStatus": {}}
    directory = MemberAgentDirectory(fs)
    owner = _bare(owner_value)
    by_uri: dict[str, dict[str, object]] = {}
    status: dict[str, str] = {}
    for org in sorted(active):
        try:
            space = directory._space(org)
            members = directory._members(space.space_id, space.owner)
            if owner not in members:
                continue
            result = directory.list(org)
            live_devices = {
                device.device_id
                for device in OrgFsDirectoryStore(
                    fs, invalid_devices=_invalid_device_diagnostics(app)
                ).list_devices(org)
            }
        except (OrgFsError, RuntimeError, ValueError):
            status[org] = "unavailable"
            continue
        state = str(result["status"])
        if state == "ok" and len(members) > 1 and callable(getattr(runtime, "status", None)):
            try:
                online = runtime.status(space.space_id).holders_online
                if not any(holder != runtime.node_id for holder in online):
                    state = "partial"
            except (OrgFsError, RuntimeError):
                state = "partial"
        status[org] = state
        for agent in result["agents"]:
            if agent["deviceId"] not in live_devices:
                status[org] = "partial"
                continue
            uri = agent["agent"]
            row = by_uri.setdefault(uri, {
                "targetKind": "agent",
                "targetUri": uri,
                "actor": uri,
                "status": "unknown",
                "deliverable": True,
                "agentId": agent["agentId"],
                "displayName": agent["name"],
                "orgs": [],
            })
            row["orgs"].append(org)
    return {"targets": [by_uri[uri] for uri in sorted(by_uri)], "orgDirectoryStatus": status}


def chat_targets_for_app(app: Any) -> list[dict[str, object]]:
    return chat_directory_for_app(app)["targets"]
