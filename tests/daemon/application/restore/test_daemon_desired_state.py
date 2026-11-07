from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.daemon.impl import  (
    DesiredState,
    DesiredStateError,
    DesiredStateStore,
    HarnessLaunchSpec,
    InteractiveSession,
)



def legacy_provider() -> dict[str, object]:
    return {
        "provider": "cc",
        "name": "coordinator",
        "headless": True,
        "cwd": "/workspace",
        "args": ["--model", "sonnet"],
        "ownership": "managed",
    }


def test_desired_state_json_is_never_read_by_the_store(tmp_path: Path) -> None:
    """The store's only input file is the .v1 (Allen's second ruling).

    ``desired-state.json`` is never looked at: on real upgraded machines it
    may hold an obsolete poison pill or a pre-v1 raw provider array -- neither
    is the store's input.  There is no back-compat reading of the canonical
    file.
    """

    legacy_path = tmp_path / "desired-state.json"
    legacy_path.write_text(json.dumps([legacy_provider()]), encoding="utf-8")

    store = DesiredStateStore(legacy_path)

    # Whatever the canonical file holds, the store starts empty and never
    # consumed it: no import, no archive, no state.
    assert store.load() == DesiredState()
    assert not (tmp_path / "desired-state.json.v1").exists()
    assert not list(tmp_path.glob("desired-state.json.migrated-*"))
    assert json.loads(legacy_path.read_text(encoding="utf-8")) == [
        legacy_provider()
    ]


def _rollback_guard_entry() -> dict[str, object]:
    return {
        "provider": "__hyprial_desired_state_schema_v1__",
        "name": "rollback-blocked",
        "note": "desired state uses schema v1; upgrade hyprial before starting the daemon",
    }


def _spec_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "name": "worker",
        "headless": True,
        "args": [],
        "ownership": "managed",
    }
    record.update(overrides)
    return record


def test_from_json_reads_harness_only_record() -> None:
    spec = HarnessLaunchSpec.from_json(
        _spec_record(harness="pi"), "providers[0]"
    )
    assert spec.harness == "pi"


def test_from_json_reads_provider_only_record() -> None:
    spec = HarnessLaunchSpec.from_json(
        _spec_record(provider="codex"), "providers[0]"
    )
    assert spec.harness == "codex"


def test_from_json_prefers_harness_when_both_keys_present() -> None:
    spec = HarnessLaunchSpec.from_json(
        _spec_record(harness="pi", provider="codex"), "providers[0]"
    )
    assert spec.harness == "pi"


def test_to_json_still_writes_provider_key() -> None:
    spec = HarnessLaunchSpec.from_json(
        _spec_record(harness="pi"), "providers[0]"
    )
    record = spec.to_json()
    assert record["provider"] == "pi"
    assert "harness" not in record


def test_harness_key_supports_cc_alias_like_provider_key() -> None:
    spec = HarnessLaunchSpec.from_json(
        _spec_record(harness="cc"), "providers[0]"
    )
    assert spec.harness == "claude"


def test_rollback_guard_entry_is_an_invalid_provider_for_legacy_parsers() -> None:
    # The guard occupies the legacy desired-state.json as a provider-array entry
    # after migration. An older hyprial build parses that file as a provider array,
    # so the entry must be rejected as an invalid harness rather than silently
    # accepted (which would restore an empty provider set). ``from_json`` is that
    # provider-array parser; this is the in-repo proxy for "an old daemon fails
    # loudly" -- the old binary's own behavior is not exercisable from here.
    with pytest.raises(DesiredStateError, match="invalid harness"):
        HarnessLaunchSpec.from_json(_rollback_guard_entry(), "providers[0]")


def test_rollback_guard_without_versioned_state_is_ignored(tmp_path: Path) -> None:
    # A half-migrated / rolled-back home: the guard sits in the legacy path
    # but the schema-v1 sibling is gone.  Allen's second ruling: rollback of
    # old binaries is out of scope, and the store never reads
    # desired-state.json -- so this home simply starts empty.
    legacy_path = tmp_path / "desired-state.json"
    legacy_path.write_text(json.dumps([_rollback_guard_entry()]), encoding="utf-8")
    assert not Path(f"{legacy_path}.v1").exists()

    store = DesiredStateStore(legacy_path)
    assert store.load() == DesiredState()


