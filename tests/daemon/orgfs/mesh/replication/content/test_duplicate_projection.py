from __future__ import annotations

from contextlib import contextmanager
import threading
import time

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.network.protocol import (
    ORGFS_SYNC_RETRY_BACKOFF_SECONDS,
)
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.storage.store import StoreError
from hyprial.daemon.impl.transport import ZenohConfig, ZenohTransport

from ..support import (
    _eventually,
    _port,
    isolated_orgfs_environment as isolated_orgfs_environment,
)


def _assert_path(runtime: OrgFsRuntime, space_id: str, path: str) -> str:
    assert runtime.facade.stat(space_id, path).path == path
    return path


def _assert_true(value: bool) -> bool:
    assert value
    return value


def _assert_text(
    runtime: OrgFsRuntime, space_id: str, node_id: str, expected: str
) -> str:
    actual = runtime.facade.read_text(space_id, f"id:{node_id}")[0]
    assert actual == expected
    return actual


def _latest_envelope(
    runtime: OrgFsRuntime, space_id: str, *, doc_id: str | None = None
) -> bytes:
    query = "SELECT envelope_bytes FROM commits"
    parameters: tuple[str, ...] = ()
    if doc_id is not None:
        query += " WHERE doc_id = ?"
        parameters = (doc_id,)
    row = runtime.stores[space_id]._db.execute(
        query + " ORDER BY rowid DESC LIMIT 1", parameters
    ).fetchone()
    assert row is not None
    return bytes(row[0])


@contextmanager
def _joined_pair(tmp_path, *, alice_logger=None):
    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    alice_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    owner = OrgFsRuntime(
        tmp_path / "owner", node_id="node-owner", author="user:owner",
        trust_tailnet_members=True,  # Duplicate projection after private admission (§7).
    )
    alice = OrgFsRuntime(
        tmp_path / "alice",
        node_id="node-alice",
        author="user:alice",
        trust_tailnet_members=True,  # Duplicate projection after private admission (§7).
        logger=alice_logger,
    )
    try:
        time.sleep(0.3)
        online = {"node-owner", "node-alice"}.__contains__
        owner.bind_transport(owner_transport, supplier_online=online)
        alice.bind_transport(
            alice_transport,
            supplier_online=online,
            holder_candidates=lambda: ("node-owner",),
        )
        space = owner.facade.create_space("duplicate-projection")
        owner.facade.invite(space.space_id, "user:alice", "rw")
        owner._mesh(space.space_id).announce()
        _eventually(lambda: alice.facade.join(space.space_id))
        yield owner, alice, space.space_id
    finally:
        alice.close()
        owner.close()
        alice_transport.close()
        owner_transport.close()


