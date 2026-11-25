"""OrgFS owner store adapter: conditional writes, absence and error mapping (contract §4.3)."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.docs import LocalOrgFs
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import StoreError
from hyprial.daemon.impl.provider_keys import storage
from hyprial.daemon.impl.provider_keys.storage.orgfs import OrgFsOwnerStore
from hyprial.identity import ProviderKeys
from hyprial.kernel import ipc_errors

KEY_ID = "pk.mfrggzdfmztwq2lknnwg23tpob"
AGENT = "0123abcd-4567-4890-abcd-ef0123456789"
META = storage.key_meta_path(KEY_ID)


@pytest.fixture(params=["runtime", "local"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[storage.OwnerStore]:
    if request.param == "runtime":
        runtime = OrgFsRuntime(tmp_path / "state", node_id="node-a", author="user:owner@example")
        try:
            fs = runtime.facade
            yield storage.FixedSpaceResolver(fs, fs.create_space("personal-owner").space_id).store_for("owner")
        finally:
            runtime.close()
    else:
        fs = LocalOrgFs(author="user:owner@example")
        yield storage.FixedSpaceResolver(fs, fs.create_space("personal-owner").space_id).store_for("owner")


def test_fixed_resolver_yields_owner_store(store: storage.OwnerStore) -> None:
    assert isinstance(store, OrgFsOwnerStore)
    assert isinstance(store, storage.OwnerStore)
    assert isinstance(storage.FixedSpaceResolver(LocalOrgFs(author="user:a"), "s"), storage.OwnerSpaceResolver)


def test_create_reads_back_and_creates_parents(store: storage.OwnerStore) -> None:
    version = store.write(META, '{"a":1}', expect_version=None, create_only=True)
    assert isinstance(version, str) and version
    assert store.read(META) == ('{"a":1}', version)
    assert store.listdir(storage.KEYS_ROOT) == (KEY_ID,)
    assert store.listdir(storage.key_dir(KEY_ID)) == ("meta.json",)
    grant = storage.grant_path(AGENT, "openai")
    store.write(grant, "{}", expect_version=None, create_only=True)
    assert store.listdir(storage.grants_dir(AGENT)) == ("openai.json",)


def test_create_only_on_existing_target_is_stale(store: storage.OwnerStore) -> None:
    store.write(META, "one", expect_version=None, create_only=True)
    with pytest.raises(storage.StaleWrite):
        store.write(META, "two", expect_version=None, create_only=True)
    assert store.read(META)[0] == "one"


def test_expect_version_compare_and_set(store: storage.OwnerStore) -> None:
    first = store.write(META, "one", expect_version=None, create_only=True)
    second = store.write(META, "two", expect_version=first, create_only=False)
    assert second != first
    with pytest.raises(storage.StaleWrite):
        store.write(META, "three", expect_version=first, create_only=False)
    assert store.read(META) == ("two", second)


def test_expect_version_on_absent_target_is_stale(store: storage.OwnerStore) -> None:
    with pytest.raises(storage.StaleWrite):
        store.write(META, "one", expect_version="dmVyc2lvbg", create_only=False)
    assert store.read(META) is None


def test_absent_read_listdir_and_remove(store: storage.OwnerStore) -> None:
    assert store.read(META) is None
    assert store.listdir(storage.key_dir(KEY_ID)) == ()
    assert store.listdir(storage.GRANTS_ROOT) == ()
    store.remove(META)
    store.remove(storage.key_dir(KEY_ID))


def test_remove_existing_then_absent(store: storage.OwnerStore) -> None:
    envelope = storage.key_envelope_path(KEY_ID, 1)
    store.write(envelope, "env", expect_version=None, create_only=True)
    store.write(META, "meta", expect_version=None, create_only=True)
    store.remove(envelope)
    assert store.read(envelope) is None
    assert store.listdir(storage.key_dir(KEY_ID)) == ("meta.json",)
    store.remove(envelope)


def test_store_refuses_paths_outside_partitions(store: storage.OwnerStore) -> None:
    for path in ("directory/user-keys/x.json", "directory/provider-keys/../user-keys/x", "/directory/provider-keys/x"):
        with pytest.raises(storage.InvalidSegment):
            store.read(path)
        with pytest.raises(storage.InvalidSegment):
            store.write(path, "x", expect_version=None, create_only=True)
        with pytest.raises(storage.InvalidSegment):
            store.remove(path)
        with pytest.raises(storage.InvalidSegment):
            store.listdir(path)


def test_create_only_with_expect_version_is_refused(store: storage.OwnerStore) -> None:
    with pytest.raises(storage.InvalidSegment):
        store.write(META, "x", expect_version="v", create_only=True)


class _FailingFs:
    """Duck-typed OrgFs whose every operation raises the configured error."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        def fail(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            raise self.error
        return fail


