"""R2h withdrawal is an OrgFS deletion visible on another replica."""

from __future__ import annotations

import json
from pathlib import Path

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from tests.daemon.orgfs.mesh.device_rows.test_round4 import (
    OWNER,
    _eventually,
    _equal,
    replicas,
)

from .test_withdraw_publish import _row, _write_settings


_IMPORTED_REPLICAS_FIXTURE = replicas


def test_withdraw_removes_binding_row_from_second_replica(replicas, tmp_path: Path) -> None:
    _fixture_root, space_id, owner, alice, _bob, _carol = replicas
    row = _row()
    store = OrgFsDirectoryStore(owner.facade)
    store.put_binding("group-acme", row)
    owner.broadcast_pending(space_id)
    _eventually(
        lambda: _equal(
            OrgFsDirectoryStore(alice.facade).list_binding_rows("group-acme"),
            [("owner-example", row)],
        )
    )

    home = tmp_path / "withdraw-home"
    _write_settings(home, row)
    app = object.__new__(DaemonApplication)
    app.state_dir = tmp_path / "withdraw-state"
    app.hyprial_home = home
    app.identity_mode = "casdoor"
    app.owner = OWNER
    app._org_identity_binding_projector = None
    app._org_binding_directory = lambda: store  # type: ignore[method-assign]
    app._log = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    app._withdraw_identity_binding(org="group-acme", all_orgs=False)
    owner.broadcast_pending(space_id)

    _eventually(
        lambda: _equal(
            OrgFsDirectoryStore(alice.facade).list_binding_rows("group-acme"), []
        )
    )
    state = json.loads(
        (app.state_dir / "identity-binding-suppression.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["orgs"] == ["group-acme"]
