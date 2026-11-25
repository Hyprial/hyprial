"""UA2b item 4: a role-carrying message to ``user:`` is refused before any submit.

A person is never a worker.  The refusal must not depend on whether this
node holds the owner's user agent, and it runs for every ``user:`` target
of the call before any target is submitted or pushed.
"""

from __future__ import annotations

import pytest

from hyprial.kernel import DaemonRequestError, ipc_errors
from tests.daemon.acceptance.acceptance_support import session
from tests.daemon.conversations.bypass.bypass_support import boot, queue_rows
from tests.support.golden.app import BETA, OWNER_USER, golden_daemon as golden_daemon


def _role_send(app, to: list[str], key: str) -> None:
    from tests.support.golden.app import ALPHA

    app.handle("message.send", {
        **session(ALPHA), "to": to, "summary": "s", "message": "do it",
        "idempotencyKey": f"key-{key}", "role": "review",
    })


@pytest.mark.parametrize("user_agent", [True, False], ids=["user-agent", "no-user-agent"])
@pytest.mark.parametrize("target", [OWNER_USER, "user:someone-else"])
def test_role_to_a_user_target_is_refused_on_every_node(
    golden_daemon, monkeypatch, user_agent, target
) -> None:
    booted = boot(golden_daemon, monkeypatch, user_agent=user_agent)
    before = set(queue_rows(booted.app, "inbox"))

    with pytest.raises(DaemonRequestError) as refused:
        _role_send(booted.app, [target], f"role-{user_agent}")

    assert refused.value.code == ipc_errors.DISPATCH_ROLE_MISMATCH
    assert booted.daemon.user_delivery.requests == []
    assert set(queue_rows(booted.app, "inbox")) == before


def test_an_earlier_agent_target_is_not_submitted_when_a_user_target_is_refused(
    golden_daemon, monkeypatch
) -> None:
    booted = boot(golden_daemon, monkeypatch, user_agent=True)
    before = {table: set(queue_rows(booted.app, table)) for table in ("inbox", "sender_copies")}

    with pytest.raises(DaemonRequestError) as refused:
        _role_send(booted.app, [BETA, OWNER_USER], "role-mixed")

    assert refused.value.code == ipc_errors.DISPATCH_ROLE_MISMATCH
    assert {table: set(queue_rows(booted.app, table)) for table in before} == before
    assert booted.daemon.user_delivery.requests == []
