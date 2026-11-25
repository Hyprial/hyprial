"""An unknown host row is not an offline verdict or an erased peer."""

import json

import pytest

from tests.integration.e2e.e2e001.test_e2e001_container_owner import load_runner


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now


def host(peer, status="unknown"):
    return {"nodeId": peer, "status": status, "observedAtMs": 1, "ageMs": 90_487,
            "stale": True, "lastError": None}


def target(peer, suffix="b", status="online"):
    actor = f"agent:e2e:{peer}:a.{suffix * 26}"
    return {"targetKind": "agent", "actor": actor, "targetUri": actor,
            "status": status, "deliverable": True}


def configured(monkeypatch, *, hosts=None, targets=None, state=None, durations=()):
    runner = load_runner()
    sender, peer = runner.NODES
    clock = Clock()
    calls = []
    waits = []
    responses = {
        "hosts": {"hosts": [host(peer)]} if hosts is None else hosts,
        "targets": {"targets": []} if targets is None else targets,
        "docker": {"Running": False, "Status": "exited"} if state is None else state,
    }

    def record(kind, kwargs):
        calls.append((kind, clock.now, kwargs))
        index = len(calls) - 1
        clock.now += durations[index] if index < len(durations) else 0.0
        return responses[kind]

    def command(node, *args, **kwargs):
        assert node == sender
        if args == ("host", "list"):
            return record("hosts", kwargs)
        assert args == ("message", "targets", "--kind", "agent")
        return record("targets", kwargs)

    def docker(args, **kwargs):
        assert args == ["inspect", "--format", "{{json .State}}", peer]
        value = record("docker", kwargs)
        return value if isinstance(value, str) else json.dumps(value)

    def wait(probe, accept, *, timeout, what, **kwargs):
        waits.append(timeout)
        assert accept(probe()), what

    monkeypatch.setattr(runner, "time", clock)
    monkeypatch.setattr(runner, "exec_json", command)
    monkeypatch.setattr(runner, "docker", docker)
    monkeypatch.setattr(runner, "wait_for", wait)
    return runner, sender, peer, responses, calls, waits


def test_retained_unknown_with_no_agent_target_and_stopped_container_is_absent(monkeypatch):
    runner, sender, peer, responses, calls, waits = configured(monkeypatch)
    runner.wait_for_target(sender, peer, absent=True, timeout=90.0)
    assert responses["hosts"]["hosts"][0]["status"] == "unknown"
    assert [row[0] for row in calls] == ["hosts", "targets", "docker"]
    assert waits == [90.0]
    assert all(0 < kwargs["timeout"] <= 90.0 - started for _, started, kwargs in calls)


def test_disappeared_host_row_also_requires_targets_and_physical_stop(monkeypatch):
    runner, sender, peer, _, calls, _ = configured(monkeypatch, hosts={"hosts": []})
    runner.wait_for_target(sender, peer, absent=True, timeout=90.0)
    assert [row[0] for row in calls] == ["hosts", "targets", "docker"]


@pytest.mark.parametrize("fault", [
    "online", "running", "paused", "state-shape", "hosts-shape", "host-status",
    "duplicate-host", "targets-shape", "target-live", "target-offline",
    "other-agent-on-peer", "target-address", "target-deliverable", "state-json",
    "host-status-shape", "hosts-error", "targets-error", "target-uri-mismatch",
    "state-not-exited",
])
def test_uncertain_or_contradictory_absence_is_refused(monkeypatch, fault):
    runner, sender, peer, responses, _, _ = configured(monkeypatch, hosts={"hosts": []})
    if fault == "online":
        responses["hosts"] = {"hosts": [host(peer, "online")]}
    elif fault in {"running", "paused"}:
        responses["docker"] = {"Running": True, "Status": fault}
    elif fault == "state-shape":
        responses["docker"] = {"Status": "exited"}
    elif fault == "state-json":
        responses["docker"] = "not-json"
    elif fault == "state-not-exited":
        responses["docker"] = {"Running": False, "Status": "created"}
    elif fault == "hosts-shape":
        responses["hosts"] = {}
    elif fault == "hosts-error":
        responses["hosts"] = {"ok": False, "hosts": []}
    elif fault == "host-status":
        responses["hosts"] = {"hosts": [{"nodeId": peer}]}
    elif fault == "host-status-shape":
        responses["hosts"] = {"hosts": [host(peer, {})]}
    elif fault == "duplicate-host":
        responses["hosts"] = {"hosts": [host(peer), host(peer)]}
    elif fault == "targets-shape":
        responses["targets"] = {}
    elif fault == "targets-error":
        responses["targets"] = {"ok": False, "targets": []}
    elif fault in {"target-live", "target-offline", "other-agent-on-peer"}:
        responses["targets"] = {"targets": [target(peer, suffix="c" if fault == "other-agent-on-peer" else "b", status="offline" if fault == "target-offline" else "online")]}
    elif fault == "target-address":
        responses["targets"] = {"targets": [{**target(peer), "actor": "unclaimed"}]}
    elif fault == "target-uri-mismatch":
        responses["targets"] = {"targets": [{**target(peer), "targetUri": target(sender)["actor"]}]}
    else:
        responses["targets"] = {"targets": [{**target(peer), "deliverable": False}]}
    with pytest.raises(AssertionError):
        runner.wait_for_target(sender, peer, absent=True, timeout=90.0)


