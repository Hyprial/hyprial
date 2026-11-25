"""Only owner-local agents reach the owner-key authority at launch (contract §3.3, §8.2).

An agent received by transfer for another owner can never
hold a slot of this owner, so its launch keeps the legacy semantics instead of failing
the owner-local resolution. The real ``context.resolve_agent`` is used here.
"""
from __future__ import annotations

import json

import pytest

from hyprial.daemon.impl.provider_keys.launch import held_slots, launch_resolver


@pytest.fixture
def real_resolution(harness):
    (harness.home / "settings.json").write_text(
        json.dumps({"providerKeys": {"authority": "owner-keys"}}), encoding="utf-8")
    del harness.app._provider_key_agent_resolver
    harness.app.agents = harness.registry
    return harness


def test_a_transfer_received_agent_of_another_owner_keeps_the_legacy_launch(real_resolution):
    harness = real_resolution
    moved = harness.registry.create_transfer_hosted("moved", pinned_owner="owner-b")
    harness.service.answer = RuntimeError("must not be called")
    assert launch_resolver(harness.app, registry=harness.registry)(moved.actor) is None
    assert held_slots(harness.app, moved.actor, registry=harness.registry) is None
    assert harness.service.calls == []


def test_a_transfer_received_agent_of_this_owner_still_uses_the_authority(real_resolution):
    harness = real_resolution
    mine = harness.registry.create_transfer_hosted("mine", pinned_owner="owner-a")
    assert launch_resolver(harness.app, registry=harness.registry)(mine.actor) == (frozenset(), ())
    assert [call[0] for call in harness.service.calls] == ["launch_secrets"]
    assert harness.service.calls[0][1].actor == "mine"


def test_a_foreign_hosted_agent_keeps_the_legacy_launch_with_real_resolution(real_resolution):
    harness = real_resolution
    guest = harness.foreign_hosted()
    harness.service.answer = RuntimeError("must not be called")
    assert launch_resolver(harness.app, registry=harness.registry)(guest.actor) is None
    assert harness.service.calls == []
