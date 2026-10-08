"""Startup-scoped wiring graph shared between _start_runtime phases."""

from __future__ import annotations

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING
if TYPE_CHECKING:
    pass



@dataclass
class _StartupGraph:
    """Startup-scoped wiring values shared between _start_runtime phases.

    The application remains the sole state owner: every field is a
    collaboration handle composed during startup and either retained on
    the application by ``_startup_compose_runtime_bridge`` or dropped
    when its phase finishes.
    """

    transport: Any
    forwarding_listen: tuple[str, ...]
    directory: Any
    presence: Any
    local_delivery: Any
    delivery: Any
    lark_events: Any
    adapters: Any
    lark_client: Any
    inbox_coordinator: Any
    inbox: Any
    shared_inbox_events: Any
    hold_policy: Any
    turn_hooks: Any
    harnesses: Any
    endpoint: Any = None
    status_endpoint: Any = None
    user_endpoint: Any = None
    user_delivery: Any = None
    org_endpoint: Any = None
    recipient_wakes: Any = None
    stop_recipient_wake_observer: Any = None
    duplicate_watch: Any = None
    actor_token: Any = None
    runtime: Any = None
    recovery: Any = None
