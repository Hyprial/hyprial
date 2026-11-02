"""Render the CLI inventory from Typer's registered command tree."""

from __future__ import annotations

from dataclasses import dataclass
import inspect

import typer
from typer._click import Context
from typer.core import TyperGroup
from typer.main import get_command


@dataclass(frozen=True)
class TopLevelCommand:
    """One command registered directly under the ``hyprial`` root."""

    name: str
    hidden: bool


@dataclass(frozen=True)
class CommandParameter:
    """The stable, operator-visible shape of one registered option/argument."""

    opts: tuple[str, ...]
    secondary_opts: tuple[str, ...]
    required: bool
    is_flag: bool
    multiple: bool
    hidden: bool
    type: str
    help: str | None
    nargs: int
    metavar: str | None
    default: object
    choices: tuple[object, ...]
    minimum: object
    maximum: object


@dataclass(frozen=True)
class CommandTreeEntry:
    """One registered node (group or leaf) below the ``hyprial`` root."""

    path: str
    kind: str
    hidden: bool
    direct_hidden: bool
    executable: bool
    callback_module: str | None
    help: str | None
    params: tuple[CommandParameter, ...]


def _parameter_shape(param: object) -> CommandParameter:
    param_type = getattr(param, "type", None)
    type_name = getattr(param_type, "name", None) or type(param_type).__name__
    choices = tuple(getattr(param_type, "choices", ()) or ())
    return CommandParameter(
        opts=tuple(getattr(param, "opts", ())),
        secondary_opts=tuple(getattr(param, "secondary_opts", ())),
        required=bool(getattr(param, "required", False)),
        is_flag=bool(getattr(param, "is_flag", False)),
        multiple=bool(getattr(param, "multiple", False)),
        hidden=bool(getattr(param, "hidden", False)),
        type=str(type_name),
        help=getattr(param, "help", None),
        nargs=int(getattr(param, "nargs", 1) or 1),
        metavar=getattr(param, "metavar", None),
        default=getattr(param, "default", None),
        choices=choices,
        minimum=getattr(param_type, "min", None),
        maximum=getattr(param_type, "max", None),
    )


def _callback_module(command: object) -> str | None:
    callback = getattr(command, "callback", None)
    if callback is None:
        return None
    callback = getattr(callback, "func", callback)
    callback = inspect.unwrap(callback)
    module = getattr(callback, "__module__", None)
    if module:
        return str(module)
    return f"{type(callback).__module__}.{type(callback).__name__}"


def registered_root_parameters(typer_app: typer.Typer) -> tuple[CommandParameter, ...]:
    """Return the parameters declared directly on the ``hyprial`` root."""

    return tuple(_parameter_shape(param) for param in getattr(get_command(typer_app), "params", ()))


def registered_top_level(typer_app: typer.Typer) -> tuple[TopLevelCommand, ...]:
    """Return root commands in Typer's registration order."""

    root = get_command(typer_app)
    context = Context(root, info_name="hyprial")
    commands: list[TopLevelCommand] = []
    for name in root.list_commands(context):
        command = root.get_command(context, name)
        if command is not None:
            commands.append(TopLevelCommand(name=name, hidden=bool(command.hidden)))
    return tuple(commands)


def registered_command_tree(typer_app: typer.Typer) -> tuple[CommandTreeEntry, ...]:
    """Return every registered node in Typer's registration order.

    A hidden parent hides its whole subtree.  ``executable`` is true for a leaf
    and for a group whose callback runs without a subcommand; plain groups only
    dispatch.  Parameters keep click's declaration order so a generated
    snapshot can be compared byte-for-byte.
    """

    root = get_command(typer_app)
    context = Context(root, info_name="hyprial")
    entries: list[CommandTreeEntry] = []

    def walk(command: object, prefix: tuple[str, ...], hidden_parent: bool) -> None:
        for name in command.list_commands(context):  # type: ignore[attr-defined]
            child = command.get_command(context, name)  # type: ignore[attr-defined]
            if child is None:
                continue
            path = (*prefix, name)
            is_group = isinstance(child, TyperGroup)
            children = getattr(child, "commands", None) if is_group else None
            direct_hidden = bool(getattr(child, "hidden", False))
            hidden = hidden_parent or direct_hidden
            entries.append(
                CommandTreeEntry(
                    path=" ".join(path),
                    kind="group" if is_group else "leaf",
                    hidden=hidden,
                    direct_hidden=direct_hidden,
                    executable=(
                        bool(getattr(child, "invoke_without_command", False))
                        if is_group
                        else True
                    ),
                    callback_module=_callback_module(child),
                    help=getattr(child, "help", None),
                    params=tuple(
                        _parameter_shape(param)
                        for param in getattr(child, "params", ())
                    ),
                )
            )
            if children:
                walk(child, path, hidden)

    walk(root, (), False)
    return tuple(entries)


def render_top_level_help(typer_app: typer.Typer) -> str:
    """Build ``hyprial help`` without maintaining a second command snapshot."""

    inventory = registered_top_level(typer_app)
    visible = tuple(item.name for item in inventory if not item.hidden)
    hidden = tuple(item.name for item in inventory if item.hidden)

    lines = [
        "Harness Bridge",
        "",
        "Usage:",
        "  hyprial COMMAND",
        "",
        "Commands:",
        *(f"  hyprial {name}" for name in visible),
    ]
    if hidden:
        lines.extend(
            [
                "",
                "Hidden commands (internal):",
                *(f"  hyprial {name}" for name in hidden),
            ]
        )
    return "\n".join(lines)
