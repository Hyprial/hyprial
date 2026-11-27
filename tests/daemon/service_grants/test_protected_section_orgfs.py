"""Red tests for the ``service-grants`` protected org-directory section on real OrgFS.

Decision (service grant documents, 2026-11-26): every assertion runs against the real
``OrgFsRuntime`` + ``LocalSpaceStore`` admission boundary and ``OrgFsDirectoryStore``
so author-rule enforcement is verified on real storage rather than only in unit
helpers.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.identity import (
    parse_protected_directory_doc_id,
    parse_protected_directory_node_id,
    protected_directory_author_allowed,
    protected_directory_doc_id,
    protected_directory_node_id,
    protected_section_for_path,
)

GRANT_ID_A = "sg.abcdefghijklmnopqrstuvwxyz"
GRANT_ID_B = "sg.bcdefghijklmnopqrstuvwxyza"


def _sample_record(grant_id: str = GRANT_ID_A) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "grantId": grant_id,
        "service": {
            "deviceId": "device-a",
            "port": 8447,
            "name": "gateway",
            "protocol": "http",
            "note": None,
        },
        "recipient": {"owner": "bob", "deviceId": "device-b"},
        "grantedAtMs": 1_700_000_000_000,
        "expiresAtMs": None,
        "revokedAtMs": None,
        "credential": None,
    }


@contextmanager
def _as(runtime: OrgFsRuntime, owner: str) -> Iterator[None]:
    previous = runtime.facade.author
    runtime.facade.author = f"user:{owner}"
    try:
        yield
    finally:
        runtime.facade.author = previous


@pytest.fixture
def orgfs_runtime(tmp_path: Path) -> Iterator[OrgFsRuntime]:
    runtime = OrgFsRuntime(
        tmp_path / "state", node_id="device-a", author="user:alice"
    )
    yield runtime
    runtime.close()


def test_protected_section_and_ids_for_service_grants() -> None:
    """``service-grants`` is a protected section with owner-scoped doc and tree ids."""

    path = f"directory/service-grants/alice/{GRANT_ID_A}.json"
    assert protected_section_for_path("directory/service-grants/alice") == "service-grants"
    assert protected_section_for_path(path) == "service-grants"

    doc_id = protected_directory_doc_id("space-1", path, space_owner="user:owner")
    assert doc_id is not None
    assert parse_protected_directory_doc_id(doc_id) == (
        "space-1",
        "service-grants",
        "user:alice",
        f"{GRANT_ID_A}.json",
    )

    node_id = protected_directory_node_id("space-1", path, space_owner="user:owner")
    assert node_id is not None
    assert parse_protected_directory_node_id(node_id) == (
        "space-1",
        "user:alice",
        path,
    )

    assert protected_directory_author_allowed(
        section="service-grants",
        path_principal="user:alice",
        author="user:alice",
        space_owner="user:owner",
    )
    assert not protected_directory_author_allowed(
        section="service-grants",
        path_principal="user:alice",
        author="user:bob",
        space_owner="user:owner",
    )


def test_ensure_org_directory_roots_creates_service_grants_parent(
    orgfs_runtime: OrgFsRuntime,
) -> None:
    """Owner setup creates ``directory/service-grants`` alongside the other roots."""

    space = orgfs_runtime.facade.create_space("group-acme")
    store = OrgFsDirectoryStore(orgfs_runtime.facade)
    store.ensure_org_directory_roots("group-acme")

    info = orgfs_runtime.facade.stat(space.space_id, "directory/service-grants")
    assert info.kind == "dir"


def test_orgfs_refuses_member_writing_into_another_owners_service_grant_path(
    orgfs_runtime: OrgFsRuntime,
) -> None:
    """Real OrgFS admission rejects Bob creating or overwriting documents under Alice's path."""

    space = orgfs_runtime.facade.create_space("group-acme")
    orgfs_runtime.facade.invite(space.space_id, "user:bob", "rw")
    store = OrgFsDirectoryStore(orgfs_runtime.facade)
    store.ensure_org_directory_roots("group-acme")

    store.put_service_grant("group-acme", "alice", GRANT_ID_A, _sample_record(GRANT_ID_A))
    assert store.read_service_grant("group-acme", "alice", GRANT_ID_A) == _sample_record(
        GRANT_ID_A
    )

    with _as(orgfs_runtime, "bob"):
        with pytest.raises(OrgFsError):
            orgfs_runtime.facade.write_text(
                space.space_id,
                f"directory/service-grants/alice/{GRANT_ID_B}.json",
                '{"forged":true}',
            )
        with pytest.raises(OrgFsError):
            orgfs_runtime.facade.write_text(
                space.space_id,
                f"directory/service-grants/alice/{GRANT_ID_A}.json",
                '{"tampered":true}',
            )
        with pytest.raises(OrgFsError):
            orgfs_runtime.facade.mkdir(space.space_id, "directory/service-grants/carol")

        store.put_service_grant(
            "group-acme", "bob", GRANT_ID_B, _sample_record(GRANT_ID_B)
        )

    listed = store.list_service_grants("group-acme")
    assert [(owner, gid) for owner, gid, _ in listed] == [
        ("alice", GRANT_ID_A),
        ("bob", GRANT_ID_B),
    ]
