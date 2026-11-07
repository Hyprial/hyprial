"""Three-pane org network status projections."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.identity.impl.org_directory import DirectoryDevice, directory_owner_principal
from tests.daemon.org.network.test_org_network_service import (
    FakeFs,
    NODE_ID,
    _service,
    home as home,
)


def test_network_shows_three_panes_with_sources_and_masks_addresses(
    home: Path,
) -> None:
    fs = FakeFs()
    service = _service(fs, home)
    service.create("group-acme")
    status = {
        "policy": "auto",
        "endpoints": ["tcp/127.0.0.1:41001"],
        "dialed": [],
        "restartRequired": True,
        "lastPoll": {"ok": True},
        "connectionTelemetry": {"ringCapacity": 16, "slots": []},
        "derpMapURL": "https://derpmap.example/map.json",
        "derpMapSource": "configured",
        "peers": [{"peer": "node-b", "endpoint": "tcp/127.0.0.1:41001", "dialed": False}],
    }
    view = _service(fs, home, forwarding_status=lambda: status).network()
    assert view["orgs"] == ["group-acme"]
    own = [d for d in view["directory"]["group-acme"] if d["deviceId"] == NODE_ID]
    assert own and "address" not in own[0] and own[0]["addressPresent"] is True
    assert view["mappedPeers"] == status["peers"]
    assert view["sidecar"]["restartRequired"] is True
    assert view["sidecar"]["connectionTelemetry"] == status["connectionTelemetry"]
    assert view["sidecar"]["derpMapURL"] == status["derpMapURL"]
    assert view["sidecar"]["derpMapSource"] == status["derpMapSource"]
    assert view["sources"] == {
        "directory": "orgfs",
        "mappedPeers": "forwarding-sidecar",
        "sidecar": "forwarding-sidecar",
    }
    # A single-org view answers with exactly that org.
    assert _service(fs, home).network("group-acme")["orgs"] == ["group-acme"]


def test_network_returns_directory_when_sidecar_status_hangs(home: Path) -> None:
    fs = FakeFs()
    service = _service(fs, home)
    service.create("group-acme")

    def hung_status() -> dict[str, Any]:
        time.sleep(2.0)
        return {"peers": []}

    started = time.monotonic()
    view = _service(fs, home, forwarding_status=hung_status).network()

    assert time.monotonic() - started < 1.5
    assert view["directory"]["group-acme"]
    assert view["paneStatus"]["directory"] == {"available": True}
    unavailable = {"available": False, "reason": "timeout"}
    assert view["paneStatus"]["mappedPeers"] == unavailable
    assert view["paneStatus"]["sidecar"] == unavailable


def test_network_reports_directory_rows_that_have_not_arrived(home: Path) -> None:
    """A row still on its way is "not caught up", not a device that does not exist."""

    fs = FakeFs()
    service = _service(fs, home)
    service.create("group-acme")
    carol = DirectoryDevice(
        device_id="node-carol",
        owner="carol@example",
        server_public="nodekey:server-carol",
        client_public="nodekey:client-carol",
        address="tcp://127.0.0.1:7447#carol",
        key_generation=1,
        updated_at="2026-10-08T00:00:00+00:00",
    )
    OrgFsDirectoryStore(fs).put_device("group-acme", carol)
    fs.content_pending.add("node-carol.json")
    fs.parked = (("user:carol@example", 2),)

    view = service.network("group-acme")

    assert "node-carol" not in {d["deviceId"] for d in view["directory"]["group-acme"]}
    problem = {
        "org": "group-acme",
        "path": "directory/devices/carol@example/node-carol.json",
        "owner": directory_owner_principal("carol@example"),
        "deviceId": "node-carol",
        "reason": "content-pending",
        "version": "v1",
    }
    assert view["directoryPending"]["group-acme"] == {
        "contentPending": [
            {
                "owner": directory_owner_principal("carol@example"),
                "deviceId": "node-carol",
                "reason": "content-pending",
            }
        ],
        "parkedCommits": [{"author": "user:carol@example", "commits": 2}],
        "dataProblems": [problem],
    }
    assert view["dataProblems"] == [problem]


def test_network_keeps_the_device_listing_when_parked_status_fails(home: Path) -> None:
    """A failing status read leaves parkedCommits unknown, never the devices."""

    fs = FakeFs()
    service = _service(fs, home)
    service.create("group-acme")

    def broken_parked(_space_id: str) -> tuple[tuple[str, int], ...]:
        raise OrgFsError("internal", {"message": "parked unavailable"})

    fs.parked_commits = broken_parked  # type: ignore[method-assign]
    view = service.network("group-acme")

    assert NODE_ID in {d["deviceId"] for d in view["directory"]["group-acme"]}
    assert view["directoryPending"]["group-acme"] == {
        "contentPending": [],
        "parkedCommits": None,
    }


def test_network_names_an_unknown_parked_author(home: Path) -> None:
    fs = FakeFs()
    service = _service(fs, home)
    service.create("group-acme")
    fs.parked = (("", 3),)

    view = service.network("group-acme")

    assert view["directoryPending"]["group-acme"]["parkedCommits"] == [
        {"author": "unknown", "commits": 3}
    ]
