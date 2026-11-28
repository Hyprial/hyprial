"""The granted Key entry under a race and under a replaced credential.

Decision (service grant recipient side fix, 2026-11-27):

- The recipient writes its Key through identity's create-only
  entry, so a user entry that appears after the consumer looked and before it
  wrote is refused (``entry-exists``), never merged over.
- The ledger records a digest of the credential ciphertext, never of
  the plaintext. When the ciphertext of the same ``(org, grantId)`` is
  replaced, the credential is opened again and the entry this slice wrote is
  replaced; an entry the user changed is left alone, as before.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.service_grants.consumer import GrantConsumer
from hyprial.daemon.impl.service_grants.consumer import keys as keys_module
from hyprial.identity import read_user_provider_value
from tests.daemon.service_grants.consumer.support import (
    KEY_FIELD,
    ORG,
    PORT,
    SECRET,
    as_author,
    grantor,
    providers_dir,
    write_user_provider,
)
from tests.daemon.service_grants.consumer_fixes.support import (
    close,
    consumer_arguments,
    reasons,
    world_at,
)


@pytest.fixture
def world(tmp_path: Path, actor_host) -> dict[str, Any]:
    made = world_at(tmp_path, actor_host)
    yield made
    close(made)


def grant_key(world: dict[str, Any], **extra: Any) -> str:
    return grantor(world, world["clock"]).grant(
        org=ORG, port=PORT, device="bob/device-b", name="magpie",
        credential={"entryId": "gateway", "fieldName": "API_KEY"}, **extra,
    )["grant"]["grantId"]


def entry_path(world: dict[str, Any], entry_id: str) -> Path:
    return providers_dir(world["bob_home"]) / f"{entry_id}.json"


def user_writes_after_the_look(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """The consumer looks at the entry path and finds nothing; then the user's entry appears."""

    look = keys_module._inspect

    def racing(path: Path) -> Any:
        seen = look(path)
        if path.parent == providers_dir(home) and not path.exists():
            write_user_provider(home, path.name.removesuffix(".json"), {"KEY": "SYNTHETIC-USER-OWNED"})
        return seen

    monkeypatch.setattr(keys_module, "_inspect", racing)