def _operations(store: OrgFsOwnerStore) -> list[Any]:
    return [
        lambda: store.read(META),
        lambda: store.write(META, "secret-sentinel", expect_version=None, create_only=True),
        lambda: store.remove(META),
        lambda: store.listdir(storage.KEYS_ROOT),
    ]


@pytest.mark.parametrize("error", [
    OrgFsError(ipc_errors.ORGFS_CONTENT_PENDING, {"node": META}),
    OrgFsError("unknown-space", {"spaceId": "s"}),
    OrgFsError("not-a-member"),
    OrgFsError("unavailable"),
    OrgFsError("closed"),
    OrgFsError("too-large"),
    OrgFsError("invalid-argument", {"message": "not a text document"}),
    StoreError("not-a-member", "device row belongs to another member"),
    StoreError("io-error"),
    OSError("disk gone"),
])
def test_transport_failures_map_to_store_unavailable(error: Exception) -> None:
    store = OrgFsOwnerStore(_FailingFs(error), "space-1")
    for operation in _operations(store):
        with pytest.raises(ProviderKeys.Error) as caught:
            operation()
        assert caught.value.code == ipc_errors.PROVIDER_STORE_UNAVAILABLE
        assert "secret-sentinel" not in str(caught.value)
        assert caught.value.__cause__ is None and caught.value.__suppress_context__


@pytest.mark.parametrize("error", [
    OrgFsError("ambiguous-path", {"candidates": ["a", "b"]}),
    StoreError("ambiguous-path"),
])
def test_ambiguous_path_maps_to_store_conflict(error: Exception) -> None:
    store = OrgFsOwnerStore(_FailingFs(error), "space-1")
    for operation in _operations(store):
        with pytest.raises(ProviderKeys.Error) as caught:
            operation()
        assert caught.value.code == ipc_errors.PROVIDER_STORE_CONFLICT


@pytest.mark.parametrize("error", [OrgFsError("stale-write", {"expected": "a", "actual": "b"}), StoreError("stale-write")])
def test_stale_write_from_backend_is_local_stale_write(error: Exception) -> None:
    store = OrgFsOwnerStore(_FailingFs(error), "space-1")
    with pytest.raises(storage.StaleWrite):
        store.write(META, "x", expect_version="v1", create_only=False)
    with pytest.raises(storage.StaleWrite):
        store.write(META, "x", expect_version=None, create_only=True)


@pytest.mark.parametrize("error", [OrgFsError("unknown-doc", {"node": META}), StoreError("unknown-doc")])
def test_unknown_doc_is_absence(error: Exception) -> None:
    store = OrgFsOwnerStore(_FailingFs(error), "space-1")
    assert store.read(META) is None
    assert store.listdir(storage.KEYS_ROOT) == ()
    store.remove(META)


def test_write_never_passes_base_version() -> None:
    seen: list[dict[str, Any]] = []

    class _Recording:
        def mkdir(self, space_id: str, path: str) -> None:
            raise OrgFsError("invalid-argument", {"message": "node already exists"})

        def write_text(self, space_id: str, node: str, content: str, **kwargs: Any) -> Any:
            seen.append(dict(kwargs, space_id=space_id, node=node))
            return type("Info", (), {"version": "v2"})()

    assert OrgFsOwnerStore(_Recording(), "space-1").write(META, "x", expect_version="v1", create_only=False) == "v2"
    assert seen == [{"space_id": "space-1", "node": META, "expect_version": "v1", "create_only": False}]


def test_blind_write_is_refused_on_a_real_store(store: storage.OwnerStore) -> None:
    store.write(META, "one", expect_version=None, create_only=True)
    with pytest.raises(storage.InvalidSegment) as caught:
        store.write(META, "two", expect_version=None, create_only=False)
    assert caught.value.code == ipc_errors.INVALID_ARGUMENT
    assert store.read(META)[0] == "one"


