# Decisions (phase D, package S, 2026-11-25):
# - Real-node verification (V report §7, defects 3 and 6) found two answers of
#   the state runtime that the request registry rewrote into ``DAEMON_ERROR``:
#   every ``orgfs.status`` (its result did not match the journal-era schema)
#   and every request on a space the startup scan refused for its layout
#   (``ORGFS_LAYOUT_UNSUPPORTED`` was not declared).  These tests go through
#   ``METHOD_REGISTRY.invoke``, the path that validates results and declared
#   errors, on a real ``DaemonApplication`` with an isolated home.
# - The refused space is a directory holding only ``journal.sqlite3``, the
#   marker the startup scan classifies as the legacy layout.
# - The methods listed in ``LAYOUT_REFUSED`` are every ``orgfs.*`` method that
#   looks a space up by ``spaceId`` and answers with the refusal; each must
#   carry the declared data (``spaceId``, ``reason``, ``guide``).
# Decisions (phase D, package D4b, 2026-11-25): ``orgfs.restore`` takes no
# ``version``; ``orgfs.checkout`` joins the table: it passes the refusal and
# its data through instead of wrapping it as ``checkout-io`` with empty data,
# and creates no checkout directory in the refused space.
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.shell.impl.daemon_wiring import daemon_dependencies

from tests.support.runtime._owned_applications import owned_applications as owned_applications

STATUS_FIELDS = {"space_id", "synced", "holders_online", "content_pending", "rejected_updates"}


def _application(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DaemonApplication:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "harness-state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    return DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node-a",
        hyprial_home=tmp_path / "home",
        owner="owner@example.com",
        **daemon_dependencies(),
    )


def test_status_answers_the_state_fields_through_the_registry(tmp_path, monkeypatch) -> None:
    app = _application(tmp_path, monkeypatch)
    created = METHOD_REGISTRY.invoke(app, "orgfs.create", {"name": "team"})
    space_id = created["space_id"]
    METHOD_REGISTRY.invoke(app, "orgfs.write", {"spaceId": space_id, "path": "a.md", "text": "x"})

    status = METHOD_REGISTRY.invoke(app, "orgfs.status", {"spaceId": space_id})

    assert set(status) == STATUS_FIELDS
    assert status["space_id"] == space_id
    assert status["holders_online"] == []
    assert status["rejected_updates"] == 0


LAYOUT_REFUSED = {
    "orgfs.resolve": {"path": "a.md"},
    "orgfs.ls": {"path": ""},
    "orgfs.stat": {"path": "a.md"},
    "orgfs.read": {"path": "a.md"},
    "orgfs.status": {},
    "orgfs.trash": {},
    "orgfs.watch": {},
    "orgfs.members": {},
    "orgfs.write": {"path": "a.md", "text": "x"},
    "orgfs.mkdir": {"path": "d"},
    "orgfs.move": {"source": "a.md", "destination": "b.md"},
    "orgfs.restore": {"node": "a.md"},
    "orgfs.remove": {"node": "a.md"},
    "orgfs.invite": {"user": "user:bob", "mode": "rw"},
    "orgfs.remove_member": {"user": "user:bob"},
    "orgfs.checkout": {"enabled": True},
}


@pytest.fixture
def refused_space(tmp_path, monkeypatch):
    space_id = str(uuid.uuid4())
    legacy = tmp_path / "state" / "orgfs" / "spaces" / space_id
    legacy.mkdir(parents=True)
    (legacy / "journal.sqlite3").write_bytes(b"")
    app = _application(tmp_path, monkeypatch)
    return app, space_id, legacy


@pytest.mark.parametrize("method", sorted(LAYOUT_REFUSED))
def test_a_refused_space_answers_the_declared_layout_error(refused_space, method: str) -> None:
    app, space_id, legacy = refused_space

    with pytest.raises(DaemonRequestError) as refused:
        METHOD_REGISTRY.invoke(app, method, {"spaceId": space_id, **LAYOUT_REFUSED[method]})

    assert refused.value.code == ipc_errors.ORGFS_LAYOUT_UNSUPPORTED, (method, str(refused.value))
    assert refused.value.data["spaceId"] == space_id
    assert refused.value.data["reason"] == "legacy-journal"
    assert refused.value.data["guide"]
    assert sorted(path.name for path in legacy.iterdir()) == ["journal.sqlite3"]
