from __future__ import annotations

import base64
import hashlib
import inspect
import json
import threading

import pytest
from pycrdt import Doc, Text

from hyprial.daemon.impl.orgfs import LocalOrgFs, OrgFs, OrgFsError
from hyprial.daemon.impl.orgfs.document.model import ORGFS_TEXT_MAX


class FakeBlobs:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def put(self, space_id: str, data: bytes, *, reason: str) -> str:
        digest = hashlib.sha256(data).hexdigest()
        self.values[digest] = data
        return digest

    def get(self, space_id: str, digest: str) -> bytes:
        return self.values[digest]

    def has(self, digest: str) -> bool:
        return digest in self.values


class FakeStore:
    def __init__(self) -> None:
        self.commits: list[tuple[str, str, str | None]] = []
        self.holders = (("node-b", True), ("node-c", False))
        self.fail = False

    def commit(self, doc_id, mutate, *, author, actor):
        if self.fail:
            raise RuntimeError("journal unavailable")
        self.commits.append((doc_id, author, actor))
        mutate(None)

    def commit_many(self, mutations, *, author, actor):
        if self.fail:
            raise RuntimeError("journal unavailable")
        self.commits.extend((doc_id, author, actor) for doc_id, _ in mutations)
        return ()

    def unconfirmed_commits(self) -> int:
        return 2

    def holders_seen(self):
        return self.holders


class FakeJoinMesh:
    """Join seam that installs an invited space before returning."""

    def __init__(self, owner: LocalOrgFs) -> None:
        self.owner = owner
        self.joiner: LocalOrgFs | None = None

    def join(self, space_id: str):
        assert self.joiner is not None
        self.joiner._spaces[space_id] = self.owner._spaces[space_id]
        return self.owner._spaces[space_id].info


def test_watch_matches_frozen_signature_verbatim() -> None:
    """Review item 12: watch has no defaults and callback is required."""

    protocol = inspect.signature(OrgFs.watch)
    implementation = inspect.signature(LocalOrgFs.watch)
    for signature in (protocol, implementation):
        assert tuple(signature.parameters) == ("self", "space_id", "glob", "callback")
        assert all(
            parameter.default is inspect.Parameter.empty
            for parameter in signature.parameters.values()
        )

    fs = LocalOrgFs(author="user:owner")
    space = fs.create_space("team")
    with pytest.raises(TypeError):
        fs.watch(space.space_id)  # type: ignore[call-arg]


def test_members_status_blobs_watch_and_store_commit() -> None:
    store = FakeStore()
    blobs = FakeBlobs()
    fs = LocalOrgFs(store, blobs, author="user:owner", actor="agent:owner:test")
    space = fs.create_space("team")
    fs.invite(space.space_id, "user:reader", "ro")
    assert fs.members(space.space_id)[-1].mode == "ro"

    seen = []
    watcher = fs.watch(space.space_id, "*.bin", seen.append)
    info = fs.write_bytes(space.space_id, "data.bin", b"binary")
    assert fs.read_bytes(space.space_id, "data.bin") == b"binary"
    assert seen[-1].kind == "created"
    watcher.close()
    fs.write_bytes(space.space_id, "data.bin", b"changed")
    assert len(seen) == 1
    text = fs.write_text(space.space_id, "readme.txt", "hello")
    assert fs.open_doc(space.space_id, f"id:{text.node_id}").doc_id() == text.doc_id

    status = fs.status(space.space_id)
    assert status.unconfirmed_commits == 2
    assert status.durable_holders_seen == ("node-b",)
    assert status.holders_online == ("node-b", "node-c")
    assert any(doc_id.startswith("tree-") for doc_id, _author, _actor in store.commits)
    assert info.blob_hash is not None


def test_non_owner_cannot_change_members_and_join_requires_holder() -> None:
    owner = LocalOrgFs(author="user:owner")
    space = owner.create_space("team")
    reader = LocalOrgFs(author="user:reader")
    with pytest.raises(OrgFsError) as error:
        owner.remove_member(space.space_id, "user:missing")
    assert error.value.code == "invalid-argument"
    with pytest.raises(OrgFsError) as error:
        reader.invite(space.space_id, "user:other")
    assert error.value.code == "unknown-space"
    with pytest.raises(OrgFsError) as error:
        reader.join(space.space_id)
    assert error.value.code == "no-holder-online"


def test_joined_non_owner_cannot_invite_or_remove_members() -> None:
    """Review item 15l: a genuinely joined member reaches the not-owner gate."""

    owner = LocalOrgFs(author="user:owner")
    space = owner.create_space("team")
    owner.invite(space.space_id, "user:joined")
    mesh = FakeJoinMesh(owner)
    joined = LocalOrgFs(mesh=mesh, author="user:joined")
    mesh.joiner = joined

    assert joined.join(space.space_id) == space
    assert {member.user for member in joined.members(space.space_id)} >= {
        "user:owner",
        "user:joined",
    }
    with pytest.raises(OrgFsError) as invite_error:
        joined.invite(space.space_id, "user:other")
    assert invite_error.value.code == "not-owner"
    with pytest.raises(OrgFsError) as remove_error:
        joined.remove_member(space.space_id, "user:owner")
    assert remove_error.value.code == "not-owner"

    assert owner.update_space_meta(space.space_id, {"aclSpaceId": "acl-1"})[
        "aclSpaceId"
    ] == "acl-1"
    assert joined.space_meta(space.space_id)["aclSpaceId"] == "acl-1"
    with pytest.raises(OrgFsError) as meta_error:
        joined.update_space_meta(space.space_id, {"aclSpaceId": "attacker"})
    assert meta_error.value.code == "not-owner"


