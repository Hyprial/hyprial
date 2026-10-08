"""Organization directory semantics: devices, org names, space naming.

Pure model plus naming rules — no storage, no daemon imports.  The storage
port lives in ``ports.py``; the daemon's OrgFS-backed implementation is
injected at the composition root (tailnet cutover §1: identity defines the
directory model, daemon provides the storage).
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from hyprial.kernel import (
    IDENTITY_BINDING_PUBLISH_SKEW_SECONDS,
    canonical_user_uri,
    parse_user_uri,
)
from hyprial.identity.impl.identity_slug import identity_slug

__all__ = [
    "DIRECTORY_DIR",
    "BINDING_ASSERTION_CLIENT_ID",
    "BINDING_ASSERTION_OWNER",
    "INVITES_DIR",
    "LEAVES_DIR",
    "PEOPLE_DIR",
    "ORG_META_DOC",
    "ORG_SPACE_PREFIX",
    "DirectoryDevice",
    "directory_device_path",
    "directory_binding_path",
    "binding_assertion_claims_unverified",
    "binding_numeric_date",
    "binding_assertion_publish_after",
    "directory_owner_principal",
    "is_org_acl_space",
    "is_org_directory_space",
    "org_from_space_name",
    "org_space_name",
    "MAX_PROTECTED_DOC_ID_LENGTH",
    "ProtectedDocIdTooLongError",
    "parse_protected_directory_node_id",
    "parse_protected_directory_doc_id",
    "protected_directory_node_id",
    "protected_directory_doc_id",
    "protected_directory_author_allowed",
]

#: An org name and its directory-space id share the ``group-`` prefix.
ORG_SPACE_PREFIX = "group-"
#: Public client id of the dedicated Casdoor binding-assertion application.
BINDING_ASSERTION_CLIENT_ID = "1be2763a29154030fbcb"
BINDING_ASSERTION_OWNER = "hyprial"
#: Reserved suffix for the daemon-owned policy space beside each directory.
_ORG_ACL_SUFFIX = "-acl"
#: One document per device owner and opaque device id:
#: ``directory/devices/<owner>/<deviceId>.json``.
DIRECTORY_DIR = "directory/devices"
#: Verified platform-account bindings: one fixed row per path-safe user key.
PEOPLE_DIR = "directory/people"
#: Pending invites: ``directory/invites/<token_id>.json``.
INVITES_DIR = "directory/invites"
#: Leave requests: ``directory/leaves/<user>.json`` (processed by owner/admin).
LEAVES_DIR = "directory/leaves"
#: The org lifecycle marker: ``directory/org.json`` holds ``{"state": …}``.
ORG_META_DOC = "directory/org.json"

#: Org names become space names and directory path segments, so they stay
#: lowercase alphanumeric with inner hyphens, 1–63 chars, alphanumeric head
#: (the same fence rule the rest of the home applies to path segments).
_ORG_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_PROTECTED_DOC_ID_FAMILY = "doc-orgdir-"
_PROTECTED_DOC_ID_PREFIX = "doc-orgdir-v3-"
_PROTECTED_NODE_ID_FAMILY = "node-orgdir-"
_PROTECTED_NODE_ID_PREFIX = "node-orgdir-v1-"
#: JavaScript-safe integer range used by JWT NumericDate claims.
_NUMERIC_DATE_LIMIT = 1 << 53
#: A protocol sanity bound, not a storage one: the local store and replica
#: name each document by a fixed-length hash of its id, so the id is never a
#: path component.  The id grows with the org name, owner and member names
#: and the device id; 1024 leaves room for long real names (253 characters
#: for an invite in ``group-internal`` already passed the old 240 file-name
#: bound) while still refusing an unbounded id on the wire.
MAX_PROTECTED_DOC_ID_LENGTH = 1024
_PROTECTED_DOC_SECTIONS = frozenset(
    {"devices", "people", "leaves", "invites", "org"}
)

#: The exact camelCase keys of the frozen wire shape (§4.1).
_RECORD_KEYS = frozenset(
    {
        "deviceId",
        "owner",
        "serverPublic",
        "clientPublic",
        "address",
        "keyGeneration",
        "updatedAt",
    }
)


def binding_assertion_claims_unverified(proof: str) -> dict[str, object]:
    """Decode only the JWT claims needed before signature verification.

    Publication uses this parser solely to delay disclosure until the signed
    ``exp`` is in the past, and login uses it to compare the confirmed account
    with the already verified login identity.  It grants no authority.
    """

    parts = proof.split(".") if isinstance(proof, str) else []
    if len(parts) != 3 or not parts[1] or len(parts[1]) > 65_536:
        raise ValueError("binding proof is not a compact JWT")
    try:
        payload = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        claims = json.loads(payload)
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("binding proof claims are invalid") from error
    if not isinstance(claims, dict):
        raise ValueError("binding proof claims are not an object")
    return claims


def binding_assertion_publish_after(proof: str) -> float:
    """Earliest wall-clock second at which the assertion may be published."""

    expires_at = binding_assertion_claims_unverified(proof).get("exp")
    return binding_numeric_date(expires_at, "expiry") + (
        IDENTITY_BINDING_PUBLISH_SKEW_SECONDS
    )


def binding_numeric_date(value: object, label: str) -> float:
    """Return one bounded JWT NumericDate or reject it without overflow."""

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 < value < _NUMERIC_DATE_LIMIT
    ):
        raise ValueError(f"binding proof {label} is missing or invalid")
    return float(value)


@dataclass(frozen=True, slots=True)
class DirectoryDevice:
    """One device's row in an organization's directory (an OrgFS document).

    Mirrors :class:`hyprial.identity.impl.device.models.DeviceRecord` plus
    the network half: ``address`` is the Tailcat address the owner's
    sidecar wrote, and ``updated_at`` is when the owning device last
    published this row (ISO8601 UTC).
    """

    device_id: str
    owner: str
    server_public: str
    client_public: str
    # LAX(tailnet-cutover): the address embeds the PSK and sits in
    # plaintext in the org's OrgFS space, readable by members only; the
    # proper shape is a sealed per-member envelope (M0 §4.1 ③).
    address: str
    key_generation: int
    updated_at: str

    def as_record(self) -> dict[str, object]:
        """The camelCase JSON projection — the frozen wire shape (§4.1)."""
        return {
            "deviceId": self.device_id,
            "owner": self.owner,
            "serverPublic": self.server_public,
            "clientPublic": self.client_public,
            "address": self.address,
            "keyGeneration": self.key_generation,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_record(cls, value: Mapping[str, object]) -> "DirectoryDevice":
        """Strict inverse of :meth:`as_record`; bad data raises ``ValueError``.

        Unknown keys are rejected for the same reason ``DeviceRecord``
        rejects them: the schema is frozen by the cutover contract, and
        documents cross machines via OrgFS sync.
        """
        if not isinstance(value, Mapping):
            raise ValueError(
                f"directory device record must be a JSON object, got {type(value).__name__}"
            )
        unknown = sorted(set(value) - _RECORD_KEYS)
        if unknown:
            raise ValueError(f"directory device record has unknown keys: {unknown}")
        missing = sorted(_RECORD_KEYS - set(value))
        if missing:
            raise ValueError(f"directory device record is missing keys: {missing}")
        return cls(
            device_id=_non_empty_string(value["deviceId"], "deviceId"),
            owner=_non_empty_string(value["owner"], "owner"),
            server_public=_nodekey(value["serverPublic"], "serverPublic"),
            client_public=_nodekey(value["clientPublic"], "clientPublic"),
            address=_non_empty_string(value["address"], "address"),
            key_generation=_generation(value["keyGeneration"]),
            updated_at=_utc_timestamp(value["updatedAt"], "updatedAt"),
        )


def directory_owner_principal(owner: str) -> str:
    """Return the canonical ``user:`` principal for a device-path owner.

    Directory records use the historical bare owner value while OrgFS
    update origins use ``user:<owner>``.  The on-disk path deliberately uses
    the bare value (a colon is reserved in canonical OrgFS references); this
    is the one normalization point used by readers and writers.
    """

    bare = parse_user_uri(owner)
    if bare is None:
        bare = owner
    return canonical_user_uri(bare)


def directory_device_path(owner: str, device_id: str) -> str:
    """Build the owner-scoped path for one opaque, safe device id."""

    principal = directory_owner_principal(owner)
    bare_owner = parse_user_uri(principal)
    assert bare_owner is not None
    _safe_path_component(bare_owner, "device owner")
    _safe_path_component(device_id, "deviceId")
    return f"{DIRECTORY_DIR}/{bare_owner}/{device_id}.json"


def directory_binding_path(user: str) -> str:
    """Build the owner-scoped binding path from the stable user-store key."""

    principal = directory_owner_principal(user)
    bare_user = parse_user_uri(principal)
    assert bare_user is not None
    return f"{PEOPLE_DIR}/{identity_slug(bare_user)}/binding.json"


def protected_directory_author_allowed(
    *, section: str, path_principal: str, author: str
) -> bool:
    """Authorize a protected row without trusting its content.

    Device paths retain the exact historical owner spelling.  People paths
    use the same stable path key as ``UserStore`` and therefore compare that
    key with the authenticated author's slug.
    """

    author_principal = directory_owner_principal(author)
    if section != "people" or author_principal == path_principal:
        return author_principal == path_principal
    path_user = parse_user_uri(path_principal)
    author_user = parse_user_uri(author_principal)
    assert path_user is not None and author_user is not None
    return identity_slug(author_user) == path_user


def _protected_tree_principal(path: str, space_owner: str | None) -> str | None:
    parts = path.split("/")
    if not parts or parts[0] != "directory" or space_owner is None:
        return None
    if len(parts) >= 3 and parts[1] in {"devices", "people"}:
        return directory_owner_principal(parts[2])
    if len(parts) >= 3 and parts[1] == "leaves":
        return directory_owner_principal(parts[2].removesuffix(".json"))
    return directory_owner_principal(space_owner)


def protected_directory_node_id(
    space_id: str, path: str, *, space_owner: str | None
) -> str | None:
    """Return the deterministic identity for a protected directory tree node."""

    principal = _protected_tree_principal(path, space_owner)
    if principal is None:
        return None
    if not space_id or not path:
        raise ValueError("protected directory node identity is incomplete")
    values = (space_id, principal, path)
    payload = "".join(f"{len(value)}:{value}" for value in values).encode("utf-8")
    return _PROTECTED_NODE_ID_PREFIX + _encode_base32(payload)


def parse_protected_directory_node_id(
    node_id: str,
) -> tuple[str, str, str] | None:
    """Decode a canonical protected tree-node identity."""

    if not node_id.startswith(_PROTECTED_NODE_ID_FAMILY):
        return None
    if not node_id.startswith(_PROTECTED_NODE_ID_PREFIX):
        raise ValueError("protected node id uses an unsupported version")
    values = _decode_base32_segments(
        node_id.removeprefix(_PROTECTED_NODE_ID_PREFIX), count=3, kind="node"
    )
    space_id, principal, path = values
    if (
        not space_id
        or directory_owner_principal(principal) != principal
        or not path
        or not (path == "directory" or path.startswith("directory/"))
    ):
        raise ValueError("protected node id fields are invalid")
    return space_id, principal, path


def protected_directory_doc_id(
    space_id: str, path: str, *, space_owner: str | None = None
) -> str | None:
    """Return the arrival-order-independent id for a protected directory doc.

    Four length-prefixed segments encode ``spaceId``, the protected section,
    the canonical path user, and the remaining path.  Unpadded lowercase
    base32 keeps the id within the transport and replica key alphabet and
    single-case.  An id longer than MAX_PROTECTED_DOC_ID_LENGTH is refused
    with ProtectedDocIdTooLongError, never truncated or hashed: every reader
    decodes the principal and path from the id itself.
    Length prefixes make the encoding injective even when a value contains
    the field separator.
    """

    parts = path.split("/")
    section: str
    principal: str
    leaf: str
    if len(parts) >= 4 and parts[:2] == ["directory", "devices"]:
        section = "devices"
        principal = directory_owner_principal(parts[2])
        leaf = "/".join(parts[3:])
    elif len(parts) >= 4 and parts[:2] == ["directory", "people"]:
        section = "people"
        principal = directory_owner_principal(parts[2])
        leaf = "/".join(parts[3:])
    elif (
        len(parts) == 3
        and parts[:2] == ["directory", "leaves"]
        and parts[2].endswith(".json")
    ):
        section = "leaves"
        principal = directory_owner_principal(parts[2].removesuffix(".json"))
        leaf = parts[2]
    elif path == ORG_META_DOC and space_owner is not None:
        section = "org"
        principal = directory_owner_principal(space_owner)
        leaf = "org.json"
    elif (
        len(parts) == 3
        and parts[:2] == ["directory", "invites"]
        and parts[2].endswith(".json")
        and space_owner is not None
    ):
        section = "invites"
        principal = directory_owner_principal(space_owner)
        leaf = parts[2]
    else:
        return None
    if not space_id or not leaf:
        raise ValueError("protected directory document identity is incomplete")
    values = (space_id, section, principal, leaf)
    payload = "".join(f"{len(value)}:{value}" for value in values).encode("utf-8")
    doc_id = _PROTECTED_DOC_ID_PREFIX + _encode_base32(payload)
    if len(doc_id) > MAX_PROTECTED_DOC_ID_LENGTH:
        raise ProtectedDocIdTooLongError(
            f"protected document id would be {len(doc_id)} characters; "
            f"the limit is {MAX_PROTECTED_DOC_ID_LENGTH}"
        )
    return doc_id


class ProtectedDocIdTooLongError(ValueError):
    """A protected path whose deterministic id exceeds the storage limit."""


def _encode_base32(payload: bytes) -> str:
    return base64.b32encode(payload).decode("ascii").rstrip("=").lower()


def _decode_base32_segments(token: str, *, count: int, kind: str) -> list[str]:
    if not token or re.fullmatch(r"[a-z2-7]+", token) is None:
        raise ValueError(f"protected {kind} id has invalid base32")
    try:
        padded = token.upper() + "=" * (-len(token) % 8)
        payload = base64.b32decode(padded).decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"protected {kind} id has invalid base32") from exc
    if _encode_base32(payload.encode("utf-8")) != token:
        raise ValueError(f"protected {kind} id base32 is not canonical")
    cursor = 0
    values: list[str] = []
    for _index in range(count):
        separator = payload.find(":", cursor)
        if separator == -1:
            raise ValueError(f"protected {kind} id has no segment length")
        length_text = payload[cursor:separator]
        if (
            not length_text.isascii()
            or not length_text.isdigit()
            or (length_text.startswith("0") and length_text != "0")
        ):
            raise ValueError(
                f"protected {kind} id has a non-canonical segment length"
            )
        length = int(length_text)
        start = separator + 1
        end = start + length
        if end > len(payload):
            raise ValueError(f"protected {kind} id segment is truncated")
        values.append(payload[start:end])
        cursor = end
    if cursor != len(payload):
        raise ValueError(f"protected {kind} id has trailing data")
    return values


def parse_protected_directory_doc_id(
    doc_id: str,
) -> tuple[str, str, str, str] | None:
    """Decode a canonical protected id, or return ``None`` for ordinary ids."""

    if not doc_id.startswith(_PROTECTED_DOC_ID_FAMILY):
        return None
    if not doc_id.startswith(_PROTECTED_DOC_ID_PREFIX):
        raise ValueError("protected document id uses an unsupported version")
    token = doc_id.removeprefix(_PROTECTED_DOC_ID_PREFIX)
    if len(doc_id) > MAX_PROTECTED_DOC_ID_LENGTH:
        raise ValueError("protected document id is too long")
    values = _decode_base32_segments(token, count=4, kind="document")
    space_id, section, principal, leaf = values
    if (
        not space_id
        or section not in _PROTECTED_DOC_SECTIONS
        or directory_owner_principal(principal) != principal
        or not leaf
    ):
        raise ValueError("protected document id fields are invalid")
    return space_id, section, principal, leaf


def org_space_name(org: str) -> str:
    """Return the org's directory-space id, identical to its ``group-*`` name.

    The existing name grammar applies to the complete Casdoor group name.
    ``-acl`` is reserved so a directory id can never collide with another
    org's ACL-space id.
    """
    if (
        not isinstance(org, str)
        or _ORG_NAME.fullmatch(org) is None
        or not org.startswith(ORG_SPACE_PREFIX)
        or len(org) == len(ORG_SPACE_PREFIX)
        or org.endswith(_ORG_ACL_SUFFIX)
    ):
        raise ValueError(
            "org name must start with 'group-', match "
            "[a-z0-9][a-z0-9-]{0,62}, and not end with reserved '-acl': "
            + repr(org if isinstance(org, str) else type(org).__name__)
        )
    return org


def is_org_acl_space(name: object) -> bool:
    """Whether ``name`` is exactly the reserved ACL space for a valid org."""

    if not isinstance(name, str) or not name.endswith(_ORG_ACL_SUFFIX):
        return False
    try:
        org_space_name(name[: -len(_ORG_ACL_SUFFIX)])
    except ValueError:
        return False
    return True


def is_org_directory_space(name: object) -> bool:
    """Whether ``name`` is a valid org directory space, excluding ACL spaces."""

    if not isinstance(name, str):
        return False
    try:
        return org_space_name(name) == name
    except ValueError:
        return False


def org_from_space_name(name: str) -> str | None:
    """Inverse of :func:`org_space_name`; ``None`` when not an org space.

    Personal and future non-org spaces must be ignorable, not fatal, so a
    non-org name is a ``None`` answer rather than an error.
    """
    return name if is_org_directory_space(name) else None


def _non_empty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _safe_path_component(value: object, label: str) -> str:
    text = _non_empty_string(value, label)
    if text in {".", ".."} or "/" in text or "\\" in text:
        raise ValueError(f"{label} must be one safe path component")
    return text


def _nodekey(value: object, label: str) -> str:
    # Same shape check the local record applies; directory rows come from
    # other machines' records, so it is re-checked here, not trusted.
    text = _non_empty_string(value, label)
    if not text.startswith("nodekey:") or len(text) <= len("nodekey:"):
        raise ValueError(f"{label} must look like 'nodekey:…'")
    return text


def _generation(value: object) -> int:
    # ``bool`` is an ``int`` subclass; a JSON ``true`` must not pass as 1.
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("keyGeneration must be an integer >= 1")
    return value


def _utc_timestamp(value: object, label: str) -> str:
    """Require an ISO8601 string whose offset is exactly UTC."""
    text = _non_empty_string(value, label)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO8601 timestamp: {error}") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must carry a UTC (zero) offset")
    return text
