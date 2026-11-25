"""Validation of ``org`` cells: membership, roles and ``org.*`` parameters.

Decision notes (cell model OC1b, contract OC0 §2.2, §2.3, §4.2):

- Validation lives in its own subpackage outside ``cells.model`` to avoid
  circular imports and is called only from ``cells.codec``
  (``from_document``, ``to_document`` and ``encode``), covering every read
  from OrgFS/cache and every write via ``CellCache.put``.
- Top-level ``hyprial-cell/v1`` shape is unchanged: ``kind == "org"``,
  ``authority`` is the owner's ``user:u.<sub>``, ``binding`` is ``keyed`` by
  the org's directory space id, ``scope`` is ``org`` naming the same org as
  ``org.name``, ``permissions`` is empty, and ``members`` are unique
  ``u.<sub>`` entries with role in ``{owner, admin, member}`` and exactly
  one ``owner`` matching ``authority``.
- ``params`` accepts only the closed set of ``org.*`` keys and rejects any
  non-``org.*`` key, network address, tailnet public key or invite token.
- ``org.members`` keys match ``members[].ref`` exactly, usernames satisfy
  ``is_person_name`` and are unique across uids, and each member has at
  least one ``d.<32hex>`` device entry with ``deviceUid`` unique across all
  members and ``keyId``/``publicKey`` either both non-empty strings or both
  ``None``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.daemon.impl.cells.model import Cell, CellError
from hyprial.daemon.impl.org.document import is_person_name
from hyprial.identity import org_from_space_name, org_space_name
from hyprial.kernel import is_cell_id_segment, is_device_id_segment, is_user_id_segment

ORG_MEMBER_ROLES: frozenset[str] = frozenset({"owner", "admin", "member"})
MAX_ORG_MEMBERS = 64
ORG_PARAM_KEYS: frozenset[str] = frozenset(
    {
        "org.name",
        "org.issuer",
        "org.directorySpace",
        "org.tailnet",
        "org.parent",
        "org.members",
    }
)
_PARENT_KEYS: frozenset[str] = frozenset({"cellId", "directorySpace", "owner"})
_MEMBER_ENTRY_KEYS: frozenset[str] = frozenset(
    {"username", "joinedAtMs", "invitedBy", "devices"}
)
_DEVICE_ENTRY_KEYS: frozenset[str] = frozenset(
    {"deviceUid", "keyId", "publicKey", "verifiedAtMs"}
)
_DEVICE_ENTRY_KEYS_WITH_MASTER: frozenset[str] = _DEVICE_ENTRY_KEYS | {"masterPublic", "serverPublic"}


def _invalid(message: str) -> CellError:
    return CellError("invalid-cell", message)


def _trimmed_str(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise _invalid(f"{label} must be a non-empty trimmed string")
    return value


def _non_neg_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid(f"{label} must be a non-negative integer")
    return value


def _exact_keys(mapping: object, expected: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(mapping, Mapping):
        raise _invalid(f"{label} must be an object")
    actual = set(mapping.keys())
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise _invalid(
            f"{label} keys mismatch (missing={missing}, unexpected={extra})"
        )
    return mapping


def _validate_org_name(value: object) -> str:
    name = _trimmed_str(value, "org.name")
    try:
        space_name = org_space_name(name)
    except ValueError as error:
        raise _invalid(f"invalid org.name {name!r}: {error}") from error
    if org_from_space_name(space_name) != name:
        raise _invalid(f"org.name {name!r} is not reversible via org_space_name")
    return name


def _validate_parent(value: object, *, self_cell_id: str, directory_space: str) -> Mapping[str, Any] | None:
    if value is None:
        return None
    parent = _exact_keys(value, _PARENT_KEYS, "org.parent")
    cell_id = parent["cellId"]
    if not isinstance(cell_id, str) or not is_cell_id_segment(cell_id):
        raise _invalid("org.parent.cellId must be a c. cell id")
    if cell_id == self_cell_id:
        raise _invalid("org.parent.cellId cannot reference itself")
    parent_space = _trimmed_str(parent["directorySpace"], "org.parent.directorySpace")
    if parent_space == directory_space:
        raise _invalid("org.parent.directorySpace must differ from org.directorySpace")
    owner = parent["owner"]
    if not isinstance(owner, str) or not is_user_id_segment(owner):
        raise _invalid("org.parent.owner must be a u.<sub> user id")
    return parent


def _validate_device(
    device: object,
    seen_devices: set[str],
    seen_public_keys: set[str],
    label: str,
) -> None:
    if not isinstance(device, Mapping):
        raise _invalid(f"{label} must be an object")
    keys = frozenset(device)
    if keys not in {_DEVICE_ENTRY_KEYS, _DEVICE_ENTRY_KEYS_WITH_MASTER}:
        raise _invalid(f"{label} keys are invalid")
    entry = device
    device_uid = entry["deviceUid"]
    if not isinstance(device_uid, str) or not is_device_id_segment(device_uid):
        raise _invalid(f"{label}.deviceUid must be a d.<32hex> device id")
    if device_uid in seen_devices:
        raise _invalid(f"duplicate deviceUid {device_uid!r} in {label}")
    seen_devices.add(device_uid)
    key_id = entry["keyId"]
    public_key = entry["publicKey"]
    if key_id is None and public_key is None:
        pass
    elif key_id is not None and public_key is not None:
        _trimmed_str(key_id, f"{label}.keyId")
        public_key = _trimmed_str(public_key, f"{label}.publicKey")
        if public_key in seen_public_keys:
            raise _invalid(f"duplicate publicKey {public_key!r} in org.members")
        seen_public_keys.add(public_key)
    else:
        raise _invalid(f"{label}.keyId and publicKey must both be strings or both be null")
    if keys == _DEVICE_ENTRY_KEYS_WITH_MASTER:
        master_public = _trimmed_str(entry["masterPublic"], f"{label}.masterPublic")
        server_public = _trimmed_str(entry["serverPublic"], f"{label}.serverPublic")
        if master_public == server_public:
            raise _invalid(f"{label}.masterPublic and serverPublic must differ")
        for value in (master_public, server_public):
            if value in seen_public_keys:
                raise _invalid(f"duplicate public key {value!r} in org.members")
            seen_public_keys.add(value)
    _non_neg_int(entry["verifiedAtMs"], f"{label}.verifiedAtMs")


def _validate_members_param(
    raw_members: object, member_refs: set[str]
) -> None:
    if not isinstance(raw_members, Mapping):
        raise _invalid("org.members must be an object")
    if set(raw_members.keys()) != member_refs:
        raise _invalid("org.members keys must equal members[].ref set")
    seen_usernames: set[str] = set()
    seen_devices: set[str] = set()
    seen_public_keys: set[str] = set()
    for uid, raw_entry in raw_members.items():
        entry = _exact_keys(raw_entry, _MEMBER_ENTRY_KEYS, f"org.members[{uid!r}]")
        username = _trimmed_str(entry["username"], f"org.members[{uid!r}].username")
        if not is_person_name(username):
            raise _invalid(f"org.members[{uid!r}].username {username!r} is invalid")
        if username in seen_usernames:
            raise _invalid(f"duplicate username {username!r} across org.members")
        seen_usernames.add(username)
        _non_neg_int(entry["joinedAtMs"], f"org.members[{uid!r}].joinedAtMs")
        invited_by = entry["invitedBy"]
        if not isinstance(invited_by, str) or not is_user_id_segment(invited_by):
            raise _invalid(f"org.members[{uid!r}].invitedBy must be a u.<sub> user id")
        devices = entry["devices"]
        if not isinstance(devices, (list, tuple)) or not devices:
            raise _invalid(f"org.members[{uid!r}].devices must be a non-empty list")
        for index, device in enumerate(devices):
            _validate_device(
                device, seen_devices, seen_public_keys,
                f"org.members[{uid!r}].devices[{index}]"
            )


def validate_org_cell(cell: Cell) -> None:
    """Validate an ``org`` cell against contract OC0 §2.2 and §2.3."""

    if cell.kind != "org":
        raise _invalid(f"expected an org cell, got {cell.kind!r}")
    if cell.binding.type != "keyed" or not cell.binding.value or cell.binding.value.strip() != cell.binding.value:
        raise _invalid("org cell binding must be keyed with a non-empty directorySpace")
    if cell.scope.kind != "org" or not cell.scope.org:
        raise _invalid("org cell scope must name a non-empty org")
    if cell.permissions.grants:
        raise _invalid("org cell permissions must be empty")

    authority_ref = cell.authority.removeprefix("user:")
    owners: list[str] = []
    member_refs: set[str] = set()
    for member in cell.members:
        if not is_user_id_segment(member.ref):
            raise _invalid(f"org member ref {member.ref!r} must be a u.<sub> user id")
        if member.role not in ORG_MEMBER_ROLES:
            raise _invalid(f"org member role {member.role!r} is not allowed")
        if member.role == "owner":
            owners.append(member.ref)
        if member.ref in member_refs:
            raise _invalid(f"duplicate org member ref {member.ref!r}")
        member_refs.add(member.ref)
    if len(member_refs) > MAX_ORG_MEMBERS:
        raise _invalid(
            f"org member count {len(member_refs)} exceeds limit {MAX_ORG_MEMBERS}"
        )
    if owners != [authority_ref]:
        raise _invalid("org cell must have exactly one owner matching authority")

    params = _exact_keys(cell.params, ORG_PARAM_KEYS, "org params")
    org_name = _validate_org_name(params["org.name"])
    if cell.scope.org != org_name:
        raise _invalid("org cell scope.org must match params['org.name']")
    _trimmed_str(params["org.issuer"], "org.issuer")
    directory_space = _trimmed_str(params["org.directorySpace"], "org.directorySpace")
    if directory_space != cell.binding.value:
        raise _invalid("org.directorySpace must equal binding.value")
    tailnet = _trimmed_str(params["org.tailnet"], "org.tailnet")
    parent = _validate_parent(
        params["org.parent"], self_cell_id=cell.cell_id, directory_space=directory_space
    )
    if parent is None and tailnet != directory_space:
        raise _invalid("top-level org.tailnet must equal org.directorySpace")
    if parent is not None and tailnet == directory_space:
        raise _invalid("sub-org org.tailnet must inherit parent tailnet")
    _validate_members_param(params["org.members"], member_refs)


__all__ = [
    "ORG_MEMBER_ROLES",
    "ORG_PARAM_KEYS",
    "MAX_ORG_MEMBERS",
    "validate_org_cell",
]
