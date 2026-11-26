"""Red tests for service-grant domain operations, crypto round-trip, leaks, and pure queries.

Decision (service grant S3, 2026-11-26): covers every business rule in plan v2
sections 3.1, 3.4, 3.6 and the S3 brief, including real OrgFS storage, real
X25519 key custody via ``EncryptionKey.Store``, and pure read helpers shared
with S4-py and S5.
"""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

import hyprial.identity as identity
from hyprial.daemon.impl.forwarding.store import ExposureStore
from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.service_grants import (
    ALL_MEMBERS,
    GrantRecipient,
    ServiceEndpoint,
    ServiceGrant,
    ServiceGrantService,
    StoredServiceGrant,
    can_open_grant_credential,
    effective_grants_for_recipient,
    effective_grants_for_service,
    is_grant_effective,
    load_org_service_grants,
)
from hyprial.identity import DirectoryDevice, EncryptionKey, SealedEnvelope
from hyprial.kernel import DaemonRequestError, ipc_errors, write_private_file_atomic

SYNTHETIC_SECRET = "SYNTHETIC-SERVICE-GRANT-TEST-KEY-9876543210"
ORG = "group-acme"


@contextmanager
def _as(runtime: OrgFsRuntime, owner: str) -> Iterator[None]:
    previous = runtime.facade.author
    runtime.facade.author = f"user:{owner}"
    try:
        yield
    finally:
        runtime.facade.author = previous


def _device(
    device_id: str,
    owner: str,
    *,
    encryption_key_id: str | None = None,
    encryption_public_key: str | None = None,
) -> DirectoryDevice:
    return DirectoryDevice(
        device_id=device_id,
        owner=owner,
        server_public=f"nodekey:server-{device_id}",
        client_public=f"nodekey:client-{device_id}",
        address=f"tcp://127.0.0.1:7447#{device_id}-psk",
        key_generation=1,
        updated_at="2026-11-26T00:00:00+00:00",
        encryption_key_id=encryption_key_id,
        encryption_public_key=encryption_public_key,
    )


def _write_user_provider(home: Path, entry_id: str, values: dict[str, str]) -> Path:
    providers = home / "secrets" / "providers"
    providers.mkdir(parents=True, exist_ok=True, mode=0o700)
    (home / "secrets").chmod(0o700)
    providers.chmod(0o700)
    path = providers / f"{entry_id}.json"
    write_private_file_atomic(
        path, (json.dumps(values, sort_keys=True) + "\n").encode("utf-8")
    )
    return path


def _make_key_store(state_dir: Path, *, sub: str, device_uid: str) -> Any:
    return EncryptionKey.Store(
        state_dir=state_dir,
        kind="user",
        casdoor_sub=sub,
        device_uid=device_uid,
        fingerprint=f"fp-{device_uid}",
    )


class _Clock:
    def __init__(self, now_ms: int = 1_700_000_000_000) -> None:
        self.value = now_ms

    def __call__(self) -> int:
        return self.value


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[dict[str, Any]]:
    home = tmp_path / "alice-home"
    home.mkdir(mode=0o700)
    bob_state = tmp_path / "bob-state"
    bob_state.mkdir(mode=0o700)

    runtime = OrgFsRuntime(
        tmp_path / "orgfs-state", node_id="device-a", author="user:alice"
    )
    store = OrgFsDirectoryStore(runtime.facade)
    space = runtime.facade.create_space(ORG)
    runtime.facade.invite(space.space_id, "user:bob", "rw")
    runtime.facade.invite(space.space_id, "user:carol", "rw")
    store.ensure_org_directory_roots(ORG)

    bob_keys = _make_key_store(bob_state, sub="bob", device_uid="device-b")
    bob_pub = bob_keys.ensure()

    store.put_device(ORG, _device("device-a", "alice"))
    with _as(runtime, "bob"):
        store.put_device(
            ORG,
            _device(
                "device-b",
                "bob",
                encryption_key_id=bob_pub["keyId"],
                encryption_public_key=bob_pub["publicKey"],
            ),
        )
        store.put_device(ORG, _device("device-b-nokey", "bob"))
    with _as(runtime, "carol"):
        store.put_device(ORG, _device("device-c", "carol"))

    exposures = ExposureStore(tmp_path / "network-exposures.json")
    exposures.set({"port": 8447, "target": "tcp:127.0.0.1:8447", "proxyProtocol": "none"})

    events: list[tuple[str, str, str, dict[str, Any]]] = []
    clock = _Clock()

    def logger(level: str, component: str, event: str, **fields: Any) -> None:
        events.append((level, component, event, fields))

    service = ServiceGrantService(
        store=store,
        home=home,
        owner="user:alice",
        local_device_id="device-a",
        exposed_ports=lambda: frozenset(int(item["port"]) for item in exposures.list()),
        now_ms=clock,
        logger=logger,
    )
    yield {
        "home": home,
        "runtime": runtime,
        "store": store,
        "exposures": exposures,
        "bob_keys": bob_keys,
        "bob_pub": bob_pub,
        "events": events,
        "clock": clock,
        "service": service,
    }
    runtime.close()


