"""Management examples must match real producers, beyond schema acceptance."""

from __future__ import annotations

from types import SimpleNamespace

from hyprial.daemon.impl.org.network.service import OrgNetworkError, OrgNetworkService
from hyprial.daemon.impl.org.network.status.view import network_view
from hyprial.daemon.impl.orgfs import LocalOrgFs
from hyprial.daemon.impl.request_catalog.org.management_examples import (
    ORG_MANAGEMENT_EXAMPLES,
)
from hyprial.identity import DirectoryDevice
from hyprial.kernel import example_to_json


def example(method, name):
    return next(
        case
        for group in ORG_MANAGEMENT_EXAMPLES
        if group.method == method
        for case in group.cases
        if case.name == name
    )


def test_network_example_matches_real_three_pane_producer():
    case = example("org.network", "configured-network")
    org = case.params["org"]
    device = DirectoryDevice(
        device_id="node-1",
        owner="alice",
        server_public="nodekey:" + "1" * 64,
        client_public="nodekey:" + "2" * 64,
        address="tcp://127.0.0.1:7447#synthetic",
        key_generation=1,
        updated_at="2026-11-03T00:00:00+00:00",
    )
    fs = SimpleNamespace(
        spaces=lambda: (SimpleNamespace(name=org, space_id="space-hyprial-dev"),)
    )
    store = SimpleNamespace(
        org_state=lambda name: "active",
        scan_devices=lambda name: (
            [device],
            {"contentPending": [], "parkedCommits": None},
        ),
    )
    from hyprial.daemon.impl.application.netendpoints.forwarding import (
        _ForwardingOpsMixin,
    )

    backend = SimpleNamespace(
        last_poll={
            "atMs": 1762075800000,
            "ok": True,
            "error": None,
            "peersReported": True,
            "peers": {"node-2": "tcp/127.0.0.1:41002"},
        }
    )
    app = SimpleNamespace(
        _forwarding_policy=SimpleNamespace(
            to_json=lambda: {"mode": "auto", "source": "default"}
        ),
        _forwarding_supervisor=None,
        _forwarding_discovery=backend,
        _forwarding_effective=("tcp/127.0.0.1:41002",),
        _forwarding_dialed=(),
        _forwarding_backend=lambda: backend,
        _forwarding_diagnostic_counters=lambda: {},
    )
    forwarding = _ForwardingOpsMixin._forwarding_status_json(app)
    actual = network_view(
        fs=fs,
        store=store,
        node_id="node-1",
        owner="alice",
        orgs=[org],
        org=org,
        require_allows=lambda **kwargs: None,
        authorization_error=OrgNetworkError,
        forwarding_status=lambda: forwarding,
        own_address=lambda: device.address,
        server_public=device.server_public,
        logger=lambda *args, **kwargs: None,
    )
    assert example_to_json(case)["result"] == actual
    assert actual["mappedPeers"] == forwarding["peers"]
    assert actual["sidecar"] is not None
    assert actual["paneStatus"]["mappedPeers"] == actual["paneStatus"]["sidecar"]


def test_execute_success_link_is_decodable_and_has_matching_workflow():
    from hyprial.identity import decode_invite, parse_bootstrap_yaml

    case = example("org.execute", "bootstrap-complete")
    link = decode_invite(case.params["link"])
    assert (link.org, link.space_id) == (case.result["org"], case.result["spaceId"])
    assert link.inviter == link.tailnet["authority"]["owner"] == "alice"
    assert link.inviter_device == link.tailnet["authority"]["deviceId"]
    assert link.inviter_address == link.tailnet["authority"]["address"]
    assert link.space_id == link.tailnet["tailnetId"]
    assert [step.kind for step in parse_bootstrap_yaml(link.workflow_yaml)] == [
        step["kind"] for step in case.result["steps"]
    ]


def test_execute_expired_link_reaches_expiry_producer():
    import pytest
    from hyprial.identity import InviteError, decode_invite

    case = example("org.execute", "expired-invite")
    with pytest.raises(InviteError) as caught:
        decode_invite(case.params["link"])
    assert (caught.value.code, str(caught.value)) == (
        case.error.code,
        case.error.message,
    )