def test_duplicate_receive_retries_failed_facade_projection(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _joined_pair(tmp_path) as (owner, alice, space_id):
        with monkeypatch.context() as held:
            held.setattr(owner, "broadcast_pending", lambda _space_id: 0)
            owner.facade.mkdir(space_id, "projtest")
        envelope = _latest_envelope(owner, space_id)
        mesh = alice._meshes[space_id]
        now = [100.0]
        monkeypatch.setattr(mesh, "_projection_clock", lambda: now[0])
        original_apply = alice.facade.apply_envelope
        failed_once = threading.Event()

        def fail_first_projection(
            target_space_id: str, target_envelope: bytes
        ) -> None:
            if target_envelope == envelope and not failed_once.is_set():
                failed_once.set()
                raise RuntimeError("injected facade projection failure")
            original_apply(target_space_id, target_envelope)

        monkeypatch.setattr(alice.facade, "apply_envelope", fail_first_projection)
        assert owner.broadcast_pending(space_id) >= 1
        assert failed_once.wait(3), "alice did not attempt the live facade projection"

        with pytest.raises(OrgFsError) as stale:
            alice.facade.stat(space_id, "projtest")
        assert stale.value.code == "unknown-doc"

        now[0] += ORGFS_SYNC_RETRY_BACKOFF_SECONDS[0]
        retried = mesh.receive(envelope, supplier="node-owner")
        assert retried.status == "applied"
        assert retried.code == "duplicate"
        assert alice.facade.stat(space_id, "projtest").path == "projtest"


def test_duplicate_projection_is_idempotent_and_normal_sync_sends_none(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _joined_pair(tmp_path) as (owner, alice, space_id):
        created = owner.facade.write_text(space_id, "stable.txt", "body")
        _eventually(
            lambda: _assert_text(alice, space_id, created.node_id, "body")
        )
        envelope = _latest_envelope(owner, space_id, doc_id=created.doc_id)
        mesh = alice._meshes[space_id]

        duplicate_results = 0
        original_finish = mesh._finish_receive

        def observe_finish(
            envelope, *, supplier, result, projection_attempted=None
        ):
            nonlocal duplicate_results
            if result.code == "duplicate":
                duplicate_results += 1
            return original_finish(
                envelope,
                supplier=supplier,
                result=result,
                projection_attempted=projection_attempted,
            )

        monkeypatch.setattr(mesh, "_finish_receive", observe_finish)
        assert mesh.sync_from("node-owner") == 0
        assert duplicate_results == 0
        monkeypatch.setattr(mesh, "_finish_receive", original_finish)

        before = alice.facade.stat(space_id, "stable.txt")
        projection_calls = 0
        original_apply = alice.facade.apply_envelope

        def count_apply(target_space_id: str, target_envelope: bytes) -> None:
            nonlocal projection_calls
            projection_calls += 1
            original_apply(target_space_id, target_envelope)

        monkeypatch.setattr(alice.facade, "apply_envelope", count_apply)
        for _ in range(8):
            result = mesh.receive(envelope, supplier="node-owner")
            assert result.status == "applied"
            assert result.code == "duplicate"
        assert projection_calls == 0
        assert alice.facade.stat(space_id, "stable.txt") == before
        print(
            "duplicate projection measurement: "
            f"normal_sync_duplicates={duplicate_results}, "
            f"duplicate_projection_calls={projection_calls}"
        )


def test_poison_projection_does_not_block_other_documents(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str, dict[str, object]]] = []

    def record(level: str, event: str, **fields: object) -> None:
        events.append((level, event, fields))

    with _joined_pair(tmp_path, alice_logger=record) as (
        owner,
        alice,
        space_id,
    ):
        with monkeypatch.context() as held:
            held.setattr(owner, "broadcast_pending", lambda _space_id: 0)
            owner.facade.mkdir(space_id, "poison")
        poison = _latest_envelope(owner, space_id)
        original_apply = alice.facade.apply_envelope
        attempts = 0

        def always_fail_poison(
            target_space_id: str, target_envelope: bytes
        ) -> None:
            nonlocal attempts
            if target_envelope == poison:
                attempts += 1
                raise RuntimeError("permanent projection failure")
            original_apply(target_space_id, target_envelope)

        mesh = alice._meshes[space_id]
        now = [100.0]
        monkeypatch.setattr(mesh, "_projection_clock", lambda: now[0])
        monkeypatch.setattr(alice.facade, "apply_envelope", always_fail_poison)
        assert owner.broadcast_pending(space_id) >= 1
        _eventually(lambda: _assert_true(attempts >= 1))

        now[0] += ORGFS_SYNC_RETRY_BACKOFF_SECONDS[0]
        attempts_before_current = attempts
        current = mesh.receive(poison, supplier="node-owner")
        assert current.code == "duplicate"
        assert attempts == attempts_before_current + 1

        owner.facade.write_text(space_id, "other.txt", "body")
        _eventually(lambda: _assert_path(alice, space_id, "other.txt"))

        repeated = _latest_envelope(owner, space_id)
        attempts_before_receive = attempts
        duplicate = mesh.receive(repeated, supplier="node-owner")
        assert duplicate.code == "duplicate"
        assert attempts == attempts_before_receive

        with monkeypatch.context() as held:
            held.setattr(owner, "broadcast_pending", lambda _space_id: 0)
            owner.facade.write_text(space_id, "from-sync.txt", "body")
        attempts_before_sync = attempts
        assert mesh.sync_from("node-owner") > 0
        assert attempts == attempts_before_sync
        assert alice.facade.stat(space_id, "from-sync.txt").path == "from-sync.txt"

        failures = [
            (level, fields)
            for level, event, fields in events
            if event == "orgfs.projection.failed"
        ]
        expected_failure = (
            "warn",
            {
                "docId": mesh._projection_key(poison)[0],
                "writer": mesh._projection_key(poison)[1],
                "exceptionType": "RuntimeError",
            },
        )
        assert failures == [expected_failure, expected_failure]


def test_projection_failure_attempts_are_bounded_and_store_only_sequences(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str, dict[str, object]]] = []

    def record(level: str, event: str, **fields: object) -> None:
        events.append((level, event, fields))

    with _joined_pair(tmp_path, alice_logger=record) as (
        owner,
        alice,
        space_id,
    ):
        unrelated = _latest_envelope(owner, space_id)
        poisoned: set[bytes] = set()
        with monkeypatch.context() as held:
            held.setattr(owner, "broadcast_pending", lambda _space_id: 0)
            for index in range(30):
                owner.facade.mkdir(space_id, f"poison-{index}")
                poisoned.add(_latest_envelope(owner, space_id))
        assert len(poisoned) == 30

        original_apply = alice.facade.apply_envelope
        attempts = 0

        def fail_poison(
            target_space_id: str, target_envelope: bytes
        ) -> None:
            nonlocal attempts
            if target_envelope in poisoned:
                attempts += 1
                raise RuntimeError("permanent projection failure")
            original_apply(target_space_id, target_envelope)

        monkeypatch.setattr(alice.facade, "apply_envelope", fail_poison)
        mesh = alice._meshes[space_id]
        now = [100.0]
        monkeypatch.setattr(mesh, "_projection_clock", lambda: now[0], raising=False)
        assert mesh.sync_from("node-owner") >= 30
        assert attempts == 30
        retained = tuple(
            value
            for failures in mesh._projection_failures.values()
            for value in failures.values()
        )
        assert len(retained) == 30
        assert all(not isinstance(value, bytes) for value in retained)

        attempt_budget = 1 + len(ORGFS_SYNC_RETRY_BACKOFF_SECONDS)
        assert attempt_budget == 2
        attempts_after_initial = attempts
        for _ in range(8):
            mesh.receive(unrelated, supplier="node-owner")
        assert attempts == attempts_after_initial

        now[0] += ORGFS_SYNC_RETRY_BACKOFF_SECONDS[0]
        mesh.receive(unrelated, supplier="node-owner")
        assert attempts == 30 * attempt_budget
        attempts_after_budget = attempts
        for _ in range(8):
            mesh.receive(unrelated, supplier="node-owner")
        assert attempts == attempts_after_budget

        abandoned = [
            fields
            for level, event, fields in events
            if level == "warn" and event == "orgfs.projection.abandoned"
        ]
        assert len(abandoned) == 30
        assert {fields["attempts"] for fields in abandoned} == {attempt_budget}
        assert all(set(fields) == {"docId", "writer", "seq", "attempts"} for fields in abandoned)

        for index in range(5):
            with monkeypatch.context() as held:
                held.setattr(owner, "broadcast_pending", lambda _space_id: 0)
                owner.facade.mkdir(space_id, f"healthy-{index}")
            healthy = _latest_envelope(owner, space_id)
            now[0] += ORGFS_SYNC_RETRY_BACKOFF_SECONDS[0]
            assert mesh.receive(healthy, supplier="node-owner").status == "applied"

        reset_budget = 1
        bounded_with_reset = 30 * attempt_budget * (1 + reset_budget)
        assert attempts == bounded_with_reset
        abandoned = [
            fields
            for level, event, fields in events
            if level == "warn" and event == "orgfs.projection.abandoned"
        ]
        failed = [
            fields
            for level, event, fields in events
            if level == "warn" and event == "orgfs.projection.failed"
        ]
        assert len(abandoned) == 60
        assert sum(fields.get("final") is True for fields in abandoned) == 30
        assert len(failed) == 31
        total_warns = len(abandoned) + len(failed)
        print(
            "projection failure bound measurement: "
            f"F=30 initial_calls={attempts_after_initial} "
            f"within_backoff_calls={attempts_after_initial - 30} "
            f"pre_reset_calls={attempts_after_budget} "
            f"bounded_total={attempts} "
            f"abandoned_warns={len(abandoned)} "
            f"final_abandoned_warns="
            f"{sum(fields.get('final') is True for fields in abandoned)} "
            f"failed_warns={len(failed)} total_warns={total_warns} "
            f"attempt_budget={attempt_budget}"
        )


