"""R7 contracts for local work verification and deterministic repair."""

from __future__ import annotations

import json
import stat

import pytest

from hyprial.daemon.impl.application.messaging import work_items as work_commands
from hyprial.daemon.impl.pac.views import work_items
from hyprial.kernel import DaemonRequestError

from .test_work_ipc import (
    INTERACTIVE,
    OTHER,
    OWNER,
    _add,
    _register_worker,
    _session,
    work_daemon as _work_daemon_fixture,
)


@pytest.fixture(name="r7_work_daemon")
def _r7_work_daemon(tmp_path, own_daemon_application, monkeypatch):  # type: ignore[no-untyped-def]
    return _work_daemon_fixture.__wrapped__(
        tmp_path, own_daemon_application, monkeypatch
    )


def _raw_write(app, space_id: str, item_id: str, item, version: str) -> str:  # type: ignore[no-untyped-def]
    written = app._handle_orgfs(
        "orgfs.write",
        {
            "spaceId": space_id,
            "path": f"work/{item_id}.md",
            "text": work_items.serialize_work_item(item),
            "expectVersion": version,
        },
    )
    return str(written["version"])


def _parsed(app, space_id: str, item_id: str):  # type: ignore[no-untyped-def]
    text, _version = app._require_orgfs_runtime().facade.read_text(
        space_id, f"work/{item_id}.md"
    )
    return work_items.parse_work_item(text)


def test_raw_verified_field_is_ignored_but_guarded_done_is_verified(
    r7_work_daemon,
) -> None:
    app, space_id = r7_work_daemon
    _register_worker(app, OWNER, "owner-session")
    added = _add(
        app,
        space_id,
        identity=_session(OWNER, "owner-session"),
        owner=OWNER,
    )
    item_id = added["item"]["id"]
    current = _parsed(app, space_id, item_id)
    forged = work_items.replace_head(
        current,
        status="done",
        closed=123,
        ownerActions=[{"action": "done", "actor": OWNER, "verified": True}],
    )
    forged_text = work_items.serialize_work_item(forged).replace(
        f"  actor: {OWNER}\n", f"  actor: {OWNER}\n  verified: true\n"
    )
    assert "verified: true" in forged_text
    app._handle_orgfs(
        "orgfs.write",
        {
            "spaceId": space_id,
            "path": f"work/{item_id}.md",
            "text": forged_text,
            "expectVersion": added["version"],
        },
    )

    shown = app.handle(
        "work.show",
        {"spaceId": space_id, "itemId": item_id, **_session(OWNER, "owner-session")},
    )
    assert shown["item"].get("invalid") is not True
    assert shown["item"]["ownerActions"][-1]["verified"] is False
    assert shown["item"]["ownerActions"][-1]["verification"] == "unverified (remote)"
    assert shown["item"]["ownerActions"][-1]["lax"] == "signed-updates"

    second = _add(
        app,
        space_id,
        identity=_session(OWNER, "owner-session"),
        owner=OWNER,
    )
    guarded = app.handle(
        "work.done",
        {
            "spaceId": space_id,
            "itemId": second["item"]["id"],
            **_session(OWNER, "owner-session"),
        },
    )
    assert guarded["item"]["ownerActions"][-1]["verified"] is True
    assert guarded["item"]["ownerActions"][-1]["verification"] == (
        "verified (LAX same-uid)"
    )

    ledger = app.state_dir / "work-verification-ledger.json"
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o600
    rows = json.loads(ledger.read_text(encoding="utf-8"))["rows"]
    assert any(
        row["itemId"] == second["item"]["id"]
        and row["action"] == "done"
        and row["caller"] == OWNER
        and row["producedVersion"] == guarded["version"]
        for row in rows
    )


def test_guarded_done_without_the_local_ledger_is_unverified_remote(
    r7_work_daemon,
) -> None:
    app, space_id = r7_work_daemon
    _register_worker(app, OWNER, "owner-session")
    added = _add(
        app,
        space_id,
        identity=_session(OWNER, "owner-session"),
        owner=OWNER,
    )
    item_id = added["item"]["id"]
    app.handle(
        "work.done",
        {"spaceId": space_id, "itemId": item_id, **_session(OWNER, "owner-session")},
    )
    (app.state_dir / "work-verification-ledger.json").unlink()

    shown = app.handle(
        "work.show",
        {"spaceId": space_id, "itemId": item_id, **_session(OWNER, "owner-session")},
    )
    action = shown["item"]["ownerActions"][-1]
    assert action["verified"] is False
    assert action["verification"] == "unverified (remote)"
    assert action["lax"] == "signed-updates"


def test_pre_w1_done_without_an_owner_action_is_valid_but_unverified(
    r7_work_daemon,
) -> None:
    app, space_id = r7_work_daemon
    body = """## M-allen-0001 Legacy done

```yaml
id: M-allen-0001
status: done
owner: user:allen
checklist: []
opened: '2026-09-29T10:00:00Z'
closed: '2026-09-29T11:00:00Z'
```
"""
    app._handle_orgfs(
        "orgfs.write",
        {"spaceId": space_id, "path": "work/M-allen-0001.md", "text": body},
    )

    shown = app.handle(
        "work.show", {"spaceId": space_id, "itemId": "M-allen-0001", "actor": "user:allen"}
    )
    assert "invalid" not in shown["item"]
    assert shown["item"]["unverifiedOperator"] is True
    assert shown["item"]["ownerActions"] == [
        {
                "action": "done",
                "actor": "user:allen",
                "verified": False,
                "verification": "unverified (legacy)",
            }
        ]


