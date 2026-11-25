"""No local caller becomes the user agent's session, or pulls or sends as it through one.

``session.register`` and ``session.register_guarded`` are the only entrances
that bind an interactive session to an actor; every later session call
(refresh, heartbeat, turns, fenced message calls) needs the session they
persisted.  Both refuse the user agent by name, by name-form and id-form URI
and by ``a.<id>`` with USER_AGENT_IMMUTABLE, before any session or agent row
is written, and after the process-claim check, which keeps its own answer.
Before the ensure has created the record only the name spellings get that
code; the id spellings get SESSION_SUPERSEDED from the stale-address check.
A fenced pull or send as the user agent then has no session to present.
``lifecycle.*`` starts go through the name gate and answer INVALID_ARGUMENT;
the worker relay serves only a managed worker's own session, which the user
agent never has (and ``session.register`` is not on the worker channel).

Unfenced claims are outside this file: the sender-authentication rollout
switch decides them for every agent alike; the enforce-mode test pins that
the switch refuses the user agent too.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.user_agent import USER_AGENT_NAME, local_user_agent, run_user_agent_ensure
from hyprial.kernel import WorkerBinding, agent_uri_actor, ipc_errors
from tests.identity.agents.registry.entity.support import (
    NODE,
    OWNER,
    _close_test_applications as _close_test_applications,
    application,
)

CODE = "USER_AGENT_IMMUTABLE"
REGISTER = ("session.register", "session.register_guarded")


def _ensured(tmp_path: Path):  # type: ignore[no-untyped-def]
    app = application(tmp_path)
    assert run_user_agent_ensure(app) == "created"
    agent = local_user_agent(app.agents.list(), OWNER)
    assert agent is not None
    return app, agent


def _spellings(agent) -> dict[str, str]:  # type: ignore[no-untyped-def]
    actor_id = agent_uri_actor(agent.uri)
    assert actor_id is not None and actor_id.startswith("a.")
    return {
        "name": USER_AGENT_NAME,
        "name-uri": f"agent:{OWNER}:{NODE}:{USER_AGENT_NAME}",
        "id-uri": agent.uri,
        "id": actor_id,
    }


def _register(actor: str, session_ref: str = "ua-session", **extra: object) -> dict[str, object]:
    return {
        "actor": actor, "sessionRef": session_ref, "cwd": "/tmp", "command": ["carrier"],
        "source": "claude-channel", "runtime": "claude_interactive", **extra,
    }


def _code(app, method: str, params: dict[str, object], origin: str | None = "ipc") -> str | None:  # type: ignore[no-untyped-def]
    try:
        app.handle(method, params, _trusted_message_origin=origin)
    except DaemonRequestError as error:
        return error.code
    return None


def _sessions(app) -> list[str]:  # type: ignore[no-untyped-def]
    return [item.actor for item in app._agent_session_domains.session.read_sessions()]


@pytest.mark.parametrize("origin", ["ipc", "worker"])
@pytest.mark.parametrize("method", REGISTER)
def test_registering_as_the_user_agent_is_refused_in_every_spelling(
    tmp_path: Path, method: str, origin: str
) -> None:
    app, agent = _ensured(tmp_path)
    before = app.agents.get(USER_AGENT_NAME)

    answers = {
        form: _code(app, method, _register(actor), origin)
        for form, actor in _spellings(agent).items()
    }

    assert answers == dict.fromkeys(_spellings(agent), CODE)
    assert _sessions(app) == []
    assert app.agents.get(USER_AGENT_NAME) == before
    assert app.desired_state.load().harnesses == ()


@pytest.mark.parametrize("method", REGISTER)
def test_registering_as_the_user_agent_before_the_ensure_creates_nothing(
    tmp_path: Path, method: str
) -> None:
    _ensured_app, agent = _ensured(tmp_path / "other-home")
    app = application(tmp_path / "home")
    expected = {
        "name": CODE,
        "name-uri": CODE,
        "id-uri": ipc_errors.SESSION_SUPERSEDED,
        "id": ipc_errors.SESSION_SUPERSEDED,
    }

    answers = {
        form: _code(app, method, _register(actor)) for form, actor in _spellings(agent).items()
    }

    assert answers == expected
    assert _sessions(app) == []
    assert app.agents.get(USER_AGENT_NAME) is None


def test_an_ordinary_agent_still_registers(tmp_path: Path) -> None:
    app, _agent = _ensured(tmp_path)
    app.handle("agent.create", {"name": "plain"})
    work = tmp_path / "work"
    work.mkdir()

    assert _code(app, "session.register",
                 _register("plain", "plain-session", cwd=str(work))) is None
    assert len(_sessions(app)) == 1


def test_a_failed_process_claim_keeps_its_authorization_refusal(tmp_path: Path) -> None:
    app, agent = _ensured(tmp_path)
    app._ipc_peer.pid = os.getpid()
    claim = {"processPid": os.getpid(), "processIdentity": "not-this-process"}

    for actor in (USER_AGENT_NAME, agent.uri):
        assert _code(app, "session.register", _register(actor, **claim)) == (
            ipc_errors.CALLER_NOT_AUTHORIZED
        ), actor
    assert _sessions(app) == []


def test_fenced_session_calls_as_the_user_agent_have_no_session(tmp_path: Path) -> None:
    app, agent = _ensured(tmp_path)
    app.handle("agent.create", {"name": "plain"})
    plain = app.agents.get("plain").uri
    _code(app, "session.register", _register(agent.uri))
    fence = {"actor": agent.uri, "sessionRef": "ua-session"}
    attempts = {
        "session.refresh": fence,
        "session.heartbeat": fence,
        "message.pending.list": {**fence, "fetched": True},
        "message.send": {"from": agent.uri, "sessionRef": "ua-session", "to": [plain],
                         "summary": "hi", "message": "hi", "idempotencyKey": "ua-send"},
    }

    answers = {method: _code(app, method, params) for method, params in attempts.items()}

    assert None not in answers.values(), answers
    assert set(answers.values()) <= {
        ipc_errors.STALE_SESSION, ipc_errors.SESSION_SUPERSEDED, CODE,
    }, answers
    assert _sessions(app) == []


def test_enforced_sender_authentication_refuses_an_unfenced_user_agent_claim(
    tmp_path: Path,
) -> None:
    app, agent = _ensured(tmp_path)
    app._sender_auth_enforced = True

    for method, params in (
        ("message.pending.list", {"actor": agent.uri}),
        ("message.send", {"from": agent.uri, "to": [f"agent:{OWNER}:{NODE}:plain"],
                          "summary": "hi", "message": "hi", "idempotencyKey": "ua-send"}),
    ):
        assert _code(app, method, params) == ipc_errors.SENDER_NOT_AUTHENTICATED, method


def test_a_lifecycle_start_of_the_user_agent_is_refused_by_the_name_gate(
    tmp_path: Path,
) -> None:
    app, agent = _ensured(tmp_path)

    answers = {
        form: _code(app, "lifecycle.start", {"name": actor, "provider": "codex"})
        for form, actor in _spellings(agent).items()
    }

    assert answers == dict.fromkeys(_spellings(agent), ipc_errors.INVALID_ARGUMENT)
    assert app.desired_state.load().harnesses == ()


def test_the_worker_relay_has_no_worker_session_for_the_user_agent(tmp_path: Path) -> None:
    app, agent = _ensured(tmp_path)
    binding = WorkerBinding(agent.uri, "ua-session", app.epoch)

    for method in ("message.pending.list", "message.send"):
        request = {"version": 1, "id": "r-1", "method": method, "params": {}}
        assert _code(app, "worker.channel.request", binding.envelope(request)) == (
            ipc_errors.STALE_SESSION
        ), method



@pytest.mark.parametrize("method", REGISTER)
def test_pending_merge_another_machines_user_agent_id_form_is_any_remote_address(
    tmp_path: Path, method: str
) -> None:
    """Pinned for the later merge step: another machine's id form is not the user agent here.

    ``session.register`` answers it exactly as it answers an ordinary agent's
    address on another machine (today it registers both; ``register_guarded``
    refuses both), never with USER_AGENT_IMMUTABLE.  The local record is
    untouched.
    """

    app, agent = _ensured(tmp_path)
    app.handle("agent.create", {"name": "plain"})
    before = app.agents.get(USER_AGENT_NAME)
    other = agent.uri.replace(f":{NODE}:", ":node-2:")
    plain_other = app.agents.get("plain").uri.replace(f":{NODE}:", ":node-2:")

    code = _code(app, method, _register(other, "other-ua"))
    plain_code = _code(app, method, _register(plain_other, "other-plain"))

    assert code == plain_code and code != CODE
    assert app.agents.get(USER_AGENT_NAME) == before
