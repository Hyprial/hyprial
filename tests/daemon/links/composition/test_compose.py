"""WP2-B3 composition: compose, bind, events, shutdown ordering, top.

Spec: docs/design/send-links-impl/wp2-composition.md §8.1, §10.2.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.composition.links import (
    BYPASS_ENV,
    LINK_SERVICE_CLOSE_TIMEOUT_S,
    WINDOW_MODES,
    close_link_service,
    compose_link_service,
    link_top_fields,
    link_window_mode,
)
from hyprial.daemon.impl.links.ports import (
    TOP_FIELD_CAPACITY_W,
    Mint,
    TOP_FIELD_OUTBOUND_IN_FLIGHT,
    Hold,
)
from hyprial.daemon.impl.links.service import LinkService
from hyprial.daemon.impl.operations.top import build_top_snapshot
from hyprial.daemon.impl.pac.actors.daemon import DaemonPacNotificationSender
from hyprial.kernel import HookBus, ipc_errors
from tests.support.links.app import OrgFsWithMeta, stub_app
from tests.support.links.gate import PermissiveLinkGate
from tests.support.links.ledger import FakeInboxPort
from tests.support.links.org import FakeClock, LogRecorder, establish, make_service

A = "agent:o:n1:a"
B = "agent:o:n1:b"


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("lane event did not arrive in time")


class _RecordingLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def __call__(self, app, level: str, event: str, **fields: object) -> None:
        self.events.append((event, {"level": level, **fields}))

    def named(self, event: str) -> list[dict[str, object]]:
        return [fields for name, fields in self.events if name == event]


def _org_fs(*orgs: str) -> OrgFsWithMeta:
    fs = OrgFsWithMeta()
    for index, org in enumerate(orgs or ("group-test",)):
        fs.add_space(f"sp-{index}", name=org)
    return fs


class _BadLedger(FakeInboxPort):
    def ledger_snapshot(self, now_ms: int):
        raise RuntimeError("ledger down")


class _Hooks:
    def __init__(self, bus: HookBus | None = None) -> None:
        self.bus = bus
        self.names: list[str] = []

    def register_consumer(self, name, observer, events):
        self.names.append(name)
        if self.bus is not None:
            self.bus.register(name, observer, events)


class _Lane:
    def __init__(self) -> None:
        self.events: list[object] = []

    def observe(self, event: object) -> None:
        self.events.append(event)


# -- C3 / C4 --------------------------------------------------------------


def test_start_failure_is_contained_and_leaves_nothing(tmp_path: Path) -> None:
    fs = _org_fs()
    hooks = _Hooks()
    logs = LogRecorder()
    app = stub_app(tmp_path, fs=fs, _daemon_hooks=hooks, _log=logs)

    service = compose_link_service(app, None, fs=fs, inbox_port=_BadLedger())

    assert service is None
    assert app.link_admission.bound is False
    failed = logs.fields_for("link.service_failed")
    assert failed and failed[0]["stage"] == "start"
    assert hooks.names == []
    assert all(space.watches == [] for space in fs._spaces.values())


def test_orgfs_failure_is_contained(tmp_path: Path) -> None:
    logs = LogRecorder()
    app = stub_app(
        tmp_path,
        _log=logs,
        _require_orgfs_runtime=lambda: (_ for _ in ()).throw(RuntimeError("no orgfs")),
    )

    assert compose_link_service(app, None) is None
    assert app.link_admission.bound is False
    failed = logs.fields_for("link.service_failed")
    assert failed and failed[0]["stage"] == "orgfs"


def test_bound_slot_is_skipped_without_raising(tmp_path: Path) -> None:
    gate = PermissiveLinkGate()
    logs = LogRecorder()
    app = stub_app(tmp_path, bound_gate=gate, _log=logs)

    assert compose_link_service(app, None) is None
    assert getattr(app, "_link_service", None) is None
    skipped = logs.fields_for("link.service_skipped")
    assert skipped and skipped[0]["reason"] == "slot-bound"
    assert skipped[0]["gateType"] == "PermissiveLinkGate"


# -- C6 -------------------------------------------------------------------


def test_composition_publishes_link_events_on_the_real_bus(tmp_path: Path) -> None:
    fs = _org_fs()
    bus = HookBus()
    app = stub_app(
        tmp_path, fs=fs, _hook_bus=bus, _daemon_hooks=_Hooks(bus)
    )
    clock = FakeClock()
    service = compose_link_service(
        app, None, fs=fs, inbox_port=FakeInboxPort(), clock_ms=clock
    )
    assert isinstance(service, LinkService)
    lane = _Lane()
    bus.register("test-links", lane, ("link.minted", "link.reclaimed"))

    establish(fs, A, B, now_ms=clock())
    clock.advance(1)
    service.tick(clock())
    _wait_for(lambda: len([e for e in lane.events if e.event == "link.minted"]) == 2)
    minted = [
        event for event in lane.events if event.event == "link.minted"
    ]
    # Both endpoints are local agents, so each actor's daemon view emits once.
    assert {event.actor for event in minted} == {A, B}
    assert all(event.source == "daemon.links" for event in minted)
    assert set(minted[0].payload) >= {
        "peer", "lifecycle", "taskId", "revision", "mintedAtMs",
        "atMs", "side",
    }

    service.drop(actor=A, peer=B, now_ms=clock())
    clock.advance(1)
    service.tick(clock())
    _wait_for(
        lambda: len([e for e in lane.events if e.event == "link.reclaimed"]) >= 1
    )
    reclaimed = [
        event for event in lane.events if event.event == "link.reclaimed"
    ]
    assert A in {event.actor for event in reclaimed}
    assert all(event.source == "daemon.links" for event in reclaimed)

    close_link_service(app)


# -- C7 -------------------------------------------------------------------


def test_close_detaches_the_lane_and_is_idempotent(tmp_path: Path) -> None:
    fs = _org_fs()
    bus = HookBus()
    app = stub_app(tmp_path, fs=fs, _hook_bus=bus, _daemon_hooks=_Hooks(bus))
    service = compose_link_service(
        app, None, fs=fs, inbox_port=FakeInboxPort()
    )
    assert service is not None
    kicks: list[int] = []
    service.kick = lambda: kicks.append(1)  # type: ignore[method-assign]

    close_link_service(app)

    assert app._link_service is None
    assert app._link_task_binder is None
    assert app._link_hook_lane is None
    assert service._thread is None or not service._thread.is_alive()
    assert all(space.watches == [] for space in fs._spaces.values())

    from hyprial.kernel import HookEventPublisher

    publisher = HookEventPublisher(bus, source="daemon.pac")
    publisher.publish("graph.closed", payload={"graphId": "g1"})
    time.sleep(0.2)
    assert kicks == []  # the lane was detached
    close_link_service(app)  # idempotent


def test_close_reports_a_wedged_service(tmp_path: Path) -> None:
    fs = _org_fs()
    app = stub_app(tmp_path, fs=fs)
    service = compose_link_service(app, None, fs=fs, inbox_port=FakeInboxPort())
    assert service is not None
    real_close = service.close
    service.close = lambda timeout=LINK_SERVICE_CLOSE_TIMEOUT_S: False  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError):
            close_link_service(app)
    finally:
        assert real_close()


# -- C9 -------------------------------------------------------------------


def test_top_fields_render_and_never_touch_the_gate(tmp_path: Path) -> None:
    statuses = (
        {"actor": A, "name": "a"},
        {"actor": B, "name": "b"},
    )
    snapshot = build_top_snapshot(
        state_dir=tmp_path / "state",
        owner="o",
        node_id="n1",
        epoch="e",
        daemon_pid=1,
        connectors=[],
        actor_statuses=list(statuses),
        pending={},
        stranded=[],
        now_ms=1,
        link_fields={A: {TOP_FIELD_CAPACITY_W: 3, TOP_FIELD_OUTBOUND_IN_FLIGHT: 1}},
    )
    rows = {row["actor"]: row for row in snapshot["actors"]}
    assert rows[A][TOP_FIELD_CAPACITY_W] == 3
    assert rows[A][TOP_FIELD_OUTBOUND_IN_FLIGHT] == 1
    assert rows[B][TOP_FIELD_CAPACITY_W] is None
    assert rows[B][TOP_FIELD_OUTBOUND_IN_FLIGHT] is None

    unwired = stub_app(tmp_path / "unwired")
    assert link_top_fields(unwired, statuses) == {
        A: {TOP_FIELD_CAPACITY_W: None, TOP_FIELD_OUTBOUND_IN_FLIGHT: None},
        B: {TOP_FIELD_CAPACITY_W: None, TOP_FIELD_OUTBOUND_IN_FLIGHT: None},
    }

    fs = _org_fs()
    app = stub_app(tmp_path / "wired", fs=fs)
    service = compose_link_service(app, None, fs=fs, inbox_port=FakeInboxPort())
    assert service is not None
    calls: list[str] = []
    service.admit_send = lambda **kwargs: calls.append("send")  # type: ignore[method-assign]
    service.admit_dispatch = lambda **kwargs: calls.append("dispatch")  # type: ignore[method-assign]
    fields = link_top_fields(app, statuses)
    assert fields[A][TOP_FIELD_CAPACITY_W] == service.stats(A)["W"]
    assert link_top_fields(app, statuses) == fields
    assert calls == []
    close_link_service(app)


# -- C10 ------------------------------------------------------------------


class _Delivery:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    def deliver(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("delivery down")
        return SimpleNamespace(message_id="m-delivered")


def test_pac_sender_calls_the_binder_around_delivery(tmp_path: Path) -> None:
    order: list[str] = []
    app = stub_app(tmp_path)

    def binder(*, sender, recipient, conversation_id):
        order.append("binder")

        def callback(message_id: str) -> None:
            order.append(f"callback:{message_id}")

        return callback

    app._link_task_binder = binder
    app._pac_notification_io = _Delivery()
    sender = DaemonPacNotificationSender(app)

    assert sender.send(
        recipient=B,
        sender=A,
        text="hi",
        conversation_id="pac-g1",
        idempotency_key="k1",
    summary="hi",
    ) == "m-delivered"
    assert order == ["binder", "callback:m-delivered"]

    order.clear()
    app._pac_notification_io = _Delivery(fail=True)
    with pytest.raises(RuntimeError):
        DaemonPacNotificationSender(app).send(
            recipient=B,
            sender=A,
            text="hi",
            conversation_id="pac-g1",
            idempotency_key="k2",
        summary="hi",
        )
    assert order == ["binder"]


def test_pac_sender_without_a_binder_is_unchanged(tmp_path: Path) -> None:
    app = stub_app(tmp_path)
    delivery = _Delivery()
    app._pac_notification_io = delivery
    assert DaemonPacNotificationSender(app).send(
        recipient=B,
        sender=A,
        text="hi",
        conversation_id="pac-g1",
        idempotency_key="k1",
    summary="hi",
    ) == "m-delivered"
    assert len(delivery.calls) == 1


# -- C1 / C2 / C5 (real startup) ------------------------------------------


def _probe(monkeypatch, tmp_path: Path):
    from hyprial.daemon.impl.application import DaemonApplication
    from hyprial.daemon.impl.application.netendpoints import endpoints as transport_owner
    from hyprial.daemon.impl.application.wiring import transport as wiring
    from tests.daemon.progress import test_progress_end_to_end as probe

    captured: dict[str, object] = {}

    class RecordingCoordinator(probe._CompositionInboxCoordinator):
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured["service_options"] = kwargs.get("service_options")
            super().__init__(*args, **kwargs)

    class RecordingRuntime(probe._CompositionRuntime):
        def __init__(self, **kwargs: object) -> None:
            captured["bridge_link_admission"] = kwargs.get("link_admission")
            super().__init__(**kwargs)

    monkeypatch.setattr(transport_owner, "ZenohTransport", probe._CompositionTransport)
    monkeypatch.setattr(
        transport_owner, "TransportSessionAuthority", lambda session: session
    )
    monkeypatch.setattr(wiring, "LivelinessDirectory", probe._CompositionDirectory)
    monkeypatch.setattr(wiring, "ZenohDeliveryTransport", probe._CompositionDelivery)
    monkeypatch.setattr(
        wiring, "LocalFirstDeliveryTransport", probe._CompositionDelivery
    )
    monkeypatch.setattr(wiring, "LarkReplyBridgeTransport", probe._CompositionBridge)
    monkeypatch.setattr(wiring, "DeliveryCustodyCoordinator", RecordingCoordinator)
    monkeypatch.setattr(
        wiring, "DeliveryCustodyFacade", lambda coordinator, _database: coordinator.inbox
    )
    monkeypatch.setattr(wiring, "AdapterRuntime", probe._CompositionAdapters)
    monkeypatch.setattr(wiring, "HarnessRuntimeActor", probe._CompositionHarnesses)
    monkeypatch.setattr(wiring, "HarnessPortClient", lambda actor, _events: actor)
    monkeypatch.setattr(wiring, "ZenohInboxEndpoint", probe._CompositionEndpoint)
    monkeypatch.setattr(wiring, "DeliveryStatusEndpoint", probe._CloseOnly)
    monkeypatch.setattr(wiring, "ReceiverUserDelivery", probe._CloseOnly)
    monkeypatch.setattr(wiring, "ZenohUserDeliveryEndpoint", probe._CloseOnly)
    monkeypatch.setattr(wiring, "ZenohUserDeliveryTransport", probe._CloseOnly)
    monkeypatch.setattr(wiring, "OrgContextMesh", probe._OrgEndpoint)
    monkeypatch.setattr(wiring, "DaemonEventBridge", RecordingRuntime)
    monkeypatch.setattr(
        DaemonApplication, "_start_lifecycle_manager", lambda self, **_kwargs: None
    )

    from hyprial.daemon.impl.composition import links as links_module
    from tests.support.links.ledger import FakeInboxPort

    monkeypatch.setattr(
        links_module,
        "_inbox_port_for",
        lambda app, identity: FakeInboxPort(),
    )

    base = Path(tmp_path)
    app = DaemonApplication(
        state_dir=base / "state",
        socket_path=base / "state" / "daemon.sock",
        node_id="node-1",
        hyprial_home=base / "home",
        owner="op",
        **_deep_module_daemon_dependencies(),
    )
    return app, captured, links_module


def _deep_module_daemon_dependencies():
    from hyprial.shell.impl.daemon_wiring import (
        daemon_dependencies as dependencies,
    )

    return dependencies()


def test_start_runtime_composes_binds_and_registers_the_lane(
    monkeypatch, tmp_path: Path
) -> None:
    app, captured, links_module = _probe(monkeypatch, tmp_path)
    logs = _RecordingLog()
    monkeypatch.setattr(links_module, "_log", logs)
    try:
        app._start_runtime()
        service = app._link_service
        assert app.link_admission.bound is True
        assert app.link_admission.gate is service
        assert isinstance(service, LinkService)
        assert callable(app._link_task_binder)
        composed = logs.named("link.service_composed")
        assert composed and composed[0]["orgAvailable"] is False
        assert composed[0]["skippedOrgSpaces"] == 0
        assert "links" in {lane.name for lane in app._hook_bus.projection()}
    finally:
        app._close()
        app.agents.close()


def test_start_runtime_skips_a_prebound_slot(monkeypatch, tmp_path: Path) -> None:
    app, _captured, links_module = _probe(monkeypatch, tmp_path)
    logs = _RecordingLog()
    monkeypatch.setattr(links_module, "_log", logs)
    app.link_admission.bind(PermissiveLinkGate())
    try:
        app._start_runtime()  # must not raise on the already-bound slot
        assert getattr(app, "_link_service", None) is None
        skipped = logs.named("link.service_skipped")
        assert skipped and skipped[0]["reason"] == "slot-bound"
        assert "links" not in {lane.name for lane in app._hook_bus.projection()}
    finally:
        app._close()
        app.agents.close()


def test_link_service_exists_before_pac_graph_authority(
    monkeypatch, tmp_path: Path
) -> None:
    from hyprial.daemon.impl.pac.graphs.authority import PacGraphAuthority

    app, _captured, links_module = _probe(monkeypatch, tmp_path)
    seen: dict[str, object] = {}
    original = PacGraphAuthority.__init__

    def patched(self, *args, **kwargs):
        sender = args[1]
        seen["service"] = getattr(sender.application, "_link_service", None)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(PacGraphAuthority, "__init__", patched)
    try:
        app._start_runtime()
        assert seen.get("service") is not None
        assert seen.get("service") is app._link_service
    finally:
        app._close()
        app.agents.close()


# -- C8 -------------------------------------------------------------------


def test_close_order_link_service_leaves_before_inbox_and_orgfs(
    monkeypatch, tmp_path: Path
) -> None:
    app, _captured, links_module = _probe(monkeypatch, tmp_path)
    order: list[str] = []

    def record(name, original):
        def wrapper(*args, **kwargs):
            order.append(name)
            return original(*args, **kwargs)

        return wrapper

    try:
        app._start_runtime()
        authority = app._pac_graph_authority
        hook_bus = app._hook_bus
        inbox = app._inbox
        orgfs = app._orgfs_runtime
        assert authority is not None

        monkeypatch.setattr(
            authority, "close", record("pac", authority.close)
        )
        monkeypatch.setattr(
            hook_bus, "close", record("bus", hook_bus.close)
        )
        monkeypatch.setattr(inbox, "shutdown", record("inbox", inbox.shutdown))
        monkeypatch.setattr(orgfs, "close", record("orgfs", orgfs.close))
        monkeypatch.setattr(
            links_module,
            "close_link_service",
            record("link", links_module.close_link_service),
        )
    finally:
        app._close()
        app.agents.close()

    assert order.index("pac") < order.index("link")
    assert order.index("link") < order.index("bus")
    assert order.index("link") < order.index("inbox")
    assert order.index("link") < order.index("orgfs")


# -- integration sanity: make_service still works with the new helper ------


def test_make_service_helper_is_untouched() -> None:
    harness = make_service(fs=None)
    assert isinstance(harness.service, LinkService)


# -- C11: nothing fallible follows bind (ruling 26 / 69 G4) ----------------


@pytest.mark.parametrize("failing", ["binder", "describe"])
def test_failure_before_bind_completes_leaves_slot_and_app_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    import hyprial.daemon.impl.composition.links as composition

    fs = _org_fs()
    hooks = _Hooks()
    logs = LogRecorder()
    app = stub_app(tmp_path, fs=fs, _daemon_hooks=hooks, _log=logs)
    if failing == "binder":

        def boom(app, service):
            raise RuntimeError("binder down")

        monkeypatch.setattr(composition, "pac_task_binder", boom)
    else:
        # Fail only on the composing thread once start() has returned.
        original = composition.LinkStore.index
        started = threading.Event()
        composing = threading.get_ident()
        real_start = composition.LinkService.start

        def start(self):
            real_start(self)
            started.set()

        def index(self):
            if started.is_set() and threading.get_ident() == composing:
                raise RuntimeError("index down")
            return original(self)

        monkeypatch.setattr(composition.LinkService, "start", start)
        monkeypatch.setattr(composition.LinkStore, "index", index)

    assert compose_link_service(app, None, fs=fs, inbox_port=FakeInboxPort()) is None

    assert app.link_admission.bound is False
    assert getattr(app, "_link_service", None) is None
    assert getattr(app, "_link_task_binder", None) is None
    assert hooks.names == []
    failed = logs.fields_for("link.service_failed")
    assert failed and failed[0]["stage"] == failing
    assert logs.fields_for("link.service_composed") == []


# -- H1: the links.windowMode operator switch -----------------------------


def test_h1_window_mode_is_observe_by_default(tmp_path: Path) -> None:
    assert link_window_mode(tmp_path / "home") == "observe"
    assert WINDOW_MODES == ("observe", "enforce")


def test_h1_window_mode_reads_settings_and_the_environment(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps({"links": {"windowMode": "enforce"}}), encoding="utf-8"
    )
    assert link_window_mode(home) == "enforce"
    assert link_window_mode(home, {}) == "enforce"
    assert link_window_mode(home, {"HYPRIAL_LINKS_WINDOW_MODE": "observe"}) == "observe"
    with pytest.raises(ValueError):
        link_window_mode(home, {"HYPRIAL_LINKS_WINDOW_MODE": "maybe"})
    (home / "settings.json").write_text(
        json.dumps({"links": {"windowMode": "maybe"}}), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        link_window_mode(home, {})


def test_h1_links_section_without_window_mode_defaults_to_observe(
    tmp_path: Path,
) -> None:
    """A partial ``links`` section is a valid configuration.

    A section carrying only ``bypass``, an empty section, or a null
    ``windowMode`` all mean the default mode, exactly like an absent
    section; only a present invalid value fails closed.
    """

    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps({"links": {"bypass": True}}), encoding="utf-8"
    )
    assert link_window_mode(home) == "observe"
    (home / "settings.json").write_text(
        json.dumps({"links": {"windowMode": None}}), encoding="utf-8"
    )
    assert link_window_mode(home) == "observe"
    (home / "settings.json").write_text(
        json.dumps({"links": {}}), encoding="utf-8"
    )
    assert link_window_mode(home) == "observe"


def test_h1_compose_succeeds_with_bypass_only_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Incident: ``{"links": {"bypass": true}}`` without ``windowMode``
    failed composition (``link.service_failed stage=construct``) and held
    every send.  Bypass-only settings must compose with the default window
    mode and the emergency switch on.
    """

    monkeypatch.delenv("HYPRIAL_LINKS_WINDOW_MODE", raising=False)
    monkeypatch.delenv(BYPASS_ENV, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps({"links": {"bypass": True}}), encoding="utf-8"
    )
    fs = _org_fs()
    app = stub_app(tmp_path, fs=fs)
    service = compose_link_service(app, None, fs=fs, inbox_port=FakeInboxPort())
    try:
        assert isinstance(service, LinkService)
        assert service.window_mode == "observe"
        assert service.bypass is True
    finally:
        close_link_service(app)


