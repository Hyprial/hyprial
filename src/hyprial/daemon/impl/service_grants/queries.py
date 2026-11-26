# Decision notes (service grant S3, 2026-11-26):
# - Effectiveness has exactly one implementation (is_grant_effective) shared by
#   service.grants, S4-py admission projection, and S5 recipient consumption.
# - Security invariants for S5 consumers are enforced and documented here:
#   grantor is taken solely from the OrgFS path (never from document body or
#   envelope header), and credential opening requires an exact (owner, deviceId)
#   recipient match on an effective grant (never "@members").
"""Pure service-grant query and OrgFS loading helpers shared with S4-py and S5."""

from __future__ import annotations

from collections.abc import Iterable

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore

from .models import (
    ALL_MEMBERS,
    GrantRecipient,
    ServiceGrant,
    StoredServiceGrant,
    normalize_bare_owner,
)


def is_grant_effective(grant: ServiceGrant, *, now_ms: int) -> bool:
    """Return whether ``grant`` is neither revoked nor expired at ``now_ms``.

    This is the sole effectiveness check across S3 ``service.grants``,
    S4-py exposure admission projection, and S5 recipient consumption.
    A grant is expired once ``expires_at_ms <= now_ms``.
    """

    if type(now_ms) is not int or now_ms < 0:
        raise ValueError("now_ms must be a non-negative integer")
    if grant.revoked_at_ms is not None:
        return False
    if grant.expires_at_ms is not None and grant.expires_at_ms <= now_ms:
        return False
    return True


def effective_grants_for_service(
    grants: Iterable[StoredServiceGrant],
    *,
    owner: str,
    device_id: str,
    port: int,
    now_ms: int,
) -> tuple[StoredServiceGrant, ...]:
    """Return effective grants authored by ``owner`` for ``(device_id, port)``.

    ``owner`` may be bare or ``user:<owner>`` and is matched against each
    grant's OrgFS path-derived ``grantor``. Intended for S4-py admission
    projection so grantor-side policy never parses raw OrgFS JSON again.
    """

    bare_owner = normalize_bare_owner(owner)
    return tuple(
        item
        for item in grants
        if item.grantor == bare_owner
        and item.grant.service.device_id == device_id
        and item.grant.service.port == port
        and is_grant_effective(item.grant, now_ms=now_ms)
    )


def recipient_matches(
    grant: ServiceGrant, *, bare_owner: str, device_id: str
) -> bool:
    """Return whether ``grant`` targets ``(bare_owner, device_id)`` or ``@members``."""

    if grant.recipient == ALL_MEMBERS:
        return True
    return (
        isinstance(grant.recipient, GrantRecipient)
        and grant.recipient.owner == bare_owner
        and grant.recipient.device_id == device_id
    )


def effective_grants_for_recipient(
    grants: Iterable[StoredServiceGrant],
    *,
    owner: str,
    device_id: str,
    now_ms: int,
) -> tuple[StoredServiceGrant, ...]:
    """Return effective grants whose recipient is ``(owner, device_id)`` or ``@members``.

    Each returned :class:`StoredServiceGrant` carries ``grantor`` taken strictly
    from the OrgFS document path (``directory/service-grants/<grantor>/<grantId>.json``).

    Recipient security invariants (for S5 consumers):
    - Always trust ``stored.grantor`` (from the OrgFS path) as the grantor and
      never read grantor identity from the document body or sealed envelope header.
    - Before surfacing a service endpoint, verify that ``grant.service.device_id``
      exists under ``directory/devices/<stored.grantor>/``.
    - Never attempt to open ``grant.credential`` unless
      :func:`can_open_grant_credential` returns ``True`` (exact ``(owner, device_id)``
      match; ``@members`` grants never carry credentials).
    """

    bare_owner = normalize_bare_owner(owner)
    return tuple(
        item
        for item in grants
        if recipient_matches(item.grant, bare_owner=bare_owner, device_id=device_id)
        and is_grant_effective(item.grant, now_ms=now_ms)
    )


def can_open_grant_credential(
    stored: StoredServiceGrant,
    *,
    recipient_owner: str,
    recipient_device_id: str,
    now_ms: int,
) -> bool:
    """Return whether this device may pass ``stored.grant.credential`` to ``open_service_grant``.

    Recipient security invariants:
    - The grant must be effective (not revoked and not expired at ``now_ms``).
    - ``stored.grant.credential`` must be present (non-null).
    - ``stored.grant.recipient`` must be a structured :class:`GrantRecipient`
      matching ``(bare recipient_owner, recipient_device_id)`` exactly;
      ``@members`` grants never qualify.
    - Callers must supply ``grantor=stored.grantor`` (from the OrgFS path) to
      ``EncryptionKey.Store.open_service_grant(...)``.
    """

    if stored.grant.credential is None:
        return False
    if not is_grant_effective(stored.grant, now_ms=now_ms):
        return False
    recipient = stored.grant.recipient
    if not isinstance(recipient, GrantRecipient):
        return False
    bare_owner = normalize_bare_owner(recipient_owner)
    return recipient.owner == bare_owner and recipient.device_id == recipient_device_id


def load_org_service_grants(
    store: OrgFsDirectoryStore,
    org: str,
    *,
    owner: str | None = None,
) -> tuple[StoredServiceGrant, ...]:
    """Load and validate all verified service grants from OrgFS for ``org``.

    Invalid documents or documents whose ``grantId`` disagrees with the filename
    are skipped rather than aborting observation of valid grants.
    """

    loaded: list[StoredServiceGrant] = []
    for bare_owner, filename_grant_id, record in store.list_service_grants(
        org, owner=owner
    ):
        try:
            grant = ServiceGrant.from_record(record)
            if grant.grant_id != filename_grant_id:
                continue
            loaded.append(
                StoredServiceGrant(org=org, grantor=bare_owner, grant=grant)
            )
        except ValueError:
            continue
    return tuple(loaded)


__all__ = [
    "can_open_grant_credential",
    "effective_grants_for_recipient",
    "effective_grants_for_service",
    "is_grant_effective",
    "load_org_service_grants",
    "recipient_matches",
]
