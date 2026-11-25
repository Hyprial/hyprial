"""Slice 6 wiring: the real ``TargetRoutines`` adapter and its daemon hookup.

Contract pinned here (module ``hyprial.daemon.impl.teleport.wiring.routines``):

- ``rewrite_agent_uri(yaml_text, *, source_uri, target_uri) -> str`` replaces
  a value equal to ``source_uri`` in the agent slots a routine spec has
  (``actor``, ``produces``, ``policy.routes[*].escalate_to``,
  ``on_task_timeout.escalate_to``, the same slots as
  ``biz/impl/routine/storage/agent_uris.py``) with ``target_uri``.  Any other
  value, other agents' URIs and free text that merely mentions the source URI
  stay as they are; text with nothing to rewrite is returned byte for byte.
- ``AppTargetRoutines(app)`` reads ``app._routine_service`` (a
  ``RoutineServicePort``) on every call, like ``AppSourceRoutines``:
  ``create_paused`` stores the rewritten YAML through ``add(...,
  enabled=False)`` with owner ``legacy_user_uri(app.owner)`` (the owner the
  daemon gives the default agent routine) and returns the stored name; it
  never resumes or dispatches.  ``existing(names)`` returns the given names
  that are stored, in the given order.  ``resume`` and ``remove`` map to the
  port.  Without a routine runtime every method raises ``TeleportError`` with
  ``TELEPORT_RUNTIME_UNSUPPORTED``.
- ``DaemonApplication._start_teleport_target`` passes
  ``routines=AppTargetRoutines(self)`` to ``start_teleport_target``.

The real routine service is the slice 1 fixture (``RoutineService`` with a
fake PAC in a temp state dir) behind the production ``RoutineServiceAdapter``.
"""

from __future__ import annotations

import importlib
import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.teleport import TeleportError
from hyprial.shell.impl.daemon_wiring import daemon_dependencies
from hyprial.shell.impl.daemon_wiring.routine_service import RoutineServiceAdapter
from tests.biz.routines.teleport.support import (
    ALPHA,
    BETA,
    Clock,
    FakePac,
    make_service,
    produced_source_yaml,
    scheduled_yaml,
    wait_for,
)

from ..support import teleport
from ..target.support import RecordingSession, key, prepare_request, protocol

TARGET = "agent:o:m:alpha"
NODE = "node-teleport"
OWNER = "owner"


def routines_module():
    return importlib.import_module("hyprial.daemon.impl.teleport.wiring.routines")


def rewrite(text: str) -> str:
    return routines_module().rewrite_agent_uri(text, source_uri=ALPHA, target_uri=TARGET)


def test_rewrite_replaces_the_source_agent_in_every_agent_slot() -> None:
    text = produced_source_yaml("selfdrive", produces=ALPHA, escalate_to=ALPHA)
    document = yaml.safe_load(rewrite(text))
    assert document["produces"] == TARGET
    assert document["on_task_timeout"]["escalate_to"] == TARGET
    routes = document["policy"]["routes"]
    assert routes[0] == {"tag": "route:self", "target": "self"}
    assert routes[1]["escalate_to"] == TARGET
    assert document["policy"]["task_template"] == "{{task.description}} {{nonce}}"


def test_rewrite_leaves_other_agents_and_free_text_alone() -> None:
    document = yaml.safe_load(scheduled_yaml("beat", actor=ALPHA, escalate_to=BETA))
    document["task"] = f"Report to {ALPHA} when done."
    rewritten = yaml.safe_load(rewrite(yaml.safe_dump(document, sort_keys=False)))
    assert rewritten["actor"] == TARGET
    assert rewritten["on_task_timeout"]["escalate_to"] == BETA
    assert rewritten["task"] == f"Report to {ALPHA} when done."
    assert {k: v for k, v in rewritten.items() if k != "actor"} == {
        k: v for k, v in document.items() if k != "actor"
    }


def test_rewrite_without_a_match_returns_the_text_unchanged() -> None:
    text = "# kept\n" + scheduled_yaml("other", actor=BETA, escalate_to=BETA)
    assert rewrite(text) == text


