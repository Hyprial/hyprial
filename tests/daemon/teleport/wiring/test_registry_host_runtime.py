"""``RegistryTargetHost.start``/``stop`` and a full receive over a real registry.

No harness binary exists on the development machine, so the daemon's
``lifecycle.start`` and ``down`` entries are replaced by recording callables.
What this pins: the params the host sends, how failures map to codes, that a
missing collaborator is refused instead of faked, and that the service undoes a
failed start against a real registry and home (nothing left behind).
What it cannot pin: that the real lifecycle accepts these params and that a
real harness resumes on the landed home (protocol section 8, UNKNOWN).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.identity.impl.agents.registry._core import AgentRegistry

from ..stream.samples import stream
from ..support import teleport
from ..target.support import (
    AGENT,
    NODE,
    OWNER,
    SESSION,
    error_code,
    feed,
    Packed,
    manifest_digest,
    prepare_request,
    receive_request,
    staging_entries,
    target,
)
from .test_registry_host import _bundle

WIRING = "hyprial.daemon.impl.teleport.wiring.target"

_OPENED: list[AgentRegistry] = []


@pytest.fixture(autouse=True)
def _close_registries():
    yield
    while _OPENED:
        _OPENED.pop().close()


def _packed(tmp_path: Path) -> Packed:
    bundle = _bundle(tmp_path, name="stream-bundle")
    result = stream().pack_bundle(bundle, tmp_path / "stream-bundle.tar")
    return Packed(bundle, result.path, result.total, result.sha256, manifest_digest(bundle))


class Recorder:
    def __init__(self, result=None, failure: BaseException | None = None) -> None:
        self.result = result
        self.failure = failure
        self.calls: list[dict] = []

    def __call__(self, params: dict):
        self.calls.append(dict(params))
        if self.failure is not None:
            raise self.failure
        return self.result


def _host(tmp_path: Path, **collaborators):
    import importlib

    home = tmp_path / "target-home"
    home.mkdir()
    registry = AgentRegistry(home / "agents.sqlite3", owner=OWNER, machine=NODE, hyprial_home=home)
    _OPENED.append(registry)
    host = importlib.import_module(WIRING).RegistryTargetHost(
        node_id=NODE,
        owner=OWNER,
        registry=registry,
        hyprial_home=home,
        runtime_available=lambda _harness: True,
        **collaborators,
    )
    return host, registry, home


def test_start_sends_the_resume_launch_and_returns_the_established_ref(tmp_path: Path) -> None:
    launch = Recorder(result={"ok": True, "sessionRef": SESSION})
    host, _registry, _home = _host(tmp_path, launch=launch)
    landed = host.land(_bundle(tmp_path), name=AGENT, harness="claude")
    assert host.start(landed, args=("--model", "m-1"), session_ref=SESSION, timeout=5.0) == SESSION
    (params,) = launch.calls
    assert params["provider"] == "claude"
    assert params["name"] == AGENT
    assert params["headless"] is True
    assert params["args"] == ["--model", "m-1"]
    assert params["cwd"] == str(landed.cwd)
    assert params["sessionRef"] == SESSION
    assert params["operationId"].startswith("teleport-start:")


def test_start_without_a_session_ref_sends_none_and_echoes_none(tmp_path: Path) -> None:
    launch = Recorder(result={"ok": True})
    host, _registry, _home = _host(tmp_path, launch=launch)
    landed = host.land(_bundle(tmp_path), name=AGENT, harness="claude")
    assert host.start(landed, args=(), session_ref=None, timeout=5.0) is None
    assert "sessionRef" not in launch.calls[0]


def test_start_failures_map_to_codes(tmp_path: Path) -> None:
    error = teleport().TeleportError
    host, _registry, _home = _host(tmp_path, launch=Recorder(failure=RuntimeError("no resume")))
    landed = host.land(_bundle(tmp_path), name=AGENT, harness="claude")
    assert error_code(host.start, landed, args=(), session_ref=SESSION, timeout=1.0) == (
        "TELEPORT_STRICT_RESUME_FAILED"
    )
    keep = Recorder(failure=error("TELEPORT_AGENT_EXISTS", "kept"))
    host._launch = keep
    assert error_code(host.start, landed, args=(), session_ref=None, timeout=1.0) == (
        "TELEPORT_AGENT_EXISTS"
    )


def test_missing_collaborators_are_refused_not_faked(tmp_path: Path) -> None:
    host, _registry, _home = _host(tmp_path)
    landed = host.land(_bundle(tmp_path), name=AGENT, harness="claude")
    assert error_code(host.start, landed, args=(), session_ref=None, timeout=1.0) == (
        "TELEPORT_RUNTIME_UNSUPPORTED"
    )
    assert error_code(host.stop, landed) == "TELEPORT_RUNTIME_UNSUPPORTED"


def test_stop_sends_the_down_request(tmp_path: Path) -> None:
    down = Recorder(result={"ok": True})
    host, _registry, _home = _host(tmp_path, down=down)
    landed = host.land(_bundle(tmp_path), name=AGENT, harness="codex")
    host.stop(landed)
    assert down.calls == [{"target": AGENT, "provider": "codex"}]


def test_service_receive_over_a_real_registry(tmp_path: Path) -> None:
    launch = Recorder(result={"ok": True, "sessionRef": SESSION})
    down = Recorder(result={"ok": True})
    host, registry, home = _host(tmp_path, launch=launch, down=down)
    receiver = teleport().StagingReceiver(teleport().staging_root(home))
    service = target().TeleportTargetService(host=host, receiver=receiver)
    packed = _packed(tmp_path)
    service.prepare(prepare_request("op-1"))
    feed(service, "op-1", packed)
    reply = service.receive(receive_request("op-1", packed))
    assert reply.agent_uri == registry.require(AGENT).uri
    assert reply.session_ref == SESSION
    assert registry.exists(AGENT) is True
    assert down.calls == []
    assert service.state("op-1") == "received"


def test_service_undoes_a_failed_start_against_a_real_registry(tmp_path: Path) -> None:
    launch = Recorder(failure=RuntimeError("worker never became ready"))
    down = Recorder(result={"ok": True})
    host, registry, home = _host(tmp_path, launch=launch, down=down)
    receiver = teleport().StagingReceiver(teleport().staging_root(home))
    service = target().TeleportTargetService(host=host, receiver=receiver)
    packed = _packed(tmp_path)
    service.prepare(prepare_request("op-1"))
    feed(service, "op-1", packed)
    code = error_code(service.receive, receive_request("op-1", packed))
    assert code == "TELEPORT_STRICT_RESUME_FAILED"
    assert down.calls == [{"target": AGENT, "provider": "claude"}]
    assert registry.exists(AGENT) is False
    assert not (home / "agents" / AGENT).exists()
    assert service.state("op-1") is None
    assert staging_entries(receiver) == []
