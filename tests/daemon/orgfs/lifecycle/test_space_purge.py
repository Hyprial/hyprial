from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import os
import threading

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime


OWNER = "user:owner"


def _runtime(tmp_path, *, binding=None):
    return OrgFsRuntime(
        tmp_path / "state",
        node_id="node-a",
        author=OWNER,
        space_binding_state=(lambda _space_id: binding),
    )


def _create_paired_spaces(runtime):
    main = runtime.facade.create_space("group-team")
    acl = runtime.facade.create_space("group-team-acl")
    runtime.facade.write_text(main.space_id, "main.txt", "main")
    runtime.facade.write_text(acl.space_id, "acl.txt", "acl")
    return main, acl


def _file_identity(path):
    value = os.stat(path)
    return value.st_dev, value.st_ino


def _open_fds_for(identities):
    matches = []
    for name in os.listdir("/dev/fd"):
        try:
            descriptor = int(name)
            value = os.fstat(descriptor)
        except (OSError, ValueError):
            continue
        if (value.st_dev, value.st_ino) in identities:
            matches.append(descriptor)
    return tuple(sorted(matches))


def _admit_delayed_broadcast(runtime, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    original = runtime.facade._execute_network_effect

    def delayed(space_id):
        entered.set()
        if not release.wait(10):
            raise TimeoutError("test broadcast remained delayed")
        return original(space_id)

    monkeypatch.setattr(runtime.facade, "_execute_network_effect", delayed)
    space = runtime.facade.create_space("group-team")
    assert entered.wait(2)
    identities = {
        _file_identity(tmp)
        for tmp in (
            runtime.state_dir / "orgfs" / "blob-refs.sqlite3",
            runtime.state_dir
            / "orgfs"
            / "spaces"
            / space.space_id
            / "journal.sqlite3",
        )
    }
    return space, release, identities


def test_close_joins_admitted_broadcast_before_closing_store_fds(
    tmp_path, monkeypatch
):
    runtime = _runtime(tmp_path)
    _space, release, identities = _admit_delayed_broadcast(runtime, monkeypatch)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            closing = pool.submit(runtime.close)
            with pytest.raises(FutureTimeout):
                closing.result(timeout=0.1)
            release.set()
            closing.result(timeout=5)
        assert _open_fds_for(identities) == ()
    finally:
        release.set()
        runtime.close()


def test_space_purge_joins_admitted_broadcast_before_releasing_store_fds(
    tmp_path, monkeypatch
):
    runtime = _runtime(
        tmp_path, binding={"org": "group-team", "state": "deleted"}
    )
    space, release, identities = _admit_delayed_broadcast(runtime, monkeypatch)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            purging = pool.submit(runtime.purge_space, space.space_id)
            with pytest.raises(FutureTimeout):
                purging.result(timeout=0.1)
            release.set()
            assert purging.result(timeout=5)["purged"] is True
        runtime.close()
        assert _open_fds_for(identities) == ()
    finally:
        release.set()
        runtime.close()


def test_space_purge_main_preserves_acl_across_restart(tmp_path):
    binding = {"org": "group-team", "state": "deleted"}
    runtime = _runtime(tmp_path, binding=binding)
    main, acl = _create_paired_spaces(runtime)
    runtime.checkout(main.space_id, True)
    runtime.serve(main.space_id, "fs")

    assert runtime.purge_space(main.space_id)["purged"] is True
    assert [item.space_id for item in runtime.facade.spaces()] == [acl.space_id]
    assert main.space_id not in runtime.stores
    assert main.space_id not in runtime._meshes
    assert main.space_id not in runtime._replicas
    assert main.space_id not in runtime._checkouts
    runtime.close()

    restarted = _runtime(tmp_path, binding=binding)
    try:
        assert [item.space_id for item in restarted.facade.spaces()] == [acl.space_id]
        assert restarted.purge_space(acl.space_id)["purged"] is True
    finally:
        restarted.close()


def test_space_purge_acl_preserves_main(tmp_path):
    runtime = _runtime(
        tmp_path, binding={"org": "group-team", "state": "deleted"}
    )
    main, acl = _create_paired_spaces(runtime)
    try:
        assert runtime.purge_space(acl.space_id)["purged"] is True
        assert [item.space_id for item in runtime.facade.spaces()] == [main.space_id]
        assert (tmp_path / "state" / "orgfs" / "spaces" / main.space_id).is_dir()
    finally:
        runtime.close()


def test_space_purge_both_paired_spaces_survives_restart(tmp_path):
    binding = {"org": "group-team", "state": "deleted"}
    runtime = _runtime(tmp_path, binding=binding)
    main, acl = _create_paired_spaces(runtime)
    assert runtime.purge_space(main.space_id)["purged"] is True
    assert runtime.purge_space(acl.space_id)["purged"] is True
    assert runtime.facade.spaces() == ()
    runtime.close()

    restarted = _runtime(tmp_path, binding=binding)
    try:
        assert restarted.facade.spaces() == ()
    finally:
        restarted.close()


def test_space_purge_repeated_id_is_idempotent(tmp_path):
    runtime = _runtime(
        tmp_path, binding={"org": "group-team", "state": "deleted"}
    )
    main, acl = _create_paired_spaces(runtime)
    try:
        assert runtime.purge_space(main.space_id)["purged"] is True
        assert runtime.purge_space(main.space_id) == {
            "spaceId": main.space_id,
            "purged": False,
            "localOnly": True,
        }
        assert [item.space_id for item in runtime.facade.spaces()] == [acl.space_id]
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("first_id", "second_id", "restart_between"),
    (
        pytest.param("group-team", "group-team-acl", False, id="directory-acl-restart"),
        pytest.param("group-team-acl", "group-team", False, id="acl-directory-restart"),
        pytest.param("group-team", "group-team-acl", True, id="directory-restart-acl"),
        pytest.param("group-team-acl", "group-team", True, id="acl-restart-directory"),
    ),
)
def test_space_purge_preserves_paired_space_for_every_ordering(
    tmp_path, first_id, second_id, restart_between
):
    binding = {"org": "group-team", "state": "deleted"}
    runtime = _runtime(tmp_path, binding=binding)
    spaces = {
        name: runtime.facade.create_space(name)
        for name in ("group-team", "group-team-acl")
    }
    first_id = spaces[first_id].space_id
    second_id = spaces[second_id].space_id
    for name, space in spaces.items():
        runtime.facade.write_text(space.space_id, f"{name}.txt", name)
    runtime.checkout(first_id, True)
    runtime.serve(first_id, "fs")

    assert runtime.purge_space(first_id) == {
        "spaceId": first_id,
        "purged": True,
        "localOnly": True,
    }
    assert [item.space_id for item in runtime.facade.spaces()] == [second_id]
    assert not (tmp_path / "state" / "orgfs" / "spaces" / first_id).exists()
    assert (tmp_path / "state" / "orgfs" / "spaces" / second_id).is_dir()
    assert first_id not in runtime.stores
    assert first_id not in runtime._meshes
    assert first_id not in runtime._replicas
    assert first_id not in runtime._checkouts

    if restart_between:
        runtime.close()
        runtime = _runtime(tmp_path, binding=binding)
        assert [item.space_id for item in runtime.facade.spaces()] == [second_id]
        assert runtime.purge_space(first_id) == {
            "spaceId": first_id,
            "purged": False,
            "localOnly": True,
        }
    assert runtime.purge_space(second_id)["purged"] is True
    assert runtime.facade.spaces() == ()
    runtime.close()

    final = _runtime(tmp_path, binding=binding)
    try:
        assert final.facade.spaces() == ()
    finally:
        final.close()


