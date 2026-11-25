"""S3c: the runtime capability cache is its own owner-keyed file, never a user profile."""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path
from typing import Any

import pytest

from hyprial.identity import (
    RuntimeCapability,
    RuntimeCapabilityError,
    RuntimeCapabilityStore,
)

PROBED_AT = "2026-11-24T12:00:00Z"
OWNER = "owner-key-1"


def cap(
    harness: str = "claude",
    model: str = "sonnet",
    provider: str | None = None,
    status: str = "available",
    **extra: Any,
) -> RuntimeCapability:
    return RuntimeCapability(
        harness=harness, provider=provider, model=model, status=status,
        probed_at=PROBED_AT, **extra,
    )


def store_at(tmp_path: Path) -> RuntimeCapabilityStore:
    return RuntimeCapabilityStore(tmp_path / "state" / "runtime-capabilities.json")


def write(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        document if isinstance(document, str) else json.dumps(document),
        encoding="utf-8",
    )


def test_absent_file_reads_as_empty_without_creating_it(tmp_path: Path) -> None:
    store = store_at(tmp_path)

    assert store.list(OWNER) == ()
    assert store.get(OWNER, "claude", "sonnet") is None
    assert not store.path.exists()


def test_set_creates_the_file_with_private_modes_and_only_the_spec_keys(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    first = cap()

    assert store.set(OWNER, first) == ("runtimeCapabilities",)

    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert set(document) == {"version", "owners"}
    assert document["version"] == 1
    [owner] = document["owners"]
    assert set(owner) == {"ownerKey", "capabilities"}
    assert owner["ownerKey"] == OWNER
    assert owner["capabilities"] == [first.to_json()]
    assert store.list(OWNER) == (first,)
    assert store.get(OWNER, "claude", "sonnet") == first


def test_get_is_an_exact_combo_match(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    pi = cap("pi", "k3", provider="kimi-coding")
    store.set(OWNER, pi)

    assert store.get(OWNER, "pi", "k3", "kimi-coding") == pi
    assert store.get(OWNER, "pi", "k3", "other") is None
    assert store.get(OWNER, "pi", "k3") is None
    assert store.get(OWNER, "pi", "k4", "kimi-coding") is None
    assert store.get("someone-else", "pi", "k3", "kimi-coding") is None
    assert store.list("someone-else") == ()


def test_upsert_by_combo_replaces_and_keeps_other_combos(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    other = cap("codex", "gpt-x")
    store.set(OWNER, cap())
    store.set(OWNER, other)
    failed = cap(status="unavailable", reason="usage-limit", detail="out")

    assert store.set(OWNER, failed) == ("runtimeCapabilities",)

    assert store.get(OWNER, "claude", "sonnet") == failed
    assert store.get(OWNER, "codex", "gpt-x") == other
    assert len(store.list(OWNER)) == 2


def test_owners_are_isolated(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.set("owner-a", cap())
    store.set("owner-b", cap(status="unavailable", reason="usage-limit"))

    assert store.get("owner-a", "claude", "sonnet").status == "available"
    assert store.get("owner-b", "claude", "sonnet").status == "unavailable"
    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert sorted(item["ownerKey"] for item in document["owners"]) == [
        "owner-a", "owner-b",
    ]


def test_no_op_set_reports_nothing_and_keeps_bytes_and_mtime(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.set(OWNER, cap())
    before_bytes = store.path.read_bytes()
    os.utime(store.path, ns=(1_000_000_000, 1_000_000_000))
    before_mtime = store.path.stat().st_mtime_ns

    assert store.set(OWNER, cap()) == ()

    assert store.path.read_bytes() == before_bytes
    assert store.path.stat().st_mtime_ns == before_mtime


def test_state_survives_a_new_store_object(tmp_path: Path) -> None:
    store_at(tmp_path).set(OWNER, cap())

    assert store_at(tmp_path).get(OWNER, "claude", "sonnet") == cap()


def test_invalid_capability_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    store = store_at(tmp_path)

    with pytest.raises(RuntimeCapabilityError):
        store.set(OWNER, cap("pi", "k3"))
    with pytest.raises(RuntimeCapabilityError):
        store.set(OWNER, cap(status="unavailable"))

    assert not store.path.exists()


def test_runtime_capability_error_is_a_value_error() -> None:
    assert issubclass(RuntimeCapabilityError, ValueError)


@pytest.mark.parametrize("round_trip", [cap(), cap("pi", "k3", provider="kimi-coding",
                                                    status="unavailable",
                                                    reason="usage-limit",
                                                    detail="quota",
                                                    retry_at="2026-11-25T00:00:00Z")])
def test_runtime_capability_keeps_the_verbatim_json_shape(
    round_trip: RuntimeCapability,
) -> None:
    parsed = RuntimeCapability.from_json(round_trip.to_json(), "capability")

    assert parsed == round_trip
    assert parsed.combo == (round_trip.harness, round_trip.provider, round_trip.model)


def test_two_store_objects_setting_different_combos_lose_nothing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state" / "runtime-capabilities.json"
    combos = [cap("claude", f"model-{index}") for index in range(12)]
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def writer(stores: RuntimeCapabilityStore, mine: list[RuntimeCapability]) -> None:
        try:
            barrier.wait(5)
            for item in mine:
                stores.set(OWNER, item)
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    threads = [
        threading.Thread(
            target=writer, args=(RuntimeCapabilityStore(path), combos[offset::2])
        )
        for offset in (0, 1)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert errors == []
    stored = RuntimeCapabilityStore(path).list(OWNER)
    assert {item.combo for item in stored} == {item.combo for item in combos}
    assert len(stored) == len(combos)


CORRUPT_DOCUMENTS = {
    "unreadable-json": "{not json",
    "not-an-object": [],
    "wrong-version": {"version": 2, "owners": []},
    "missing-version": {"owners": []},
    "owners-not-array": {"version": 1, "owners": {}},
    "duplicate-owner-key": {
        "version": 1,
        "owners": [
            {"ownerKey": OWNER, "capabilities": []},
            {"ownerKey": OWNER, "capabilities": []},
        ],
    },
    "duplicate-combo": {
        "version": 1,
        "owners": [{
            "ownerKey": OWNER,
            "capabilities": [cap().to_json(), cap().to_json()],
        }],
    },
    "invalid-capability": {
        "version": 1,
        "owners": [{
            "ownerKey": OWNER,
            "capabilities": [{"harness": "emacs", "model": "x"}],
        }],
    },
}


@pytest.mark.parametrize("name", sorted(CORRUPT_DOCUMENTS))
def test_corrupt_file_fails_loud_on_every_operation(
    tmp_path: Path, name: str
) -> None:
    store = store_at(tmp_path)
    write(store.path, CORRUPT_DOCUMENTS[name])
    before = store.path.read_bytes()

    with pytest.raises(RuntimeCapabilityError):
        store.list(OWNER)
    with pytest.raises(RuntimeCapabilityError):
        store.get(OWNER, "claude", "sonnet")
    with pytest.raises(RuntimeCapabilityError):
        store.set(OWNER, cap("codex", "gpt-x"))

    assert store.path.read_bytes() == before


def test_importable_from_the_identity_facade() -> None:
    import hyprial.identity as identity

    for name in ("RuntimeCapability", "RuntimeCapabilityError", "RuntimeCapabilityStore"):
        assert hasattr(identity, name), name
