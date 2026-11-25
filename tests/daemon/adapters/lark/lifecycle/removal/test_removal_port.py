"""The production Lark port carries removal custody through the actor mailbox."""

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from hyprial.daemon.impl.adapters.lark.runtime.runtime import AdapterRuntime
from hyprial.daemon.impl.composition.events import CorrelatedDomainEvents, DomainCommandError
from hyprial.daemon.impl.composition.ports import LarkPortClient
from hyprial.kernel import ChannelConfiguration
from tests.daemon.adapters.lark.lifecycle.test_lark_lifecycle_guards import _Launcher, _gateway


@pytest.fixture
def running():
    launcher = _Launcher()
    events = CorrelatedDomainEvents()
    channels = ChannelConfiguration(gateways=(_gateway("app"),))
    runtime = AdapterRuntime(
        launcher, channels, event_sink=events, configuration_source=lambda: channels,
    )
    client = LarkPortClient(runtime, events, timeout=3)
    client.start("app")
    try:
        yield runtime, client, launcher
    finally:
        runtime.stop()


def begin(runtime, client, token="proof", **overrides):
    expected = {
        "expected_generation": runtime.generation,
        "expected_version": client.read_adapter("app").version,
    }
    expected.update(overrides)
    return client.begin_removal("app", token, **expected)


def test_port_removal_fences_start_until_matching_commit(running):
    runtime, client, launcher = running
    result = begin(runtime, client)
    assert not result.adapter.process_running
    assert launcher.processes[0].stopped
    with pytest.raises(DomainCommandError, match="removal"):
        client.start("app")
    with pytest.raises(DomainCommandError, match="does not match"):
        client.finish_removal("app", "wrong", committed=True)
    with pytest.raises(DomainCommandError, match="removal"):
        client.reload()
    client.finish_removal("app", "proof", committed=True)
    assert client.read_adapter("app") is None
    assert launcher.calls == 1


@pytest.mark.parametrize("field", ["expected_generation", "expected_version"])
def test_stale_preview_runtime_fence_refuses_before_stop(running, field):
    runtime, client, launcher = running
    with pytest.raises(DomainCommandError) as refused:
        begin(runtime, client, **{field: 100000})
    assert refused.value.code == "PREVIEW_STALE"
    assert not launcher.processes[0].stopped
    assert client.read_adapter("app").process_running
    assert runtime._authority.removals == {}


@pytest.mark.parametrize("token", ["", "x" * 129, None])
def test_invalid_removal_token_has_no_process_effect(running, token):
    runtime, client, launcher = running
    with pytest.raises(DomainCommandError) as refused:
        begin(runtime, client, token)
    assert refused.value.code == "INVALID_ARGUMENT"
    assert not launcher.processes[0].stopped
    assert runtime._authority.removals == {}


def test_abort_releases_custody_without_starting_the_adapter(running):
    runtime, client, launcher = running
    begin(runtime, client)
    client.finish_removal("app", "proof", committed=False)
    assert client.read_adapter("app").configured
    assert not client.read_adapter("app").process_running
    assert launcher.calls == 1
    client.start("app")
    assert launcher.calls == 2


def test_invalid_completion_keeps_the_removal_fence(running):
    runtime, client, launcher = running
    begin(runtime, client)
    with pytest.raises(DomainCommandError) as refused:
        client.finish_removal("app", "proof", committed="false")
    assert refused.value.code == "INVALID_ARGUMENT"
    with pytest.raises(DomainCommandError, match="removal"):
        client.start("app")
    client.finish_removal("app", "proof", committed=False)
    assert launcher.calls == 1


def test_timed_out_caller_does_not_release_or_replay_stop(running):
    runtime, client, launcher = running
    entered = threading.Event()
    release = threading.Event()
    calls = []
    process = launcher.processes[0]
    original_stop = process.stop

    def gated_stop(timeout=5):
        calls.append("stop")
        entered.set()
        assert release.wait(5)
        original_stop(timeout)

    process.stop = gated_stop
    impatient = LarkPortClient(runtime, client._events, timeout=0.05)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(begin, runtime, impatient)
        try:
            assert entered.wait(2)
            with pytest.raises(DomainCommandError) as refused:
                pending.result(timeout=2)
            assert refused.value.code == "DOMAIN_COMMAND_TIMEOUT"
            with pytest.raises(DomainCommandError, match="unsettled"):
                client.finish_removal("app", "proof", committed=True)
            with pytest.raises(DomainCommandError, match="removal"):
                client.start("app")
        finally:
            release.set()
    deadline = time.monotonic() + 3
    while not runtime._authority.removals["app"].settled and time.monotonic() < deadline:
        runtime.status("app")
        time.sleep(0.005)
    assert runtime._authority.removals["app"].settled
    client.finish_removal("app", "proof", committed=True)
    assert client.read_adapter("app") is None
    assert calls == ["stop"]