def test_abandoned_content_projection_has_one_reset_and_bounded_warnings(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str, dict[str, object]]] = []

    def record(level: str, event: str, **fields: object) -> None:
        events.append((level, event, fields))

    with _joined_pair(tmp_path, alice_logger=record) as (
        owner,
        alice,
        space_id,
    ):
        created = owner.facade.write_text(space_id, "bounded.txt", "A")
        _eventually(lambda: _assert_text(alice, space_id, created.node_id, "A"))
        mesh = alice._meshes[space_id]
        now = [100.0]
        monkeypatch.setattr(mesh, "_projection_clock", lambda: now[0])

        with monkeypatch.context() as held:
            held.setattr(owner, "broadcast_pending", lambda _space_id: 0)
            owner.facade.write_text(space_id, f"id:{created.node_id}", "AB")
        poisoned = _latest_envelope(owner, space_id, doc_id=created.doc_id)
        original_apply = alice.facade.apply_envelope
        poisoned_applies = 0

        def keep_poisoned(
            target_space_id: str, target_envelope: bytes
        ) -> None:
            nonlocal poisoned_applies
            if target_envelope == poisoned:
                poisoned_applies += 1
                raise RuntimeError("permanent content projection failure")
            original_apply(target_space_id, target_envelope)

        monkeypatch.setattr(alice.facade, "apply_envelope", keep_poisoned)
        assert mesh.receive(poisoned, supplier="node-owner").status == "applied"
        now[0] += ORGFS_SYNC_RETRY_BACKOFF_SECONDS[0]
        assert mesh.receive(poisoned, supplier="node-owner").code == "duplicate"

        for index in range(20):
            with monkeypatch.context() as held:
                held.setattr(owner, "broadcast_pending", lambda _space_id: 0)
                owner.facade.write_text(
                    space_id,
                    f"id:{created.node_id}",
                    f"healthy-after-poison-{index}",
                )
            healthy = _latest_envelope(owner, space_id, doc_id=created.doc_id)
            now[0] += ORGFS_SYNC_RETRY_BACKOFF_SECONDS[0]
            assert mesh.receive(healthy, supplier="node-owner").status == "applied"

        attempt_budget = 1 + len(ORGFS_SYNC_RETRY_BACKOFF_SECONDS)
        failed = [
            fields
            for level, event, fields in events
            if level == "warn" and event == "orgfs.projection.failed"
        ]
        abandoned = [
            fields
            for level, event, fields in events
            if level == "warn" and event == "orgfs.projection.abandoned"
        ]
        assert len(failed) == 2
        assert len(abandoned) == 2
        assert sum(fields.get("final") is True for fields in abandoned) == 1
        assert poisoned_applies == attempt_budget * 2
        print(
            "content projection reset measurement: "
            f"writes=20 poisoned_apply_calls={poisoned_applies} "
            f"failed_warns={len(failed)} abandoned_warns={len(abandoned)} "
            f"total_warns={len(failed) + len(abandoned)}"
        )


