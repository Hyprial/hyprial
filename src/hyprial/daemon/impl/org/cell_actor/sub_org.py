# Decision notes (OC6 sub-org cell_actor helpers):
# - Owner-side `org` cell mirrors `org.parent = {cellId, directorySpace, owner}`
#   and inherits `org.tailnet` from the root ancestor org cell or OrgFS space.
# - `upsert_owner_org_member` fails closed unless the candidate UID/username is
#   currently confirmed in every ancestor org along the parent chain.
# - Removing or leaving a parent org NEVER auto-removes members from child cells
#   (O16); instead `compute_sub_org_roster_status` flags `out_of_parent_members`
#   and `parent_missing` in roster and snapshot projections.
"""Sub-org cell binding, fail-closed parent verification, and convergence status helpers."""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.cells.ports import CellRuntimePort
from hyprial.daemon.impl.forwarding.membership.hardening.left_orgs import read_left_orgs
from hyprial.daemon.impl.forwarding.policy import ForwardingSidecarError
from hyprial.daemon.impl.org.network.state import org_create_intent
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.identity import ORG_META_DOC, org_from_space_name, org_space_name
from hyprial.kernel import canonical_user_uri, is_user_id_segment, keyed_cell_id


def _orgfs_facade(app: Any) -> Any | None:
    getter = getattr(app, "_require_orgfs_runtime", None)
    if not callable(getter):
        return None
    try:
        runtime = getter()
    except Exception:  # noqa: BLE001 - stopped/partial apps may lack orgfs
        return None
    return getattr(runtime, "facade", None)


def _read_meta(fs: Any, space_id: str) -> dict[str, Any]:
    if fs is None:
        return {}
    try:
        raw = fs.read_text(space_id, ORG_META_DOC)
        text = raw[0] if isinstance(raw, tuple) else raw
    except (OrgFsError, ValueError):
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _find_space_by_org(fs: Any, org: str) -> Any | None:
    if fs is None:
        return None
    try:
        target = org_space_name(org)
        spaces = fs.spaces()
    except (OrgFsError, ValueError):
        return None
    return next((space for space in spaces if space.name == target), None)


def _find_org_by_space_id(fs: Any, space_id: str) -> tuple[str | None, Any | None]:
    if fs is None:
        return None, None
    try:
        spaces = fs.spaces()
    except (OrgFsError, ValueError):
        return None, None
    for space in spaces:
        if space.space_id == space_id:
            return org_from_space_name(space.name), space
    return None, None


def _left_orgs(app: Any) -> set[str]:
    home = getattr(app, "hyprial_home", None)
    if not home:
        return set()
    try:
        return set(read_left_orgs(Path(home)))
    except OSError:
        return set()


def _is_meta_deleted(meta: Mapping[str, Any]) -> bool:
    return meta.get("state") == "deleted"


def _local_owner_ref(app: Any, runtime: CellRuntimePort | None) -> str | None:
    if runtime is not None:
        return runtime.identity.authority.removeprefix("user:")
    user_id = getattr(app, "user_id", None)
    if isinstance(user_id, str) and user_id.strip():
        candidate = f"u.{user_id.strip()}"
        if is_user_id_segment(candidate):
            return candidate
    return None


def _lookup_parent_cell(
    runtime: CellRuntimePort | None, parent_space_id: str
) -> Any | None:
    if runtime is None:
        return None
    cell_id = keyed_cell_id("org", runtime.identity.authority, parent_space_id)
    cell = runtime.lookup(cell_id)
    if cell is None or cell.retired:
        return None
    return cell


