"""Minimal Unix-WebSocket Codex app-server for isolated launch preflight tests."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tomllib
from pathlib import Path

import websockets


def _session_flags(argv: list[str]) -> dict[str, object]:
    """Parse the real ``-c`` pairs the launch put after ``app-server``.

    Codex's app-server applies each ``-c key=value`` as a session-flags layer.
    Echo the actual received values rather than inventing a test-shaped layer.
    """

    pairs: list[str] = []
    for index, argument in enumerate(argv):
        if argument == "-c" and index + 1 < len(argv):
            pairs.append(argv[index + 1])
    if not pairs:
        return {}
    return tomllib.loads("\n".join(pairs))


def _result(method: str, params: dict[str, object]) -> dict[str, object]:
    codex_home = Path(os.environ["CODEX_HOME"])
    config_path = codex_home / "config.toml"
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    session_flags = {
        "allow_login_shell": False,
        "shell_environment_policy": {"inherit": "all"},
        **_session_flags(sys.argv),
    }
    if method == "initialize":
        return {"userAgent": "fake-codex-interactive", "codexHome": str(codex_home)}
    if method == "config/read":
        return {
            "config": config,
            "layers": [
                {
                    "name": {"type": "sessionFlags"},
                    "config": session_flags,
                },
                {
                    "name": {"type": "user", "file": str(config_path)},
                    "config": config,
                },
                {
                    "name": {"type": "system", "file": "/etc/codex/config.toml"},
                    "config": {},
                },
            ],
        }
    if method == "account/read":
        return {"account": None, "requiresOpenaiAuth": False}
    if method == "skills/list":
        return {
            "data": [
                {"cwd": cwd, "skills": [], "errors": []}
                for cwd in params.get("cwds", [])
                if isinstance(cwd, str)
            ]
        }
    return {"data": []}


async def _handle(socket: object) -> None:
    async for raw in socket:  # type: ignore[attr-defined]
        request = json.loads(raw)
        identifier = request.get("id")
        if identifier is None:
            continue
        params = request.get("params")
        response = {
            "id": identifier,
            "result": _result(
                str(request.get("method")), params if isinstance(params, dict) else {}
            ),
        }
        await socket.send(json.dumps(response, separators=(",", ":")))  # type: ignore[attr-defined]


async def _serve(path: Path) -> None:
    async with websockets.unix_serve(_handle, path=str(path)):
        await asyncio.Future()


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] != "app-server":
        raise SystemExit(17)
    listen = sys.argv[sys.argv.index("--listen") + 1]
    prefix = "unix://"
    if not listen.startswith(prefix):
        raise SystemExit(2)
    asyncio.run(_serve(Path(listen.removeprefix(prefix))))


if __name__ == "__main__":
    main()
