"""Agent command family, split by command responsibility."""

from . import admin, config_rebind, create, grant, home, restore, secret  # noqa: F401

_ORDER = {
    name: index
    for index, name in enumerate(
        (
            "create",
            "host-invite",
            "list",
            "readiness",
            "ensure-home",
            "start",
            "stop",
            "top",
            "move",
            "unblock",
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


def order_agent_commands() -> None:
    """Re-apply the visible order after late registrations (``agent move``)."""

    admin.agent_app.registered_commands.sort(
        key=lambda command: _ORDER.get(command.name or "", len(_ORDER))
    )
