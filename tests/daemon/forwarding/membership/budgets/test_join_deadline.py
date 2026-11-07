"""The join deadline is shared, not restarted by each bootstrap stage."""
import threading
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.org.network.service import OrgNetworkError, OrgNetworkService


def test_two_orgfs_joins_share_remaining_budget(monkeypatch):
    from hyprial.daemon.impl.org.network.tailnet.budget import JoinBudget

    clock = [0.0]
    calls = []
    budget = JoinBudget(clock=lambda: clock[0])

    def join(space, *, timeout):
        calls.append((space, timeout))
        clock[0] += 25

    service = object.__new__(OrgNetworkService)
    service._join_budget = budget
    service._join_timeout_seconds = 30
    service._fs = SimpleNamespace(join=join)
    service._sleep = lambda seconds: None
    clock[0] = 60
    service._join_with_retry("directory")
    with pytest.raises(OrgNetworkError):
        service._join_with_retry("acl")
    assert calls[0][0] == "directory"
    assert calls[0][1] == pytest.approx(30, abs=0.01)
    assert calls[1] == ("acl", 20)


def test_expired_deadline_names_stage_without_starting_next_effect():
    from hyprial.daemon.impl.org.network.tailnet.budget import JoinBudget

    clock = [0.0]
    budget = JoinBudget(clock=lambda: clock[0])
    clock[0] = 106
    with pytest.raises(OrgNetworkError) as caught:
        budget.remaining("member-announce", 35)
    assert caught.value.details == {
        "operation": "join", "leg": "member-announce", "budgetSeconds": 105,
    }
    assert caught.value.code == "MEMBERSHIP_SIDECAR_TIMEOUT"


def test_ipc_membership_redial_and_mapping_draw_from_shared_deadline(monkeypatch, tmp_path):
    from hyprial.daemon.impl.forwarding import slots
    from hyprial.daemon.impl.org.network.ipc import _request_service
    from hyprial.daemon.impl.org.network.tailnet.budget import JoinBudget

    calls = []
    app = SimpleNamespace(
        hyprial_home=tmp_path, node_id="node", owner="owner",
        _require_orgfs_runtime=lambda: SimpleNamespace(facade=object()),
        _start_forwarding_supervisor=lambda: None,
        _forwarding_supervisor=SimpleNamespace(
            membership_request=lambda op, params, *, timeout: calls.append((op, timeout)) or {},
        ),
        _await_forwarding_reconcile=lambda reason, *, timeout: calls.append(("redial", timeout)),
    )
    monkeypatch.setattr(slots, "bind_peer_slot", lambda app, peer, address, *, timeout: calls.append(("map", timeout)))
    service = _request_service(app)
    clock = [0.0]
    service._join_budget = JoinBudget(clock=lambda: clock[0])
    clock[0] = 100
    service._membership_request("member-announce", {})
    service._request_redial()
    service._map_peer("peer", "address")
    assert calls == [("member-announce", 5), ("redial", 5), ("map", 5)]
    clock[0] = 106
    with pytest.raises(OrgNetworkError):
        service._membership_request("member-announce", {})
    assert len(calls) == 3


@pytest.mark.parametrize("timeout,expected", [(7, 7), (None, 120)])
@pytest.mark.parametrize("operation", ["join", "serve", "checkout", "open-mesh"])
def test_orgfs_lifecycle_bounds_wait_without_cancelling_accepted_operation(monkeypatch, timeout, expected, operation):
    from hyprial.daemon.impl.orgfs.api import OrgFsError
    from hyprial.daemon.impl.orgfs.composition import lifecycle
    from hyprial.kernel import AdmissionResult

    waits = []
    records = []
    clock = [0.0]
    authority = SimpleNamespace(
        actor=object(), generation=1, commands={}, waiters={},
        runtime=SimpleNamespace(tell=lambda *args: AdmissionResult.ACCEPTED),
    )
    runtime = lifecycle.RuntimeLifecycle()
    runtime._resource_lock = threading.Lock()
    runtime._lifecycle_lock = threading.Lock()
    runtime._purging_spaces = set()
    runtime._lifecycle_context = SimpleNamespace(running=False)
    runtime._lifecycle_closing = False
    runtime._ensure_lifecycle_authority = lambda: authority
    runtime.logger = lambda level, event, **fields: records.append((level, event, fields))

    def wait(timeout):
        waits.append(timeout)
        clock[0] += timeout
        return False

    monkeypatch.setattr(lifecycle.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(lifecycle, "threading", SimpleNamespace(
        Event=lambda: SimpleNamespace(wait=wait),
    ))
    command = {
        "join": lifecycle._JoinSpace("directory"),
        "serve": lifecycle._ServeSpace("directory", "fs"),
        "checkout": lifecycle._CheckoutSpace("directory", True),
        "open-mesh": lifecycle._OpenSpaceMesh("directory"),
    }[operation]
    with pytest.raises(OrgFsError) as caught:
        if operation == "join":
            runtime.join("directory", timeout=timeout)
        else:
            runtime._ask_lifecycle(command, timeout=timeout)
    assert waits == [expected]
    assert caught.value.details["leg"] == ("join-orgfs" if operation == "join" else "orgfs-lifecycle")
    assert caught.value.details["budgetSeconds"] == expected
    assert authority.waiters == {}
    assert len(authority.commands) == 1
    assert records == [("warn", "orgfs.lifecycle.wait-timeout", {
        "operation": operation, "waitedSeconds": expected, "budgetSeconds": expected,
        "commandId": next(iter(authority.commands)),
    })]


def test_join_returns_at_shared_deadline_when_orgfs_never_answers(monkeypatch):
    from hyprial.daemon.impl.org.network import service as module
    from hyprial.daemon.impl.orgfs.api import OrgFsError

    clock = [0.0]
    calls = []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    parsed = SimpleNamespace(space_id="directory", acl_space_id="acl", workflow_yaml="")
    monkeypatch.setattr(module, "decode_invite", lambda link: parsed)
    monkeypatch.setattr(module, "parse_bootstrap_yaml", lambda value: [
        SimpleNamespace(kind="join-org"), SimpleNamespace(kind="register-device"),
    ])

    def never_answers(space, *, timeout):
        calls.append((space, timeout))
        clock[0] += timeout
        raise OrgFsError("unavailable", {"leg": "join-orgfs", "budgetSeconds": timeout})

    service = object.__new__(OrgNetworkService)
    service._redeem_tailnet = lambda parsed: clock.__setitem__(0, 100)
    service._announce_tailnet = lambda space: None
    service._join_timeout_seconds = 30
    service._fs = SimpleNamespace(join=never_answers)
    with pytest.raises(OrgNetworkError) as caught:
        service.execute("invite")
    assert clock[0] == 105
    assert calls == [("directory", 5)]
    assert caught.value.details == {"operation": "join", "leg": "join-orgfs", "budgetSeconds": 105}
    assert service._join_budget is None


def test_join_cli_budget_and_stage_plan_use_stderr(capsys):
    from hyprial.shell.impl.invites.join import request_join

    calls = []
    runtime = SimpleNamespace(request=lambda *args, **kwargs: calls.append((args, kwargs)))
    request_join(runtime, "secret-invite", json_output=True)
    assert calls == [(("org.join", {"link": "secret-invite"}), {"timeout": 120})]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "redeeming" in captured.err and "fetching roster" in captured.err
    assert "announcing" in captured.err and "secret-invite" not in captured.err