def test_space_purge_requires_local_owner_and_owner_only_members(tmp_path):
    runtime = _runtime(tmp_path)
    space = runtime.facade.create_space("scratch")
    runtime.facade.invite(space.space_id, "user:other")
    with pytest.raises(OrgFsError, match="space-purge-refused") as members:
        runtime.purge_space(space.space_id)
    assert members.value.details["reason"] == "other-members"
    runtime.facade.remove_member(space.space_id, "user:other")
    runtime.author = "user:other"
    with pytest.raises(OrgFsError, match="space-purge-refused") as owner:
        runtime.purge_space(space.space_id)
    assert owner.value.details["reason"] == "not-local-owner"
    runtime.close()


def test_space_purge_requires_bound_org_to_be_logically_deleted(tmp_path):
    runtime = _runtime(tmp_path, binding={"org": "team", "state": "active"})
    space = runtime.facade.create_space("group-team")
    with pytest.raises(OrgFsError, match="space-purge-refused") as rejected:
        runtime.purge_space(space.space_id)
    assert rejected.value.details == {
        "spaceId": space.space_id,
        "org": "team",
        "state": "active",
        "reason": "org-not-deleted",
    }
    runtime.close()

    deleted = _runtime(tmp_path, binding={"org": "team", "state": "deleted"})
    try:
        assert deleted.purge_space(space.space_id)["purged"] is True
    finally:
        deleted.close()