def test_failed_then_successful_record_in_one_pass_still_warns(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str, dict[str, object]]] = []

    def record(level: str, event: str, **fields: object) -> None:
        events.append((level, event, fields))

    with _joined_pair(tmp_path, alice_logger=record) as (
        owner,
        alice,
        space_id,
    ):
        created = owner.facade.write_text(space_id, "warn-order.txt", "A")
        _eventually(lambda: _assert_text(alice, space_id, created.node_id, "A"))
        with monkeypatch.context() as held:
            held.setattr(owner, "broadcast_pending", lambda _space_id: 0)
            owner.facade.write_text(space_id, f"id:{created.node_id}", "AB")
            failed_envelope = _latest_envelope(
                owner, space_id, doc_id=created.doc_id
            )
            owner.facade.write_text(space_id, f"id:{created.node_id}", "ABC")
            successful_envelope = _latest_envelope(
                owner, space_id, doc_id=created.doc_id
            )

        original_apply = alice.facade.apply_envelope

        def fail_earlier_record(
            target_space_id: str, target_envelope: bytes
        ) -> None:
            if target_envelope == failed_envelope:
                raise RuntimeError("first record fails")
            original_apply(target_space_id, target_envelope)

        monkeypatch.setattr(alice.facade, "apply_envelope", fail_earlier_record)
        mesh = alice._meshes[space_id]
        assert mesh._import_envelope(
            failed_envelope, supplier="node-owner"
        ).status == "applied"
        assert mesh._import_envelope(
            successful_envelope, supplier="node-owner"
        ).status == "applied"
        mesh._queue_projection(successful_envelope, supplier="node-owner")
        mesh._repair_projections()
        failed = [
            fields
            for level, event, fields in events
            if level == "warn" and event == "orgfs.projection.failed"
        ]
        assert len(failed) == 1


