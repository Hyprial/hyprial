from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from hyprial.daemon.impl.application import DaemonApplication


def test_daemon_wraps_serve_and_checkout_results_as_orgfs_json() -> None:
    @dataclass(frozen=True)
    class Result:
        operation: str
        values: tuple[str, ...]

    runtime = SimpleNamespace(
        facade=SimpleNamespace(spaces=lambda: ()),
        serve=lambda space_id, backend: Result(
            f"serve:{space_id}:{backend}", ("resident",)
        ),
        checkout=lambda space_id, enabled: Result(
            f"checkout:{space_id}:{enabled}", ("projected",)
        ),
    )
    application = object.__new__(DaemonApplication)
    application._orgfs_runtime = runtime

    assert application._handle_orgfs(
        "orgfs.serve", {"spaceId": "space-1", "backend": "memory"}
    ) == {"operation": "serve:space-1:memory", "values": ["resident"]}
    assert application._handle_orgfs(
        "orgfs.checkout", {"spaceId": "space-1", "enabled": True}
    ) == {"operation": "checkout:space-1:True", "values": ["projected"]}


def test_daemon_routes_local_space_purge_without_content_plan_semantics() -> None:
    calls: list[str] = []
    runtime = SimpleNamespace(
        facade=SimpleNamespace(spaces=lambda: ()),
        purge_space=lambda space_id: (
            calls.append(space_id)
            or {"spaceId": space_id, "purged": True, "localOnly": True}
        ),
    )
    application = object.__new__(DaemonApplication)
    application._orgfs_runtime = runtime

    assert application._handle_orgfs(
        "orgfs.space_purge", {"spaceId": "space-1"}
    ) == {"spaceId": "space-1", "purged": True, "localOnly": True}
    assert calls == ["space-1"]
