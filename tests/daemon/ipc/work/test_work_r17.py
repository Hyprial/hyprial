"""R17b name-conflict discovery, reading, and resolution contracts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.application.messaging import work_items as work_commands
from hyprial.daemon.impl.application.messaging.work_ledger import (
    WorkVerificationLedger,
)
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.pac.views import missions
from hyprial.kernel import DaemonRequestError
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _deep_module_daemon_dependencies,
)

from .test_work_ipc import (
    OTHER,
    OWNER,
    _FakeHarnesses,
    _FakeInbox,
    _FakePresence,
    _add,
    _register_worker,
    _session,
)


@pytest.fixture
def work_daemon(
    tmp_path: Path,
    own_daemon_application,
    monkeypatch: pytest.MonkeyPatch,
):  # type: ignore[no-untyped-def]
    app = own_daemon_application(
        DaemonApplication(
            state_dir=tmp_path / "state",
            socket_path=tmp_path / "daemon.sock",
            hyprial_home=tmp_path / "home",
            node_id="node-1",
            owner="allen",
            **_deep_module_daemon_dependencies(),
        )
    )
    app._inbox = _FakeInbox()  # type: ignore[assignment]
    app._harnesses = _FakeHarnesses()  # type: ignore[assignment]
    app._presence = _FakePresence()  # type: ignore[assignment]
    app._restore_done.set()
    app._ipc_peer.pid = 9000
    monkeypatch.setattr(
        work_commands,
        "_read_process_parent",
        lambda pid: 1 if pid == 9000 else 0,
        raising=False,
    )
    monkeypatch.setattr(
        work_commands,
        "process_birth_identity",
        lambda pid: f"birth:{pid}",
        raising=False,
    )
    space_id = app._handle_orgfs("orgfs.create", {"name": "work"})["space_id"]
    app._handle_orgfs("orgfs.mkdir", {"spaceId": space_id, "path": "work"})
    return app, space_id


def _copies(
    app: Any,
    space_id: str,
    item_id: str,
    *,
    total: int = 2,
    invalid_last: bool = False,
) -> tuple[str, ...]:
    facade = app._require_orgfs_runtime().facade
    original, _version = facade.read_text(space_id, f"work/{item_id}.md")
    for number in range(2, total + 1):
        text = (
            "this copy is not a work item\n"
            if invalid_last and number == total
            else original.replace("Daemon-owned work", f"copy {number}", 1)
        )
        staged = facade.write_text(space_id, f"work/staged-{number}.md", text)
        if number == 2:
            facade.move(space_id, f"id:{staged.node_id}", f"work/{item_id}.md")
        else:
            # A third replica can arrive from replication after the local path
            # is already ambiguous. Reproduce that projected state directly;
            # the first two copies still use the public OrgFS conflict path.
            facade._spaces[space_id].nodes[staged.node_id].name = f"{item_id}.md"
    return tuple(
        sorted(node.node_id for node in facade.resolve(space_id, f"work/{item_id}.md"))
    )


def _mission_request(app: Any):
    def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "workflow.list":
            return {
                "runs": [
                    {
                        "graphId": "wf-round",
                        "name": "conflict patrol",
                        "sender": "user:allen",
                        "state": "running",
                        "createdAtMs": 10**12,
                        "routineName": "missions",
                    }
                ]
            }
        return app._handle_orgfs(method, params)

    return request


def test_conflicts_are_discoverable_with_every_node_id(work_daemon) -> None:
    app, space_id = work_daemon
    added = _add(app, space_id, identity={"actor": "user:allen"})
    item_id = added["item"]["id"]
    node_ids = _copies(app, space_id, item_id)

    listed = app.handle(
        "work.ls",
        {"spaceId": space_id, "invalid": True, "actor": "user:allen"},
    )
    collected = missions.collect(
        _mission_request(app),
        space_id,
        routine="missions",
        current="wf-round",
        now_ms=10**12,
    )

    conflicts = [item for item in listed["items"] if item["id"] == item_id]
    assert sorted(item["nodeId"] for item in conflicts) == list(node_ids)
    assert collected["conflicts"] == [
        {"path": f"work/{item_id}.md", "nodeIds": list(node_ids)}
    ]


def test_show_refuses_ambiguous_path_and_reads_each_raw_copy(work_daemon) -> None:
    app, space_id = work_daemon
    added = _add(app, space_id, identity={"actor": "user:allen"})
    item_id = added["item"]["id"]
    node_ids = _copies(app, space_id, item_id, invalid_last=True)

    with pytest.raises(DaemonRequestError) as ambiguous:
        app.handle(
            "work.show",
            {"spaceId": space_id, "itemId": item_id, "actor": "user:allen"},
        )
    assert ambiguous.value.code == "WORK_ITEM_NAME_CONFLICT"
    assert ambiguous.value.data == {"itemId": item_id, "nodeIds": list(node_ids)}

    copies = [
        app.handle(
            "work.show",
            {
                "spaceId": space_id,
                "itemId": item_id,
                "node": f"id:{node_id}",
                "actor": "user:allen",
            },
        )
        for node_id in node_ids
    ]
    assert {copy["nodeId"] for copy in copies} == set(node_ids)
    assert any(copy["item"].get("invalid") is True for copy in copies)
    assert any("Daemon-owned work" in copy["raw"] for copy in copies)
    assert any(copy["raw"] == "this copy is not a work item\n" for copy in copies)


def test_owner_resolve_moves_instead_of_deleting_and_records_proof(
    work_daemon,
) -> None:
    app, space_id = work_daemon
    _register_worker(app, OWNER, "owner-session")
    added = _add(
        app,
        space_id,
        identity=_session(OWNER, "owner-session"),
        owner=OWNER,
    )
    item_id = added["item"]["id"]
    node_ids = _copies(app, space_id, item_id)
    kept_node = node_ids[0]

    resolved = app.handle(
        "work.resolve",
        {
            "spaceId": space_id,
            "itemId": item_id,
            "keep": f"id:{kept_node}",
            **_session(OWNER, "owner-session"),
        },
    )

    assert resolved["itemId"] == item_id
    assert resolved["keptNode"] == kept_node
    assert len(resolved["moved"]) == 1
    moved = resolved["moved"][0]
    assert moved["nodeId"] == next(node for node in node_ids if node != kept_node)
    assert moved["itemId"].startswith("M-owner-")
    facade = app._require_orgfs_runtime().facade
    rows = [row for row in facade.listdir(space_id, "work") if not row.deleted]
    assert sorted(row.name for row in rows) == sorted(
        [f"{item_id}.md", f"{moved['itemId']}.md"]
    )
    assert len(facade.resolve(space_id, f"work/{item_id}.md")) == 1

    kept = app.handle(
        "work.show",
        {"spaceId": space_id, "itemId": item_id, "actor": "user:allen"},
    )
    split = app.handle(
        "work.show",
        {"spaceId": space_id, "itemId": moved["itemId"], "actor": "user:allen"},
    )
    assert kept["item"]["log"][-1].endswith(
        f"{OWNER}: resolve kept id:{kept_node}; other copy -> {moved['itemId']}"
    )
    assert split["item"]["log"][-1].endswith(
        f"{OWNER}: resolve split from {item_id} (conflict)"
    )
    assert app.handle(
        "work.ls",
        {"spaceId": space_id, "invalid": True, "actor": "user:allen"},
    )["items"] == []

    rows = WorkVerificationLedger(
        Path(app.state_dir) / "work-verification-ledger.json"
    ).rows()
    assert rows[-1] == {
        "action": "resolve",
        "itemId": item_id,
        "spaceId": space_id,
        "keptNode": kept_node,
        "movedTo": moved["itemId"],
        "caller": OWNER,
        "callerVerified": True,
        "producedVersion": resolved["producedVersion"],
    }


def test_resolve_enforces_membership_operator_and_typed_target_errors(
    work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any rw member may resolve (it moves, never edits); a non-member may not.

    Squire runs resolve after the user decides and is never that user's
    operator, so an owner-only guard would block the conflict patrol.
    """

    app, space_id = work_daemon
    _register_worker(app, OWNER, "owner-session")
    _register_worker(app, OTHER, "other-session")
    owned = _add(
        app,
        space_id,
        identity=_session(OWNER, "owner-session"),
        owner=OWNER,
    )
    owned_id = owned["item"]["id"]
    owned_nodes = _copies(app, space_id, owned_id)

    # An authorised caller whose principal is not (or no longer) a member:
    # this daemon only admits its own owner's actors, so drop that owner from
    # the member list rather than inventing a foreign caller.
    facade = app._require_orgfs_runtime().facade
    members = facade.members
    with monkeypatch.context() as patched:
        patched.setattr(
            facade,
            "members",
            lambda space: tuple(m for m in members(space) if m.user != "user:allen"),
        )
        with pytest.raises(DaemonRequestError) as outsider:
            app.handle(
                "work.resolve",
                {
                    "spaceId": space_id,
                    "itemId": owned_id,
                    "keep": f"id:{owned_nodes[0]}",
                    **_session(OTHER, "other-session"),
                },
            )
    assert outsider.value.code == "WORK_ITEM_MEMBER_REQUIRED"
    assert outsider.value.data == {"caller": OTHER}
    assert len(facade.resolve(space_id, f"work/{owned_id}.md")) == 2

    with pytest.raises(DaemonRequestError) as unknown:
        app.handle(
            "work.resolve",
            {
                "spaceId": space_id,
                "itemId": owned_id,
                "keep": "id:not-a-copy",
                **_session(OWNER, "owner-session"),
            },
        )
    assert unknown.value.code == "WORK_ITEM_CONFLICT_NODE_UNKNOWN"

    by_member = app.handle(
        "work.resolve",
        {
            "spaceId": space_id,
            "itemId": owned_id,
            "keep": f"id:{owned_nodes[0]}",
            **_session(OTHER, "other-session"),
        },
    )
    assert by_member["caller"] == OTHER
    kept = app.handle(
        "work.show", {"spaceId": space_id, "itemId": owned_id, "actor": "user:allen"}
    )
    assert f" {OTHER}: resolve kept id:{owned_nodes[0]}" in kept["item"]["log"][-1]
    member_rows = [
        row
        for row in WorkVerificationLedger(
            Path(app.state_dir) / "work-verification-ledger.json"
        ).rows()
        if row.get("action") == "resolve" and row.get("itemId") == owned_id
    ]
    assert [row["caller"] for row in member_rows] == [OTHER]

    plain = _add(app, space_id, identity={"actor": "user:allen"})
    plain_node = app._require_orgfs_runtime().facade.resolve(
        space_id, f"work/{plain['item']['id']}.md"
    )[0]
    with pytest.raises(DaemonRequestError) as not_conflicted:
        app.handle(
            "work.resolve",
            {
                "spaceId": space_id,
                "itemId": plain["item"]["id"],
                "keep": f"id:{plain_node.node_id}",
                "actor": "user:allen",
            },
        )
    assert not_conflicted.value.code == "WORK_ITEM_NOT_CONFLICTED"

    operator_item = _add(
        app,
        space_id,
        identity={"actor": "user:allen"},
        owner="user:allen",
    )
    operator_id = operator_item["item"]["id"]
    operator_nodes = _copies(app, space_id, operator_id)
    resolved = app.handle(
        "work.resolve",
        {
            "spaceId": space_id,
            "itemId": operator_id,
            "keep": f"id:{operator_nodes[0]}",
            "actor": "user:allen",
        },
    )
    shown = app.handle(
        "work.show",
        {"spaceId": space_id, "itemId": operator_id, "actor": "user:allen"},
    )
    assert shown["item"]["log"][-1].endswith("(unverified operator)")
    ledger = WorkVerificationLedger(
        Path(app.state_dir) / "work-verification-ledger.json"
    ).rows()
    operator_rows = [
        row
        for row in ledger
        if row.get("action") == "resolve" and row.get("itemId") == operator_id
    ]
    assert len(operator_rows) == 1
    assert operator_rows[0]["callerVerified"] is False
    assert operator_rows[0]["producedVersion"] == resolved["producedVersion"]


