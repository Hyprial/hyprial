"""Local leave bookkeeping: the orgs this node has left (§5, task D).

OrgFS spaces are shared state — a node that leaves cannot un-join the
space unilaterally, it can only stop counting it.  That local decision
lives in ``state/tailcat/left-orgs.json`` inside the daemon's home: a
JSON array of org names.  The service's org listing subtracts it, so a
left org disappears from ``orgs()``/``list()``/``network()`` on this node
while the space itself stays for the remaining members.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

from hyprial.identity import org_space_name
from hyprial.kernel import atomic_json_write

__all__ = [
    "LEFT_ORGS_RELPATH",
    "ORG_ACL_AGENT_URI_MARKER_RELPATH",
    "ORG_ACL_GRANTS_RELPATH",
    "ORG_CREATE_INTENTS_RELPATH",
    "add_left_org",
    "add_org_acl_grant",
    "begin_org_create",
    "clear_left_org",
    "finish_org_create",
    "inspect_org_create_intent",
    "org_create_intent",
    "left_orgs_path",
    "migrate_org_acl_grant_agents",
    "org_acl_grants_path",
    "read_left_orgs",
    "read_org_acl_grants",
    "remove_org_acl_grant",
    "update_org_create",
]

#: ``LEFT_ORGS_RELPATH`` / ``left_orgs_path`` / ``read_left_orgs`` live in a
#: leaf shared with the membership import (no cycle); re-exported here.
from hyprial.daemon.impl.forwarding.membership.hardening.left_orgs import (  # noqa: E402
    LEFT_ORGS_RELPATH as LEFT_ORGS_RELPATH,
    left_orgs_path as left_orgs_path,
    read_left_orgs as read_left_orgs,
)

#: Owner-node ACL entry authorizations (design-org-orgfs-usage §8.3):
#: ``{org: {agent URI: {"grantedAtMs": int, "untilMs": int | null}}}``.
#: Local by design — the entry gate runs only on the owner node, so the
#: grants never enter orgfs and never synchronize.  They are org state,
#: not tailcat state, so they live under ``state/org/``.
ORG_ACL_GRANTS_RELPATH = "state/org/org-acl-grants.json"
ORG_ACL_AGENT_URI_MARKER_RELPATH = "state/org/org-acl-grants.agent-uri-migrated.json"

#: Durable creator-owned recovery records for multi-step ``org.create``.
ORG_CREATE_INTENTS_RELPATH = "state/org/create-intents.json"

def add_left_org(home: Path, org: str) -> list[str]:
    """Record ``org`` as left (idempotent) and return the new list."""

    org_space_name(org)  # the same name rule as every org-taking entry point
    orgs = read_left_orgs(home)
    if org in orgs:
        return orgs
    orgs = sorted([*orgs, org])
    _write_left_orgs(home, orgs)
    return orgs


def clear_left_org(home: Path, org: str) -> list[str]:
    """Forget that ``org`` was left (an explicit rejoin); return the new list."""

    orgs = read_left_orgs(home)
    if org not in orgs:
        return orgs
    orgs = [name for name in orgs if name != org]
    _write_left_orgs(home, orgs)
    return orgs


def _write_left_orgs(home: Path, orgs: list[str]) -> None:
    path = left_orgs_path(home)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(orgs, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _org_create_intents_path(home: Path) -> Path:
    return Path(home) / ORG_CREATE_INTENTS_RELPATH


_INTENT_REQUIRED = frozenset(
    {"nodeId", "owner", "startedAt", "directorySpaceId", "aclSpaceId"}
)
_INTENT_OPTIONAL = frozenset({"parent", "parentSpaceId", "tailnetId"})


def _validate_org_create_intent_record(
    record: object,
) -> dict[str, str | None] | None:
    if not isinstance(record, dict):
        return None
    keys = set(record)
    if not _INTENT_REQUIRED.issubset(keys) or not keys.issubset(
        _INTENT_REQUIRED | _INTENT_OPTIONAL
    ):
        return None
    if not all(
        isinstance(record[key], str) and record[key]
        for key in ("nodeId", "owner", "startedAt")
    ):
        return None
    if any(
        record.get(key) is not None and not isinstance(record.get(key), str)
        for key in (
            "directorySpaceId",
            "aclSpaceId",
            "parent",
            "parentSpaceId",
            "tailnetId",
        )
    ):
        return None
    return dict(record)


def _read_org_create_intents(home: Path) -> dict[str, dict[str, str | None]]:
    try:
        value = json.loads(_org_create_intents_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    intents: dict[str, dict[str, str | None]] = {}
    for org, record in value.items():
        if not isinstance(org, str):
            continue
        validated = _validate_org_create_intent_record(record)
        if validated is not None:
            intents[org] = validated
    return intents


def inspect_org_create_intent(
    home: Path, org: str
) -> tuple[dict[str, str | None] | None, str | None]:
    """Read and validate one create intent, distinguishing absence from corruption."""

    path = _org_create_intents_path(home)
    if not path.exists():
        return None, None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "create-intents.json is unreadable or corrupt"
    if not isinstance(value, dict):
        return None, "create-intents.json must be a JSON object"
    if org not in value:
        return None, None
    validated = _validate_org_create_intent_record(value[org])
    if validated is None:
        return None, f"create intent for org {org!r} is corrupt"
    return validated, None


def _write_org_create_intents(
    home: Path, intents: dict[str, dict[str, str | None]]
) -> None:
    atomic_json_write(_org_create_intents_path(home), intents)


def org_create_intent(home: Path, org: str) -> dict[str, str | None] | None:
    record = _read_org_create_intents(home).get(org)
    return None if record is None else dict(record)


def begin_org_create(
    home: Path,
    *,
    org: str,
    node_id: str,
    owner: str,
    started_at: str,
    parent: str | None = None,
    parent_space_id: str | None = None,
    tailnet_id: str | None = None,
) -> dict[str, str | None]:
    org_space_name(org)
    intents = _read_org_create_intents(home)
    existing = intents.get(org)
    if existing is not None:
        return dict(existing)
    record: dict[str, str | None] = {
        "nodeId": node_id,
        "owner": owner,
        "startedAt": started_at,
        "directorySpaceId": None,
        "aclSpaceId": None,
    }
    if parent is not None:
        record["parent"] = parent
    if parent_space_id is not None:
        record["parentSpaceId"] = parent_space_id
    if tailnet_id is not None:
        record["tailnetId"] = tailnet_id
    intents[org] = record
    _write_org_create_intents(home, intents)
    return dict(record)


def update_org_create(
    home: Path,
    org: str,
    *,
    directory_space_id: str | None = None,
    acl_space_id: str | None = None,
    parent: str | None = None,
    parent_space_id: str | None = None,
    tailnet_id: str | None = None,
) -> dict[str, str | None]:
    intents = _read_org_create_intents(home)
    record = intents.get(org)
    if record is None:
        raise ValueError(f"org create intent is absent for {org}")
    if directory_space_id is not None:
        record["directorySpaceId"] = directory_space_id
    if acl_space_id is not None:
        record["aclSpaceId"] = acl_space_id
    if parent is not None:
        record["parent"] = parent
    if parent_space_id is not None:
        record["parentSpaceId"] = parent_space_id
    if tailnet_id is not None:
        record["tailnetId"] = tailnet_id
    _write_org_create_intents(home, intents)
    return dict(record)


def finish_org_create(home: Path, org: str) -> None:
    intents = _read_org_create_intents(home)
    if intents.pop(org, None) is not None:
        _write_org_create_intents(home, intents)


def org_acl_grants_path(home: Path) -> Path:
    return Path(home) / ORG_ACL_GRANTS_RELPATH


def read_org_acl_grants(
    home: Path, *, now_ms: int | None = None
) -> dict[str, dict[str, dict[str, int | None]]]:
    """Live ACL entry grants per org; expired entries never come back.

    # LAX(tailnet-cutover): a corrupt file is treated as empty rather than
    # failing loudly; the proper shape is a typed state record with a
    # schema version, like the other files under ``state/tailcat/``.
    """

    try:
        raw = org_acl_grants_path(home).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    grants: dict[str, dict[str, dict[str, int | None]]] = {}
    for org, agents in value.items():
        if not isinstance(org, str) or not isinstance(agents, dict):
            continue
        live: dict[str, dict[str, int | None]] = {}
        for agent, entry in agents.items():
            if not isinstance(agent, str) or not isinstance(entry, dict):
                continue
            granted_at = entry.get("grantedAtMs")
            until = entry.get("untilMs")
            if isinstance(granted_at, bool) or not isinstance(granted_at, int):
                continue
            if until is not None and (
                isinstance(until, bool) or not isinstance(until, int)
            ):
                continue
            if now_ms is not None and until is not None and until <= now_ms:
                continue
            live[agent] = {"grantedAtMs": granted_at, "untilMs": until}
        if live:
            grants[org] = live
    return grants


def add_org_acl_grant(
    home: Path,
    org: str,
    agent: str,
    *,
    granted_at_ms: int,
    until_ms: int | None = None,
) -> dict[str, int | None]:
    """Record (or re-stamp) one agent's ACL entry grant and return the entry."""

    org_space_name(org)  # the same name rule as every org-taking entry point
    grants = read_org_acl_grants(home, now_ms=granted_at_ms)
    entry: dict[str, int | None] = {
        "grantedAtMs": granted_at_ms,
        "untilMs": until_ms,
    }
    grants.setdefault(org, {})[agent] = entry
    _write_org_acl_grants(home, grants)
    return entry


