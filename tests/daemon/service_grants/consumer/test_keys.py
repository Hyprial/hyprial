"""Red tests for the Key half of a received service grant.

Decision (service grant S5, 2026-11-26): the credential is opened only when
``can_open_grant_credential`` holds, through identity's ``open_service_grant``
with structured fields and the grantor taken from the document path. The
plaintext becomes one user-provider entry whose id is fixed by the grant id.
This slice never overwrites an entry it did not write and never deletes one
it did not write: the ownership proof is the private ledger described in
``keys.py``. Plaintext never reaches logs, events, exceptions or return
values.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.service_grants.consumer import LEDGER_RELPATH, GrantConsumer, entry_id_for_grant
from hyprial.identity import read_user_provider_value
from tests.daemon.service_grants.consumer.support import (
    KEY_FIELD,
    ORG,
    PORT,
    SECRET,
    Clock,
    Recorder,
    as_author,
    build_directory,
    device_row,
    grantor,
    provider_ids,
    providers_dir,
    write_user_provider,
)


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Any]:
    directory = build_directory(tmp_path)
    world = {**directory, "tmp": tmp_path, "clock": Clock(), "events": Recorder()}
    yield world
    directory["runtime"].close()


def consumer(world: dict[str, Any], **overrides: Any) -> GrantConsumer:
    arguments: dict[str, Any] = {
        "orgfs": world["runtime"].facade,
        "home": world["bob_home"],
        "state_dir": world["bob_state"],
        "owner": "bob",
        "device_id": "device-b",
        "now_ms": world["clock"],
        "logger": world["events"],
    }
    arguments.update(overrides)
    return GrantConsumer(**arguments)


def grant_key(world: dict[str, Any], **extra: Any) -> str:
    made = grantor(world, world["clock"]).grant(
        org=ORG, port=PORT, device="bob/device-b", name="magpie",
        credential={"entryId": "gateway", "fieldName": "API_KEY"}, **extra,
    )
    return made["grant"]["grantId"]


def test_opened_key_is_written_once_and_read_back_through_identity(world: dict[str, Any]) -> None:
    grant_id = grant_key(world)
    entry_id = entry_id_for_grant(grant_id)
    source = consumer(world)

    source.refresh()

    assert provider_ids(world["bob_home"]) == [entry_id]
    assert read_user_provider_value(world["bob_home"], entry_id, KEY_FIELD) == SECRET
    mode = (providers_dir(world["bob_home"]) / f"{entry_id}.json").stat().st_mode & 0o777
    assert mode == 0o600
    [received] = world["events"].of("service.grant.received")
    assert received == {"org": ORG, "grantId": grant_id, "grantor": "alice", "entryId": entry_id}
    assert SECRET not in world["events"].text()


def test_ciphertext_sealed_to_another_device_is_never_opened(world: dict[str, Any]) -> None:
    made = grantor(world, world["clock"]).grant(
        org=ORG, port=PORT, device="carol/device-c", name="magpie",
        credential={"entryId": "gateway", "fieldName": "API_KEY"},
    )
    raw = world["store"].read_service_grant(ORG, "alice", made["grant"]["grantId"])
    assert raw is not None
    with as_author(world["runtime"], "alice"):
        world["store"].put_service_grant(
            ORG, "alice", made["grant"]["grantId"], {**raw, "recipient": {"owner": "bob", "deviceId": "device-b"}}
        )
    source = consumer(world)

    source.refresh()

    assert provider_ids(world["bob_home"]) == []
    assert source.entries()
    [rejected] = world["events"].of("service.grant.rejected")
    assert (rejected["grantId"], rejected["reason"]) == (made["grant"]["grantId"], "credential-unopenable")
    assert SECRET not in world["events"].text()


def test_copied_ciphertext_under_another_grantor_pointing_at_their_device_is_refused(world: dict[str, Any]) -> None:
    """Alice's ciphertext for this device, re-published by mallory for mallory's own service."""

    grant_id = grant_key(world)
    raw = world["store"].read_service_grant(ORG, "alice", grant_id)
    assert raw is not None
    space_id = next(item.space_id for item in world["runtime"].facade.spaces() if item.name == ORG)
    world["runtime"].facade.invite(space_id, "user:mallory", "rw")
    with as_author(world["runtime"], "mallory"):
        world["store"].put_device(ORG, device_row("device-m", "mallory"))
        world["store"].put_service_grant(
            ORG, "mallory", grant_id, {**raw, "service": {**raw["service"], "deviceId": "device-m"}}
        )
    source = consumer(world)

    source.refresh()

    assert provider_ids(world["bob_home"]) == []
    rejected = [row for row in world["events"].of("service.grant.rejected") if row["grantId"] == grant_id]
    assert [(row["grantor"], row["reason"]) for row in rejected] == [("mallory", "credential-unopenable")]
    assert world["events"].of("service.grant.received") == []
    assert SECRET not in world["events"].text()
    assert read_user_provider_value(world["alice_home"], "gateway", "API_KEY") == SECRET


def test_users_own_entry_with_the_same_id_is_never_overwritten_or_deleted(world: dict[str, Any]) -> None:
    grant_id = grant_key(world)
    entry_id = entry_id_for_grant(grant_id)
    owned = write_user_provider(world["bob_home"], entry_id, {"KEY": "SYNTHETIC-USER-OWNED"})
    before = owned.read_bytes()
    source = consumer(world)

    source.refresh()

    assert owned.read_bytes() == before
    assert read_user_provider_value(world["bob_home"], entry_id, "KEY") == "SYNTHETIC-USER-OWNED"
    assert source.entries()
    [rejected] = world["events"].of("service.grant.rejected")
    assert (rejected["grantId"], rejected["reason"]) == (grant_id, "entry-exists")

    grantor(world, world["clock"]).revoke(org=ORG, grant_id=grant_id)
    source.refresh()

    assert owned.read_bytes() == before
    assert provider_ids(world["bob_home"]) == [entry_id]


def test_revoked_or_expired_grant_deletes_the_entry_this_slice_wrote(world: dict[str, Any]) -> None:
    grant_id = grant_key(world, expires_at_ms=world["clock"].value + 120_000)
    entry_id = entry_id_for_grant(grant_id)
    alice = grantor(world, world["clock"])
    source = consumer(world)
    source.refresh()
    assert provider_ids(world["bob_home"]) == [entry_id]

    alice.revoke(org=ORG, grant_id=grant_id)
    source.refresh()

    assert provider_ids(world["bob_home"]) == []
    second = grantor(world, world["clock"]).grant(
        org=ORG, port=PORT, device="bob/device-b", name="magpie",
        credential={"entryId": "gateway", "fieldName": "API_KEY"},
        expires_at_ms=world["clock"].value + 120_000,
    )["grant"]["grantId"]
    source.refresh()
    assert provider_ids(world["bob_home"]) == [entry_id_for_grant(second)]
    world["clock"].value += 121_000
    source.refresh()

    assert provider_ids(world["bob_home"]) == []


def test_clearing_the_credential_deletes_the_key_and_keeps_the_connection(world: dict[str, Any]) -> None:
    grant_id = grant_key(world)
    entry_id = entry_id_for_grant(grant_id)
    raw = world["store"].read_service_grant(ORG, "alice", grant_id)
    assert raw is not None
    source = consumer(world)
    source.refresh()
    assert provider_ids(world["bob_home"]) == [entry_id]

    with as_author(world["runtime"], "alice"):
        world["store"].put_service_grant(ORG, "alice", grant_id, {**raw, "credential": None})
    source.refresh()

    assert provider_ids(world["bob_home"]) == []
    assert source.entries()
    assert not source.owns_entry(entry_id)


def test_entry_edited_by_the_user_is_not_deleted_on_withdrawal(world: dict[str, Any]) -> None:
    grant_id = grant_key(world)
    entry_id = entry_id_for_grant(grant_id)
    alice = grantor(world, world["clock"])
    source = consumer(world)
    source.refresh()
    path = providers_dir(world["bob_home"]) / f"{entry_id}.json"
    edited = b'{"KEY": "SYNTHETIC-USER-EDITED"}\n'
    path.write_bytes(edited)
    path.chmod(0o600)

    alice.revoke(org=ORG, grant_id=grant_id)
    source.refresh()

    assert path.read_bytes() == edited
    assert not source.owns_entry(entry_id)


def test_restart_does_not_write_again_or_add_a_second_entry(world: dict[str, Any]) -> None:
    grant_id = grant_key(world)
    entry_id = entry_id_for_grant(grant_id)
    source = consumer(world)
    source.refresh()
    path = providers_dir(world["bob_home"]) / f"{entry_id}.json"
    stat_before = path.stat()
    events_before = len(world["events"].events)

    restarted = consumer(world)
    restarted.refresh()

    assert provider_ids(world["bob_home"]) == [entry_id]
    stat_after = path.stat()
    assert (stat_after.st_mtime_ns, stat_after.st_ino, stat_after.st_size) == (
        stat_before.st_mtime_ns, stat_before.st_ino, stat_before.st_size
    )
    assert world["events"].names()[events_before:].count("service.grant.received") == 0
    assert read_user_provider_value(world["bob_home"], entry_id, KEY_FIELD) == SECRET


def test_unchanged_ticks_never_touch_the_entry(world: dict[str, Any]) -> None:
    grant_key(world)
    source = consumer(world)
    source.refresh()
    [path] = providers_dir(world["bob_home"]).glob("*.json")
    stat_before = path.stat()

    for _ in range(5):
        source.refresh()

    stat_after = path.stat()
    assert (stat_after.st_mtime_ns, stat_after.st_ino) == (stat_before.st_mtime_ns, stat_before.st_ino)


def test_missing_entry_is_restored_and_ledger_loss_leaves_entries_alone(world: dict[str, Any]) -> None:
    grant_id = grant_key(world)
    entry_id = entry_id_for_grant(grant_id)
    source = consumer(world)
    source.refresh()
    path = providers_dir(world["bob_home"]) / f"{entry_id}.json"
    path.unlink()

    source.refresh()

    assert read_user_provider_value(world["bob_home"], entry_id, KEY_FIELD) == SECRET

    (world["bob_home"] / LEDGER_RELPATH).unlink()
    orphan = consumer(world)
    assert not orphan.owns_entry(entry_id)
    grantor(world, world["clock"]).revoke(org=ORG, grant_id=grant_id)
    orphan.refresh()

    assert provider_ids(world["bob_home"]) == [entry_id]
    assert not orphan.owns_entry(entry_id)


def test_plaintext_from_a_failed_open_never_leaves_the_consumer(world: dict[str, Any]) -> None:
    grant_id = grant_key(world)
    calls: list[str] = []

    def failing(stored: object) -> bytes:
        calls.append(stored.grant.grant_id)
        raise RuntimeError(f"synthetic failure containing {SECRET}")

    source = consumer(world, open_credential=failing)

    source.refresh()

    assert calls == [grant_id]
    assert provider_ids(world["bob_home"]) == []
    assert [row["reason"] for row in world["events"].of("service.grant.rejected")] == ["credential-unopenable"]
    assert SECRET not in world["events"].text()
    assert SECRET not in repr(vars(source))


def test_plaintext_is_not_retained_in_consumer_state_after_a_successful_open(world: dict[str, Any]) -> None:
    grant_key(world)
    source = consumer(world)

    source.refresh()

    assert SECRET not in repr(vars(source))
    assert SECRET not in repr(source.entries()) + repr(source.devices())
    assert SECRET not in world["events"].text()


def test_opener_receives_structured_fields_and_the_path_grantor(world: dict[str, Any]) -> None:
    grant_id = grant_key(world)
    seen: list[dict[str, object]] = []

    def recording(stored: object) -> bytes:
        seen.append(
            {
                "org": stored.org,
                "grant_id": stored.grant.grant_id,
                "grantor": stored.grantor,
                "service_device": stored.grant.service.device_id,
                "port": stored.grant.service.port,
                "recipient": stored.grant.recipient.as_record(),
            }
        )
        return b"SYNTHETIC-OPENED-VALUE"

    source = consumer(world, open_credential=recording)
    source.refresh()

    assert seen == [
        {
            "org": ORG,
            "grant_id": grant_id,
            "grantor": "alice",
            "service_device": "device-a",
            "port": PORT,
            "recipient": {"owner": "bob", "deviceId": "device-b"},
        }
    ]
    assert read_user_provider_value(world["bob_home"], entry_id_for_grant(grant_id), KEY_FIELD) == "SYNTHETIC-OPENED-VALUE"


def test_a_device_without_an_encryption_key_gets_the_connection_and_no_key(world: dict[str, Any]) -> None:
    grant_id = grant_key(world)
    source = consumer(world, state_dir=world["tmp"] / "no-key-state")

    source.refresh()

    assert source.entries()
    assert provider_ids(world["bob_home"]) == []
    [rejected] = world["events"].of("service.grant.rejected")
    assert (rejected["grantId"], rejected["reason"]) == (grant_id, "key-unavailable")


def test_a_device_without_a_committed_account_derives_nothing(world: dict[str, Any]) -> None:
    grant_key(world)
    grantor(world, world["clock"]).grant(org=ORG, port=PORT, device="@members", name="shared")
    source = consumer(world, owner=None, device_id=None)

    source.refresh()

    assert source.entries() == {}
    assert provider_ids(world["bob_home"]) == []


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privilege on Windows")
def test_a_symlink_at_the_entry_path_is_refused_and_left_alone(world: dict[str, Any]) -> None:
    grant_id = grant_key(world)
    entry_id = entry_id_for_grant(grant_id)
    outside = world["tmp"] / "outside.json"
    outside.write_text('{"KEY": "SYNTHETIC-OUTSIDE"}\n', encoding="utf-8")
    (providers_dir(world["bob_home"]) / f"{entry_id}.json").symlink_to(outside)
    source = consumer(world)

    source.refresh()

    assert outside.read_text(encoding="utf-8") == '{"KEY": "SYNTHETIC-OUTSIDE"}\n'
    assert provider_ids(world["bob_home"]) == []
    assert world["events"].names().count("service.grant.rejected") == 1