def test_h1_compose_passes_the_configured_window_mode_to_the_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HYPRIAL_LINKS_WINDOW_MODE", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps({"links": {"windowMode": "enforce"}}), encoding="utf-8"
    )
    fs = _org_fs()
    app = stub_app(tmp_path, fs=fs)
    service = compose_link_service(app, None, fs=fs, inbox_port=FakeInboxPort())
    assert isinstance(service, LinkService)
    assert service.window_mode == "enforce"
    close_link_service(app)


def test_h1_compose_defaults_to_observe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HYPRIAL_LINKS_WINDOW_MODE", raising=False)
    fs = _org_fs()
    app = stub_app(tmp_path, fs=fs)
    service = compose_link_service(app, None, fs=fs, inbox_port=FakeInboxPort())
    assert isinstance(service, LinkService)
    assert service.window_mode == "observe"
    close_link_service(app)


def test_h1_unset_switch_does_not_self_lock_on_an_offline_peers_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E2E-001 phase2: the default switch must not hold a cwnd=1 sender.

    The peer is offline, so the sender's first request is released and never
    acked: it stays in flight against the sender's own cwnd.  The legacy
    decision table counts that own row and holds the next send with
    ``LINK_WINDOW_FULL``/``cwnd``.  No switch is set here (no settings.json,
    no ``HYPRIAL_LINKS_WINDOW_MODE``), so only the daemon default decides.
    """

    monkeypatch.delenv("HYPRIAL_LINKS_WINDOW_MODE", raising=False)
    fs = _org_fs()
    app = stub_app(tmp_path, fs=fs)
    clock = FakeClock()
    service = compose_link_service(
        app, None, fs=fs, inbox_port=FakeInboxPort(), clock_ms=clock
    )
    assert isinstance(service, LinkService)
    establish(fs, A, B, now_ms=clock())
    clock.advance(1)
    service.tick(clock())
    first = service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=clock()
    )
    assert not isinstance(first, Hold), first
    service.submitted(
        sender=A, target=B, message_id="m1", reply_to=None, hold_reason=None,
        now_ms=clock(),
    )
    with service._mem:
        assert [item.message_id for item in service._states[A].outbound] == ["m1"]
    second = service.admit_send(
        sender=A, target=B, intent="request", reply_to=None, now_ms=clock()
    )
    assert second != Hold(ipc_errors.LINK_WINDOW_FULL, B, "cwnd"), second
    assert not isinstance(second, Hold), second
    close_link_service(app)


def test_a_hosted_agent_is_local_on_the_first_check_after_composition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restart path: the registry is built in DaemonApplication.__init__ and
    bound to app.agents before any startup phase, so compose_link_service
    already reads it.  The first send after a restart must see the hosted
    agent as local, not CALLER_NOT_AUTHORIZED.
    """

    monkeypatch.setenv(BYPASS_ENV, "0")
    fs = _org_fs()
    hosted = "agent:visitor@example.test:n1:worker"
    app = stub_app(
        tmp_path,
        fs=fs,
        agents=SimpleNamespace(local_actors=lambda: (A, hosted)),
    )
    service = compose_link_service(app, None, fs=fs, inbox_port=FakeInboxPort())
    assert service is not None
    try:
        now = time.time_ns() // 1_000_000
        assert service.admit_send(
            sender=hosted, target=A, intent="request", reply_to=None, now_ms=now
        ) == Mint(peer=A)
    finally:
        close_link_service(app)