def test_targets_of_other_nodes_do_not_claim_the_stopped_peer(monkeypatch):
    runner, sender, peer, responses, _, _ = configured(monkeypatch)
    responses["targets"] = {"targets": [target(sender, suffix="a")]}
    runner.wait_for_target(sender, peer, absent=True, timeout=90.0)


def test_positive_wait_rejects_retained_unknown(monkeypatch):
    runner, sender, peer, _, _, _ = configured(monkeypatch)
    with pytest.raises(AssertionError):
        runner.wait_for_target(sender, peer, timeout=90.0)


def test_positive_wait_accepts_declared_online_without_inferring_freshness(monkeypatch):
    runner, sender, peer, responses, calls, waits = configured(monkeypatch)
    responses["hosts"] = {"hosts": [host(peer, "online")]}
    runner.wait_for_target(sender, peer, timeout=90.0)
    assert [row[0] for row in calls] == ["hosts"] and waits == [90.0]


@pytest.mark.parametrize("absent,durations", [(True, (40.0, 30.0, 21.0)), (False, (90.1,))])
def test_late_complete_observation_does_not_extend_the_original_deadline(monkeypatch, absent, durations):
    runner, sender, peer, responses, _, _ = configured(monkeypatch, hosts={"hosts": []}, durations=durations)
    if not absent:
        responses["hosts"] = {"hosts": [host(peer, "online")]}
    with pytest.raises(AssertionError):
        runner.wait_for_target(sender, peer, absent=absent, timeout=90.0)


def test_exhaustion_between_observations_starts_no_further_command(monkeypatch):
    runner, sender, peer, _, calls, _ = configured(monkeypatch, durations=(90.0,))
    with pytest.raises(AssertionError, match="original deadline"):
        runner.wait_for_target(sender, peer, absent=True, timeout=90.0)
    assert [row[0] for row in calls] == ["hosts"]


def test_poll_attempts_share_one_budget_for_all_observations(monkeypatch):
    runner, sender, peer, responses, calls, _ = configured(
        monkeypatch, durations=(20.0, 20.0, 20.0, 10.0, 10.0, 1.0),
    )
    responses["hosts"] = {"hosts": [host(peer, "online")]}

    def wait(probe, accept, *, timeout, what):
        assert timeout == 90.0
        assert not accept(probe())
        runner.time.now += 0.5
        responses["hosts"] = {"hosts": [host(peer)]}
        assert accept(probe()), what

    monkeypatch.setattr(runner, "wait_for", wait)
    runner.wait_for_target(sender, peer, absent=True, timeout=90.0)
    assert [kwargs["timeout"] for _, _, kwargs in calls] == [30.0, 30.0, 30.0, 29.5, 19.5, 9.5]
    assert runner.time.now == 81.5


def test_negative_inspection_cannot_target_an_unowned_container(monkeypatch):
    runner, sender, _, _, calls, _ = configured(monkeypatch, hosts={"hosts": []})
    with pytest.raises(AssertionError):
        runner.wait_for_target(sender, "unowned-production-node", absent=True, timeout=90.0)
    assert calls == []
