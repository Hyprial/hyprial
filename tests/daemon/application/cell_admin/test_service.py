"""Unit tests for the unregistered cell editing core service (I6-3)."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.application.actors.cell_admin import (
    get_agent_cell,
    update_agent_cell,
)
from hyprial.daemon.impl.cells.actor import (
    CellActorMemberSnapshot,
    CellActorSnapshot,
    CellActorState,
    CellMemberPhase,
)
from hyprial.daemon.impl.cells.model import (
    Cell,
    CellBinding,
    CellError,
    CellMember,
    CellPermissions,
    CellScope,
)
from hyprial.daemon.impl.cells.ports import CellRuntimeHealth
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.kernel import (
    DaemonRequestError,
    canonical_agent_uri,
    ipc_errors,
    keyed_cell_id,
    subject_cell_id,
)
from tests.daemon.cells.params.param_support import org_cell
from tests.daemon.cells.runtime.cell_runtime_support import authority_for

OWNER = authority_for("user:alice")
AGENT_ID = "a.worker00000000000000000001"
AGENT_URI = canonical_agent_uri("alice", "node-1", "worker")
CELL_ID = subject_cell_id("agent", AGENT_ID)
USER_AGENT_ID = "a.user0000000000000000000001"
USER_CELL_ID = subject_cell_id("agent", USER_AGENT_ID)


class _FakeAgents:
    def __init__(self, agents: dict[str, Any]) -> None:
        self._by_key = dict(agents)

    def get(self, name: str) -> Any:
        return self._by_key.get(name)


class _FakeRuntime:
    def __init__(
        self,
        cells: dict[str, Cell],
        *,
        stale: bool = False,
        detail: str | None = None,
        identity: Any = None,
    ) -> None:
        self._cells = dict(cells)
        self._health = CellRuntimeHealth(source_stale=stale, detail=detail, updated_at_ms=100)
        self.identity = identity if identity is not None else SimpleNamespace(authority=OWNER)
        self.update_calls: list[dict[str, Any]] = []

    def lookup(self, cell_id: str) -> Cell | None:
        return self._cells.get(cell_id)

    def health(self) -> CellRuntimeHealth:
        return self._health

    def update(
        self,
        cell_id: str,
        *,
        name: str | None = None,
        members: Any = None,
        permissions: CellPermissions | None = None,
        params: dict[str, Any] | None = None,
        expect_revision: int | None = None,
    ) -> Cell:
        self.update_calls.append(
            {
                "cell_id": cell_id,
                "name": name,
                "members": members,
                "permissions": permissions,
                "params": params,
                "expect_revision": expect_revision,
            }
        )
        current = self._cells.get(cell_id)
        if current is None:
            raise CellError("unknown-cell", f"{cell_id} is not known")
        if current.retired:
            raise CellError("retired", f"{cell_id} is retired")
        if expect_revision is not None and expect_revision != current.revision:
            raise CellError("stale-revision", f"{cell_id} is at revision {current.revision}")
        changes: dict[str, Any] = {}
        if name is not None:
            changes["name"] = name
        if members is not None:
            changes["members"] = tuple(members)
        if permissions is not None:
            changes["permissions"] = permissions
        if params is not None:
            changes["params"] = params
        revised = current.revised(now_ms=current.updated_at_ms + 1, **changes)
        if (
            revised.name,
            revised.members,
            revised.permissions,
            dict(revised.params),
            revised.retired,
        ) == (
            current.name,
            current.members,
            current.permissions,
            dict(current.params),
            current.retired,
        ):
            return current
        self._cells[cell_id] = revised
        return revised


class _FakeActorRegistry:
    def __init__(self, snapshots: dict[str, CellActorSnapshot]) -> None:
        self._snapshots = dict(snapshots)

    def snapshot(self, cell_id: str) -> CellActorSnapshot | None:
        return self._snapshots.get(cell_id)


class _FakeFacade:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def spaces(self) -> list[Any]:
        self.calls += 1
        if self.fail:
            raise OrgFsError("unavailable", {"message": "orgfs down"})
        return []


def _make_cell(*, revision: int = 7, retired: bool = False) -> Cell:
    return Cell(
        cell_id=CELL_ID,
        kind="agent",
        authority=OWNER,
        scope=CellScope.of_org(),
        binding=CellBinding.subject(AGENT_ID),
        name="worker",
        members=(CellMember(AGENT_URI, "harness"),),
        params={"cell.entry": AGENT_URI, "cell.members": {AGENT_URI: {"kind": "claude"}}},
        revision=revision,
        retired=retired,
        updated_at_ms=1000,
    )


def _make_snapshot(*, revision: int = 7, effective_revision: int = 7) -> CellActorSnapshot:
    return CellActorSnapshot(
        cell_id=CELL_ID,
        epoch="ce-00000000000000000001-abc",
        sequence=2,
        revision=revision,
        effective_revision=effective_revision,
        observed_document_digest="obs-digest",
        effective_document_digest="eff-digest",
        entry_member=AGENT_URI,
        state=CellActorState.ACTIVE,
        source_stale=False,
        updated_at_ms=1000,
        members=(
            CellActorMemberSnapshot(
                name=AGENT_URI,
                role="harness",
                kind="claude",
                phase=CellMemberPhase.READY,
                running=True,
            ),
        ),
    )


def _make_app(
    *,
    cell: Cell | None = None,
    snapshot: CellActorSnapshot | None = None,
    stale: bool = False,
    orgfs_fail: bool = False,
    orgfs_runtime_raises: bool = False,
) -> tuple[Any, _FakeFacade]:
    agent = SimpleNamespace(
        actor="worker",
        uri=AGENT_URI,
        agent_id=AGENT_ID,
        cell_id=CELL_ID,
        owner=OWNER,
        capabilities={},
    )
    user_agent = SimpleNamespace(
        actor="hyprial-user",
        uri=canonical_agent_uri("alice", "node-1", "hyprial-user"),
        agent_id=USER_AGENT_ID,
        cell_id=USER_CELL_ID,
        owner=OWNER,
        capabilities={"template": "user"},
    )
    cells = {CELL_ID: cell or _make_cell()}
    runtime = _FakeRuntime(cells, stale=stale)
    facade = _FakeFacade(fail=orgfs_fail)

    def require_orgfs() -> Any:
        if orgfs_runtime_raises:
            raise OrgFsError("unavailable", {"message": "runtime unreachable"})
        return SimpleNamespace(facade=facade)

    app = SimpleNamespace(
        agents=_FakeAgents({"worker": agent, AGENT_URI: agent, "hyprial-user": user_agent}),
        _cell_runtime=runtime,
        _cell_actor_registry=_FakeActorRegistry({CELL_ID: snapshot} if snapshot else {}),
        _require_orgfs_runtime=require_orgfs,
    )
    return app, facade


def test_get_returns_cell_snippet_and_actor_snapshot_with_zero_orgfs_io() -> None:
    snap = _make_snapshot(revision=7, effective_revision=7)
    app, facade = _make_app(cell=_make_cell(revision=7), snapshot=snap, orgfs_runtime_raises=True)

    result = get_agent_cell(app, {"name": "worker"})

    assert facade.calls == 0
    assert result["ok"] is True
    assert result["cell"] == {
        "cellId": CELL_ID,
        "revision": 7,
        "members": [{"ref": AGENT_URI, "role": "harness"}],
        "params": {
            "cell.entry": AGENT_URI,
            "cell.members": {AGENT_URI: {"kind": "claude"}},
        },
    }
    assert result["snapshot"]["epoch"] == snap.epoch
    assert result["snapshot"]["state"] == "active"
    assert result["snapshot"]["effectiveRevision"] == 7
    assert result["snapshot"]["members"][0]["name"] == AGENT_URI
    assert result["snapshot"]["members"][0]["phase"] == "ready"
    assert result["snapshot"]["members"][0]["running"] is True


def test_get_handles_missing_actor_snapshot_and_unknown_agent() -> None:
    app, _ = _make_app(snapshot=None)
    app._cell_actor_registry = None

    result = get_agent_cell(app, {"name": "worker"})
    assert result["ok"] is True
    assert result["snapshot"] is None

    with pytest.raises(DaemonRequestError) as missing:
        get_agent_cell(app, {"name": "nobody"})
    assert missing.value.code == ipc_errors.AGENT_NOT_FOUND


def test_update_cas_succeeds_and_reports_effective_applied_revision() -> None:
    snap = _make_snapshot(revision=7, effective_revision=7)
    app, facade = _make_app(cell=_make_cell(revision=7), snapshot=snap)

    result = update_agent_cell(
        app,
        {
            "name": "worker",
            "expectedRevision": 7,
            "members": [
                {"ref": "planner", "role": "harness"},
                {"ref": "lark:ops", "role": "adapter"},
            ],
            "params": {
                "cell.entry": "planner",
                "cell.members": {
                    "planner": {"kind": "codex"},
                    "lark:ops": {"adapter": "lark-main"},
                },
            },
        },
    )

    assert facade.calls == 1
    assert result == {
        "ok": True,
        "cellId": CELL_ID,
        "revision": 8,
        "appliedRevision": 7,
        "state": "active",
    }
    updated = app._cell_runtime.lookup(CELL_ID)
    assert updated is not None
    assert updated.revision == 8
    assert updated.param("cell.entry") == "planner"


def test_update_cas_conflict_returns_invalid_argument_with_expected_and_current() -> None:
    app, _ = _make_app(cell=_make_cell(revision=9), snapshot=_make_snapshot(revision=9, effective_revision=9))

    with pytest.raises(DaemonRequestError) as conflict:
        update_agent_cell(
            app,
            {
                "name": "worker",
                "expectedRevision": 7,
                "members": [{"ref": AGENT_URI, "role": "harness"}],
                "params": {"cell.entry": AGENT_URI},
            },
        )

    assert conflict.value.code == ipc_errors.INVALID_ARGUMENT
    assert conflict.value.data == {"expected": 7, "current": 9}
    assert app._cell_runtime.update_calls == []


@pytest.mark.parametrize(
    "forbidden",
    [
        {"id": "c.other"},
        {"cellId": "c.other"},
        {"kind": "department"},
        {"authority": authority_for("user:mallory")},
        {"binding": {"type": "minted", "value": ""}},
        {"scope": {"kind": "owner", "org": None}},
        {"retired": True},
        {"retire": True},
        {"revision": 99},
        {"permissions": {"read": ["*"]}},
    ],
)
def test_update_rejects_non_whitelist_and_immutable_fields(forbidden: dict[str, Any]) -> None:
    app, _ = _make_app(cell=_make_cell(revision=7), snapshot=_make_snapshot())

    with pytest.raises(DaemonRequestError) as refused:
        update_agent_cell(
            app,
            {
                "name": "worker",
                "expectedRevision": 7,
                "members": [{"ref": AGENT_URI, "role": "harness"}],
                **forbidden,
            },
        )

    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert app._cell_runtime.update_calls == []


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"members": [{"ref": AGENT_URI, "role": "harness"}], "params": {"cell.entry": "missing"}},
        {"members": [{"ref": "lark:ops", "role": "adapter"}], "params": {"cell.entry": "lark:ops"}},
        {"members": [{"ref": AGENT_URI, "role": "harness"}], "params": {"cell.entry": 123}},
        {"members": [{"ref": AGENT_URI, "role": "harness"}], "params": {"cell.members": {"ghost": {"kind": "claude"}}}},
        {"members": [{"ref": AGENT_URI, "role": "harness"}], "params": {"cell.members": {AGENT_URI: {"kind": 42}}}},
        {"members": [{"ref": AGENT_URI, "role": "harness"}, {"ref": AGENT_URI, "role": "adapter"}], "params": {}},
    ],
)
def test_update_validates_structure_with_parser_and_codec(bad_payload: dict[str, Any]) -> None:
    app, _ = _make_app(cell=_make_cell(revision=7), snapshot=_make_snapshot())

    with pytest.raises(DaemonRequestError) as invalid:
        update_agent_cell(app, {"name": "worker", "expectedRevision": 7, **bad_payload})

    assert invalid.value.code == ipc_errors.INVALID_ARGUMENT
    assert app._cell_runtime.update_calls == []


def test_update_fails_closed_when_orgfs_is_disconnected() -> None:
    for flags in (
        {"stale": True},
        {"orgfs_fail": True},
        {"orgfs_runtime_raises": True},
    ):
        app, _ = _make_app(cell=_make_cell(revision=7), snapshot=_make_snapshot(), **flags)
        with pytest.raises(DaemonRequestError) as disconnected:
            update_agent_cell(
                app,
                {
                    "name": "worker",
                    "expectedRevision": 7,
                    "members": [{"ref": AGENT_URI, "role": "harness"}],
                    "params": {},
                },
            )
        assert disconnected.value.code == ipc_errors.ORG_SOURCE_UNREACHABLE
        assert app._cell_runtime.update_calls == []


def test_update_refuses_a_retired_cell() -> None:
    retired_cell = replace(_make_cell(revision=7), retired=True)
    app_retired, _ = _make_app(cell=retired_cell, snapshot=_make_snapshot())
    with pytest.raises(DaemonRequestError) as retired_refused:
        update_agent_cell(
            app_retired,
            {
                "name": "worker",
                "expectedRevision": 7,
                "members": [],
                "params": {},
            },
        )
    assert retired_refused.value.code == ipc_errors.INVALID_ARGUMENT


def test_update_supports_cell_name_and_rejects_oversized_document() -> None:
    app, _ = _make_app(cell=_make_cell(revision=7), snapshot=_make_snapshot(revision=7, effective_revision=7))

    updated = update_agent_cell(
        app,
        {
            "name": "worker",
            "expectedRevision": 7,
            "cellName": "renamed-cell",
        },
    )
    assert updated["revision"] == 8
    stored = app._cell_runtime.lookup(CELL_ID)
    assert stored is not None
    assert stored.name == "renamed-cell"

    with pytest.raises(DaemonRequestError) as oversized:
        update_agent_cell(
            app,
            {
                "name": "worker",
                "expectedRevision": 8,
                "params": {"blob": "x" * (257 * 1024)},
            },
        )
    assert oversized.value.code == ipc_errors.INVALID_ARGUMENT


def test_get_and_update_reject_non_agent_cell_kind() -> None:
    dept_cell = org_cell("sp-eng", authority=OWNER)
    app, _ = _make_app(cell=_make_cell(revision=7), snapshot=_make_snapshot())
    app._cell_runtime._cells[dept_cell.cell_id] = dept_cell
    dept_agent = SimpleNamespace(
        actor="dept-alias",
        uri=canonical_agent_uri("alice", "node-1", "dept-alias"),
        agent_id="a.dept00000000000000000001",
        cell_id=dept_cell.cell_id,
        owner=OWNER,
        capabilities={},
    )
    app.agents._by_key["dept-alias"] = dept_agent

    with pytest.raises(DaemonRequestError) as get_refused:
        get_agent_cell(app, {"name": "dept-alias"})
    assert get_refused.value.code == ipc_errors.INVALID_ARGUMENT

    with pytest.raises(DaemonRequestError) as update_refused:
        update_agent_cell(
            app,
            {
                "name": "dept-alias",
                "expectedRevision": 1,
                "cellName": "hijacked",
            },
        )
    assert update_refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert app._cell_runtime.update_calls == []


def test_get_and_update_reject_retired_cell() -> None:
    retired_cell = replace(_make_cell(revision=7), retired=True)
    app_retired, _ = _make_app(cell=retired_cell, snapshot=_make_snapshot())

    with pytest.raises(DaemonRequestError) as get_retired:
        get_agent_cell(app_retired, {"name": "worker"})
    assert get_retired.value.code == ipc_errors.INVALID_ARGUMENT

    with pytest.raises(DaemonRequestError) as update_retired:
        update_agent_cell(
            app_retired,
            {
                "name": "worker",
                "expectedRevision": 7,
                "members": [],
                "params": {},
            },
        )
    assert update_retired.value.code == ipc_errors.INVALID_ARGUMENT
    assert app_retired._cell_runtime.update_calls == []


def test_a_cell_with_the_user_template_is_held_to_the_user_agent_allow_list() -> None:
    """I7a (contract i7-0 v2 section 2.3): the cell's template decides, whatever record names it."""

    app, _ = _make_app(cell=_make_cell(revision=7), snapshot=_make_snapshot())
    user_cell = Cell(
        cell_id=USER_CELL_ID,
        kind="agent",
        authority=OWNER,
        scope=CellScope.of_org(),
        binding=CellBinding.subject(USER_AGENT_ID),
        name="hyprial-user",
        members=(),
        params={"agent.role": "user", "agent.template": "user"},
        revision=1,
        retired=False,
        updated_at_ms=1000,
    )
    app._cell_runtime._cells[USER_CELL_ID] = user_cell

    assert get_agent_cell(app, {"name": "hyprial-user"})["cell"]["cellId"] == USER_CELL_ID

    with pytest.raises(DaemonRequestError) as update_user:
        update_agent_cell(
            app,
            {
                "name": "hyprial-user",
                "expectedRevision": 1,
                "members": [],
                "params": {},
            },
        )
    assert update_user.value.code == ipc_errors.USER_AGENT_IMMUTABLE
    assert app._cell_runtime.update_calls == []

    shadow_agent = SimpleNamespace(
        actor="user-shadow",
        uri=canonical_agent_uri("alice", "node-1", "user-shadow"),
        agent_id=USER_AGENT_ID,
        cell_id=USER_CELL_ID,
        owner=OWNER,
        capabilities={},
    )
    app.agents._by_key["user-shadow"] = shadow_agent

    with pytest.raises(DaemonRequestError) as update_shadow:
        update_agent_cell(
            app,
            {
                "name": "user-shadow",
                "expectedRevision": 1,
                "cellName": "hijacked",
            },
        )
    assert update_shadow.value.code == ipc_errors.INVALID_ARGUMENT
    assert app._cell_runtime.update_calls == []


