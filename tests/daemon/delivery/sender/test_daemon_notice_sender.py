"""The daemon's own service identity passes the ``onBehalfOf`` check.

Allen 2026-11-06: squire is an ordinary agent and the backend does not know
it.  R2 (owner ruling 2026-11-19): the daemon sends no notices to the user
any more (they are logged); a send that names its sender with
``onBehalfOf`` must accept the service identity while the old name-form
squire address is a retired one.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.adapters.user_delivery import UserDeliveryRequest, UserDeliveryResult
from hyprial.identity.impl.agents import addressing
from hyprial.kernel import canonical_agent_uri
from tests.kernel.actors.identity.support import application
from tests.support.runtime._owned_applications import close_application


class _RecordingUserBus:
    def __init__(self) -> None:
        self.requests: list[UserDeliveryRequest] = []

    def deliver(self, request: UserDeliveryRequest) -> UserDeliveryResult:
        self.requests.append(request)
        return UserDeliveryResult(request.message_id, True, native_message_id="n-1")


@pytest.fixture
def app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[DaemonApplication]:
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", True)
    instance = application(tmp_path)
    try:
        # An ordinary agent the user named squire: it must not become the
        # sender of anything the daemon itself says.
        instance.handle("agent.create", {"name": "squire"})
        yield instance
    finally:
        close_application(instance)


def test_the_notice_sender_passes_the_on_behalf_of_check(app) -> None:
    """The service identity passes the ``onBehalfOf`` check; the old
    name-form squire sender is a retired address of a registered agent."""

    service = app._dispatch_service_actor
    assert app._resolve_on_behalf_actor(service) == service

    with pytest.raises(DaemonRequestError) as retired:
        app._resolve_on_behalf_actor(
            canonical_agent_uri(app.owner, app.node_id, "squire")
        )
    assert retired.value.code == "AGENT_ADDRESS_RETIRED"
