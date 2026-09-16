"""Release-artifact application installation for ``hyprial install``."""

from .core import (
    InstallError,
    install_application,
    launch_application,
    prepare_application_launch,
    upgrade_application,
)

__all__ = [
    "InstallError",
    "install_application",
    "launch_application",
    "prepare_application_launch",
    "upgrade_application",
]
