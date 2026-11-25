"""AT10 wiring: capability entry points answer to a verified caller's ledger.

Card 359 asks the frozen capability enum (``tool-surface``/
``shared-path``/``org-context``/``isolation``) to be bound to the real entry
points that must consult it.  This file drives the two entry points that
already exist in the daemon:

* ``message.send`` to a retired ``route:<adapter>:<route>`` target is refused
  with ``UNSUPPORTED_TARGET`` (I4b); the ``channel`` capability is retired
  (2026-11-24) and a stored ``channel`` row changes no answer;
* the org shared folder reads (``orgfs.read``/``ls``/``stat``/``history``/
  ``export``) are the **org-context** entry point.

The rules mirror the AT09 wiring (``test_daemon_visibility_wiring``): a hosted
visitor is fenced and default-denied when the ledger names no grant; a grant
whose scope names the resource allows it; a withdrawal takes effect on the very
next call; and the host-local path plus a verified NATIVE caller keep today's
behaviour (no caller identity is imposed).  ``tool-surface``/``shared-path``/
``isolation`` have no daemon entry point in this build -- they stay decision
layer + CLI, which the module docstring of ``hyprial.agents.enforcement``
states.
"""

from __future__ import annotations

from tests.support.runtime._owned_applications import close_application, owned_applications as owned_applications

import json
import re
from contextlib import closing
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.application import (
    DaemonApplication,
    DaemonRequestError,
)
from hyprial.daemon.impl.application.messaging import orgfs_bridge
from hyprial.daemon.impl.application.messaging.orgfs_bridge import (
    _ORG_CONTEXT_READ_METHODS,
)
from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport


from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections

NODE = "node-1"
OWNER = "op"
NATIVE_URI = "agent:op:node-1:native"
VISITOR_URI = "agent:guest:node-1:visitor"
ROUTE = "route:lark:ops"
OTHER_ROUTE = "route:lark:alerts"


class _StubPresence:
    def actor_online(self, actor: str) -> bool:
        return True

    def online_actors(self) -> tuple[str, ...]:
        return ()

    def raw_online_actors(self) -> tuple[str, ...]:
        return ()

    def liveness_keeps(self, actor: str) -> bool:
        return True

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


class _FakeSupervisor:
    def status(self) -> tuple[()]:
        return ()


class _FakeGateway:
    """A route gateway that records instead of calling the Lark API."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def send_chat(self, chat_id: str, text: str, *, idempotency_key: str) -> str:
        self.calls.append((chat_id, text))
        return "om_test"

    def send_chat_file(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"kind": "file"}

    def send_chat_image(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"kind": "image"}


def _offline_route_gateway(app: DaemonApplication) -> _FakeGateway:
    gateway = _FakeGateway()
    app._route_lark_gateway = lambda config: gateway  # type: ignore[method-assign]
    return gateway


def _application(
    request: pytest.FixtureRequest, tmp_path: Path, *, routes: bool = False
) -> DaemonApplication:
    home = tmp_path / "home"
    if routes:
        home.mkdir(parents=True, exist_ok=True)
        (home / "channels.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "gateways": [
                        {
                            "provider": "lark",
                            "name": "lark",
                            "appId": "cli_test",
                            "credentialRef": "lark-lark",
                            "routes": [
                                {"name": "ops", "type": "direct", "nativeId": "oc_ops"},
                                {
                                    "name": "alerts",
                                    "type": "direct",
                                    "nativeId": "oc_alerts",
                                },
                            ],
                            "defaultRoute": "ops",
                        }
                    ],
                }
            ),
            "utf-8",
        )
        (home / "secrets").mkdir(parents=True, exist_ok=True)
        (home / "secrets" / "lark-lark.json").write_text(
            json.dumps({"appSecret": "s"}), "utf-8"
        )
    app = DaemonApplication(
        state_dir=tmp_path,
        socket_path=tmp_path / "daemon.sock",
        node_id=NODE,
        hyprial_home=home,
        owner=OWNER,
        **_app_injections(),
    )
    request.addfinalizer(lambda: close_application(app))
    app._inbox = InboxService(tmp_path / "inbox.sqlite3", MemoryDeliveryTransport())
    app._harnesses = _FakeSupervisor()  # type: ignore[assignment]
    app._presence = _StubPresence()  # type: ignore[assignment]
    return app


def _register_session(app: DaemonApplication, actor: str, session_ref: str) -> None:
    app.handle(
        "session.register",
        {
            "actor": actor,
            "sessionRef": session_ref,
            "cwd": "/work",
            "command": ["claude"],
            "source": "claude-channel",
            "channelConfirmed": True,
        },
    )


def _official(app: DaemonApplication, name: str) -> str:
    """The address agent ``name`` hosted here is reached at: its record's URI."""

    agent = app.agents.get(name)
    assert agent is not None
    return agent.uri