def test_space_purge_path_fence_refuses_symlink_without_touching_target(tmp_path):
    runtime = _runtime(tmp_path)
    space = runtime.facade.create_space("scratch")
    space_path = tmp_path / "state" / "orgfs" / "spaces" / space.space_id
    moved = tmp_path / "outside-space"
    os.rename(space_path, moved)
    space_path.symlink_to(moved, target_is_directory=True)

    with pytest.raises(OrgFsError, match="unsafe-space-path"):
        runtime.purge_space(space.space_id)

    assert moved.is_dir()
    assert (moved / "journal.sqlite3").is_file()
    runtime.close()


def test_space_purge_path_fence_refuses_ancestry_swap_without_touching_target(
    tmp_path, monkeypatch
):
    runtime = _runtime(tmp_path)
    space = runtime.facade.create_space("scratch")
    orgfs_root = tmp_path / "state" / "orgfs"
    parked_root = tmp_path / "parked-orgfs"
    outside_root = tmp_path / "outside-orgfs"
    outside_space = outside_root / "spaces" / space.space_id
    outside_space.mkdir(parents=True)
    (outside_root / "replica").mkdir()
    marker = outside_space / "must-survive.txt"
    marker.write_text("outside", encoding="utf-8")
    original_remove = runtime._remove_fenced_tree
    swapped = False

    def swap_ancestry_then_remove(path, target_space_id):
        nonlocal swapped
        if not swapped:
            os.rename(orgfs_root, parked_root)
            orgfs_root.symlink_to(outside_root, target_is_directory=True)
            swapped = True
        return original_remove(path, target_space_id)

    monkeypatch.setattr(runtime, "_remove_fenced_tree", swap_ancestry_then_remove)
    try:
        with pytest.raises(OrgFsError) as rejected:
            runtime.purge_space(space.space_id)

        assert rejected.value.code == "unsafe-space-path"
        assert outside_space.is_dir()
        assert marker.read_text("utf-8") == "outside"
    finally:
        runtime.close()


def test_space_purge_prepared_intent_resumes_before_detach(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    space = runtime.facade.create_space("scratch")

    def interrupted(_space_id):
        raise OSError("simulated crash before detach")

    monkeypatch.setattr(runtime, "_detach_space", interrupted)
    with pytest.raises(OrgFsError, match="space-purge-incomplete"):
        runtime.purge_space(space.space_id)
    intent = tmp_path / "state" / "orgfs" / "purge-intents" / f"{space.space_id}.json"
    assert '"state":"prepared"' in intent.read_text("utf-8")
    runtime.close()

    resumed = _runtime(tmp_path)
    try:
        assert resumed.facade.spaces() == ()
        assert not intent.exists()
        assert not (tmp_path / "state" / "orgfs" / "spaces" / space.space_id).exists()
    finally:
        resumed.close()


def test_space_purge_resume_deletes_blob_after_release_interruption(
    tmp_path, monkeypatch
):
    runtime = _runtime(tmp_path)
    space = runtime.facade.create_space("scratch")
    digest = runtime.blobs.put(space.space_id, b"orphan-on-crash", reason="test")

    def interrupted(_digest):
        raise OSError("simulated crash after ref release")

    monkeypatch.setattr(runtime.blobs, "delete_if_unreferenced", interrupted)
    with pytest.raises(OrgFsError, match="space-purge-incomplete"):
        runtime.purge_space(space.space_id)
    intent = tmp_path / "state" / "orgfs" / "purge-intents" / f"{space.space_id}.json"
    assert runtime.blobs.references(space.space_id) == ()
    assert runtime.blobs.contains(digest) is True
    assert intent.is_file()
    intent_payload = intent.read_text("utf-8")
    assert '"state":"detached"' in intent_payload
    assert f'"cleanupDigests":["{digest}"]' in intent_payload
    runtime.close()

    resumed = _runtime(tmp_path)
    try:
        assert resumed.blobs.contains(digest) is False
        assert not intent.exists()
    finally:
        resumed.close()


def test_space_purge_durable_intent_resumes_after_detach_failure(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    space = runtime.facade.create_space("scratch")

    def interrupted(_space_id):
        raise OSError("simulated crash after detach")

    monkeypatch.setattr(runtime, "_delete_space_storage", interrupted)
    with pytest.raises(OrgFsError, match="space-purge-incomplete") as failed:
        runtime.purge_space(space.space_id)
    assert failed.value.details["resumable"] is True
    assert space.space_id not in {item.space_id for item in runtime.facade.spaces()}
    intent = tmp_path / "state" / "orgfs" / "purge-intents" / f"{space.space_id}.json"
    assert intent.is_file()
    runtime.close()

    resumed = _runtime(tmp_path)
    try:
        assert resumed.facade.spaces() == ()
        assert not intent.exists()
        assert not (tmp_path / "state" / "orgfs" / "spaces" / space.space_id).exists()
    finally:
        resumed.close()
