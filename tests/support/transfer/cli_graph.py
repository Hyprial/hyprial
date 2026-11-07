"""Stable CLI contract observations, captured before the transfer extraction.

R1 moved the family under ``agent``: ``agent move`` is the operator entry
point, and the protocol-level commands live in the hidden
``agent transfer`` group.  Routes are recorded as full paths from the root so
a future relocation cannot pass unnoticed.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from typing import get_type_hints

from typer.main import get_command


def _row(route, leaf):
    callback = leaf.callback
    while hasattr(callback, "__wrapped__"):
        callback = callback.__wrapped__
    hints = get_type_hints(callback)
    return {
        "route": route, "hidden": leaf.hidden, "help": leaf.help,
        "callback": callback.__name__,
        "signature": [
            [name, str(parameter.kind), str(hints.get(name, parameter.annotation)),
             type(parameter.default).__name__]
            for name, parameter in inspect.signature(callback).parameters.items()
        ],
        "parameters": [{
            "name": p.name, "kind": type(p).__name__,
            "opts": p.opts, "secondary_opts": p.secondary_opts,
            "default": str(p.default) if isinstance(p.default, Path) else p.default,
            "required": p.required, "nargs": p.nargs, "multiple": p.multiple,
            "type": type(p.type).__name__, "type_name": p.type.name,
            "path_type": getattr(getattr(p.type, "type", None), "__name__", None),
            "is_flag": getattr(p, "is_flag", None),
            "flag_value": getattr(p, "flag_value", None),
            "help": getattr(p, "help", None), "hidden": getattr(p, "hidden", None),
        } for p in leaf.params],
    }


def transfer_command_graph(cli):
    root = get_command(cli.app)
    agent = root.commands["agent"]
    rows = [_row("agent move", agent.commands["move"])]
    transfer_group = agent.commands["transfer"]
    for name, leaf in transfer_group.commands.items():
        if hasattr(leaf, "commands"):
            for child, sub in leaf.commands.items():
                rows.append(_row(f"agent transfer {name} {child}", sub))
        else:
            rows.append(_row(f"agent transfer {name}", leaf))
    return rows
