"""An isolated daemon creates a local org without starting a Tailnet sidecar."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "contract/e2e"))

from _daemon_runner import IsolatedDaemon, hyprial_executable, short_temp_root  # noqa: E402


def test_org_create_succeeds_on_an_isolated_daemon() -> None:
    with tempfile.TemporaryDirectory(
        prefix="hyprial-org-create-", dir=short_temp_root()
    ) as raw:
        daemon = IsolatedDaemon(hyprial_executable(), Path(raw) / "node", "org-create-isolated")
        assert daemon.env["HYPRIAL_NETWORK_ISOLATED"] == "1"
        daemon.start()
        try:
            created = daemon.command(
                "org", "create", "isolated-group", "--json", exits=(0,), timeout=60
            )
            listed = daemon.command("org", "list", "--json")
            status = daemon.ps()
            invited = daemon.command(
                "org", "invite", created["org"], "--user", "bob@example", "--json",
                exits=(0, 1), timeout=60,
            )
        finally:
            daemon.stop()

    assert created["ok"] is True
    assert created["spaceId"]
    assert created["tailnetInitialized"] is False
    assert created["published"] is created["devicePublished"] is None
    assert listed["orgs"][0]["state"] == "active"
    assert status["zenoh"]["isolated"] is True
    assert invited["ok"] is False
    assert invited["code"] == "MEMBERSHIP_NOT_INITIALIZED"
