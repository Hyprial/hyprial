"""The peripheral daemon fixture retains explicit init and failure semantics."""
from __future__ import annotations

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.org.network import ipc
from hyprial.kernel import DaemonRequestError
from hyprial.shell.impl.daemon_wiring import daemon_dependencies
from tests.support.tailnet_daemon import membership_services


@pytest.fixture
def app(tmp_path, own_daemon_application):
    return own_daemon_application(DaemonApplication(
        state_dir=tmp_path / "state", socket_path=tmp_path / "daemon.sock",
        hyprial_home=tmp_path / "home", node_id="fixture-node", owner="fixture-owner",
        **daemon_dependencies(),
    ))


def test_membership_fixture_initializes_and_restores_authority_without_directory_import(app):
    with membership_services() as peers:
        created = ipc.ipc_org_create(app, {"org": "group-fixture"})
        fake, store = next(iter(peers.values()))
        assert [op for op, _ in fake.calls] == [
            "membership-status", "membership-init", "member-announce",
        ]
        init = fake.calls[1][1]
        assert init["migration"] == {"source": "new", "sourceDigest": "", "completed": True}
        assert init["members"] == [fake.local]
        receipts = fake.receipts.copy()
        assert store.stat().st_mode & 0o777 == 0o600

    with membership_services() as peers:
        repeated = ipc.ipc_org_create(app, {"org": "group-fixture"})
        restored, _ = next(iter(peers.values()))
        assert repeated["spaceId"] == created["spaceId"]
        assert [op for op, _ in restored.calls] == ["membership-status"]
        assert dict(restored.receipts) == {key: list(value) for key, value in receipts.items()}
        assert restored.leases == {}
        status = restored.membership_request("membership-status", {})
        assert status["readiness"]["member"] is True
        assert status["tailnets"][0]["role"] == "authority"


def test_membership_fixture_preserves_status_failure(app):
    with membership_services() as peers:
        ipc._build_service(app)
        fake, _ = next(iter(peers.values()))
        fake.failures["membership-status"] = "MEMBERSHIP_STORE_INVALID"
        with pytest.raises(DaemonRequestError) as refused:
            ipc.ipc_org_create(app, {"org": "group-refused"})
        assert refused.value.code == "MEMBERSHIP_STORE_INVALID"
        assert fake.tailnets == {}
        assert fake.receipts == {}
        assert [op for op, _ in fake.calls] == ["membership-status"]
