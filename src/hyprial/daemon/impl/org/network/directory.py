"""The OrgFS-backed ``DirectoryStore``: org directories as OrgFS documents.

Each organization occupies one OrgFS directory space named ``group-NAME``
(:func:`hyprial.identity.impl.org_directory.org_space_name`); the
directory itself is plain documents under ``directory/devices/<owner>/``, one
per device, plus pending invites under ``directory/invites/``.  The semantics
(the record shapes, the naming rules) are identity's; this module only
decides *where* the bytes live, which is the daemon's half of the §4.1
port split.

Device ownership is only partial until G4 signs ``origin.author``; admission
assumes that field is genuine.  Protected documents use deterministic ids
derived from ``(spaceId, section, path user, leaf)``.  Readers require that id
at the protected path, so an unlinked or arbitrarily linked document is never
adopted and arrival order cannot change authorization.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from hyprial.daemon.impl.orgfs.api import OrgFs, OrgFsError
from hyprial.identity import (
    DIRECTORY_DIR,
    INVITES_DIR,
    LEAVES_DIR,
    PEOPLE_DIR,
    ORG_META_DOC,
    DirectoryDevice,
    binding_assertion_publish_after,
    directory_device_path,
    directory_binding_path,
    directory_owner_principal,
    org_from_space_name,
    org_space_name,
    protected_directory_doc_id,
)
from hyprial.kernel import ORGFS_CONTENT_PENDING, canonical_user_uri

_LOG = logging.getLogger(__name__)
_OWNER_CREATED_DIRS = (
    "directory",
    DIRECTORY_DIR,
    PEOPLE_DIR,
    INVITES_DIR,
    LEAVES_DIR,
)


class OrgFsDirectoryStore:
    """:class:`~hyprial.identity.impl.org_directory.DirectoryStore` over OrgFS.

    ``fs`` is the OrgFS facade (``OrgFsRuntime.facade``).  Every method
    addresses the org's directory space by its complete ``group-NAME`` through
    ``fs.spaces()``, so a missing org surfaces as the orgfs
    ``unknown-space`` error rather than a silent empty answer.
    """

    def __init__(
        self, fs: OrgFs, *, logger: Callable[..., None] | None = None
    ) -> None:
        self._fs = fs
        self._logger = logger

    # -- DirectoryStore ---------------------------------------------------

    def list_devices(self, org: str) -> list[DirectoryDevice]:
        return self._scan(org)[0]

    def scan_devices(
        self, org: str
    ) -> tuple[list[DirectoryDevice], dict[str, list[dict[str, Any]]]]:
        """Readable device rows, plus what has not arrived yet ("not caught up").

        ``contentPending`` names devices whose tree node is here but whose
        content is not; ``parkedCommits`` counts, per author, commits held
        behind a missing predecessor (such a device may have no node at all).
        A reader must be able to tell either apart from a device that does
        not exist.
        """
        devices, content_pending = self._scan(org)
        try:
            parked: list[dict[str, Any]] | None = self.parked_commits(org)
        except Exception as error:  # noqa: BLE001 - must not hide the device listing
            parked = None  # unknown, never "nothing parked"
            fields = {"org": org, "detail": str(error)}
            if self._logger is not None:
                self._logger("warn", "org", "directory.parked_unavailable", **fields)
            else:
                _LOG.warning("org.directory.parked_unavailable org=%s detail=%s", org, error)
        return devices, {"contentPending": content_pending, "parkedCommits": parked}

    def _scan(
        self, org: str
    ) -> tuple[list[DirectoryDevice], list[dict[str, str]]]:
        space_id = self._space_id(org)
        try:
            entries = self._fs.listdir(space_id, DIRECTORY_DIR)
        except OrgFsError as error:
            if error.code == "unknown-doc":
                # An org whose directory has no device documents yet (only
                # just created, or nobody published): empty, not an error.
                return [], []
            raise
        candidates: list[DirectoryDevice] = []
        pending: list[dict[str, str]] = []
        for owner_entry in entries:
            if owner_entry.kind != "dir":
                # Pre-release rule: the former flat layout is not read.
                continue
            owner_path = f"{DIRECTORY_DIR}/{owner_entry.name}"
            try:
                path_principal = directory_owner_principal(owner_entry.name)
                owner_devices = self._fs.listdir(space_id, f"id:{owner_entry.node_id}")
            except (OrgFsError, ValueError):
                continue
            for entry in owner_devices:
                if entry.kind != "doc" or not entry.name.endswith(".json"):
                    continue
                path = f"{owner_path}/{entry.name}"
                try:
                    expected_doc_id = protected_directory_doc_id(
                        space_id,
                        path,
                        space_owner=self._space_owner(space_id),
                    )
                    if entry.doc_id != expected_doc_id:
                        self._warn_doc_id_mismatch(
                            path,
                            expected=expected_doc_id,
                            actual=entry.doc_id,
                        )
                        continue
                    text, _version = self._fs.read_text(
                        space_id, f"id:{entry.node_id}"
                    )
                    device = DirectoryDevice.from_record(json.loads(text))
                    record_principal = directory_owner_principal(device.owner)
                    authors = {
                        directory_owner_principal(author)
                        for author in self._fs.document_authors(
                            space_id, f"id:{entry.node_id}"
                        )
                    }
                except (OrgFsError, TypeError, ValueError) as error:
                    self._warn_invalid_device(path, error)
                    if (
                        isinstance(error, OrgFsError)
                        and error.code == ORGFS_CONTENT_PENDING
                    ):
                        pending.append({
                            "owner": path_principal,
                            "deviceId": entry.name.removesuffix(".json"),
                            "reason": ORGFS_CONTENT_PENDING,
                        })
                    continue
                expected_name = f"{device.device_id}.json"
                if (
                    record_principal != path_principal
                    or authors != {path_principal}
                    or entry.name != expected_name
                ):
                    self._warn_owner_mismatch(
                        path,
                        path_principal=path_principal,
                        record_principal=record_principal,
                        authors=authors,
                    )
                    continue
                candidates.append(device)
        conflicts = self._device_key_conflicts(candidates)
        for key, (fields, owners) in sorted(conflicts.items()):
            self._warn_key_conflict(key, fields=fields, owners=owners)
        devices = [
            device
            for device in candidates
            if device.server_public not in conflicts
            and device.client_public not in conflicts
        ]
        devices.sort(key=lambda device: (device.device_id, device.owner))
        pending.sort(key=lambda row: (row["deviceId"], row["owner"]))
        return devices, pending

    def parked_commits(self, org: str) -> list[dict[str, Any]]:
        """Commits received for ``org``'s directory but parked behind a gap."""
        status = self._fs.status(self._space_id(org))
        return [
            # Rows parked before the author column existed carry ''.
            {"author": author or "unknown", "commits": count}
            for author, count in status.parked_commits
        ]

    def put_device(self, org: str, device: DirectoryDevice) -> None:
        space_id = self._space_id(org)
        path = directory_device_path(device.owner, device.device_id)
        # Shared roots are created by the org owner during create.  Enrollment
        # creates only the caller's owner-scoped leaf directory.
        self._mkdir_if_absent(space_id, path.rsplit("/", 1)[0])
        self._fs.write_text(
            space_id,
            path,
            json.dumps(device.as_record(), sort_keys=True, separators=(",", ":")),
        )

    def put_binding(self, org: str, record: dict[str, Any]) -> None:
        """Publish one exact four-field binding row under its stable user key."""

        if set(record) != {"user", "larkUnionId", "proof", "publishedAt"}:
            raise ValueError("binding row must have exactly the frozen four fields")
        user = record.get("user")
        if not isinstance(user, str) or not user:
            raise ValueError("binding row user must be a non-empty string")
        proof = record.get("proof")
        if not isinstance(proof, str):
            raise ValueError("binding row proof must be a string")
        if time.time() <= binding_assertion_publish_after(proof):
            raise ValueError("binding proof is not expired past the skew budget")
        path = directory_binding_path(user)
        space_id = self._space_id(org)
        self._mkdir_if_absent(space_id, path.rsplit("/", 1)[0])
        self._fs.write_text(
            space_id,
            path,
            json.dumps(record, sort_keys=True, separators=(",", ":")),
        )

    def remove_binding(self, org: str, user: str) -> bool:
        """Delete the caller's stable binding row; absent is idempotent."""

        space_id = self._space_id(org)
        try:
            self._fs.remove(space_id, directory_binding_path(user))
        except OrgFsError as error:
            if error.code == "unknown-doc":
                return False
            raise
        return True

    def list_binding_rows(self, org: str) -> list[tuple[str, dict[str, Any]]]:
        """Read author-checked candidate rows; proof verification is separate."""

        space_id = self._space_id(org)
        try:
            users = self._fs.listdir(space_id, PEOPLE_DIR)
        except OrgFsError as error:
            if error.code == "unknown-doc":
                return []
            raise
        rows: list[tuple[str, dict[str, Any]]] = []
        for user_entry in users:
            if user_entry.kind != "dir":
                continue
            path = f"{PEOPLE_DIR}/{user_entry.name}/binding.json"
            try:
                node = self._fs.stat(space_id, path)
                expected = protected_directory_doc_id(
                    space_id, path, space_owner=self._space_owner(space_id)
                )
                if node.kind != "doc" or node.doc_id != expected:
                    raise ValueError("binding row has the wrong protected document id")
                text, _version = self._fs.read_text(space_id, f"id:{node.node_id}")
                record = json.loads(text)
                if not isinstance(record, dict):
                    raise ValueError("binding row must be a JSON object")
                user = record.get("user")
                if not isinstance(user, str) or not user:
                    raise ValueError("binding row has no user")
                authors = {
                    directory_owner_principal(author)
                    for author in self._fs.document_authors(
                        space_id, f"id:{node.node_id}"
                    )
                }
                if authors != {directory_owner_principal(user)}:
                    raise ValueError("binding row author does not equal its user")
            except (OrgFsError, TypeError, ValueError) as error:
                self._warn_invalid_binding(path, error)
                continue
            rows.append((user_entry.name, record))
        return rows

    def is_member(self, org: str, user: str) -> bool:
        """Return whether the exact user principal is a current org member."""

        principal = directory_owner_principal(user)
        space_id = self._space_id(org)
        return principal == directory_owner_principal(self._space_owner(space_id)) or any(
            directory_owner_principal(member.user) == principal
            for member in self._fs.members(space_id)
        )

    def ensure_org_directory_roots(self, org: str) -> None:
        """Create shared directory roots while the facade is the space owner."""

        space_id = self._space_id(org)
        for path in _OWNER_CREATED_DIRS:
            self._mkdir_if_absent(space_id, path)

    def orgs(self) -> list[str]:
        orgs = [
            org
            for org in (
                org_from_space_name(space.name) for space in self._fs.spaces()
            )
            if org is not None
        ]
        return sorted(set(orgs))

    def binding_watch_targets(self) -> list[tuple[str, str]]:
        """Current org names and spaces for binding-row/meta projections."""

        return [
            (org, self._space_id(org))
            for org in self.orgs()
        ]

    # -- invites (OrgFS-backed, used by OrgNetworkService) -----------------

    def put_invite(self, org: str, token_id: str, record: dict[str, Any]) -> None:
        """Register one pending invite document (``directory/invites``)."""

        space_id = self._space_id(org)
        self._ensure_directory(space_id, INVITES_DIR)
        self._fs.write_text(
            space_id,
            f"{INVITES_DIR}/{token_id}.json",
            json.dumps(record, sort_keys=True, separators=(",", ":")),
        )

    # -- membership lifecycle (org leave/remove/delete; task D) -------------

    def remove_device(
        self, org: str, device_id: str, *, owner: str | None = None
    ) -> bool:
        """Delete one device document; ``False`` when it was already absent."""

        space_id = self._space_id(org)
        if owner is None:
            owner = next(
                (
                    device.owner
                    for device in self.list_devices(org)
                    if device.device_id == device_id
                ),
                None,
            )
        if owner is None:
            return False
        try:
            self._fs.remove(space_id, directory_device_path(owner, device_id))
        except OrgFsError as error:
            if error.code == "unknown-doc":
                return False
            raise
        return True

    def put_leave_request(self, org: str, user: str, record: dict[str, Any]) -> None:
        """Write one leave request (``directory/leaves/<user>.json``)."""

        space_id = self._space_id(org)
        self._ensure_directory(space_id, LEAVES_DIR)
        self._fs.write_text(
            space_id,
            f"{LEAVES_DIR}/{user}.json",
            json.dumps(record, sort_keys=True, separators=(",", ":")),
        )

    def list_leave_requests(self, org: str) -> dict[str, dict[str, Any]]:
        """Every pending leave request: user (from the filename) → record."""

        space_id = self._space_id(org)
        try:
            entries = self._fs.listdir(space_id, LEAVES_DIR)
        except OrgFsError as error:
            if error.code == "unknown-doc":
                return {}
            raise
        requests: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if entry.kind != "doc" or not entry.name.endswith(".json"):
                continue
            text, _version = self._fs.read_text(space_id, f"id:{entry.node_id}")
            record = json.loads(text)
            if not isinstance(record, dict):
                continue
            user = entry.name.removesuffix(".json")
            try:
                expected_doc_id = protected_directory_doc_id(
                    space_id,
                    f"{LEAVES_DIR}/{entry.name}",
                    space_owner=self._space_owner(space_id),
                )
            except ValueError:
                continue
            if entry.doc_id != expected_doc_id:
                self._warn_doc_id_mismatch(
                    f"{LEAVES_DIR}/{entry.name}",
                    expected=expected_doc_id,
                    actual=entry.doc_id,
                )
                continue
            requests[user] = record
        return requests

    def leave_request_authors(self, org: str, user: str) -> tuple[str, ...]:
        """Return durable authors for one leave request used by reconciliation."""

        space_id = self._space_id(org)
        return self._fs.document_authors(space_id, f"{LEAVES_DIR}/{user}.json")

    def leave_request_covers_current_add(self, org: str, user: str) -> bool:
        """Return whether the request causally follows the member's latest add."""

        space_id = self._space_id(org)
        return self._fs.document_covers_member_add(
            space_id, f"{LEAVES_DIR}/{user}.json", directory_owner_principal(user)
        )

    def remove_leave_request(self, org: str, user: str) -> bool:
        """Delete one leave request; ``False`` when it was already absent."""

        space_id = self._space_id(org)
        try:
            self._fs.remove(space_id, f"{LEAVES_DIR}/{user}.json")
        except OrgFsError as error:
            if error.code == "unknown-doc":
                return False
            raise
        return True

    def put_org_meta(self, org: str, record: dict[str, Any]) -> None:
        """Write the org lifecycle marker (``directory/org.json``)."""

        space_id = self._space_id(org)
        self._ensure_directory(space_id, "directory")
        try:
            text, _version = self._fs.read_text(space_id, ORG_META_DOC)
            current = json.loads(text)
        except OrgFsError as error:
            if error.code != "unknown-doc":
                raise
            current = {}
        except ValueError as error:
            raise OrgFsError(
                "invalid-argument", {"message": "org metadata is not valid JSON"}
            ) from error
        if not isinstance(current, dict):
            raise OrgFsError(
                "invalid-argument", {"message": "org metadata must be a JSON object"}
            )
        record = {**current, **record}
        self._fs.write_text(
            space_id,
            ORG_META_DOC,
            json.dumps(record, sort_keys=True, separators=(",", ":")),
        )

    def org_state(self, org: str) -> str:
        """The org's lifecycle state; ``"active"`` when no marker exists."""

        space_id = self._space_id(org)
        try:
            text, _version = self._fs.read_text(space_id, ORG_META_DOC)
        except OrgFsError as error:
            if error.code == "unknown-doc":
                return "active"
            raise
        try:
            record = json.loads(text)
        except ValueError:
            return "active"
        state = record.get("state") if isinstance(record, dict) else None
        return state if isinstance(state, str) and state else "active"

    # -- internals ----------------------------------------------------------

    def _space_id(self, org: str) -> str:
        name = org_space_name(org)
        matches = [space for space in self._fs.spaces() if space.name == name]
        if len(matches) == 1:
            return matches[0].space_id
        if len(matches) > 1:
            raise OrgFsError(
                "ambiguous-space", {"org": org, "space": name, "count": len(matches)}
            )
        raise OrgFsError("unknown-space", {"org": org, "space": name})

    def _space_owner(self, space_id: str) -> str:
        return next(space.owner for space in self._fs.spaces() if space.space_id == space_id)

    def _ensure_directory(self, space_id: str, path: str) -> None:
        """Create every directory component in order when absent."""

        parts = path.split("/")
        for index in range(1, len(parts) + 1):
            self._mkdir_if_absent(space_id, "/".join(parts[:index]))

    def _mkdir_if_absent(self, space_id: str, path: str) -> None:
        try:
            self._fs.mkdir(space_id, path)
        except OrgFsError as error:
            if error.code != "invalid-argument" or "already exists" not in str(error):
                raise
            # "node already exists" is the happy path here.

    def _warn_owner_mismatch(
        self,
        path: str,
        *,
        path_principal: str,
        record_principal: str,
        authors: set[str],
    ) -> None:
        fields = {
            "path": path,
            "pathPrincipal": path_principal,
            "recordPrincipal": record_principal,
            "docAuthors": sorted(authors),
        }
        if self._logger is not None:
            self._logger("warn", "org", "directory.device_owner_mismatch", **fields)
            return
        _LOG.warning(
            "org.directory.device_owner_mismatch path=%s path_principal=%s "
            "record_principal=%s doc_authors=%s",
            path,
            path_principal,
            record_principal,
            sorted(authors),
        )

    def _warn_doc_id_mismatch(
        self, path: str, *, expected: str | None, actual: str | None
    ) -> None:
        fields = {"path": path, "expectedDocId": expected, "actualDocId": actual}
        if self._logger is not None:
            self._logger("warn", "org", "directory.protected_doc_id_mismatch", **fields)
            return
        _LOG.warning(
            "org.directory.protected_doc_id_mismatch path=%s expected=%s actual=%s",
            path,
            expected,
            actual,
        )

    def _warn_invalid_device(self, path: str, error: Exception) -> None:
        fields = {"path": path, "detail": str(error)}
        if self._logger is not None:
            self._logger("warn", "org", "directory.device_invalid", **fields)
            return
        _LOG.warning("org.directory.device_invalid path=%s detail=%s", path, error)

    def _warn_invalid_binding(self, path: str, error: Exception) -> None:
        fields = {"path": path, "detail": str(error)}
        if self._logger is not None:
            self._logger("warn", "org", "directory.binding_invalid", **fields)
            return
        _LOG.warning("org.directory.binding_invalid path=%s detail=%s", path, error)

    @staticmethod
    def _device_key_conflicts(
        devices: list[DirectoryDevice],
    ) -> dict[str, tuple[set[str], set[str]]]:
        uses: dict[str, tuple[set[str], set[str]]] = {}
        for device in devices:
            owner = directory_owner_principal(device.owner)
            for field, key in (
                ("serverPublic", device.server_public),
                ("clientPublic", device.client_public),
            ):
                fields, owners = uses.setdefault(key, (set(), set()))
                fields.add(field)
                owners.add(owner)
        return {
            key: (fields, owners)
            for key, (fields, owners) in uses.items()
            if len(owners) > 1
        }

    def _warn_key_conflict(
        self, key: str, *, fields: set[str], owners: set[str]
    ) -> None:
        values = {
            "key": key,
            "keyFields": sorted(fields),
            "owners": sorted(owners),
        }
        if self._logger is not None:
            self._logger("warn", "org", "directory.device_key_conflict", **values)
            return
        _LOG.warning(
            "org.directory.device_key_conflict key=%s key_fields=%s owners=%s",
            key,
            sorted(fields),
            sorted(owners),
        )