def test_repair_restores_latest_valid_version_and_records_proof(r7_work_daemon) -> None:
    app, space_id = r7_work_daemon
    _register_worker(app, OWNER, "owner-session")
    added = _add(
        app,
        space_id,
        identity=_session(OWNER, "owner-session"),
        owner=OWNER,
        checklist=["ship"],
    )
    item_id = added["item"]["id"]
    checked = app.handle(
        "work.check",
        {
            "spaceId": space_id,
            "itemId": item_id,
            "number": 1,
            **_session(OWNER, "owner-session"),
        },
    )
    invalid = work_items.replace_head(
        _parsed(app, space_id, item_id),
        checklist=[{"text": "ship", "done": False}],
    )
    invalid_version = _raw_write(
        app, space_id, item_id, invalid, checked["version"]
    )

    repaired = app.handle(
        "work.repair",
        {"spaceId": space_id, "itemId": item_id, **_session(OWNER, "owner-session")},
    )

    assert repaired["version"] not in {invalid_version, checked["version"]}
    assert repaired["item"]["checklist"] == [{"text": "ship", "done": True}]
    assert repaired["item"]["log"][-1].endswith(
        f"{OWNER}: repair {invalid_version} -> {checked['version']}"
    )
    assert repaired["item"]["ownerActions"][-1]["verification"] == (
        "verified (LAX same-uid)"
    )
    rows = json.loads(
        (app.state_dir / "work-verification-ledger.json").read_text(encoding="utf-8")
    )["rows"]
    assert {
        "action": "repair",
        "itemId": item_id,
        "caller": OWNER,
        "invalidVersion": invalid_version,
        "restoredVersion": checked["version"],
        "producedVersion": repaired["version"],
    }.items() <= rows[-1].items()

    shown = app.handle(
        "work.show",
        {"spaceId": space_id, "itemId": item_id, **_session(OWNER, "owner-session")},
    )
    assert "invalid" not in shown["item"]


def test_repair_refuses_a_non_owner_agent(r7_work_daemon) -> None:
    app, space_id = r7_work_daemon
    _register_worker(app, OWNER, "owner-session")
    _register_worker(app, OTHER, "other-session")
    added = _add(
        app,
        space_id,
        identity=_session(OWNER, "owner-session"),
        owner=OWNER,
        checklist=["ship"],
    )
    invalid = work_items.replace_head(
        _parsed(app, space_id, added["item"]["id"]), status="done", closed=123
    )
    _raw_write(app, space_id, added["item"]["id"], invalid, added["version"])

    with pytest.raises(DaemonRequestError) as refused:
        app.handle(
            "work.repair",
            {
                "spaceId": space_id,
                "itemId": added["item"]["id"],
                **_session(OTHER, "other-session"),
            },
        )
    assert refused.value.code == "WORK_ITEM_OWNER_REQUIRED"


def test_unverified_operator_can_repair_and_is_flagged(r7_work_daemon) -> None:
    app, space_id = r7_work_daemon
    added = _add(
        app,
        space_id,
        identity={"actor": "user:allen"},
        owner="user:allen",
        checklist=["ship"],
    )
    invalid = work_items.replace_head(
        _parsed(app, space_id, added["item"]["id"]), status="done", closed=123
    )
    invalid_version = _raw_write(
        app, space_id, added["item"]["id"], invalid, added["version"]
    )

    repaired = app.handle(
        "work.repair",
        {"spaceId": space_id, "itemId": added["item"]["id"], "actor": "user:allen"},
    )
    assert repaired["item"]["unverifiedOperator"] is True
    assert repaired["item"]["ownerActions"][-1]["verification"] == "unverified"
    assert repaired["item"]["log"][-1].endswith(
        f"user:allen: repair {invalid_version} -> {added['version']} "
        "(unverified operator)"
    )


def test_repair_refuses_when_no_version_ever_validated(r7_work_daemon) -> None:
    app, space_id = r7_work_daemon
    body = """## M-allen-0001 Never valid

```yaml
id: M-allen-0001
status: done
owner: user:allen
checklist:
  - {text: ship, done: false}
```
"""
    app._handle_orgfs(
        "orgfs.write",
        {"spaceId": space_id, "path": "work/M-allen-0001.md", "text": body},
    )

    with pytest.raises(DaemonRequestError) as missing:
        app.handle(
            "work.repair",
            {"spaceId": space_id, "itemId": "M-allen-0001", "actor": "user:allen"},
        )
    assert missing.value.code == "WORK_ITEM_NO_VALID_VERSION"
    assert "no validating version" in str(missing.value).lower()


def test_claude_registered_process_child_is_the_agent(
    r7_work_daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _space_id = r7_work_daemon
    app.handle(
        "session.register",
        {
            "actor": INTERACTIVE,
            "sessionRef": "claude-process",
            "cwd": "/work/interactive",
            "command": ["claude"],
            "source": "claude-channel",
            "runtime": "claude_interactive",
            "channelConfirmed": True,
            "channelProtocolVersion": 2,
            "channelLeaseToken": "work-item-channel-lease-token-0001",
            "processPid": 200,
            "processIdentity": "birth:200",
        },
    )
    app._ipc_peer.pid = 300
    monkeypatch.setattr(work_commands, "_read_process_parent", {300: 200, 200: 1}.get)

    caller, unverified = app._work_caller({"actor": "user:allen"})

    assert caller == INTERACTIVE
    assert unverified is False
