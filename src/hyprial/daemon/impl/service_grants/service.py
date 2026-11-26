# Decision notes (service grant S3, 2026-11-26):
# - Credential sealing reads only from <home>/secrets/providers/<entryId>.json
#   through private-file checks and seals via identity.SealedEnvelope using bare
#   owner strings. Plaintext bytes never enter logs, events, return payloads, or
#   exception messages.
# - User-provider files support both legacy flat maps and configured
#   hyprial-model-secret/v1|v2 records. When fieldName is omitted, the entry
#   must contain exactly one field; ambiguous entries fail with INVALID_ARGUMENT.
# - No new error codes are introduced; every refusal uses existing frozen codes
#   in hyprial.kernel.ipc_errors.
"""Service-grant domain service: ``service.grant``, ``service.revoke``, ``service.grants``."""

from __future__ import annotations

import base64
import json
import re
import stat
import time
from collections.abc import Callable, Mapping, Set as AbstractSet
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.identity import SealedEnvelope, org_space_name
from hyprial.kernel import (
    DaemonRequestError,
    PrivateAccessUnsupported,
    ipc_errors,
    read_private_file,
)

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
    validate_grant_id,
)
from .queries import (
    effective_grants_for_recipient,
    is_grant_effective,
    load_org_service_grants,
    recipient_matches,
)

_ENTRY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CREDENTIAL_KEYS = frozenset({"entryId", "fieldName"})
_PROVIDER_RECORD_SCHEMAS = frozenset(
    {"hyprial-model-secret/v1", "hyprial-model-secret/v2"}
)
_MAX_PROVIDER_BYTES = 1024 * 1024


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
            "org service-grants directory is not initialized; the org owner must run an upgraded daemon once",
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
    if not isinstance(entry_id, str) or _ENTRY_RE.fullmatch(entry_id) is None:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, "credential.entryId is invalid"
        )
    if field_name is not None and (
        not isinstance(field_name, str) or _ENTRY_RE.fullmatch(field_name) is None
    ):
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, "credential.fieldName is invalid"
        )
    return entry_id, field_name


def _validate_provider_ancestors(home: Path, entry_id: str) -> Path:
    root = Path(home)
    if not root.is_absolute():
        raise DaemonRequestError(
            ipc_errors.AGENT_SECRET_RESOLUTION_FAILED,
            f"provider secret unavailable for entry {entry_id!r}",
        )
    for directory in (root, root / "secrets", root / "secrets" / "providers"):
        try:
            meta = directory.lstat()
        except FileNotFoundError:
            raise DaemonRequestError(
                ipc_errors.AGENT_SECRET_RESOLUTION_FAILED,
                f"provider secret missing for entry {entry_id!r}",
            ) from None
        except OSError:
            raise DaemonRequestError(
                ipc_errors.AGENT_SECRET_RESOLUTION_FAILED,
                f"provider secret unavailable for entry {entry_id!r}",
            ) from None
        if stat.S_ISLNK(meta.st_mode) or not stat.S_ISDIR(meta.st_mode):
            raise DaemonRequestError(
                ipc_errors.AGENT_SECRET_RESOLUTION_FAILED,
                f"provider secret path is unsafe for entry {entry_id!r}",
            )
    return root / "secrets" / "providers" / f"{entry_id}.json"


def _extract_provider_values(document: object, entry_id: str) -> dict[str, str]:
    if not isinstance(document, dict):
        raise DaemonRequestError(
            ipc_errors.AGENT_SECRET_RESOLUTION_FAILED,
            f"provider secret format invalid for entry {entry_id!r}",
        )
    raw_values: object
    if "$schema" in document:
        schema = document.get("$schema")
        if schema not in _PROVIDER_RECORD_SCHEMAS or set(document) != {
            "$schema",
            "values",
            "routes",
        }:
            raise DaemonRequestError(
                ipc_errors.AGENT_SECRET_RESOLUTION_FAILED,
                f"provider secret format invalid for entry {entry_id!r}",
            )
        raw_values = document.get("values")
    else:
        raw_values = document
    if not isinstance(raw_values, dict):
        raise DaemonRequestError(
            ipc_errors.AGENT_SECRET_RESOLUTION_FAILED,
            f"provider secret format invalid for entry {entry_id!r}",
        )
    values: dict[str, str] = {}
    for key, val in raw_values.items():
        if not isinstance(key, str) or not isinstance(val, str):
            raise DaemonRequestError(
                ipc_errors.AGENT_SECRET_RESOLUTION_FAILED,
                f"provider secret format invalid for entry {entry_id!r}",
            )
        values[key] = val
    return values


