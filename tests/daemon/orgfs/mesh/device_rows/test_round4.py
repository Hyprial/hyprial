# Decisions:
# - Phase D T1: R4-A and R4-B run on the state host (``device_rows/support``).
#   A forged child under Alice's protected directory that arrives before the
#   directory is ``Pending("dependency")`` (design §3.6: nothing stored, no
#   queue, so there is no capacity to consume); delivered again after the
#   directory, as the next pull would, it is ``Rejected("protected-path")``
#   and counted once (§3.7).
# - Package S (2026-11-25): the replica key-validator test, the purge refusal
#   test and the journal envelope helpers were deleted with the replicas,
#   content purge and the journal store.  ``test_r4_c_put_broadcast`` no
#   longer calls ``broadcast_pending``: the runtime publishes every commit.
"""ROUND 4 device-row admission, storage-key, and purge acceptance."""

from __future__ import annotations

import socket
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.transport import ZenohConfig, ZenohTransport
from hyprial.daemon.impl.orgfs.membership import Accepted, Rejected
from hyprial.identity import (
    DirectoryDevice,
    directory_device_path,
    protected_directory_doc_id,
)

from .support import ORG, Frame, Org, new_row

OWNER = "owner@example"
ALICE = "alice@example"
BOB = "bob@example"
CAROL = "carol@example"


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _eventually(action, *, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return action()
        except (AssertionError, OrgFsError) as error:
            last_error = error
            time.sleep(0.05)
    if last_error is not None:
        raise last_error
    raise AssertionError("condition did not become true")


def _equal(actual, expected):
    assert actual == expected
    return actual


def _device(owner: str, device_id: str, *, address: str) -> DirectoryDevice:
    return DirectoryDevice(
        device_id=device_id,
        owner=owner,
        server_public=f"nodekey:server-{owner}-{device_id}",
        client_public=f"nodekey:client-{owner}-{device_id}",
        address=address,
        key_generation=1,
        updated_at="2026-10-07T12:00:00+00:00",
    )


@pytest.fixture
def replicas(tmp_path: Path):
    endpoint = f"tcp/127.0.0.1:{_port()}"
    transports = [
        ZenohTransport(ZenohConfig(listen=(endpoint,))),
        ZenohTransport(ZenohConfig(connect=(endpoint,))),
        ZenohTransport(ZenohConfig(connect=(endpoint,))),
        ZenohTransport(ZenohConfig(connect=(endpoint,))),
    ]
    # Model private sidecar admission (§7) before cold directory replication.
    # Protected path/author/commit checks below remain real, including forgeries.
    owner = OrgFsRuntime(
        tmp_path / "owner", node_id="node-owner", author=f"user:{OWNER}", trust_tailnet_members=True
    )
    alice = OrgFsRuntime(
        tmp_path / "alice", node_id="node-alice", author=f"user:{ALICE}", trust_tailnet_members=True
    )
    bob = OrgFsRuntime(
        tmp_path / "bob", node_id="node-bob", author=f"user:{BOB}", trust_tailnet_members=True
    )
    carol = OrgFsRuntime(
        tmp_path / "carol", node_id="node-carol", author=f"user:{CAROL}", trust_tailnet_members=True
    )
    runtimes = (owner, alice, bob, carol)
    try:
        time.sleep(0.3)
        online = {runtime.node_id for runtime in runtimes}.__contains__
        for runtime, transport in zip(runtimes, transports, strict=True):
            runtime.bind_transport(transport, supplier_online=online)
        space = owner.facade.create_space("group-acme")
        for path in (
            "directory",
            "directory/devices",
            "directory/people",
            "directory/invites",
            "directory/leaves",
        ):
            owner.facade.mkdir(space.space_id, path)
        for member in (ALICE, BOB, CAROL):
            owner.facade.invite(space.space_id, f"user:{member}", "rw")
        owner._mesh(space.space_id).announce()
        for runtime in (alice, bob, carol):
            _eventually(lambda runtime=runtime: runtime.facade.join(space.space_id))
        yield tmp_path, space.space_id, owner, alice, bob, carol
    finally:
        for runtime in reversed(runtimes):
            runtime.close()
        for transport in reversed(transports):
            transport.close()


def test_r4_c_put_broadcast_reaches_the_receiver(replicas) -> None:
    _tmp_path, space_id, owner, alice, _bob, _carol = replicas
    row = _device(
        OWNER,
        "opaque-device",
        address="tcp://10.0.0.7:7447#plaintext-test-psk",
    )
    OrgFsDirectoryStore(owner.facade).put_device("group-acme", row)
    path = directory_device_path(OWNER, row.device_id)
    doc_id = protected_directory_doc_id(
        space_id, path, space_owner=f"user:{OWNER}"
    )
    assert doc_id is not None

    _eventually(
        lambda: _equal(
            [item.device_id for item in OrgFsDirectoryStore(alice.facade).list_devices("group-acme")],
            [row.device_id],
        )
    )


def test_r5_long_protected_path_is_stored_not_refused(replicas) -> None:
    """A protected id past a file-name length is stored: files are named by hash.

    Until 0.5.3 this path was refused with ``protected-doc-id-too-long``
    (a 240-character cap from ``docs/<id>.loro``); real Casdoor names hit it.
    """

    _tmp_path, space_id, owner, _alice, _bob, _carol = replicas
    owner.facade.mkdir(space_id, f"directory/devices/{OWNER}")
    path = f"directory/devices/{OWNER}/{'d' * 200}.json"
    owner.facade.write_text(space_id, path, "{}")
    text, _version = owner.facade.read_text(space_id, path)
    assert text == "{}"


@pytest.fixture
def state_org():
    value = Org()
    try:
        yield value
    finally:
        value.close()


def _state_arrival_order_updates(org: Org) -> tuple[list[Frame], Frame, list[Frame]]:
    space_id = org.space_id
    org.directory(BOB).put_device(ORG, _device(BOB, "bob-device", address="tcp://10.0.0.8:7447#bob"))
    org.deliver()
    mkdir = org.capture(ALICE, lambda: org.fs(ALICE).mkdir(space_id, f"directory/devices/{ALICE}"))
    for frame in mkdir:
        assert org.apply(BOB, frame, peer="node-alice") == Accepted()
    parent = org.node_id(BOB, f"directory/devices/{ALICE}")
    forged = org.forge_rows(
        BOB,
        [new_row("bob-forged-child", parent=parent, name="evil.json",
                 doc_id="doc-00000000-0000-4000-8000-000000000099")],
        author=BOB,
    )
    later = org.capture(ALICE, lambda: org.fs(ALICE).mkdir(space_id, f"directory/devices/{ALICE}/later"))
    return mkdir, forged, later


def _assert_state_alice_tree(org: Org, user: str) -> None:
    assert org.fs(user).stat(org.space_id, f"directory/devices/{ALICE}/later").kind == "dir"
    assert org.missing(user, f"directory/devices/{ALICE}/evil.json")


def _rejection_code(outcome: object) -> str | None:
    return outcome.code if isinstance(outcome, Rejected) else None


def test_r4_a_protected_parent_id_is_judged_before_parent_arrives(state_org: Org) -> None:
    """R4-A: an early forged child cannot fork or wedge Alice's tree."""

    org = state_org
    mkdir, forged, later = _state_arrival_order_updates(org)
    before = org.listing(OWNER)
    early = org.apply(OWNER, forged, peer="node-bob")
    assert not isinstance(early, Accepted)
    assert org.listing(OWNER) == before
    for frame in mkdir:
        assert org.apply(OWNER, frame, peer="node-alice") == Accepted()
    assert _rejection_code(org.apply(OWNER, forged, peer="node-bob")) == "protected-path"

    for frame in mkdir:
        assert org.apply(CAROL, frame, peer="node-alice") == Accepted()
    assert _rejection_code(org.apply(CAROL, forged, peer="node-bob")) == "protected-path"

    for receiver in (OWNER, CAROL):
        for frame in later:
            assert org.apply(receiver, frame, peer="node-alice") == Accepted()
        _assert_state_alice_tree(org, receiver)
    assert org.listing(OWNER) == org.listing(CAROL)


def test_r4_b_protected_id_verdict_does_not_consume_pending_capacity(state_org: Org) -> None:
    """R4-B: waiting for a predecessor stores and counts nothing; the verdict counts once."""

    org = state_org
    mkdir, forged, later = _state_arrival_order_updates(org)
    for frame in mkdir:
        assert org.apply(CAROL, frame, peer="node-alice") == Accepted()

    before = org.listing(OWNER)
    waiting = org.apply(OWNER, forged, peer="node-bob")
    held = org.apply(CAROL, forged, peer="node-bob")
    assert not isinstance(waiting, (Accepted, Rejected))
    assert _rejection_code(held) == "protected-path"
    assert org.listing(OWNER) == before
    assert (org.rejected(OWNER), org.rejected(CAROL)) == (0, 1)

    for frame in mkdir:
        assert org.apply(OWNER, frame, peer="node-alice") == Accepted()
    assert _rejection_code(org.apply(OWNER, forged, peer="node-bob")) == "protected-path"
    assert isinstance(org.apply(CAROL, forged, peer="node-bob"), Rejected)
    assert (org.rejected(OWNER), org.rejected(CAROL)) == (1, 1)
    for receiver in (OWNER, CAROL):
        for frame in later:
            assert org.apply(receiver, frame, peer="node-alice") == Accepted()
        _assert_state_alice_tree(org, receiver)