def test_resolve_three_copies_allocates_two_distinct_ids(work_daemon) -> None:
    app, space_id = work_daemon
    added = _add(
        app,
        space_id,
        identity={"actor": "user:allen"},
        owner="user:allen",
    )
    item_id = added["item"]["id"]
    node_ids = _copies(app, space_id, item_id, total=3)

    resolved = app.handle(
        "work.resolve",
        {
            "spaceId": space_id,
            "itemId": item_id,
            "keep": f"id:{node_ids[0]}",
            "actor": "user:allen",
        },
    )

    moved_ids = [entry["itemId"] for entry in resolved["moved"]]
    assert len(moved_ids) == 2
    assert len(set(moved_ids)) == 2
    assert len(app._require_orgfs_runtime().facade.listdir(space_id, "work")) == 3


def _valid_and_invalid(app: Any, space_id: str, item_id: str) -> tuple[str, str]:
    facade = app._require_orgfs_runtime().facade
    node_ids = _copies(app, space_id, item_id, invalid_last=True)
    raws = {
        node_id: facade.read_text(space_id, f"id:{node_id}")[0] for node_id in node_ids
    }
    invalid = next(n for n, raw in raws.items() if raw == "this copy is not a work item\n")
    valid = next(n for n in node_ids if n != invalid)
    return valid, invalid


