"""Local state-directory OwnerStore adapter (J22: credentials/grants stored locally)."""
from __future__ import annotations

import json
import multiprocessing
import os
import stat
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.provider_keys import storage
from hyprial.identity import ProviderKeys
from hyprial.kernel import ipc_errors

KEY_ID = "pk.mfrggzdfmztwq2lknnwg23tpob"
AGENT = "0123abcd-4567-4890-abcd-ef0123456789"
META = storage.key_meta_path(KEY_ID)


def _cas_worker(home_str: str, path: str, expect_version: str | None, create_only: bool,
                value: str, sync_dir_str: str) -> None:
    sync_dir = Path(sync_dir_str)
    store = storage.LocalSpaceResolver(Path(home_str)).store_for("u.alice")
    (sync_dir / f"{value}.ready").write_text("1", encoding="utf-8")
    deadline = time.monotonic() + 10.0
    while not (sync_dir / "go").exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    try:
        version = store.write(path, value, expect_version=expect_version, create_only=create_only)
        outcome = [value, "ok", version]
    except storage.StaleWrite:
        outcome = [value, "stale", None]
    (sync_dir / f"{value}.json").write_text(json.dumps(outcome), encoding="utf-8")


def test_local_resolver_stores_under_hyprial_home_state(tmp_path: Path) -> None:
    resolver = storage.LocalSpaceResolver(tmp_path)
    assert isinstance(resolver, storage.OwnerSpaceResolver)

    store = resolver.store_for("u.alice")
    assert isinstance(store, storage.LocalOwnerStore)
    assert isinstance(store, storage.OwnerStore)

    version = store.write(META, '{"a":1}', expect_version=None, create_only=True)
    assert version
    assert store.read(META) == ('{"a":1}', version)
    assert store.listdir(storage.KEYS_ROOT) == (KEY_ID,)
    assert store.listdir(storage.key_dir(KEY_ID)) == ("meta.json",)

    state_root = tmp_path / "state" / "provider-keys" / "store"
    assert state_root.is_dir()
    assert any(path.name == "meta.json" and path.read_text() == '{"a":1}' for path in state_root.rglob("meta.json"))


def test_local_store_cas_and_remove_semantics(tmp_path: Path) -> None:
    store = storage.LocalSpaceResolver(tmp_path).store_for("u.alice")
    with pytest.raises(storage.StaleWrite):
        store.write(META, "one", expect_version="missing", create_only=False)

    v1 = store.write(META, "one", expect_version=None, create_only=True)
    with pytest.raises(storage.StaleWrite):
        store.write(META, "two", expect_version=None, create_only=True)

    v2 = store.write(META, "two", expect_version=v1, create_only=False)
    assert v2 != v1
    with pytest.raises(storage.StaleWrite):
        store.write(META, "three", expect_version=v1, create_only=False)
    assert store.read(META) == ("two", v2)

    grant = storage.grant_path(AGENT, "openai")
    store.write(grant, "{}", expect_version=None, create_only=True)
    assert store.listdir(storage.grants_dir(AGENT)) == ("openai.json",)
    store.remove(grant)
    assert store.read(grant) is None
    assert store.listdir(storage.grants_dir(AGENT)) == ()
    store.remove(grant)


def test_local_store_refuses_invalid_paths_and_blind_writes(tmp_path: Path) -> None:
    store = storage.LocalSpaceResolver(tmp_path).store_for("u.alice")
    for path in ("directory/user-keys/x.json", "directory/provider-keys/../user-keys/x", "/directory/provider-keys/x"):
        with pytest.raises(storage.InvalidSegment):
            store.read(path)
        with pytest.raises(storage.InvalidSegment):
            store.write(path, "x", expect_version=None, create_only=True)
        with pytest.raises(storage.InvalidSegment):
            store.remove(path)
    with pytest.raises(storage.InvalidSegment):
        store.write(META, "x", expect_version=None, create_only=False)
    with pytest.raises(storage.InvalidSegment):
        store.write(META, "x", expect_version="v1", create_only=True)