def test_a_user_entry_created_after_the_look_is_never_overwritten(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race: the entry appears between the check and the write."""

    grant_id = grant_key(world)
    entry_id = keys_module.entry_id_for_grant(grant_id)
    user_writes_after_the_look(monkeypatch, world["bob_home"])

    GrantConsumer(**consumer_arguments(world)).refresh()

    assert json.loads(entry_path(world, entry_id).read_bytes()) == {"KEY": "SYNTHETIC-USER-OWNED"}
    assert read_user_provider_value(world["bob_home"], entry_id, KEY_FIELD) == "SYNTHETIC-USER-OWNED"
    assert reasons(world) == [(grant_id, "entry-exists")]
    assert world["events"].of("service.grant.received") == []


def test_a_user_entry_created_after_the_look_is_never_deleted_with_the_grant(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    grant_id = grant_key(world)
    entry_id = keys_module.entry_id_for_grant(grant_id)
    source = GrantConsumer(**consumer_arguments(world))
    with monkeypatch.context() as patch:
        user_writes_after_the_look(patch, world["bob_home"])
        source.refresh()

    grantor(world, world["clock"]).revoke(org=ORG, grant_id=grant_id)
    source.refresh()

    assert json.loads(entry_path(world, entry_id).read_bytes()) == {"KEY": "SYNTHETIC-USER-OWNED"}
    assert not source.owns_entry(entry_id)


def test_a_new_entry_never_goes_through_the_merging_writer(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first write is create-only; the merging writer is never the one that creates the entry."""

    from hyprial.identity import SecretResolver

    grant_key(world)
    calls: list[str] = []
    original = SecretResolver.write_user_provider

    def recording(self: Any, entry_id: str, values: Any) -> Path:
        calls.append(entry_id)
        return original(self, entry_id, values)

    monkeypatch.setattr(SecretResolver, "write_user_provider", recording)

    GrantConsumer(**consumer_arguments(world)).refresh()

    assert calls == []
    assert len(list(providers_dir(world["bob_home"]).glob("*.json"))) == 1


def replace_credential(world: dict[str, Any], grant_id: str, *, secret: str) -> None:
    """The grantor seals a different Key under the same grant document.

    This is how ``ServiceGrantService.grant`` seals: the same header fields
    (so the document stays one consistent grant) and the public key bob
    published in his device row. Only the Key inside changes.
    """

    import base64

    from hyprial.identity import SealedEnvelope

    raw = world["store"].read_service_grant(ORG, "alice", grant_id)
    assert raw is not None and raw["service"] is not None
    recipient = next(row for row in world["store"].list_devices(ORG) if row.device_id == "device-b")
    sealed = SealedEnvelope.seal_service_grant(
        secret.encode("utf-8"),
        org=ORG,
        grant_id=grant_id,
        grantor="alice",
        service_device=raw["service"]["deviceId"],
        port=raw["service"]["port"],
        recipient_owner="bob",
        recipient_device="device-b",
        recipient_public_key=base64.b64decode(recipient.encryption_public_key or "", validate=True),
    )
    with as_author(world["runtime"], "alice"):
        world["store"].put_service_grant(ORG, "alice", grant_id, {**raw, "credential": sealed})


def test_a_replaced_credential_reopens_and_replaces_the_entry_this_slice_wrote(world: dict[str, Any]) -> None:
    grant_id = grant_key(world)
    entry_id = keys_module.entry_id_for_grant(grant_id)
    source = GrantConsumer(**consumer_arguments(world))
    source.refresh()
    assert read_user_provider_value(world["bob_home"], entry_id, KEY_FIELD) == SECRET

    replace_credential(world, grant_id, secret="SYNTHETIC-ROTATED-KEY-0123456789ABCDEF")
    source.refresh()

    assert read_user_provider_value(world["bob_home"], entry_id, KEY_FIELD) == "SYNTHETIC-ROTATED-KEY-0123456789ABCDEF"
    assert source.owns_entry(entry_id)
    assert reasons(world) == []
    assert [row["entryId"] for row in world["events"].of("service.grant.received")] == [entry_id, entry_id]


def test_a_replaced_credential_leaves_an_entry_the_user_changed_alone(world: dict[str, Any]) -> None:
    grant_id = grant_key(world)
    entry_id = keys_module.entry_id_for_grant(grant_id)
    source = GrantConsumer(**consumer_arguments(world))
    source.refresh()
    entry_path(world, entry_id).write_bytes(b'{"KEY": "SYNTHETIC-USER-EDITED"}\n')

    replace_credential(world, grant_id, secret="SYNTHETIC-ROTATED-KEY-0123456789ABCDEF")
    source.refresh()

    assert entry_path(world, entry_id).read_bytes() == b'{"KEY": "SYNTHETIC-USER-EDITED"}\n'
    assert not source.owns_entry(entry_id)
    assert reasons(world) == [(grant_id, "entry-modified")]


def test_a_rotated_entry_is_still_taken_back_with_the_grant(world: dict[str, Any]) -> None:
    grant_id = grant_key(world)
    entry_id = keys_module.entry_id_for_grant(grant_id)
    source = GrantConsumer(**consumer_arguments(world))
    source.refresh()
    replace_credential(world, grant_id, secret="SYNTHETIC-ROTATED-KEY-0123456789ABCDEF")
    source.refresh()

    grantor(world, world["clock"]).revoke(org=ORG, grant_id=grant_id)
    source.refresh()

    assert not entry_path(world, entry_id).exists()
    assert not source.owns_entry(entry_id)


def test_an_unchanged_credential_is_not_opened_again(world: dict[str, Any]) -> None:
    grant_id = grant_key(world)
    opened: list[str] = []

    def counting(stored: Any) -> bytes:
        opened.append(stored.grant.grant_id)
        return SECRET.encode()

    source = GrantConsumer(**consumer_arguments(world), open_credential=counting)
    for _ in range(4):
        source.refresh()

    assert opened == [grant_id]


def test_an_unopenable_replacement_keeps_the_entry_and_reports_the_reason(world: dict[str, Any]) -> None:
    grant_id = grant_key(world)
    entry_id = keys_module.entry_id_for_grant(grant_id)
    source = GrantConsumer(**consumer_arguments(world))
    source.refresh()
    raw = world["store"].read_service_grant(ORG, "alice", grant_id)
    assert raw is not None
    damaged = json.loads(json.dumps(raw))
    damaged["credential"]["payload"]["ciphertext"] = "AAAA"
    with as_author(world["runtime"], "alice"):
        world["store"].put_service_grant(ORG, "alice", grant_id, damaged)

    source.refresh()

    assert read_user_provider_value(world["bob_home"], entry_id, KEY_FIELD) == SECRET
    assert [row[1] for row in reasons(world)] == ["credential-unopenable"]
    assert source.owns_entry(entry_id)


def test_the_ledger_holds_a_digest_of_the_ciphertext_and_no_plaintext(world: dict[str, Any]) -> None:
    grant_id = grant_key(world)
    source = GrantConsumer(**consumer_arguments(world))
    source.refresh()

    text = (world["bob_home"] / keys_module.LEDGER_RELPATH).read_text()

    assert SECRET not in text
    [row] = json.loads(text)["entries"]
    raw = world["store"].read_service_grant(ORG, "alice", grant_id)
    assert raw is not None
    assert json.dumps(raw["credential"]) not in text
    assert row["credentialDigest"] != row["digest"]
    assert len(row["credentialDigest"]) == 64
