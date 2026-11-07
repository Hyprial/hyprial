"""A relayed user delivery is attributed to its original sender.

User deliveries reach a live user-proxy from the daemon's dispatch service,
with the real sender in ``origin.onBehalfOf``.  When the proxy forwards the
turn to a route (the owner's DM), the "转述自" line must name that sender,
not dispatch-coordinator.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

from ..support import (
    FakeHarnessStatus,
    FakeInbox,
    _TEST_APPLICATIONS,
    _close_test_applications as _close_test_applications,
    _request,
)


@pytest.mark.parametrize(
    ("sender", "attributed"),
    [
        pytest.param("worker", "agent:bob:receiver-mac:worker", id="local-agent"),
        pytest.param(
            "agent:carol:other-mac:helper",
            "agent:carol:other-mac:helper",
            id="remote-agent",
        ),
        pytest.param("user:bob", "user:bob", id="user"),
        # No onBehalfOf to speak for: the relay itself is the honest sender.
        pytest.param("inbox-watchdog", None, id="bare-service"),
    ],
)
def test_proxy_route_forward_attributes_the_original_sender(
    tmp_path: Path, sender: str, attributed: str | None
) -> None:
    """A user delivery relayed through the live proxy and forwarded by it to a
    route is attributed to the original sender, not dispatch-coordinator."""
    from hyprial.kernel import HarnessLaunchSpec

    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="receiver-mac",
        owner="bob",
        **_deep_module_daemon_dependencies(),
    )
    _TEST_APPLICATIONS.append(app)
    inbox = FakeInbox()
    app._inbox = inbox
    app._harnesses = FakeHarnessStatus(running=True)  # type: ignore[assignment]
    app._presence = object()  # type: ignore[assignment]
    app.handle("agent.create", {"name": "bob-proxy"})
    app.handle("agent.create", {"name": "worker"})
    app.desired_state.upsert_harness(
        HarnessLaunchSpec(harness="user-proxy", name="bob-proxy", headless=True)
    )
    result = app._deliver_to_live_user_proxy(
        "bob-proxy", replace(_request(), sender=sender)
    )
    assert result is not None and result.accepted
    relayed = inbox.submitted[0]
    assert relayed.sender == app._dispatch_service_actor

    posts: list[str] = []

    def route(target: str, **kwargs: object) -> list[dict[str, object]]:
        posts.append(str(kwargs["sender"]))
        return []

    app._deliver_route_target = route  # type: ignore[method-assign]
    outcome = app._forward_as_actor(relayed, "route:lark:bob-dm", "relayed text")

    assert outcome.accepted
    assert posts == [attributed or app._dispatch_service_actor]


def test_proxy_route_forward_ignores_on_behalf_of_from_another_sender(
    tmp_path: Path,
) -> None:
    """origin.onBehalfOf is trusted only on rows this daemon's dispatch
    service wrote; any other sender keeps its own attribution."""
    from dataclasses import replace as dc_replace

    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="receiver-mac",
        owner="bob",
        **_deep_module_daemon_dependencies(),
    )
    _TEST_APPLICATIONS.append(app)
    app._inbox = FakeInbox()
    app._harnesses = FakeHarnessStatus(running=True)  # type: ignore[assignment]
    app._presence = object()  # type: ignore[assignment]
    app.handle("agent.create", {"name": "bob-proxy"})
    from hyprial.kernel import HarnessLaunchSpec

    app.desired_state.upsert_harness(
        HarnessLaunchSpec(harness="user-proxy", name="bob-proxy", headless=True)
    )
    assert app._deliver_to_live_user_proxy(
        "bob-proxy", replace(_request(), sender="user:bob")
    ).accepted  # type: ignore[union-attr]
    relayed = app._inbox.submitted[0]  # type: ignore[union-attr]
    forged = dc_replace(relayed, sender="agent:mallory:evil-mac:spoofer")

    posts: list[str] = []
    app._deliver_route_target = (  # type: ignore[method-assign]
        lambda target, **kwargs: posts.append(str(kwargs["sender"])) or []
    )
    app._forward_as_actor(forged, "route:lark:bob-dm", "relayed text")

    assert posts == ["agent:mallory:evil-mac:spoofer"]
