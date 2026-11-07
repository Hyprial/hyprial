from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path

from hyprial.daemon.impl.desired_state import DesiredState, ServiceConnection


SERVER_PUBLIC = "nodekey:" + "1" * 64
ADDRESS = "tcGeneratedPrivateAddressForTestsOnly"


def catalog_document(
    *,
    name: str = "api",
    device_id: str = "sub2api",
    remote_port: int = 8080,
    local_port: int | None = 18080,
    protocol: str = "http",
    env: str = "{ANTHROPIC_BASE_URL: 'http://127.0.0.1:{port}'}",
    auth: str = "request an application key separately",
    guide: str = "Use the assigned loopback port.",
) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"deviceId: {device_id}\n"
        f"remotePort: {remote_port}\n"
        f"protocol: {protocol}\n"
        f"localPort: {json.dumps(local_port)}\n"
        "usage: claude-code-baseurl\n"
        f"env: {env}\n"
        f"auth: {json.dumps(auth)}\n"
        "---\n"
        f"{guide}\n"
    )


def write_access(
    path: Path,
    *,
    devices: list[dict[str, object]] | None = None,
    space_id: str | None = None,
    owner: str = "user:owner",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    payload = {
        "schemaVersion": 1,
        "catalogTrust": (
            None if space_id is None else {"spaceId": space_id, "owner": owner}
        ),
        "devices": devices
        if devices is not None
        else [
            {
                "deviceId": "sub2api",
                "serverPublic": SERVER_PUBLIC,
                "address": ADDRESS,
                "generation": 7,
                "ports": [8080],
                "enabled": True,
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def mapping(
    *, name: str = "api", device_id: str = "sub2api", local_port: int = 18080
) -> dict[str, object]:
    return {
        "name": name,
        "deviceId": device_id,
        "remotePort": 8080,
        "recordGeneration": 7,
        "localPort": local_port,
        "activeConnections": 0,
        "path": "unknown",
        "pathDetail": None,
        "observedAtMs": None,
        "lastDialState": "unknown",
        "lastDialMs": None,
        "lastError": None,
        "lastErrorAtMs": None,
    }


class FakeStatePort:
    supports_state_cost_origin = False

    def __init__(self) -> None:
        self.snapshot = DesiredState()
        self.calls: list[tuple[str, object]] = []
        self.fail_actual_port = False

    def load(self) -> DesiredState:
        return DesiredState.from_json(self.snapshot.to_json())

    def set_service_connection(self, name: str, local_port: int) -> DesiredState:
        self.calls.append(("set", (name, local_port)))
        prior_sets = sum(call[0] == "set" for call in self.calls[:-1])
        if self.fail_actual_port and prior_sets > 0:
            raise RuntimeError("persist actual port failed")
        values = {value.name: value for value in self.snapshot.service_connections}
        values[name] = ServiceConnection.from_json(
            {"name": name, "localPort": local_port}
        )
        self.snapshot = replace(
            self.snapshot,
            service_connections=tuple(values[key] for key in sorted(values)),
        )
        return self.load()

    def remove_service_connection(self, name: str) -> DesiredState:
        self.calls.append(("remove", name))
        self.snapshot = replace(
            self.snapshot,
            service_connections=tuple(
                value
                for value in self.snapshot.service_connections
                if value.name != name
            ),
        )
        return self.load()

    def set_service_registry(self, cache) -> DesiredState:
        self.calls.append(("registry", cache))
        self.snapshot = replace(self.snapshot, service_registry=cache)
        return self.load()


class FakeController:
    def __init__(self, *, supported: bool = True, assigned_port: int = 18080) -> None:
        self.supports_service_connect = supported
        self.assigned_port = assigned_port
        self.calls: list[tuple[str, object]] = []
        self.services: dict[str, dict[str, object]] = {}
        self.map_entered = threading.Event()
        self.map_release = threading.Event()
        self.block_map = False
        self.fail_map: Exception | None = None

    def map_service(
        self,
        name: str,
        device_id: str,
        address: str,
        server_public: str,
        remote_port: int,
        record_generation: int,
        local_port: int = 0,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "map",
                (
                    name,
                    device_id,
                    address,
                    server_public,
                    remote_port,
                    record_generation,
                    local_port,
                ),
            )
        )
        self.map_entered.set()
        if self.block_map and not self.map_release.wait(1.0):
            raise TimeoutError("test map barrier expired")
        if self.fail_map is not None:
            raise self.fail_map
        value = mapping(
            name=name,
            device_id=device_id,
            local_port=local_port or self.assigned_port,
        )
        value["remotePort"] = remote_port
        value["recordGeneration"] = record_generation
        self.services[name] = value
        return dict(value)

    def unmap_service(self, name: str) -> bool:
        self.calls.append(("unmap", name))
        return self.services.pop(name, None) is not None

    def service_status(self) -> dict[str, object]:
        self.calls.append(("status", None))
        return {"services": [dict(value) for value in self.services.values()]}


class ManualScheduler:
    def __init__(self) -> None:
        self.callback = None
        self.closed = False

    def schedule(self, key, generation, delay, callback) -> bool:
        if self.closed:
            return False
        self.callback = lambda: callback(generation)
        return True

    def cancel(self, key) -> None:
        self.callback = None

    def shutdown(self, timeout: float) -> bool:
        self.closed = True
        self.callback = None
        return True