@pytest.mark.parametrize("key", ("owner", "name", "spaceId", "createdAt"))
def test_core_space_metadata_is_immutable(key: str) -> None:
    fs = LocalOrgFs(author="user:owner")
    space = fs.create_space("team")
    before = fs.space_meta(space.space_id)

    with pytest.raises(OrgFsError) as error:
        fs.update_space_meta(space.space_id, {key: "attacker-value"})

    assert error.value.code == "invalid-argument"
    assert fs.space_meta(space.space_id) == before


def test_text_over_four_mib_rejects_with_blob_guidance() -> None:
    """Review item 15e: oversized text is too-large and directs callers to blob."""

    fs = LocalOrgFs(author="user:owner")
    space = fs.create_space("team")
    with pytest.raises(OrgFsError) as error:
        fs.write_text(space.space_id, "large.txt", "x" * (ORGFS_TEXT_MAX + 1))
    assert error.value.code == "too-large"
    assert error.value.details == {"maxBytes": ORGFS_TEXT_MAX, "use": "blob"}
    with pytest.raises(OrgFsError) as missing:
        fs.stat(space.space_id, "large.txt")
    assert missing.value.code == "unknown-doc"


def test_failed_durable_commit_rolls_back_facade_state() -> None:
    store = FakeStore()
    fs = LocalOrgFs(store, author="user:owner")
    space = fs.create_space("team")
    store.fail = True
    with pytest.raises(OrgFsError, match="journal unavailable"):
        fs.write_text(space.space_id, "not-committed.md", "no")
    assert fs.resolve(space.space_id, "")
    with pytest.raises(OrgFsError) as missing:
        fs.stat(space.space_id, "not-committed.md")
    assert missing.value.code == "unknown-doc"


def test_facade_serializes_inbound_apply_without_swapping_local_identity(
    monkeypatch,
) -> None:
    """F4: facade apply and local writes serialize without author/actor swapping."""

    fs = LocalOrgFs(author="user:owner", actor="agent:local")
    space = fs.create_space("team")
    item = fs.write_text(space.space_id, "note.md", "local-before")
    document = fs._spaces[space.space_id].contents[item.doc_id or ""]
    remote = Doc(client_id=424242)
    remote.apply_update(document.export())
    before = remote.get_state()
    text = remote.get("text", type=Text)
    text.clear()
    text.insert(0, "remote")
    envelope = json.dumps(
        {
            "docId": item.doc_id,
            "origin": {"author": "user:remote", "actor": "agent:remote"},
            "update": base64.b64encode(remote.get_update(before)).decode(),
        }
    ).encode()

    entered = threading.Event()
    release = threading.Event()
    original_finish = fs._finish

    def blocked_finish(*args, **kwargs):
        if kwargs.get("author") == "user:remote":
            entered.set()
            assert release.wait(2)
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(fs, "_finish", blocked_finish)
    inbound = threading.Thread(
        target=fs.apply_envelope, args=(space.space_id, envelope), daemon=True
    )
    inbound.start()
    assert entered.wait(2)
    assert (fs.author, fs.actor) == ("user:owner", "agent:local")

    local_started = threading.Event()
    local_done = threading.Event()

    def local_write() -> None:
        local_started.set()
        fs.write_text(space.space_id, "note.md", "local-after")
        local_done.set()

    local = threading.Thread(target=local_write, daemon=True)
    local.start()
    assert local_started.wait(2)
    assert not local_done.wait(0.1)
    release.set()
    inbound.join(2)
    local.join(2)
    assert not inbound.is_alive()
    assert local_done.is_set()
    assert fs.read_text(space.space_id, "note.md")[0] == "local-after"
    authors = [entry.author for entry in fs.history(space.space_id, "note.md")]
    assert authors[:2] == ["user:owner", "user:remote"]


def test_a_reinvite_keeps_added_at_and_the_owner_keeps_the_space_birth(monkeypatch) -> None:
    """added_at orders who dials whom between org devices (org.network.dialing),
    so a re-invite of a current member (a second device, a mode change) must
    not move it, and the owner's row stays at the space's creation time."""

    from hyprial.daemon.impl.orgfs.document.operations import spaces as spaces_module

    ticks = iter(f"2026-10-0{day}T00:00:00+00:00" for day in range(1, 10))
    monkeypatch.setattr(spaces_module, "_now", lambda: next(ticks))
    fs = LocalOrgFs(FakeStore(), FakeBlobs(), author="user:owner", actor="agent:owner:test")
    space = fs.create_space("team")

    def added_at(user: str) -> str:
        return next(m.added_at for m in fs.members(space.space_id) if m.user == user)

    first = fs.invite(space.space_id, "user:reader", "ro").added_at
    again = fs.invite(space.space_id, "user:reader", "rw")
    assert again.added_at == first == added_at("user:reader")
    assert again.mode == "rw"

    fs.invite(space.space_id, "user:owner", "rw")
    assert added_at("user:owner") == space.created_at

    fs.remove_member(space.space_id, "user:reader")
    back = fs.invite(space.space_id, "user:reader", "rw")
    assert back.added_at != first  # a returning member is a new joiner