@pytest.mark.parametrize(
    "org_params",
    [
        {"org.issuer": "evil"},
        {"org.name": "acme"},
        {"org.members": {}},
        {"org.": "empty-suffix"},
    ],
)
def test_update_rejects_org_namespace_param_keys(org_params: dict[str, Any]) -> None:
    app, _ = _make_app(cell=_make_cell(revision=7), snapshot=_make_snapshot())

    with pytest.raises(DaemonRequestError) as refused:
        update_agent_cell(
            app,
            {
                "name": "worker",
                "expectedRevision": 7,
                "params": org_params,
            },
        )

    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert app._cell_runtime.update_calls == []


def test_update_rejects_user_template_param_preventing_self_brick() -> None:
    app, _ = _make_app(
        cell=_make_cell(revision=7),
        snapshot=_make_snapshot(revision=7, effective_revision=7),
    )

    with pytest.raises(DaemonRequestError) as refused:
        update_agent_cell(
            app,
            {
                "name": "worker",
                "expectedRevision": 7,
                "params": {"agent.template": "user"},
            },
        )
    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert app._cell_runtime.update_calls == []

    fetched = get_agent_cell(app, {"name": "worker"})
    assert fetched["ok"] is True

    updated = update_agent_cell(
        app,
        {
            "name": "worker",
            "expectedRevision": 7,
            "params": {"agent.template": "squire"},
        },
    )
    assert updated["revision"] == 8


