"""Fixed zenoh slot pool: endpoints, persisted assignment, bind/unbind.

Public surface of the stable-zenoh-session-slots design (§2/§5) on the
daemon side.  The S2b peer-access actor hooks ``bind_peer_slot`` /
``unbind_peer_slot`` (or the idempotent ``reconcile_slot_bindings``)
after updating ``allow``; everything else is wiring detail.
"""

from .reconcile import (
    bind_peer_slot,
    reconcile_slot_bindings,
    slot_store_for_app,
    unbind_peer_slot,
)
from .store import (
    DEFAULT_SLOT_POOL_SIZE,
    SLOT_POOL_ENV,
    SLOT_TRANSPORT_ENV,
    SLOTS_DIR_RELPATH,
    SLOTS_STATE_RELPATH,
    TCP_SLOT_GROW_SPEC,
    SlotAssignments,
    ensure_slots_dir,
    slot_endpoint,
    slot_endpoints,
    slot_locator,
    slot_pool_size,
    slot_socket_path,
    slot_socket_paths,
    slot_specs,
    slot_transport,
    slots_dir,
)

__all__ = [
    "DEFAULT_SLOT_POOL_SIZE",
    "SLOT_POOL_ENV",
    "SLOT_TRANSPORT_ENV",
    "SLOTS_DIR_RELPATH",
    "SLOTS_STATE_RELPATH",
    "TCP_SLOT_GROW_SPEC",
    "SlotAssignments",
    "bind_peer_slot",
    "ensure_slots_dir",
    "reconcile_slot_bindings",
    "slot_endpoint",
    "slot_endpoints",
    "slot_locator",
    "slot_pool_size",
    "slot_socket_path",
    "slot_socket_paths",
    "slot_specs",
    "slot_store_for_app",
    "slot_transport",
    "slots_dir",
    "unbind_peer_slot",
]