def _read_user_provider_secret(
    home: Path, *, entry_id: str, field_name: str | None
) -> bytes:
    path = _validate_provider_ancestors(home, entry_id)
    try:
        data, _meta = read_private_file(path, max_bytes=_MAX_PROVIDER_BYTES)
        document = json.loads(data.decode("utf-8"))
    except FileNotFoundError:
        raise DaemonRequestError(
            ipc_errors.AGENT_SECRET_RESOLUTION_FAILED,
            f"provider secret missing for entry {entry_id!r}",
        ) from None
    except (OSError, PrivateAccessUnsupported, UnicodeError, json.JSONDecodeError):
        raise DaemonRequestError(
            ipc_errors.AGENT_SECRET_RESOLUTION_FAILED,
            f"provider secret unavailable for entry {entry_id!r}",
        ) from None
    values = _extract_provider_values(document, entry_id)
    if field_name is None:
        if len(values) != 1:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                f"credential.fieldName is required for entry {entry_id!r}",
            )
        selected_value = next(iter(values.values()))
    else:
        if field_name not in values:
            raise DaemonRequestError(
                ipc_errors.AGENT_SECRET_RESOLUTION_FAILED,
                f"provider secret field missing for entry {entry_id!r}",
            )
        selected_value = values[field_name]
    if not selected_value:
        raise DaemonRequestError(
            ipc_errors.AGENT_SECRET_RESOLUTION_FAILED,
            f"provider secret value is empty for entry {entry_id!r}",
        )
    return selected_value.encode("utf-8")


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
            cred_ref = _parse_credential_ref(credential)

        if port not in self._exposed_ports():
            raise DaemonRequestError(
                ipc_errors.SERVICE_NOT_REGISTERED,
                f"port {port} is not currently exposed on this device",
            )

        recipient_public_bytes: bytes | None = None
        if isinstance(recipient, GrantRecipient):
            try:
                devices = self._store.list_devices(org)
            except OrgFsError as error:
                raise _translate_orgfs_error(error, context="read org directory") from None
            target_row = next(
                (
                    row
                    for row in devices
                    if row.device_id == recipient.device_id
                    and normalize_bare_owner(row.owner) == recipient.owner
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

        grant_id = generate_grant_id()
        sealed_credential: dict[str, Any] | None = None
        if cred_ref is not None:
            assert isinstance(recipient, GrantRecipient)
            assert recipient_public_bytes is not None
            entry_id, field_name = cred_ref
            secret_bytes = _read_user_provider_secret(
                self._home, entry_id=entry_id, field_name=field_name
            )
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
            granted_at_ms=now,
            expires_at_ms=expires_at_ms,
            revoked_at_ms=None,
            credential=sealed_credential,
        )
        try:
            self._store.put_service_grant(org, self._owner, grant_id, grant.as_record())
        except OrgFsError as error:
            raise _translate_orgfs_error(error, context="write service grant") from None

        stored = StoredServiceGrant(org=org, grantor=self._owner, grant=grant)
        self._emit(
            "info",
            "service.grant.created",
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
        return {"grant": grant_view(stored, now_ms=now, effective=is_grant_effective(grant, now_ms=now))}

    def revoke(self, *, org: str, grant_id: str) -> dict[str, Any]:
        """Revoke one of the caller's grants by setting ``revokedAtMs`` and clearing ``credential``."""

        try:
            org_space_name(org)
            validate_grant_id(grant_id)
        except ValueError as error:
            raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from None

        try:
            raw_doc = self._store.read_service_grant(org, self._owner, grant_id)
        except OrgFsError as error:
            raise _translate_orgfs_error(error, context="read service grant") from None

        if raw_doc is None:
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

        try:
            existing = ServiceGrant.from_record(raw_doc)
        except ValueError:
            raise DaemonRequestError(
                ipc_errors.SERVICE_REGISTRY_INVALID,
                "stored service grant document is invalid",
            ) from None

        if existing.revoked_at_ms is not None:
            return {"grantId": grant_id, "revoked": False}

        now = self._now_ms()
        revoked_grant = ServiceGrant(
            schema_version=existing.schema_version,
            grant_id=existing.grant_id,
            service=existing.service,
            recipient=existing.recipient,
            granted_at_ms=existing.granted_at_ms,
            expires_at_ms=existing.expires_at_ms,
            revoked_at_ms=now,
            credential=None,
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
        )
        return {"grantId": grant_id, "revoked": True}

    def grants(
        self,
        *,
        org: str | None = None,
        port: int | None = None,
        include_revoked: bool = False,
    ) -> dict[str, list[dict[str, Any]]]:
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
        orgs = [org] if org is not None else self._store.orgs()
        granted_items: list[StoredServiceGrant] = []
        received_items: list[StoredServiceGrant] = []

        for target_org in orgs:
            try:
                loaded = load_org_service_grants(self._store, target_org)
            except OrgFsError as error:
                if org is not None:
                    raise _translate_orgfs_error(
                        error, context="list service grants"
                    ) from None
                continue
            own_items = [item for item in loaded if item.grantor == self._owner]
            other_items = [item for item in loaded if item.grantor != self._owner]

            for item in own_items:
                if port is not None and item.grant.service.port != port:
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
                if port is not None and item.grant.service.port != port:
                    continue
                received_items.append(item)

        granted_items.sort(key=lambda item: (item.org, item.grantor, item.grant.grant_id))
        received_items.sort(key=lambda item: (item.org, item.grantor, item.grant.grant_id))
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
                grant_view(
                    item,
                    now_ms=now,
                    effective=is_grant_effective(item.grant, now_ms=now),
                )
                for item in received_items
            ],
        }

    def _emit(self, level: str, event: str, **fields: Any) -> None:
        if self._logger is not None:
            self._logger(level, "service", event, **fields)


__all__ = ["ServiceGrantService"]
