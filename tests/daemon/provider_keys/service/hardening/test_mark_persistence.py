"""Durable rollback marks are persisted before success and never written torn [security].

A restart is simulated by dropping the in-process mark cache on the app, so the next
service is built from the durable marks under the local state directory only.
Where the service also commits to OrgFS (whose file store renames too), the
injected ``os.replace`` failure is limited to the high-water marks file.
"""
from __future__ import annotations

import json
import os

import pytest

from hyprial.daemon.impl.provider_keys import storage
from hyprial.daemon.impl.provider_keys.api import GRANT_TTL_MS
from hyprial.daemon.impl.provider_keys.ops.marks import HighWater
from hyprial.identity import ProviderKeys
from hyprial.kernel import DaemonRequestError, ipc_errors

from ..conftest import HOUR, NOW, SECRET, SUB

LATER = NOW + GRANT_TTL_MS + HOUR
PATH = "directory/provider-grants/a/b.json"


def _code(call) -> str:
    with pytest.raises(DaemonRequestError) as caught:
        call()
    assert SECRET not in str(caught.value)
    return caught.value.code


def _restart(world) -> None:
    if hasattr(world.app, "_provider_key_high_water"):
        del world.app._provider_key_high_water


def _marks(world) -> dict:
    path = next((world.state / "provider-keys").glob("high-water-*.json"))
    return json.loads(path.read_text())["marks"]


def _fail_replace_once(monkeypatch) -> None:
    real = os.replace
    failing = [True]

    def flaky(*args, **kwargs):
        if failing[0]:
            failing[0] = False
            raise OSError(28, "No space left on device")
        return real(*args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky)


def _fail_mark_replace_once(monkeypatch) -> None:
    real = os.replace
    failing = [True]

    def flaky(source, target, *args, **kwargs):
        name = os.path.basename(os.fspath(target))
        if failing[0] and name.startswith("high-water-") and name.endswith(".json"):
            failing[0] = False
            raise OSError(28, "No space left on device")
        return real(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky)


def test_a_revoke_mark_whose_save_failed_is_saved_by_the_retry(world, monkeypatch):
    world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    path = storage.grant_path(ref.agent, "anthropic")
    active = world.store.read(path)[0]
    _fail_mark_replace_once(monkeypatch)
    assert _code(lambda: world.service().revoke(agent=ref, grant_id="anthropic")) == \
        ipc_errors.PROVIDER_STORE_UNAVAILABLE
    again = world.service().revoke(agent=ref, grant_id="anthropic")
    assert again["changed"] is False and again["grant"]["state"] == "revoked"
    assert _marks(world)[path][0] == 2
    found = world.store.read(path)
    world.store.write(path, active, expect_version=found[1], create_only=False)
    _restart(world)
    assert _code(lambda: world.service().launch_secrets(ref, now_ms=LATER)) == \
        ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID


def test_a_failed_save_keeps_the_previous_in_memory_mark(tmp_path, monkeypatch):
    marks = HighWater(tmp_path, SUB)
    first = {"seq": 1, "type": "provider-grant-state"}
    second = {**first, "seq": 2}
    marks.observe(PATH, first)
    _fail_replace_once(monkeypatch)
    with pytest.raises(ProviderKeys.Error) as caught:
        marks.observe(PATH, second)
    assert caught.value.code == ipc_errors.PROVIDER_STORE_UNAVAILABLE
    assert marks._seen[PATH][0] == 1
    marks.observe(PATH, second)
    assert HighWater(tmp_path, SUB)._seen[PATH][0] == 2


