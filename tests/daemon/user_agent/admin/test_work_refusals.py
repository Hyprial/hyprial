"""Scheduled and workflow work aimed at the user agent: refused, with or without a role.

The user agent has no harness, so work admitted for it would never run.
Routine, workflow and remote-workflow admission refuse it before
``dispatch_gate``; the message send path is untouched.  A routine always
carries a role (its schema requires one) and names its actor by address,
never by bare name.  The resume test stores its routine directly, as an
older daemon that knew no user agent would have admitted it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.dispatch.remote import RemoteWorkflow
from hyprial.daemon.impl.user_agent import (
    USER_AGENT_NAME,
    local_user_agent,
    run_user_agent_ensure,
)
from hyprial.identity import PacError
from tests.identity.agents.registry.entity.support import (
    NODE,
    OWNER,
    _close_test_applications as _close_test_applications,
    _scheduled_routine,
    application,
    enable_routines,
)

CODE = "USER_AGENT_IMMUTABLE"


def _ensured(tmp_path: Path):  # type: ignore[no-untyped-def]
    app = application(tmp_path)
    enable_routines(app, tmp_path)
    assert run_user_agent_ensure(app) == "created"
    agent = local_user_agent(app.agents.list(), OWNER)
    assert agent is not None
    return app, agent


def _routine(name: str, actor: str) -> str:
    return _scheduled_routine(name, actor)


def test_a_routine_for_the_user_agent_is_refused(tmp_path: Path) -> None:
    app, agent = _ensured(tmp_path)
    actor = agent.uri

    with pytest.raises(DaemonRequestError) as raised:
        app.handle("routine.add", {"yaml": _routine("mine", actor),
                                   "actor": f"user:{OWNER}"})
    assert raised.value.code == CODE
    assert app.handle("routine.list", {"actor": f"user:{OWNER}"})["routines"] == []


def test_another_machines_user_agent_is_refused_as_a_routine_actor(tmp_path: Path) -> None:
    app, _agent = _ensured(tmp_path)
    remote = f"agent:{OWNER}:node-2:{USER_AGENT_NAME}"

    with pytest.raises(DaemonRequestError) as raised:
        app.handle("routine.add", {"yaml": _routine("theirs", remote),
                                   "actor": f"user:{OWNER}"})
    assert raised.value.code == CODE


def test_pending_merge_another_machines_user_agent_id_form_is_no_user_agent_refusal(
    tmp_path: Path,
) -> None:
    """Pinned for the later merge step: an id-form address on another machine is ordinary.

    It takes the remote-workflow path every other remote agent takes; whatever
    that answers here, it is not the user agent refusal.
    """

    app, agent = _ensured(tmp_path)
    remote = agent.uri.replace(f":{NODE}:", ":node-2:")

    try:
        app.handle("routine.add", {"yaml": _routine("theirs", remote),
                                   "actor": f"user:{OWNER}"})
    except DaemonRequestError as error:
        assert error.code != CODE


def test_resuming_a_routine_bound_to_the_user_agent_is_refused(tmp_path: Path) -> None:
    app, agent = _ensured(tmp_path)
    app._routine_service.add(
        yaml_text=_routine("stale", agent.uri), owner=f"user:{OWNER}",
        enabled=False,
    )

    with pytest.raises(DaemonRequestError) as raised:
        app.handle("routine.resume", {"name": "stale", "actor": f"user:{OWNER}"})
    assert raised.value.code == CODE
    paused = app.handle("routine.pause", {"name": "stale", "actor": f"user:{OWNER}"})
    assert paused["enabled"] is False


def _workflow(owner: str, role: str | None) -> SimpleNamespace:
    node = SimpleNamespace(kind="task", owner=owner, role=role, expands=None, launch=None,
                           first_output_eta=None, human_gates=None, worker=None)
    return SimpleNamespace(nodes=(node,), escalate_to=None)


@pytest.mark.parametrize("role", [None, "execute"])
def test_a_workflow_node_pinned_to_the_user_agent_is_refused(
    tmp_path: Path, role: str | None
) -> None:
    app, agent = _ensured(tmp_path)

    with pytest.raises(DaemonRequestError) as raised:
        app._workflow_admit(_workflow(agent.uri, role), f"user:{OWNER}")
    assert raised.value.code == CODE


def test_a_borrowed_actor_request_for_the_user_agent_is_refused(tmp_path: Path) -> None:
    app, agent = _ensured(tmp_path)
    remote = RemoteWorkflow.__new__(RemoteWorkflow)
    remote.app = app

    with pytest.raises(PacError) as raised:
        remote.admit_local({"owner": agent.uri, "role": None})
    assert raised.value.code == CODE


def test_an_ordinary_agent_still_takes_a_routine(tmp_path: Path) -> None:
    app, _agent = _ensured(tmp_path)
    app.handle("agent.create", {"name": "plain"})
    plain = app.agents.get("plain").uri

    app.handle("routine.add", {"yaml": _routine("plain-work", plain),
                               "actor": f"user:{OWNER}"})
    names = [row["name"] for row in app.handle(
        "routine.list", {"actor": f"user:{OWNER}"})["routines"]]
    assert "plain-work" in names


def test_the_user_agent_as_the_calling_actor_is_refused(tmp_path: Path) -> None:
    """The other reading of "actor": the caller (routine owner) is the user agent.

    A user agent has no session, so principal verification refuses the claim
    first (PAC_PRINCIPAL_UNVERIFIED / CALLER_NOT_AUTHORIZED): authorization keeps
    precedence and no refusal of ours is needed.
    """

    app, agent = _ensured(tmp_path)
    app.handle("agent.create", {"name": "plain"})
    plain = app.agents.get("plain").uri
    app.handle("routine.add", {"yaml": _routine("owned", plain), "actor": f"user:{OWNER}"})

    for method, params in (
        ("routine.add", {"yaml": _routine("by-user-agent", plain)}),
        ("routine.pause", {"name": "owned"}),
        ("routine.resume", {"name": "owned"}),
    ):
        with pytest.raises(DaemonRequestError) as raised:
            app.handle(method, {**params, "actor": agent.uri})
        assert raised.value.code in {"PAC_PRINCIPAL_UNVERIFIED", "CALLER_NOT_AUTHORIZED"}, (
            method, raised.value.code,
        )
    names = [row["name"] for row in app.handle(
        "routine.list", {"actor": f"user:{OWNER}"})["routines"]]
    assert "by-user-agent" not in names


def test_a_produces_address_with_the_reserved_name_is_refused(tmp_path: Path) -> None:
    import yaml

    app, _agent = _ensured(tmp_path)
    document = yaml.safe_load(_scheduled_routine("coordinator", "unused"))
    document.pop("actor")
    document["produces"] = f"agent:{OWNER}:{NODE}:hyprial-user"

    with pytest.raises(DaemonRequestError) as raised:
        app.handle("routine.add", {"yaml": yaml.safe_dump(document), "actor": f"user:{OWNER}"})
    assert raised.value.code == CODE
