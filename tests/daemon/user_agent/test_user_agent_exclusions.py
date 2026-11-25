"""Listings, worker admission and replication exclusions for local and remote user agents.

"Remote" is another machine's instance of the same owner: the same name, its
own id and machine segment.  It reaches this node only as an address
(presence liveliness carries the URI and nothing else), so only its name form
is recognised; its id form is an ordinary agent address until the merge step.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.cells.runtime.agents import agent_cell_spec
from hyprial.daemon.impl.dispatch.admission import dispatch_gate
from hyprial.daemon.impl.org.membership.migration.inventory import snapshot
from hyprial.daemon.impl.transfer.landing.registry import (
    LandingError,
    RegistryLandingAuthority,
)
from hyprial.daemon.impl.user_agent import (
    CREATED,
    USER_AGENT_NAME,
    local_user_agent,
    run_user_agent_ensure,
)
from hyprial.kernel import DaemonRequestError
from tests.daemon.org_membership.storage.support import FakeFacade
from tests.identity.agents.registry.entity.support import (
    NODE,
    OWNER,
    _close_test_applications as _close_test_applications,
    application,
)

REMOTE = f"agent:{OWNER}:node-2:{USER_AGENT_NAME}"
REMOTE_ID_FORM = f"agent:{OWNER}:node-2:a.aaaaaaaaaaaaaaaaaaaaaaaaaa"


def _ensured(tmp_path: Path):  # type: ignore[no-untyped-def]
    app = application(tmp_path)
    assert run_user_agent_ensure(app) == CREATED
    agent = local_user_agent(app.agents.list(), OWNER)
    assert agent is not None
    return app, agent


def _quiet(*_args: Any, **_fields: Any) -> None:
    return None


@pytest.mark.parametrize("role", ["plan", "dispatch", "review", "execute"])
def test_no_work_is_dispatched_to_a_local_or_remote_user_agent(role: str) -> None:
    for target, facts in (
        (f"agent:{OWNER}:{NODE}:a.bbbbbbbbbbbbbbbbbbbbbbbbbb", {"template": "user"}),
        (f"agent:{OWNER}:{NODE}:{USER_AGENT_NAME}", {}),
        (REMOTE, {}),
    ):
        with pytest.raises(DaemonRequestError) as raised:
            dispatch_gate(target=target, capabilities=facts, role=role,
                          emit=_quiet, source="test")
        assert raised.value.code == "DISPATCH_ROLE_MISMATCH"
        assert "user agent" in str(raised.value)


@pytest.mark.parametrize("role", ["plan", "dispatch", "review", "execute"])
def test_pending_merge_another_machines_user_agent_id_form_is_gated_like_any_agent(
    role: str,
) -> None:
    """Pinned for the later merge step: no local record, no name form, no refusal."""

    assert dispatch_gate(target=REMOTE_ID_FORM, capabilities={"interactive": False},
                         role=role, emit=_quiet, source="test") == ()


def test_plain_messages_to_a_user_agent_are_not_gated() -> None:
    assert dispatch_gate(target=REMOTE, capabilities={}, role=None,
                         emit=_quiet, source="test") == ()


def test_an_ordinary_agent_is_still_a_worker() -> None:
    assert dispatch_gate(target=f"agent:{OWNER}:node-2:worker", capabilities={"interactive": False},
                         role="execute", emit=_quiet, source="test") == ()


def test_targets_never_list_a_local_or_remote_user_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, agent = _ensured(tmp_path)
    app.handle("agent.create", {"name": "plain"})
    original = app._actor_status_snapshot

    def with_remote() -> list[dict[str, Any]]:
        return [*original(), {"actor": REMOTE, "status": "online"},
                {"actor": REMOTE_ID_FORM, "status": "online"}]

    monkeypatch.setattr(app, "_actor_status_snapshot", with_remote)

    rows = {row["targetUri"]: row for row in app.handle("targets", {"kind": "agent"})["targets"]}

    assert agent.uri not in rows
    assert REMOTE not in rows
    assert [row.get("displayName") for row in rows.values() if row["targetUri"] != REMOTE_ID_FORM] == [
        "plain"
    ]
    assert REMOTE_ID_FORM in rows, "pending merge: an id-form remote address is an ordinary agent"


def test_the_opt_in_agent_list_shows_the_marker_and_never_offers_a_routine(
    tmp_path: Path,
) -> None:
    app, agent = _ensured(tmp_path)
    app.handle("agent.create", {"name": "plain"})

    listed = app.handle("agent.list", {"includeUserAgent": True})
    rows = {row["actor"]: row for row in listed["agents"]}

    assert rows[USER_AGENT_NAME]["capabilities"] == {"template": "user"}
    assert rows[USER_AGENT_NAME]["userAgent"] is True
    assert rows[USER_AGENT_NAME]["userActor"] == f"user:{OWNER}"
    assert "role" not in rows[USER_AGENT_NAME]
    assert agent.uri not in listed["agentsWithoutRoutine"]
    assert "userAgent" not in rows["plain"] and "userActor" not in rows["plain"]


def test_the_restore_wake_scan_never_starts_it(tmp_path: Path) -> None:
    app, _agent = _ensured(tmp_path)

    app._restore_wake_scan(0)

    assert app.desired_state.load().harnesses == ()
    assert app._harnesses.running == {}


def test_the_agent_cell_spec_has_no_member(tmp_path: Path) -> None:
    _app, agent = _ensured(tmp_path)

    spec = agent_cell_spec(agent)

    assert spec.members == ()
    assert spec.name == USER_AGENT_NAME
    assert spec.binding.value == agent.agent_id


def test_the_membership_migration_inventory_leaves_it_out(tmp_path: Path) -> None:
    app, agent = _ensured(tmp_path)
    app.handle("agent.create", {"name": "plain"})

    rows = snapshot(app, FakeFacade(owner=OWNER))["agents"]

    assert [row["name"] for row in rows] == ["plain"]
    assert agent.uri not in {row["agent"] for row in rows}


class _NeverCreates:
    owner = OWNER

    def create(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the reserved name is refused before the row is minted")


def test_a_transfer_never_lands_the_reserved_name(tmp_path: Path) -> None:
    authority = RegistryLandingAuthority(
        _NeverCreates(), home_root=tmp_path / "agents", granted_by="user:" + OWNER  # type: ignore[arg-type]
    )

    with pytest.raises(LandingError):
        authority.adopt_agent(USER_AGENT_NAME, owner=OWNER, home=tmp_path / "staged",
                              source_machine="node-2")
