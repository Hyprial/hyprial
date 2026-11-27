# Decision notes (service grant integration fix, 2026-11-27):
# - Service-grant documents live at directory/service-grants/<owner>/<grantId>.json
#   in the org's directory space, a protected section whose author must be the
#   path owner. Their storage methods are one group, moved here whole out of
#   the directory store module, which had no room left under the line limit;
#   the store inherits them, so every caller keeps calling the store.
# - The group relies on four things the store owns: the facade (_fs), the
#   space lookup (_space_id, _space_owner), owner-scoped directory creation
#   (_mkdir_if_absent) and the doc-id mismatch warning. It defines none of them.
# - remove_service_grant deletes one of the caller's own documents; OrgFS's
#   protected-section rule is what refuses anyone else. An absent document is
#   False, not an error, so a cleanup that races another cleanup is harmless.
# - service_grant_ids lists the names of one owner's documents from one
#   listing, without reading any of them: it is what a document count costs.
"""Owner-scoped service-grant documents in an org directory space."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from hyprial.daemon.impl.orgfs.api import OrgFs, OrgFsError
from hyprial.identity import directory_owner_principal, protected_directory_doc_id
from hyprial.kernel import parse_user_uri

SERVICE_GRANTS_DIR = "directory/service-grants"
SERVICE_GRANT_MAX_BYTES = 64 * 1024


class ServiceGrantDocuments:
    """The service-grant document methods of the OrgFS directory store."""

    _fs: OrgFs

    def put_service_grant(
        self, org: str, owner: str, grant_id: str, record: Mapping[str, Any]
    ) -> None:
        """Write one owner-scoped service grant under ``directory/service-grants/<owner>/``."""

        bare, path = self._service_grant_path(owner, grant_id)
        space_id = self._space_id(org)
        self._mkdir_if_absent(space_id, f"{SERVICE_GRANTS_DIR}/{bare}")
        payload = json.dumps(dict(record), sort_keys=True, separators=(",", ":"))
        self._fs.write_text(space_id, path, payload)

    def remove_service_grant(self, org: str, owner: str, grant_id: str) -> bool:
        """Delete one owner-scoped service grant; ``False`` when it was already absent."""

        _bare, path = self._service_grant_path(owner, grant_id)
        try:
            self._fs.remove(self._space_id(org), path)
        except OrgFsError as error:
            if error.code == "unknown-doc":
                return False
            raise
        return True

    def service_grant_ids(self, org: str, owner: str) -> list[str]:
        """The grant ids of every document in ``owner``'s grants directory, none of them read."""

        bare = self._bare_owner_segment(owner)
        try:
            entries = self._fs.listdir(self._space_id(org), f"{SERVICE_GRANTS_DIR}/{bare}")
        except OrgFsError as error:
            if error.code == "unknown-doc":
                return []
            raise
        return sorted(
            entry.name.removesuffix(".json")
            for entry in entries
            if entry.kind == "doc" and entry.name.endswith(".json")
        )

    def read_service_grant(self, org: str, owner: str, grant_id: str) -> dict[str, Any] | None:
        """Read one verified service-grant document, or ``None`` when absent."""

        bare, path = self._service_grant_path(owner, grant_id)
        space_id = self._space_id(org)
        try:
            entry = self._fs.stat(space_id, path)
        except OrgFsError as error:
            if error.code == "unknown-doc":
                return None
            raise
        if entry.kind != "doc":
            return None
        return self._read_verified_service_grant(
            space_id, path, entry, directory_owner_principal(bare)
        )

    def service_grant_exists(self, org: str, owner: str, grant_id: str) -> bool:
        """Return whether a document exists at the owner's grant path, readable or not."""

        try:
            return self._fs.stat(self._space_id(org), self._service_grant_path(owner, grant_id)[1]).kind == "doc"
        except OrgFsError as error:
            if error.code == "unknown-doc":
                return False
            raise

    def list_service_grants(
        self, org: str, *, owner: str | None = None
    ) -> list[tuple[str, str, dict[str, Any]]]:
        """List verified ``(bare_owner, grant_id, record)`` tuples for one org."""

        space_id = self._space_id(org)
        if owner is not None:
            bare = self._bare_owner_segment(owner)
            targets = [(bare, f"{SERVICE_GRANTS_DIR}/{bare}")]
        else:
            try:
                owner_entries = self._fs.listdir(space_id, SERVICE_GRANTS_DIR)
            except OrgFsError as error:
                if error.code == "unknown-doc":
                    return []
                raise
            targets = []
            for item in owner_entries:
                if item.kind == "dir":
                    try:
                        if self._bare_owner_segment(item.name) == item.name:
                            targets.append((item.name, f"id:{item.node_id}"))
                    except ValueError:
                        continue
        results: list[tuple[str, str, dict[str, Any]]] = []
        for bare, ref in targets:
            try:
                principal = directory_owner_principal(bare)
                docs = self._fs.listdir(space_id, ref)
            except OrgFsError as error:
                if error.code == "unknown-doc":
                    continue
                raise
            except ValueError:
                continue
            for entry in docs:
                if entry.kind != "doc" or not entry.name.endswith(".json"):
                    continue
                path = f"{SERVICE_GRANTS_DIR}/{bare}/{entry.name}"
                rec = self._read_verified_service_grant(space_id, path, entry, principal)
                if rec is not None:
                    results.append((bare, entry.name.removesuffix(".json"), rec))
        return sorted(results, key=lambda item: (item[0], item[1]))

    @staticmethod
    def _bare_owner_segment(owner: str) -> str:
        bare = parse_user_uri(directory_owner_principal(owner))
        if not bare or bare in {".", ".."} or any(ch in bare for ch in ("/", "\\", ":")):
            raise ValueError("service grant owner is not a safe path segment")
        return bare

    @classmethod
    def _service_grant_path(cls, owner: str, grant_id: str) -> tuple[str, str]:
        bare = cls._bare_owner_segment(owner)
        if not isinstance(grant_id, str) or not grant_id or grant_id in {".", ".."} or any(
            ch in grant_id for ch in ("/", "\\", ":")
        ):
            raise ValueError("service grant id is not a safe path segment")
        return bare, f"{SERVICE_GRANTS_DIR}/{bare}/{grant_id}.json"

    def _read_verified_service_grant(
        self, space_id: str, path: str, entry: Any, path_principal: str
    ) -> dict[str, Any] | None:
        node_id, doc_id = entry.node_id, entry.doc_id
        if entry.size is None or entry.size > SERVICE_GRANT_MAX_BYTES:
            return None
        try:
            expected = protected_directory_doc_id(
                space_id, path, space_owner=self._space_owner(space_id)
            )
            if doc_id != expected:
                self._warn_doc_id_mismatch(path, expected=expected, actual=doc_id)
                return None
            text, _version = self._fs.read_text(space_id, f"id:{node_id}")
            if len(text.encode("utf-8", "surrogatepass")) > SERVICE_GRANT_MAX_BYTES:
                return None
            record = json.loads(text)
            if not isinstance(record, dict):
                return None
            authors = {
                directory_owner_principal(author)
                for author in self._fs.document_authors(space_id, f"id:{node_id}")
            }
        except (OrgFsError, TypeError, ValueError, RecursionError):
            return None
        return record if authors == {path_principal} else None


__all__ = ["SERVICE_GRANTS_DIR", "SERVICE_GRANT_MAX_BYTES", "ServiceGrantDocuments"]
