"""AT06 second slice: the target's offline trust anchor (fail-closed by shape)."""

from __future__ import annotations

import base64
import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hyprial.daemon.impl.transfer.archive.signature import (
    SignatureKeyError,
    generate_signing_key,
    public_key_id,
    sign_bundle,
    verify_bundle_signature,
)
from hyprial.daemon.impl.transfer.archive.trust import (
    RETIREMENT_REFUSE,
    RETIREMENT_WARN,
    TRUST_STORE_RELPATH,
    TRUST_STORE_SCHEMA_VERSION,
    TrustedSourceKey,
    TrustStore,
    TrustStoreFormatError,
    TrustStoreKeyConflict,
    TrustStoreUnknownKey,
    add_trusted_source_key,
    load_trust_store,
    retire_trusted_source_key,
    trust_store_path,
    write_trust_store,
)

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
ADDED_AT = "2026-10-11T00:00:00+00:00"
RETIRED_AT = "2026-10-11T06:00:00+00:00"
DIGEST = "sha256:" + "ab" * 32


def _key(**overrides):
    _, public = generate_signing_key()
    fields = dict(
        actor="codex-sw",
        source_machine="node-a",
        public_key=public,
        added_at=ADDED_AT,
    )
    fields.update(overrides)
    return TrustedSourceKey.create(**fields), public


# -- the empty anchor refuses rather than allows ------------------------------


def test_a_missing_store_is_an_empty_store_not_a_wildcard(tmp_path: Path) -> None:
    path = trust_store_path(tmp_path)

    assert path == tmp_path / TRUST_STORE_RELPATH
    store = load_trust_store(path)

    assert store.keys == ()
    assert store.retirement_policy == RETIREMENT_WARN
    assert store.resolver()("ed25519:" + "0" * 32) is None


def test_an_empty_store_refuses_a_real_signature(tmp_path: Path) -> None:
    private, _ = generate_signing_key()
    block = sign_bundle(private_bytes=private, digest=DIGEST)

    with pytest.raises(SignatureKeyError):
        verify_bundle_signature(
            block,
            digest=DIGEST,
            public_key_resolver=load_trust_store(tmp_path / "x").resolver(),
        )


# -- enrollment ---------------------------------------------------------------


def test_enrolling_a_key_makes_exactly_that_key_verify(tmp_path: Path) -> None:
    path = trust_store_path(tmp_path)
    registered, public = _key()

    store = add_trusted_source_key(
        path,
        actor="codex-sw",
        source_machine="node-a",
        public_key=public,
        added_at=ADDED_AT,
    )

    assert store.key(public_key_id(public)) is not None
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700

    private, _ = generate_signing_key()
    block = sign_bundle(private_bytes=private, digest=DIGEST)
    # A key we enrolled is not the key that signed.
    with pytest.raises(SignatureKeyError):
        verify_bundle_signature(
            block, digest=DIGEST, public_key_resolver=store.resolver()
        )
    assert registered.key_id == public_key_id(public)


def test_the_enrolled_key_verifies_a_bundle_it_signed(tmp_path: Path) -> None:
    path = trust_store_path(tmp_path)
    private, public = generate_signing_key()
    add_trusted_source_key(
        path,
        actor="codex-sw",
        source_machine="node-a",
        public_key=public,
        added_at=ADDED_AT,
    )
    store = load_trust_store(path)
    block = sign_bundle(private_bytes=private, digest=DIGEST)

    verify_bundle_signature(block, digest=DIGEST, public_key_resolver=store.resolver())


def test_re_enrolling_the_same_key_is_a_no_op(tmp_path: Path) -> None:
    path = trust_store_path(tmp_path)
    _, public = generate_signing_key()
    first = add_trusted_source_key(
        path, actor="a", source_machine="n", public_key=public, added_at=ADDED_AT
    )
    second = add_trusted_source_key(
        path, actor="a", source_machine="n", public_key=public, added_at=ADDED_AT
    )

    assert first == second
    assert len(load_trust_store(path).keys) == 1


def test_re_pointing_a_key_at_another_actor_is_refused(tmp_path: Path) -> None:
    path = trust_store_path(tmp_path)
    _, public = generate_signing_key()
    add_trusted_source_key(
        path,
        actor="someone-else",
        source_machine="n",
        public_key=public,
        added_at=ADDED_AT,
    )

    with pytest.raises(TrustStoreKeyConflict) as caught:
        add_trusted_source_key(
            path,
            actor="codex-sw",
            source_machine="n",
            public_key=public,
            added_at=ADDED_AT,
        )
    assert caught.value.code == "trust_store_key_conflict"


def test_a_hand_built_store_refuses_a_conflicting_re_registration() -> None:
    first, public = _key()
    other = TrustedSourceKey.create(
        actor="intruder",
        source_machine="node-b",
        public_key=public,
        added_at=ADDED_AT,
    )

    with pytest.raises(TrustStoreKeyConflict):
        TrustStore(keys=(first,)).with_key(other)


# -- retirement is recorded, never a silent deletion --------------------------


