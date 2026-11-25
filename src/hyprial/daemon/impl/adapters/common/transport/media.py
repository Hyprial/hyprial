"""Durable SQLite authority for sealed provider-native media bytes."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.adapters.contracts import (
    FrozenJsonMap,
    FrozenJsonV1,
    SealedProviderMediaRef,
    canonical_json_v1,
    freeze_json_v1,
)

from .types import NativeMediaScope, SealedNativeMedia


SCHEMA_VERSION = 1
_FORBIDDEN_KEY = re.compile(
    r"(?:url|uri|path|secret|token|credential|cookie|authorization)", re.IGNORECASE
)
_FORBIDDEN_VALUE = re.compile(r"^(?:https?://|file://|/|~[/\\])", re.IGNORECASE)


class NativeMediaDenied(ValueError):
    pass


class _SealedByteStream:
    def __init__(self, body: bytes, metadata: SealedNativeMedia) -> None:
        self._body = body
        self._offset = 0
        self._closed = False
        self.name = metadata.name
        self.media_type = metadata.media_type
        self.size = metadata.size
        self.sha256 = metadata.sha256

    async def read(self, max_bytes: int) -> bytes:
        if self._closed:
            raise NativeMediaDenied("NATIVE_MEDIA_STREAM_CLOSED")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        if self._offset >= len(self._body):
            return b""
        end = min(len(self._body), self._offset + max_bytes)
        chunk = self._body[self._offset : end]
        self._offset = end
        return chunk

    async def aclose(self) -> None:
        self._closed = True


def _json_ready(value: FrozenJsonV1) -> Any:
    if isinstance(value, FrozenJsonMap):
        return {key: _json_ready(nested) for key, nested in value}
    if isinstance(value, tuple):
        return [_json_ready(nested) for nested in value]
    return value


def _validate_native_metadata(value: object) -> FrozenJsonMap:
    frozen = freeze_json_v1(value)
    if not isinstance(frozen, FrozenJsonMap):
        raise TypeError("native_metadata must be a JSON object")

    def visit(item: FrozenJsonV1) -> None:
        if isinstance(item, FrozenJsonMap):
            for key, nested in item:
                if _FORBIDDEN_KEY.search(key):
                    raise NativeMediaDenied("NATIVE_MEDIA_METADATA_FORBIDDEN")
                visit(nested)
        elif isinstance(item, tuple):
            for nested in item:
                visit(nested)
        elif isinstance(item, str) and _FORBIDDEN_VALUE.search(item):
            raise NativeMediaDenied("NATIVE_MEDIA_METADATA_FORBIDDEN")

    visit(frozen)
    return frozen


class SealedNativeMediaAuthority:
    """A byte authority, not a delivery ledger or provider URL cache."""

    def __init__(self, path: Path, *, max_blob_bytes: int) -> None:
        if not isinstance(path, Path):
            raise TypeError("path must be pathlib.Path")
        if isinstance(max_blob_bytes, bool) or max_blob_bytes < 1:
            raise ValueError("max_blob_bytes must be positive")
        self._max_blob_bytes = max_blob_bytes
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path)
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS common_transport_schema (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    version INTEGER NOT NULL
                )
                """
            )
            row = self._connection.execute(
                "SELECT version FROM common_transport_schema WHERE singleton=1"
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO common_transport_schema(singleton,version) VALUES(1,?)",
                    (SCHEMA_VERSION,),
                )
            elif row != (SCHEMA_VERSION,):
                raise RuntimeError("unsupported common transport schema version")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sealed_native_media (
                    handle_id TEXT PRIMARY KEY,
                    owner_principal TEXT NOT NULL,
                    organization_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    instance_id TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK(generation > 0),
                    target_ref_id TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    name TEXT,
                    media_type TEXT,
                    size INTEGER NOT NULL CHECK(size >= 0),
                    sha256 TEXT NOT NULL,
                    native_metadata_json BLOB NOT NULL,
                    opaque_ref_digest TEXT NOT NULL,
                    body BLOB NOT NULL
                )
                """
            )
            self._connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def _optional_text(value: str | None, name: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be a non-empty string or None")
        if len(value.encode("utf-8")) > 4096:
            raise ValueError(f"{name} exceeds 4096 UTF-8 bytes")
        return value

    @staticmethod
    def _opaque_digest(
        scope: NativeMediaScope,
        *,
        name: str | None,
        media_type: str | None,
        size: int,
        sha256: str,
        native_metadata: FrozenJsonMap,
    ) -> str:
        envelope = {
            "ownerPrincipal": scope.owner_principal,
            "organizationId": scope.organization_id,
            "provider": scope.provider,
            "instanceId": scope.instance_id,
            "generation": scope.generation,
            "targetRefId": scope.target_ref_id,
            "contentDigest": scope.content_digest,
            "name": name,
            "mediaType": media_type,
            "size": size,
            "sha256": sha256,
            "nativeMetadata": _json_ready(native_metadata),
        }
        return hashlib.sha256(b"hyprial-native-media-v1\0" + canonical_json_v1(envelope)).hexdigest()

    def seal(
        self,
        *,
        scope: NativeMediaScope,
        body: bytes,
        name: str | None,
        media_type: str | None,
        native_metadata: object,
    ) -> SealedProviderMediaRef:
        if not isinstance(scope, NativeMediaScope):
            raise TypeError("scope must be NativeMediaScope")
        if not isinstance(body, bytes):
            raise TypeError("body must be bytes")
        if len(body) > self._max_blob_bytes:
            raise NativeMediaDenied("NATIVE_MEDIA_TOO_LARGE")
        name = self._optional_text(name, "name")
        media_type = self._optional_text(media_type, "media_type")
        metadata = _validate_native_metadata(native_metadata)
        digest = hashlib.sha256(body).hexdigest()
        opaque = self._opaque_digest(
            scope,
            name=name,
            media_type=media_type,
            size=len(body),
            sha256=digest,
            native_metadata=metadata,
        )
        handle = f"native-media:v1:{secrets.token_urlsafe(32)}"
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO sealed_native_media(
                    handle_id,owner_principal,organization_id,provider,instance_id,
                    generation,target_ref_id,content_digest,name,media_type,size,sha256,
                    native_metadata_json,opaque_ref_digest,body
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    handle,
                    scope.owner_principal,
                    scope.organization_id,
                    scope.provider,
                    scope.instance_id,
                    scope.generation,
                    scope.target_ref_id,
                    scope.content_digest,
                    name,
                    media_type,
                    len(body),
                    digest,
                    canonical_json_v1(metadata),
                    opaque,
                    body,
                ),
            )
        return SealedProviderMediaRef(scope.instance_id, scope.generation, opaque, handle)

    def _read_validated(
        self, source: SealedProviderMediaRef, scope: NativeMediaScope
    ) -> tuple[SealedNativeMedia, bytes]:
        if not isinstance(source, SealedProviderMediaRef):
            raise TypeError("source must be SealedProviderMediaRef")
        if not isinstance(scope, NativeMediaScope):
            raise TypeError("scope must be NativeMediaScope")
        with self._lock:
            row = self._connection.execute(
                """
                SELECT owner_principal,organization_id,provider,instance_id,generation,
                       target_ref_id,content_digest,name,media_type,size,sha256,
                       native_metadata_json,opaque_ref_digest,
                       CASE WHEN length(body) <= ? THEN body ELSE NULL END
                FROM sealed_native_media WHERE handle_id=?
                """,
                (self._max_blob_bytes, source.authority_handle_id),
            ).fetchone()
        if row is None:
            raise NativeMediaDenied("NATIVE_MEDIA_NOT_FOUND")
        (
            owner,
            organization,
            provider,
            instance,
            generation,
            target,
            content_digest,
            name,
            media_type,
            size,
            sha256,
            metadata_json,
            opaque,
            body,
        ) = row
        exact_scope = (
            owner,
            organization,
            provider,
            instance,
            generation,
            target,
            content_digest,
        )
        requested_scope = (
            scope.owner_principal,
            scope.organization_id,
            scope.provider,
            scope.instance_id,
            scope.generation,
            scope.target_ref_id,
            scope.content_digest,
        )
        if exact_scope != requested_scope or (
            source.instance_id != instance or source.generation != generation
        ):
            raise NativeMediaDenied("NATIVE_MEDIA_SCOPE_DENIED")
        if source.opaque_ref_digest != opaque:
            raise NativeMediaDenied("NATIVE_MEDIA_REF_DENIED")
        if size > self._max_blob_bytes or body is None:
            raise NativeMediaDenied("NATIVE_MEDIA_TOO_LARGE")
        body = bytes(body)
        if len(body) != size or hashlib.sha256(body).hexdigest() != sha256:
            raise NativeMediaDenied("NATIVE_MEDIA_INTEGRITY_DRIFT")
        try:
            metadata = _validate_native_metadata(json.loads(bytes(metadata_json)))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise NativeMediaDenied("NATIVE_MEDIA_INTEGRITY_DRIFT") from error
        expected_opaque = self._opaque_digest(
            scope,
            name=name,
            media_type=media_type,
            size=size,
            sha256=sha256,
            native_metadata=metadata,
        )
        if opaque != expected_opaque:
            raise NativeMediaDenied("NATIVE_MEDIA_INTEGRITY_DRIFT")
        return (
            SealedNativeMedia(
                owner,
                organization,
                provider,
                instance,
                generation,
                target,
                content_digest,
                name,
                media_type,
                size,
                sha256,
                metadata,
            ),
            body,
        )

    def resolve(
        self, source: SealedProviderMediaRef, *, scope: NativeMediaScope
    ) -> SealedNativeMedia:
        metadata, _body = self._read_validated(source, scope)
        return metadata

    def open(
        self,
        source: SealedProviderMediaRef,
        *,
        scope: NativeMediaScope,
        max_bytes: int,
    ) -> _SealedByteStream:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        metadata, body = self._read_validated(source, scope)
        if metadata.size > max_bytes:
            raise NativeMediaDenied("NATIVE_MEDIA_TOO_LARGE")
        return _SealedByteStream(body, metadata)

    def close(self) -> None:
        with self._lock:
            self._connection.close()


__all__ = [
    "NativeMediaDenied",
    "SCHEMA_VERSION",
    "SealedNativeMediaAuthority",
]
