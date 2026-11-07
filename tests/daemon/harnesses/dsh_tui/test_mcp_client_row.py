"""The shared ``@deepseek-ai/dsh-mcp-client`` patch row.

dsh-mcp-client spawns ``command`` with ``args``; the interactive launcher
used to repeat the interpreter in ``args``, so the server never started and
``failOnStartupError`` silently dropped every Harness tool. The row builder is
shared by the interactive launcher and the headless SDK launch (#1582) and
refuses that shape outright.
"""

from __future__ import annotations

import sys

import pytest

from hyprial.daemon.impl.harnesses.dsh_tui import (
    DSH_MCP_CLIENT_PACKAGE,
    dsh_mcp_client_row,
)

ARGS = [
    "-m",
    "hyprial.daemon.impl.mcp.entry",
    "agent-channel",
    "--actor",
    "a",
]


def test_the_row_carries_the_stdio_server_verbatim() -> None:
    row = dsh_mcp_client_row(
        row_id="mcp-harness-bridge",
        server_name="harness-bridge",
        command=sys.executable,
        args=ARGS,
        env={"HYPRIAL_HOME": "/h"},
    )

    assert row == {
        "id": "mcp-harness-bridge",
        "name": DSH_MCP_CLIENT_PACKAGE,
        "config": {
            "transport": "stdio",
            "serverName": "harness-bridge",
            "failOnStartupError": True,
            "command": sys.executable,
            "args": ARGS,
            "env": {"HYPRIAL_HOME": "/h"},
        },
    }


@pytest.mark.parametrize(
    "args",
    [[sys.executable, *ARGS], ["python3", *ARGS]],
)
def test_args_that_repeat_an_interpreter_are_refused(args: list[str]) -> None:
    with pytest.raises(ValueError, match="repeat"):
        dsh_mcp_client_row(
            row_id="r",
            server_name="harness-bridge",
            command=sys.executable,
            args=args,
            env={},
        )


def test_server_names_follow_the_dsh_mcp_client_pattern() -> None:
    with pytest.raises(ValueError, match="serverName"):
        dsh_mcp_client_row(
            row_id="r", server_name="has space", command="x", args=[], env={}
        )
