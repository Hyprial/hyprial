"""S3c: PAC owner resolution has no user-profile source.

A ``users.json`` lying in the state directory is never a candidate source:
short owner names are not guessed from it, a garbage file does not turn
resolution into ``PAC_RESOLUTION_UNAVAILABLE``, and full principal URIs and
registry candidates keep resolving.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.pac.contracts import principals
from hyprial.daemon.impl.pac.contracts.principals import resolve_owner
from hyprial.identity import PAC_OWNER_AMBIGUOUS, PAC_OWNER_UNRESOLVED, PacError
from hyprial.identity.impl.agents.registry._core import AgentRegistry

MACHINE = "test-node"


@pytest.fixture()
def state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    return state


def write_users(state: Path, owner: str) -> None:
    (state / "users.json").write_text(
        json.dumps({
            "version": 1,
            "users": [{
                "owner": owner, "ownerKey": owner, "loginName": owner, "agents": [],
                "preferredReceiver": {"machine": MACHINE, "machineKey": MACHINE},
                "notificationRules": [],
            }],
        }),
        encoding="utf-8",
    )


def test_short_owner_is_not_resolved_from_a_users_json(state: Path) -> None:
    write_users(state, "carol")

    with pytest.raises(PacError) as raised:
        resolve_owner("carol", state_dir=state)

    assert raised.value.code == PAC_OWNER_UNRESOLVED
    assert raised.value.data["candidates"] == []


def test_garbage_users_json_is_not_even_read(state: Path) -> None:
    (state / "users.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(PacError) as raised:
        resolve_owner("carol", state_dir=state)

    assert raised.value.code == PAC_OWNER_UNRESOLVED


def test_registry_candidates_are_the_only_local_source(state: Path) -> None:
    registry = AgentRegistry(state / "agents.sqlite3", owner="allen", machine=MACHINE)
    squire = registry.create("squire").uri
    registry.close()
    write_users(state, "squire")

    with pytest.raises(PacError) as raised:
        resolve_owner("squire", state_dir=state)

    assert raised.value.code == PAC_OWNER_AMBIGUOUS
    candidates = raised.value.data["candidates"]
    assert [candidate["uri"] for candidate in candidates] == [squire]
    assert {candidate["source"] for candidate in candidates} == {"agents-registry"}


def test_full_principal_uri_still_resolves_with_a_users_json_present(state: Path) -> None:
    write_users(state, "carol")

    report = resolve_owner("user:carol", state_dir=state)

    assert report.selected_uri == "user:carol"
    assert report.completeness == "exact"
    assert report.candidates == []


def test_the_profile_source_is_deleted_from_the_module() -> None:
    assert not hasattr(principals, "SOURCE_USER_PROFILES")
    assert not hasattr(principals, "_user_profile_candidates")