def test_get_and_update_reject_cross_agent_and_wrong_subject_agent_cells() -> None:
    app, _ = _make_app(cell=_make_cell(revision=7), snapshot=_make_snapshot())

    other_agent_id = "a.other000000000000000000001"
    other_cell_id = subject_cell_id("agent", other_agent_id)
    app._cell_runtime._cells[other_cell_id] = Cell(
        cell_id=other_cell_id,
        kind="agent",
        authority=OWNER,
        scope=CellScope.of_org(),
        binding=CellBinding.subject(other_agent_id),
        name="other",
        members=(),
        params={},
        revision=1,
        retired=False,
        updated_at_ms=1000,
    )
    app.agents._by_key["impostor"] = SimpleNamespace(
        actor="impostor",
        uri=canonical_agent_uri("alice", "node-1", "impostor"),
        agent_id="a.impostor000000000000000001",
        cell_id=other_cell_id,
        owner=OWNER,
        capabilities={},
    )

    keyed_id = keyed_cell_id("agent", OWNER, "rogue")
    app._cell_runtime._cells[keyed_id] = Cell(
        cell_id=keyed_id,
        kind="agent",
        authority=OWNER,
        scope=CellScope.of_org(),
        binding=CellBinding.keyed("rogue"),
        name="rogue",
        members=(),
        params={},
        revision=1,
        retired=False,
        updated_at_ms=1000,
    )
    app.agents._by_key["keyed-agent"] = SimpleNamespace(
        actor="keyed-agent",
        uri=canonical_agent_uri("alice", "node-1", "keyed-agent"),
        agent_id="a.keyed000000000000000000001",
        cell_id=keyed_id,
        owner=OWNER,
        capabilities={},
    )

    minted_agent_id = "a.minted00000000000000000001"
    minted_cell_id = subject_cell_id("agent", minted_agent_id)
    app._cell_runtime._cells[minted_cell_id] = Cell(
        cell_id=minted_cell_id,
        kind="agent",
        authority=OWNER,
        scope=CellScope.of_org(),
        binding=CellBinding.minted(),
        name="minted",
        members=(),
        params={},
        revision=1,
        retired=False,
        updated_at_ms=1000,
    )
    app.agents._by_key["minted-agent"] = SimpleNamespace(
        actor="minted-agent",
        uri=canonical_agent_uri("alice", "node-1", "minted-agent"),
        agent_id=minted_agent_id,
        cell_id=minted_cell_id,
        owner=OWNER,
        capabilities={},
    )

    for name in ("impostor", "keyed-agent", "minted-agent"):
        with pytest.raises(DaemonRequestError) as get_refused:
            get_agent_cell(app, {"name": name})
        assert get_refused.value.code == ipc_errors.INVALID_ARGUMENT

        with pytest.raises(DaemonRequestError) as update_refused:
            update_agent_cell(
                app,
                {
                    "name": name,
                    "expectedRevision": 1,
                    "cellName": "hijacked",
                },
            )
        assert update_refused.value.code == ipc_errors.INVALID_ARGUMENT

    assert app._cell_runtime.update_calls == []