def read_sub_org_cell_binding(
    app: Any,
    *,
    runtime: CellRuntimePort | None,
    org: str,
    space_id: str | None,
) -> tuple[str | None, str | None]:
    """Resolve ``(parent_org, tailnet_id)`` from OrgFS metadata, intent, or owner cell."""

    fs = _orgfs_facade(app)
    parent_org: str | None = None
    tailnet_id: str | None = None

    if space_id is not None:
        meta = _read_meta(fs, space_id)
        if isinstance(meta.get("parent"), str) and meta["parent"]:
            parent_org = str(meta["parent"])
        if isinstance(meta.get("tailnetId"), str) and meta["tailnetId"]:
            tailnet_id = str(meta["tailnetId"])

    home = getattr(app, "hyprial_home", None)
    if home:
        intent = org_create_intent(Path(home), org)
        if intent is not None:
            if parent_org is None and isinstance(intent.get("parent"), str) and intent["parent"]:
                parent_org = str(intent["parent"])
            if tailnet_id is None and isinstance(intent.get("tailnetId"), str) and intent["tailnetId"]:
                tailnet_id = str(intent["tailnetId"])

    if runtime is not None and space_id is not None:
        cell = _lookup_parent_cell(runtime, space_id)
        if cell is not None:
            cell_tailnet = cell.param("org.tailnet")
            if tailnet_id is None and isinstance(cell_tailnet, str) and cell_tailnet:
                tailnet_id = cell_tailnet
            cell_parent = cell.param("org.parent")
            if parent_org is None and isinstance(cell_parent, Mapping):
                p_space = cell_parent.get("directorySpace")
                if isinstance(p_space, str) and p_space:
                    resolved, _ = _find_org_by_space_id(fs, p_space)
                    if resolved is not None:
                        parent_org = resolved

    if tailnet_id is None and space_id is not None and parent_org is None:
        tailnet_id = space_id

    return parent_org, tailnet_id


