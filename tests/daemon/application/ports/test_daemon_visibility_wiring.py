"""AT09 wiring: targets/send answer to a verified caller's ledger.

Card 358 asks the two delivery-facing methods to apply ``see-actors`` and
``send-to`` per *verified* caller identity.  These tests drive
``DaemonApplication.handle`` with the real registry and session store:

* a hosted visitor with no grant sees only itself in ``targets``, and a
  ``send`` from it is refused with ``TARGET_NOT_AUTHORIZED`` before anything is
  queued;
* a grant widens both, and a withdrawal takes effect on the very next call;
* the host-local path (no session binding -- CLI, workers, inbound forwards)
  and a verified NATIVE caller keep today's view, which is the explicit rule
  this wiring adopts for card 358's "host 本地操作兼容" clause;
* an identity presented with a made-up ``sessionRef`` is refused: a claim is
  not a credential.
"""

from __future__ import annotations

from tests.support.runtime._owned_applications import close_application, owned_applications as owned_applications

import json
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport


from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections

NODE = "node-1"
OWNER = "op"
NATIVE_URI = "agent:op:node-1:native"
VISITOR_URI = "agent:guest:node-1:visitor"


class _StubPresence:
    def __init__(self, online: tuple[str, ...] = ()) -> None:
        self._online = online

    def actor_online(self, actor: str) -> bool:
        return actor in self._online

    def online_actors(self) -> tuple[str, ...]:
        return self._online

    def raw_online_actors(self) -> tuple[str, ...]:
        return self._online

    def liveness_keeps(self, actor: str) -> bool:
        return True

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


class _FakeSupervisor:
    def status(self) -> tuple[()]:
        return ()