def test_a_retired_key_is_recorded_and_still_resolves(tmp_path: Path) -> None:
    path = trust_store_path(tmp_path)
    _, public = generate_signing_key()
    add_trusted_source_key(
        path, actor="a", source_machine="n", public_key=public, added_at=ADDED_AT
    )
    key_id = public_key_id(public)

    store = retire_trusted_source_key(path, key_id, retired_at=RETIRED_AT)

    assert store.key(key_id).retired_at == RETIRED_AT
    # Retirement must not become a second way for a key to vanish: a bundle
    # already in flight still resolves, and the *policy* decides the outcome.
    assert store.resolver()(key_id) == public
    assert store.is_retired(key_id, now=NOW) is True
    assert store.is_retired(key_id, now=NOW - timedelta(hours=12)) is False


def test_the_retirement_notice_names_the_policy() -> None:
    key, _ = _key(retired_at=None)
    retired = TrustStore(keys=(key,), retirement_policy=RETIREMENT_REFUSE).retiring(
        key.key_id, retired_at=RETIRED_AT
    )

    notice = retired.retirement_notice(key.key_id, now=NOW)
    assert notice is not None
    assert key.key_id in notice
    assert RETIRED_AT in notice
    assert RETIREMENT_REFUSE in notice
    assert retired.retirement_notice(key.key_id, now=NOW - timedelta(days=1)) is None
    assert retired.retirement_notice("ed25519:" + "0" * 32, now=NOW) is None


def test_retiring_an_unknown_key_is_named() -> None:
    with pytest.raises(TrustStoreUnknownKey):
        TrustStore().retiring("ed25519:" + "0" * 32, retired_at=RETIRED_AT)


def test_a_naive_clock_is_refused() -> None:
    key, _ = _key()
    store = TrustStore(keys=(key,))

    with pytest.raises(TrustStoreFormatError):
        store.is_retired(key.key_id, now=datetime(2026, 10, 11, 12, 0))


# -- the on-disk shape --------------------------------------------------------


def test_the_store_roundtrips_through_its_frozen_shape(tmp_path: Path) -> None:
    path = trust_store_path(tmp_path)
    key, public = _key()
    store = TrustStore(keys=(key,), retirement_policy=RETIREMENT_REFUSE)

    write_trust_store(path, store)
    raw = json.loads(path.read_text("utf-8"))

    assert raw["schemaVersion"] == TRUST_STORE_SCHEMA_VERSION
    assert raw["retirementPolicy"] == RETIREMENT_REFUSE
    assert raw["keys"][0]["publicKey"] == base64.b64encode(public).decode()
    assert load_trust_store(path) == store


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda raw: raw | {"extra": 1}, id="extra-field"),
        pytest.param(
            lambda raw: {k: v for k, v in raw.items() if k != "keys"},
            id="missing-field",
        ),
        pytest.param(lambda raw: raw | {"schemaVersion": 99}, id="bad-schema"),
        pytest.param(lambda raw: raw | {"retirementPolicy": "ignore"}, id="bad-policy"),
        pytest.param(lambda raw: raw | {"keys": "not a list"}, id="keys-not-a-list"),
    ],
)
def test_a_malformed_store_is_refused(tmp_path: Path, mutate) -> None:
    key, _ = _key()
    path = tmp_path / "store.json"
    path.write_text(json.dumps(mutate(TrustStore(keys=(key,)).as_dict())))

    with pytest.raises(TrustStoreFormatError):
        load_trust_store(path)


def test_a_key_id_that_is_not_its_public_hash_is_refused(tmp_path: Path) -> None:
    key, _ = _key()
    raw = TrustStore(keys=(key,)).as_dict()
    raw["keys"][0]["keyId"] = "ed25519:" + "f" * 32
    path = tmp_path / "store.json"
    path.write_text(json.dumps(raw))

    with pytest.raises(TrustStoreFormatError) as caught:
        load_trust_store(path)
    assert "does not hash" in str(caught.value)


def test_a_key_id_without_the_prefix_or_a_short_key_is_refused(tmp_path: Path) -> None:
    key, _ = _key()
    raw = TrustStore(keys=(key,)).as_dict()
    path = tmp_path / "store.json"

    raw["keys"][0]["keyId"] = "abc"
    path.write_text(json.dumps(raw))
    with pytest.raises(TrustStoreFormatError):
        load_trust_store(path)

    raw = TrustStore(keys=(key,)).as_dict()
    raw["keys"][0]["publicKey"] = base64.b64encode(b"short").decode()
    path.write_text(json.dumps(raw))
    with pytest.raises(TrustStoreFormatError):
        load_trust_store(path)


def test_a_duplicated_key_id_is_refused() -> None:
    key, _ = _key()
    raw = TrustStore(keys=(key,)).as_dict()
    raw["keys"] = [raw["keys"][0], dict(raw["keys"][0])]

    with pytest.raises(TrustStoreFormatError) as caught:
        TrustStore.from_dict(raw)
    assert "more than once" in str(caught.value)


def test_a_timestamp_without_an_offset_is_refused() -> None:
    _, public = generate_signing_key()

    with pytest.raises(TrustStoreFormatError):
        TrustedSourceKey.create(
            actor="a",
            source_machine="n",
            public_key=public,
            added_at="2026-10-11T00:00:00",
        )

    key, _ = _key()
    with pytest.raises(TrustStoreFormatError):
        TrustStore(keys=(key,)).retiring(key.key_id, retired_at="not-a-timestamp")


def test_an_unreadable_store_file_is_named(tmp_path: Path) -> None:
    path = tmp_path / "store.json"
    path.write_text("{not json")

    with pytest.raises(TrustStoreFormatError):
        load_trust_store(path)
