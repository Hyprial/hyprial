"""Feature gate providerKeys.authority (contract §8.7): default legacy, fail closed."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.provider_keys.storage import gate
from hyprial.identity import ProviderKeys
from hyprial.kernel import ipc_errors


def _settings(home: Path, text: str) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(text, encoding="utf-8")
    return home


def test_absent_home_or_file_is_legacy(tmp_path: Path) -> None:
    assert gate.authority(None) == "legacy"
    assert gate.authority(tmp_path / "missing") == "legacy"
    assert gate.authority(tmp_path) == "legacy"


@pytest.mark.parametrize("record", [{}, {"owner": "x"}, {"providerKeys": {}}, {"providerKeys": {"other": 1}}])
def test_absent_section_or_key_is_legacy(tmp_path: Path, record: dict) -> None:
    assert gate.authority(_settings(tmp_path, json.dumps(record))) == "legacy"


@pytest.mark.parametrize("value", ["legacy", "owner-keys"])
def test_known_values(tmp_path: Path, value: str) -> None:
    home = _settings(tmp_path, json.dumps({"providerKeys": {"authority": value}}))
    assert gate.authority(home) == value
    assert gate.authority(str(home)) == value


@pytest.mark.parametrize("text", [
    json.dumps({"providerKeys": {"authority": "Owner-Keys"}}),
    json.dumps({"providerKeys": {"authority": " owner-keys"}}),
    json.dumps({"providerKeys": {"authority": "owner"}}),
    json.dumps({"providerKeys": {"authority": None}}),
    json.dumps({"providerKeys": {"authority": True}}),
    json.dumps({"providerKeys": "owner-keys"}),
    json.dumps({"providerKeys": None}),
    json.dumps(["providerKeys"]),
    "{not json",
    "",
    "\udcff",
])
def test_unknown_or_corrupt_fails_closed(tmp_path: Path, text: str) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_bytes(text.encode("utf-8", "surrogateescape"))
    with pytest.raises(ProviderKeys.Error) as caught:
        gate.authority(home)
    assert caught.value.code == ipc_errors.PROVIDER_STORE_UNAVAILABLE


@pytest.mark.parametrize("record", [
    {"provider_keys": {"authority": "owner-keys"}},
    {"provider_keys": {"authority": "legacy"}},
    {"provider_keys": {}},
    {"provider_keys": None},
    {"providerKeys": {"authority": "legacy"}, "provider_keys": {"authority": "owner-keys"}},
    {"providerKeys": {"authority": "owner-keys"}, "provider_keys": {"authority": "owner-keys"}},
    {"provider_keys.authority": "owner-keys"},
    {"providerKeys.authority": "owner-keys"},
])
def test_frozen_logical_name_spelling_is_refused_not_ignored(tmp_path: Path, record: dict) -> None:
    with pytest.raises(ProviderKeys.Error) as caught:
        gate.authority(_settings(tmp_path, json.dumps(record)))
    assert caught.value.code == ipc_errors.PROVIDER_STORE_UNAVAILABLE


def test_persisted_key_is_camel_case_section() -> None:
    assert (gate.SECTION, gate.KEY) == ("providerKeys", "authority")


def test_unreadable_settings_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "settings.json").mkdir()
    with pytest.raises(ProviderKeys.Error) as caught:
        gate.authority(tmp_path)
    assert caught.value.code == ipc_errors.PROVIDER_STORE_UNAVAILABLE


def test_no_environment_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HYPRIAL_PROVIDER_KEYS_AUTHORITY", "HYPRIAL_PROVIDER_KEYS", "PROVIDER_KEYS_AUTHORITY"):
        monkeypatch.setenv(name, "owner-keys")
    assert gate.authority(tmp_path) == "legacy"
    assert not [name for name in vars(gate) if "write" in name.lower() or "set" in name.lower()]