def _hosted_visitor(
    app: DaemonApplication, *, session_ref: str = "visitor-session"
) -> str:
    """Host the visitor; its carrier registers under the name-form URI.

    The daemon boundary canonicalizes that URI to the record's official
    address, which is what every later call and verdict names.
    """

    app.handle("agent.create", {"name": "native"})
    app.agents.create_transfer_hosted("visitor", pinned_owner="guest")
    _register_session(app, VISITOR_URI, session_ref)
    return _official(app, "visitor")


def _grant(
    app: DaemonApplication,
    *,
    actor: str,
    capability: str,
    scope: str,
    grant_id: str = "g1",
) -> None:
    """Grant with the raw ledger scope string (fixed scopes are bare words)."""

    app.handle(
        "agent.grant",
        {
            "actor": actor,
            "grantId": grant_id,
            "capability": capability,
            "scope": scope,
            "revision": 1,
        },
    )


def _targets(app: DaemonApplication, params: dict[str, Any]) -> list[str]:
    rows = app.handle("targets", params)["targets"]
    return [row["targetUri"] for row in rows]


def _array_scope(*items: str) -> str:
    return json.dumps(list(items))


def _send(
    app: DaemonApplication,
    *,
    sender: str,
    target: str,
    session: str | None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "from": sender,
        "to": [target],
        "message": "hi",
        "summary": "hi",
    }
    if session is not None:
        params["sessionRef"] = session
    return app.handle("message.send", params)




def test_a_hosted_route_send_is_unsupported(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path, routes=True)
    visitor = _hosted_visitor(app)
    gateway = _offline_route_gateway(app)

    with pytest.raises(DaemonRequestError) as failure:
        _send(app, sender=visitor, target=ROUTE, session="visitor-session")

    assert failure.value.code == "UNSUPPORTED_TARGET"
    assert gateway.calls == []


