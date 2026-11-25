# Decision record
# - 2026-11-03 (merge of release/v0.5.5.4, fa42667d4): ``org join`` bounds the
#   whole bootstrap with one shared ``JoinBudget``.  Invite acceptance runs the
#   same effects through this resumable runner, so each attempt holds its own
#   budget over the same legs (``member-announce``, each step kind and the
#   ``join-orgfs`` waits) and clears it on exit; a resumed attempt starts a
#   fresh budget because completed checkpoints are not re-executed.
"""Resume the existing org bootstrap with read-back checks at durable boundaries.

A space listing is discovery only. Directory and ACL membership, the ACL
policy and the directory's ACL binding are read independently. Checkpoints
skip completed bootstrap effects, never substitute for these live proofs.
"""

from __future__ import annotations

import json

from hyprial.daemon.impl.org.network.binding import INVITE_SPACE_MODES, resolve_org_acl_space
from hyprial.daemon.impl.org.network.policy import ACL_POLICY_DOC
from hyprial.daemon.impl.org.network.tailnet.budget import JoinBudget
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.identity import org_space_name, parse_bootstrap_yaml, role_grants_from_document


def space_committed(
    service, space_id: str, *, required_mode: str, name: str | None = None,
) -> bool:
    fs = service._fs
    if not hasattr(fs, "spaces"):
        return False
    matches = [space for space in fs.spaces() if space.space_id == space_id]
    if len(matches) != 1 or (name is not None and matches[0].name != name):
        return False
    owner = service._owner.removeprefix("user:")
    return matches[0].owner.removeprefix("user:") == owner or any(
        member.user.removeprefix("user:") == owner
        and (member.mode == required_mode or (required_mode == "ro" and member.mode == "rw"))
        for member in fs.members(space_id)
    )


def missing_membership_step(service, link) -> str | None:
    try:
        if not space_committed(service, link.space_id, name=org_space_name(link.org),
                               required_mode=INVITE_SPACE_MODES["directory"]):
            return "directory-committed"
        if not space_committed(service, link.acl_space_id,
                               required_mode=INVITE_SPACE_MODES["acl"]):
            return "acl-applied"
        acl, reason = resolve_org_acl_space(service._fs, link.org)
        if reason or acl is None or acl.space_id != link.acl_space_id:
            return "space-bound"
        text, _version = service._fs.read_text(link.acl_space_id, ACL_POLICY_DOC)
        role_grants_from_document(json.loads(text))
    except (OrgFsError, ValueError, TypeError):
        return "acl-applied"
    return None


def device_committed(service, link) -> bool:
    return any(
        device.owner.removeprefix("user:") == service._owner.removeprefix("user:")
        and device.device_id == service._node_id
        for device in service._store.list_devices(link.org)
    )


def run_bootstrap(service, parsed, journal):
    """Use the normal step executor under one shared join budget."""

    service._join_budget = JoinBudget()
    try:
        return _run_bootstrap(service, parsed, journal, service._join_budget)
    finally:
        service._join_budget = None


def _run_bootstrap(service, parsed, journal, budget):
    """Split join-org only at its two effects."""
    from hyprial.daemon.impl.org.network.create.sub_org import link_tailnet_id

    completed = journal.record["completed"]
    for kind, action in (
        ("tailnet-redeemed", lambda: service._redeem_tailnet(parsed)),
        ("tailnet-announced", lambda: service._announce_tailnet(link_tailnet_id(parsed))),
    ):
        if kind not in completed:
            journal.begin(kind)
            if kind == "tailnet-announced":
                budget.remaining("member-announce")
            action()
            journal.complete_step(kind)
    executed = journal.record["steps"]
    for step in parse_bootstrap_yaml(parsed.workflow_yaml):
        if step.kind in completed:
            continue
        journal.begin(step.kind)
        budget.remaining(step.kind)
        if step.kind == "join-org":
            for kind, space, mode in (
                ("directory-committed", parsed.space_id, INVITE_SPACE_MODES["directory"]),
                ("acl-applied", parsed.acl_space_id, INVITE_SPACE_MODES["acl"]),
            ):
                journal.begin(kind)
                if not space_committed(service, space, required_mode=mode):
                    service._join_with_retry(space)
                if space_committed(service, space, required_mode=mode):
                    journal.complete_step(kind)
            journal.begin("space-bound")
            missing = missing_membership_step(service, parsed)
            if missing:
                raise OrgFsError("no-holder-online", {"missingStep": missing})
            journal.complete_step("space-bound")
            detail = "joined the org directory and ACL spaces"
        else:
            detail = service._execute_step(parsed, step.kind)
            if step.kind == "register-device" and (
                detail.startswith("skipped") or not device_committed(service, parsed)
            ):
                raise OrgFsError("no-holder-online", {"missingStep": step.kind})
        budget.remaining(step.kind)
        executed.append({"kind": step.kind, "ok": True, "detail": detail})
        journal.complete_step(step.kind)
    missing = missing_membership_step(service, parsed)
    if missing:
        journal.begin(missing)
        raise OrgFsError("no-holder-online", {"missingStep": missing})
    return {"org": parsed.org, "spaceId": parsed.space_id, "steps": list(executed)}
