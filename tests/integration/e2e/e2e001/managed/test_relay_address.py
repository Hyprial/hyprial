"""The Docker relay advertises only the IPv4 proven by its owned internal bridge."""

import copy
import json

import pytest

from tests.integration.e2e.e2e001.test_e2e001_container_owner import load_runner


def topology(runner):
    network = {
        "Id": "owned-network", "Name": runner.NETWORK, "Driver": "bridge", "Internal": True,
        "Labels": {"hyprial-e2e001": "1"}, "IPAM": {"Config": [{"Subnet": "172.29.0.0/16"}]},
        "Containers": {"owned-relay": {"Name": runner.RELAY, "IPv4Address": "172.29.31.2/16"}},
    }
    container = {
        "Id": "owned-relay", "Name": "/" + runner.RELAY,
        "Config": {"Labels": {"hyprial-e2e001": "1"}}, "State": {"Running": True},
        "NetworkSettings": {"Networks": {runner.NETWORK: {"NetworkID": "owned-network", "IPAddress": "172.29.31.2"}}},
    }
    return network, container


def start(monkeypatch, runner, network, container):
    calls = []

    def docker(argv, **kwargs):
        calls.append(argv)
        if argv[:1] == ["run"]:
            assert argv[argv.index("--network") + 1] == runner.NETWORK
            assert argv[argv.index("--name") + 1] == runner.RELAY
            return "owned-relay"
        if argv == ["logs", runner.RELAY]:
            return json.dumps({"derp": {"host": runner.RELAY, "derpPort": 31001, "stunPort": 31002, "insecureForTests": True}})
        if argv == ["network", "inspect", runner.NETWORK]:
            return json.dumps([network])
        if argv == ["container", "inspect", runner.RELAY]:
            return json.dumps([container])
        raise AssertionError(f"unexpected command: {argv}")

    def wait(probe, accept, **kwargs):
        value = probe()
        assert accept(value)
        return value

    monkeypatch.setattr(runner, "docker", docker)
    monkeypatch.setattr(runner, "wait_for", wait)
    runner.start_relay()
    return calls


def test_relay_uses_owned_inspected_ipv4_with_its_actual_dynamic_ports(monkeypatch):
    runner = load_runner()
    calls = start(monkeypatch, runner, *topology(runner))
    assert ["network", "inspect", runner.NETWORK] in calls
    assert ["container", "inspect", runner.RELAY] in calls
    assert runner._DERP == {"host": "172.29.31.2", "derpPort": 31001, "stunPort": 31002, "insecureForTests": True}
    assert json.loads(runner.container_environment(runner.NODES[0])["HYPRIAL_TAILCAT_DERP"])["host"] == "172.29.31.2"


@pytest.mark.parametrize("change", [
    "external-network", "host-driver", "network-label", "relay-label", "network-id",
    "relay-name", "foreign-container", "multiple-networks", "not-running", "outside-subnet",
])
def test_relay_rejects_unowned_or_mismatched_topology(monkeypatch, change):
    runner = load_runner()
    network, container = copy.deepcopy(topology(runner))
    if change == "external-network":
        network["Internal"] = False
    elif change == "host-driver":
        network["Driver"] = "host"
    elif change == "network-label":
        network["Labels"] = {}
    elif change == "relay-label":
        container["Config"]["Labels"] = {}
    elif change == "network-id":
        container["NetworkSettings"]["Networks"][runner.NETWORK]["NetworkID"] = "other-network"
    elif change == "relay-name":
        container["Name"] = "/other-container"
    elif change == "foreign-container":
        network["Containers"]["owned-relay"]["Name"] = "other-container"
    elif change == "multiple-networks":
        container["NetworkSettings"]["Networks"]["other-network"] = {}
    elif change == "not-running":
        container["State"]["Running"] = False
    elif change == "outside-subnet":
        network["IPAM"]["Config"] = [{"Subnet": "10.42.0.0/16"}]
    with pytest.raises(AssertionError, match="owned|private|IPv4|network|relay|bridge"):
        start(monkeypatch, runner, network, container)


@pytest.mark.parametrize("host", ["localhost", "hyprial-e2e001-relay", "1.1.1.1", "127.0.0.1", "169.254.1.2", "0.0.0.0", "::1"])
def test_container_configuration_refuses_dns_or_non_private_bridge_ipv4(host):
    runner = load_runner()
    runner._DERP.update({"host": host, "derpPort": 31001, "stunPort": 31002, "insecureForTests": True})
    with pytest.raises(AssertionError, match="private|IPv4|relay"):
        runner.container_environment(runner.NODES[0])
