# Decision notes (service grant documents, 2026-11-26):
# - Credential sealing reads the value only through identity's public
#   read_user_provider_value, so record decoding, route consistency, privacy
#   checks and field selection are identity's rules (grant documents fix). Without
#   fieldName an unconfigured entry yields its whole file text and a configured
#   record is refused as field-required. Plaintext bytes never enter logs,
#   events, return payloads, or exception messages.
# - No new error codes are introduced; every refusal uses existing frozen codes
#   in hyprial.kernel.ipc_errors.
# - Grant documents fix (2026-11-26): a grant is refused unless
#   this device has a row under the caller in that org's directory; identifiers
#   are validated before any secret is read. Revocation never depends on
#   parsing the old document: an unreadable own document is replaced by a
#   revoked tombstone that keeps whatever fields still validate.
# - Integration fix (2026-11-27): at most one effective grant exists per
#   (local device, port, recipient), and a grant that finds it rewrites it in
#   place: the grantId and grantedAtMs stay, credential, expiry, name,
#   protocol and note are written again and updatedAtMs is set. The recipient
#   keeps the same Key entry and service entry, and its own edits to them. A
#   new grantId is issued only when no effective grant exists (never granted,
#   revoked or expired). Should a race have left several effective grants for
#   one target, the earliest is updated and the others are revoked. The reply
#   is {grant, updated}; a first grant emits service.grant.created and an
#   update service.grant.updated, with the same fields.
# - Every grant and revoke also deletes the caller's own documents in that
#   org that died (were revoked, or expired) more than CLEANUP_AFTER_MS ago,
#   so a grantor's directory stays within what recipients read
#   (MAX_GRANTOR_GRANT_DOCUMENTS). A document that cannot be parsed is not
#   touched. A failed deletion never fails the write: it is one
#   service.grant.cleanup_failed event; what was deleted is one
#   service.grant.cleaned event.
# - A new grantId is refused while the caller holds GRANT_DOCUMENT_CAP
#   documents in the org after that cleanup; an update in place is never
#   refused. No frozen error code names a quota, so the refusal is
#   SERVICE_UNAVAILABLE, already declared for service.grant, with a message
#   that says why and what to do.
# - A credential without a name is refused (INVALID_ARGUMENT): the recipient
#   derives a service entry only from a named grant, so the Key would arrive
#   with nothing to use it through. Without a credential a name stays
#   optional: such a grant only admits the device.
# - service.grants reads other grantors within the recipient's scan bounds:
#   a grantor over a limit is left out of "received" and named in
#   limitedGrantors (grantor null when the whole org stopped), so the listing
#   never answers "no grant" for what the daemon could not read either. The
#   caller's own grants are listed whole. documentCount gives each org's
#   document count under the caller, beside documentLimit, the per-grantor
#   bound recipients read by. Without org, an org whose grants cannot be read
#   is left out of every other key and named in unreadableOrgs, so it is
#   never reported as "no grant"; asked for that one org, the request refuses.
# - Received status (2026-11-27): the four added received-row fields are read
#   from the existing Key ledger and an existing manager's snapshot/bindings.
#   No listing starts service-connect. Without a manager, connected is false
#   and serviceName is null because no new derivation is done for display.
"""Service-grant domain service: ``service.grant``, ``service.revoke``, ``service.grants``."""

from __future__ import annotations

import base64
import dataclasses
import time
from collections.abc import Callable, Mapping, Set as AbstractSet
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.identity import (
    SealedEnvelope,
    SecretResolutionError,
    org_space_name,
    read_user_provider_value,
)
from hyprial.kernel import DaemonRequestError, ipc_errors

from .consumer.keys import stored_key
from .models import (
    ALL_MEMBERS,
    GRANT_PROTOCOLS,
    SCHEMA_VERSION,
    GrantRecipient,
    ServiceEndpoint,
    ServiceGrant,
    StoredServiceGrant,
    generate_grant_id,
    grant_view,
    normalize_bare_owner,
    parse_device_target,
    received_grant_view,
    validate_grant_id,
)
from .queries import (
    MAX_GRANTOR_GRANT_DOCUMENTS,
    GrantScanLimited,
    ScanLimits,
    effective_grants_for_recipient,
    is_grant_effective,
    load_org_service_grants,
    recipient_matches,
    scan_org_service_grants,
)

