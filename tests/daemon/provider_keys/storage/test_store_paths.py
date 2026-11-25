"""Owner store port: §4.2 path helpers, segment validation and space resolvers."""
from __future__ import annotations

import pytest

from hyprial.daemon.impl.provider_keys import storage
from hyprial.identity import ProviderKeys
from hyprial.kernel import ipc_errors

KEY_ID = "pk.mfrggzdfmztwq2lknnwg23tpob"
AGENT = "0123abcd-4567-4890-abcd-ef0123456789"


def test_paths_follow_contract_layout() -> None:
    assert storage.KEYS_ROOT == "directory/provider-keys"
    assert storage.GRANTS_ROOT == "directory/provider-grants"
    assert storage.key_dir(KEY_ID) == f"directory/provider-keys/{KEY_ID}"
    assert storage.key_meta_path(KEY_ID) == f"directory/provider-keys/{KEY_ID}/meta.json"
    assert storage.key_envelope_path(KEY_ID, 3) == f"directory/provider-keys/{KEY_ID}/envelope-3.json"
    assert storage.grants_dir(AGENT) == f"directory/provider-grants/{AGENT}"
    assert storage.grant_path(AGENT, "openai.main_2") == f"directory/provider-grants/{AGENT}/openai.main_2.json"


@pytest.mark.parametrize("key_id", [
    "pk.MFRGGZDFMZTWQ2LKNNWG23TPOB", "pk.mfrggzdfmztwq2lknnwg23tpo", "pk.mfrggzdfmztwq2lknnwg23tpob1",
    "pk.mfrggzdfmztwq2lknnwg23tp0b", "../../user-keys", "pk.mfrggzdfmztwq2lknnwg23tpob/..", "",
])
def test_key_paths_reject_bad_key_ids(key_id: str) -> None:
    for build in (storage.key_dir, storage.key_meta_path, lambda value: storage.key_envelope_path(value, 1)):
        with pytest.raises(storage.InvalidSegment) as caught:
            build(key_id)
        assert caught.value.code == ipc_errors.INVALID_ARGUMENT
        assert key_id not in str(caught.value) or key_id == ""


@pytest.mark.parametrize("version", [0, -1, True, 1.0, "1", None, 2**53, 2**64, 10**18])
def test_envelope_path_rejects_bad_dek_version(version: object) -> None:
    with pytest.raises(storage.InvalidSegment):
        storage.key_envelope_path(KEY_ID, version)  # type: ignore[arg-type]


@pytest.mark.parametrize("version", [1, 9, 10**15, 2**53 - 1])
def test_envelope_builder_and_parser_agree(version: int) -> None:
    name = storage.key_envelope_path(KEY_ID, version).rsplit("/", 1)[1]
    assert storage.envelope_dek_version(name) == version


@pytest.mark.parametrize("digits", [str(2**53), "9999999999999999", "1" + "0" * 16, "1" * 18])
def test_envelope_parser_rejects_versions_the_builder_refuses(digits: str) -> None:
    assert storage.envelope_dek_version(f"envelope-{digits}.json") is None


@pytest.mark.parametrize("agent", [
    AGENT.upper(), AGENT.replace("-", ""), "..", "0123abcd-4567-4890-abcd-ef012345678", "{" + AGENT + "}", "",
])
def test_grant_paths_reject_non_canonical_agent(agent: str) -> None:
    with pytest.raises(storage.InvalidSegment):
        storage.grants_dir(agent)
    with pytest.raises(storage.InvalidSegment):
        storage.grant_path(agent, "openai")


@pytest.mark.parametrize("grant_id", ["..", ".hidden", "-x", "a/b", "a\\b", "", "a" * 129, "a b", "a\x00"])
def test_grant_path_rejects_bad_grant_ids(grant_id: str) -> None:
    with pytest.raises(storage.InvalidSegment):
        storage.grant_path(AGENT, grant_id)


def test_grant_path_accepts_maximum_length_slot() -> None:
    assert storage.grant_path(AGENT, "a" * 128).endswith("/" + "a" * 128 + ".json")


def test_child_name_parsers() -> None:
    assert storage.envelope_dek_version("envelope-12.json") == 12
    for name in ("envelope-0.json", "envelope-01.json", "envelope-x.json", "meta.json", "envelope-1.json.bak"):
        assert storage.envelope_dek_version(name) is None
    assert storage.grant_id_from_name("openai.json") == "openai"
    for name in ("openai", ".json", "..json", "a/b.json"):
        assert storage.grant_id_from_name(name) is None


def test_unavailable_resolver_fails_closed() -> None:
    with pytest.raises(ProviderKeys.Error) as caught:
        storage.UNAVAILABLE_RESOLVER.store_for("owner-sub")
    assert caught.value.code == ipc_errors.PROVIDER_STORE_UNAVAILABLE
    assert "owner-sub" not in str(caught.value)


@pytest.mark.parametrize("path", [
    "/directory/provider-keys/x", "directory/provider-keys/../user-keys/x", "directory/provider-keys//x",
    "directory/provider-keys/./x", "directory/user-keys/x", "directory/provider-keysx/a", "directory",
    "directory/provider-keys/a\\b", "directory/provider-keys/a\x00", "",
])
def test_store_path_check_rejects_paths_outside_partitions(path: str) -> None:
    with pytest.raises(storage.InvalidSegment):
        storage.check_store_path(path)


def test_store_path_check_accepts_partitions() -> None:
    for path in (storage.KEYS_ROOT, storage.GRANTS_ROOT, storage.key_meta_path(KEY_ID), storage.grant_path(AGENT, "x")):
        assert storage.check_store_path(path) == path
