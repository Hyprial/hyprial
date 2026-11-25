"""Current selection survives service reconstruction, not membership loss."""

import json

import pytest

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.org.network.state import add_left_org
from hyprial.kernel import DaemonRequestError
from tests.daemon.org_membership.context.support import org_daemon as _org_daemon  # noqa: F401


@pytest.mark.parametrize("loss", ["left", "deleted", "partial"])
def test_real_directory_current_invalidation(org_daemon, loss):
    app, space = org_daemon
    fs = app._require_orgfs_runtime().facade
    store = OrgFsDirectoryStore(fs)
    fs.create_space("group-other")
    store.put_org_meta("group-other", {"state": "active"})
    before = fs.read_text(space, "directory/org.json")
    app.handle("org.use", {"org": "group-work"})
    assert app.handle("org.resolve", {})["spaceId"] == space
    assert fs.read_text(space, "directory/org.json") == before
    if loss == "left":
        add_left_org(app.hyprial_home, "group-work")
    else:
        store.put_org_meta("group-work", {"state": loss})
    resolved = app.handle("org.resolve", {})
    assert resolved["org"] == "group-other"
    assert resolved["notices"]
    saved = json.loads((app.hyprial_home / "state/org/current.json").read_text())
    assert saved == {"current": None}
    assert all(not row["current"] for row in app.handle("org.list", {})["orgs"])


def test_corrupt_current_fails_closed_before_resolving_document_space(org_daemon):
    app, space = org_daemon
    app.handle("org.use", {"org": "group-work"})
    path = app.hyprial_home / "state/org/current.json"
    path.write_text("{")
    with pytest.raises(DaemonRequestError) as error:
        app.handle("org.resolve", {})
    assert error.value.code == "DAEMON_NOT_READY"
    assert not app._require_orgfs_runtime().facade.listdir(space, "work")


def test_documents_on_space_outside_active_orgs_remain_readable(org_daemon):
    app, _space = org_daemon
    plain = app._handle_orgfs("orgfs.create", {"name": "plain-space"})["space_id"]
    app._handle_orgfs("orgfs.mkdir", {"spaceId": plain, "path": "work"})
    selected = app.handle("org.resolve", {"spaceId": plain})
    assert any("observe" in notice.lower() for notice in selected["notices"])
    result = app.handle("orgfs.ls", {"spaceId": selected["spaceId"], "path": "work"})
    assert result["nodes"] == []


def test_org_list_does_not_repair_stale_selection(tmp_path):
    from hyprial.daemon.impl.org.membership.context.resolver import OrgContext
    from hyprial.daemon.impl.org.membership.context.settings import write_current

    write_current(tmp_path, "group-stale")
    path = tmp_path / "state/org/current.json"
    before = path.read_bytes()
    events = []
    context = OrgContext(
        tmp_path, {"orgs": [{
            "org": "group-live", "spaceId": "space-live", "role": "owner",
            "state": "active", "devices": 1, "authorityDevice": False,
        }], "leftOrgs": []}, lambda *args, **kw: events.append((args, kw)),
        repair_current=False,
    )
    result = context.listing()
    assert path.read_bytes() == before
    assert events == []
    assert result["notices"]
    assert result["orgs"][0]["current"] is False
    assert result["orgs"][0]["authorityDevice"] is False