def test_unknown_or_malformed_desired_state_fails_loudly(tmp_path: Path) -> None:
    legacy_path = tmp_path / "desired-state.json"
    versioned_path = Path(f"{legacy_path}.v1")
    versioned_path.write_text(
        json.dumps({"schemaVersion": 4, "providers": []}), encoding="utf-8"
    )

    with pytest.raises(DesiredStateError, match="unsupported desired-state schema"):
        DesiredStateStore(legacy_path).load()


def test_mailbox_role_and_interactive_registry_round_trip(tmp_path: Path) -> None:
    store = DesiredStateStore(tmp_path / "desired-state.json")
    store.set_mailbox_role(True)
    expected = InteractiveSession(
        actor="agent:allen:mac:coordinator",
        cwd="/workspace/hq",
        command=("codex", "--profile", "coordinator"),
        source="self:codex-tui",
    )

    store.register_interactive(expected)
    store.register_interactive(expected)

    reloaded = store.load()
    assert reloaded.as_mailbox is True
    assert reloaded.interactive_sessions == (expected,)
    assert reloaded.harnesses == ()
    assert reloaded.channel_pins == ()


def test_interactive_channel_telemetry_round_trips_without_schema_bump(
    tmp_path: Path,
) -> None:
    store = DesiredStateStore(tmp_path / "desired-state.json")
    current = InteractiveSession(
        actor="agent:owner:node:coordinator",
        cwd="/workspace/hq",
        command=("claude",),
        source="claude-channel",
        session_ref="session-current",
        runtime="claude_interactive",
        channel_confirmed=True,
        channel_build_version="0.4.1.dev0",
        channel_protocol_version=1,
        owner_fence=True,
    )

    store.register_interactive(current)

    assert store.load().interactive_sessions == (current,)
    document = store.load().to_json()
    assert document["schemaVersion"] == 3
    assert document["interactiveSessions"][0] == {
        "actor": current.actor,
        "cwd": current.cwd,
        "command": ["claude"],
        "source": "claude-channel",
        "sessionRef": "session-current",
        "runtime": "claude_interactive",
        "channelConfirmed": True,
        "channelBuildVersion": "0.4.1.dev0",
        "channelProtocolVersion": 1,
        "ownerFence": True,
    }


def test_legacy_interactive_channel_without_telemetry_remains_readable() -> None:
    legacy = InteractiveSession.from_json(
        {
            "actor": "legacy-coordinator",
            "cwd": "/workspace/hq",
            "command": ["claude"],
            "source": "claude-channel",
            "runtime": "claude_interactive",
            "channelConfirmed": True,
        },
        "interactiveSessions[0]",
    )

    assert legacy.channel_build_version is None
    assert legacy.channel_protocol_version is None
    assert legacy.owner_fence is None


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("channelBuildVersion", "secret value with spaces", "safe package-version"),
        ("channelProtocolVersion", True, "positive integer"),
        ("ownerFence", "yes", "boolean"),
        ("channelLeaseDigest", "not-a-digest", "SHA-256 digest"),
    ],
)
def test_interactive_channel_telemetry_rejects_unsafe_shapes(
    field: str, value: object, match: str
) -> None:
    record: dict[str, object] = {
        "actor": "coordinator",
        "cwd": "/workspace/hq",
        "command": ["claude"],
        "source": "claude-channel",
        field: value,
    }

    with pytest.raises(DesiredStateError, match=match):
        InteractiveSession.from_json(record, "interactiveSessions[0]")


