"""Zenoh endpoint configuration: desired-state persistence and daemon merge."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.application import _zenoh_endpoints
from hyprial.daemon.impl.desired_state import DesiredStateStore, ZenohEndpoints


def test_merge_prefers_environment_overrides_per_side() -> None:
    merged = _zenoh_endpoints(
        env_listen=("tcp/127.0.0.1:7447",),
        env_connect=(),
        stored_listen=("tcp/127.0.0.1:7449",),
        stored_connect=("tcp/100.64.30.3:7447",),
    )
    assert merged == (("tcp/127.0.0.1:7447",), ("tcp/100.64.30.3:7447",))


def test_merge_falls_back_to_desired_state_when_environment_is_empty() -> None:
    merged = _zenoh_endpoints(
        env_listen=(),
        env_connect=(),
        stored_listen=("tcp/127.0.0.1:7447",),
        stored_connect=("tcp/127.0.0.1:7448",),
    )
    assert merged == (("tcp/127.0.0.1:7447",), ("tcp/127.0.0.1:7448",))


def test_merge_defaults_to_isolated_when_nothing_is_configured() -> None:
    merged = _zenoh_endpoints(
        env_listen=(),
        env_connect=(),
        stored_listen=(),
        stored_connect=(),
    )
    assert merged == ((), ())


def test_zenoh_endpoints_from_json_rejects_non_string_items() -> None:
    with pytest.raises(ValueError, match="array of non-empty strings"):
        ZenohEndpoints.from_json({"listen": ["tcp/127.0.0.1:7447", 42]}, "zenoh")


def test_store_persists_endpoints_used_by_restart_survival(
    tmp_path: Path,
) -> None:
    path = tmp_path / "desired-state.json"
    store = DesiredStateStore(path)
    store.update_zenoh_endpoints(
        listen=("tcp/127.0.0.1:7447",), connect=("tcp/127.0.0.1:7448",)
    )

    reloaded = DesiredStateStore(path).load()
    assert reloaded.zenoh == ZenohEndpoints(
        listen=("tcp/127.0.0.1:7447",), connect=("tcp/127.0.0.1:7448",)
    )
    # Round-trip through to_json keeps the document valid for the store's own
    # fail-loud validation on every save.
    parsed = DesiredStateStore(path).load()
    assert parsed.zenoh == reloaded.zenoh
