"""Authority store image safety; current-key remote API remains unimplemented."""

import os
import sqlite3
import json

import pytest

from .support import LocalContext, body, call, state, image_rows


@pytest.mark.parametrize("replacement", ["database", "parent"])
def test_sqlite_never_opens_a_retargeted_disk_path(tmp_path, monkeypatch, replacement):
    context = LocalContext(tmp_path / "home")
    context.provision()
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    external_db = outside / "store.sqlite3"
    connect = sqlite3.connect
    with connect(external_db) as database:
        database.execute("CREATE TABLE sentinel(value TEXT)")
        database.execute("INSERT INTO sentinel VALUES('external-original')")
    database.close()
    external_db.chmod(0o600)
    original = external_db.read_bytes()
    authority = state(context.home) / "authority"
    target = authority / "store.sqlite3"
    opened_disk = []
    raced = []

    def retarget_before_sqlite_open(database, *args, **kwargs):
        if not raced:
            if replacement == "database":
                if target.exists():
                    target.rename(authority / "displaced.sqlite3")
                target.symlink_to(external_db)
            else:
                authority.rename(authority.with_name("displaced-authority"))
                authority.symlink_to(outside, target_is_directory=True)
            raced.append(True)
        if database != ":memory:":
            opened_disk.append(str(database))
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", retarget_before_sqlite_open)
    call(context, body(context))
    assert raced == [True]
    assert external_db.read_bytes() == original
    assert opened_disk == [], "SQLite must never resolve an authority disk pathname"
    assert not (outside / "store.json").exists()


@pytest.mark.parametrize("name", ["store.sqlite3", "store.sqlite3-journal", "store.sqlite3-wal", "store.sqlite3-shm"])
@pytest.mark.parametrize("mutation", ["symlink", "hardlink", "mode"])
def test_database_and_auxiliary_files_are_checked_before_sqlite_use(tmp_path, name, mutation):
    context = LocalContext(tmp_path / "home")
    assert call(context, body(context, csr="bad CSR"))[0] == 400
    parent = state(context.home) / "authority"
    path = parent / name
    outside = tmp_path / "outside-sentinel"
    outside.write_bytes(b"do-not-touch")
    outside.chmod(0o600)
    if path.exists():
        path.rename(parent / "saved-original.sqlite3")
    if mutation == "symlink":
        path.symlink_to(outside)
    elif mutation == "hardlink":
        os.link(outside, path)
    else:
        path.write_bytes(b"unsafe-mode")
        path.chmod(0o644)
    status, response = call(context, body(context))
    assert status == 503
    assert json.loads(response)["receipt"] is None
    assert outside.read_bytes() == b"do-not-touch"


def test_image_capacity_is_rejected_before_publication_with_old_image_unchanged(tmp_path, monkeypatch):
    from hyprial.identity.impl.device.tailnet.authority import store
    context = LocalContext(tmp_path / "home")
    assert call(context, body(context, csr="bad CSR"))[0] == 400
    path = state(context.home) / "authority/store.sqlite3"
    before = path.read_bytes()
    monkeypatch.setattr(store, "MAX_IMAGE_BYTES", len(before) + 4096)
    status, response = call(context, body(context, csr="invalid" + "X" * 40000))
    assert status == 503
    assert json.loads(response)["receipt"] is None
    assert path.read_bytes() == before
    assert len(image_rows(context, "SELECT * FROM operations")) == 1


def test_an_initialized_missing_image_is_not_reset(tmp_path):
    context = LocalContext(tmp_path / "home")
    assert call(context, body(context, csr="bad CSR"))[0] == 400
    path = state(context.home) / "authority/store.sqlite3"
    path.unlink()
    status, response = call(context, body(context))
    assert status == 503
    assert json.loads(response)["error"]["code"] == "UNSAFE_AUTHORITY_STATE"
    assert not path.exists()


def test_corrupt_image_is_unsafe_not_a_retryable_network_failure(tmp_path):
    context = LocalContext(tmp_path / "home")
    assert call(context, body(context, csr="bad CSR"))[0] == 400
    path = state(context.home) / "authority/store.sqlite3"
    path.write_bytes(b"corrupt database image")
    status, response = call(context, body(context))
    result = json.loads(response)
    assert status == 503
    assert result["error"] == {"code": "UNSAFE_AUTHORITY_STATE", "retryable": False}
    assert path.read_bytes() == b"corrupt database image"