__all__ = ["OrgFsDirectoryStore"]


def leave_request_admitted(
    store: OrgFsDirectoryStore,
    log: Callable[..., Any],
    org: str,
    user: str,
) -> bool:
    """Whether ``user``'s leave request in ``org`` may be fulfilled now.

    The request is member-written: the owner's sweep fulfils it only when its
    name is a user, its OrgFS authors are exactly that user, and it was written
    after the member's current add.  Anything else is logged and skipped (an
    unparseable name is also removed), never fatal to the sweep.
    """

    # The filename is member-written: a name that cannot be a user
    # is dropped with a warning instead of stopping the sweep.
    try:
        canonical_user_uri(user.removeprefix("user:"))
    except ValueError as error:
        log(
            "warn",
            "org",
            "org.leaves.invalid-request",
            org=org,
            user=user,
            detail=str(error),
        )
        try:
            store.remove_leave_request(org, user)
        except OrgFsError as remove_error:
            log(
                "warn",
                "org",
                "org.leaves.invalid-request-not-removed",
                org=org,
                user=user,
                detail=str(remove_error),
            )
        return False
    try:
        path_principal = directory_owner_principal(user)
        authors = {
            directory_owner_principal(author)
            for author in store.leave_request_authors(org, user)
        }
    except (OrgFsError, ValueError) as error:
        log(
            "warn",
            "org",
            "org.leaves.author-unreadable",
            org=org,
            user=user,
            detail=str(error),
        )
        return False
    if authors != {path_principal}:
        log(
            "warn",
            "org",
            "org.leaves.author-mismatch",
            org=org,
            user=user,
            authors=sorted(authors),
        )
        return False
    if not store.leave_request_covers_current_add(org, user):
        log(
            "info",
            "org",
            "org.leaves.stale",
            org=org,
            user=user,
        )
        return False
    return True
