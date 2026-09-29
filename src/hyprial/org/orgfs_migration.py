"""M0/M1 org-context candidate distribution through orgfs.

The accepted slot remains :file:`$HYPRIAL_HOME/org-context.md`.  This module
only mirrors those accepted bytes into the ``context`` space
(:data:`ORG_CONTEXT_SPACE_NAME`) and stages
candidate files back through :class:`OrgContextStore`.
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from hyprial.contracts import ipc_errors
from hyprial.orgfs.api import OrgFsError, SpaceInfo
from hyprial.orgfs.runtime import OrgFsRuntime

from .document import OrgDocumentError, parse_document
from .store import OrgContextStore, OrgStoreError, OrgVersionError, PendingRecord


#: Allen 2026-09-29: the orgfs space holding org context is named ``context``.
ORG_CONTEXT_SPACE_NAME = "context"
ORG_FETCH_SOURCE_SETTINGS_KEY = "org"
ORG_FETCH_SOURCE_FIELD = "fetchSource"
OrgFetchSource = Literal["mesh", "orgfs"]


class OrgMigrationError(RuntimeError):
    """A stable org migration refusal with optional daemon response data."""

    def __init__(
        self, code: str, message: str, data: dict[str, object] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.data = dict(data or {})


def read_org_fetch_source(hyprial_home: Path) -> OrgFetchSource:
    """Read ``org.fetchSource``; M0 is the absence/default state."""

    path = Path(hyprial_home) / "settings.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "mesh"
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot parse {path}: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError(f"cannot parse {path}: top level is not an object")
    section = raw.get(ORG_FETCH_SOURCE_SETTINGS_KEY)
    if section is None:
        return "mesh"
    value = section.get(ORG_FETCH_SOURCE_FIELD) if isinstance(section, dict) else None
    if value not in {"mesh", "orgfs"}:
        raise ValueError(f"{path}: org.fetchSource must be mesh or orgfs")
    return value


def write_org_fetch_source(source: str, hyprial_home: Path) -> Path:
    """Atomically set ``org.fetchSource`` while preserving unrelated settings."""

    normalized = source.strip().lower()
    if normalized not in {"mesh", "orgfs"}:
        raise ValueError("org.fetchSource must be mesh or orgfs")
    path = Path(hyprial_home) / "settings.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot parse {path}: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError(f"cannot parse {path}: top level is not an object")
    section = raw.get(ORG_FETCH_SOURCE_SETTINGS_KEY)
    section = dict(section) if isinstance(section, dict) else {}
    section[ORG_FETCH_SOURCE_FIELD] = normalized
    raw[ORG_FETCH_SOURCE_SETTINGS_KEY] = section
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    staging.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    os.replace(staging, path)
    return path


class OrgContextOrgFsBridge:
    """Publish accepted bytes and stage orgfs candidate documents."""

    def __init__(
        self,
        hyprial_home: Path,
        runtime: OrgFsRuntime,
        *,
        logger: Callable[..., None] | None = None,
    ) -> None:
        self.home = Path(hyprial_home)
        self.runtime = runtime
        self.store = OrgContextStore(self.home)
        self.logger = logger

    def _log(self, level: str, event: str, **fields: object) -> None:
        if self.logger is not None:
            self.logger(level, event, **fields)

    def _space(self, *, required: bool) -> SpaceInfo | None:
        matches = tuple(
            info
            for info in self.runtime.facade.spaces()
            if info.name == ORG_CONTEXT_SPACE_NAME
        )
        if len(matches) == 1:
            return matches[0]
        if not required:
            return None
        detail = (
            "this node has not joined the org-context space"
            if not matches
            else "this node has multiple joined spaces named org-context"
        )
        raise OrgMigrationError(
            ipc_errors.ORG_SPACE_NOT_JOINED,
            detail,
            {"hint": "hyprial fs join <spaceId>"},
        )

    def _username(self) -> str:
        author = self.runtime.author
        username = author.removeprefix("user:")
        if (
            not author.startswith("user:")
            or not username
            or username in {".", ".."}
            or any(character in username for character in "/\r\n")
        ):
            raise OrgMigrationError(
                ipc_errors.INVALID_ARGUMENT,
                "orgfs candidate author is not a safe user URI",
            )
        return username

    @staticmethod
    def _candidate_path(username: str) -> str:
        if (
            not username
            or username in {".", ".."}
            or any(character in username for character in "/\r\n")
        ):
            raise OrgMigrationError(
                ipc_errors.INVALID_ARGUMENT,
                "candidate user must be a single path segment",
            )
        return f"candidates/{username}.md"

    def _ensure_candidates_dir(self, space_id: str) -> None:
        try:
            info = self.runtime.facade.stat(space_id, "candidates")
        except OrgFsError as error:
            if error.code != "unknown-doc":
                raise
            self.runtime.facade.mkdir(space_id, "candidates")
            return
        if info.kind != "dir":
            raise OrgFsError(
                "invalid-argument", {"message": "candidates is not a directory"}
            )

    def publish_accepted(self) -> bool:
        """M0/M1 dual-publish; failure leaves adoption and pending intact."""

        try:
            accepted = self.store.accepted_path.read_bytes()
        except FileNotFoundError:
            return False
        except OSError as error:
            self._log(
                "warn",
                "org.context.orgfs_publish_failed",
                detail=str(error),
            )
            return False
        digest = hashlib.sha256(accepted).hexdigest()
        try:
            self.store.queue_orgfs_publish(digest)
            content = accepted.decode("utf-8")
            space = self._space(required=False)
            if space is None:
                return False
            self._ensure_candidates_dir(space.space_id)
            path = self._candidate_path(self._username())
            try:
                current, version = self.runtime.facade.read_text(space.space_id, path)
            except OrgFsError as error:
                if error.code != "unknown-doc":
                    raise
                self.runtime.facade.write_text(space.space_id, path, content)
            else:
                if current.encode("utf-8") != accepted:
                    self.runtime.facade.write_text(
                        space.space_id,
                        path,
                        content,
                        base_version=version,
                    )
            self.store.clear_orgfs_publish_pending(digest)
            return True
        except (
            OrgFsError,
            OrgStoreError,
            OrgMigrationError,
            OSError,
            UnicodeError,
        ) as error:
            self._log(
                "warn",
                "org.context.orgfs_publish_failed",
                detail=str(error),
                documentSha256=digest,
            )
            return False

    def _candidate_users(self, space_id: str) -> tuple[str, ...]:
        try:
            nodes = self.runtime.facade.listdir(space_id, "candidates")
        except OrgFsError as error:
            if error.code == "unknown-doc":
                return ()
            raise
        return tuple(
            sorted(
                f"user:{node.name[:-3]}"
                for node in nodes
                if node.kind == "doc" and node.name.endswith(".md") and node.name[:-3]
            )
        )

    def _stage_candidate(self, space_id: str, source: str) -> PendingRecord | None:
        username = source.removeprefix("user:")
        path = self._candidate_path(username)
        try:
            snapshot = self.runtime.facade.read_text_snapshot(space_id, path)
        except OrgFsError as error:
            if error.code == "unknown-doc":
                return None
            raise
        document = parse_document(snapshot.content)
        accepted = self.store.load_accepted()
        if accepted is not None and document.meta.version <= accepted.meta.version:
            raise OrgVersionError(
                f"incoming version {document.meta.version} must be newer than "
                f"accepted version {accepted.meta.version}"
            )
        # The owner snapshot binds accepted bytes, version, and canonical URI;
        # a concurrent move/recreate cannot splice a replacement identity into
        # provenance after the content read.
        return self.store.stage(
            document,
            source=f"{snapshot.node.uri}@{snapshot.version}",
        )

    def fetch(self, *, source: str | None, timeout: float) -> dict[str, object]:
        """M1 refresh within the bound, then stage only local replica files."""

        space = self._space(required=True)
        assert space is not None
        refreshed = self.runtime.refresh_space(space.space_id, timeout=timeout)
        attributions = self.runtime.writer_attributions(space.space_id)
        available_nodes = tuple(sorted(attributions))
        deprecated = False
        if source is None:
            requested = self._candidate_users(space.space_id)
        elif source.startswith("user:"):
            self._candidate_path(source.removeprefix("user:"))
            requested = (source,)
        else:
            requested = tuple(sorted(attributions.get(source, ())))
            deprecated = True
            if not requested:
                raise OrgMigrationError(
                    ipc_errors.ORG_SOURCE_UNREACHABLE,
                    f"org-context source {source!r} has never published to this space",
                    {"availableSources": list(available_nodes)},
                )
        staged: list[PendingRecord] = []
        response_count = 0
        for candidate_source in requested:
            try:
                record = self._stage_candidate(space.space_id, candidate_source)
            except (
                OrgDocumentError,
                OrgStoreError,
                OrgVersionError,
                TypeError,
            ) as error:
                response_count += 1
                self._log(
                    "warn",
                    "org.context.rejected",
                    reason="orgfs-candidate-invalid",
                    source=candidate_source,
                    detail=str(error),
                )
                continue
            if record is None:
                continue
            response_count += 1
            staged.append(record)
        result: dict[str, object] = {
            "requestedSources": list(requested),
            "responseCount": response_count,
            "receivedCount": len(staged),
            "rejectedCount": response_count - len(staged),
            "candidates": [item.to_json() for item in staged],
            "source": "orgfs",
            "refreshed": refreshed,
            "spaceId": space.space_id,
        }
        if deprecated:
            result["deprecatedSourceTarget"] = True
            result["sourceTargetHint"] = "use --from user:<name>"
        return result


__all__ = [
    "ORG_CONTEXT_SPACE_NAME",
    "ORG_FETCH_SOURCE_FIELD",
    "ORG_FETCH_SOURCE_SETTINGS_KEY",
    "OrgContextOrgFsBridge",
    "OrgMigrationError",
    "read_org_fetch_source",
    "write_org_fetch_source",
]