def test_blind_write_is_refused_before_the_backend() -> None:
    fs = _FailingFs(OrgFsError("unavailable"))
    with pytest.raises(storage.InvalidSegment):
        OrgFsOwnerStore(fs, "space-1").write(META, "x", expect_version=None, create_only=False)
    assert fs.calls == []


class _WriteFailingFs:
    """mkdir succeeds; write_text raises the configured error."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls: list[str] = []

    def mkdir(self, space_id: str, path: str) -> None:
        self.calls.append("mkdir")

    def write_text(self, space_id: str, node: str, content: str, **kwargs: Any) -> Any:
        self.calls.append("write_text")
        raise self.error


@pytest.mark.parametrize("error", [
    OrgFsError(ipc_errors.ORGFS_CONTENT_PENDING, {"node": META}),
    OrgFsError("not-a-member"),
    OrgFsError("too-large"),
    StoreError("too-large"),
    ValueError("secret-sentinel is not text"),
    TypeError("secret-sentinel"),
    RuntimeError("secret-sentinel"),
    KeyError("secret-sentinel"),
])
@pytest.mark.parametrize("conditional", [
    {"expect_version": None, "create_only": True}, {"expect_version": "v1", "create_only": False},
])
def test_write_text_failures_map_to_store_unavailable(error: BaseException, conditional: dict[str, Any]) -> None:
    fs = _WriteFailingFs(error)
    with pytest.raises(ProviderKeys.Error) as caught:
        OrgFsOwnerStore(fs, "space-1").write(META, "secret-sentinel", **conditional)
    assert fs.calls[-1] == "write_text"
    assert caught.value.code == ipc_errors.PROVIDER_STORE_UNAVAILABLE
    assert "secret-sentinel" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


@pytest.mark.parametrize("error", [ValueError("secret-sentinel"), TypeError("x"), RuntimeError("x"), KeyError("x")])
def test_any_other_backend_exception_maps_to_store_unavailable(error: BaseException) -> None:
    store = OrgFsOwnerStore(_FailingFs(error), "space-1")
    for operation in _operations(store):
        with pytest.raises(ProviderKeys.Error) as caught:
            operation()
        assert caught.value.code == ipc_errors.PROVIDER_STORE_UNAVAILABLE
        assert "secret-sentinel" not in str(caught.value)
        assert caught.value.__cause__ is None and caught.value.__suppress_context__


@pytest.mark.parametrize("error", [
    storage.StaleWrite(),
    storage.InvalidSegment(),
    ProviderKeys.Error(ipc_errors.PROVIDER_STORE_CONFLICT),
])
def test_local_errors_from_the_backend_pass_through_untouched(error: BaseException) -> None:
    store = OrgFsOwnerStore(_WriteFailingFs(error), "space-1")
    with pytest.raises(type(error)) as caught:
        store.write(META, "x", expect_version="v1", create_only=False)
    assert caught.value is error


class _MkdirFailingFs:
    """mkdir raises the configured error; write_text records the call and would succeed."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls: list[str] = []

    def mkdir(self, space_id: str, path: str) -> None:
        self.calls.append("mkdir")
        raise self.error

    def write_text(self, space_id: str, node: str, content: str, **kwargs: Any) -> Any:
        self.calls.append("write_text")
        return type("Info", (), {"version": "v2"})()


@pytest.mark.parametrize("error", [
    OrgFsError("not-a-member"),
    OrgFsError("unknown-space", {"spaceId": "space-1"}),
    OrgFsError("invalid-argument", {"message": "parent is not a directory"}),
    StoreError("io-error"),
    OSError("disk gone"),
])
@pytest.mark.parametrize("conditional", [
    {"expect_version": None, "create_only": True}, {"expect_version": "v1", "create_only": False},
])
def test_parent_mkdir_failure_refuses_before_write_text(error: BaseException, conditional: dict[str, Any]) -> None:
    fs = _MkdirFailingFs(error)
    with pytest.raises(ProviderKeys.Error) as caught:
        OrgFsOwnerStore(fs, "space-1").write(META, "secret-sentinel", **conditional)
    assert caught.value.code == ipc_errors.PROVIDER_STORE_UNAVAILABLE
    assert fs.calls == ["mkdir"]
    assert "secret-sentinel" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