def test_a_stored_retired_channel_grant_changes_no_answer(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """A ``channel`` row left from before the retirement is skipped on read."""

    app = _application(request, tmp_path)
    visitor = _hosted_visitor(app)
    native = _official(app, "native")
    space_id = _create_space(app)
    _grant(app, actor=visitor, capability="send-to", scope=_array_scope(native))
    _grant(app, actor=visitor, capability="org-context", scope="accepted", grant_id="g2")
    _grant(app, actor=visitor, capability="see-actors", scope=_array_scope(native), grant_id="g3")
    with closing(sqlite3.connect(app._agent_registry.database)) as db, db:
        db.execute(
            "INSERT INTO agent_capability_grants SELECT actor, entity_token, 'old', "
            "'channel', ?, granted_by, 1 FROM agent_capability_grants WHERE grant_id='g1'",
            (_array_scope("lark:ops"),),
        )

    assert _send(app, sender=visitor, target=native, session="visitor-session")["ok"] is True
    assert native in _targets(app, {"actor": visitor, "sessionRef": "visitor-session"})
    read = app.handle("orgfs.read", {
        "actor": visitor, "sessionRef": "visitor-session",
        "spaceId": space_id, "path": "readme.md",
    })
    assert read["text"] == "hello"
    with pytest.raises(DaemonRequestError) as failure:
        _send(app, sender=visitor, target=ROUTE, session="visitor-session")
    assert failure.value.code == "UNSUPPORTED_TARGET"


def test_a_native_route_send_is_unsupported_too(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path, routes=True)
    _hosted_visitor(app)
    native = _official(app, "native")
    gateway = _offline_route_gateway(app)

    with pytest.raises(DaemonRequestError) as failure:
        _send(app, sender=native, target=ROUTE, session=None)

    assert failure.value.code == "UNSUPPORTED_TARGET"
    assert failure.value.data == {"target": ROUTE}
    assert gateway.calls == []


def test_removing_the_send_to_gate_lets_the_same_send_through(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """Sensitivity check: the TARGET_NOT_AUTHORIZED refusal comes from the gate."""

    app = _application(request, tmp_path)
    visitor = _hosted_visitor(app)
    native = _official(app, "native")

    with pytest.raises(DaemonRequestError) as failure:
        _send(app, sender=visitor, target=native, session="visitor-session")
    assert failure.value.code == "TARGET_NOT_AUTHORIZED"

    app._enforce_send_to = lambda caller, targets: None  # type: ignore[method-assign]

    assert _send(app, sender=visitor, target=native, session="visitor-session")["ok"] is True














# -- org-context: org shared folder reads -----------------------------------


def _create_space(app: DaemonApplication) -> str:
    created = app._handle_orgfs("orgfs.create", {"name": "team"})
    space_id = created["space_id"]
    app._handle_orgfs(
        "orgfs.write", {"spaceId": space_id, "path": "readme.md", "text": "hello"}
    )
    return space_id


def test_a_hosted_orgfs_read_without_an_org_context_grant_is_refused(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    visitor = _hosted_visitor(app)

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "orgfs.read",
            {
                "actor": visitor,
                "sessionRef": "visitor-session",
                "spaceId": "space-1",
                "path": "readme.md",
            },
        )

    assert failure.value.code == "ORG_CONTEXT_NOT_AUTHORIZED"
    assert failure.value.data["caller"] == app.agents.uri_for("visitor")
    assert failure.value.data["caller"] != VISITOR_URI


def test_an_org_context_grant_lets_the_read_through(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    visitor = _hosted_visitor(app)
    space_id = _create_space(app)
    _grant(app, actor=visitor, capability="org-context", scope="accepted")

    read = app.handle(
        "orgfs.read",
        {
            "actor": visitor,
            "sessionRef": "visitor-session",
            "spaceId": space_id,
            "path": "readme.md",
        },
    )

    assert read["text"] == "hello"


def test_org_context_withdrawal_takes_effect_on_the_next_call(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    visitor = _hosted_visitor(app)
    space_id = _create_space(app)
    _grant(app, actor=visitor, capability="org-context", scope="accepted")
    read_params = {
        "actor": visitor,
        "sessionRef": "visitor-session",
        "spaceId": space_id,
        "path": "readme.md",
    }
    assert app.handle("orgfs.read", read_params)["text"] == "hello"

    app.handle("agent.revoke", {"actor": visitor, "grantId": "g1"})

    with pytest.raises(DaemonRequestError) as failure:
        app.handle("orgfs.read", read_params)
    assert failure.value.code == "ORG_CONTEXT_NOT_AUTHORIZED"


def test_the_host_local_orgfs_read_path_is_not_fenced(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    _hosted_visitor(app)
    space_id = _create_space(app)

    read = app.handle(
        "orgfs.read", {"spaceId": space_id, "path": "readme.md"}
    )

    assert read["text"] == "hello"


def test_a_made_up_session_binding_cannot_read_the_org_folder(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    visitor = _hosted_visitor(app)

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "orgfs.read",
            {
                "actor": visitor,
                "sessionRef": "made-up",
                "spaceId": "space-1",
                "path": "readme.md",
            },
        )

    assert failure.value.code in ("STALE_SESSION", "SESSION_SUPERSEDED")


# -- drift guards -----------------------------------------------------------


def test_the_org_context_read_set_covers_the_methods_it_names() -> None:
    """A renamed ``orgfs.*`` method cannot silently drop its gate."""

    source = Path(orgfs_bridge.__file__).read_text(encoding="utf-8")
    served = set(re.findall(r'method == "(orgfs\.[^"]+)"', source))
    assert served, "the source scan found no orgfs dispatch comparisons"
    assert _ORG_CONTEXT_READ_METHODS <= served
    assert {"orgfs.read", "orgfs.ls", "orgfs.stat"} <= _ORG_CONTEXT_READ_METHODS


# -- two tenants on one host: separate ledgers, and gates that matter --------


def _second_visitor(
    app: DaemonApplication, name: str, session_ref: str, owner: str = "guest2"
) -> str:
    app.agents.create_transfer_hosted(name, pinned_owner=owner)
    _register_session(app, f"agent:{owner}:{NODE}:{name}", session_ref)
    return _official(app, name)


def test_two_hosted_visitors_keep_separate_target_views(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """A see-actors grant to one tenant never widens another's view.

    The first visitor calls with its pinned-owner name URI, so the hosted
    name-to-id canonicalization at the targets boundary stays exercised; the
    second calls with its id address.
    """

    app = _application(request, tmp_path)
    visitor = _hosted_visitor(app)
    native = _official(app, "native")
    other = _second_visitor(app, "visitor2", "visitor2-session")
    _grant(
        app,
        actor=visitor,
        capability="see-actors",
        scope=_array_scope(native),
    )

    assert visitor != VISITOR_URI
    rows = _targets(app, {"actor": VISITOR_URI, "sessionRef": "visitor-session"})
    assert rows == sorted([native, visitor])

    other_rows = _targets(app, {"actor": other, "sessionRef": "visitor2-session"})
    assert other_rows == [other]
    assert native not in other_rows


def test_a_send_to_grant_does_not_cross_tenants(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    visitor = _hosted_visitor(app)
    native = _official(app, "native")
    other = _second_visitor(app, "visitor2", "visitor2-session")
    _grant(
        app,
        actor=visitor,
        capability="send-to",
        scope=_array_scope(native),
    )

    assert _send(
        app, sender=visitor, target=native, session="visitor-session"
    )["ok"] is True

    with pytest.raises(DaemonRequestError) as failure:
        _send(app, sender=other, target=native, session="visitor2-session")
    assert failure.value.code == "TARGET_NOT_AUTHORIZED"
    assert failure.value.data["caller"] == other