def remove_org_acl_grant(home: Path, org: str, agent: str) -> bool:
    """Forget one agent's ACL entry grant; False when nothing was recorded."""

    grants = read_org_acl_grants(home)
    if agent not in grants.get(org, {}):
        return False
    del grants[org][agent]
    if not grants[org]:
        del grants[org]
    _write_org_acl_grants(home, grants)
    return True


def _write_org_acl_grants(
    home: Path, grants: dict[str, dict[str, dict[str, int | None]]]
) -> None:
    path = org_acl_grants_path(home)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(grants, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def migrate_org_acl_grant_agents(
    home: Path, mapping: Callable[[str], str | None], *, at_ms: int
) -> int | None:
    """Re-key ACL grants from name-form agent URIs to official URIs, once.

    UUID addressing P4 (spec §2.4): the entry gate looks the caller's
    canonical URI up in these keys, and after the id-form flip a local agent
    calls under its id URI.  ``mapping`` answers the official URI or None to
    keep a key.  When both spellings hold a grant the later grant wins.  The
    rewrite is one atomic file replace followed by the marker file
    (``ORG_ACL_AGENT_URI_MARKER_RELPATH``) that makes a second run a no-op;
    an absent or unreadable grants file is left untouched and unmarked.
    """

    marker = Path(home) / ORG_ACL_AGENT_URI_MARKER_RELPATH
    if marker.exists():
        return None
    try:
        value = json.loads(org_acl_grants_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    rewritten = 0
    migrated: dict[str, object] = {}
    for org, agents in value.items():
        if not isinstance(agents, dict):
            migrated[org] = agents
            continue
        keyed: dict[str, object] = {}
        for agent, entry in agents.items():
            official = mapping(agent) if isinstance(agent, str) else None
            key = agent if official is None else official
            rewritten += key != agent
            previous = keyed.get(key)
            if previous is None or _granted_at(entry) >= _granted_at(previous):
                keyed[key] = entry
        migrated[org] = keyed
    if rewritten:
        _write_org_acl_grants(home, migrated)
    atomic_json_write(marker, {"migratedAtMs": at_ms, "rewritten": rewritten})
    return rewritten


def _granted_at(entry: object) -> int:
    granted = entry.get("grantedAtMs") if isinstance(entry, dict) else None
    return granted if isinstance(granted, int) and not isinstance(granted, bool) else -1
