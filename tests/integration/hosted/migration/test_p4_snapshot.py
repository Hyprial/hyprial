"""run-hosted-registry.py's after_p4 states exactly what the UUID P4 open does.

AT01/AT04 compare the old registry before and after the first id-form open
through ``after_p4`` (docs/design/uuid-addressing-implementation-2026-11-03.md
§2.4).  These tests open the same seeded bd6eb960 fixture with the product's
AgentRegistry (no daemon) and pin both directions: the documented rewrite is
the whole difference, and any change outside it -- or a missing P4 step -- makes
the contract comparison fail.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from hyprial.identity import AgentRegistry

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location("hosted_registry_p4", ROOT / "contract/e2e/run-hosted-registry.py")
registry_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry_mod)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "home").mkdir()
    (tmp_path / "state").mkdir()
    registry_mod.seed(tmp_path)
    return tmp_path


def sql(root: Path, statement: str, *params: object) -> None:
    with sqlite3.connect(root / "state/agents.sqlite3") as db:
        db.execute(statement, params)


def open_registry(root: Path) -> None:
    AgentRegistry(root / "state/agents.sqlite3", owner="host-owner", machine="host-node",
                  hyprial_home=root / "home").close()


def migrated(root: Path) -> dict:
    open_registry(root)
    return registry_mod.snapshot(root)


def test_the_p4_open_is_exactly_the_documented_rewrite(root: Path) -> None:
    old = registry_mod.snapshot(root)
    assert old["derivedIds"] == {"agents": None, "agent_secret_grants": None}
    expected = registry_mod.after_p4(old)
    assert migrated(root) == expected
    tokens = {row[0]: row[4] for row in old["rows"]["agents"]}
    ids = {actor: registry_mod.agent_id_for(token) for actor, token in tokens.items()}
    assert sorted(row[3] for row in expected["rows"]["agents"]) == [
        f"agent:host-owner:host-node:{ids['native']}", f"agent:visitor@example.test:host-node:{ids['hosted']}"]
    assert expected["derivedIds"] == {"agents": 0, "agent_secret_grants": 0, "retired_session_refs": 0}
    assert "agent_name_ledger" not in expected["rows"]
    # The fixture's drifted fences keep their name-form uri; only the new keys appear.
    for key, _token, _active, payload in expected["rows"]["lifecycle_resources"]:
        if key.startswith("agent-record:"):
            before = json.loads(dict((row[0], row[3]) for row in old["rows"]["lifecycle_resources"])[key])
            assert json.loads(payload) == {**before, "agentId": ids[key.split(":")[1]], "displayName": None}
    assert expected["files"] == old["files"]
    assert "pins" not in expected["rows"]
    # Idempotent: the next open migrates nothing.
    assert registry_mod.after_p4(expected) == expected
    second = registry_mod.snapshot(root)
    open_registry(root)
    assert registry_mod.snapshot(root) == second


@pytest.mark.parametrize("actor", ["native", "hosted"])
def test_a_fence_that_still_matches_its_row_moves_to_the_id_uri(root: Path, actor: str) -> None:
    old = registry_mod.snapshot(root)
    row = next(row for row in old["rows"]["agents"] if row[0] == actor)
    current = registry_mod.record_json(old, row, row[3])
    payload = {key: value for key, value in current.items() if key not in ("agentId", "displayName")}
    sql(root, "UPDATE lifecycle_resources SET payload = ? WHERE resource_key = ?",
        json.dumps(payload, sort_keys=True), f"agent-record:{actor}")
    old = registry_mod.snapshot(root)
    expected = registry_mod.after_p4(old)
    record = next(json.loads(r[3]) for r in expected["rows"]["lifecycle_resources"] if r[0] == f"agent-record:{actor}")
    assert record["uri"] == f"{row[3].rsplit(':', 1)[0]}:{registry_mod.agent_id_for(row[4])}"
    assert migrated(root) == expected


def test_a_binding_fence_follows_its_agent(root: Path) -> None:
    uri = "agent:host-owner:host-node:native"
    sql(root, "INSERT INTO lifecycle_resources VALUES(?, 'binding-token', 1, ?)",
        f"binding:{uri}", json.dumps({"actor": uri, "harness": "pi"}, sort_keys=True))
    expected = registry_mod.after_p4(registry_mod.snapshot(root))
    assert any(key.startswith("binding:agent:host-owner:host-node:a.") for key, *_ in expected["rows"]["lifecycle_resources"])
    assert migrated(root) == expected


def tamper_home_row(root: Path) -> None:
    sql(root, "UPDATE lifecycle_resources SET payload = replace(payload, 'ready', 'cleaned') "
              "WHERE resource_key = 'agent-home:native'")


def tamper_home_file(root: Path) -> None:
    path = root / "home/agents/native/state/home-receipt.json"
    path.write_bytes(path.read_bytes() + b"\n")


def revert_uri(root: Path) -> None:
    sql(root, "UPDATE agents SET uri = 'agent:host-owner:host-node:native' WHERE actor = 'native'")


def unstamp_grant(root: Path) -> None:
    sql(root, "UPDATE agent_secret_grants SET agent_id = NULL WHERE agent = 'hosted'")


def change_legacy_route_requirement(root: Path) -> None:
    sql(root, "UPDATE agent_secret_grants SET route_required = 1 WHERE agent = 'hosted'")


def move_drifted_fence(root: Path) -> None:
    sql(root, "UPDATE lifecycle_resources SET payload = replace(payload, 'host-node:hosted', ?) "
              "WHERE resource_key = 'agent-record:hosted'",
        "host-node:" + registry_mod.agent_id_for("56104387eed34bd38cc2791a39a6cd38"))


@pytest.mark.parametrize("change", [tamper_home_row, tamper_home_file, revert_uri,
                                    unstamp_grant, change_legacy_route_requirement, move_drifted_fence])
def test_any_change_outside_p4_or_a_missing_p4_step_fails(root: Path, change) -> None:
    old = registry_mod.snapshot(root)
    open_registry(root)
    assert registry_mod.snapshot(root) == registry_mod.after_p4(old)
    change(root)
    assert registry_mod.snapshot(root) != registry_mod.after_p4(old)
