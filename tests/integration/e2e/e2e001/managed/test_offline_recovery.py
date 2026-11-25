"""The Docker offline phase follows the public CLI and keeps recovery checks."""

import json

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common import support
from hyprial.shell.impl.cli.commands.common.root import app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401
from tests.integration.e2e.e2e001.test_e2e001_container_owner import load_runner


@pytest.mark.parametrize(
    "fault,expected",
    [
        (None, None),
        ("not-queued", "offline send was not queued"),
        ("not-durable", "sender outbox"),
        ("not-delivered", "offline message delivered after receiver restart"),
        ("not-drained", "sender outbox drained after receipt"),
        ("not-acknowledged", "offline acknowledgement failed"),
    ],
)
def test_offline_queue_uses_public_observation_and_requires_recovery(
    monkeypatch, fault, expected
):
    runner = load_runner()
    sender, receiver = runner.NODES
    sender_actor = f"agent:e2e:{sender}:a.aaaaaaaaaaaaaaaaaaaaaaaaaa"
    receiver_actor = f"agent:e2e:{receiver}:a.bbbbbbbbbbbbbbbbbbbbbbbbbb"
    state = {"running": True, "queued": False, "acknowledged": False}
    events = []
    budgets = []
    observed_requests = []

    def request(method, params=None, **kwargs):
        observed_requests.append((method, params))
        assert method == "outbox.list"
        assert params is None
        return {"ok": True, "entries": [{"messageId": "offline-message"}]}

    def shell(args, **kwargs):
        assert args in (["docker", "stop", receiver], ["docker", "start", receiver])
        events.append(args[1])
        assert kwargs == {"timeout": 30.0}
        state["running"] = args[1] == "start"

    def command(node, *args, **kwargs):
        if args[:2] == ("message", "send"):
            assert node == sender and not state["running"]
            assert args[2:6] == ("--from", sender_actor, "--to", receiver_actor)
            state["queued"] = True
            events.append("send")
            return {
                "messageId": "offline-message",
                "deliveries": [{"queued": fault != "not-queued"}],
            }
        if args == ("ps",):
            if node == sender:
                count = int(state["queued"] and not state["running"])
                if fault == "not-durable":
                    count = 0
                if fault == "not-drained" and state["queued"]:
                    count = 1
                return {"outboxCount": count}
            assert node == receiver and state["running"]
            pending = [] if fault == "not-delivered" else [{"messageId": "offline-message"}]
            return {"pendingMessages": pending}
        if args == ("message", "ack", "--from", receiver_actor, "offline-message"):
            assert node == receiver and state["running"]
            state["acknowledged"] = fault != "not-acknowledged"
            events.append("ack")
            return {"acknowledged": state["acknowledged"]}
        assert node == sender and not state["running"]
        result = CliRunner().invoke(app, [*args, "--json"])
        assert result.exit_code == 0, result.stdout
        events.append("observe")
        return json.loads(result.stdout)

    def wait_for(read, accept, *, timeout, what):
        budgets.append((what, timeout))
        assert accept(read()), what

    def ready(node):
        assert node == receiver and state["running"]
        events.append("ready")

    def endpoints(node, status):
        assert node == receiver and state["running"]
        assert isinstance(status, dict)
        events.append("endpoints")

    monkeypatch.setattr(support, "_daemon_request", request)
    monkeypatch.setattr(runner, "sh", shell)
    monkeypatch.setattr(runner, "exec_json", command)
    monkeypatch.setattr(runner, "wait_for", wait_for)
    monkeypatch.setattr(runner, "wait_daemon_ready", ready)
    monkeypatch.setattr(runner, "assert_managed_endpoints", endpoints)
    if expected is not None:
        with pytest.raises(AssertionError, match=expected):
            runner.offline_queue(sender, sender_actor, receiver, receiver_actor)
        return
    assert runner.offline_queue(sender, sender_actor, receiver, receiver_actor) == "offline-message"
    assert events == ["stop", "send", "observe", "start", "ready", "endpoints", "ack"]
    assert observed_requests == [("outbox.list", None)]
    assert budgets == [
        ("sender outbox", 30.0),
        ("offline message delivered after receiver restart", 90.0),
        ("sender outbox drained after receipt", 90.0),
    ]
    assert state["acknowledged"]
