"""Shared runtime composition probe for addressing and startup tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _deep_module_daemon_dependencies,
)


def _probe(monkeypatch: Any, tmp_path: Path) -> tuple[Any, dict[str, object], None]:
    from hyprial.daemon.impl.application import DaemonApplication
    from hyprial.daemon.impl.application.netendpoints import (
        endpoints as transport_owner,
    )
    from hyprial.daemon.impl.application.wiring import transport as wiring
    from tests.daemon.progress import test_progress_end_to_end as probe

    captured: dict[str, object] = {}

    class RecordingCoordinator(probe._CompositionInboxCoordinator):
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured["service_options"] = kwargs.get("service_options")
            super().__init__(*args, **kwargs)

    class RecordingRuntime(probe._CompositionRuntime):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)

    monkeypatch.setattr(transport_owner, "ZenohTransport", probe._CompositionTransport)
    monkeypatch.setattr(
        transport_owner,
        "TransportSessionAuthority",
        lambda session, *, actor_host=None: session,
    )
    monkeypatch.setattr(wiring, "LivelinessDirectory", probe._CompositionDirectory)
    monkeypatch.setattr(
        "hyprial.daemon.impl.inbox.network.ZenohDeliveryTransport",
        probe._CompositionDelivery,
    )
    monkeypatch.setattr(
        wiring, "LocalFirstDeliveryTransport", probe._CompositionDelivery
    )
    monkeypatch.setattr(wiring, "LarkReplyBridgeTransport", probe._CompositionBridge)
    monkeypatch.setattr(wiring, "DeliveryCustodyCoordinator", RecordingCoordinator)
    monkeypatch.setattr(
        wiring,
        "DeliveryCustodyFacade",
        lambda coordinator, _database: coordinator.inbox,
    )
    monkeypatch.setattr(wiring, "AdapterRuntime", probe._CompositionAdapters)
    monkeypatch.setattr(wiring, "HarnessRuntimeActor", probe._CompositionHarnesses)
    monkeypatch.setattr(
        wiring, "HarnessPortClient", lambda actor, _events, *, actor_host: actor
    )
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

    base = Path(tmp_path)
    app = DaemonApplication(
        state_dir=base / "state",
        socket_path=base / "state" / "daemon.sock",
        node_id="node-1",
        hyprial_home=base / "home",
        owner="op",
        **_deep_module_daemon_dependencies(),
    )
    return app, captured, None