def test_partial_store_initialization_is_preserved_without_automatic_reset(tmp_path, monkeypatch):
    from hyprial.identity.impl.device.tailnet.authority import store
    context = LocalContext(tmp_path / "home")

    def interrupt(name):
        if name == "BEFORE_INITIAL_IMAGE":
            raise KeyboardInterrupt

    monkeypatch.setattr(store, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        call(context, body(context))
    parent = state(context.home) / "authority"
    assert (parent / "store.json").exists()
    assert not (parent / "store.sqlite3").exists()
    marker = (parent / "store.json").read_bytes()
    monkeypatch.setattr(store, "_checkpoint", lambda name: None)
    status, raw = call(context, body(context))
    assert status == 503
    assert json.loads(raw)["error"]["code"] == "UNSAFE_AUTHORITY_STATE"
    assert (parent / "store.json").read_bytes() == marker
    assert not (parent / "store.sqlite3").exists()


def test_missing_sqlite_serialization_has_no_disk_path_fallback(tmp_path, monkeypatch):
    context = LocalContext(tmp_path / "home")
    context.provision()
    connect = sqlite3.connect
    targets = []

    class WithoutSerialization:
        def __init__(self):
            self.actual = connect(":memory:")

        def __getattr__(self, name):
            if name in ("serialize", "deserialize"):
                raise AttributeError(name)
            return getattr(self.actual, name)

    def no_serialize(target, *args, **kwargs):
        targets.append(target)
        return WithoutSerialization()

    monkeypatch.setattr(sqlite3, "connect", no_serialize)
    status, response = call(context, body(context))
    assert status == 503
    assert json.loads(response)["error"]["code"] == "UNSAFE_AUTHORITY_STATE"
    assert targets == [":memory:"]
    assert not (state(context.home) / "authority/store.sqlite3").exists()


@pytest.mark.parametrize("field,value", [("tailnetId", "other"), ("caSha256", "0" * 64), ("home", {"device": 0, "inode": 0})])
def test_image_metadata_is_bound_to_home_tailnet_and_ca(tmp_path, field, value):
    context = LocalContext(tmp_path / "home")
    call(context, body(context, csr="seed refusal"))
    path = state(context.home) / "authority/store.sqlite3"
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(path.read_bytes())
        marker = json.loads(connection.execute("SELECT value FROM metadata").fetchone()[0])
        marker[field] = value
        connection.execute("UPDATE metadata SET value=?", (json.dumps(marker, separators=(",", ":")).encode(),))
        connection.commit()
        tampered = connection.serialize()
    finally:
        connection.close()
    path.write_bytes(tampered)
    status, raw = call(context, body(context))
    assert status == 503
    assert json.loads(raw)["error"]["code"] == "UNSAFE_AUTHORITY_STATE"
    assert path.read_bytes() == tampered


@pytest.mark.parametrize("next_operation", ["exact_replay", "new_transaction"])
def test_initialized_image_above_one_mib_remains_usable_below_its_capacity(tmp_path, next_operation):
    from hyprial.identity.impl.device.tailnet.authority.store import MAX_IMAGE_BYTES
    context = LocalContext(tmp_path / "home")
    path = state(context.home) / "authority/store.sqlite3"
    # Grow a real initialized image using valid bounded requests and durable
    # business refusals, not a synthetic oversized/invalid database blob.
    for _ in range(64):
        raw = body(context, csr="malformed CSR " + "X" * 50000)
        assert len(raw) < 65536
        previous = call(context, raw)
        assert previous[0] == 400
        assert json.loads(previous[1])["receipt"] is not None
        if path.stat().st_size > 1024 * 1024:
            break
    assert 1024 * 1024 < path.stat().st_size < MAX_IMAGE_BYTES
    if next_operation == "exact_replay":
        assert call(context, raw) == previous
    else:
        status, response = call(context, body(context, csr="new malformed CSR"))
        assert status == 400
        assert json.loads(response)["receipt"] is not None
    assert 1024 * 1024 < path.stat().st_size < MAX_IMAGE_BYTES