def test_identity_facade_exports_only_expected_sealed_envelope_surface() -> None:
    """Identity facade exposes ``SealedEnvelope`` with only ``seal_service_grant`` and ``ServiceGrantError``."""

    assert identity.SealedEnvelope is SealedEnvelope
    public = {name for name in dir(SealedEnvelope) if not name.startswith("_")}
    assert public == {"seal_service_grant", "ServiceGrantError"}


def test_grant_refuses_unexposed_port_unknown_recipient_members_with_credential_and_missing_key(
    harness: dict[str, Any],
) -> None:
    """All four S3 refusal preconditions fail before writing any grant document."""

    service: ServiceGrantService = harness["service"]
    store: OrgFsDirectoryStore = harness["store"]
    _write_user_provider(harness["home"], "gateway", {"API_KEY": SYNTHETIC_SECRET})

    with pytest.raises(DaemonRequestError) as unexposed:
        service.grant(org=ORG, port=9999, device="bob/device-b")
    assert unexposed.value.code == ipc_errors.SERVICE_NOT_REGISTERED

    with pytest.raises(DaemonRequestError) as missing_dev:
        service.grant(org=ORG, port=8447, device="bob/no-such-device")
    assert missing_dev.value.code == ipc_errors.SERVICE_DEVICE_UNAVAILABLE

    with pytest.raises(DaemonRequestError) as wrong_owner:
        service.grant(org=ORG, port=8447, device="carol/device-b")
    assert wrong_owner.value.code == ipc_errors.SERVICE_DEVICE_UNAVAILABLE

    with pytest.raises(DaemonRequestError) as members_cred:
        service.grant(
            org=ORG,
            port=8447,
            device="@members",
            credential={"entryId": "gateway", "fieldName": "API_KEY"},
        )
    assert members_cred.value.code == ipc_errors.INVALID_ARGUMENT

    with pytest.raises(DaemonRequestError) as no_key:
        service.grant(
            org=ORG,
            port=8447,
            device="bob/device-b-nokey",
            credential={"entryId": "gateway", "fieldName": "API_KEY"},
        )
    assert no_key.value.code == ipc_errors.SERVICE_DEVICE_UNAVAILABLE

    assert store.list_service_grants(ORG) == []


def test_grant_with_credential_round_trips_and_blocks_moved_owner_and_never_leaks_secret(
    harness: dict[str, Any],
) -> None:
    """Sealed credential opens on Bob's device, fails if copied to another owner, and never leaks plaintext."""

    service: ServiceGrantService = harness["service"]
    store: OrgFsDirectoryStore = harness["store"]
    bob_keys = harness["bob_keys"]
    events: list[tuple[str, str, str, dict[str, Any]]] = harness["events"]
    _write_user_provider(harness["home"], "gateway", {"API_KEY": SYNTHETIC_SECRET})

    result = service.grant(
        org=ORG,
        port=8447,
        device="bob/device-b",
        name="magpie",
        protocol="http",
        note="Internal gateway",
        credential={"entryId": "gateway", "fieldName": "API_KEY"},
    )
    grant_view = result["grant"]
    grant_id = grant_view["grantId"]

    assert SYNTHETIC_SECRET not in json.dumps(result)
    assert SYNTHETIC_SECRET not in json.dumps(events)
    assert any(ev[2] == "service.grant.created" for ev in events)
    assert grant_view["hasCredential"] is True
    assert grant_view["grantor"] == "alice"
    assert grant_view["recipient"] == {"owner": "bob", "deviceId": "device-b"}

    raw_doc = store.read_service_grant(ORG, "alice", grant_id)
    assert raw_doc is not None
    assert SYNTHETIC_SECRET not in json.dumps(raw_doc)
    assert raw_doc["credential"] is not None

    opened = bob_keys.open_service_grant(
        raw_doc["credential"],
        org=ORG,
        grant_id=raw_doc["grantId"],
        grantor="alice",
        service_device=raw_doc["service"]["deviceId"],
        port=raw_doc["service"]["port"],
        recipient_owner=raw_doc["recipient"]["owner"],
        recipient_device=raw_doc["recipient"]["deviceId"],
    )
    assert opened == SYNTHETIC_SECRET.encode("utf-8")

    copied = copy.deepcopy(raw_doc)
    with _as(harness["runtime"], "carol"):
        store.put_service_grant(ORG, "carol", grant_id, copied)

    with pytest.raises(DaemonRequestError) as moved_err:
        bob_keys.open_service_grant(
            copied["credential"],
            org=ORG,
            grant_id=copied["grantId"],
            grantor="carol",
            service_device=copied["service"]["deviceId"],
            port=copied["service"]["port"],
            recipient_owner=copied["recipient"]["owner"],
            recipient_device=copied["recipient"]["deviceId"],
        )
    assert moved_err.value.code == ipc_errors.PAYLOAD_INVALID
    assert SYNTHETIC_SECRET not in str(moved_err.value)

    write_private_file_atomic(
        harness["home"] / "secrets" / "providers" / "raw.json",
        SYNTHETIC_SECRET.encode("utf-8"),
    )
    single_field = service.grant(
        org=ORG,
        port=8447,
        device="bob/device-b",
        credential={"entryId": "raw"},
    )["grant"]
    raw_single = store.read_service_grant(ORG, "alice", single_field["grantId"])
    assert raw_single is not None
    opened_single = bob_keys.open_service_grant(
        raw_single["credential"],
        org=ORG,
        grant_id=raw_single["grantId"],
        grantor="alice",
        service_device=raw_single["service"]["deviceId"],
        port=raw_single["service"]["port"],
        recipient_owner=raw_single["recipient"]["owner"],
        recipient_device=raw_single["recipient"]["deviceId"],
    )
    assert opened_single == SYNTHETIC_SECRET.encode("utf-8")