def test_later_content_projection_resets_abandoned_predecessor(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _joined_pair(tmp_path) as (owner, alice, space_id):
        created = owner.facade.write_text(space_id, "content.txt", "A")
        _eventually(lambda: _assert_text(alice, space_id, created.node_id, "A"))
        mesh = alice._meshes[space_id]
        now = [100.0]
        monkeypatch.setattr(mesh, "_projection_clock", lambda: now[0], raising=False)

        with monkeypatch.context() as held:
            held.setattr(owner, "broadcast_pending", lambda _space_id: 0)
            owner.facade.write_text(space_id, f"id:{created.node_id}", "AB")
        update_ab = _latest_envelope(owner, space_id, doc_id=created.doc_id)
        original_apply = alice.facade.apply_envelope
        failed_calls = 0

        def fail_ab_twice(target_space_id: str, target_envelope: bytes) -> None:
            nonlocal failed_calls
            if target_envelope == update_ab and failed_calls < 2:
                failed_calls += 1
                raise RuntimeError("transient content projection failure")
            original_apply(target_space_id, target_envelope)

        monkeypatch.setattr(alice.facade, "apply_envelope", fail_ab_twice)
        first = mesh.receive(update_ab, supplier="node-owner")
        assert first.status == "applied"
        assert failed_calls == 1

        duplicate = mesh.receive(update_ab, supplier="node-owner")
        assert duplicate.code == "duplicate"
        assert failed_calls == 1

        now[0] += ORGFS_SYNC_RETRY_BACKOFF_SECONDS[0]
        duplicate = mesh.receive(update_ab, supplier="node-owner")
        assert duplicate.code == "duplicate"
        assert failed_calls == 2

        with monkeypatch.context() as held:
            held.setattr(owner, "broadcast_pending", lambda _space_id: 0)
            owner.facade.write_text(space_id, f"id:{created.node_id}", "ABC")
        update_abc = _latest_envelope(owner, space_id, doc_id=created.doc_id)
        latest = mesh.receive(update_abc, supplier="node-owner")
        assert latest.status == "applied"
        assert alice.facade.read_text(space_id, f"id:{created.node_id}")[0] == "ABC"


def test_restart_rebuilds_permanently_poisoned_content_projection(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    alice_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    owner = OrgFsRuntime(
        tmp_path / "owner-restart", node_id="node-owner", author="user:owner",
        trust_tailnet_members=True,  # Restart recovery on the admitted private path (§7).
    )
    alice_root = tmp_path / "alice-restart"
    alice = OrgFsRuntime(
        alice_root, node_id="node-alice", author="user:alice",
        trust_tailnet_members=True,  # Restart recovery on the admitted private path (§7).
    )
    restarted: OrgFsRuntime | None = None
    restarted_transport: ZenohTransport | None = None
    try:
        time.sleep(0.3)
        online = {"node-owner", "node-alice"}.__contains__
        owner.bind_transport(owner_transport, supplier_online=online)
        alice.bind_transport(
            alice_transport,
            supplier_online=online,
            holder_candidates=lambda: ("node-owner",),
        )
        space = owner.facade.create_space("projection-restart")
        owner.facade.invite(space.space_id, "user:alice", "rw")
        owner._mesh(space.space_id).announce()
        _eventually(lambda: alice.facade.join(space.space_id))
        created = owner.facade.write_text(space.space_id, "restart.txt", "A")
        _eventually(
            lambda: _assert_text(alice, space.space_id, created.node_id, "A")
        )

        with monkeypatch.context() as held:
            held.setattr(owner, "broadcast_pending", lambda _space_id: 0)
            owner.facade.write_text(
                space.space_id, f"id:{created.node_id}", "AB"
            )
        poisoned = _latest_envelope(
            owner, space.space_id, doc_id=created.doc_id
        )
        required = _latest_envelope(owner, space.space_id)
        original_apply = alice.facade.apply_envelope

        def keep_poisoned(
            target_space_id: str, target_envelope: bytes
        ) -> None:
            if target_envelope == poisoned:
                raise RuntimeError("permanent predecessor projection failure")
            original_apply(target_space_id, target_envelope)

        mesh = alice._meshes[space.space_id]
        now = [100.0]
        monkeypatch.setattr(mesh, "_projection_clock", lambda: now[0])
        monkeypatch.setattr(alice.facade, "apply_envelope", keep_poisoned)
        assert mesh.receive(poisoned, supplier="node-owner").status == "applied"
        assert mesh.receive(required, supplier="node-owner").status == "applied"
        now[0] += ORGFS_SYNC_RETRY_BACKOFF_SECONDS[0]
        assert mesh.receive(poisoned, supplier="node-owner").code == "duplicate"

        with monkeypatch.context() as held:
            held.setattr(owner, "broadcast_pending", lambda _space_id: 0)
            owner.facade.write_text(
                space.space_id, f"id:{created.node_id}", "ABC"
            )
        healthy = _latest_envelope(
            owner, space.space_id, doc_id=created.doc_id
        )
        required = _latest_envelope(owner, space.space_id)
        now[0] += ORGFS_SYNC_RETRY_BACKOFF_SECONDS[0]
        assert mesh.receive(healthy, supplier="node-owner").status == "applied"
        assert mesh.receive(required, supplier="node-owner").status == "applied"
        now[0] += ORGFS_SYNC_RETRY_BACKOFF_SECONDS[0]
        assert mesh.receive(healthy, supplier="node-owner").code == "duplicate"

        with pytest.raises(OrgFsError) as pending:
            alice.facade.read_text(space.space_id, f"id:{created.node_id}")
        assert pending.value.code == "content-pending"

        alice.close()
        restarted_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
        restarted = OrgFsRuntime(
            alice_root, node_id="node-alice", author="user:alice",
            trust_tailnet_members=True,  # Same private-path fixture after restart (§7).
        )
        restarted.bind_transport(
            restarted_transport,
            supplier_online=online,
            holder_candidates=lambda: ("node-owner",),
        )
        assert restarted.facade.read_text(
            space.space_id, f"id:{created.node_id}"
        )[0] == "ABC"
    finally:
        if restarted is not None:
            restarted.close()
        alice.close()
        owner.close()
        if restarted_transport is not None:
            restarted_transport.close()
        alice_transport.close()
        owner_transport.close()


def test_missing_failed_record_does_not_block_later_same_key_projection(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str, dict[str, object]]] = []

    def record(level: str, event: str, **fields: object) -> None:
        events.append((level, event, fields))

    with _joined_pair(tmp_path, alice_logger=record) as (
        owner,
        alice,
        space_id,
    ):
        created = owner.facade.write_text(space_id, "missing.txt", "A")
        _eventually(lambda: _assert_text(alice, space_id, created.node_id, "A"))
        mesh = alice._meshes[space_id]
        now = [100.0]
        monkeypatch.setattr(mesh, "_projection_clock", lambda: now[0], raising=False)

        with monkeypatch.context() as held:
            held.setattr(owner, "broadcast_pending", lambda _space_id: 0)
            owner.facade.write_text(space_id, f"id:{created.node_id}", "AB")
        update_ab = _latest_envelope(owner, space_id, doc_id=created.doc_id)
        original_apply = alice.facade.apply_envelope
        projected: list[bytes] = []

        def fail_ab_once(target_space_id: str, target_envelope: bytes) -> None:
            if target_envelope == update_ab and update_ab not in projected:
                projected.append(target_envelope)
                raise RuntimeError("retain missing projection")
            projected.append(target_envelope)
            original_apply(target_space_id, target_envelope)

        monkeypatch.setattr(alice.facade, "apply_envelope", fail_ab_once)
        assert mesh.receive(update_ab, supplier="node-owner").status == "applied"
        failed_key = mesh._projection_key(update_ab)[:2]
        failed_seq = mesh._projection_key(update_ab)[2]
        now[0] += ORGFS_SYNC_RETRY_BACKOFF_SECONDS[0]

        original_failed_record = mesh._failed_projection_record

        def miss_failed_record(doc_id: str, writer: str, seq: int) -> bytes:
            if (doc_id, writer) == failed_key and seq == failed_seq:
                raise StoreError("projection-behind", "forced missing record")
            return original_failed_record(doc_id, writer, seq)

        monkeypatch.setattr(mesh, "_failed_projection_record", miss_failed_record)
        with monkeypatch.context() as held:
            held.setattr(owner, "broadcast_pending", lambda _space_id: 0)
            owner.facade.write_text(space_id, f"id:{created.node_id}", "ABC")
        update_abc = _latest_envelope(owner, space_id, doc_id=created.doc_id)
        assert mesh.receive(update_abc, supplier="node-owner").status == "applied"
        assert update_abc in projected
        assert failed_seq not in mesh._projection_failures.get(failed_key, {})
        assert any(
            event == "orgfs.projection.abandoned"
            and fields.get("seq") == failed_seq
            for _level, event, fields in events
        )


def test_successful_same_key_projection_clears_failure_warning_dedup(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str, dict[str, object]]] = []

    def record(level: str, event: str, **fields: object) -> None:
        events.append((level, event, fields))

    with _joined_pair(tmp_path, alice_logger=record) as (
        owner,
        alice,
        space_id,
    ):
        created = owner.facade.write_text(space_id, "warn.txt", "A")
        _eventually(lambda: _assert_text(alice, space_id, created.node_id, "A"))
        mesh = alice._meshes[space_id]
        now = [100.0]
        monkeypatch.setattr(mesh, "_projection_clock", lambda: now[0], raising=False)

        with monkeypatch.context() as held:
            held.setattr(owner, "broadcast_pending", lambda _space_id: 0)
            owner.facade.write_text(space_id, f"id:{created.node_id}", "AB")
        update_ab = _latest_envelope(owner, space_id, doc_id=created.doc_id)
        original_apply = alice.facade.apply_envelope

        def keep_ab_poisoned(target_space_id: str, target_envelope: bytes) -> None:
            if target_envelope == update_ab:
                raise RuntimeError("persistent predecessor failure")
            original_apply(target_space_id, target_envelope)

        monkeypatch.setattr(alice.facade, "apply_envelope", keep_ab_poisoned)
        assert mesh.receive(update_ab, supplier="node-owner").status == "applied"
        now[0] += ORGFS_SYNC_RETRY_BACKOFF_SECONDS[0]
        assert mesh.receive(update_ab, supplier="node-owner").code == "duplicate"

        with monkeypatch.context() as held:
            held.setattr(owner, "broadcast_pending", lambda _space_id: 0)
            owner.facade.write_text(space_id, f"id:{created.node_id}", "ABC")
        update_abc = _latest_envelope(owner, space_id, doc_id=created.doc_id)
        assert mesh.receive(update_abc, supplier="node-owner").status == "applied"

        failed_events = [
            fields
            for level, event, fields in events
            if level == "warn" and event == "orgfs.projection.failed"
        ]
        assert len(failed_events) == 2


