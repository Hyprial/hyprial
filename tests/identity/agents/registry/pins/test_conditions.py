"""Conditional pin writes reject stale readers across SQLite connections."""

import subprocess
import sys

import pytest

from hyprial.identity import AgentError, AgentRegistry


@pytest.fixture
def registry(tmp_path):
    value = AgentRegistry(tmp_path / "agents.sqlite3", owner="alice", machine="node")
    yield value
    value.close()


def test_conditions_distinguish_absent_null_and_canonical_actor(registry):
    for actor in ("one", "two"):
        registry.create(actor)
    assert registry.pin("lark", "one", expected_actor=None) is None
    with pytest.raises(AgentError) as failure:
        registry.pin("lark", "two", expected_actor=None)
    assert failure.value.code == "PIN_STATE_CONFLICT"
    assert failure.value.current_actor == "agent:alice:node:one"
    assert registry.pin("lark", "two", expected_actor="agent:alice:node:one") == "agent:alice:node:one"
    # The omitted argument preserves the historical unconditional CLI move.
    assert registry.pin("lark", "one") == "agent:alice:node:two"


def test_stale_condition_is_checked_even_for_an_idempotent_target(registry):
    registry.create("one")
    registry.pin("lark", "one")
    with pytest.raises(AgentError) as failure:
        registry.pin("lark", "one", expected_actor=None)
    assert failure.value.code == "PIN_STATE_CONFLICT"


def test_conditional_unpin_does_not_remove_a_replacement(registry):
    registry.create("one")
    registry.create("two")
    registry.pin("lark", "one")
    registry.pin("lark", "two")
    with pytest.raises(AgentError) as failure:
        registry.unpin("lark", expected_actor="agent:alice:node:one")
    assert failure.value.code == "PIN_STATE_CONFLICT"
    assert registry.pins() == {"lark": "agent:alice:node:two"}
    assert registry.unpin("lark", expected_actor="agent:alice:node:two") == "agent:alice:node:two"
    assert registry.unpin("lark", expected_actor=None) is None


@pytest.mark.parametrize("second", ["pin", "unpin"])
def test_two_processes_with_same_observation_have_exactly_one_winner(tmp_path, second):
    database = tmp_path / "agents.sqlite3"
    registry = AgentRegistry(database, owner="alice", machine="node")
    for actor in ("old", "first", "second"):
        registry.create(actor)
    registry.pin("lark", "old")
    registry.close()
    script = """
import sys
from pathlib import Path
from hyprial.identity import AgentRegistry, AgentError
registry = AgentRegistry(Path(sys.argv[1]), owner='alice', machine='node')
print('READY', flush=True)
sys.stdin.readline()
try:
    if sys.argv[2] == 'unpin':
        registry.unpin('lark', expected_actor='agent:alice:node:old')
    else:
        registry.pin('lark', sys.argv[3], expected_actor='agent:alice:node:old')
except AgentError as error:
    print(error.code)
else:
    print('CHANGED')
"""
    children = [subprocess.Popen(
        [sys.executable, "-c", script, str(database), action, actor],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) for action, actor in (("pin", "first"), (second, "second"))]
    try:
        for child in children:
            assert child.stdout.readline().strip() == "READY"
        for child in children:
            child.stdin.write("go\n")
            child.stdin.flush()
        results = []
        for child in children:
            stdout, stderr = child.communicate(timeout=15)
            assert child.returncode == 0, stderr
            results.append(stdout.strip())
        assert sorted(results) == ["CHANGED", "PIN_STATE_CONFLICT"]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            for pipe in (child.stdin, child.stdout, child.stderr):
                if pipe is not None:
                    pipe.close()