GRANT_DOCUMENT_CAP = 240
CLEANUP_AFTER_MS = 7 * 24 * 60 * 60 * 1000
CREATED_EVENT = "service.grant.created"
UPDATED_EVENT = "service.grant.updated"
CLEANED_EVENT = "service.grant.cleaned"
CLEANUP_FAILED_EVENT = "service.grant.cleanup_failed"
_CREDENTIAL_KEYS = frozenset({"entryId", "fieldName"})
_UNPARSEABLE = (ValueError, TypeError, KeyError, RecursionError)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _translate_orgfs_error(error: OrgFsError, *, context: str) -> DaemonRequestError:
    if error.code in {"not-a-member", "not-owner", "protected-path"}:
        return DaemonRequestError(
            ipc_errors.SERVICE_NOT_AUTHORIZED,
            f"not authorized to {context} in org directory",
        )
    if error.code == "unknown-space":
        return DaemonRequestError(
            ipc_errors.SERVICE_NOT_REGISTERED,
            "org directory space is not available",
        )
    if error.code == "unknown-doc":
        return DaemonRequestError(
            ipc_errors.SERVICE_UNAVAILABLE,
            "org service-grants directory is not initialized yet; it is created the next time "
            "the org owner's upgraded daemon publishes its device row",
        )
    return DaemonRequestError(
        ipc_errors.SERVICE_UNAVAILABLE,
        f"org directory unavailable while attempting to {context}",
    )


def _parse_credential_ref(credential: object) -> tuple[str, str | None]:
    if not isinstance(credential, Mapping):
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, "credential must be an object"
        )
    unknown = set(credential) - _CREDENTIAL_KEYS
    if unknown or "entryId" not in credential:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            "credential must contain entryId and optional fieldName only",
        )
    entry_id = credential.get("entryId")
    field_name = credential.get("fieldName")
    if type(entry_id) is not str or (field_name is not None and type(field_name) is not str):
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, "credential.entryId and credential.fieldName must be strings"
        )
    return entry_id, field_name


def _read_secret(home: Path, entry_id: str, field_name: str | None) -> bytes:
    try:
        value = read_user_provider_value(home, entry_id, field_name)
    except SecretResolutionError as error:
        raise DaemonRequestError(error.code, str(error)) from None
    except ValueError:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, "credential.entryId or credential.fieldName is invalid"
        ) from None
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError:
        raise DaemonRequestError(
            ipc_errors.AGENT_SECRET_RESOLUTION_FAILED,
            f"secret resolution invalid: entry={entry_id!r} phase='encode'",
        ) from None


def _died_at(grant: ServiceGrant) -> int | None:
    ends = [value for value in (grant.revoked_at_ms, grant.expires_at_ms) if value is not None]
    return min(ends) if ends else None


def _row_owner(row: Any) -> str | None:
    try:
        return normalize_bare_owner(row.owner)
    except ValueError:
        return None


