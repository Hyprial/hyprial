"""Bounded, non-secret E2E observations from commands already in the scenario."""

from __future__ import annotations

import json
import re
import time
from collections import deque
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class PresenceTrace:
    def __init__(self, *, capacity: int = 512) -> None:
        if capacity < 1:
            raise ValueError("presence trace capacity must be positive")
        self._started = time.monotonic()
        self._rows: deque[dict[str, object]] = deque(maxlen=capacity)
        self._dropped = 0

    def _append(self, event: str, **fields: object) -> None:
        if len(self._rows) == self._rows.maxlen:
            self._dropped += 1
        self._rows.append(
            {
                "at": datetime.now(UTC).isoformat(),
                "elapsedMs": int((time.monotonic() - self._started) * 1000),
                "event": event,
                **fields,
            }
        )

    def record_cli(self, node: str, command: str, value: Mapping[str, Any]) -> None:
        """Keep only the named presence/status fields; never retain IPC payloads."""

        if command == "hosts":
            source = value.get("hosts")
            hosts = source if isinstance(source, list) else []
            rows = [
                {
                    "nodeId": item["nodeId"] if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", item["nodeId"]) else "<redacted>",
                    "status": item.get("status") if isinstance(item.get("status"), str) and item.get("status") in {"online", "offline"} else None,
                }
                for item in hosts[:32]
                if isinstance(item, dict) and isinstance(item.get("nodeId"), str)
            ]
            self._append(
                "hosts", node=node, hosts=rows,
                truncatedHosts=max(0, len(hosts) - 32),
            )
        elif command == "ps":
            zenoh = value.get("zenoh")
            zenoh = zenoh if isinstance(zenoh, dict) else {}
            self._append("ps.zenoh", node=node, **self._zenoh(zenoh))
            daemon = value.get("daemon")
            if isinstance(daemon, dict):
                self.record_cli(node, "ping", daemon)
        elif command == "ping":
            daemon = value.get("daemon")
            status = daemon if isinstance(daemon, dict) else value
            self._append(
                "ping", node=node,
                running=status.get("running") if type(status.get("running")) is bool else None,
                phase=status.get("phase") if isinstance(status.get("phase"), str) and status.get("phase") in {"restoring", "serving", "reconciled"} else None,
            )

    @staticmethod
    def _zenoh(value: Mapping[str, Any]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key in ("listen", "connect"):
            raw = value.get(key)
            entries = raw if isinstance(raw, list) else []
            # Endpoint text helps distinguish requested links, but never keep
            # URL userinfo or unconstrained diagnostics from the full ps row.
            result[key] = [
                item if isinstance(item, str) and len(item) <= 256
                and re.fullmatch(
                    r"tcp/(?:\[[0-9a-fA-F:]+\]|[A-Za-z0-9_.-]+):[0-9]{1,5}", item
                ) else "<redacted>"
                for item in entries[:8]
            ]
            result[f"truncated{key.title()}"] = max(0, len(entries) - 8)
        return result

    def record_docker_state(self, label: str, states: Mapping[str, object]) -> None:
        self._append("docker-state", label=label, states=dict(states))

    def record_diagnostic_loss(self, stage: str, error: BaseException) -> None:
        self._append("diagnostic-loss", stage=stage, errorType=type(error).__name__)

    def document(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "capacity": self._rows.maxlen,
            "droppedRows": self._dropped,
            "rows": list(self._rows),
        }

    def write(self, path: Path) -> None:
        path.write_text(
            json.dumps(self.document(), sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
