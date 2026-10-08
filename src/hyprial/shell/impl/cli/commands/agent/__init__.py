"""Agent command family, split by command responsibility."""

from . import admin, create, grant, home, restore, secret  # noqa: F401

_ORDER = {
    name: index
    for index, name in enumerate(
        (
            "create", "host-invite", "grant", "revoke", "grants", "list",
            "home-census", "restore-policy", "restore-threshold", "unblock", "destroy",
        )
    )
}
admin.agent_app.registered_commands.sort(
    key=lambda command: _ORDER.get(command.name or "", len(_ORDER))
)
