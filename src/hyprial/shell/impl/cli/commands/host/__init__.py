"""The ``hyprial host`` command family."""

import typer


host_app = typer.Typer(
    help="Discover hosts on the network and register machines as move targets."
)
