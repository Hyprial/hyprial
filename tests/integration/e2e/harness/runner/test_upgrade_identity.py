"""A legacy CLI projection never substitutes for independent generation proof."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from contract.e2e._upgrade_continuity import channel_probe, identity as identities, installation
from tests.support.runtime.environment import isolated_environment


def facts(reported_epochs: bool):
    before = {"pid": 101, "version": "old"}
    after = {"pid": 202, "version": "new"}
    if reported_epochs:
        before["epoch"] = "old-epoch"
        after["epoch"] = "new-epoch"
    live = {"pid": 202, "version": "new", "epoch": "new-epoch", "running": True, "phase": "serving"}
    return before, after, live


def verify(before, after, live):
    return installation.verify_restart_identity(
        before, after, live,
        old_pid=101, old_epoch="old-epoch", old_version="old", new_version="new",
    )


@pytest.mark.parametrize("reported_epochs", [False, True], ids=["legacy-projection", "current-projection"])
def test_independent_live_generation_is_required(reported_epochs: bool) -> None:
    assert verify(*facts(reported_epochs)) == "new-epoch"


@pytest.mark.parametrize(
    "where,key,value",
    [
        (0, "pid", 999), (0, "version", "wrong"),
        (1, "pid", 101), (1, "pid", 999), (1, "version", "wrong"),
        (2, "pid", 999), (2, "version", "wrong"), (2, "running", False),
        (2, "phase", "starting"), (2, "epoch", "old-epoch"), (2, "epoch", ""),
    ],
)
@pytest.mark.parametrize("reported_epochs", [False, True])
def test_cli_success_cannot_hide_wrong_or_unready_generation(
    reported_epochs: bool, where: int, key: str, value: object
) -> None:
    values = deepcopy(facts(reported_epochs))
    values[where][key] = value
    with pytest.raises(AssertionError):
        verify(*values)


@pytest.mark.parametrize("where,value", [(0, "wrong"), (1, "wrong"), (0, None), (1, None)])
def test_any_reported_epoch_must_match_exactly(where: int, value: object) -> None:
    values = facts(True)
    values[where]["epoch"] = value
    with pytest.raises(AssertionError):
        verify(*values)


@pytest.mark.parametrize("where", [0, 1])
def test_partial_epoch_projection_is_not_legacy(where: int) -> None:
    values = facts(True)
    del values[where]["epoch"]
    with pytest.raises(AssertionError):
        verify(*values)


OLD_ACTOR = "agent:e2e:e2e-upgrade-continuity:channel-probe"
AGENT_ID = "a." + "a" * 26
CURRENT_ACTOR = "agent:e2e:e2e-upgrade-continuity:" + AGENT_ID


class ProbeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def actor_identity():
    before = {
        "actor": "channel-probe", "owner": "e2e", "machine": "e2e-upgrade-continuity",
        "agentId": AGENT_ID, "uri": OLD_ACTOR,
    }
    return {"before": before, "current": {**before, "uri": CURRENT_ACTOR}}


def channel_row():
    return {
        "actor": CURRENT_ACTOR, "sessionRef": "original-session", "ownerFence": True,
        "channelBuildVersion": "old", "channelProtocolVersion": 2,
        "channelGeneration": "current", "channelCurrentThisGeneration": True,
        "channelCurrentEpoch": "new-epoch", "channelRecentlyObserved": True,
        "channelAlive": True,
    }


def require_current_channel(row, identity):
    return channel_probe.wait_for_channel_current(
        {"ps": lambda _env: {"interactiveSessions": [row]}, "time": ProbeClock(),
         "_channel_diagnostic": lambda _path: "controlled", "actor_identity": identity},
        Path("/fixture/hyprial"), {}, actor=OLD_ACTOR, session_ref="original-session",
        build_version="old", process=SimpleNamespace(pid=71, poll=lambda: None), error_log=None,
        daemon_epoch="new-epoch", stage="old-channel-refreshed-on-new-daemon",
        protocol_version=2, generation="current", require_operational_liveness=True,
        timeout=0.3,
    )


def test_fenced_current_channel_matches_independent_registry_incarnation_after_migration():
    row = channel_row()
    assert require_current_channel(row, actor_identity()) is row


@pytest.mark.parametrize("key,value", [
    ("actor", "agent:e2e:e2e-upgrade-continuity:a." + "b" * 26),
    ("sessionRef", "foreign-session"), ("ownerFence", False),
    ("channelBuildVersion", "new"), ("channelProtocolVersion", 1),
    ("channelGeneration", "legacy_or_unknown"), ("channelCurrentThisGeneration", False),
    ("channelCurrentEpoch", "old-epoch"), ("channelRecentlyObserved", False),
    ("channelAlive", False),
])
def test_registry_actor_mapping_does_not_relax_channel_fences(key, value):
    row = {**channel_row(), key: value}
    with pytest.raises(AssertionError):
        require_current_channel(row, actor_identity())


def test_registry_mapping_cannot_accept_a_recreated_incarnation():
    identity = actor_identity()
    identity["current"]["agentId"] = "a." + "b" * 26
    with pytest.raises(AssertionError):
        require_current_channel(channel_row(), identity)


def test_registry_actor_mapping_cannot_hide_a_different_stdio_pid(tmp_path: Path):
    facts_path = tmp_path / "ready.json"
    facts_path.write_text(json.dumps({
        "phase": "upgrade-auto-restart", "childPid": 999, "childReturnCode": None,
    }))
    with pytest.raises(AssertionError, match="stdio child continuity"):
        channel_probe.wait_for_channel_child_alive(
            {"json": json, "time": ProbeClock(), "_channel_diagnostic": lambda _path: "controlled"},
            facts_path, SimpleNamespace(pid=71, poll=lambda: None), None,
            child_pid=72, phase="upgrade-auto-restart", timeout=0.3,
        )


def read_public_registry(state: Path, env):
    def run(*command, env):
        result = subprocess.run(
            [str(item) for item in command], env=env, text=True,
            capture_output=True, timeout=30, check=False,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    return identities.read_agent_identities(
        {"json": json, "run": run}, state, Path(sys.executable), env,
        owner="e2e", machine="e2e-upgrade-continuity", names=("channel-probe",),
    )


def test_public_registry_snapshot_reads_do_not_rewrite_the_fixture(tmp_path: Path):
    from hyprial.identity import AgentRegistry

    state = tmp_path / "state"
    state.mkdir()
    registry = AgentRegistry(state / "agents.sqlite3", owner="e2e", machine="e2e-upgrade-continuity")
    try:
        agent = registry.create("channel-probe")
        expected = (agent.agent_id, agent.uri)
    finally:
        registry.close()
    database = state / "agents.sqlite3"
    original = database.read_bytes()
    env = isolated_environment(tmp_path / "reader")
    root = next(parent for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").is_file())
    env["PYTHONPATH"] = str(root / "src") + os.pathsep + str(root)
    observed = read_public_registry(state, env)["channel-probe"]
    assert (observed["agentId"], observed["uri"]) == expected
    assert "entityToken" not in observed
    assert database.read_bytes() == original


@pytest.mark.parametrize("field,value", [
    ("agentId", "a." + "b" * 26), ("actor", "another-agent"),
    ("machine", "another-node"), ("owner", "another-owner"),
    ("uri", OLD_ACTOR),
])
def test_current_registry_identity_must_retain_the_original_entity_and_namespace(field, value):
    pair = actor_identity()
    pair["current"][field] = value
    with pytest.raises(AssertionError):
        identities.migrated_identities(
            {"channel-probe": pair["before"]}, {"channel-probe": pair["current"]},
        )