def test_resolve_moves_an_unparseable_copy_as_is(work_daemon) -> None:
    """Review 1157 M1: only the kept copy has to parse."""

    app, space_id = work_daemon
    item_id = _add(app, space_id, identity={"actor": "user:allen"})["item"]["id"]
    valid, invalid = _valid_and_invalid(app, space_id, item_id)

    with pytest.raises(DaemonRequestError) as keep_invalid:
        app.handle(
            "work.resolve",
            {"spaceId": space_id, "itemId": item_id, "keep": f"id:{invalid}", "actor": "user:allen"},
        )
    assert keep_invalid.value.code == "WORK_ITEM_INVALID"

    resolved = app.handle(
        "work.resolve",
        {"spaceId": space_id, "itemId": item_id, "keep": f"id:{valid}", "actor": "user:allen"},
    )

    [moved] = resolved["moved"]
    assert moved["nodeId"] == invalid
    facade = app._require_orgfs_runtime().facade
    raw, _version = facade.read_text(space_id, f"work/{moved['itemId']}.md")
    assert raw == "this copy is not a work item\n"
    kept = app.handle("work.show", {"spaceId": space_id, "itemId": item_id, "actor": "user:allen"})
    assert kept["item"]["log"][-1].endswith(f"other copy -> {moved['itemId']} (unverified operator)")


