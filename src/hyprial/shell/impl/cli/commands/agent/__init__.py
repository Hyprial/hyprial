"""Agent command family, split by command responsibility."""

from . import (  # noqa: F401
    admin,
    attach,
    cell,
    config_rebind,
    create,
    grant,
    home,
    restore,
    secret,
    teleport,
)

_ORDER = {
    name: index
    for index, name in enumerate(
        (
            "create",
            "attach",
            "detach",
            "list",
            "readiness",
            "ensure-home",
            "start",
            "args",
            "stop",
            "top",
            "teleport",
            "unblock",
            "rename",
            "cell",
            "restore",
            "grant",
            "grants",
            "destroy",
        )
    )
}
admin.agent_app.registered_commands.sort(
    key=lambda command: _ORDER.get(command.name or "", len(_ORDER))
)