def test_existing_projection_frontiers_are_initialized_before_receive(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = f"tcp/127.0.0.1:{_port()}"
    transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    runtime = OrgFsRuntime(
        tmp_path / "existing", node_id="node-existing", author="user:existing"
    )
    try:
        space = runtime.facade.create_space("existing-projection")
        runtime.facade.write_text(space.space_id, "stable.txt", "body")
        envelope = _latest_envelope(runtime, space.space_id)
        runtime.bind_transport(transport, supplier_online=lambda _supplier: True)
        mesh = runtime._meshes[space.space_id]
        mesh._supplier_owner = {"node-peer": "user:existing"}.get
        projection_calls = 0
        original_apply = runtime.facade.apply_envelope

        def count_apply(target_space_id: str, target_envelope: bytes) -> None:
            nonlocal projection_calls
            projection_calls += 1
            original_apply(target_space_id, target_envelope)

        monkeypatch.setattr(runtime.facade, "apply_envelope", count_apply)
        result = mesh.receive(envelope, supplier="node-peer")
        assert result.code == "duplicate"
        assert projection_calls == 0
        assert runtime.facade.stat(space.space_id, "stable.txt").path == "stable.txt"
    finally:
        runtime.close()
        transport.close()


def test_projection_log_read_failure_does_not_escape_receive(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _joined_pair(tmp_path) as (owner, alice, space_id):
        with monkeypatch.context() as held:
            held.setattr(owner, "broadcast_pending", lambda _space_id: 0)
            owner.facade.mkdir(space_id, "poison-log-read")
        poison = _latest_envelope(owner, space_id)
        mesh = alice._meshes[space_id]
        now = [100.0]
        monkeypatch.setattr(mesh, "_projection_clock", lambda: now[0])
        original_apply = alice.facade.apply_envelope

        def fail_poison(
            target_space_id: str, target_envelope: bytes
        ) -> None:
            if target_envelope == poison:
                raise RuntimeError("retain failed projection")
            original_apply(target_space_id, target_envelope)

        monkeypatch.setattr(alice.facade, "apply_envelope", fail_poison)
        assert owner.broadcast_pending(space_id) >= 1
        _eventually(
            lambda: _assert_true(bool(mesh._projection_failures))
        )
        now[0] += ORGFS_SYNC_RETRY_BACKOFF_SECONDS[0]
        poison_key = mesh._projection_key(poison)[:2]
        unrelated = _latest_envelope(owner, space_id, doc_id="meta")
        original_log_range = mesh._log_range

        def fail_log_range(doc_id, writer, *, after, limit):
            if (doc_id, writer) == poison_key:
                raise RuntimeError("injected durable log read failure")
            return original_log_range(
                doc_id, writer, after=after, limit=limit
            )

        monkeypatch.setattr(mesh, "_log_range", fail_log_range)
        result = mesh.receive(unrelated, supplier="node-owner")
        assert result.code == "duplicate"