def test_provider_registry_is_keyed_and_only_restores_managed_headless_specs(
    tmp_path: Path,
) -> None:
    store = DesiredStateStore(tmp_path / "desired-state.json")
    first = HarnessLaunchSpec("pi", "squire", True, cwd="/one")
    replacement = HarnessLaunchSpec("pi", "squire", True, cwd="/two")

    store.upsert_harness(first)
    store.upsert_harness(replacement)

    assert store.load().harnesses == (replacement,)
    assert store.remove_harness("pi", "squire").harnesses == ()

    invalid = HarnessLaunchSpec("codex", "tui", False)
    with pytest.raises(DesiredStateError, match="must be headless"):
        store.upsert_harness(invalid)


def test_channel_pins_are_versioned_idempotent_and_survive_store_recreation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "desired-state.json"
    store = DesiredStateStore(path)

    created, previous = store.set_channel_pin("#hyprial-contract", "agent:alice:mac:worker")
    repeated, repeated_previous = store.set_channel_pin(
        "#hyprial-contract", "agent:alice:mac:worker"
    )

    assert previous is None
    assert repeated_previous == "agent:alice:mac:worker"
    assert (
        created.channel_pins
        == repeated.channel_pins
        == (("#hyprial-contract", "agent:alice:mac:worker"),)
    )
    document = DesiredStateStore(path).load().to_json()
    assert document["channelPins"] == {"#hyprial-contract": "agent:alice:mac:worker"}
    # The retired TS-era conversation-pin field is no longer written.
    assert "legacyConversationPins" not in document
    assert document["deprecatedSharedChannels"] == []
    assert DesiredStateStore(path).load().channel_pins == created.channel_pins

    removed, removed_previous = DesiredStateStore(path).remove_channel_pin(
        "#hyprial-contract"
    )
    assert removed_previous == "agent:alice:mac:worker"
    assert removed.channel_pins == ()


def test_malformed_channel_pin_state_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "desired-state.json"
    versioned = Path(f"{path}.v1")
    versioned.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": [],
                "interactiveSessions": [],
                "channelPins": {"#contract": ""},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(DesiredStateError, match="must be a non-empty string"):
        DesiredStateStore(path).load()


def test_zenoh_endpoints_round_trip_and_partial_updates_survive_reload(
    tmp_path: Path,
) -> None:
    path = tmp_path / "desired-state.json"
    store = DesiredStateStore(path)

    # Absent config is the loopback-only default.
    assert store.load().zenoh.listen == ()
    assert store.load().zenoh.connect == ()

    updated = store.update_zenoh_endpoints(
        listen=("tcp/127.0.0.1:7447", "tcp/127.0.0.1:7448"),
        connect=("tcp/100.64.30.3:7447",),
    )
    assert updated.zenoh.listen == ("tcp/127.0.0.1:7447", "tcp/127.0.0.1:7448")
    assert updated.zenoh.connect == ("tcp/100.64.30.3:7447",)

    # Partial update keeps the untouched side.
    partial = store.update_zenoh_endpoints(connect=())
    assert partial.zenoh.listen == ("tcp/127.0.0.1:7447", "tcp/127.0.0.1:7448")
    assert partial.zenoh.connect == ()

    # A fresh store instance (daemon restart) sees the persisted endpoints.
    reloaded = DesiredStateStore(path).load()
    assert reloaded.zenoh.listen == ("tcp/127.0.0.1:7447", "tcp/127.0.0.1:7448")
    assert reloaded.zenoh.connect == ()

    document = DesiredStateStore(path).load().to_json()
    assert document["zenoh"] == {
        "listen": ["tcp/127.0.0.1:7447", "tcp/127.0.0.1:7448"],
        "connect": [],
    }


def test_zenoh_endpoints_reject_invalid_documents(tmp_path: Path) -> None:
    path = tmp_path / "desired-state.json"
    versioned = Path(f"{path}.v1")
    versioned.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": [],
                "interactiveSessions": [],
                "zenoh": {"listen": ["tcp/127.0.0.1:7447", "tcp/127.0.0.1:7447"]},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(DesiredStateError, match="duplicate endpoints"):
        DesiredStateStore(path).load()

    versioned.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": [],
                "interactiveSessions": [],
                "zenoh": {"listen": ["tcp/127.0.0.1:7447", 42]},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(DesiredStateError, match="array of non-empty strings"):
        DesiredStateStore(path).load()
