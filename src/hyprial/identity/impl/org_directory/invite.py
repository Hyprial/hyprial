"""Invite links: the one artifact that crosses the human channel (§2.4).

The link carries everything an invitee needs to reach the inviter — org,
directory and ACL spaces, inviter identity, inviter's Tailcat address — plus the join
workflow bytes and their sha256, so the invitee can verify what it is
about to execute.  Expiry is carried inside the payload, not trusted from
the transport.

Out of scope this round, by ruling (§3.4): administrator device
signatures and atomic one-time consumption.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from hyprial.identity.impl.org_directory.models import org_space_name

__all__ = [
    "INVITE_MALFORMED",
    "INVITE_DIGEST_MISMATCH",
    "INVITE_EXPIRED",
    "INVITE_SCHEME",
    "InviteError",
    "InviteLink",
    "decode_invite",
    "encode_invite",
    "invite_deep_link",
    "invite_https_url",
]

INVITE_SCHEME = "hyprial-invite:v1:"

#: The deep-link form's fixed authority (§5.1): ``hyprial://join#v1.<p>``.
DEEP_LINK_BASE = "hyprial://join"
#: The fragment marker every URL form carries: ``#v1.<payload>``.
FRAGMENT_PREFIX = "v1."

INVITE_MALFORMED = "INVITE_MALFORMED"
INVITE_EXPIRED = "INVITE_EXPIRED"
INVITE_DIGEST_MISMATCH = "INVITE_DIGEST_MISMATCH"

#: ``secrets.token_urlsafe`` alphabet; the link's one-time bearer secret.
_TOKEN = re.compile(r"[A-Za-z0-9_-]+")
#: sha256 as lowercase hex, exactly as ``hashlib.hexdigest`` produces it.
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")

#: The exact camelCase keys of the encoded payload (§4.1).
_RECORD_KEYS = frozenset(
    {
        "org",
        "spaceId",
        "aclSpaceId",
        "inviter",
        "inviterDevice",
        "inviterAddress",
        "token",
        "workflowYaml",
        "workflowSha256",
        "expiresAt",
    }
)


class InviteError(ValueError):
    """A link that cannot be trusted or executed; ``code`` names why."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class InviteLink:
    """An invitation to join one org, addressed to one invitee at a time.

    ``inviter_address`` and ``token`` are secrets: the link must travel a
    private channel, and nothing here logs or echoes them.
    """

    org: str
    space_id: str
    acl_space_id: str
    inviter: str
    inviter_device: str
    inviter_address: str
    token: str
    workflow_yaml: str
    workflow_sha256: str
    expires_at: str


def encode_invite(link: InviteLink) -> str:
    """Serialize as ``INVITE_SCHEME`` + unpadded base64url of the JSON.

    Shape problems are raised here (as ``INVITE_MALFORMED``) rather than
    baked into a link that only fails at the far end.  Digest consistency
    and expiry are the decoder's job — an expired link still encodes.
    """
    return INVITE_SCHEME + _payload(link)


def invite_https_url(link: InviteLink, base_url: str) -> str:
    """The clickable https form: ``<base_url>/join#v1.<payload>`` (§5.1).

    ``base_url`` is the profile's ``inviteBaseUrl``; the payload rides in
    the fragment so a statically hosted landing page never sees it.
    """
    if not isinstance(base_url, str) or not base_url.startswith("https://"):
        raise InviteError(
            INVITE_MALFORMED,
            f"invite base URL must be an https:// URL, got {base_url!r}",
        )
    return f"{base_url.rstrip('/')}/join#{FRAGMENT_PREFIX}{_payload(link)}"


def invite_deep_link(link: InviteLink) -> str:
    """The deep-link form: ``hyprial://join#v1.<payload>`` (§5.1)."""

    return f"{DEEP_LINK_BASE}#{FRAGMENT_PREFIX}{_payload(link)}"


