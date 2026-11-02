"""The ``hyprial host`` command family."""

from hyprial.shell.impl.cli.commands.common.groups import group


host_app = group(
    help="Discover hosts on the network and register machines as move targets."
)
