# Decisions:
# - Phase D T1: ``test_idle_runtime_tick_expires_pending_without_an_import``
#   runs on ``OrgFsRuntime``.  The state host keeps no pending envelopes
#   to expire (design §3.6); what an idle maintenance tick owes it is the §9
#   R4 verification batch: after a reopen every document is unverified and
#   counted in ``content_pending``, and one due tick with no peer to pull from
#   verifies them.
# - Package S (2026-11-25): the journal-only drain tests of this file were
#   deleted with the journal store.
from __future__ import annotations

from tests.daemon.orgfs.runtime_state.support import Peers


def test_idle_runtime_tick_expires_pending_without_an_import(tmp_path, monkeypatch):
    """The maintenance tick makes progress on an idle space without a pull."""

    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "harness-state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    peers = Peers(tmp_path / "idle-runtime")
    try:
        receiver = peers.add("node-receiver", "user:owner")
        info = receiver.fs.create_space("group-idle")
        for index in range(5):
            receiver.fs.write_text(info.space_id, f"note-{index}.md", str(index))
        receiver.reopen()
        assert receiver.runtime is not None
        assert receiver.fs.status(info.space_id).content_pending == 5
        receiver.runtime._directory_sync_due = 0.0

        receiver.runtime.anti_entropy_tick(1.0)

        assert receiver.fs.status(info.space_id).content_pending == 0
        assert "orgfs.sync.maintain-failed" not in receiver.events.names()
        assert [receiver.fs.read_text(info.space_id, f"note-{index}.md")[0] for index in range(5)] == [
            str(index) for index in range(5)
        ]
    finally:
        peers.close()
