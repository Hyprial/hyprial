"""Adapter migration is a schema boundary, not a worker-kind exception."""
import json

import pytest

from hyprial.daemon.impl.desired_state import DesiredState, DesiredStateStore
from hyprial.kernel import DECLARED_HARNESSES, DesiredStateError


def legacy_document(version=1):
    return {
        "schemaVersion": version,
        "providers": [
            {"provider": "pi", "name": "X", "headless": True},
            {"provider": "lark", "name": "X", "headless": True,
             "pinnedOwner": "old-owner", "nickname": "old-adapter"},
            {"provider": "codex", "name": "Y", "headless": True},
            {"provider": "lark", "name": "other", "headless": True},
        ],
        "interactiveSessions": [],
        "channelPins": {"X": "agent:owner:node:X"},
    }


@pytest.mark.parametrize("version", [1, 2])
def test_migration_separates_same_named_adapter_and_worker(version):
    state = DesiredState.from_json(legacy_document(version))
    assert state.schema_version == 3
    assert [(s.harness, s.name) for s in state.harnesses] == [("pi", "X"), ("codex", "Y")]
    assert [(s.provider, s.name) for s in state.adapters] == [("lark", "X"), ("lark", "other")]
    assert all(not hasattr(s, "harness") for s in state.adapters)
    assert state.channel_pins == (("X", "agent:owner:node:X"),)
    assert state.adapters[0].to_json()["legacyLaunch"]["pinnedOwner"] == "old-owner"
    assert DesiredState.from_json(state.to_json()) == state
    assert all(s.harness in DECLARED_HARNESSES for s in state.harnesses)


@pytest.mark.parametrize("headless", [None, False])
def test_legacy_adapter_preserves_implicit_headless_semantics(headless):
    row = {"provider": "lark", "name": "legacy"}
    if headless is not None:
        row["headless"] = headless
    state = DesiredState.from_json({"schemaVersion": 1, "providers": [row], "interactiveSessions": []})
    assert state.harnesses == ()
    assert state.adapters[0].to_json()["legacyLaunch"]["headless"] is True


def test_legacy_file_migration_is_durable_and_idempotent(tmp_path):
    path = tmp_path / "desired-state.json"
    path.with_suffix(".json.v1").write_text(json.dumps(legacy_document()))
    store = DesiredStateStore(path)
    state = store.load()
    assert len(state.adapters) == 2
    assert store.harness_spec("X").harness == "pi"
    assert store.harness_spec("Y").harness == "codex"
    assert store._sqlite_shadow.read_document()["schemaVersion"] == 3
    assert len(list(tmp_path.glob("*.migrated-*"))) == 1
    assert DesiredStateStore(path).load() == state


@pytest.mark.parametrize("version", [0, 4, True, "3"])
def test_unknown_schema_fails_loudly(version):
    with pytest.raises(DesiredStateError, match="schema version"):
        DesiredState.from_json(legacy_document(version))


def test_v3_does_not_accept_adapter_in_worker_collection():
    with pytest.raises(DesiredStateError):
        DesiredState.from_json(legacy_document(3) | {"adapters": []})


def test_malformed_adapters_are_not_dropped():
    for adapters in (None, {}, [None], [{"provider": "unknown", "name": "X"}],
                     [{"provider": "lark", "name": "X", "status": []}],
                     [{"provider": "lark", "name": "X", "harness": "lark"}]):
        with pytest.raises(DesiredStateError):
            DesiredState.from_json({"schemaVersion": 3, "providers": [],
                                    "interactiveSessions": [], "adapters": adapters})