class ServiceGrantService:
    """Owner-authored service grant lifecycle and readonly listing."""

    def __init__(
        self,
        *,
        store: OrgFsDirectoryStore,
        home: Path,
        owner: str,
        local_device_id: str,
        exposed_ports: Callable[[], AbstractSet[int]],
        now_ms: Callable[[], int] | None = None,
        logger: Callable[..., None] | None = None,
        status: Callable[[], Mapping[tuple[str, str], Mapping[str, Any]]] | None = None,
    ) -> None:
        self._store = store
        self._home = Path(home)
        self._owner = normalize_bare_owner(owner)
        if not isinstance(local_device_id, str) or not local_device_id:
            raise ValueError("local_device_id must be a non-empty string")
        self._local_device_id = local_device_id
        self._exposed_ports = exposed_ports
        self._now_ms = now_ms or _now_ms
        self._logger = logger
        self._status = status

    def _received_view(
        self, item: StoredServiceGrant, now: int, statuses: Mapping[tuple[str, str], Mapping[str, Any]]
    ) -> dict[str, Any]:
        entry_id = stored_key(self._home, item.org, item.grant.grant_id)
        status = statuses.get((item.org, item.grant.grant_id), {})
        service_name = status.get("serviceName")
        return received_grant_view(
            item,
            now_ms=now,
            effective=is_grant_effective(item.grant, now_ms=now),
            key_stored=entry_id is not None,
            entry_id=entry_id,
            connected=status.get("connected") is True,
            service_name=service_name if isinstance(service_name, str) else None,
        )

    def grant(
        self,
        *,
        org: str,
        port: int,
        device: str,
        expires_at_ms: int | None = None,
        name: str | None = None,
        protocol: str | None = None,
        note: str | None = None,
        credential: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Authorize one recipient device or ``@members`` to reach an exposed local port."""

        now = self._now_ms()
        try:
            org_space_name(org)
            if type(port) is not int or not (1 <= port <= 65535):
                raise ValueError("port must be an integer in 1..65535")
            recipient = parse_device_target(device)
            resolved_protocol = "tcp" if protocol is None else protocol
            if resolved_protocol not in GRANT_PROTOCOLS:
                raise ValueError("protocol must be 'tcp' or 'http'")
            if expires_at_ms is not None:
                if type(expires_at_ms) is not int or expires_at_ms <= now:
                    raise ValueError("expiresAtMs must be an integer in the future")
            endpoint = ServiceEndpoint(
                device_id=self._local_device_id,
                port=port,
                name=name,
                protocol=resolved_protocol,
                note=note,
            )
        except ValueError as error:
            raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from None

        cred_ref: tuple[str, str | None] | None = None
        if credential is not None:
            if recipient == ALL_MEMBERS:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "service grant for @members cannot include a credential",
                )
            if endpoint.name is None:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "a service grant with a credential needs a name: the recipient "
                    "uses the Key through a service entry, and gets one only for a "
                    "named service",
                )
            cred_ref = _parse_credential_ref(credential)

        if port not in self._exposed_ports():
            raise DaemonRequestError(
                ipc_errors.SERVICE_NOT_REGISTERED,
                f"port {port} is not currently exposed on this device",
            )

        try:
            devices = self._store.list_devices(org)
        except OrgFsError as error:
            raise _translate_orgfs_error(error, context="read org directory") from None
        if not any(
            row.device_id == self._local_device_id and _row_owner(row) == self._owner
            for row in devices
        ):
            raise DaemonRequestError(
                ipc_errors.SERVICE_DEVICE_UNAVAILABLE,
                "this device has not published its row under the caller in that org directory",
            )

        recipient_public_bytes: bytes | None = None
        if isinstance(recipient, GrantRecipient):
            target_row = next(
                (
                    row
                    for row in devices
                    if row.device_id == recipient.device_id
                    and _row_owner(row) == recipient.owner
                ),
                None,
            )
            if target_row is None:
                raise DaemonRequestError(
                    ipc_errors.SERVICE_DEVICE_UNAVAILABLE,
                    "recipient device is not registered under that owner in the org directory",
                )
            if cred_ref is not None:
                if (
                    target_row.encryption_key_id is None
                    or target_row.encryption_public_key is None
                ):
                    raise DaemonRequestError(
                        ipc_errors.SERVICE_DEVICE_UNAVAILABLE,
                        "recipient device has not published an encryption public key",
                    )
                try:
                    recipient_public_bytes = base64.b64decode(
                        target_row.encryption_public_key, validate=True
                    )
                except ValueError:
                    raise DaemonRequestError(
                        ipc_errors.SERVICE_DEVICE_UNAVAILABLE,
                        "recipient device encryption public key is invalid",
                    ) from None

        current = self._current(org, port=port, recipient=recipient, now=now)
        self._clean(org, now)
        if current is None:
            self._check_room(org)
            grant_id = generate_grant_id()
            granted_at_ms: int | None = now
            updated_at_ms: int | None = None
        else:
            grant_id = current.grant_id
            granted_at_ms = current.granted_at_ms
            updated_at_ms = now

        sealed_credential: dict[str, Any] | None = None
        if cred_ref is not None:
            assert isinstance(recipient, GrantRecipient)
            assert recipient_public_bytes is not None
            entry_id, field_name = cred_ref
            secret_bytes = _read_secret(self._home, entry_id, field_name)
            try:
                sealed_credential = SealedEnvelope.seal_service_grant(
                    secret_bytes,
                    org=org,
                    grant_id=grant_id,
                    grantor=self._owner,
                    service_device=self._local_device_id,
                    port=port,
                    recipient_owner=recipient.owner,
                    recipient_device=recipient.device_id,
                    recipient_public_key=recipient_public_bytes,
                )
            except SealedEnvelope.ServiceGrantError as error:
                raise DaemonRequestError(error.code, str(error)) from None

        grant = ServiceGrant(
            schema_version=SCHEMA_VERSION,
            grant_id=grant_id,
            service=endpoint,
            recipient=recipient,
            granted_at_ms=granted_at_ms,
            expires_at_ms=expires_at_ms,
            revoked_at_ms=None,
            credential=sealed_credential,
            updated_at_ms=updated_at_ms,
        )
        try:
            self._store.put_service_grant(org, self._owner, grant_id, grant.as_record())
        except OrgFsError as error:
            raise _translate_orgfs_error(error, context="write service grant") from None

        stored = StoredServiceGrant(org=org, grantor=self._owner, grant=grant)
        self._emit(
            "info",
            CREATED_EVENT if current is None else UPDATED_EVENT,
            org=org,
            grantId=grant_id,
            grantor=self._owner,
            serviceDeviceId=self._local_device_id,
            port=port,
            recipient=(
                ALL_MEMBERS
                if isinstance(recipient, str)
                else f"{recipient.owner}/{recipient.device_id}"
            ),
            hasCredential=sealed_credential is not None,
            expiresAtMs=expires_at_ms,
        )
        return {
            "grant": grant_view(stored, now_ms=now, effective=is_grant_effective(grant, now_ms=now)),
            "updated": current is not None,
        }

    def _current(
        self, org: str, *, port: int, recipient: GrantRecipient | str, now: int
    ) -> ServiceGrant | None:
        """The effective grant to rewrite for this target; extra ones a race left are revoked."""

        matches = sorted(
            (
                item.grant
                for item in self._own_effective(org, port=port, now=now)
                if item.grant.recipient == recipient
            ),
            key=lambda grant: (grant.granted_at_ms or 0, grant.grant_id),
        )
        for extra in matches[1:]:
            self._revoke(org, extra.grant_id)
        return matches[0] if matches else None

    def _check_room(self, org: str) -> None:
        try:
            count = len(self._store.service_grant_ids(org, self._owner))
        except OrgFsError as error:
            raise _translate_orgfs_error(error, context="count service grants") from None
        if count >= GRANT_DOCUMENT_CAP:
            raise DaemonRequestError(
                ipc_errors.SERVICE_UNAVAILABLE,
                f"service grant document limit reached in org {org}: {count} of "
                f"{GRANT_DOCUMENT_CAP} documents hold no room for a new grant "
                f"(recipients read at most {MAX_GRANTOR_GRANT_DOCUMENTS} per grantor); "
                "renewing an existing grant still works; "
                "revoke grants you no longer use; they are deleted 7 days later",
            )

    def _clean(self, org: str, now: int) -> None:
        """Delete the caller's documents in ``org`` dead for more than CLEANUP_AFTER_MS; never raises."""

        try:
            loaded = load_org_service_grants(self._store, org, owner=self._owner)
        except Exception as error:  # noqa: BLE001 - cleanup never fails the write
            self._cleanup_failed(org, None, error)
            return
        removed: list[str] = []
        for item in loaded:
            died_at = _died_at(item.grant)
            if died_at is None or now - died_at <= CLEANUP_AFTER_MS:
                continue
            try:
                if self._store.remove_service_grant(org, self._owner, item.grant.grant_id):
                    removed.append(item.grant.grant_id)
            except Exception as error:  # noqa: BLE001 - cleanup never fails the write
                self._cleanup_failed(org, item.grant.grant_id, error)
        if removed:
            self._emit("info", CLEANED_EVENT, org=org, grantor=self._owner, grantIds=removed)

    def _cleanup_failed(self, org: str, grant_id: str | None, error: Exception) -> None:
        code = getattr(error, "code", None)
        self._emit(
            "warn",
            CLEANUP_FAILED_EVENT,
            org=org,
            grantor=self._owner,
            grantId=grant_id,
            errorCode=code if isinstance(code, str) else type(error).__name__,
        )

    def revoke(self, *, org: str, grant_id: str) -> dict[str, Any]:
        """Revoke one of the caller's grants by setting ``revokedAtMs`` and clearing ``credential``.

        Revocation does not depend on parsing the stored document. When the
        caller's own document exists but cannot be read or validated, a
        minimal revoked tombstone replaces it, keeping each field that still
        validates; the reader lists it as revoked. An absent document returns
        ``revoked: false``; a write failure is a request error. Either way the
        caller's long-dead documents in ``org`` are then cleaned up.
        """

        try:
            org_space_name(org)
            validate_grant_id(grant_id)
        except ValueError as error:
            raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from None
        result = self._revoke(org, grant_id)
        self._clean(org, self._now_ms())
        return result

    def _revoke(self, org: str, grant_id: str) -> dict[str, Any]:
        try:
            raw_doc = self._store.read_service_grant(org, self._owner, grant_id)
            exists = raw_doc is not None or self._store.service_grant_exists(
                org, self._owner, grant_id
            )
        except OrgFsError as error:
            raise _translate_orgfs_error(error, context="read service grant") from None

        if not exists:
            try:
                others = self._store.list_service_grants(org)
            except OrgFsError as error:
                raise _translate_orgfs_error(error, context="list service grants") from None
            if any(
                owner != self._owner and gid == grant_id
                for owner, gid, _record in others
            ):
                raise DaemonRequestError(
                    ipc_errors.SERVICE_NOT_AUTHORIZED,
                    "cannot revoke a service grant owned by another member",
                )
            return {"grantId": grant_id, "revoked": False}

        existing: ServiceGrant | None
        try:
            existing = None if raw_doc is None else ServiceGrant.from_record(raw_doc)
        except _UNPARSEABLE:
            existing = None
        if existing is not None and existing.grant_id != grant_id:
            existing = None
        if existing is not None and existing.revoked_at_ms is not None:
            return {"grantId": grant_id, "revoked": False}

        now = self._now_ms()
        revoked_grant = (
            ServiceGrant.tombstone(grant_id, raw_doc, revoked_at_ms=now)
            if existing is None
            else dataclasses.replace(existing, revoked_at_ms=now, credential=None)
        )
        try:
            self._store.put_service_grant(
                org, self._owner, grant_id, revoked_grant.as_record()
            )
        except OrgFsError as error:
            raise _translate_orgfs_error(error, context="revoke service grant") from None

        self._emit(
            "info",
            "service.grant.revoked",
            org=org,
            grantId=grant_id,
            grantor=self._owner,
            revokedAtMs=now,
            tombstone=existing is None,
        )
        return {"grantId": grant_id, "revoked": True}

    def revoke_all_for_port(self, port: int) -> dict[str, Any]:
        """Revoke every effective grant this device issued for ``port``, in every org.

        Meant for the path that stops exposing ``port`` (wired by a later
        slice). "Exposed" is the desired state in the exposure store, not
        whether a listener is up, so callers invoke this when the desired
        exposure is removed. Only grants authored by the caller whose
        ``service.deviceId`` is this device are touched. A failure in one org
        is reported in ``failed`` with its error code and does not stop the
        other orgs.
        """

        if type(port) is not int or not (1 <= port <= 65535):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "port must be an integer in 1..65535"
            )
        now = self._now_ms()
        try:
            orgs = self._store.orgs()
        except OrgFsError as error:
            raise _translate_orgfs_error(error, context="list orgs") from None
        revoked: list[dict[str, str]] = []
        failed: list[dict[str, str]] = []
        for org in orgs:
            try:
                for item in self._own_effective(org, port=port, now=now):
                    if self._revoke(org, item.grant.grant_id)["revoked"]:
                        revoked.append({"org": org, "grantId": item.grant.grant_id})
            except DaemonRequestError as error:
                failed.append({"org": org, "code": error.code})
            self._clean(org, now)
        return {"port": port, "revoked": revoked, "failed": failed}

    def _own_effective(
        self, org: str, *, port: int, now: int
    ) -> list[StoredServiceGrant]:
        try:
            loaded = load_org_service_grants(self._store, org, owner=self._owner)
        except OrgFsError as error:
            raise _translate_orgfs_error(error, context="list service grants") from None
        return [
            item
            for item in loaded
            if is_grant_effective(item.grant, now_ms=now)
            and item.grant.service is not None
            and item.grant.service.device_id == self._local_device_id
            and item.grant.service.port == port
        ]

    def grants(
        self,
        *,
        org: str | None = None,
        port: int | None = None,
        include_revoked: bool = False,
    ) -> dict[str, Any]:
        """List caller-authored and caller-received grants without decrypting or connecting."""

        try:
            if org is not None:
                org_space_name(org)
            if port is not None and (type(port) is not int or not (1 <= port <= 65535)):
                raise ValueError("port must be an integer in 1..65535")
            if type(include_revoked) is not bool:
                raise ValueError("includeRevoked must be a boolean")
        except ValueError as error:
            raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from None

        now = self._now_ms()
        try:
            orgs = [org] if org is not None else self._store.orgs()
        except OrgFsError as error:
            raise _translate_orgfs_error(error, context="list orgs") from None
        granted_items: list[StoredServiceGrant] = []
        received_items: list[StoredServiceGrant] = []
        document_count: dict[str, int] = {}
        limited: list[dict[str, Any]] = []
        unreadable: list[str] = []

        for target_org in orgs:
            try:
                own_items, other_items, org_limited = self._read_org(target_org)
                document_count[target_org] = len(
                    self._store.service_grant_ids(target_org, self._owner)
                )
            except OrgFsError as error:
                if org is not None:
                    raise _translate_orgfs_error(
                        error, context="list service grants"
                    ) from None
                unreadable.append(target_org)
                continue
            limited.extend(org_limited)

            for item in own_items:
                if port is not None and (item.grant.service is None or item.grant.service.port != port):
                    continue
                if include_revoked or is_grant_effective(item.grant, now_ms=now):
                    granted_items.append(item)

            if include_revoked:
                matched_received = [
                    item
                    for item in other_items
                    if recipient_matches(
                        item.grant,
                        bare_owner=self._owner,
                        device_id=self._local_device_id,
                    )
                ]
            else:
                matched_received = list(
                    effective_grants_for_recipient(
                        other_items,
                        owner=self._owner,
                        device_id=self._local_device_id,
                        now_ms=now,
                    )
                )
            for item in matched_received:
                if port is not None and (item.grant.service is None or item.grant.service.port != port):
                    continue
                received_items.append(item)

        granted_items.sort(key=lambda item: (item.org, item.grantor, item.grant.grant_id))
        received_items.sort(key=lambda item: (item.org, item.grantor, item.grant.grant_id))
        statuses = {} if self._status is None else self._status()
        return {
            "granted": [
                grant_view(
                    item,
                    now_ms=now,
                    effective=is_grant_effective(item.grant, now_ms=now),
                )
                for item in granted_items
            ],
            "received": [
                self._received_view(item, now, statuses)
                for item in received_items
            ],
            "documentCount": document_count,
            "documentLimit": MAX_GRANTOR_GRANT_DOCUMENTS,
            "limitedGrantors": limited,
            "unreadableOrgs": sorted(unreadable),
        }

    def _read_org(
        self, org: str
    ) -> tuple[
        tuple[StoredServiceGrant, ...], list[StoredServiceGrant], list[dict[str, Any]]
    ]:
        """The caller's grants in ``org`` read whole, and other grantors' within the scan bounds."""

        try:
            scan = scan_org_service_grants(self._store, org, ScanLimits(self._store.orgfs))
        except GrantScanLimited as stopped:
            own = load_org_service_grants(self._store, org, owner=self._owner)
            return own, [], [{"org": org, "grantor": None, "reason": stopped.reason}]
        if self._owner in scan.limited:
            own = load_org_service_grants(self._store, org, owner=self._owner)
        else:
            own = tuple(item for item in scan.grants if item.grantor == self._owner)
        others = [item for item in scan.grants if item.grantor != self._owner]
        limited = [
            {"org": org, "grantor": grantor, "reason": reason}
            for grantor, reason in sorted(scan.limited.items())
            if grantor != self._owner
        ]
        return own, others, limited

    def _emit(self, level: str, event: str, **fields: Any) -> None:
        if self._logger is not None:
            self._logger(level, "service", event, **fields)


__all__ = ["ServiceGrantService"]
