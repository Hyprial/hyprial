from dataclasses import replace

import pytest

from hyprial.daemon.impl.desired_state.store import DesiredStateStore
from hyprial.kernel import DesiredStateError, HarnessLaunchSpec


def candidate(model="hy4-flash"):
    return {
        "authKind": "connection",
        "connectionRef": "connection-1",
        "configRevision": 1,
        "model": model,
        "harness": "pi",
        "mode": "headless",
        "intentDigest": "a" * 64,
    }


def prepare(
    store,
    operation="op-1",
    revision=0,
    *,
    agent="a.fixture",
    member="member-1",
    model="hy4-flash",
    **kwargs,
):
    return store.model_binding_store.prepare(
        agent, member, operation, revision, candidate(model), **kwargs
    )


def test_prepare_before_first_launch_and_finalize_once(tmp_path):
    store = DesiredStateStore(tmp_path / "desired-state.json")
    assert store.load().schema_version == 3
    first = prepare(store)
    assert first.phase == "prepared"
    assert store.load().harnesses == ()
    assert store.load().schema_version == 4
    done = store.model_binding_store.finalize(
        "a.fixture", "member-1", "op-1", first.request_digest, 0
    )
    assert done.result_revision == 1
    assert prepare(DesiredStateStore(store.legacy_path)) == done
    assert (
        store.model_binding_store.status(
            "a.fixture", "member-1"
        ).binding.binding_revision
        == 1
    )
    assert store.published_state() == store.load()


def test_stale_whole_document_cannot_erase_prepared(tmp_path):
    store = DesiredStateStore(tmp_path / "desired-state.json")
    stale = store.load()
    prepare(store)
    with pytest.raises(DesiredStateError):
        store.save(replace(stale, as_mailbox=True))
    assert (
        store.model_binding_store.status("a.fixture", "member-1").pending.operation_id
        == "op-1"
    )


def test_priority_and_terminal_digest_persist(tmp_path):
    store = DesiredStateStore(tmp_path / "desired-state.json")
    first = prepare(store)
    with pytest.raises(DesiredStateError, match="operation-digest-mismatch"):
        prepare(store, model="different")
    with pytest.raises(DesiredStateError, match="operation-in-progress"):
        prepare(store, operation="other", revision=9, operation_limit=None)
    cancelled = store.model_binding_store.cancel(
        "a.fixture", "member-1", "op-1", first.request_digest, 0
    )
    assert cancelled.phase == "cancelled"
    assert (
        prepare(DesiredStateStore(store.legacy_path), operation_limit=None) == cancelled
    )
    with pytest.raises(DesiredStateError, match="revision-mismatch"):
        prepare(store, operation="new", revision=9, operation_limit=None)
    with pytest.raises(DesiredStateError, match="operation-limit-setting"):
        prepare(store, operation="new", operation_limit=None)


def test_cleanup_blocks_all_members_even_without_binding(tmp_path):
    store = DesiredStateStore(tmp_path / "desired-state.json")
    api = store.model_binding_store
    api.mark_cleanup("a.fixture", "entity-1", "destroy-1")
    for member in ["member-1", "member-2"]:
        with pytest.raises(DesiredStateError, match="destroy-cleanup-pending"):
            prepare(store, member=member)
    api.mark_cleanup("a.fixture", "entity-1", "destroy-2", expected_attempt="destroy-1")
    with pytest.raises(DesiredStateError):
        api.purge_destroyed_agent("a.fixture", "entity-1", "destroy-1")
    prepare(store, agent="a.new-incarnation")
    api.purge_destroyed_agent("a.fixture", "entity-1", "destroy-2")
    assert api.status("a.new-incarnation", "member-1").pending is not None
    assert store.load().schema_version == 4


def test_schema_four_roundtrip_and_no_downgrade_after_empty_purge(tmp_path):
    store = DesiredStateStore(tmp_path / "desired-state.json")
    api = store.model_binding_store
    api.mark_cleanup("a.fixture", "entity-1", "destroy-1")
    api.purge_destroyed_agent("a.fixture", "entity-1", "destroy-1")
    state = DesiredStateStore(store.legacy_path).load()
    assert state.schema_version == 4
    payload = state.to_json()
    for key in ["modelBindings", "modelBindingOperations", "modelAgentLifecycle"]:
        assert payload[key] == []
    assert type(state).from_json(payload) == state
    payload["modelUnknown"] = []
    with pytest.raises(DesiredStateError):
        type(state).from_json(payload)


def test_cleanup_prevents_finalize_and_rollback_is_attempt_fenced(tmp_path):
    store = DesiredStateStore(tmp_path / "desired-state.json")
    first = prepare(store)
    api = store.model_binding_store
    api.mark_cleanup("a.fixture", "entity-1", "destroy-1")
    with pytest.raises(DesiredStateError, match="destroy-cleanup-pending"):
        api.finalize("a.fixture", "member-1", "op-1", first.request_digest, 0)
    with pytest.raises(DesiredStateError, match="destroy-cleanup-pending"):
        api.cancel("a.fixture", "member-1", "op-1", first.request_digest, 0)
    api.rollback_cleanup("a.fixture", "entity-1", "destroy-1")
    api.finalize("a.fixture", "member-1", "op-1", first.request_digest, 0)
    with pytest.raises(DesiredStateError, match="operation-already-completed"):
        api.cancel("a.fixture", "member-1", "op-1", first.request_digest, 0)


def test_stale_json_import_does_not_erase_bindings(tmp_path):
    import json

    store = DesiredStateStore(tmp_path / "desired-state.json")
    legacy = store.load().to_json()
    prepare(store)
    store.versioned_path.write_text(json.dumps(legacy))
    with pytest.raises(DesiredStateError):
        DesiredStateStore(store.legacy_path)
    assert store.load().schema_version == 4


def test_successful_launch_reference_roundtrips_without_replacing_configuration(
    tmp_path,
):
    store = DesiredStateStore(tmp_path / "desired-state.json")
    first = prepare(store)
    store.model_binding_store.finalize(
        "a.fixture", "member-1", "op-1", first.request_digest, 0
    )
    spec = HarnessLaunchSpec(
        "pi",
        "display",
        True,
        member="member-1",
        model_binding={"bindingRef": first.binding_ref, "bindingRevision": 1},
    )
    state = store.load()
    store.save(replace(state, harnesses=(spec,)))
    assert store.load().harnesses[0].model_binding == spec.model_binding
    assert store.load().model_bindings == state.model_bindings
    assert HarnessLaunchSpec.from_json(spec.to_json(), "spec") == spec
    with pytest.raises(DesiredStateError):
        type(state)(harnesses=(spec,)).to_json()
