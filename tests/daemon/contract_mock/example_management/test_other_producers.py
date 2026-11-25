"""Remaining management examples against isolated real producer calls."""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.application.messaging.status.views import _StatusViewsMixin
from hyprial.daemon.impl.org.network.ipc import _translate
from hyprial.daemon.impl.org.network.service import OrgNetworkError, OrgNetworkService
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.kernel import DaemonRequestError, example_to_json

from .test_producer_examples import example


def test_hosts_examples_match_status_producer():
    for name, nodes in (("online-host", ("node-1",)), ("no-hosts", ())):
        app = SimpleNamespace(
            _worker_status_snapshot=nullcontext,
            _online_host_nodes=lambda: nodes,
            _status_refresh=SimpleNamespace(
                targets=lambda prefix: (),
                snapshot=lambda target: None,
                status=lambda target: {
                    "observedAtMs": 1762075800000,
                    "ageMs": 0,
                    "stale": False,
                    "lastError": None,
                    "expired": False,
                },
            ),
            _refresh_response_fields=lambda params: {},
        )
        assert (
            _StatusViewsMixin._ipc_hosts(app, {})
            == example_to_json(example("hosts", name))["result"]
        )


def test_top_examples_match_required_snapshot_fields(tmp_path):
    from hyprial.daemon.impl.operations.top import build_top_snapshot

    result = build_top_snapshot(
        state_dir=tmp_path,
        owner="alice",
        node_id="node-1",
        epoch="epoch-example",
        daemon_pid=4242,
        connectors=[],
        pending={},
        stranded=[],
        now_ms=1762075800000,
    )
    for name in ("empty-runtime", "no-actors"):
        assert example_to_json(example("top.snapshot", name))["result"] == {
            **result,
            "quota": {"sources": []},
        }


def test_not_ready_examples_use_handler_messages():
    from hyprial.daemon.impl.request_catalog.org.network_specs import org_publish
    from hyprial.daemon.impl.request_catalog.org.view_specs import hosts, top_snapshot

    for method, handler in (
        ("hosts", hosts),
        ("top.snapshot", top_snapshot),
        ("org.publish", org_publish),
    ):
        case = example(method, "service-not-ready")
        with pytest.raises(DaemonRequestError) as caught:
            handler(SimpleNamespace(), case.params)
        assert (caught.value.code, str(caught.value)) == (
            case.error.code,
            case.error.message,
        )


def test_publish_example_matches_real_application_view(tmp_path):
    (tmp_path / "org-context.md").write_text("synthetic accepted context")
    app = SimpleNamespace(
        hyprial_home=tmp_path,
        _org_endpoint=SimpleNamespace(publish_accepted=lambda: True),
        _require_org_context_bridge=lambda: SimpleNamespace(
            publish_accepted=lambda: True
        ),
    )
    assert (
        _StatusViewsMixin._ipc_org_publish(app)
        == example_to_json(example("org.publish", "published"))["result"]
    )


def _no_meta(space_id, path):
    raise ValueError(path)


def test_delete_error_is_actual_authorized_service_refusal(tmp_path):
    service = object.__new__(OrgNetworkService)
    service._require_allows = lambda **kwargs: None
    service._home = tmp_path
    service._node_id = "node-authority"
    service._network_isolated = False
    service._require_space = lambda org: SimpleNamespace(space_id="space-hyprial-dev")
    service._org_state = lambda org: "deleted"
    service._tailnet_raw_call = lambda op, params: {"tailnets": []}
    service._store = SimpleNamespace(orgs=lambda: [])
    service._fs = SimpleNamespace(read_text=_no_meta)
    case = example("org.delete", "already-deleted")
    with pytest.raises(OrgNetworkError) as caught:
        service.delete(case.params["org"])
    assert (caught.value.code, str(caught.value)) == (
        case.error.code,
        case.error.message,
    )


def test_execute_steps_match_actual_bootstrap_producer(tmp_path):
    service = object.__new__(OrgNetworkService)
    service._home = tmp_path
    service._log = lambda *args, **kwargs: None
    service._redeem_tailnet = lambda parsed: None
    service._announce_tailnet = lambda space: None
    service._tailnet_candidates = lambda space: {
        "node-2": "tcp://127.0.0.1:7447#synthetic"
    }
    service._directory_peers_of = lambda org: service._tailnet_candidates(None)
    service._map_peer = lambda peer, address: 41002
    service._request_redial = None
    service._oidc_issuer = None
    service._oidc_roster = None
    service._join_with_retry = lambda space: None
    service._publish_into = lambda org: True
    case = example("org.execute", "bootstrap-complete")
    assert service.execute(case.params["link"]) == example_to_json(case)["result"]


def test_repair_examples_match_real_recovery_and_create(tmp_path):
    from hyprial.daemon.impl.org.network.directory import InvalidDeviceQuarantine
    from tests.daemon.org.network.test_org_network_service import FakeFs

    fs = FakeFs()
    org, space, acl = "group-hyprial-dev", "space-hyprial-dev", "space-hyprial-dev-acl"
    for name, ident in ((org, space), (org + "-acl", acl)):
        fs.space_ids[name] = ident
        fs.metadata[ident] = {
            "name": name,
            "owner": "user:alice",
            "spaceId": ident,
            "createdAt": "2026-11-03T00:00:00+00:00",
        }
    fs.metadata[space]["aclSpaceId"] = acl
    fs.spaces_listing = lambda: [
        SimpleNamespace(
            space_id=row["spaceId"],
            name=row["name"],
            owner=row["owner"],
            created_at=row["createdAt"],
        )
        for row in fs.metadata.values()
    ]
    service = OrgNetworkService(
        fs=fs,
        home=tmp_path,
        node_id="node-1",
        owner="alice",
        invalid_devices=InvalidDeviceQuarantine(),
        membership_request=lambda op, params: {"tailnets": [{"tailnetId": space}]},
    )
    case = example("org.repair", "repairable-candidate")
    assert service.repair(apply=True) == example_to_json(case)["result"]
    assert (
        service.repair(apply=False)
        == example_to_json(example("org.repair", "empty-candidate-list"))["result"]
    )


def test_repair_unavailable_error_uses_actual_orgfs_translation():
    def closing_spaces():
        raise OrgFsError("unavailable", {"message": "orgfs runtime is closing"})

    service = object.__new__(OrgNetworkService)
    service._fs = SimpleNamespace(spaces=closing_spaces)
    service._home = None
    service._node_id, service._owner = "node-1", "alice"
    with pytest.raises(OrgFsError) as caught:
        service.repair(apply=False)
    error = _translate(caught.value)
    case = example("org.repair", "orgfs-closing")
    assert (error.code, str(error), error.data) == (
        case.error.code,
        case.error.message,
        dict(case.error.data),
    )
