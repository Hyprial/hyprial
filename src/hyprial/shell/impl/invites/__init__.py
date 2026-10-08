"""Device-side invite pickup (tailnet cutover §5.3).

Shell-side by ruling (T11): the daemon never reads ``secrets/login.json``,
so reading the Casdoor ``hyprialInvites`` account property and joining
each invite happens here; the join itself is delegated to the daemon over
IPC (``org.join``).
"""

from hyprial.shell.impl.invites.casdoor import (
    CasdoorAccountClient,
    InvitePickupError,
)
from hyprial.shell.impl.invites.pending import run_pending

__all__ = ["CasdoorAccountClient", "InvitePickupError", "run_pending"]