def test_a_short_write_never_replaces_the_marks_file(world, monkeypatch):
    world.login()
    ref = world.ref()
    real = os.write
    short = [True]

    def partial(fd, data):
        if short[0] and len(data) > 20:
            short[0] = False
            return real(fd, data[: len(data) // 2])
        return real(fd, data)

    monkeypatch.setattr(os, "write", partial)
    world.service().allow(agent=ref, provider="anthropic")
    monkeypatch.setattr(os, "write", real)
    _restart(world)
    rows = world.service().status(agent=ref)["grants"]
    assert [row["state"] for row in rows] == ["ok"]
    assert storage.grant_path(ref.agent, "anthropic") in _marks(world)


@pytest.mark.parametrize("cached", [False, True])
def test_cached_observation_refuses_another_instances_higher_mark(tmp_path, cached):
    first = HighWater(tmp_path, SUB)
    initial = {"seq": 1, "state": "active"}
    if cached:
        first.observe(PATH, initial)
    second = HighWater(tmp_path, SUB)
    second.observe(PATH, {"seq": 2, "state": "revoked"})
    with pytest.raises(ProviderKeys.Error) as caught:
        first.observe(PATH, initial)
    assert caught.value.code == ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID
    assert first.mark(PATH)[0] == 2


def test_mark_and_slot_inventory_refresh_another_instances_commits(tmp_path):
    first = HighWater(tmp_path, SUB)
    second = HighWater(tmp_path, SUB)
    payload = {"seq": 3, "state": "revoked"}
    second.observe(PATH, payload)
    assert first.mark(PATH) == (3, ProviderKeys.record_digest(payload))
    second.observe("directory/provider-grants/a/c.json", {"seq": 1, "state": "active"})
    assert first.marked_names("directory/provider-grants/a") == ("b.json", "c.json")
    first.observe("directory/provider-grants/a/d.json", {"seq": 1, "state": "active"})
    assert second.marked_names("directory/provider-grants/a") == ("b.json", "c.json", "d.json")
    assert HighWater(tmp_path, SUB).marked_names("directory/provider-grants/a") == ("b.json", "c.json", "d.json")


@pytest.mark.parametrize("reader", ["observe", "mark", "marked_names"])
def test_cached_reads_refuse_a_damaged_durable_file(tmp_path, reader):
    marks = HighWater(tmp_path, SUB)
    payload = {"seq": 1, "state": "active"}
    marks.observe(PATH, payload)
    next((tmp_path / "provider-keys").glob("high-water-*.json")).write_text("{")
    with pytest.raises(ProviderKeys.Error) as caught:
        if reader == "observe":
            marks.observe(PATH, payload)
        elif reader == "mark":
            marks.mark(PATH)
        else:
            marks.marked_names("directory/provider-grants/a")
    assert caught.value.code == ipc_errors.PROVIDER_STORE_UNAVAILABLE


@pytest.mark.parametrize("reader", ["observe", "mark", "marked_names"])
def test_cached_reads_refuse_same_sequence_different_digest(tmp_path, reader):
    marks = HighWater(tmp_path, SUB)
    payload = {"seq": 1, "state": "active"}
    marks.observe(PATH, payload)
    path = next((tmp_path / "provider-keys").glob("high-water-*.json"))
    altered = json.loads(path.read_text())
    altered["marks"][PATH][1] = ProviderKeys.record_digest({"seq": 1, "state": "revoked"})
    path.write_text(json.dumps(altered))
    with pytest.raises(ProviderKeys.Error) as caught:
        if reader == "observe":
            marks.observe(PATH, payload)
        elif reader == "mark":
            marks.mark(PATH)
        else:
            marks.marked_names("directory/provider-grants/a")
    assert caught.value.code == ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID


def test_an_existing_service_sees_another_instances_deleted_slot_and_regrant_floor(world):
    world.login()
    ref = world.ref()
    first = world.service()
    _restart(world)
    second = world.service()
    assert first._ctx.high_water is not second._ctx.high_water
    second.allow(agent=ref, provider="anthropic")
    second.revoke(agent=ref, grant_id="anthropic")
    world.store.remove(storage.grant_path(ref.agent, "anthropic"))
    assert first.slot_names(ref) == frozenset({"anthropic"})
    assert _code(lambda: first.launch_secrets(ref, now_ms=NOW + 1)) == \
        ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID
    result = first.allow(agent=ref, provider="anthropic")
    assert result["changed"] is True and result["grant"]["seq"] == 3
    assert second._ctx.high_water.mark(storage.grant_path(ref.agent, "anthropic"))[0] == 3
