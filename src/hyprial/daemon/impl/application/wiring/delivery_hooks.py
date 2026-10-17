"""Delivery intercept wiring (unified-hooks H3 + the H2 recap seam)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from hyprial.daemon.impl.hooks import (
    DeliveryHookService,
    consumer_intercept_context,
    harness_supports_delivery_intercept,
)
from hyprial.kernel import HookInterceptPort, parse_agent_uri

if TYPE_CHECKING:
    from hyprial.daemon.impl.hooks import DaemonHookService


def wire_delivery_hooks(
    *,
    daemon_hooks: "DaemonHookService",
    hook_invoker: Any,
    hook_consumers: Mapping[str, object] | None,
    node_id: str,
    desired_state: Any,
    logger: Any,
) -> DeliveryHookService:
    """Compose the delivery intercept service over the daemon hook bus.

    ``delivery_intercept_supported`` keeps the recipient's declared headless
    mechanism in charge of whether prompt context can be injected at all;
    ``consumer_intercept_context`` adapts the shell-registered ``recap``
    consumer's ``recent_recaps`` serving into each intercept request
    (empty context, and fail-open, when no consumer serves it).
    """

    def delivery_intercept_supported(actor: str) -> bool:
        parsed = parse_agent_uri(actor)
        if parsed is None or parsed[1] != node_id:
            return True
        spec = next(
            (
                item
                for item in desired_state.load().harnesses
                if item.name == parsed[2]
            ),
            None,
        )
        if spec is None:
            return True
        return harness_supports_delivery_intercept(spec.harness)

    return DeliveryHookService(
        hooks=daemon_hooks,
        port=HookInterceptPort(invoke=hook_invoker.invoke, logger=logger),
        supports_injection=delivery_intercept_supported,
        intercept_context=consumer_intercept_context(hook_consumers),
        logger=logger,
    )


__all__ = ["wire_delivery_hooks"]
