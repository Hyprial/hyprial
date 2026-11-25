"""UUID addressing §2.3: the daemon's delivery claim and how it is wired.

The inbox receive and the local-first transport ask one daemon predicate,
``_local_recipient_claimed``.  It answers True for everything while the
id-form switch is off, so wiring it changes nothing before the flip.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from types import SimpleNamespace

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.kernel import canonical_agent_uri
from tests.daemon.routing.failloud.conftest import name_uri


def test_switch_off_claims_every_recipient(app) -> None:
    for recipient in (name_uri("ghost"), name_uri("a.aaaaaaaaaaaaaaaaaaaaaaaaaa")):
        assert app._local_recipient_claimed(recipient) is True


def test_switch_on_claims_only_what_this_node_can_drain(flipped) -> None:
    worker = flipped.agents.require("worker")
    online = name_uri("streamer")
    flipped._presence = SimpleNamespace(  # type: ignore[assignment]
        actor_online=lambda actor: actor == online,
        online_actors=lambda: (online,),
        online_mailboxes=lambda: (),
    )

    claimed = flipped._local_recipient_claimed
    assert claimed(worker.uri) is True
    assert claimed(online) is True
    assert claimed(name_uri("pac-expansion")) is True
    # squire is an ordinary agent (Allen 2026-11-06): with no record nothing
    # here drains its name address.
    assert claimed(name_uri("squire")) is False
    assert claimed(name_uri("_turn-hooks")) is True
    assert claimed(canonical_agent_uri("op", "far-node", "ghost")) is True
    assert claimed(canonical_agent_uri("someone", "node-1", "ghost")) is True
    assert claimed("channel:lark:adapter") is True
    assert claimed(name_uri("ghost")) is False
    assert claimed(name_uri("a.aaaaaaaaaaaaaaaaaaaaaaaaaa")) is False


def test_switch_on_claims_no_name_uri_of_a_live_agent(flipped) -> None:
    """No agent reads rows at a name URI (the D4 read alias is gone), so
    neither its actor-name nor its display-name URI is claimed.  The send
    boundary still refuses both as retired."""

    flipped.agents.rename("worker", "builder")
    claimed = flipped._local_recipient_claimed

    assert claimed(name_uri("worker")) is False
    assert claimed(name_uri("builder")) is False


def _calls(function, name: str) -> list[ast.Call]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    ]


def _is_claim(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "_local_recipient_claimed"
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def test_delivery_plane_injects_the_claim_into_transport_and_inbox() -> None:
    compose = DaemonApplication._startup_compose_delivery_plane

    (local,) = _calls(compose, "LocalFirstDeliveryTransport")
    assert any(
        keyword.arg == "recipient_claimed" and _is_claim(keyword.value)
        for keyword in local.keywords
    )
    (coordinator,) = _calls(compose, "DeliveryCustodyCoordinator")
    options = next(
        keyword.value
        for keyword in coordinator.keywords
        if keyword.arg == "service_options"
    )
    assert isinstance(options, ast.Dict)
    assert any(
        isinstance(key, ast.Constant)
        and key.value == "recipient_claimed"
        and _is_claim(value)
        for key, value in zip(options.keys, options.values, strict=True)
    )
