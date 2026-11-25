"""I7a: the owner edits the user agent cell through ``agent.cell.update`` within one allow-list.

Decision notes (contract i7-0 v2 section 2.3, owner items A-1 and A-8):

- The user agent cell may differ from its current document only in its
  adapter members, their ``cell.members.<ref>.adapter`` datum and ``user``
  connections between them.  Everything else (cell name, template, role,
  any other param key) must stay byte-equal, so ``cell show`` round-trips
  through ``cell apply``.
- A changed or removed template is ``USER_AGENT_IMMUTABLE``; every other
  difference is ``INVALID_ARGUMENT`` with a message naming the rule.
- Only the local operator reads the user agent cell; any other caller keeps
  ``USER_AGENT_IMMUTABLE``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from hyprial.daemon.impl.application.actors.cell_admin import (
    get_agent_cell,
    update_agent_cell,
)
from hyprial.daemon.impl.cells.model import Cell, CellBinding, CellScope, thaw_json
from hyprial.daemon.impl.request_catalog.agent.cell import (
    AGENT_CELL_GET_SPEC,
    AGENT_CELL_UPDATE_SPEC,
)
from hyprial.daemon.impl.user_agent import admin
from hyprial.kernel import DaemonRequestError, ipc_errors
from tests.daemon.application.cell_admin.test_service import (
    OWNER,
    USER_AGENT_ID,
    USER_CELL_ID,
    _make_app,
    _make_cell,
    _make_snapshot,
)

USER_PARAMS = {"agent.role": "user", "agent.template": "user"}
ADAPTER_MEMBER = {"ref": "im", "role": "adapter"}
BOUND_PARAMS = {
    **USER_PARAMS,
    "cell.members": {"im": {"adapter": "personal-bot"}},
    "cell.connections": [{"from": "im", "on": "user", "to": "im", "route": "my-dm"}],
}


def _operator(params: dict[str, Any], *, trusted_message_origin: str | None) -> tuple[bool, None]:
    return trusted_message_origin != "worker", None


def _user_app(*, members: tuple[Any, ...] = (), params: dict[str, Any] | None = None) -> Any:
    app, _ = _make_app(cell=_make_cell(revision=7), snapshot=_make_snapshot())
    app._identity_binding_operator = _operator
    app._cell_runtime._cells[USER_CELL_ID] = Cell(
        cell_id=USER_CELL_ID,
        kind="agent",
        authority=OWNER,
        scope=CellScope.of_org(),
        binding=CellBinding.subject(USER_AGENT_ID),
        name="hyprial-user",
        members=members,
        params=dict(USER_PARAMS if params is None else params),
        revision=1,
        updated_at_ms=1000,
    )
    return app


def _update(app: Any, **fields: Any) -> dict[str, Any]:
    return update_agent_cell(app, {"name": "hyprial-user", "expectedRevision": 1, **fields})


def _refused(app: Any, **fields: Any) -> DaemonRequestError:
    with pytest.raises(DaemonRequestError) as refused:
        _update(app, **fields)
    assert app._cell_runtime.update_calls == []
    return refused.value


def test_the_owner_writes_an_adapter_member_and_a_user_connection() -> None:
    app = _user_app()

    updated = _update(app, members=[ADAPTER_MEMBER], params=BOUND_PARAMS)

    assert (updated["ok"], updated["revision"]) == (True, 2)
    cell = app._cell_runtime.lookup(USER_CELL_ID)
    assert [member.to_json() for member in cell.members] == [ADAPTER_MEMBER]
    assert cell.param("cell.members") == {"im": {"adapter": "personal-bot"}}
    assert cell.param("agent.template") == "user"


def test_members_alone_are_accepted_and_keep_the_current_params() -> None:
    app = _user_app()

    updated = _update(app, members=[ADAPTER_MEMBER])

    assert updated["revision"] == 2
    assert thaw_json(app._cell_runtime.lookup(USER_CELL_ID).params) == USER_PARAMS


def test_apply_on_the_user_agent_returns_without_waiting_and_states_what_it_shows() -> None:
    app = _user_app()

    updated = _update(app, members=[ADAPTER_MEMBER], params=BOUND_PARAMS)

    assert (updated["appliedRevision"], updated["state"]) == (0, "unknown")


def test_show_round_trips_through_apply_unchanged() -> None:
    from hyprial.daemon.impl.cells.model import CellMember

    app = _user_app(members=(CellMember("im", "adapter"),), params=BOUND_PARAMS)
    shown = get_agent_cell(app, {"name": "hyprial-user"})["cell"]

    applied = _update(app, members=shown["members"], params=shown["params"])

    assert applied["revision"] == 1
    assert thaw_json(app._cell_runtime.lookup(USER_CELL_ID).params) == BOUND_PARAMS


@pytest.mark.parametrize(
    "params",
    [
        {**BOUND_PARAMS, "agent.template": "squire"},
        {key: value for key, value in BOUND_PARAMS.items() if key != "agent.template"},
    ],
    ids=["changed", "removed"],
)
def test_a_changed_or_removed_template_is_user_agent_immutable(params: dict[str, Any]) -> None:
    error = _refused(_user_app(), members=[ADAPTER_MEMBER], params=params)

    assert error.code == ipc_errors.USER_AGENT_IMMUTABLE


_VIOLATIONS: dict[str, dict[str, Any]] = {
    "harness-member": {
        "members": [ADAPTER_MEMBER, {"ref": "w", "role": "harness"}],
        "params": BOUND_PARAMS,
    },
    "cell-entry": {"members": [ADAPTER_MEMBER], "params": {**BOUND_PARAMS, "cell.entry": "im"}},
    "cell-fail-open": {
        "members": [ADAPTER_MEMBER], "params": {**BOUND_PARAMS, "cell.failOpen": "im"},
    },
    "member-kind": {
        "members": [ADAPTER_MEMBER],
        "params": {**BOUND_PARAMS, "cell.members": {"im": {"adapter": "b", "kind": "claude"}}},
    },
    "member-model": {
        "members": [ADAPTER_MEMBER],
        "params": {**BOUND_PARAMS, "cell.members": {"im": {"adapter": "b", "model": "m"}}},
    },
    "empty-adapter": {
        "members": [ADAPTER_MEMBER],
        "params": {**BOUND_PARAMS, "cell.members": {"im": {"adapter": ""}}},
    },
    "another-label": {
        "members": [ADAPTER_MEMBER],
        "params": {
            **BOUND_PARAMS,
            "cell.connections": [{"from": "im", "on": "done", "to": "im"}],
        },
    },
    "extra-connection-key": {
        "members": [ADAPTER_MEMBER],
        "params": {
            **BOUND_PARAMS,
            "cell.connections": [{"from": "im", "on": "user", "to": "im", "note": "x"}],
        },
    },
    "non-string-endpoint": {
        "members": [ADAPTER_MEMBER],
        "params": {
            **BOUND_PARAMS,
            "cell.connections": [{"from": ["im"], "on": "user", "to": "im"}],
        },
    },
    "new-param-key": {
        "members": [ADAPTER_MEMBER], "params": {**BOUND_PARAMS, "routine.retention": 3},
    },
    "changed-role": {
        "members": [ADAPTER_MEMBER], "params": {**BOUND_PARAMS, "agent.role": "admin"},
    },
    "changed-cell-name": {"cellName": "renamed"},
}


@pytest.mark.parametrize("fields", list(_VIOLATIONS.values()), ids=list(_VIOLATIONS))
def test_every_other_difference_is_invalid_argument(fields: dict[str, Any]) -> None:
    error = _refused(_user_app(), **fields)

    assert error.code == ipc_errors.INVALID_ARGUMENT
    assert "user agent cell" in str(error)


def test_an_ordinary_cell_still_cannot_take_the_user_template() -> None:
    app = _user_app()

    with pytest.raises(DaemonRequestError) as refused:
        update_agent_cell(
            app, {"name": "worker", "expectedRevision": 7, "params": {"agent.template": "user"}},
        )

    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert app._cell_runtime.update_calls == []


def test_the_local_operator_reads_the_user_agent_cell() -> None:
    app = _user_app()

    shown = get_agent_cell(app, {"name": "hyprial-user"}, trusted_message_origin="ipc")

    assert shown["cell"]["cellId"] == USER_CELL_ID
    assert shown["cell"]["params"] == USER_PARAMS


def test_a_bound_agent_caller_cannot_read_the_user_agent_cell() -> None:
    app = _user_app()

    with pytest.raises(DaemonRequestError) as refused:
        get_agent_cell(app, {"name": "hyprial-user"}, trusted_message_origin="worker")

    assert refused.value.code == ipc_errors.USER_AGENT_IMMUTABLE
    assert get_agent_cell(app, {"name": "worker"}, trusted_message_origin="worker")["ok"] is True


def test_the_dispatch_gate_exempts_exactly_the_two_cell_document_methods() -> None:
    assert admin.CELL_DOCUMENT_EXEMPT == frozenset({"agent.cell.get", "agent.cell.update"})
    assert admin.refused_target_param(AGENT_CELL_GET_SPEC) is None
    assert admin.refused_target_param(AGENT_CELL_UPDATE_SPEC) is None
    assert admin.refused_target_param(replace(AGENT_CELL_UPDATE_SPEC, name="agent.other")) == "name"


def test_a_user_agent_cell_without_its_template_seed_fails_closed() -> None:
    app = _user_app(params={})

    harness = _refused(app, members=[ADAPTER_MEMBER, {"ref": "w", "role": "harness"}])
    template = _refused(app, params={"agent.template": "x"})
    adapter = _refused(app, members=[ADAPTER_MEMBER], params={"agent.template": "user"})

    assert harness.code == ipc_errors.USER_AGENT_IMMUTABLE
    assert template.code == ipc_errors.USER_AGENT_IMMUTABLE
    assert adapter.code == ipc_errors.INVALID_ARGUMENT
    with pytest.raises(DaemonRequestError) as read:
        get_agent_cell(app, {"name": "hyprial-user"}, trusted_message_origin="worker")
    assert read.value.code == ipc_errors.USER_AGENT_IMMUTABLE
