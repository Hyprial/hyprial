"""Documents under work/ use the same reads and version checks as other paths."""

import pytest

from hyprial.kernel import DaemonRequestError
from tests.daemon.org_membership.context.support import org_daemon as _org_daemon  # noqa: F401


@pytest.mark.parametrize("path", ["work/notes.txt", "notes.txt"])
def test_arbitrary_current_content_uses_normal_orgfs_read_write_and_cas(org_daemon, path):
    app, space = org_daemon
    content = "# Ordinary document\n\n```yaml\nownerActions: [not-an-approval]\n```\n"
    first = app.handle("orgfs.write", {"spaceId": space, "path": path, "text": content})
    read = app.handle("orgfs.read", {"spaceId": space, "path": path})
    assert read["text"] == content
    assert read["version"] == first["version"]
    changed = app.handle("orgfs.write", {
        "spaceId": space, "path": path, "text": "replacement without work metadata",
        "expectVersion": first["version"],
    })
    with pytest.raises(DaemonRequestError) as stale:
        app.handle("orgfs.write", {
            "spaceId": space, "path": path, "text": "must not replace the new head",
            "expectVersion": first["version"],
        })
    assert stale.value.code == "stale-write"
    final = app.handle("orgfs.read", {"spaceId": space, "path": path})
    assert final["text"] == "replacement without work metadata"
    assert final["version"] == changed["version"]
    assert not (app.state_dir / "work-verification-ledger.json").exists()
