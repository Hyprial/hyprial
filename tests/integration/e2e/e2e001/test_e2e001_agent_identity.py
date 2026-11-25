"""Docker mesh identities come from agent creation and survive offline phases."""

import pytest

from tests.integration.e2e.e2e001.test_e2e001_container_owner import load_runner


def test_driver_uses_the_published_id_and_keeps_it_while_peer_is_offline(monkeypatch):
    runner = load_runner()
    node = runner.NODES[0]
    uri = f"agent:fixture-owner:{node}:a.aaaaaaaaaaaaaaaaaaaaaaaaaa"

    def command(actual_node, *args, **kwargs):
        assert actual_node == node
        if args == ("agent", "create", "--name", runner.DRIVER):
            return {"ok": True, "agent": {"actor": runner.DRIVER, "uri": uri}}
        if args == ("ps",):
            return {"daemon": {"owner": "fixture-owner"}}
        raise AssertionError(f"unexpected identity query: {args}")

    monkeypatch.setattr(runner, "exec_json", command)
    runner.register_driver(node)
    assert runner.driver_uri(node) == uri

    def offline(*args, **kwargs):
        raise AssertionError("offline peer must not be queried for its stable identity")

    monkeypatch.setattr(runner, "exec_json", offline)
    assert runner.driver_uri(node) == uri


@pytest.mark.parametrize("uri", [None, "agent:fixture-owner:hyprial-e2e001-a:legacy-driver"])
def test_driver_creation_refuses_a_missing_or_retired_address(monkeypatch, uri):
    runner = load_runner()

    def command(node, *args, **kwargs):
        if args == ("ps",):
            return {"daemon": {"owner": "fixture-owner"}}
        return {"ok": True, "agent": {"actor": runner.DRIVER, "uri": uri}}

    monkeypatch.setattr(runner, "exec_json", command)
    with pytest.raises(AssertionError, match="id address"):
        runner.register_driver(runner.NODES[0])
