"""S3c: persistent configuration is channels only; ``state/users.json`` is not read."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.kernel import PersistentConfigStore


@pytest.fixture()
def homes(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    (home / "channels.json").write_text(
        json.dumps({"version": 1, "gateways": []}), encoding="utf-8"
    )
    return home, state


def test_a_channels_only_home_loads(homes: tuple[Path, Path]) -> None:
    home, state = homes

    configuration = PersistentConfigStore(home, state).load()

    assert configuration.channels.gateways == ()


def test_a_home_without_channels_json_loads_empty(tmp_path: Path) -> None:
    home = tmp_path / "bare"
    (home / "state").mkdir(parents=True)

    configuration = PersistentConfigStore(home, home / "state").load()

    assert configuration.channels.gateways == ()


@pytest.mark.parametrize(
    "garbage",
    ["{not json", "[]", json.dumps({"version": 9, "users": "x"}),
     json.dumps({"version": 1, "users": [{"ownerKey": "a"}, {"ownerKey": "a"}]})],
)
def test_a_garbage_users_json_beside_it_does_not_break_loading(
    homes: tuple[Path, Path], garbage: str
) -> None:
    home, state = homes
    (state / "users.json").write_text(garbage, encoding="utf-8")

    configuration = PersistentConfigStore(home, state).load()

    assert configuration.channels.gateways == ()
    assert (state / "users.json").read_text(encoding="utf-8") == garbage


def test_the_loaded_configuration_has_no_users(homes: tuple[Path, Path]) -> None:
    home, state = homes

    configuration = PersistentConfigStore(home, state).load()

    assert not hasattr(configuration, "users")
    assert [field for field in type(configuration).__dataclass_fields__] == ["channels"]


def test_the_user_profiles_vocabulary_is_gone_from_the_kernel() -> None:
    import hyprial.kernel as kernel
    from hyprial.kernel.impl.configuration import persistent_config

    assert not hasattr(kernel, "UserProfiles")
    assert not hasattr(persistent_config, "UserProfiles")
