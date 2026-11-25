"""Constructor rollback authority extracted from the application composition root."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class _InitializationMixin:
    """Release already-started authorities after a constructor failure."""

    def _abort_initialization(self) -> None:
        for attribute, close_operation in (
            ("_status_refresh", "close"),
            ("_database_health", "close"),
            ("_maintenance_watchdog", "close"),
            ("_operator_alarm_delivery", "close"),
            ("_inbox_watchdog", "close"),
            ("_quota_watchdog", "close"),
            ("_blocking_failures", "close"),
            ("_restore_policy", "close"),
            ("_agent_session_domains", "close"),
            ("_state_persistence", "close"),
            ("_gateway_logger", "close"),
            ("_logger", "close"),
        ):
            resource = getattr(self, attribute, None)
            if resource is not None:
                try:
                    getattr(resource, close_operation)()
                except BaseException:
                    pass

    def _construct_or_rollback(self, factory: Callable[[], Any]) -> Any:
        try:
            return factory()
        except BaseException:
            self._abort_initialization()
            raise
