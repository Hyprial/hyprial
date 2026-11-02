"""Dependency-free constants shared by forwarding configuration and runtime."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


# The port every hyprial node listens on. A well-known port lets a directory of
# hosts become a list of endpoints without a second lookup.
DEFAULT_PEER_PORT = 7447

FORWARDING_COMMAND_ENV = "HYPRIAL_FORWARDING_COMMAND"
FORWARDING_UP_ENV = "HYPRIAL_FORWARDING_UP"

SERVICE_CONTROL_TIMEOUT_SECONDS = 3.0
SERVICE_REGISTRY_READ_TIMEOUT_SECONDS = 1.0
SERVICE_RECONCILE_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class MembershipBudget:
    seconds: float
    legs: tuple[str, ...]


# Each operation has one shared deadline (`seconds`); the per-leg entries are
# independent caps under that deadline, not slices that add up to it. The
# composite values (join=105, join-ipc=110, join-cli=120) are hand-set totals.
MEMBERSHIP_BUDGETS: Mapping[str, MembershipBudget] = MappingProxyType({
    "membership-init": MembershipBudget(10.0, ("membership-init",)),
    "membership-status": MembershipBudget(10.0, ("membership-status",)),
    "invite-create": MembershipBudget(10.0, ("invite-create",)),
    "authority-repin": MembershipBudget(10.0, ("authority-repin",)),
    "invite-redeem": MembershipBudget(65.0, ("invite-redeem", "roster-fetch")),
    "roster-fetch": MembershipBudget(35.0, ("roster-fetch",)),
    "member-announce": MembershipBudget(35.0, ("member-announce",)),
    "member-revoke": MembershipBudget(35.0, ("member-revoke",)),
    "join": MembershipBudget(105.0, ("invite-redeem", "member-announce", "bootstrap")),
    "join-ipc": MembershipBudget(110.0, ("join",)),
    "join-cli": MembershipBudget(120.0, ("join",)),
})
MEMBERSHIP_REPLY_MARGIN_SECONDS = 1.0