def test_removal_examples_match_real_journal(tmp_path, monkeypatch):
    import uuid

    monkeypatch.setattr(
        "hyprial.daemon.impl.forwarding.membership.removal.uuid.uuid4",
        lambda: uuid.UUID(int=1),
    )
    for method, owner, device, local, role in (
        ("org.leave", "alice", "node-1", "node-1", "member"),
        ("org.remove", "alex", "node-alex", "node-authority", "authority"),
    ):
        case = example(method, "pending-confirmation")
        table = {
            "tailnetId": "space-hyprial-dev",
            "revision": 3,
            "authorityDeviceId": "node-authority",
            "role": role,
            "members": [
                {
                    "owner": owner,
                    "deviceId": device,
                    "state": "active",
                    "masterPublic": "nodekey:" + "3" * 64,
                    "serverPublic": "nodekey:" + "4" * 64,
                }
            ],
        }
        service = _removal_service(tmp_path / method, local, table)
        actual = getattr(service, method.split(".")[1])(**case.params)
        assert example_to_json(case)["result"] == actual


def test_network_empty_example_matches_real_forwarding_off_projection():
    from hyprial.daemon.impl.application.netendpoints.forwarding import (
        _ForwardingOpsMixin,
    )

    app = SimpleNamespace(
        _forwarding_policy=SimpleNamespace(
            to_json=lambda: {"mode": "auto", "source": "default"}
        ),
        _forwarding_supervisor=None,
        _forwarding_discovery=None,
        _forwarding_unavailable=None,
        _forwarding_diagnostic_counters=lambda: {},
    )
    actual = network_view(
        fs=SimpleNamespace(),
        store=SimpleNamespace(),
        node_id="node-1",
        owner="alice",
        orgs=[],
        org=None,
        require_allows=lambda **kwargs: None,
        authorization_error=OrgNetworkError,
        forwarding_status=lambda: _ForwardingOpsMixin._forwarding_status_json(app),
        own_address=lambda: "tcp://127.0.0.1:7447#synthetic",
        server_public="nodekey:" + "1" * 64,
        logger=lambda *args, **kwargs: None,
    )
    assert (
        actual == example_to_json(example("org.network", "no-organizations"))["result"]
    )


def test_network_unknown_space_example_is_a_real_translated_error():
    import pytest
    from hyprial.daemon.impl.org.network.ipc import _translate
    from hyprial.daemon.impl.orgfs.api import OrgFsError

    case = example("org.network", "unknown-space")
    with pytest.raises(OrgFsError) as caught:
        network_view(
            fs=SimpleNamespace(spaces=lambda: ()),
            store=SimpleNamespace(org_state=lambda org: "partial"),
            node_id="node-1",
            owner="alice",
            orgs=[case.params["org"]],
            org=case.params["org"],
            require_allows=lambda **kwargs: None,
            authorization_error=OrgNetworkError,
            forwarding_status=None,
            own_address=lambda: None,
            server_public="nodekey:" + "1" * 64,
            logger=lambda *args, **kwargs: None,
        )
    error = _translate(caught.value)
    assert (error.code, str(error), error.data) == (
        case.error.code,
        case.error.message,
        dict(case.error.data),
    )


def test_removal_authority_refusal_is_real_journal_error(tmp_path):
    import pytest

    for method, owner in (("org.leave", "alice"), ("org.remove", "alex")):
        case = example(method, "authority-refused")
        table = {
            "tailnetId": "space-hyprial-dev",
            "authorityDeviceId": "node-authority",
            "members": [
                {"owner": owner, "deviceId": "node-authority", "state": "active"}
            ],
        }
        service = _removal_service(
            tmp_path / method,
            "node-authority" if method == "org.leave" else "node-1",
            table,
        )
        with pytest.raises(OrgNetworkError) as caught:
            getattr(service, method.split(".")[1])(**case.params)
        assert (caught.value.code, str(caught.value)) == (
            case.error.code,
            case.error.message,
        )


def _removal_service(home, local, table):
    service = object.__new__(OrgNetworkService)
    service._home = home
    service._node_id = local
    service._network_isolated = False
    service._owner = "alice"
    service._fs = LocalOrgFs(author="user:alice")
    service._require_allows = lambda **kwargs: None
    service._require_space = lambda org: SimpleNamespace(space_id=table["tailnetId"])
    service._org_state = lambda org: "active"
    service._org_cell_bound = None
    service._tailnet_raw_call = lambda op, params: {"tailnets": [table]}
    return service
