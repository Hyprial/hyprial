"""The P2 facade joins exact identity cleanup, never treating pending as gone."""
import threading
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.composition import AgentSessionDomains, CorrelatedDomainEvents, DomainCommandError
from hyprial.daemon.impl.composition.views import AgentDirectoryFacade
from hyprial.identity import AgentDestroySettled
from hyprial.kernel import PortAdmission


def facade_for(actor, bus):
    domains = object.__new__(AgentSessionDomains)
    domains._registry = actor._registry
    domains.agent = actor
    domains._agent_events = bus
    return AgentDirectoryFacade(domains), bus


def test_destroy_settles_and_replays_without_touching_replacement(home_owner):
    bus = CorrelatedDomainEvents()
    registry, actor, agent = home_owner(configured=False, event_sink=bus.publish)
    facade, bus = facade_for(actor, bus)
    try:
        kwargs = dict(expected_entity_token=agent.entity_token, operation_id="destroy:0")
        assert facade.settle_landing_destroy(agent.actor, **kwargs) == "destroyed"
        assert facade.settle_landing_destroy(agent.actor, **kwargs) == "already-cleaned"
        replacement = registry.create(agent.actor)
        assert facade.settle_landing_destroy(agent.actor, **kwargs) == "stale-incarnation"
        assert registry.require(agent.actor) == replacement
    finally:
        bus.close()


def test_pending_cleanup_does_not_return_gone(home_owner, monkeypatch):
    bus = CorrelatedDomainEvents()
    registry, actor, agent = home_owner(configured=False, event_sink=bus.publish)
    facade, bus = facade_for(actor, bus)
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    original = registry.home_provisioner.cleanup
    values, errors = [], []
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(registry.home_provisioner, "cleanup", held)
    def run():
        try:
            values.append(facade.settle_landing_destroy(agent.actor, expected_entity_token=agent.entity_token, operation_id="destroy:held"))
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(3)
        assert not done.is_set() and values == []
    finally:
        release.set()
        thread.join(5)
        bus.close()
    assert not thread.is_alive() and not errors
    assert values == ["destroyed"]


@pytest.mark.parametrize("admission", [PortAdmission.CLOSING, PortAdmission.OVERLOADED])
def test_rejected_destroy_is_not_gone(home_owner, monkeypatch, admission):
    bus = CorrelatedDomainEvents()
    registry, actor, agent = home_owner(configured=False, event_sink=bus.publish)
    facade, bus = facade_for(actor, bus)
    monkeypatch.setattr(actor, "submit", lambda command: admission)
    try:
        with pytest.raises(DomainCommandError):
            facade.settle_landing_destroy(agent.actor, expected_entity_token=agent.entity_token, operation_id="destroy:reject")
        assert registry.require(agent.actor) == agent
    finally:
        bus.close()


@pytest.mark.parametrize("field,value", [("actor", "other"), ("expected_entity_token", "other"), ("disposition", "pending")])
def test_invalid_destroy_receipt_is_not_gone(field, value):
    values = dict(correlation_id="destroy", generation=1, version=1, actor="worker", expected_entity_token="token", disposition="destroyed")
    values[field] = value
    event = AgentDestroySettled(**values)
    facade = AgentDirectoryFacade(SimpleNamespace(_registry=SimpleNamespace(owner="test", machine="test"), call_agent_settled=lambda *args: event))
    with pytest.raises(DomainCommandError):
        facade.settle_landing_destroy("worker", expected_entity_token="token", operation_id="destroy")


@pytest.mark.parametrize("operation,token", [("", "token"), ("x", ""), (123, "token")])
def test_invalid_destroy_identity_never_submitted(operation, token):
    facade = AgentDirectoryFacade(SimpleNamespace(_registry=SimpleNamespace(owner="test", machine="test")))
    with pytest.raises(ValueError):
        facade.settle_landing_destroy("worker", expected_entity_token=token, operation_id=operation)


def test_facade_verification_preserves_identity_and_manifest(home_owner, tmp_path):
    from .test_payload import payload
    bus = CorrelatedDomainEvents()
    _registry, actor, agent = home_owner(configured=False, event_sink=bus.publish)
    facade, bus = facade_for(actor, bus)
    _stage, expected = payload(tmp_path)
    try:
        assert facade.verify_landing_payload(agent.actor, expected_entity_token=agent.entity_token, expected_files=expected) == "absent"
    finally:
        bus.close()
