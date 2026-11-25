"""An isolated daemon fixture for ordinary organization and document requests."""

from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.shell.impl.daemon_wiring import daemon_dependencies
from tests.support.runtime.environment import UNIT_USER_ID


@pytest.fixture(name="org_daemon")
def org_daemon(tmp_path, own_daemon_application):
    app = own_daemon_application(DaemonApplication(
        state_dir=tmp_path / "state", socket_path=tmp_path / "daemon.sock",
        hyprial_home=tmp_path / "home", node_id="node-1", owner="allen", user_id=UNIT_USER_ID,
        **daemon_dependencies(),
    ))
    app._inbox = SimpleNamespace(pending_messages=lambda actor: ())
    app._harnesses = SimpleNamespace(
        projected_worker_session_refs=lambda: {},
        projected_worker_process_identities=lambda: {},
        status=lambda: (),
    )
    app._presence = SimpleNamespace(online_actors=lambda: ())
    app._restore_done.set()
    space = app.handle("orgfs.create", {"name": "group-work"})["space_id"]
    OrgFsDirectoryStore(app._require_orgfs_runtime().facade).put_org_meta(
        "group-work", {"state": "active"}
    )
    app.handle("orgfs.mkdir", {"spaceId": space, "path": "work"})
    return app, space