def test_a_resolve_interrupted_before_the_move_finishes_on_retry(
    work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 1157 M2: a fault in fs.move leaves a renamed copy that the next
    resolve recognizes by its split line and only moves (no second id)."""

    app, space_id = work_daemon
    item_id = _add(app, space_id, identity={"actor": "user:allen"})["item"]["id"]
    node_ids = _copies(app, space_id, item_id)
    facade = app._require_orgfs_runtime().facade
    real_move = facade.move

    def broken_move(*_args, **_kwargs):
        raise OrgFsError("io-error", {"detail": "injected"})

    monkeypatch.setattr(facade, "move", broken_move)
    with pytest.raises(DaemonRequestError):
        app.handle(
            "work.resolve",
            {"spaceId": space_id, "itemId": item_id, "keep": f"id:{node_ids[0]}", "actor": "user:allen"},
        )
    monkeypatch.setattr(facade, "move", real_move)
    assert len(facade.resolve(space_id, f"work/{item_id}.md")) == 2

    resolved = app.handle(
        "work.resolve",
        {"spaceId": space_id, "itemId": item_id, "keep": f"id:{node_ids[0]}", "actor": "user:allen"},
    )

    [moved] = resolved["moved"]
    assert moved["nodeId"] == node_ids[1]
    assert len(facade.resolve(space_id, f"work/{item_id}.md")) == 1
    split = app.handle(
        "work.show", {"spaceId": space_id, "itemId": moved["itemId"], "actor": "user:allen"}
    )
    assert split["item"]["id"] == moved["itemId"]
    assert split["item"]["log"][-1].endswith(f"resolve split from {item_id} (conflict) (unverified operator)")
    kept = app.handle("work.show", {"spaceId": space_id, "itemId": item_id, "actor": "user:allen"})
    assert sum(f"-> {moved['itemId']}" in line for line in kept["item"]["log"]) == 1


def test_a_read_only_member_may_not_resolve(work_daemon, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review 1174 minor: the guard is rw, not any membership."""

    app, space_id = work_daemon
    item_id = _add(app, space_id, identity={"actor": "user:allen"})["item"]["id"]
    node_ids = _copies(app, space_id, item_id)
    facade = app._require_orgfs_runtime().facade
    members = facade.members
    monkeypatch.setattr(
        facade,
        "members",
        lambda space: tuple(
            replace(m, mode="ro") if m.user == "user:allen" else m for m in members(space)
        ),
    )

    with pytest.raises(DaemonRequestError) as refused:
        app.handle(
            "work.resolve",
            {"spaceId": space_id, "itemId": item_id, "keep": f"id:{node_ids[0]}", "actor": "user:allen"},
        )
    assert refused.value.code == "WORK_ITEM_MEMBER_REQUIRED"


def test_resolve_never_moves_a_copy_onto_an_existing_item(
    work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 1174: a concurrent add can take the allocated id; refuse, do not overwrite."""

    from hyprial.daemon.impl.application.messaging.work import conflicts

    app, space_id = work_daemon
    item_id = _add(app, space_id, identity={"actor": "user:allen"})["item"]["id"]
    other_id = _add(app, space_id, identity={"actor": "user:allen"})["item"]["id"]
    node_ids = _copies(app, space_id, item_id)
    facade = app._require_orgfs_runtime().facade
    other_before, _version = facade.read_text(space_id, f"work/{other_id}.md")
    monkeypatch.setattr(conflicts, "allocate_item_ids", lambda _fs, _space, owners: [other_id] * len(owners))

    with pytest.raises(DaemonRequestError) as taken:
        app.handle(
            "work.resolve",
            {"spaceId": space_id, "itemId": item_id, "keep": f"id:{node_ids[0]}", "actor": "user:allen"},
        )

    assert taken.value.code == "WORK_ITEM_ALREADY_EXISTS"
    assert facade.read_text(space_id, f"work/{other_id}.md")[0] == other_before


def test_a_retry_after_a_failed_rename_writes_one_pointer(
    work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 1174: the kept copy must not log the same "-> new id" twice."""

    app, space_id = work_daemon
    item_id = _add(app, space_id, identity={"actor": "user:allen"})["item"]["id"]
    node_ids = _copies(app, space_id, item_id)
    facade = app._require_orgfs_runtime().facade
    real_write = facade.write_text

    def failing_rename(space, path, text, **kwargs):
        if path == f"id:{node_ids[1]}":
            raise OrgFsError("io-error", {"detail": "injected"})
        return real_write(space, path, text, **kwargs)

    monkeypatch.setattr(facade, "write_text", failing_rename)
    with pytest.raises(DaemonRequestError):
        app.handle(
            "work.resolve",
            {"spaceId": space_id, "itemId": item_id, "keep": f"id:{node_ids[0]}", "actor": "user:allen"},
        )
    monkeypatch.setattr(facade, "write_text", real_write)

    resolved = app.handle(
        "work.resolve",
        {"spaceId": space_id, "itemId": item_id, "keep": f"id:{node_ids[0]}", "actor": "user:allen"},
    )

    [moved] = resolved["moved"]
    kept = app.handle("work.show", {"spaceId": space_id, "itemId": item_id, "actor": "user:allen"})
    assert sum(f"-> {moved['itemId']}" in line for line in kept["item"]["log"]) == 1