def test_revoke_clears_credential_keeps_document_and_refuses_other_owners_grant(
    harness: dict[str, Any],
) -> None:
    """Revocation sets ``revokedAtMs``, clears ``credential``, preserves the doc, and blocks cross-owner revoke."""

    service: ServiceGrantService = harness["service"]
    store: OrgFsDirectoryStore = harness["store"]
    events = harness["events"]
    clock: _Clock = harness["clock"]
    _write_user_provider(harness["home"], "gateway", {"API_KEY": SYNTHETIC_SECRET})

    created = service.grant(
        org=ORG,
        port=8447,
        device="bob/device-b",
        credential={"entryId": "gateway", "fieldName": "API_KEY"},
    )["grant"]
    grant_id = created["grantId"]

    clock.value += 5_000
    revoked = service.revoke(org=ORG, grant_id=grant_id)
    assert revoked == {"grantId": grant_id, "revoked": True}
    assert any(ev[2] == "service.grant.revoked" for ev in events)
    assert SYNTHETIC_SECRET not in json.dumps(events)

    raw_doc = store.read_service_grant(ORG, "alice", grant_id)
    assert raw_doc is not None
    assert raw_doc["revokedAtMs"] == clock.value
    assert raw_doc["credential"] is None

    assert service.revoke(org=ORG, grant_id=grant_id) == {
        "grantId": grant_id,
        "revoked": False,
    }

    bob_service = ServiceGrantService(
        store=store,
        home=harness["home"],
        owner="bob",
        local_device_id="device-b",
        exposed_ports=lambda: frozenset({8447}),
        now_ms=clock,
    )
    with _as(harness["runtime"], "bob"):
        with pytest.raises(DaemonRequestError) as forbidden:
            bob_service.revoke(org=ORG, grant_id=grant_id)
        assert forbidden.value.code == ipc_errors.SERVICE_NOT_AUTHORIZED


def test_grants_lists_granted_and_received_readonly_without_decrypting(
    harness: dict[str, Any],
) -> None:
    """``service.grants`` separates own vs received grants and respects filters."""

    service: ServiceGrantService = harness["service"]
    store: OrgFsDirectoryStore = harness["store"]
    clock: _Clock = harness["clock"]
    _write_user_provider(harness["home"], "gateway", {"API_KEY": SYNTHETIC_SECRET})

    g_bob = service.grant(
        org=ORG,
        port=8447,
        device="bob/device-b",
        credential={"entryId": "gateway", "fieldName": "API_KEY"},
    )["grant"]
    g_members = service.grant(org=ORG, port=8447, device="@members")["grant"]

    alice_view = service.grants(org=ORG)
    assert {item["grantId"] for item in alice_view["granted"]} == {
        g_bob["grantId"],
        g_members["grantId"],
    }
    assert alice_view["received"] == []

    bob_service = ServiceGrantService(
        store=store,
        home=harness["home"],
        owner="bob",
        local_device_id="device-b",
        exposed_ports=lambda: frozenset(),
        now_ms=clock,
    )
    bob_view = bob_service.grants(org=ORG)
    assert bob_view["granted"] == []
    assert {item["grantId"] for item in bob_view["received"]} == {
        g_bob["grantId"],
        g_members["grantId"],
    }
    assert SYNTHETIC_SECRET not in json.dumps(bob_view)

    carol_service = ServiceGrantService(
        store=store,
        home=harness["home"],
        owner="carol",
        local_device_id="device-c",
        exposed_ports=lambda: frozenset(),
        now_ms=clock,
    )
    carol_view = carol_service.grants(org=ORG)
    assert {item["grantId"] for item in carol_view["received"]} == {g_members["grantId"]}


