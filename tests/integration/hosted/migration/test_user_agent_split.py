"""AT01/AT04 split the daemon's own user agent out of the P4 comparison.

The hosted scenarios start a real daemon, which creates the owner's user agent
(``hyprial-user``) on its own thread after ``daemon.ready``.  These tests seed
the same bd6eb960 fixture, open it with the product's AgentRegistry and run
the product's ensure (no daemon), then pin that the split leaves exactly the
P4 rewrite, that the split-out part is checked, and that any other change
still fails the comparison.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from hyprial.daemon.impl.user_agent import CREATED, ensure_user_agent
from hyprial.identity import AgentRegistry

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location("hosted_registry_split", ROOT / "contract/e2e/run-hosted-registry.py")
registry_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry_mod)
split = registry_mod.user_agent


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "home").mkdir()
    (tmp_path / "state").mkdir()
    registry_mod.seed(tmp_path)
    return tmp_path


def _start(root: Path) -> None:
    registry = AgentRegistry(root / "state/agents.sqlite3", owner="host-owner", machine="host-node",
                             hyprial_home=root / "home")
    try:
        assert ensure_user_agent(agents=registry) == CREATED
    finally:
        registry.close()


def test_the_rest_is_exactly_p4_and_the_user_agent_part_is_checked(root: Path) -> None:
    old = registry_mod.snapshot(root)
    _start(root)
    whole = registry_mod.snapshot(root)

    assert whole != registry_mod.after_p4(old)
    rest, own = split.split_user_agent(whole)
    assert rest == registry_mod.after_p4(old)
    split.check_user_agent(own, "host-owner", "host-node", registry_mod.agent_id_for)
    assert own["files"] and all(path.startswith("home/agents/hyprial-user/") for path in own["files"])
    with pytest.raises(AssertionError):
        split.check_user_agent(own, "other-owner", "host-node", registry_mod.agent_id_for)


def test_any_other_change_still_fails_the_comparison(root: Path) -> None:
    old = registry_mod.snapshot(root)
    _start(root)
    with sqlite3.connect(root / "state/agents.sqlite3") as db:
        db.execute("UPDATE agents SET cwd = '/elsewhere' WHERE actor = 'native'")

    rest, _ = split.split_user_agent(registry_mod.snapshot(root))
    assert rest != registry_mod.after_p4(old)


def test_the_wait_returns_only_a_settled_present_user_agent() -> None:
    answers = iter([
        {"userAgent": {"state": "pending", "attempts": 0}},
        {"userAgent": {"state": "present", "attempts": 0}},
        {"userAgent": {"state": "present", "attempts": 1, "address": "agent:o:n:a.x"}},
    ])
    sleeps: list[float] = []

    block = split.wait_user_agent(lambda: next(answers), sleep=sleeps.append, clock=lambda: 0.0)

    assert block["address"] == "agent:o:n:a.x" and len(sleeps) == 2
    assert split.wait_user_agent(lambda: {"agents": []}) is None
    for state in ("name-taken", "unavailable"):
        with pytest.raises(AssertionError, match="did not create"):
            split.wait_user_agent(lambda state=state: {"userAgent": {"state": state, "attempts": 1}})
    times = iter([0.0, 31.0])
    with pytest.raises(AssertionError, match="did not settle"):
        split.wait_user_agent(lambda: {"userAgent": {"state": "pending", "attempts": 0}},
                              sleep=lambda _: None, clock=lambda: next(times))