def test_get_and_update_reject_foreign_authority_agent_cell() -> None:
    foreign_cell = replace(_make_cell(revision=7), authority=authority_for("user:mallory"))
    app, _ = _make_app(cell=foreign_cell, snapshot=_make_snapshot())

    with pytest.raises(DaemonRequestError) as get_author_refused:
        get_agent_cell(app, {"name": "worker"})
    assert get_author_refused.value.code == ipc_errors.INVALID_ARGUMENT

    with pytest.raises(DaemonRequestError) as update_author_refused:
        update_agent_cell(
            app,
            {
                "name": "worker",
                "expectedRevision": 7,
                "cellName": "hijacked",
            },
        )
    assert update_author_refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert app._cell_runtime.update_calls == []

    app._cell_runtime.author = None
    app._cell_runtime.identity = SimpleNamespace(authority=OWNER)

    with pytest.raises(DaemonRequestError) as get_identity_refused:
        get_agent_cell(app, {"name": "worker"})
    assert get_identity_refused.value.code == ipc_errors.INVALID_ARGUMENT

    with pytest.raises(DaemonRequestError) as update_identity_refused:
        update_agent_cell(
            app,
            {
                "name": "worker",
                "expectedRevision": 7,
                "cellName": "hijacked",
            },
        )
    assert update_identity_refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert app._cell_runtime.update_calls == []