def test_pure_query_helpers_handle_revoked_expired_mismatch_and_members(
    harness: dict[str, Any],
) -> None:
    """Single effectiveness helper and pure S4/S5 query helpers enforce all predicates."""

    now = 1_700_000_010_000
    endpoint = ServiceEndpoint(device_id="device-a", port=8447, name="svc", protocol="http")
    active = StoredServiceGrant(
        org=ORG,
        grantor="alice",
        grant=ServiceGrant(
            schema_version=1,
            grant_id="sg.aaaaaaaaaaaaaaaaaaaaaaaaaa",
            service=endpoint,
            recipient=GrantRecipient(owner="bob", device_id="device-b"),
            granted_at_ms=now - 5_000,
            expires_at_ms=now + 5_000,
            credential={"v": 1},
        ),
    )
    revoked = StoredServiceGrant(
        org=ORG,
        grantor="alice",
        grant=ServiceGrant(
            schema_version=1,
            grant_id="sg.bbbbbbbbbbbbbbbbbbbbbbbbbb",
            service=endpoint,
            recipient=GrantRecipient(owner="bob", device_id="device-b"),
            granted_at_ms=now - 5_000,
            revoked_at_ms=now - 1_000,
        ),
    )
    expired = StoredServiceGrant(
        org=ORG,
        grantor="alice",
        grant=ServiceGrant(
            schema_version=1,
            grant_id="sg.cccccccccccccccccccccccccc",
            service=endpoint,
            recipient=GrantRecipient(owner="bob", device_id="device-b"),
            granted_at_ms=now - 5_000,
            expires_at_ms=now,
        ),
    )
    other_recipient = StoredServiceGrant(
        org=ORG,
        grantor="alice",
        grant=ServiceGrant(
            schema_version=1,
            grant_id="sg.dddddddddddddddddddddddddd",
            service=endpoint,
            recipient=GrantRecipient(owner="carol", device_id="device-c"),
            granted_at_ms=now - 5_000,
        ),
    )
    members = StoredServiceGrant(
        org=ORG,
        grantor="alice",
        grant=ServiceGrant(
            schema_version=1,
            grant_id="sg.eeeeeeeeeeeeeeeeeeeeeeeeee",
            service=endpoint,
            recipient=ALL_MEMBERS,
            granted_at_ms=now - 5_000,
        ),
    )
    all_items = (active, revoked, expired, other_recipient, members)

    assert is_grant_effective(active.grant, now_ms=now) is True
    assert is_grant_effective(revoked.grant, now_ms=now) is False
    assert is_grant_effective(expired.grant, now_ms=now) is False

    for_service = effective_grants_for_service(
        all_items, owner="user:alice", device_id="device-a", port=8447, now_ms=now
    )
    assert [item.grant.grant_id for item in for_service] == [
        active.grant.grant_id,
        other_recipient.grant.grant_id,
        members.grant.grant_id,
    ]

    for_bob = effective_grants_for_recipient(
        all_items, owner="user:bob", device_id="device-b", now_ms=now
    )
    assert [(item.grantor, item.grant.grant_id) for item in for_bob] == [
        ("alice", active.grant.grant_id),
        ("alice", members.grant.grant_id),
    ]

    assert can_open_grant_credential(
        active, recipient_owner="bob", recipient_device_id="device-b", now_ms=now
    )
    assert not can_open_grant_credential(
        active, recipient_owner="bob", recipient_device_id="device-other", now_ms=now
    )
    assert not can_open_grant_credential(
        members, recipient_owner="bob", recipient_device_id="device-b", now_ms=now
    )
    assert not can_open_grant_credential(
        revoked, recipient_owner="bob", recipient_device_id="device-b", now_ms=now
    )

    loaded = load_org_service_grants(harness["store"], ORG)
    assert loaded == ()