def resolve_org_parent_param(
    app: Any,
    *,
    runtime: CellRuntimePort,
    org: str,
    space_id: str,
    parent_org: str,
    tailnet_hint: str | None = None,
) -> tuple[dict[str, str], str]:
    """Build validated ``org.parent`` param dict and inherited ``org.tailnet`` id."""

    from hyprial.daemon.impl.org.cell_actor.sync import org_ownership
    from hyprial.daemon.impl.org.network.service import OrgNetworkError

    fs = _orgfs_facade(app)
    left = _left_orgs(app)
    raw_app_owner = str(getattr(app, "owner", "") or "").removeprefix("user:").strip()
    local_owner_uri = canonical_user_uri(raw_app_owner) if raw_app_owner else ""
    seen = {org_space_name(org)}
    curr = org_space_name(parent_org)
    immediate_space_id: str | None = None
    immediate_cell_id: str | None = None
    immediate_owner_ref: str | None = None
    root_tailnet: str | None = None
    expected_next_space: str | None = None
    intermediate_tailnets: list[tuple[str, str]] = []

    while True:
        if curr in seen:
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"sub-org parent cycle detected at {curr!r}",
            )
        seen.add(curr)
        if curr in left:
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"parent org {curr!r} was left on this machine",
            )
        curr_space = _find_space_by_org(fs, curr)
        if curr_space is None:
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"parent org {curr!r} directory space is unavailable",
            )
        if expected_next_space is not None and curr_space.space_id != expected_next_space:
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"parent org {curr!r} space {curr_space.space_id!r} mismatches expected parent space {expected_next_space!r}",
            )
        curr_meta = _read_meta(fs, curr_space.space_id)
        if _is_meta_deleted(curr_meta):
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"parent org {curr!r} has been deleted",
            )
        if curr_meta.get("state") in {"creating", "partial"}:
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"parent org {curr!r} is {curr_meta.get('state')}",
            )
        raw_space_owner = getattr(curr_space, "owner", None)
        space_owner_uri = (
            canonical_user_uri(raw_space_owner.removeprefix("user:").strip())
            if isinstance(raw_space_owner, str) and raw_space_owner.strip()
            else ""
        )
        ownership = org_ownership(app, curr)
        if (
            not local_owner_uri
            or space_owner_uri != local_owner_uri
            or not ownership.writable
            or ownership.space_id != curr_space.space_id
        ):
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"parent org {curr!r} is not owned and writable by the local user",
            )
        curr_cell = _lookup_parent_cell(runtime, curr_space.space_id)
        if immediate_space_id is None:
            immediate_space_id = curr_space.space_id
            if curr_cell is not None:
                immediate_cell_id = curr_cell.cell_id
                immediate_owner_ref = curr_cell.authority.removeprefix("user:")
            else:
                local_ref = _local_owner_ref(app, runtime)
                if local_ref is None:
                    raise OrgNetworkError(
                        "ORG_CELL_UNAVAILABLE",
                        f"parent org {curr!r} owner uid cannot be resolved",
                    )
                immediate_owner_ref = local_ref
                immediate_cell_id = keyed_cell_id(
                    "org", canonical_user_uri(local_ref), curr_space.space_id
                )
        meta_parent = curr_meta.get("parent")
        meta_tailnet = curr_meta.get("tailnetId")
        meta_parent_space = curr_meta.get("parentSpaceId")
        if meta_parent is not None and (not isinstance(meta_parent, str) or not meta_parent):
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"parent org {curr!r} has invalid parent metadata",
            )
        cell_parent = curr_cell.param("org.parent") if curr_cell is not None else None
        if curr_cell is not None:
            if curr_cell.param("org.directorySpace") != curr_space.space_id:
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"parent org {curr!r} cell directorySpace mismatches space {curr_space.space_id!r}",
                )
            if (meta_parent is None) != (cell_parent is None):
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"parent org {curr!r} metadata and cell parent binding disagree",
                )
        if meta_parent is None:
            if meta_parent_space is not None:
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"root org {curr!r} metadata cannot declare parentSpaceId without parent",
                )
            if meta_tailnet is not None and (
                not isinstance(meta_tailnet, str) or meta_tailnet != curr_space.space_id
            ):
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"root org {curr!r} metadata tailnetId {meta_tailnet!r} must equal directorySpace {curr_space.space_id!r}",
                )
            if curr_cell is not None:
                cell_tailnet = curr_cell.param("org.tailnet")
                if cell_tailnet != curr_space.space_id:
                    raise OrgNetworkError(
                        "ORG_ACTION_NOT_AUTHORIZED",
                        f"root org {curr!r} cell org.tailnet {cell_tailnet!r} must equal directorySpace {curr_space.space_id!r}",
                    )
            root_tailnet = curr_space.space_id
            break
        if meta_tailnet is not None:
            if not isinstance(meta_tailnet, str) or not meta_tailnet or meta_tailnet == curr_space.space_id:
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"parent sub-org {curr!r} metadata tailnetId {meta_tailnet!r} is invalid",
                )
            intermediate_tailnets.append((curr, meta_tailnet))
        if meta_parent_space is not None:
            if not isinstance(meta_parent_space, str) or not meta_parent_space:
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"parent sub-org {curr!r} has invalid parentSpaceId",
                )
            expected_next_space = meta_parent_space
        else:
            expected_next_space = None
        if curr_cell is not None:
            if not isinstance(cell_parent, Mapping):
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"parent sub-org {curr!r} cell is missing org.parent binding",
                )
            cell_p_space = cell_parent.get("directorySpace")
            if not isinstance(cell_p_space, str) or not cell_p_space:
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"parent sub-org {curr!r} cell org.parent.directorySpace is invalid",
                )
            if meta_parent_space is not None and cell_p_space != meta_parent_space:
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"parent sub-org {curr!r} metadata parentSpaceId mismatches cell org.parent.directorySpace",
                )
            resolved_parent_org, _ = _find_org_by_space_id(fs, cell_p_space)
            if resolved_parent_org is not None and org_space_name(resolved_parent_org) != org_space_name(meta_parent):
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"parent sub-org {curr!r} metadata parent disagrees with cell org.parent",
                )
            expected_next_space = cell_p_space
            cell_tailnet = curr_cell.param("org.tailnet")
            if not isinstance(cell_tailnet, str) or not cell_tailnet or cell_tailnet == curr_space.space_id:
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"parent sub-org {curr!r} cell org.tailnet is invalid",
                )
            if meta_tailnet is not None and cell_tailnet != meta_tailnet:
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"parent sub-org {curr!r} metadata tailnetId mismatches cell org.tailnet",
                )
            intermediate_tailnets.append((curr, cell_tailnet))
        curr = org_space_name(meta_parent)

    assert immediate_space_id is not None
    assert immediate_cell_id is not None
    assert immediate_owner_ref is not None
    assert root_tailnet is not None
    home = getattr(app, "hyprial_home", None)
    if home:
        child_intent = org_create_intent(Path(home), org)
        if child_intent is not None and (
            "parent" in child_intent or "parentSpaceId" in child_intent
        ):
            intent_p_space = child_intent.get("parentSpaceId")
            if not isinstance(intent_p_space, str) or not intent_p_space or intent_p_space != immediate_space_id:
                raise OrgNetworkError(
                    "ORG_ACTION_NOT_AUTHORIZED",
                    f"sub-org {org!r} intent parentSpaceId {intent_p_space!r} mismatches parent {parent_org!r} space {immediate_space_id!r}",
                )
    child_meta = _read_meta(fs, space_id)
    if "parentSpaceId" in child_meta:
        meta_p_space = child_meta.get("parentSpaceId")
        if not isinstance(meta_p_space, str) or not meta_p_space or meta_p_space != immediate_space_id:
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"sub-org {org!r} metadata parentSpaceId {meta_p_space!r} mismatches parent {parent_org!r} space {immediate_space_id!r}",
            )
    for anc_org, anc_tailnet in intermediate_tailnets:
        if anc_tailnet != root_tailnet:
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"parent sub-org {anc_org!r} tailnet {anc_tailnet!r} mismatches root tailnet {root_tailnet!r}",
            )
    if tailnet_hint is not None and tailnet_hint != root_tailnet:
        raise OrgNetworkError(
            "ORG_ACTION_NOT_AUTHORIZED",
            f"sub-org {org!r} tailnet hint {tailnet_hint!r} mismatches root tailnet {root_tailnet!r}",
        )
    effective_tailnet = root_tailnet
    if effective_tailnet == space_id:
        raise OrgNetworkError(
            "ORG_ACTION_NOT_AUTHORIZED",
            f"sub-org {org!r} cannot use its own directorySpace as org.tailnet",
        )
    if not getattr(app, "network_isolated", False):
        supervisor = getattr(app, "_forwarding_supervisor", None)
        if supervisor is None:
            starter = getattr(app, "_start_forwarding_supervisor", None)
            if callable(starter):
                starter()
                supervisor = getattr(app, "_forwarding_supervisor", None)
        membership_req = (
            getattr(supervisor, "membership_request", None)
            if supervisor is not None
            else None
        )
        if not callable(membership_req):
            raise OrgNetworkError(
                "MEMBERSHIP_NOT_INITIALIZED",
                f"tailnet sidecar unavailable for root org tailnet {effective_tailnet!r}",
            )
        try:
            status = membership_req("membership-status", {})
        except ForwardingSidecarError as error:
            raise OrgNetworkError(error.code, str(error), error.details) from error
        tailnets = status.get("tailnets", ()) if isinstance(status, Mapping) else ()
        if any(
            isinstance(item, Mapping) and item.get("tailnetId") == space_id
            for item in tailnets
        ):
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"org {org!r} space {space_id!r} already has its own initialized tailnet and cannot be bound as a sub-org",
            )
        matched = next(
            (
                item
                for item in tailnets
                if isinstance(item, Mapping) and item.get("tailnetId") == effective_tailnet
            ),
            None,
        )
        if matched is None:
            raise OrgNetworkError(
                "MEMBERSHIP_NOT_INITIALIZED",
                f"root org tailnet {effective_tailnet!r} is not initialized in sidecar",
            )
        local_node_id = getattr(app, "node_id", None)
        authority_device_id = matched.get("authorityDeviceId")
        if (
            not isinstance(local_node_id, str)
            or not local_node_id
            or matched.get("role") != "authority"
            or not isinstance(authority_device_id, str)
            or authority_device_id != local_node_id
            or matched.get("memberReady") is False
        ):
            raise OrgNetworkError(
                "AUTHORITY_REQUIRED",
                f"this device {local_node_id!r} is not the active authority for root org tailnet {effective_tailnet!r}",
            )
    return (
        {
            "cellId": immediate_cell_id,
            "directorySpace": immediate_space_id,
            "owner": immediate_owner_ref,
        },
        effective_tailnet,
    )