def test_local_store_cas_is_atomic_across_instances_and_processes(tmp_path: Path) -> None:
    resolver = storage.LocalSpaceResolver(tmp_path)
    store_a = resolver.store_for("u.alice")
    store_b = resolver.store_for("u.alice")
    v1 = store_a.write(META, "v1", expect_version=None, create_only=True)
    v2 = store_a.write(META, "v2", expect_version=v1, create_only=False)
    assert v2 != v1
    with pytest.raises(storage.StaleWrite):
        store_b.write(META, "stale", expect_version=v1, create_only=False)

    sync_dir = tmp_path / "sync"
    sync_dir.mkdir(mode=0o700)
    ctx = multiprocessing.get_context("spawn")
    p1 = ctx.Process(target=_cas_worker, args=(str(tmp_path), META, v2, False, "one", str(sync_dir)))
    p2 = ctx.Process(target=_cas_worker, args=(str(tmp_path), META, v2, False, "two", str(sync_dir)))
    p1.start()
    p2.start()
    try:
        deadline = time.monotonic() + 10.0
        while not ((sync_dir / "one.ready").exists() and (sync_dir / "two.ready").exists()):
            assert time.monotonic() < deadline
            time.sleep(0.005)
        (sync_dir / "go").write_text("1", encoding="utf-8")
        p1.join(timeout=15)
        p2.join(timeout=15)
        assert p1.exitcode == 0 and p2.exitcode == 0
    finally:
        p1.close()
        p2.close()
    outcomes = [
        json.loads((sync_dir / "one.json").read_text(encoding="utf-8")),
        json.loads((sync_dir / "two.json").read_text(encoding="utf-8")),
    ]
    winners = [item for item in outcomes if item[1] == "ok"]
    losers = [item for item in outcomes if item[1] == "stale"]
    assert len(winners) == 1 and len(losers) == 1
    assert store_a.read(META) == (winners[0][0], winners[0][2])


def test_local_store_rejects_torn_version_record(tmp_path: Path) -> None:
    store = storage.LocalSpaceResolver(tmp_path).store_for("u.alice")
    store.write(META, "one", expect_version=None, create_only=True)
    owner_root = next((tmp_path / "state" / "provider-keys" / "store").iterdir())
    for ver in (owner_root / ".versions").glob("*.ver"):
        ver.unlink()
    with pytest.raises(ProviderKeys.Error) as caught:
        store.read(META)
    assert caught.value.code == ipc_errors.PROVIDER_STORE_UNAVAILABLE