@pytest.fixture
def real(tmp_path: Path):
    clock = Clock()
    pac = FakePac()
    service = make_service(tmp_path, pac=pac, clock=clock)
    app = SimpleNamespace(owner="owner-1", _routine_service=RoutineServiceAdapter(service))
    try:
        yield SimpleNamespace(app=app, pac=pac, clock=clock, service=service)
    finally:
        service.close()


def _tick(real, times: int) -> None:
    for _ in range(times):
        real.clock.now_ms += 3_600_000
        real.service.submit_timer()


def test_create_paused_stores_the_rewritten_yaml_disabled_without_dispatch(real) -> None:
    adapter = routines_module().AppTargetRoutines(real.app)
    text = scheduled_yaml("moved", actor=ALPHA, escalate_to=BETA)
    name = adapter.create_paused(yaml_text=text, source_uri=ALPHA, target_uri=TARGET)
    assert name == "moved"
    exported = real.app._routine_service.export(name="moved")
    assert exported["enabled"] is False
    assert exported["owner"] == "user:owner-1"
    stored = yaml.safe_load(exported["yaml"])
    assert stored["actor"] == TARGET
    assert stored["on_task_timeout"]["escalate_to"] == BETA
    _tick(real, 3)
    real.service.add(yaml_text=scheduled_yaml("probe", actor=BETA), owner="user:op")
    _tick(real, 1)
    wait_for(lambda: "probe" in real.pac.starts)
    assert real.service.status(name="moved")["enabled"] is False
    assert real.service.status(name="moved")["inFlight"] == []
    assert "moved" not in real.pac.starts
    assert real.pac.rearm_calls == []


def test_existing_resume_and_remove_reach_the_routine_service(real) -> None:
    adapter = routines_module().AppTargetRoutines(real.app)
    adapter.create_paused(
        yaml_text=scheduled_yaml("moved", actor=ALPHA), source_uri=ALPHA, target_uri=TARGET
    )
    assert adapter.existing(["absent", "moved"]) == ["moved"]
    adapter.resume("moved")
    assert real.app._routine_service.export(name="moved")["enabled"] is True
    adapter.remove("moved")
    assert adapter.existing(["moved"]) == []


def test_without_a_routine_runtime_every_call_is_unsupported() -> None:
    adapter = routines_module().AppTargetRoutines(
        SimpleNamespace(owner="owner-1", _routine_service=None)
    )
    calls = [
        lambda: adapter.existing(["x"]),
        lambda: adapter.create_paused(yaml_text="name: x\n", source_uri=ALPHA, target_uri=TARGET),
        lambda: adapter.resume("x"),
        lambda: adapter.remove("x"),
    ]
    for call in calls:
        with pytest.raises(TeleportError) as failure:
            call()
        assert failure.value.code == "TELEPORT_RUNTIME_UNSUPPORTED"


def _application(tmp_path: Path) -> DaemonApplication:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        endpoint = f"tcp/127.0.0.1:{probe.getsockname()[1]}"
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
        hyprial_home=tmp_path / "home",
        zenoh_listen=(endpoint,),
        zenoh_connect=(),
        **daemon_dependencies(),
    )
    app.network_isolated = True
    return app


def test_daemon_target_consults_its_routine_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request
) -> None:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    fake = binaries / "claude"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ.get('PATH', '')}")
    app = _application(tmp_path / "daemon")
    service = make_service(tmp_path / "routines")

    def close() -> None:
        app._close()
        app._close_home_lease()
        service.close()

    request.addfinalizer(close)
    service.add(yaml_text=scheduled_yaml("taken", actor=BETA), owner="user:op")
    app._routine_service = RoutineServiceAdapter(service)
    session = RecordingSession()
    app._start_teleport_target(session)
    assert isinstance(app._teleport_target.service.routines, routines_module().AppTargetRoutines)

    def prepare(operation_id: str, names: tuple[str, ...]) -> dict:
        k = key("prepare", operation_id, node=NODE)
        message = prepare_request(
            operation_id, target_node=NODE, owner=OWNER, agent="fresh",
            routines=names, dry_run=True,
        )
        replies = session.handler()(k, protocol().encode_prepare_request(message))
        return teleport().decode_reply(replies[0][1]).header

    assert prepare("op-taken", ("free", "taken"))["error"]["code"] == "TELEPORT_ROUTINE_CONFLICT"
    assert prepare("op-free", ("free",))["ok"] is True