def _payload(link: InviteLink) -> str:
    """The unpadded base64url JSON payload shared by all three forms."""

    try:
        record = _link_record(link)
    except ValueError as error:
        raise InviteError(INVITE_MALFORMED, str(error)) from error
    return _base64url(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def decode_invite(text: str) -> InviteLink:
    """Decode and verify a link; raises :class:`InviteError` on any doubt.

    Checks, in the order the contract fixes: scheme and payload shape
    (``INVITE_MALFORMED``), digest ``sha256(workflow_yaml) ==
    workflow_sha256`` (``INVITE_DIGEST_MISMATCH``), then expiry
    (``INVITE_EXPIRED``).

    Accepts every form of the same payload (§5.1): the bare-token form
    ``hyprial-invite:v1:<p>``, the https form ``https://<host>/join#v1.<p>``,
    the deep link ``hyprial://join#v1.<p>``, and the bare ``v1.<p>``.
    Surrounding whitespace and one pair of matching quotes are stripped
    first — the link travels through chat clients and shell quoting.
    Anything else is ``INVITE_MALFORMED``.

    # LAX(tailnet-cutover): no administrator device signature, no atomic
    # one-time consumption, and no host whitelist on the https form; the
    # proper link is signed by an admin device key, redeemed exactly once,
    # and pinned to the operator's invite host (§3.4, §5.1).
    """
    body = _extract_payload(text)
    try:
        padded = body + "=" * (-len(body) % 4)
        payload = base64.b64decode(padded, altchars=b"-_", validate=True)
        record = json.loads(payload.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as error:
        raise InviteError(
            INVITE_MALFORMED, f"invite payload is not base64url JSON: {error}"
        ) from error
    link = _link_from_record(record)
    digest = hashlib.sha256(link.workflow_yaml.encode("utf-8")).hexdigest()
    if digest != link.workflow_sha256:
        raise InviteError(
            INVITE_DIGEST_MISMATCH,
            "workflow_sha256 does not match workflow_yaml",
        )
    if datetime.now(UTC) >= _parse_utc(link.expires_at):
        raise InviteError(INVITE_EXPIRED, f"invite expired at {link.expires_at}")
    return link


def _base64url(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _extract_payload(text: object) -> str:
    """Strip transport dressing and return the bare payload, or refuse.

    The four accepted forms share one payload (§5.1); everything about
    the dressing — scheme, host, path — is checked here so the decoder
    proper only ever sees the payload itself.
    """

    if not isinstance(text, str):
        raise InviteError(INVITE_MALFORMED, "invite must be a string")
    candidate = text.strip()
    if (
        len(candidate) >= 2
        and candidate[0] == candidate[-1]
        and candidate[0] in ("'", '"')
    ):
        candidate = candidate[1:-1].strip()
    if candidate.startswith(INVITE_SCHEME):
        return candidate[len(INVITE_SCHEME) :]
    if candidate.startswith(FRAGMENT_PREFIX):
        return candidate[len(FRAGMENT_PREFIX) :]
    if "://" in candidate:
        base, separator, fragment = candidate.partition("#")
        if not separator or not fragment.startswith(FRAGMENT_PREFIX):
            raise InviteError(
                INVITE_MALFORMED,
                f"invite URL must carry the payload as '#{FRAGMENT_PREFIX}<payload>'",
            )
        if base.startswith("https://"):
            remainder = base[len("https://") :]
            slash = remainder.find("/")
            path = remainder[slash:] if slash >= 0 else ""
            # LAX(tailnet-cutover): any host is accepted; the proper
            # decoder pins the operator's invite host (§5.1).
            if slash <= 0 or not path.endswith("/join"):
                raise InviteError(
                    INVITE_MALFORMED,
                    f"invite https URL path must end with '/join', got {base!r}",
                )
        elif base.rstrip("/") == DEEP_LINK_BASE:
            pass
        else:
            raise InviteError(
                INVITE_MALFORMED,
                f"invite URL must be https://<host>/join or {DEEP_LINK_BASE}, "
                f"got {base!r}",
            )
        return fragment[len(FRAGMENT_PREFIX) :]
    raise InviteError(
        INVITE_MALFORMED,
        f"invite must start with {INVITE_SCHEME!r}, an https join URL, "
        f"{DEEP_LINK_BASE!r}, or {FRAGMENT_PREFIX!r}",
    )


def _link_record(link: InviteLink) -> dict[str, object]:
    """The camelCase payload, after checking every field's shape."""
    return {
        "org": _org(link.org),
        "spaceId": _field(link.space_id, "spaceId"),
        "aclSpaceId": _field(link.acl_space_id, "aclSpaceId"),
        "inviter": _field(link.inviter, "inviter"),
        "inviterDevice": _field(link.inviter_device, "inviterDevice"),
        "inviterAddress": _field(link.inviter_address, "inviterAddress"),
        "token": _token(link.token),
        "workflowYaml": _field(link.workflow_yaml, "workflowYaml"),
        "workflowSha256": _digest(link.workflow_sha256),
        "expiresAt": _canonical_timestamp(link.expires_at),
    }


def _link_from_record(value: object) -> InviteLink:
    if not isinstance(value, dict):
        raise InviteError(
            INVITE_MALFORMED,
            f"invite payload must be a JSON object, got {type(value).__name__}",
        )
    unknown = sorted(set(value) - _RECORD_KEYS)
    if unknown:
        raise InviteError(INVITE_MALFORMED, f"invite payload has unknown keys: {unknown}")
    missing = sorted(_RECORD_KEYS - set(value))
    if missing:
        raise InviteError(INVITE_MALFORMED, f"invite payload is missing keys: {missing}")
    try:
        return InviteLink(
            org=_org(value["org"]),
            space_id=_field(value["spaceId"], "spaceId"),
            acl_space_id=_field(value["aclSpaceId"], "aclSpaceId"),
            inviter=_field(value["inviter"], "inviter"),
            inviter_device=_field(value["inviterDevice"], "inviterDevice"),
            inviter_address=_field(value["inviterAddress"], "inviterAddress"),
            token=_token(value["token"]),
            workflow_yaml=_field(value["workflowYaml"], "workflowYaml"),
            workflow_sha256=_digest(value["workflowSha256"]),
            expires_at=_timestamp(value["expiresAt"]),
        )
    except ValueError as error:
        raise InviteError(INVITE_MALFORMED, str(error)) from error


def _org(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("org must be a string")
    try:
        org_space_name(value)
    except ValueError as error:
        raise ValueError(str(error)) from error
    return value


def _field(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _token(value: object) -> str:
    text = _field(value, "token")
    if _TOKEN.fullmatch(text) is None:
        raise ValueError("token must contain only [A-Za-z0-9_-]")
    return text


def _digest(value: object) -> str:
    text = _field(value, "workflowSha256")
    if _SHA256_HEX.fullmatch(text) is None:
        raise ValueError("workflowSha256 must be 64 lowercase hex characters")
    return text


def _timestamp(value: object) -> str:
    text = _field(value, "expiresAt")
    _parse_utc(text)
    return text


def _canonical_timestamp(value: object) -> str:
    """The wire form: UTC with a literal ``Z`` (``...T12:00:00[.ffffff]Z``).

    The landing page (hyprial.ai/join) accepts only RFC 3339 ``Z`` times, so
    an ``isoformat()`` value ending in ``+00:00`` must never reach a link.
    """

    parsed = _parse_utc(_timestamp(value)).astimezone(UTC)
    return parsed.isoformat().replace("+00:00", "Z")


def _parse_utc(text: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise ValueError(f"expiresAt must be an ISO8601 timestamp: {error}") from error
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("expiresAt must carry a UTC (zero) offset")
    return parsed