def _is_member_confirmed_in_space(
    app: Any,
    *,
    fs: Any,
    runtime: CellRuntimePort | None,
    space_id: str,
    uid_ref: str | None,
    username: str | None,
) -> bool:
    """Return whether user is confirmed in OrgFS AND in active parent org cell when hosted."""

    if fs is None:
        return False
    try:
        space = next((s for s in fs.spaces() if s.space_id == space_id), None)
    except (OrgFsError, ValueError):
        return False
    if space is None:
        return False
    if _is_meta_deleted(_read_meta(fs, space_id)):
        return False
    if username is not None:
        norm_name = username.removeprefix("user:").strip()
        target_uri = canonical_user_uri(norm_name)
        raw_owner = str(getattr(space, "owner", "") or "").removeprefix("user:").strip()
        owner_uri = canonical_user_uri(raw_owner) if raw_owner else ""
        try:
            in_orgfs = target_uri == owner_uri or any(
                canonical_user_uri(str(m.user).removeprefix("user:").strip()) == target_uri
                for m in fs.members(space_id)
            )
        except (OrgFsError, ValueError):
            in_orgfs = False
        if not in_orgfs:
            return False

    if runtime is not None:
        cell = _lookup_parent_cell(runtime, space_id)
        if cell is not None:
            top_refs = {member.ref for member in cell.members}
            param_members = cell.param("org.members")
            if not isinstance(param_members, Mapping):
                return False
            if uid_ref is not None:
                if uid_ref not in top_refs or uid_ref not in param_members:
                    return False
            elif username is not None:
                norm_name = username.removeprefix("user:").strip()
                matched = any(
                    uid in top_refs
                    and isinstance(entry, Mapping)
                    and str(entry.get("username") or "").removeprefix("user:").strip()
                    == norm_name
                    for uid, entry in param_members.items()
                )
                if not matched:
                    return False
    return True


