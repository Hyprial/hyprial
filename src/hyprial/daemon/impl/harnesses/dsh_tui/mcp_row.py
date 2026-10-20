"""The ``@deepseek-ai/dsh-mcp-client`` patch row shared by every dsh launch.

The interactive launcher (``hyprial start dsh``) and the headless SDK launch
(#1582, ``dsh_sdk/mcp_patch.py``) both mount a Hyprial stdio MCP server
through this row; one builder keeps the two from drifting.

Decisions:

- dsh-mcp-client hands ``command`` and ``args`` to the MCP SDK's
  ``StdioClientTransport``, i.e. ``spawn(command, args)``. ``args`` therefore
  must not repeat an interpreter: ``python python -m hyprial.cli`` makes
  Python run its own binary as a script, the server dies at startup, and
  ``failOnStartupError`` drops the plugin without a visible error (measured on
  the real PTY: no ``mcp__harness-bridge__*`` tool, ``/mcp`` empty). The
  builder refuses that shape instead of writing it.
- ``serverName`` is checked against the client's own pattern
  (``^[A-Za-z0-9_-]{1,32}$``) so a bad name fails here, not inside dsh.
- ``failOnStartupError`` defaults to true: a launch without its Harness
  tools must not look healthy.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import PurePath

DSH_MCP_CLIENT_PACKAGE = "@deepseek-ai/dsh-mcp-client"
_SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_INTERPRETER = re.compile(r"^(python|pypy|node)[0-9.]*$")


def dsh_mcp_client_row(
    *,
    row_id: str,
    server_name: str,
    command: str,
    args: Sequence[str],
    env: Mapping[str, str],
    fail_on_startup_error: bool = True,
) -> dict[str, object]:
    """Build one stdio dsh-mcp-client insert row."""

    if _SERVER_NAME.fullmatch(server_name) is None:
        raise ValueError(f"invalid dsh-mcp-client serverName: {server_name!r}")
    if args and (
        args[0] == command or _INTERPRETER.fullmatch(PurePath(args[0]).name)
    ):
        raise ValueError(
            "dsh-mcp-client args repeat the interpreter; spawn(command, args) "
            f"would run {command} {args[0]} ..."
        )
    return {
        "id": row_id,
        "name": DSH_MCP_CLIENT_PACKAGE,
        "config": {
            "transport": "stdio",
            "serverName": server_name,
            "failOnStartupError": fail_on_startup_error,
            "command": command,
            "args": list(args),
            "env": dict(env),
        },
    }
