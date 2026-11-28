# Decisions:
# - Phase D, package T2: rewritten onto ``OrgFsRuntime``.  Restore of an
#   old version is gone (design §6: restore brings back the last state) and
#   ``status.durable_holders_seen`` is deleted (design §1.1), so the honest
#   report is re-stated for the current file: when the only holder loses the
#   bytes of a file it references, a read and ``stat`` say the content is
#   pending with the expected digest and size, ``localState: absent`` and no
#   suggested holder (no stale or empty bytes are returned), and putting the
#   same bytes back restores reads.
"""The last holder losing a blob is reported, and a re-supplied blob restores it."""

from __future__ import annotations

import hashlib

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.composition.state import OrgFsRuntime


@pytest.fixture(autouse=True)
def isolated_orgfs_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


def test_last_holder_loss_reported_honestly(tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path / "runtime", node_id="node-a", author="user:owner")
    try:
        space = runtime.facade.create_space("team")
        payload = b"only holder"
        node = runtime.facade.write_bytes(space.space_id, "asset.bin", payload)
        digest = hashlib.sha256(payload).hexdigest()
        assert node.blob_hash == digest
        runtime.blobs._path(digest).unlink()  # noqa: SLF001 - the holder loses the bytes

        with pytest.raises(OrgFsError) as unavailable:
            runtime.facade.read_bytes(space.space_id, f"id:{node.node_id}")
        assert unavailable.value.code == "content-pending"
        assert unavailable.value.details["expectedSha256"] == digest
        assert unavailable.value.details["expectedSize"] == len(payload)
        assert unavailable.value.details["localState"] == "absent"
        assert unavailable.value.details["suggestedHolders"] == []
        assert runtime.facade.stat(space.space_id, f"id:{node.node_id}").content_state == "pending"
        assert runtime.status(space.space_id).holders_online == ()

        assert runtime.blobs.put(space.space_id, payload, reason="replica") == digest
        assert runtime.facade.read_bytes(space.space_id, f"id:{node.node_id}") == payload
        assert runtime.facade.stat(space.space_id, f"id:{node.node_id}").content_state == "arrived"
    finally:
        runtime.close()
