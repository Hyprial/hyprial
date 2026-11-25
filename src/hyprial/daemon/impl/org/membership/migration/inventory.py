# Decision record
# - 2026-11-03 (#1886 alignment, HQ ruling): membership rows key on the
#   immutable agent id (``a.<id>``), never on the short name.  A registry record's stored ``uri``
#   is its official address; after the UUID-addressing flip its last segment
#   is the ``a.`` agent id, not the actor name.  The inventory therefore
#   accepts a record whose URI is canonical for its owner and machine and whose
#   segment is either the actor name or an ``a.`` id; the short name is kept
#   only to map accepted-context residents, which still list names.
# - 2026-11-18 (UA1b): the owner's user agent is left out of the inventory.
#   Every machine holds an instance with the same ``a.`` id, so one membership
#   row (one ``agent`` URI) could not hold them and the second machine would
#   meet ``membership-conflict``; leaving it out also keeps the frozen
#   inventory digest of an already-migrated node unchanged.
# - 2026-11-24 (S3a): user rows carry only the owner.  The receivers routing
#   projection (preferred machine, squire, userProxy) left with
#   receivers.json.  A finished journal is compared without the retired key
#   and stays done (apply.py); a node whose membership step is unfinished
#   sees a new inventory digest (source-drift) and follows the manual step in
#   docs/guides/migration-remove-squire.md.
# - 2026-11-24 (S3c): the users profile is gone, so user rows hold the node
#   owner and the owners of registry agents only, and the
#   profile-agent-unresolved problem has no source any more.
"""Read-only adapters for existing registry and accepted-context authority.

Only accepted bytes map residents. The containing space's explicit org metadata
names the target; a global file or unresolved legacy space is never guessed.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from hyprial.daemon.impl.org.document import parse_document
from hyprial.daemon.impl.org.store import OrgContextStore
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.user_agent.naming import is_user_agent
from hyprial.identity import OrgMembership as M
from hyprial.kernel import (
    canonical_agent_uri,
    canonical_user_uri,
    is_agent_id_segment,
    parse_agent_uri,
)


def canonical_owner(owner: str) -> str:
    bare = owner.removeprefix("user:")
    if not bare or canonical_user_uri(bare) != "user:" + bare or bare.strip() != bare:
        raise ValueError("noncanonical owner")
    return bare


def agent_address_matches(uri: str, owner: str, machine: str, actor: str) -> bool:
    """Whether a record URI is the official address of ``actor`` on ``machine``."""

    parsed = parse_agent_uri(uri)
    if parsed is None or uri != canonical_agent_uri(*parsed):
        return False
    uri_owner, uri_machine, segment = parsed
    if (uri_owner, uri_machine) != (owner, machine):
        return False
    return segment == actor or is_agent_id_segment(segment)


def snapshot(app, fs) -> dict:
    agents = []
    owners = {canonical_owner(str(app.owner))}
    problems = []
    for agent in app._agent_registry.list():
        if is_user_agent(agent):
            continue
        owner = canonical_owner(agent.owner)
        if not agent_address_matches(agent.uri, owner, agent.machine, agent.actor):
            raise ValueError("noncanonical agent")
        if not is_agent_id_segment(agent.agent_id):
            raise ValueError("noncanonical agent id")
        owners.add(owner)
        agents.append({"agent": agent.uri, "owner": owner, "name": agent.actor,
                       "node": agent.machine, "agentId": agent.agent_id})
    contexts, context_problems = accepted_contexts(Path(app.hyprial_home), fs)
    return {"agents": sorted(agents, key=lambda row: row["agent"]),
            "users": [{"owner": owner} for owner in sorted(owners)],
            "contexts": contexts, "problems": problems + context_problems}


def accepted_contexts(home: Path, fs) -> tuple[list[dict], list[dict]]:
    store = OrgContextStore(home)
    accepted = store.load_accepted()
    if accepted is None:
        return [], []
    payload = store.accepted_path.read_bytes()
    accepted = parse_document(payload.decode("utf-8"))
    issue = store.accepted_slot_issue(accepted, payload)
    if issue is not None:
        return [], [{"entity": "context:local", "reason": "accepted-context-unverified"}]
    sha = hashlib.sha256(payload).hexdigest()
    contexts = []
    for space in sorted(fs.spaces(), key=lambda item: item.space_id):
        paths = ["org-context.md"]
        try:
            paths.extend("candidates/" + node.name for node in fs.listdir(space.space_id, "candidates")
                         if node.kind == "doc" and node.name.endswith(".md"))
        except OrgFsError as error:
            if error.code != "unknown-doc":
                raise
        for path in paths:
            try:
                text, _ = fs.read_text(space.space_id, path)
            except OrgFsError as error:
                if error.code != "unknown-doc":
                    raise
                continue
            if hashlib.sha256(text.encode()).hexdigest() != sha:
                continue
            document = parse_document(text)
            org = fs.space_meta(space.space_id).get("org")
            try:
                M.org_kind(org)
            except ValueError:
                org = None
            contexts.append({"source": f"{space.space_id}/{path}", "org": org,
                             "digest": sha, "residents": document.data["residents"]})
    if not contexts:
        contexts.append({"source": "context:local", "org": None, "digest": sha,
                         "residents": accepted.data["residents"]})
    return contexts, []


def resident_targets(source: dict) -> tuple[dict[str, list[str]], list[dict]]:
    targets = {}
    problems = list(source.get("problems", []))
    for context in source["contexts"]:
        org = context.get("org")
        if not org:
            problems.append({"entity": context["source"], "reason": "context-org-unresolved"})
            continue
        for resident in context["residents"]:
            matches = [agent for agent in source["agents"]
                       if (agent["name"], agent["node"]) == (resident["name"], resident["node"])]
            if len(matches) != 1:
                problems.append({"entity": context["source"], "reason": "resident-ambiguous" if matches else "resident-unresolved"})
                continue
            targets.setdefault(matches[0]["agent"], []).append(org)
    return targets, problems
