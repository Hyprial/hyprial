"""Dependency-free constants shared by forwarding configuration and runtime."""

from __future__ import annotations


# The port every hyprial node listens on. A well-known port lets a directory of
# hosts become a list of endpoints without a second lookup.
DEFAULT_PEER_PORT = 7447

FORWARDING_COMMAND_ENV = "HYPRIAL_FORWARDING_COMMAND"
FORWARDING_UP_ENV = "HYPRIAL_FORWARDING_UP"

SERVICE_CONTROL_TIMEOUT_SECONDS = 3.0
SERVICE_REGISTRY_READ_TIMEOUT_SECONDS = 1.0
SERVICE_RECONCILE_INTERVAL_SECONDS = 5.0
