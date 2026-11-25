from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from hyprial.daemon.impl.adapters.common.transport import (
    NativeMediaDenied,
    NativeMediaScope,
    SealedNativeMediaAuthority,
)


BODY = b"durable-native-media"
SHA256 = hashlib.sha256(BODY).hexdigest()
CONTENT_DIGEST = hashlib.sha256(b"canonical-content").hexdigest()


def _scope(**changes: object) -> NativeMediaScope:
    values: dict[str, object] = {
        "owner_principal": "agent:owner",
        "organization_id": "org-1",
        "provider": "telegram",
        "instance_id": "telegram-main",
        "generation": 7,
        "target_ref_id": "target-1",
        "content_digest": CONTENT_DIGEST,
    }
    values.update(changes)
    return NativeMediaScope(**values)  # type: ignore[arg-type]


def test_sqlite_media_authority_survives_reopen_and_streams_with_exact_scope(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "sealed-media.sqlite3"
        authority = SealedNativeMediaAuthority(path, max_blob_bytes=1024)
        source = authority.seal(
            scope=_scope(),
            body=BODY,
            name="artifact.bin",
            media_type="application/octet-stream",
            native_metadata={"objectId": "native-7", "revision": "1"},
        )
        authority.close()

        reopened = SealedNativeMediaAuthority(path, max_blob_bytes=1024)
        metadata = reopened.resolve(source, scope=_scope())
        assert metadata.owner_principal == "agent:owner"
        assert metadata.organization_id == "org-1"
        assert metadata.provider == "telegram"
        assert metadata.instance_id == "telegram-main"
        assert metadata.generation == 7
        assert metadata.target_ref_id == "target-1"
        assert metadata.content_digest == CONTENT_DIGEST
        assert metadata.size == len(BODY)
        assert metadata.sha256 == SHA256
        assert dict(metadata.native_metadata) == {"objectId": "native-7", "revision": "1"}

        stream = reopened.open(source, scope=_scope(), max_bytes=len(BODY))
        chunks = []
        while chunk := await stream.read(3):
            chunks.append(chunk)
        assert b"".join(chunks) == BODY
        await stream.aclose()
        reopened.close()

        connection = sqlite3.connect(path)
        try:
            assert connection.execute("PRAGMA user_version").fetchone() == (1,)
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert tables == {"common_transport_schema", "sealed_native_media"}
        finally:
            connection.close()

    asyncio.run(scenario())


def test_media_handle_alone_grants_nothing_and_all_authority_fences_reject(
    tmp_path: Path,
) -> None:
    authority = SealedNativeMediaAuthority(tmp_path / "authority.sqlite3", max_blob_bytes=1024)
    scope = _scope()
    source = authority.seal(
        scope=scope,
        body=BODY,
        name=None,
        media_type="application/octet-stream",
        native_metadata={"objectId": "native-7"},
    )
    for forged in (
        replace(scope, owner_principal="agent:attacker"),
        replace(scope, organization_id="org-2"),
        replace(scope, provider="discord"),
        replace(scope, instance_id="telegram-other"),
        replace(scope, generation=8),
        replace(scope, target_ref_id="target-2"),
        replace(scope, content_digest="b" * 64),
    ):
        with pytest.raises(NativeMediaDenied, match="NATIVE_MEDIA_SCOPE_DENIED"):
            authority.resolve(source, scope=forged)
    with pytest.raises(NativeMediaDenied, match="NATIVE_MEDIA_SCOPE_DENIED"):
        authority.resolve(replace(source, generation=6), scope=scope)
    with pytest.raises(NativeMediaDenied, match="NATIVE_MEDIA_REF_DENIED"):
        authority.resolve(replace(source, opaque_ref_digest="c" * 64), scope=scope)
    with pytest.raises(NativeMediaDenied, match="NATIVE_MEDIA_NOT_FOUND"):
        authority.resolve(replace(source, authority_handle_id="native-media:v1:forged"), scope=scope)
    with pytest.raises(NativeMediaDenied, match="NATIVE_MEDIA_TOO_LARGE"):
        authority.open(source, scope=scope, max_bytes=len(BODY) - 1)
    authority.close()


def test_media_resolver_enforces_authority_bound_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "authority.sqlite3"
    authority = SealedNativeMediaAuthority(path, max_blob_bytes=1024)
    source = authority.seal(
        scope=_scope(),
        body=BODY,
        name=None,
        media_type=None,
        native_metadata={"objectId": "native-7"},
    )
    with pytest.raises(NativeMediaDenied, match="NATIVE_MEDIA_TOO_LARGE"):
        authority.seal(
            scope=_scope(),
            body=b"x" * 1025,
            name=None,
            media_type=None,
            native_metadata={"objectId": "native-8"},
        )
    authority.close()

    bounded = SealedNativeMediaAuthority(path, max_blob_bytes=len(BODY) - 1)
    with pytest.raises(NativeMediaDenied, match="NATIVE_MEDIA_TOO_LARGE"):
        bounded.resolve(source, scope=_scope())
    bounded.close()


@pytest.mark.parametrize(
    ("column", "tampered"),
    (
        ("body", b"tampered"),
        ("size", len(BODY) + 1),
        ("sha256", "0" * 64),
        ("native_metadata_json", b'{"objectId":"substituted"}'),
    ),
)
def test_media_rejects_symlink_path_url_metadata_and_integrity_drift(
    tmp_path: Path, column: str, tampered: object
) -> None:
    authority = SealedNativeMediaAuthority(tmp_path / "authority.sqlite3", max_blob_bytes=1024)
    local = tmp_path / "secret.bin"
    local.write_bytes(BODY)
    link = tmp_path / "secret-link"
    os.symlink(local, link)
    with pytest.raises(TypeError, match="body must be bytes"):
        authority.seal(
            scope=_scope(),
            body=link,  # type: ignore[arg-type]
            name=None,
            media_type=None,
            native_metadata={"objectId": "native-7"},
        )
    with pytest.raises(NativeMediaDenied, match="NATIVE_MEDIA_METADATA_FORBIDDEN"):
        authority.seal(
            scope=_scope(),
            body=BODY,
            name=None,
            media_type=None,
            native_metadata={"downloadUrl": "https://caller.invalid/file"},
        )

    source = authority.seal(
        scope=_scope(),
        body=BODY,
        name=None,
        media_type=None,
        native_metadata={"objectId": "native-7"},
    )
    authority.close()
    connection = sqlite3.connect(tmp_path / "authority.sqlite3")
    try:
        connection.execute(
            f"UPDATE sealed_native_media SET {column}=? WHERE handle_id=?",
            (tampered, source.authority_handle_id),
        )
        connection.commit()
    finally:
        connection.close()
    reopened = SealedNativeMediaAuthority(tmp_path / "authority.sqlite3", max_blob_bytes=1024)
    with pytest.raises(NativeMediaDenied, match="NATIVE_MEDIA_INTEGRITY_DRIFT"):
        reopened.open(source, scope=_scope(), max_bytes=1024)
    reopened.close()