def test_local_store_rejects_symlinks_and_enforces_private_modes(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)

    home_link = tmp_path / "home-link"
    home_link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ProviderKeys.Error) as caught:
        storage.LocalSpaceResolver(home_link)
    assert caught.value.code == ipc_errors.PROVIDER_STORE_UNAVAILABLE

    missing_home = tmp_path / "missing-home"
    with pytest.raises(ProviderKeys.Error) as caught:
        storage.LocalSpaceResolver(missing_home)
    assert caught.value.code == ipc_errors.PROVIDER_STORE_UNAVAILABLE
    assert not missing_home.exists()

    loose_home = tmp_path / "loose-home"
    loose_home.mkdir(mode=0o755)
    os.chmod(loose_home, 0o755)
    with pytest.raises(ProviderKeys.Error) as caught:
        storage.LocalSpaceResolver(loose_home)
    assert caught.value.code == ipc_errors.PROVIDER_STORE_UNAVAILABLE
    assert not (loose_home / "state").exists()

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    pre_store = home / "state" / "provider-keys" / "store"
    pre_store.mkdir(parents=True, mode=0o755)
    os.chmod(pre_store, 0o755)
    store = storage.LocalSpaceResolver(home).store_for("u.alice")
    store.write(META, '{"k":1}', expect_version=None, create_only=True)

    assert stat.S_IMODE((home / "state").stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "state" / "provider-keys").stat().st_mode) == 0o700
    assert stat.S_IMODE(pre_store.stat().st_mode) == 0o700
    owner_root = next(pre_store.iterdir())
    assert stat.S_IMODE(owner_root.stat().st_mode) == 0o700
    assert stat.S_IMODE((owner_root / "directory" / "provider-keys" / KEY_ID).stat().st_mode) == 0o700
    assert stat.S_IMODE((owner_root / "directory" / "provider-keys" / KEY_ID / "meta.json").stat().st_mode) == 0o600

    escape_home = tmp_path / "escape-home"
    escape_home.mkdir(mode=0o700)
    (escape_home / "state" / "provider-keys").mkdir(parents=True, mode=0o700)
    (escape_home / "state" / "provider-keys" / "store").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ProviderKeys.Error) as caught:
        storage.LocalSpaceResolver(escape_home).store_for("u.alice").write(
            META, "escape", expect_version=None, create_only=True
        )
    assert caught.value.code == ipc_errors.PROVIDER_STORE_UNAVAILABLE
    assert list(outside.iterdir()) == []

    part_home = tmp_path / "part-home"
    part_home.mkdir(mode=0o700)
    part_store = storage.LocalSpaceResolver(part_home).store_for("u.alice")
    part_store.write(META, "ok", expect_version=None, create_only=True)
    part_root = next((part_home / "state" / "provider-keys" / "store").iterdir())
    other_key = "pk.aaaaaaaaaaaaaaaaaaaaaaaaaa"
    (part_root / "directory" / "provider-keys" / other_key).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ProviderKeys.Error) as caught:
        part_store.write(storage.key_meta_path(other_key), "escape", expect_version=None, create_only=True)
    assert caught.value.code == ipc_errors.PROVIDER_STORE_UNAVAILABLE
    assert list(outside.iterdir()) == []


def test_local_store_hardens_existing_nested_dirs_and_cas_lock_on_read_and_list(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    store = storage.LocalSpaceResolver(home).store_for("u.alice")
    v1 = store.write(META, '{"k":1}', expect_version=None, create_only=True)

    owner_root = next((home / "state" / "provider-keys" / "store").iterdir())
    dir_layer = owner_root / "directory"
    keys_dir = dir_layer / "provider-keys"
    part_dir = keys_dir / KEY_ID
    lock_file = owner_root / ".cas.lock"

    for path in (dir_layer, keys_dir, part_dir):
        os.chmod(path, 0o755)
    os.chmod(lock_file, 0o644)

    assert store.read(META) == ('{"k":1}', v1)
    for path in (dir_layer, keys_dir, part_dir):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert stat.S_IMODE(lock_file.stat().st_mode) == 0o600

    for path in (keys_dir, part_dir):
        os.chmod(path, 0o755)
    os.chmod(lock_file, 0o644)

    assert store.listdir(storage.KEYS_ROOT) == (KEY_ID,)
    assert store.listdir(storage.key_dir(KEY_ID)) == ("meta.json",)
    for path in (keys_dir, part_dir):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert stat.S_IMODE(lock_file.stat().st_mode) == 0o600

    os.chmod(lock_file, 0o644)
    v2 = store.write(META, '{"k":2}', expect_version=v1, create_only=False)
    assert v2 != v1
    assert stat.S_IMODE(lock_file.stat().st_mode) == 0o600

    outside_lock = tmp_path / "outside-lock"
    outside_lock.write_text("x", encoding="utf-8")
    os.chmod(outside_lock, 0o644)
    lock_file.unlink()
    lock_file.symlink_to(outside_lock)
    with pytest.raises(ProviderKeys.Error) as caught:
        store.read(META)
    assert caught.value.code == ipc_errors.PROVIDER_STORE_UNAVAILABLE
    with pytest.raises(ProviderKeys.Error) as caught_write:
        store.write(META, '{"k":3}', expect_version=v2, create_only=False)
    assert caught_write.value.code == ipc_errors.PROVIDER_STORE_UNAVAILABLE
    assert stat.S_IMODE(outside_lock.stat().st_mode) == 0o644
