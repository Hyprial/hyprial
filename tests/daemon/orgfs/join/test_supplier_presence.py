"""A join must not report a partially consumed sync page as successful."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from hyprial.daemon.impl.org.network.directory import InvalidDeviceQuarantine
from hyprial.daemon.impl.org.network.service import OrgNetworkService
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.storage.store import StoreError
from tests.daemon.orgfs.presence.test_orgfs_rebuild_catchup import _Bus, _Presence


@contextmanager
def _interrupted_pair(tmp_path, monkeypatch, *, interrupt_after, cause):
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    bus = _Bus()
    events = []
    # Model private sidecar admission (§7); presence loss still fences every page.
    owner = OrgFsRuntime(
        tmp_path / "owner", node_id="node-owner", author="user:owner",
        trust_tailnet_members=True,
    )
    reader = OrgFsRuntime(
        tmp_path / "reader", node_id="node-reader", author="user:reader",
        trust_tailnet_members=True,
        logger=lambda level, event, **fields: events.append((event, fields)),
    )
    session = bus.session("node-reader")
    presence = _Presence(session)
    state = {"applied": 0, "interrupted": False, "received": 0}

    def recover():
        presence.online.add("node-owner")
        session.callbacks_complete = True

    state["recover"] = recover
    try:
        owner.bind_transport(bus.session("node-owner"), supplier_online=lambda _: True)
        space = owner.facade.create_space("join interrupted within a page")
        owner.facade.invite(space.space_id, "user:reader", "rw")
        for index in range(3):
            owner.facade.write_text(space.space_id, f"file-{index}.txt", f"body-{index}")
        # Bind after seeding so only the requested sync can deliver these bytes.
        reader.bind_transport(
            session,
            supplier_online=presence.supplier_online,
            holder_candidates=lambda: ("node-owner",),
        )
        mesh = reader._mesh(space.space_id)
        original_receive = mesh._receive

        def receive_then_flip(envelope, **kwargs):
            state["received"] += 1
            result = original_receive(envelope, **kwargs)
            if result.status == "applied":
                state["applied"] += 1
                if state["applied"] == interrupt_after and not state["interrupted"]:
                    if cause == "callbacks-incomplete":
                        session.begin_rebuild()
                        assert "node-owner" in presence.online
                    else:
                        presence.online.remove("node-owner")
                    state["callbackHistoryComplete"] = session.callbacks_complete
                    state["interrupted"] = True
            return result

        monkeypatch.setattr(mesh, "_receive", receive_then_flip)
        yield owner, reader, space, mesh, state, events
    finally:
        reader.close()
        owner.close()


def _assert_not_joined(reader, space_id):
    assert reader.facade.spaces() == ()
    with pytest.raises(OrgFsError) as error:
        reader.facade.stat(space_id, "id:root")
    assert error.value.code == "unknown-space"


def _assert_complete(owner, reader, space):
    assert reader.facade.spaces() == (space,)
    assert reader.facade.space_meta(space.space_id) == owner.facade.space_meta(space.space_id)
    for index in range(3):
        assert reader.facade.read_text(space.space_id, f"file-{index}.txt")[0] == f"body-{index}"
    source = owner.stores.get(space.space_id)
    target = reader.stores.get(space.space_id)
    assert target.document_ids() == source.document_ids()
    for doc_id in source.document_ids():
        assert target.frontier(doc_id) == source.frontier(doc_id)
    assert target.member_mode("user:reader") == "rw"


@pytest.mark.parametrize("interrupt_after", [1, 3])
@pytest.mark.parametrize("cause", ["supplier-offline", "callbacks-incomplete"])
def test_sync_fails_immediately_when_supplier_disappears_mid_page(
    tmp_path, monkeypatch, interrupt_after, cause,
):
    with _interrupted_pair(tmp_path, monkeypatch, interrupt_after=interrupt_after, cause=cause) as pair:
        owner, reader, space, mesh, state, events = pair
        with pytest.raises(StoreError) as error:
            mesh.sync_from("node-owner")
        assert error.value.code == "not-a-member"
        assert error.value.details["reason"] == "supplier-not-online"
        assert state["interrupted"]
        assert state["callbackHistoryComplete"] is (cause != "callbacks-incomplete")
        assert state["received"] == interrupt_after + 1
        assert not any(event == "orgfs.sync.completed" for event, _ in events)
        _assert_not_joined(reader, space.space_id)
        # Accepted journal entries remain useful; a new pull must finish them.
        state["recover"]()
        assert reader.facade.join(space.space_id) == space
        _assert_complete(owner, reader, space)


@pytest.mark.parametrize("interrupt_after", [1, 3])
@pytest.mark.parametrize("cause", ["supplier-offline", "callbacks-incomplete"])
def test_org_join_retries_mid_page_presence_loss_without_half_joined_state(
    tmp_path, monkeypatch, interrupt_after, cause,
):
    with _interrupted_pair(tmp_path, monkeypatch, interrupt_after=interrupt_after, cause=cause) as pair:
        owner, reader, space, _mesh, state, events = pair
        retries = []

        def presence_recovers(seconds):
            retries.append(seconds)
            assert state["interrupted"]
            assert state["callbackHistoryComplete"] is (cause != "callbacks-incomplete")
            _assert_not_joined(reader, space.space_id)
            state["recover"]()

        service = OrgNetworkService(
            fs=reader.facade, home=tmp_path / "reader", node_id="node-reader",
            owner="reader", invalid_devices=InvalidDeviceQuarantine(),
            sleep=presence_recovers, join_timeout_seconds=1.0,
        )
        service._join_with_retry(space.space_id)
        assert len(retries) == 1
        assert sum(event == "orgfs.sync.completed" for event, _ in events) == 1
        _assert_complete(owner, reader, space)
        # Idempotent rejoin does not expose a second/partial local space.
        assert reader.facade.join(space.space_id) == space
        _assert_complete(owner, reader, space)
