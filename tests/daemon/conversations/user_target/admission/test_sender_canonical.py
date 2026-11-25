"""UA2b item 7: a user agent address never authors a stored message.

The user agent never has a session, so naming this machine's user agent
(name-form or id-form address, or bare name) as an explicit ``from`` is
refused with ``SENDER_NOT_AUTHENTICATED`` (the operator form is
``user:<owner>``); another machine's instance is no local sender at all and
keeps ``SENDER_UNRESOLVED``, and so does an id form on another machine, which
is an ordinary agent address until the merge step.
An ``onBehalfOf`` naming any machine's instance is stored as ``user:<owner>``,
like the targets.
"""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.user_agent import local_user_agent_address
from hyprial.kernel import DaemonRequestError, agent_uri_actor, ipc_errors
from tests.daemon.conversations.bypass.bypass_support import origin_of, queue_rows, stored
from tests.daemon.conversations.user_target.user_target_support import (
    boot_with_user_agent,
    user_agent_id_address,
)
from tests.support.golden.app import BETA, OWNER_USER, golden_daemon as golden_daemon

LOCAL_INSTANCE = "agent:op:node-1:hyprial-user"
OTHER_INSTANCE = "agent:op:node-9:hyprial-user"


def _send(app, params: dict, key: str, **kwargs) -> dict:
    return app.handle("message.send", {
        **params, "to": [BETA], "summary": "s", "message": "hello", "idempotencyKey": key,
    }, **kwargs)


@pytest.mark.parametrize("origin", [None, "daemon"], ids=["in-process", "daemon"])
@pytest.mark.parametrize(("instance", "code"), [
    (LOCAL_INSTANCE, ipc_errors.SENDER_NOT_AUTHENTICATED),
    ("hyprial-user", ipc_errors.SENDER_NOT_AUTHENTICATED),
    (OTHER_INSTANCE, ipc_errors.SENDER_UNRESOLVED),
])
def test_a_user_agent_address_as_from_is_refused(
    golden_daemon, monkeypatch, instance, code, origin
) -> None:
    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    before = set(queue_rows(booted.app, "sender_copies"))

    with pytest.raises(DaemonRequestError) as refused:
        _send(booted.app, {"from": instance}, f"from-{instance}",
              _trusted_message_origin=origin)

    assert refused.value.code == code
    assert set(queue_rows(booted.app, "sender_copies")) == before


@pytest.mark.parametrize("origin", [None, "daemon"], ids=["in-process", "daemon"])
@pytest.mark.parametrize(("which", "code"), [
    ("own", ipc_errors.SENDER_NOT_AUTHENTICATED),
    ("pending-merge-other-machine", ipc_errors.SENDER_UNRESOLVED),
])
def test_an_id_form_user_agent_address_as_from(golden_daemon, which, code, origin) -> None:
    """This machine's id form is its user agent (registry marker); another machine's is not.

    An id-minting daemon, as in production: the record's own URI is its id form.
    """

    app = golden_daemon(id_form=True, user_agent=True, links=False).app
    own = local_user_agent_address(app.agents)
    assert own is not None and agent_uri_actor(own).startswith("a.")
    sender = own if which == "own" else user_agent_id_address(app, "node-9")

    with pytest.raises(DaemonRequestError) as refused:
        app.handle("message.send", {
            "from": sender, "to": [OWNER_USER], "summary": "s", "message": "hello",
            "idempotencyKey": f"id-form-{which}",
        }, _trusted_message_origin=origin)

    assert refused.value.code == code


@pytest.mark.parametrize("instance", [OTHER_INSTANCE, "local-id-form"])
def test_on_behalf_of_a_user_agent_address_is_stored_as_the_owner(
    golden_daemon, monkeypatch, instance
) -> None:
    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    if instance == "local-id-form":
        instance = user_agent_id_address(booted.app)

    answer = _send(booted.app, {"from": OWNER_USER, "onBehalfOf": instance}, "obo")

    row = stored(booted.app, answer["messageId"])["sender_copies"]
    assert origin_of(row)["onBehalfOf"] == OWNER_USER
