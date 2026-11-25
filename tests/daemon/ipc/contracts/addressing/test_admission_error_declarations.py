"""UUID addressing §2.3 over the strict METHOD_REGISTRY.

The registry rewrites an undeclared error code (or undeclared error data) to
DAEMON_ERROR.  Address admission raises AGENT_ADDRESS_RETIRED (id address in
its data), TARGET_UNRESOLVED (``{target}``) and a data-less
SESSION_SUPERSEDED; every registered method whose handler reaches it must
declare those codes with the producer's data shape, or the
fail-loud answer and its migration hint are lost on CLI, MCP and HTTP.

``ADMISSION_CODES`` maps each registered method to the codes its handler can
raise: ``_authenticated_message_caller`` (sender / onBehalfOf resolution and
the session fence) raises RETIRED and SUPERSEDED; the send boundary adds
UNRESOLVED.  ``org.acl.*`` swallows the caller error and
``routine.*``/``workflow.list`` fold the fence into CALLER_NOT_AUTHORIZED, so
they are not listed.  threads.* and conversations.* resolve their caller with
message.history's rule (cell model re-sync after #2547).

``agent.start`` creates on start (nested agent.create); the harness binary
pin runs before creation, so the start test pins it, and the test
application runs no lifecycle manager, so the start stops there, past every
name check.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.application.actors import agent_start
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.daemon.impl.request_catalog.address_errors import (
    RETIRED_ADDRESS_DATA,
    TARGET_UNRESOLVED_DATA,
)
from hyprial.identity.impl.agents import addressing
from hyprial.kernel import DaemonRequestError, canonical_agent_uri, ipc_errors
from tests.kernel.actors.identity.support import application
from tests.support.runtime._owned_applications import close_application

RETIRED = ipc_errors.AGENT_ADDRESS_RETIRED
UNRESOLVED = ipc_errors.TARGET_UNRESOLVED
SUPERSEDED = ipc_errors.SESSION_SUPERSEDED

ADMISSION_CODES: dict[str, frozenset[str]] = {
    "message.send": frozenset({RETIRED, UNRESOLVED, SUPERSEDED}),
    "message.send.key_lookup": frozenset({RETIRED, SUPERSEDED}),
    "message.send.lookup": frozenset({RETIRED, SUPERSEDED}),
    "message.history": frozenset({RETIRED, SUPERSEDED}),
    "message.status": frozenset({RETIRED, SUPERSEDED}),
    "message.pending.batch": frozenset({RETIRED, SUPERSEDED}),
    "progress.list": frozenset({RETIRED, SUPERSEDED}),
    # The local-file path policy authenticates ``from``.
    "orgfs.export": frozenset({RETIRED, SUPERSEDED}),
    "orgfs.import": frozenset({RETIRED, SUPERSEDED}),
    "session.turn.refresh": frozenset({SUPERSEDED}),
    "targets": frozenset({SUPERSEDED}),
    "conversations.list": frozenset({RETIRED, SUPERSEDED}),
    "conversations.history": frozenset({RETIRED, SUPERSEDED}),
    "conversations.create": frozenset({RETIRED, SUPERSEDED}),
    "conversations.get": frozenset({RETIRED, SUPERSEDED}),
    "conversations.members.add": frozenset({RETIRED, SUPERSEDED}),
    "conversations.members.remove": frozenset({RETIRED, SUPERSEDED}),
    "conversations.post": frozenset({RETIRED, SUPERSEDED}),
}

_DATA_SCHEMA = {
    RETIRED: RETIRED_ADDRESS_DATA,
    UNRESOLVED: TARGET_UNRESOLVED_DATA,
    SUPERSEDED: None,
}

#: message.send also re-raises the user-delivery receiver's TARGET_UNRESOLVED
#: (cell model S10) with the per-target messageId, so its one declaration is
#: the admission shape plus an optional messageId; target stays required.
_SEND_TARGET_UNRESOLVED_DATA = {
    "type": "object",
    "properties": {
        "target": {"type": "string"},
        "messageId": {"type": "string"},
    },
    "required": ["target"],
    "additionalProperties": False,
}
_DATA_OVERRIDES = {("message.send", UNRESOLVED): _SEND_TARGET_UNRESOLVED_DATA}


def _declared(method: str) -> dict[str, object]:
    binding = METHOD_REGISTRY.get(method)
    assert binding is not None, method
    return {error.code: error.data_schema for error in binding.spec.errors}


def _plain(value: object) -> object:
    if isinstance(value, dict) or hasattr(value, "items"):
        return {key: _plain(item) for key, item in value.items()}  # type: ignore[union-attr]
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


@pytest.mark.parametrize("method", sorted(ADMISSION_CODES))
def test_every_admission_code_is_declared_with_the_producer_data(method) -> None:
    declared = _declared(method)
    for code in sorted(ADMISSION_CODES[method]):
        assert code in declared, f"{method} does not declare {code}"
        expected = _DATA_OVERRIDES.get((method, code), _DATA_SCHEMA[code])
        assert _plain(declared[code]) == expected, (method, code)


#: Takes ``onBehalfOf`` but never authenticates a caller: a person's
#: read-only inbox query resolves the consumer name and ignores the field.
_UNAUTHENTICATED_ON_BEHALF_OF = frozenset({"message.query"})


def test_every_on_behalf_of_method_is_in_the_admission_table() -> None:
    # ``onBehalfOf`` is resolved by _resolve_on_behalf_actor, which refuses a
    # retired name URI; a new method taking it must join the table.
    for spec in METHOD_REGISTRY.specs:
        properties = spec.params_schema.get("properties", {})
        if "onBehalfOf" in properties and spec.name not in _UNAUTHENTICATED_ON_BEHALF_OF:
            assert RETIRED in ADMISSION_CODES.get(spec.name, ()), spec.name


def test_no_spec_declares_session_superseded_with_data() -> None:
    # The release contract: SESSION_SUPERSEDED never carries data, so the
    # dead-id session refusal must not attach any either.
    for spec in METHOD_REGISTRY.specs:
        for error in spec.errors:
            if error.code == SUPERSEDED:
                assert error.data_schema is None, spec.name


# -- the same codes, raised by the real application through the registry ----

OWNER = "op"
NODE = "node-1"


def _name_uri(name: str) -> str:
    return canonical_agent_uri(OWNER, NODE, name)


@pytest.fixture
def flipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[DaemonApplication]:
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", True)
    instance = application(tmp_path)
    try:
        instance.handle("agent.create", {"name": "worker"})
        yield instance
    finally:
        close_application(instance)


def _refused(app: DaemonApplication, method: str, params: dict) -> DaemonRequestError:
    with pytest.raises(DaemonRequestError) as caught:
        app.handle(method, params)
    return caught.value


_USER = f"user:{OWNER}"


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("message.history", {"conversationId": "c1"}),
        ("message.status", {"messageId": "m1"}),
        ("progress.list", {"deliveryId": "d1"}),
        ("message.pending.batch", {}),
        ("conversations.list", {}),
        ("conversations.history", {"conversationId": "c." + "a" * 26}),
    ],
)
def test_on_behalf_of_a_name_uri_keeps_its_retired_code_and_id_hint(
    flipped, method, params
) -> None:
    worker = flipped.agents.require("worker")
    error = _refused(
        flipped, method, {"from": _USER, "onBehalfOf": _name_uri("worker"), **params}
    )
    assert error.code == RETIRED
    assert error.data == {
        "target": _name_uri("worker"),
        "address": worker.uri,
        "agentId": worker.agent_id,
    }


def test_key_lookup_from_a_name_uri_keeps_its_retired_code(flipped) -> None:
    worker = flipped.agents.require("worker")
    error = _refused(
        flipped,
        "message.send.key_lookup",
        {"from": _name_uri("worker"), "operationKey": "k" * 22},
    )
    assert error.code == RETIRED
    assert error.data["address"] == worker.uri


def test_a_session_at_a_dead_id_is_superseded_without_data(flipped) -> None:
    dead = _name_uri("a.aaaaaaaaaaaaaaaaaaaaaaaaaa")
    error = _refused(
        flipped,
        "message.history",
        {"actor": dead, "sessionRef": "s-dead", "conversationId": "c1"},
    )
    assert error.code == SUPERSEDED
    assert error.data is None


def test_recreating_a_destroyed_name_creates_a_new_agent(flipped) -> None:
    flipped.handle("agent.create", {"name": "temp"})
    old = flipped.agents.require("temp")
    assert flipped.agents.destroy("temp")

    flipped.handle("agent.create", {"name": "temp"})

    assert flipped.agents.require("temp").agent_id != old.agent_id


def test_starting_a_destroyed_name_passes_the_name_check(
    flipped, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        agent_start.shutil, "which", lambda name: f"/usr/local/bin/{name}"
    )
    flipped.handle("agent.create", {"name": "temp"})
    assert flipped.agents.destroy("temp")

    error = _refused(flipped, "agent.start", {"name": "temp", "harness": "claude"})

    assert error.code == ipc_errors.LIFECYCLE_MANAGER_UNAVAILABLE


def test_attaching_a_destroyed_name_creates_a_new_agent(flipped) -> None:
    flipped.handle("agent.create", {"name": "temp"})
    old = flipped.agents.require("temp")
    assert flipped.agents.destroy("temp")

    flipped.handle("agent.attach", {"name": "temp"})

    assert flipped.agents.require("temp").agent_id != old.agent_id
