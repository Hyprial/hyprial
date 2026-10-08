"""Safe human views for the service-connect command results."""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from typing import Any

_UNTRUSTED_CATALOG_LABEL = "Untrusted catalog text (review before use):"


def _row(data: Mapping[str, Any]) -> Mapping[str, Any]:
    value = data.get("service")
    return value if isinstance(value, Mapping) else {}


def _text(value: object, default: str = "-") -> str:
    return default if value is None else str(value)


def _endpoint(row: Mapping[str, Any]) -> str:
    return f"{_text(row.get('deviceId'))}:{_text(row.get('remotePort'))}"


def _local_endpoint(row: Mapping[str, Any]) -> str:
    port = row.get("localPort")
    return f"127.0.0.1:{port}" if type(port) is int else "127.0.0.1:-"


def _path(row: Mapping[str, Any]) -> str:
    value = row.get("path")
    return value if value in {"direct", "relay", "unknown"} else "unknown"


def _env_lines(env: object) -> list[str]:
    if not isinstance(env, Mapping):
        return []
    lines = []
    for key, value in env.items():
        if not isinstance(key, str):
            continue
        lines.append(f"Use:  export {key}={shlex.quote(str(value))}")
    return lines


def render_connect(data: Mapping[str, Any]) -> str:
    """Render a successful mapping without exposing protected access material."""
    row = _row(data)
    name = _text(row.get("name"))
    endpoint = _endpoint(row)
    local = _local_endpoint(row)
    state = row.get("state")
    if state == "failed":
        lines = [f"Failed: {name} ({endpoint}) → http://{local}"]
    else:
        lines = [f"Connected: {name} ({endpoint}) → http://{local}   [{_path(row)}]"]
    lines.extend(_env_lines(data.get("env")))
    if not isinstance(data.get("env"), Mapping) or not data["env"]:
        lines.append(f"Use:  connect to {local}")
    if data.get("auth"):
        lines.append(f"Key:  {data['auth']}")
    lines.append(f"Guide: hyprial service guide {name}")
    return "\n".join(lines)


def _dial(value: object) -> str:
    if type(value) is int or isinstance(value, float):
        return f"{value:g}ms" if isinstance(value, float) else f"{value}ms"
    return "-"


def render_list(data: Mapping[str, Any]) -> str:
    """Render rows with path and TCP dial duration, never implying RTT."""
    rows = data.get("services")
    lines = ["NAME  DEVICE:PORT  LOCAL            PATH     DIAL  STATE     LAST ERROR"]
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            local = _text(row.get("localPort"))
            error = _text(row.get("lastError"), "—")
            lines.append(
                f"{_text(row.get('name')):<5} "
                f"{_endpoint(row):<12} "
                f"127.0.0.1:{local:<13} "
                f"{_path(row):<8} "
                f"{_dial(row.get('lastDialMs')):<5} "
                f"{_text(row.get('state')):<9} "
                f"{error}"
            )
    if len(lines) == 1:
        lines.append("(none)")
    registry = data.get("registry")
    if isinstance(registry, Mapping):
        source = registry.get("source")
        if source is not None:
            lines.append(f"source: {source}")
        if registry.get("error"):
            lines.append(f"Warning: {registry['error']}")
    return "\n".join(lines)


def render_guide(data: Mapping[str, Any], *, full: bool = False) -> str:
    """Render either the short guide body or all safe catalog hints."""
    entry = data.get("entry")
    if not isinstance(entry, Mapping):
        return "-"
    if not full:
        return f"{_UNTRUSTED_CATALOG_LABEL}\n{_text(entry.get('guide'))}"
    lines = [
        _UNTRUSTED_CATALOG_LABEL,
        f"source: {_text(data.get('source'))}",
        f"name: {_text(entry.get('name'))}",
        f"deviceId: {_text(entry.get('deviceId'))}",
        f"remotePort: {_text(entry.get('remotePort'))}",
        f"protocol: {_text(entry.get('protocol'))}",
        f"localPort: {_text(entry.get('localPort'))}",
        f"usage: {_text(entry.get('usage'))}",
    ]
    env = entry.get("env")
    if isinstance(env, Mapping) and env:
        lines.append("env:")
        lines.extend(f"  {key}={value}" for key, value in env.items())
    lines.append(f"auth: {_text(entry.get('auth'))}")
    lines.extend(["", _text(entry.get("guide"))])
    return "\n".join(lines)


def render_disconnect(data: Mapping[str, Any]) -> str:
    name = _text(data.get("name"))
    return f"Disconnected: {name}" if data.get("removed") else f"Not connected: {name}"


__all__ = ["render_connect", "render_disconnect", "render_guide", "render_list"]
