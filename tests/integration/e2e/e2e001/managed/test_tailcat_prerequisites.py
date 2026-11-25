"""Docker mesh uses the current real device, membership and private-slot contract."""

import json
from subprocess import CompletedProcess

import pytest

from tests.integration.e2e.e2e001.test_e2e001_container_owner import load_runner


def test_image_builds_current_linux_sidecar_and_labrelay():
    runner = load_runner()
    text = (runner.ROOT / "contract/e2e/docker/Dockerfile").read_text()
    assert "COPY sidecar/hyprial-tailcat" in text
    assert "golang:1.27.1" in text
    assert "./cmd/hyprial-tailcat-labrelay" in text
    assert "./cmd/hyprial-tailcat" in text
    assert "CGO_ENABLED=0" in text


def test_container_uses_private_backend_and_owned_relay():
    runner = load_runner()
    runner._DERP.update({"host": "172.29.31.2", "derpPort": 12345, "stunPort": 12346, "insecureForTests": True})
    env = runner.container_environment(runner.NODES[0])
    assert env["HYPRIAL_ZENOH_LISTEN"] == "unixsock-stream//data/home/net/zenoh.sock"
    assert "HYPRIAL_ZENOH_CONNECT" not in env
    assert env["HYPRIAL_FORWARDING"] == "on"
    assert env["HYPRIAL_TAILCAT_BINARY"] == "/opt/hyprial/bin/hyprial-tailcat"
    assert json.loads(env["HYPRIAL_TAILCAT_DERP"])["host"] == "172.29.31.2"
    assert env["HYPRIAL_PEER_DISCOVERY"] == env["HYPRIAL_ZENOH_GOSSIP"] == "0"
    assert env["HYPRIAL_USAGE_DISABLE"] == "1"


def test_entrypoint_initializes_device_without_replacing_pid_one():
    runner = load_runner()
    text = (runner.ROOT / "contract/e2e/docker/entrypoint.sh").read_text()
    assert "ensure_device_key" in text
    assert "device_id=os.environ[\"HYPRIAL_NODE_ID\"]" in text
    assert "exec hyprial daemon run" in text


@pytest.mark.parametrize("foreign", ["tcp/other-node:7447", "unixsock-stream//outside/slot-00"])
def test_endpoint_proof_refuses_direct_or_foreign_connections(foreign):
    runner = load_runner()
    value = {"zenoh": {
        "listen": ["unixsock-stream//data/home/net/zenoh.sock"], "connect": [foreign],
    }, "forwarding": {"endpoints": [foreign]}}
    with pytest.raises(AssertionError, match="private|slot"):
        runner.assert_managed_endpoints(runner.NODES[0], value)


def test_endpoint_proof_requires_reported_slots_to_be_dialed():
    runner = load_runner()
    slot = "unixsock-stream//data/home/state/tailcat/slots/slot-00"
    runner.assert_managed_endpoints(runner.NODES[0], {
        "zenoh": {"listen": ["unixsock-stream//data/home/net/zenoh.sock"], "connect": [slot]},
        "forwarding": {"endpoints": [slot]},
    })
    with pytest.raises(AssertionError, match="dial|slot"):
        runner.assert_managed_endpoints(runner.NODES[0], {
            "zenoh": {"listen": ["unixsock-stream//data/home/net/zenoh.sock"], "connect": []},
            "forwarding": {"endpoints": [slot]},
        })


def test_org_invite_join_uses_public_lifecycle_and_stdin_only(monkeypatch):
    runner = load_runner()
    space = "fixture-space"
    name = "group-e2e001-fixture"
    invite = "https://fixture.invalid/invite?token=EPHEMERAL_TEST_TOKEN"
    actor = f"agent:e2e:{runner.NODES[1]}:a.aaaaaaaaaaaaaaaaaaaaaaaaaa"
    monkeypatch.setattr(runner, "_ORG_NAMES", {space: name}, raising=False)
    calls = []

    def command(node, *args, **kwargs):
        calls.append((node, args))
        if args == ("org", "invite", name, "--user", "e2e"):
            return {"ok": True, "link": invite}
        if args == ("ps",):
            return {"daemon": {"owner": "e2e"}}
        if args == ("org", "acl", "grant", name, actor):
            return {"ok": True}
        raise AssertionError(f"must use the public org lifecycle: {args}")

    def execute(argv, **kwargs):
        assert argv == ["docker", "exec", "-i", runner.NODES[1], "hyprial", "org", "join", "-", "--json"]
        assert invite not in str(argv)
        assert kwargs["input"] == invite
        return CompletedProcess(argv, 0, json.dumps({"ok": True, "org": name, "steps": [{"ok": True}]}), "")

    def wait(probe, accept, **kwargs):
        value = probe()
        assert accept(value)
        return value

    monkeypatch.setattr(runner, "exec_json", command)
    monkeypatch.setattr(runner.subprocess, "run", execute)
    monkeypatch.setattr(runner, "wait_for", wait)
    runner.invite_org_owner(runner.NODES[0], space, "e2e")
    runner.join_org_space(runner.NODES[1], space, actor=actor)
    assert not any("fs" in args for _, args in calls)
