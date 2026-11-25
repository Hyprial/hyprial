# Decision notes (phase B, S10; R2 2026-11-19):
# - The user:<owner> zenoh request/receipt transport for another owner's
#   person, the receiver that hands it to that machine's user-proxy, its
#   idempotency ledger and actors.  R2 removed the squire DM and the adapter
#   registry it used.
# - It stays under adapters, not under daemon/impl/delivery, whose modules
#   import only the stdlib and the kernel.  Its own boundary row,
#   intra/user-delivery-isolation, pins that it never loads orgfs, cells,
#   conversations or threads, so it keeps working while OrgFS is down.
"""Receiver-owned ``user:<owner>`` delivery to the owner's user-proxy."""

from hyprial.daemon.impl.adapters.user_delivery.receiver import (
    ReceiverUserDelivery,
    UserDeliveryLedger,
    UserDeliveryPort,
    UserDeliveryRequest,
    UserDeliveryResult,
    UserDeliveryTarget,
    ZenohUserDeliveryEndpoint,
    ZenohUserDeliveryTransport,
    display_sender,
    is_user_target,
)

__all__ = [
    "ReceiverUserDelivery",
    "UserDeliveryLedger",
    "UserDeliveryPort",
    "UserDeliveryRequest",
    "UserDeliveryResult",
    "UserDeliveryTarget",
    "ZenohUserDeliveryEndpoint",
    "ZenohUserDeliveryTransport",
    "display_sender",
    "is_user_target",
]