def verify_parent_chain_confirmed_member(
    app: Any,
    *,
    runtime: CellRuntimePort | None,
    org: str,
    space_id: str,
    uid_ref: str | None,
    username: str,
) -> None:
    """Fail closed unless ``uid_ref``/``username`` is confirmed across every ancestor org."""

    from hyprial.daemon.impl.org.network.service import OrgNetworkError

    fs = _orgfs_facade(app)
    left = _left_orgs(app)
    parent_org, _tailnet = read_sub_org_cell_binding(
        app, runtime=runtime, org=org, space_id=space_id
    )
    if parent_org is None:
        return
    seen = {org_space_name(org)}
    curr = org_space_name(parent_org)
    while True:
        if curr in seen:
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"sub-org parent cycle detected at {curr!r}",
            )
        seen.add(curr)
        if curr in left:
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"parent org {curr!r} was left on this machine",
            )
        curr_space = _find_space_by_org(fs, curr)
        if curr_space is None or _is_meta_deleted(_read_meta(fs, curr_space.space_id)):
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"parent org {curr!r} is missing or deleted",
            )
        if not _is_member_confirmed_in_space(
            app,
            fs=fs,
            runtime=runtime,
            space_id=curr_space.space_id,
            uid_ref=uid_ref,
            username=username,
        ):
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"user {username!r} is not a confirmed member of parent org {curr!r}",
            )
        next_parent, _ = read_sub_org_cell_binding(
            app, runtime=runtime, org=curr, space_id=curr_space.space_id
        )
        if next_parent is None:
            return
        curr = org_space_name(next_parent)


