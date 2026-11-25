"""Organization resolution selects a space for ordinary OrgFS document calls."""

from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.org.network import ipc
from tests.daemon.org_membership.context.support import org_daemon as _org_daemon  # noqa: F401


@pytest.fixture
def organizations(org_daemon, monkeypatch):
    app, first = org_daemon
    second = app.handle("orgfs.create", {"name": "second"})["space_id"]
    rows = [{"org": name, "spaceId": space, "state": "active", "role": "owner", "devices": 0}
            for name, space in (("group-a", first), ("group-b", second))]
    monkeypatch.setattr(ipc, "_request_service", lambda app: SimpleNamespace(
        list=lambda: {"orgs": rows, "leftOrgs": []}
    ))
    return app, first, second, rows


def test_default_override_and_legacy_space_resolve_for_normal_documents(organizations):
    app, first, second, rows = organizations
    app.handle("org.use", {"org": "group-b"})
    selected = app.handle("org.resolve", {})
    assert selected["spaceId"] == second
    app.handle("orgfs.write", {"spaceId": selected["spaceId"], "path": "notes.txt", "text": "B"})
    selected = app.handle("org.resolve", {"org": "group-a"})
    assert selected["spaceId"] == first
    app.handle("orgfs.write", {"spaceId": selected["spaceId"], "path": "work/notes.txt", "text": "A"})
    rows[0]["state"] = "sync-problem"
    legacy = app.handle("org.resolve", {"spaceId": first})
    assert legacy["spaceId"] == first
    assert any("observe" in notice.lower() for notice in legacy["notices"])
    assert app.handle("orgfs.read", {"spaceId": first, "path": "work/notes.txt"})["text"] == "A"
    assert app.handle("orgfs.read", {"spaceId": second, "path": "notes.txt"})["text"] == "B"


def test_stale_current_selection_reports_notice_before_document_access(organizations):
    app, first, second, rows = organizations
    app.handle("org.use", {"org": "group-b"})
    rows.pop()
    result = app.handle("org.resolve", {})
    assert result["spaceId"] == first
    assert result["notices"]
