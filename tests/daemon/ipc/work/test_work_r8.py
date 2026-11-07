"""R8 contracts for carrier identity, ledger durability, and bounded reads."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from typing import Any

import pytest

from hyprial.daemon.impl.application.actors.sessions import registry as session_registry
from hyprial.daemon.impl.application.messaging import work_items as work_commands
from hyprial.daemon.impl.application.messaging.work_ledger import (
    WorkLedgerError,
    WorkVerificationLedger,
)
from hyprial.daemon.impl.mcp.channel import adapter as channel_adapter
from hyprial.daemon.impl.mcp.channel.adapter import ClaudeChannelAdapter
from hyprial.daemon.impl.pac.views import work_items
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.kernel import DaemonRequestError, ipc_errors

from .test_work_ipc import (
    INTERACTIVE,
    OWNER,
    _add,
    _register_worker,
    _session,
    work_daemon as _work_daemon_fixture,
)


@pytest.fixture(name="r8_work_daemon")
def _r8_work_daemon(tmp_path, own_daemon_application, monkeypatch):  # type: ignore[no-untyped-def]
    return _work_daemon_fixture.__wrapped__(
        tmp_path, own_daemon_application, monkeypatch
    )


class _CaptureProxy:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def call(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"ok": True}


async def _claude_registration(
    monkeypatch: pytest.MonkeyPatch, *, parent_pid: int
) -> dict[str, Any]:
    monkeypatch.setattr(
        channel_adapter,
        "os",
        type("_Os", (), {"getppid": staticmethod(lambda: parent_pid)}),
        raising=False,
    )
    monkeypatch.setattr(
        channel_adapter,
        "process_birth_identity",
        lambda pid: f"proc-starttime:{pid}",
        raising=False,
    )
    proxy = _CaptureProxy()
    adapter = ClaudeChannelAdapter(
        proxy,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        actor=INTERACTIVE,
        session_ref="claude-process",
        cwd="/work/interactive",
        command=("claude",),
    )
    await adapter._register()
    call = next(item for item in proxy.calls if item["method"] == "session.register")
    return {
        "actor": call["actor"],
        "sessionRef": call["session_ref"],
        **call["params"],
    }


@pytest.mark.anyio
async def test_claude_carrier_registers_parent_harness_process_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    params = await _claude_registration(monkeypatch, parent_pid=200)

    assert params["processPid"] == 200
    assert params["processIdentity"] == "proc-starttime:200"


@pytest.mark.anyio
async def test_child_of_real_claude_registration_is_the_agent(
    r8_work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, space_id = r8_work_daemon
    added = _add(
        app,
        space_id,
        identity={"actor": "user:allen"},
        owner="user:allen",
    )
    params = await _claude_registration(monkeypatch, parent_pid=200)
    app._ipc_peer.pid = 250
    monkeypatch.setattr(
        session_registry,
        "_read_process_parent",
        {250: 200, 200: 1}.get,
        raising=False,
    )
    monkeypatch.setattr(
        session_registry,
        "process_birth_identity",
        lambda pid: f"proc-starttime:{pid}",
        raising=False,
    )
    app.handle("session.register", params, _trusted_message_origin="ipc")
    app._ipc_peer.pid = 300
    monkeypatch.setattr(
        work_commands, "_read_process_parent", {300: 200, 200: 1}.get
    )
    monkeypatch.setattr(
        work_commands,
        "process_birth_identity",
        lambda pid: f"proc-starttime:{pid}",
    )

    with pytest.raises(DaemonRequestError) as refused:
        app.handle(
            "work.done",
            {
                "spaceId": space_id,
                "itemId": added["item"]["id"],
                "actor": "user:allen",
            },
        )

    assert refused.value.code == "WORK_ITEM_OWNER_REQUIRED"


def test_session_register_rejects_claimed_pid_outside_ipc_peer_ancestry(
    r8_work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _space_id = r8_work_daemon
    app._ipc_peer.pid = 300
    monkeypatch.setattr(
        session_registry, "_read_process_parent", {300: 1}.get, raising=False
    )
    monkeypatch.setattr(
        session_registry,
        "process_birth_identity",
        lambda pid: f"proc-starttime:{pid}",
        raising=False,
    )

    with pytest.raises(DaemonRequestError) as refused:
        app.handle(
            "session.register",
            {
                "actor": INTERACTIVE,
                "sessionRef": "forged-process",
                "cwd": "/work/interactive",
                "command": ["claude"],
                "source": "claude-channel",
                "processPid": 200,
                "processIdentity": "proc-starttime:200",
            },
            _trusted_message_origin="ipc",
        )

    assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    assert app.desired_state.load().interactive_sessions == ()


def _carrier_registration(process_pid: int, process_identity: str, session_ref: str) -> dict[str, object]:
    return {
        "actor": INTERACTIVE,
        "sessionRef": session_ref,
        "cwd": "/work/interactive",
        "command": ["codex"],
        "source": "codex-app-server",
        "processPid": process_pid,
        "processIdentity": process_identity,
    }


def test_session_register_accepts_real_child_but_rejects_real_sibling(
    r8_work_daemon,
) -> None:
    app, _space_id = r8_work_daemon
    children = [
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        for _ in range(2)
    ]
    try:
        claimed, sibling = children
        identity = session_registry.process_birth_identity(claimed.pid)
        assert identity is not None

        app._ipc_peer.pid = os.getpid()
        accepted = app.handle(
            "session.register",
            _carrier_registration(claimed.pid, identity, "codex-child"),
            _trusted_message_origin="ipc",
        )
        assert accepted["registered"] is True

        app._ipc_peer.pid = sibling.pid
        with pytest.raises(DaemonRequestError) as refused:
            app.handle(
                "session.register",
                _carrier_registration(claimed.pid, identity, "codex-sibling"),
                _trusted_message_origin="ipc",
            )
        assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    finally:
        for child in children:
            child.terminate()
        for child in children:
            child.wait(timeout=5)


def test_session_register_rejects_claim_when_identity_mismatches(
    r8_work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _space_id = r8_work_daemon
    app._ipc_peer.pid = 300
    monkeypatch.setattr(session_registry, "process_birth_identity", lambda _pid: "actual")

    with pytest.raises(DaemonRequestError) as refused:
        app.handle(
            "session.register",
            _carrier_registration(300, "claimed", "identity-mismatch"),
            _trusted_message_origin="ipc",
        )

    assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    assert app.desired_state.load().interactive_sessions == ()


def test_session_register_rejects_unreadable_parent_chain(
    r8_work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _space_id = r8_work_daemon
    app._ipc_peer.pid = 300
    monkeypatch.setattr(session_registry, "_read_process_parent", lambda _pid: None)
    monkeypatch.setattr(
        session_registry, "process_birth_identity", lambda _pid: "claimed"
    )

    with pytest.raises(DaemonRequestError) as refused:
        app.handle(
            "session.register",
            _carrier_registration(200, "claimed", "unreadable-parent"),
            _trusted_message_origin="ipc",
        )

    assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    assert app.desired_state.load().interactive_sessions == ()


def test_session_register_invalid_peer_discards_process_claim(
    r8_work_daemon,
) -> None:
    app, _space_id = r8_work_daemon
    app._ipc_peer.pid = None

    app.handle(
        "session.register",
        _carrier_registration(200, "claimed", "invalid-peer"),
        _trusted_message_origin="ipc",
    )

    (session,) = app.desired_state.load().interactive_sessions
    assert session.process_pid is None
    assert session.process_identity is None


def test_lax_note_log_marks_the_unverified_operator(
    r8_work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, space_id = r8_work_daemon
    added = _add(app, space_id, identity={"actor": "user:allen"})
    app._ipc_peer.pid = 300
    monkeypatch.setattr(work_commands, "_read_process_parent", lambda _pid: None)

    noted = app.handle(
        "work.note",
        {
            "spaceId": space_id,
            "itemId": added["item"]["id"],
            "text": "ancestry unavailable",
            "actor": "user:allen",
        },
    )

    assert noted["item"]["log"][-1].endswith(
        "user:allen: ancestry unavailable (unverified operator)"
    )


def test_corrupt_ledger_does_not_turn_landed_done_into_an_error(
    r8_work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, space_id = r8_work_daemon
    _register_worker(app, OWNER, "owner-session")
    added = _add(
        app,
        space_id,
        identity=_session(OWNER, "owner-session"),
        owner=OWNER,
    )
    ledger = app.state_dir / "work-verification-ledger.json"
    ledger.write_text("{corrupt", encoding="utf-8")
    events: list[str] = []
    monkeypatch.setattr(
        app,
        "_log",
        lambda _level, _component, event, **_fields: events.append(event),
    )

    completed = app.handle(
        "work.done",
        {
            "spaceId": space_id,
            "itemId": added["item"]["id"],
            **_session(OWNER, "owner-session"),
        },
    )

    assert completed["item"]["status"] == "done"
    assert completed["item"]["ownerActions"][-1]["verification"] == (
        "unverified (remote)"
    )
    assert "daemon.work.ledger_corrupt" in events


def test_ledger_append_error_does_not_turn_landed_done_into_an_error(
    r8_work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, space_id = r8_work_daemon
    _register_worker(app, OWNER, "owner-session")
    added = _add(
        app,
        space_id,
        identity=_session(OWNER, "owner-session"),
        owner=OWNER,
    )
    events: list[str] = []

    def fail_append(_self, _row) -> None:  # type: ignore[no-untyped-def]
        raise WorkLedgerError("injected append failure")

    monkeypatch.setattr(WorkVerificationLedger, "append", fail_append)
    monkeypatch.setattr(
        app,
        "_log",
        lambda _level, _component, event, **_fields: events.append(event),
    )

    completed = app.handle(
        "work.done",
        {
            "spaceId": space_id,
            "itemId": added["item"]["id"],
            **_session(OWNER, "owner-session"),
        },
    )

    assert completed["item"]["status"] == "done"
    assert completed["item"]["ownerActions"][-1]["verification"] == (
        "unverified (remote)"
    )
    assert "daemon.work.ledger_corrupt" in events


def test_ledger_appends_without_rewriting_the_existing_file(tmp_path) -> None:
    ledger = WorkVerificationLedger(tmp_path / "work-verification-ledger.json")
    first = {"itemId": "M-allen-0001", "action": "acceptance"}
    second = {"itemId": "M-allen-0001", "action": "done"}

    ledger.append(first)
    original = ledger.path.read_bytes()
    ledger.append(second)

    assert ledger.path.read_bytes() == original
    assert ledger.rows() == (first, second)


def test_ledger_rejects_a_partial_journal_record(tmp_path) -> None:
    ledger = WorkVerificationLedger(tmp_path / "work-verification-ledger.json")
    ledger.append({"itemId": "M-allen-0001", "action": "acceptance"})
    ledger.journal_path.write_bytes(b'{"version":1,"row":{}')

    with pytest.raises(WorkLedgerError, match="incomplete record"):
        ledger.rows()


def test_repeated_ls_reuses_validation_for_the_same_item_head(
    r8_work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, space_id = r8_work_daemon
    added = _add(app, space_id, identity={"actor": "user:allen"})
    item_id = added["item"]["id"]
    version = added["version"]
    facade = app._require_orgfs_runtime().facade
    for assignee in ("user:first", "user:second", "user:third"):
        current = work_items.parse_work_item(
            facade.read_text(space_id, f"work/{item_id}.md")[0]
        )
        written = app._handle_orgfs(
            "orgfs.write",
            {
                "spaceId": space_id,
                "path": f"work/{item_id}.md",
                "text": work_items.serialize_work_item(
                    work_items.replace_head(current, assignee=assignee)
                ),
                "expectVersion": version,
            },
        )
        version = str(written["version"])
    calls = 0
    facade_type = type(facade)
    original = facade_type.journal_history

    def counted(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(facade_type, "journal_history", counted)
    app.handle("work.ls", {"spaceId": space_id, "actor": "user:allen"})
    first_calls = calls
    app.handle("work.ls", {"spaceId": space_id, "actor": "user:allen"})

    assert first_calls >= 1
    assert calls == first_calls


def test_history_cache_key_includes_physical_document_id(
    r8_work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, space_id = r8_work_daemon
    added = _add(app, space_id, identity={"actor": "user:allen"})
    item_id = added["item"]["id"]
    facade = app._require_orgfs_runtime().facade
    snapshot = facade.read_text_snapshot(space_id, f"work/{item_id}.md")
    app._work_inspect_history(space_id, item_id)
    replacement = replace(
        snapshot,
        node=replace(snapshot.node, doc_id=f"{snapshot.node.doc_id}-replacement"),
    )
    facade_type = type(facade)
    monkeypatch.setattr(
        facade_type,
        "read_text_snapshot",
        lambda _self, _space_id, _path: replacement,
    )

    class CacheMissReached(RuntimeError):
        pass

    def cache_miss(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise CacheMissReached

    monkeypatch.setattr(facade_type, "journal_history", cache_miss)

    with pytest.raises(CacheMissReached):
        app._work_inspect_history(space_id, item_id)


def test_non_purge_history_read_error_is_not_raised_as_a_request_failure(
    r8_work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, space_id = r8_work_daemon
    added = _add(app, space_id, identity={"actor": "user:allen"})
    item_id = added["item"]["id"]
    app.handle(
        "work.note",
        {
            "spaceId": space_id,
            "itemId": item_id,
            "text": "second version",
            "actor": "user:allen",
        },
    )
    app._work_history_cache.clear()
    facade = app._require_orgfs_runtime().facade

    def fail_read_at(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OrgFsError("unavailable", {"message": "injected read failure"})

    monkeypatch.setattr(type(facade), "journal_history", fail_read_at)

    inspection = app._work_inspect_history(space_id, item_id)

    assert inspection.error is not None
    assert str(inspection.error).startswith("history_unavailable:journal-unavailable")
    assert inspection.error.data["historyIncomplete"] is True


def test_forgetting_facade_history_does_not_launder_a_rewrite(
    r8_work_daemon,
) -> None:
    app, space_id = r8_work_daemon
    added = _add(
        app,
        space_id,
        identity={"actor": "user:allen"},
        checklist=["ship"],
    )
    item_id = added["item"]["id"]
    checked = app.handle(
        "work.check",
        {
            "spaceId": space_id,
            "itemId": item_id,
            "number": 1,
            "actor": "user:allen",
        },
    )
    facade = app._require_orgfs_runtime().facade
    facade._forget_history(facade._spaces[space_id])
    current = work_items.parse_work_item(
        facade.read_text(space_id, f"work/{item_id}.md")[0]
    )
    laundered = work_items.replace_head(
        current, checklist=[{"text": "ship", "done": False}]
    )
    app._handle_orgfs(
        "orgfs.write",
        {
            "spaceId": space_id,
            "path": f"work/{item_id}.md",
            "text": work_items.serialize_work_item(laundered),
            "expectVersion": checked["version"],
        },
    )

    shown = app.handle(
        "work.show",
        {"spaceId": space_id, "itemId": item_id, "actor": "user:allen"},
    )

    assert shown["item"]["invalid"] is True
    assert "rewritten" in shown["item"]["reason"]


def test_parser_strips_verified_like_keys_without_projection() -> None:
    item = work_items.new_work_item(
        item_id="M-allen-0001",
        title="Parse boundary",
        owner=OWNER,
        assignee=None,
        checklist=[],
        depends=[],
        acceptance="",
        opened=1,
    )
    forged = work_items.serialize_work_item(
        work_items.replace_head(
            item,
            status="done",
            closed=2,
            ownerActions=[{"action": "done", "actor": OWNER}],
        )
    ).replace(
        f"  actor: {OWNER}\n",
        f"  actor: {OWNER}\n  verified: true\n  verificationHint: forged\n",
    )
    forged = forged.replace("status: done\n", "status: done\nverified: true\n")

    parsed = work_items.parse_work_item(forged)

    assert "verified" not in parsed.head
    assert parsed.head["ownerActions"] == [{"action": "done", "actor": OWNER}]


def test_projection_uses_only_the_ledger_even_if_parser_passes_forged_keys(
    r8_work_daemon,
) -> None:
    app, space_id = r8_work_daemon
    item = work_items.new_work_item(
        item_id="M-allen-0001",
        title="Projection boundary",
        owner=OWNER,
        assignee=None,
        checklist=[],
        depends=[],
        acceptance="",
        opened=1,
    )
    head = dict(item.head)
    head.update(
        {
            "status": "done",
            "closed": 2,
            "ownerActions": [
                {
                    "action": "done",
                    "actor": OWNER,
                    "verified": True,
                    "verification": "verified",
                }
            ],
        }
    )
    forged = work_items.WorkItem(
        head=head,
        prefix=item.prefix,
        suffix=item.suffix,
        title=item.title,
        log=item.log,
    )

    projected = app._work_result(
        forged, "head-version", space_id, ("forged-action-version",)
    )["item"]

    assert projected["ownerActions"] == [
        {
            "action": "done",
            "actor": OWNER,
            "verified": False,
            "verification": "unverified (remote)",
            "lax": "signed-updates",
        }
    ]


def test_projection_qualifies_verified_and_legacy_owner_actions() -> None:
    explicit = work_items.new_work_item(
        item_id="M-allen-0001",
        title="Qualified verification",
        owner=OWNER,
        assignee=None,
        checklist=[],
        depends=[],
        acceptance="",
        opened=1,
    )
    explicit = work_items.replace_head(
        explicit,
        status="done",
        closed=2,
        ownerActions=[{"action": "done", "actor": OWNER}],
    )
    verified = work_items.project_owner_actions(explicit, ["verified"])
    assert verified[0]["verified"] is True
    assert verified[0]["verification"] == "verified (LAX same-uid)"

    legacy = work_items.replace_head(explicit, ownerActions=[])
    projected_legacy = work_items.project_owner_actions(legacy, [])
    assert projected_legacy[0]["verified"] is False
    assert projected_legacy[0]["verification"] == "unverified (legacy)"