def snapshot_sub_org_status(
    app: Any,
    *,
    org: str,
    space_id: str | None,
    writable: bool,
) -> tuple[str | None, str | None, bool, tuple[str, ...]]:
    """Compute lightweight sub-org snapshot convergence fields from cell/OrgFS."""

    if space_id is None:
        return None, None, False, ()
    from hyprial.daemon.impl.org.network.policy import roles_from_orgfs

    runtime: CellRuntimePort | None = getattr(app, "_cell_runtime", None)
    if runtime is None and writable:
        from hyprial.daemon.impl.composition.cells import cell_runtime

        runtime = cell_runtime(app)
    parent_org, tailnet_id = read_sub_org_cell_binding(
        app, runtime=runtime, org=org, space_id=space_id
    )
    if parent_org is None:
        return None, tailnet_id, False, ()

    member_usernames: dict[str, str] = {}
    if writable and runtime is not None:
        cell = _lookup_parent_cell(runtime, space_id)
        if cell is not None:
            raw_members = cell.param("org.members") or {}
            member_usernames = {
                m.ref: str((raw_members.get(m.ref) or {}).get("username") or m.ref)
                for m in cell.members
            }
    if not member_usernames:
        fs = _orgfs_facade(app)
        if fs is not None:
            try:
                member_usernames = {name: name for name in roles_from_orgfs(fs, org)}
            except (OrgFsError, ValueError):
                member_usernames = {}
    return compute_sub_org_roster_status(
        app,
        runtime=runtime,
        org=org,
        space_id=space_id,
        member_usernames=member_usernames,
    )


def compute_sub_org_roster_status(
    app: Any,
    *,
    runtime: CellRuntimePort | None,
    org: str,
    space_id: str | None,
    member_usernames: Mapping[str, str],
) -> tuple[str | None, str | None, bool, tuple[str, ...]]:
    """Compute parent link, inherited tailnet, parent_missing, and out_of_parent_members."""

    parent_org, tailnet_id = read_sub_org_cell_binding(
        app, runtime=runtime, org=org, space_id=space_id
    )
    if parent_org is None:
        return None, tailnet_id, False, ()

    fs = _orgfs_facade(app)
    left = _left_orgs(app)
    ancestors: list[Any] = []
    seen = {org_space_name(org)}
    curr = org_space_name(parent_org)
    parent_missing = False

    while True:
        if curr in seen or curr in left:
            parent_missing = True
            break
        seen.add(curr)
        curr_space = _find_space_by_org(fs, curr)
        if curr_space is None or _is_meta_deleted(_read_meta(fs, curr_space.space_id)):
            parent_missing = True
            break
        ancestors.append(curr_space)
        next_parent, _ = read_sub_org_cell_binding(
            app, runtime=runtime, org=curr, space_id=curr_space.space_id
        )
        if next_parent is None:
            break
        curr = org_space_name(next_parent)

    if parent_missing:
        return (
            parent_org,
            tailnet_id,
            True,
            tuple(sorted(member_usernames)),
        )

    out_of_parent: list[str] = []
    for key, uname in sorted(member_usernames.items()):
        uid_ref = key if is_user_id_segment(key) else None
        confirmed = all(
            _is_member_confirmed_in_space(
                app,
                fs=fs,
                runtime=runtime,
                space_id=anc_space.space_id,
                uid_ref=uid_ref,
                username=uname,
            )
            for anc_space in ancestors
        )
        if not confirmed:
            out_of_parent.append(key)
    return parent_org, tailnet_id, False, tuple(out_of_parent)