def _application(request: pytest.FixtureRequest, tmp_path: Path) -> DaemonApplication:
    app = DaemonApplication(
        state_dir=tmp_path,
        socket_path=tmp_path / "daemon.sock",
        node_id=NODE,
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


def _hosted_visitor(
    app: DaemonApplication, *, session_ref: str = "visitor-session"
) -> str:
    """One hosted visitor with a daemon-minted session binding, plus a peer."""

    app.handle("agent.create", {"name": "native"})
    app.agents.create_transfer_hosted("visitor", pinned_owner="guest")
    _register_session(app, VISITOR_URI, session_ref)
    return VISITOR_URI


def _targets(app: DaemonApplication, params: dict[str, Any]) -> list[str]:
    rows = app.handle("targets", params)["targets"]
    return [row["targetUri"] for row in rows]


def _send(app: DaemonApplication, *, sender: str, target: str, session: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {
        "from": sender,
        "to": [target],
        "message": "hi",
        "summary": "hi",
    }
    if session is not None:
        params["sessionRef"] = session
    return app.handle("message.send", params)


def _grant(
    app: DaemonApplication,
    *,
    actor: str,
    capability: str,
    scope: list[str],
    grant_id: str = "g1",
) -> None:
    app.handle(
        "agent.grant",
        {
            "actor": actor,
            "grantId": grant_id,
            "capability": capability,
            "scope": json.dumps(scope),
            "revision": 1,
        },
    )


# -- the host-local path and a native caller are unchanged -------------------


def test_the_host_local_path_still_sees_every_target(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    _hosted_visitor(app)

    assert _targets(app, {}) == sorted([NATIVE_URI, VISITOR_URI])


def test_targets_preserve_stored_legacy_profile_owner_spelling(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    app.user_profiles.ensure(
        owner="legacy:owner",
        owner_key="legacy-owner",
        login_name="legacy-owner",
        machine=NODE,
        machine_key=NODE,
    )
    channel = "adapter:op:node-1:lark"
    app.user_profiles.set_squire_channel("legacy-owner", channel)
    app.user_profiles.bind_owner_open_id(
        "legacy-owner", channel=channel, open_id="ou_legacy"
    )

    assert _targets(app, {}) == ["user:legacy:owner"]


def test_a_verified_native_caller_keeps_todays_view(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """Card 358 names hosted visitors; tightening native is a ruling."""

    app = _application(request, tmp_path)
    _hosted_visitor(app)
    _register_session(app, NATIVE_URI, "native-session")

    rows = _targets(app, {"actor": NATIVE_URI, "sessionRef": "native-session"})

    assert rows == sorted([NATIVE_URI, VISITOR_URI])


# -- a hosted visitor is fenced to the ledger --------------------------------


def test_targets_fences_a_hosted_visitor_to_itself(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    _hosted_visitor(app)

    rows = _targets(app, {"actor": VISITOR_URI, "sessionRef": "visitor-session"})

    assert rows == [VISITOR_URI]


def test_a_grant_widens_the_view_and_a_withdrawal_takes_effect_next_call(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    _hosted_visitor(app)
    params = {"actor": VISITOR_URI, "sessionRef": "visitor-session"}

    assert _targets(app, params) == [VISITOR_URI]

    _grant(app, actor=VISITOR_URI, capability="see-actors", scope=[NATIVE_URI])
    assert _targets(app, params) == sorted([NATIVE_URI, VISITOR_URI])

    app.handle("agent.revoke", {"actor": VISITOR_URI, "grantId": "g1"})
    assert _targets(app, params) == [VISITOR_URI]


def test_a_send_without_a_send_to_grant_is_refused_before_anything_queues(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    _hosted_visitor(app)

    with pytest.raises(DaemonRequestError) as failure:
        _send(app, sender=VISITOR_URI, target=NATIVE_URI, session="visitor-session")

    assert failure.value.code == "TARGET_NOT_AUTHORIZED"
    assert failure.value.data["caller"] == VISITOR_URI
    assert failure.value.data["target"] == NATIVE_URI
    assert app._inbox.outbox_count() == 0  # type: ignore[union-attr]


def test_a_send_to_a_granted_target_proceeds_and_self_needs_no_grant(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    _hosted_visitor(app)
    _grant(app, actor=VISITOR_URI, capability="send-to", scope=[NATIVE_URI])

    granted = _send(
        app, sender=VISITOR_URI, target=NATIVE_URI, session="visitor-session"
    )
    assert granted["ok"] is True

    itself = _send(
        app, sender=VISITOR_URI, target=VISITOR_URI, session="visitor-session"
    )
    assert itself["ok"] is True


def test_the_host_local_send_path_is_not_fenced(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """No session binding: the CLI/worker path keeps today's rules."""

    app = _application(request, tmp_path)
    _hosted_visitor(app)
    _register_session(app, NATIVE_URI, "native-session")

    # No send-to grant is recorded anywhere; the unfenced path still sends.
    sent = _send(app, sender=NATIVE_URI, target=VISITOR_URI, session=None)

    assert sent["ok"] is True


# -- a claim is not a credential --------------------------------------------


def test_a_made_up_session_binding_cannot_send_and_does_not_narrow_targets(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """Only a HOSTED caller is fenced for ``targets``; ``send`` fences always.

    A presented identity that does not verify must not buy anything: for
    ``message.send`` the claim is refused outright (the same rule the inbox
    methods already used).  For ``targets`` a non-hosted caller keeps today's
    view -- card 358 tightens hosted visitors, and fencing natives would newly
    refuse a native agent's ``targets`` after a daemon restart.  Limitation
    recorded on ``_visibility_caller``: that means a claim of another actor's
    name is not separable on this path, which is why the refusal side (send)
    keeps the fence.
    """

    app = _application(request, tmp_path)
    _hosted_visitor(app)

    assert _targets(app, {"actor": NATIVE_URI, "sessionRef": "not-a-binding"}) == sorted(
        [NATIVE_URI, VISITOR_URI]
    )

    with pytest.raises(DaemonRequestError) as send_failure:
        _send(app, sender=NATIVE_URI, target=VISITOR_URI, session="not-a-binding")

    assert send_failure.value.code in {"STALE_SESSION", "SESSION_SUPERSEDED"}
    assert app._inbox.outbox_count() == 0  # type: ignore[union-attr]


def test_a_native_caller_whose_binding_went_stale_still_sees_the_list(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """The reviewer's (b) case: no new refusal for a native agent's targets."""

    app = _application(request, tmp_path)
    _hosted_visitor(app)
    _register_session(app, NATIVE_URI, "native-session")
    _register_session(app, NATIVE_URI, "native-session-2")  # supersedes the first

    assert _targets(app, {"actor": NATIVE_URI, "sessionRef": "native-session"}) == sorted(
        [NATIVE_URI, VISITOR_URI]
    )
    assert _targets(app, {"actor": NATIVE_URI, "sessionRef": "native-session-2"}) == sorted(
        [NATIVE_URI, VISITOR_URI]
    )


def test_a_superseded_binding_cannot_borrow_the_ledger(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """The visitor's grant must not answer for a stale binding."""

    app = _application(request, tmp_path)
    _hosted_visitor(app)
    _grant(app, actor=VISITOR_URI, capability="see-actors", scope=[NATIVE_URI])
    _register_session(app, VISITOR_URI, "visitor-session-2")

    with pytest.raises(DaemonRequestError) as failure:
        _targets(app, {"actor": VISITOR_URI, "sessionRef": "visitor-session"})

    assert failure.value.code in {"SESSION_SUPERSEDED", "STALE_SESSION"}


# -- the L0 boundary item 1 asks the attack list to name --------------------


def test_a_sessionless_claim_of_the_host_owner_user_is_the_l0_boundary(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    "AT09 item 1's attack: no ``sessionRef``, claim ``user:<host owner>``.\n\n    A user principal has no session to bind, so #1073's sender custody cannot\n    answer for it -- there is no session to compare the claim against.  The\n    claim therefore rides the same host-local path the operator's own CLI\n    sends use, and the recipient sees exactly the claimed ``user:<owner>``.\n    That is the L0 same-uid boundary the support table records: what keeps it\n    honest is uid ownership of the trusted socket, not this fence.  The\n    registered-agent half of the same attack *is* fenced once the sender-auth\n    rollout runs in enforce mode (``tests/daemon/delivery/message/test_authenticated_message_sender.py``\n    refuses a registered agent's sessionless claim there), which is why the\n    boundary is about principals without sessions, not about a missing check.\n    "

    app = _application(request, tmp_path)
    _hosted_visitor(app)

    sent = app.handle(
        "message.send",
        {"from": f"user:{OWNER}", "to": [VISITOR_URI], "message": "claimed owner", "summary": "claimed owner"},
        _trusted_message_origin="ipc",
    )

    assert sent["ok"] is True
    sessionless = app.handle(
        "message.send",
        {
            "from": f"user:{OWNER}",
            "onBehalfOf": NATIVE_URI,
            "to": [VISITOR_URI],
            "message": "claimed owner, relayed",
            "summary": "claimed owner, relayed",
        },
        _trusted_message_origin="ipc",
    )
    assert sessionless["ok"] is True
    assert sessionless["messageId"] != sent["messageId"]
